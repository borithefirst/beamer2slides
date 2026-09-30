"""A theme's border around the page (a parchment's double rule inset from the edges, drawn in the
background canvas): background, as the page's own ground is. Read as a graphic it made one figure
region of the whole page, every line a label of a figure too big to crop, and each slide one
picture; drawn as lines, its top and bottom rules framed the words as a table. And a background
canvas's picture or shading stays whole under what render takes out of the page: cut, it left a
white box under a native table and behind a picture, and those boxes, differing slide to slide,
were cut out of the layouts' decoration."""

import zlib
from pathlib import Path

import numpy as np
import pytest

from beamer2slides import pdf
from beamer2slides.classify import classify
from beamer2slides.extract import extract_page
from beamer2slides.ir import Deck, deck_json
from beamer2slides.raw_types import RawDoc
from beamer2slides.render import load_png, render_backgrounds

from .test_hidden_text import one_page, text

W, H = 400, 200
PARCHMENT = [(240, 228, 200), (232, 216, 180), (222, 204, 164), (212, 192, 150)]
"""The canvas: four bands across the page, 100 pt each (not flat: a flat page is painted back
around a picture, `render.paint_out_leftovers`)."""
RECTANGLES = b"0.3 0.2 0.1 RG 1.6 w 5 5 390 190 re S 0.5 w 8.5 8.5 383 183 re S\n"
LINES = b"".join(b"0.3 0.2 0.1 RG %g w %g %g m %g %g l S\n" % (w, *a, *b)
                 for w, i in ((1.6, 5.0), (0.5, 8.5))
                 for a, b in (((i, i), (W - i, i)), ((W - i, i), (W - i, H - i)),
                              ((W - i, H - i), (i, H - i)), ((i, H - i), (i, i))))
WORDS = (text(30, 165, b"Coral reef surveys", 16) + text(30, 130, b"Reefs grow slowly in warm and clear water", 11)
         + text(30, 112, b"Bleaching follows a heatwave by weeks", 11)
         + text(30, 70, b"North", 11) + text(110, 70, b"41%", 11) + text(30, 55, b"South", 11) + text(110, 55, b"23%", 11))
DRAWING = b"0 0 0.6 RG 1 w 260 90 m 260 30 l 340 30 l S 262 35 m 285 95 305 0 336 80 c S\n"  # a plot: axes, a curve


def canvas() -> bytes:
    data = zlib.compress(b"".join(bytes(c) for c in PARCHMENT))
    return b"<< /Type /XObject /Subtype /Image /Width 4 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8 " \
           b"/Filter /FlateDecode /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def page(border: bytes, tmp_path: Path) -> tuple[Path, RawDoc, Deck]:
    content = b"q %d 0 0 %d 0 0 cm /Im0 Do Q\n" % (W, H) + border + WORDS + DRAWING
    path = tmp_path / "framed.pdf"
    path.write_bytes(one_page(content, {"Im0": canvas()}, W, H))
    doc = pdf.Document(path)
    try:
        p = extract_page(doc[0], "1")
    finally:
        doc.close()
    p["frame_label"] = None
    raw: RawDoc = {"version": 1, "source": {"pdf": str(path), "producer": "", "pages": 1, "title": ""}, "pages": [p]}
    return path, raw, classify(raw)


@pytest.mark.parametrize("border", [RECTANGLES, LINES], ids=["rectangles", "lines"])
def test_a_border_around_the_page_leaves_the_slide_as_it_was(border: bytes, tmp_path: Path) -> None:
    _, _, bare = page(b"", tmp_path)
    _, raw, framed = page(border, tmp_path)
    [slide], [plain] = framed["slides"], bare["slides"]

    def elements(s: object) -> list[tuple[str, list[int]]]:
        assert isinstance(s, dict)
        return [(e["kind"], [round(v) for v in e["bbox"]]) for e in s["elements"]]
    assert framed["stats"]["native_share"] == bare["stats"]["native_share"] == 1.0
    assert elements(slide) == elements(plain) and [k for k, _ in elements(slide)].count("image") == 1
    assert slide["figure_regions"] == plain["figure_regions"]
    border_ids = {d["id"] for d in raw["pages"][0]["drawings"] if d["stroke"] == "#4d331a"}
    assert len(border_ids) == (2 if border == RECTANGLES else 8)
    assert not border_ids & {i for e in slide["elements"] if e["kind"] == "image" for i in e.get("drawings", [])}


def test_the_page_background_stays_whole_under_a_picture(tmp_path: Path) -> None:
    path, raw, deck = page(b"", tmp_path)
    rendered = deck_json(deck)
    [png] = render_backgrounds(path, raw, rendered, tmp_path / "bg", frozenset())
    bg = load_png(png)
    z = bg.shape[1] / W
    [figure] = [e for e in deck["slides"][0]["elements"] if e["kind"] == "image"]
    x0, y0, x1, y1 = (round(v * z) for v in figure["bbox"])
    expected = np.array([PARCHMENT[min(3, int(x / z) // 100)] for x in range(x0, x1)])
    # (the lines are gone with the picture; what is left of its box is the canvas, not white)
    assert (np.abs(bg[y0:y1, x0:x1].astype(int) - expected).max(axis=2) <= 2).mean() > 0.99
