"""A picture's crop reaches the ink of every glyph the background loses to it (render.reach_held_ink).

Glyph boxes run from origin to advance: a slanted (italic) glyph's ink reaches left of its
origin. real_beamer-monodromy s11: the `q` beside a tikz-cd arrow had its centre inside the
figure's box, so it left the background, while the crop of the box cut its left side off."""

from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides import pdf
from beamer2slides.arrays import Mask
from beamer2slides.json_types import JsonObject
from beamer2slides.render import render_backgrounds

from .test_hidden_text import one_page
from .test_pictures_hunt import num_list, raw_of

ZOOM = 4.0
ARROW = b"0 0 0 RG 1 w 110 60 m 110 130 l S 110 60 m 170 60 l S\n"


def slanted_h(x: float) -> bytes:
    """An H at 20 pt slanted left at the top (its ink reaches ~6.7 pt left of its origin)."""
    return b"BT /F1 20 Tf 1 0 -0.6 1 %.2f 100 Tm (H) Tj ET\n" % x


def dark(path: Path) -> Mask:
    doc = pdf.Document(path)
    try:
        img = doc[0].render(ZOOM, None, False)
    finally:
        doc.close()
    return img.max(axis=2) < 128


def ink_left(path: Path) -> float:
    xs = np.nonzero(dark(path).any(axis=0))[0]
    return float(xs.min()) / ZOOM


def crop_after_render(tmp_path: Path, x: float, owned: bool) -> tuple[list[float], float, Mask]:
    """The figure's box after render, the H's ink left edge, and the dark pixels the background
    keeps left of the box the figure was given."""
    alone = tmp_path / "h.pdf"
    alone.write_bytes(one_page(slanted_h(x)))
    path = tmp_path / "figure.pdf"
    path.write_bytes(one_page(ARROW + slanted_h(x)))
    raw = raw_of(path)
    page = raw["pages"][0]
    (label,) = page["spans"]
    given = [100.0, 30.0, 175.0, 145.0]
    bx = label["bbox"]
    assert given[0] < (bx[0] + bx[2]) / 2 and ink_left(alone) < given[0] - 1  # the case: ink past the box's edge
    if owned:
        given[0] = min(given[0], bx[0])
        assert ink_left(alone) < given[0] - 1
    figure: JsonObject = {"id": "f0", "kind": "image", "role": "figure", "bbox": list(given),
                          "spans": [label["id"]] if owned else []}
    deck: JsonObject = {"slides": [{"page": 0, "size": [400, 200], "elements": [figure], "on_layout": []}]}
    [png] = render_backgrounds(path, raw, deck, tmp_path / "out", frozenset())
    bg = np.array(dark_png(png))
    left = bg[:, : int(given[0] * bg.shape[1] / 400)]
    return num_list(figure["bbox"]), ink_left(alone), left


def dark_png(png: Path) -> Mask:
    return np.array(Image.open(png).convert("L")) < 128


def test_a_label_the_figure_owns_is_cropped_whole(tmp_path: Path) -> None:
    box, ink, _ = crop_after_render(tmp_path, 104.0, True)
    assert box[0] <= ink - 0.5


def test_a_glyph_whose_centre_the_box_holds_is_cropped_whole(tmp_path: Path) -> None:
    """Not the figure's own label: its centre in the box takes it from the background all the same."""
    box, ink, left = crop_after_render(tmp_path, 92.0, False)
    assert box[0] <= ink - 0.5
    assert not left.any()  # (it left the background: the crop alone shows it)
