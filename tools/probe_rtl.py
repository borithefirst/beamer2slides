"""Probe: does Slides take a paragraph's `direction`, and what do START and END mean then?

`emit` writes `direction: RIGHT_TO_LEFT` on a Hebrew paragraph (classify.Paragraph.direction)
and mirrors its alignment, because the API's START and END are the reading direction's own
ends, not the page's left and right. Both halves of that are Google's word, so they are asked
here: three boxes of the same Hebrew word at the same place, one told which way it reads and
one not, plus a Latin one, and a fourth pair for END. The read-back says whether the field
survives (a request Google refuses throws out the whole batch, which is how a Hebrew deck
would end up as pictures), the thumbnail says which edge the ink sits on.

Usage: python tools/probe_rtl.py
"""

from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.google_auth import slides_service
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

OUT = Path(__file__).resolve().parents[1] / "out"
WORD = "בסיפור"  # the word the whole bidi story was measured on
BOX_W, BOX_H = 300, 40
# (name, text, alignment, direction)
CASES = [
    ("rtl-start", WORD, "START", "RIGHT_TO_LEFT"),
    ("rtl-end", WORD, "END", "RIGHT_TO_LEFT"),
    ("hebrew-untold", WORD, "START", None),
    ("latin-start", "besipur", "START", None),
]


def main() -> None:
    slides = slides_service()
    pres = execute(slides.presentations().create(body={"title": "b2s probe rtl"}))
    pid = pres["presentationId"]
    page = pres["slides"][0]["objectId"]
    reqs = [{"deleteObject": {"objectId": e["objectId"]}} for e in pres["slides"][0].get("pageElements", [])]
    boxes = []
    for i, (name, text, alignment, direction) in enumerate(CASES):
        oid = f"rtl_{i}"
        x, y = 40, 20 + i * (BOX_H + 20)
        style = {"alignment": alignment, "lineSpacing": 100, "spaceAbove": pt(0), "spaceBelow": pt(0),
                 "indentStart": pt(0), "indentFirstLine": pt(0)}
        fields = "alignment,lineSpacing,spaceAbove,spaceBelow,indentStart,indentFirstLine"
        if direction:
            style["direction"], fields = direction, fields + ",direction"
        reqs += [
            text_box(oid, page, x, y, BOX_W, BOX_H),
            {"insertText": {"objectId": oid, "text": text}},
            {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"}, "fields": "fontFamily,fontSize",
                                 "style": {"fontFamily": "Arial", "fontSize": pt(24)}}},
            {"updateParagraphStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                      "fields": fields, "style": style}},
        ]
        boxes.append((name, oid, x, y))
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))

    live = execute(slides.presentations().get(presentationId=pid))
    read = {}
    for el in live["slides"][0]["pageElements"]:
        para = el["shape"]["text"]["textElements"][0].get("paragraphMarker", {}).get("style", {})
        read[el["objectId"]] = {k: para.get(k) for k in ("alignment", "direction")}

    path = OUT / "probe_rtl.png"
    save_thumbnail(slides, pid, page, path)
    img = np.asarray(Image.open(path).convert("RGB")).mean(axis=2)
    k = img.shape[1] / 720
    print("case           read back                              ink in the box (pt from its left)")
    for name, oid, x, y in boxes:
        crop = img[int(y * k):int((y + BOX_H) * k), int(x * k):int((x + BOX_W) * k)]
        cols = np.where((crop < 128).any(axis=0))[0]
        where = f"{cols[0] / k:6.1f} - {cols[-1] / k:6.1f}" if len(cols) else "no ink"
        print(f"{name:<14} {str(read[oid]):<38} {where}   (box 0 - {BOX_W})")
    print(f"https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()
