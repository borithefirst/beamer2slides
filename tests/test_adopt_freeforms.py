"""Freeform shapes traced from the slide's thumbnail (deck_freeforms, through deck_ir(foreign=True,
thumbnails=...)): the API says nothing of a freeform's geometry, so its outline is traced in the
picture and written as a filled TikZ path. Offline: hand-made `presentations.get` answers and numpy
thumbnails 720 px wide, so a thumbnail pixel is a Slides point."""

import numpy as np
import pytest

from beamer2slides import adopt, adopt_shapes, deck_freeforms
from beamer2slides.inverse import Context

from .test_adopt_fills import UNREAD, deck, elements, page, pt, shape, solid

NONE = {"propertyState": "NOT_RENDERED"}


@pytest.fixture(autouse=True)
def lengths_as_written(monkeypatch):
    monkeypatch.setattr(adopt, "to_bp", lambda text: text)


def outlined(pe: dict, hexc: str, weight: float) -> dict:
    pe["shape"]["shapeProperties"]["outline"] = {"outlineFill": solid(hexc), "weight": pt(weight),
                                                 "propertyState": "RENDERED"}
    return pe


def paint(a: np.ndarray, mask: np.ndarray, hexc: str) -> np.ndarray:
    a[mask] = [int(hexc[i:i + 2], 16) for i in (1, 3, 5)]
    return a


def disc(cx, cy, r) -> np.ndarray:
    y, x = np.mgrid[0:405, 0:720] + 0.5
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def ring_area(rings) -> float:
    """In Slides pt² (the IR draws the 720 pt deck on a 453.54 pt beamer page)."""
    areas = sorted((abs(deck_freeforms.area(r)) for r in rings), reverse=True)
    return (areas[0] - sum(areas[1:])) / K ** 2              # one outline and the holes in it


K = 453.54 / 720


def test_a_solid_freeform_is_traced_as_its_outline_not_its_box():
    thumb = paint(page(), disc(140, 140, 40), "#3366cc")
    [el] = elements(deck(shape("a", "CUSTOM", 100, 100, 80, 80, solid("3366CC"))), thumb)
    tr = el["trace"]
    assert len(tr["rings"]) == 1 and tr["fill"].lower() == "#3366cc" and tr["source"] == "thumbnail"
    assert abs(ring_area(tr["rings"]) - np.pi * 40 ** 2) < 0.03 * np.pi * 40 ** 2
    assert len(tr["rings"][0]) < 80                         # simplified, not one point per pixel
    out = adopt_shapes.shape_block(el, Context(), "")
    assert "even odd rule" in out and "rectangle (80" not in out and "cycle" in out
    assert "_traced" not in el


def test_without_thumbnails_nothing_is_traced():
    [el] = elements(deck(shape("a", "CUSTOM", 100, 100, 80, 80, solid("3366CC"))))
    assert "trace" not in el


def test_a_squiggle_the_api_reads_as_empty_gets_its_colour_and_its_outline():
    y, x = np.mgrid[0:405, 0:720] + 0.5
    band = (np.abs((y - 100) - (x - 100) * 0.5) < 8) & (x >= 100) & (x < 300)
    [el] = elements(deck(shape("a", None, 100, 92, 200, 116, UNREAD)), paint(page(), band, "#fac2bd"))
    assert el["fill"] == "#fac2bd" and el["fill_source"] == "thumbnail"
    assert el["trace"]["fill"] == "#fac2bd"
    assert ring_area(el["trace"]["rings"]) < 0.25 * 200 * 116      # a band, not the box


def test_a_freeform_that_fills_its_box_stays_a_rectangle():
    [el] = elements(deck(shape("a", "CUSTOM", 100, 100, 80, 60, solid("3366CC"))),
                    page((100, 100, 80, 60, "#3366cc")))
    assert "trace" not in el


def test_what_an_opaque_shape_above_hides_is_the_freeforms():
    """A square over the middle of the disc leaves no hole in it."""
    thumb = paint(page(), disc(140, 140, 40), "#3366cc")
    thumb[130:150, 130:150] = [255, 170, 0]
    els = elements(deck(shape("a", "CUSTOM", 100, 100, 80, 80, solid("3366CC")),
                        shape("b", "RECTANGLE", 130, 130, 20, 20, solid("FFAA00"))), thumb)
    assert len(els[0]["trace"]["rings"]) == 1


def test_a_hole_showing_the_page_is_the_shapes_own_and_one_showing_art_is_not():
    donut = disc(140, 140, 40) & ~disc(140, 140, 15)
    [el] = elements(deck(shape("a", "CUSTOM", 100, 100, 80, 80, solid("3366CC"))),
                    paint(page(), donut, "#3366cc"))
    assert len(el["trace"]["rings"]) == 2
    # the same hole showing ink no element of the deck accounts for: something above it the API did
    # not tell (Canva's art on sc-memphis' panel), so the disc goes on under it
    thumb = paint(paint(page(), donut, "#3366cc"), disc(140, 140, 15), "#ffa49c")
    [el] = elements(deck(shape("a", "CUSTOM", 100, 100, 80, 80, solid("3366CC"))), thumb)
    assert len(el["trace"]["rings"]) == 1


def test_a_hole_under_a_shape_nobody_could_read_is_not_the_freeforms():
    """A `{}` shape above that shows two colours (Canva art) has no fill anyone can say and is dropped;
    what shows in its box, page colour included, may be its own."""
    thumb = paint(page(), disc(140, 140, 40), "#3366cc")
    thumb[125:155, 125:140] = [255, 255, 255]
    thumb[125:155, 140:155] = [0, 170, 0]
    els = elements(deck(shape("a", "CUSTOM", 100, 100, 80, 80, solid("3366CC")),
                        shape("art", "CUSTOM", 125, 125, 30, 30, UNREAD)), thumb)
    assert [e["id"] for e in els] == ["a"] and len(els[0]["trace"]["rings"]) == 1


def test_ink_of_the_same_colour_running_on_outside_the_box_is_a_neighbours():
    """A tile over a band of its own colour that crosses the whole slide: the band is not the tile."""
    [el] = elements(deck(shape("a", "CUSTOM", 100, 100, 100, 100, solid("FAC2BD"))),
                    page((0, 130, 720, 40, "#fac2bd")))
    assert "trace" not in el


def test_an_outline_only_ring_is_traced_as_the_stroke():
    ring = disc(140, 140, 41) & ~disc(140, 140, 38)
    pe = outlined(shape("a", "CUSTOM", 101, 101, 78, 78, NONE), "C8253C", 3)
    [el] = elements(deck(pe), paint(page(), ring, "#c8253c"))
    tr = el["trace"]
    assert tr["fill"].lower() == "#c8253c" and len(tr["rings"]) == 2
    assert abs(ring_area(tr["rings"]) - np.pi * (41 ** 2 - 38 ** 2)) < 0.25 * np.pi * (41 ** 2 - 38 ** 2)


def test_a_traced_path_keeps_its_points_below_the_top_edge():
    """adopt_shapes.pt once wrote -0.67 as 0.67: a thin traced stroke along the box top came out as a
    sliver above it."""
    assert adopt_shapes.pt(-0.67) == "-0.67" and adopt_shapes.pt(-0.001) == "0" and adopt_shapes.pt(10) == "10"
    assert adopt_shapes.P(1, 0.5) == "(1pt,-0.5pt)"


def test_an_outline_in_another_colour_is_drawn_inside_the_traced_edge():
    el = {"kind": "shape", "bbox": [10, 10, 30, 30], "trace": {
        "rings": [[[10, 10], [30, 10], [30, 30], [10, 30]]], "fill": "#ffffff", "alpha": None,
        "stroke": "#000000", "weight": 1.0, "source": "thumbnail"}}
    out = adopt_shapes.traced_block(el, Context(), "")
    assert "\\clip" in out and "line width=2.00pt" in out and "draw=black" in out
