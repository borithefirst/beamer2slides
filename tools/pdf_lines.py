"""Print every text line of a PDF with its origin and size, in Google Slides points
(720 pt page width). Handy when tuning a beamer theme's geometry against a template.

Usage: python tools/pdf_lines.py deck.pdf [page ...]   (pages 1-based)
"""

import sys

import pymupdf


def main() -> None:
    doc = pymupdf.open(sys.argv[1])
    pages = [int(p) for p in sys.argv[2:]] or range(1, len(doc) + 1)
    for n in pages:
        page = doc[n - 1]
        k = 720 / page.rect.width
        print(f"--- page {n}")
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = [s for s in line["spans"] if s["text"].strip()]
                if spans:
                    s = spans[0]
                    text = "".join(x["text"] for x in line["spans"]).strip()
                    print(f"  x {s['origin'][0] * k:7.1f}  base {s['origin'][1] * k:7.1f}  size {s['size'] * k:5.1f}"
                          f"  {s['font']:<28} {text[:60]}")


if __name__ == "__main__":
    main()
