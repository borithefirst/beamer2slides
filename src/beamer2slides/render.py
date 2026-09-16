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


SHAPE_SAMPLE_ZOOM = 2.0


def _fill_fraction(page: pymupdf.Page, rect: pymupdf.Rect, fill: str, avoid: list[pymupdf.Rect]) -> float:
    """Share of sample points inside `rect` (away from text and pictures) that render in `fill`."""
    zoom = max(SHAPE_SAMPLE_ZOOM, 8 / max(min(rect.width, rect.height), 0.01))  # hairlines: enough pixels across
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[..., :3].astype(int)
    target = np.array([int(fill[i:i + 2], 16) for i in (1, 3, 5)])
    ys = np.linspace(pix.height * 0.2, pix.height * 0.8 - 1, 12).astype(int)
    xs = np.linspace(pix.width * 0.02, pix.width * 0.98 - 1, 40).astype(int)
    hits = total = 0
    for y in ys:
        for x in xs:
            px, py = rect.x0 + x / zoom, rect.y0 + y / zoom
            if any(a.contains(pymupdf.Point(px, py)) for a in avoid):
                continue
            total += 1
            hits += int(np.abs(img[y, x] - target).max() <= 12)
    return hits / total if total else 0.0


def _probe(el: dict) -> pymupdf.Rect:
    """The inside of a shape, clear of rounded corners and anti-aliased edges (thin bars keep
    their middle)."""
    r = pymupdf.Rect(el["bbox"])
    dx, dy = min(el["radius"] + 1.5, r.width / 4), min(1.5, r.height / 4)
    return r + (dx, dy, -dx, -dy)


def verify_and_remove_shapes(original: pymupdf.Page, page: pymupdf.Page, slide: dict, raw_page: dict) -> None:
    """Keep only shapes whose panel visibly shows its fill colour (beamer draws shadows as
    black rectangles under a soft mask), then remove those panels from the background."""
    avoid = [pymupdf.Rect(s["bbox"]) for s in raw_page["spans"]] + \
            [pymupdf.Rect(i["bbox"]) for i in raw_page["images"]] + \
            [pymupdf.Rect(e["bbox"]) for e in slide["elements"] if e["kind"] == "image"]
    keep = []
    for el in slide["elements"]:
        if el["kind"] != "shape":
            keep.append(el)
            continue
        probe = _probe(el)
        if probe.is_empty or _fill_fraction(original, probe, el["fill"], avoid) < 0.9:
            continue
        keep.append(el)
    slide["elements"] = keep
    remaining = [e for e in keep if e["kind"] == "shape"]
    for margin in (1.5, 5.0):  # MuPDF's "covered" test is conservative; widen if a panel survived
        if not remaining:
            break
        for el in remaining:
            page.add_redact_annot(pymupdf.Rect(el["bbox"]) + (-margin, -margin, margin, margin), fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=pymupdf.PDF_REDACT_TEXT_NONE)
        remaining = [el for el in remaining
                     if _fill_fraction(page, _probe(el), el["fill"], avoid) >= 0.9]
    if remaining:  # could not be removed: leave those panels in the background only
        slide["elements"] = [e for e in slide["elements"] if e not in remaining]


def render_backgrounds(pdf: Path, raw: dict, deck: dict, out: Path) -> list[Path]:
    doc = pymupdf.open(pdf)
    original = pymupdf.open(pdf)
    raw_pages = {p["index"]: p for p in raw["pages"]}  # by PDF page (overlay steps may be skipped)
    spans = {s["id"]: s for page in raw["pages"] for s in page["spans"]}
    for slide in deck["slides"]:
        page = doc[slide["page"]]
        texts = [e for e in slide["elements"] if e["kind"] == "text"]
        figures = [e for e in slide["elements"] if e["kind"] == "image"]

        text_ids = [sid for el in texts for sid in el["spans"]] + slide.get("on_layout", [])
        for sid in text_ids:
            s = spans[sid]
            page.add_redact_annot(_band(s["bbox"], s["origin"][1], s["size"]), fill=False)
        if text_ids:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        strokes = [st for el in texts for st in el.get("strokes", [])]
        if strokes:  # fraction bars of fractions converted to text
            for x0, y0, x1, y1 in strokes:
                page.add_redact_annot(pymupdf.Rect(x0 - 1.5, y0 - 1.5, x1 + 1.5, y1 + 1.5), fill=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                  text=pymupdf.PDF_REDACT_TEXT_NONE)

        for fig in figures:
            path = out / "figures" / f"{fig['id']}.png"
            fig["px"] = crop_figure(page, fig["bbox"], raw_pages[slide["page"]]["images"], path)
            fig["file"] = str(path.relative_to(out)).replace("\\", "/")
        # Native tables leave the background the same way pictures do (text and rules), without a crop.
        figures += [e for e in slide["elements"] if e["kind"] in ("table", "diagram")]
        for fig in figures:
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

        # Panels last: figure crops taken above still show the panel colour behind them.
        verify_and_remove_shapes(original[slide["page"]], page, slide, raw_pages[slide["page"]])

    clean = out / "background.pdf"
    # garbage>=3 deduplicates objects, which after redaction corrupts beamer's soft-mask
    # shadows (Madrid blocks render as black bars). Only drop unused objects.
    doc.save(clean, garbage=1, deflate=True)

    paths = render_pages(clean, out / "backgrounds", BACKGROUND_WIDTH_PX, "bg",
                         [s["page"] for s in deck["slides"]])
    images = {i["id"]: i for page in raw["pages"] for i in page["images"]}
    for slide, path in zip(deck["slides"], paths):
        bullets = []
        for el in slide["elements"]:
            for p in el.get("paragraphs", []):
                b = p["bullet"]
                if b and b["kind"] == "image":
                    bullets.append(images[b["image"]]["bbox"])
                elif b and b.get("patch"):  # number drawn on a vector box
                    x0, y0, x1, y1 = b["bbox"]
                    bullets.append([x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5])
        if bullets:
            patch_rects(path, bullets, BACKGROUND_WIDTH_PX / slide["size"][0])
        slide["background"] = str(path.relative_to(out)).replace("\\", "/")
        slide["background_color"] = uniform_color(path)
    return paths


def uniform_color(png: Path, tolerance: int = 3) -> str | None:
    """The single colour of a background with nothing left on it, else None. Such slides get
    a plain Slides background colour instead of a picture."""
    pix = pymupdf.Pixmap(str(png))
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[..., :3]
    ref = np.median(img[::17, ::17].reshape(-1, 3), axis=0)
    if np.abs(img.astype(np.int16) - ref.astype(np.int16)).max() > tolerance:
        return None
    return "#" + "".join(f"{int(v):02x}" for v in ref)


def render_pages(pdf: Path, out_dir: Path, width_px: int, prefix: str, pages: list[int]) -> list[Path]:
    doc = pymupdf.open(pdf)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for page in (doc[n] for n in pages):
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
