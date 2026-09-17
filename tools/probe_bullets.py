"""Probe: preset bullet glyphs in Slides - their ink height, position and colour control.

Findings (emit.BULLET_SHAPES, docs/calibration.md):
- A bullet keeps the style its paragraph had when createParagraphBullets ran, until a later
  updateTextStyle covers the whole paragraph in one request. Styling the text in two requests
  keeps a bullet in its own colour and size.
- createParagraphBullets sets nesting levels relative to the shallowest paragraph of its range.
- A preset's own glyphs are those of levels 0-2; levels 3-8 repeat ● ○ ■ for every preset.
- A bullet larger than its text pushes the line down; one no larger does not move it.

Creates one text box per glyph with the text "HH" in black Lato 24 pt and the bullet in blue
at 24 and 12 pt, then measures the blue ink against the H baseline and indentFirstLine in the
thumbnail (out/probe_bullets.png).

Usage: python tools/probe_bullets.py
"""

from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.emit import PAD_X
from beamer2slides.google_auth import slides_service
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

OUT = Path(__file__).resolve().parents[1] / "out"
GLYPHS = [("●", "BULLET_DISC_CIRCLE_SQUARE", 0), ("○", "BULLET_DISC_CIRCLE_SQUARE", 1), ("■", "BULLET_DISC_CIRCLE_SQUARE", 2),
          ("➢", "BULLET_ARROW3D_CIRCLE_SQUARE", 0), ("★", "BULLET_STAR_CIRCLE_SQUARE", 0),
          ("◆", "BULLET_DIAMOND_CIRCLE_SQUARE", 0), ("◇", "BULLET_DIAMONDX_HOLLOWDIAMOND_SQUARE", 1),
          ("❏", "BULLET_CHECKBOX", 0)]
SIZES, TEXT, FIRST, START, PITCH = (24, 12), 24, 60, 90, 44
BLUE = {"opaqueColor": {"rgbColor": {"blue": 1}}}
BLACK = {"opaqueColor": {"rgbColor": {}}}


def fixed(s: int, e: int) -> dict:
    return {"type": "FIXED_RANGE", "startIndex": s, "endIndex": e}


def main() -> None:
    slides = slides_service()
    pres = execute(slides.presentations().create(body={"title": "b2s probe bullets"}))
    pid, page = pres["presentationId"], pres["slides"][0]["objectId"]
    reqs = [{"deleteObject": {"objectId": e["objectId"]}} for e in pres["slides"][0].get("pageElements", [])]
    boxes = []
    for c, size in enumerate(SIZES):
        for r, (_, preset, level) in enumerate(GLYPHS):
            oid, x, y = f"probe_g{c}_{r}", 10 + c * 300, 8 + r * PITCH
            text = "x\n" + "\t" * level + "HH"  # the dummy "x" paragraph makes the level absolute
            reqs += [text_box(oid, page, x, y, 250, 40), {"insertText": {"objectId": oid, "text": text}},
                     {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"}, "fields": "fontFamily,fontSize,foregroundColor",
                                          "style": {"fontFamily": "Lato", "fontSize": pt(size), "foregroundColor": BLUE}}},
                     {"createParagraphBullets": {"objectId": oid, "bulletPreset": preset, "textRange": fixed(0, len(text))}},
                     {"deleteText": {"objectId": oid, "textRange": fixed(0, 2)}}]
            reqs += [{"updateTextStyle": {"objectId": oid, "textRange": fixed(k, k + 1), "fields": "fontSize,foregroundColor",
                                          "style": {"fontSize": pt(TEXT), "foregroundColor": BLACK}}} for k in (0, 1)]
            reqs.append({"updateParagraphStyle": {"objectId": oid, "textRange": fixed(0, 2), "fields": "indentFirstLine,indentStart",
                                                  "style": {"indentFirstLine": pt(FIRST), "indentStart": pt(START)}}})
            boxes.append((size, GLYPHS[r][0], x, y))
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    png = OUT / "probe_bullets.png"
    save_thumbnail(slides, pid, page, png)
    im = np.asarray(Image.open(png).convert("RGB")).astype(int)
    k = im.shape[1] / 720
    blue = (im[..., 2] > 150) & (im[..., 0] < 120) & (im[..., 1] < 120)
    black = im.sum(axis=2) < 200
    for size, glyph, x, y in boxes:
        x0, y0, x1, y1 = (round(v * k) for v in (x, y, x + 290, y + PITCH))
        ys, xs = np.nonzero(blue[y0:y1, x0:x1])
        base = (np.nonzero(black[y0:y1, x0:x1])[0].max() + 1) / k
        top, bottom, right = ys.min() / k - base, (ys.max() + 1) / k - base, (xs.max() + 1) / k - PAD_X - FIRST
        print(f"{glyph} {size:>2} pt: ink height {(bottom - top) / size:.3f} em, bottom {-bottom / size:+.3f} em above "
              f"the baseline, gap to indentFirstLine {-right / size:.3f} em")
    print(f"https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()
