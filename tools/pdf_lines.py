"""Print every text line of a PDF with its origin and size, in Google Slides points
(720 pt page width). Handy when tuning a beamer theme's geometry against a template.

Usage: python tools/pdf_lines.py deck.pdf [page ...]   (pages 1-based)
"""

import sys

from beamer2slides.extract import spans
from beamer2slides.pdf import Document, Page


def text_lines(page: Page) -> list[dict]:
    """Spans on one baseline, left to right: {"text", "origin", "size", "font"} of the first span."""
    lines: list[list[dict]] = []
    for s in sorted((s for s in spans(page) if s["text"].strip()), key=lambda s: (s["origin"][1], s["origin"][0])):
        if lines and abs(lines[-1][0]["origin"][1] - s["origin"][1]) <= 0.3 * s["size"]:
            lines[-1].append(s)
        else:
            lines.append([s])
    out = []
    for line in lines:
        line.sort(key=lambda s: s["origin"][0])
        text = ""
        for prev, s in zip([None] + line, line):
            gap = s["bbox"][0] - prev["bbox"][2] if prev else 0
            text += (" " if prev and gap > 0.1 * s["size"] else "") + s["text"]
        out.append({"text": text.strip(), "origin": line[0]["origin"], "size": line[0]["size"], "font": line[0]["font"]})
    return out


def main() -> None:
    doc = Document(sys.argv[1])
    pages = [int(p) for p in sys.argv[2:]] or range(1, len(doc) + 1)
    for n in pages:
        page = doc[n - 1]
        k = 720 / page.width
        print(f"--- page {n}")
        for line in text_lines(page):
            print(f"  x {line['origin'][0] * k:7.1f}  base {line['origin'][1] * k:7.1f}  size {line['size'] * k:5.1f}"
                  f"  {line['font']:<28} {line['text'][:60]}")


if __name__ == "__main__":
    main()
