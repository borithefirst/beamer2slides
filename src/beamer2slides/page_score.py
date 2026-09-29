"""Ink in the right place: a compiled page against Google's own picture of the slide it stands for.

The residual count `compare` gives is the read-back's view - classify of the compiled PDF against the
deck's IR - and that view can be wrong about a right page (a wrapped paragraph read as several,
stacked boxes merged, shapes read as background). This asks the thumbnail instead: the pixels that
differ from the page's ground on each side (`fidelity.text_mask`), and how much of each side's ink
has ink of the other within a pixel. Used by `devtools/adopt_bench.py` to score adopt, and by
`frame_guard` to tell a frame the loop made worse from one it made better.

Scores (1.0 = the deck reproduced):
  boxes   ink overlap inside the deck's element boxes (with room around them)
  page    ink overlap over the whole page: the backdrop and decoration included
  pixels  1 - mean |RGB difference| / 255 over the whole page
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from .arrays import Mask, SignedRGB
from .deck_ir_types import TargetSlide, TargetText
from .ir_types import Box
from .json_types import Json, JsonObject, as_array, as_objects

if TYPE_CHECKING:
    from .pdf import Page

PixelBox = tuple[int, int, int, int]


@dataclass(frozen=True, kw_only=True)
class InkBox:
    """An element's box and the room its ink may take around it: its largest words' size (4 pt for
    an element with no words)."""
    bbox: Box
    room: float


@dataclass(frozen=True, kw_only=True)
class SlideBoxes:
    """What the `boxes` score reads of a slide: its width (pt) and its elements' boxes, in order."""
    width: float
    boxes: tuple[InkBox, ...]


NO_WORDS_ROOM = 4.0


def page_ground(a: SignedRGB) -> SignedRGB:
    """The colour the page mostly is, as the background ink is measured against."""
    flat = a.reshape(-1, 3).astype(np.int32)
    packed = (flat[:, 0] << 16) | (flat[:, 1] << 8) | flat[:, 2]   # np.unique(axis=0) took ~0.5 s a page
    top = int(np.bincount(packed, minlength=1 << 24).argmax())
    return np.broadcast_to(np.array([top >> 16, (top >> 8) & 255, top & 255], dtype=a.dtype), a.shape)


def target_boxes(slide: TargetSlide) -> SlideBoxes:
    """A target slide (deck_ir's read) as the `boxes` score reads it."""
    out: list[InkBox] = []
    for el in slide.elements:
        sizes: list[float] = [r.size or NO_WORDS_ROOM for p in el.paragraphs for r in p.runs] \
            if isinstance(el, TargetText) else []
        out.append(InkBox(bbox=el.bbox, room=max(sizes) if sizes else NO_WORDS_ROOM))
    return SlideBoxes(width=slide.size[0], boxes=tuple(out))


def _number(v: Json, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{where}: a number, not {v!r}")
    return v


def json_boxes(slide: JsonObject) -> SlideBoxes:
    """A slide as JSON - a target's, or deck.json's - as the `boxes` score reads it: any element's
    `paragraphs` give it room, whatever its kind."""
    out: list[InkBox] = []
    for el in as_objects(slide["elements"], "a slide's elements"):
        paragraphs = as_objects(el.get("paragraphs") or [], "an element's paragraphs")
        sizes = [r.get("size") or NO_WORDS_ROOM for p in paragraphs for r in as_objects(p["runs"], "a paragraph's runs")]
        room = max(_number(s, "a run's size") for s in sizes) if paragraphs and any(p["runs"] for p in paragraphs) \
            else NO_WORDS_ROOM
        x0, y0, x1, y1 = (_number(v, "an element's bbox") for v in as_array(el["bbox"], "an element's bbox"))
        out.append(InkBox(bbox=(x0, y0, x1, y1), room=room))
    return SlideBoxes(width=_number(as_array(slide["size"], "a slide's size")[0], "a slide's width"),
                      boxes=tuple(out))


def pixel_boxes(slide: SlideBoxes, w: int, h: int) -> list[tuple[int, PixelBox]]:
    """(element index, pixel box) of the slide's elements, with room around them, on the reference's
    pixel grid; boxes wholly off the page left out."""
    out: list[tuple[int, PixelBox]] = []
    px = w / slide.width
    for k, b in enumerate(slide.boxes):
        size = b.room
        x0, y0, x1, y1 = b.bbox
        a0, b0 = max(0, int((x0 - size) * px)), max(0, int((y0 - size) * px))
        a1, b1 = min(w, int((x1 + 2 * size) * px)), min(h, int((y1 + size) * px))
        if a1 > a0 and b1 > b0:                        # a box off the page: a negative end would
            out.append((k, (a0, b0, a1, b1)))          # count from the far edge
    return out


def element_boxes(slide: JsonObject, w: int, h: int) -> list[tuple[int, PixelBox]]:
    """`pixel_boxes` of a slide given as JSON."""
    return pixel_boxes(json_boxes(slide), w, h)


def boxes_mask(slide: SlideBoxes, w: int, h: int) -> Mask:
    """The slide's element boxes, with room around them. Scoring only the whole page would let one
    unreproduced backdrop hide everything else; `page` reports that."""
    m = np.zeros((h, w), dtype=bool)
    for _, (a0, b0, a1, b1) in pixel_boxes(slide, w, h):
        m[b0:b1, a0:a1] = True
    return m


def covered_mask(slide: JsonObject, w: int, h: int) -> Mask:
    """`boxes_mask` of a slide given as JSON."""
    return boxes_mask(json_boxes(slide), w, h)


def slide_boxes(slide: TargetSlide | JsonObject) -> SlideBoxes:
    """What the `boxes` score reads of a target slide, typed or as JSON."""
    return target_boxes(slide) if isinstance(slide, TargetSlide) else json_boxes(slide)


def overlap(m_ref: Mask, m_got: Mask) -> float:
    from .fidelity import dilate
    inter = (dilate(m_ref) & m_got).sum() + (m_ref & dilate(m_got)).sum()
    total = m_ref.sum() + m_got.sum()
    return float(inter / total) if total else 1.0


def ink_masks(ref: SignedRGB, got: SignedRGB) -> tuple[Mask, Mask]:
    """Each side's ink, both measured against the reference's ground."""
    from .fidelity import text_mask
    ground = page_ground(ref)
    return text_mask(ref, ground), text_mask(got, ground)


def ink_scores(ref: SignedRGB, got: SignedRGB, slide: JsonObject) -> tuple[dict[str, float], Mask, Mask]:
    """{boxes, page, pixels} of `got` against `ref` (int16 RGB arrays of one size), and both ink masks."""
    h, w = ref.shape[:2]
    m_ref, m_got = ink_masks(ref, got)
    covered = covered_mask(slide, w, h)
    scores = {"boxes": round(overlap(m_ref & covered, m_got & covered), 3),
              "page": round(overlap(m_ref, m_got), 3),
              "pixels": round(1 - float(np.abs(ref - got).mean()) / 255, 3)}
    return scores, m_ref, m_got


def load_thumbnail(source: object) -> SignedRGB | None:
    """A thumbnail (a path, PIL image or array) as an int16 RGB array; None when there is none."""
    from PIL import Image
    from .fidelity import rgb_array
    if source is None:
        return None
    if isinstance(source, (str, Path)):
        if not Path(source).is_file():
            return None
        with Image.open(source) as im:
            return rgb_array(im)
    if isinstance(source, Image.Image):
        return rgb_array(source)
    a = np.asarray(source)
    return a[..., :3].astype(np.int16) if a.ndim == 3 else None


def render_like(page: Page, ref: SignedRGB) -> SignedRGB:
    """A PDF page rendered onto the reference's pixel grid (int16 RGB)."""
    from PIL import Image
    from .fidelity import rgb_array_at
    h, w = ref.shape[:2]
    img = Image.fromarray(page.render(w / page.width)).convert("RGB")
    return rgb_array_at(img, (w, h))


def boxes_score(page: Page, ref: SignedRGB, slide: SlideBoxes) -> float:
    """The `boxes` score of a PDF page against a thumbnail of the slide `slide` (`target_boxes`)."""
    got = render_like(page, ref)
    h, w = ref.shape[:2]
    m_ref, m_got = ink_masks(ref, got)
    covered = boxes_mask(slide, w, h)
    return overlap(m_ref & covered, m_got & covered)
