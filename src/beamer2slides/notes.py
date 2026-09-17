"""Beamer speaker notes: find them in the PDF, take them out of the slides.

Two beamer modes are supported, both with beamer's default "note page" template
(a grey header band with the frame title, the date and a tiny thumbnail of the frame):
  \\setbeameroption{show notes}                      a note page after each frame
  \\setbeameroption{show notes on second screen=right} double-width pages, note on the right

`prepare` writes a notes-free slides.pdf and returns the note text per slide page.
"""

from typing import NamedTuple

from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as R

from .extract import spans as page_spans
from .pdf import Document, Page

Box = tuple[float, float, float, float]


def _contains(outer: Box, inner) -> bool:
    return inner[0] >= outer[0] and inner[1] >= outer[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _spans_in(spans: list[dict], area: Box) -> list[dict]:
    return [s for s in spans if s["text"].strip() and
            area[0] <= (s["bbox"][0] + s["bbox"][2]) / 2 <= area[2] and area[1] <= (s["bbox"][1] + s["bbox"][3]) / 2 <= area[3]]


def _note_header(page: Page, spans: list[dict], area: Box) -> float | None:
    """Bottom of the note page header band inside `area`, or None if this is no note page."""
    x0, y0, x1, y1 = area
    width, height = x1 - x0, y1 - y0
    for d in page.drawings():
        r = d["rect"]
        if d.get("fill") is None or abs(r[0] - x0) > 1 or abs(r[2] - x1) > 1 or r[1] > y0 + 1:
            continue
        if not 0.15 * height < r[3] - y0 < 0.35 * height:
            continue
        header = (x0 + 0.6 * width, y0, x1, r[3])
        inside = _spans_in(spans, area)
        # Text size of the note itself: the frame thumbnail can hold more words than a short note.
        body = [s for s in inside if not _contains(header, s["bbox"])] or inside
        sizes = sorted(s["size"] for s in body)
        if not sizes:
            return None
        tiny = [s for s in inside if s["size"] < 0.45 * sizes[len(sizes) // 2] and _contains(header, s["bbox"])]
        if len(tiny) >= 1:  # the frame thumbnail
            return r[3]
    return None


def _note_text(spans: list[dict], area: Box, header_bottom: float) -> str:
    body = (area[0], header_bottom, area[2], area[3])
    lines: list[list] = []  # [baseline, x1, text, size]
    for s in sorted(_spans_in(spans, body), key=lambda s: (round(s["origin"][1], 1), s["bbox"][0])):
        text, size, baseline = s["text"].strip(), s["size"], s["origin"][1]
        line = lines[-1] if lines else None
        if line and abs(baseline - line[0]) <= 0.3 * size:
            # Pieces of one word (a style change) join; a word gap becomes a space.
            line[2] += ("" if s["bbox"][0] - line[1] < 0.1 * size else " ") + text
            line[1] = s["bbox"][2]
            line[3] = max(line[3], size)
        else:
            lines.append([baseline, s["bbox"][2], text, size])
    out, prev = [], None
    for baseline, _, text, size in lines:
        if prev is not None and baseline - prev[0] > 1.6 * size:
            out.append("")
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


class Prepared(NamedTuple):
    pdf: Path                    # the slides without note pages
    notes: dict[int, str]        # note text by page of `pdf`
    mode: str | None             # "note pages", "second screen" or None
    labels: list[str] | None     # page labels of `pdf` when its own are stale (pages were deleted)


def prepare(pdf: Path, out: Path) -> Prepared:
    doc = Document(pdf)
    try:
        return Prepared(*_prepare(doc, pdf, out))
    finally:
        doc.close()


def _prepare(doc: Document, pdf: Path, out: Path) -> tuple:
    if not len(doc):
        return pdf, {}, None, None
    first = doc[0]
    notes: dict[int, str] = {}
    path = out / "slides.pdf"

    if first.width / first.height >= 2.2:
        half = [(p.width / 2, 0.0, p.width, p.height) for p in doc]
        spans = [page_spans(p) for p in doc]
        headers = [_note_header(p, s, a) for p, s, a in zip(doc, spans, half)]
        if sum(h is not None for h in headers) < 0.5 * len(doc):
            return pdf, {}, None, None
        for page, s, area, header in zip(doc, spans, half, headers):
            if header is not None:
                text = _note_text(s, area, header)
                if text:
                    notes[page.index] = text
        edited = pdfium.PdfDocument(str(pdf))
        for page in doc:  # keep the left half: the slide
            raw = edited[page.index].raw
            left, top = page.left, page.top
            box = (left, top - page.height, left + page.width / 2, top)
            R.FPDFPage_SetMediaBox(raw, *box)
            R.FPDFPage_SetCropBox(raw, *box)
        edited.save(str(path))
        edited.close()
        return path, notes, "second screen", None

    keep: list[int] = []
    for page in doc:
        spans = page_spans(page) if page.index else []
        header = _note_header(page, spans, page.rect) if page.index else None
        if header is None:
            keep.append(page.index)
            continue
        text = _note_text(spans, page.rect, header)
        if keep and text:
            notes[keep[-1]] = (notes.get(keep[-1], "") + "\n" + text).strip()
    if len(keep) == len(doc):
        return pdf, {}, None, None
    edited = pdfium.PdfDocument(str(pdf))
    for index in reversed(range(len(doc))):
        if index not in keep:
            edited.del_page(index)
    edited.save(str(path))
    edited.close()
    # The saved page label tree still counts the deleted pages.
    labels = [doc.label(k) or str(k + 1) for k in keep]
    return path, {keep.index(k): v for k, v in notes.items()}, "note pages", labels
