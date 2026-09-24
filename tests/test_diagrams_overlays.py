"""Visual hunt, fixer L: TikZ drawings rebuilt as native diagrams, overlay steps of one frame,
figures against theme furniture - on synthetic pages."""

import math

from beamer2slides import pdf
from beamer2slides.classify import classify
from beamer2slides.extract import extract_page, select_overlays
from beamer2slides.render import load_png, render_backgrounds

from .test_charts_diagrams import H, W, Page, body_text, deck, elements, lines, rect
from .test_hidden_text import one_page


# ---------------------------------------------------------------- overlay steps

def step(index: int, label: str, title: str, words: str, extra_top: str = "", title_size: float = 14.35) -> dict:
    page = Page(index, label)
    page.text(title, 10, 20, size=title_size)
    if extra_top:
        page.text(extra_top, 200, 40, size=8)  # a label or subtitle inside the top fifth
    page.words(words, 30, 120)
    page.words("A. Author Short title 2026", 30, 250, size=6)
    return page.raw()


def kept(*pages: dict) -> list[int]:
    raw = {"version": 1, "source": {}, "pages": list(pages)}
    return [p["index"] for p in select_overlays(raw, "last")["pages"]]


def test_a_label_drawn_in_the_top_band_on_one_step_does_not_split_the_frame():
    # (Dijkstra: a distance label over a top vertex changes from step to step)
    words = "vertices s a b c t are relaxed in order of distance"
    assert kept(step(0, "4", "Dijkstra", words, "d = inf"), step(1, "4", "Dijkstra", words, "d = 3"),
                step(2, "4", "Dijkstra", words + " done", "d = 8")) == [2]


def test_a_framesubtitle_that_changes_per_step_does_not_split_the_frame():
    a = step(0, "3", "Regular expressions", "identifiers letters digits", "Identifiers", title_size=14.35)
    b = step(1, "3", "Regular expressions", "identifiers letters digits numbers", "Numbers")
    assert kept(a, b) == [1]


def test_a_step_that_swaps_most_of_its_words_under_the_same_title_is_the_same_frame():
    a = step(0, "4", "Three noise regimes", "Read noise dominated at low flux the variance is constant")
    b = step(1, "4", "Three noise regimes", "Shot noise grows with the signal at low flux Poisson applies")
    assert kept(a, b) == [1]


def test_a_title_changed_by_only_keeps_the_frame_when_nearly_everything_else_stays():
    words = "Which of these sorts is stable merge quick heap insertion selection"
    assert kept(step(0, "10", "Quiz", words), step(1, "10", "Quiz: answer", words)) == [1]


def test_uncounted_frames_sharing_a_number_stay_apart():
    title = step(0, "1", "", "Fatigue of welded joints Maria Lindqvist University")
    outline = step(1, "1", "Outline", "Motivation Method Results Conclusion")
    assert kept(title, outline) == [0, 1]
    # the pages of a frame with allowframebreaks: beamer's continuation text on the second
    a = step(0, "9", "References", "Smith 2019 Deep sets Jones 2020 Graph networks Brown 2021")
    b = step(1, "9", "References (cont.)", "Kipf 2017 Semi supervised Velickovic 2018 attention Xu 2019")
    assert kept(a, b) == [0, 1]


# ---------------------------------------------------------------- diagrams

def diagram_of(p) -> dict:
    return next(e for e in elements(p) if e["kind"] == "diagram")


def node_texts(diagram: dict) -> list[str]:
    return ["".join(r["text"] for par in n["paragraphs"] for r in par) for n in diagram["nodes"] if n["paragraphs"]]


def test_edge_labels_whose_baselines_round_apart_stay_two_labels():
    """Two arrows' labels on one line, their baselines a fifth of a point apart: sorted by the
    rounded baseline the right one came first, and the one-sided gap joined the left one to it
    across the node between ("connect SYN+ACK")."""
    p = Page()
    for x0, x1, name in ((40.8, 108.8, "CLOSED"), (143.7, 211.7, "SYN_SENT"), (246.5, 318.4, "ESTABLISHED")):
        p.draw(rect(x0, 83.8, x1, 106.5), type="s", stroke="#000000", width=0.4)
        p.text(name, x0 + 6, 98, 7.97)
    p.draw(lines((109.2, 95.1), (142.1, 95.1)), type="s", stroke="#000000", width=0.8)
    p.draw(lines((212.1, 95.1), (245.0, 95.1)), type="s", stroke="#000000", width=0.8)
    p.text("connect", 113.0, 90.6, 5.98)
    p.text("SYN+ACK", 216.0, 90.4, 5.98)
    body_text(p)
    labels = node_texts(diagram_of(p))
    assert "connect" in labels and "SYN+ACK" in labels, labels


def pipeline(p: Page) -> list[dict]:
    """Two boxes and an arrow between them: a native diagram when nothing is see-through."""
    boxes = [p.draw(rect(x0, 70, x0 + 62, 100), type="fs", fill="#ffffff", stroke="#23373b", width=0.8)
             for x0 in (62, 150)]
    p.text("Raw", 80, 88, 7.97)
    p.text("Model", 165, 88, 7.97)
    arrow = p.draw(lines((124, 85), (150, 85)), type="s", stroke="#23373b", width=0.8)
    body_text(p)
    return boxes + [arrow]


def test_see_through_nodes_and_lines_keep_a_diagram_a_picture():
    """opacity=0.3 on the steps still to come: native shapes came out opaque."""
    p = Page()
    pipeline(p)
    assert any(e["kind"] == "diagram" for e in elements(p))
    for key, value in (("fill_opacity", 0.302), ("stroke_opacity", 0.302), ("soft_mask", True)):
        p = Page()
        pipeline(p)[1 if key == "fill_opacity" else 2][key] = value
        els = elements(p)
        assert not any(e["kind"] == "diagram" for e in els) and any(e["kind"] == "image" for e in els), key


def test_a_qr_code_is_one_picture_not_hundreds_of_rules():
    """426 black modules of a QR code became 426 native rectangles, the code no longer scanned
    as one piece and cost a request each."""
    p = Page()
    for i in range(12):
        for j in range(12):
            if (i + 2 * j) % 3:
                p.draw(rect(300 + 4 * j, 80 + 4 * i, 304 + 4 * j, 84 + 4 * i), fill="#000000")
    body_text(p)
    els = elements(p)
    assert not any(e.get("role") == "rule" for e in els) and any(e["kind"] == "image" for e in els)
    p = Page()  # a few swatches stay shapes
    for j in range(4):
        p.draw(rect(300 + 12 * j, 80, 310 + 12 * j, 90), fill="#1f77b4")
    body_text(p)
    assert not any(e["kind"] == "image" for e in elements(p))


def quadrants(p: Page, dividers_first: bool) -> None:
    """A 2x2 priority matrix: four tinted quadrants, grey dividers on their shared edges."""
    def dividers():
        p.draw(lines((227.85, 45.09), (227.85, 215.17)), type="s", stroke="#808080", width=0.4)
        p.draw(lines((100.29, 130.13), (355.41, 130.13)), type="s", stroke="#808080", width=0.4)
    if dividers_first:
        dividers()
    for (x0, y0, x1, y1), fill in (((100.29, 45.09, 227.85, 130.13), "#dff1df"), ((227.85, 45.09, 355.41, 130.13), "#e4eff6"),
                                   ((100.29, 130.13, 227.85, 215.17), "#f0f0f0"), ((227.85, 130.13, 355.41, 215.17), "#fae5e5")):
        p.draw(lines((x0, y0), (x1, y0), (x1, y1), (x0, y1), closed=True), fill=fill)
    if not dividers_first:
        dividers()
    p.words("Quick wins", 137, 60)
    p.words("Major projects", 255, 60)


def test_a_panel_under_lines_drawn_after_it_stays_in_the_background():
    """The matrix's dividers run on the quadrants' shared edges, drawn after them: as native
    panels the quadrants hid them (half each)."""
    p = Page()
    quadrants(p, dividers_first=False)
    assert not any(e["kind"] == "shape" for e in elements(p))
    p = Page()
    quadrants(p, dividers_first=True)  # drawn before: under the fills in the PDF too
    assert sum(e["kind"] == "shape" for e in elements(p)) == 4


def test_bars_of_an_xbar_chart_are_part_of_its_picture():
    """Three series of an xbar chart, one rectangle per bar, the long ones wider than a quarter
    of the page: they were panels, cut off the chart's picture and set on top of it."""
    p = Page()
    p.draw(lines((139.44, 98.3), (139.44, 172.0)), type="s", stroke="#000000", width=0.4)  # the y axis, no x axis
    for y0, x1 in ((101.1, 303.1), (104.7, 295.4), (108.2, 299.2), (157.8, 233.0)):
        p.draw(rect(139.4, y0, x1, y0 + 3.55), fill="#1f77b4")
    p.words("Food", 110, 108)
    p.words("Rent", 110, 161)
    body_text(p)
    slide = deck(p)["slides"][0]
    els = slide["elements"]
    assert not slide["panels"] and not any(e["kind"] == "shape" for e in els), [e["kind"] for e in els]
    fig = next(e for e in els if e["kind"] in ("image", "diagram"))  # (a picture in a real chart, with its ticks)
    assert fig["bbox"][2] >= 303 and fig["bbox"][3] >= 161
    p = Page()  # a code listing's frame: its top and bottom strips are no bar series
    p.draw(rect(12.91, 71.1, 252.43, 74.49), fill="#f5f5f5")
    p.draw(lines((13.11, 71.1), (13.11, 74.49)), type="s", stroke="#b3b3b3", width=0.4)
    p.draw(rect(12.91, 184.08, 252.43, 187.47), fill="#f5f5f5")
    p.draw(lines((13.11, 184.08), (13.11, 187.47)), type="s", stroke="#b3b3b3", width=0.4)
    body_text(p)
    assert len(deck(p)["slides"][0]["panels"]) == 2


def test_a_box_of_a_footline_of_boxes_is_theme_not_a_figure():
    """author | title | date | page: the date's box, too narrow to be theme artwork alone,
    was a figure and the date a label baked into the background."""
    p = Page()
    for (x0, x1), fill, text in (((0, 150), "#001f5c", "J. Miller"), ((150, 317.48), "#00307f", "Fatigue of welded joints"),
                                 ((317.48, 399.11), "#002866", "12 Nov 2026"), ((399.11, W), "#001f5c", "5 / 9")):
        p.draw(rect(x0, 246.09, x1, H), fill=fill)
        p.words(text, x0 + 6, 252.3, size=5.98)
    body_text(p, 120)
    slide = deck(p)["slides"][0]
    assert not any(e["kind"] in ("image", "shape") for e in slide["elements"]), [e["kind"] for e in slide["elements"]]
    assert not any(b["reason"] == "figure" for b in slide["left_in_background"]), slide["left_in_background"]


def chart_over_footline(band_first: bool) -> dict:
    p = Page()
    footline = lambda: [p.draw(rect(x0, 246.48, x1, H), fill=fill) for x0, x1, fill in
                        ((0, 151.18, "#a30000"), (151.18, 302.36, "#ececec"), (302.36, W, "#d9d9d9"))]
    if band_first:
        footline()
    p.draw(lines((111.1, 42.8), (111.1, 235.4), (390.1, 235.4)), type="s", stroke="#000000", width=0.4)
    p.draw(lines((115, 60), (200, 150), (380, 220)), type="s", stroke="#0000ff", width=0.8)
    p.words("10", 126.6, 248.5, size=10.9)  # an x tick label reaching under the band
    p.words("10", 250.6, 248.5, size=10.9)
    if not band_first:
        footline()
    body_text(p, 30)
    return next(e for e in deck(p)["slides"][0]["elements"] if e["kind"] == "image")


def test_a_figure_ends_where_a_footline_drawn_after_it_begins():
    """The chart's tick labels reach under the footline, which the PDF draws over them: the
    picture reached into the band and showed its colours over the footline's texts."""
    assert chart_over_footline(band_first=False)["bbox"][3] == 246.48
    assert chart_over_footline(band_first=True)["bbox"][3] > 250  # the chart is drawn over it


def test_a_logo_in_the_sidebar_corner_is_not_the_first_word_of_the_title():
    """Berkeley with a \\logo of text: the logo's word on the corner square joined the frame title
    in the headline beside it ('UofT  Sensor network'), which then started at the sidebar."""
    p = Page()
    p.draw(rect(0, 44.83, 44.83, 246.75), fill="#3333b3")  # the sidebar
    p.draw(rect(0, 0, W, 44.83), fill="#adade0")         # the headline
    p.draw(rect(0, 0, 44.83, 44.83), fill="#8585d1")     # their corner, the logo on it
    p.draw(rect(7.55, 15.97, 37.28, 28.86), fill="#ffffff")
    p.text("UofT", 10.54, 26.0, size=9.96, color="#000080")
    p.words("Sensor network", 53.34, 26.0, size=14.35)
    body_text(p, 120)
    titles = [e for e in elements(p) if e.get("role") == "title"]
    assert [" ".join("".join(r["text"] for r in par["runs"]) for par in t["paragraphs"]) for t in titles] == ["Sensor network"]


def test_a_logo_beside_the_last_line_is_no_hole_in_it():
    """Darmstadt's \\logo of words in the bottom-right corner, right of the last references
    line: it became a hole at that line's end, and the box as wide as the logo's right edge."""
    p = Page()
    body_text(p, 120)
    p.words("[8] N. Entezari, S. A. Al-Sayouri and E. E. Papalexakis. All you", 28.35, 232, size=7.97)
    end = p.words("need is low (rank): defending against adversarial attacks. WSDM, 2020.", 42.49, 241.5, size=7.97)
    p.draw(rect(end + 10, 234.2, end + 25, 241.7), fill="#cc0000")
    p.text("ETH", end + 12, 240.7, size=4.98, color="#ffffff")
    els = elements(p)
    assert not any(e["kind"] == "image" for e in els), [(e["kind"], e.get("role")) for e in els]
    last = next(e for e in els if e["kind"] == "text" and "2020." in str(e["paragraphs"]))
    assert last["bbox"][2] <= end + 1


def rounded(x0, y0, x1, y1, r) -> list:
    k = 0.448 * r
    return [["l", [[x0 + r, y0], [x1 - r, y0]]], ["c", [[x1 - r, y0], [x1 - r + k, y0], [x1, y0 + r - k], [x1, y0 + r]]],
            ["l", [[x1, y0 + r], [x1, y1 - r]]], ["c", [[x1, y1 - r], [x1, y1 - r + k], [x1 - r + k, y1], [x1 - r, y1]]],
            ["l", [[x1 - r, y1], [x0 + r, y1]]], ["c", [[x0 + r, y1], [x0 + r - k, y1], [x0, y1 - r + k], [x0, y1 - r]]],
            ["l", [[x0, y1 - r], [x0, y0 + r]]], ["c", [[x0, y0 + r], [x0, y0 + r - k], [x0 + r - k, y0], [x0 + r, y0]]]]


def test_rounded_nodes_keep_their_corner_radius():
    """rounded corners=3pt: the node got Slides' default rounding (a sixth of its short side)."""
    from beamer2slides.emit import FontMapper, diagram_requests, element_template_keys

    p = Page()
    for x0, name in ((40, "Cohort"), (160, "Treatment arm")):  # the second too wide to go inside
        p.draw(rounded(x0, 80, x0 + 60, 110, 3), type="fs", fill="#eeeeee", stroke="#000000", width=0.4)["corners"] = \
            {"tl": 3.0, "tr": 3.0, "bl": 3.0, "br": 3.0}
        p.text(name, x0 + 3, 98, 7.97)
    p.draw(lines((100, 95), (160, 95)), type="s", stroke="#000000", width=0.4)
    body_text(p)
    el = diagram_of(p)
    assert [n.get("radius") for n in el["nodes"] if n["shape"]] == [3.0, 3.0]
    assert element_template_keys(el, 1.0) == [("ROUND_RECTANGLE", 0.1, None)] * 2
    asked = []
    reqs = diagram_requests(el, "s", "d", 1.0, FontMapper(),
                            template=lambda key: asked.append(key) or {"id": "tpl", "w": 100, "h": 100})
    assert asked == [("ROUND_RECTANGLE", 0.1, None)] * 2
    made = [r["createShape"]["shapeType"] for r in reqs if "createShape" in r]
    assert made and set(made) == {"TEXT_BOX"}, made  # labels in their own boxes; both nodes copied


def test_a_horizontal_first_elbow_is_written_from_its_other_end():
    """-| as bentConnector3 at adj 1 came out with its turn halfway (three segments); the adj 0
    template draws |- right, so a -| elbow is that one run backwards, its head at its start."""
    from beamer2slides.emit import FontMapper, diagram_requests, element_template_keys

    p = Page()
    for x0, y0, name in ((20, 40, "Lexer"), (120, 100, "Parser")):
        p.draw(rect(x0, y0, x0 + 40, y0 + 20), type="s", stroke="#000000", width=0.4)
        p.text(name, x0 + 8, y0 + 13, 7.97)
    p.draw(lines((60, 50), (140, 50), (140, 97)), type="s", stroke="#000000", width=0.4)  # -| into Parser's top
    p.draw(lines((137.5, 96), (140, 100), (142.5, 96), (140, 97), closed=True), type="fs", fill="#000000",
           stroke="#000000", width=0.4)
    body_text(p)
    el = diagram_of(p)
    (ln,) = el["lines"]
    # (the head's tip, where its 0.4 pt mitred outline ends: 0.38 pt past its path)
    assert ln["bend"] == "vh" and ln["from"] == [140, 100.38] and ln["via"] == [140, 50] and ln["to"] == [60, 50]
    assert (ln["arrow_from"], ln["arrow_to"]) == ("STEALTH_ARROW", None)
    assert element_template_keys(el, 1.0)[-1] == ("BENT_CONNECTOR", 0.0, None)
    reqs = diagram_requests(el, "s", "d", 1.0, FontMapper(), template=lambda key: {"id": "tpl", "w": 100, "h": 100})
    move = next(r["updatePageElementTransform"]["transform"] for r in reqs if "updatePageElementTransform" in r)
    assert (move["translateX"], move["translateY"]) == (140 * 12700, round(100.38 * 12700))
    assert move["scaleX"] == -0.8 and abs(move["scaleY"] + 0.5038) < 1e-6
    heads = next(r["updateLineProperties"]["lineProperties"] for r in reqs if "updateLineProperties" in r)
    assert (heads["startArrow"], heads["endArrow"]) == ("STEALTH_ARROW", "NONE")
    ends = [r["updateLineProperties"]["lineProperties"] for r in reqs if "updateLineProperties" in r][-1]
    assert ends["startConnection"]["connectedObjectId"] == "d_n1" and ends["endConnection"]["connectedObjectId"] == "d_n0"


def test_a_turned_stamp_keeps_its_letters_where_it_crosses_native_words(tmp_path):
    """A pink DRAFT turned 25 degrees over the bullets (tikz overlay): a turned glyph's box is far
    larger than its ink, and the words' x-height bands switched off the F and T it met."""
    c, s = math.cos(math.radians(25)), math.sin(math.radians(25))
    content = (b"BT /F1 12 Tf 20 150 Td (Native words run across the page under a stamp) Tj ET\n"
               b"BT /F1 12 Tf 20 120 Td (A second line of native words to make a paragraph) Tj ET\n"
               b"BT 1 0.4 0.6 rg /F1 48 Tf %.4f %.4f %.4f %.4f 90 25 Tm (DRAFT) Tj ET\n" % (c, s, -s, c))
    path = tmp_path / "stamp.pdf"
    path.write_bytes(one_page(content))
    raw = {"version": 1, "source": {"pdf": str(path), "producer": "", "pages": 1, "title": ""}, "pages": []}
    doc = pdf.Document(path)
    try:
        raw["pages"] = [extract_page(doc[0], "1")]
        raw["pages"][0]["frame_label"] = None
    finally:
        doc.close()
    deck = classify(raw)
    assert [e["kind"] for e in deck["slides"][0]["elements"]] == ["text"]  # both lines native, the stamp not
    [png] = render_backgrounds(path, raw, deck, tmp_path / "bg")
    bg = load_png(png)
    doc = pdf.Document(path)
    try:
        original = doc[0].render(bg.shape[1] / 400)
    finally:
        doc.close()
    pink = lambda im: int(((im[..., 0] > 200) & (im[..., 1] < 150) & (im[..., 2] > 100) & (im[..., 2] < 200)).sum())
    assert pink(bg) >= 0.99 * pink(original)
