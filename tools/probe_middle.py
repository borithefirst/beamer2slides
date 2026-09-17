"""Probe: first baseline of a single-line text box with contentAlignment MIDDLE, as a
function of box height and font size (Lato). Prints baseline - box middle per size, from
the ink bottom of capital H's in the thumbnail.

Usage: python tools/probe_middle.py
"""

from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.google_auth import slides_service
from beamer2slides.gslides import EMU_PER_PT, execute, pt, save_thumbnail, text_box

OUT = Path(__file__).resolve().parents[1] / "out"
SIZES = [10, 16, 22, 30, 40]
HEIGHTS = [30, 60, 90]


def main() -> None:
    slides = slides_service()
    pres = execute(slides.presentations().create(body={"title": "b2s probe middle"}))
    pid = pres["presentationId"]
    page = pres["slides"][0]["objectId"]
    reqs = [{"deleteObject": {"objectId": e["objectId"]}} for e in pres["slides"][0].get("pageElements", [])]
    boxes = []
    for i, z in enumerate(SIZES):
        for j, h in enumerate(HEIGHTS):
            oid = f"mid_{i}_{j}"
            x, y = 20 + j * 230, 10 + i * 78
            h_eff = min(h, 76)
            reqs.append(text_box(oid, page, x, y, 200, h_eff))
            reqs += [
                {"insertText": {"objectId": oid, "text": "HHHH"}},
                {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"}, "fields": "fontFamily,fontSize",
                                     "style": {"fontFamily": "Lato", "fontSize": pt(z)}}},
                {"updateShapeProperties": {"objectId": oid, "fields": "contentAlignment",
                                           "shapeProperties": {"contentAlignment": "MIDDLE"}}},
                {"updateParagraphStyle": {"objectId": oid, "textRange": {"type": "ALL"}, "fields": "lineSpacing,spaceAbove,spaceBelow",
                                          "style": {"lineSpacing": 100, "spaceAbove": pt(0), "spaceBelow": pt(0)}}},
            ]
            boxes.append((z, x, y, h_eff))
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    path = OUT / "probe_middle.png"
    save_thumbnail(slides, pid, page, path)
    img = np.asarray(Image.open(path).convert("RGB")).mean(axis=2)
    k = img.shape[1] / 720
    print("size height  baseline-middle  (in em)")
    for z, x, y, h in boxes:
        crop = img[int(y * k):int((y + h) * k), int((x + 5) * k):int((x + 150) * k)]
        rows = np.where((crop < 128).any(axis=1))[0]
        if not len(rows):
            print(z, h, "no ink")
            continue
        base = y + (rows[-1] + 1) / k
        top_ink = y + rows[0] / k
        d = base - (y + h / 2)
        print(f"{z:4} {h:6} {d:8.2f} {d / z:7.3f}   cap {base - top_ink:.2f}")
    print(f"https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()
