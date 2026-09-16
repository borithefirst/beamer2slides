"""Stage 1: dump each PDF page's text spans, images, drawings and links (raw.json)."""

from pathlib import Path

import pymupdf

TEXT_FLAGS = pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP


def _r(values, nd=2):
    return [round(float(v), nd) for v in values]


def _hex(rgb) -> str | None:
    if rgb is None:
        return None
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb[:3])


def _rounded_corners(d: dict) -> dict[str, float]:
    """Which bbox corners of a path are drawn with a curve, and the curve's radius."""
    r = d["rect"]
    corners: dict[str, float] = {}
    for item in d["items"]:
        if item[0] != "c":
            continue
        p1, p4 = item[1], item[4]
        mx, my = (p1.x + p4.x) / 2, (p1.y + p4.y) / 2
        key = ("t" if my < (r.y0 + r.y1) / 2 else "b") + ("l" if mx < (r.x0 + r.x1) / 2 else "r")
        corners[key] = round(max(abs(p4.x - p1.x), abs(p4.y - p1.y)), 2)
    return corners


def extract_page(page: pymupdf.Page) -> dict:
    n = page.number
    spans = []
    for block in page.get_text("dict", flags=TEXT_FLAGS)["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for s in line["spans"]:
                if not s["text"].strip():
                    continue
                spans.append({
                    "id": f"p{n}s{len(spans)}", "text": s["text"], "font": s["font"],
                    "size": round(s["size"], 3), "color": f"#{s['color']:06x}",
                    "origin": _r(s["origin"]), "bbox": _r(s["bbox"]), "dir": _r(line["dir"], 3),
                })

    images = [{
        "id": f"p{n}i{i}", "bbox": _r(info["bbox"]), "px": [info["width"], info["height"]],
        "xref": info.get("xref", 0),
    } for i, info in enumerate(page.get_image_info(xrefs=True))]

    drawings = [{
        "id": f"p{n}d{i}", "type": d["type"], "items": "".join(item[0] for item in d["items"]),
        "bbox": _r(d["rect"]), "fill": _hex(d.get("fill")), "stroke": _hex(d.get("color")),
        "width": round(d["width"], 2) if d.get("width") else None,
        "fill_opacity": round(d.get("fill_opacity") or 1.0, 3),
        "corners": _rounded_corners(d),
    } for i, d in enumerate(page.get_drawings())]

    links = [{"bbox": _r(link["from"]), "uri": link["uri"]}
             for link in page.get_links() if link.get("uri")]

    return {
        "index": n, "label": page.get_label() or str(n + 1),
        "size": _r((page.rect.width, page.rect.height)),
        "spans": spans, "images": images, "drawings": drawings, "links": links,
    }


def select_overlays(raw: dict, mode: str) -> dict:
    """Beamer gives every overlay step of a frame its own page, all with the frame number
    as page label. mode 'last' keeps only the final (complete) step of each frame; 'all'
    keeps every page. Handout PDFs have one page per label, so both are the same there."""
    if mode == "all":
        return raw
    pages = raw["pages"]
    kept = [p for i, p in enumerate(pages) if i + 1 == len(pages) or pages[i + 1]["label"] != p["label"]]
    return {**raw, "pages": kept, "overlays": {"mode": mode, "dropped": len(pages) - len(kept)}}


def extract(pdf: Path) -> dict:
    doc = pymupdf.open(pdf)
    return {
        "version": 1,
        "source": {"pdf": str(pdf), "producer": doc.metadata.get("producer"), "pages": doc.page_count,
                   "title": doc.metadata.get("title") or ""},
        "pages": [extract_page(page) for page in doc],
    }
