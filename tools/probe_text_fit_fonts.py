"""Probe: what tools/text_fit.py found about the substitute fonts that the calibration had not
measured (docs/calibration.md, "Small caps, em spaces, scripts").

1. `smallCaps`: how large Slides draws a small capital, relative to the capital of the same
   letter (advance of a lowercase letter under smallCaps / advance of its capital), in PT Serif
   and Lato, and the ink height of both, which says whether it is a scaled capital.
2. Em spaces (U+2003, what classify writes for a \\quad) and the other spaces, in Roboto Mono,
   Lato and PT Serif: a monospaced font may draw every space at its one advance.
3. `baselineOffset`: the size Slides sets a SUBSCRIPT or SUPERSCRIPT at (its advance) and how far
   it lowers or raises it (ink of H as its own, subscript and superscript, at 40 and 20 pt).

Advances as in tools/probe_advances.py: each row is "|  " + ch * N + "  |" (a sentence once), and
the distance between the two bars less the same row without the character, in the same font, is
N advances.

Usage: python tools/probe_text_fit_fonts.py
(prints a JSON summary, also written with the thumbnails to out/probe_text_fit_fonts)
"""

import json
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_text_fit_fonts"
N = 8
SIZE = 16
ROW = 30
PER_COLUMN = 13
COLUMN_W = 176
SENTENCE = "Workstation"  # short enough not to wrap in its row

SC = {"smallCaps": True}
SUB = {"baselineOffset": "SUBSCRIPT"}
SUP = {"baselineOffset": "SUPERSCRIPT"}
SPACES = (" ", " ", " ", " ", " ")
# (label, font, text, extra style): a single character is repeated N times, a longer text once
ADVANCE_CASES = [
    *[(f"{font} {ch}", font, ch, {}) for font in ("PT Serif", "Lato") for ch in "MAWma"],
    *[(f"{font} {ch} smallCaps", font, ch, SC) for font in ("PT Serif", "Lato") for ch in "mawMA"],
    *[(f"{font} sentence{' smallCaps' if extra else ''}", font, SENTENCE, extra)
      for font in ("PT Serif", "Lato") for extra in ({}, SC)],
    *[(f"{font} U+{ord(ch):04X}", font, ch, {}) for font in ("Roboto Mono", "Lato", "PT Serif") for ch in SPACES],
    ("Roboto Mono 0", "Roboto Mono", "0", {}),
    ("Roboto Mono a", "Roboto Mono", "a", {}),
    *[(f"Lato 0 {name}", "Lato", "0", extra) for name, extra in (("none", {}), ("sub", SUB), ("sup", SUP))],
    *[(f"Lato x {name}", "Lato", "x", extra) for name, extra in (("none", {}), ("sub", SUB), ("sup", SUP))],
]
VERTICAL_CASES = [  # (label, text, font, size, extra): ink of each, in one-line boxes of the same top
    ("Lato H 40", "HH", "Lato", 40, {}),
    ("Lato H 40 sub", "HH", "Lato", 40, SUB),
    ("Lato H 40 sup", "HH", "Lato", 40, SUP),
    ("Lato H 20", "HH", "Lato", 20, {}),
    ("Lato H 20 sub", "HH", "Lato", 20, SUB),
    ("Lato H 20 sup", "HH", "Lato", 20, SUP),
    ("PT Serif M 40", "MM", "PT Serif", 40, {}),
    ("PT Serif m 40 smallCaps", "mm", "PT Serif", 40, SC),
    ("Lato M 40", "MM", "Lato", 40, {}),
    ("Lato m 40 smallCaps", "mm", "Lato", 40, SC),
]
VBOX_W, VBOX_H = 170, 70
# A line's first baseline says which size Slides lays it out at (6.48 + 0.968 em below the box
# top): a Lato 20 line holding a smallCaps run of PT Serif 26 - lowercase only, with a space in
# it, with a capital in it - against the same run without smallCaps.
LINE_CASES = [  # (label, [(text, font, size, extra), ...])
    ("plain 20", [("Hx mm x", "Lato", 20, {})]),
    ("big run 26", [("Hx ", "Lato", 20, {}), ("mm", "PT Serif", 26, {}), (" x", "Lato", 20, {})]),
    ("smallCaps 26 lowercase", [("Hx ", "Lato", 20, {}), ("mm", "PT Serif", 26, SC), (" x", "Lato", 20, {})]),
    ("smallCaps 26 with space", [("Hx ", "Lato", 20, {}), ("mm mm", "PT Serif", 26, SC), (" x", "Lato", 20, {})]),
    ("smallCaps 26 with capital", [("Hx ", "Lato", 20, {}), ("Mm", "PT Serif", 26, SC), (" x", "Lato", 20, {})]),
    ("smallCaps 34 lowercase", [("Hx ", "Lato", 20, {}), ("mm", "PT Serif", 34, SC), (" x", "Lato", 20, {})]),
    ("smallCaps 34 with space", [("Hx ", "Lato", 20, {}), ("mm mm", "PT Serif", 34, SC), (" x", "Lato", 20, {})]),
]
LBOX_W, LBOX_H = 230, 80


def reps(text: str) -> int:
    return N if len(text) == 1 else 1


def style_request(oid: str, font: str, size: float, extra: dict, start: int | None = None, end: int | None = None) -> dict:
    style = {"fontFamily": font, "fontSize": pt(size), "foregroundColor": {"opaqueColor": {"rgbColor": {}}}, **extra}
    rng = {"type": "ALL"} if start is None else {"type": "FIXED_RANGE", "startIndex": start, "endIndex": end}
    return {"updateTextStyle": {"objectId": oid, "textRange": rng, "fields": ",".join(style), "style": style}}


def main() -> None:
    slides = slides_service()
    pres = execute(slides.presentations().create(body={"title": "b2s probe text fit fonts"}))
    try:
        run(slides, pres)
    finally:
        execute(drive_service().files().delete(fileId=pres["presentationId"]))


def run(slides, pres: dict) -> None:
    pid = pres["presentationId"]
    first = pres["slides"][0]["objectId"]
    reqs = [{"deleteObject": {"objectId": e["objectId"]}} for e in pres["slides"][0].get("pageElements", [])]
    per_page = PER_COLUMN * (720 // COLUMN_W)
    fonts = list(dict.fromkeys(font for _, font, _, _ in ADVANCE_CASES))
    # the bars' own spaces are in the row's font: one reference row per font
    rows = [(f"reference {font}", font, "", {}) for font in fonts] + ADVANCE_CASES
    n_pages = -(-len(rows) // per_page)
    pages = [first] + [f"page_{i}" for i in range(1, n_pages + 1)]  # the last one holds the vertical cases
    reqs += [{"createSlide": {"objectId": p}} for p in pages[1:]]
    placed = []
    for i, (label, font, ch, extra) in enumerate(rows):
        page, k = pages[i // per_page], i % per_page
        x, y = 4 + (k // PER_COLUMN) * COLUMN_W, 2 + (k % PER_COLUMN) * ROW
        oid = f"adv_{i}"
        body = ch * reps(ch) if ch else ""
        reqs += [text_box(oid, page, x, y, COLUMN_W - 2, ROW + 6),
                 {"insertText": {"objectId": oid, "text": "|  " + body + "  |"}},
                 style_request(oid, font, SIZE, {})]
        if extra and body:
            reqs.append(style_request(oid, font, SIZE, extra, 3, 3 + len(body)))
        placed.append((label, font, reps(ch) if ch else 1, page, x, y))
    vpage = pages[-1]
    vplaced = []
    per_row = 720 // VBOX_W
    for i, (label, text, font, size, extra) in enumerate(VERTICAL_CASES):
        oid = f"ver_{i}"
        x, y = 4 + (i % per_row) * VBOX_W, 20 + (i // per_row) * (VBOX_H + 20)
        reqs += [text_box(oid, vpage, x, y, VBOX_W - 4, VBOX_H), {"insertText": {"objectId": oid, "text": text}},
                 style_request(oid, font, size, extra)]
        vplaced.append((label, x, y))
    lpage = "page_lines"
    pages.append(lpage)
    reqs.append({"createSlide": {"objectId": lpage}})
    lplaced = []
    for i, (label, segments) in enumerate(LINE_CASES):
        oid = f"line_{i}"
        x, y = 4 + (i % 3) * (LBOX_W + 4), 10 + (i // 3) * (LBOX_H + 10)
        reqs += [text_box(oid, lpage, x, y, LBOX_W, LBOX_H),
                 {"insertText": {"objectId": oid, "text": "".join(s[0] for s in segments)}}]
        start = 0
        for text, font, size, extra in segments:
            reqs.append(style_request(oid, font, size, extra, start, start + len(text)))
            start += len(text)
        lplaced.append((label, x, y))
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))

    images = {}
    for page in pages:
        path = OUT / f"{page}.png"
        save_thumbnail(slides, pid, page, path)
        images[page] = np.asarray(Image.open(path).convert("RGB")).mean(axis=2)
    k = images[first].shape[1] / 720

    def bars(img, x: float, y: float) -> float:
        crop = img[int((y + 4) * k):int((y + ROW) * k), int(x * k):int((x + COLUMN_W - 4) * k)]
        cols = np.where((crop < 128).any(axis=0))[0]
        left = cols[0]
        while left + 1 in cols:
            left += 1
        right = cols[-1]
        while right - 1 in cols:
            right -= 1
        return (right - left) / k

    reference = {font: bars(images[page], x, y) for _, font, _, page, x, y in placed[:len(fonts)]}
    result = {"advance_em": {}, "vertical_pt": {}}
    for label, font, count, page, x, y in placed[len(fonts):]:
        result["advance_em"][label] = round((bars(images[page], x, y) - reference[font]) / count / SIZE, 4)
    img = images[vpage]
    for label, x, y in vplaced:
        crop = img[int(y * k):int((y + VBOX_H) * k), int(x * k):int((x + VBOX_W - 4) * k)]
        ys = np.where((crop < 128).any(axis=1))[0]
        xs = np.where((crop < 128).any(axis=0))[0]
        result["vertical_pt"][label] = {"top": round(ys[0] / k, 2), "bottom": round((ys[-1] + 1) / k, 2),
                                        "height": round((ys[-1] + 1 - ys[0]) / k, 2),
                                        "width": round((xs[-1] + 1 - xs[0]) / k, 2)}
    img = images[lpage]
    result["line_size_pt"] = {}
    for label, x, y in lplaced:
        crop = img[int(y * k):int((y + LBOX_H) * k), int(x * k):int((x + 20) * k)]  # the H alone
        ys = np.where((crop < 128).any(axis=1))[0]
        baseline = (ys[-1] + 1) / k
        result["line_size_pt"][label] = round((baseline - 6.48) / 0.968, 2)
    print(json.dumps(result, indent=1, ensure_ascii=False))
    (OUT / "result.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
