"""Code listings set in a proportional face (listings' columns=fixed in CM sans, Bera Sans,
Computer Modern) and in mono faces the font tables did not know (Courier, Nimbus Mono): code by
its column grid, its spaces and indentation from the columns, left aligned, never prose or a plain
table (talks, frisem, esi-dev1-slides, talksx, zds-2022-drivers, defense). Synthetic pages, no
PDF."""

from beamer2slides import extract
from beamer2slides.classify import PageClassifier
from beamer2slides.classify_model import Span, new_line, new_paragraph
from beamer2slides.classify_tables import numbered_listing
from beamer2slides.fonts import font_info
from beamer2slides.ir import TextElement
from beamer2slides.pdf import Char, char_box
from beamer2slides.raw_types import RawSpan

from .test_span_joining import Page, Shown

SIZE = 10.0
PITCH = 0.6 * SIZE   # listings' basewidth: 0.6 em columns
LEFT = 30.0          # the listing's first column
LEADING = 12.0
# CM sans-like advances (em): narrow i/l/t/f, wide m - a proportional face
WIDTHS = {"i": 0.24, "l": 0.24, "t": 0.36, "f": 0.31, "r": 0.34, "s": 0.38, "m": 0.76, "w": 0.69,
          "(": 0.39, ")": 0.39, ";": 0.28, "=": 0.55, "+": 0.55, "<": 0.55, "{": 0.5, "}": 0.5, ",": 0.28}


def advance(c: str) -> float:
    return WIDTHS.get(c, 0.5) * SIZE


def listing_char(c: str, x: float, y: float, font: str) -> Char:
    adv = advance(c)
    return Char(c=c, font=font, size=SIZE, color=0, alpha=255, origin=(x, y),
                box=char_box(x, y, 1.0, 0.0, adv, SIZE, 0.8, -0.2), dir=(1.0, 0.0), obj=0,
                font_id=0, advance=adv, synthetic=False, ascent=0.8, descent=-0.2, exact_advance=True)


def tokens(text: str) -> list[tuple[int, str]]:
    """(column, token) of a code line as listings cuts it: letters and digits one token, any
    other character one of its own, a space an empty column."""
    out: list[tuple[int, str]] = []
    col = 0
    while col < len(text):
        c = text[col]
        if c == " ":
            col += 1
            continue
        end = col + 1
        while c.isalnum() and end < len(text) and text[end].isalnum():
            end += 1
        out.append((col, text[col:end]))
        col = end
    return out


def fixed_columns(lines: list[str], font: str) -> list[Char]:
    """Glyphs of code lines set as columns=fixed sets them: a token of n characters fills n
    columns, its glyphs spread with one glue before, between and after them."""
    chars: list[Char] = []
    for i, line in enumerate(lines):
        y = 100.0 + i * LEADING
        for col, token in tokens(line):
            glue = (len(token) * PITCH - sum(advance(c) for c in token)) / (len(token) + 1)
            x = LEFT + col * PITCH + glue
            for c in token:
                chars.append(listing_char(c, x, y, font))
                x += advance(c) + glue
    return chars


def raw_spans(chars: list[Char]) -> list[RawSpan]:
    out: list[RawSpan] = []
    for i, s in enumerate(extract.spans(Page(chars, {}), Shown(), False, chars, {})):
        span: RawSpan = {"id": f"p0s{i}", "text": s.text, "font": s.font, "size": s.size, "color": "#000000",
                         "alpha": 255, "origin": list(s.origin), "bbox": list(s.bbox), "dir": list(s.dir),
                         "smallcaps": False}
        if s.columns is not None:
            span["columns"] = list(s.columns)
        out.append(span)
    return out


def text_boxes(spans: list[RawSpan]) -> list[TextElement]:
    page = PageClassifier({"index": 0, "label": "1", "size": [364.0, 273.0], "spans": spans, "images": [],
                           "drawings": [], "links": []}, SIZE)
    out: list[TextElement] = []
    for e in page.classify()["elements"]:
        if e["kind"] == "text":
            out.append(e)
    return out


def paragraph_texts(box: TextElement) -> list[str]:
    return ["".join(r["text"] for r in p["runs"]) for p in box["paragraphs"]]


CODE = ["int total = 0;",
        "for (int i = 0; i < n; i++) {",
        "    total = total + i;",
        "}"]


def test_a_fixed_columns_listing_in_a_sans_face_is_code() -> None:
    # talks 9-36: 'data [] a = [] | a : [a]' in CMSS17, its tokens' glues 0.05-0.08 em apiece:
    # read as letterspaced prose ('f o r a l l'), centred, its indentation gone
    spans = raw_spans(fixed_columns(CODE, "CMSS10"))
    grids = [s.get("columns") for s in spans]
    assert {round(g[0], 3) if g is not None else None for g in grids} == {PITCH}
    [box] = text_boxes(spans)
    assert box.get("code") is True
    assert [line for text in paragraph_texts(box) for line in text.split("\n")] == CODE
    assert {p["align"] for p in box["paragraphs"]} == {"left"}


def test_a_listing_line_number_stays_off_the_grid() -> None:
    # esi-dev1-slides: '1  import java.util.Arrays;', the number right-aligned left of the code
    chars = fixed_columns(["import java.util.Arrays;", "int[] t = new int[n];", "t[0] = 1;"], "SFSS1000")
    numbers = [listing_char(str(i + 1), LEFT - 9.0, 100.0 + i * LEADING, "SFSS1000") for i in range(3)]
    spans = raw_spans(sorted(numbers + chars, key=lambda c: (c.origin[1], c.origin[0])))
    assert [s["text"] for s in spans if "columns" not in s] == ["1", "2", "3"]


def test_prose_and_a_tracked_title_are_no_grid() -> None:
    # words touch: no grid, whatever their glyphs' widths
    prose: list[Char] = []
    x = LEFT
    for word in "the total is small for us".split():
        for c in word:
            prose.append(listing_char(c, x, 100.0, "CMSS10"))
            x += advance(c)
        x += 0.33 * SIZE
    assert extract.column_grid(prose) == {}
    # a title tracked evenly (\textls): every glyph one glue apart is letterspacing, not columns
    tracked: list[Char] = []
    x = LEFT
    for word in ("SIMPLE", "TITLES", "STAY"):
        for c in word:
            tracked.append(listing_char(c, x, 100.0, "CMSS10"))
            x += advance(c) + 0.1 * SIZE
        x += 0.4 * SIZE
    assert extract.column_grid(tracked) == {}


def test_courier_and_nimbus_mono_are_mono() -> None:
    # talksx: Courier listings were prose in Lato
    for name in ("NimbusMonL-Regu", "NimbusMonL-ReguObli", "TeXGyreCursor-Regular", "Courier", "CourierNewPSMT"):
        assert font_info(name).family == "mono", name


def raw_mono(i: int, text: str, font: str, x0: float, advance_pt: float) -> RawSpan:
    return {"id": f"s{i}", "text": text, "font": font, "size": 6.0, "color": "#000000", "alpha": 255,
            "origin": [x0, 100.0], "bbox": [x0, 95.5, x0 + len(text) * advance_pt, 101.5],
            "dir": [1.0, 0.0], "smallcaps": False}


def page_spans(raw: list[RawSpan]) -> list[Span]:
    return PageClassifier({"index": 0, "label": "1", "size": [364.0, 273.0], "spans": raw, "images": [],
                           "drawings": [], "links": []}, SIZE).spans()


def test_a_narrower_mono_face_keeps_its_word_spaces() -> None:
    # zds-2022-drivers 32: minted comments in LMMono10-Italic (0.525 em) among LMMono8's 0.531 em
    # columns drifted a column by mid-line: 'devicesreadiness', 'nodelabel', 'val =>console'
    adv = 0.525 * 6.0
    words = "/* initialize driver (e.g. check for dependent devices readiness) */".split(" ")
    raw: list[RawSpan] = []
    x = 41.05
    for i, word in enumerate(words):
        raw.append(raw_mono(i, word, "LMMono10-Italic", x, adv))
        x += (len(word) + 1) * adv
    par = new_paragraph([new_line(page_spans(raw))], align="left", reason=None)
    runs = PageClassifier.runs(par, "", False, 0.531 * 6.0)
    assert "".join(r["text"] for r in runs) == " ".join(words)


def test_an_algorithm_between_rules_is_no_table() -> None:
    # defense 35: algpseudocode's '1:', '2:', ... left of each line - a table of centred cells
    def row(i: int, text: str, x0: float, number: str) -> list[RawSpan]:
        return [raw_mono(2 * i, number, "SFRM0600", 10.0, 2.9), raw_mono(2 * i + 1, text, "CMMI6", x0, 3.0)]

    def rows(*made: list[RawSpan]) -> list[list[Span]]:
        spans = page_spans([s for r in made for s in r])
        return [spans[k:k + 2] for k in range(0, len(spans), 2)]
    assert numbered_listing(rows(row(1, "Mapping", 20.0, "1:"), row(2, "Parse", 20.0, "2:"), row(3, "map", 30.0, "3:")))
    assert not numbered_listing(rows(row(1, "a", 20.0, "1:"), row(2, "b", 20.0, "3:"), row(3, "c", 20.0, "4:")))
    assert not numbered_listing(rows(row(1, "Condition", 20.0, "1"), row(2, "Action", 20.0, "2"),
                                     row(3, "Rule", 20.0, "3")))
