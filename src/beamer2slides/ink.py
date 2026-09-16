"""Measure where ink (dark pixels) is in rendered images.

Used on both sides of every fidelity comparison: PDF pages rendered by PyMuPDF
and slide thumbnails rendered by Google. Coordinates come back in points, given
the image's pixels-per-point scale.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pymupdf

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


def gray_from_pixmap(pix: pymupdf.Pixmap) -> np.ndarray:
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if pix.n != 1:
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pix)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)


def load_gray(path: Path) -> np.ndarray:
    return gray_from_pixmap(pymupdf.Pixmap(str(path)))


def render_gray(page: pymupdf.Page, px_per_pt: float) -> np.ndarray:
    return gray_from_pixmap(page.get_pixmap(matrix=pymupdf.Matrix(px_per_pt, px_per_pt), colorspace=pymupdf.csGRAY))


def _crop(gray: np.ndarray, region: Box | None, px_per_pt: float) -> tuple[np.ndarray, int, int]:
    if region is None:
        return gray, 0, 0
    h, w = gray.shape
    x0 = max(0, int(np.floor(region.x0 * px_per_pt)))
    y0 = max(0, int(np.floor(region.y0 * px_per_pt)))
    x1 = min(w, int(np.ceil(region.x1 * px_per_pt)))
    y1 = min(h, int(np.ceil(region.y1 * px_per_pt)))
    return gray[y0:y1, x0:x1], x0, y0


def ink_box(gray: np.ndarray, px_per_pt: float, region: Box | None = None) -> Box | None:
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


def ink_bands(gray: np.ndarray, px_per_pt: float, region: Box | None = None, min_gap_px: int = 2) -> list[Box]:
    """Horizontal bands of ink (text lines), top to bottom, each with its own x-extent."""
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
