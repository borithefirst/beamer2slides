"""Dump what PyMuPDF sees on PDF pages: text spans, images, vector drawings.

Usage: python tools/probe.py file.pdf [page-numbers, 1-based] [--png DIR]
"""

import argparse
from collections import Counter
from pathlib import Path

import pymupdf


def fmt_rect(r) -> str:
    x0, y0, x1, y1 = r
    return f"({x0:6.1f},{y0:6.1f})-({x1:6.1f},{y1:6.1f})"


def probe_page(page: pymupdf.Page) -> None:
    print(f"\n=== page {page.number + 1}  size {page.rect.width:.1f} x {page.rect.height:.1f} pt ===")

    print("-- text spans (block/line) --")
    d = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES)
    for b in d["blocks"]:
        if b["type"] != 0:
            continue
        for li, line in enumerate(b["lines"]):
            for s in line["spans"]:
                if not s["text"].strip():
                    continue
                print(
                    f"  b{b['number']:<2} l{li:<2} {fmt_rect(s['bbox'])} "
                    f"{s['font'][:22]:<22} {s['size']:5.2f} #{s['color']:06x} {s['text']!r}"
                )

    print("-- images --")
    for info in page.get_image_info(xrefs=True):
        print(f"  xref {info['xref']:<4} {fmt_rect(info['bbox'])} {info['width']}x{info['height']}px")

    drawings = page.get_drawings()
    kinds = Counter()
    for dr in drawings:
        kinds[(dr["type"], tuple(i[0] for i in dr["items"]))] += 1
    print(f"-- drawings: {len(drawings)} paths --")
    for (typ, items), n in kinds.most_common(12):
        print(f"  {n:4}x type={typ} items={''.join(items)[:40]}")
    for dr in drawings[:8]:
        fill = dr.get("fill")
        print(f"  {fmt_rect(dr['rect'])} type={dr['type']} fill={fill} items={len(dr['items'])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("pages", nargs="*", type=int)
    ap.add_argument("--png", type=Path, help="also render the pages as PNGs into this folder")
    args = ap.parse_args()

    doc = pymupdf.open(args.pdf)
    print(f"{args.pdf}: {doc.page_count} pages, metadata={doc.metadata}")
    fonts = {f[3] for p in doc for f in p.get_fonts()}
    print("fonts:", sorted(fonts))
    pages = [n - 1 for n in args.pages] or range(doc.page_count)
    for n in pages:
        page = doc[n]
        probe_page(page)
        if args.png:
            args.png.mkdir(parents=True, exist_ok=True)
            out = args.png / f"{Path(args.pdf).stem}-p{n + 1:02}.png"
            page.get_pixmap(dpi=110).save(out)


if __name__ == "__main__":
    main()
