"""Draw classification decisions on top of the PDF pages, for eyeballing."""

from pathlib import Path

import pymupdf

REASON_COLORS = {
    "math": (0.85, 0.1, 0.1),
    "figure": (0.1, 0.35, 0.9),
    "theme": (0.5, 0.5, 0.5),
    "rotated": (0.6, 0.2, 0.7),
    "unsure": (1.0, 0.55, 0.0),
}
NATIVE = (0.0, 0.65, 0.2)
FIGURE = (0.0, 0.55, 0.75)


def render_debug(pdf: Path, deck: dict, out_dir: Path, zoom: float = 3.0) -> list[Path]:
    doc = pymupdf.open(pdf)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for slide in deck["slides"]:
        page = doc[slide["page"]]
        for p in slide["panels"]:
            page.draw_rect(pymupdf.Rect(p["bbox"]), color=(0.9, 0.75, 0.0), width=0.4, dashes="[1 1] 0")
        for r in slide["figure_regions"]:
            page.draw_rect(pymupdf.Rect(r), color=REASON_COLORS["figure"], width=0.6, dashes="[3 2] 0")
        for left in slide["left_in_background"]:
            color = REASON_COLORS.get(left["reason"], (0, 0, 0))
            for b in left["bboxes"]:
                page.draw_rect(pymupdf.Rect(b), color=None, fill=color, fill_opacity=0.25, width=0)
        for el in slide["elements"]:
            x0, y0, x1, y1 = el["bbox"]
            if el["kind"] == "table":
                page.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=(0.55, 0.1, 0.6), width=1.0)
                for col in el["columns"]:
                    page.draw_line((col["x0"], y0), (col["x0"], y1), color=(0.55, 0.1, 0.6), width=0.3)
                page.insert_text((x0, y0 - 1.5), f"table {len(el['row_baselines'])}x{len(el['columns'])}",
                                 fontsize=3.5, color=(0.55, 0.1, 0.6))
                continue
            if el["kind"] == "shape":
                page.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=(0.9, 0.4, 0.0), width=0.8, dashes="[2 1] 0")
                page.insert_text((x1 - 40, y0 + 4), el["shape"].lower()[:18], fontsize=3, color=(0.9, 0.4, 0.0))
                continue
            if el["kind"] == "image":
                page.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=FIGURE, width=1.0)
                page.insert_text((x0, y0 - 1.5), f"picture {len(el['spans'])} labels", fontsize=3.5, color=FIGURE)
                continue
            page.draw_rect(pymupdf.Rect(x0 - 1, y0 - 1, x1 + 1, y1 + 1), color=NATIVE, width=0.7)
            for par in el["paragraphs"]:
                for line in par["lines"]:
                    page.draw_line((line["x0"], line["baseline"]), (line["x1"], line["baseline"]),
                                   color=NATIVE, width=0.3)
                if par["bullet"]:
                    page.draw_rect(pymupdf.Rect(par["bullet"]["bbox"]), color=NATIVE, width=0.3)
            label = f"{el['role']} " + " ".join(
                f"L{p['level']}{p['align'][0]}" if p["bullet"] else p["align"][0] for p in el["paragraphs"])
            page.insert_text((el["bbox"][0], el["bbox"][1] - 1.5), label[:60], fontsize=3.5, color=NATIVE)
        path = out_dir / f"slide-{slide['page'] + 1:03}.png"
        page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(path)
        paths.append(path)
    return paths
