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
from PIL import Image, ImageDraw

from beamer2slides.arrays import RGB
from beamer2slides.classify import classify
from beamer2slides.emit import merge_blocks
from beamer2slides.extract import extract, select_overlays
from beamer2slides.ir import deck_json
from beamer2slides.json_types import Json, JsonObject, as_array, as_int, as_objects, as_str
from beamer2slides.render import BACKGROUND_WIDTH_PX, render_backgrounds

ROOT = Path(__file__).resolve().parents[1]
RING = 6   # px of page around a box its colour is judged against

Box = tuple[float, float, float, float]


def number(value: Json, where: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    raise ValueError(f"{where}: a number was expected, found {value!r}")


def bbox(el: JsonObject) -> Box:
    x0, y0, x1, y1 = (number(v, f"{el.get('id')}.bbox") for v in as_array(el["bbox"], f"{el.get('id')}.bbox"))
    return x0, y0, x1, y1


def boxes(slide: JsonObject) -> list[tuple[str, Box]]:
    """Movable units: a block (its shapes and everything on them) or a single element."""
    elements = merge_blocks(as_objects(slide["elements"], "slide.elements"))
    out: list[tuple[str, Box]] = []
    taken: set[int] = set()
    for el in elements:
        if el["kind"] == "shape" and el.get("title_bar"):
            x0, y0, x1, y1 = bbox(el)
            members = [e for e in elements if e["kind"] != "shape" or e.get("block") == el["block"]]
            members = [e for e in members if x0 <= (bbox(e)[0] + bbox(e)[2]) / 2 <= x1
                       and y0 <= (bbox(e)[1] + bbox(e)[3]) / 2 <= y1]
            taken |= {id(e) for e in members}
            out.append((f"block {el['block']}", bbox(el)))
    for el in elements:
        if id(el) not in taken and el.get("role") not in ("footer",):
            out.append((f"{el['kind']} {el['id']}", bbox(el)))
    return out


def dirty_share(img: RGB, rect_px: tuple[int, int, int, int], ring: int) -> float:
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
    pdfs: list[Path] = args.pdfs
    threshold: float = args.threshold
    for pdf in pdfs:
        name = pdf.stem if pdf.stem != "talk" else pdf.parent.name
        out = ROOT / "out" / "leftovers" / name
        out.mkdir(parents=True, exist_ok=True)
        raw = select_overlays(extract(pdf, None), "last")
        deck = deck_json(classify(raw))
        render_backgrounds(pdf, raw, deck, out, frozenset())
        for slide in as_objects(deck["slides"], "deck.slides"):
            png = out / as_str(slide["background"], "slide.background")
            image = Image.open(png).convert("RGB")
            img: RGB = np.asarray(image)
            k = BACKGROUND_WIDTH_PX / number(as_array(slide["size"], "slide.size")[0], "slide.size")
            page = as_int(slide["page"], "slide.page")
            flagged: list[tuple[str, Box, float]] = []
            for label, (x0, y0, x1, y1) in boxes(slide):
                share = dirty_share(img, (int(x0 * k), int(y0 * k), int(x1 * k) + 1, int(y1 * k) + 1), RING)
                flagged.append((label, (x0, y0, x1, y1), share))
            bad = [f for f in flagged if f[2] > threshold]
            if not bad:
                continue
            print(f"{name} slide {page + 1}: " + "; ".join(f"{l} {s:.0%}" for l, _, s in bad))
            draw = ImageDraw.Draw(image)
            for label, (x0, y0, x1, y1), share in flagged:
                color = (230, 0, 0) if share > threshold else (0, 153, 0)
                draw.rectangle([x0 * k, y0 * k, x1 * k, y1 * k], outline=color, width=3)
            image.save(out / f"leftovers-{page + 1:03}.png")
        (out / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
