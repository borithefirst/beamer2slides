"""Probe: how far down does a preset bullet larger than its text push its line, and the lines after it?

emit caps every bullet at its item's text size (`emit_metrics.bullet_size_of`: "a larger bullet
would push the line down"), measured once only for a ▶ written as a .pptx character (+0.9 pt at
133%, +1.8 pt at 150%; docs/project-notes.md "Triangle bullets through the .pptx"). The cap leaves
bullets the PDF draws larger than their text's preset at the text's size: beamer's square
`itemize subitem` at the parent's size beside \\scriptsize words (0.74 em of ink against the square
preset's 0.45 em; real_dstalk-datascience-ta 12, 62% of the PDF's), its balls (0.55 em against the
disc's 0.41; real_defense-defense 31). Lifting the cap needs the push per bullet size for the
presets emit writes, so that it can be paid for in the item's spaceAbove.

One API text box per preset glyph (● ■) and ratio, holding three bulleted items "HH" in black Lato
18 pt; the middle item's bullet (blue) is written at ratio x 18 pt as emit writes it (paragraph
styled like the bullet, bullets created, the text styled in two requests). Each item's baseline
(the bottom of its H ink) and the bullet's ink are measured in the thumbnail
(out/probe_bullet_push.png) and printed against the box at ratio 1.0: how far the middle line and
the last line moved, in pt and per em of the text. Everything it creates is deleted.

Result (2026-10-04): a bullet larger than its text sets its own line's ascent - the middle line
drops +2.7 / +5.4 / +8.6 / +11.3 / +14.4 pt at 1.15 / 1.33 / 1.5 / 1.65 / 1.85x, the last line
0.175 / 0.375 / 0.575 / 0.8 / 1.0 em of the text, disc and square alike: no spaceAbove can pay
for that, and the cap stays.

Usage: python tools/probe_bullet_push.py
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.emit import PAD_X
from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.google_types import (BulletPreset, Presentation, SlidesOptionalColor, SlidesRequest, SlidesService,
                                        SlidesTextStyle, object_id, presentation_id)
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

OUT = Path(__file__).resolve().parents[1] / "out"
SIZE = 18.0
RATIOS = (1.0, 1.15, 1.33, 1.5, 1.65, 1.85)
BOX_W, BOX_H, PITCH_X, PITCH_Y = 110.0, 120.0, 118.0, 130.0
BLUE: SlidesOptionalColor = {"opaqueColor": {"rgbColor": {"blue": 1}}}
BLACK: SlidesOptionalColor = {"opaqueColor": {"rgbColor": {}}}


@dataclass(frozen=True, kw_only=True)
class Glyph:
    name: str
    preset: BulletPreset
    level: int
    """The nesting level showing the glyph (emit_metrics.BULLET_SHAPES); reached by tabs."""


GLYPHS = (Glyph(name="disc", preset="BULLET_DISC_CIRCLE_SQUARE", level=0),
          Glyph(name="square", preset="BULLET_DISC_CIRCLE_SQUARE", level=2))


@dataclass(frozen=True, kw_only=True)
class Box:
    oid: str
    glyph: Glyph
    ratio: float
    x: float
    y: float


def style(oid: str, s: SlidesTextStyle, a: int, b: int) -> SlidesRequest:
    return {"updateTextStyle": {"objectId": oid, "style": s, "fields": "fontFamily,fontSize,foregroundColor",
                                "textRange": {"type": "FIXED_RANGE", "startIndex": a, "endIndex": b}}}


def requests(box: Box, page: str) -> list[SlidesRequest]:
    """Three items "HH" at one level; the middle bullet `ratio` times the text's size. A level
    above 0 is reached by tabs under a dummy item at level 0, deleted after the bullets are made
    (createParagraphBullets counts levels from the shallowest paragraph of its range)."""
    oid, tabs = box.oid, "\t" * box.glyph.level
    dummy = "x\n" if box.glyph.level else ""
    items = [tabs + "HH"] * 3
    text = dummy + "\n".join(items)
    starts = [len(dummy) + sum(len(i) + 1 for i in items[:k]) for k in range(3)]
    bullet: SlidesTextStyle = {"fontFamily": "Lato", "fontSize": pt(SIZE), "foregroundColor": BLUE}
    big: SlidesTextStyle = {"fontFamily": "Lato", "fontSize": pt(SIZE * box.ratio), "foregroundColor": BLUE}
    words: SlidesTextStyle = {"fontFamily": "Lato", "fontSize": pt(SIZE), "foregroundColor": BLACK}
    reqs: list[SlidesRequest] = [text_box(oid, page, box.x, box.y, BOX_W, BOX_H),
                                 {"insertText": {"objectId": oid, "text": text}},
                                 style(oid, bullet, 0, len(text)),
                                 style(oid, big, starts[1], starts[1] + len(items[1])),
                                 {"createParagraphBullets": {"objectId": oid, "bulletPreset": box.glyph.preset,
                                                             "textRange": {"type": "ALL"}}}]
    if dummy:
        reqs.append({"deleteText": {"objectId": oid, "textRange": {"type": "FIXED_RANGE", "startIndex": 0,
                                                                   "endIndex": len(dummy)}}})
    # the tabs are gone once they made the levels: each item is "HH" now
    for k in range(3):
        at = 3 * k
        reqs += [style(oid, words, at, at + 1), style(oid, words, at + 1, at + 2)]
    reqs.append({"updateParagraphStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                          "fields": "indentFirstLine,indentStart",
                                          "style": {"indentFirstLine": pt(20), "indentStart": pt(45)}}})
    return reqs


def main() -> None:
    slides = slides_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe bullet push"}))
    pid = presentation_id(pres)
    try:
        measure(slides, pres, pid)
    finally:
        execute(drive_service(None).files().delete(fileId=pid))


def measure(slides: SlidesService, pres: Presentation, pid: str) -> None:
    first = pres.get("slides", [])[0]
    page = object_id(first)
    boxes = [Box(oid=f"probe_bp{g}_{r}", glyph=glyph, ratio=ratio, x=8 + r * PITCH_X, y=10 + g * PITCH_Y)
             for g, glyph in enumerate(GLYPHS) for r, ratio in enumerate(RATIOS)]
    reqs: list[SlidesRequest] = [{"deleteObject": {"objectId": object_id(e)}} for e in first.get("pageElements", [])]
    for box in boxes:
        reqs += requests(box, page)
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    png = OUT / "probe_bullet_push.png"
    save_thumbnail(slides, pid, page, png, None)
    im = np.asarray(Image.open(png).convert("RGB")).astype(int)
    k = im.shape[1] / 720
    blue = (im[..., 2] > 150) & (im[..., 0] < 120) & (im[..., 1] < 120)
    black = im.sum(axis=2) < 200
    control: dict[str, list[float]] = {}
    for box in boxes:
        x0, y0, x1, y1 = (round(v * k) for v in (box.x, box.y, box.x + BOX_W, box.y + BOX_H))
        rows = np.nonzero(black[y0:y1, x0 + round((PAD_X + 40) * k):x1].any(axis=1))[0]
        if len(rows) == 0:
            print(f"{box.glyph.name} {box.ratio:.2f}: no words found")
            continue
        runs = np.split(rows, np.nonzero(np.diff(rows) > 1)[0] + 1)
        bases = [(r.max() + 1) / k for r in runs]
        if len(bases) != 3:
            print(f"{box.glyph.name} {box.ratio:.2f}: {len(bases)} lines of words, not 3")
            continue
        control.setdefault(box.glyph.name, bases)
        ref = control[box.glyph.name]
        ys, _ = np.nonzero(blue[y0 + round((bases[0] + 2) * k):y0 + round((bases[1] + 2) * k), x0:x1])
        ink = (ys.max() - ys.min() + 1) / k if len(ys) else 0.0
        print(f"{box.glyph.name} {box.ratio:.2f}: bullet ink {ink / (SIZE * box.ratio):.3f} em of its size, "
              f"{ink / SIZE:.3f} em of the text; middle line {bases[1] - ref[1]:+.2f} pt, "
              f"last line {bases[2] - ref[2]:+.2f} pt ({(bases[2] - ref[2]) / SIZE:+.3f} em of the text)")
    print(f"thumbnail: {png}")


if __name__ == "__main__":
    main()
