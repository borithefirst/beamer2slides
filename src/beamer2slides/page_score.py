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

from pathlib import Path

import numpy as np


def page_ground(a: np.ndarray) -> np.ndarray:
    """The colour the page mostly is, as the background ink is measured against."""
    flat = a.reshape(-1, 3).astype(np.int32)
    packed = (flat[:, 0] << 16) | (flat[:, 1] << 8) | flat[:, 2]   # np.unique(axis=0) took ~0.5 s a page
    top = int(np.bincount(packed, minlength=1 << 24).argmax())
    return np.broadcast_to(np.array([top >> 16, (top >> 8) & 255, top & 255], dtype=a.dtype), a.shape)


def element_boxes(slide: dict, w: int, h: int) -> list[tuple[int, tuple[int, int, int, int]]]:
    """(element index, pixel box) of the slide's elements, with room around them, on the reference's
    pixel grid; boxes wholly off the page left out."""
    out = []
    px = w / slide["size"][0]
    for k, el in enumerate(slide["elements"]):
        size = max((r.get("size") or 4) for p in el.get("paragraphs", []) for r in p["runs"]) \
            if el.get("paragraphs") and any(p["runs"] for p in el["paragraphs"]) else 4.0
        x0, y0, x1, y1 = el["bbox"]
        a0, b0 = max(0, int((x0 - size) * px)), max(0, int((y0 - size) * px))
        a1, b1 = min(w, int((x1 + 2 * size) * px)), min(h, int((y1 + size) * px))
        if a1 > a0 and b1 > b0:                        # a box off the page: a negative end would
            out.append((k, (a0, b0, a1, b1)))          # count from the far edge
    return out


def covered_mask(slide: dict, w: int, h: int) -> np.ndarray:
    """The slide's element boxes, with room around them. Scoring only the whole page would let one
    unreproduced backdrop hide everything else; `page` reports that."""
    m = np.zeros((h, w), dtype=bool)
    for _, (a0, b0, a1, b1) in element_boxes(slide, w, h):
        m[b0:b1, a0:a1] = True
    return m


def overlap(m_ref: np.ndarray, m_got: np.ndarray) -> float:
    from .fidelity import dilate
    inter = (dilate(m_ref) & m_got).sum() + (m_ref & dilate(m_got)).sum()
    total = m_ref.sum() + m_got.sum()
    return float(inter / total) if total else 1.0


def ink_masks(ref: np.ndarray, got: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Each side's ink, both measured against the reference's ground."""
    from .fidelity import text_mask
    ground = page_ground(ref)
    return text_mask(ref, ground), text_mask(got, ground)


def ink_scores(ref: np.ndarray, got: np.ndarray, slide: dict) -> tuple[dict, np.ndarray, np.ndarray]:
    """{boxes, page, pixels} of `got` against `ref` (int16 RGB arrays of one size), and both ink masks."""
    h, w = ref.shape[:2]
    m_ref, m_got = ink_masks(ref, got)
    covered = covered_mask(slide, w, h)
    scores = {"boxes": round(overlap(m_ref & covered, m_got & covered), 3),
              "page": round(overlap(m_ref, m_got), 3),
              "pixels": round(1 - float(np.abs(ref - got).mean()) / 255, 3)}
    return scores, m_ref, m_got


def load_thumbnail(source) -> np.ndarray | None:
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


def render_like(page, ref: np.ndarray) -> np.ndarray:
    """A PDF page rendered onto the reference's pixel grid (int16 RGB)."""
    from PIL import Image
    from .fidelity import rgb_array
    h, w = ref.shape[:2]
    img = Image.fromarray(page.render(w / page.width)).convert("RGB")
    return rgb_array(img, (w, h))


def boxes_score(page, ref: np.ndarray, slide: dict) -> float:
    """The `boxes` score of a PDF page against a thumbnail of the slide `slide` (its IR)."""
    got = render_like(page, ref)
    h, w = ref.shape[:2]
    m_ref, m_got = ink_masks(ref, got)
    covered = covered_mask(slide, w, h)
    return overlap(m_ref & covered, m_got & covered)
