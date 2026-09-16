"""Generate raster test images for the decks (deterministic, no extra dependencies)."""

from pathlib import Path

import numpy as np
import pymupdf

OUT = Path(__file__).resolve().parent / "img"


def save_rgb(arr: np.ndarray, path: Path) -> None:
    h, w, _ = arr.shape
    pymupdf.Pixmap(pymupdf.csRGB, w, h, np.ascontiguousarray(arr, dtype=np.uint8).tobytes(), False).save(path)


def photo(w=1200, h=800) -> np.ndarray:
    """Smooth colour field with soft blobs and noise: behaves like a photograph (JPEG)."""
    rng = np.random.default_rng(7)
    y, x = np.mgrid[0:h, 0:w] / max(w, h)
    img = np.stack([120 + 100 * np.sin(3 * x + 1), 110 + 90 * np.cos(4 * y), 150 + 80 * np.sin(2 * (x + y))], -1)
    for cx, cy, r, col in ((0.3, 0.3, 0.12, (240, 200, 60)), (0.7, 0.5, 0.18, (40, 90, 160))):
        d = np.exp(-(((x - cx) ** 2 + (y - cy) ** 2) / (2 * r * r)))[..., None]
        img = img * (1 - d) + np.array(col) * d
    img += rng.normal(0, 6, img.shape)
    return np.clip(img, 0, 255)


def plot(w=800, h=600) -> np.ndarray:
    """A flat-colour bar chart with axes: behaves like an exported matplotlib PNG."""
    img = np.full((h, w, 3), 255.0)
    img[h - 60:h - 57, 60:w - 20] = 40  # x axis
    img[20:h - 57, 60:63] = 40          # y axis
    colors = [(31, 119, 180), (255, 127, 14), (44, 160, 44), (214, 39, 40), (148, 103, 189)]
    for i, (value, col) in enumerate(zip((0.55, 0.8, 0.35, 0.95, 0.6), colors)):
        x0 = 100 + i * 135
        img[int(h - 60 - value * (h - 100)):h - 60, x0:x0 + 90] = col
    for gy in range(h - 60 - 100, 20, -100):  # light grid
        img[gy, 64:w - 20] = np.minimum(img[gy, 64:w - 20], 225)
    return img


def main() -> None:
    OUT.mkdir(exist_ok=True)
    save_rgb(photo(), OUT / "photo.jpg")
    save_rgb(plot(), OUT / "plot.png")
    print("written:", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
