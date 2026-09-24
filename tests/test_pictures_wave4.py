"""Visual hunt, wave 4 fixer P: pictures, bullets and diagrams - on synthetic pages."""

import numpy as np
from PIL import Image

from beamer2slides.render import render_backgrounds

from .test_hidden_text import one_page
from .test_pictures_hunt import raw_of


def ball_under_a_figure_edge(tmp_path):
    path = tmp_path / "bullets.pdf"
    # the ball bullet an image, as beamer's shaded balls come back
    content = (b"0 0.45 0.7 rg 20 60 100 100 re f\n"
               b"q 6 0 0 6 118 54 cm BI /W 1 /H 1 /CS /RGB /BPC 8 ID \x33\x33\xb3 EI Q\n"
               b"BT /F1 11 Tf 128 54 Td (Item words) Tj ET\n")
    path.write_bytes(one_page(content))
    raw = raw_of(path)
    page = raw["pages"][0]
    (image,) = page["images"]
    words = page["spans"]
    bullet = {"kind": "image", "image": image["id"], "bbox": image["bbox"]}
    text = {"id": "t0", "kind": "text", "role": "body", "bbox": [128, 136, 200, 150], "spans": [s["id"] for s in words],
            "paragraphs": [{"bullet": bullet, "runs": [], "lines": []}]}
    figure = {"id": "f0", "kind": "image", "role": "figure", "bbox": [19, 39, 123.2, 147], "spans": []}
    deck = {"slides": [{"page": 0, "size": [400, 200], "elements": [figure, text], "on_layout": []}]}
    render_backgrounds(path, raw, deck, tmp_path / "out")
    return deck["slides"][0], image, bullet


def test_a_ball_bullet_a_figure_box_reaches_keeps_its_colour(tmp_path):
    """r3_charts_v3 s8 (r8 regression): the pie's box, widened to its pin label, reached half over
    the list's balls; the figure's removal took the half of each ball inside it from the
    background, the colour read from what was left found no ball, and the Slides bullets came
    out in each item's first word's colour (blue, orange, black) where the PDF has navy balls."""
    slide, image, bullet = ball_under_a_figure_edge(tmp_path)
    assert image["bbox"][0] < 123.2 < image["bbox"][2]  # the figure box ends inside the ball: the case
    assert bullet.get("color"), bullet
    r, g, b = (int(bullet["color"][k:k + 2], 16) for k in (1, 3, 5))
    assert abs(r - 0x33) < 24 and abs(g - 0x33) < 24 and abs(b - 0xb3) < 24


def test_a_ball_bullet_a_figure_box_reaches_leaves_the_background(tmp_path):
    """The same ball is patched out of the background whole: no half ball beside the Slides one."""
    slide, image, _ = ball_under_a_figure_edge(tmp_path)
    bg = np.array(Image.open(tmp_path / "out" / slide["background"]).convert("RGB")).astype(int)
    zoom = bg.shape[1] / 400
    x0, y0, x1, y1 = (int(round(v * zoom)) for v in image["bbox"])
    area = bg[y0:y1, x0:x1]
    ball = (area[..., 0] < 90) & (area[..., 1] < 80) & (area[..., 2] > 150)
    assert not ball.any()


def test_every_numbered_hebrew_ball_keeps_its_number():
    """r3_scripts_rtl s4 (scripts-18): item 1's ball came out a plain dot. Its digit hangs right of
    the item's words more than a word space, the digits below it on the same edge, and
    `figure_label_apart` took the Hebrew words (next to the ball's image) for a figure label and
    the digit for a column's first word: the digit went to a line of its own and the ball had no
    number. A lone number is no column of words."""
    from .test_rtl_lists import SIZE, enumerate_page
    from beamer2slides.classify import classify_page

    pg = enumerate_page()
    # (as the deck has them: the 2nd and 3rd balls come out 12 pt, no small images but regions,
    # and the first item's words end near the second's ball)
    for im in pg["images"][1:3]:
        x0, y0, x1, y1 = im["bbox"]
        im["bbox"], im["px"] = [x0, y0 - 0.5, x1, y1 + 0.5], [12, 12]
    slide = classify_page(pg, SIZE)
    numbers = sorted(p["bullet"]["text"] for e in slide["elements"] for p in e.get("paragraphs", [])
                     if p["bullet"] and p["bullet"]["text"])
    assert numbers == ["1", "2", "3", "4"], numbers
    loose = [e for e in slide["elements"] if e["kind"] == "text"
             and "".join(r["text"] for p in e["paragraphs"] for r in p["runs"]).strip().isdigit()]
    assert not loose


def test_an_icon_bullet_picture_takes_its_whole_glyph(tmp_path):
    """r1_design_v3 s1, s6, s10 (design-v8, WORSE in r8): an icon bullet's picture was its
    span's box; FontAwesome under xelatex gives the warning triangle an advance of half its
    ink, and the crop cut off the right half with the '!'. An icon's picture is grown to its
    glyph's ink as a formula's is. (Here the box is the left half of a W.)"""
    content = b"BT /F1 20 Tf 20 150 Td (W) Tj ET\nBT /F1 12 Tf 60 150 Td (Security words) Tj ET\n"
    path = tmp_path / "icon.pdf"
    path.write_bytes(one_page(content))
    raw = raw_of(path)
    glyph, *words = raw["pages"][0]["spans"]
    assert glyph["text"] == "W"
    x0, y0, x1, y1 = glyph["bbox"]
    text = {"id": "t0", "kind": "text", "role": "body", "bbox": [60, 35, 160, 55],
            "spans": [s["id"] for s in words], "paragraphs": []}
    icon = {"id": "u0", "kind": "image", "role": "icon", "anchor": "t0",
            "bbox": [x0, y0, (x0 + x1) / 2, y1], "spans": [glyph["id"]]}
    deck = {"slides": [{"page": 0, "size": [400, 200], "elements": [icon, text], "on_layout": []}]}
    render_backgrounds(path, raw, deck, tmp_path / "out")
    assert icon["bbox"][2] >= x1 - 1.5  # the whole glyph
    crop = np.array(Image.open(tmp_path / "out" / icon["file"]).convert("L")).astype(int)
    right = crop[:, int(crop.shape[1] * 0.75):]
    assert (right < 128).any()  # the right strokes of the W are in the picture


def test_a_line_running_through_a_picture_stays_in_the_background_at_its_edges(tmp_path):
    """r2_code_v2 s6 (r8 regression): a git graph's main line is one path through the branch
    picture's box. Painting the box out of the background took the line one pixel past the box
    edge, and the crop began a pixel later: white nicks in the line either side of the picture.
    The rows a background drawing crosses the box's edge on are left alone (the crop covers
    them; an anchored picture's transparent ground leaves such paths to the background)."""
    content = (b"0.4 0.4 1 RG 2 w 20 100 m 300 100 l S\n"
               b"1 0.6 0.2 rg 140 110 20 20 re f\n")
    path = tmp_path / "graph.pdf"
    path.write_bytes(one_page(content))
    raw = raw_of(path)
    figure = {"id": "f0", "kind": "image", "role": "figure", "bbox": [120, 60, 220, 110], "spans": []}
    deck = {"slides": [{"page": 0, "size": [400, 200], "elements": [figure], "on_layout": []}]}
    render_backgrounds(path, raw, deck, tmp_path / "out")
    bg = np.array(Image.open(tmp_path / "out" / deck["slides"][0]["background"]).convert("RGB")).astype(int)
    zoom = bg.shape[1] / 400
    y = int(round(100 * zoom))  # (PDF y 100 is 100 from the bottom of a 200 pt page)
    for a, b in ((int(120 * zoom) - 1, int(120 * zoom) + 3), (int(220 * zoom) - 2, int(220 * zoom) + 2)):
        px = bg[y - 1:y + 2, a:b]  # just inside the box's edges and the pixel past them: the line is there
        assert ((px[..., 2] - px[..., 0]) > 80).any(axis=0).all(), (a, b)
    # and the rest of the box is painted: the orange square is gone
    sq = bg[int(75 * zoom):int(85 * zoom), int(145 * zoom):int(155 * zoom)]
    assert not ((sq[..., 0] - sq[..., 2]) > 80).any()


def test_a_comma_over_its_listing_frame_edge_stays_on_its_line():
    """r2_code_v2 s4: '"name": "order-service",' is one character too wide for its listing's
    frame: the comma's box starts inside the panel and its centre lies past the edge, so it was
    on no panel, in another colour than the string before it, and became a box of its own that
    Slides set ~70 px right of the word."""
    from .test_charts_diagrams import Page, body_text, deck, rect

    mono = "RobotoMono-Regular"
    p = Page()
    body_text(p)
    p.draw(rect(22.48, 91.56, 173.12, 198.76), fill="#f5f5f0")
    p.text('"name":', 47.64, 116.0, size=8.97, font=mono, w=37.7, color="#1a1a1a")
    p.text('"order-service"', 90.7, 116.0, size=8.97, font=mono, w=80.7, color="#2e7d32")
    p.text(",", 171.37, 116.0, size=8.97, font=mono, w=4.49, color="#1a1a1a")
    p.text('"version":', 47.64, 128.0, size=8.97, font=mono, w=53.8, color="#1a1a1a")
    p.text('"2.3.1",', 106.8, 128.0, size=8.97, font=mono, w=43.0, color="#2e7d32")
    slide = deck(p)["slides"][0]
    lines = ["".join(r["text"] for r in par["runs"]) for e in slide["elements"] if e["kind"] == "text"
             for par in e["paragraphs"]]
    assert any(ln.rstrip().endswith('"order-service",') for ln in lines), lines
    assert "," not in [ln.strip() for ln in lines]


def test_a_label_of_a_row_centred_over_a_callout_stays_with_its_row():
    """r1_design_v1 s5 (design-v17): a timeline's labels 'Week 0 ... Week 6' stood in a row, and
    the one centred over a callout box below it ('Median time to go-live: 5.5 weeks') was taken
    for that box's title and went into its picture, alone of its row: the row's other labels
    stayed text. A label of a row of like labels that stay text is the row's."""
    from .test_charts_diagrams import Page, deck, ellipse, lines, rect

    bold = "LMSans10-Bold"
    p = Page()
    p.draw(lines((64.6, 127.0), (404.7, 127.0)), type="s", fill=None, stroke="#bfbfbf", width=1.99)
    for i, (x, name, week) in enumerate([(46.3, "Kick-off", "0"), (131.4, "Survey", "1"), (220.9, "Install", "2\u20133"),
                                         (306.0, "Pilot", "4"), (388.9, "Go-live", "6")]):
        cx = 64.55 + 85.05 * i
        p.draw(ellipse(cx, 127.05, 12.75, 12.75), fill="#1f3a93")
        p.text(str(i + 1), cx - 3, 130.8, size=10.91, font=bold, w=6.0, color="#ffffff")
        p.text(name, x, 101.8, size=9.96, font=bold, w=5.0 * len(name), color="#262626")
        x0 = cx - (15.5 if len(week) == 1 else 20.55)
        p.text("Week", x0, 159.2, size=9.96, w=22.8, color="#666666")
        p.text(week, x0 + 26.2, 159.2, size=9.96, w=4.9 * len(week), color="#666666")
    p.draw(rect(157.9, 184.2, 311.4, 200.3), type="fs", fill="#fef5e7", stroke="#f39c12", width=0.8)
    x = 161.6
    for word, font in [("Median", None), ("time", None), ("to", None), ("go-live:", None), ("5.5", bold), ("weeks", bold)]:
        kw = {"font": font} if font else {}
        x = p.text(word, x, 194.7, size=9.96, w=4.9 * len(word), color="#262626", **kw)["bbox"][2] + 3.3
    p.words("Body text that sets the size of the deck and more words", 30, 225, size=9.96)
    slide = deck(p)["slides"][0]
    texts = ["".join(r["text"] for par in e["paragraphs"] for r in par["runs"]) for e in slide["elements"]
             if e["kind"] == "text"]
    weeks = sorted(t for t in texts if t.startswith("Week"))
    assert weeks == ["Week 0", "Week 1", "Week 2\u20133", "Week 4", "Week 6"], texts


def test_a_hole_on_a_photo_keeps_the_photo_under_it(tmp_path):
    """r1_design_v2 s1 (design-v5): '|' and icon holes on a full-bleed title photo. The photo's
    part inside each hole box was cut out of the background (white boxes), and the hole pictures
    were opaque crops of the photo: once Slides set the words off the PDF's place, each picture
    showed as a boxed patch and the white box beside it hid a letter of the next word. The photo
    stays whole in the background and the hole's picture is its glyphs on a transparent ground."""
    content = (b"q 400 0 0 200 0 0 cm BI /W 2 /H 1 /CS /RGB /BPC 8 ID \x1a\x2a\x3a\x30\x40\x50 EI Q\n"
               b"1 1 1 rg BT /F1 14 Tf 190 100 Td (W) Tj ET\n"
               b"BT /F1 12 Tf 40 100 Td (Words before) Tj 200 0 Td (after) Tj ET\n")
    path = tmp_path / "photo.pdf"
    path.write_bytes(one_page(content))
    raw = raw_of(path)
    glyph = next(s for s in raw["pages"][0]["spans"] if s["text"] == "W")
    words = [s for s in raw["pages"][0]["spans"] if s is not glyph]
    x0, y0, x1, y1 = glyph["bbox"]
    text = {"id": "t0", "kind": "text", "role": "body", "bbox": [40, 90, 300, 110],
            "spans": [s["id"] for s in words], "paragraphs": []}
    hole = {"id": "h0", "kind": "image", "role": "math", "anchor": "t0", "bbox": [x0 - 1, y0, x1 + 1, y1],
            "spans": [glyph["id"]]}
    deck = {"slides": [{"page": 0, "size": [400, 200], "elements": [hole, text], "on_layout": []}]}
    render_backgrounds(path, raw, deck, tmp_path / "out")
    bg = np.array(Image.open(tmp_path / "out" / deck["slides"][0]["background"]).convert("RGB")).astype(int)
    zoom = bg.shape[1] / 400
    a0, b0, a1, b1 = (int(round(v * zoom)) for v in hole["bbox"])
    under = bg[b0 + 2:b1 - 2, a0 + 2:a1 - 2]
    assert (under.max(axis=2) < 120).all()  # the photo's dark pixels, no white box, no glyph
    crop = np.array(Image.open(tmp_path / "out" / hole["file"]))
    assert crop.shape[2] == 4 and (crop[..., 3] == 0).mean() > 0.2  # the glyph on a clear ground
