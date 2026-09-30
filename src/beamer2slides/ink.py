"""Measure where ink (dark pixels) is in rendered images.

Used on both sides of every fidelity comparison: PDF pages rendered by PDFium
and slide thumbnails rendered by Google. Coordinates come back in points, given
the image's pixels-per-point scale.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .arrays import Gray
from .pdf import Page

INK_THRESHOLD = 128  # grey level below which a pixel counts as ink (50% coverage)


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


def load_gray(path: Path) -> Gray:
    return np.asarray(Image.open(path).convert("L"))


def render_gray(page: Page, px_per_pt: float) -> Gray:
    return np.asarray(Image.fromarray(page.render(px_per_pt, clip=None, transparent=False)).convert("L"))


def _crop(gray: Gray, region: Box | None, px_per_pt: float) -> tuple[Gray, int, int]:
    if region is None:
        return gray, 0, 0
    h, w = gray.shape
    x0 = max(0, int(np.floor(region.x0 * px_per_pt)))
    y0 = max(0, int(np.floor(region.y0 * px_per_pt)))
    x1 = min(w, int(np.ceil(region.x1 * px_per_pt)))
    y1 = min(h, int(np.ceil(region.y1 * px_per_pt)))
    return gray[y0:y1, x0:x1], x0, y0


def ink_box(gray: Gray, px_per_pt: float, region: Box | None) -> Box | None:
    """Bounding box of all ink inside `region` (points), or None if there is none."""
    sub, ox, oy = _crop(gray, region, px_per_pt)
    ink = sub < INK_THRESHOLD
    rows = np.flatnonzero(ink.any(axis=1))
    cols = np.flatnonzero(ink.any(axis=0))
    if rows.size == 0:
        return None
    return Box(
        (ox + cols[0]) / px_per_pt,
        (oy + rows[0]) / px_per_pt,
        (ox + cols[-1] + 1) / px_per_pt,
        (oy + rows[-1] + 1) / px_per_pt,
    )


BAND_GAP_PX = 2
"""Two bands of ink (`ink_bands`) are parted by more blank rows than this."""


def ink_bands(gray: Gray, px_per_pt: float, region: Box | None, *, min_gap_px: int) -> list[Box]:
    """Horizontal bands of ink (text lines), top to bottom, each with its own x-extent (`region`
    None: the whole image; `min_gap_px` usually `BAND_GAP_PX`)."""
    sub, ox, oy = _crop(gray, region, px_per_pt)
    ink = sub < INK_THRESHOLD
    rows = np.flatnonzero(ink.any(axis=1))
    if rows.size == 0:
        return []
    splits = np.flatnonzero(np.diff(rows) > min_gap_px) + 1
    bands = []
    for group in np.split(rows, splits):
        cols = np.flatnonzero(ink[group[0]:group[-1] + 1].any(axis=0))
        bands.append(Box(
            (ox + cols[0]) / px_per_pt,
            (oy + group[0]) / px_per_pt,
            (ox + cols[-1] + 1) / px_per_pt,
            (oy + group[-1] + 1) / px_per_pt,
        ))
    return bands
