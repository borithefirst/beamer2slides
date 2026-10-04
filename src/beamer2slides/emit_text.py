"""Text boxes: run and line sizes, line pitches, the holes formula pictures sit in, box widths and
paragraph ends, and the requests that write them.

The planners work on emit's `SetRun`s and `SetParagraph`s (`emit_model`): `text_element_requests`
takes a parsed text element, `text_box_requests_of` any `SetText`. The dict entries
(`text_box_requests`, `run_sizes`, `in_sentence`, `hole_runs`, ...) serve the callers that still
hold dicts - tables, diagrams, the theme's layout texts, holes, deck_ir, the tests - and read them
once through `emit_model`.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from copy import copy
from dataclasses import dataclass, replace
from typing import Literal

from .emit_metrics import (
    ASCENT_EM, BASELINE_A, DESCENT_EM, LINE_EM, MIDDLE_BASELINE_EM, PAD_X, PX_PT, SOFT_BREAK, FontMapper,
    bullet_char_of, bullet_extent_of, bullet_level_of, bullet_preset_of, bullet_size_of, letter_face_style,
    letter_faces, u16,
)
from .emit_model import (
    ElementDict, JsonMap, Placeholder, PptxText, SetParagraph, SetRun, SetText, Shell, ShellParagraph, block_of, box_of,
    json_number, number_box, number_box_of, run_of, set_text, text_of,
)
from .emit_widths import (
    ADDED_SPACE, LINE_SEPARATOR, SCRIPT_SIZE, SMALL_CAPS_SIZE, WORD_JOINER, guessed_chars, held_starts, held_width_of, joined_runs,
    paragraph_dict, pdf_line_breaks_of, recorded_starts, runs_between, set_runs_of, slides_lines_of, slides_width_of,
)
from .fonts import cjk_font, font_info, google_font
from .google_types import (
    AffineTransform, BulletPreset, SlidesParagraphStyle, SlidesRequest, SlidesTextStyle, bullet_preset,
    slides_text_style,
)
from .gslides import EMU_PER_PT, emu, pt, text_color
from .ir import Align, Script
from .ir_types import Box, Number, TextElement
from .json_types import Json, JsonObject
from .typing_compat import assert_never


def small_caps_line(text: str, smallcaps: bool, z: float) -> float:
    """`line_size` of a run's text and small caps."""
    if smallcaps and text and all(c.islower() for c in text):
        return z * SMALL_CAPS_SIZE
    return z


def line_size(run: JsonMap, z: float) -> float:
    """`line_size_of` a run dict (deck_ir's runs read from a deck carry no font of the PDF's)."""
    text = run.get("text", "")
    return small_caps_line(text if isinstance(text, str) else "", bool(run.get("smallcaps")), z)


def line_size_of(run: SetRun, z: float) -> float:
    """The size Slides lays out a line holding this run at, when the run is its largest: its
    own - except a smallCaps run of lowercase letters only, which Slides draws wholly in the
    small font, at SMALL_CAPS_SIZE. One space, capital or comma in the run and the line takes
    the full size (tools/probe_text_fit_fonts.py, `line_size_pt`: PT Serif 26 small caps in a
    Lato 20 line - 'mm' lays out at 20.3, 'mm mm' and 'Mm' at 26.3; 34 pt 'mm' at 24.0)."""
    return small_caps_line(run.text, run.smallcaps, z)


def body_size(runs: Sequence[SetRun], sizes: Sequence[float]) -> float | None:
    """The Slides size most of a paragraph's characters are set at (its scripts and holes aside)."""
    count: dict[float, int] = {}
    for run, z in zip(runs, sizes):
        if not run.script and not run.hole and not run.hole_size and run.text.strip():
            count[z] = count.get(z, 0) + len(run.text.strip())
    return max(count, key=lambda z: (count[z], z)) if count else None


# A size holding this share of an item's characters is its text's, for the bullet's cap: one
# {\Large} word is not, but the prose between \texttt words is (real_ansible-meetup-201-beamer 23:
# "Keys DOCUMENTATION and RETURN are YAML" is 19 mono letters to 15 sans, and its ball came out
# at the mono size, 25% smaller than its neighbours').
BULLET_CAP_SHARE = 0.3


def bullet_cap(runs: Sequence[SetRun], sizes: Sequence[float]) -> float | None:
    """The largest Slides size at least BULLET_CAP_SHARE of a paragraph's characters are set at
    (scripts and holes aside), which its bullet may reach; None with no such characters."""
    count: dict[float, int] = {}
    for run, z in zip(runs, sizes):
        if not run.script and not run.hole and not run.hole_size and run.text.strip():
            count[z] = count.get(z, 0) + len(run.text.strip())
    total = sum(count.values())
    held = [z for z, n in count.items() if n >= BULLET_CAP_SHARE * total]
    return max(held) if held else None


def run_sizes(runs: Sequence[JsonMap], scale: float, fonts: FontMapper) -> list[float]:
    return run_sizes_of(set_runs_of(runs), scale, fonts)


def run_sizes_of(runs: Sequence[SetRun], scale: float, fonts: FontMapper) -> list[float]:
    """The Slides size of each run. A subscript is set no larger than the text around it
    (`body_size`): Slides lowers a SUBSCRIPT run 0.371 em of its own size, where TeX lowers one
    0.15 em (0.25 beside a superscript), so every point it grows reaches further into the line
    below; and FontMapper would give it more than the text (the size is the text's, the font a
    small optical cut, which FontMapper reads as wider per em - 23.4 pt against 21.2). At the
    text's size Slides draws its digits as tall as TeX's (0.665 x 0.71 em against cmss8's) and
    it is what a person typing a subscript gets (tools/probe_subscripts.py)."""
    sizes = [fonts.size_of(r, scale)[1] for r in runs]
    body = body_size(runs, sizes)
    if body is None:
        return sizes
    return [min(z, body) if r.script == "sub" else z for r, z in zip(runs, sizes)]


def baseline_offset(script: Script | None) -> Literal["NONE", "SUPERSCRIPT", "SUBSCRIPT"]:
    """A run's script as Slides' `baselineOffset` (NONE said, so a restyled run comes down)."""
    if script is None:
        return "NONE"
    match script:
        case "super":
            return "SUPERSCRIPT"
        case "sub":
            return "SUBSCRIPT"
        case _:
            assert_never(script)


def run_width(run: SetRun, z: float, scale: float, fonts: FontMapper) -> float:
    """About how wide Slides sets a run (pt): measured advances where there are some."""
    if run.hole_size:
        return len(run.text) * HOLE_SPACE_EM * run.hole_size
    width = slides_width_of([run], scale, fonts)
    return width if width is not None else len(run.text) * 0.5 * z * (SCRIPT_SIZE if run.script else 1.0)


def line_sizes(p: JsonMap, sizes: Sequence[float], scale: float, fonts: FontMapper) -> list[float]:
    return line_sizes_of(paragraph_dict(p), sizes, scale, fonts)


def line_sizes_of(p: SetParagraph, sizes: Sequence[float], scale: float, fonts: FontMapper) -> list[float]:
    """The size Slides lays out each of a paragraph's lines at: the largest run *on that line*
    (`line_size`). A wrapped paragraph whose runs differ in size - an inline formula's italic
    letters, a larger word - does not have that size on every line, and Slides' pitch from one
    line to the next is the first one's descent and the next one's ascent (0.227 and 0.968 of
    each's own size, tools/probe_subscripts.py `crowding`). Which runs land on which line is
    estimated from the PDF's line widths, the runs laid end to end at their Slides widths."""
    n = len(p.lines)
    sized = [line_size_of(r, z) for r, z in zip(p.runs, sizes)]
    if not sized:
        return [p.size * scale] * n
    if n == 1 or len(set(sized)) == 1:
        return [max(sized)] * n
    widths = [run_width(r, z, scale, fonts) for r, z in zip(p.runs, sizes)]
    total = sum(widths)
    spans = [max(1e-6, ln.x1 - ln.x0) for ln in p.lines]
    bounds = [0.0]
    for w in spans:
        bounds.append(bounds[-1] + w * total / sum(spans))
    out = [0.0] * n
    pos = 0.0
    for s, w in zip(sized, widths):
        a, b = pos, pos + w
        pos = b
        mid = (a + b) / 2
        for j in range(n):
            lo, hi = bounds[j], bounds[j + 1]
            # a run lies on the line holding its middle, and on any line it covers half an em of
            if lo <= mid < hi or (j == n - 1 and mid >= hi) or min(b, hi) - max(a, lo) > 0.5 * s:
                out[j] = max(out[j], s)
    common = body_size(p.runs, sized) or max(sized)
    return [z or common for z in out]


def extra_below(r: float, z: float) -> float:
    """Extra space lineSpacing r adds under a line of size z (negative when r < 1)."""
    return (r - 1) * LINE_EM * z if r >= 1 else -(1 - r) * 0.25 * LINE_EM * z


def extra_above(r: float, z: float) -> float:
    """lineSpacing >= 100% never moves a line down; below 100% it pulls the baseline up."""
    return 0.0 if r >= 1 else -(1 - r) * 0.75 * LINE_EM * z


def snap(v: float) -> float:
    """Slides lays lines out on whole CSS pixels (0.75 pt)."""
    return round(v / PX_PT) * PX_PT


def line_pitch(z: float, r: float, z2: float) -> float:
    """Baseline distance between wrapped lines of one paragraph (from a line of size z to one of
    size z2)."""
    return snap(inner_pitch(z, r, z2))


def inner_pitch(z1: float, r: float, z2: float) -> float:
    """Unsnapped baseline distance between two wrapped lines of one paragraph, of sizes z1 and z2."""
    if z1 == z2:
        return LINE_EM * z1 * r
    return DESCENT_EM * z1 + ASCENT_EM * z2 + extra_below(r, z1) + extra_above(r, z2)


def pitch_between(z1: float, r1: float, z2: float, r2: float, gap: float) -> float:
    """Baseline distance from the last line of one paragraph to the first line of the next, `gap`
    (spaceBelow + spaceAbove) apart. The step snaps to whole pixels as a whole, space included:
    over the hunt's r8 renders (18 boxes of 5-14 single-line paragraphs, Lato, Roboto Mono,
    Carlito, Fira Sans, 8.7-17.3 pt) snap(natural + gap) is 0.07 pt rms off Google's step, the
    natural pitch snapped and the gap added 0.30 (a listing's Lato 11.6 pt numbers 0.5 pt short
    a line, its Roboto Mono 13.4 pt code 0.5 pt long: r2_code_v3/v4)."""
    return snap(DESCENT_EM * z1 + ASCENT_EM * z2 + extra_below(r1, z1) + extra_above(r2, z2) + gap)


RATIO_RANGE = (0.5, 3.0)  # the lineSpacing ratios vertical_layout searches


def solve_increasing(f: Callable[[float], float], target: float, lo: float, hi: float) -> float:
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) < target else (lo, mid)
    return (lo + hi) / 2


HOLE_FONT, HOLE_SPACE_EM = "Roboto Mono", 0.6  # monospaced: a space is exactly 0.6 em


def sentence_words(runs: Sequence[SetRun]) -> tuple[bool, str | None]:
    """(whether the runs share their paragraph with other words or a formula, the Google font of
    its upright words when they are in one), for `in_sentence`."""
    if sum(1 for r in runs if r.text.strip() or r.hole) < 2:
        return False, None
    # (a CJK face is not a Latin text face: a math letter among Japanese words keeps its substitute)
    upright = {r.font for r in runs if r.text.strip() and not r.hole and not cjk_font(r.font)
               and (google_font(r.font) or (None, 0, True))[1:] == (400, False)}
    faces = {g[0] for f in upright if (g := google_font(f)) is not None}
    return True, sorted(upright)[0] if len(faces) == 1 else None


def math_letter_in(run: SetRun, words: str) -> bool:
    """Whether `run` is a math-font letter set in the Google font `words` of the words around it."""
    return bool(words) and not run.hole and font_info(run.font).family == "math" and not google_font(run.font) \
        and run.family == font_info(words).family


def in_sentence(runs: Sequence[JsonMap]) -> list[JsonMap]:
    """`in_sentence_of` run dicts (a table's cells, a hole's paragraphs), as dicts."""
    shared, words = sentence_words(set_runs_of(runs))
    if not shared:
        return list(runs)
    out: list[JsonMap] = []
    for r in runs:
        r = r if r.get("in_sentence") else {**r, "in_sentence": True}
        # (a run dict without a family is never the words' family: run_of would read it as sans)
        if words is not None and r.get("family") is not None and math_letter_in(run_of(r), words):
            r = {**r, "font": words}
        out.append(r)
    return out


def in_sentence_of(runs: Sequence[SetRun]) -> list[SetRun]:
    """A paragraph's runs, each marked `in_sentence` when another run with words or a formula
    shares the paragraph (a table cell, a node's line): FontMapper then leaves its letters' shape
    alone (`shape_ratio`). Sized to its own PDF width, a reference's author list ("J. Park, W.
    Zhang, et al. ", 0.905) or a journal abbreviation came out 10% larger than the title beside
    it, and a bullet with it: one size across a line matters more than one run's width. Copies,
    never the IR (sync diffs it).

    A math-font letter among words in a Google font the PDF itself uses (Calibri's Carlito,
    Fira Sans) is set in that font: classify gives it their family (`math_family`), and that
    family's substitute put a Lato-italic β between Carlito words, visibly heavier."""
    shared, words = sentence_words(runs)
    if not shared:
        return list(runs)
    out: list[SetRun] = []
    for r in runs:
        r = r if r.in_sentence else replace(r, in_sentence=True)
        if words is not None and math_letter_in(r, words):
            r = replace(r, font=words)
        out.append(r)
    return out


def hole_spaces(hole: float, z: float, scale: float) -> tuple[int, float]:
    """(how many no-break spaces a hole `hole` PDF pt wide takes at size z, the size that makes
    them exactly that wide)."""
    width = hole * scale
    n = max(1, math.ceil(width / (HOLE_SPACE_EM * z)))
    return n, round(width / (HOLE_SPACE_EM * n), 2)


def hole_run(run: JsonMap, scale: float, fonts: FontMapper) -> JsonObject:
    """`hole_run_of` a run dict, as a dict."""
    set_run = run_of(run)
    if set_run.hole is None:
        raise KeyError("hole")
    n, size = hole_spaces(set_run.hole, fonts.size_of(set_run, scale)[1], scale)
    return {**run, "text": " " * n, "hole_size": size}


def hole_run_of(run: SetRun, hole: float, scale: float, fonts: FontMapper) -> SetRun:
    """The gap under an inline formula picture `hole` PDF pt wide (and the word space after it):
    no-break spaces in a monospaced font, sized so they are exactly as wide as the formula and
    no taller than the line."""
    n, size = hole_spaces(hole, fonts.size_of(run, scale)[1], scale)
    return replace(run, text=" " * n, hole_size=size)


# Written after the word space in front of a hole, in that word's run. Slides keeps a space and
# the no-break spaces after it together (UAX #14's old "× GL"), so the word before a formula
# went down with it to the next line: "pointwise, / but ∫..." in a box 54 pt wider than
# "... pointwise, but" (r1_math_v2 s6; 'than', r3_textfx_v1 s3; 'of', real_beamer-monodromy s3).
# A zero-width space breaks there by UAX #14 (LB8) but not in Slides (r10), nor does an en,
# thin or punctuation space, nor NEL; a LINE SEPARATOR (U+2028) does, takes no room and reads
# back as itself (tools/probe_hole_break.py). deck_ir, merge.collapse_holes and the other
# readers drop it, and an old ZWSP (`emit_widths.HOLE_BREAKS`); pull writes nothing for them.
HOLE_BREAK: str = LINE_SEPARATOR


def hole_runs(runs: Sequence[JsonMap], scale: float, fonts: FontMapper) -> list[JsonMap]:
    """`hole_runs_of` run dicts, as dicts."""
    out: list[JsonMap] = []
    for r in runs:
        if r.get("hole"):
            last = out[-1] if out else None
            text = None if last is None else last.get("text")
            if HOLE_BREAK and last is not None and not last.get("hole") and isinstance(text, str) and text.endswith(" "):
                out[-1] = {**last, "text": text + HOLE_BREAK}
            out.append(hole_run(r, scale, fonts))
        else:
            out.append(r)
    return out


def hole_runs_of(runs: Sequence[SetRun], scale: float, fonts: FontMapper) -> list[SetRun]:
    """A paragraph's runs as its text box holds them: each hole its no-break spaces (hole_run),
    and HOLE_BREAK after a word space in front of one."""
    out: list[SetRun] = []
    for r in runs:
        if r.hole:
            if HOLE_BREAK and out and not out[-1].hole and out[-1].text.endswith(" "):
                out[-1] = replace(out[-1], text=out[-1].text + HOLE_BREAK)
            out.append(hole_run_of(r, r.hole, scale, fonts))
        else:
            out.append(r)
    return out


def held_paragraph(p: SetParagraph, scale: float, fonts: FontMapper) -> SetParagraph:
    """A paragraph as its text box holds it: runs in sentences, the spaces classify added kept only
    on lines they leave no wider than the PDF's (`within_budget`), each hole its no-break spaces, a
    word joiner where Slides would break inside a word (`joined_runs`), and the line starts
    classify recorded moved to that text (`held_starts`)."""
    budgeted = within_budget(replace(p, runs=tuple(in_sentence_of(p.runs))), scale, fonts)
    ir = list(budgeted.runs)
    held = joined_runs(hole_runs_of(ir, scale, fonts))
    return replace(budgeted, runs=kept_bullet(held, p.bullet is not None),
                   line_starts=held_starts(ir, held, budgeted.line_starts))


# The spaces classify adds (`emit_widths.ADDED_SPACE`) are each right for their gap, but a line is
# right only as a whole: Lato's wider letters already make up for its narrower spaces, and a
# centred title 'Mistake:  string types' with its thick space came out 3.5% wider than the PDF's,
# every word on it shifted (real_talksx s17, s23, s34, s41, s47, s74).
# How much wider than the PDF's line Slides may set it with its added spaces (of the PDF line's
# extent): a line wider than that is written with plain spaces.
SPACE_BUDGET = 0.01


def within_budget(p: SetParagraph, scale: float, fonts: FontMapper) -> SetParagraph:
    """The paragraph with the spaces classify added (ADDED_SPACE) taken out of every line that
    Slides would set wider than the PDF's line (plus SPACE_BUDGET of it) with them: all of a line's
    or none, so that its code words and sentences stand alike. A line Slides cannot measure, or
    whose words cannot be told (`line_bounds`), keeps none. Its `line_starts` follow the text."""
    text = "".join(r.text for r in p.runs)
    added = [(m.start(), m.end()) for m in ADDED_SPACE.finditer(text)]
    if not added:
        return p
    bounds = line_bounds(p, text, scale, fonts)
    drop: set[int] = set()
    for k, (a, b) in enumerate(bounds or []):
        mine = [(s, e) for s, e in added if a <= s < b]
        if mine and not line_fits(p, k, text, a, b, scale, fonts):
            drop.update(i for s, e in mine for i in range(s, e))
    if bounds is None:
        drop = {i for s, e in added for i in range(s, e)}
    if not drop:
        return p
    runs: list[SetRun] = []
    at = 0
    for r in p.runs:
        kept = "".join(ch for i, ch in enumerate(r.text, at) if i not in drop)
        at += len(r.text)
        if kept or not r.text:
            runs.append(replace(r, text=kept))
    starts = None if p.line_starts is None else \
        tuple(s - sum(1 for i in drop if i < s) for s in p.line_starts)
    return replace(p, runs=tuple(runs), line_starts=starts)


def line_bounds(p: SetParagraph, text: str, scale: float, fonts: FontMapper) -> list[tuple[int, int]] | None:
    """Each PDF line's characters in the paragraph's text: the whole of a one-line paragraph, a
    title's lines between its soft breaks, a wrapped paragraph's between the starts classify
    recorded (`recorded_starts`) or that TeX's widths find (`pdf_line_breaks_of`); None when
    they are not known."""
    if not p.lines:
        return None
    cuts = [i for i, ch in enumerate(text) if ch == SOFT_BREAK]
    if cuts:
        return list(zip([0, *(c + 1 for c in cuts)], [*cuts, len(text)])) if len(cuts) + 1 == len(p.lines) else None
    if len(p.lines) == 1:
        return [(0, len(text))]
    starts = recorded_starts(p, text) or pdf_line_breaks_of(p, scale, fonts)
    return None if starts is None else list(zip([0, *starts], [*starts, len(text)]))


def line_fits(p: SetParagraph, k: int, text: str, a: int, b: int, scale: float, fonts: FontMapper) -> bool:
    """Whether Slides sets line k (the characters a to b, as they are) no wider than the PDF's
    line plus SPACE_BUDGET of it: a hanging label's line from its start to the tab stop and its
    text from there (`p.tab_x0`), holes as wide as their no-break spaces (`held_width_of`)."""
    a += len(text[a:b]) - len(text[a:b].lstrip(" "))
    b = a + len(text[a:b].rstrip())
    line = p.lines[k]
    tab = text.find("\t", a, b)
    if tab >= 0:
        rest = _budget_width(runs_between(p.runs, tab + 1, b), scale, fonts)
        w = None if rest is None or p.tab_x0 is None else (p.tab_x0 - line.x0) * scale + rest
    else:
        w = _budget_width(runs_between(p.runs, a, b), scale, fonts)
    return w is not None and w <= (line.x1 - line.x0) * scale * (1 + SPACE_BUDGET)


# Monospaced faces a PDF names and Slides draws as they are (`fonts.google_font`), at their one
# advance (em): Courier New's every glyph is 1229/2048. Nobody probed them for `slides_width`, but
# a column grid needs no probe (real_talksx's \code words, Nimbus Mono -> Courier New).
FIXED_ADVANCE_EM = {"Courier New": 0.6}


def _budget_width(runs: Sequence[SetRun], scale: float, fonts: FontMapper) -> float | None:
    """`held_width_of` the runs, a run in a FIXED_ADVANCE_EM face at its advance; None where a run
    is in a face nobody measured."""
    total = 0.0
    for r in runs:
        family, size = fonts.size_of(r, scale)
        w = held_width_of([r], scale, fonts) if r.hole or family not in FIXED_ADVANCE_EM else \
            len(r.text) * FIXED_ADVANCE_EM[family] * size * (SCRIPT_SIZE if r.script else 1.0)
        if w is None:
            return None
        total += w
    return total


def kept_bullet(runs: Sequence[SetRun], bulleted: bool) -> tuple[SetRun, ...]:
    """The runs of a list item of one character ('R', real_presentation-biore s81) with a
    WORD_JOINER after it: its run's style is written in two parts like any other's, as one request
    over a whole paragraph restyles its bullet too (black on a teal list), and a style request
    cannot be cut inside one character (tools/probe_short_bullet.py). Readers drop the joiner."""
    if not bulleted or len("".join(r.text for r in runs)) != 1:
        return tuple(runs)
    last = max(i for i, r in enumerate(runs) if r.text)
    return tuple(replace(r, text=r.text + WORD_JOINER) if i == last else r for i, r in enumerate(runs))


LineSizes = Sequence[Sequence[float] | float]
"""Per paragraph its size, or the size of each of its lines (`line_sizes`)."""


def vertical_layout(paras: Sequence[JsonMap], baselines: Sequence[Sequence[float]],
                    sizes: LineSizes) -> tuple[list[float], list[float]]:
    """`vertical_layout_of` paragraph dicts (one list of baselines and one size each)."""
    if not len(paras) == len(baselines) == len(sizes):
        raise ValueError(f"vertical_layout: {len(paras)} paragraphs, {len(baselines)} baselines, {len(sizes)} sizes")
    return vertical_layout_of(baselines, sizes)


# Written on every list item that follows another (`list_spacing`): Slides' own boxes say
# COLLAPSE_LISTS, under which spaceAbove and spaceBelow between two list items are dropped
# (docs/calibration.md), and NEVER_COLLAPSE keeps them, as between any two paragraphs (what Drive's
# importer writes on every paragraph; adopt measured it on creandum-board's items: deck_ir
# `stacked_baseline`). `tools/probe_list_spacing.py` measures it on a box the API makes.
LIST_SPACING: Literal["NEVER_COLLAPSE"] = "NEVER_COLLAPSE"


def list_spacing(bulleted: Sequence[bool]) -> list[bool]:
    """Which paragraphs of a box are written `LIST_SPACING`: a list item right after another, whose
    spaceAbove (its gap to the item above, `vertical_layout_of`) a collapsing list would drop."""
    return [b and i > 0 and bulleted[i - 1] for i, b in enumerate(bulleted)]


def vertical_layout_of(baselines: Sequence[Sequence[float]], sizes: LineSizes) -> tuple[list[float], list[float]]:
    """lineSpacing ratio and spaceAbove per paragraph so Slides baselines land on the PDF's: a
    wrapped paragraph's ratio is its own lines' pitch, and the gap to the paragraph above is its
    spaceAbove - between two list items too, which are written `LIST_SPACING` so Slides keeps it.
    (Under COLLAPSE_LISTS the gap had to come from the upper item's lineSpacing, and for a wrapped
    item one ratio covered its inner lines and that gap: its continuation lines came out looser than
    the PDF's - monodromy s7 +10.3 pt on a 26.9 pt pitch - and the gap tighter.)

    Pitches snap to whole pixels, so each paragraph aims at the original position measured
    from where Slides will actually have put the previous one: rounding errors don't add up."""
    lines = [[s] * len(bl) if isinstance(s, (int, float)) else list(s) for s, bl in zip(sizes, baselines)]
    ratios, space_above = _vertical_pass(baselines, lines, None)
    for _ in range(3):  # a paragraph's ratio depends on the next one's (below 100% it moves up)
        estimate = ratios
        ratios, space_above = _vertical_pass(baselines, lines, estimate)
        if ratios == estimate:
            break
    return ratios, space_above


def _vertical_pass(baselines: Sequence[Sequence[float]], lines: Sequence[Sequence[float]],
                   estimate: Sequence[float] | None) -> tuple[list[float], list[float]]:
    ratios: list[float] = []
    space_above = [0.0] * len(baselines)
    pulled: dict[int, float] = {}  # paragraph -> lineSpacing < 1 that pulls it up to its target
    first = baselines[0][0]  # predicted Slides baseline of the current paragraph's first line
    for i, (bl, zs) in enumerate(zip(baselines, lines)):
        n = len(bl)
        z = zs[-1]  # the last line's size: what the next paragraph is spaced from
        uniform = len(set(zs)) == 1

        def inner(r: float) -> float:  # first to last baseline of this paragraph, unsnapped
            return (n - 1) * LINE_EM * z * r if uniform else sum(inner_pitch(a, r, b) for a, b in zip(zs, zs[1:]))

        has_next = i + 1 < len(baselines)
        next_r = estimate[i + 1] if estimate and has_next else 1.0
        if n > 1:
            r = (bl[-1] - bl[0]) / (n - 1) / (LINE_EM * z) if uniform else \
                solve_increasing(inner, bl[-1] - bl[0], *RATIO_RANGE)
        else:
            r = pulled.get(i, 1.0)
        r = round(min(3.0, max(0.5, r)), 4)
        ratios.append(r)
        last = first + ((n - 1) * line_pitch(z, r, z) if uniform else sum(line_pitch(a, r, b) for a, b in zip(zs, zs[1:])))
        if has_next:
            zn = lines[i + 1][0]
            gap = baselines[i + 1][0] - last - pitch_between(z, r, zn, 1.0, 0.0)
            if gap < -PX_PT and len(baselines[i + 1]) == 1:
                # Tighter than Slides' natural pitch (block title right above its body):
                # a lineSpacing below 100% moves the next single line up.
                pulled[i + 1] = max(0.5, 1 + gap / (0.75 * LINE_EM * zn))
            # (aimed unsnapped: the step snaps as a whole, its space included, `pitch_between`)
            rn = pulled.get(i + 1, next_r)
            unsnapped = DESCENT_EM * z + ASCENT_EM * zn + extra_below(r, z) + extra_above(rn, zn)
            space_above[i + 1] = max(0.0, baselines[i + 1][0] - last - unsnapped)
            first = last + pitch_between(z, r, zn, rn, space_above[i + 1])
    return ratios, space_above


def hugs(p: JsonMap) -> Align:
    return hugs_of(paragraph_dict(p))


def hugs_of(p: SetParagraph) -> Align:
    """Which page edge the paragraph's lines are drawn against, which `align` says for a
    left-to-right paragraph and understates for a right-to-left one: a Hebrew paragraph
    whose lines all end together hugs the **right**, and `align` calls that "left" because
    its lines also start together (justified prose) or because it has only one line, where
    nothing was measured at all. Slides is told an alignment relative to the reading
    direction, so it needs the edge, not the name."""
    if p.direction == "rtl" and p.align == "left" and \
            max(ln.x1 for ln in p.lines) - min(ln.x1 for ln in p.lines) <= 1:
        return "right"
    return p.align


Measured = list[tuple[float, float] | None]
"""Per paragraph of a box, `slides_lines`' (widest line, least joining edge), None where unmeasured."""


def box_lines(paras: Sequence[JsonMap], edges: Sequence[str], scale: float, fonts: FontMapper) -> Measured | None:
    return box_lines_of([paragraph_dict(p) for p in paras], edges, scale, fonts)


def box_lines_of(paras: Sequence[SetParagraph], edges: Sequence[str], scale: float, fonts: FontMapper) -> Measured | None:
    """Per paragraph of a left-aligned box: (right edge of its widest line, the least right edge
    at which a line would take its next word) in Slides pt as Slides sets its PDF lines
    (slides_lines), None for a paragraph that cannot be measured; None for a box whose lines
    are not drawn from their left edge."""
    if set(edges) != {"left"} or any(p.direction == "rtl" for p in paras):
        return None
    return [slides_lines_of(p, scale, fonts) if "".join(r.text for r in p.runs).strip() else (0.0, math.inf)
            for p in paras]


# Slides pt a measured box keeps free past its widest line. slides_width sums calibrated
# advances; Slides sets kerned glyphs and rounds, and on the r6 renders a line came out up to
# 0.6 pt wider than predicted: of the full lines 1.0 pt from the box's edge half wrapped their
# last word ("if" over "any."), none 1.55 pt or more from it did.
LINE_MARGIN = 2.5


def justified_right(paras: Sequence[SetParagraph], scale: float, widest: float, joins: float) -> float | None:
    """The right edge (Slides pt) of a measured box's text where its justified paragraphs can be
    written JUSTIFIED, or None where they cannot and are written ragged (START).

    JUSTIFIED sets every line but a paragraph's last out to the box's edge, so the edge is where
    the PDF's full lines end - never further: an edge `LINE_MARGIN` past Slides' widest line ran
    the lines through the panel or frame the PDF's stopped at (visual hunt r7: \\fcolorbox, block
    and tcolorbox bodies). Slides breaks a justified line as a ragged one, at natural spaces, so
    the edge must also leave `LINE_MARGIN` past the widest line as Slides sets it (`widest`, a
    line TeX shrank comes out longer than its PDF extent) and before the next line's first word
    (`joins`, a narrower substitute pulls a word up). Short of the PDF's edge for the second is
    fine; where no edge meets both, the words keep their lines and give up justification."""
    edges = justified_edges(paras, scale)
    if not edges:
        return None
    right = min(min(edges), joins - LINE_MARGIN)
    return right if right >= widest + LINE_MARGIN else None


def justified_edges(paras: Sequence[SetParagraph], scale: float) -> list[float]:
    """Where the full lines of a box's justified paragraphs end (Slides pt)."""
    return [max(ln.x1 for ln in p.lines[:-1]) * scale for p in paras if p.justified and len(p.lines) > 1]


def flowed_justified_right(paras: Sequence[SetParagraph], scale: float, fonts: FontMapper) -> float | None:
    """The right edge (Slides pt) for a box of justified paragraphs whose PDF lines could not be
    measured one by one (slides_lines: a word TeX hyphenated at a line's end, which Slides
    will not break, so its lines break elsewhere anyway): their full lines' own edge, when the
    words flowed into it `LINE_MARGIN` short of it take no more lines than the PDF's (the box
    would grow over what is under it); else None, and the box is ragged. A ragged paragraph
    of several lines in the box keeps it ragged: its breaks need room past its lines."""
    edges = justified_edges(paras, scale)
    if not edges or any(len(p.lines) > 1 and not p.justified for p in paras):
        return None
    right = min(edges)
    for p in paras:
        n = flowed_lines(p, scale, fonts, right - LINE_MARGIN, None)
        if n is None or n > len(p.lines):
            return None
    return right


def paragraph_ends(paras: Sequence[SetParagraph], measured: Measured | None, right: float,
                   justify_box: bool, scale: float, fonts: FontMapper) -> list[tuple[bool, float]]:
    """Per paragraph of a text box whose text ends at `right` (Slides pt): whether it is written
    JUSTIFIED, and its indentEnd (Slides pt), the room it keeps free before that edge.

    One edge cannot break every paragraph where TeX did when one paragraph's widest line as
    Slides sets it runs past where another's next word would join its line (visual hunt r7:
    "Office hours 14:00-15:30" 5.6 pt wider in Lato than in the PDF, and the paragraph beside
    it pulled a word up to the slide's edge; "Cambridge University / Press."). Such a paragraph
    ends at its own edge instead: the middle of its own range, at least `LINE_MARGIN` past its
    widest line. A justified paragraph ends where its full lines do (justified_right), short of
    it for its next word, and is ragged where no edge fits: over a box of justified paragraphs
    (`justify_box`) that is the box's own edge. Only a left-to-right box measured line by line
    (box_lines) has a paragraph edge; any other keeps its box's. In such a box a paragraph in a
    face nobody measured ends short of its PDF next word's join where the box's edge reaches it
    (`unmeasured_end`)."""
    out: list[tuple[bool, float]] = []
    for i, p in enumerate(paras):
        g = measured[i] if measured else None
        n = len(p.lines)
        justified = p.justified and hugs_of(p) == "left" and n > 1
        if measured is None or p.direction == "rtl":
            out.append((justified and justify_box, 0.0))
        elif g is not None and n > 1:
            widest, joins = g
            if justified:
                edge = min(max(ln.x1 for ln in p.lines[:-1]) * scale, joins - LINE_MARGIN, right)
                if edge >= widest + LINE_MARGIN:
                    out.append((True, right - edge))
                    continue
            if joins - LINE_MARGIN < right - 0.01:
                own = widest + max((joins - widest) / 2, LINE_MARGIN)
                out.append((False, max(0.0, right - own)))
            else:
                out.append((False, 0.0))
        elif justified and not justify_box:
            edge = max(ln.x1 for ln in p.lines[:-1]) * scale
            lines = flowed_lines(p, scale, fonts, edge - LINE_MARGIN, None) if edge <= right else None
            if lines is not None and lines <= n:
                out.append((True, right - edge))
            else:
                own = unmeasured_end(p, right, scale, fonts) if g is None else None
                out.append((False, 0.0 if own is None else right - own))
        elif justified and justify_box:
            out.append((True, 0.0))
        else:
            own = unmeasured_end(p, right, scale, fonts) if g is None else None
            out.append((False, 0.0 if own is None else right - own))
    return out


def unmeasured_end(p: SetParagraph, right: float, scale: float, fonts: FontMapper) -> float | None:
    """Where (Slides pt) a left-aligned wrapped paragraph in a face nobody measured ends
    when its box's text edge `right` reaches where its next line's first word would join its
    line in the PDF (`wrap_limit`, less `LINE_MARGIN`), or None to keep the box's edge.

    A box of such paragraphs is sized from its widest PDF line and the least room before any
    paragraph's next word; a wider paragraph beside a narrower one leaves the narrower one's
    edge past its own join (real_thesis-defense s4: an item ending at 403 pt, its next word
    joining at 443.9, in a box ending at 447.6 for an item 442 pt wide; 'sources' came up).
    It ends in the middle of its own range, never before its PDF lines as an unmeasured face
    may set them (`UNMEASURED_PAD` of each line, at least `UNMEASURED_EM`): a word joined up
    costs no line, a line wrapped adds one. None where no such edge stays short of the join."""
    limit = p.wrap_limit
    if not limit or len(p.lines) < 2 or hugs_of(p) != "left" or p.direction == "rtl" or \
            any(SOFT_BREAK in r.text for r in p.runs) or \
            all(r.hole_size or slides_width_of([r], scale, fonts) is not None for r in p.runs):
        return None  # (a measured face whose lines could not be measured may run wider: Lato for CM)
    joins = limit * scale - LINE_MARGIN
    if right <= joins:
        return None
    widest = max(ln.x1 for ln in p.lines) * scale
    keep = max(ln.x1 * scale + max(UNMEASURED_PAD * (ln.x1 - ln.x0) * scale, UNMEASURED_EM * p.size * scale)
               for ln in p.lines)
    end = max(widest + (limit * scale - widest) / 2, keep)
    return end if end < joins and end < right else None


def unhyphenated_room(paras: Sequence[SetParagraph], measured: Measured | None, left: float, right: float,
                      justify_box: bool, scale: float, fonts: FontMapper) -> float:
    """How much further right (Slides pt) a box whose text runs from `left` to `right` must end
    for no paragraph that could not be measured line by line to take more lines than the PDF's -
    at most an em of its text. Slides hyphenates nothing: a word TeX broke at a line's end moves
    whole, and a line more grows the box over what is under it (visual hunt: design-v9, 'Robots
    de-/ployed in 9 sites' in a KPI tile came out on three lines, 'sites' over the tile's edge).
    A justified paragraph written JUSTIFIED at its own edge (flowed_justified_right) is left as
    it is; a centred one is measured across the whole box."""
    grow = 0.0
    for i, p in enumerate(paras):
        n = len(p.lines)
        if n < 2 or (measured and measured[i] is not None) or (justify_box and p.justified) or \
                p.direction == "rtl" or hugs_of(p) not in ("left", "center"):
            continue
        centred = hugs_of(p) == "center"

        def lines_at(extra: float) -> int | None:
            # (called within this paragraph's turn of the loop: `p` and `centred` are its own)
            if centred:
                return flowed_lines(p, scale, fonts, right + extra - left - LINE_MARGIN, 0.0)
            return flowed_lines(p, scale, fonts, right + extra - LINE_MARGIN, None)

        now = lines_at(0.0)
        cap = p.size * scale
        if now is None or now <= n or (lines_at(cap) or n + 1) > n:
            continue
        lo, hi = 0.0, cap
        for _ in range(20):
            mid = (lo + hi) / 2
            got = lines_at(mid)
            lo, hi = (lo, mid) if got is not None and got <= n else (mid, hi)
        grow = max(grow, hi)
    return grow


def flowed_lines(p: SetParagraph, scale: float, fonts: FontMapper, right: float, start: float | None) -> int | None:
    """How many lines Slides sets a paragraph's words on in a box whose text ends at `right`
    (Slides pt): as many words on each line as fit, at their natural spaces, the first line
    starting where the PDF's does (a \\parindent) and the others at the paragraph's text edge -
    or every line at `start` (a centred paragraph, measured across its box). None when a run's
    advances are not known."""
    runs = p.runs
    text = "".join(r.text for r in runs)

    def width(a: int, b: int) -> float | None:
        total = 0.0
        for run in runs_between(runs, a, b):
            if run.hole_size:
                total += len(run.text) * HOLE_SPACE_EM * run.hole_size
                continue
            w = slides_width_of([run], scale, fonts)
            if w is None:
                return None
            total += w
        return total

    ends = [i for i, ch in enumerate(text) if ch == " " and i > 0 and text[i - 1] != " "] + [len(text.rstrip())]
    a, count = len(text) - len(text.lstrip()), 0
    while a < len(text.rstrip()):
        x0 = start if start is not None else (p.lines[0].x0 if count == 0 else p.text_x0) * scale
        best: int | None = None
        for b in (e for e in ends if e > a):
            w = width(a, b)
            if w is None:
                return None
            if x0 + w > right:
                break
            best = b
        if best is None:
            return None  # (a word longer than the line: Slides breaks it inside)
        count += 1
        a = best
        while a < len(text) and text[a] == " ":
            a += 1
    return count


@dataclass(frozen=True, kw_only=True)
class Page:
    """A slide's size (Slides pt), which every box emit writes is kept within where its words
    allow (`on_page`)."""
    width: float
    height: float


@dataclass(frozen=True, kw_only=True)
class Frame:
    """A text box as `_text_requests` sizes it (Slides pt), with each paragraph's (JUSTIFIED,
    indentEnd) (`paragraph_ends`) and the edge it is set against (`hugs_of`)."""
    x: float
    y: float
    w: float
    h: float
    ends: tuple[tuple[bool, float], ...]
    edges: tuple[Align, ...]


# A character slides_width guesses (guessed_chars) may come out this much (em) wider than its guess.
GUESSED_PAD_EM = 0.4
# Past a run in a face nobody measured (a Google font the PDF uses itself, so its widths are the
# PDF's up to kerning and rounding), room kept beyond its PDF extent: this share of it, at least
# UNMEASURED_EM, as much as a guessed character's (a footline's lone page number in Fira Sans keeps
# its left edge and its box ends at the page's, 09_metropolis_fira).
UNMEASURED_PAD = 0.05
UNMEASURED_EM = GUESSED_PAD_EM


def on_page(f: Frame, paras: Sequence[SetParagraph], bottom: float | None, page: Page, scale: float,
            fonts: FontMapper) -> Frame:
    """A text box `f` brought within `page` as far as no word moves or wraps: Slides sets the
    words from the box's left edge (PAD_X in) for left-aligned text, from its right edge for
    right-aligned, from its middle for centred, so

    * a box of left-aligned paragraphs gives up room past its words on the right: down to
      `LINE_MARGIN` past the widest line as Slides sets it (`_reach`), keeping each paragraph's
      own edge (its indentEnd shrinks by what the box does; a JUSTIFIED one gives up no more
      than its indentEnd, its lines being set out to that edge);
    * a box of right-aligned lines gives up room on the left, a box of centred lines the same on
      both sides (its middle stays), down to `LINE_MARGIN` past its widest line;
    * a top-aligned box (`bottom`: where its words end, None for one centred vertically) gives up
      the room under them.

    What it cannot give up stays: the insets beside words drawn at the page's edge, a box mixing
    alignments, paragraphs nobody can measure. Slides breaks a line where it no longer fits, so
    a narrower box keeps every line that fitted before, and joins none it had not."""
    x, w, h, ends = f.x, f.w, f.h, list(f.ends)
    edges: list[Align] = list(f.edges)
    aligns = set(edges)
    rtl = any(p.direction == "rtl" for p in paras)
    if x + w > page.width and aligns == {"left"} and not rtl:
        text_right = x + w - PAD_X
        cut = x + w - page.width
        for p, (justify, end) in zip(paras, ends):
            allow = end
            reach = None if justify else _reach(p, scale, fonts)
            if reach is not None:
                allow = max(end, text_right - reach - LINE_MARGIN)
            cut = min(cut, allow)
        if cut > 0:
            w -= cut
            ends = [(justify, max(0.0, end - cut)) for justify, end in ends]
        if x + w > page.width + 0.01:
            flush = flush_right(paras, w, page, scale, fonts)
            if flush is not None:
                x, w = flush
                edges = ["right" for _ in paras]
                ends = [(False, 0.0)] * len(paras)
    elif (x < 0 or x + w > page.width) and aligns <= {"center", "right"} and len(aligns) == 1 and not rtl and \
            all(p.bullet is None and not p.tab_x0 for p in paras):
        lines = [_own_lines(p, scale, fonts) for p in paras]
        if all(ls is not None for ls in lines):
            free = w - 2 * PAD_X - max(ln.slides for ls in lines if ls is not None for ln in ls) - LINE_MARGIN
            if aligns == {"center"}:
                cut = max(0.0, min(max(-x, x + w - page.width), free / 2))
                x, w = x + cut, w - 2 * cut
            else:
                cut = max(0.0, min(-x, free))
                x, w = x + cut, w - cut
    if bottom is not None and f.y + h > page.height:
        h = max(min(h, page.height - f.y), bottom - f.y)
    return Frame(x=x, y=f.y, w=w, h=h, ends=tuple(ends), edges=tuple(edges))


# How far apart (Slides pt) the right ends of a box's lines, and each line's Slides width and its
# PDF extent, may be for the box to be written right-aligned instead (`flush_right`).
FLUSH_TOL = 1.0


def flush_right(paras: Sequence[SetParagraph], w: float, page: Page, scale: float,
                fonts: FontMapper) -> tuple[float, float] | None:
    """(x, w) of a left-aligned box of single lines, `w` wide, that must reach past `page` to keep its words
    from wrapping (words set at the page's edge: a frame number in the footline), written
    right-aligned instead: the box ends `PAD_X` past where the PDF's lines end, and its room for a
    wider face lies on the left. Only where the lines end together, as the PDF draws them, and Slides
    sets each as wide as the PDF does to `FLUSH_TOL` (or in the PDF's own face), so its words land
    where the left-aligned box set them, within that. None where that does not hold or the box would
    still not fit."""
    lines = [_own_lines(p, scale, fonts) for p in paras]
    own = [ln for ls in lines if ls is not None for ln in ls]
    if any(ls is None or len(ls) != 1 or p.bullet is not None or p.tab_x0 for ls, p in zip(lines, paras)) or not own or \
            any(abs(ln.slides - (ln.x1 - ln.x0)) > FLUSH_TOL for ln in own if ln.measured):
        return None
    right = max(ln.x1 for ln in own)
    if right - min(ln.x1 for ln in own) > FLUSH_TOL or right + PAD_X > page.width:
        return None
    width = min(w, right + PAD_X)  # (its width, the room past the words now on their left; or to the page's edge)
    if width - 2 * PAD_X < max(ln.slides for ln in own) + LINE_MARGIN:
        return None
    return right + PAD_X - width, width


@dataclass(frozen=True, kw_only=True)
class OwnLine:
    """A line of a paragraph that Slides cannot break elsewhere (`_own_lines`), in Slides pt: where
    the PDF's starts and ends, and how wide Slides sets it (`measured`), or the PDF's extent and
    `UNMEASURED_PAD` for a face nobody measured."""
    x0: float
    x1: float
    slides: float
    measured: bool


def _own_lines(p: SetParagraph, scale: float, fonts: FontMapper) -> list[OwnLine] | None:
    """A paragraph's lines when they are its own (one line, or lines a soft break ends: a title's);
    None for one Slides wraps, or with a hole in a face nobody measured. A hanging label's line
    ("label<TAB>text") is measured from its label's start, its text from the tab stop
    (indentStart, `p.tab_x0`); None where the label would run past that stop."""
    text = "".join(r.text for r in p.runs)
    tab, tab_x0 = text.find("\t"), p.tab_x0
    if tab >= 0 and (text.count("\t") > 1 or not tab_x0 or len(p.lines) > 1):
        return None
    cuts = [i for i, ch in enumerate(text) if ch == SOFT_BREAK]
    if not cuts and len(p.lines) > 1 or cuts and len(cuts) + 1 != len(p.lines):
        return None
    bounds = [-1, *cuts, len(text)]
    out: list[OwnLine] = []
    for a, b, ln in zip(bounds, bounds[1:], p.lines):
        x0, x1 = ln.x0 * scale, ln.x1 * scale
        if tab >= 0 and tab_x0:
            label = _span_width(runs_between(p.runs, a + 1, tab), scale, fonts)
            rest = _span_width(runs_between(p.runs, tab + 1, b), scale, fonts)
            stop = tab_x0 * scale
            if label is None or rest is None or x0 + label + LINE_MARGIN > stop:
                return None
            out.append(OwnLine(x0=x0, x1=x1, slides=stop - x0 + rest, measured=True))
            continue
        runs = runs_between(p.runs, a + 1, b)
        w = _span_width(runs, scale, fonts)
        if w is None:
            if any(r.hole_size for r in runs):
                return None
            pad = max(UNMEASURED_PAD * (x1 - x0), UNMEASURED_EM * p.size * scale)
            out.append(OwnLine(x0=x0, x1=x1, slides=x1 - x0 + pad, measured=False))
        else:
            out.append(OwnLine(x0=x0, x1=x1, slides=w, measured=True))
    return out


def _span_width(runs: Sequence[SetRun], scale: float, fonts: FontMapper) -> float | None:
    """How wide Slides sets these runs (pt) at most: holes as their no-break spaces, a guessed
    advance with GUESSED_PAD_EM more; None where a face is not measured."""
    total = 0.0
    for run in runs:
        if run.hole_size:
            total += len(run.text) * HOLE_SPACE_EM * run.hole_size
            continue
        w = slides_width_of([run], scale, fonts)
        if w is None:
            return None
        total += w + guessed_chars([run], scale, fonts) * GUESSED_PAD_EM * fonts.size_of(run, scale)[1]
    return total


def words_right(text: SetText, scale: float, fonts: FontMapper) -> float | None:
    """How far right (Slides pt) the box of an upright text set against its left edge must reach for
    its words, as Slides sets them, not to wrap: past its widest line (`_reach`), `LINE_MARGIN` and
    the inset; what `on_page` cannot give up where the words themselves come within that of the
    page's edge. None for other texts, or lines nobody can measure."""
    paras = _prepared(text, scale, fonts)[0]
    if text.rotation or {hugs_of(p) for p in paras} != {"left"} or any(p.direction == "rtl" for p in paras):
        return None
    reaches = [_reach(p, scale, fonts) for p in paras]
    known = [r for r in reaches if r is not None]
    return max(known) + LINE_MARGIN + PAD_X if known and len(known) == len(reaches) else None


def _reach(p: SetParagraph, scale: float, fonts: FontMapper) -> float | None:
    """How far right (Slides pt) a left-aligned paragraph's words run as Slides sets its PDF lines:
    each line from where the PDF's starts (`slides_lines_of`, `_own_lines`), never short of the
    PDF's own extent. None where its lines are not known."""
    pdf_right = max(ln.x1 for ln in p.lines) * scale
    lines = _own_lines(p, scale, fonts)
    if lines is not None:
        return max([pdf_right] + [ln.x0 + ln.slides for ln in lines])
    text = "".join(r.text for r in p.runs)
    if any(r.hole_size for r in p.runs) or "\t" in text or SOFT_BREAK in text:
        return None
    g = slides_lines_of(p, scale, fonts)
    if g is None:
        return None
    return max(g[0] + guessed_chars(p.runs, scale, fonts) * GUESSED_PAD_EM * p.size * scale, pdf_right)


def text_box_requests(el: JsonMap, slide_id: str, object_id: str, scale: float, fonts: FontMapper) -> list[SlidesRequest]:
    """`text_box_requests_of` a text dict in a box of its own (no placeholder, no internal links, no
    block bar, right limit or marks): a layout's text (theme), the tests' texts."""
    return text_box_requests_of(text_of(el), slide_id, object_id, scale, fonts, None, None, None, None, None)


def text_element_requests(el: TextElement, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                          placeholder: Placeholder | None, page_slide: Mapping[int, str] | None,
                          bar: Sequence[float] | None, right_limit: float | None,
                          marks: Sequence[str] | None) -> list[SlidesRequest]:
    """The text box of a parsed text element (either stage)."""
    return text_box_requests_of(set_text(el), slide_id, object_id, scale, fonts, placeholder, page_slide, bar,
                                right_limit, marks)


def _prepared(text: SetText, scale: float, fonts: FontMapper
              ) -> tuple[list[SetParagraph], list[list[float]], list[float], list[float]]:
    """A text's paragraphs as a box writes them (runs in sentences, holes), with their runs' sizes,
    each paragraph's largest and the size its bullet may take."""
    paras = [held_paragraph(p, scale, fonts) for p in text.paragraphs]
    # A line is as tall as its largest run, as Slides lays it out (line_size: small caps), and a
    # subscript is no larger than its text (run_sizes).
    sized = [run_sizes_of(p.runs, scale, fonts) for p in paras]
    base_sizes = [max(zs) if p.runs else p.size * scale for p, zs in zip(paras, sized)]
    # A bullet is no larger than its item's text (`body_size`), not its largest run: one {\Large}
    # word or a superscript's optical cut grew that item's bullet over its neighbours'; nor smaller
    # than its prose when \texttt words outnumber it (`bullet_cap`).
    bullet_caps = [bullet_cap(p.runs, zs) or base for p, zs, base in zip(paras, sized, base_sizes)]
    return paras, sized, base_sizes, bullet_caps


def shell_route(text: SetText) -> bool:
    """Whether a text comes in the .pptx as a text shell (`text_shell_of`) instead of being created
    through the API: when it has bullets and every one is a bullet no preset draws (CHAR_BULLETS:
    beamer's ▶, which createParagraphBullets could only write as ➢), its bullets are the .pptx's
    `a:buChar`. A box mixing such bullets with others keeps today's path: a preset's glyphs are
    what the bullet metrics measured, a number needs Slides' autonumbering (never probed through a
    .pptx), and one box holds one kind of bullet in beamer anyway. Upright, left-to-right prose
    only, and no empty paragraph (an empty bulleted paragraph loses its bullet at the import)."""
    bullets = [p.bullet for p in text.paragraphs if p.bullet is not None]
    return bool(bullets) and not text.rotation and not text.code and \
        all(bullet_char_of(b) is not None for b in bullets) and \
        all(p.direction is None and "".join(r.text for r in p.runs) for p in text.paragraphs)


def text_shell_of(text: SetText, box: Box, scale: float, fonts: FontMapper) -> PptxText | None:
    """The text shell the .pptx carries for a text whose bullets no preset draws (`shell_route`;
    None for any other), at `box` (Slides pt; the API pass places and sizes it as a box it creates):
    one paragraph per paragraph, each a placeholder character in its bullet's size, colour and
    family, its bullet the character at 100% of it, its level relative to the shallowest bullet's.
    The API pass (`text_shell_requests`) puts each paragraph's words in front of its character and
    deletes the character, so no paragraph is ever empty, and styles the words as a box it made."""
    if not shell_route(text):
        return None
    paras, _, base_sizes, caps = _prepared(text, scale, fonts)
    least = min(p.level for p in paras if p.bullet is not None)
    out: list[ShellParagraph] = []
    for p, base, cap in zip(paras, base_sizes, caps):
        font = fonts.size_of(p.runs[0], scale)[0] if p.runs else "Lato"
        if p.bullet is None:
            out.append(ShellParagraph(level=0, char=None, color=None, size=round(base, 1), font=font,
                                      text_size=round(base, 1)))
            continue
        out.append(ShellParagraph(level=min(8, max(0, p.level - least)), char=bullet_char_of(p.bullet),
                                  color=p.bullet.color or (p.runs[0].color if p.runs else None),
                                  size=bullet_size_of(p.bullet, cap, scale, True), font=font,
                                  text_size=round(base, 1)))
    return PptxText(box=box, paragraphs=tuple(out))


def text_shell_requests(el: TextElement, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                        shell: Shell, page_slide: Mapping[int, str] | None, bar: Sequence[float] | None,
                        right_limit: float | None, marks: Sequence[str] | None) -> list[SlidesRequest]:
    """`text_shell_requests_of` a parsed text element (either stage)."""
    return text_shell_requests_of(set_text(el), slide_id, object_id, scale, fonts, shell, page_slide, bar, right_limit,
                                  marks)


def text_shell_requests_of(text: SetText, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                           shell: Shell, page_slide: Mapping[int, str] | None, bar: Sequence[float] | None,
                           right_limit: float | None, marks: Sequence[str] | None) -> list[SlidesRequest]:
    """`text_box_requests_of` for a text the .pptx brought as a shell (`text_shell_of`): the same
    box, words, styles and indents, written into the shell instead of a box created here, and no
    createParagraphBullets (the shell's bullets are the PDF's own)."""
    return _text_requests(text, slide_id, object_id, scale, fonts, None, shell, page_slide, bar, right_limit, marks,
                          None)


def text_box_requests_of(text: SetText, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                         placeholder: Placeholder | None, page_slide: Mapping[int, str] | None,
                         bar: Sequence[float] | None, right_limit: float | None,
                         marks: Sequence[str] | None) -> list[SlidesRequest]:
    """A text box for a text element. With `bar` (the PDF box of a block's title bar that this
    one-line text sits on) the box fills the bar and centres its text vertically, so the
    title stays in the middle of the bar when the block is resized. `right_limit` (PDF x) is
    how far a box of unwrapped left-aligned text may extend. `marks` highlights the hole runs,
    one colour each (measure_places). The box is sized for its words alone, wherever that puts
    it: `text_requests_on_page` keeps it on a slide."""
    return _text_requests(text, slide_id, object_id, scale, fonts, placeholder, None, page_slide, bar, right_limit,
                          marks, None)


def text_requests_on_page(planned: Sequence[SlidesRequest], el: TextElement, slide_id: str, object_id: str,
                          scale: float, fonts: FontMapper, placeholder: Placeholder | None, shell: Shell | None,
                          page_slide: Mapping[int, str] | None, bar: Sequence[float] | None,
                          right_limit: float | None, page: Page) -> list[SlidesRequest]:
    """A text element's requests as `text_element_requests` (`shell`: `text_shell_requests`)
    planned them, or, where that box reaches past `page`, the text planned again with its box
    kept on the page as far as its words allow (`on_page`: no word moves or wraps elsewhere).
    `placeholder` and `shell` say what the planned transform scales (their base size)."""
    base = (placeholder.base_w, placeholder.base_h) if placeholder is not None else \
        (shell.base_w, shell.base_h) if shell is not None else None
    if not past_page(planned, object_id, base, page):
        return list(planned)
    return _text_requests(set_text(el), slide_id, object_id, scale, fonts, placeholder, shell, page_slide, bar,
                          right_limit, None, page)


def past_page(reqs: Sequence[SlidesRequest], object_id: str, base: tuple[float, float] | None, page: Page) -> bool:
    """Whether the box `reqs` create (or, with `base` - the size the imported object had -, place
    by an ABSOLUTE transform) for `object_id` reaches past `page` (by 0.01 pt). A turned box is
    never said to: `on_page` leaves it alone."""
    for r in reqs:
        made, moved = r.get("createShape"), r.get("updatePageElementTransform")
        if made is not None and made.get("objectId") == object_id:
            props = made.get("elementProperties")
            size = None if props is None else props.get("size")
            t = None if props is None else props.get("transform")
            width = None if size is None else size.get("width")
            height = None if size is None else size.get("height")
            if t is None or width is None or height is None:
                return False
            w, h = width.get("magnitude", 0.0) / EMU_PER_PT, height.get("magnitude", 0.0) / EMU_PER_PT
        elif moved is not None and moved["objectId"] == object_id and moved["applyMode"] == "ABSOLUTE" and base:
            t = moved["transform"]
            w, h = base
        else:
            continue
        if t.get("shearX") or t.get("shearY"):
            return False
        sx, sy = t.get("scaleX", 1.0), t.get("scaleY", 1.0)
        x0, y0 = t.get("translateX", 0.0) / EMU_PER_PT, t.get("translateY", 0.0) / EMU_PER_PT
        x1, y1 = x0 + sx * w, y0 + sy * h
        return min(x0, x1) < -0.01 or min(y0, y1) < -0.01 or max(x0, x1) > page.width + 0.01 or \
            max(y0, y1) > page.height + 0.01
    return False


def turned_about(x: float, y: float, rotation: float, pivot: tuple[float, float]) -> AffineTransform:
    """The transform of a box whose corner is at (`x`, `y`) upright (Slides pt), turned `rotation`
    degrees clockwise on the page about `pivot`."""
    angle = math.radians(rotation)
    cos, sin = math.cos(angle), math.sin(angle)
    px, py = pivot
    tx = cos * (x - px) - sin * (y - py) + px
    ty = sin * (x - px) + cos * (y - py) + py
    return {"scaleX": round(cos, 6), "shearX": round(-sin, 6), "shearY": round(sin, 6), "scaleY": round(cos, 6),
            "unit": "EMU", "translateX": round(tx * EMU_PER_PT), "translateY": round(ty * EMU_PER_PT)}


def _bullet_requests(paras: Sequence[SetParagraph], texts: Sequence[str], object_id: str, scale: float,
                     fonts: FontMapper, base_sizes: Sequence[float], bullet_caps: Sequence[float]) -> list[SlidesRequest]:
    """A created box's words and bullets: the text with its levels as tabs, each paragraph's base
    style, then createParagraphBullets per range of one preset."""
    reqs: list[SlidesRequest] = []
    # Bullets: contiguous ranges with the same preset. createParagraphBullets consumes the
    # leading tabs and sets nesting levels relative to the range's shallowest paragraph, so a
    # range whose levels start above 0 begins with a dummy paragraph, deleted right after.
    levels = [bullet_level_of(p.bullet, p.level) if p.bullet is not None else 0 for p in paras]
    ranges: list[tuple[int, int, BulletPreset]] = []  # (first paragraph, last paragraph, preset)
    for i, p in enumerate(paras):
        preset = bullet_preset(bullet_preset_of(p.bullet), "a bullet") if p.bullet is not None else None
        if preset and ranges and ranges[-1][2] == preset and ranges[-1][1] == i - 1:
            ranges[-1] = (ranges[-1][0], i, preset)
        elif preset:
            ranges.append((i, i, preset))
    dummies = {first for first, last, _ in ranges if min(levels[first:last + 1]) > 0}
    starts_tabbed: list[int] = []
    parts: list[str] = []
    pos = 0
    for i, t in enumerate(texts):
        if i in dummies:
            parts.append("-")
            pos += 2
        starts_tabbed.append(pos)
        parts.append("\t" * levels[i] + t)
        pos += levels[i] + u16(t) + 1  # (every index below counts UTF-16 units, as Slides does: u16)
    reqs.append({"insertText": {"objectId": object_id, "text": "\n".join(parts), "insertionIndex": 0}})
    # A bullet keeps the text style it was created with, unless a later style request covers
    # its whole paragraph. So every paragraph first gets its base family and size, bulleted
    # ones the bullet's size and colour, and the runs are styled below in parts.
    for i, (p, start, level, size, cap) in enumerate(zip(paras, starts_tabbed, levels, base_sizes, bullet_caps)):
        family = fonts.size_of(p.runs[0], scale)[0] if p.runs else "Lato"
        length = level + u16("".join(r.text for r in p.runs))
        style: SlidesTextStyle = {"fontFamily": family, "fontSize": pt(size)}
        if p.bullet is not None:
            style["fontSize"] = pt(bullet_size_of(p.bullet, cap, scale, False))
            color = p.bullet.color or (p.runs[0].color if p.runs else None)
            if color:
                style["foregroundColor"] = text_color(color)
        if length:
            reqs.append({"updateTextStyle": {
                "objectId": object_id, "fields": ",".join(style), "style": style,
                "textRange": {"type": "FIXED_RANGE", "startIndex": start - (2 if i in dummies else 0),
                              "endIndex": start + length},
            }})
    for first, last, preset in reversed(ranges):
        start = starts_tabbed[first] - (2 if first in dummies else 0)
        end = starts_tabbed[last] + levels[last] + u16(texts[last])
        reqs.append({"createParagraphBullets": {
            "objectId": object_id, "bulletPreset": preset,
            "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": end},
        }})
        if first in dummies:
            reqs.append({"deleteText": {"objectId": object_id,
                                        "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": start + 2}}})
    return reqs


def _text_requests(text: SetText, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                   placeholder: Placeholder | None, shell: Shell | None, page_slide: Mapping[int, str] | None,
                   bar: Sequence[float] | None, right_limit: float | None,
                   marks: Sequence[str] | None, page: Page | None) -> list[SlidesRequest]:
    """`text_box_requests_of`, or with `shell` its words written into a text shell the .pptx
    carried (`text_shell_requests`); with `page`, its box kept on it (`on_page`)."""
    marks_left = list(marks or [])
    paras, sized, base_sizes, bullet_caps = _prepared(text, scale, fonts)
    per_line = [line_sizes_of(p, zs, scale, fonts) for p, zs in zip(paras, sized)]
    sizes = [max(ls) for ls in per_line]

    edges = [hugs_of(p) for p in paras]
    # (a centred or right-aligned paragraph's longest line can start left of its first line)
    left_pdf = min(p.bullet.bbox[0] if p.bullet is not None and p.direction != "rtl" else
                   min([p.text_x0] + ([ln.x0 for ln in p.lines] if e != "left" else []))
                   for p, e in zip(paras, edges))
    # (a right-to-left paragraph's bullet hangs right of its text, as a left-to-right one's
    # hangs left of it, so it is that side's edge)
    right_pdf = max([line.x1 for p in paras for line in p.lines] +
                    [p.bullet.bbox[2] for p in paras if p.bullet is not None and p.direction == "rtl"])
    first_baseline = paras[0].lines[0].baseline * scale
    last_baseline = paras[-1].lines[-1].baseline * scale

    baselines = [[line.baseline * scale for line in p.lines] for p in paras]
    ratios, space_above = vertical_layout_of(baselines, per_line)
    spaced = list_spacing([p.bullet is not None for p in paras])

    inner_w = (right_pdf - left_pdf) * scale
    # Titles carry their line breaks as soft breaks (SOFT_BREAK) and need no tight width.
    multiline = any(len(p.lines) > 1 and not any(SOFT_BREAK in r.text for r in p.runs) for p in paras)
    # Wrapped paragraphs need a width that breaks where TeX did: wide enough for the longest
    # line, narrower than where the next line's first word would fit. The middle of that range
    # tolerates the substitute font being a little wider or narrower. Single lines get room so
    # that a slightly wider font never wraps them.
    limits = [p.wrap_limit for p in paras if p.wrap_limit and not any(SOFT_BREAK in r.text for r in p.runs)]
    room = (min(limits) - right_pdf) * scale if limits else 0.0
    measured = box_lines_of(paras, edges, scale, fonts) if multiline else None
    known = [g for g in measured or [] if g is not None]
    justify_box = False  # whether the box's right edge is its justified paragraphs' own (justified_right)
    if not multiline:
        slack = max(0.15 * inner_w, 2 * max(sizes))
    elif known and len(known) == len(paras):
        # Each PDF line as Slides sets its words: a TeX-full line in a narrow column comes out a
        # few points wider in Lato, more than the room the PDF leaves before the next word.
        # Where one paragraph's widest line needs more than another's next word leaves (room
        # < 0), the line keeps its words: a word joined up costs no line, a wrapped one adds
        # one and the box grows over what is under it.
        widest, joins = max(g[0] for g in known), min(g[1] for g in known)
        inner_w = widest - left_pdf * scale
        # A paragraph whose next word joins short of the widest line ends at its own edge
        # (paragraph_ends): the others' room is to their own next words, or for lines with none
        # to the PDF's edge. (One beside a picture left every other line LINE_MARGIN, and Slides
        # wrapped two whose math ran 2-3 pt wider than measured: real_beamer-monodromy s14.)
        beyond = [g[1] for g in known if widest + 2 * LINE_MARGIN <= g[1] < math.inf]
        if joins < widest + 2 * LINE_MARGIN:
            joins = min(beyond) if beyond else max(widest + 2 * LINE_MARGIN, 2 * right_pdf * scale - widest)
        room = joins - widest
        slack = max(room / 2, LINE_MARGIN)
        right = justified_right(paras, scale, widest, joins)
        if right is not None:
            slack, justify_box = right - widest, True
    else:
        slack = room / 2 if room > 4 else 2 + 0.01 * inner_w
        if known:
            # Some paragraph could not be measured (a thin space, a symbol without CM metrics):
            # the PDF's extent still sizes the box, never narrower than the lines that were.
            slack = max(slack, max(g[0] for g in known) + LINE_MARGIN - left_pdf * scale - inner_w)
        right = flowed_justified_right(paras, scale, fonts) if measured is not None else None
        if right is not None:
            slack, justify_box = right - left_pdf * scale - inner_w, True
        if not text.code:
            slack += unhyphenated_room(paras, measured, left_pdf * scale, left_pdf * scale + inner_w + slack,
                                       justify_box, scale, fonts)
    ends = paragraph_ends(paras, measured, left_pdf * scale + inner_w + slack, justify_box, scale, fonts) \
        if multiline and not text.code else [(False, 0.0)] * len(paras)
    x = left_pdf * scale - PAD_X
    aligns = set(edges)
    if aligns == {"center"}:
        x -= slack / 2
    elif aligns == {"right"}:
        x -= slack
    z_first, z_last = per_line[0][0], per_line[-1][-1]
    y = first_baseline - (BASELINE_A + ASCENT_EM * z_first + extra_above(ratios[0], z_first))
    w = inner_w + 2 * PAD_X + slack
    h = last_baseline - y + DESCENT_EM * z_last + extra_below(ratios[-1], z_last) + 4
    if right_limit and not multiline and aligns == {"left"} and not any(SOFT_BREAK in r.text for p in paras for r in p.runs):
        # Room up to the block edge or the next element: text typed later wraps where a user
        # expects, not a few points after the converted words.
        w = max(w, right_limit * scale - x)
    middle = bool(bar) and placeholder is None and len(paras) == 1 and len(paras[0].lines) == 1 and ratios[0] == 1
    if middle and bar is not None:
        h = (bar[3] - bar[1]) * scale
        y = first_baseline - MIDDLE_BASELINE_EM * sizes[0] - h / 2
        if aligns == {"left"}:
            w = max(w, (bar[2] - 1) * scale - x)  # to the bar's end: the title wraps with the block

    if placeholder is not None:
        y += placeholder.dy
    if page is not None and not text.rotation:
        # (the words' bottom: emit's box keeps BOX_ROOM under it, which the page may take back)
        bottom = None if middle else last_baseline + DESCENT_EM * z_last + extra_below(ratios[-1], z_last)
        framed = on_page(Frame(x=x, y=y, w=w, h=h, ends=tuple(ends), edges=tuple(edges)), paras, bottom, page,
                         scale, fonts)
        x, w, h, ends, edges = framed.x, framed.w, framed.h, list(framed.ends), list(framed.edges)

    reqs: list[SlidesRequest]
    if placeholder is not None:
        # An existing layout placeholder (the slide title): its size is fixed at creation, so
        # it is resized through the transform's scale. Text is not scaled by that.
        reqs = [
            {"updatePageElementTransform": {"objectId": object_id, "applyMode": "ABSOLUTE", "transform": {
                "scaleX": w / placeholder.base_w, "scaleY": h / placeholder.base_h, "unit": "EMU",
                "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}}},
            {"updateShapeProperties": {"objectId": object_id, "fields": "contentAlignment,autofit.autofitType",
                                       "shapeProperties": {"contentAlignment": "TOP",
                                                           "autofit": {"autofitType": "NONE"}}}},
        ]
    elif shell is not None:
        # A text box the .pptx carried (text_shell_of): placed and sized as a placeholder is, and
        # brought to the front where a box created now would land.
        reqs = [
            {"updatePageElementTransform": {"objectId": object_id, "applyMode": "ABSOLUTE", "transform": {
                "scaleX": w / shell.base_w, "scaleY": h / shell.base_h, "unit": "EMU",
                "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}}},
            {"updatePageElementsZOrder": {"pageElementObjectIds": [object_id], "operation": "BRING_TO_FRONT"}},
        ]
        if middle:
            reqs.append({"updateShapeProperties": {"objectId": object_id, "fields": "contentAlignment",
                                                   "shapeProperties": {"contentAlignment": "MIDDLE"}}})
    else:
        transform: AffineTransform = {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                                      "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}
        if text.rotation and abs(abs(text.rotation) - 90) < 0.01:
            # Laid out in the text's own frame (classify.rotated_texts): turn the box onto the page.
            turn = 1 if text.rotation > 0 else -1
            transform = {"scaleX": 0, "scaleY": 0, "shearX": -turn, "shearY": turn, "unit": "EMU",
                         "translateX": round(-turn * y * EMU_PER_PT), "translateY": round(turn * x * EMU_PER_PT)}
        elif text.rotation:
            # Set upright about the middle of its words (a tilted box adopt wrote: marked.unturned):
            # turned back about it. (Written as a quarter turn, it stood hundreds of points off the page.)
            middle = ((left_pdf + right_pdf) / 2 * scale,
                      (first_baseline - ASCENT_EM * z_first + last_baseline + DESCENT_EM * z_last) / 2)
            transform = turned_about(x, y, text.rotation, middle)
        reqs = [{"createShape": {
            "objectId": object_id, "shapeType": "TEXT_BOX",
            "elementProperties": {
                "pageObjectId": slide_id,
                "size": {"width": emu(w), "height": emu(h)},
                "transform": transform,
            },
        }}]
        if middle:
            reqs.append({"updateShapeProperties": {"objectId": object_id, "fields": "contentAlignment",
                                                   "shapeProperties": {"contentAlignment": "MIDDLE"}}})

    texts = ["".join(r.text for r in p.runs) for p in paras]
    if shell is not None:
        # Each paragraph's words go in front of its placeholder character, which goes after
        # (never an empty paragraph: it would lose its bullet), the last paragraph first so the
        # earlier ones' indices hold. The bullets are the shell's: nothing styles a whole paragraph.
        for i in reversed(range(len(texts))):
            at, n = 2 * i, u16(texts[i])
            reqs.append({"insertText": {"objectId": object_id, "text": texts[i], "insertionIndex": at}})
            reqs.append({"deleteText": {"objectId": object_id, "textRange": {
                "type": "FIXED_RANGE", "startIndex": at + n, "endIndex": at + n + 1}}})
    else:
        reqs += _bullet_requests(paras, texts, object_id, scale, fonts, base_sizes, bullet_caps)

    # From here on indices refer to the final text, without tabs.
    pos = 0
    for p, t, ratio, above, cap, edge, zs, (justify, indent_end), listed in zip(
            paras, texts, ratios, space_above, bullet_caps, edges, sized, ends, spaced):
        p_start, p_end = pos, pos + u16(t)
        pos = p_end + 1
        start = p_start
        for run, z in zip(p.runs, zs):
            if not run.text:
                continue
            font, fields = fonts.style_of(run, scale)
            style: SlidesTextStyle = slides_text_style(font, "a run's font (FontMapper.style_of)")
            if "fontSize" in style:
                style["fontSize"] = pt(z)  # (a subscript no larger than its text: run_sizes)
            if run.hole_size:
                style = {"fontFamily": HOLE_FONT, "fontSize": pt(run.hole_size), "bold": False,
                         "italic": False}
                fields = ["fontFamily", "fontSize", "bold", "italic"]
            style["smallCaps"] = run.smallcaps
            style["foregroundColor"] = text_color(run.color)
            style["underline"] = run.underline
            style["strikethrough"] = run.strike
            style["baselineOffset"] = baseline_offset(run.script)
            fields = fields + ["smallCaps", "foregroundColor", "underline", "strikethrough", "baselineOffset"]
            if run.highlight:
                style["backgroundColor"] = text_color(run.highlight)
                fields.append("backgroundColor")
            if run.hole_size and marks_left:
                style["backgroundColor"] = text_color(marks_left.pop(0))
                fields.append("backgroundColor")
            written = ",".join(dict.fromkeys(fields))
            if run.link and run.link.startswith("#page="):
                target = page_slide.get(int(run.link[6:])) if page_slide else None
                if target:
                    style["link"] = {"pageObjectId": target}  # TOC entries jump to their slide
                    written += ",link"
            elif run.link:
                style["link"] = {"url": run.link}
                written += ",link"
            end = start + u16(run.text)
            # (one request over the whole paragraph would restyle its bullet too; the cut before
            # the last character, never inside its surrogate pair)
            cuts = [start, end - u16(run.text[-1]), end] \
                if p.bullet is not None and start == p_start and end == p_end and len(run.text) > 1 else [start, end]
            for c0, c1 in zip(cuts, cuts[1:]):
                reqs.append({"updateTextStyle": {
                    "objectId": object_id, "style": style, "fields": written,
                    "textRange": {"type": "FIXED_RANGE", "startIndex": c0, "endIndex": c1},
                }})
            for c0, c1, face in letter_faces(run.text, run.font, start):
                if not (p.bullet is not None and c0 == p_start and c1 == p_end):  # (its bullet's too)
                    reqs.append({"updateTextStyle": {
                        "objectId": object_id, "style": letter_face_style(face, run.bold), "fields": "weightedFontFamily",
                        "textRange": {"type": "FIXED_RANGE", "startIndex": c0, "endIndex": c1}}})
            start = end

        # Slides measures a paragraph from where it *starts*, which is the right edge of a
        # right-to-left one (classify.Paragraph.direction): START is that edge, and so is
        # indentStart. What classify measured is the page's own left and right, so both are
        # mirrored here, around the edge the paragraph hugs (`hugs`).
        rtl = p.direction == "rtl"
        # Code lines carry their indentation as leading spaces already.
        # A paragraph that hugs the other edge, or the middle, places itself: an indent would
        # only offset it.
        start_edge = "right" if rtl else "left"
        room = (right_pdf - max(ln.x1 for ln in p.lines)) if rtl else (p.text_x0 - left_pdf)
        text_indent = 0.0 if text.code or edge != start_edge else room * scale
        if p.bullet is not None:
            # Slides ends the bullet glyph a little before indentFirstLine.
            b_x0, b_x1, gap = bullet_extent_of(p.bullet, cap, scale, shell is not None)
            side = (right_pdf - b_x0) if rtl else (b_x1 - left_pdf)
            first_indent = side * scale + gap
        elif p.tab_x0 and not text.code and not rtl:
            # "label<TAB>content": a tab after the hanging label jumps to indentStart.
            # (a right-to-left label's tab lands where nothing in the PDF says: no hang)
            first_indent, text_indent = text_indent, (p.tab_x0 - left_pdf) * scale
        else:
            first_indent = text_indent
            if edge == start_edge and not rtl and not text.code and len(p.lines) > 1 and \
                    p.lines[0].x0 > p.text_x0 + 0.2 * p.size:
                # a first line set in by \parindent (classify.Paragraph.indent)
                first_indent += (p.lines[0].x0 - p.text_x0) * scale
        # Justified prose stays justified (classify.PageClassifier.is_justified) where its text
        # ends at its PDF lines' edge (justified_right, paragraph_ends); Slides leaves the last
        # line ragged, as TeX does. A paragraph whose breaks need an edge short of the box's
        # keeps the difference free (indentEnd), written only where there is one.
        end = round(indent_end, 2)
        paragraph: SlidesParagraphStyle = {
            "alignment": "JUSTIFIED" if justify else "CENTER" if edge == "center" else
                         "START" if (edge == "right") == rtl else "END",
            "lineSpacing": round(100 * ratio, 1),
            "spaceAbove": pt(round(above, 2)), "spaceBelow": pt(0),
            "indentStart": pt(round(text_indent, 2)),
            "indentFirstLine": pt(round(first_indent, 2)),
        }
        if end > 0.01:
            paragraph["indentEnd"] = pt(end)
        if rtl:
            paragraph["direction"] = "RIGHT_TO_LEFT"
        if listed:
            paragraph["spacingMode"] = LIST_SPACING  # (its spaceAbove is the gap to the item above)
        reqs.append({"updateParagraphStyle": {
            "objectId": object_id,
            "textRange": {"type": "FIXED_RANGE", "startIndex": p_start, "endIndex": max(p_end, p_start + 1)},
            "style": paragraph,
            "fields": "alignment,lineSpacing,spaceAbove,spaceBelow,indentStart,indentFirstLine" +
                      (",indentEnd" if end > 0.01 else "") + (",direction" if rtl else "") +
                      (",spacingMode" if listed else ""),
        }})
    return reqs


def number_box_requests(number: JsonMap, slide_id: str, object_id: str, scale: float,
                        fonts: FontMapper) -> list[SlidesRequest]:
    """`number_requests` of a ball's number dict."""
    run, center, height = number_box_of(number)
    return number_box_requests_of(run, center, height, slide_id, object_id, scale, fonts, None)


def number_requests(number: Number, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                    page: Page | None) -> list[SlidesRequest]:
    """The box of a parsed ball's number, kept within `page` where given."""
    run, center, height = number_box(number)
    return number_box_requests_of(run, center, height, slide_id, object_id, scale, fonts, page)


def number_box_requests_of(run: SetRun, center: tuple[float, float], height: float, slide_id: str, object_id: str,
                           scale: float, fonts: FontMapper, page: Page | None) -> list[SlidesRequest]:
    """A literal list number (`run`) centred on its ball picture (classify.literal_list_numbers),
    `height` PDF pt tall around `center`: a box around the ball's centre with centred text and
    contentAlignment MIDDLE. Lato digits are 0.72 em tall, so a baseline 0.362 em below the middle
    puts them in the middle too. A box that would reach past `page` (a ball at a slide's left edge)
    is narrowed about its middle, down to its number and `LINE_MARGIN` between the insets."""
    font, fields = fonts.style_of(run, scale)
    style = slides_text_style(font, "a number's font (FontMapper.style_of)")
    size = fonts.size_of(run, scale)[1]
    cx, cy = center[0] * scale, center[1] * scale
    w = height * scale + 2 * PAD_X + len(run.text) * size  # never wraps "(iv)"
    h = max(height * scale, LINE_EM * size + 2)
    if page is not None:
        over = max(w / 2 - cx, cx + w / 2 - page.width)
        number_w = _span_width([run], scale, fonts)
        least = 2 * PAD_X + LINE_MARGIN + (number_w if number_w is not None else len(run.text) * size)
        if over > 0:
            w = min(w, max(least, w - 2 * over))
    style["foregroundColor"] = text_color(run.color)
    return [
        {"createShape": {"objectId": object_id, "shapeType": "TEXT_BOX", "elementProperties": {
            "pageObjectId": slide_id, "size": {"width": emu(w), "height": emu(h)},
            "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                          "translateX": round((cx - w / 2) * EMU_PER_PT), "translateY": round((cy - h / 2) * EMU_PER_PT)}}}},
        {"updateShapeProperties": {"objectId": object_id, "fields": "contentAlignment,autofit.autofitType",
                                   "shapeProperties": {"contentAlignment": "MIDDLE", "autofit": {"autofitType": "NONE"}}}},
        {"insertText": {"objectId": object_id, "text": run.text, "insertionIndex": 0}},
        {"updateTextStyle": {"objectId": object_id, "style": style, "fields": ",".join(fields + ["foregroundColor"]),
                             "textRange": {"type": "ALL"}}},
        {"updateParagraphStyle": {"objectId": object_id, "textRange": {"type": "ALL"},
                                  "style": {"alignment": "CENTER", "lineSpacing": 100, "spaceAbove": pt(0),
                                            "spaceBelow": pt(0), "indentStart": pt(0),
                                            "indentFirstLine": pt(0)},
                                  "fields": "alignment,lineSpacing,spaceAbove,spaceBelow,indentStart,indentFirstLine"}},
    ]


def _block_head(e: JsonMap) -> bool:
    """A block's title bar: a shape of a block with no `title_bar` of its own."""
    return e["kind"] == "shape" and block_of(e) is not None and not e.get("title_bar")


def _round_top(e: JsonMap) -> bool:
    return e["shape"] == "ROUND_RECTANGLE" or (e["shape"] == "ROUND_2_SAME_RECTANGLE" and not e["flip"])


def _round_bottom(e: JsonMap) -> bool:
    return e["shape"] == "ROUND_RECTANGLE" or (e["shape"] == "ROUND_2_SAME_RECTANGLE" and bool(e["flip"]))


def _z_rank(e: JsonMap) -> int:
    """Creation order is z-order: title bars go above every body (shapes come first, then the rest)."""
    return 0 if e["kind"] == "shape" and not _block_head(e) else 1 if _block_head(e) else 2


def merge_blocks(elements: Sequence[ElementDict]) -> list[ElementDict]:
    """A block body (see classify.blocks) reaches up under its title bar, with the outline
    of the whole block: resizing the block as a group can then never open a gap between
    the two, and the body's shadow falls behind the whole block.

    The elements come back as the caller's own dicts (a merged body a copy), so a caller's
    element type stays its own."""
    heads = {block_of(e): e for e in elements if _block_head(e)}
    out: list[ElementDict] = []
    for el in elements:
        head = heads.get(block_of(el)) if el.get("title_bar") else None
        if head is None:
            out.append(el)
            continue
        x0, _, x1, y1 = box_of(el["bbox"], "bbox")
        top = box_of(el["title_bar"], "title_bar")[1]
        shape, flip = {(True, True): ("ROUND_RECTANGLE", False), (False, False): ("RECTANGLE", False),
                       (True, False): ("ROUND_2_SAME_RECTANGLE", False),
                       (False, True): ("ROUND_2_SAME_RECTANGLE", True)}[(_round_top(head), _round_bottom(el))]
        bbox: Json = [x0, top, x1, y1]
        merged = copy(el)
        merged["bbox"] = bbox
        merged["shape"] = shape
        merged["flip"] = flip
        merged["radius"] = max(json_number(el["radius"], "radius"), json_number(head["radius"], "radius"))
        out.append(merged)
    return sorted(out, key=_z_rank)
