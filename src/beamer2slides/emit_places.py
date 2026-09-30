"""Pictures placed by measurement: scratch slides with coloured holes and words, read off Google's
thumbnails; panels grown to the words Slides sets.

The scratch slides are planned from records (`ScratchSlide` in, `ScratchJob` out) and a measured
place is a `Place`. `measure_jobs` and `measure_places` take a deck dict and DeckPlan's `placed`
(DeckPlan, sync, the tests), `grown_panels` a slide dict.
"""

import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from .arrays import Floats32, Mask, RGB
from .emit_holes import (
    OVERLAY_STRETCH_MEASURED, Hole, HoleSlide, SlideDict, fit_overlay, hole_neighbours_of, hole_offset_of,
    hole_slide_dicts, mark_drifts_of, slide_holes_of, text_right_limit,
)
from .emit_metrics import LINE_EM, PAD_X, SYMBOL_ADVANCE_EM, FontMapper, rgb, u16
from .emit_model import Anchored, JsonMap, Place, SetText, block_of, box_of, json_number, objects_of, point_of, text_of
from .emit_pptx import api_error, batch, template_key
from .emit_text import box_lines_of, hole_runs_of, hugs_of, in_sentence_of, text_box_requests_of
from .emit_widths import paragraph_dict, set_runs_of
from .gapi import HttpError
from .google_auth import credentials_for_threads, fetcher_for_threads, shared_service, slides_service
from .google_types import SlidesService
from .gslides import per_thread
from .ir_types import Box, Mark
from .json_types import Json, JsonObject

# The prediction (emit_holes) is off by several points now and then (fallback fonts, kerning,
# wraps). So each slide with holes or overlays gets a scratch copy of those text boxes on a white
# slide, every hole run and every word an overlay's marks lie on highlighted in a mark colour and
# all text black; Google's thumbnail of it shows where the gaps and words really are, and the
# pictures are moved (overlays also stretched) there before they are grouped.

HOLE_MARKS = ["#ff00ff", "#00ffff", "#ffff00", "#00ff00"]  # 0/255 channels only (mark_alpha)
MARK_CORE = 0.9  # a pixel at least this much covered by a mark is inside it

Rect = tuple[float, float, float, float]
"""A highlighted rectangle on a thumbnail (x0, y0, x1, y1), slide pt."""


def mark_alpha(img: RGB, color: str) -> Floats32:
    """How much of each pixel of an RGB thumbnail a highlight in `color` covers (white page).
    Dark glyph pixels have the colour's full channels low too: they count as uncovered."""
    c = [int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    px = img.astype(np.float32)
    full = np.min([px[..., i] for i in range(3) if c[i] == 255], axis=0)
    alpha = np.mean([(255 - px[..., i]) / 255 for i in range(3) if c[i] == 0], axis=0)
    return np.where(full >= 200, np.clip(alpha, 0.0, 1.0), 0.0)


def _runs(mask: Mask) -> list[tuple[int, int]]:
    """Index ranges [a, b] of consecutive True values."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    cuts = np.flatnonzero(np.diff(idx) > 1)
    return [(int(g[0]), int(g[-1])) for g in np.split(idx, cuts + 1)]


def _edges(profile: Floats32, a: int, b: int) -> tuple[float, float]:
    """Sub-pixel extent of a covered range [a, b]: partly covered neighbours add their share."""
    lo = a - (profile[a - 1] if a > 0 else 0.0)
    hi = b + 1 + (profile[b + 1] if b + 1 < profile.size else 0.0)
    return float(lo), float(hi)


def find_marks(alpha: Floats32, px_per_pt: float) -> list[Rect]:
    """Highlighted rectangles (x0, y0, x1, y1 in pt) in a mark_alpha map. A glyph lying over a
    highlight hides it only in some rows, so columns count as covered if any row of the band is."""
    out: list[Rect] = []
    core = alpha >= MARK_CORE
    for r0, r1 in _runs(core.sum(axis=1) >= 3):
        cols = alpha[r0:r1 + 1].max(axis=0)
        for c0, c1 in _runs(cols >= MARK_CORE):
            if c1 - c0 < 2:
                continue
            rows = alpha[:, c0:c1 + 1].max(axis=1)
            x0, x1 = _edges(cols, c0, c1)
            y0, y1 = _edges(rows, r0, r1)
            out.append((x0 / px_per_pt, y0 / px_per_pt, x1 / px_per_pt, y1 / px_per_pt))
    return out


def pick_gap(marks: Sequence[Rect], x0: float, cy: float, width: float, pitch: float) -> tuple[float, float] | None:
    """(dx, dy) from the predicted gap start `x0` and line middle `cy` (slide pt) to the mark as
    wide as the hole that lies nearest; dy is whole line pitches (a line Slides wrapped
    differently), 0 on the predicted line."""
    fits = [m for m in marks if abs((m[2] - m[0]) - width) <= max(1.5, 0.12 * width)]
    if not fits:
        return None
    m = min(fits, key=lambda m: abs(m[0] - x0) + abs((m[1] + m[3]) / 2 - cy))
    lines = round(((m[1] + m[3]) / 2 - cy) / pitch)
    return m[0] - x0, lines * pitch


def ink_end(img: RGB, px_per_pt: float, y0: float, y1: float, x0: float, x1: float) -> float | None:
    """Right end (pt) of the dark text pixels between rows y0..y1 and columns x0..x1 (pt)."""
    h, w = img.shape[:2]
    a, b = max(0, int(x0 * px_per_pt)), min(w, math.ceil(x1 * px_per_pt))
    band = img[max(0, int(y0 * px_per_pt)):min(h, math.ceil(y1 * px_per_pt)), a:b]
    if not band.size:
        return None
    cols = np.flatnonzero((band.max(axis=2) < 110).any(axis=0))
    return (a + cols[-1] + 1) / px_per_pt if len(cols) else None


def slides_texts_of(text: SetText, scale: float, fonts: FontMapper) -> list[str]:
    """Each paragraph's text as its text box ends up holding it (holes as their no-break spaces)."""
    return ["".join(r.text for r in hole_runs_of(p.runs, scale, fonts)) for p in text.paragraphs]


def slides_texts(el: JsonMap, scale: float, fonts: FontMapper) -> list[str]:
    """`slides_texts_of` a text dict: only its paragraphs' runs are read."""
    return ["".join(r.text for r in hole_runs_of(set_runs_of(objects_of(p["runs"], "runs")), scale, fonts))
            for p in objects_of(el["paragraphs"], "paragraphs")]


@dataclass(frozen=True, kw_only=True)
class MarkedWord:
    """Words an overlay's marks lie on, in its anchor's text as Slides holds it."""
    range: tuple[int, int]
    """(start, end) in UTF-16 units, as Slides counts (u16)."""
    marks: tuple[tuple[int, int], ...]
    """(mark index, 0 left edge / 1 right edge)."""
    width: float | None
    """The PDF width of the words a right edge ends; None when only left edges lie on them."""


WordKey = tuple[int, int, int]
"""Words in a text: (paragraph, first token, last token)."""


def mark_words(marks: Sequence[Mark], paras: Sequence[str]) -> list[MarkedWord]:
    """The words an overlay's marks lie on, in `paras`, the final text of its anchor's text box
    (slides_texts_of). A right edge ends the last word before it (the whole run of words before
    it on its line finds it); a left edge starts the word a right edge on the same line ends, or
    else the word after the ones before it. Marks whose words aren't found are left out."""
    starts = [sum(u16(t) + 1 for t in paras[:i]) for i in range(len(paras))]
    tokens = [[(u16(t[:m.start()]), u16(t[:m.end()]), m.group()) for m in re.finditer(r"\S+", t)] for t in paras]

    def locate(want: list[str]) -> tuple[int, int] | None:
        """(paragraph, index of the last token) of a run of words; shorter tails if unique."""
        for k in range(len(want), 0, -1):
            hits = [(pi, j) for pi, toks in enumerate(tokens) for j in range(k - 1, len(toks))
                    if [t[2] for t in toks[j - k + 1:j + 1]] == want[-k:]]
            if hits and (k == len(want) or len(hits) == 1):
                return hits[0]
        return None

    ranges: dict[WordKey, tuple[int, int]] = {}
    sides: dict[WordKey, list[tuple[int, int]]] = {}
    widths: dict[WordKey, float | None] = {}

    def add(key: WordKey, mark: tuple[int, int], width: float | None) -> None:
        pi, j0, j1 = key
        if key not in ranges:
            ranges[key] = (starts[pi] + tokens[pi][j0][0], starts[pi] + tokens[pi][j1][1])
            sides[key] = []
            widths[key] = width
        sides[key].append(mark)
        widths[key] = widths[key] or width

    ends: list[tuple[int, tuple[str, ...], float, WordKey]] = []  # (mark index, texts before the word, its closed x0, word key)
    lefts: list[int] = []
    for i, m in enumerate(marks):
        before = m.before
        if not (before and abs(before[-1][6] + before[-1][0] - m.hole_x0) <= 0.3):
            lefts.append(i)
            continue
        n = len(before[-1][5].split())
        hit = locate(" ".join(b[5] for b in before).split())
        if hit and n and hit[1] - n + 1 >= 0:
            key = (hit[0], hit[1] - n + 1, hit[1])
            ends.append((i, tuple(b[5] for b in before[:-1]), before[-1][6], key))
            add(key, (i, 1), before[-1][0])
    for i in lefts:
        m = marks[i]
        texts = tuple(b[5] for b in m.before)
        # (the same words before and x0 on two lines, "Stochastic" and "but" at the left margin:
        # classify lists a word's left edge right before its right edge)
        near = [(abs(j - i - 1), k) for j, t, x0, k in ends if t == texts and abs(x0 - m.hole_x0) <= 0.3]
        found: WordKey | None = min(near)[1] if near else None
        if found is None and texts:
            hit = locate(" ".join(texts).split())
            if hit and hit[1] + 1 < len(tokens[hit[0]]):
                found = (hit[0], hit[1] + 1, hit[1] + 1)
        if found is not None:
            add(found, (i, 0), None)
    return [MarkedWord(range=r, marks=tuple(sides[k]), width=widths[k]) for k, r in ranges.items()]


def pick_word(marks: Sequence[Rect], x0: float, width: float | None, top: float, bottom: float) -> Rect | None:
    """The highlighted word (slide pt) starting nearest the predicted `x0`, within its text box's
    rows top..bottom, about as wide as the PDF word if `width` is known, at most 25 pt away."""
    fits = [m for m in marks if top <= (m[1] + m[3]) / 2 <= bottom and abs(m[0] - x0) <= 25
            and (width is None or abs((m[2] - m[0]) - width) <= max(3.0, 0.3 * width))]
    return min(fits, key=lambda m: abs(m[0] - x0)) if fits else None


# The least room (em of the words' size) grown_panels keeps between a line's last word and its
# panel's right edge; beamer's block keeps 4 pt at 10.9 pt (0.37 em).
PANEL_MARGIN_EM = 0.5


def grown_panels(slide: SlideDict, scale: float, fonts: FontMapper) -> SlideDict:
    """The slide with each panel shape widened rightwards by as much as the words on it come
    out wider in Slides than in the PDF, so that they keep the PDF's inner margin (a block
    body in Lato runs a few points longer than in LM Sans, and its last word sat on the
    panel's edge or past it). A block's title bar and body grow together, never past the
    page's right edge; left-aligned text only (centred text grows both ways: its box does).

    A panel grows only where a line needs it: as far as the line keeps the panel's inner margin
    (the least of its words' left margins on it and the PDF's own right margin of that line),
    never further than the words grew. A short or ragged line with room to spare left the
    panel wider than the PDF's for nothing (r2_themes_v5 s3, r3_dense_v1 s10)."""
    els = objects_of(slide["elements"], "elements")
    panels = [i for i, e in enumerate(els) if e["kind"] == "shape" and e.get("role") == "panel"]
    if not panels:
        return slide

    def box(i: int) -> Box:
        # (read where it is needed: a panel with no box on a slide with no text on it is no step's fault)
        return box_of(els[i]["bbox"], "bbox")

    homes: list[tuple[int, JsonObject]] = []  # (panel index, text element on it)
    for el in els:
        if el["kind"] != "text" or el.get("role") == "title" or el.get("rotation") or not el["paragraphs"]:
            continue
        x0, y0, x1, y1 = box_of(el["bbox"], "bbox")
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        home = [i for i in panels if box(i)[0] <= cx <= box(i)[2] and box(i)[1] <= cy <= box(i)[3]]
        if home:
            homes.append((home[-1], el))  # (the topmost: creation order is z-order, and a block's shadow lies under it)
    # (a panel's inner margin: how close its words come to its left edge; a block's title bar and
    # body are one panel for this)
    blocks_of = {i: block_of(els[i]) for i in panels}
    group = {i: ("p", i) if b is None else ("b", b) for i, b in blocks_of.items()}
    pads: dict[tuple[str, int], float] = {}
    for i, el in homes:
        pad = box_of(el["bbox"], "bbox")[0] - box(i)[0]
        if pad >= 0:
            pads[group[i]] = min(pads.get(group[i], math.inf), pad)
    over: dict[int, float] = {}
    for i, el in homes:
        paras = [replace(p, runs=tuple(hole_runs_of(in_sentence_of(p.runs), scale, fonts)))
                 for p in map(paragraph_dict, objects_of(el["paragraphs"], "paragraphs"))]
        measured = box_lines_of(paras, [hugs_of(p) for p in paras], scale, fonts)
        if not measured or any(g is None for g in measured):
            continue  # (every paragraph measured: an unmeasured one could be the widest)
        right = max(line.x1 for p in paras for line in p.lines)
        edge = box(i)[2]
        if right >= edge:
            continue
        slides_right = max(g[0] for g in measured if g is not None) / scale
        # (a list's indent or a tcolorbox's wide padding is no margin the words need: half an em
        # keeps them off the edge)
        em = max(p.size for p in paras)
        margin = min(pads.get(group[i], 0.0), edge - right, PANEL_MARGIN_EM * em)
        grow = min(slides_right - right, slides_right + margin - edge)
        if grow > 0.5:
            over[i] = max(over.get(i, 0.0), grow)
    blocks: dict[int, float] = {}
    for i, g in over.items():
        b = blocks_of.get(i)
        if b is not None:
            blocks[b] = max(blocks.get(b, 0.0), g)
    for i in panels:
        b = blocks_of[i]
        if b is not None and b in blocks:
            over[i] = blocks[b]
    # (a block's shadow panel lies under the whole block, a few points right and down: it grows too)
    for b, g in blocks.items():
        union = [box(i) for i in panels if blocks_of[i] == b]
        ux0, uy0 = min(r[0] for r in union), min(r[1] for r in union)
        ux1, uy1 = max(r[2] for r in union), max(r[3] for r in union)
        for i in panels:
            if blocks_of[i] is None and i not in over:
                x0, y0, x1, y1 = box(i)
                if all(0 <= d <= 5 for d in (x0 - ux0, y0 - uy0, x1 - ux1, y1 - uy1)):
                    over[i] = g
    # (a panel drawn round a grown one and ending just right of it - a tcolorbox's frame-coloured
    # panel under its body - grows with it, or the frame's right side is covered: r1_design_v1 s6)
    for i, g in list(over.items()):
        ix0, iy0, ix1, iy1 = box(i)
        for j in panels:
            if j not in over:
                x0, y0, x1, y1 = box(j)
                if x0 <= ix0 + 0.5 and y0 <= iy0 + 0.5 and iy1 - 0.5 <= y1 and 0 <= x1 - ix1 <= 5:
                    over[j] = g
    if not over:
        return slide
    page_w = point_of(slide["size"], "size")[0]
    out: list[Json] = list(els)
    for i, g in over.items():
        e, (x0, y0, x1, y1) = els[i], box(i)
        g = min(g, page_w - 1 - x1)
        if g <= 0.5:
            continue
        wider: JsonObject = {**e, "bbox": [x0, y0, round(x1 + g, 2), y1]}
        bar = e.get("title_bar")
        if bar:
            b0, b1, b2, b3 = box_of(bar, "title_bar")
            wider["title_bar"] = [b0, b1, round(b2 + g, 2), b3]
        if template_key(wider, scale) == template_key(e, scale):  # (the template shapes are fixed)
            out[i] = wider
    grown = copy(slide)
    grown["elements"] = out
    return grown


def title_bar_under(el: JsonMap, slide: JsonMap) -> Box | None:
    """The PDF box of the block title bar a text element sits on, if any."""
    x0, y0, x1, y1 = box_of(el["bbox"], "bbox")
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for e in objects_of(slide["elements"], "elements"):
        if e["kind"] == "shape" and e.get("block") is not None and not e.get("title_bar"):
            b = box_of(e["bbox"], "bbox")
            if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
                return b
    return None


@dataclass(frozen=True, kw_only=True)
class ScratchText:
    """A text box copied onto a scratch slide."""
    at: int
    """Its place among the slide's texts (`HoleSlide.texts`)."""
    index: int
    """Its place among the slide's elements (its object id)."""
    text: SetText
    bbox: Box
    bar: Box | None
    """The block title bar it sits on (title_bar_under)."""
    right_limit: float | None
    """How far right its box may reach (text_right_limit)."""


@dataclass(frozen=True, kw_only=True)
class ScratchSlide:
    """A slide whose holes and overlays are measured, as its scratch slide is planned from it."""
    page: int
    texts: tuple[ScratchText, ...]
    """The texts holding a hole with a picture or anchoring an overlay, in slide order."""
    holes: HoleSlide
    placed: tuple[Box, ...]
    """Each of `holes.pictures` where the plan puts it (DeckPlan.placed), PDF pt."""


@dataclass(frozen=True, kw_only=True)
class GapToFind:
    """A hole to find on a scratch slide's thumbnail."""
    picture: str
    x0: float
    """The expected gap start, slide pt."""
    cy: float
    """The middle of the hole's line, slide pt."""
    width: float
    pitch: float
    colour: str
    hang: tuple[float, float, float] | None
    """A hole last on its PDF line, which Slides may let hang past the box edge undrawn: (the
    word space before it, the box's left and right edges), slide pt; None for the rest."""


@dataclass(frozen=True, kw_only=True)
class WordToFind:
    """Words an overlay's marks lie on (a MarkedWord), highlighted on a scratch slide."""
    range: tuple[int, int]
    marks: tuple[tuple[int, int], ...]
    width: float | None
    colour: str
    x0: float
    """Where the words are predicted to start, slide pt."""
    top: float
    bottom: float
    """The rows of their text box, slide pt."""


@dataclass(frozen=True, kw_only=True)
class OverlayToFit:
    """An overlay whose words are looked for on a scratch slide."""
    picture: Anchored
    box: tuple[float, float]
    """Its predicted PDF x extent (DeckPlan.placed)."""
    drifts: tuple[float, ...]
    """Its marks' predicted drifts (mark_drifts_of), PDF pt."""
    words: tuple[WordToFind, ...]


@dataclass(frozen=True, kw_only=True)
class ScratchJob:
    """What one scratch slide is read for."""
    slide: str
    page: int
    gaps: tuple[GapToFind, ...]
    overlays: tuple[OverlayToFit, ...]


def _measured(holes: HoleSlide) -> tuple[list[Hole], list[Anchored]]:
    """A slide's holes that have a picture, and its overlays anchored to one of its texts: what its
    scratch slide measures."""
    ids = {t.id for t in holes.texts}
    return ([h for h in slide_holes_of(holes) if h.picture is not None],
            [o for o in holes.pictures if o.marks and o.anchor in ids])


def measure_jobs(deck: JsonMap, scale: float, fonts: FontMapper, placed: Callable[[JsonObject, int], JsonObject],
                 page_slide: Mapping[int, str]) -> tuple[list[JsonObject], list[ScratchJob]]:
    """`measure_jobs_of` a deck dict, its pictures placed by `placed` (DeckPlan.placed)."""
    slides: list[ScratchSlide] = []
    for slide in objects_of(deck["slides"], "slides"):
        holes, texts, pictures = hole_slide_dicts(slide)
        found, overlays = _measured(holes)
        if not found and not overlays:
            continue
        n = int(json_number(slide["page"], "page"))
        anchors = {o.anchor for o in overlays}
        wanted = sorted({h.where[0] for h in found} | {i for i, t in enumerate(holes.texts) if t.id in anchors})
        index = {id(e): i for i, e in enumerate(objects_of(slide["elements"], "elements"))}
        scratch = tuple(ScratchText(at=i, index=index[id(texts[i])], text=text_of(texts[i]),
                                    bbox=box_of(texts[i]["bbox"], "bbox"), bar=title_bar_under(texts[i], slide),
                                    right_limit=text_right_limit(texts[i], slide)) for i in wanted)
        slides.append(ScratchSlide(page=n, texts=scratch, holes=holes,
                                   placed=tuple(box_of(placed(d, n)["bbox"], "bbox") for d in pictures)))
    return measure_jobs_of(slides, scale, fonts, page_slide)


def measure_jobs_of(slides: Sequence[ScratchSlide], scale: float, fonts: FontMapper, page_slide: Mapping[int, str]
                    ) -> tuple[list[JsonObject], list[ScratchJob]]:
    """The scratch slides of measure_places: their requests, and what each is read for."""
    reqs: list[JsonObject] = []
    jobs: list[ScratchJob] = []
    for slide in slides:
        holes, overlays = _measured(slide.holes)
        if not holes and not overlays:
            continue

        def placed(pic: Anchored) -> Box:
            return next(b for a, b in zip(slide.holes.pictures, slide.placed) if a is pic)

        n = slide.page
        sid = f"b2s_m{n:03}"
        reqs.append({"createSlide": {"objectId": sid, "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
        reqs.append({"updatePageProperties": {"objectId": sid, "fields": "pageBackgroundFill.solidFill.color",
                                              "pageProperties": {"pageBackgroundFill": {"solidFill": {
                                                  "color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}}}}})
        found: list[GapToFind] = []
        measured: list[OverlayToFit] = []
        taken: list[tuple[str, float, float, float, float]] = []  # (colour, x0, x1, y0, y1) of every highlight, slide pt
        for t in slide.texts:
            anchor = slide.holes.texts[t.at].id
            mine = [h for h in holes if h.where[0] == t.at]
            marked = [o for o in overlays if o.anchor == anchor]
            if not mine and not marked:
                continue
            colours = [HOLE_MARKS[(len(found) + k) % len(HOLE_MARKS)] for k in range(len(mine))]
            oid = f"{sid}_t{t.index}"
            plain = replace(t.text, paragraphs=tuple(replace(p, runs=tuple(replace(r, highlight=None) for r in p.runs))
                                                     for p in t.text.paragraphs))
            reqs += text_box_requests_of(plain, sid, oid, scale, fonts, None, page_slide, t.bar, t.right_limit, colours)
            reqs.append({"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"}, "fields": "foregroundColor",
                                             "style": {"foregroundColor": rgb("#000000")}}})
            top, bottom = t.bbox[1] * scale - 4, t.bbox[3] * scale + 4
            for h, colour in zip(mine, colours):
                if h.picture is not None:
                    x0, y0, x1, y1 = (v * scale for v in placed(h.picture))
                    taken.append((colour, x0, x1, y0, y1))
            for o in marked:
                bx0, _, bx1, _ = o.bbox
                a0, _, a1, _ = placed(o)
                words: list[WordToFind] = []
                for w in mark_words(o.marks, slides_texts_of(t.text, scale, fonts)):
                    m = o.marks[w.marks[0][0]]
                    x = m.x - (w.width or 0.0) * w.marks[0][1]  # the word's PDF x0
                    x0 = (a0 + (x - bx0) * (a1 - a0) / (bx1 - bx0)) * scale
                    x1 = x0 + (w.width or 0.0) * scale

                    def clash(c: str) -> float:
                        """How close the nearest highlight of colour c on these rows is."""
                        return min([max(0.0, s[1] - x1, x0 - s[2]) for s in taken
                                    if s[0] == c and s[3] < bottom and s[4] > top] + [math.inf])
                    colour = max(HOLE_MARKS, key=lambda c: (clash(c), -sum(s[0] == c for s in taken)))
                    taken.append((colour, x0, x1, top, bottom))
                    words.append(WordToFind(range=w.range, marks=w.marks, width=w.width, colour=colour, x0=x0, top=top,
                                            bottom=bottom))
                    reqs.append({"updateTextStyle": {"objectId": oid, "fields": "backgroundColor",
                                                     "style": {"backgroundColor": rgb(colour)},
                                                     "textRange": {"type": "FIXED_RANGE", "startIndex": w.range[0],
                                                                   "endIndex": w.range[1]}}})
                measured.append(OverlayToFit(picture=o, box=(a0, a1), drifts=tuple(mark_drifts_of(o.marks, scale, fonts)),
                                             words=tuple(words)))
            for h, colour in zip(mine, colours):
                pic = h.picture
                if pic is None:
                    continue
                x0, y0, _, y1 = placed(pic)
                z = fonts.size_of(h.run, scale)[1]
                bl = h.paragraph.baselines
                pitch = (bl[-1] - bl[0]) / (len(bl) - 1) * scale if len(bl) > 1 else LINE_EM * z
                at, size = h.where[2], h.run.size
                prev_end, _ = hole_neighbours_of(h.paragraph, at)
                # The middle of the hole's text line, not of the picture: a brace's label below
                # or above the formula would put that a line off.
                line = min(bl, key=lambda b: (not y0 - 0.5 <= b <= y1 + 0.5, abs((y0 + y1) / 2 - b + 0.35 * size)))
                found.append(GapToFind(
                    picture=pic.id, x0=x0 * scale - hole_offset_of(h.paragraph, at, pic.bbox, scale, z),  # expected gap start
                    cy=(line - 0.35 * size) * scale, width=h.gap.width * scale, pitch=pitch, colour=colour,
                    # last on its PDF line: a hole Slides lets hang past the box edge isn't drawn
                    hang=None if h.gap.next_x0 is not None else
                    (SYMBOL_ADVANCE_EM[" "] * z if prev_end is not None else 0.0,
                     t.bbox[0] * scale - PAD_X, t.bbox[2] * scale + 2 * PAD_X)))
        jobs.append(ScratchJob(slide=sid, page=n, gaps=tuple(found), overlays=tuple(measured)))
    return reqs, jobs


def overlay_move(o: OverlayToFit, marks: Mapping[str, Sequence[Rect]], scale: float
                 ) -> tuple[Place | None, list[list[float | None]]]:
    """The move and stretch taking an overlay picture from its predicted extent to the one its
    measured words give (fit_overlay; marks not measured keep their predicted drift), or None if
    no word was found; and [x, predicted drift, measured drift or None] per mark (PDF pt)."""
    got: dict[int, float] = {}
    for w in o.words:
        rect = pick_word(marks[w.colour], w.x0, w.width and w.width * scale, w.top, w.bottom)
        if rect is None:
            continue
        for i, side in w.marks:
            got[i] = rect[2 * side] / scale - o.picture.marks[i].x
    xs = [m.x for m in o.picture.marks]
    table: list[list[float | None]] = [[x, round(d, 2), round(got[i], 2) if i in got else None]
                                       for i, (x, d) in enumerate(zip(xs, o.drifts))]
    if not got:
        return None, table
    b0, b1 = fit_overlay(o.picture.bbox, [(x, got.get(i, d)) for i, (x, d) in enumerate(zip(xs, o.drifts))],
                         OVERLAY_STRETCH_MEASURED)
    a0, a1 = o.box
    return Place(dx=(b0 - a0) * scale, dy=0.0, sx=(b1 - b0) / (a1 - a0)), table


def _place_json(m: Place) -> list[Json]:
    """moves.json's entry for a move: [dx, dy], and an overlay's stretch after them."""
    return [round(v, 4) for v in (m.dx, m.dy) + (() if m.sx is None else (m.sx,))]


def measure_places(slides: SlidesService, pid: str, deck: JsonMap, scale: float, fonts: FontMapper,
                   placed: Callable[[JsonObject, int], JsonObject], page_slide: Mapping[int, str], out: Path,
                   page_width: float) -> tuple[dict[str, Place], list[str]]:
    """Moves for hole and overlay pictures (slide pt; an overlay's `sx` stretches it and dx moves
    its left edge), measured on scratch slides (see above), and the scratch slides to delete.
    What can't be found keeps its predicted place.

    `page_width`: how wide the deck being measured is, in slide pt. A thumbnail is a fixed number
    of pixels wide whatever the page is, so it alone says how many pixels a point is."""
    from PIL import Image
    from .gslides import save_thumbnail

    started = time.monotonic()
    reqs, jobs = measure_jobs(deck, scale, fonts, placed, page_slide)
    if not jobs:
        return {}, []
    from . import net
    if net.downloads_off():  # (no thumbnail could be read: no scratch slides either)
        print("picture places: predicted (downloads are switched off, so no thumbnail can be measured)")
        return {}, []
    try:
        batch(slides, pid, reqs)
    except HttpError as e:
        print(f"warning: could not measure the picture places ({api_error(e)}); keeping the predicted places")
        return {}, []  # (a refused batch created nothing)

    # One client per worker thread, not one per slide: `build(...)` fetches a discovery document
    # every time it is called. Where a caller handed its own client over there is only that one,
    # which is not thread-safe, so the thumbnails are fetched one at a time.
    creds, fetch = credentials_for_threads(), fetcher_for_threads()
    client = per_thread(lambda: slides_service(creds))
    workers = 1 if shared_service("slides", "v1") else 6
    tables: dict[str, list[list[float | None]]] = {}

    def measure(job: ScratchJob) -> dict[str, Place]:
        n = job.page
        path = out / "holes" / f"marks-{n + 1:03}.png"
        try:
            save_thumbnail(client(), pid, job.slide, path, fetch)
        except Exception as e:  # noqa: BLE001 - HttpError, OSError, or a harness fetcher's own
            print(f"warning: slide {n + 1}: no thumbnail to measure the picture places ({e})")
            return {}
        img: RGB = np.asarray(Image.open(path).convert("RGB"))
        px_per_pt = img.shape[1] / page_width
        marks = {c: find_marks(mark_alpha(img, c), px_per_pt)
                 for c in {f.colour for f in job.gaps} | {w.colour for o in job.overlays for w in o.words}}
        moves: dict[str, Place] = {}
        for o in job.overlays:
            move, tables[o.picture.id] = overlay_move(o, marks, scale)
            if move is None:
                print(f"warning: slide {n + 1}: words of {o.picture.id} not found; keeping its predicted place")
            elif abs(move.dx) >= 0.2 or (move.sx is not None and abs(move.sx - 1) >= 0.002):
                moves[o.picture.id] = move
        for f in job.gaps:
            gap = pick_gap(marks[f.colour], f.x0, f.cy, f.width, f.pitch)
            if gap is None and f.hang:
                space, left, right = f.hang
                end = ink_end(img, px_per_pt, f.cy - 0.2 * f.pitch, f.cy + 0.2 * f.pitch, left, right)
                if end is not None and abs(end + space - f.x0) < 0.5 * f.width + 10:
                    gap = (end + space - f.x0, 0.0)
            if gap is None:
                print(f"warning: slide {n + 1}: gap of {f.picture} not found; keeping its predicted place")
            elif abs(gap[0]) >= 0.2 or gap[1]:
                moves[f.picture] = Place(dx=gap[0], dy=gap[1], sx=None)
        return moves

    moves: dict[str, Place] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(measure, jobs):
            moves.update(result)
    (out / "holes" / "moves.json").write_text(json.dumps({k: _place_json(m) for k, m in moves.items()}, indent=1),
                                              encoding="utf-8")
    # (per overlay mark: PDF x, predicted drift, measured drift)
    (out / "holes" / "overlays.json").write_text(json.dumps(tables), encoding="utf-8")
    n_holes, n_overlays = sum(len(job.gaps) for job in jobs), sum(len(job.overlays) for job in jobs)
    print(f"  picture places: {n_holes} formula gaps and {n_overlays} overlays measured on {len(jobs)} slides, "
          f"{len(moves)} pictures moved ({time.monotonic() - started:.1f} s)")
    return moves, [job.slide for job in jobs]
