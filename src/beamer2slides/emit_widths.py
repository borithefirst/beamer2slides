"""How wide Slides sets words and where it breaks lines: measured advances per face, and a
paragraph's lines in the PDF and in Slides.

Each measure works on emit's `SetRun`s and `SetParagraph`s (the `_of` twins). The names without it
take the dicts tables, classify and the tests still hold, read once through `emit_model`.
"""

import functools
import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import replace

from . import bidi
from .emit_metrics import (
    ADVANCES, CM_ADVANCES, DESIGN_WIDTH, FONT_FOR_FAMILY, ROBOTO_MONO_ADVANCE_EM, SOFT_BREAK,
    SYMBOL_ADVANCE_EM, UNMEASURED_ADVANCE_EM, FontMapper, advance_widths, cm_face_of, design_width,
)
from .emit_model import JsonMap, SetParagraph, SetRun, paragraph_of, run_of, runs_of
from .fonts import font_info, written_advance
from .mono_edges import EDGE_SPACE


ZWSP = "​"
LINE_SEPARATOR = " "   # no width; Slides may end a line after it, before no-break spaces too (emit_text.HOLE_BREAK)
HOLE_BREAKS = ZWSP + LINE_SEPARATOR  # what emit writes (or wrote) in front of a hole: no character of the text
WORD_JOINER = "⁠"       # no break here, no width (tools/probe_script_break.py); emit writes it: joined_runs
# What classify adds before a word space where TeX's is wider than Lato's: U+2008 after a
# sentence, a script or a formula letter (`classify_text.widened`), EDGE_SPACEs at an edge of
# inline code (`mono_edges.edge_fill`). emit keeps them only on a line they leave no wider than the
# PDF's (`emit_text.within_budget`); readers take them and their space for one space.
ADDED_SPACE = re.compile("(?: |" + EDGE_SPACE + "+)(?= )")
SCRIPT_SIZE = 2 / 3          # super- and subscripts in Slides (measured 0.665: tools/probe_text_fit_fonts.py)
WRAP_MARGIN = 1.0            # Slides pt kept free in a cell so kerning or rounding cannot wrap it
SMALL_CAPS_SIZE = 0.70       # Slides draws a small capital at 70% of its capital (tools/probe_text_fit_fonts.py)
LABEL_ROOM = 2.5             # Slides pt a hanging label keeps before its tab stop (as emit_text._own_lines)
LINE_RATIO = (0.6, 1.5)      # a line with holes or a label as Slides sets it, to its PDF extent: else not that line


def set_runs_of(runs: Sequence[JsonMap]) -> tuple[SetRun, ...]:
    return tuple(run_of(r) for r in runs)


def slides_width(runs: Sequence[JsonMap], scale: float, fonts: FontMapper) -> float | None:
    return slides_width_of(set_runs_of(runs), scale, fonts)


def slides_width_of(runs: Sequence[SetRun], scale: float, fonts: FontMapper) -> float | None:
    """Advance width (Slides pt) of one line of these runs as Slides sets them, or None when a
    run is in a font the probe did not measure (a Google font the PDF itself uses).

    The calibrated size factors make a *sentence* as wide in Slides as in the PDF; a number is
    not a sentence - Lato's digits are tabular at 0.58 em where Computer Modern's are 0.5 - so a
    number column comes out ~14% wider than the PDF's, more than a column's slack."""
    total = 0.0
    for run in runs:
        family, size = fonts.size_of(run, scale)
        style = fonts.face_of(run)  # (the face Slides draws a small optical cut's weight in: FontMapper.face)
        table: dict[str, float]
        if family == FONT_FOR_FAMILY["mono"]:
            table = {}
            unmeasured = ROBOTO_MONO_ADVANCE_EM
        elif family in ADVANCES:
            # (a symbol the face lacks comes from Slides' fallback font, as probe_symbols measured
            # it: ⊂ is 0.981 em, not 0.6 - a line with '⊂ ℝ' came out 8 pt wider than predicted,
            # and a box sized to it broke a word early, V-control-16)
            table, unmeasured = face_advances(family, style), UNMEASURED_ADVANCE_EM
        else:
            return None
        if run.script:
            size *= SCRIPT_SIZE
        total += fonts.leader_correction_of(run, family) * size  # (a leader's dots: their measured pitch)
        # (script capitals and a math font's operators are written in a face of their own:
        # emit_metrics.letter_faces)
        for ch in run.text:
            written = written_advance(ch, run.font)
            if written is not None:
                total += written * size
                continue
            ch = " " if ch == " " else ch  # (a no-break space, a \colorbox's padding, is a space's width)
            if unicodedata.combining(ch):
                continue  # (a macron over its letter: no advance of its own)
            if run.smallcaps and ch.islower():
                total += table.get(ch.upper(), unmeasured) * size * SMALL_CAPS_SIZE
            else:
                total += table.get(ch, wide_advance(ch, unmeasured)) * size
    return total


@functools.lru_cache(maxsize=None)
def face_advances(family: str, style: str) -> dict[str, float]:
    """A calibrated face's advances (em), with Slides' fallback font's for the symbols it lacks."""
    return {**SYMBOL_ADVANCE_EM, **ADVANCES[family][style]}


def wide_advance(ch: str, unmeasured: float) -> float:
    """The advance (em) of a character the probe did not measure: a CJK ideograph, kana or
    full-width form is a whole em in every fallback font (unicodedata's East Asian Width W / F).
    At 0.6 em a Japanese header came out 40% narrower than Slides sets it and wrapped its cell.
    A bidi mark (`bidi.MARKS`: the LRM or RLM `bidi.logical_line` writes) draws nothing, nor
    does the break before a hole (HOLE_BREAKS), nor a word joiner (`joined_runs`)."""
    if ch in bidi.MARKS or ch in HOLE_BREAKS or ch == WORD_JOINER:
        return 0.0
    return 1.0 if unicodedata.east_asian_width(ch) in "WF" else unmeasured


def runs_between(runs: Sequence[SetRun], a: int, b: int) -> list[SetRun]:
    """The runs' characters a to b (indices into their joined text)."""
    out: list[SetRun] = []
    at = 0
    for run in runs:
        text = run.text
        lo, hi = max(a, at), min(b, at + len(text))
        if lo < hi:
            out.append(replace(run, text=text[lo - at:hi - at]))
        at += len(text)
    return out


def wrapped_width(runs: Sequence[JsonMap], starts: Sequence[int], scale: float, fonts: FontMapper) -> float | None:
    return wrapped_width_of(set_runs_of(runs), starts, scale, fonts)


def wrapped_width_of(set_runs: Sequence[SetRun], starts: Sequence[int], scale: float, fonts: FontMapper) -> float | None:
    """Slides width of the widest of a wrapped cell's PDF lines (`starts`: where each line after
    the first begins), a word TeX hyphenated at a line's end taken whole: Slides does not
    hyphenate, and a column that holds each of the PDF's lines so wraps the cell in as many lines
    or fewer - never more, which would grow its row."""
    text = "".join(r.text for r in set_runs)
    bounds = [0, *starts, len(text)]
    widths: list[float] = []
    for a, b in zip(bounds, bounds[1:]):
        while b < len(text) and not text[b - 1].isspace() and not text[b].isspace():
            b += 1  # (the line ended inside a word)
        while b > a and text[b - 1].isspace():
            b -= 1
        w = slides_width_of(runs_between(set_runs, a, b), scale, fonts)
        if w is None:
            return None
        widths.append(w)
    return max(widths)


def wrap_joins(runs: Sequence[JsonMap], starts: Sequence[int], scale: float, fonts: FontMapper) -> float | None:
    return wrap_joins_of(set_runs_of(runs), starts, scale, fonts)


def wrap_joins_of(set_runs: Sequence[SetRun], starts: Sequence[int], scale: float, fonts: FontMapper) -> float | None:
    """Slides width of the narrowest of a wrapped cell's PDF lines with the next line's first
    word joined to it: a column whose text room is less than that breaks the cell where TeX did
    (table_columns). None when a run is in a font the probe did not measure."""
    text = "".join(r.text for r in set_runs)
    bounds = [len(text) - len(text.lstrip()), *starts]
    joins: list[float] = []
    for a, s in zip(bounds, bounds[1:]):
        b = s
        while b < len(text) and text[b].isspace():
            b += 1
        while b < len(text) and not text[b].isspace():
            b += 1  # (through the next word; a word TeX hyphenated is taken whole by wrapped_width)
        w = slides_width_of(runs_between(set_runs, a, b), scale, fonts)
        if w is None:
            return None
        joins.append(w)
    return min(joins) if joins else None


def wrap_window(runs: Sequence[JsonMap], lines: int, scale: float, fonts: FontMapper) -> tuple[float, float] | None:
    return wrap_window_of(set_runs_of(runs), lines, scale, fonts)


def wrap_window_of(set_runs: Sequence[SetRun], lines: int, scale: float, fonts: FontMapper
                   ) -> tuple[float, float] | None:
    """(least text width at which Slides breaks these runs' words into no more than `lines`
    lines, least width at which it breaks them into fewer), Slides pt: Slides breaks greedily at
    spaces, so a column whose text room lies between the two keeps a wrapped cell's row as many
    lines tall as the PDF's, wherever its breaks fall. A word TeX hyphenated at a line's end can
    go down to the next line: taken whole on its line (wrapped_width), it widened a column by
    the hyphen's second half and the table past its frame (r2_tables_v1 slide 2, r2_tables_v2
    slide 10). None when a run is in a font the probe did not measure."""
    text = "".join(r.text for r in set_runs)
    words = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    if not words:
        return None
    at = [0.0]
    for k in range(len(text)):
        w = slides_width_of(runs_between(set_runs, k, k + 1), scale, fonts)
        if w is None:
            return None
        at.append(at[-1] + w)

    def count(width: float) -> int:
        n, first = 1, 0
        for j in range(1, len(words)):
            if at[words[j][1]] - at[words[first][0]] > width:
                n, first = n + 1, j
        return n

    widths = sorted({at[words[j][1]] - at[words[i][0]] for i in range(len(words)) for j in range(i, len(words))})

    def least(n: int) -> float:
        lo, hi = 0, len(widths) - 1  # (the widest, the whole text on one line, always fits)
        while lo < hi:
            mid = (lo + hi) // 2
            if count(widths[mid]) <= n:
                hi = mid
            else:
                lo = mid + 1
        return widths[lo]
    return least(lines), (least(lines - 1) if lines > 1 else math.inf)


def pdf_width(runs: Sequence[JsonMap]) -> float | None:
    return pdf_width_of(set_runs_of(runs))


def pdf_width_of(runs: Sequence[SetRun]) -> float | None:
    """Width (PDF pt) TeX sets these Computer Modern runs at, from CM's own advances (within
    0.3% on one-line paragraphs: test_computer_modern_advances_give_the_pdf_its_own_line_widths),
    or None for a run in another font or with characters the tables lack."""
    total = 0.0
    for run in runs:
        # (no design size: not a TeX optical cut - CM, EC, Latin Modern -, so no CM metrics either;
        # cm_face_of says None for it too)
        face, design = cm_face_of(run), font_info(run.font).design_size
        if face is None or design is None or run.hole or run.smallcaps:
            return None
        slides = ADVANCES.get(FONT_FOR_FAMILY.get(run.family, ""), ADVANCES["Lato"])["regular"]
        _, em, _, skipped = advance_widths(run.text, CM_ADVANCES[face], slides)
        if skipped:
            return None
        total += em * design_width(DESIGN_WIDTH.get(run.family, DESIGN_WIDTH["sans"]), design) * run.size
    return total


LINE_FIT_TOL = 0.06  # a PDF line may be this much wider than its words (a justified line's spaces)
# A line may be narrower than its words set with full word spaces by a thin space ("1\,mM": the
# extracted text has a space where TeX put 0.167 em), which is 0.17 em less than a word space.
THIN_SPACE_EM = 0.17
GUESSED_RUN_CHARS = 2  # a run without CM metrics this short (≈, ×, a math-italic "."): its width is estimated
GUESSED_TOL = 0.3      # the share an estimated run's width may be off by


def paragraph_dict(p: JsonMap) -> SetParagraph:
    """A paragraph dict with its runs, as the measures here read it."""
    return paragraph_of(p, runs_of(p["runs"]))


def pdf_line_breaks(p: JsonMap, scale: float | None, fonts: FontMapper | None) -> list[int] | None:
    return pdf_line_breaks_of(paragraph_dict(p), scale, fonts)


def pdf_line_breaks_of(p: SetParagraph, scale: float | None, fonts: FontMapper | None) -> list[int] | None:
    """Where each of a paragraph's PDF lines after the first starts (indices into its runs'
    joined text, at a word), found by setting its words with CM's advances into the lines'
    extents; None when the paragraph's words cannot be measured or do not come out as its lines
    (a hyphenated line end, a font without CM metrics). With `fonts` (and the `scale` it sets
    at), a symbol or two in a font without CM metrics (≈, ×, a barred letter) is estimated from
    its Slides advance, which the calibration makes about as wide as the PDF's: a whole paragraph
    went unmeasured for one ≈."""
    runs, lines = p.runs, p.lines
    text = "".join(r.text for r in runs)
    if any(SOFT_BREAK in r.text or "\t" in r.text for r in runs):
        return None
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    starts: list[int] = []
    a = 0
    while text[a:a + 1] == " ":
        a += 1

    def width(a: int, b: int) -> tuple[float, float] | None:
        """(PDF width of the characters a to b, how far off it may be)."""
        total = guessed = 0.0
        for run in runs_between(runs, a, b):
            w = pdf_width_of([run])
            if w is None:
                bare = "".join(ch for ch in run.text if not unicodedata.combining(ch))  # (no advance)
                if fonts is None or scale is None or run.hole or len(bare.strip()) > GUESSED_RUN_CHARS:
                    return None
                w = pdf_width_of([replace(run, text=bare)])
                if w is None:
                    s = slides_width_of([replace(run, text=bare)], scale, fonts)
                    if s is None:
                        return None
                    w = s / scale
                    guessed += w
            total += w
        return total, GUESSED_TOL * guessed

    for k, line in enumerate(lines):
        extent = line.x1 - line.x0
        thin = THIN_SPACE_EM * max(r.size for r in runs)
        if k == len(lines) - 1:
            end = len(text.rstrip())
        else:
            found: int | None = None
            for b in (s for s in spaces if s > a):
                got = width(a, b)
                if got is None:
                    return None
                if got[0] - got[1] > extent * (1 + 0.01) + 0.5 + thin:
                    break
                found = b
            if found is None:
                return None
            end = found
        got = width(a, end)
        if got is None or not extent * (1 - LINE_FIT_TOL) - 0.5 - got[1] <= got[0] <= extent * 1.01 + 0.5 + thin + got[1]:
            return None
        if k < len(lines) - 1:
            a = end
            while text[a:a + 1] == " ":
                a += 1
            starts.append(a)
    return starts


def recorded_starts(p: SetParagraph, text: str) -> list[int] | None:
    """Where classify found the paragraph's lines after the first start in `text`
    (`classify.line_starts`: each at a word, then the text's length), or None when it said
    nothing or said it of another text or other lines (merged words, an IR from elsewhere).
    What the page says wins over setting the words into the lines' extents (pdf_line_breaks),
    which needs TeX's widths: a Palatino paragraph, or one with a formula's letters, was not
    measured and Slides re-broke it (V-control-16, V-fonts-8, lang-18)."""
    got = p.line_starts
    if not got or len(got) != len(p.lines) or got[-1] != len(text):
        return None
    starts = list(got[:-1])
    if any(not 0 < b < len(text) or text[b - 1] != " " or text[b] == " " for b in starts) or \
            any(b <= a for a, b in zip(starts, starts[1:])):
        return None
    return starts


def guessed_chars(runs: Sequence[SetRun], scale: float, fonts: FontMapper) -> int:
    """How many of the runs' characters slides_width guesses (no measured advance: polytonic
    Greek in PT Serif). Where TeX's widths did not confirm the lines (recorded_starts), a box
    measured from guesses is no better than the PDF's extents: Greek at 0.6 em came out wider
    than Slides sets it (r1_lang_v1 s4, 4 lines of 5 either way). A character written in a face
    of its own with a measured advance (`fonts.written_advance`: a script capital, an operator of
    a TeX math font's run) is measured, as slides_width counts it; a text font's × is not."""
    count = 0
    for run in runs:
        family, _ = fonts.size_of(run, scale)
        if family == FONT_FOR_FAMILY["mono"]:
            continue
        if family not in ADVANCES:
            return len(run.text)
        table = face_advances(family, fonts.face_of(run))
        count += sum(1 for ch in run.text if (ch.upper() if run.smallcaps else ch) not in table
                     and written_advance(ch, run.font) is None
                     and ch not in " " and not unicodedata.combining(ch) and ch not in bidi.MARKS
                     and ch not in HOLE_BREAKS and ch != WORD_JOINER and unicodedata.east_asian_width(ch) not in "WF")
    return count


OPENING_BRACKETS = frozenset("([{")


def breaks_before(text: str, i: int) -> bool:
    """Whether Slides may end a line just before `text[i]`, inside a word: at an opening bracket
    after a letter of East Asian width 'ambiguous' (Greek: π[γ], π([x])), which UAX #14 LB30 would
    keep together and Slides does not; after a Latin letter or a digit it does
    (tools/probe_script_break.py; real_beamer-monodromy s17 ended a line on π and set its
    subscript [γ] on the next)."""
    return 0 < i < len(text) and text[i] in OPENING_BRACKETS and text[i - 1].isalpha() and \
        unicodedata.east_asian_width(text[i - 1]) == "A"


def joined_runs(runs: Sequence[SetRun]) -> tuple[SetRun, ...]:
    """The runs with a WORD_JOINER wherever Slides would break inside a word (`breaks_before`),
    at the end of the run before it: TeX set π[γ]([x]) whole, and a box wide enough for its
    line pulled the π up onto the line above (real_beamer-monodromy s17 item 5). The joiner
    takes no room and keeps the two together (tools/probe_script_break.py); a hole's no-break
    spaces are never split."""
    text = "".join(r.text for r in runs)
    cuts = {i for i in range(1, len(text)) if breaks_before(text, i)}
    if not cuts:
        return tuple(runs)
    out: list[SetRun] = []
    at = 0
    for r in runs:
        if r.hole:
            out.append(r)
        else:
            pieces = [WORD_JOINER + ch if at + k in cuts and k > 0 else ch for k, ch in enumerate(r.text)]
            end = WORD_JOINER if at + len(r.text) in cuts else ""
            out.append(replace(r, text="".join(pieces) + end))
        at += len(r.text)
    return tuple(out)


def first_break(text: str, a: int, end: int) -> int:
    """Where Slides may first end a line that has taken the words from `a` on: at the next
    space, after a hyphen inside the word before it (not a leading one, nor one before a
    digit: UAX #14 LB25), or before a bracket opening after a Greek letter (`breaks_before`), as
    `text_layout.wrap` breaks. Taken to its space, the next line's first word was
    'Санкт-Петербургский', and the box left room for 'Санкт-', which Slides pulled up onto the
    line above (r3_scripts_ruxe s1)."""
    space = text.find(" ", a)
    space = end if space < 0 or space > end else space
    for i in range(a + 1, space):
        if breaks_before(text, i):
            return i
        if i < space - 1 and text[i] == "-" and not text[i - 1].isspace() and not text[i + 1].isdigit():
            return i + 1
    return space


def slides_lines(p: JsonMap, scale: float, fonts: FontMapper) -> tuple[float, float] | None:
    return slides_lines_of(paragraph_dict(p), scale, fonts)


def held_width_of(runs: Sequence[SetRun], scale: float, fonts: FontMapper) -> float | None:
    """`slides_width_of` runs a text box holds, a hole among them as wide as its no-break spaces
    are made (`emit_text.hole_spaces`: exactly the hole's width)."""
    words = slides_width_of([r for r in runs if not r.hole], scale, fonts)
    return None if words is None else words + sum((r.hole or 0.0) * scale for r in runs)


def held_index(ir: Sequence[SetRun], held: Sequence[SetRun], at: int) -> int:
    """Where character `at` of the IR runs' joined text lies in the text a box holds (`held`: the
    same runs, each hole its no-break spaces: `emit_text.hole_runs_of`), before a hole starting
    there."""
    a = b = 0
    for r, s in zip(ir, held):
        if at <= a + len(r.text):
            return b + _held_offset(s, at - a)
        a, b = a + len(r.text), b + len(s.text)
    return b


def _held_offset(s: SetRun, k: int) -> int:
    """Where the run's character `k` lies in the run as the box holds it (`s`): a hole's no-break
    spaces end where they end, a word joiner (`joined_runs`) is not counted and the character
    after it (or the run's end past one ending it) is where `k` lies."""
    if s.hole or WORD_JOINER not in s.text:
        return min(k, len(s.text))
    i = n = 0
    while i < len(s.text) and n < k:
        n += s.text[i] != WORD_JOINER
        i += 1
    while i < len(s.text) and s.text[i] == WORD_JOINER:
        i += 1
    return i


def held_starts(ir: Sequence[SetRun], held: Sequence[SetRun], starts: tuple[int, ...] | None) -> tuple[int, ...] | None:
    """classify's `line_starts` (into the IR runs' text) in the text a box holds; None when the
    runs do not pair up. (The last, the text's end, stays its end: past a hole ending it.)"""
    if starts is None or len(ir) != len(held):
        return None
    end, held_end = sum(len(r.text) for r in ir), sum(len(s.text) for s in held)
    return tuple(held_end if s >= end else held_index(ir, held, s) for s in starts)


def slides_lines_of(p: SetParagraph, scale: float, fonts: FontMapper) -> tuple[float, float] | None:
    """(right edge of the widest line, the least right edge at which a line's next word would
    join it) in Slides pt, of a left-aligned paragraph's PDF lines as Slides sets their words
    (measured advances: slides_width); None when that is not known. A text box whose text ends
    between the two breaks the paragraph where TeX did.

    A hole is as wide as its no-break spaces (the paragraph as its box holds it), and a hanging
    label's line ("label<TAB>text") is measured from the tab stop (`p.tab_x0`) on, its label
    from where the paragraph starts (`text_x0`; None where it would reach the stop). Left out,
    an enumerated item ending on a framed formula went unmeasured: its box kept the PDF's extent,
    Lato's words took a little more, and the formula wrapped onto a line of its own, pushing the
    items under it down over the next picture (real_beamer-monodromy s17)."""
    runs, lines = p.runs, p.lines
    text = "".join(r.text for r in runs)
    tab = text.find("\t")
    if any((r.hole and r.hole_size is None) or SOFT_BREAK in r.text for r in runs) or not text.strip() or \
            tab >= 0 and (text.count("\t") > 1 or p.tab_x0 is None):
        return None  # (a hole still the IR's: its text is not what the box holds)
    # (a justified paragraph TeX's widths cannot measure keeps its own edge, flowed into it:
    # flowed_justified_right; measured by its lines, one set a hair wider in Slides than TeX's
    # shrunk line gave up JUSTIFIED for START)
    recorded = recorded_starts(p, text) if len(lines) > 1 and not p.justified and \
        guessed_chars(runs, scale, fonts) <= GUESSED_RUN_CHARS else None
    starts: list[int] | None = [] if len(lines) == 1 else recorded or pdf_line_breaks_of(p, scale, fonts)
    if starts is None:
        return None
    end = len(text.rstrip())
    # (a hole opening the paragraph is its no-break spaces: they take their room)
    bounds = [len(text) - len(text.lstrip(" ")), *starts, end]
    if tab >= 0 and not bounds[0] <= tab < bounds[1]:
        return None  # (a tab past the first line is no hanging label)

    def right(k: int, a: int, b: int) -> float | None:
        """Where line k ends in Slides when it holds the characters a to b."""
        if a <= tab < b and p.tab_x0 is not None:
            label = held_width_of(runs_between(runs, a, tab), scale, fonts)
            rest = held_width_of(runs_between(runs, tab + 1, b), scale, fonts)
            stop = p.tab_x0 * scale
            if label is None or rest is None or p.text_x0 * scale + label + LABEL_ROOM > stop:
                return None
            return stop + rest
        w = held_width_of(runs_between(runs, a, b), scale, fonts)
        return None if w is None else lines[k].x0 * scale + w

    # (a line of holes or a label is no wider in Slides than half as much again: starts classify
    # recorded before a framed formula became one hole put 106 letters on a 300 pt line, and the
    # caption's box ran 250 pt past the slide, real_beamer-monodromy s15)
    checked = tab >= 0 or any(r.hole for r in runs)
    widest, joins = 0.0, math.inf
    for k, (a, b) in enumerate(zip(bounds, bounds[1:])):
        b = a + len(text[a:b].rstrip())  # (Slides lets a line's last space hang past the edge)
        w = right(k, a, b)
        if w is None:
            return None
        extent = (lines[k].x1 - lines[k].x0) * scale
        if checked and not LINE_RATIO[0] * extent <= w - lines[k].x0 * scale <= LINE_RATIO[1] * extent + 2:
            return None
        widest = max(widest, w)
        if k + 1 < len(lines):
            j = right(k, a, first_break(text, bounds[k + 1], end))
            if j is None:
                return None
            joins = min(joins, j)
    return widest, joins
