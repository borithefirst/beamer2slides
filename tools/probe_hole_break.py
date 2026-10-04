"""Probe: what lets Slides end a line between a word and the formula hole after it?

A hole is no-break spaces in Roboto Mono (emit_text.hole_runs_of). Slides keeps a word space and
the no-break spaces after it together, so the word before a hole went down with it whenever the
PDF broke there ('of' / √γ(t), real_beamer-monodromy s3). This probe writes "value of" + SEP +
HOLE + " zz" in black Lato 16 pt into boxes of a sweep of widths, SEP and HOLE per variant:

    space         " ", five no-break spaces                     (what emit wrote until 2026-10-04)
    space-zwsp    " " + ZERO WIDTH SPACE                        (emit.HOLE_BREAK in r10: no break)
    ensp          an EN SPACE (UAX #14 BA) in place of the space
    space-ls      " " + LINE SEPARATOR (U+2028)                 (emit.HOLE_BREAK now)
    space-ps      " " + PARAGRAPH SEPARATOR (U+2029)
    space-nel     " " + NEXT LINE (U+0085)
    emsp-hole     " ", the hole five EM SPACEs                  (breakable, but it hangs at a line end)

and measures in the thumbnail (out/probe_hole_break.png) each box's first line's ink end, its
second line's ink start and its line count. Measured 2026-10-04: only U+2028 and U+2029 let
"value of" stay with the hole opening the next line at its full width, and at 190 pt the line is
as wide as with the plain space (130.0 pt): no room taken, no forced break. A ZWSP, an en, thin
or punctuation space, NEL, or a doubled space change nothing; a hole of em or ideographic spaces
breaks but hangs at the line end, the hole lost. Slides reads U+2028 back as itself.
Everything it creates is deleted.

Usage: python tools/probe_hole_break.py
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.google_types import Presentation, SlidesRequest, SlidesService, SlidesTextStyle, object_id, presentation_id
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

OUT = Path(__file__).resolve().parents[1] / "out"
SIZE = 16.0
HEAD, TAIL = "value of", " zz"
NB = "\xa0" * 5
WIDTHS = (90.0, 100.0, 110.0, 190.0)
BOX_H, ROW, GAP = 54.0, 56.0, 70.0


@dataclass(frozen=True, kw_only=True)
class Variant:
    name: str
    sep: str   # between 'of' and the hole, in Lato
    hole: str  # in Roboto Mono


VARIANTS = [Variant(name="space", sep=" ", hole=NB),
            Variant(name="space-zwsp", sep=" ​", hole=NB),
            Variant(name="ensp", sep=" ", hole=NB),
            Variant(name="space-ls", sep="  ", hole=NB),
            Variant(name="space-ps", sep="  ", hole=NB),
            Variant(name="space-nel", sep=" \u0085", hole=NB),
            Variant(name="emsp-hole", sep=" ", hole=" " * 5)]


def box_at(v: int, w: int) -> tuple[float, float]:
    return 8 + sum(WIDTHS[:w]) + GAP * w, 8 + v * ROW


def requests(oid: str, page: str, x: float, y: float, width: float, v: Variant) -> list[SlidesRequest]:
    style: SlidesTextStyle = {"fontFamily": "Lato", "fontSize": pt(SIZE),
                              "foregroundColor": {"opaqueColor": {"rgbColor": {}}}}
    a = len(HEAD) + len(v.sep)
    return [text_box(oid, page, x, y, width, BOX_H), {"insertText": {"objectId": oid, "text": HEAD + v.sep + v.hole + TAIL}},
            {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"}, "style": style,
                                 "fields": "fontFamily,fontSize,foregroundColor"}},
            {"updateTextStyle": {"objectId": oid, "style": {"fontFamily": "Roboto Mono"}, "fields": "fontFamily",
                                 "textRange": {"type": "FIXED_RANGE", "startIndex": a, "endIndex": a + len(v.hole)}}}]


def lines_of(dark: np.ndarray, x: float, y: float, w: float, k: float) -> list[tuple[float, float]]:
    """(ink start, ink end) of each line of ink in the box, pt from the box's left."""
    x0, y0, x1, y1 = (round(c * k) for c in (x, y, x + w + 5, y + BOX_H))
    band = dark[y0:y1, x0:x1]
    rows = band.any(axis=1)
    spans: list[tuple[int, int]] = []
    start = None
    for r, on in enumerate(rows):
        if on and start is None:
            start = r
        if (not on or r + 1 == len(rows)) and start is not None:
            if r - start >= round(4 * k) or spans:  # (a speck above the first line is no line)
                spans.append((start, r))
            start = None
    out: list[tuple[float, float]] = []
    for a, b in spans:
        cols = np.nonzero(band[a:b].any(axis=0))[0]
        out.append((float(cols.min()) / k, float(cols.max() + 1) / k))
    return out


def main() -> None:
    slides = slides_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe hole break"}))
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
            reqs += requests(f"probe_hb{i}_{j}", page, x, y, width, v)
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    png = OUT / "probe_hole_break.png"
    save_thumbnail(slides, pid, page, png, None)
    im = np.asarray(Image.open(png).convert("RGB")).astype(int)
    k = im.shape[1] / 720
    dark = im.sum(axis=2) < 300
    print(f"Lato {SIZE:g} pt; per box width (pt, insets 7.2 each side): line 1 end | line 2 start / lines")
    print(f"{'':12}" + "".join(f"{w:>16g}" for w in WIDTHS))
    for i, v in enumerate(VARIANTS):
        cells: list[str] = []
        for j, width in enumerate(WIDTHS):
            x, y = box_at(i, j)
            ls = lines_of(dark, x, y, width, k)
            cell = f"{ls[0][1]:.1f}" if ls else "-"
            if len(ls) > 1:
                cell += f"|{ls[1][0]:.1f}"
            cells.append(f"{cell}/{len(ls)}")
        print(f"{v.name:12}" + "".join(f"{c:>16}" for c in cells))
    print(f"thumbnail: {png}")


if __name__ == "__main__":
    main()
