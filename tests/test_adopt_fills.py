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


def test_a_preset_adopt_cannot_draw_becomes_the_thumbnails_picture(tmp_path):
    """en-mos: a rotated CURVED_UP_ARROW has no geometry in `adopt_shapes.preset`, and drawing it as
    a plain rectangle (`shape_block`'s fallback) turns a curled arrow icon into a solid diamond. With
    a folder to write to, `deck_fills.settle` bakes it as the thumbnail's own pixels in its (already
    rotated) bounding box instead, so the arrow's true outline survives."""
    from PIL import Image
    thumb = page((100, 100, 80, 80, "#3366cc"))
    arrow = {"kind": "shape", "role": "panel", "shape_type": "CURVED_UP_ARROW", "bbox": [100, 100, 180, 180],
             "fill": "#3366cc", "outline": None, "id": "ar", "object": "ar", "group": None}
    assert deck_fills.settle([dict(arrow)], thumb, 1.0, "#ffffff") == [arrow]
    got = deck_fills.settle([dict(arrow)], thumb, 1.0, "#ffffff", False, tmp_path)
    assert [e["kind"] for e in got] == ["image"]
    pic = got[0]
    assert pic["id"] == "ar~shape" and pic["fill_source"] == "thumbnail" and pic["bbox"] == [100, 100, 180, 180]
    im = np.asarray(Image.open(pic["file"]))
    assert im.shape[:2] == (80, 80)


def test_a_known_preset_is_never_replaced_by_a_picture(tmp_path):
    """A shape `adopt_shapes.preset` already knows how to draw (a plain RECTANGLE) is left as a shape
    even with a pictures folder handed in - the fallback is only for a name `preset` returns None
    for, never a chance to lose an editable shape's geometry."""
    thumb = page((100, 100, 80, 80, "#3366cc"))
    rect = {"kind": "shape", "role": "panel", "shape_type": "RECTANGLE", "bbox": [100, 100, 180, 180],
            "fill": "#3366cc", "outline": None, "id": "rc", "object": "rc", "group": None}
    got = deck_fills.settle([dict(rect)], thumb, 1.0, "#ffffff", False, tmp_path)
    assert [e["kind"] for e in got] == ["shape"]


def test_a_not_rendered_fill_with_a_bogus_solid_colour_is_unread_too():
    """china-pptx: a .pptx gradient or theme fill on a shape can come back `NOT_RENDERED` with a
    default `solidFill` alongside it that is not what is drawn (deck_ir.unread_fill) - the same
    "nowhere in the answer" state a .pptx table style's cell colour comes back in."""
    fill = {"propertyState": "NOT_RENDERED",
            "solidFill": {"color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}}
    els = elements(deck(shape("a", "RECTANGLE", 100, 100, 200, 100, fill)),
                   page((100, 100, 200, 100, "#3366cc")))
    assert len(els) == 1 and els[0]["fill"] == "#3366cc" and els[0]["fill_source"] == "thumbnail"
    assert no_marks_left(els)


def test_a_text_boxs_own_fill_no_ramp_fits_is_baked_behind_it(tmp_path):
    """china-pptx's tall panel: a `NOT_RENDERED` fill behind a text box's own words, shaded in a way
    `read_region`'s single-axis ramp cannot fit at all (neither "x" nor "y" reaches half its pixels
    there). The text stays native with no fill of its own; what is behind it becomes a picture of the
    thumbnail, its own words - not just an element drawn above it - painted out, under its own id
    (`<id>~fill`) so it never collides with the text box it sits behind."""
    from PIL import Image
    rng = np.random.default_rng(2)
    thumb = page()
    thumb[90:310, 90:310] = rng.integers(0, 256, (220, 220, 3))              # neither flat nor a ramp
    thumb[150:160, 120:280] = [255, 225, 126]                                # its own word, drawn on top
    panel = {"kind": "text", "role": "body", "shape_type": "TEXT_BOX", "bbox": [90, 90, 310, 310],
             "fill": None, "outline": None, "fill_unread": True, "id": "t", "object": "t", "group": None,
             "paragraphs": [{"runs": [{"text": "Word", "color": "#ffe17e"}]}]}
    assert deck_fills.settle([dict(panel)], thumb, 1.0, "#ffffff")[0]["id"] == "t"    # no folder: unchanged
    got = deck_fills.settle([dict(panel)], thumb, 1.0, "#ffffff", False, tmp_path)
    assert [e["kind"] for e in got] == ["image", "text"]
    pic, text = got
    assert pic["id"] == "t~fill" and text["id"] == "t"
    assert not text.get("fill") and not text.get("fill_gradient")
    assert "object" not in pic and "key" not in pic
    im = np.asarray(Image.open(pic["file"]))
    yellow = (np.abs(im[50:70, 20:180, :3].astype(int) - [255, 225, 126]).max(axis=2) <= 14).mean()
    assert yellow < 0.05                                                      # its own words painted out


def test_a_placeholder_is_never_a_candidate():
    pe = shape("a", "RECTANGLE", 100, 100, 200, 100, UNREAD)
    pe["shape"]["placeholder"] = {"type": "BODY"}
    assert elements(deck(pe), page((100, 100, 200, 100, "#3366cc"))) == []


def test_a_placeholders_own_unsaid_fill_is_read_from_the_thumbnail():
    """china-pptx 173: a caption placeholder's own fill is a gradient panel over a photo, and the
    API says of it only `{}` (RENDERED, no solidFill) - unlike the 772 placeholders whose own
    NOT_RENDERED is no fill at all. Read as nothing, the caption stood on the bare photo."""
    pe = shape("cap", "RECTANGLE", 0, 100, 720, 120, UNREAD)
    pe["shape"]["placeholder"] = {"type": "BODY"}
    pe["shape"]["text"] = {"textElements": [{"paragraphMarker": {"style": {}}},
                                            {"textRun": {"content": "Why is this city relevant?\n",
                                                         "style": {"fontSize": pt(28)}}}]}
    thumb = page()
    t = (np.arange(720) + 0.5) / 720
    ramp = np.array([250, 250, 245]) + (np.array([190, 205, 235]) - np.array([250, 250, 245])) * t[:, None]
    thumb[100:220] = ramp.round().astype(np.uint8)[None]
    el = next(e for e in elements(deck(pe), thumb) if e.get("id") == "cap")
    assert el.get("fill_gradient") or el.get("fill_source") == "thumbnail"
    none = shape("none", "RECTANGLE", 0, 100, 720, 120, {"propertyState": "NOT_RENDERED"})
    none["shape"]["placeholder"], none["shape"]["text"] = pe["shape"]["placeholder"], pe["shape"]["text"]
    kept = next(e for e in elements(deck(none), thumb) if e.get("id") == "none")
    assert not kept.get("fill_gradient") and not kept.get("fill")        # its own NOT_RENDERED: no fill


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


def test_a_sliver_beside_a_full_width_picture_still_fits_a_gradient():
    """china-pptx slide 47: a full-width title strip across the top and a picture almost as wide as
    the page below it leave only two thin vertical slivers of the page's own radial background
    visible - 5.3% of the page here, matching the real slide. `page_gradient` must still fit it
    (`PAGE_GRAD_MIN_SHARE` was 0.08, too high for that; it is now 0.03) - the two slivers alone gave
    the right centre and colours on the live deck, confirmed against a sibling slide sharing the same
    layout with 22% of the page visible. Painted at 1600x900 (Google's own `getThumbnail` LARGE
    width, not this file's usual 720): at 720 the slivers are physically too few pixels wide for
    `_radial_centre`'s fixed step to triangulate at all, which is a resolution artefact of the test,
    not a real slide's - `getThumbnail` never comes back narrower than this."""
    W, H = 1600, 900
    SCALE = W / 720
    title = shape("title", "TEXT_BOX", 0, 0, 720, 108, solid("ffffff"))
    picture = shape("pic", "CUSTOM", 30, 108, 665, 297, solid("ffffff"))
    pres = bg_deck(solid("ddebcf"), title, picture)
    page_w, page_h, _ = page_size_for(pres, None, True)
    px = W / page_w
    title_h = round(108 * SCALE)
    pic_x0, pic_x1 = round(30 * SCALE), round(695 * SCALE)
    bboxes = [{"bbox": [0.0, 0.0, page_w, 108 * page_w / 720]},
              {"bbox": [30 * page_w / 720, 108 * page_w / 720, 695 * page_w / 720, page_h]}]
    mask = deck_fills.page_visible_mask(bboxes, px, W, H)
    cx, cy = page_w * 0.5, page_h * 0.5                 # IR pt, the page's own centre
    yy, xx = np.mgrid[0:H, 0:W]
    r_full = np.hypot(xx - cx * px, yy - cy * px)
    r_out = r_full[mask].max()                          # scaled to the radius the slivers themselves
    inner, outer = np.array([255.0, 255.0, 201.0]), np.array([22.0, 107.0, 19.0])
    thumb = (inner + (outer - inner) * np.clip(r_full / r_out, 0, 1)[..., None]).round().astype(np.uint8)
    thumb[0:title_h, 0:W] = [255, 255, 255]             # the title's own white box
    thumb[title_h:H, pic_x0:pic_x1] = [255, 255, 255]   # the picture's own white box
    s = slide0(pres, thumb)
    assert s["background_color"] is None or s["background_color"] == "#ddebcf"
    g = s["background_gradient"]
    assert g is not None and g["type"] == "radial"
    assert abs(g["center"][0] - cx) <= 15 and abs(g["center"][1] - cy) <= 15
    got_in, got_out = deck_fills.rgb(g["colors"][0]), deck_fills.rgb(g["colors"][1])
    assert np.abs(got_in - inner).max() <= 25 and np.abs(got_out - outer).max() <= 25


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


def test_an_inherited_picture_is_judged_by_its_cropped_piece(tmp_path):
    """instagram: the master draws six pieces of one screenshot, each through its own
    `cropProperties`. The thumbnail shows only the piece; the whole sheet squeezed into the box
    matched a quarter of it, so every piece voted absent and was dropped from every slide."""
    from PIL import Image
    import io
    sheet = Image.new("RGB", (400, 100))
    for i, c in enumerate([(255, 0, 0), (0, 160, 0), (255, 220, 0), (0, 0, 255)]):
        sheet.paste(c, (100 * i, 0, 100 * i + 100, 100))
    png = io.BytesIO()
    sheet.save(png, format="PNG")
    url = "https://example.test/sheet.png"
    piece = {"objectId": "piece", "size": {"width": pt(200), "height": pt(100)}, "transform": at(0, 0),
             "image": {"contentUrl": url, "imageProperties": {"cropProperties": {"leftOffset": 0.75}}}}
    pres = multi([piece], 2)
    slides = deck_ir(pres, foreign=True, fetch=lambda u: png.getvalue(), images=tmp_path,
                     thumbnails=lambda n: page((0, 0, 200, 100, "#0000FF")))["slides"]
    assert all(any(e.get("inherited") for e in s["elements"]) for s in slides)


def test_a_piece_drawn_over_is_judged_by_what_its_own_crop_shows(tmp_path):
    """instagram: the master's bottom bar is one piece of a screenshot whose middle is transparent,
    and another opaque piece of the same sheet is drawn over the bar's middle. Read as the whole
    sheet, that cover was see-through ink, the bar was judged under it, voted absent and dropped:
    the deck lost the ends of its bottom bar on every slide."""
    from PIL import Image
    import io
    sheet = Image.new("RGBA", (100, 300), (0, 0, 0, 0))            # the middle: transparent
    sheet.paste((40, 40, 40, 255), (0, 0, 100, 100))              # the bar
    sheet.paste((0, 90, 160, 255), (0, 200, 100, 300))            # the cover
    png = io.BytesIO()
    sheet.save(png, format="PNG")
    url = "https://example.test/sheet.png"

    def piece(oid, x, w, crop):
        return {"objectId": oid, "size": {"width": pt(w), "height": pt(60)}, "transform": at(x, 300),
                "image": {"contentUrl": url, "imageProperties": {"cropProperties": crop}}}

    bar = piece("bar", 0, 720, {"bottomOffset": 2 / 3})
    cover = piece("cover", 100, 520, {"topOffset": 2 / 3})
    thumb = page((0, 300, 720, 60, "#282828"), (100, 300, 520, 60, "#005aa0"))
    slides = deck_ir(multi([bar, cover], 2), foreign=True, fetch=lambda u: png.getvalue(), images=tmp_path,
                     thumbnails=lambda n: thumb)["slides"]
    assert all(inherited_ids([s]) == {"m~bar", "m~cover"} for s in slides)


def test_a_layouts_unsaid_placeholder_fill_is_read_from_the_thumbnail():
    """ml-vs-stats: every title stands on a dark bar that only the layout's title placeholder draws,
    and the API gives that fill as `{}` (a gradient or picture fill). The slide's placeholder says
    INHERIT; the `{}` it inherits was taken for no fill at all, and white words stood on white."""
    lay = shape("lt", "TEXT_BOX", 200, 0, 520, 30, UNREAD)
    lay["shape"]["placeholder"] = {"type": "TITLE"}
    title = shape("t", "TEXT_BOX", 200, 0, 520, 30, {"propertyState": "INHERIT"})
    title["shape"]["placeholder"] = {"type": "TITLE", "parentObjectId": "lt"}
    title["shape"]["text"] = {"textElements": [{"paragraphMarker": {"style": {}}},
                                               {"textRun": {"content": "Contrasting analyses\n",
                                                            "style": {"fontSize": pt(14)}}}]}
    pres = deck(title)
    pres["layouts"][0]["pageElements"] = [lay]
    el = next(e for e in elements(pres, page((200, 0, 520, 30, "#434343"))) if e.get("id") == "t")
    assert el.get("fill") == "#434343" and el.get("fill_source") == "thumbnail"


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


# ---------------------------------------------------------------- round things (en-smartart, yc-seed-white)

def noisy(a: np.ndarray, x: int, y: int, w: int, h: int, seed: int = 3, oval: bool = False) -> None:
    """Paint noise (neither a flat colour nor a ramp) into `a`'s (x, y, w, h), only inside the
    inscribed ellipse when `oval`."""
    rng = np.random.default_rng(seed)
    patch = rng.integers(0, 256, (h, w, 3)).astype(np.uint8)
    if oval:
        yy, xx = np.mgrid[0:h, 0:w]
        keep = ((xx + 0.5 - w / 2) / (w / 2)) ** 2 + ((yy + 0.5 - h / 2) / (h / 2)) ** 2 <= 1
        a[y:y + h, x:x + w][keep] = patch[keep]
    else:
        a[y:y + h, x:x + w] = patch


def ball(oid="b", box=(200, 100, 300, 200)) -> dict:
    return {"kind": "shape", "role": "panel", "shape_type": "ELLIPSE", "bbox": list(box), "fill": None,
            "outline": None, "fill_unread": True, "id": oid, "object": oid, "group": None}


def panel_under(box=(150, 50, 350, 250)) -> dict:
    return {"kind": "shape", "role": "panel", "shape_type": "RECTANGLE", "bbox": list(box), "fill": "#3366cc",
            "outline": None, "id": "u", "object": "u", "group": None}


def test_a_gradient_ellipse_is_a_picture_of_only_its_ellipse(tmp_path):
    """en-smartart's balls: an ELLIPSE with a `{}` fill over another shape was cut square from the
    thumbnail, its corners printing the panel under it over whatever else was there."""
    from PIL import Image
    a = page((150, 50, 200, 200, "#3366cc"))
    noisy(a, 200, 100, 100, 100, oval=True)
    got = deck_fills.settle([panel_under(), ball()], a, 1.0, "#ffffff", False, tmp_path)
    pic, = (e for e in got if e["kind"] == "image")
    im = np.asarray(Image.open(pic["file"]))
    assert im.shape == (100, 100, 4)
    assert im[2, 2, 3] == 0 and im[97, 97, 3] == 0, "the corners are not the ball's"
    assert im[50, 50, 3] == 255 and im[50, 1, 3] > 0, "its middle and rim are"


def test_a_nodes_see_through_text_box_on_its_shape_is_no_picture_of_it(tmp_path):
    """A SmartArt node is a shape and a NOT_RENDERED text box of exactly its box: the text box's
    unread fill is the shape under it, and a picture of it would hide the ball square."""
    a = page((150, 50, 200, 200, "#3366cc"))
    noisy(a, 200, 100, 100, 100, oval=True)
    words = {"kind": "text", "role": "body", "bbox": [200, 100, 300, 200], "id": "t", "object": "t",
             "fill_unread": True, "paragraphs": [{"runs": [{"text": "Facebook", "color": "#ffffff"}]}]}
    got = deck_fills.settle([panel_under(), ball(), words], a, 1.0, "#ffffff", False, tmp_path)
    assert [e["id"] for e in got if e["kind"] == "image"] == ["b"], "the ball's picture, no `t~fill`"
    assert not next(e for e in got if e["id"] == "t").get("fill")


def test_a_picture_under_a_see_through_shape_does_not_take_its_tint_twice(tmp_path):
    """en-smartart's funnel is white at 0.4 over the balls: the thumbnail shows the balls through it,
    and the funnel is drawn again above their picture, so the crop has that tint taken back out."""
    from PIL import Image
    a = page((150, 50, 200, 200, "#3366cc"))
    noisy(a, 200, 100, 100, 100)
    truth = a[100:200, 200:300].astype(float).copy()
    a[100:150, 200:300] = np.round(0.6 * a[100:150, 200:300] + 0.4 * 255).astype(np.uint8)
    blob = {**ball(), "shape_type": "CUSTOM"}
    veil = {"kind": "shape", "role": "panel", "shape_type": "RECTANGLE", "bbox": [180, 90, 320, 150],
            "fill": "#ffffff", "fill_alpha": 0.4, "outline": None, "id": "v", "object": "v", "group": None}
    got = deck_fills.settle([panel_under(), blob, veil], a, 1.0, "#ffffff", False, tmp_path)
    pic, = (e for e in got if e["kind"] == "image")
    im = np.asarray(Image.open(pic["file"]).convert("RGB")).astype(float)
    assert np.abs(im[5:45, 5:95] - truth[5:45, 5:95]).max() <= 2, "under the veil: the picture itself"
    assert np.abs(im[55:95] - truth[55:95]).max() <= 1, "outside it: untouched"


def test_a_see_through_freeform_is_traced_by_its_opaque_outline():
    """en-smartart's funnel: white at 0.4 over other shapes, so its fill has no one colour to key on
    ("alpha-over"); its red outline does, and what that encloses is the shape."""
    from PIL import Image, ImageDraw
    img = Image.fromarray(page((100, 50, 300, 250, "#3366cc")))
    tri = [(150, 100), (350, 100), (250, 250)]
    inner = Image.new("L", img.size, 0)
    ImageDraw.Draw(inner).polygon(tri, fill=255)
    arr = np.asarray(img).astype(float)
    m = np.asarray(inner) > 0
    arr[m] = 0.6 * arr[m] + 0.4 * 255
    img = Image.fromarray(arr.round().astype(np.uint8))
    ImageDraw.Draw(img).polygon(tri, outline=(218, 28, 39), width=2)
    funnel = {"kind": "shape", "role": "panel", "shape_type": "CUSTOM", "bbox": [150, 100, 350, 250],
              "fill": "#ffffff", "fill_alpha": 0.4, "outline": "#da1c27", "weight": 2.0, "id": "f",
              "object": "f", "group": None}
    got = deck_fills.settle([panel_under((100, 50, 400, 300)), funnel], np.asarray(img), 1.0, "#ffffff")
    f = next(e for e in got if e["id"] == "f")
    assert f.get("trace"), "traced"
    xs = [x for ring in f["trace"]["rings"] for x, _ in ring]
    ys = [y for ring in f["trace"]["rings"] for _, y in ring]
    assert min(xs) == pytest.approx(150, abs=3) and max(xs) == pytest.approx(350, abs=3)
    assert max(ys) == pytest.approx(250, abs=4), "down to the point, not the box's corners"
    assert f["trace"]["fill"] == "#ffffff" and f["trace"]["alpha"] == 0.4


NO_FILL = {"propertyState": "NOT_RENDERED", "solidFill": {"color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}}
TEAL = (51, 183, 191)


def outlined(pe: dict, colour=TEAL, weight: float = 2.0) -> dict:
    r, g, b = (v / 255 for v in colour)
    pe["shape"]["shapeProperties"]["outline"] = {
        "outlineFill": {"solidFill": {"color": {"rgbColor": {"red": r, "green": g, "blue": b}}}}, "weight": pt(weight)}
    return pe


def striped(a: np.ndarray, x: int, y: int, w: int, h: int, one=(140, 20, 40), two=(240, 192, 32)) -> None:
    """Stripes 3 px wide in `a`'s (x, y, w, h), dark red and yellow unless told: neither one colour, a
    ramp nor a single paint over a ground, like the many colours of en-mos' wavy header."""
    cols = (np.arange(w) // 3) % 2
    a[y:y + h, x:x + w] = np.where(cols[None, :, None] == 0, one, two)


def test_an_outline_only_freeform_is_traced_as_its_line(tmp_path):
    """en-mos' master: two thin curves over the wavy header, outline only, whose fill comes back
    NOT_RENDERED - which is also how Slides says "no fill". The box holds the header's many colours,
    so no fill reads; it was a picture of the whole box with the box's rectangle drawn round it (a
    straight line across the header on 80 slides). Its outline alone is what is traced."""
    from PIL import Image, ImageDraw
    a = page()
    striped(a, 0, 40, 720, 50)
    img = Image.fromarray(a)
    xs = np.linspace(100, 600, 200)
    ImageDraw.Draw(img).line([(x, 80 + 20 * np.sin((x - 100) / 500 * 4 * np.pi)) for x in xs], fill=TEAL, width=2)
    pres = deck(shape("wave", "CUSTOM", 0, 40, 720, 50, UNREAD),
                outlined(shape("curve", "CUSTOM", 100, 60, 500, 40, NO_FILL)))
    els = deck_ir(pres, foreign=True, thumbnails=lambda n: np.asarray(img), images=tmp_path)["slides"][0]["elements"]
    mine = [e for e in els if e["id"].startswith("curve")]
    assert [e["kind"] for e in mine] == ["shape"], "no picture of its box, no rectangle round it"
    tr = mine[0]["trace"]
    assert tr["fill"] == "#33b7bf" and not mine[0].get("fill")
    ys = [y for ring in tr["rings"] for _, y in ring]
    x0, y0, x1, y1 = mine[0]["bbox"]                                    # IR pt: 453.54 across
    assert min(ys) == pytest.approx(y0, abs=1.5) and max(ys) == pytest.approx(y1, abs=1.5)
    assert no_marks_left(els) and not any("_not_rendered" in e for e in els)


def test_an_outlined_freeform_round_an_unread_fill_keeps_its_picture_and_no_box(tmp_path):
    """A NOT_RENDERED freeform whose outline goes round what no reading explains (a .pptx gradient,
    china-pptx) is no outline-only line: traced with its outline as the paint, what it holds is taken
    in with the ring (thicker than any stroke), so it stays the thumbnail's picture, which holds its
    outline too - and not also its box's rectangle, all an untraced freeform's outline could draw."""
    from PIL import Image, ImageDraw
    a = page()
    striped(a, 200, 100, 200, 100)
    yy, xx = np.mgrid[0:405, 0:720]
    a[((xx - 300) / 100) ** 2 + ((yy - 150) / 50) ** 2 > 1] = 255        # an oval blob, not its box
    img = Image.fromarray(a)
    ImageDraw.Draw(img).ellipse([200, 100, 399, 199], outline=TEAL, width=2)
    pres = deck(outlined(shape("blob", "CUSTOM", 200, 100, 200, 100, NO_FILL)))
    els = deck_ir(pres, foreign=True, thumbnails=lambda n: np.asarray(img), images=tmp_path)["slides"][0]["elements"]
    assert [e["kind"] for e in els] == ["image"] and not els[0].get("trace")


def test_an_outline_ring_on_a_picture_is_no_line(tmp_path):
    """On a slide with a background picture, over a picture, any colour may show: what a thin ring
    holds may be what is under it or a fill of its own no reading explained, which tracing leaves a
    hole. A ring that encloses something is left to the thumbnail's picture."""
    from PIL import Image, ImageDraw
    ramp = np.linspace(0, 1, 400)[None, :, None]
    a = page()
    a[50:350, 100:500] = np.round((1 - ramp) * [30, 40, 120] + ramp * [120, 30, 60]).astype(np.uint8)
    photo = Image.fromarray(a[50:350, 100:500].copy())
    photo.save(tmp_path / "photo.png")
    striped(a, 200, 100, 200, 100, (240, 192, 32), (250, 180, 200))
    img = Image.fromarray(a)
    ImageDraw.Draw(img).ellipse([200, 100, 399, 199], outline=TEAL, width=2)
    under = {"kind": "image", "role": "figure", "bbox": [100, 50, 500, 350], "file": str(tmp_path / "photo.png"),
             "id": "p", "object": "p", "group": None}
    ring = {"kind": "shape", "role": "panel", "shape_type": "CUSTOM", "bbox": [200, 100, 400, 200], "fill": None,
            "outline": "#33b7bf", "weight": 2.0, "fill_unread": True, "_not_rendered": True, "id": "r",
            "object": "r", "group": None}
    got = deck_fills.settle([under, ring], np.asarray(img), 1.0, None, True, tmp_path)
    assert [(e["kind"], e["id"]) for e in got] == [("image", "p"), ("image", "r")]


def test_a_ring_cut_by_a_caption_in_its_colour_is_still_a_ring(tmp_path):
    """cs161-net 13: a red ring round the houses and their wires, a caption in the same red across its
    rim. The trace leaves the rim under those words out (they may be the letters), which opened the
    ring into a line: drawn so, the wires only its crop held were gone. Under words of its colour the
    stroke still closes the ring; here the letters cross its rim, so what lies under them is no line
    to close the traced ring with (`closed_under_words`), and the ring is the thumbnail's picture."""
    from PIL import Image, ImageDraw
    red = (200, 30, 50)
    a = page()
    noisy(a, 260, 120, 80, 40)
    house = Image.fromarray(a[120:160, 260:340].copy())
    house.save(tmp_path / "house.png")
    img = Image.fromarray(a)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([200, 100, 399, 209], radius=20, outline=red, width=2)
    for x in range(262, 340, 9):                         # the caption's letters, over the rim
        draw.rectangle([x, 200, x + 5, 214], fill=red)
    ring = {"kind": "shape", "role": "panel", "shape_type": "CUSTOM", "bbox": [200, 100, 400, 210], "fill": None,
            "outline": "#c81e32", "weight": 2.0, "fill_unread": True, "_not_rendered": True, "id": "r",
            "object": "r", "group": None}
    pic = {"kind": "image", "role": "figure", "bbox": [260, 120, 340, 160], "file": str(tmp_path / "house.png"),
           "id": "h", "object": "h", "group": None}
    caption = {"kind": "text", "role": "body", "bbox": [255, 196, 350, 218], "id": "t", "object": "t",
               "paragraphs": [{"runs": [{"text": "local network", "color": "#c81e32"}]}]}
    got = deck_fills.settle([ring, pic, caption], np.asarray(img), 1.0, "#ffffff", False, tmp_path)
    r = next(e for e in got if e["id"].startswith("r"))
    assert r["kind"] == "image" and not r.get("trace"), "the ring's picture, not an open line"


def drawn(rings, size=(720, 405)) -> np.ndarray:
    """The pixels a trace's rings fill, even-odd (at 1 px per pt)."""
    from PIL import Image, ImageDraw
    out = np.zeros(size[::-1], dtype=bool)
    for ring in rings:
        im = Image.new("1", size, 0)
        ImageDraw.Draw(im).polygon([tuple(p) for p in ring], fill=1)
        out ^= np.asarray(im, dtype=bool)
    return out


def test_a_ring_round_what_shows_the_slide_is_its_line_closed_under_its_caption(tmp_path):
    """cs161-net 13: a green ring round two red rings, their houses and wires, drawn above them, and
    a caption box with green and red words across its rim. What it holds shows the white slide
    wherever nothing under it lies, so its NOT_RENDERED fill is no fill: it is its line, all round.
    It was the thumbnail's picture of its whole box, the diagram under it and all, and that crop
    painted the rim under the caption's box out as the caption's letters: a ring with a gap."""
    from PIL import Image, ImageDraw
    green, red = (0, 136, 43), (200, 37, 60)
    a = page((230, 158, 170, 4, "#0063c0"),                 # a wire, under the ring
             (150, 130, 60, 60, "#fde29a"), (172, 165, 14, 25, "#c0604a"))    # a house, under it too
    Image.fromarray(a[130:190, 150:210].copy()).save(tmp_path / "house.png")
    img = Image.fromarray(a)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([100, 100, 499, 259], radius=25, outline=green, width=3)
    for x in range(170, 320, 8):                            # the caption's red words, inside the ring
        draw.rectangle([x, 238, x + 1, 248], fill=red)
    house = {"kind": "image", "role": "figure", "bbox": [150, 130, 210, 190], "file": str(tmp_path / "house.png"),
             "id": "h", "object": "h", "group": None}
    wire = {"kind": "shape", "role": "panel", "shape_type": "RECTANGLE", "bbox": [230, 158, 400, 162],
            "fill": "#0063c0", "outline": None, "id": "w", "object": "w", "group": None}
    ring = {"kind": "shape", "role": "panel", "shape_type": "CUSTOM", "bbox": [100, 100, 500, 260], "fill": None,
            "outline": "#00882b", "weight": 3.0, "fill_unread": True, "_not_rendered": True, "id": "r",
            "object": "r", "group": None}
    caption = {"kind": "text", "role": "body", "bbox": [160, 232, 340, 290], "id": "t", "object": "t",
               "paragraphs": [{"runs": [{"text": "local network", "color": "#c8253c"}]},
                              {"runs": [{"text": "wide area network", "color": "#00882b"}]}]}
    got = deck_fills.settle([house, wire, ring, caption], np.asarray(img), 1.0, "#ffffff", False, tmp_path)
    r = next(e for e in got if e["id"] == "r")
    assert r["kind"] == "shape" and r.get("trace"), "its line, not a picture of its box"
    ink = drawn(r["trace"]["rings"])
    assert ink[256:260, 170:330].any(axis=0).all(), "closed under the caption's box"
    assert ink[100:104, 150:450].any(axis=0).all() and not ink[130:230, 130:470].any(), "a line, holding nothing"
    assert [e["id"] for e in got if e["kind"] == "image"] == ["h"]


def test_a_ring_an_opaque_bar_crosses_still_holds_what_it_holds(tmp_path):
    """cs161-net 13: a wire drawn above a ring hides it where it crosses, and the trace takes in only
    3 px of what it hides - a pixel's gap left in the ring opened it, and a ring that holds nothing
    is no ring: taken for a line. Round a fill no reading explains, over a picture, the fill was
    lost. The ring goes on under what hides it, and stays the thumbnail's picture."""
    from PIL import Image, ImageDraw
    ramp = np.linspace(0, 1, 400)[None, :, None]
    a = page()
    a[50:350, 100:500] = np.round((1 - ramp) * [30, 40, 120] + ramp * [120, 30, 60]).astype(np.uint8)
    Image.fromarray(a[50:350, 100:500].copy()).save(tmp_path / "photo.png")
    striped(a, 200, 100, 200, 100, (240, 192, 32), (250, 180, 200))
    img = Image.fromarray(a)
    draw = ImageDraw.Draw(img)
    draw.ellipse([200, 100, 399, 199], outline=TEAL, width=2)
    draw.rectangle([380, 145, 419, 154], fill=(68, 68, 68))   # the bar, above the ring
    under = {"kind": "image", "role": "figure", "bbox": [100, 50, 500, 350], "file": str(tmp_path / "photo.png"),
             "id": "p", "object": "p", "group": None}
    ring = {"kind": "shape", "role": "panel", "shape_type": "CUSTOM", "bbox": [200, 100, 400, 200], "fill": None,
            "outline": "#33b7bf", "weight": 2.0, "fill_unread": True, "_not_rendered": True, "id": "r",
            "object": "r", "group": None}
    bar = {"kind": "shape", "role": "panel", "shape_type": "RECTANGLE", "bbox": [380, 145, 420, 155],
           "fill": "#444444", "outline": None, "id": "b", "object": "b", "group": None}
    got = deck_fills.settle([under, ring, bar], np.asarray(img), 1.0, None, True, tmp_path)
    r = next(e for e in got if e["id"] == "r")
    assert r["kind"] == "image" and not r.get("trace"), "the ring's picture, not an open line"


def test_a_crop_keeps_its_own_outline_where_words_of_another_colour_lie(tmp_path):
    """cs161-net 10: a red frame round a box of grey words, its crop taken from the thumbnail. A
    pixel nearer the words' grey than their white ground was taken for a letter and painted out of
    the crop, rims and all, which left the frame with gaps wherever a text box lay on it."""
    from PIL import Image
    a = page()
    a[100:103, 100:200:4] = 34                           # grey letters
    a[104:106, 90:210] = (200, 30, 50)                   # the frame's rim, across their box, 2 px below
    words = {"kind": "text", "role": "body", "bbox": [95, 95, 205, 117], "id": "t", "object": "t",
             "paragraphs": [{"runs": [{"text": "words", "color": "#222222"}]}]}
    frame = {"kind": "shape", "role": "panel", "shape_type": "CUSTOM", "bbox": [80, 60, 220, 140],
             "outline": "#c81e32", "id": "f", "object": "f", "group": None}
    pic = deck_fills.thumbnail_picture(a, frame, [words], 1.0, None, tmp_path)
    im = np.asarray(Image.open(pic["file"]).convert("RGB")).astype(int)
    assert np.abs(im[44:46, 20:120] - (200, 30, 50)).max() <= 1, "the frame is no letter"
    assert np.abs(im[40:43, 20:120:4] - 34).min() > 100, "the letters are painted out"


def test_a_presets_outline_is_drawn_above_its_picture(tmp_path):
    """A preset whose `{}` fill is a picture keeps its outline, drawn above the thumbnail's picture of
    it: the preset's geometry is known, unlike a freeform's."""
    a = page()
    noisy(a, 200, 100, 200, 100, oval=True)
    pres = deck(outlined(shape("o", "ELLIPSE", 200, 100, 200, 100, UNREAD)))
    els = deck_ir(pres, foreign=True, thumbnails=lambda n: a, images=tmp_path)["slides"][0]["elements"]
    assert [e["kind"] for e in els] == ["image", "shape"]
    assert els[1]["outline"] == "#33b7bf" and not els[1].get("fill")


def test_a_picture_google_shows_round_is_masked_to_its_ellipse(tmp_path):
    """yc-seed-white slide 10: a .pptx picture with an ellipse geometry comes through the API as a
    plain picture; its thumbnail shows it only inside the ellipse, the page in the corners."""
    from PIL import Image
    from beamer2slides.deck_thumbs import thumbnail_picture_masks
    yy, xx = np.mgrid[0:100, 0:100]
    photo = np.dstack([xx * 2.5, yy * 2.5, 128 + 100 * np.sin(xx / 10.0)])      # smooth, as a photo is
    file = tmp_path / "p.png"
    Image.fromarray(photo.astype(np.uint8)).save(file)
    src = np.asarray(Image.open(file).convert("RGB"))

    def shown(oval):
        a = page()
        patch = a[100:200, 200:300]
        if oval:
            yy, xx = np.mgrid[0:100, 0:100]
            keep = ((xx + 0.5 - 50) / 50) ** 2 + ((yy + 0.5 - 50) / 50) ** 2 <= 1
            patch[keep] = src[keep]
        else:
            patch[:] = src
        el = {"kind": "image", "bbox": [200, 100, 300, 200], "file": str(file), "id": "p"}
        thumbnail_picture_masks([el], a, 1.0)
        return el.get("mask")

    assert shown(True) == "ellipse"
    assert shown(False) is None, "a picture shown whole is a rectangle"


def test_a_see_through_text_box_on_a_photo_takes_no_picture_of_it(tmp_path):
    """china-pptx 183: a full-width NOT_RENDERED text box over the Qing gate's photo shows the photo
    running on across its edges, so its fill is nothing; baked, it smeared the words' band. The dark
    pines along its edges are nearer the words' black than the sky is, so the seam test must not take
    them all for letters and be left with no side to judge."""
    yy, xx = np.mgrid[0:405, 0:720].astype(float)
    wave = np.sin(xx / 6 + yy / 5)
    a = np.dstack([150 + 20 * wave, 190 + 20 * wave, 230 + 15 * wave])                 # sky
    pines = ((yy >= 80) & (yy < 112)) | ((yy >= 148) & (yy < 180))
    a[pines] = np.dstack([25 + 15 * wave, 45 + 15 * wave, 25 + 15 * wave])[pines]
    a = a.round().astype(np.uint8)
    a[125:135, 200:520] = 0                                                              # its words
    words = {"kind": "text", "role": "body", "shape_type": "TEXT_BOX", "bbox": [0, 100, 720, 160],
             "fill": None, "outline": None, "fill_unread": True, "id": "t", "object": "t", "group": None,
             "paragraphs": [{"runs": [{"text": "European spheres", "color": "#000000"}]}]}
    got = deck_fills.settle([dict(words)], a, 1.0, "#ffffff", False, tmp_path)
    assert [e["id"] for e in got] == ["t"], "no `t~fill`"


def test_a_text_boxs_fill_picture_leaves_a_picture_under_it_showing(tmp_path):
    """china-pptx 138: a page-sized text box whose fill is baked took the portrait beside the poem
    into its picture (its black hat painted out as a letter) and drew that over the portrait. Where
    the thumbnail shows a picture under the box as that picture is, the fill picture is clear."""
    from PIL import Image
    yy, xx = np.mgrid[0:80, 0:80]
    photo = np.dstack([xx * 3, yy * 3, 128 + 100 * np.sin(xx / 10.0)]).astype(np.uint8)
    photo[5:20, 20:60] = 0                                                              # the hat
    file = tmp_path / "portrait.png"
    Image.fromarray(photo).save(file)
    rng = np.random.default_rng(2)
    a = page()
    a[90:310, 90:310] = rng.integers(0, 256, (220, 220, 3))                        # its panel, edge to edge
    a[150:160, 120:200] = 0                                                            # the poem
    a[200:280, 210:290] = photo
    portrait = {"kind": "image", "bbox": [210, 200, 290, 280], "file": str(file), "id": "p", "object": "p"}
    poem = {"kind": "text", "role": "body", "shape_type": "TEXT_BOX", "bbox": [90, 90, 310, 310],
            "fill": None, "outline": None, "fill_unread": True, "id": "t", "object": "t", "group": None,
            "paragraphs": [{"runs": [{"text": "A cup of wine", "color": "#000000"}]}]}
    got = deck_fills.settle([dict(portrait), dict(poem)], a, 1.0, "#ffffff", False, tmp_path)
    pic = next(e for e in got if e["id"] == "t~fill")
    im = np.asarray(Image.open(pic["file"]))
    assert (im[110:190, 120:200, 3] == 0).mean() > 0.95, "the portrait shows through"
    assert (im[117:128, 142:178, 3] == 0).all(), "the hat too, not painted out as a letter"
    assert (im[20:40, 20:100, 3] == 255).all(), "the rest is the fill"


def test_pull_writes_a_round_picture_clipped_and_outlined_round():
    from types import SimpleNamespace
    from beamer2slides.inverse import picture_edits, picture_latex
    te = {"bbox": [10, 20, 110, 80], "mask": "ellipse", "outline": {"color": "#000000", "weight": 2.0}}
    assert picture_edits(te)
    tex = picture_latex(te, SimpleNamespace(natural=(50, 30), rel="p.png"), Context())
    assert "\\clip (0pt,0pt) ellipse [x radius=50.00pt,y radius=30.00pt]" in tex, tex
    assert "\\draw[draw=" in tex and tex.count("ellipse [") == 2, tex
