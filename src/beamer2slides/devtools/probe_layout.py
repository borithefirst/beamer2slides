"""Where things really are on a slide, measured on Google's renderer (the `layout-*` sync scenarios
of tests/test_sync_live.py, docs/project-notes.md "Layout probes").

A read-back gives every element's *box*, which is not where its ink is: a text box does not grow
with its text (no autofit survives the .pptx import), so words can run past its bottom, and the
hole a formula picture covers moves with the words while the picture does not. These helpers ask
the thumbnail instead, on throwaway copies of the slide:

  inks(api, pid, slide, {"name": {object ids}, ...})   the ink of each set of objects alone, in pt
  holes(api, pid, slide, text_id)                      where each formula hole of a text is set, in pt

Each copy is the slide duplicated with everything but the objects asked about deleted, compared
with a copy holding nothing (layout decoration and background stay alike in both, so what differs
is the objects' own ink). The copies are deleted again before returning. Positions are slide pt
(the deck's own page, 720 pt wide for the test talk).

  python -m beamer2slides.devtools.probe_layout <deck> <slide title> [--ids ID ...] [--holes TEXT_ID]
"""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

from ..arrays import Mask, SignedRGB
from ..google_types import all_elements, children, object_id, part
from ..json_types import Json, JsonObject, JsonShapeError, as_int, as_object, as_objects, as_str
from .sync_check import EMU_PER_PT, Element, Model, flatten, presentation_id

if TYPE_CHECKING:
    from ..google_types import Page, PageElement, Presentation, SlidesService

HOLE_COLOURS = ["#ff00ff", "#00c8ff", "#ffb000", "#00d060"]
NBSP = "\xa0"
INK_THRESHOLD = 48   # a thumbnail channel differing by more than this from the empty copy's is ink
STRIP_PT = 6.0       # `overlap`'s strips, pt wide


@dataclass(frozen=True, kw_only=True)
class Ink:
    mask: Mask            # True where the objects put ink (thumbnail pixels)
    per_pt: float               # thumbnail pixels per slide pt

    @property
    def box(self) -> list[float] | None:
        """[x0, y0, x1, y1] of the ink, slide pt (None: no ink at all)."""
        ys, xs = np.nonzero(self.mask)
        if not len(xs):
            return None
        return [round(float(v) / self.per_pt, 2) for v in (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)]

    def rows(self, x0: float, x1: float) -> tuple[float, float] | None:
        """(top, bottom) of the ink between x0 and x1 (pt)."""
        a, b = max(0, int(x0 * self.per_pt)), int(x1 * self.per_pt) + 1
        ys = np.nonzero(self.mask[:, a:b].any(axis=1))[0]
        return None if not len(ys) else (float(ys.min()) / self.per_pt, float(ys.max() + 1) / self.per_pt)


def box_json(box: Sequence[float] | None) -> Json:
    return None if box is None else [v for v in box]


@dataclass(frozen=True, kw_only=True)
class Overlap:
    """How far two inks run into each other (`overlap_of`)."""
    band_pt: float                 # the deepest one runs into the other in a strip
    width_pt: float                # how wide the stretch of strips where they overlap is
    clearance_pt: float | None     # the smallest vertical gap in a strip holding both (< 0: overlap)
    at: tuple[float, float, float, float] | None   # the box of that stretch (pt)
    pixels_pt2: float              # ink in common

    def json(self) -> JsonObject:
        return {"band_pt": self.band_pt, "width_pt": self.width_pt, "clearance_pt": self.clearance_pt,
                "at": box_json(self.at), "pixels_pt2": self.pixels_pt2}


def overlap_of(a: Ink, b: Ink, strip_pt: float) -> Overlap:
    """How far two inks run into each other. Words over words share few *pixels* (the lines of one
    interleave with the other's), so the measure is the extents: the page is cut into vertical
    strips `strip_pt` wide, and in each strip where both have ink the overlap of their [top, bottom]
    spans is taken. `band_pt`: the largest of those (how deep one runs into the other), `width_pt`:
    how wide the stretch of strips where they overlap is, `at`: the box of that stretch (pt);
    `clearance_pt`: the smallest vertical gap between the two in any strip where both have ink
    (negative: they overlap by that much; None: no strip holds both); `pixels_pt2`: ink in common."""
    w = max(1, round(strip_pt * a.per_pt))
    band = 0.0
    xs: list[int] = []
    box: tuple[int, int, int, int] | None = None
    clear: int | None = None
    for x in range(0, a.mask.shape[1], w):
        ra = np.nonzero(a.mask[:, x:x + w].any(axis=1))[0]
        rb = np.nonzero(b.mask[:, x:x + w].any(axis=1))[0]
        if not len(ra) or not len(rb):
            continue
        top, bottom = int(max(ra.min(), rb.min())), int(min(ra.max(), rb.max())) + 1
        clear = (top - bottom) if clear is None else min(clear, top - bottom)
        if bottom > top:
            band = max(band, (bottom - top) / a.per_pt)
            xs.append(x)
            box = (min(box[0], x), min(box[1], top), max(box[2], x + w), max(box[3], bottom)) if box else \
                (x, top, x + w, bottom)
    both = a.mask & b.mask
    at = None if box is None else (round(box[0] / a.per_pt, 1), round(box[1] / a.per_pt, 1),
                                   round(box[2] / a.per_pt, 1), round(box[3] / a.per_pt, 1))
    return Overlap(band_pt=round(float(band), 1), width_pt=round(len(xs) * w / a.per_pt, 1),
                   clearance_pt=None if clear is None else round(float(clear) / a.per_pt, 1), at=at,
                   pixels_pt2=round(float(both.sum()) / a.per_pt ** 2, 1))


def overlap(a: Ink, b: Ink) -> JsonObject:
    """`overlap_of` in `STRIP_PT` strips, as the JSON the layout scenarios record."""
    return overlap_of(a, b, STRIP_PT).json()


def _ink(pixels: SignedRGB, empty: SignedRGB, per_pt: float) -> Ink:
    return Ink(mask=np.abs(pixels - empty).max(axis=2) > INK_THRESHOLD, per_pt=per_pt)


def ink_files(folder: Path, name: str, width_pt: float) -> Ink:
    """An Ink again from the thumbnails `inks` kept (<name>.png against empty.png)."""
    base = _pixels(folder / "empty.png")
    return _ink(_pixels(folder / f"{name}.png"), base, base.shape[1] / width_pt)


def _page_width(pres: Presentation) -> float:
    magnitude = pres.get("pageSize", {}).get("width", {}).get("magnitude")
    if magnitude is None:
        raise JsonShapeError("presentation: no page width")
    return magnitude / EMU_PER_PT


def _ids(elements: Sequence[PageElement]) -> list[str]:
    return [object_id(e) for e in all_elements(elements, "slide")]


def _leaves(elements: Sequence[PageElement]) -> list[str]:
    out: list[str] = []
    for e in elements:
        kids = children(e, "slide")
        out += _leaves(kids) if kids else [object_id(e)]
    return out


Extra = Callable[[Mapping[str, str]], list[JsonObject]]


def _nothing_more(mapping: Mapping[str, str]) -> list[JsonObject]:
    return []


def _copy(api: SlidesService, pid: str, slide: Page, keep: Set[str], extra: Extra) -> tuple[str, dict[str, str]]:
    """A copy of the slide with only `keep` (object ids of the original) left on it; `extra`
    gives more requests from the id mapping. (copy id, mapping original id -> copy id)"""
    from beamer2slides.gslides import execute
    tag = uuid.uuid4().hex[:8]
    elements = slide.get("pageElements", [])
    ids = _ids(elements)
    mapping = {object_id(slide): f"plc_{tag}"}
    mapping.update({oid: f"plc_{tag}_{i}" for i, oid in enumerate(ids)})
    gone = [mapping[oid] for oid in _leaves(elements) if oid not in keep]
    object_ids: JsonObject = {k: v for k, v in mapping.items()}
    reqs: list[JsonObject] = [{"duplicateObject": {"objectId": object_id(slide), "objectIds": object_ids}}]
    deletes: list[JsonObject] = [{"deleteObject": {"objectId": oid}} for oid in gone]
    reqs += deletes
    execute(api.presentations().batchUpdate(presentationId=pid, body={"requests": reqs + extra(mapping)}))
    return mapping[object_id(slide)], mapping


def _drop(api: SlidesService, pid: str, copies: list[str]) -> None:
    from beamer2slides.gslides import execute
    if copies:
        execute(api.presentations().batchUpdate(presentationId=pid, body={
            "requests": [{"deleteObject": {"objectId": c}} for c in copies]}))


def _pixels(path: Path) -> SignedRGB:
    return np.asarray(Image.open(path).convert("RGB")).astype(np.int16)


def _slide(api: SlidesService, pid: str, slide_id: str) -> tuple[Page, float]:
    from beamer2slides.gslides import execute
    pres = execute(api.presentations().get(presentationId=pid))
    return next(s for s in pres.get("slides", []) if object_id(s) == slide_id), _page_width(pres)


def inks(api: SlidesService, pid: str, slide_id: str, sets: Mapping[str, Set[str]], folder: Path) -> dict[str, Ink]:
    """name -> the ink the objects of that set draw on the slide on their own. `folder` keeps the
    thumbnails (<name>.png, and empty.png)."""
    from beamer2slides.gslides import save_thumbnail
    slide, width = _slide(api, pid, slide_id)
    folder.mkdir(parents=True, exist_ok=True)
    copies: list[str] = []
    try:
        empty, _ = _copy(api, pid, slide, set(), _nothing_more)
        copies.append(empty)
        made: dict[str, str] = {}
        for name, keep in sets.items():
            made[name], _ = _copy(api, pid, slide, set(keep), _nothing_more)
            copies.append(made[name])
        save_thumbnail(api, pid, empty, folder / "empty.png")
        base = _pixels(folder / "empty.png")
        per_pt = base.shape[1] / width
        out: dict[str, Ink] = {}
        for name, cid in made.items():
            save_thumbnail(api, pid, cid, folder / f"{name}.png")
            out[name] = _ink(_pixels(folder / f"{name}.png"), base, per_pt)
        return out
    finally:
        _drop(api, pid, copies)


def hole_runs(shape: JsonObject) -> list[tuple[int, int]]:
    """(start, end) UTF-16 ranges of a text's formula holes: runs of no-break spaces set in Roboto
    Mono (emit's holes), neighbouring runs joined."""
    runs: list[tuple[int, int]] = []
    for t in as_objects(part(shape.get("text"), "shape.text").get("textElements", []), "textElements"):
        found = t.get("textRun")
        if not found:
            continue
        run = as_object(found, "textRun")
        style = part(run.get("style"), "textRun.style")
        family = part(style.get("weightedFontFamily"), "weightedFontFamily").get("fontFamily") or style.get("fontFamily")
        content = as_str(run.get("content"), "textRun.content")
        if family == "Roboto Mono" and content and set(content) <= {NBSP}:
            start, end = as_int(t.get("startIndex", 0), "startIndex"), as_int(t.get("endIndex"), "endIndex")
            if runs and runs[-1][1] == start:
                runs[-1] = (runs[-1][0], end)
            else:
                runs.append((start, end))
    return runs


def holes(api: SlidesService, pid: str, slide_id: str, text_id: str, folder: Path) -> list[list[float] | None]:
    """[x0, y0, x1, y1] (pt) of each formula hole of the text `text_id` as Slides sets it, in text
    order (None: a hole Slides drew no ink of): the holes are painted in a colour of their own on a
    copy holding only that text."""
    from beamer2slides.gslides import save_thumbnail
    from .deck_edits import rgb
    slide, width = _slide(api, pid, slide_id)
    el = next(e for e in flatten(slide.get("pageElements", []), np.eye(3, dtype=np.float64), (), "slide")
              if e.id == text_id)
    runs = hole_runs(el.shape)
    if not runs:
        return []
    colours = [HOLE_COLOURS[i % len(HOLE_COLOURS)] for i in range(len(runs))]

    def paint(mapping: Mapping[str, str]) -> list[JsonObject]:
        return [{"updateTextStyle": {"objectId": mapping[text_id], "fields": "backgroundColor",
                                     "textRange": {"type": "FIXED_RANGE", "startIndex": a, "endIndex": b},
                                     "style": {"backgroundColor": {"opaqueColor": rgb(c)}}}}
                for (a, b), c in zip(runs, colours)]
    copy, _ = _copy(api, pid, slide, {text_id}, paint)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"holes-{text_id}.png"
        save_thumbnail(api, pid, copy, path)
    finally:
        _drop(api, pid, [copy])
    px = _pixels(path)
    per_pt = px.shape[1] / width
    boxes: list[list[float] | None] = []
    for c in colours:
        want = np.array([int(c[i:i + 2], 16) for i in (1, 3, 5)])
        # (only the colour itself: the anti-aliased rim blends into the text colour)
        boxes.append(Ink(mask=np.abs(px - want).max(axis=2) < 40, per_pt=per_pt).box)
    return boxes


def pictures_on(model: Model, slide_id: str, text_id: str) -> list[Element]:
    """The formula pictures grouped with a text (its unit's pictures), in reading order."""
    s = next(x for x in model.slides if x.id == slide_id)
    text = next(e for e in s.elements if e.id == text_id)
    pics = [e for e in s.elements if e.kind == "image" and text.parent and e.parent == text.parent]
    return sorted(pics, key=lambda e: (round(e.box[1] / 4), e.box[0]))


def main() -> None:
    from beamer2slides.google_auth import slides_service

    from .sync_check import read_with
    ap = argparse.ArgumentParser()
    ap.add_argument("deck")
    ap.add_argument("title")
    ap.add_argument("--ids", nargs="*", default=[])
    ap.add_argument("--holes")
    ap.add_argument("--out", type=Path, default=Path("out") / "probe-layout")
    args = ap.parse_args()
    deck: str = args.deck
    title: str = args.title
    ids: list[str] = args.ids
    hole_text: str | None = args.holes
    out: Path = args.out
    api = slides_service()
    pid = presentation_id(deck)
    model = read_with(api, pid)
    s = model.one(title)
    result: JsonObject = {}
    if ids:
        result["ink"] = {k: box_json(v.box) for k, v in inks(api, pid, s.id, {i: {i} for i in ids}, out).items()}
    if hole_text:
        result["holes"] = [box_json(b) for b in holes(api, pid, s.id, hole_text, out)]
        result["pictures"] = [box_json(e.box) for e in pictures_on(model, s.id, hole_text)]
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
