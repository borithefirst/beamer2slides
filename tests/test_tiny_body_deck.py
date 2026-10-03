"""A deck whose body is \\tiny, on synthetic pages modelled on real_africa-remote-sens-30 (a public
Russian talk, never republished): a custom footline as small as the body read as the deck's
furniture, a longtable broken across frames whose rows hang between side rules under a ruled
head, pifont star bullets as native bullets, and a draft image's translucent veil under a block
drawn over it."""

from beamer2slides import classify as C
from beamer2slides.fonts import font_info
from beamer2slides.ir import Element, ShapeElement, TableElement, TextElement, deck_json, slide_of
from beamer2slides.json_types import JsonObject
from beamer2slides.raw_types import DrawingType, PathItem, RawDoc, RawDrawing, RawPage, RawSpan

from .json_reads import jobj, jobjs, jstr

W, H = 362.83, 272.13
SANS = "SFSS0700"
MONO = "SFTT0800"
DINGBATS = "Dingbats"
BLACK = "#000000"
CHAR = 0.46  # em per character


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

    def fill(self, box: tuple[float, float, float, float], color: str, opacity: float) -> None:
        x0, y0, x1, y1 = box
        self._draw("f", [("re", [[x0, y0], [x1, y1]])], fill=color, stroke=None, width=None, opacity=opacity)

    def rule(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self._draw("s", [("l", [[x0, y0], [x1, y1]])], fill=None, stroke=BLACK, width=0.85, opacity=1.0)

    def _draw(self, type: DrawingType, path: list[PathItem], *, fill: str | None, stroke: str | None,
              width: float | None, opacity: float) -> None:
        pts = [p for _, ps in path for p in ps]
        self.drawings.append({
            "id": f"p{self.index}d{len(self.drawings)}", "type": type, "items": "".join(op for op, _ in path),
            "bbox": [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)],
            "fill": fill, "stroke": stroke, "width": width, "fill_opacity": opacity, "stroke_opacity": 1.0,
            "soft_mask": False, "corners": {}, "path": path})

    def raw(self) -> RawPage:
        return {"index": self.index, "label": str(self.index + 1), "size": [W, H], "spans": self.spans, "images": [],
                "drawings": self.drawings, "links": [], "frame_label": None}


def doc(pages: list[Page]) -> RawDoc:
    return {"version": 1, "source": {"pdf": "", "producer": "", "pages": len(pages), "title": ""},
            "pages": [p.raw() for p in pages]}


def deck(pages: list[Page]) -> JsonObject:
    return deck_json(C.classify(doc(pages)))


def elements(d: JsonObject, page: int) -> list[Element]:
    return slide_of(jobj(d, "slides", page))["elements"]


def texts(els: list[Element]) -> list[TextElement]:
    out: list[TextElement] = []
    for e in els:
        if e["kind"] == "text":
            out.append(e)
    return out


def tables(els: list[Element]) -> list[TableElement]:
    out: list[TableElement] = []
    for e in els:
        if e["kind"] == "table":
            out.append(e)
    return out


def shapes(els: list[Element]) -> list[ShapeElement]:
    out: list[ShapeElement] = []
    for e in els:
        if e["kind"] == "shape":
            out.append(e)
    return out


def words_of(e: TextElement) -> list[str]:
    return ["".join(r["text"] for r in p["runs"]) for p in e["paragraphs"]]


# ---------------------------------------------------------------- the footline is the deck's

FOOTLINE = "Институт Географии РАН, г. Москва, 30.09.2025"


def framed_list(n: int, author_x: float) -> tuple[Page, list[str]]:
    """A frame of \\tiny items whose last line sits a line above a 5 pt footline (institute and
    date, the author, the frame number); returns the page and its footline's span ids."""
    page = Page(n)
    page.words(f"Кадр номер {n + 1}", 8.5, 24, 10.91)
    for k, item in enumerate(["Первый пункт списка здесь", "Второй пункт списка здесь", "Третий пункт списка",
                              "Четвёртый пункт списка рядом"]):
        baseline = 239.1 + 7.2 * k
        page.span("•", 20.0, baseline, 6.0, SANS)
        page.words(item, 28.35, baseline, 6.0)
    foot = page.words(FOOTLINE, 28.35, 268.54, 4.98) + page.words("Леменкова П. А.", author_x, 268.54, 4.98)
    foot.append(page.span(str(n + 1), 331.1, 268.54, 4.98, SANS))
    return page, [s["id"] for s in foot[:-1]]


def test_a_footline_as_small_as_the_body_is_furniture_and_joins_no_list():
    # (body 6 pt: the 5 pt footline passed the theme size test as words of the page, and the
    # list above took it as its last paragraph; the author sat 2 pt further right on frames 1-9)
    built = [framed_list(n, 225.0 if n == 0 else 223.0) for n in range(4)]
    d = deck([page for page, _ in built])
    for n, (_, footline) in enumerate(built):
        els = texts(elements(d, n))
        assert not any(sid in e["spans"] for e in els for sid in footline), [words_of(e) for e in els]
        items = next(e for e in els if any("Первый" in w for w in words_of(e)))
        assert len(items["paragraphs"]) == 4 and all(p["bullet"] for p in items["paragraphs"])
    layout = ["".join(jstr(r, "text") for r in jobjs(t, "paragraphs", 0, "runs")) for t in jobjs(d, "layout_texts")]
    assert FOOTLINE in layout


def test_a_word_frame_titles_share_is_no_furniture():
    # ("and" at about one place in four frame titles, CMYK and greyscale ... Cut out and faded: as
    # furniture the titles were cut in three)
    pages: list[Page] = []
    for n, title in enumerate(["CMYK and greyscale", "Alpha and palette", "Turned and mirrored", "Cut out and faded"]):
        page = Page(n)
        page.words(title, 8.5, 20.0, 10.0)
        page.words("Body text of the frame in the middle", 30.0, 140.0, 10.0)
        pages.append(page)
    assert not any(C.furniture(doc(pages), 10.0).values())


# ---------------------------------------------------------------- a longtable's continued rows

def longtable() -> Page:
    """A longtable's first frame: the head between two rules, then rows hanging between side
    rules that run on to the frame's end with no rule under them; the citation column 0.65 em
    from the DOI column, some cells over several lines."""
    page = Page(0)
    page.words("Апробация: опубликовано", 8.5, 24.0, 10.91)
    size = 6.97
    page.rule(29.1, 70.02, 333.74, 70.02)
    page.rule(29.1, 78.84, 333.74, 78.84)
    page.words("№", 35.9, 76.03, size)
    page.words("Страна", 56.4, 76.03, size)
    page.words("Публикация", 139.2, 76.03, size)
    page.words("Индекс doi", 207.8, 76.03, size)
    rows = [("1", "Алжир", ["[Lemenkova202313xyz]"], ["10.3390/asi6040061"]),
            ("2", "Ангола", ["[Lemenkova202412xyz]"], ["10.17707/AgricultForest"]),
            ("3", "Ботсвана", ["[Lemenkova202217xyz]"], ["10.3390/ijgi11090473"]),
            ("4", "Эфиопия", ["[Lemenkova202221xyz,", "Lemenkova202205wxyz,", "Lemenkova202118wxyz]"],
             ["10.24425/jwld.2022.1415;", "10.5755/j01.erem.78.1.2,", "10.4314/sinet.v44i1"]),
            ("5", "Гана", ["[Lemenkova202211xyz]"], ["10.24425/gac.2022.1411"]),
            ("6", "Гвинея", ["[Lemenkova202325xyz]"], ["10.5281/zenodo.1067328"])]
    baseline = 84.85
    for number, country, cites, dois in rows:
        page.words(number, 35.9, baseline, size)
        page.words(country, 56.4, baseline, size)
        for k, (cite, doi) in enumerate(zip(cites, dois)):
            page.span(cite, 139.2, baseline + 7.12 * k, size, SANS)
            page.span(doi, 207.8, baseline + 7.12 * k, size, SANS)
        baseline += 7.12 * len(cites)
    y = 70.45
    while y < baseline:  # (each row's side rules, as the PDF draws them)
        page.rule(29.52, y, 29.52, y + 7.97)
        page.rule(333.31, y, 333.31, y + 7.97)
        y += 7.12
    return page


def test_a_longtable_head_with_side_rules_running_on_is_one_native_table():
    page = longtable()
    tabs = tables(elements(deck([page]), 0))
    assert len(tabs) == 1, [e["kind"] for e in elements(deck([page]), 0)]
    cells = [["".join(r["text"] for r in c) for c in row] for row in tabs[0]["cells"]]
    assert cells[0] == ["№", "Страна", "Публикация", "Индекс doi"]
    assert cells[1] == ["1", "Алжир", "[Lemenkova202313xyz]", "10.3390/asi6040061"]
    # (the DOIs stay in their column: the citation's lines under it were read as one wrapped cell
    # across both columns, merged)
    assert not tabs[0].get("merges")
    assert not any("Lemenkova" in c and "10." in c for row in cells for c in row), cells
    assert ["", "", "Lemenkova202118wxyz]", "10.4314/sinet.v44i1"] in cells
    assert not any(e["kind"] == "image" for e in elements(deck([page]), 0))


# ---------------------------------------------------------------- dingbat star bullets

def test_pifont_star_bullets_are_native_star_bullets_of_their_items():
    assert font_info(DINGBATS).family == "icon"
    page = Page(0)
    page.words("Уганда", 8.5, 24.0, 10.91)
    stars: list[str] = []
    baseline = 58.29
    for item in [["Тектоника: Великая Рифтовая Долина"], ["География: Уганда расположена в восточной"],
                 ["Геология: Альбертинский Грабен"]]:
        stars.append(page.span("✴", 161.05, baseline, 5.98, DINGBATS)["id"])
        for k, line in enumerate(item):
            page.words(line, 170.0, baseline + 7.97 * k, 6.97)
        baseline += 7.97 * len(item) + 3
    els = elements(deck([page]), 0)
    items = next(e for e in texts(els) if any("Тектоника" in w for w in words_of(e)))
    bullets = [p["bullet"] for p in items["paragraphs"]]
    assert [b["kind"] if b else None for b in bullets] == ["glyph"] * 3
    assert all(b and b["kind"] == "glyph" and b["text"] == "★" for b in bullets)
    # (the glyphs leave the background with their items; no picture of them stays where the PDF
    # drew them while Slides sets the items' lines elsewhere)
    assert set(stars) <= set(items["spans"])
    assert not any(e["kind"] == "image" for e in els)


# ---------------------------------------------------------------- a veil under a block

def veiled(block: bool) -> Page:
    """\\includegraphics[draft]'s box (a frame and a 60% white veil over its file name), with a
    red block drawn over part of it when `block`."""
    page = Page(0)
    page.words("Часть I", 8.5, 24.0, 10.91)
    page.fill((64.82, 102.27, 306.17, 199.63), "#ffffff", 0.6)
    page.span("Fig_52.jpg", 67.84, 152.6, 7.97, MONO)
    page.words("Обоснование", 157.32, 154.0, 9.96)
    if block:
        page.fill((248.1, 155.98, 302.02, 224.98), "#bf0000", 1.0)
        for k, word in enumerate(["Проблема,", "цели,", "задачи,", "данные,", "методы,"]):
            page.words(word, 252.1, 164.4 + 9.47 * k, 7.97)
    return page


def test_a_translucent_veil_under_a_block_drawn_over_it_stays_in_the_background():
    # (as a highlight it was grouped with its words, over the block's picture, and washed the
    # red out down to its edge)
    assert not any(s["role"] == "highlight" for s in shapes(elements(deck([veiled(True)]), 0)))
    # (with nothing drawn over it, it is the highlight of the words under it, as before)
    assert any(s["role"] == "highlight" for s in shapes(elements(deck([veiled(False)]), 0)))
