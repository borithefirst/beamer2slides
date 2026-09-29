"""`preset_geometry`: rasterising `adopt_shapes.preset`'s default-adjustment silhouette and
comparing it with a foreign deck's thumbnail (`devtools/preset_survey.py`'s phase 1 survey).

Offline: hand-built numpy thumbnails (1 px per pt, so a shape's box in pt is its box in pixels)
and bare elements (`deck_records.record`) - only the fields `preset_geometry.match_score` itself reads, not a full
`presentations.get` answer (that pipeline is `deck_ir`'s and `deck_fills`' to test)."""

from collections.abc import Sequence

import numpy as np
from PIL import Image, ImageDraw

from beamer2slides.arrays import RGB, SignedRGB
from beamer2slides.deck_ir_types import TargetShape
from beamer2slides.devtools import preset_geometry as pg
from beamer2slides.json_types import JsonObject

from .deck_records import record

BLUE = "#1a2b3c"
NO_FRAME = None  # shape_el without a frame: deck_records' upright one


def canvas(w: int, h: int) -> RGB:
    """A white w by h page."""
    return np.full((h, w, 3), 255, dtype=np.uint8)


def thumbnail(a: RGB) -> SignedRGB:
    """A painted canvas as `deck_fills.load` hands a thumbnail to `match_score`."""
    return a.astype(np.int16)


def paint(a: RGB, pts: Sequence[tuple[float, float]], colour: str) -> None:
    """Paint a filled polygon (image pixel coordinates) onto `a` in place."""
    im = Image.fromarray(a)
    ImageDraw.Draw(im).polygon(pts, fill=(int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)))
    a[:] = np.asarray(im)


def parallelogram_pts(x0: float, y0: float, w: float, h: float, slant_share: float) -> list[tuple[float, float]]:
    """The four corners (image px) of a parallelogram in a w by h box at (x0, y0): `slant_share`
    of `min(w, h)` is `adopt_shapes.preset`'s PARALLELOGRAM `x` (0.25 is the default it draws)."""
    x = min(w, h) * slant_share
    return [(x0, y0 + h), (x0 + x, y0), (x0 + w, y0), (x0 + w - x, y0 + h)]


def shape_el(kind: str, x0: float, y0: float, w: float, h: float, fill: str | None,
             frame: JsonObject | None) -> TargetShape:
    d: JsonObject = {"kind": "shape", "shape_type": kind, "bbox": [x0, y0, x0 + w, y0 + h], "fill": fill}
    if frame is not None:
        d["frame"] = frame
    el = record(d)
    assert isinstance(el, TargetShape)
    return el


def rings_of(kind: str, w: float, h: float) -> list[pg.Ring]:
    rings = pg.default_rings(kind, w, h, None)
    assert rings is not None
    return rings


# ---------------------------------------------------------------- path_polygons / default_rings

def test_parallelogram_area_matches_default_slant() -> None:
    rings = rings_of("PARALLELOGRAM", 100.0, 50.0)
    mask = pg.rasterize(rings, 100, 50, 1.0)
    x = min(100, 50) * 0.25
    expected = (100 + (100 - 2 * x)) / 2 * 50
    assert abs(int(mask.sum()) - expected) / expected < 0.02


def test_rounded_rectangle_curves_are_flattened_and_area_is_close() -> None:
    w, h, scale = 100.0, 50.0, 2.0
    rings = rings_of("ROUND_RECTANGLE", w, h)
    assert len(rings) == 1 and len(rings[0]) > 4          # the corners are sampled, not 4 bare points
    mask = pg.rasterize(rings, int(w * scale), int(h * scale), scale)
    r = min(w, h) * 0.16667
    expected = (w * h - r * r * (4 - np.pi)) * scale * scale
    assert abs(int(mask.sum()) - expected) / expected < 0.02


def test_donut_is_an_even_odd_ring() -> None:
    w = h = 100.0
    rings = rings_of("DONUT", w, h)
    assert len(rings) == 2                                # outer and inner circle, toggled even-odd
    mask = pg.rasterize(rings, 100, 100, 1.0)
    r_out, r_in = 50.0, 50.0 - 100.0 * 0.25                # adj default 25000: a quarter of ss off the radius
    expected = np.pi * (r_out ** 2 - r_in ** 2)
    assert abs(int(mask.sum()) - expected) / expected < 0.03


def test_unknown_preset_has_no_default_rings() -> None:
    assert pg.default_rings("NOT_A_REAL_PRESET", 10.0, 10.0, None) is None


def test_stroke_only_preset_has_no_fill_silhouette() -> None:
    # a bracket's "s"-mode path draws no fill: nothing for the pixel comparison to use
    assert pg.default_rings("LEFT_BRACKET", 10.0, 40.0, None) is None


# ---------------------------------------------------------------- match_score

def test_match_score_agrees_when_thumbnail_draws_the_default_slant() -> None:
    a = canvas(120, 80)
    pts = parallelogram_pts(10, 10, 100, 60, 0.25)        # exactly adopt_shapes' own default
    paint(a, pts, BLUE)
    el = shape_el("PARALLELOGRAM", 10, 10, 100, 60, BLUE, NO_FRAME)
    score = pg.match_score(thumbnail(a), el, [], 1.0, False)
    assert score is not None
    assert score.iou > 0.9


def test_match_score_disagrees_on_a_thin_ribbon_like_ja_schedule() -> None:
    # ja-schedule's finding: what Google actually shows for a PARALLELOGRAM is a thin slanted
    # ribbon hugging one edge, not the wide fill our default adjustment (x = ss * 0.25) draws -
    # so most of what our polygon claims is bare page, and most of the real ribbon sits outside it.
    a = canvas(120, 80)
    x0, y0, w, h = 10, 10, 100, 60
    ribbon = [(x0 + w - 22, y0), (x0 + w, y0), (x0 + w - 6, y0 + h), (x0 + w - 28, y0 + h)]
    paint(a, ribbon, BLUE)
    el = shape_el("PARALLELOGRAM", x0, y0, w, h, BLUE, NO_FRAME)
    score = pg.match_score(thumbnail(a), el, [], 1.0, False)
    assert score is not None
    assert score.iou < 0.5
    assert score.p_out > 0.3                           # our wide default paints over bare page


def test_match_score_none_without_a_fill() -> None:
    a = canvas(120, 80)
    el = shape_el("PARALLELOGRAM", 10, 10, 100, 60, None, NO_FRAME)
    assert pg.match_score(thumbnail(a), el, [], 1.0, False) is None


def test_match_score_none_for_non_adjustable_preset() -> None:
    a = canvas(120, 80)
    el = shape_el("RECTANGLE", 10, 10, 100, 60, BLUE, NO_FRAME)
    assert pg.match_score(thumbnail(a), el, [], 1.0, False) is None


def test_match_score_none_when_rotated() -> None:
    a = canvas(120, 80)
    pts = parallelogram_pts(10, 10, 100, 60, 0.25)
    paint(a, pts, BLUE)
    el = shape_el("PARALLELOGRAM", 10, 10, 100, 60, BLUE, {"rotation": 30.0})
    assert pg.match_score(thumbnail(a), el, [], 1.0, False) is None


def test_match_score_none_when_mostly_covered_above() -> None:
    a = canvas(120, 80)
    pts = parallelogram_pts(10, 10, 100, 60, 0.25)
    paint(a, pts, BLUE)
    el = shape_el("PARALLELOGRAM", 10, 10, 100, 60, BLUE, NO_FRAME)
    cover: JsonObject = {"kind": "shape", "bbox": [0, 0, 120, 80], "fill": "#ff0000", "fill_alpha": 1.0, "role": "panel"}
    assert pg.match_score(thumbnail(a), el, [record(cover)], 1.0, False) is None


def test_match_score_flip_mirrors_the_default_polygon() -> None:
    a = canvas(120, 80)
    x0, y0, w, h = 10, 10, 100, 60
    pts = parallelogram_pts(x0, y0, w, h, 0.25)          # the default slant, mirrored left to right
    mirrored_pts = [(2 * x0 + w - x, y) for x, y in pts]
    paint(a, mirrored_pts, BLUE)
    el = shape_el("PARALLELOGRAM", x0, y0, w, h, BLUE, {"flip": True})
    mirrored = pg.match_score(thumbnail(a), el, [], 1.0, False)
    upright = pg.match_score(thumbnail(a), shape_el("PARALLELOGRAM", x0, y0, w, h, BLUE, {}), [], 1.0, False)
    assert mirrored is not None and upright is not None
    assert mirrored.iou > 0.9
    assert upright.iou < mirrored.iou
