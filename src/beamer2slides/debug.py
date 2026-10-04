"""Draw classification decisions on top of the PDF pages, for eyeballing."""

import functools
from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import ir
from .pdf import Document
from .render import PNG_WRITERS, PngWriter

Rgb = tuple[float, float, float]
Dashes = tuple[float, float]

REASON_COLORS: dict[str, Rgb] = {
    "math": (0.85, 0.1, 0.1),
    "figure": (0.1, 0.35, 0.9),
    "theme": (0.5, 0.5, 0.5),
    "rotated": (0.6, 0.2, 0.7),
    "unsure": (1.0, 0.55, 0.0),
}
NATIVE: Rgb = (0.0, 0.65, 0.2)
FIGURE: Rgb = (0.0, 0.55, 0.75)
PANEL: Rgb = (0.9, 0.75, 0.0)
UNKNOWN_REASON: Rgb = (0.0, 0.0, 0.0)


class Canvas:
    """Drawing in page points on a page rendered at `zoom` pixels per point."""

    def __init__(self, image: Image.Image, zoom: float) -> None:
        self.image, self.zoom = image, zoom
        self.overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.overlay)

    def _c(self, color: Rgb, alpha: float) -> tuple[int, int, int, int]:
        r, g, b = color
        return round(255 * r), round(255 * g), round(255 * b), round(255 * alpha)

    def line(self, p: Sequence[float], q: Sequence[float], color: Rgb, width: float,
             dashes: Dashes | None) -> None:
        z = self.zoom
        (x0, y0), (x1, y1) = p, q
        w = max(1, round(width * z))
        fill = self._c(color, 1.0)
        if not dashes:
            self.draw.line([(x0 * z, y0 * z), (x1 * z, y1 * z)], fill=fill, width=w)
            return
        on, off = dashes
        length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        t = 0.0
        while t < length:
            e = min(t + on, length)
            a, b = t / length, e / length
            self.draw.line([((x0 + (x1 - x0) * a) * z, (y0 + (y1 - y0) * a) * z),
                            ((x0 + (x1 - x0) * b) * z, (y0 + (y1 - y0) * b) * z)], fill=fill, width=w)
            t = e + off

    def outline(self, r: Sequence[float], color: Rgb, width: float, dashes: Dashes | None) -> None:
        """The four sides of box `r`."""
        x0, y0, x1, y1 = r
        for p, q in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
            self.line(p, q, color, width, dashes)

    def shade(self, r: Sequence[float], fill: Rgb, opacity: float) -> None:
        """Box `r` filled with `fill` at `opacity`."""
        x0, y0, x1, y1 = r
        z = self.zoom
        self.draw.rectangle([x0 * z, y0 * z, x1 * z, y1 * z], fill=self._c(fill, opacity))

    def text(self, at: tuple[float, float], text: str, size: float, color: Rgb) -> None:
        font = ImageFont.load_default(size * self.zoom)
        x, y = at
        self.draw.text((x * self.zoom, y * self.zoom), text, fill=self._c(color, 1.0), font=font, anchor="ls")

    def save(self, path: Path) -> None:
        # (fast compression: a picture for a person to look at, written by every classify)
        Image.alpha_composite(self.image.convert("RGBA"), self.overlay).convert("RGB").save(path, compress_level=1)


def _draw_element(page: Canvas, el: ir.Element) -> None:
    x0, y0, x1, y1 = el["bbox"]
    if el["kind"] == "diagram":
        teal = (0.0, 0.5, 0.5)
        for n in el["nodes"]:
            page.outline(n["bbox"], teal, 0.8, None)
        for ln in el["lines"]:
            page.line(ln["from"], ln["to"], teal, 0.8, None)
        page.text((x0, y0 - 1.5), f"diagram {len(el['nodes'])} nodes {len(el['lines'])} lines", 3.5, teal)
        return
    if el["kind"] == "table":
        purple = (0.55, 0.1, 0.6)
        page.outline((x0, y0, x1, y1), purple, 1.0, None)
        for col in el["columns"]:
            page.line((col["x0"], y0), (col["x0"], y1), purple, 0.3, None)
        page.text((x0, y0 - 1.5), f"table {len(el['row_baselines'])}x{len(el['columns'])}", 3.5, purple)
        return
    if el["kind"] == "shape":
        orange = (0.9, 0.4, 0.0)
        page.outline((x0, y0, x1, y1), orange, 0.8, (2, 1))
        page.text((x1 - 40, y0 + 4), el["shape"].lower()[:18], 3, orange)
        return
    if el["kind"] == "image":
        page.outline((x0, y0, x1, y1), FIGURE, 1.0, None)
        page.text((x0, y0 - 1.5), f"picture {len(el['spans'])} labels", 3.5, FIGURE)
        return
    page.outline((x0 - 1, y0 - 1, x1 + 1, y1 + 1), NATIVE, 0.7, None)
    for par in el["paragraphs"]:
        for line in ([] if el.get("rotation") else par["lines"]):  # (turned text: lines in its own frame)
            page.line((line["x0"], line["baseline"]), (line["x1"], line["baseline"]), NATIVE, 0.3, None)
        bullet = par["bullet"]
        if bullet:
            page.outline(bullet["bbox"], NATIVE, 0.3, None)
    label = f"{el['role']} " + " ".join(
        f"L{p['level']}{p['align'][0]}" if p["bullet"] else p["align"][0] for p in el["paragraphs"])
    page.text((el["bbox"][0], el["bbox"][1] - 1.5), label[:60], 3.5, NATIVE)


def render_debug(pdf: Path, deck: ir.Deck, out_dir: Path, zoom: float) -> list[Path]:
    """One picture per slide under `out_dir`: the page at `zoom` pixels per point with what
    classify decided drawn over it."""
    writer = PngWriter(PNG_WRITERS)  # (the pictures are written while the next page is drawn)
    try:
        paths = _render_debug(pdf, deck, out_dir, zoom, writer)
        writer.finish()
    finally:
        writer.close()
    return paths


def _render_debug(pdf: Path, deck: ir.Deck, out_dir: Path, zoom: float, writer: PngWriter) -> list[Path]:
    doc = Document(pdf)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for slide in deck["slides"]:
        page = Canvas(Image.fromarray(doc[slide["page"]].render(zoom, clip=None, transparent=False)), zoom)
        for p in slide["panels"]:
            page.outline(p["bbox"], PANEL, 0.4, (1, 1))
        for r in slide["figure_regions"]:
            page.outline(r, REASON_COLORS["figure"], 0.6, (3, 2))
        for left in slide["left_in_background"]:
            color = REASON_COLORS.get(left["reason"], UNKNOWN_REASON)
            for b in left["bboxes"]:
                page.shade(b, color, 0.25)
        for el in slide["elements"]:
            _draw_element(page, el)
        path = out_dir / f"slide-{slide['page'] + 1:03}.png"
        writer.submit(functools.partial(page.save, path))
        paths.append(path)
    doc.close()
    return paths
