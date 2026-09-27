"""Fills the Slides API reads as empty, read back from the slide's thumbnail (deck_fills, and
deck_ir(foreign=True, thumbnails=...)). Offline: hand-made `presentations.get` answers and numpy
thumbnails 720 px wide, so a thumbnail pixel is a Slides point."""

from pathlib import Path

import numpy as np
import pytest

from beamer2slides import adopt, adopt_shapes, deck_fills
from beamer2slides.deck_ir import deck_ir, page_size_for
from beamer2slides.inverse import Context

from .test_adopt_tables import table

EMU = 12700
UNREAD = {}                                  # what the API says of a gradient, picture or texture fill


@pytest.fixture(autouse=True)
def lengths_as_written(monkeypatch):
    monkeypatch.setattr(adopt, "to_bp", lambda text: text)


def pt(v: float) -> dict:
    return {"magnitude": v * EMU, "unit": "EMU"}


def at(x: float, y: float) -> dict:
    return {"scaleX": 1.0, "scaleY": 1.0, "translateX": x * EMU, "translateY": y * EMU, "unit": "EMU"}


def solid(hexc: str) -> dict:
    r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return {"solidFill": {"color": {"rgbColor": {"red": r, "green": g, "blue": b}}}}


def shape(oid, kind, x, y, w, h, fill) -> dict:
    pe = {"objectId": oid, "size": {"width": pt(w), "height": pt(h)}, "transform": at(x, y),
          "shape": {"shapeProperties": {"shapeBackgroundFill": fill}}}
    if kind:
        pe["shape"]["shapeType"] = kind
    return pe


def deck(*elements, bg: str = "FFFFFF") -> dict:
    """A 720 x 405 pt deck, `bg` background (white by default), one slide holding `elements`."""
    return {"presentationId": "p", "title": "t", "pageSize": {"width": pt(720), "height": pt(405)},
            "masters": [{"objectId": "m", "pageElements": [],
                         "pageProperties": {"pageBackgroundFill": solid(bg)}}],
            "layouts": [{"objectId": "L", "layoutProperties": {"masterObjectId": "m"}, "pageElements": []}],
            "slides": [{"objectId": "s", "slideProperties": {"layoutObjectId": "L"},
                        "pageElements": list(elements)}]}


def page(*rects, bg: str = "FFFFFF") -> np.ndarray:
    """A `bg`-coloured (white by default) 720 x 405 thumbnail with (x, y, w, h, colour) rectangles
    painted in order."""
    a = np.full((405, 720, 3), [int(bg[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.uint8)
    for x, y, w, h, c in rects:
        a[y:y + h, x:x + w] = [int(c[i:i + 2], 16) for i in (1, 3, 5)]
    return a


def elements(pres: dict, thumb=None) -> list[dict]:
    return deck_ir(pres, foreign=True, thumbnails=(lambda n: thumb) if thumb is not None else None
                   )["slides"][0]["elements"]


def no_marks_left(els: list[dict]) -> bool:
    return not any("fill_unread" in e or any("fill_unread" in c for c in e.get("table_cells", []))
                   for e in els)


# ---------------------------------------------------------------- shapes

def test_a_fill_the_api_cannot_say_is_read_from_a_flat_box():
    els = elements(deck(shape("a", "CUSTOM", 100, 100, 200, 100, UNREAD)),
                   page((100, 100, 200, 100, "#3366cc")))
    assert len(els) == 1 and els[0]["fill"] == "#3366cc" and els[0]["fill_source"] == "thumbnail"
    assert no_marks_left(els)


def test_without_thumbnails_nothing_changes():
    """Offline (pull, tests): a shape with only an unread fill is dropped as it always was, a solid one
    stays, and no marker reaches the IR."""
    els = elements(deck(shape("a", "CUSTOM", 100, 100, 200, 100, UNREAD),
                        shape("b", "RECTANGLE", 400, 100, 50, 50, solid("FF0000")),
                        table()))
    assert [e["kind"] for e in els] == ["shape", "table"] and els[0]["fill"].lower() == "#ff0000"
    assert no_marks_left(els)
    assert all(c.get("fill_source") is None for c in els[1]["table_cells"])


def test_a_picture_fill_becomes_the_thumbnails_picture_of_it(tmp_path):
    """sc-memphis: a photo cut to a freeform comes back as `{}` and is no colour at all. With a folder
    to write to it becomes the thumbnail's pixels in its box, letters of a text above painted out
    (the text draws them) and the page round the photo transparent; without one it is dropped."""
    from PIL import Image
    rng = np.random.default_rng(1)
    thumb = page()
    thumb[100:200, 100:300] = rng.integers(0, 256, (100, 200, 3))          # the photo
    thumb[140:150, 150:250] = [255, 225, 126]                               # yellow words on it
    photo = {"kind": "shape", "role": "panel", "shape_type": "CUSTOM", "bbox": [90, 90, 310, 210],
             "fill": None, "outline": None, "fill_unread": True, "id": "ph", "object": "ph", "group": None}
    words = {"kind": "text", "role": "body", "bbox": [140, 130, 260, 160], "id": "t", "object": "t",
             "paragraphs": [{"runs": [{"text": "Gallery", "color": "#ffe17e"}]}]}
    assert deck_fills.settle([dict(photo), dict(words)], thumb, 1.0, "#ffffff") == [words]
    got = deck_fills.settle([dict(photo), dict(words)], thumb, 1.0, "#ffffff", False, tmp_path)
    assert [e["kind"] for e in got] == ["image", "text"]
    pic = got[0]
    assert pic["id"] == "ph" and pic["fill_source"] == "thumbnail" and pic["bbox"] == [90, 90, 310, 210]
    im = np.asarray(Image.open(pic["file"]))
    assert im.shape == (120, 220, 4)
    assert im[5, 5, 3] == 0 and im[50, 50, 3] == 255                        # page out, photo in
    yellow = (np.abs(im[50:60, 60:160, :3].astype(int) - [255, 225, 126]).max(axis=2) <= 14).mean()
    assert yellow < 0.05                                                    # the words are painted out


def test_a_placeholder_is_never_a_candidate():
    pe = shape("a", "RECTANGLE", 100, 100, 200, 100, UNREAD)
    pe["shape"]["placeholder"] = {"type": "BODY"}
    assert elements(deck(pe), page((100, 100, 200, 100, "#3366cc"))) == []


def test_a_gradient_bar_becomes_an_axis_shading():
    """cs161's header bar: black to red along x, which TikZ draws as left/middle/right colours."""
    thumb = page()
    t = (np.arange(720) + 0.5) / 720
    ramp = np.array([8, 3, 2]) + (np.array([226, 89, 82]) - np.array([8, 3, 2])) * t[:, None]
    thumb[80:110] = ramp.round().astype(np.uint8)[None]
    els = elements(deck(shape("bar", "RECTANGLE", 0, 80, 720, 30, UNREAD)), thumb)
    g = els[0]["fill_gradient"]
    assert g["axis"] == "x" and els[0].get("fill") is None
    first, middle, last = (deck_fills.rgb(c) for c in g["colors"])
    assert np.abs(first - [8, 3, 2]).max() <= 8 and np.abs(last - [226, 89, 82]).max() <= 8
    assert np.abs(middle - [117, 46, 42]).max() <= 8
    out = adopt_shapes.shape_block(els[0], Context(), "")
    assert "left color=" in out and "right color=" in out and "middle color=" in out
    assert "fill=" not in out


def test_ink_under_a_candidate_is_never_painted_over():
    """A green square under the box shows through it: the box is no flat panel, and filling it would
    hide the square."""
    pres = deck(shape("under", "RECTANGLE", 150, 130, 30, 30, solid("00AA00")),
                shape("a", "CUSTOM", 100, 100, 200, 100, UNREAD))
    els = elements(pres, page((100, 100, 200, 100, "#3366cc"), (150, 130, 30, 30, "#00aa00")))
    assert [e["fill"].lower() for e in els] == ["#00aa00"]


def test_a_box_whose_colour_runs_on_around_it_is_not_filled():
    """What an unfilled squiggle tile on a panel shows is the panel: no edge of its own is seen."""
    pres = deck(shape("panel", "RECTANGLE", 50, 50, 500, 300, solid("3366CC")),
                shape("a", "CUSTOM", 100, 100, 200, 100, UNREAD))
    els = elements(pres, page((50, 50, 500, 300, "#3366cc")))
    assert [e["fill"].lower() for e in els] == ["#3366cc"] and len(els) == 1


def test_the_background_colour_is_no_fill():
    assert elements(deck(shape("a", "CUSTOM", 100, 100, 200, 100, UNREAD)), page()) == []


def test_a_candidate_settled_above_hides_the_one_under_it():
    """Settled from the top down: the upper panel takes the colour and covers the tile under it,
    which is not given that colour too."""
    pres = deck(shape("tile", "CUSTOM", 50, 50, 50, 50, UNREAD),       # in the wave's corner
                shape("wave", "CUSTOM", 50, 50, 300, 200, UNREAD))
    # the pink starts at the tile's first whole pixel, so the tile's own edges show on two sides
    els = elements(pres, page((51, 51, 299, 199, "#fac2bd")))
    assert len(els) == 1 and els[0]["fill"] == "#fac2bd" and els[0]["bbox"][2] > 200


def test_a_box_where_the_page_shows_is_no_panel_even_with_nothing_under_it():
    """With nothing under an element its own texture may carry a border (the stray check is waived),
    but a box 80% pink and 20% page is a tile over a pink shape's edge, not a pink tile."""
    pres = deck(shape("tile", "CUSTOM", 100, 100, 100, 100, UNREAD))
    assert elements(pres, page((100, 120, 100, 80, "#fac2bd"))) == []
    # the same box all pink but for a darker printed border of its own is its fill
    els = elements(pres, page((100, 100, 100, 100, "#fac2bd"), (104, 104, 92, 3, "#b08070")))
    assert [e["fill"] for e in els] == ["#fac2bd"]


def test_a_turned_shape_is_not_read():
    pe = shape("a", "RECTANGLE", 100, 100, 200, 100, UNREAD)
    pe["transform"].update({"scaleX": 0.866, "shearX": -0.5, "shearY": 0.5, "scaleY": 0.866})
    assert elements(deck(pe), page((100, 100, 200, 100, "#3366cc"))) == []


# ---------------------------------------------------------------- table cells

def test_cells_a_table_style_colours_are_read_and_the_page_colour_is_not():
    """test_adopt_tables' table at (50, 80), columns 100/60/60, rows of 30: its header cell over
    columns 1-2 is NOT_RENDERED but drawn #ffeeaa by a .pptx style; the unfilled cells show the page."""
    thumb = page((150, 80, 120, 30, "#ffeeaa"), (200, 90, 30, 8, "#222222"))    # the header's words
    el = next(e for e in elements(deck(table()), thumb) if e["kind"] == "table")
    cells = {(c["row"], c["col"]): c for c in el["table_cells"]}
    assert cells[0, 1]["fill"] == "#ffeeaa" and cells[0, 1]["fill_source"] == "thumbnail"
    assert cells[1, 1]["fill"] is None and cells[2, 1]["fill"] is None
    assert cells[0, 0]["fill"].lower() == "#cce5ff" and cells[0, 0].get("fill_source") is None
    assert no_marks_left([el])


def test_cells_over_a_page_of_another_colour_take_nothing():
    """comps-analysis: a table standing on a grey page shows the grey in every unfilled cell."""
    thumb = page((0, 0, 720, 405, "#444444"))
    el = next(e for e in elements(deck(table()), thumb) if e["kind"] == "table")
    assert all(c.get("fill_source") is None for c in el["table_cells"])


# ---------------------------------------------------------------- the reading itself

def test_read_region_tells_flat_gradient_and_neither():
    a = np.zeros((40, 100, 3), dtype=np.int16) + [10, 20, 200]
    region, allow = np.ones((40, 100), bool), np.zeros((40, 100), bool)
    assert deck_fills.read_region(a, region, allow, True)[0] == "solid"
    a[:, :] = (np.linspace(0, 200, 100)[None, :, None]).astype(np.int16)
    assert deck_fills.read_region(a, region, allow, True)[:2] == ("gradient", "x")
    assert deck_fills.read_region(a, region, allow, False) is None
    a[:, ::7] = 255                          # stripes: neither
    assert deck_fills.read_region(a, region, allow, True) is None


def test_a_pie_takes_the_angles_its_thumbnail_shows():
    """intro-lecture's grading chart: five PIE shapes in one box, whose dragged angles the API does
    not give - each was drawn as the preset's 270 degree slice. The colours around the centre say
    them: a slice under another shows only its own part, and the smallest arc holding it is drawn."""
    a = page()
    yy, xx = np.mgrid[0:405, 0:720]
    ang = np.degrees(np.arctan2(yy - 200, xx - 300)) % 360        # clockwise from +x, y down
    disc = (xx - 300) ** 2 + (yy - 200) ** 2 <= 100 ** 2
    a[disc & (ang < 90)] = [208, 224, 227]                          # top: 0-90
    a[disc & (ang >= 90)] = [69, 129, 142]                          # under it: 90-360 shows
    box = [200, 100, 400, 300]
    under = {"kind": "shape", "shape_type": "PIE", "bbox": box, "fill": "#45818e", "id": "u", "object": "u"}
    top = {"kind": "shape", "shape_type": "PIE", "bbox": box, "fill": "#d0e0e3", "id": "t", "object": "t"}
    out = deck_fills.settle([under, top], a, 1.0, "#ffffff")
    start, sweep = next(e for e in out if e["id"] == "t")["pie"]
    assert min(start, 360 - start) < 1 and abs(sweep - 90) < 1.5
    start, sweep = next(e for e in out if e["id"] == "u")["pie"]
    assert abs(start - 90) < 1 and abs(sweep - 270) < 1.5
    block = adopt_shapes.shape_block(next(e for e in out if e["id"] == "t"), Context(), "")
    assert "end angle=-" in block and "270" not in block


def draw_rounded(a, x0, y0, x1, y1, r, colour):
    """A rounded box painted into `a` (float), 4x4 supersampled as a renderer's antialiasing draws it."""
    yy, xx = np.mgrid[0:a.shape[0], 0:a.shape[1]] + 0.5
    cover = np.zeros(a.shape[:2])
    for sy in (-0.375, -0.125, 0.125, 0.375):
        for sx in (-0.375, -0.125, 0.125, 0.375):
            x, y = xx + sx, yy + sy
            cx, cy = np.clip(x, x0 + r, x1 - r), np.clip(y, y0 + r, y1 - r)
            cover += ((x - cx) ** 2 + (y - cy) ** 2 <= r * r) & (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
    cover = (cover / 16)[..., None]
    a[:] = a * (1 - cover) + np.array(colour) * cover


def test_a_rounded_rectangle_takes_the_corners_its_thumbnail_shows():
    """journey-maps' title bars are ROUND_RECTANGLEs whose corners were dragged square; the API gives
    no adjustment, and the preset's default drew them a sixth of their height round. A pill keeps
    its half-height corners, and a corner hidden under another shape says nothing."""
    a = page().astype(float)

    def rounded(*args):
        draw_rounded(a, *args)
    rounded(0, 0, 720, 45, 0, [204, 255, 0])
    rounded(100, 100, 400, 160, 30, [66, 133, 244])
    rounded(450, 100, 650, 200, 16, [219, 68, 55])
    rounded(0, 250, 200, 330, 12, [15, 157, 88])                    # on the page's left edge
    tab = {"kind": "shape", "shape_type": "ROUND_RECTANGLE", "bbox": [0, 250, 200, 330], "fill": "#0f9d58", "id": "t", "object": "t"}
    assert abs(deck_fills.corner_radius(a.astype(np.int16), tab, [], 1.0) - 12) < 1
    bar ={"kind": "shape", "shape_type": "ROUND_RECTANGLE", "bbox": [0, 0, 720, 45], "fill": "#ccff00", "id": "b", "object": "b"}
    pill = {"kind": "shape", "shape_type": "ROUND_RECTANGLE", "bbox": [100, 100, 400, 160], "fill": "#4285f4", "id": "p", "object": "p"}
    card = {"kind": "shape", "shape_type": "ROUND_RECTANGLE", "bbox": [450, 100, 650, 200], "fill": "#db4437", "id": "c", "object": "c"}
    lid = {"kind": "shape", "shape_type": "RECTANGLE", "bbox": [440, 90, 720, 300], "fill": "#db4437", "id": "l", "object": "l"}
    out = {e["id"]: e for e in deck_fills.settle([bar, pill, card], a.astype(np.int16), 1.0, "#ffffff")}
    assert out["b"]["corner_radius"] < 0.5 and abs(out["p"]["corner_radius"] - 30) < 1 and abs(out["c"]["corner_radius"] - 16) < 1
    assert "rounded" not in adopt_shapes.shape_block(out["b"], Context(), "")
    assert "rounded=30" in adopt_shapes.shape_block(out["p"], Context(), "")
    card.pop("corner_radius")
    hidden = {e["id"]: e for e in deck_fills.settle([card, lid], a.astype(np.int16), 1.0, "#ffffff")}
    assert "corner_radius" not in hidden["c"]


def test_an_outlined_box_is_read_by_its_outline_and_not_on_another_ones():
    """gdg24 54: a pale pink card outlined in black on a grey page, standing on a yellow card's black
    outline. The pink is too near the grey to see an edge in, but its outline is not; and the corner
    on the yellow card's outline is no edge at all - it read as square, the only corner read."""
    a = np.full((405, 720, 3), 238.0)
    for box, r, fill in (((200, 200, 400, 330), 30, [251, 188, 4]), ((260, 60, 460, 200), 30, [248, 216, 216])):
        x0, y0, x1, y1 = box
        draw_rounded(a, x0 - 2, y0 - 2, x1 + 2, y1 + 2, r + 2, [0, 0, 0])      # a 4 px outline on the edge
        draw_rounded(a, x0 + 2, y0 + 2, x1 - 2, y1 - 2, r - 2, fill)
    yellow = {"kind": "shape", "shape_type": "ROUND_RECTANGLE", "bbox": [200, 200, 400, 330], "fill": "#fbbc04",
              "outline": "#000000", "weight": 4, "id": "y", "object": "y"}
    pink = {"kind": "shape", "shape_type": "ROUND_RECTANGLE", "bbox": [260, 60, 460, 200], "fill": "#f8d8d8",
            "outline": "#000000", "weight": 4, "id": "p", "object": "p"}
    out = {e["id"]: e for e in deck_fills.settle([yellow, pink], a.astype(np.int16), 1.0, "#eeeeee")}
    assert abs(out["p"]["corner_radius"] - 30) < 1.5 and abs(out["y"]["corner_radius"] - 30) < 1.5


# ---------------------------------------------------------------- the page background

def picture_fill(url: str) -> dict:
    return {"stretchedPictureFill": {"contentUrl": url}}


def bg_deck(slide_fill, *elements, layout_fill=None, master_fill=None) -> dict:
    """Like `deck`, but the slide (and optionally its layout or master) carries its own
    `pageBackgroundFill`, so a mismatch between what the API reports and what the thumbnail shows
    can be built without depending on the master's default white."""
    pres = deck(*elements)
    pres["slides"][0]["pageProperties"] = {"pageBackgroundFill": slide_fill}
    if layout_fill is not None:
        pres["layouts"][0]["pageProperties"] = {"pageBackgroundFill": layout_fill}
    if master_fill is not None:
        pres["masters"][0]["pageProperties"] = {"pageBackgroundFill": master_fill}
    return pres


def slide0(pres: dict, thumb=None, foreign: bool = True, fetch=None, images=None) -> dict:
    return deck_ir(pres, foreign=foreign, fetch=fetch, images=images,
                   thumbnails=(lambda n: thumb) if thumb is not None else None)["slides"][0]


def test_a_linear_gradient_at_an_angle_is_recovered():
    """A .pptx `<a:gradFill>` (or a radial fill) always comes back as one flat `solidFill` -
    `pageBackgroundFill` has no gradient type at all. Here the reported colour is plain grey and the
    thumbnail is a diagonal ramp: neither axis, so the fit has to find the angle itself."""
    yy, xx = np.mgrid[0:405, 0:720]
    t = (xx + yy) / (719 + 404)
    c0, c1 = np.array([10.0, 10.0, 10.0]), np.array([240.0, 80.0, 30.0])
    thumb = (c0 + (c1 - c0) * t[..., None]).round().astype(np.uint8)
    s = slide0(bg_deck(solid("808080")), thumb)
    assert s["background_color"] == "#808080"                  # the API's own (wrong) answer, kept
    g = s["background_gradient"]
    assert g["type"] == "linear"
    assert abs(g["angle"] - (-45.0)) < 5
    got0, got1 = deck_fills.rgb(g["colors"][0]), deck_fills.rgb(g["colors"][1])
    assert np.abs(got0 - c0).max() <= 12 and np.abs(got1 - c1).max() <= 12


def test_a_radial_gradient_is_recovered():
    """`page_gradient` reports its centre and radius in IR pt (`center`, `bbox`'s own unit), not
    thumbnail pixels - a foreign deck's IR page is beamer's own size, not the Slides page's, so the
    two differ by the deck's `scale` even for this 720 px wide thumbnail. The centre is chosen in IR
    pt and only turned into pixels to paint the thumbnail, exactly as `adopt`'s px_box math does the
    other way round for every other candidate in this file."""
    pres = bg_deck(solid("406090"))
    page_w, _, _ = page_size_for(pres, None, True)
    px = 720 / page_w
    cx, cy = page_w * 0.55, page_w * 0.3         # IR pt, comfortably inside the page either way
    yy, xx = np.mgrid[0:405, 0:720]
    r = np.hypot(xx - cx * px, yy - cy * px)
    r_out = r.max()
    inner, outer = np.array([250.0, 240.0, 200.0]), np.array([30.0, 30.0, 60.0])
    thumb = (inner + (outer - inner) * np.clip(r / r_out, 0, 1)[..., None]).round().astype(np.uint8)
    s = slide0(pres, thumb)
    g = s["background_gradient"]
    assert g["type"] == "radial"
    assert abs(g["center"][0] - cx) <= 10 and abs(g["center"][1] - cy) <= 10
    got_in, got_out = deck_fills.rgb(g["colors"][0]), deck_fills.rgb(g["colors"][1])
    assert np.abs(got_in - inner).max() <= 14 and np.abs(got_out - outer).max() <= 14


def test_a_matching_flat_colour_is_left_alone():
    """The ordinary case - almost every slide's: nothing is fitted when the reported colour already
    explains the thumbnail's background."""
    thumb = page()
    thumb[:, :] = [0x33, 0x66, 0x99]
    s = slide0(bg_deck(solid("336699")), thumb)
    assert s["background_gradient"] is None and s["background_color"] == "#336699"


def test_artwork_fits_no_gradient():
    """greece-ppt's swirl artwork: the page is genuinely not flat, but it is not a ramp either -
    neither model may explain more than noise, and the flat (wrong) colour is left standing rather
    than drawn as a false gradient."""
    checker = (np.indices((405, 720)).sum(axis=0) // 20) % 2
    thumb = np.where(checker[..., None].astype(bool), [20, 20, 20], [230, 90, 40]).astype(np.uint8)
    s = slide0(bg_deck(solid("808080")), thumb)
    assert s["background_gradient"] is None and s["background_color"] == "#808080"


def test_without_a_thumbnail_the_background_is_unchanged():
    pres = bg_deck(solid("336699"))
    s = slide0(pres, None)
    assert s["background_color"] == "#336699" and s["background_gradient"] is None
    s = slide0(pres, None, foreign=False)
    assert s["background_color"] == "#336699" and s["background_gradient"] is None


def test_a_fine_texture_is_never_drawn_as_a_smooth_gradient():
    """en-flowchart's layout: a fine striped, gold-on-cream texture no picture URL exists for at all
    (the API's only witness for the whole chain is the master's own flat `pageBackgroundFill`). A
    plain affine fit still explains most of it within `PAGE_GRAD_TOL` - the stripe (+-15) rides on a
    real left-to-right trend spanning far more than that - so without a check for the fine grain
    itself `page_gradient` would draw it as a clean ramp; `page_textured` catches grain a real
    gradient never has, and `deck_ir` falls back to the thumbnail's own picture instead of either the
    already-wrong flat colour or a smoothed-over lie."""
    yy, xx = np.mgrid[0:405, 0:720]
    t = xx / 719
    c0, c1 = np.array([200.0, 180.0, 120.0]), np.array([120.0, 90.0, 40.0])
    base = c0 + (c1 - c0) * t[..., None]
    stripe = np.where((xx // 4) % 2 == 0, 15.0, -15.0)
    thumb = np.clip(base + stripe[..., None], 0, 255).astype(np.uint8)
    assert deck_fills.page_textured(thumb, np.ones((405, 720), dtype=bool))


def test_page_gradient_refuses_that_texture_and_a_picture_is_kept_instead(tmp_path):
    yy, xx = np.mgrid[0:405, 0:720]
    t = xx / 719
    c0, c1 = np.array([200.0, 180.0, 120.0]), np.array([120.0, 90.0, 40.0])
    base = c0 + (c1 - c0) * t[..., None]
    stripe = np.where((xx // 4) % 2 == 0, 15.0, -15.0)
    thumb = np.clip(base + stripe[..., None], 0, 255).astype(np.uint8)
    s = slide0(bg_deck(solid("FFFFFF")), thumb, images=tmp_path)
    assert s["background_gradient"] is None
    assert s.get("background_file") and Path(s["background_file"]).exists()


def test_a_clean_gradient_still_a_gradient_past_the_texture_check():
    """The texture guard must not swallow the genuine ramps the two tests above it already prove:
    ordinary JPEG-ish noise (a few levels) is not `PAGE_TEXTURE_STD` of grain."""
    yy, xx = np.mgrid[0:405, 0:720]
    t = (xx + yy) / (719 + 404)
    c0, c1 = np.array([10.0, 10.0, 10.0]), np.array([240.0, 80.0, 30.0])
    thumb = (c0 + (c1 - c0) * t[..., None]).round().astype(np.uint8)
    assert not deck_fills.page_textured(thumb, np.ones((405, 720), dtype=bool))
    s = slide0(bg_deck(solid("808080")), thumb)
    assert s["background_gradient"] is not None


# ---------------------------------------------------------------- inherited elements

def group(oid: str, *children) -> dict:
    return {"objectId": oid, "transform": at(0, 0), "elementGroup": {"children": list(children)}}


def multi(master_elements, n_slides: int, layout_elements=()) -> dict:
    """An `n_slides`-slide 720x405 deck whose every slide shares one layout and master, the master
    (and optionally the layout) carrying `master_elements`/`layout_elements` - `deck_ir`'s `foreign`
    path (`inherited_chain`) adds these to every slide with no check of its own; the tests below are
    that check (`deck_ir.vote_inherited`, `decide_drops`)."""
    return {"presentationId": "p", "title": "t", "pageSize": {"width": pt(720), "height": pt(405)},
            "masters": [{"objectId": "m", "pageElements": list(master_elements),
                         "pageProperties": {"pageBackgroundFill": solid("FFFFFF")}}],
            "layouts": [{"objectId": "L", "layoutProperties": {"masterObjectId": "m"},
                        "pageElements": list(layout_elements)}],
            "slides": [{"objectId": f"s{i}", "slideProperties": {"layoutObjectId": "L"},
                       "pageElements": []} for i in range(n_slides)]}


def inherited_ids(slides: list[dict]) -> set[str]:
    return {e["id"] for s in slides for e in s["elements"] if e.get("inherited")}


def test_an_inherited_element_never_shown_is_dropped_everywhere():
    """ua-space: a master group (here, one band shape) the API says every slide draws, but Google's
    own thumbnail never once shows it - a `showMasterSp` off in the source .pptx, which the API
    cannot say at all. Checked on both slides, confirmed shown on neither: dropped from both, not
    left standing because any one slide's own read was inconclusive."""
    band = shape("band", "RECTANGLE", 0, 0, 720, 60, solid("CC9966"))
    pres = multi([band], 2)
    slides = deck_ir(pres, foreign=True, thumbnails=lambda n: page())["slides"]
    assert inherited_ids(slides) == set()


def test_an_inherited_element_shown_on_one_slide_is_kept_on_all():
    """The same band, but Google's thumbnail draws it plainly on one of two slides - what a deck a
    person actually built in Slides usually looks like. One slide that shows it settles it for both:
    the other, where nothing but a mismatch would otherwise be read, keeps it rather than being
    guessed missing from a single slide's own vote."""
    band = shape("band", "RECTANGLE", 0, 0, 720, 60, solid("CC9966"))
    pres = multi([band], 2)
    thumbs = [page((0, 0, 720, 60, "#CC9966")), page()]
    slides = deck_ir(pres, foreign=True, thumbnails=lambda n: thumbs[n])["slides"]
    assert len(inherited_ids(slides)) == 1
    assert all(any(e.get("inherited") for e in s["elements"]) for s in slides)


def test_a_group_is_dropped_or_kept_as_one_piece():
    """Two master shapes sharing one group - a coloured band and a thin accent line - where a text
    box on every slide happens to sit exactly over the line, so the line's own box never stands clear
    of it and never casts a vote of its own. The band alone reads confidently absent on both slides;
    the group is one decision, not two, so the never-checkable line leaves with it."""
    band = shape("band", "RECTANGLE", 0, 0, 720, 60, solid("CC9966"))
    thin = shape("line", "RECTANGLE", 0, 0, 720, 4, solid("112233"))
    pres = multi([group("g1", band, thin)], 2)
    cover = shape("cover", "RECTANGLE", 0, 0, 720, 4, solid("336699"))
    for s in pres["slides"]:
        s["pageElements"] = [cover]
    slides = deck_ir(pres, foreign=True, thumbnails=lambda n: page((0, 0, 720, 4, "#336699")))["slides"]
    assert inherited_ids(slides) == set()


def test_an_inconclusive_element_is_never_dropped():
    """Without a thumbnail at all, `vote_inherited` never runs - nothing casts a vote, so nothing can
    be dropped, whatever `inherited_chain` found."""
    odd = shape("odd", "RECTANGLE", 0, 0, 720, 60, solid("445566"))
    pres = multi([odd], 1)
    slides = deck_ir(pres, foreign=True, thumbnails=None)["slides"]
    assert len(inherited_ids(slides)) == 1


def test_a_solid_the_thumbnail_disagrees_with_falls_through_to_the_layout_picture(tmp_path):
    """china-pptx: the slide's own solidFill sits over the layout's radial picture, which Slides
    actually draws instead of it. The picture is taken only once confirmed on the page's own pixels
    (fetched and resampled to the page) - offered on its say-so alone, the layout's or master's
    *shared* picture wrongly overrode other slides whose real background was a gradient of their
    own, unrelated to it (thai-history, found live in the corpus)."""
    from PIL import Image
    import io
    thumb = page()
    thumb[:, :] = [23, 108, 20]                     # the picture's own green, nothing like the fill
    green = io.BytesIO()
    Image.new("RGB", (8, 8), (23, 108, 20)).save(green, format="PNG")
    url = "https://example.test/green.png"
    pres = bg_deck(solid("ddebcf"), layout_fill=picture_fill(url))
    s = slide0(pres, thumb, fetch=lambda u: green.getvalue(), images=tmp_path)
    assert s["background_picture"] == url
    assert s["background_color"] is None and s["background_gradient"] is None


def test_a_layout_picture_that_does_not_match_is_left_for_the_gradient_fit(tmp_path):
    """A layout's or master's picture shared across slides that do not actually draw it (a different
    layout's own gradient, thai-history's real defect): the picture is confirmed against the page's
    own pixels and, failing that, the mismatch still goes to `page_gradient` rather than painting a
    wrong picture over it."""
    from PIL import Image
    import io
    yy, xx = np.mgrid[0:405, 0:720]
    t = yy / 404
    c0, c1 = np.array([0.0, 0.0, 130.0]), np.array([0.0, 70.0, 255.0])
    thumb = (c0 + (c1 - c0) * t[..., None]).round().astype(np.uint8)
    grey = io.BytesIO()
    Image.new("RGB", (8, 8), (150, 150, 150)).save(grey, format="PNG")   # unrelated shared photo
    pres = bg_deck(solid("000082"), layout_fill=picture_fill("https://example.test/shared.jpg"))
    s = slide0(pres, thumb, fetch=lambda u: grey.getvalue(), images=tmp_path)
    assert s["background_picture"] is None
    g = s["background_gradient"]
    assert g is not None and g["type"] == "linear"


def test_a_solid_the_thumbnail_disagrees_with_falls_through_to_the_layout_colour():
    thumb = page()
    thumb[:, :] = [0x11, 0x22, 0x33]
    pres = bg_deck(solid("ddebcf"), layout_fill=solid("112233"))
    s = slide0(pres, thumb)
    assert s["background_color"] == "#112233" and s["background_gradient"] is None
    assert s["background_picture"] is None


def test_the_gradient_is_drawn_as_a_clipped_shading():
    linear = {"type": "linear", "angle": -30.0, "colors": ["#0a0a0a", "#f05014"]}
    out = adopt.background_gradient_latex(linear, [720, 405], Context(), "TEXT")
    assert "setbeamertemplate{background canvas}" in out and "shade[left color=" in out
    assert "rotate=-30" in out and "\\clip" in out and "TEXT" in out
    radial = {"type": "radial", "center": [300, 200], "radius": 350, "colors": ["#faf0c8", "#1e1e3c"]}
    out2 = adopt.background_gradient_latex(radial, [720, 405], Context(), "TEXT")
    assert "shading=radial" in out2 and "inner color=" in out2 and "outer color=" in out2


def test_a_drive_videos_poster_frame_is_read_off_the_thumbnail(tmp_path):
    """No API gives a Drive video's poster frame (a play panel stood in); the slide's thumbnail shows it."""
    a = np.random.default_rng(1).integers(0, 256, (405, 720, 3)).astype(np.int16)
    video = {"kind": "image", "role": "figure", "bbox": [100, 100, 300, 220], "id": "v", "object": "v",
             "video": {"source": "DRIVE", "id": "x", "url": None}}
    tube = {**video, "id": "y", "file": "hq.jpg", "video": {"source": "YOUTUBE", "id": "y"}}
    out = deck_fills.settle([video, tube], a, 1.0, "#ffffff", pictures=tmp_path)
    v = next(e for e in out if e["id"] == "v")
    assert v["poster"] == "thumbnail" and v["video"]["source"] == "DRIVE"
    from PIL import Image
    assert (np.asarray(Image.open(v["file"]).convert("RGB")) == a[100:220, 100:300]).all()
    assert "poster" not in next(e for e in out if e["id"] == "y"), "YouTube's own thumbnail stays"


def test_a_themed_frame_takes_its_gradient_as_its_own_backdrop_option():
    """A recovered theme re-arms its canvas every frame, outvoting a canvas the frame's body sets
    (korea-pptx): the ramp goes in as the frame's `backdrop=`, replacing the layout's own."""
    assert adopt.with_option("[plain,label=p60,background=Teal]", "backdrop", "figures/g.png") == \
        "[plain,label=p60,background=Teal,backdrop=figures/g.png]"
    opts = adopt.with_option(adopt.without_options("[label=p6,backdrop=figures/a.png,background=Teal]",
                                                   "background", "backdrop"), "backdrop", "figures/g.png")
    assert opts == "[label=p6,backdrop=figures/g.png]"


def test_a_gradient_backdrop_is_drawn_from_the_fitted_model(tmp_path):
    from PIL import Image
    linear = {"type": "linear", "angle": 90.0, "colors": ["#ff0000", "#0000ff"]}
    rel = adopt.gradient_backdrop(linear, (720.0, 405.0), tmp_path)
    img = np.asarray(Image.open(tmp_path / rel).convert("RGB")).astype(int)
    top, bottom = img[0, img.shape[1] // 2], img[-1, img.shape[1] // 2]
    assert top[2] > 200 and bottom[0] > 200, "90 degrees runs up the page: colour 0 at the bottom"
    assert adopt.gradient_backdrop(linear, (720.0, 405.0), tmp_path) == rel     # one file per ramp
    radial = {"type": "radial", "center": [360.0, 202.5], "radius": 400.0, "colors": ["#ffffff", "#000000"]}
    img = np.asarray(Image.open(tmp_path / adopt.gradient_backdrop(radial, (720.0, 405.0), tmp_path)))
    assert img[img.shape[0] // 2, img.shape[1] // 2].min() > 250 and img[0, 0].max() < 160
    assert adopt.gradient_backdrop(linear, (720.0, 405.0), None) is None
