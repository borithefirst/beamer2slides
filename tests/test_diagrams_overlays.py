"""Visual hunt, fixer L: TikZ drawings rebuilt as native diagrams, overlay steps of one frame,
figures against theme furniture - on synthetic pages."""

from beamer2slides.extract import select_overlays

from .test_charts_diagrams import H, W, Page, body_text, elements, lines, rect


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
    assert ln["bend"] == "vh" and ln["from"] == [140, 100] and ln["via"] == [140, 50] and ln["to"] == [60, 50]
    assert (ln["arrow_from"], ln["arrow_to"]) == ("STEALTH_ARROW", None)
    assert element_template_keys(el, 1.0)[-1] == ("BENT_CONNECTOR", 0.0, None)
    reqs = diagram_requests(el, "s", "d", 1.0, FontMapper(), template=lambda key: {"id": "tpl", "w": 100, "h": 100})
    move = next(r["updatePageElementTransform"]["transform"] for r in reqs if "updatePageElementTransform" in r)
    assert (move["translateX"], move["translateY"]) == (140 * 12700, 100 * 12700)
    assert (move["scaleX"], move["scaleY"]) == (-0.8, -0.5)
    heads = next(r["updateLineProperties"]["lineProperties"] for r in reqs if "updateLineProperties" in r)
    assert (heads["startArrow"], heads["endArrow"]) == ("STEALTH_ARROW", "NONE")
    ends = [r["updateLineProperties"]["lineProperties"] for r in reqs if "updateLineProperties" in r][-1]
    assert ends["startConnection"]["connectedObjectId"] == "d_n1" and ends["endConnection"]["connectedObjectId"] == "d_n0"
