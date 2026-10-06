"""Charts and diagrams on synthetic pages: tick rows and axis labels that belong to their
chart, legend boxes, bar series and funnels that are no panels, curves that are no ellipses,
marks that are no bullets, node labels, Venn diagrams, and labels no picture would hold."""

import math
from collections.abc import Sequence

from beamer2slides import classify as C
from beamer2slides import ir
from beamer2slides.classify import Line, PageClassifier, Rect, Span, body_size, classify, new_line, new_span
from beamer2slides.emit import FontMapper, diagram_requests
from beamer2slides.fonts import font_info
from beamer2slides.google_types import slides_json
from beamer2slides.ir import element_json
from beamer2slides.raw_types import DrawingType, PathItem, RawDoc, RawDrawing, RawImage, RawPage, RawSpan

from .json_reads import jnum, jobj, jstr

W, H = 453.54, 255.12
SANS, MONO = "LMSans10-Regular", "LMMono10-Regular"


# ---------------------------------------------------------------- synthetic pages

class Page:
    def __init__(self, index: int = 0, label: str = "1") -> None:
        self.index, self.label = index, label
        self.spans: list[RawSpan] = []
        self.drawings: list[RawDrawing] = []
        self.images: list[RawImage] = []

    def text(self, text: str, x0: float, baseline: float, size: float = 10.91, font: str = SANS,
             w: float | None = None, color: str = "#000000") -> RawSpan:
        w = len(text) * 0.5 * size if w is None else w
        s: RawSpan = {"id": f"p{self.index}s{len(self.spans)}", "text": text, "font": font, "size": size,
                      "color": color, "alpha": 255, "origin": [x0, baseline],
                      "bbox": [x0, baseline - 0.75 * size, x0 + w, baseline + 0.25 * size],
                      "dir": [1.0, -0.0], "smallcaps": False}
        self.spans.append(s)
        return s

    def words(self, text: str, x0: float, baseline: float, size: float = 10.91, font: str = SANS) -> float:
        """A line of words a word space apart, one span each; returns where it ends."""
        for word in text.split():
            x0 = self.text(word, x0, baseline, size, font)["bbox"][2] + 0.33 * size
        return x0

    def draw(self, path: list[PathItem], type: DrawingType = "f", fill: str | None = "#1f77b4",
             stroke: str | None = None, width: float | None = None) -> RawDrawing:
        points = [p for _, pts in path for p in pts]
        d: RawDrawing = {
            "id": f"p{self.index}d{len(self.drawings)}", "type": type, "items": "".join(op for op, _ in path),
            "bbox": [min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)],
            "fill": fill if "f" in type else None, "stroke": stroke if "s" in type else None,
            "width": width if "s" in type else None, "fill_opacity": 1.0, "stroke_opacity": 1.0, "soft_mask": False,
            "corners": {}, "path": path}
        self.drawings.append(d)
        return d

    def raw(self) -> RawPage:
        return {"index": self.index, "label": self.label, "size": [W, H], "spans": self.spans,
                "images": self.images, "drawings": self.drawings, "links": [], "frame_label": None}


def raw_doc(pages: list[RawPage]) -> RawDoc:
    """raw.json of `pages`, from a PDF that has no name, producer or title."""
    return {"version": 1, "source": {"pdf": "", "producer": "", "pages": len(pages), "title": ""}, "pages": pages}


def rect(x0: float, y0: float, x1: float, y1: float) -> list[PathItem]:
    return [("re", [[x0, y0], [x1, y1]])]


Point = tuple[float, float]


def lines(*points: Point, closed: bool = False) -> list[PathItem]:
    pts = list(points) + ([points[0]] if closed else [])
    return [("l", [list(a), list(b)]) for a, b in zip(pts, pts[1:])]


def ellipse(cx: float, cy: float, rx: float, ry: float) -> list[PathItem]:
    k = 0.5523
    e, s, w, n = [cx + rx, cy], [cx, cy + ry], [cx - rx, cy], [cx, cy - ry]
    return [("c", [e, [cx + rx, cy - k * ry], [cx + k * rx, cy - ry], n]),
            ("c", [n, [cx - k * rx, cy - ry], [cx - rx, cy - k * ry], w]),
            ("c", [w, [cx - rx, cy + k * ry], [cx - k * rx, cy + ry], s]),
            ("c", [s, [cx + k * rx, cy + ry], [cx + rx, cy + k * ry], e])]


def body_text(page: Page, y: float = 225) -> None:
    """A sentence of body text, so the deck's body size is the usual 10.9 pt."""
    page.words("Body text that sets the size of the deck", 30, y)


def deck(*pages: Page) -> ir.Deck:
    return classify(raw_doc([p.raw() for p in pages]))


def elements(page: Page) -> list[ir.Element]:
    return deck(page)["slides"][0]["elements"]


def paragraphs_of(e: ir.Element) -> list[ir.Paragraph]:
    """A text element's paragraphs; none for another kind."""
    return e["paragraphs"] if e["kind"] == "text" else []


def run_texts(runs: Sequence[ir.Run]) -> str:
    return "".join(r["text"] for r in runs)


def texts(els: Sequence[ir.Element]) -> list[str]:
    return [" / ".join(run_texts(p["runs"]) for p in e["paragraphs"]) for e in els if e["kind"] == "text"]


def text_elements(els: Sequence[ir.Element]) -> list[ir.TextElement]:
    return [e for e in els if e["kind"] == "text"]


def images(els: Sequence[ir.Element]) -> list[ir.ImageElement]:
    return [e for e in els if e["kind"] == "image"]


def diagrams(els: Sequence[ir.Element]) -> list[ir.DiagramElement]:
    return [e for e in els if e["kind"] == "diagram"]


def span(text: str, x0: float, size: float = 7.97, baseline: float = 180.0, font: str = SANS) -> Span:
    w = len(text) * 0.5 * size
    return new_span(id=f"s{x0:.1f}", text=text, font=font, size=size, color="#000000",
                rect=Rect(x0, baseline - 0.75 * size, x0 + w, baseline + 0.25 * size),
                baseline=baseline, horizontal=True, info=font_info(font), link=None, drawn=False, visual=None)


TICK = 7.97  # a tick label's size (pt)


def row(labels: Sequence[str], x0: float, gap_em: float) -> Line:
    out: list[Span] = []
    for t in labels:
        out.append(span(t, x0, TICK))
        x0 = out[-1].rect.x1 + gap_em * TICK
    return new_line(out)


# ---------------------------------------------------------------- body size

def test_body_size_ignores_the_footline_on_every_frame():
    """A \\tiny footline repeated on every frame (author, title, date) outweighed the body
    text of a deck with little text per slide: body 6 pt, and every size gate followed it."""
    pages = []
    for i in range(6):
        p = Page(i, str(i + 1))
        p.words("Priya Raman (Department of Astronomy) DEEPSKY-5 photometric pipeline Survey team meeting", 20, 250, 5.98)
        p.words(f"Slide {i} says a few words", 30, 80 + i)
        pages.append(p)
    assert body_size(raw_doc([p.raw() for p in pages])) == 10.9


# ---------------------------------------------------------------- tick rows

def test_tick_row_at_one_pitch_with_labels_run_together_at_its_end():
    """pgfplots sets 300 .. 950 0.72 em apart and runs the last ones into one span of
    digits: still a tick row."""
    labels = [str(v) for v in range(300, 1000, 50)] + ["1,0001,0501,100"]
    line = row(labels, 84, 0.72)
    line.spans[-1].rect = Rect(line.spans[-2].rect.x1 + 2.5, line.spans[-1].rect.y0,
                               line.spans[-2].rect.x1 + 2.5 + 56, line.spans[-1].rect.y1)
    assert PageClassifier.tick_row(line)


def test_tick_row_of_month_names_half_an_em_apart():
    assert PageClassifier.tick_row(row(["Jan", "Feb", "Mar", "Apr", "May", "Jun"], 90, 0.55))


def test_tick_row_of_two_dates_an_em_apart():
    assert PageClassifier.tick_row(row(["01/2026", "03/2026"], 95, 1.2))


def test_prose_is_no_tick_row():
    assert not PageClassifier.tick_row(row("the cat sat on the mat today".split(), 90, 0.33))
    assert not PageClassifier.tick_row(row(["Wavelength", "(nm)"], 90, 0.33))


def test_tick_row_under_a_plot_is_part_of_its_picture():
    p = Page()
    p.draw(lines((90, 63), (386, 63), (386, 171), (90, 171), closed=True), type="s", stroke="#000000", width=0.4)
    p.draw(lines((92, 160), (150, 120), (220, 140), (300, 80), (380, 90)), type="s", stroke="#0000cc", width=0.8)
    x = 84.0
    for v in range(300, 1000, 50):
        label = p.text(str(v), x, 180.5, 7.97)["bbox"]
        p.draw(lines(((label[0] + label[2]) / 2, 171), ((label[0] + label[2]) / 2, 168)), type="s", stroke="#000000", width=0.4)
        x = label[2] + 0.72 * 7.97
    body_text(p)
    els = elements(p)
    assert not any("300" in t for t in texts(els)), texts(els)
    fig = next(e for e in images(els) if e["role"] == "figure")
    assert not fig.get("anchor") and fig["bbox"][3] >= 182


# ---------------------------------------------------------------- shapes of paths

def test_upright_ellipse_is_closed_and_meets_its_box_at_the_middles():
    assert C.upright_ellipse(ellipse(100, 100, 30, 20), Rect(70, 80, 130, 120))
    # a sine wave drawn as four curves: open
    wave: list[PathItem] = [
        ("c", [[0, 50], [10, 30], [20, 30], [30, 50]]), ("c", [[30, 50], [40, 70], [50, 70], [60, 50]]),
        ("c", [[60, 50], [70, 30], [80, 30], [90, 50]]), ("c", [[90, 50], [100, 70], [110, 70], [120, 50]])]
    assert not C.upright_ellipse(wave, Rect(0, 35, 120, 65))
    # an ellipse turned 45 degrees: its curve ends are no longer at the box's middles
    turned: list[PathItem] = [(op, [[100 + (x - 100) * math.cos(0.8) - (y - 100) * math.sin(0.8),
                                     100 + (x - 100) * math.sin(0.8) + (y - 100) * math.cos(0.8)] for x, y in pts])
                              for op, pts in ellipse(100, 100, 40, 15)]
    xs = [x for _, pts in turned for x, _ in pts]
    ys = [y for _, pts in turned for _, y in pts]
    assert not C.upright_ellipse(turned, Rect(min(xs), min(ys), max(xs), max(ys)))


def test_box_outline_is_one_axis_aligned_outline():
    p = Page()
    assert C.box_outline(p.draw(rect(10, 10, 300, 40)), Rect(10, 10, 300, 40))
    assert C.box_outline(p.draw(lines((10, 10), (300, 10), (300, 40), (10, 40), closed=True)), Rect(10, 10, 300, 40))
    bars = rect(100, 114, 115, 118) + rect(150, 118, 164, 127) + rect(198, 118, 212, 142)
    assert not C.box_outline(p.draw(bars), Rect(100, 114, 212, 142))
    trapezium = lines((40, 57), (325, 57), (300, 84), (67, 84), closed=True)
    assert not C.box_outline(p.draw(trapezium), Rect(40, 57, 325, 84))


def test_bar_series_and_funnel_steps_are_no_panels():
    p = Page()
    p.draw(rect(100, 114, 115, 118) + rect(150, 118, 164, 127) + rect(198, 118, 212, 142) + rect(246, 118, 260, 159))
    for i, (a, b) in enumerate([(41, 325), (67, 299), (92, 274)]):
        y = 57 + 30 * i
        p.draw(lines((a, y), (b, y), (b - 25, y + 27), (a + 25, y + 27), closed=True), fill="#3366cc")
    body_text(p)
    slide = deck(p)["slides"][0]
    assert not slide["panels"], slide["panels"]
    assert not any(e["kind"] == "shape" and e.get("role") == "panel" for e in slide["elements"])


# ---------------------------------------------------------------- legends

def legend(p: Page) -> None:
    p.draw(rect(170, 204, 320, 221), fill="#ffffff")  # the legend's white box
    for i, name in enumerate(["EMEA", "Americas", "APAC"]):
        x = 176 + 48 * i
        p.draw(rect(x, 208.2, x + 9, 216.9), fill=["#1f77b4", "#ff7f0e", "#2ca02c"][i])
        p.text(name, x + 12, 216, 8.97)


def test_legend_box_is_no_highlight_of_its_words_and_no_panel():
    """A legend's white box around swatches and names read as the names' highlight: the
    background lost the swatches under a native white box."""
    p = Page()
    legend(p)
    body_text(p)
    slide = deck(p)["slides"][0]
    runs = [r for e in slide["elements"] if e["kind"] == "text" for par in e["paragraphs"] for r in par["runs"]]
    assert not any(r.get("highlight") for r in runs)
    assert not any(e["kind"] == "shape" for e in slide["elements"])
    assert not slide["panels"]


# ---------------------------------------------------------------- bullets

def test_dots_on_a_timeline_rail_are_no_bullets():
    p = Page()
    p.draw(lines((65.6, 40), (65.6, 230)), type="s", stroke="#b9bcc5", width=1.5)  # the rail
    for i, item in enumerate(["Battery pairing for panels", "Spain and Portugal launch", "Dynamic tariffs"]):
        y = 84 + 31 * i
        p.draw(ellipse(65.6, y - 4, 4.25, 4.25), fill="#2a9d8f")
        p.words(item, 78, y)
    for e in elements(p):
        for par in paragraphs_of(e):
            assert not par.get("bullet"), par["bullet"]


def test_title_accent_bar_is_no_bullet_and_takes_no_title():
    """A title's accent bar (a tall thin rectangle left of it in the header band) became a
    bullet; read as no bullet, the title next to it was taken for its figure's label and left
    in the background."""
    p = Page()
    p.draw(rect(28, 12, 32, 27), fill="#e63946")
    p.words("Lab setup", 38.5, 24, 11.96)
    body_text(p)
    els = elements(p)
    title = next(e for e in text_elements(els) if "Lab" in texts([e])[0])
    assert not title["paragraphs"][0]["bullet"]


# ---------------------------------------------------------------- labels beside graphics

def test_bar_chart_category_labels_belong_to_the_chart():
    """Category labels set flush right against a horizontal bar chart, some too long for a
    tick label: as text boxes they re-wrapped over the next one."""
    p = Page()
    bars: list[PathItem] = []
    for i, (name, length) in enumerate([("Warehouse picking backlog", 150), ("Carrier handover delayed", 120),
                                        ("Customer not available", 90), ("Other", 30)]):
        y = 60 + 28 * i
        bars += rect(197, y, 197 + length, y + 16)
        p.text(name, 192 - len(name) * 0.5 * 7.97, y + 11, 7.97)
    p.draw(bars, fill="#4c72b0")  # one bar series: one path
    body_text(p)
    els = elements(p)
    assert not any("Warehouse" in t or "Other" in t for t in texts(els)), texts(els)
    fig = next(e for e in els if e["kind"] == "image")
    assert fig["bbox"][0] < 100


def test_timeline_years_beside_tick_marks_stay_text():
    """A year under a timeline's tick mark joined it as a label, but a tick mark and a year
    make a cluster too small for a picture: the year was left in the background."""
    p = Page()
    p.draw(lines((20, 145), (440, 145)), type="s", stroke="#999999", width=1.0)  # the rail (decoration)
    for i, year in enumerate(["2021", "2022", "2023", "2024", "2025"]):
        x = 41 + 74.4 * i
        p.draw(lines((x, 141), (x, 148)), type="s", stroke="#333333", width=0.8)
        p.text(year, x - 9, 157, 8.97)
    body_text(p)
    assert all(y in " ".join(texts(elements(p))) for y in ["2021", "2023", "2025"])


def test_words_beside_a_bare_vertical_rule_stay_text():
    """A column separator or a listing's frame labels no words (its line numbers go with it)."""
    p = Page()
    p.draw(rect(249, 71, 252, 187), fill="#dddddd")
    p.words("What changed?", 260.6, 90, 10.91)
    p.words("Short note", 260.6, 120, 10.91)
    body_text(p)
    assert {"What changed?", "Short note"} <= set(t.strip() for t in texts(elements(p)))


def test_code_beside_its_gutter_stays_text():
    p = Page()
    p.draw(rect(13, 71, 16, 187), fill="#dddddd")  # the gutter
    for i, code in enumerate(["from functools import lru_cache", "", "def fib(n):", "if n < 2:"]):
        y = 82 + 11 * i
        p.text(str(i + 1), 17.3, y, 5.98)
        if code:
            p.words(code, 31 + (22 if code.startswith("if") else 0), y, 8.97, MONO)
    body_text(p)
    joined = " ".join(texts(elements(p)))
    assert "def" in joined and "fib(n):" in joined and "lru_cache" in joined, joined


# ---------------------------------------------------------------- diagrams

def test_redrawn_node_label_goes_to_the_copy_on_top():
    """\\node<2->[fill=yellow] redraws a node on the next overlay step, label and all: the
    label went to the unfilled first copy, doubled, under an empty filled box."""
    p = Page()
    for fill in (None, "#ffff00"):
        p.draw(rect(80, 64, 140, 84), type="s" if fill is None else "fs", fill=fill, stroke="#000000", width=0.4)
        p.text("Lexer", 97, 77, 8.97)
    p.draw(rect(200, 64, 260, 84), type="s", stroke="#000000", width=0.4)
    p.text("Parser", 216, 77, 8.97)
    p.draw(lines((140, 74), (200, 74)), type="s", stroke="#000000", width=0.4)
    body_text(p)
    diagram = next(iter(diagrams(elements(p))))
    labelled = {"".join(r["text"] for par in n["paragraphs"] for r in par): n for n in diagram["nodes"] if n["paragraphs"]}
    assert "LexerLexer" not in labelled and labelled["Lexer"]["fill"] == "#ffff00", list(labelled)


def test_crossing_circles_are_a_venn_picture_not_a_diagram():
    """Words of a Venn diagram are placed by region; as native ellipses each circle set all
    the words in it centred, and a set's title above a circle made it an overlay."""
    p = Page()
    p.draw(ellipse(185.8, 133.6, 65.2, 65.2), fill="#0072b2")
    p.draw(ellipse(265.2, 133.6, 65.2, 65.2), fill="#009e73")
    p.words("Climate policy", 119, 64, 10.91)
    p.words("Carbon tax", 130, 130, 8.97)
    p.words("Renewables", 203, 137, 8.97)
    p.words("Strategic reserves", 270, 130, 8.97)
    body_text(p)
    els = elements(p)
    assert not any(e["kind"] == "diagram" or e["kind"] == "shape" for e in els), [e["kind"] for e in els]
    fig = next(iter(images(els)))
    assert not fig.get("anchor")
    assert not any("Carbon" in t or "Renewables" in t for t in texts(els))


def test_open_curves_are_no_ellipse_nodes():
    """Four curves in a row (a logo's arch, a wave) became an ELLIPSE node of their box."""
    p = Page()
    p.draw(rect(398, 28, 437, 67), type="s", stroke="#000000", width=0.4)
    arch: list[PathItem] = [
        ("c", [[402, 60], [402, 45], [410, 35], [417, 35]]), ("c", [[417, 35], [424, 35], [432, 45], [432, 60]]),
        ("c", [[432, 60], [428, 50], [422, 42], [417, 42]]), ("c", [[417, 42], [412, 42], [406, 50], [402, 60]])]
    tail: list[PathItem] = [("c", [[432, 60], [430, 62], [428, 63], [426, 63]]),
                            ("c", [[426, 63], [420, 63], [410, 63], [404, 62]])]
    p.draw(arch[:2] + tail, type="s", stroke="#003366", width=1.2)
    body_text(p)
    assert not any(any(n["shape"] == "ELLIPSE" for n in e["nodes"]) for e in diagrams(elements(p)))


def test_a_script_label_is_one_label_with_its_script():
    """R_s beside a resistor: one free label, R and a subscript s (set as one run of plain text
    it read "Rs"; as two labels the s stood alone below the R)."""
    p = Page()
    p.draw(rect(123.5, 172.2, 150.4, 182.3), type="s", stroke="#000000", width=0.8)
    p.draw(lines((40, 177), (123.5, 177)), type="s", stroke="#000000", width=0.4)
    p.draw(lines((150.4, 177), (200, 177)), type="s", stroke="#000000", width=0.4)
    p.text("R", 54, 165, 9.27)  # (to 58.6)
    p.text("s", 58.7, 166.4, 6.78, font="LMRoman8-Regular")
    body_text(p)
    [d] = diagrams(elements(p))
    [label] = [n for n in d["nodes"] if n["shape"] is None]
    [runs] = label["paragraphs"]
    assert [(r["text"], r["script"]) for r in runs] == [("R", None), ("s", "sub")]
    assert label["baselines"] == [165]
    # Slides lowers the s (SUBSCRIPT), at no more than the R's size (emit.run_sizes)
    reqs = [slides_json(r) for r in diagram_requests(element_json(d), "s", "d", 1.0, FontMapper(), None)]
    styles = [jobj(r, "updateTextStyle", "style") for r in reqs if "updateTextStyle" in r]
    assert [jstr(s, "baselineOffset") for s in styles] == ["NONE", "SUBSCRIPT"]
    assert jnum(styles[1], "fontSize", "magnitude") <= jnum(styles[0], "fontSize", "magnitude")


def test_a_superscript_label_is_on_its_letter_s_line():
    """x^2 over an edge: the raised 2 is on the x's line (read on its own baseline, it was a line
    of its own above the x)."""
    p = Page()
    p.draw(lines((40, 177), (200, 177)), type="s", stroke="#000000", width=0.4)
    p.draw(rect(200, 170, 230, 184), type="s", stroke="#000000", width=0.8)
    p.text("x", 100, 172, 10.9, font="CMMI10")
    p.text("2", 105.8, 168, 7.97, font="CMR8")
    p.text("+", 112, 172, 10.9, font="CMR10")
    body_text(p)
    [d] = diagrams(elements(p))
    [label] = [n for n in d["nodes"] if n["shape"] is None]
    [runs] = label["paragraphs"]
    assert [(r["text"].strip(), r["script"]) for r in runs] == [("x", None), ("2", "super"), ("+", None)]


def test_a_box_labelled_with_a_big_operator_holds_its_picture():
    """A box's label no run can say (a big operator) is a picture of the whole label on the box,
    anchored to the diagram; the box stays a shape."""
    p = Page()
    p.draw(rect(60, 150, 140, 190), type="s", stroke="#000000", width=0.8)
    p.draw(lines((140, 170), (200, 170)), type="s", stroke="#000000", width=0.4)
    p.draw(rect(200, 160, 240, 180), type="s", stroke="#000000", width=0.8)
    p.text("∑", 80, 178, 10.9, font="CMEX10")
    p.text("x", 92, 172, 10.9, font="CMMI10")
    body_text(p)
    found = elements(p)
    [d] = diagrams(found)
    [picture] = [e for e in found if e["kind"] == "image"]
    assert picture.get("anchor") == d["id"] and picture["role"] == "math"
    assert [n["paragraphs"] for n in d["nodes"] if n["shape"]] == [[], []]


def test_a_big_operator_between_boxes_keeps_a_diagram_a_picture():
    p = Page()
    p.draw(rect(60, 150, 140, 190), type="s", stroke="#000000", width=0.8)
    p.draw(lines((140, 170), (200, 170)), type="s", stroke="#000000", width=0.4)
    p.draw(rect(200, 160, 240, 180), type="s", stroke="#000000", width=0.8)
    p.text("∑", 165, 166, 10.9, font="CMEX10")
    body_text(p)
    assert not diagrams(elements(p))


def test_text_inside_a_node_that_is_none_of_its_labels_keeps_it_a_picture():
    """A section number drawn in a filled TikZ node, read with its heading as one formula,
    stays in the background: a native rectangle drawn over it hid it."""
    p = Page()
    p.draw(rect(0, 0, W, H), fill="#142d55")
    p.draw(rect(56.69, 93.54, 124.73, 161.58), fill="#00966e")
    p.text("1", 74.27, 148.32, 59.78, font="LMSans10-Bold", w=32.9, color="#ffffff")
    p.text("Field", 148.6, 126.67, 24.79, font="LMSans10-Bold", w=54.4, color="#ffffff")
    p.text("campaign", 212.06, 126.67, 24.79, font="LMSans10-Bold", w=107.4, color="#ffffff")
    p.words("Dune erosion under storm surges", 148.6, 151.07, 9.96)
    els = elements(p)
    assert not any(e["kind"] in ("diagram", "shape") for e in els), [e["kind"] for e in els]


def test_label_well_inside_a_filled_node_is_not_what_an_overlay_follows():
    """A "?" on a disc drawn over its line: the disc was stretched after the "?" as Slides
    set it, and came out an ellipse."""
    p = Page()
    p.draw(ellipse(127.56, 127.56, 42.52, 42.52), fill="#00966e")
    p.text("?", 117.21, 141.4, 39.85, font="LMSans10-Bold", w=20.7, color="#ffffff")
    p.text("Questions?", 196.59, 123.51, 24.79, font="LMSans10-Bold", w=123.9)
    p.text("l.meyer@icr-kiel.example", 196.59, 144.22, 9.96, w=100.2)
    body_text(p)
    overlays = [e for e in images(elements(p)) if e.get("overlay")]  # (only a picture is an overlay)
    assert overlays and not any(o.get("anchor") for o in overlays)
