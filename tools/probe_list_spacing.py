"""Probe: does Slides keep a list item's spaceAbove when the item says spacingMode NEVER_COLLAPSE?

emit writes the gap between two list items as the lower item's spaceAbove with spacingMode
`emit_text.LIST_SPACING` (NEVER_COLLAPSE) on it, and each item's lineSpacing from its own lines'
pitch (`emit_text.vertical_layout_of`). Slides' own boxes say COLLAPSE_LISTS, under which spaceAbove
and spaceBelow between list items are dropped (docs/calibration.md). This probe makes one API text
box per variant, each a bulleted item that wraps to two lines and a one-line item after it, in
black Lato 20 pt:

    collapse      nothing written (Slides' own spacing)
    above         item 2 spaceAbove 12 pt, mode left as Slides has it
    never-2       item 2 spaceAbove 12 pt and NEVER_COLLAPSE on item 2        (what emit writes)
    never-1       item 2 spaceAbove 12 pt and NEVER_COLLAPSE on item 1 only
    never-2-r130  as never-2, item 1 lineSpacing 130                         (emit's ratio + gap)
    never-early   as never-2, the paragraph style written before createParagraphBullets

then measures each line's baseline (the bottom of its H ink) in the thumbnail
(out/probe_list_spacing.png) and prints, per variant, the spacingMode Slides reads back, the
measured inner pitch of item 1 and its last line to item 2's step, against what emit's model
(`emit.line_pitch`, `emit.pitch_between`) predicts with and without the gap. One thumbnail pixel
is 0.45 pt.

Usage: python tools/probe_list_spacing.py
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.emit import line_pitch, pitch_between
from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.google_types import (Presentation, SlidesParagraphStyle, SlidesRange, SlidesRequest, SlidesService,
                                        object_id, part, presentation_id)
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box
from beamer2slides.json_types import as_objects, as_str

OUT = Path(__file__).resolve().parents[1] / "out"
SIZE, GAP, BOX_W, BOX_H = 20.0, 12.0, 330.0, 125.0
ITEM1, ITEM2 = "HHH " * 8 + "HHH", "HHH HHH"
TEXT = ITEM1 + "\n" + ITEM2


@dataclass(frozen=True, kw_only=True)
class Variant:
    name: str
    ratio: float  # item 1's lineSpacing / 100
    gap: float  # item 2's spaceAbove
    never: tuple[int, ...]  # the items (0, 1) written NEVER_COLLAPSE
    early: bool  # the paragraph style written before createParagraphBullets


VARIANTS = [Variant(name="collapse", ratio=1.0, gap=0.0, never=(), early=False),
            Variant(name="above", ratio=1.0, gap=GAP, never=(), early=False),
            Variant(name="never-2", ratio=1.0, gap=GAP, never=(1,), early=False),
            Variant(name="never-1", ratio=1.0, gap=GAP, never=(0,), early=False),
            Variant(name="never-2-r130", ratio=1.3, gap=GAP, never=(1,), early=False),
            Variant(name="never-early", ratio=1.0, gap=GAP, never=(1,), early=True)]


def fixed(s: int, e: int) -> SlidesRange:
    return {"type": "FIXED_RANGE", "startIndex": s, "endIndex": e}


def styles(oid: str, v: Variant) -> list[SlidesRequest]:
    reqs: list[SlidesRequest] = []
    ranges = [fixed(0, len(ITEM1) + 1), fixed(len(ITEM1) + 1, len(TEXT))]
    for k, at in enumerate(ranges):
        style: SlidesParagraphStyle = {"lineSpacing": round(100 * (v.ratio if k == 0 else 1.0), 1)}
        fields = "lineSpacing"
        if k == 1 and v.gap:
            style["spaceAbove"] = pt(v.gap)
            fields += ",spaceAbove"
        if k in v.never:
            style["spacingMode"] = "NEVER_COLLAPSE"
            fields += ",spacingMode"
        reqs.append({"updateParagraphStyle": {"objectId": oid, "textRange": at, "style": style, "fields": fields}})
    return reqs


def box_at(n: int) -> tuple[float, float]:
    return 10 + (n % 2) * 355, 8 + (n // 2) * 132


def baselines(black: np.ndarray, x: float, y: float, k: float) -> list[float]:
    """Bottoms of the rows of ink right of the bullets, in pt from the box's top."""
    x0, y0, x1, y1 = (round(v * k) for v in (x + 45, y, x + BOX_W, y + BOX_H))
    rows = black[y0:y1, x0:x1].any(axis=1)
    out: list[float] = []
    for r in range(len(rows)):
        if rows[r] and (r + 1 == len(rows) or not rows[r + 1]):
            out.append((r + 1) / k)
    return out


def main() -> None:
    slides = slides_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe list spacing"}))
    pid = presentation_id(pres)
    try:
        measure(slides, pres, pid)
    finally:
        execute(drive_service(None).files().delete(fileId=pid))


def measure(slides: SlidesService, pres: Presentation, pid: str) -> None:
    """The six boxes written, read back and measured on the thumbnail."""
    first = pres.get("slides", [])[0]
    page = object_id(first)
    reqs: list[SlidesRequest] = [{"deleteObject": {"objectId": object_id(e)}} for e in first.get("pageElements", [])]
    for n, v in enumerate(VARIANTS):
        oid = f"probe_ls{n}"
        x, y = box_at(n)
        made: list[SlidesRequest] = [
            text_box(oid, page, x, y, BOX_W, BOX_H), {"insertText": {"objectId": oid, "text": TEXT}},
            {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"}, "fields": "fontFamily,fontSize",
                                 "style": {"fontFamily": "Lato", "fontSize": pt(SIZE)}}}]
        reqs += made
        bullets: SlidesRequest = {"createParagraphBullets": {"objectId": oid, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                                                             "textRange": fixed(0, len(TEXT))}}
        reqs += [*styles(oid, v), bullets] if v.early else [bullets, *styles(oid, v)]
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    read = execute(slides.presentations().get(presentationId=pid))
    modes: dict[str, list[str]] = {}
    for el in read.get("slides", [])[0].get("pageElements", []):
        shape = el.get("shape")
        if shape is None:
            continue
        elements = as_objects(part(shape.get("text"), "text").get("textElements", []), "textElements")
        modes[object_id(el)] = [as_str(part(part(te.get("paragraphMarker"), "paragraphMarker").get("style"), "style")
                                       .get("spacingMode", "-"), "spacingMode")
                                for te in elements if "paragraphMarker" in te]
    png = OUT / "probe_list_spacing.png"
    save_thumbnail(slides, pid, page, png, None)
    im = np.asarray(Image.open(png).convert("RGB")).astype(int)
    k = im.shape[1] / 720
    black = im.sum(axis=2) < 300
    print(f"Lato {SIZE:g} pt, gap {GAP:g} pt; inner = item 1's two lines, step = item 1's last line to item 2")
    for n, v in enumerate(VARIANTS):
        x, y = box_at(n)
        got = baselines(black, x, y, k)
        inner_model = line_pitch(SIZE, v.ratio, SIZE)
        dropped = pitch_between(SIZE, v.ratio, SIZE, 1.0, 0.0)
        kept = pitch_between(SIZE, v.ratio, SIZE, 1.0, v.gap)
        mode = modes.get(f"probe_ls{n}", [])
        if len(got) != 3:
            print(f"{v.name:13} modes {mode}: {len(got)} rows of ink found, 3 expected: {[round(b, 2) for b in got]}")
            continue
        print(f"{v.name:13} modes {mode}: inner {got[1] - got[0]:6.2f} (model {inner_model:6.2f})  "
              f"step {got[2] - got[1]:6.2f} (model, gap kept {kept:6.2f}, gap dropped {dropped:6.2f})")
    print(f"thumbnail: {png}")


if __name__ == "__main__":
    main()
