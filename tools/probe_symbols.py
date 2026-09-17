"""Probe: advance widths (em) of math symbols in Slides text (Lato, with Slides' own fallback
fonts for glyphs Lato lacks). emit.formula_shifts uses them to predict where an inline
formula picture's gap lands after symbols like ∈ or ℝ earlier on the line.

Each row is "|  " + symbol * N + "  |"; the distance between the two bars, minus the same
row without symbols, is N advances.

Usage: python tools/probe_symbols.py   (prints a dict for emit.SYMBOL_ADVANCE_EM)
"""

from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.google_auth import slides_service
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

OUT = Path(__file__).resolve().parents[1] / "out"
SYMBOLS = list("=+−<>≤≥×·/∑∏∫∈∉⊂⊆∪∩→←⇒⇔≈≠±∞ℝℕℤℚℂαβγδεθλμπσφωΔΣΩ∂∇′∀∃∧∨⊥∥∘…") + [" ", "x", "2"]
N = 8
SIZE = 16
ROW = 24
PER_COLUMN = 16


def main() -> None:
    slides = slides_service()
    pres = execute(slides.presentations().create(body={"title": "b2s probe symbols"}))
    pid = pres["presentationId"]
    page = pres["slides"][0]["objectId"]
    reqs = [{"deleteObject": {"objectId": e["objectId"]}} for e in pres["slides"][0].get("pageElements", [])]
    rows = [""] + SYMBOLS  # the first row is the reference
    boxes = []
    for i, sym in enumerate(rows):
        oid = f"sym_{i}"
        x, y = 10 + (i // PER_COLUMN) * 178, 5 + (i % PER_COLUMN) * ROW
        reqs += [
            text_box(oid, page, x, y, 175, ROW),
            {"insertText": {"objectId": oid, "text": "|  " + sym * N + "  |"}},
            {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"}, "fields": "fontFamily,fontSize",
                                 "style": {"fontFamily": "Lato", "fontSize": pt(SIZE)}}},
        ]
        boxes.append((sym, x, y))
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    path = OUT / "probe_symbols.png"
    save_thumbnail(slides, pid, page, path)
    img = np.asarray(Image.open(path).convert("RGB")).mean(axis=2)
    k = img.shape[1] / 720

    def bars(x: float, y: float) -> float:
        crop = img[int((y + 8) * k):int((y + 22) * k), int(x * k):int((x + 175) * k)]
        cols = np.where((crop < 128).any(axis=0))[0]
        # the bars: the first and last ink columns (each bar is one or two columns wide)
        left = cols[0]
        while left + 1 in cols:
            left += 1
        right = cols[-1]
        while right - 1 in cols:
            right -= 1
        return (right - left) / k

    reference = bars(boxes[0][1], boxes[0][2])
    table = {}
    for sym, x, y in boxes[1:]:
        table[sym] = round((bars(x, y) - reference) / N / SIZE, 3)
    print("SYMBOL_ADVANCE_EM = {")
    for sym, adv in table.items():
        print(f"    {sym!r}: {adv},")
    print("}")
    print(f"https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()


