"""Stage 1: dump each PDF page's text spans, images, drawings and links (raw.json)."""

import dataclasses
import math
import re
import unicodedata
from pathlib import Path

from . import bidi, type3
from .fonts import font_info
from .pdf import NO_OBJECT, OBJ_IMAGE, Char, Document, Page, char_box

LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
                           "ﬅ": "st", "ﬆ": "st"})


def _r(values, nd=2):
    return [round(float(v), nd) for v in values]


def _hex(rgb) -> str | None:
    if rgb is None:
        return None
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb[:3])


def _points(item: tuple) -> list:
    op = item[0]
    if op == "re":
        x0, y0, x1, y1 = item[1]
        return [[x0, y0], [x1, y1]]
    if op == "qu":
        p0, p1, p2, p3 = item[1]  # the quad's corners in drawing order: ul, ll, lr, ur
        return [list(p) for p in (p0, p3, p2, p1)]  # ul, ur, lr, ll
    return [list(p) for p in item[1:]]


def _path(d: dict, max_items: int = 20) -> list | None:
    """Path geometry for small drawings (diagram nodes, lines, arrow tips): [op, [[x, y], ...]]."""
    if len(d["items"]) > max_items:
        return None
    return [[item[0], [[round(x, 2), round(y, 2)] for x, y in _points(item)]] for item in d["items"]]


def _rounded_corners(d: dict) -> dict[str, float]:
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


def _small_caps(page: Page, chars: list[Char]) -> bool:
    """OpenType small caps (fontspec \\textsc): lowercase letters drawn with an alternate glyph.
    The text layer only says 'metropolis', the page shows METROPOLIS in small capitals. An
    alternate glyph has another advance than the font's default glyph for the letter."""
    lower = [ch for ch in chars if not ch.synthetic and len(ch.c) == 1 and ch.c.islower() and ch.exact_advance]
    if len(lower) < 2:
        return False
    alternate = 0
    for ch, default in zip(lower, page.glyph_widths([(ch.font_id, ch.c, ch.size) for ch in lower])):
        if default and abs(ch.advance - default) > SMALL_CAPS_WIDTH * max(default, 0.01):
            alternate += 1
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


def tracked_gaps(chars: list[Char], tracks: dict[int, float] | None = None) -> dict[int, bool]:
    """Letterspaced stretches: character index -> True where the gap before it is a word gap,
    False where it is the tracking between two letters of a word (never a word space). A stretch is a
    run of glyphs in one font on one line none of which touch (each gap at least TRACK_MIN): the
    tracking (within TRACK_BAND of the median), a kern off it, or a word gap (TRACK_WORD more),
    with at least TRACK_LETTERS tracked gaps between letters. In ordinary words the glyphs touch
    and only word spaces stand apart. Math and monospaced glyphs are left alone. `tracks`, when
    given, gets each index's stretch tracking (em)."""
    gaps = _gaps(chars)
    out: dict[int, bool] = {}
    i = 1
    while i < len(chars):
        if gaps[i] is None or gaps[i] < TRACK_MIN:
            i += 1
            continue
        j = i
        while j < len(chars) and gaps[j] is not None and gaps[j] >= TRACK_MIN:
            j += 1
        seg = range(i, j)  # gaps[k] is the gap between chars[k - 1] and chars[k]
        i = j
        if font_info(chars[seg[0]].font).family in ("math", "mono"):
            continue
        near = sorted(gaps[k] for k in seg if TRACK_MIN <= gaps[k] <= TRACK_MAX)
        if len(near) < TRACK_LETTERS:
            continue
        track = near[len(near) // 2]
        letters = [k for k in seg if abs(gaps[k] - track) <= TRACK_BAND
                   and chars[k - 1].c.isalpha() and chars[k].c.isalpha()]
        inner = [k for k in seg if gaps[k] < track + TRACK_WORD]
        # (ordinary words: most inner gaps touch, only the word spaces are near the median)
        if len(letters) < TRACK_LETTERS or len(letters) < 0.6 * len(inner):
            continue
        for k in seg:
            out[k] = gaps[k] >= track + TRACK_WORD
            if tracks is not None:
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


def _accent_overhang(page: Page, chars: list[Char]) -> list[Char]:
    """An accented italic letter's accent can reach past its advance (Calibri Italic's ì), and
    PDFium's loose box - the advance the text page gives - reaches to the ink: the word space
    after it shrank below JOIN_GAP ('yì yuè' -> 'yìyuè'). Such a letter takes the font's width."""
    todo = [k for k, ch in enumerate(chars) if not ch.synthetic and ch.exact_advance and len(ch.c) == 1
            and ch.dir[0] > 0.999 and len(unicodedata.normalize("NFD", ch.c)) > 1 and font_info(ch.font).italic]
    if not todo:
        return chars
    out = list(chars)
    for k, width in zip(todo, page.glyph_widths([(chars[k].font_id, chars[k].c, chars[k].size) for k in todo])):
        ch = chars[k]
        if width and 0.5 * ch.advance < width < ch.advance - 0.02 * ch.size:
            out[k] = dataclasses.replace(ch, advance=width, exact_advance=False, box=char_box(
                ch.origin[0], ch.origin[1], ch.dir[0], ch.dir[1], width, ch.size, ch.ascent, ch.descent))
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


def _flatten(items: list) -> list[list[tuple[float, float]]]:
    """A filled path's items as closed polygons: chains of items that join end to start (a
    filled subpath is closed whether or not the PDF closes it), curves in straight steps."""
    polys: list[list] = []
    chain: list | None = None  # the subpath being followed
    for item in items:
        op = item[0]
        if op in ("re", "qu"):
            if op == "re":
                x0, y0, x1, y1 = item[1]
                polys.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
            else:
                polys.append([tuple(p) for p in item[1]])
            chain = None
            continue
        start = tuple(item[1])
        if op == "l":
            pts = [tuple(item[2])]
        else:
            p0, p1, p2, p3 = item[1:5]
            pts = []
            for k in range(1, CURVE_STEPS + 1):
                t = k / CURVE_STEPS
                u = 1 - t
                pts.append(tuple(u ** 3 * p0[i] + 3 * u * u * t * p1[i] + 3 * u * t * t * p2[i] + t ** 3 * p3[i]
                                 for i in (0, 1)))
        if chain is not None and chain[-1] == start:
            chain.extend(pts)
        else:
            chain = [start, *pts]
            polys.append(chain)
    return [p for p in polys if len(p) >= 3]


def _winding(polys: list, x: float, y: float, even_odd: bool) -> bool:
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


def _in(box, x: float, y: float) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


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
        self.covers: list[tuple] = []  # (object id, bbox, polygons, "image" or None for a box, even_odd)
        for d in page.drawings():
            if d["type"] not in ("f", "fs") or d.get("fill") is None or d.get("soft_mask") \
                    or d.get("fill_opacity", 1.0) < 1.0 or self._cut(d["object"], d["rect"]):
                continue
            items = d["items"]
            box_only = len(items) == 1 and items[0][0] == "re"
            self.covers.append((d["object"], d["rect"], None if box_only else _flatten(items),
                                d.get("even_odd", False)))
        kinds = {po.id: po.type for po in objects}
        for info in page.images():
            if kinds.get(info["object"]) == OBJ_IMAGE:  # its box is what its clips let show
                self.covers.append((info["object"], info["bbox"], "image", False))
        self.grid: dict[tuple[int, int], list[int]] = {}
        for k, (_, (x0, y0, x1, y1), _, _) in enumerate(self.covers):
            if x1 - x0 > 4 * self.rect[2] or y1 - y0 > 4 * self.rect[3]:
                continue  # a runaway coordinate: nothing to index
            for gx in range(math.floor(x0 / GRID), math.floor(x1 / GRID) + 1):
                for gy in range(math.floor(y0 / GRID), math.floor(y1 / GRID) + 1):
                    self.grid.setdefault((gx, gy), []).append(k)
        self._opaque: dict[int, bool] = {}

    def _cut(self, obj: int, rect) -> bool:
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

    def _covered(self, obj: int, x: float, y: float) -> bool:
        for k in self.grid.get((math.floor(x / GRID), math.floor(y / GRID)), ()):
            cover, box, polys, even_odd = self.covers[k]
            if cover <= obj or not _in(box, x, y):
                continue
            if polys == "image":
                if self._image_opaque(cover):
                    return True
            elif polys is None or _winding(polys, x, y, even_odd):
                return True
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


def spans(page: Page, visibility: Visibility | None = None, hidden: bool = False,
          chars: list[Char] | None = None) -> list[dict]:
    """Runs of glyphs on one line with the same font, size and colour, split at word gaps. Only
    glyphs that show (`Visibility`), or with `hidden` only those on the page that don't. `chars`:
    the page's characters as read (`page_chars`), when the caller has them."""
    out = []
    run: list[Char] = []
    x0, y0, x1, y1 = page.rect
    visibility = visibility or Visibility(page)

    def flush():
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
            out.append({"text": text, "font": first.font, "size": first.size, "color": first.color,
                        "alpha": first.alpha, "origin": first.origin, "bbox": (bx0, by0, bx1, by1),
                        "dir": first.dir, "chars": list(run)})
        run.clear()

    # characters outside the page (e.g. the cut-off half of a notes-on-second-screen page)
    shown = [ch for ch in (page.chars() if chars is None else chars)
             if not (ch.box[2] <= x0 or ch.box[0] >= x1 or ch.box[3] <= y0 or ch.box[1] >= y1)
             and visibility.hidden(ch) == hidden]
    shown = _accent_overhang(page, shown)
    tracks: dict[int, float] = {}
    tracked, narrow = tracked_gaps(shown, tracks), narrow_spaces(shown)

    def letter_space(at: Char, width: float) -> Char:
        """A no-break space `width` pt wide after the glyph `at` (TRACK_SPACED)."""
        ux, uy = at.dir
        px, py = at.origin[0] + ux * at.advance, at.origin[1] + uy * at.advance
        return Char(LETTER_SPACE, at.font, at.size, at.color, at.alpha, (px, py),
                    char_box(px, py, ux, uy, width, at.size, at.ascent, at.descent),
                    at.dir, NO_OBJECT, at.font_id, width, True, at.ascent, at.descent)

    prev: Char | None = None
    for k, ch in enumerate(shown):
        space = None
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
            run.append(Char(" ", ch.font, ch.size, ch.color, ch.alpha, (px, py),
                            char_box(px, py, ux, uy, width, ch.size, ch.ascent, ch.descent),
                            ch.dir, NO_OBJECT, ch.font_id, width, True, ch.ascent, ch.descent))
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
            new_line = ch.dir != prev.dir or offset > NEW_BASELINE or gap > NEW_LINE_GAP or gap < BACK_GAP
            if new_line or gap >= WORD_GAP:
                flush()
            elif gap >= JOIN_GAP and offset < SAME_BASELINE and prev.c != " " and ch.c != " " \
                    and not (_mono(prev.font) and _mono(ch.font) and gap * size < MONO_JOIN_GAP * prev.advance):
                if style:
                    flush()
                width = gap * size
                space = Char(" ", ch.font, ch.size, ch.color, ch.alpha, (px, py),
                             char_box(px, py, ux, uy, width, ch.size, ch.ascent, ch.descent),
                             ch.dir, NO_OBJECT, ch.font_id, width, True, ch.ascent, ch.descent)
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


def _opacity(d: dict, key: str) -> float:
    """A drawing's fill or stroke opacity; 1 when the backend says nothing (0 is fully transparent)."""
    v = d.get(key)
    return 1.0 if v is None else v


def _visible(d: dict) -> dict | None:
    """What of a drawing shows: a fill or stroke at opacity 0 (a tikz node drawn with opacity=0
    on an overlay step) is left out, and a drawing with neither is none."""
    fill = d["type"] in ("f", "fs") and _opacity(d, "fill_opacity") > 0
    stroke = d["type"] in ("s", "fs") and _opacity(d, "stroke_opacity") > 0
    if fill and stroke or d["type"] == ("f" if fill else "s" if stroke else None):
        return d
    if not (fill or stroke):
        return None
    if fill:
        return {k: v for k, v in d.items() if k not in ("color", "stroke_opacity", "width")} | {"type": "f"}
    return {k: v for k, v in d.items() if k not in ("fill", "fill_opacity", "even_odd")} | {"type": "s"}


def _shadow_pieces(drawings: list[dict]) -> list[tuple]:
    """Beamer's block shadows: a black rectangle under a soft mask whose shadings fade its
    edges, offset right and down from the panel painted over it. The mask contents are not
    page objects, so the visible parts of the shadow (right of and below the panel) are
    reported as shading pieces on whole points, as the shadings themselves would be."""
    pieces = []
    for i, m in enumerate(drawings):
        if not m.get("soft_mask") or m["type"] != "f":
            continue
        mx0, my0, mx1, my1 = m["rect"]
        for p in drawings[i + 1:]:
            if p["type"] not in ("f", "fs") or p.get("soft_mask") or _opacity(p, "fill_opacity") < 1.0:
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


# U+2010 HYPHEN (Calibri's, fontspec's): the Google substitutes have no glyph for it, and Slides
# drew it from a fallback font, wide and with room around it.
HYPHENS = str.maketrans({"‐": "-"})
# Inferior figures a text font's ToUnicode gives its old-style small-cap figures (Palatino
# Linotype with Numbers=OldStyle in \textsc: 'Du sublime (1674)' reads 'DU SUBLIME ₍₁₆₇₄₎').
INFERIORS = str.maketrans("₀₁₂₃₄₅₆₇₈₉₍₎", "0123456789()")
INFERIOR_RUN = re.compile(r"[₀-₉₍₎]+")


def readable(text: str, font: str) -> str:
    """A span's text as its words: ligatures as letters, U+2010 as '-', and in a text font a
    run of inferior figures that no letter or closing bracket carries (CO₂ and x₁ keep theirs:
    '§₂₅', ',₁₈₉₉', '₍₁₆₇₄₎') as the figures they are."""
    text = text.translate(LIGATURES).translate(HYPHENS)
    if font_info(font).family == "math" or not INFERIOR_RUN.search(text):
        return text

    def figures(m: re.Match) -> str:
        run = m.group()
        before = text[m.start() - 1] if m.start() else None
        # (at the span's start the letter may be the span before's: a lone figure stays)
        carried = before.isalnum() or before in ")]}" if before else len(run) < 2
        return run if carried and "₍" not in run else run.translate(INFERIORS)
    return INFERIOR_RUN.sub(figures, text)


def extract_page(page: Page, label: str) -> dict:
    n = page.index
    out_spans = []
    visibility = Visibility(page)
    chars, decoded = page_chars(page)
    for s in spans(page, visibility, chars=chars):
        if not s["text"].strip():
            continue
        out_spans.append({
            # Ligature code points (xelatex/lualatex text layers) as plain letters, so the
            # text stays searchable and spell-checkable in Slides.
            "id": f"p{n}s{len(out_spans)}", "text": readable(s["text"], s["font"]), "font": s["font"],
            "size": round(s["size"], 3), "color": f"#{s['color']:06x}", "alpha": s["alpha"],
            "origin": _r(s["origin"]), "bbox": _r(s["bbox"]), "dir": _r(s["dir"], 3),
            # (a TeX bitmap font's small caps are a font of their own: ECCC1095)
            "smallcaps": s["chars"][0].font_id not in decoded and _small_caps(page, s["chars"]),
        })

    page_drawings = page.drawings()
    found = [(info["bbox"], [info["width"], info["height"]]) for info in page.images()]
    found += [(b, [b[2] - b[0], b[3] - b[1]]) for b in _shadow_pieces(page_drawings)]
    images = [{"id": f"p{n}i{i}", "bbox": _r(b), "px": px} for i, (b, px) in enumerate(found)]

    # ids are indices into page.drawings() (render.crop_overlay finds the objects by them), so a
    # drawing that does not show leaves a gap
    drawings = [{
        "id": f"p{n}d{i}", "type": d["type"], "items": "".join(item[0] for item in d["items"]),
        "bbox": _r(d["rect"]), "fill": _hex(d.get("fill")), "stroke": _hex(d.get("color")),
        "width": round(d["width"], 2) if d.get("width") else None,
        "fill_opacity": round(_opacity(d, "fill_opacity"), 3),
        "stroke_opacity": round(_opacity(d, "stroke_opacity"), 3),
        "soft_mask": bool(d.get("soft_mask")),  # a soft mask or a blend mode (multiply)
        "corners": _rounded_corners(d),
        "path": _path(d),
    } for i, d in ((i, _visible(d)) for i, d in enumerate(page_drawings)) if d is not None]

    links = [{"bbox": _r(link["bbox"]), **({"uri": link["uri"]} if "uri" in link else {"page": link["page"]})}
             for link in page.links()]

    # Anything entirely outside the page (e.g. the cut-off half of a notes-on-second-screen page).
    x0, y0, x1, y1 = page.rect
    inside = lambda b: b[2] > x0 and b[0] < x1 and b[3] > y0 and b[1] < y1
    out_spans = [s for s in out_spans if inside(s["bbox"])]
    images = [i for i in images if inside(i["bbox"])]
    drawings = [d for d in drawings if inside(d["bbox"])]
    links = [l for l in links if inside(l["bbox"])]

    # The words drawn but not seen are still the frame's: beamer draws what a later overlay step
    # uncovers at alpha 0 (transparent mode), and select_overlays tells steps apart by their words.
    hidden = [t for s in spans(page, visibility, hidden=True, chars=chars) if (t := readable(s["text"], s["font"]).strip())]

    return {
        "index": n, "label": label,
        "size": _r((page.width, page.height)),
        "spans": out_spans, "images": images, "drawings": drawings, "links": links,
        **({"hidden_text": hidden} if hidden else {}),
    }


def select_overlays(raw: dict, mode: str) -> dict:
    """Beamer gives every overlay step of a frame its own page, all with the frame number
    as page label. mode 'last' keeps only the final (complete) step of each frame; 'all'
    keeps every page. Handout PDFs have one page per label, so both are the same there."""
    if mode == "all":
        return raw
    pages = raw["pages"]

    def title(p: dict) -> str:
        """The frame title: the largest text in the top fifth of the page. (Not all of that
        band: a subtitle set with \\framesubtitle<n>, or a TikZ label drawn up there on one
        step, changed the band's text from step to step and split one frame into several.)"""
        band = [s for s in p["spans"] if s["bbox"][3] < 0.2 * p["size"][1] and s["text"].strip()]
        if not band:
            return ""
        big = max(s["size"] for s in band)
        return " ".join(s["text"].strip() for s in sorted(band, key=lambda s: (round(s["origin"][1]), s["bbox"][0]))
                        if s["size"] >= 0.9 * big)

    def words(p: dict) -> list[str]:  # what is drawn, seen or not (`hidden_text`)
        return [w for t in [s["text"] for s in p["spans"]] + p.get("hidden_text", []) for w in t.split()]

    def same_frame(a: dict, b: dict) -> bool:
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
    out = {}
    for name, page in dests:
        m = FRAME_STEP.fullmatch(name)
        if m and m.group(1) in names and page >= 0:
            out.setdefault(page, m.group(1))
    return out


def extract(pdf: Path, labels: list[str] | None = None) -> dict:
    """`labels` replaces the PDF's page labels (notes.prepare deletes pages, and PDFium can't
    rewrite the label tree)."""
    doc = Document(pdf)
    try:
        meta = doc.metadata
        frames = frame_labels(doc.named_dests())
        pages = [extract_page(page, _label(labels[page.index] if labels else doc.label(page.index), page.index))
                 for page in doc]
        for page in pages:
            page["frame_label"] = frames.get(page["index"])
        return {
            "version": 1,
            "source": {"pdf": str(pdf), "producer": meta["producer"], "pages": len(doc), "title": meta["title"]},
            "pages": pages,
        }
    finally:
        doc.close()
