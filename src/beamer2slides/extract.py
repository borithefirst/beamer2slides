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


def _label(page: pymupdf.Page) -> str:
    label = page.get_label() or str(page.number + 1)
    if label.startswith("<FEFF") and label.endswith(">"):  # raw UTF-16BE hex string
        try:
            label = bytes.fromhex(label[5:-1]).decode("utf-16-be")
        except ValueError:
            pass
    return label


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

    links = []
    for link in page.get_links():
        if link.get("uri"):
            links.append({"bbox": _r(link["from"]), "uri": link["uri"]})
        elif link["kind"] in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) and link.get("page", -1) >= 0:
            links.append({"bbox": _r(link["from"]), "page": link["page"]})  # TOC entries, \hyperlink

    # Anything entirely outside the page (e.g. the cut-off half of a notes-on-second-screen page).
    area = page.rect
    inside = lambda b: b[2] > area.x0 and b[0] < area.x1 and b[3] > area.y0 and b[1] < area.y1
    spans = [s for s in spans if inside(s["bbox"])]
    images = [i for i in images if inside(i["bbox"])]
    drawings = [d for d in drawings if inside(d["bbox"])]
    links = [l for l in links if inside(l["bbox"])]

    return {
        "index": n, "label": _label(page),
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

    def heading(p: dict) -> str:  # text in the top fifth of the page: the frame title
        return " ".join(s["text"].strip() for s in sorted(p["spans"], key=lambda s: s["bbox"][0])
                        if s["bbox"][3] < 0.2 * p["size"][1])

    def words(p: dict) -> list[str]:
        return [w for s in p["spans"] for w in s["text"].split()]

    def same_frame(a: dict, b: dict) -> bool:
        """Overlay steps share the frame number, the heading and most of their text (a later
        step shows what the earlier one did). Themes that don't count some frames (title and
        section pages) share numbers too, but not their text."""
        if a["label"] != b["label"] or heading(a) != heading(b):
            return False
        wa, wb = words(a), set(words(b))
        # \only<n> swaps some text between steps, so require a majority, not everything.
        return not wa or sum(w in wb for w in wa) >= 0.5 * len(wa)

    kept = [p for i, p in enumerate(pages) if i + 1 == len(pages) or not same_frame(p, pages[i + 1])]
    return {**raw, "pages": kept, "overlays": {"mode": mode, "dropped": len(pages) - len(kept)}}


def extract(pdf: Path) -> dict:
    doc = pymupdf.open(pdf)
    return {
        "version": 1,
        "source": {"pdf": str(pdf), "producer": doc.metadata.get("producer"), "pages": doc.page_count,
                   "title": doc.metadata.get("title") or ""},
        "pages": [extract_page(page) for page in doc],
    }
