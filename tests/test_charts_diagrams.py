"""Charts and diagrams on synthetic pages: tick rows and axis labels that belong to their
chart, legend boxes, bar series and funnels that are no panels, curves that are no ellipses,
marks that are no bullets, node labels, Venn diagrams, and labels no picture would hold."""

import math

from beamer2slides import classify as C
from beamer2slides.classify import Line, PageClassifier, Rect, Span, body_size, classify
from beamer2slides.fonts import font_info

W, H = 453.54, 255.12
SANS, MONO = "LMSans10-Regular", "LMMono10-Regular"


# ---------------------------------------------------------------- synthetic pages

class Page:
    def __init__(self, index: int = 0, label: str = "1"):
        self.index, self.label = index, label
        self.spans, self.drawings, self.images = [], [], []

    def text(self, text: str, x0: float, baseline: float, size: float = 10.91, font: str = SANS,
             w: float | None = None, color: str = "#000000") -> dict:
        w = len(text) * 0.5 * size if w is None else w
        s = {"id": f"p{self.index}s{len(self.spans)}", "text": text, "font": font, "size": size, "color": color,
             "alpha": 255, "origin": [x0, baseline], "bbox": [x0, baseline - 0.75 * size, x0 + w, baseline + 0.25 * size],
             "dir": [1.0, -0.0], "smallcaps": False}
        self.spans.append(s)
        return s

    def words(self, text: str, x0: float, baseline: float, size: float = 10.91, font: str = SANS) -> float:
        """A line of words a word space apart, one span each; returns where it ends."""
        for word in text.split():
            x0 = self.text(word, x0, baseline, size, font)["bbox"][2] + 0.33 * size
        return x0

    def draw(self, path: list, type: str = "f", fill: str | None = "#1f77b4", stroke: str | None = None,
             width: float | None = None) -> dict:
        points = [p for _, pts in path for p in pts]
        d = {"id": f"p{self.index}d{len(self.drawings)}", "type": type, "items": "".join(op for op, _ in path),
             "bbox": [min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)],
             "fill": fill if "f" in type else None, "stroke": stroke if "s" in type else None,
             "width": width if "s" in type else None, "fill_opacity": 1.0, "corners": {}, "path": path}
        self.drawings.append(d)
        return d

    def raw(self) -> dict:
        return {"index": self.index, "label": self.label, "size": [W, H], "spans": self.spans,
                "images": self.images, "drawings": self.drawings, "links": [], "frame_label": None}


def rect(x0, y0, x1, y1) -> list:
    return [["re", [[x0, y0], [x1, y1]]]]


def lines(*points, closed: bool = False) -> list:
    pts = list(points) + ([points[0]] if closed else [])
    return [["l", [list(a), list(b)]] for a, b in zip(pts, pts[1:])]


def ellipse(cx, cy, rx, ry) -> list:
    k = 0.5523
    e, s, w, n = [cx + rx, cy], [cx, cy + ry], [cx - rx, cy], [cx, cy - ry]
    return [["c", [e, [cx + rx, cy - k * ry], [cx + k * rx, cy - ry], n]],
            ["c", [n, [cx - k * rx, cy - ry], [cx - rx, cy - k * ry], w]],
            ["c", [w, [cx - rx, cy + k * ry], [cx - k * rx, cy + ry], s]],
            ["c", [s, [cx + k * rx, cy + ry], [cx + rx, cy + k * ry], e]]]


def body_text(page: Page, y: float = 225) -> None:
    """A sentence of body text, so the deck's body size is the usual 10.9 pt."""
    page.words("Body text that sets the size of the deck", 30, y)


def deck(*pages: Page) -> dict:
    return classify({"version": 1, "source": {"title": ""}, "pages": [p.raw() for p in pages]})


def elements(page: Page) -> list[dict]:
    return deck(page)["slides"][0]["elements"]


def texts(els: list[dict]) -> list[str]:
    return [" / ".join("".join(r["text"] for r in p["runs"]) for p in e["paragraphs"]) for e in els if e["kind"] == "text"]


def span(text: str, x0: float, size: float = 7.97, baseline: float = 180.0, font: str = SANS) -> Span:
    w = len(text) * 0.5 * size
    return Span(id=f"s{x0:.1f}", text=text, font=font, size=size, color="#000000",
                rect=Rect(x0, baseline - 0.75 * size, x0 + w, baseline + 0.25 * size),
                baseline=baseline, horizontal=True, info=font_info(font))


def row(labels: list[str], x0: float, gap_em: float, size: float = 7.97) -> Line:
    out = []
    for t in labels:
        out.append(span(t, x0, size))
        x0 = out[-1].rect.x1 + gap_em * size
    return Line(out)


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
    raw = {"pages": [p.raw() for p in pages]}
    assert body_size(raw) == 10.9


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
    fig = next(e for e in els if e["kind"] == "image" and e["role"] == "figure")
    assert not fig.get("anchor") and fig["bbox"][3] >= 182


# ---------------------------------------------------------------- shapes of paths

def test_upright_ellipse_is_closed_and_meets_its_box_at_the_middles():
    assert C.upright_ellipse(ellipse(100, 100, 30, 20), Rect(70, 80, 130, 120))
    # a sine wave drawn as four curves: open
    wave = [["c", [[0, 50], [10, 30], [20, 30], [30, 50]]], ["c", [[30, 50], [40, 70], [50, 70], [60, 50]]],
            ["c", [[60, 50], [70, 30], [80, 30], [90, 50]]], ["c", [[90, 50], [100, 70], [110, 70], [120, 50]]]]
    assert not C.upright_ellipse(wave, Rect(0, 35, 120, 65))
    # an ellipse turned 45 degrees: its curve ends are no longer at the box's middles
    turned = [[op, [[100 + (x - 100) * math.cos(0.8) - (y - 100) * math.sin(0.8),
                     100 + (x - 100) * math.sin(0.8) + (y - 100) * math.cos(0.8)] for x, y in pts]]
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
        for par in e.get("paragraphs", []):
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
    title = next(e for e in els if e["kind"] == "text" and "Lab" in texts([e])[0])
    assert not title["paragraphs"][0]["bullet"]


# ---------------------------------------------------------------- labels beside graphics

def test_bar_chart_category_labels_belong_to_the_chart():
    """Category labels set flush right against a horizontal bar chart, some too long for a
    tick label: as text boxes they re-wrapped over the next one."""
    p = Page()
    bars = []
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
    diagram = next(e for e in elements(p) if e["kind"] == "diagram")
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
    fig = next(e for e in els if e["kind"] == "image")
    assert not fig.get("anchor")
    assert not any("Carbon" in t or "Renewables" in t for t in texts(els))


def test_open_curves_are_no_ellipse_nodes():
    """Four curves in a row (a logo's arch, a wave) became an ELLIPSE node of their box."""
    p = Page()
    p.draw(rect(398, 28, 437, 67), type="s", stroke="#000000", width=0.4)
    arch = [["c", [[402, 60], [402, 45], [410, 35], [417, 35]]], ["c", [[417, 35], [424, 35], [432, 45], [432, 60]]],
            ["c", [[432, 60], [428, 50], [422, 42], [417, 42]]], ["c", [[417, 42], [412, 42], [406, 50], [402, 60]]]]
    p.draw(arch[:2] + [["c", [[432, 60], [430, 62], [428, 63], [426, 63]]], ["c", [[426, 63], [420, 63], [410, 63], [404, 62]]]],
           type="s", stroke="#003366", width=1.2)
    body_text(p)
    assert not any(e["kind"] == "diagram" and any(n["shape"] == "ELLIPSE" for n in e["nodes"]) for e in elements(p))


def test_script_labels_keep_a_circuit_a_picture():
    p = Page()
    p.draw(rect(123.5, 172.2, 150.4, 182.3), type="s", stroke="#000000", width=0.8)
    p.draw(lines((40, 177), (123.5, 177)), type="s", stroke="#000000", width=0.4)
    p.draw(lines((150.4, 177), (200, 177)), type="s", stroke="#000000", width=0.4)
    p.text("R", 54, 165, 9.27)
    p.text("s", 60, 166.4, 6.78, font="LMRoman8-Regular")
    body_text(p)
    assert not any(e["kind"] == "diagram" for e in elements(p))


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
    overlays = [e for e in elements(p) if e.get("overlay")]
    assert overlays and not any(o.get("anchor") for o in overlays)
