"""Freeform shapes traced from the slide's thumbnail (deck_freeforms, through deck_ir(foreign=True,
thumbnails=...)): the API says nothing of a freeform's geometry, so its outline is traced in the
picture and written as a filled TikZ path. Offline: hand-made `presentations.get` answers and numpy
thumbnails 720 px wide, so a thumbnail pixel is a Slides point."""

import numpy as np
import pytest

from beamer2slides import adopt, adopt_shapes, deck_fills, deck_freeforms
from beamer2slides.inverse import Context

from .test_adopt_fills import EMU, UNREAD, at, deck, elements, page, pt, shape, solid

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


def test_a_ring_of_one_noisy_colour_is_that_colour():
    """drawing-workshop 52: JPEG noise split the purple page around an icon over several 8-wide
    buckets, none holding half the ring, so `ring_colour` said nothing and the icon's hole was
    filled. The colour is taken where most of the ring lies within TOL of the busiest bucket."""
    rng = np.random.default_rng(0)
    a = np.full((60, 60, 3), (147, 11, 144), np.int16) + rng.integers(-5, 6, (60, 60, 3))
    a = np.clip(a, 0, 255).astype(np.uint8)
    got = deck_freeforms.ring_colour(a, (20, 20, 40, 40))
    assert got is not None and np.abs(np.asarray(got, float) - (147, 11, 144)).max() <= 2
    a[:, :33] = (0, 200, 0)                                 # mostly green: purple lost the ring
    assert np.abs(np.asarray(deck_freeforms.ring_colour(a, (20, 20, 40, 40)), float) - (0, 200, 0)).max() <= 2
    a[:28] = (230, 230, 230)                              # three colours: none has half of it
    assert deck_freeforms.ring_colour(a, (20, 20, 40, 40)) is None


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


def test_a_fill_the_page_already_shows_is_traced_by_its_outline_not_left_a_rectangle():
    """A white cloud on a near-white page (showcase's `water`, blob() with an #a8dadc outline): the
    fill gives marching squares no edge of its own, so without an outline to key on it used to fall
    back to the whole box (a visible rectangle). Traced from the ring instead, a disc - whose corners
    are page colour too - comes out a disc, not its 80 x 80 box."""
    ring = disc(140, 140, 41) & ~disc(140, 140, 38)
    pe = outlined(shape("a", "CUSTOM", 100, 100, 80, 80, solid("FFFFFF")), "A8DADC", 1.5)
    [el] = elements(deck(pe, bg="F7FBFC"), paint(page(bg="F7FBFC"), ring, "#a8dadc"))
    tr = el["trace"]
    assert tr["fill"].lower() == "#ffffff" and tr["stroke"].lower() == "#a8dadc"
    assert len(tr["rings"]) == 1
    assert abs(ring_area(tr["rings"]) - np.pi * 40 ** 2) < 0.25 * np.pi * 40 ** 2
    out = adopt_shapes.shape_block(el, Context(), "")
    assert "rectangle (80" not in out and "cycle" in out


def test_a_fill_the_page_shows_with_no_outline_still_falls_back_to_the_box():
    """No outline at all (the title slide's own cloud, drawn with `line=None`): there is nothing left
    to trace an edge from, so the shape is refused as before - a real limit, not a regression."""
    deck_freeforms.REFUSED.clear()
    [el] = elements(deck(shape("a", "CUSTOM", 100, 100, 80, 80, solid("FFFFFF")), bg="F7FBFC"),
                    page(bg="F7FBFC"))
    assert "trace" not in el


def test_shared_boxes_finds_freeform_siblings_at_one_box():
    """A PowerPoint SmartArt diagram's freeform pieces (a water-cycle's five curved arrows) all keep
    the diagram's own canvas box in the API's answer - the geometry that tells them apart is a path
    the API never gives. Two or more shapes of the same source group declaring the exact same box
    is that, not a coincidence; a lone shape, or shapes in different groups, is not a cluster."""
    els = [
        {"kind": "shape", "shape_type": "CUSTOM", "group": "g1", "bbox": [10, 10, 50, 50]},
        {"kind": "shape", "shape_type": "TEXT_BOX", "group": "g1", "bbox": [15, 15, 45, 25]},
        {"kind": "shape", "shape_type": "CUSTOM", "group": "g1", "bbox": [10, 10, 50, 50]},
        {"kind": "shape", "shape_type": "CUSTOM", "group": "g2", "bbox": [10, 10, 50, 50]},
        {"kind": "shape", "shape_type": "CUSTOM", "group": "g1", "bbox": [200, 200, 40, 40]},
    ]
    assert deck_freeforms.shared_boxes(els) == [[0, 2]]


def test_a_box_two_freeforms_share_is_baked_as_one_picture_not_two_rectangles(tmp_path):
    """en-smartart's water-cycle diagram: two (of the real deck's five) curved arrows report the
    identical box. Tracing each alone reads the other as an opaque shape covering that whole shared
    box (`deck_fills.opaque`), so both used to fall back to "the box, solid" - two identical, stacked,
    opaque rectangles hiding the label between them. Baked once instead, under the diagram's own
    picture behind its label, which stays native and keeps its words."""
    thumb = page()
    thumb[100:300, 100:300] = [191, 162, 42]                   # the two arrows' ink, painted as one
    thumb[120:140, 150:250] = [126, 225, 255]                  # a label's words, inside the shared box
    arrow_a = {"kind": "shape", "shape_type": "CUSTOM", "group": "dia", "bbox": [100, 100, 300, 300],
              "fill": "#2aa2bf", "fill_alpha": 1.0, "outline": None, "id": "a1", "object": "a1"}
    label = {"kind": "text", "role": "body", "group": "dia", "bbox": [140, 120, 260, 150], "id": "lbl",
             "object": "lbl", "paragraphs": [{"runs": [{"text": "Precipitacion", "color": "#7ee1ff"}]}]}
    arrow_b = {"kind": "shape", "shape_type": "CUSTOM", "group": "dia", "bbox": [100, 100, 300, 300],
              "fill": "#2aa2bf", "fill_alpha": 1.0, "outline": None, "id": "a2", "object": "a2"}
    got = deck_fills.settle([dict(arrow_a), dict(label), dict(arrow_b)], thumb, 1.0, "#ffffff", False, tmp_path)
    assert [e["kind"] for e in got] == ["image", "text"]
    assert got[0]["id"] == "dia~arrows" and got[0]["group"] == "dia" and got[0]["bbox"] == [100, 100, 300, 300]
    assert got[1]["id"] == "lbl" and got[1]["paragraphs"] == label["paragraphs"]   # untouched


def test_a_lone_freeform_that_fills_its_box_is_still_left_a_rectangle(tmp_path):
    """A single freeform at its own true box (no sibling sharing it) is not this cluster: the old
    "the box, solid" fallback still applies, and the new bake path leaves it alone."""
    el = {"kind": "shape", "shape_type": "CUSTOM", "group": "dia", "bbox": [100, 100, 300, 300],
          "fill": "#2aa2bf", "fill_alpha": 1.0, "outline": None, "id": "a1", "object": "a1"}
    thumb = page((100, 100, 200, 200, "2AA2BF"))
    got = deck_fills.settle([dict(el)], thumb, 1.0, "#ffffff", False, tmp_path)
    assert len(got) == 1 and got[0]["kind"] == "shape" and "trace" not in got[0]


def test_without_a_pictures_folder_a_shared_box_cluster_is_left_for_tracing():
    """Offline reads with no folder to write to (`pull`) never bake a picture: the cluster is left as
    it was, exactly as any other unread fill would be without one."""
    thumb = page()
    thumb[100:300, 100:300] = [191, 162, 42]
    arrow_a = {"kind": "shape", "shape_type": "CUSTOM", "group": "dia", "bbox": [100, 100, 300, 300],
              "fill": "#2aa2bf", "fill_alpha": 1.0, "outline": None, "id": "a1", "object": "a1"}
    arrow_b = {"kind": "shape", "shape_type": "CUSTOM", "group": "dia", "bbox": [100, 100, 300, 300],
              "fill": "#2aa2bf", "fill_alpha": 1.0, "outline": None, "id": "a2", "object": "a2"}
    got = deck_fills.settle([dict(arrow_a), dict(arrow_b)], thumb, 1.0, "#ffffff")
    assert [e["id"] for e in got] == ["a1", "a2"]


def curved_arrow() -> tuple[np.ndarray, np.ndarray]:
    """A quarter ring in the lower left of the box (100, 100)-(260, 260): it touches the box's left and
    bottom sides only, as a curved arrow drawn in a frame larger than itself does. (all, its inside
    less a 3 px rim)."""
    y, x = np.mgrid[0:405, 0:720] + 0.5
    band = disc(210, 260, 110) & ~disc(210, 260, 80) & (x < 210) & (y < 260)
    return band, band & ~deck_freeforms.dilate(~band, 3)


def test_ink_in_its_own_fill_and_outline_need_not_reach_every_side_of_the_box():
    """china-pptx 60: four red arrows outlined in green, each a curve in a turned frame far larger
    than its ink, were refused (`sides`: the ink reached two sides of the box) and drawn as four giant
    red rectangles. Red wrapped in green all along its edge is the element's own ink - a neighbour of
    the fill's colour does not wear the outline too - so it is traced; a dark hair running on from it
    (a branch against the sky) is left out, being no rim of either paint."""
    band, fill = curved_arrow()
    thumb = paint(paint(page(), band, "#5d992b"), fill, "#ff0000")
    thumb[180:182, 150:230] = 0                              # a black hair from the ring outwards
    pe = outlined(shape("a", "CUSTOM", 100, 100, 160, 160, solid("FF0000")), "5D992B", 3)
    [el] = elements(deck(pe), thumb)
    tr = el["trace"]
    assert tr["fill"].lower() == "#ff0000" and tr["stroke"].lower() == "#5d992b"
    assert abs(ring_area(tr["rings"]) - band.sum()) < 0.1 * band.sum()
    assert max(p[0] for r in tr["rings"] for p in r) / K < 214          # the hair is not the arrow's
    # the same ink with no outline round it vouches for nothing: the box is left as before
    [el] = elements(deck(pe), paint(page(), band, "#ff0000"))
    assert "trace" not in el


def elbow(oid: str, x0: float, x1: float) -> dict:
    """A SmartArt connector as the API gives it: a line with no type from (x0, 130) to (x1, 170), its
    box all we are told of the elbow drawn in it."""
    return {"objectId": oid, "size": {"width": pt(x1 - x0), "height": pt(40)}, "transform": at(x0, 130),
            "line": {"lineProperties": {"lineFill": solid("474B78"), "weight": pt(4)}}}


def test_connectors_sharing_a_bar_are_each_traced():
    """en-smartart 3: an org chart's 18 elbow connectors, one line each, down from the parent, along a
    bar they all share and down to the child. 12 were refused: the bar ran on out of each box into its
    sibling's (`continues`), or a sibling traced first hid the stub they share (`sides`). A line of
    the same group in the same paint is kin: what runs on into its box, or lies under its ink, is
    their common ink."""
    thumb = page()
    navy = [0x47, 0x4B, 0x78]
    thumb[130:150, 198:202] = navy                          # the parent's stub
    thumb[148:152, 98:302] = navy                           # the bar
    for x in (100, 150, 300):                               # the children's stubs
        thumb[150:170, x - 2:x + 2] = navy
    lines = [elbow("a", 100, 200), elbow("b", 200, 300), elbow("c", 150, 200)]
    group = {"objectId": "g", "transform": at(0, 0), "size": {"width": pt(720), "height": pt(405)},
             "elementGroup": {"children": lines}}
    els = elements(deck(group), thumb)
    assert [e["id"] for e in els if e.get("trace")] == ["a", "b", "c"]
    a = next(e for e in els if e["id"] == "a")
    xs = [p[0] / K for r in a["trace"]["rings"] for p in r]
    assert min(xs) < 100 and max(xs) > 200                  # its whole run, the stub c shares included


def test_a_kins_own_piece_in_the_box_stays_the_kins():
    """drawing-workshop 14: each letter of a word is a freeform line of one group in black, and the
    edge of the next letter lies in this one's box. Ink a kin shares is the line's only where it runs
    on from the line's own: a piece apart that the kin above already traced is the kin's."""
    thumb = page()
    for x0, y0, x1, y1 in ((100, 100, 160, 160), (120, 120, 140, 140)):
        thumb[y0 - 2:y1 + 2, x0 - 2:x1 + 2] = 0
        thumb[y0 + 2:y1 - 2, x0 + 2:x1 - 2] = 255

    def outline(oid, x0, y0, x1, y1):
        return {"objectId": oid, "size": {"width": pt(x1 - x0), "height": pt(y1 - y0)}, "transform": at(x0, y0),
                "line": {"lineProperties": {"lineFill": solid("000000"), "weight": pt(4)}}}

    group = {"objectId": "g", "transform": at(0, 0), "size": {"width": pt(720), "height": pt(405)},
             "elementGroup": {"children": [outline("big", 100, 100, 160, 160), outline("small", 120, 120, 140, 140)]}}
    els = {e["id"]: e for e in elements(deck(group), thumb)}
    assert len(els["small"]["trace"]["rings"]) == 2 and len(els["big"]["trace"]["rings"]) == 2


def slope(x0: float = 100, x1: float = 300) -> np.ndarray:
    """A 4 px stroke from (x0, 130) down to (x1, 150)."""
    y, x = np.mgrid[0:405, 0:720] + 0.5
    return (np.abs(y - 132 - (x - x0) * 16 / (x1 - x0)) <= 2) & (x >= x0) & (x < x1)


def sloped(oid: str = "a") -> dict:
    return {"objectId": oid, "size": {"width": pt(200), "height": pt(20)}, "transform": at(100, 130),
            "line": {"lineProperties": {"lineFill": solid("474B78"), "weight": pt(4)}}}


def test_a_strokes_ink_is_its_paint_and_an_edge_of_something_else_beside_it_is_not():
    """jeb-arch 4: a connector's end meets a box outline of a lighter blue, which read as the stroke's
    paint well enough to become a hook of it. Ink more than an antialiased rim from any pixel of the
    stroke's own paint is not the stroke's."""
    thumb = paint(page(), slope(), "#474b78")
    thumb[100:200, 300:303] = [0x5B, 0x9B, 0xD5]            # the box's left edge, where the stroke ends
    box = outlined(shape("box", "RECTANGLE", 300, 100, 100, 100, NONE), "5B9BD5", 3)
    els = {e["id"]: e for e in elements(deck(box, sloped()), thumb)}
    tr = els["a"]["trace"]
    assert not [p for r in tr["rings"] for p in r if p[0] / K > 299 and p[1] / K < 140]


def test_a_hairline_that_rarely_shows_its_paint_is_not_cut_into_dashes():
    """journey-maps 2: a 0.47 pt outline is a pixel wide on the thumbnail, its paint showing whole at
    one pixel in several; cut back to those it came out dashed. (Its antialiased trail is drawn two
    pixels tall here so that its steps stay joined.)"""
    y, x = np.mgrid[0:405, 0:720] + 0.5
    hair = (np.abs(y - 132 - (x - 100) * 16 / 200) <= 1) & (x >= 100) & (x < 300)
    thumb = paint(paint(page(), hair, "#9193ae"), hair & (x.astype(int) % 8 == 0), "#474b78")
    pe = sloped()
    pe["line"]["lineProperties"]["weight"] = pt(0.5)
    [el] = elements(deck(pe), thumb)
    assert len(el["trace"]["rings"]) == 1


def test_a_stroke_whose_end_is_mixed_with_another_line_is_asked_whole():
    """journey-maps 4: three curves leave one origin, and where they run together no pixel is any one
    curve's paint. Cut back to its paint the curve no longer reached the box's side and was refused;
    asked whole, it is traced."""
    thumb = paint(page(), slope(), "#474b78")
    y, x = np.mgrid[0:405, 0:720] + 0.5
    thumb[slope() & (x < 108)] = [82, 114, 82]              # navy and green, half and half
    [el] = elements(deck(sloped()), thumb)
    assert min(p[0] for r in el["trace"]["rings"] for p in r) / K < 101


def connector(oid: str, origin, axis, length: float, height: float, weight: float) -> dict:
    """A SmartArt connector as the API gives it: a line with no type, its frame `length` by `height`
    turned so that its first axis runs along `axis` (a unit vector, y down)."""
    (ox, oy), (cx, cy) = origin, axis
    return {"objectId": oid, "size": {"width": pt(length), "height": pt(height)},
            "transform": {"scaleX": cx, "shearX": -cy, "shearY": cy, "scaleY": cx,
                          "translateX": ox * EMU, "translateY": oy * EMU, "unit": "EMU"},
            "line": {"lineProperties": {"lineFill": solid("207F97"), "weight": pt(weight)}}}


def band(p, q, half: float) -> np.ndarray:
    """The pixels within `half` of the segment p-q (a stroke with butt ends)."""
    y, x = np.mgrid[0:405, 0:720] + 0.5
    (px_, py), (qx, qy) = p, q
    dx, dy = qx - px_, qy - py
    n = np.hypot(dx, dy)
    along = ((x - px_) * dx + (y - py) * dy) / n
    across = np.abs((x - px_) * dy - (y - py) * dx) / n
    return (along >= 0) & (along <= n) & (across <= half)


def test_a_line_ending_under_a_photo_disc_above_it_is_traced_to_the_disc():
    """en-smartart 4: three connectors leave from under Homer, a photo cut to a disc (a `{}` fill no
    colour or ramp explains). The disc's rim cuts each line's end off short of its box's side, and
    that side mostly lies beside the disc, so it was not unseen: all three were refused (`sides`)
    and left out. Ink that ends against something drawn above it runs on under it."""
    thumb = paint(page(), band((165, 130), (240, 60), 2), "#207f97")
    photo = disc(140, 140, 40)
    y, x = np.mgrid[0:405, 0:720]
    for k, colour in enumerate(("#ffd90f", "#ffffff", "#d1b271")):
        paint(thumb, photo & (x // 6 % 3 == k), colour)
    n = np.hypot(75, 70)
    line = connector("line", (165, 130), (75 / n, -70 / n), n, 0, 4)
    els = {e["id"]: e for e in elements(deck(line, shape("homer", "ELLIPSE", 100, 100, 80, 80, UNREAD)), thumb)}
    xs = [p[0] / K for r in els["line"]["trace"]["rings"] for p in r]
    assert 170 < min(xs) < 178 and max(xs) > 238                    # from the disc's rim to its end
    # the same ink with nothing above it that could hide its end: a piece of something, left as before
    assert "trace" not in elements(deck(line), thumb)[0]


def test_a_turned_connectors_ink_is_looked_for_in_its_whole_frame():
    """en-smartart 4: a SmartArt connector runs along the middle of a frame as tall as its stroke,
    turned. Its box is that of the frame's two ends, and the frame's other two corners - its ink's
    corners - lie beyond it by more than the antialiased rim: looked for in that box grown by the
    rim, the traced ink came out with its ends cut square."""
    c, s = np.cos(np.radians(30)), np.sin(np.radians(30))
    ox, oy = 200, 150
    ink = band((ox + 6 * s, oy + 6 * c), (ox + 80 * c + 6 * s, oy - 80 * s + 6 * c), 6)
    [el] = elements(deck(connector("line", (ox, oy), (c, -s), 80, 12, 12)), paint(page(), ink, "#207f97"))
    ys = [p[1] / K for r in el["trace"]["rings"] for p in r]
    assert min(ys) < oy - 80 * s + 1 and max(ys) > oy + 12 * c - 1  # both far corners of the frame


def test_an_outline_in_another_colour_is_drawn_inside_the_traced_edge():
    el = {"kind": "shape", "bbox": [10, 10, 30, 30], "trace": {
        "rings": [[[10, 10], [30, 10], [30, 30], [10, 30]]], "fill": "#ffffff", "alpha": None,
        "stroke": "#000000", "weight": 1.0, "source": "thumbnail"}}
    out = adopt_shapes.traced_block(el, Context(), "")
    assert "\\clip" in out and "line width=2.00pt" in out and "draw=black" in out
