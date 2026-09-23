"""Probe: advance widths (em) of text characters in Slides, per substitute font and style.

emit sizes table columns so that no cell wraps in Slides: a cell that was one line in the PDF
and wraps in Slides doubles its row, and the table grows over whatever sits under it (a
caption). The calibrated size factors make a *sentence* as wide as the PDF's; a number is not
a sentence - Lato's digits are tabular at 0.58 em where Computer Modern's are 0.5 - so the
column needs the characters' own widths. Slides' Lato is not the Lato 2.015 on google/fonts
(its space is 0.19 em there, 0.256 here; its slash 0.313 against 0.452), so the widths are
measured on Google's renderer, not read out of a font file.

Each row is "|  " + ch * N + "  |" in one font and style; the distance between the two bars,
minus the same row without the character, is N advances. One slide per font and style.

Usage: python tools/probe_advances.py   (writes src/beamer2slides/calibration/advances.json)
"""

import json
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_advances"
TABLE = ROOT / "src" / "beamer2slides" / "calibration" / "advances.json"
CHARS = [chr(c) for c in range(32, 127)] + list("éèàüöäçñÉ–—’“”°µ€£")
STYLES = {"regular": (False, False), "bold": (True, False), "italic": (False, True), "bold_italic": (True, True)}
FONTS = ["Lato", "PT Serif"]
N = 8
SIZE = 12
ROW = 17
PER_COLUMN = 23
COLUMN_W = 143


def main() -> None:
    slides = slides_service()
    pres = execute(slides.presentations().create(body={"title": "b2s probe advances"}))
    pid = pres["presentationId"]
    first = pres["slides"][0]["objectId"]
    reqs = [{"deleteObject": {"objectId": e["objectId"]}} for e in pres["slides"][0].get("pageElements", [])]
    jobs = [(font, style) for font in FONTS for style in STYLES]
    pages = [first] + [f"page_{i}" for i in range(1, len(jobs))]
    reqs += [{"createSlide": {"objectId": p}} for p in pages[1:]]
    rows = [""] + CHARS  # the first row is the reference
    boxes = {}
    for j, ((font, style), page) in enumerate(zip(jobs, pages)):
        bold, italic = STYLES[style]
        for i, ch in enumerate(rows):
            oid = f"adv_{j}_{i}"
            x, y = 4 + (i // PER_COLUMN) * COLUMN_W, 2 + (i % PER_COLUMN) * ROW
            reqs += [
                text_box(oid, page, x, y, COLUMN_W - 2, ROW + 6),
                {"insertText": {"objectId": oid, "text": "|  " + ch * N + "  |"}},
                {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                     "fields": "fontFamily,fontSize,bold,italic,foregroundColor",
                                     "style": {"fontFamily": font, "fontSize": pt(SIZE), "bold": bold, "italic": italic,
                                               "foregroundColor": {"opaqueColor": {"rgbColor": {}}}}}},
            ]
            boxes.setdefault((font, style), []).append((ch, x, y))
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))

    table: dict = {}
    for (font, style), page in zip(jobs, pages):
        path = OUT / f"{font.replace(' ', '')}-{style}.png"
        save_thumbnail(slides, pid, page, path)
        img = np.asarray(Image.open(path).convert("RGB")).mean(axis=2)
        k = img.shape[1] / 720

        def bars(x: float, y: float) -> float:
            # the line's ink: from above cap height to below the descender, inside this box only
            crop = img[int((y + 7) * k):int((y + 22) * k), int(x * k):int((x + COLUMN_W - 4) * k)]
            cols = np.where((crop < 128).any(axis=0))[0]
            left = cols[0]
            while left + 1 in cols:
                left += 1
            right = cols[-1]
            while right - 1 in cols:
                right -= 1
            return (right - left) / k

        rows_here = boxes[(font, style)]
        reference = bars(rows_here[0][1], rows_here[0][2])
        table.setdefault(font, {})[style] = {ch: round((bars(x, y) - reference) / N / SIZE, 3)
                                             for ch, x, y in rows_here[1:]}
        print(font, style, {c: table[font][style][c] for c in "0,. aHm/"})
    TABLE.write_text(json.dumps({
        "source": "tools/probe_advances.py: advance widths (em) on Google Slides' renderer",
        "fonts": table}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("wrote", TABLE)
    execute(drive_service().files().delete(fileId=pid))


if __name__ == "__main__":
    main()
