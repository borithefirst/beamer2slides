"""Header and footer words the layouts carry (`promote_theme_text`, a slide's `on_layout`) under a
figure's crop: Slides stacks the layout under every slide element, so an opaque crop hid them
where the PDF draws them over the figure's ground (real_africa-remote-sens-30 s2: a frame running
down to the page foot cut 'Институт Геог|рафии' off at its left edge). The crop is see-through
where the figure paints nothing (`render.crop_ground`, `clear_in_place`)."""

from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.arrays import RGBA
from beamer2slides.json_types import JsonObject, as_str
from beamer2slides.render import render_backgrounds

from .test_hidden_text import one_page
from .test_pictures_hunt import num_list, raw_of

# A frame from below the title down to the page foot with a red square in it, and a footline
# drawn after it whose words run from left of the frame into it (PDF y up, page 400 x 200).
CONTENT = (b"0 0 0 RG 1 w 100 5 200 175 re S\n"
           b"1 0 0 rg 180 90 40 40 re f\n"
           b"BT 0 0 0 rg /F1 6 Tf 50 8 Td (Institute of Geography, Moscow, 30.09.2025) Tj ET\n")
FIGURE_BOX = [99.0, 19.0, 301.0, 196.0]  # top-down, as deck.json says it


def crop_of(tmp_path: Path, on_layout: bool) -> tuple[RGBA, list[float]]:
    path = tmp_path / "page.pdf"
    path.write_bytes(one_page(CONTENT))
    raw = raw_of(path)
    footline = [s["id"] for s in raw["pages"][0]["spans"] if s["origin"][1] > 185]
    assert footline  # the case: the footline's words were read
    picture: JsonObject = {"id": "f0", "kind": "image", "role": "figure", "bbox": list(FIGURE_BOX), "spans": []}
    deck: JsonObject = {"slides": [{"page": 0, "size": [400, 200], "elements": [picture],
                                    "on_layout": list(footline) if on_layout else []}]}
    render_backgrounds(path, raw, deck, tmp_path / "out", frozenset())
    with Image.open(tmp_path / "out" / as_str(picture["file"], "file")) as im:
        px: RGBA = np.asarray(im.convert("RGBA"), dtype=np.uint8)
    return px, num_list(picture["bbox"])


def at(crop: RGBA, box: list[float], x: float, y: float) -> list[int]:
    """The crop's RGBA pixel at page point (x, y), top-down."""
    zoom = crop.shape[1] / (box[2] - box[0])
    return [int(v) for v in crop[int((y - box[1]) * zoom), int((x - box[0]) * zoom)]]


def region(crop: RGBA, box: list[float], x0: float, y0: float, x1: float, y1: float) -> RGBA:
    """The crop's pixels over the page box (x0, y0, x1, y1), top-down."""
    zoom = crop.shape[1] / (box[2] - box[0])
    return crop[int((y0 - box[1]) * zoom):int((y1 - box[1]) * zoom), int((x0 - box[0]) * zoom):int((x1 - box[0]) * zoom)]


def test_layout_words_under_a_figure_show_through_its_crop(tmp_path: Path) -> None:
    crop, box = crop_of(tmp_path, True)
    assert (region(crop, box, 105, 188, 165, 193)[..., 3] == 0).all()  # over the footline's words: see-through
    assert at(crop, box, 130, 60)[3] == 0      # the frame's empty inside too (the same ground)


def test_the_frame_and_what_it_holds_stay_opaque(tmp_path: Path) -> None:
    crop, box = crop_of(tmp_path, True)
    assert at(crop, box, 200, 100) == [255, 0, 0, 255]   # the red square
    left = region(crop, box, 99.5, 21, 100.5, 194)
    assert (left[..., 3] == 255).any(axis=1).all()   # the frame's left rule, all the way down, over the words too


def test_a_figure_clear_of_layout_words_keeps_its_opaque_crop(tmp_path: Path) -> None:
    crop, _ = crop_of(tmp_path, False)
    assert (crop[..., 3] == 255).all()
