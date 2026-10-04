"""Probe: does Slides break a line between a letter and a script run, or before an opening bracket?

monodromy s17 item 5: "... and π[γ]([x])" with [γ] a subscript came out with π ending the line
and [γ]([x]) opening the next, though UAX #14 LB30 allows no break between a letter and "["
(`emit_widths.first_break` assumed none). This probe writes "and π" + tail in black Lato 16 pt,
the tail per variant:

    sub-bracket     [γ] subscript, then ([x])          (what emit writes)
    sub-letter      y subscript, then ([x])
    plain-bracket   [γ] on the baseline, then ([x])
    paren           ([x]) right after π
    sup-letter      2 superscript, then ([x])
    wj-sub-bracket  sub-bracket with a WORD JOINER (U+2060) after π
    wj-paren        paren with a WORD JOINER after π
    bom-sub-bracket sub-bracket with a ZERO WIDTH NO-BREAK SPACE (U+FEFF) after π

into boxes of a sweep of widths, and measures in the thumbnail (out/probe_script_break.png) where
each box's first line ends. Per variant and width it prints that edge and whether π stayed on the
first line without its tail (a break inside the word). Everything it creates is deleted.

Usage: python tools/probe_script_break.py
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image

from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.google_types import Presentation, SlidesRequest, SlidesService, SlidesTextStyle, object_id, presentation_id
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

OUT = Path(__file__).resolve().parents[1] / "out"
SIZE = 16.0
HEAD = "and π"
WIDTHS = (50.0, 56.0, 62.0, 69.0, 75.0, 82.0, 88.0)
BOX_H, ROW = 46.0, 48.0


@dataclass(frozen=True, kw_only=True)
class Variant:
    name: str
    join: str  # between π and the script: nothing, or a character asking for no break
    script: str  # the text right after π, possibly a script
    offset: Literal["NONE", "SUBSCRIPT", "SUPERSCRIPT"]  # its baselineOffset
    tail: str  # the rest of the word


VARIANTS = [Variant(name="sub-bracket", join="", script="[γ]", offset="SUBSCRIPT", tail="([x])"),
            Variant(name="sub-letter", join="", script="y", offset="SUBSCRIPT", tail="([x])"),
            Variant(name="plain-bracket", join="", script="[γ]", offset="NONE", tail="([x])"),
            Variant(name="paren", join="", script="", offset="NONE", tail="([x])"),
            Variant(name="sup-letter", join="", script="2", offset="SUPERSCRIPT", tail="([x])"),
            Variant(name="wj-sub-bracket", join="⁠", script="[γ]", offset="SUBSCRIPT", tail="([x])"),
            Variant(name="wj-paren", join="⁠", script="", offset="NONE", tail="([x])"),
            Variant(name="bom-sub-bracket", join="﻿", script="[γ]", offset="SUBSCRIPT", tail="([x])")]


def box_at(v: int, w: int) -> tuple[float, float]:
    return 8 + sum(WIDTHS[:w]) + 6.0 * w, 8 + v * ROW


def requests(oid: str, page: str, x: float, y: float, width: float, v: Variant) -> list[SlidesRequest]:
    text = HEAD + v.join + v.script + v.tail
    style: SlidesTextStyle = {"fontFamily": "Lato", "fontSize": pt(SIZE),
                              "foregroundColor": {"opaqueColor": {"rgbColor": {}}}}
    reqs: list[SlidesRequest] = [
        text_box(oid, page, x, y, width, BOX_H), {"insertText": {"objectId": oid, "text": text}},
        {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"}, "style": style,
                             "fields": "fontFamily,fontSize,foregroundColor"}}]
    if v.script and v.offset != "NONE":
        a = len(HEAD) + len(v.join)
        reqs.append({"updateTextStyle": {
            "objectId": oid, "style": {"baselineOffset": v.offset}, "fields": "baselineOffset",
            "textRange": {"type": "FIXED_RANGE", "startIndex": a, "endIndex": a + len(v.script)}}})
    return reqs


def first_line_end(dark: np.ndarray, x: float, y: float, w: float, k: float) -> tuple[float, int]:
    """(right edge of the first line's ink in pt from the box's left, how many lines of ink)."""
    x0, y0, x1, y1 = (round(c * k) for c in (x, y, x + w + 5, y + BOX_H))
    band = dark[y0:y1, x0:x1]
    rows = band.any(axis=1)
    lines: list[tuple[int, int]] = []
    start = None
    for r, on in enumerate(rows):
        if on and start is None:
            start = r
        if (not on or r + 1 == len(rows)) and start is not None:
            if r - start >= round(4 * k) or lines:  # (a speck above the first line is no line)
                lines.append((start, r))
            start = None
    if not lines:
        return 0.0, 0
    a, b = lines[0]
    cols = np.nonzero(band[a:b].any(axis=0))[0]
    return float(cols.max() + 1) / k, len(lines)


def main() -> None:
    slides = slides_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe script break"}))
    pid = presentation_id(pres)
    try:
        measure(slides, pres, pid)
    finally:
        execute(drive_service(None).files().delete(fileId=pid))


def measure(slides: SlidesService, pres: Presentation, pid: str) -> None:
    first = pres.get("slides", [])[0]
    page = object_id(first)
    reqs: list[SlidesRequest] = [{"deleteObject": {"objectId": object_id(e)}} for e in first.get("pageElements", [])]
    for i, v in enumerate(VARIANTS):
        for j, width in enumerate(WIDTHS):
            x, y = box_at(i, j)
            reqs += requests(f"probe_sb{i}_{j}", page, x, y, width, v)
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    png = OUT / "probe_script_break.png"
    save_thumbnail(slides, pid, page, png, None)
    im = np.asarray(Image.open(png).convert("RGB")).astype(int)
    k = im.shape[1] / 720
    dark = im.sum(axis=2) < 300
    print(f"Lato {SIZE:g} pt; per box width (pt, insets 7.2 each side): first line's ink end / lines")
    print(f"{'':14}" + "".join(f"{w:>11g}" for w in WIDTHS))
    for i, v in enumerate(VARIANTS):
        cells = []
        for j, width in enumerate(WIDTHS):
            x, y = box_at(i, j)
            end, n = first_line_end(dark, x, y, width, k)
            cells.append(f"{end:7.1f}/{n}  ")
        print(f"{v.name:14}" + "".join(f"{c:>11}" for c in cells))
    print(f"thumbnail: {png}")


if __name__ == "__main__":
    main()
