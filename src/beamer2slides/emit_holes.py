"""Pictures placed by prediction: a formula picture over its hole, an overlay stretched over the
words it marks.

The planning reads records (`HoleSlide`: the slide's texts as `emit_model.HoleText`, its anchored
pictures as `Anchored`). Callers holding a slide dict - DeckPlan, checks, devtools/alignment, sync,
the tests - reach it through `slide_holes`, `fit_holes`, `formula_shifts`, `overlay_boxes`,
`hole_offset` and `space_shift`, which read the dict as emit always read it.
"""

from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass
from typing import TypeVar

from .classify_model import HOLE_PAD
from .emit_metrics import MATH_SPACE_EM, SOFT_BREAK, SYMBOL_ADVANCE_EM, FontMapper
from .emit_model import (
    Anchored, Gap, HoleParagraph, HoleText, JsonMap, SetRun, anchored_of, box_of, gap_of, hole_paragraph_of,
    hole_text_of, json_number, mark_of, objects_of, point_of, run_of,
)
from .emit_text import held_paragraph
from .emit_widths import GUESSED_RUN_CHARS, guessed_chars, held_index, held_width_of, recorded_starts, runs_between
from .fonts import google_font
from .ir_types import BeforeWord, Box, Color, Mark
from .json_types import Json, JsonObject

SlideDict = TypeVar("SlideDict", bound=JsonObject)
"""A slide dict a step gives back rewritten: the caller's own type."""


@dataclass(frozen=True, kw_only=True)
class HoleSlide:
    """What placing pictures at words reads of a slide."""
    texts: tuple[HoleText, ...]
    """Its text elements, in slide order."""
    pictures: tuple[Anchored, ...]
    """Its pictures anchored to a text or marked as overlays, in slide order."""


@dataclass(frozen=True, kw_only=True)
class Hole:
    """A formula hole on a slide and the picture that goes over it."""
    where: tuple[int, int, int]
    """(text, paragraph, run): its place among the slide's texts."""
    paragraph: HoleParagraph
    run: SetRun
    gap: Gap
    picture: Anchored | None


def _elements(slide: JsonMap) -> list[JsonObject]:
    return objects_of(slide["elements"], "elements")


def hole_slide_dicts(slide: JsonMap) -> tuple[HoleSlide, list[JsonObject], list[JsonObject]]:
    """A slide dict read as its holes and overlays are placed from it, and the dicts it read:
    (the slide, its text dicts, its picture dicts), each list in the record's order."""
    elements = _elements(slide)
    texts = [e for e in elements if e["kind"] == "text"]
    read = tuple(hole_text_of(e) for e in texts)
    # (a formula picture is read only where a text it may belong to has a hole, as emit always did)
    holed = {t.id for t in read if any(g is not None for p in t.paragraphs for g in p.gaps)}
    pictures = [e for e in elements if e["kind"] == "image" and (e.get("marks") or e.get("anchor") in holed)]
    return HoleSlide(texts=read, pictures=tuple(anchored_of(e) for e in pictures)), texts, pictures


def text_right_limit(el: JsonMap, slide: JsonMap) -> float | None:
    """How far right (PDF x) a text element's box may reach: inside a panel (a block body),
    as far from the panel's right edge as the text is from its left edge; elsewhere the
    mirrored left margin of the page, stopping short of anything to the right on the same
    lines (the other column, a picture)."""
    if el.get("role") not in ("body", "title", None) or not el["paragraphs"] or el.get("rotation"):
        return None
    x0, y0, x1, y1 = box_of(el["bbox"], "bbox")
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    elements = _elements(slide)
    shapes = [box_of(e["bbox"], "bbox") for e in elements if e["kind"] == "shape" and e.get("role") == "panel"]
    panels = [b for b in shapes if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]]
    if panels:
        px0, _, px1, _ = min(panels, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        limit = px1 - max(x0 - px0, 2.0)
    else:
        margin = min((box_of(e["bbox"], "bbox")[0] for e in elements if e["kind"] == "text"
                      and e.get("role") in ("body", None) and not e.get("rotation")),
                     default=x0)
        limit = point_of(slide["size"], "size")[0] - margin
        size = json_number(objects_of(el["paragraphs"], "paragraphs")[0]["size"], "size")
        for o in elements:
            if o is el or o["kind"] == "shape":
                continue
            ox0, oy0, _, oy1 = box_of(o["bbox"], "bbox")
            if ox0 >= x1 - 1 and oy0 < y1 and oy1 > y0:
                limit = min(limit, ox0 - size)
    return limit if limit > x1 else None


def _gap_at(p: HoleParagraph, at: int) -> Gap:
    gap = p.gaps[at]
    if gap is None:
        raise ValueError(f"run {at} of the paragraph is no hole")
    return gap


def earlier_holes_of(p: HoleParagraph, at: int) -> list[tuple[float, float]]:
    """(x0, width) of the holes before run `at` on its line (PDF pt)."""
    gap = _gap_at(p, at)
    words = [b[6] for b in gap.before]
    if not words:
        return []
    return [(g.x0, g.width) for g in p.gaps[:at] if g is not None and min(words) <= g.x0 < gap.x0]


def space_shift(run: JsonMap, em: float, holes: Sequence[tuple[float, float]]) -> float:
    """`space_shift_of` a run dict (the tests): a run with no hole has no gap to shift. `holes`:
    see `space_shift_of`, () for none."""
    gap = gap_of(run)
    return 0.0 if gap is None else space_shift_of(run_of(run), gap, em, holes)


def space_shift_of(run: SetRun, gap: Gap, em: float, holes: Sequence[tuple[float, float]]) -> float:
    """What word spaces add to a formula gap's position in Slides. The substitute's size
    calibration makes its glyphs wider and its spaces narrower than TeX's, evening out at word
    ends; a formula starts after a space, so it lands one space deficit early. TeX also
    stretches some spaces (after a colon, around math): Slides sets a plain space there.
    A gap holding an earlier hole (`holes`) becomes a space and that hole's no-break spaces."""
    if google_font(run.font):
        return 0.0  # the PDF's own font: its spaces too
    words = sorted((b[6], b[6] + b[0]) for b in gap.before)
    if not words:
        return 0.0
    size, space = run.size, SYMBOL_ADVANCE_EM[" "] * em
    ends = [(a[1], b[0]) for a, b in zip(words, words[1:])] + [(words[-1][1], gap.x0)]
    shift = 0.0
    gaps: list[float | None] = []
    for a, b in ends:
        inside = [w for x, w in holes if a - 0.5 <= x < b]
        shift += sum(space + w for w in inside) - (b - a) if inside else 0.0
        gaps.append(None if inside else b - a)
    spaces = [g for g in gaps if g is not None and g > 0.15 * size]
    nominal = min(sorted(spaces)[len(spaces) // 2], 0.4 * size) if spaces else 0.0
    shift += sum(nominal - g for g in spaces)
    last = gaps[-1]
    if last is None:
        shift += space  # (the gap's own PDF space went with the hole before)
    elif last > 0.15 * size:
        shift += space - nominal
    return shift


def slide_holes_of(slide: HoleSlide) -> list[Hole]:
    """Every hole on a slide and its picture, in text order. A picture covers most of its hole
    (it usually starts where the hole does, but a radical's sign can reach further left) on one
    of the paragraph's lines; holes on two lines can have the same x: each takes the nearest
    picture not taken yet."""
    pictures = [e for e in slide.pictures if e.anchor]
    out: list[Hole] = []
    taken: set[str] = set()
    for ti, el in enumerate(slide.texts):
        for pi, p in enumerate(el.paragraphs):
            for ri, (run, gap) in enumerate(zip(p.runs, p.gaps)):
                if gap is None:
                    continue
                a, b = gap.x0 - HOLE_PAD, gap.x0 - HOLE_PAD + gap.width
                near = [e for e in pictures if e.anchor == el.id and e.id not in taken
                        and min(b, e.bbox[2]) - max(a, e.bbox[0]) > 0.5 * min(b - a, e.bbox[2] - e.bbox[0])]
                pic = min(near, default=None, key=lambda e: (min(
                    abs((e.bbox[1] + e.bbox[3]) / 2 - baseline + 0.35 * run.size) for baseline in p.baselines) // 4,
                    abs(e.bbox[0] + HOLE_PAD - gap.x0)))
                if pic is not None:
                    taken.add(pic.id)
                out.append(Hole(where=(ti, pi, ri), paragraph=p, run=run, gap=gap, picture=pic))
    return out


def slide_holes(slide: JsonMap) -> list[tuple[JsonObject, JsonObject, JsonObject, JsonObject | None]]:
    """`slide_holes_of` a slide dict: (text element, paragraph, hole run, its picture), the
    slide's own dicts (checks, devtools/alignment, sync, the tests)."""
    holes, texts, pictures = hole_slide_dicts(slide)
    out: list[tuple[JsonObject, JsonObject, JsonObject, JsonObject | None]] = []
    for h in slide_holes_of(holes):
        ti, pi, ri = h.where
        p = objects_of(texts[ti]["paragraphs"], "paragraphs")[pi]
        pic = h.picture
        out.append((texts[ti], p, objects_of(p["runs"], "runs")[ri],
                    None if pic is None else next(d for a, d in zip(holes.pictures, pictures) if a is pic)))
    return out


def hole_neighbours_of(p: HoleParagraph, at: int) -> tuple[float | None, float | None]:
    """PDF x where the word before hole `at` ends, if a space separates them (Slides has a word
    space there too), and where the word after it starts, if nothing separates them in the
    text (the hole reaches up to it)."""
    gap = _gap_at(p, at)
    words = [b[6] + b[0] for b in gap.before]
    prev_end = None
    if at > 0 and p.runs[at - 1].text.endswith(" ") and words and \
            not any(x >= max(words) for x, _ in earlier_holes_of(p, at)):
        prev_end = max(words)
    nxt = p.runs[at + 1].text if at + 1 < len(p.runs) else ""
    return prev_end, (gap.next_x0 if not nxt[:1].isspace() else None)


def fitted_width(h: Hole, scale: float, fonts: FontMapper) -> float | None:
    """A hole as wide as the PDF's room between the words around it, less the Slides word space
    before it, so the words after it keep their place; never narrower than its picture. None:
    it keeps its width."""
    prev_end, next_x0 = hole_neighbours_of(h.paragraph, h.where[2])
    pic = h.picture
    if pic is None:
        return None
    width = pic.bbox[2] - pic.bbox[0]
    if next_x0 is None:
        # (render grows a formula picture to its glyphs' ink: an integral's overhang)
        return round(width, 2) if width > h.gap.width else None
    start = prev_end + SYMBOL_ADVANCE_EM[" "] * fonts.size_of(h.run, scale)[1] / scale \
        if prev_end is not None else pic.bbox[0]
    return round(max(width, next_x0 - start), 2)


def fit_holes_of(slide: HoleSlide, scale: float, fonts: FontMapper) -> dict[tuple[int, int, int], float]:
    """The new width of each hole that changes (`fitted_width`), by its place (`Hole.where`)."""
    out: dict[tuple[int, int, int], float] = {}
    for h in slide_holes_of(slide):
        width = fitted_width(h, scale, fonts)
        if width is not None:
            out[h.where] = width
    return out


def fit_holes(slide: SlideDict, scale: float, fonts: FontMapper) -> SlideDict:
    """The slide dict with each hole as wide as `fit_holes_of` says; the same dict when none changes.
    (A copy of the caller's own type: DeckPlan's deck is still read as it comes.)"""
    holes, _, _ = hole_slide_dicts(slide)
    widths = fit_holes_of(holes, scale, fonts)
    if not widths:
        return slide
    elements: list[Json] = []
    ti = -1
    for el in _elements(slide):
        if el["kind"] != "text":
            elements.append(el)
            continue
        ti += 1
        paragraphs: list[Json] = []
        for pi, p in enumerate(objects_of(el["paragraphs"], "paragraphs")):
            runs: list[Json] = []
            for ri, r in enumerate(objects_of(p["runs"], "runs")):
                width = widths.get((ti, pi, ri))
                runs.append(r if width is None else {**r, "hole": width})
            paragraphs.append({**p, "runs": runs})
        elements.append({**el, "paragraphs": paragraphs})
    out = copy(slide)
    out["elements"] = elements
    return out


def hole_offset_of(p: HoleParagraph, at: int, pic: Box, scale: float, z: float) -> float:
    """Where a picture (its PDF box `pic`) sits in the gap of hole `at` (slide pt from the gap's
    start): the Slides word space before the gap and the gap's room after the picture are shared
    in the PDF's proportion."""
    prev_end, next_x0 = hole_neighbours_of(p, at)
    if prev_end is None:
        return 0.0
    x0, _, x1, _ = pic
    room = max(0.0, _gap_at(p, at).width - (x1 - x0)) * scale
    left = max(0.0, x0 - prev_end) * scale
    right = max(0.0, next_x0 - x1) * scale if next_x0 is not None else room
    space = SYMBOL_ADVANCE_EM[" "] * z
    return (space + room) * left / (left + right) - space if left + right > 0 else 0.0


def hole_offset(p: JsonMap, run: JsonMap, pic: JsonMap, scale: float, z: float) -> float:
    """`hole_offset_of` the dicts `slide_holes` gives (the tests)."""
    at = next(k for k, r in enumerate(objects_of(p["runs"], "runs")) if r is run)
    return hole_offset_of(hole_paragraph_of(p), at, box_of(pic["bbox"], "bbox"), scale, z)


def _same_words(before: Sequence[BeforeWord], head: str) -> bool:
    """Whether the words classify found before a hole on its PDF line (`Gap.before`) are about
    the text from that line's start to the hole: line starts recorded at the wrong words put a
    hole that opens its line after the forty letters before it."""
    got = sum(not ch.isspace() for b in before for ch in b[5])
    want = sum(not ch.isspace() for ch in head)
    return abs(got - want) <= max(2.0, 0.2 * want)


def slides_hole_x(h: Hole, scale: float, fonts: FontMapper) -> float | None:
    """Where Slides starts a hole's no-break spaces (slide pt), by the measured advances of what
    its text box holds before it on its PDF line, from where that line starts (its x0, or the
    tab stop after a hanging label): the line model the boxes are sized by (slides_lines_of).
    None where that is not known: another alignment, a stretched (justified) line, a line not
    recorded, a font the probe did not measure.

    The word-by-word estimate (formula_shift's) missed what TeX's math spacing and scripts add up
    to: a `k_t^⊤` after `S_t = S_{t-1} + v_t k` was put 23 pt right of the gap Slides left it,
    one at the end of a hanging "SGD update:" line 63 pt (real_linear-attention-a s35, s38). Both
    end their paragraphs, where Slides paints no highlight on the hole's spaces, and the measure
    by the line's ink (measure_places) accepts only a place near the predicted one: they stayed."""
    p = h.paragraph.setting
    if p is None or p.align != "left" or p.direction is not None or not p.lines:
        return None
    held = held_paragraph(p, scale, fonts)
    if len(held.runs) != len(p.runs):
        return None
    text = "".join(r.text for r in held.runs)
    at = held_index(p.runs, held.runs, sum(len(r.text) for r in p.runs[:h.where[2]]))
    starts: list[int] | None = [] if len(p.lines) == 1 else recorded_starts(held, text)
    if starts is None:
        return None
    k = sum(s <= at for s in starts)
    if p.justified and k < len(p.lines) - 1:
        return None  # (Slides stretches the line's spaces)
    begin = 0 if k == 0 else starts[k - 1]
    head = text[begin:at]
    if SOFT_BREAK in head or not _same_words(h.gap.before, head):
        return None
    x0 = p.lines[k].x0
    if "\t" in head:
        if p.tab_x0 is None:
            return None
        begin, x0 = begin + head.rindex("\t") + 1, p.tab_x0
    before = runs_between(held.runs, begin, at)
    if guessed_chars([r for r in before if not r.hole], scale, fonts) > GUESSED_RUN_CHARS:
        return None
    width = held_width_of(before, scale, fonts)
    return None if width is None else x0 * scale + width


def formula_shift(h: Hole, pic: Anchored, scale: float, fonts: FontMapper) -> float:
    """How far (PDF pt) the picture of a hole in a left-aligned paragraph moves to sit over the
    gap where Slides will put it: where Slides starts the hole (slides_hole_x), the picture
    keeping its place in the hole; else as the words before it on its line come out a little
    narrower or wider."""
    em = fonts.size_of(h.run, scale)[1] / scale  # the line's Slides font size, in PDF points
    at = h.where[2]
    offset = hole_offset_of(h.paragraph, at, pic.bbox, scale, em * scale) / scale
    x = slides_hole_x(h, scale, fonts)
    if x is not None:
        # (the hole starts HOLE_PAD left of its ink, gap.x0)
        return x / scale + offset - (h.gap.x0 - HOLE_PAD)
    shift = HOLE_PAD  # the gap has room for the picture's padding on its left too
    for w, font, family, bold, italic, text, _ in h.gap.before:
        if family == "math":
            # Math symbols come from Lato or Slides' fallback fonts, at their own widths.
            shift += sum(SYMBOL_ADVANCE_EM.get(ch, 0.55) for ch in text) * em - w
        else:
            spaces = len(text) - len(text.strip())
            # A math space inside the span (" 2," after ≥): TeX's thick space is wider.
            pdf_spaces = spaces * MATH_SPACE_EM * h.run.size
            shift += (w - pdf_spaces) * (fonts.width_ratio(font, family, bold, italic) - 1) + \
                spaces * SYMBOL_ADVANCE_EM[" "] * em - pdf_spaces
    return shift + space_shift_of(h.run, h.gap, em, earlier_holes_of(h.paragraph, at)) + offset


def formula_shifts_of(slide: HoleSlide, scale: float, fonts: FontMapper) -> dict[str, float]:
    """PDF-point x offsets for inline formula pictures (`formula_shift`), by picture id: those of
    0.2 pt or more."""
    out: dict[str, float] = {}
    for h in slide_holes_of(slide):
        pic = h.picture
        if h.paragraph.align != "left" or pic is None:
            continue
        shift = formula_shift(h, pic, scale, fonts)
        if abs(shift) >= 0.2:
            out[pic.id] = shift
    return out


def formula_shifts(slide: JsonMap, scale: float, fonts: FontMapper) -> dict[str, float]:
    """`formula_shifts_of` a slide dict."""
    return formula_shifts_of(hole_slide_dicts(slide)[0], scale, fonts)


OVERLAY_STRETCH = 0.1  # how much a graphic between words may widen or narrow with predicted words
OVERLAY_STRETCH_MEASURED = 0.3  # ... with measured ones (a brace spans its phrase)


def mark_drift(m: Mark, scale: float, fonts: FontMapper) -> float:
    """Predicted drift (PDF pt, Slides minus PDF) of an overlay's mark: a formula gap starting at
    the mark (formula_shifts_of)."""
    run = SetRun(text=" ", font=m.font, family=m.family, size=m.size, bold=m.bold, italic=m.italic, smallcaps=False,
                 script=None, color=Color("#000000"), underline=False, strike=False, highlight=None, link=None,
                 hole=1.0, hole_size=None, cell=False, in_sentence=False, pitch=None)
    gap = Gap(width=1.0, x0=m.hole_x0, before=m.before, next_x0=None)
    probe = HoleSlide(
        texts=(HoleText(id="mark", paragraphs=(
            HoleParagraph(align="left", baselines=(0.35 * m.size,), runs=(run,), gaps=(gap,), setting=None),)),),
        pictures=(Anchored(id="gap", bbox=(m.hole_x0 - HOLE_PAD, 0.0, m.hole_x0, 0.0), anchor="mark", marks=()),))
    # (the gap's picture starts HOLE_PAD before the gap; shifts under 0.2 pt are not reported)
    return formula_shifts_of(probe, scale, fonts).get("gap", HOLE_PAD) - HOLE_PAD + m.pads


def mark_drifts_of(marks: Sequence[Mark], scale: float, fonts: FontMapper) -> list[float]:
    """`mark_drift` of each of an overlay's marks."""
    return [mark_drift(m, scale, fonts) for m in marks]


def mark_drifts(el: JsonMap, scale: float, fonts: FontMapper) -> list[float]:
    """`mark_drifts_of` an overlay dict's marks."""
    return mark_drifts_of([mark_of(m) for m in objects_of(el["marks"], "marks")], scale, fonts)


def fit_overlay(bbox: Sequence[float], drifts: Sequence[tuple[float, float]], cap: float) -> tuple[float, float]:
    """PDF x extent of a picture following its marks' drifts ((x, drift)): moved by the mean
    drift and stretched by the least-squares slope (at most `cap`) when the marks are 10 pt apart or more."""
    xs = [x for x, _ in drifts]
    mx, md = sum(xs) / len(xs), sum(d for _, d in drifts) / len(drifts)
    spread = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (d - md) for x, d in drifts) / spread if max(xs) - min(xs) >= 10 else 0.0
    slope = max(-cap, min(cap, slope))
    x0, _, x1, _ = bbox
    return x0 + md + slope * (x0 - mx), x1 + md + slope * (x1 - mx)


def overlay_boxes_of(slide: HoleSlide, scale: float, fonts: FontMapper) -> dict[str, tuple[float, float]]:
    """PDF x extents for graphics drawn over or at words (classify.overlay: arrows, braces,
    callouts), following where Slides sets those words, as predicted (mark_drifts_of);
    measure_places corrects them on the thumbnail."""
    return {o.id: fit_overlay(o.bbox, list(zip([m.x for m in o.marks], mark_drifts_of(o.marks, scale, fonts))),
                              OVERLAY_STRETCH)
            for o in slide.pictures if o.marks}


def overlay_boxes(slide: JsonMap, scale: float, fonts: FontMapper) -> dict[str, tuple[float, float]]:
    """`overlay_boxes_of` a slide dict."""
    return overlay_boxes_of(hole_slide_dicts(slide)[0], scale, fonts)
