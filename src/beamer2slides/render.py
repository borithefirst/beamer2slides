"""Stage 3: background images with the converted content removed, plus figure pictures.

Text becomes native in Slides, so its glyphs are redacted from a copy of the PDF.
Only glyphs are removed: images and vector graphics stay untouched, and nothing is
painted over (panels and gradients under the text survive).

Figures are then cropped from that text-free page (so native text never ends up inside
a picture) and removed from the background: vector paths fully inside the figure box,
its labels, and the image pixels within the box.
"""

from pathlib import Path

import numpy as np
import pymupdf

BACKGROUND_WIDTH_PX = 2000
FIGURE_PX_PER_PT = 6.0     # ~ 3 px per Slides point on a 4:3 deck
FIGURE_MAX_PX = 3000


def _band(bbox: list[float], baseline: float, size: float) -> pymupdf.Rect:
    """A thin horizontal band through the glyphs' x-height: it touches this line's
    characters but never the ascenders or descenders of neighbouring lines."""
    x0, _, x1, _ = bbox
    return pymupdf.Rect(x0 + 0.2, baseline - 0.45 * size, x1 - 0.2, baseline - 0.2 * size)


def crop_figure(page: pymupdf.Page, bbox: list[float], raw_images: list[dict], path: Path) -> list[int]:
    rect = pymupdf.Rect(bbox)
    zoom = FIGURE_PX_PER_PT
    # A raster image filling the figure keeps its native resolution (up to the cap).
    for im in raw_images:
        ir = pymupdf.Rect(im["bbox"])
        if ir.width > 0 and rect.contains(ir) and ir.get_area() > 0.8 * rect.get_area():
            zoom = max(zoom, im["px"][0] / ir.width)
    zoom = min(zoom, FIGURE_MAX_PX / max(rect.width, rect.height))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect, alpha=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(path)
    return [pix.width, pix.height]


def render_backgrounds(pdf: Path, raw: dict, deck: dict, out: Path) -> list[Path]:
    doc = pymupdf.open(pdf)
    spans = {s["id"]: s for page in raw["pages"] for s in page["spans"]}
    for slide in deck["slides"]:
        page = doc[slide["page"]]
        texts = [e for e in slide["elements"] if e["kind"] == "text"]
        figures = [e for e in slide["elements"] if e["kind"] == "image"]

        for el in texts:
            for sid in el["spans"]:
                s = spans[sid]
                page.add_redact_annot(_band(s["bbox"], s["origin"][1], s["size"]), fill=False)
        if texts:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)

        for fig in figures:
            path = out / "figures" / f"{fig['id']}.png"
            fig["px"] = crop_figure(page, fig["bbox"], raw["pages"][slide["page"]]["images"], path)
            fig["file"] = str(path.relative_to(out)).replace("\\", "/")
            page.add_redact_annot(pymupdf.Rect(fig["bbox"]), fill=False)
        if figures:
            # Labels and image pixels: exactly the figure box.
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            # Vector paths: MuPDF's "covered" test is conservative (strokes, transformed
            # TikZ nodes), so test against a box with some margin.
            for fig in figures:
                page.add_redact_annot(pymupdf.Rect(fig["bbox"]) + (-5, -5, 5, 5), fill=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                  text=pymupdf.PDF_REDACT_TEXT_NONE)

    clean = out / "background.pdf"
    # garbage>=3 deduplicates objects, which after redaction corrupts beamer's soft-mask
    # shadows (Madrid blocks render as black bars). Only drop unused objects.
    doc.save(clean, garbage=1, deflate=True)

    paths = render_pages(clean, out / "backgrounds", BACKGROUND_WIDTH_PX, "bg")
    images = {i["id"]: i for page in raw["pages"] for i in page["images"]}
    for slide, path in zip(deck["slides"], paths):
        bullets = [images[p["bullet"]["image"]]["bbox"]
                   for el in slide["elements"] if el["kind"] == "text" for p in el["paragraphs"]
                   if p["bullet"] and p["bullet"]["kind"] == "image"]
        if bullets:
            patch_rects(path, bullets, BACKGROUND_WIDTH_PX / slide["size"][0])
        slide["background"] = str(path.relative_to(out)).replace("\\", "/")
    return paths


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
