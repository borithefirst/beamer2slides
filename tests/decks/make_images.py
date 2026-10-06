"""Generate raster test images for the decks (deterministic, no extra dependencies)."""

from collections.abc import Callable
from pathlib import Path

import numpy as np
from PIL import Image  # noqa: F401  (used by FILES)

OUT = Path(__file__).resolve().parent / "img"


def save_rgb(arr: np.ndarray, path: Path) -> None:
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(path)


def photo(w: int, h: int) -> np.ndarray:
    """Smooth colour field with soft blobs and noise: behaves like a photograph (JPEG)."""
    rng = np.random.default_rng(7)
    y, x = np.mgrid[0:h, 0:w] / max(w, h)
    img = np.stack([120 + 100 * np.sin(3 * x + 1), 110 + 90 * np.cos(4 * y), 150 + 80 * np.sin(2 * (x + y))], -1)
    for cx, cy, r, col in ((0.3, 0.3, 0.12, (240, 200, 60)), (0.7, 0.5, 0.18, (40, 90, 160))):
        d = np.exp(-(((x - cx) ** 2 + (y - cy) ** 2) / (2 * r * r)))[..., None]
        img = img * (1 - d) + np.array(col) * d
    img += rng.normal(0, 6, img.shape)
    return np.clip(img, 0, 255)


def plot(w: int, h: int) -> np.ndarray:
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


def badge(w: int, h: int) -> np.ndarray:
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
def photo_jpg(p: Path) -> None:
    save_rgb(photo(1200, 800), p)


def plot_png(p: Path) -> None:
    save_rgb(plot(800, 600), p)


def photo_big_jpg(p: Path) -> None:
    rgb(photo(2400, 1600)).save(p, quality=80)


def photo_cmyk_jpg(p: Path) -> None:
    rgb(photo(600, 400)).convert("CMYK").save(p, quality=80)


def logo_png(p: Path) -> None:
    Image.fromarray(badge(600, 400)).save(p)


def plot_gray_png(p: Path) -> None:
    rgb(plot(800, 600)).convert("L").save(p)


def plot_indexed_png(p: Path) -> None:
    rgb(plot(800, 600)).convert("P", palette=Image.Palette.ADAPTIVE, colors=16).save(p)


# Icons and logos for the box-and-arrow schematics of deck 31_box_diagrams: flat shapes on a
# transparent ground (what a cloud vendor's or a project's icon is), an opaque square photo, and a
# vector logo (a one-page PDF written by hand).
def icon_canvas(n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    y, x = np.mgrid[0:n, 0:n] / n
    return np.zeros((n, n, 4), np.uint8), x, y


def icon_db(p: Path) -> None:
    img, x, y = icon_canvas(256)
    body = (np.abs(x - 0.5) <= 0.34) & (y >= 0.2) & (y <= 0.8)
    top = ((x - 0.5) / 0.34) ** 2 + ((y - 0.2) / 0.1) ** 2 <= 1
    bottom = ((x - 0.5) / 0.34) ** 2 + ((y - 0.8) / 0.1) ** 2 <= 1
    img[body | top | bottom] = (52, 101, 164, 255)
    for band in (0.4, 0.6):
        ring = np.abs(((x - 0.5) / 0.34) ** 2 + ((y - band) / 0.1) ** 2 - 1) < 0.12
        img[ring & (y > band) & body] = (230, 236, 245, 255)
    img[top & (((x - 0.5) / 0.3) ** 2 + ((y - 0.2) / 0.075) ** 2 <= 1)] = (114, 159, 207, 255)
    Image.fromarray(img).save(p)


def icon_cloud(p: Path) -> None:
    img, x, y = icon_canvas(256)
    shape = np.zeros(x.shape, bool)
    for cx, cy, r in ((0.32, 0.58, 0.17), (0.5, 0.45, 0.22), (0.7, 0.57, 0.16)):
        shape |= (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    shape |= (x >= 0.32) & (x <= 0.7) & (y >= 0.55) & (y <= 0.73)
    img[shape] = (66, 133, 244, 255)
    Image.fromarray(img).save(p)


def icon_user(p: Path) -> None:
    img, x, y = icon_canvas(256)
    head = (x - 0.5) ** 2 + (y - 0.32) ** 2 <= 0.17 ** 2
    shoulders = ((x - 0.5) / 0.33) ** 2 + ((y - 0.9) / 0.36) ** 2 <= 1
    img[head | (shoulders & (y <= 0.9))] = (60, 64, 67, 255)
    Image.fromarray(img).save(p)


def icon_gear(p: Path) -> None:
    img, x, y = icon_canvas(256)
    r, a = np.hypot(x - 0.5, y - 0.5), np.arctan2(y - 0.5, x - 0.5)
    teeth = (np.cos(8 * a) > 0.3) & (r <= 0.46)
    img[((r <= 0.36) | teeth) & (r >= 0.14)] = (244, 160, 0, 255)
    Image.fromarray(img).save(p)


def icon_lock(p: Path) -> None:
    img, x, y = icon_canvas(256)
    body = (np.abs(x - 0.5) <= 0.3) & (y >= 0.45) & (y <= 0.88)
    shackle = np.abs(np.hypot(x - 0.5, y - 0.45) - 0.19) <= 0.05
    img[body | (shackle & (y <= 0.47))] = (219, 68, 55, 255)
    img[(x - 0.5) ** 2 + (y - 0.63) ** 2 <= 0.06 ** 2] = (255, 255, 255, 255)
    Image.fromarray(img).save(p)


def thumb_jpg(p: Path) -> None:
    rgb(photo(300, 300)).save(p, quality=85)


def logo_vector_pdf(p: Path) -> None:
    """A green hexagon with a white play triangle, as PDF paths (what an SVG logo becomes)."""
    content = (b"0.204 0.659 0.325 rg 50 95 m 89 72.5 l 89 27.5 l 50 5 l 11 27.5 l 11 72.5 l h f "
               b"1 1 1 rg 40 30 m 72 50 l 40 70 l h f")
    objects = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
               b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]/Contents 4 0 R/Resources<<>>>>",
               b"<</Length %d>>stream\n" % len(content) + content + b"\nendstream"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    p.write_bytes(bytes(out))


FILES: dict[str, Callable[[Path], None]] = {
    "icon_db.png": icon_db,
    "icon_cloud.png": icon_cloud,
    "icon_user.png": icon_user,
    "icon_gear.png": icon_gear,
    "icon_lock.png": icon_lock,
    "thumb.jpg": thumb_jpg,
    "logo_vector.pdf": logo_vector_pdf,
    "photo.jpg": photo_jpg,
    "plot.png": plot_png,
    "photo_big.jpg": photo_big_jpg,
    "photo_cmyk.jpg": photo_cmyk_jpg,
    "logo.png": logo_png,
    "plot_gray.png": plot_gray_png,
    "plot_indexed.png": plot_indexed_png,
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
