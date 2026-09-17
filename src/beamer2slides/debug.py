"""Draw classification decisions on top of the PDF pages, for eyeballing."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .pdf import Document

REASON_COLORS = {
    "math": (0.85, 0.1, 0.1),
    "figure": (0.1, 0.35, 0.9),
    "theme": (0.5, 0.5, 0.5),
    "rotated": (0.6, 0.2, 0.7),
    "unsure": (1.0, 0.55, 0.0),
}
NATIVE = (0.0, 0.65, 0.2)
FIGURE = (0.0, 0.55, 0.75)


class Canvas:
    """Drawing in page points on a page rendered at `zoom` pixels per point."""

    def __init__(self, image: Image.Image, zoom: float):
        self.image, self.zoom = image, zoom
        self.overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.overlay)

    def _c(self, color, alpha=1.0):
        return tuple(round(255 * v) for v in color) + (round(255 * alpha),)

    def line(self, p, q, color, width, dashes: tuple[float, float] | None = None):
        z = self.zoom
        (x0, y0), (x1, y1) = p, q
        w = max(1, round(width * z))
        if not dashes:
            self.draw.line([(x0 * z, y0 * z), (x1 * z, y1 * z)], fill=self._c(color), width=w)
            return
        on, off = dashes
        length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        t = 0.0
        while t < length:
            e = min(t + on, length)
            a, b = t / length, e / length
            self.draw.line([((x0 + (x1 - x0) * a) * z, (y0 + (y1 - y0) * a) * z),
                            ((x0 + (x1 - x0) * b) * z, (y0 + (y1 - y0) * b) * z)], fill=self._c(color), width=w)
            t = e + off

    def rect(self, r, color=None, width=0.5, fill=None, fill_opacity=1.0, dashes=None):
        x0, y0, x1, y1 = r
        if fill is not None:
            z = self.zoom
            self.draw.rectangle([x0 * z, y0 * z, x1 * z, y1 * z], fill=self._c(fill, fill_opacity))
        if color is not None:
            for p, q in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
                self.line(p, q, color, width, dashes)

    def text(self, at, text, size, color):
        font = ImageFont.load_default(size * self.zoom)
        x, y = at
        self.draw.text((x * self.zoom, y * self.zoom), text, fill=self._c(color), font=font, anchor="ls")

    def save(self, path: Path):
        Image.alpha_composite(self.image.convert("RGBA"), self.overlay).convert("RGB").save(path)


def render_debug(pdf: Path, deck: dict, out_dir: Path, zoom: float = 3.0) -> list[Path]:
    doc = Document(pdf)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for slide in deck["slides"]:
        page = Canvas(Image.fromarray(doc[slide["page"]].render(zoom)), zoom)
        for p in slide["panels"]:
            page.rect(p["bbox"], color=(0.9, 0.75, 0.0), width=0.4, dashes=(1, 1))
        for r in slide["figure_regions"]:
            page.rect(r, color=REASON_COLORS["figure"], width=0.6, dashes=(3, 2))
        for left in slide["left_in_background"]:
            color = REASON_COLORS.get(left["reason"], (0, 0, 0))
            for b in left["bboxes"]:
                page.rect(b, fill=color, fill_opacity=0.25)
        for el in slide["elements"]:
            x0, y0, x1, y1 = el["bbox"]
            if el["kind"] == "diagram":
                teal = (0.0, 0.5, 0.5)
                for n in el["nodes"]:
                    page.rect(n["bbox"], color=teal, width=0.8)
                for ln in el["lines"]:
                    page.line(ln["from"], ln["to"], color=teal, width=0.8)
                page.text((x0, y0 - 1.5), f"diagram {len(el['nodes'])} nodes {len(el['lines'])} lines", 3.5, teal)
                continue
            if el["kind"] == "table":
                purple = (0.55, 0.1, 0.6)
                page.rect((x0, y0, x1, y1), color=purple, width=1.0)
                for col in el["columns"]:
                    page.line((col["x0"], y0), (col["x0"], y1), color=purple, width=0.3)
                page.text((x0, y0 - 1.5), f"table {len(el['row_baselines'])}x{len(el['columns'])}", 3.5, purple)
                continue
            if el["kind"] == "shape":
                orange = (0.9, 0.4, 0.0)
                page.rect((x0, y0, x1, y1), color=orange, width=0.8, dashes=(2, 1))
                page.text((x1 - 40, y0 + 4), el["shape"].lower()[:18], 3, orange)
                continue
            if el["kind"] == "image":
                page.rect((x0, y0, x1, y1), color=FIGURE, width=1.0)
                page.text((x0, y0 - 1.5), f"picture {len(el['spans'])} labels", 3.5, FIGURE)
                continue
            page.rect((x0 - 1, y0 - 1, x1 + 1, y1 + 1), color=NATIVE, width=0.7)
            for par in el["paragraphs"]:
                for line in par["lines"]:
                    page.line((line["x0"], line["baseline"]), (line["x1"], line["baseline"]), color=NATIVE, width=0.3)
                if par["bullet"]:
                    page.rect(par["bullet"]["bbox"], color=NATIVE, width=0.3)
            label = f"{el['role']} " + " ".join(
                f"L{p['level']}{p['align'][0]}" if p["bullet"] else p["align"][0] for p in el["paragraphs"])
            page.text((el["bbox"][0], el["bbox"][1] - 1.5), label[:60], 3.5, NATIVE)
        path = out_dir / f"slide-{slide['page'] + 1:03}.png"
        page.save(path)
        paths.append(path)
    doc.close()
    return paths
