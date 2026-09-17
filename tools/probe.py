"""Dump what extraction sees on PDF pages: text spans, images, vector drawings.

Usage: python tools/probe.py file.pdf [page-numbers, 1-based] [--png DIR]
"""

import argparse
from collections import Counter
from pathlib import Path

from PIL import Image

from beamer2slides.extract import spans
from beamer2slides.pdf import Document, Page


def fmt_rect(r) -> str:
    x0, y0, x1, y1 = r
    return f"({x0:6.1f},{y0:6.1f})-({x1:6.1f},{y1:6.1f})"


def probe_page(page: Page) -> None:
    print(f"\n=== page {page.index + 1}  size {page.width:.1f} x {page.height:.1f} pt ===")

    print("-- text spans --")
    for s in spans(page):
        if s["text"].strip():
            print(f"  {fmt_rect(s['bbox'])} {s['font'][:22]:<22} {s['size']:5.2f} #{s['color']:06x} {s['text']!r}")

    print("-- images and shadings --")
    for info in page.images():
        print(f"  {fmt_rect(info['bbox'])} {info['width']}x{info['height']}px")

    drawings = page.drawings()
    kinds = Counter()
    for dr in drawings:
        kinds[(dr["type"], "".join(i[0] for i in dr["items"]))] += 1
    print(f"-- drawings: {len(drawings)} paths --")
    for (typ, items), n in kinds.most_common(12):
        print(f"  {n:4}x type={typ} items={items[:40]}")
    for dr in drawings[:8]:
        print(f"  {fmt_rect(dr['rect'])} type={dr['type']} fill={dr.get('fill')} items={len(dr['items'])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("pages", nargs="*", type=int)
    ap.add_argument("--png", type=Path, help="also render the pages as PNGs into this folder")
    args = ap.parse_args()

    doc = Document(args.pdf)
    print(f"{args.pdf}: {len(doc)} pages, metadata={doc.metadata}")
    pages = [n - 1 for n in args.pages] or range(len(doc))
    fonts = {ch.font for n in pages for ch in doc[n].chars()}
    print("fonts:", sorted(fonts))
    for n in pages:
        page = doc[n]
        probe_page(page)
        if args.png:
            args.png.mkdir(parents=True, exist_ok=True)
            Image.fromarray(page.render(110 / 72)).save(args.png / f"{Path(args.pdf).stem}-p{n + 1:02}.png")


if __name__ == "__main__":
    main()
