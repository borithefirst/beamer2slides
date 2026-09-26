"""Fills the Slides API reads as empty, read back from the slide's thumbnail (deck_fills, and
deck_ir(foreign=True, thumbnails=...)). Offline: hand-made `presentations.get` answers and numpy
thumbnails 720 px wide, so a thumbnail pixel is a Slides point."""

import numpy as np
import pytest

from beamer2slides import adopt, adopt_shapes, deck_fills
from beamer2slides.deck_ir import deck_ir
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


def deck(*elements) -> dict:
    """A 720 x 405 pt deck, white background, one slide holding `elements`."""
    return {"presentationId": "p", "title": "t", "pageSize": {"width": pt(720), "height": pt(405)},
            "masters": [{"objectId": "m", "pageElements": [],
                         "pageProperties": {"pageBackgroundFill": solid("FFFFFF")}}],
            "layouts": [{"objectId": "L", "layoutProperties": {"masterObjectId": "m"}, "pageElements": []}],
            "slides": [{"objectId": "s", "slideProperties": {"layoutObjectId": "L"},
                        "pageElements": list(elements)}]}


def page(*rects) -> np.ndarray:
    """A white 720 x 405 thumbnail with (x, y, w, h, colour) rectangles painted in order."""
    a = np.full((405, 720, 3), 255, dtype=np.uint8)
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
