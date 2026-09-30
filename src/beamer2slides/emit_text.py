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
from dataclasses import replace

from .emit_metrics import (
    ASCENT_EM, BASELINE_A, DESCENT_EM, LINE_EM, MIDDLE_BASELINE_EM, PAD_X, PX_PT, SOFT_BREAK, FontMapper,
    bullet_extent_of, bullet_level_of, bullet_preset_of, bullet_size_of, rgb, u16,
)
from .emit_model import (
    ElementDict, JsonMap, Placeholder, SetParagraph, SetRun, SetText, block_of, box_of, json_number, number_box,
    number_box_of, run_of, set_text, text_of,
)
from .emit_widths import (
    SCRIPT_SIZE, SMALL_CAPS_SIZE, paragraph_dict, runs_between, set_runs_of, slides_lines_of, slides_width_of,
)
from .fonts import cjk_font, font_info, google_font
from .gslides import EMU_PER_PT, emu, pt
from .ir import Align
from .ir_types import Number, TextElement
from .json_types import Json, JsonObject


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
# "... pointwise, but" (r1_math_v2 s6; 'than', r3_textfx_v1 s3). A zero-width space breaks
# there by UAX #14 (LB8, ahead of LB12), as text_layout.wrap models it - but Slides does not:
# written live (r10), both words still went down with their holes. So none is written; deck_ir,
# merge.collapse_holes and the other readers still drop one (pull writes nothing for it).
HOLE_BREAK: str = ""


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


LineSizes = Sequence[Sequence[float] | float]
"""Per paragraph its size, or the size of each of its lines (`line_sizes`)."""


def vertical_layout(paras: Sequence[JsonMap], baselines: Sequence[Sequence[float]],
                    sizes: LineSizes) -> tuple[list[float], list[float]]:
    """`vertical_layout_of` paragraph dicts: of each it reads whether it has a bullet."""
    return vertical_layout_of([bool(p["bullet"]) for p in paras], baselines, sizes)


def vertical_layout_of(bulleted: Sequence[bool], baselines: Sequence[Sequence[float]],
                       sizes: LineSizes) -> tuple[list[float], list[float]]:
    """lineSpacing ratio and spaceAbove per paragraph (`bulleted`: whether each is a list item)
    so Slides baselines land on the PDF's.

    Slides ignores spaceAbove/spaceBelow between items of a bulleted list, so there the gap
    to the next item has to come from the item's own lineSpacing; for a wrapped item one
    ratio covers its inner lines plus that gap, spreading the difference evenly.

    Pitches snap to whole pixels, so each paragraph aims at the original position measured
    from where Slides will actually have put the previous one: rounding errors don't add up."""
    lines = [[s] * len(bl) if isinstance(s, (int, float)) else list(s) for s, bl in zip(sizes, baselines)]
    ratios, space_above = _vertical_pass(bulleted, baselines, lines, None)
    for _ in range(3):  # a paragraph's ratio depends on the next one's (below 100% it moves up)
        estimate = ratios
        ratios, space_above = _vertical_pass(bulleted, baselines, lines, estimate)
        if ratios == estimate:
            break
    return ratios, space_above


def _vertical_pass(bulleted: Sequence[bool], baselines: Sequence[Sequence[float]], lines: Sequence[Sequence[float]],
                   estimate: Sequence[float] | None) -> tuple[list[float], list[float]]:
    ratios: list[float] = []
    space_above = [0.0] * len(bulleted)
    pulled: dict[int, float] = {}  # paragraph -> lineSpacing < 1 that pulls it up to its target
    first = baselines[0][0]  # predicted Slides baseline of the current paragraph's first line
    for i, (bullet, bl, zs) in enumerate(zip(bulleted, baselines, lines)):
        n = len(bl)
        z = zs[-1]  # the last line's size: what the next paragraph is spaced from
        uniform = len(set(zs)) == 1

        def inner(r: float) -> float:  # first to last baseline of this paragraph, unsnapped
            return (n - 1) * LINE_EM * z * r if uniform else sum(inner_pitch(a, r, b) for a, b in zip(zs, zs[1:]))

        has_next = i + 1 < len(bulleted)
        list_link = has_next and bullet and bulleted[i + 1]
        next_r = estimate[i + 1] if estimate and has_next else 1.0
        if list_link:
            target = baselines[i + 1][0] - first
            zn = lines[i + 1][0]
            r = solve_increasing(lambda r: inner(r) + DESCENT_EM * z + ASCENT_EM * zn +
                                 extra_below(r, z) + extra_above(next_r, zn), target, *RATIO_RANGE)
        elif n > 1:
            r = (bl[-1] - bl[0]) / (n - 1) / (LINE_EM * z) if uniform else \
                solve_increasing(inner, bl[-1] - bl[0], *RATIO_RANGE)
        else:
            r = pulled.get(i, 1.0)
        r = round(min(3.0, max(0.5, r)), 4)
        ratios.append(r)
        last = first + ((n - 1) * line_pitch(z, r, z) if uniform else sum(line_pitch(a, r, b) for a, b in zip(zs, zs[1:])))
        if has_next:
            zn = lines[i + 1][0]
            natural = pitch_between(z, r, zn, next_r, 0.0)
            if not list_link:
                gap = baselines[i + 1][0] - last - pitch_between(z, r, zn, 1.0, 0.0)
                free = len(baselines[i + 1]) == 1 and not (bulleted[i + 1] and i + 2 < len(bulleted) and bulleted[i + 2])
                if gap < -PX_PT and free:
                    # Tighter than Slides' natural pitch (block title right above its body):
                    # a lineSpacing below 100% moves the next single line up.
                    rn = max(0.5, 1 + gap / (0.75 * LINE_EM * zn))
                    pulled[i + 1] = rn
                # (aimed unsnapped: the step snaps as a whole, its space included, `pitch_between`)
                rn = pulled.get(i + 1, next_r)
                unsnapped = DESCENT_EM * z + ASCENT_EM * zn + extra_below(r, z) + extra_above(rn, zn)
                space_above[i + 1] = max(0.0, baselines[i + 1][0] - last - unsnapped)
                natural = pitch_between(z, r, zn, rn, space_above[i + 1])
            first = last + natural
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
    (box_lines) has a paragraph edge; any other keeps its box's."""
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
            out.append((True, right - edge) if lines is not None and lines <= n else (False, 0.0))
        else:
            out.append((justified and justify_box, 0.0))
    return out


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


def text_box_requests(el: JsonMap, slide_id: str, object_id: str, scale: float, fonts: FontMapper) -> list[JsonObject]:
    """`text_box_requests_of` a text dict in a box of its own (no placeholder, no internal links, no
    block bar, right limit or marks): a layout's text (theme), the tests' texts."""
    return text_box_requests_of(text_of(el), slide_id, object_id, scale, fonts, None, None, None, None, None)


def text_element_requests(el: TextElement, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                          placeholder: Placeholder | None, page_slide: Mapping[int, str] | None,
                          bar: Sequence[float] | None, right_limit: float | None,
                          marks: Sequence[str] | None) -> list[JsonObject]:
    """The text box of a parsed text element (either stage)."""
    return text_box_requests_of(set_text(el), slide_id, object_id, scale, fonts, placeholder, page_slide, bar,
                                right_limit, marks)


def text_box_requests_of(text: SetText, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                         placeholder: Placeholder | None, page_slide: Mapping[int, str] | None,
                         bar: Sequence[float] | None, right_limit: float | None,
                         marks: Sequence[str] | None) -> list[JsonObject]:
    """A text box for a text element. With `bar` (the PDF box of a block's title bar that this
    one-line text sits on) the box fills the bar and centres its text vertically, so the
    title stays in the middle of the bar when the block is resized. `right_limit` (PDF x) is
    how far a box of unwrapped left-aligned text may extend. `marks` highlights the hole runs,
    one colour each (measure_places)."""
    marks_left = list(marks or [])
    paras = [replace(p, runs=tuple(hole_runs_of(in_sentence_of(p.runs), scale, fonts))) for p in text.paragraphs]
    # A line is as tall as its largest run, as Slides lays it out (line_size: small caps), and a
    # subscript is no larger than its text (run_sizes).
    sized = [run_sizes_of(p.runs, scale, fonts) for p in paras]
    base_sizes = [max(zs) if p.runs else p.size * scale for p, zs in zip(paras, sized)]
    # A bullet is no larger than its item's text (`body_size`), not its largest run: one {\Large}
    # word or a superscript's optical cut grew that item's bullet over its neighbours'.
    bullet_caps = [body_size(p.runs, zs) or base for p, zs, base in zip(paras, sized, base_sizes)]
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
    ratios, space_above = vertical_layout_of([p.bullet is not None for p in paras], baselines, per_line)

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

    reqs: list[JsonObject]
    if placeholder is not None:
        # An existing layout placeholder (the slide title): its size is fixed at creation, so
        # it is resized through the transform's scale. Text is not scaled by that.
        y += placeholder.dy
        reqs = [
            {"updatePageElementTransform": {"objectId": object_id, "applyMode": "ABSOLUTE", "transform": {
                "scaleX": w / placeholder.base_w, "scaleY": h / placeholder.base_h, "unit": "EMU",
                "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}}},
            {"updateShapeProperties": {"objectId": object_id, "fields": "contentAlignment,autofit.autofitType",
                                       "shapeProperties": {"contentAlignment": "TOP",
                                                           "autofit": {"autofitType": "NONE"}}}},
        ]
    else:
        transform: JsonObject = {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                                 "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}
        if text.rotation:
            # Laid out in the text's own frame (classify.rotated_texts): turn the box onto the page.
            turn = 1 if text.rotation > 0 else -1
            transform = {"scaleX": 0, "scaleY": 0, "shearX": -turn, "shearY": turn, "unit": "EMU",
                         "translateX": round(-turn * y * EMU_PER_PT), "translateY": round(turn * x * EMU_PER_PT)}
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
    # Bullets: contiguous ranges with the same preset. createParagraphBullets consumes the
    # leading tabs and sets nesting levels relative to the range's shallowest paragraph, so a
    # range whose levels start above 0 begins with a dummy paragraph, deleted right after.
    levels = [bullet_level_of(p.bullet, p.level) if p.bullet is not None else 0 for p in paras]
    ranges: list[tuple[int, int, str]] = []  # (first paragraph, last paragraph, preset)
    for i, p in enumerate(paras):
        preset = bullet_preset_of(p.bullet) if p.bullet is not None else None
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
        style: JsonObject = {"fontFamily": family, "fontSize": pt(size)}
        if p.bullet is not None:
            style["fontSize"] = pt(bullet_size_of(p.bullet, cap, scale))
            color = p.bullet.color or (p.runs[0].color if p.runs else None)
            if color:
                style["foregroundColor"] = rgb(color)
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

    # From here on indices refer to the final text, without tabs.
    pos = 0
    for p, t, ratio, above, cap, edge, zs, (justify, indent_end) in zip(paras, texts, ratios, space_above, bullet_caps,
                                                                       edges, sized, ends):
        p_start, p_end = pos, pos + u16(t)
        pos = p_end + 1
        start = p_start
        for run, z in zip(p.runs, zs):
            if not run.text:
                continue
            style, fields = fonts.style_of(run, scale)
            if "fontSize" in style:
                style["fontSize"] = pt(z)  # (a subscript no larger than its text: run_sizes)
            if run.hole_size:
                style = {"fontFamily": HOLE_FONT, "fontSize": pt(run.hole_size), "bold": False, "italic": False}
                fields = ["fontFamily", "fontSize", "bold", "italic"]
            style.update({"smallCaps": run.smallcaps, "foregroundColor": rgb(run.color),
                          "underline": run.underline, "strikethrough": run.strike,
                          "baselineOffset": "NONE" if run.script is None else
                          {"super": "SUPERSCRIPT", "sub": "SUBSCRIPT"}[run.script]})
            fields = fields + ["smallCaps", "foregroundColor", "underline", "strikethrough", "baselineOffset"]
            if run.highlight:
                style["backgroundColor"] = rgb(run.highlight)
                fields.append("backgroundColor")
            if run.hole_size and marks_left:
                style["backgroundColor"] = rgb(marks_left.pop(0))
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
            b_x0, b_x1, gap = bullet_extent_of(p.bullet, cap, scale)
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
        reqs.append({"updateParagraphStyle": {
            "objectId": object_id,
            "textRange": {"type": "FIXED_RANGE", "startIndex": p_start, "endIndex": max(p_end, p_start + 1)},
            "style": {
                "alignment": "JUSTIFIED" if justify else "CENTER" if edge == "center" else
                             "START" if (edge == "right") == rtl else "END",
                "lineSpacing": round(100 * ratio, 1),
                "spaceAbove": pt(round(above, 2)), "spaceBelow": pt(0),
                "indentStart": pt(round(text_indent, 2)), "indentFirstLine": pt(round(first_indent, 2)),
                **({"indentEnd": pt(end)} if end > 0.01 else {}),
                **({"direction": "RIGHT_TO_LEFT"} if rtl else {}),
            },
            "fields": "alignment,lineSpacing,spaceAbove,spaceBelow,indentStart,indentFirstLine" +
                      (",indentEnd" if end > 0.01 else "") + (",direction" if rtl else ""),
        }})
    return reqs


def number_box_requests(number: JsonMap, slide_id: str, object_id: str, scale: float,
                        fonts: FontMapper) -> list[JsonObject]:
    """`number_requests` of a ball's number dict."""
    run, center, height = number_box_of(number)
    return number_box_requests_of(run, center, height, slide_id, object_id, scale, fonts)


def number_requests(number: Number, slide_id: str, object_id: str, scale: float, fonts: FontMapper) -> list[JsonObject]:
    """The box of a parsed ball's number."""
    run, center, height = number_box(number)
    return number_box_requests_of(run, center, height, slide_id, object_id, scale, fonts)


def number_box_requests_of(run: SetRun, center: tuple[float, float], height: float, slide_id: str, object_id: str,
                           scale: float, fonts: FontMapper) -> list[JsonObject]:
    """A literal list number (`run`) centred on its ball picture (classify.literal_list_numbers),
    `height` PDF pt tall around `center`: a box around the ball's centre with centred text and
    contentAlignment MIDDLE. Lato digits are 0.72 em tall, so a baseline 0.362 em below the middle
    puts them in the middle too."""
    style, fields = fonts.style_of(run, scale)
    size = fonts.size_of(run, scale)[1]
    cx, cy = center[0] * scale, center[1] * scale
    w = height * scale + 2 * PAD_X + len(run.text) * size  # never wraps "(iv)"
    h = max(height * scale, LINE_EM * size + 2)
    style["foregroundColor"] = rgb(run.color)
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
                                  "style": {"alignment": "CENTER", "lineSpacing": 100, "spaceAbove": pt(0), "spaceBelow": pt(0),
                                            "indentStart": pt(0), "indentFirstLine": pt(0)},
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
