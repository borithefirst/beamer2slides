"""A logo's words are the logo's (`PageClassifier.logo_words`), on synthetic pages modelled on
real_decision-tree-lect-decision-analy and real_africa-remote-sens-30 (public talks, never
republished). graphicx's draft placeholder for a missing logo file - a framed box with the file's
name in CMTT - is drawn in the header and footer bands of every frame: its words repeat, so they
are furniture, but they stay the logo's picture (as layout text in Roboto Mono they came out 9%
taller than the PDF's). Furniture a big figure reaches over - a draft frame drawn down to the page
foot over the footline author on a few frames - is still theme text, on the layout."""

from beamer2slides import classify as C
from beamer2slides.ir import Element, ImageElement, deck_json, slide_of
from beamer2slides.json_types import JsonObject
from beamer2slides.raw_types import DrawingType, PathItem, RawDoc, RawDrawing, RawPage, RawSpan

from .json_reads import jobj, jobjs, jstr, jstrs

W, H = 362.83, 272.13
SANS = "CMSS8"
MONO = "CMTT8"
BLACK = "#000000"
CHAR = 0.5  # em per character
FOOT = 5.978
AUTHOR = "Dr Nathan Green"


class Page:
    def __init__(self, index: int) -> None:
        self.index = index
        self.spans: list[RawSpan] = []
        self.drawings: list[RawDrawing] = []

    def span(self, text: str, x0: float, baseline: float, size: float, font: str) -> RawSpan:
        w = len(text) * CHAR * size
        s: RawSpan = {"id": f"p{self.index}s{len(self.spans)}", "text": text, "font": font, "size": size,
                      "color": BLACK, "alpha": 255, "origin": [x0, baseline],
                      "bbox": [x0, baseline - 0.75 * size, x0 + w, baseline + 0.25 * size],
                      "dir": [1.0, -0.0], "smallcaps": False}
        self.spans.append(s)
        return s

    def words(self, text: str, x0: float, baseline: float, size: float) -> list[RawSpan]:
        """A line of words a word space apart, one span each."""
        out: list[RawSpan] = []
        for word in text.split():
            out.append(self.span(word, x0, baseline, size, SANS))
            x0 = out[-1]["bbox"][2] + 0.33 * size
        return out

    def frame(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """A draft placeholder's frame: four rules, as graphicx draws them."""
        for a, b in (((x0, y0), (x0, y1)), ((x0, y0), (x1, y0)), ((x0, y1), (x1, y1)), ((x1, y0), (x1, y1))):
            self._draw("s", [("l", [[a[0], a[1]], [b[0], b[1]]])], fill=None, stroke=BLACK, width=0.4)

    def _draw(self, type: DrawingType, path: list[PathItem], *, fill: str | None, stroke: str | None,
              width: float | None) -> None:
        pts = [p for _, ps in path for p in ps]
        self.drawings.append({
            "id": f"p{self.index}d{len(self.drawings)}", "type": type, "items": "".join(op for op, _ in path),
            "bbox": [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)],
            "fill": fill, "stroke": stroke, "width": width, "fill_opacity": 1.0, "stroke_opacity": 1.0,
            "soft_mask": False, "corners": {}, "path": path})

    def raw(self) -> RawPage:
        return {"index": self.index, "label": str(self.index + 1), "size": [W, H], "spans": self.spans, "images": [],
                "drawings": self.drawings, "links": [], "frame_label": None}


def doc(pages: list[Page]) -> RawDoc:
    return {"version": 1, "source": {"pdf": "", "producer": "", "pages": len(pages), "title": ""},
            "pages": [p.raw() for p in pages]}


def deck(pages: list[Page]) -> JsonObject:
    return deck_json(C.classify(doc(pages)))


def layout_words(d: JsonObject) -> list[str]:
    return ["".join(jstr(r, "text") for r in jobjs(t, "paragraphs", 0, "runs")) for t in jobjs(d, "layout_texts")]


def elements(d: JsonObject, page: int) -> list[Element]:
    return slide_of(jobj(d, "slides", page))["elements"]


def images(els: list[Element]) -> list[ImageElement]:
    out: list[ImageElement] = []
    for e in els:
        if e["kind"] == "image":
            out.append(e)
    return out


def frame_page(n: int, logos: bool) -> Page:
    """A frame of words above a two-line footline (author, then the talk's title) and the frame
    number; with `logos`, a draft logo box in the top left corner holding 'icl.pdf' and another
    in the footline's right corner holding 'collage.', as real_decision-tree's every frame has."""
    page = Page(n)
    page.words(f"Frame number {n + 1}", 40.0, 24.0, 10.91)
    for k in range(3):  # (set a little further right frame by frame: no furniture of the deck)
        page.words("Body line of the frame with its words", 40.0 + 3.0 * n, 90.0 + 14.0 * k, 10.91)
    page.words(AUTHOR, 8.5, 253.65, FOOT)
    page.words("Decision analytic modelling", 8.5, 260.6, FOOT)
    page.span(f"{n + 1} / 4", 8.5, 267.6, FOOT, SANS)
    if logos:
        page.frame(5.67, 5.67, 31.18, 31.18)
        page.span("icl.pdf", 7.79, 19.59, FOOT, MONO)
        page.frame(337.32, 246.54, 362.83, 272.05)
        page.span("collage.", 339.44, 260.55, FOOT, MONO)
    return page


def test_a_draft_logos_file_name_stays_the_logos_picture() -> None:
    pages = [frame_page(n, True) for n in range(4)]
    names = {n: [s["id"] for s in p.spans if s["font"] == MONO] for n, p in enumerate(pages)}
    d = deck(pages)
    layout = layout_words(d)
    assert AUTHOR in layout   # (the footline is still the layout's)
    assert "icl.pdf" not in layout and "collage." not in layout, layout
    for n in range(4):
        on_layout = jstrs(jobj(d, "slides", n), "on_layout")
        assert not set(names[n]) & set(on_layout), on_layout
        # (the PDF's own glyphs: in the box's picture or the background, never set in a text box)
        texts = [sid for e in elements(d, n) if e["kind"] == "text" for sid in e["spans"]]
        pictured = [sid for e in images(elements(d, n)) for sid in e["spans"]]
        left = [sid for kept in jobjs(jobj(d, "slides", n), "left_in_background") for sid in jstrs(kept, "spans")]
        assert not set(names[n]) & set(texts) and set(names[n]) <= set(pictured) | set(left)


def test_furniture_a_big_figure_reaches_over_is_still_layout_text() -> None:
    # (a draft image frame from under the title down to the page foot, over the footline author
    # on one frame in four: real_africa-remote-sens-30 p1/p12/p42/p46)
    pages = [frame_page(n, False) for n in range(4)]
    pages[1].frame(5.0, 30.67, 60.0, 271.23)
    author = [s["id"] for s in pages[1].spans if s["text"] in AUTHOR.split()]
    d = deck(pages)
    assert AUTHOR in layout_words(d)
    assert set(author) <= set(jstrs(jobj(d, "slides", 1), "on_layout"))
