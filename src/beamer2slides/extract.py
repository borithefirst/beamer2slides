"""Stage 1: dump each PDF page's text spans, images, drawings and links (raw.json)."""

import dataclasses
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from . import bidi, type3
from .fonts import font_info
from .pdf import NO_OBJECT, OBJ_IMAGE, Char, Document, Page, PdfDocument, char_box
from .pdf.api import Drawing, Link, stretch_across
from .raw_types import (
    DrawingType, PathItem, RawColor, RawDoc, RawDrawing, RawImage, RawLink, RawMark, RawPage, RawSpan,
)
from .typing_compat import assert_never

LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
                           "ﬅ": "st", "ﬆ": "st"})


def _r(values: Iterable[float], nd: int) -> list[float]:
    return [round(float(v), nd) for v in values]


def _hex(rgb: tuple[float, float, float] | None) -> RawColor | None:
    if rgb is None:
        return None
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb[:3])


Point = tuple[float, float]
Box = tuple[float, float, float, float]


def _xy(v: object) -> Point:
    """A path item's point (a pair of numbers, as the backend gives it: a tuple or a list)."""
    if isinstance(v, (tuple, list)) and len(v) == 2:
        x, y = v
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            return x, y
    raise ValueError(f"not a point: {v!r}")


def _box4(v: object) -> Box:
    """A "re" item's rectangle (x0, y0, x1, y1)."""
    if isinstance(v, (tuple, list)) and len(v) == 4:
        x0, y0, x1, y1 = v
        if isinstance(x0, (int, float)) and isinstance(y0, (int, float)) \
                and isinstance(x1, (int, float)) and isinstance(y1, (int, float)):
            return x0, y0, x1, y1
    raise ValueError(f"not a rectangle: {v!r}")


def _quad(v: object) -> tuple[Point, Point, Point, Point]:
    """A "qu" item's corners in drawing order: ul, ll, lr, ur."""
    if isinstance(v, (tuple, list)) and len(v) == 4:
        p0, p1, p2, p3 = v
        return _xy(p0), _xy(p1), _xy(p2), _xy(p3)
    raise ValueError(f"not a quad: {v!r}")


def _points(item: Sequence[object]) -> list[list[float]]:
    op = item[0]
    if op == "re":
        x0, y0, x1, y1 = _box4(item[1])
        return [[x0, y0], [x1, y1]]
    if op == "qu":
        p0, p1, p2, p3 = _quad(item[1])
        return [list(p) for p in (p0, p3, p2, p1)]  # ul, ur, lr, ll
    return [list(_xy(p)) for p in item[1:]]


PATH_ITEMS = 20  # a drawing of more pieces has no path in raw.json


def _path(d: Drawing) -> list[PathItem] | None:
    """Path geometry for small drawings (diagram nodes, lines, arrow tips): [op, [[x, y], ...]]."""
    if len(d["items"]) > PATH_ITEMS:
        return None
    return [(item[0], [[round(x, 2), round(y, 2)] for x, y in _points(item)]) for item in d["items"]]


def _rounded_corners(d: Drawing) -> dict[str, float]:
    """Which bbox corners of a path are drawn with a curve, and the curve's radius."""
    x0, y0, x1, y1 = d["rect"]
    corners: dict[str, float] = {}
    for item in d["items"]:
        if item[0] != "c":
            continue
        p1, p4 = item[1], item[4]
        mx, my = (p1[0] + p4[0]) / 2, (p1[1] + p4[1]) / 2
        key = ("t" if my < (y0 + y1) / 2 else "b") + ("l" if mx < (x0 + x1) / 2 else "r")
        corners[key] = round(max(abs(p4[0] - p1[0]), abs(p4[1] - p1[1])), 2)
    return corners


def _label(label: str | None, index: int) -> str:
    label = label or str(index + 1)
    if label.startswith("<FEFF") and label.endswith(">"):  # raw UTF-16BE hex string
        try:
            label = bytes.fromhex(label[5:-1]).decode("utf-16-be")
        except ValueError:
            pass
    return label


SMALL_CAPS_WIDTH = 0.03  # relative advance difference that marks an alternate glyph
SCALED_SPREAD = 0.01  # letters whose advances all differ from the font's by one factor, within this


def _small_caps(page: Page, chars: list[Char]) -> bool:
    """OpenType small caps (fontspec \\textsc): lowercase letters drawn with an alternate glyph.
    The text layer only says 'metropolis', the page shows METROPOLIS in small capitals. An
    alternate glyph has another advance than the font's default glyph for the letter. Only
    letters set level are judged: a turned glyph's advance is read off the upright box around it
    (`pdf.Char.advance`), longer than the glyph - TikZ's rotate=20 'Tilted' read as small caps."""
    lower = [ch for ch in chars if not ch.synthetic and len(ch.c) == 1 and ch.c.islower() and ch.exact_advance
             and ch.dir[0] > 0.999]
    if len(lower) < 2:
        return False
    alternate = 0
    ratios: dict[str, float] = {}
    for ch, default in zip(lower, page.glyph_widths([(ch.font_id, ch.c, ch.size) for ch in lower])):
        if default and abs(ch.advance - default) > SMALL_CAPS_WIDTH * max(default, 0.01):
            alternate += 1
        if default:
            ratios[ch.c] = ch.advance / max(default, 0.01)
    # Every letter off its default advance by one factor is text scaled across (an included
    # figure stretched wider than tall: 'Likelihood' at 0.95, a legend at 1.162), its glyphs the
    # font's own. An alternate glyph's advance differs letter by letter.
    if len(ratios) >= 2 and max(ratios.values()) <= (1 + SCALED_SPREAD) * min(ratios.values()):
        return False
    return alternate >= 2 and alternate >= 0.7 * len(lower)


# Gaps between glyphs on a line, in ems of the following glyph. TeX output has no space
# characters: word spaces are pen moves. A small move (thin math spaces) becomes a space inside
# the span; a word space ends the span (classify groups words by geometry).
JOIN_GAP = 0.15
WORD_GAP = 0.3
NEW_LINE_GAP = 1.0
BACK_GAP = -0.6
SAME_BASELINE = 0.05
NEW_BASELINE = 0.8
DROPPED_LINE = 0.5  # ems down, with the pen moved back: a new line (see `spans`)
# In a monospaced face a space is a whole advance (0.525 em in CMTT), and listings' default
# columns=fixed spreads each token's glyphs over a basewidth grid wider than that advance: the
# pen moves between two tokens with no space between them ("self" ",", "llama" "-") reach 0.2-0.42
# advances, over JOIN_GAP, while a real space is 1.0 (flexible, verbatim) to 1.6 (fixed). Measured
# on the hunt's listings (r1_ml_v1, r2_code_v1/v4, r4_control_d1): nothing lies between 0.45 and 0.95.
MONO_JOIN_GAP = 0.5  # in advances of the glyph before


def _mono(font: str) -> bool:
    return font_info(font).family == "mono"


# A narrow space TeX sets between two glyphs of one font (a KPI's '18 mo' at 26 pt: 0.14 em; a
# French « guillemet's thin space: 0.12 em) is below JOIN_GAP. Such a gap is a space when the
# glyphs on both sides of it touch their neighbours: a kern or an italic correction is no wider
# than 0.1 em, and letterspaced glyphs (\textls, small caps tracked by microtype) are
# apart all along the word.
NARROW_GAP = 0.11
TOUCHING = 0.06
# Letterspacing (\textls, soul's \so, fontspec LetterSpace): the glyphs of each word stand apart
# by the same tracking (0.1 - 0.3 em), a word gap is that plus a space. With JOIN_GAP and
# WORD_GAP alone every tracked letter gap became a space ('S PA C E D', 'l e s s i s m o r e')
# and the words' own gaps were lost among them. `tracked_gaps` finds such stretches.
TRACK_MIN, TRACK_MAX = 0.08, 0.28
TRACK_BAND = 0.06     # em around the stretch's tracking
TRACK_WORD = 0.15     # em past the tracking that makes a word gap
TRACK_LETTERS = 4     # tracked gaps between letters a stretch needs at least
# Slides has no letter spacing. Joined, 'S P A C E D' (\textls[200], 0.2 em) came out 'SPACED':
# the tracking was gone. Its own space is 0.19 em in Lato and 0.24 in PT Serif at the calibrated
# sizes (calibration/advances.json; the fonts' no-break space is a space's width, not yet probed
# on Slides' renderer), so a stretch tracked by
# this much or more has a no-break space between its letters - nearer the PDF's gaps than none,
# and no line breaks inside its words - and a word gap of a no-break space and a space (the PDF's
# is the tracking plus a space). Tighter tracking (microtype's small caps, 0.11 em) stays joined.
TRACK_SPACED = 0.14
# Tight tracking: text set with negative letterspacing moves its word gaps down by as much. A
# journal article's figure caption placed as a picture (real_pnuc s17, DejaVu Sans at 4.2 pt) has
# every glyph 0.056 em into the one before and word gaps of 0.10 - 0.17 em, under JOIN_GAP: the
# words ran together ('Hazardratios (HRs)forrespiratory'). On such a line a gap is read against
# its stretch's tracking (`tight_tracking`): its letters stood up to 0.11 em past it (an en dash
# before a letter), its word spaces 0.15 em and more. Over the corpus and the built decks every
# other stretch of TIGHT_GAPS glyph gaps or more has its median within 0.01 em of touching
# (kerns are pairs, not lines).
TIGHT = -0.03       # em: a line whose median glyph gap is this far below touching is tracked tight
TIGHT_GAPS = 8      # gaps in text fonts a line needs for its median to say so
TIGHT_OWN = 4       # gaps a stretch of one font on such a line needs to say its own tracking
TIGHT_JOIN = 0.13   # em past the tracking: a word space on a tight line
LETTER_SPACE = " "


def _gaps(chars: list[Char]) -> list[float | None]:
    """For each character, the pen move from the glyph before it in ems, None where it starts
    another line or another font, size or colour."""
    out: list[float | None] = [None]
    for prev, ch in zip(chars, chars[1:]):
        if ch.dir != prev.dir or combining_mark(ch.c) or combining_mark(prev.c) or (ch.font, round(ch.size, 3), ch.color) != (prev.font, round(prev.size, 3), prev.color):
            out.append(None)
            continue
        ux, uy = ch.dir
        px, py = prev.origin[0] + prev.dir[0] * prev.advance, prev.origin[1] + prev.dir[1] * prev.advance
        size = max(ch.size, 0.01)
        gap = ((ch.origin[0] - px) * ux + (ch.origin[1] - py) * uy) / size
        offset = abs((ch.origin[0] - px) * uy - (ch.origin[1] - py) * ux) / size
        out.append(gap if offset < SAME_BASELINE and BACK_GAP < gap < NEW_LINE_GAP else None)
    return out


def tracked_gaps(chars: list[Char], tracks: dict[int, float]) -> dict[int, bool]:
    """Letterspaced stretches: character index -> True where the gap before it is a word gap,
    False where it is the tracking between two letters of a word (never a word space). A stretch is a
    run of glyphs in one font on one line none of which touch (each gap at least TRACK_MIN): the
    tracking (within TRACK_BAND of the median), a kern off it, or a word gap (TRACK_WORD more),
    with at least TRACK_LETTERS tracked gaps between letters. In ordinary words the glyphs touch
    and only word spaces stand apart. Math and monospaced glyphs are left alone. `tracks` gets
    each index's stretch tracking (em)."""
    gaps = _gaps(chars)
    out: dict[int, bool] = {}
    i = 1
    while i < len(chars):
        # (index k, gaps[k]) of a stretch: gaps[k] is the gap between chars[k - 1] and chars[k]
        seg: list[tuple[int, float]] = []
        while i < len(chars) and (g := gaps[i]) is not None and g >= TRACK_MIN:
            seg.append((i, g))
            i += 1
        if not seg:
            i += 1
            continue
        if font_info(chars[seg[0][0]].font).family in ("math", "mono"):
            continue
        near = sorted(g for _, g in seg if g <= TRACK_MAX)
        if len(near) < TRACK_LETTERS:
            continue
        track = near[len(near) // 2]
        letters = [k for k, g in seg if abs(g - track) <= TRACK_BAND
                   and chars[k - 1].c.isalpha() and chars[k].c.isalpha()]
        inner = [k for k, g in seg if g < track + TRACK_WORD]
        # (ordinary words: most inner gaps touch, only the word spaces are near the median)
        if len(letters) < TRACK_LETTERS or len(letters) < 0.6 * len(inner):
            continue
        for k, g in seg:
            out[k] = g >= track + TRACK_WORD
            tracks[k] = track
    return out


def narrow_spaces(chars: list[Char]) -> set[int]:
    """Indices of characters after a narrow space (NARROW_GAP .. JOIN_GAP) between glyphs of one
    text font whose other neighbours touch them, on a stretch of that font where such loose gaps
    are rare (tracked small caps are loose all along, a kern among them touches)."""
    gaps = _gaps(chars)
    segment = [0] * len(gaps)  # which stretch of one font on one line each gap belongs to
    for k in range(1, len(gaps)):
        segment[k] = segment[k - 1] + (gaps[k] is None)
    loose: dict[int, int] = {}
    total: dict[int, int] = {}
    for k, g in enumerate(gaps):
        if g is not None:
            total[segment[k]] = total.get(segment[k], 0) + 1
            loose[segment[k]] = loose.get(segment[k], 0) + (TRACK_MIN <= g < 0.2)
    out = set()
    for k, g in enumerate(gaps):
        if g is None or not NARROW_GAP <= g < JOIN_GAP or chars[k].c == " " or chars[k - 1].c == " " \
                or font_info(chars[k].font).family in ("math", "mono"):
            continue
        before = gaps[k - 1] if k > 0 else None
        after = gaps[k + 1] if k + 1 < len(gaps) else None
        if all(g2 is None or g2 < TOUCHING for g2 in (before, after)) \
                and loose[segment[k]] <= max(2, 0.1 * total[segment[k]]):
            out.add(k)
    return out


def tight_tracking(chars: list[Char]) -> dict[int, float]:
    """Character index -> the tracking (em, below TIGHT) of the tight line it stands on, for the
    gap before it (TIGHT_JOIN). A line - the stretches of text fonts at one baseline, each one
    font on one line (`_gaps`) - is tight when the median of its TIGHT_GAPS or more glyph gaps is
    TIGHT or less; a stretch of TIGHT_OWN gaps or more says its own tracking (a bold label's
    differs), a shorter one takes its line's. Math and monospaced glyphs are left alone."""
    gaps = _gaps(chars)
    stretches: list[list[int]] = []  # indices of glyphs whose gap before them is known
    for k, g in enumerate(gaps):
        if g is None:
            stretches.append([])
        elif stretches:
            stretches[-1].append(k)
    lines: dict[tuple[tuple[float, float], float], list[list[int]]] = {}
    for stretch in stretches:
        if not stretch or font_info(chars[stretch[0]].font).family not in ("sans", "serif"):
            continue
        ch = chars[stretch[0]]
        ux, uy = ch.dir
        across = round((ch.origin[1] * ux - ch.origin[0] * uy) / max(ch.size, 0.01) * 10)  # tenths of an em
        lines.setdefault((ch.dir, across), []).append(stretch)
    out: dict[int, float] = {}
    for found in lines.values():
        line = sorted(g for s in found for k in s if (g := gaps[k]) is not None)
        if len(line) < TIGHT_GAPS or line[len(line) // 2] > TIGHT:
            continue
        for stretch in found:
            own = sorted(g for k in stretch if (g := gaps[k]) is not None)
            track = own[len(own) // 2] if len(own) >= TIGHT_OWN else line[len(line) // 2]
            if track <= TIGHT:
                out.update((k, track) for k in stretch)
    return out


# listings' columns=fixed (its default) in a proportional face - the sans or serif a deck's
# \lstset leaves basicstyle in, Bera Sans, CM sans - puts each token of n characters in a box n
# columns wide and spreads its glyphs evenly over it: the same glue before, between and after
# them, a different glue per token ('data' 0.05 em, 'Stream' 0.08 em on one line), so neither
# word spaces nor tracking read it (r: 'f o r a l l', 's t e p p e r', 'Scanner =new'), and the
# words joined as prose came out 15-30% short with their columns gone. Such a line is a grid: a
# token's box is w + 2g wide (w from its first glyph's origin to its last glyph's advance, g the
# glue between its glyphs), which is n columns, and every stretch of touching tokens sits
# centred over its own columns. `column_grid` finds the pitch from the page's evenly spread
# tokens and the lines whose stretches all sit on it; `spans` joins a stretch's glyphs and splits
# at an empty column, and the raw span says its grid (`columns`), from which classify counts the
# spaces and treats the line as code.
GRID_EM = (0.4, 0.85)    # a column pitch, in em of the glyphs
GRID_GLUE = 0.012        # em: the spread of a token's glue (an inexact advance aside)
GRID_TOUCH = 0.004       # em: glyphs of a token on a grid stand at least this far apart (bold 0.009)
GRID_AGREE = 0.012       # the tokens' pitches agree within this share
GRID_TOKENS = 3          # tokens of three glyphs or more agreeing on a page's pitch
GRID_SPACE = 0.8         # in columns: a gap this wide holds an empty column
GRID_FIT = 0.12          # in columns: how far a stretch may sit off its line's grid
GRID_BOX = 0.04          # in columns: how far a token's box may be off whole columns
GRID_SQUEEZE = 0.001     # em: a squeezed token's glyphs overlap by one glue at least this deep
GRID_STRETCHES = 5       # stretches on the grid that make a line code by themselves
GRID_NEAR = 3.0          # in line sizes: a short line beside a grid line, on its phase, is code
GRID_OUTLIER = 4         # stretches a line beside a grid line needs to have one off its phase


@dataclass(frozen=True, kw_only=True)
class Columns:
    """A line's column grid: the pitch (pt) and the x of one column edge."""
    pitch: float
    edge: float

    def cell(self, x: float) -> float:
        """The column edge nearest `x`."""
        return self.edge + round((x - self.edge) / self.pitch) * self.pitch


def _grid_lines(chars: list[Char]) -> list[list[int]]:
    """Indices of level text drawn left to right on one baseline in one size, in content order
    (a comment far right of its code included): the lines a grid is looked for on."""
    lines: list[list[int]] = []
    for k, ch in enumerate(chars):
        if ch.synthetic or not ch.c.strip() or ch.dir != (1.0, 0.0) or combining_mark(ch.c):
            continue
        if lines:
            prev = chars[lines[-1][-1]]
            # (listings lowers an asterisk 0.2 em in Bera Sans: it is in its column all the same)
            if abs(ch.origin[1] - prev.origin[1]) <= 0.25 * ch.size and abs(ch.size - prev.size) <= 0.02 * ch.size \
                    and ch.origin[0] >= prev.origin[0] + prev.advance - 0.5 * ch.size:
                lines[-1].append(k)
                continue
        lines.append([k])
    return lines


def _tokens(chars: list[Char], line: list[int]) -> list[tuple[float, float]]:
    """(pitch, glue) in pt of each evenly spread token of GRID_TOKENS or more glyphs of one text
    font on a line: its glyphs apart by one glue (GRID_GLUE; one gap may be off where a glyph's
    advance is not the one TeX set), its pitch its box w + 2g over its glyphs."""
    out: list[tuple[float, float]] = []

    def gap(a: Char, b: Char) -> float:
        return b.origin[0] - a.origin[0] - a.advance

    run: list[Char] = []

    def close() -> None:
        if len(run) >= GRID_TOKENS:
            gaps = sorted(gap(a, b) for a, b in zip(run, run[1:]))
            g = gaps[len(gaps) // 2]
            size = run[0].size
            even = sum(abs(x - g) <= GRID_GLUE * size for x in gaps)
            if g >= GRID_TOUCH * size and even >= max(2, len(gaps) - 1):
                w = run[-1].origin[0] + run[-1].advance - run[0].origin[0]
                out.append(((w + 2 * g) / len(run), g))
        run.clear()

    for k in line:
        ch = chars[k]
        if font_info(ch.font).family not in ("sans", "serif"):
            close()
            continue
        if run and (ch.font != run[-1].font or not GRID_TOUCH * ch.size <= gap(run[-1], ch) <= 0.45 * ch.size
                    or len(run) >= 2 and abs(gap(run[-1], ch) - gap(run[0], run[1])) > GRID_GLUE * ch.size):
            close()  # (another token's glue: the next token, touching this one, starts here)
        run.append(ch)
    close()
    return out


def _empty_column(a: Char, b: Char, pitch: float) -> bool:
    """Whether an empty column (a space) lies between two glyphs on a grid (GRID_SPACE): the gap
    between them, and what of a glyph wider than its column ('==') reaches out of it."""
    reach = max(0.0, a.advance - pitch) / 2 + max(0.0, b.advance - pitch) / 2
    return b.origin[0] - a.origin[0] - a.advance + reach >= GRID_SPACE * pitch


def _stretches(chars: list[Char], line: list[int], pitch: float) -> list[list[int]]:
    """A line's glyphs in stretches split at empty columns."""
    out: list[list[int]] = []
    for k in line:
        if out and not _empty_column(chars[out[-1][-1]], chars[k], pitch):
            out[-1].append(k)
            continue
        out.append([k])
    return out


@dataclass(frozen=True, kw_only=True)
class Phase:
    """Where a stretch's first column starts, in columns mod 1: `sure` read off a token at one
    end of it, else the `maybe`s it could be (see `_phase`)."""
    sure: float | None
    maybe: tuple[float, ...]


def _phase(chars: list[Char], stretch: list[int], pitch: float) -> Phase:
    """A stretch's phase. A token's glyphs stand one glue apart and that glue is also before its
    first and after its last glyph, so a stretch opening (or ending) on three glyphs or more one
    glue apart starts (ends) a glue before (after) them, and a lone glyph is centred in its
    column. Otherwise its tokens' glues are unknown: it sits centred over its columns, or starts
    or ends with a glyph alone in its column ('"Content-Type:', each quote its own token)."""
    first, last = chars[stretch[0]], chars[stretch[-1]]
    size = first.size
    glyphs = [chars[k] for k in stretch]
    gaps = [b.origin[0] - a.origin[0] - a.advance for a, b in zip(glyphs, glyphs[1:])]

    def at(x: float) -> float:
        return (x / pitch) % 1.0

    def token(run: list[Char], glue: list[float]) -> float | None:
        """The glue of the token `run` opens with, when its first glyphs - three or more, one
        glue apart - fill whole columns with that glue around them (single glyphs in their own
        columns can stand evenly too: '"f"')."""
        m = 1
        while m < len(glue) and glue[m] >= 0 and abs(glue[m] - glue[0]) <= GRID_GLUE * size:
            m += 1
        for k in range(m + 1, 2, -1):  # (k glyphs: the token may end before the even run does)
            w = max(c.origin[0] + c.advance for c in run[:k]) - min(c.origin[0] for c in run[:k])
            if glue[0] >= 0 and abs(w + 2 * glue[0] - k * pitch) <= GRID_BOX * pitch:
                return glue[0]
        return None
    n = len(stretch)
    if n == 1:
        return Phase(sure=at(first.origin[0] + first.advance / 2 - pitch / 2), maybe=())
    g = token(glyphs, gaps) if n >= 3 else None
    if g is not None:
        return Phase(sure=at(first.origin[0] - g), maybe=())
    g = token(glyphs[::-1], gaps[::-1]) if n >= 3 else None
    if g is not None:
        return Phase(sure=at(last.origin[0] + last.advance + g), maybe=())
    centre = (first.origin[0] + last.origin[0] + last.advance) / 2
    return Phase(sure=None, maybe=(at(centre - n * pitch / 2), at(first.origin[0] + first.advance / 2 - pitch / 2),
                                   at(last.origin[0] + last.advance / 2 + pitch / 2)))


def _touching(chars: list[Char], stretch: list[int], pitch: float) -> bool:
    """Whether glyphs of a stretch touch as a word's do. Not those of a glyph wider than its
    column ('==', 'm' of 'nom'), nor a token squeezed into fewer columns than its glyphs need:
    one glue apart all the same, that glue below nothing ('nom' -0.012 pt; a word's letters
    touch at 0 or kern by a glyph's own amount)."""
    size = chars[stretch[0]].size
    glyphs = [chars[k] for k in stretch]
    gaps = [b.origin[0] - a.origin[0] - a.advance for a, b in zip(glyphs, glyphs[1:])]
    for i, g in enumerate(gaps):
        if g >= GRID_TOUCH * size or max(glyphs[i].advance, glyphs[i + 1].advance) >= 0.95 * pitch:
            continue
        lo = hi = i
        while lo > 0 and abs(gaps[lo - 1] - g) <= GRID_SQUEEZE * size:
            lo -= 1
        while hi + 1 < len(gaps) and abs(gaps[hi + 1] - g) <= GRID_SQUEEZE * size:
            hi += 1
        squeezed = g <= -GRID_SQUEEZE * size and hi > lo \
            and any(c.advance >= 0.95 * pitch for c in glyphs[lo:hi + 2])
        if not squeezed:
            return True
    return False


def _near(p: float, at: float) -> bool:
    return min((p - at) % 1.0, (at - p) % 1.0) <= GRID_FIT


def _on_phase(phases: Sequence[float], at: float) -> bool:
    return all(_near(p, at) for p in phases)


def _fits(phase: Phase, at: float) -> bool:
    """Whether a stretch can sit on a line's phase `at`."""
    return _near(phase.sure, at) if phase.sure is not None else any(_near(m, at) for m in phase.maybe)


def _common_phase(phases: Sequence[Phase]) -> tuple[float, int]:
    """The phase most stretches share (the circular mean of the sure readings near the one most
    others are near, else of the centred readings) and how many stretches are off it by GRID_FIT."""
    sure = [p.sure for p in phases if p.sure is not None] or [p.maybe[0] for p in phases]
    ref = max(sure, key=lambda r: sum(_fits(p, r) for p in phases))
    near = [p for p in sure if _near(p, ref)]
    at = math.atan2(sum(math.sin(2 * math.pi * p) for p in near),
                    sum(math.cos(2 * math.pi * p) for p in near)) / (2 * math.pi) % 1.0
    return at, sum(not _fits(p, at) for p in phases)


@dataclass(frozen=True, kw_only=True)
class _Read:
    """A line read on a pitch: its stretches (a leading line number off the grid left out,
    `numbered`), their phase, how many are read surely on it and how many are off it, and
    whether one of its tokens is spread at the pitch."""
    parts: list[list[int]]
    phase: float
    surely: int
    off: int
    numbered: bool
    at_pitch: bool


def column_grid(chars: list[Char]) -> dict[int, Columns]:
    """Character index -> its line's column grid, for the lines set on one (listings'
    columns=fixed in a proportional face: GRID_EM, above). A page's pitch, per size, is the one
    GRID_TOKENS evenly spread tokens agree on (GRID_AGREE) - with glues of their own: equal glues
    everywhere are letterspacing (\\textls), not a grid; fewer tokens' pitch holds where a line
    has GRID_STRETCHES stretches read surely on it ('data [] a = [] | a : [a]'), or is numbered
    off the grid and spread at the pitch ('1  import java.util.Arrays;'). A line is on it
    when every stretch sits on one phase (GRID_FIT, `_phase`) and no stretch's glyphs touch, and
    it has a token at the pitch or GRID_STRETCHES stretches read surely; a shorter line within
    GRID_NEAR of such a line, on its phase, is too (a lone '}'). A leading number off the grid (a
    listing's line number) stays out.
    Monospaced lines are left to the monospaced path (MONO_JOIN_GAP, classify's code_pitch)."""
    lines = [l for l in _grid_lines(chars)
             if sum(font_info(chars[k].font).family in ("sans", "serif") for k in l) >= 0.5 * len(l)]
    by_size: dict[float, list[tuple[float, float]]] = {}
    tokens = {id(l): _tokens(chars, l) for l in lines}
    for l in lines:
        size = chars[l[0]].size
        by_size.setdefault(round(size, 1), []).extend(
            (p, g) for p, g in tokens[id(l)] if GRID_EM[0] * size <= p <= GRID_EM[1] * size)
    pitches: dict[float, float] = {}
    weak: dict[float, list[float]] = {}  # (pitches fewer tokens say: taken where a line proves one)
    for key, found in by_size.items():
        found.sort()
        best: list[tuple[float, float]] = []
        for i in range(len(found)):
            group = [t for t in found[i:] if t[0] <= found[i][0] * (1 + GRID_AGREE)]
            if len(group) > len(best):
                best = group
        glues = [g for _, g in best]
        if len(best) >= GRID_TOKENS and max(glues) - min(glues) > GRID_GLUE * key:
            pitches[key] = best[len(best) // 2][0]
        elif 0 < len(best) < GRID_TOKENS:
            weak[key] = [p for p, _ in found]
    if not pitches and not weak:
        return {}

    def on_grid(l: list[int], pitch: float) -> _Read | None:
        parts = _stretches(chars, l, pitch)
        numbered = False
        if len(parts) > 1 and all(chars[k].c.isdigit() for k in parts[0]):
            rest, off = _common_phase([_phase(chars, s, pitch) for s in parts[1:]])
            if not off and not _fits(_phase(chars, parts[0], pitch), rest):
                parts = parts[1:]  # a line number set right of its own box, off the grid
                numbered = True
        if any(_touching(chars, s, pitch) for s in parts):
            return None  # touching glyphs: words, not a grid
        phases = [_phase(chars, s, pitch) for s in parts]
        at, off = _common_phase(phases)
        return _Read(parts=parts, phase=at, surely=sum(p.sure is not None and _near(p.sure, at) for p in phases),
                     off=off, numbered=numbered,
                     at_pitch=any(abs(p - pitch) <= GRID_AGREE * pitch for p, _ in tokens[id(l)]))

    for l in lines:
        key = round(chars[l[0]].size, 1)
        if key in pitches:
            continue
        for candidate in weak.get(key, []):
            read = on_grid(l, candidate)
            if read is not None and not read.off and (
                    read.surely >= GRID_STRETCHES or read.numbered and read.at_pitch and len(read.parts) >= 2):
                # (a listing of short tokens: a long line on it proves the pitch, or a numbered one)
                pitches[key] = candidate
                break
    found_lines: list[tuple[list[int], Columns, bool, float]] = []  # (glyphs, grid, sure, baseline)
    for l in lines:
        pitch = pitches.get(round(chars[l[0]].size, 1))
        if pitch is None:
            continue
        read = on_grid(l, pitch)
        if read is None or read.off > (1 if len(read.parts) >= GRID_OUTLIER else 0):
            continue
        # (one stretch off a long line's phase - listings spreading a string's escape its own
        # way - is on the grid beside a line that surely is)
        sure = not read.off and (read.surely >= GRID_STRETCHES or read.at_pitch)
        found_lines.append(([k for s in read.parts for k in s], Columns(pitch=pitch, edge=read.phase * pitch), sure,
                            chars[read.parts[0][0]].origin[1]))
    taken = [f for f in found_lines if f[2]]
    rest = [f for f in found_lines if not f[2]]
    while rest:  # (line by line out from the sure ones: a listing's '}' under a short line)
        near = [f for f in rest if any(
            abs(b - f[3]) <= GRID_NEAR * chars[f[0][0]].size and g.pitch == f[1].pitch
            and _on_phase([(f[1].edge - g.edge) / g.pitch % 1.0], 0.0) for _, g, _, b in taken)]
        if not near:
            break
        taken += near
        rest = [f for f in rest if f not in near]
    return {k: grid for glyphs, grid, _, _ in taken for k in glyphs}


def _drawn_stretches(page: Page, chars: list[Char], ks: list[int]) -> dict[int, float]:
    """How much wider than tall each of these characters' text objects draws them
    (`pdf.api.stretch_across`): a font's width at a character's size is that much short of its
    drawn advance in a picture scaled across, as the backends' own fallback widths are stretched."""
    objects = page.objects()
    out: dict[int, float] = {}
    for k in ks:
        obj = chars[k].obj
        if obj not in out:
            a, b, c, d, _, _ = objects[obj].matrix if 0 <= obj < len(objects) else (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
            out[obj] = stretch_across(a, b, c, d)
    return out


def _accent_overhang(page: Page, chars: list[Char]) -> list[Char]:
    """An accented italic letter's accent can reach past its advance (Calibri Italic's ì), and
    PDFium's loose box - the advance the text page gives - reaches to the ink: the word space
    after it shrank below JOIN_GAP ('yì yuè' -> 'yìyuè'). Such a letter takes the font's width."""
    todo = [k for k, ch in enumerate(chars) if not ch.synthetic and ch.exact_advance and len(ch.c) == 1
            and ch.dir[0] > 0.999 and len(unicodedata.normalize("NFD", ch.c)) > 1 and font_info(ch.font).italic]
    if not todo:
        return chars
    out = list(chars)
    stretch = _drawn_stretches(page, chars, todo)
    for k, font_width in zip(todo, page.glyph_widths([(chars[k].font_id, chars[k].c, chars[k].size) for k in todo])):
        ch = chars[k]
        width = font_width * stretch[ch.obj] if font_width else font_width
        if width and 0.5 * ch.advance < width < ch.advance - 0.02 * ch.size:
            out[k] = dataclasses.replace(ch, advance=width, exact_advance=False, box=char_box(
                ch.origin[0], ch.origin[1], ch.dir[0], ch.dir[1], width, ch.size, ch.ascent, ch.descent))
    return out


# A ligature's advance is PDFium's loose box (`pdf.Char`: one glyph for several characters), which
# in an italic face reaches to the ink of its overhanging last letter: 0.06 - 0.07 em past the
# glyph's advance for fi and ffi, 0.15 - 0.16 for LMSans-Oblique's ff and LinBiolinum Italic's ft
# (the next letter of its word stood that far into it). The word space after \emph{left} then
# measured 0.14 em, under JOIN_GAP ('leftaction', real_beamer-monodromy s15). The sum of its
# letters' widths is the advance within 0.04 em (over the corpus and the built decks every
# letter after an italic ligature in its word stood -0.04 to 0 em from that sum: no space).
LIGATURE_OVERHANG = 0.02  # em: a loose box this much past its letters' widths reaches the ink


def _ligature_overhang(page: Page, chars: list[Char]) -> list[Char]:
    """An italic ligature whose loose box reaches LIGATURE_OVERHANG or more past the widths of
    its letters is as wide as they are (`_accent_overhang` for an accent's overhang)."""
    todo = [(k, letters) for k, ch in enumerate(chars) if not ch.synthetic and ch.exact_advance and ch.dir[0] > 0.999
            and len(letters := unicodedata.normalize("NFKC", ch.c)) >= 2 and letters.isalpha()
            and font_info(ch.font).italic]
    if not todo:
        return chars
    widths = page.glyph_widths([(chars[k].font_id, c, chars[k].size) for k, letters in todo for c in letters])
    out = list(chars)
    stretch = _drawn_stretches(page, chars, [k for k, _ in todo])
    at = 0
    for k, letters in todo:
        mine, at = widths[at:at + len(letters)], at + len(letters)
        ch = chars[k]
        known = [w for w in mine if w]
        width = sum(known) * stretch[ch.obj]
        if len(known) == len(letters) and 0.5 * ch.advance < width <= ch.advance - LIGATURE_OVERHANG * ch.size:
            out[k] = dataclasses.replace(ch, advance=width, exact_advance=False, box=char_box(
                ch.origin[0], ch.origin[1], ch.dir[0], ch.dir[1], width, ch.size, ch.ascent, ch.descent))
    return out


# TeX's italic correction: in math every letter's box is widened by its font's italic correction
# (TFM), a kern the PDF does not draw, so the pen gap after an italic letter is that correction
# plus whatever space TeX set (in text, \/ and \emph/\textit's automatic one before ')' or ';').
# Beamer's sans math draws $f(h)$ with CMSSI10's f, whose correction is 0.21705 em: the gap to '('
# measured 0.21700 em on every built deck (02_math, 13_inline_math, 28_display_math, 29_tikz p17),
# over JOIN_GAP, and became a space ('f (h' in Slides). CMMI's V and Y have 0.222 em, a medium
# space's width. The real spaces after italic letters on the built decks are a space plus the
# correction (CMSSI10 a 0.010 + 0.278 thick before '=', b 0.031 + 0.222 medium before '+') or a
# word space (LMSans9-Oblique 'f' then 'p' at 0.267, f's correction 0.218). So a gap within
# ITALIC_MATCH of the letter's correction is the correction itself and the letter's advance takes
# it; anything else is as before. Over the hunt's 528 PDFs, 228 gaps after a letter of the table
# were its correction to 0.001 em (CMMI10 V before '(' '|' '.', CMSSI9 W before ')', LMSans10 V
# before ',', CMMI8 T before '='); the nearest other gap was 0.007 em off (LMSans10-Oblique 'W h'),
# then 0.026. 0.003 em also keeps CMSSI10's f clear of a fully shrunk word space (0.2222).
# (cm-super's SFSI f, 0.223 - 0.225, is within it: only a justified line shrunk to the limit
# after an italic f without \/ would lose its space.) The table: TFM italic corrections of
# TeX's italic Type 1 faces (OML CMMI/CMMIB, LM's LMMathItalic = lmmi = cmmi metrics; OT1
# CMSSI/CMTI; T1 cm-super SFSI/SFTI, Type 3 ECSI/ECTI; LM LMSans-Oblique/LMRoman-Italic), letters
# whose correction reaches JOIN_GAP - ITALIC_MATCH; smaller ones never made a space. (OpenType
# faces under xelatex/lualatex are not in it: their correction is the glyph's ink past its advance,
# which `Char` does not carry.)
ITALIC_MATCH = 0.003  # em of the italic glyph
ITALIC_CORRECTIONS: dict[str, dict[str, float]] = {
    "CMMI5": {"VY": 0.278, "ΓΥFPTW": 0.174}, "CMMI6": {"VY": 0.259, "ΓΥFPTW": 0.162},
    "CMMI7": {"VY": 0.246, "ΓΥFPTW": 0.154}, "CMMI8": {"VY": 0.236, "ΓΥFPTW": 0.148},
    "CMMI9": {"VY": 0.228}, "CMMI10": {"VY": 0.222}, "CMMI12": {"VY": 0.218},
    "CMMIB5": {"VY": 0.322, "ΓΥFPTW": 0.201}, "CMMIB6": {"VY": 0.3, "ΓΥFPTW": 0.188},
    "CMMIB7": {"VY": 0.284, "ΓΥFPTW": 0.178}, "CMMIB8": {"VY": 0.272, "ΓΥFPTW": 0.17},
    "CMMIB9": {"VY": 0.263, "ΓΥFPTW": 0.164}, "CMMIB10": {"VY": 0.256, "ΓΥFPTW": 0.16},
    "CMSSI8": {"ﬀf": 0.221, "Y": 0.174, "VW": 0.162}, "CMSSI9": {"ﬀf": 0.219, "Y": 0.173, "VW": 0.162},
    "CMSSI10": {"ﬀf": 0.217, "Y": 0.173, "VW": 0.161}, "CMSSI12": {"ﬀf": 0.216, "Y": 0.172, "VW": 0.161},
    "CMSSI17": {"ﬀf": 0.213, "Y": 0.171, "VW": 0.161},
    "CMTI7": {"ﬀf": 0.218, "Y": 0.197, "VW": 0.186, "IX": 0.156, "Ξ": 0.15, "ΠHMNU": 0.148},
    "CMTI8": {"ﬀf": 0.215, "Y": 0.196, "VW": 0.185, "IX": 0.157, "ΠHMNU": 0.154, "Ξ": 0.152},
    "CMTI9": {"ﬀf": 0.213, "Y": 0.194, "VW": 0.184, "ΠHMNU": 0.16, "IX": 0.158, "Ξ": 0.152},
    "CMTI10": {"ﬀf": 0.212, "Y": 0.194, "VW": 0.184, "ΠHMNU": 0.164, "IX": 0.158, "Ξ": 0.153},
    "CMTI12": {"ﬀf": 0.211, "Y": 0.193, "VW": 0.183, "IX": 0.158, "ΠHMNU": 0.157, "Ξ": 0.153},
    "SFSI0800": {"ﬀf": 0.225, "Y": 0.174, "VW": 0.162}, "SFSI0900": {"ﬀf": 0.225, "Y": 0.173, "VW": 0.162},
    "SFSI1000": {"ﬀf": 0.223, "Y": 0.173, "VW": 0.161}, "SFSI1095": {"ﬀf": 0.224, "Y": 0.172, "VW": 0.161},
    "SFSI1200": {"ﬀf": 0.223, "Y": 0.172, "VW": 0.161}, "SFSI1440": {"ﬀf": 0.218, "Y": 0.17, "VW": 0.159},
    "SFSI1728": {"ﬀf": 0.217, "Y": 0.169, "VW": 0.158}, "SFSI2074": {"ﬀf": 0.216, "Y": 0.168, "VW": 0.157},
    "SFSI2488": {"ﬀf": 0.214, "Y": 0.167, "VW": 0.157},
    "SFTI0800": {"ﬀf": 0.215, "Y": 0.195, "VW": 0.185, "IX": 0.157, "HMNU": 0.154},
    "SFTI0900": {"ﬀf": 0.213, "Y": 0.194, "VW": 0.184, "HMNU": 0.16, "IX": 0.158},
    "SFTI1000": {"ﬀf": 0.212, "Y": 0.194, "VW": 0.184, "HMNU": 0.164, "IX": 0.158},
    "SFTI1095": {"ﬀf": 0.212, "Y": 0.194, "VW": 0.183, "HIMNUX": 0.158},
    "SFTI1200": {"ﬀf": 0.211, "Y": 0.193, "VW": 0.183, "IX": 0.158, "HMNU": 0.157},
    "SFTI1440": {"ﬀf": 0.21, "Y": 0.193, "VW": 0.183, "IX": 0.159, "HMNU": 0.154},
    "SFTI1728": {"ﬀf": 0.21, "Y": 0.192, "VW": 0.183, "IX": 0.159, "HMNU": 0.151},
    "SFTI2074": {"ﬀf": 0.209, "Y": 0.192, "VW": 0.183, "IX": 0.159, "HMNU": 0.149, "CKZ": 0.147},
    "SFTI2488": {"ﬀf": 0.209, "Y": 0.192, "VW": 0.182, "IX": 0.159, "CHKMNUZ": 0.147},
    "LMSans8-Oblique": {"ﬀf": 0.219, "Y": 0.171, "VW": 0.159},
    "LMSans9-Oblique": {"ﬀf": 0.218, "Y": 0.172, "VW": 0.161},
    "LMSans10-Oblique": {"ﬀf": 0.217, "Y": 0.172, "V": 0.161, "W": 0.16},
    "LMSans12-Oblique": {"ﬀf": 0.216, "Y": 0.172, "VW": 0.16},
    "LMSans17-Oblique": {"ﬀf": 0.214, "Y": 0.172, "VW": 0.162},
    "LMRoman7-Italic": {"ﬀf": 0.162}, "LMRoman8-Italic": {"ﬀf": 0.171, "Y": 0.152},
    "LMRoman9-Italic": {"ﬀf": 0.172, "Y": 0.156, "VW": 0.15},
    "LMRoman10-Italic": {"ﬀf": 0.173, "Y": 0.158, "VW": 0.153},
    "LMRoman12-Italic": {"ﬀf": 0.173, "Y": 0.161, "VW": 0.153},
}
LM_MATH_ITALIC = re.compile(r"LMMathItalic(\d+)-(Regular|Bold)")
TYPE3_ITALIC = re.compile(r"EC(SI|TI)(\d{4})")


def italic_correction(font: str, c: str) -> float | None:
    """TeX's italic correction of the glyph `c` in `font`, em (`ITALIC_CORRECTIONS`), None when
    the table has none for it."""
    name = font.split("+", 1)[-1]
    if m := LM_MATH_ITALIC.fullmatch(name):
        name = ("CMMI" if m.group(2) == "Regular" else "CMMIB") + m.group(1)
    elif m := TYPE3_ITALIC.fullmatch(name):  # pdflatex's bitmap EC fonts: cm-super's metrics
        name = "SF" + m.group(1) + m.group(2)
    return next((ic for letters, ic in ITALIC_CORRECTIONS.get(name, {}).items() if c in letters), None)


def _italic_corrections(chars: list[Char]) -> list[Char]:
    """An italic letter followed on its line at the distance of its italic correction
    (`ITALIC_MATCH`) is as wide as TeX's box of it: its advance takes the correction, so no
    space is read after it, inside a span or between two."""
    out = list(chars)
    for k, (prev, ch) in enumerate(zip(chars, chars[1:])):
        if prev.synthetic or ch.dir != prev.dir or combining_mark(ch.c) or not ch.c.strip():
            continue
        ic = italic_correction(prev.font, prev.c)
        if ic is None:
            continue
        ux, uy = prev.dir
        px, py = prev.origin[0] + ux * prev.advance, prev.origin[1] + uy * prev.advance
        gap = (ch.origin[0] - px) * ux + (ch.origin[1] - py) * uy
        offset = abs((ch.origin[0] - px) * uy - (ch.origin[1] - py) * ux)
        if offset < SAME_BASELINE * max(ch.size, 0.01) and abs(gap - ic * prev.size) <= ITALIC_MATCH * prev.size:
            width = prev.advance + gap
            out[k] = dataclasses.replace(prev, advance=width, exact_advance=False, box=char_box(
                prev.origin[0], prev.origin[1], ux, uy, width, prev.size, prev.ascent, prev.descent))
    return out


def combining_mark(c: str) -> bool:
    """The character is nothing but combining marks (a macron, an acute): no width of its own."""
    return bool(c) and all(unicodedata.combining(u) for u in c)


#  Where a character is sampled to tell whether it shows: 3 x 3 points across its box. It is
# hidden when most of them are (a word cut at a clip's edge keeps the letters mostly inside).
SAMPLES = (0.2, 0.5, 0.8)
HIDDEN_SAMPLES = 5
CURVE_STEPS = 8
GRID = 24.0  # pt, cells of the index of covering objects


Polygon = list[Point]


def _flatten(items: Sequence[Sequence[object]]) -> list[Polygon]:
    """A filled path's items as closed polygons: chains of items that join end to start (a
    filled subpath is closed whether or not the PDF closes it), curves in straight steps."""
    polys: list[Polygon] = []
    chain: Polygon | None = None  # the subpath being followed
    for item in items:
        op = item[0]
        if op in ("re", "qu"):
            if op == "re":
                x0, y0, x1, y1 = _box4(item[1])
                polys.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
            else:
                polys.append(list(_quad(item[1])))
            chain = None
            continue
        start = _xy(item[1])
        pts: Polygon
        if op == "l":
            pts = [_xy(item[2])]
        else:
            p0, p1, p2, p3 = (_xy(p) for p in item[1:5])
            pts = []
            for k in range(1, CURVE_STEPS + 1):
                t = k / CURVE_STEPS
                u = 1 - t
                x, y = (u ** 3 * p0[i] + 3 * u * u * t * p1[i] + 3 * u * t * t * p2[i] + t ** 3 * p3[i]
                        for i in (0, 1))
                pts.append((x, y))
        if chain is not None and chain[-1] == start:
            chain.extend(pts)
        else:
            chain = [start, *pts]
            polys.append(chain)
    return [p for p in polys if len(p) >= 3]


def _winding(polys: list[Polygon], x: float, y: float, even_odd: bool) -> bool:
    """Whether (x, y) is inside the polygons under the path's fill rule."""
    wind = crossings = 0
    for poly in polys:
        n = len(poly)
        for i in range(n):
            (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
            if (ay <= y) != (by <= y):
                cx = ax + (y - ay) * (bx - ax) / (by - ay)
                if cx > x:
                    crossings += 1
                    wind += 1 if by > ay else -1
    return crossings % 2 == 1 if even_odd else wind != 0


def _in(box: Box, x: float, y: float) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


@dataclass(frozen=True, kw_only=True)
class _Cover:
    """Something opaque painted over what came before it: its object id, its box in page space,
    and what of the box it paints - all of it, the polygons of a filled path (under its fill
    rule), or an image's pixels where nothing is see-through (`Visibility._image_opaque`). A path's
    `items` are flattened into polygons only when a glyph is asked about inside its box
    (`Visibility._polygons`): most fills cover no letter, and flattening every one was a third of
    reading the page's words."""
    obj: int
    box: Box
    paints: Literal["box", "path", "image"]
    items: Sequence[Sequence[object]]
    even_odd: bool


class Sight(Protocol):
    """What `spans` asks of a page's visibility: whether one glyph is hidden (`Visibility`)."""

    def hidden(self, ch: Char) -> bool: ...


class Visibility:
    """Which characters a reader of the page sees. PDFium's text page reports every glyph drawn,
    also those that never show: outside their clip (the other panel of an `\\includegraphics[trim,
    clip]`, a tikz spy's magnified copy, a pgfplots pin beyond the axis), under an opaque fill or
    image painted after them (Boadilla's author box running under the title box, a caption under
    the footline, a legend under a white callout) or at alpha 0 (`opacity=0`). Such a character
    is not text of the slide. It stays in the page's rendering, where it is as hidden as in the
    PDF: render only switches off what classify made native."""

    def __init__(self, page: Page):
        self.page = page
        objects = page.objects()
        self.clips = {po.id: po.clip for po in objects if getattr(po, "clip", None) is not None}
        self.rect = page.rect
        # What paints over what came before it: opaque fills and images, in page space. The clip
        # is known only by its box, which may promise more than the clip path lets through (a
        # mindmap's connection bar is clipped to the space between two circles), so something
        # its clip cuts does not count.
        self.covers: list[_Cover] = []
        for d in page.drawings():
            if d["type"] not in ("f", "fs") or d.get("fill") is None or d.get("soft_mask") \
                    or d.get("fill_opacity", 1.0) < 1.0 or self._cut(d["object"], d["rect"]):
                continue
            items = d["items"]
            box_only = len(items) == 1 and items[0][0] == "re"
            self.covers.append(_Cover(obj=d["object"], box=d["rect"], paints="box" if box_only else "path",
                                      items=[] if box_only else items, even_odd=d.get("even_odd", False)))
        kinds = {po.id: po.type for po in objects}
        for info in page.images():
            if kinds.get(info["object"]) == OBJ_IMAGE:  # its box is what its clips let show
                self.covers.append(_Cover(obj=info["object"], box=info["bbox"], paints="image", items=[],
                                          even_odd=False))
        self._polys: dict[int, list[Polygon]] = {}  # cover index -> its path flattened (`_polygons`)
        self.grid: dict[tuple[int, int], list[int]] = {}
        for k, cover in enumerate(self.covers):
            x0, y0, x1, y1 = cover.box
            if x1 - x0 > 4 * self.rect[2] or y1 - y0 > 4 * self.rect[3]:
                continue  # a runaway coordinate: nothing to index
            for gx in range(math.floor(x0 / GRID), math.floor(x1 / GRID) + 1):
                for gy in range(math.floor(y0 / GRID), math.floor(y1 / GRID) + 1):
                    self.grid.setdefault((gx, gy), []).append(k)
        self._opaque: dict[int, bool] = {}

    def _cut(self, obj: int, rect: Box) -> bool:
        clip = self.clips.get(obj)
        return clip is not None and not (clip[0] <= rect[0] + 0.5 and clip[1] <= rect[1] + 0.5
                                         and clip[2] >= rect[2] - 0.5 and clip[3] >= rect[3] - 0.5)

    def _image_opaque(self, obj: int) -> bool:
        """An image paints over what is under it when nothing in it is see-through and no clip
        cuts it (a clip path is known only by its box)."""
        if obj not in self._opaque:
            im = self.page.embedded_image(obj)
            self._opaque[obj] = im is not None and not (im.transparent or im.blended or im.clipped)
        return self._opaque[obj]

    def _polygons(self, k: int) -> list[Polygon]:
        """The k-th cover's path as polygons (`_flatten`), worked out the first time it is asked."""
        if k not in self._polys:
            self._polys[k] = _flatten(self.covers[k].items)
        return self._polys[k]

    def _covered(self, obj: int, x: float, y: float) -> bool:
        for k in self.grid.get((math.floor(x / GRID), math.floor(y / GRID)), ()):
            cover = self.covers[k]
            if cover.obj <= obj or not _in(cover.box, x, y):
                continue
            if cover.paints == "image":
                if self._image_opaque(cover.obj):
                    return True
            elif cover.paints == "box":
                return True
            elif cover.paints == "path":
                if _winding(self._polygons(k), x, y, cover.even_odd):
                    return True
            else:
                assert_never(cover.paints)
        return False

    def hidden(self, ch: Char) -> bool:
        if ch.synthetic or ch.obj == NO_OBJECT:
            return False
        if ch.alpha == 0:
            return True
        x0, y0, x1, y1 = ch.box
        clip = self.clips.get(ch.obj)
        # Only what lies on the page is judged: a letter the page edge cuts shows its part on
        # the page (an overfull table's "see" and line-end hyphen at the right edge). Counted as
        # hidden, it left the word broken ("se") and a hyphenated word apart ("re source"),
        # while its visible part stayed in the background next to the native box.
        on = hidden = 0
        for fx in SAMPLES:
            for fy in SAMPLES:
                x, y = x0 + (x1 - x0) * fx, y0 + (y1 - y0) * fy
                if not _in(self.rect, x, y):
                    continue
                on += 1
                if clip is not None and not _in(clip, x, y) or self._covered(ch.obj, x, y):
                    hidden += 1
        return not on or hidden * len(SAMPLES) ** 2 >= HIDDEN_SAMPLES * on


MARK_PREFIX = "B2S"  # the marked-content tags slides.sty writes around an adopted element


Marks = tuple[RawMark, ...]
"""The B2S marks around one page object, outermost first."""


def page_marks(page: Page) -> dict[int, Marks]:
    """The B2S marks around each page object that has any, outermost first, as (tag, params):
    slides.sty's own (`adopt.SLIDES_MARKS`), others (a tagged PDF's /P, /Span) left out. A form's
    children are inside the marks around the form, which PDFium lists as the form's only."""
    objects = page.objects()
    found: dict[int, Marks] = {}
    for po in objects:  # a form comes before what it holds
        own: Marks = tuple(m for m in getattr(po, "marks", ()) if m[0].startswith(MARK_PREFIX))
        outer = found.get(po.parent, ()) if po.parent is not None else ()
        if outer or own:
            found[po.id] = outer + own
    return found


def _marks_json(marks: Marks) -> list[RawMark]:
    return [(tag, dict(params)) for tag, params in marks]


@dataclass(frozen=True, kw_only=True)
class PageSpan:
    """A run of glyphs on one line in one font, size and colour (`spans`): its text as drawn, the
    first glyph's style and origin, the box of its glyphs (combining marks left out), the glyphs
    (word spaces made by `spans` among them, `Char.synthetic`) and the B2S marks around it
    (outermost first; none off an adopted page). `columns`: (pitch, x of its first column's left
    edge) when its line is set on a column grid (`column_grid`), else None."""
    text: str
    font: str
    size: float
    color: int
    alpha: int
    origin: tuple[float, float]
    bbox: Box
    dir: tuple[float, float]
    chars: tuple[Char, ...]
    marks: Marks
    columns: tuple[float, float] | None


def shown_spans(page: Page) -> list[PageSpan]:
    """`spans` of the glyphs that show, the page's characters as PDFium reads them (no Type 3 or
    right-to-left reading) and no marks: what a tool looking at one page wants."""
    return spans(page, Visibility(page), False, page.chars(), {})


def spans(page: Page, visibility: Sight, hidden: bool, chars: list[Char],
          marks: dict[int, Marks]) -> list[PageSpan]:
    """Runs of glyphs on one line with the same font, size and colour, split at word gaps. Only
    glyphs that show (`Visibility`), or with `hidden` only those on the page that don't. `chars`:
    the page's characters as read (`page_chars`, or `page.chars()`). `marks` (`page_marks`): a
    span never crosses from one B2S mark to another, and carries its own."""
    out: list[PageSpan] = []
    run: list[Char] = []
    x0, y0, x1, y1 = page.rect
    run_grid: list[Columns] = []  # the column grid of the run's glyphs, when they are on one

    def mark_of(ch: Char) -> Marks:
        return marks.get(ch.obj, ())

    def flush() -> None:
        if run and any(not ch.synthetic for ch in run):
            text = "".join(ch.c for ch in run)
            # A combining mark sits over the letter before it; its own box (PDFium gives it one,
            # advance included) would stretch the span over the space that follows.
            boxed = [ch for ch in run if not combining_mark(ch.c)] or run
            bx0 = min(ch.box[0] for ch in boxed)
            by0 = min(ch.box[1] for ch in boxed)
            bx1 = max(ch.box[2] for ch in boxed)
            by1 = max(ch.box[3] for ch in boxed)
            first = run[0]
            mark: Marks = next((mark_of(ch) for ch in run if not ch.synthetic), ())
            columns = None
            if run_grid:
                # (the stretch sits centred over its columns: GRID_EM)
                grid = run_grid[0]
                columns = (grid.pitch, grid.cell((bx0 + bx1) / 2 - len(boxed) * grid.pitch / 2))
            out.append(PageSpan(text=text, font=first.font, size=first.size, color=first.color, alpha=first.alpha,
                                origin=first.origin, bbox=(bx0, by0, bx1, by1), dir=first.dir, chars=tuple(run),
                                marks=mark, columns=columns))
        run.clear()
        run_grid.clear()

    # characters outside the page (e.g. the cut-off half of a notes-on-second-screen page)
    shown = [ch for ch in chars
             if not (ch.box[2] <= x0 or ch.box[0] >= x1 or ch.box[3] <= y0 or ch.box[1] >= y1)
             and visibility.hidden(ch) == hidden]
    shown = _italic_corrections(_ligature_overhang(page, _accent_overhang(page, shown)))
    tracks: dict[int, float] = {}
    tracked, narrow, tight = tracked_gaps(shown, tracks), narrow_spaces(shown), tight_tracking(shown)
    grid = column_grid(shown)

    def letter_space(at: Char, width: float) -> Char:
        """A no-break space `width` pt wide after the glyph `at` (TRACK_SPACED)."""
        ux, uy = at.dir
        px, py = at.origin[0] + ux * at.advance, at.origin[1] + uy * at.advance
        return Char(c=LETTER_SPACE, font=at.font, size=at.size, color=at.color, alpha=at.alpha, origin=(px, py),
                    box=char_box(px, py, ux, uy, width, at.size, at.ascent, at.descent),
                    dir=at.dir, obj=NO_OBJECT, font_id=at.font_id, advance=width, synthetic=True, ascent=at.ascent,
                    descent=at.descent, exact_advance=True)

    prev: Char | None = None
    last_mark: Marks = ()
    for k, ch in enumerate(shown):
        space = None
        # Another element's (or paragraph's) glyphs: its own span, whatever the gap. A space
        # between the two is read by classify from the gap, as between any spans.
        if marks and not ch.synthetic:
            mark = mark_of(ch)
            if prev is not None and mark != last_mark:
                last_mark = mark
                flush()
                run.append(ch)
                prev = ch
                continue
            last_mark = mark
        on = grid.get(k)
        if run_grid and on is None and combining_mark(ch.c):
            run.append(ch)  # (over the letter before it, in its column)
            continue
        if run_grid and on is not run_grid[0] or on is not None and not run_grid:
            flush()  # a grid line's glyphs are spans of their own
        if on is not None:
            # On a column grid (column_grid): a stretch's glyphs join, an empty column splits
            # (classify counts the spaces from the columns), and so does a change of style.
            if prev is not None and run:
                style = (ch.font, round(ch.size, 3), ch.color, ch.alpha) != \
                    (prev.font, round(prev.size, 3), prev.color, prev.alpha)
                if style or _empty_column(prev, ch, on.pitch):
                    flush()
            run.append(ch)
            run_grid[:] = [on]
            prev = ch
            continue
        if k in tracked and prev is not None and not combining_mark(ch.c):
            # letterspaced: a tracked gap joins, a word gap is a space (classify reads one between
            # spans); a wide tracking is a no-break space between letters, and one more at a word gap
            spaced = tracks[k] >= TRACK_SPACED and prev.c.strip() and ch.c.strip()
            if spaced:
                ux, uy = ch.dir
                gap = (ch.origin[0] - prev.origin[0]) * ux + (ch.origin[1] - prev.origin[1]) * uy - prev.advance
                run.append(letter_space(prev, tracks[k] * ch.size if tracked[k] else max(0.0, gap)))
            if tracked[k]:
                flush()
            run.append(ch)
            prev = ch
            continue
        if k in narrow and prev is not None:
            ux, uy = ch.dir
            px, py = prev.origin[0] + ux * prev.advance, prev.origin[1] + uy * prev.advance
            width = ((ch.origin[0] - px) * ux + (ch.origin[1] - py) * uy)
            run.append(Char(c=" ", font=ch.font, size=ch.size, color=ch.color, alpha=ch.alpha, origin=(px, py),
                            box=char_box(px, py, ux, uy, width, ch.size, ch.ascent, ch.descent),
                            dir=ch.dir, obj=NO_OBJECT, font_id=ch.font_id, advance=width, synthetic=True,
                            ascent=ch.ascent, descent=ch.descent, exact_advance=True))
            run.append(ch)
            prev = ch
            continue
        if prev is not None:
            ux, uy = ch.dir
            px, py = prev.origin[0] + prev.dir[0] * prev.advance, prev.origin[1] + prev.dir[1] * prev.advance
            size = max(ch.size, 0.01)
            gap = ((ch.origin[0] - px) * ux + (ch.origin[1] - py) * uy) / size
            offset = abs((ch.origin[0] - px) * uy - (ch.origin[1] - py) * ux) / size
            style = (ch.font, round(ch.size, 3), ch.color, ch.alpha) != (prev.font, round(prev.size, 3), prev.color, prev.alpha)
            # A glyph back and down by half an em is the next line (\\ with a negative skip stacking
            # 'THE' over 'END' 0.75 em apart): joined, the span read 'THEEND' on one baseline. An
            # accent goes back and up, a TeX under-accent's dot or bar a quarter em down at most.
            dropped = gap < 0 and (ch.origin[1] - py) * ux - (ch.origin[0] - px) * uy > DROPPED_LINE * size
            new_line = ch.dir != prev.dir or offset > NEW_BASELINE or gap > NEW_LINE_GAP or gap < BACK_GAP or dropped
            # (on a tight line a word space is read against the tracking: TIGHT_JOIN)
            join = tight[k] + TIGHT_JOIN if k in tight else JOIN_GAP
            if new_line or gap >= WORD_GAP:
                flush()
            elif gap >= join and offset < SAME_BASELINE and prev.c != " " and ch.c != " " \
                    and not (_mono(prev.font) and _mono(ch.font) and gap * size < MONO_JOIN_GAP * prev.advance):
                if style:
                    flush()
                width = gap * size
                space = Char(c=" ", font=ch.font, size=ch.size, color=ch.color, alpha=ch.alpha, origin=(px, py),
                             box=char_box(px, py, ux, uy, width, ch.size, ch.ascent, ch.descent),
                             dir=ch.dir, obj=NO_OBJECT, font_id=ch.font_id, advance=width, synthetic=True,
                             ascent=ch.ascent, descent=ch.descent, exact_advance=True)
            elif style:
                flush()
        if space:
            run.append(space)
        run.append(ch)
        # A combining mark is drawn over the letter before it, so the pen stays where that letter
        # left it: lualatex sets \={x} as x plus U+0304, and PDFium gives the mark an advance of
        # its own. Counted, it eats the word space that follows ("x̄and").
        prev = prev if prev is not None and combining_mark(ch.c) else ch
    flush()
    return out


def _opacity(v: float | None) -> float:
    """A drawing's fill or stroke opacity; 1 when the backend says nothing (0 is fully transparent)."""
    return 1.0 if v is None else v


def _visible(d: Drawing) -> tuple[Drawing, DrawingType] | None:
    """What of a drawing shows, and so its type: a fill or stroke at opacity 0 (a tikz node drawn
    with opacity=0 on an overlay step) is left out, and a drawing with neither is none."""
    fill = d["type"] in ("f", "fs") and _opacity(d.get("fill_opacity")) > 0
    stroke = d["type"] in ("s", "fs") and _opacity(d.get("stroke_opacity")) > 0
    if fill and stroke:
        return d, "fs"
    if not (fill or stroke):
        return None
    shown: DrawingType = "f" if fill else "s"
    if d["type"] == shown:
        return d, shown
    part = d.copy()
    part["type"] = shown
    if fill:  # (what the stroke alone says)
        part.pop("color", None)
        part.pop("stroke_opacity", None)
        part.pop("width", None)
        part.pop("dash", None)
        part.pop("dash_phase", None)
    else:
        part.pop("fill", None)
        part.pop("fill_opacity", None)
        part.pop("even_odd", None)
    return part, shown


def _shadow_pieces(drawings: list[Drawing]) -> list[tuple[int, int, int, int]]:
    """Beamer's block shadows: a black rectangle under a soft mask whose shadings fade its
    edges, offset right and down from the panel painted over it. The mask contents are not
    page objects, so the visible parts of the shadow (right of and below the panel) are
    reported as shading pieces on whole points, as the shadings themselves would be."""
    pieces: list[tuple[int, int, int, int]] = []
    for i, m in enumerate(drawings):
        if not m.get("soft_mask") or m["type"] != "f":
            continue
        mx0, my0, mx1, my1 = m["rect"]
        for p in drawings[i + 1:]:
            if p["type"] not in ("f", "fs") or p.get("soft_mask") or _opacity(p.get("fill_opacity")) < 1.0:
                continue
            px0, py0, px1, py1 = p["rect"]
            dx, dy = mx1 - px1, my1 - py1
            if 0.5 <= dx <= 8 and abs(dx - dy) <= 0.25 and abs(mx0 - px0 - dx) <= 0.25 and my0 < py1 - dy:
                pieces.append((math.floor(px1), math.floor(my0), math.ceil(mx1), math.ceil(my1)))
                pieces.append((math.floor(mx0), math.floor(py1), math.ceil(mx1), math.ceil(my1)))
                break
    return pieces


def page_chars(page: Page) -> tuple[list[Char], set[int]]:
    """The page's characters, those in bitmap TeX fonts (Type 3: pdflatex without cm-super) read
    as their encoding says and named for their TeX font (`type3`), right-to-left lines as the page
    draws them (`bidi.visual_chars`), and the font ids read as TeX fonts."""
    chars = page.chars()
    found = type3.page_fonts(chars)
    return bidi.visual_chars(type3.decode(chars, found) if found else chars), set(found)


class Reading:
    """A PDF opened once for the passes that read its pages one after the other (`notes.prepare_read`
    finding the speaker notes, then `extract_read`): one Document, so PDFium's text pages, object
    walks and path traces are made once, and each page's characters (`page_chars`) are read once.
    Nothing changes a Char once read (`dataclasses.replace` makes another), so passes share them;
    each answer is a list of its own. PDFium is not thread-safe: a Reading stays on the thread
    that opened it."""

    def __init__(self, pdf: Path) -> None:
        self.pdf = pdf
        self.doc: PdfDocument = Document(pdf)
        self._chars: dict[int, tuple[list[Char], set[int]]] = {}

    def page_chars(self, page: Page) -> tuple[list[Char], set[int]]:
        """`page_chars` of `page`, a page of this Reading's document."""
        if page.index not in self._chars:
            self._chars[page.index] = page_chars(page)
        chars, decoded = self._chars[page.index]
        return list(chars), set(decoded)

    def close(self) -> None:
        self._chars.clear()
        self.doc.close()

    def __enter__(self) -> "Reading":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# U+2010 HYPHEN (Calibri's, fontspec's): the Google substitutes have no glyph for it, and Slides
# drew it from a fallback font, wide and with room around it. U+2011 NON-BREAKING HYPHEN is what
# xdvipdfmx's ToUnicode gives fontspec's Cambria and Palatino Linotype for the '-' the source
# typed (lang_v1, lang_v3 'Spear-Danes'): Caladea and PT Serif lack it too, and Slides drew a
# short dash raised off the hyphen's height.
HYPHENS = str.maketrans({"‐": "-", "‑": "-"})
# Inferior figures a text font's ToUnicode gives its old-style small-cap figures (Palatino
# Linotype with Numbers=OldStyle in \textsc: 'Du sublime (1674)' reads 'DU SUBLIME ₍₁₆₇₄₎').
INFERIORS = str.maketrans("₀₁₂₃₄₅₆₇₈₉₍₎", "0123456789()")
INFERIOR_RUN = re.compile(r"[₀-₉₍₎]+")


def readable(text: str, font: str) -> str:
    """A span's text as its words: ligatures as letters, U+2010/U+2011 as '-', and in a text font a
    run of inferior figures that no letter or closing bracket carries (CO₂ and x₁ keep theirs:
    '§₂₅', ',₁₈₉₉', '₍₁₆₇₄₎') as the figures they are."""
    text = text.translate(LIGATURES).translate(HYPHENS)
    if font_info(font).family == "math" or not INFERIOR_RUN.search(text):
        return text

    def figures(m: re.Match[str]) -> str:
        run = m.group()
        before = text[m.start() - 1] if m.start() else None
        # (at the span's start the letter may be the span before's: a lone figure stays)
        carried = before.isalnum() or before in ")]}" if before else len(run) < 2
        return run if carried and "₍" not in run else run.translate(INFERIORS)
    return INFERIOR_RUN.sub(figures, text)


def _image(image_id: str, bbox: Iterable[float], px: list[float], marks: Marks | None) -> RawImage:
    image: RawImage = {"id": image_id, "bbox": _r(bbox, 2), "px": px}
    if marks is not None:
        image["marks"] = _marks_json(marks)
    return image


def _drawing(drawing_id: str, d: Drawing, shown: DrawingType, marks: Marks | None) -> RawDrawing:
    width = d.get("width")
    out: RawDrawing = {
        "id": drawing_id, "type": shown, "items": "".join(item[0] for item in d["items"]),
        "bbox": _r(d["rect"], 2), "fill": _hex(d.get("fill")), "stroke": _hex(d.get("color")),
        "width": round(width, 2) if width else None,
        "fill_opacity": round(_opacity(d.get("fill_opacity")), 3),
        "stroke_opacity": round(_opacity(d.get("stroke_opacity")), 3),
        "soft_mask": bool(d.get("soft_mask")),  # a soft mask or a blend mode (multiply)
        "corners": _rounded_corners(d),
        "path": _path(d),
    }
    dash = d.get("dash", ())
    if "s" in shown and any(v > 0 for v in dash):
        out["dash"] = [round(v, 2) for v in dash]
    if marks is not None:
        out["marks"] = _marks_json(marks)
    return out


def _link(link: Link) -> RawLink:
    bbox = _r(link["bbox"], 2)
    uri = link.get("uri")
    if isinstance(uri, str):
        return {"bbox": bbox, "uri": uri}
    page = link.get("page")
    if isinstance(page, int):
        return {"bbox": bbox, "page": page}
    raise ValueError(f"a link with neither a URI nor a page: {link}")


def extract_page(page: Page, label: str) -> RawPage:
    return _extract_page(page, label, page_chars(page))


def _extract_page(page: Page, label: str, read: tuple[list[Char], set[int]]) -> RawPage:
    """`extract_page` with the page's `page_chars` read (`Reading.page_chars`)."""
    n = page.index
    out_spans: list[RawSpan] = []
    visibility = Visibility(page)
    chars, decoded = read
    marks = page_marks(page)

    def span_json(s: PageSpan, sid: str) -> RawSpan:
        span: RawSpan = {
            # Ligature code points (xelatex/lualatex text layers) as plain letters, so the
            # text stays searchable and spell-checkable in Slides.
            "id": sid, "text": readable(s.text, s.font), "font": s.font,
            "size": round(s.size, 3), "color": f"#{s.color:06x}", "alpha": s.alpha,
            "origin": _r(s.origin, 2), "bbox": _r(s.bbox, 2), "dir": _r(s.dir, 3),
            # (a TeX bitmap font's small caps are a font of their own: ECCC1095)
            "smallcaps": s.chars[0].font_id not in decoded and _small_caps(page, list(s.chars)),
        }
        if s.marks:
            span["marks"] = _marks_json(s.marks)
        if s.columns is not None:
            span["columns"] = _r(s.columns, 3)
        return span

    for s in spans(page, visibility, False, chars, marks):
        if s.text.strip():
            out_spans.append(span_json(s, f"p{n}s{len(out_spans)}"))

    page_drawings = page.drawings()
    images = [_image(f"p{n}i{i}", info["bbox"], [info["width"], info["height"]], marks.get(info["object"]))
              for i, info in enumerate(page.images())]
    images += [_image(f"p{n}i{len(images) + i}", b, [b[2] - b[0], b[3] - b[1]], None)
               for i, b in enumerate(_shadow_pieces(page_drawings))]

    # ids are indices into page.drawings() (render.crop_overlay finds the objects by them), so a
    # drawing that does not show leaves a gap
    drawings = [_drawing(f"p{n}d{i}", shown[0], shown[1], marks.get(d["object"]))
                for i, d in enumerate(page_drawings) if (shown := _visible(d)) is not None]

    links = [_link(link) for link in page.links()]

    # Anything entirely outside the page (e.g. the cut-off half of a notes-on-second-screen page).
    x0, y0, x1, y1 = page.rect

    def inside(b: Sequence[float]) -> bool:
        return b[2] > x0 and b[0] < x1 and b[3] > y0 and b[1] < y1
    out_spans = [s for s in out_spans if inside(s["bbox"])]
    images = [i for i in images if inside(i["bbox"])]
    drawings = [d for d in drawings if inside(d["bbox"])]
    links = [l for l in links if inside(l["bbox"])]

    # The words drawn but not seen are still the frame's: beamer draws what a later overlay step
    # uncovers at alpha 0 (transparent mode), and select_overlays tells steps apart by their words.
    hidden_runs = spans(page, visibility, True, chars, marks)
    hidden = [t for s in hidden_runs if (t := readable(s.text, s.font).strip())]
    # On a page whose elements say what they are (adopt's marks), a marked element's words the page
    # hides (under a picture drawn after them) are still that element's, hidden in the deck as here.
    hidden_spans = [span_json(s, f"p{n}h{i}") for i, s in enumerate(r for r in hidden_runs if r.marks
                                                                   and r.text.strip() and inside(r.bbox))]

    out: RawPage = {
        "index": n, "label": label,
        "size": _r((page.width, page.height), 2),
        "spans": out_spans, "images": images, "drawings": drawings, "links": links,
    }
    if hidden:
        out["hidden_text"] = hidden
    if hidden_spans:
        out["hidden_spans"] = hidden_spans
    return out


def select_overlays(raw: RawDoc, mode: str) -> RawDoc:
    """Beamer gives every overlay step of a frame its own page, all with the frame number
    as page label. mode 'last' keeps only the final (complete) step of each frame; 'all'
    keeps every page. Handout PDFs have one page per label, so both are the same there."""
    if mode == "all":
        return raw
    pages = raw["pages"]

    def title(p: RawPage) -> str:
        """The frame title: the largest text in the top fifth of the page. (Not all of that
        band: a subtitle set with \\framesubtitle<n>, or a TikZ label drawn up there on one
        step, changed the band's text from step to step and split one frame into several.)"""
        band = [s for s in p["spans"] if s["bbox"][3] < 0.2 * p["size"][1] and s["text"].strip()]
        if not band:
            return ""
        big = max(s["size"] for s in band)
        return " ".join(s["text"].strip() for s in sorted(band, key=lambda s: (round(s["origin"][1]), s["bbox"][0]))
                        if s["size"] >= 0.9 * big)

    def words(p: RawPage) -> list[str]:  # what is drawn, seen or not (`hidden_text`)
        return [w for t in [s["text"] for s in p["spans"]] + p.get("hidden_text", []) for w in t.split()]

    def same_frame(a: RawPage, b: RawPage) -> bool:
        """Overlay steps share the frame number, and the title or nearly all of their text (a
        later step shows what the earlier one did). Themes that don't count some frames (title
        and section pages) share numbers too, but neither their title nor their text. With
        another title, only a step that keeps nearly everything ("Quiz" -> "Quiz: answer").
        With the title the same, \\only<n> may swap most of the words (a block's title and body,
        an image's caption): a third is enough, title and footline included. (The pages of a
        frame with allowframebreaks share their number too, but beamer's continuation text
        gives them another title from the second on; one set to nothing makes them one frame.)"""
        if a["label"] != b["label"]:
            return False
        wa, wb = words(a), set(words(b))
        if not wa:
            return title(a) == title(b)
        share = sum(w in wb for w in wa) / len(wa)
        return share >= 0.8 or (title(a) == title(b) and share >= 0.3)

    kept = [p for i, p in enumerate(pages) if i + 1 == len(pages) or not same_frame(p, pages[i + 1])]
    return {**raw, "pages": kept, "overlays": {"mode": mode, "dropped": len(pages) - len(kept)}}


FRAME_STEP = re.compile(r"(.+)<(\d+)>")


def frame_labels(dests: list[tuple[str, int]]) -> dict[int, str]:
    """Page index -> beamer frame label. `\\begin{frame}[label=x]` puts the destination x on the
    frame's first page and x<n> on its n-th overlay step; hyperref's own destinations (page.3,
    Navigation3) have no steps."""
    names = {name for name, _ in dests}
    out: dict[int, str] = {}
    for name, page in dests:
        m = FRAME_STEP.fullmatch(name)
        if m and m.group(1) in names and page >= 0:
            out.setdefault(page, m.group(1))
    return out


def extract(pdf: Path, labels: list[str] | None) -> RawDoc:
    """raw.json of `pdf`. `labels`, when given, replace the PDF's page labels (notes.prepare deletes
    pages, and PDFium can't rewrite the label tree)."""
    with Reading(pdf) as reading:
        return extract_read(reading, labels)


def extract_read(reading: Reading, labels: list[str] | None) -> RawDoc:
    """`extract` of the PDF `reading` holds open (a pass before it, `notes.prepare_read`, read the
    same pages)."""
    doc = reading.doc
    meta = doc.metadata
    frames = frame_labels(doc.named_dests())
    pages = [_extract_page(page, _label(labels[page.index] if labels else doc.label(page.index), page.index),
                           reading.page_chars(page))
             for page in doc]
    for page in pages:
        page["frame_label"] = frames.get(page["index"])
    return {
        "version": 1,
        "source": {"pdf": str(reading.pdf), "producer": meta["producer"], "pages": len(doc), "title": meta["title"]},
        "pages": pages,
    }
