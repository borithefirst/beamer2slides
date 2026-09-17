"""Generate raster test images for the decks (deterministic, no extra dependencies)."""

from pathlib import Path

import numpy as np
from PIL import Image  # noqa: F401  (used by FILES)

OUT = Path(__file__).resolve().parent / "img"


def save_rgb(arr: np.ndarray, path: Path) -> None:
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(path)


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


def badge(w=600, h=400) -> np.ndarray:
    """Flat shapes on a transparent ground: behaves like a logo (RGBA PNG)."""
    y, x = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w, 4), np.uint8)
    disc = (x - w / 2) ** 2 / (w / 2.4) ** 2 + (y - h / 2) ** 2 / (h / 2.4) ** 2 <= 1
    img[disc] = (36, 110, 185, 255)
    ring = np.abs(np.hypot((x - w / 2) / (w / 6), (y - h / 2) / (h / 6)) - 1) < 0.12
    img[ring & disc] = (250, 214, 70, 255)
    return img


def rgb(arr: np.ndarray) -> "Image.Image":
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


# Cases for the lossless `\includegraphics` route (deck 23_raster_images): a high-resolution
# photo, the same photo as a CMYK JPEG (whose PDF stream carries a decode array), a logo with
# an alpha channel, a greyscale and an indexed-palette PNG.
FILES = {
    "photo.jpg": lambda p: save_rgb(photo(), p),
    "plot.png": lambda p: save_rgb(plot(), p),
    "photo_big.jpg": lambda p: rgb(photo(2400, 1600)).save(p, quality=80),
    "photo_cmyk.jpg": lambda p: rgb(photo(600, 400)).convert("CMYK").save(p, quality=80),
    "logo.png": lambda p: Image.fromarray(badge()).save(p),
    "plot_gray.png": lambda p: rgb(plot()).convert("L").save(p),
    "plot_indexed.png": lambda p: rgb(plot()).convert("P", palette=Image.ADAPTIVE, colors=16).save(p),
}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    # Files already there are kept: the decks in tests/decks/out were compiled with those very
    # bytes, and a Pillow of another version would write different ones.
    for name, write in FILES.items():
        if not (OUT / name).exists():
            write(OUT / name)
    print("written:", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
