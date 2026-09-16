"""Stage 3: background images with the converted content removed.

Text becomes native in Slides, so its glyphs are redacted from a copy of the PDF.
Only glyphs are removed: images and vector graphics stay untouched, and nothing is
painted over (panels and gradients under the text survive).
"""

from pathlib import Path

import numpy as np
import pymupdf

BACKGROUND_WIDTH_PX = 2000


def _band(bbox: list[float], baseline: float, size: float) -> pymupdf.Rect:
    """A thin horizontal band through the glyphs' x-height: it touches this line's
    characters but never the ascenders or descenders of neighbouring lines."""
    x0, _, x1, _ = bbox
    return pymupdf.Rect(x0 + 0.2, baseline - 0.45 * size, x1 - 0.2, baseline - 0.2 * size)


def redacted_pdf(pdf: Path, raw: dict, deck: dict, out: Path) -> Path:
    doc = pymupdf.open(pdf)
    spans = {s["id"]: s for page in raw["pages"] for s in page["spans"]}
    for slide in deck["slides"]:
        page = doc[slide["page"]]
        for el in slide["elements"]:
            for sid in el["spans"]:
                s = spans[sid]
                page.add_redact_annot(_band(s["bbox"], s["origin"][1], s["size"]), fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    out.parent.mkdir(parents=True, exist_ok=True)
    # garbage>=3 deduplicates objects, which after redaction corrupts beamer's soft-mask
    # shadows (Madrid blocks render as black bars). Only drop unused objects.
    doc.save(out, garbage=1, deflate=True)
    return out


def render_pages(pdf: Path, out_dir: Path, width_px: int, prefix: str) -> list[Path]:
    doc = pymupdf.open(pdf)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for page in doc:
        zoom = width_px / page.rect.width
        path = out_dir / f"{prefix}-{page.number + 1:03}.png"
        page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(path)
        paths.append(path)
    return paths


def patch_rects(png: Path, rects_pt: list[list[float]], px_per_pt: float) -> None:
    """Paint rectangles with the median colour of a thin ring around them.

    Used for ball bullets: removing those images from the PDF is unreliable (themes draw
    them with soft masks shared with shadows), but they sit on flat panel colours."""
    pix = pymupdf.Pixmap(str(png))
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n).copy()
    for x0, y0, x1, y1 in rects_pt:
        a0, b0 = int(np.floor(x0 * px_per_pt)) - 1, int(np.floor(y0 * px_per_pt)) - 1
        a1, b1 = int(np.ceil(x1 * px_per_pt)) + 1, int(np.ceil(y1 * px_per_pt)) + 1
        r = 3
        ring = np.concatenate([
            img[max(0, b0 - r):b0, max(0, a0 - r):a1 + r].reshape(-1, pix.n),
            img[b1:b1 + r, max(0, a0 - r):a1 + r].reshape(-1, pix.n),
            img[b0:b1, max(0, a0 - r):a0].reshape(-1, pix.n),
            img[b0:b1, a1:a1 + r].reshape(-1, pix.n),
        ])
        img[max(0, b0):b1, max(0, a0):a1] = np.median(ring, axis=0).astype(np.uint8)
    pymupdf.Pixmap(pix.colorspace, pix.width, pix.height, img.tobytes(), pix.alpha).save(png)


def render_backgrounds(pdf: Path, raw: dict, deck: dict, out: Path) -> list[Path]:
    clean = redacted_pdf(pdf, raw, deck, out / "background.pdf")
    paths = render_pages(clean, out / "backgrounds", BACKGROUND_WIDTH_PX, "bg")
    images = {i["id"]: i for page in raw["pages"] for i in page["images"]}
    for slide, path in zip(deck["slides"], paths):
        bullets = [images[p["bullet"]["image"]]["bbox"] for el in slide["elements"] for p in el["paragraphs"]
                   if p["bullet"] and p["bullet"]["kind"] == "image"]
        if bullets:
            patch_rects(path, bullets, BACKGROUND_WIDTH_PX / slide["size"][0])
        slide["background"] = str(path.relative_to(out)).replace("\\", "/")
    return paths
