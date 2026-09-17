"""Find what stays behind when native elements are moved: ink in the background picture
under converted elements (shadows, strips, bullets, rules that were not removed).

Runs extract, classify and render locally (no Google API) into out/leftovers/<pdf stem>/,
then, for every native element (blocks as a whole), compares the background under its box
with the page colour around it. Prints the suspicious ones and writes
leftovers-NNN.png with those boxes outlined in red (others in green).

Usage: python tools/leftovers.py deck.pdf [deck.pdf ...] [--threshold 0.02]
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pymupdf

from beamer2slides.classify import classify
from beamer2slides.emit import merge_blocks
from beamer2slides.extract import extract, select_overlays
from beamer2slides.render import BACKGROUND_WIDTH_PX, render_backgrounds

ROOT = Path(__file__).resolve().parents[1]


def boxes(slide: dict) -> list[tuple[str, list[float]]]:
    """Movable units: a block (its shapes and everything on them) or a single element."""
    elements = merge_blocks(slide["elements"])
    out, taken = [], set()
    for el in elements:
        if el["kind"] == "shape" and el.get("title_bar"):
            x0, y0, x1, y1 = el["bbox"]
            members = [e for e in elements if e["kind"] != "shape" or e.get("block") == el["block"]]
            members = [e for e in members if x0 <= (e["bbox"][0] + e["bbox"][2]) / 2 <= x1
                       and y0 <= (e["bbox"][1] + e["bbox"][3]) / 2 <= y1]
            taken |= {id(e) for e in members}
            out.append((f"block {el['block']}", el["bbox"]))
    for el in elements:
        if id(el) not in taken and el.get("role") not in ("footer",):
            out.append((f"{el['kind']} {el['id']}", el["bbox"]))
    return out


def dirty_share(img: np.ndarray, rect_px: tuple[int, int, int, int], ring: int = 6) -> float:
    """Share of pixels inside the box that differ from the median colour of a ring around it."""
    a0, b0, a1, b1 = rect_px
    h, w = img.shape[:2]
    a0, b0, a1, b1 = max(0, a0), max(0, b0), min(w, a1), min(h, b1)
    if a1 <= a0 or b1 <= b0:
        return 0.0
    around = np.concatenate([
        img[max(0, b0 - ring):b0, a0:a1].reshape(-1, 3), img[b1:b1 + ring, a0:a1].reshape(-1, 3),
        img[b0:b1, max(0, a0 - ring):a0].reshape(-1, 3), img[b0:b1, a1:a1 + ring].reshape(-1, 3)])
    if not len(around):
        return 0.0
    ref = np.median(around, axis=0)
    inside = img[b0:b1, a0:a1].reshape(-1, 3).astype(int)
    return float((np.abs(inside - ref).max(axis=1) > 24).mean())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--threshold", type=float, default=0.02)
    args = ap.parse_args()
    for pdf in args.pdfs:
        name = pdf.stem if pdf.stem != "talk" else pdf.parent.name
        out = ROOT / "out" / "leftovers" / name
        out.mkdir(parents=True, exist_ok=True)
        raw = select_overlays(extract(pdf), "last")
        deck = classify(raw)
        render_backgrounds(pdf, raw, deck, out)
        for slide in deck["slides"]:
            png = out / slide["background"]
            pix = pymupdf.Pixmap(str(png))
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[..., :3]
            k = BACKGROUND_WIDTH_PX / slide["size"][0]
            flagged = []
            for label, (x0, y0, x1, y1) in boxes(slide):
                share = dirty_share(img, (int(x0 * k), int(y0 * k), int(x1 * k) + 1, int(y1 * k) + 1))
                flagged.append((label, [x0, y0, x1, y1], share))
            bad = [f for f in flagged if f[2] > args.threshold]
            if not bad:
                continue
            print(f"{name} slide {slide['page'] + 1}: " + "; ".join(f"{l} {s:.0%}" for l, _, s in bad))
            doc = pymupdf.open()
            page = doc.new_page(width=pix.width, height=pix.height)
            page.insert_image(page.rect, filename=str(png))
            for label, (x0, y0, x1, y1), share in flagged:
                color = (0.9, 0, 0) if share > args.threshold else (0, 0.6, 0)
                page.draw_rect(pymupdf.Rect(x0 * k, y0 * k, x1 * k, y1 * k), color=color, width=3)
            page.get_pixmap().save(out / f"leftovers-{slide['page'] + 1:03}.png")
        (out / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
