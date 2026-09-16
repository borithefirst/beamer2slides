"""Beamer speaker notes: find them in the PDF, take them out of the slides.

Two beamer modes are supported, both with beamer's default "note page" template
(a grey header band with the frame title, the date and a tiny thumbnail of the frame):
  \\setbeameroption{show notes}                      a note page after each frame
  \\setbeameroption{show notes on second screen=right} double-width pages, note on the right

`prepare` writes a notes-free slides.pdf and returns the note text per slide page.
"""

from pathlib import Path

import pymupdf


def _note_header(page: pymupdf.Page, area: pymupdf.Rect) -> float | None:
    """Bottom of the note page header band inside `area`, or None if this is no note page."""
    for d in page.get_drawings():
        r = d["rect"]
        if d.get("fill") is None or abs(r.x0 - area.x0) > 1 or abs(r.x1 - area.x1) > 1 or r.y0 > area.y0 + 1:
            continue
        if not 0.15 * area.height < r.y1 - area.y0 < 0.35 * area.height:
            continue
        header = pymupdf.Rect(area.x0 + 0.6 * area.width, area.y0, area.x1, r.y1)
        spans = [s for b in page.get_text("dict", clip=area)["blocks"] for l in b.get("lines", [])
                 for s in l["spans"] if s["text"].strip()]
        sizes = sorted(s["size"] for s in spans)
        if not sizes:
            return None
        tiny = [s for s in spans if s["size"] < 0.45 * sizes[len(sizes) // 2] and header.contains(pymupdf.Rect(s["bbox"]))]
        if len(tiny) >= 1:  # the frame thumbnail
            return r.y1
    return None


def _note_text(page: pymupdf.Page, area: pymupdf.Rect, header_bottom: float) -> str:
    body = pymupdf.Rect(area.x0, header_bottom, area.x1, area.y1)
    lines: list[tuple[float, float, str, float]] = []  # baseline, x0, text, size
    for block in page.get_text("dict", clip=body)["blocks"]:
        for line in block.get("lines", []):
            text = " ".join(s["text"].strip() for s in line["spans"] if s["text"].strip())
            if text:
                size = max(s["size"] for s in line["spans"])
                lines.append((line["spans"][0]["origin"][1], line["bbox"][0], text, size))
    lines.sort()
    out, prev = [], None
    for baseline, _, text, size in lines:
        if prev is not None and abs(baseline - prev[0]) <= 0.3 * size:
            out[-1] += " " + text
        elif prev is not None and baseline - prev[0] > 1.6 * size:
            out.append("")
            out.append(text)
        else:
            out.append(text)
        prev = (baseline, size)
    # Join wrapped lines of one paragraph; blank entries separate paragraphs.
    paragraphs, current = [], []
    for item in out:
        if item == "":
            paragraphs.append(" ".join(current))
            current = []
        else:
            current.append(item)
    if current:
        paragraphs.append(" ".join(current))
    return "\n".join(p for p in paragraphs if p)


def prepare(pdf: Path, out: Path) -> tuple[Path, dict[int, str], str | None]:
    doc = pymupdf.open(pdf)
    if not doc.page_count:
        return pdf, {}, None
    first = doc[0].rect
    notes: dict[int, str] = {}

    if first.width / first.height >= 2.2:
        half = [pymupdf.Rect(p.rect.width / 2, 0, p.rect.width, p.rect.height) for p in doc]
        headers = [_note_header(p, a) for p, a in zip(doc, half)]
        if sum(h is not None for h in headers) >= 0.5 * doc.page_count:
            for page, area, header in zip(doc, half, headers):
                if header is not None:
                    text = _note_text(page, area, header)
                    if text:
                        notes[page.number] = text
                page.set_mediabox(pymupdf.Rect(0, 0, page.rect.width / 2, page.rect.height))
            path = out / "slides.pdf"
            doc.save(path, garbage=1, deflate=True)
            return path, notes, "second screen"
        return pdf, {}, None

    keep: list[int] = []
    for page in doc:
        header = _note_header(page, page.rect) if page.number else None
        if header is None:
            keep.append(page.number)
            continue
        text = _note_text(page, page.rect, header)
        if keep and text:
            notes[keep[-1]] = (notes.get(keep[-1], "") + "\n" + text).strip()
    if len(keep) == doc.page_count:
        return pdf, {}, None
    doc.select(keep)
    path = out / "slides.pdf"
    doc.save(path, garbage=1, deflate=True)
    return path, {keep.index(k): v for k, v in notes.items()}, "note pages"
