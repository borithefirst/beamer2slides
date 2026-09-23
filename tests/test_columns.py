"""Lines, columns and paragraphs on synthetic pages: where a line ends (a column's gutter, a
subtitle touching a heading), which lines continue a paragraph (a column below its picture),
how a lone line is aligned, and how Chinese or Japanese lines are joined and wrapped."""

from beamer2slides.classify import PageClassifier, Rect, Span, classify_page, first_word_width
from beamer2slides.fonts import font_info

W, H = 362.83, 272.13
FONT = "LMSans10-Regular"
CJK_FONT = "HaranoAjiGothic-Medium"


def span(text: str, x0: float, baseline: float, size: float = 11.0, w: float | None = None,
         font: str = FONT) -> Span:
    w = len(text) * 0.5 * size if w is None else w
    return Span(id=f"s{x0:.0f}-{baseline:.0f}", text=text, font=font, size=size, color="#000000",
                rect=Rect(x0, baseline - 0.75 * size, x0 + w, baseline + 0.25 * size),
                baseline=baseline, horizontal=True, info=font_info(font))


def raw(s: Span) -> dict:
    return {"id": s.id, "text": s.text, "font": s.font, "size": s.size, "color": s.color, "alpha": 255,
            "origin": [s.rect.x0, s.baseline], "bbox": s.rect.as_list(), "dir": [1.0, 0.0], "smallcaps": False}


def page(spans: list[Span]) -> dict:
    for i, s in enumerate(spans):
        s.id = f"p0s{i}"
    return {"index": 0, "label": "1", "size": [W, H], "spans": [raw(s) for s in spans], "images": [],
            "drawings": [], "links": [], "frame_label": None}


def paragraphs(spans: list[Span], body: float = 11.0) -> list[dict]:
    slide = classify_page(page(spans), body)
    return [p for el in slide["elements"] if el["kind"] == "text" for p in el["paragraphs"]]


def build_lines(spans: list[Span]):
    return PageClassifier(page(spans), 11.0).build_lines(spans)


def text(p: dict) -> str:
    return "".join(r["text"] for r in p["runs"])


# -- gutters -----------------------------------------------------------------------------------

LEFT = ["Reefs grow slowly in warm and", "Bleaching follows a heatwave", "Recovery takes a decade or",
        "Surveys count the colonies", "Models predict their future"]
SHORT = ["clear", "for", "more", "by", "in"]
RIGHT = ["water where the light reaches down", "weeks after the temperature peak", "longer between events",
         "diver transects every year", "under the warming scenarios"]


def multicol(gap_em: float = 1.4) -> list[Span]:
    """Two columns of prose whose left lines end on short words, the gutter under the 2 em
    at which the words of a line join (a multicol gutter is 1.3-1.8 em)."""
    out = []
    for i in range(5):
        y = 90 + 13.5 * i
        out.append(span(LEFT[i], 20, y, w=128))
        out.append(span(SHORT[i], 151.3, y, w=15))
        out.append(span(RIGHT[i], 166.3 + gap_em * 11, y, w=140))
    return out


def test_a_column_ending_on_a_short_word_is_not_joined_across_the_gutter():
    texts = [text(p) for p in paragraphs(multicol())]
    for left, short, right in zip(LEFT, SHORT, RIGHT):
        assert not any(short in t and right in t for t in texts), texts
    assert any(LEFT[0] in t and SHORT[0] in t for t in texts)


def test_a_wide_space_in_one_line_is_not_a_gutter():
    """A \\quad in one line of a paragraph: no other line has a gap there, so no column."""
    spans = []
    for i in range(4):
        y = 90 + 13.5 * i
        spans.append(span("The measured bleaching threshold of the reef", 20, y, w=200))
        if i == 1:
            spans.append(span("see", 223.3, y, w=16))  # then a 1.4 em space
            spans.append(span("Table 2 for the values", 254.7, y, w=85))
        else:
            spans.append(span("was reached in the summer", 223.3, y, w=116))
    lines = build_lines(spans)
    assert any("see" in l.text and "Table 2" in l.text for l in lines), [l.text for l in lines]


def test_a_label_at_the_start_of_its_line_is_not_a_column():
    """'1.2' then the entry, further along, on every line: a table of contents, not columns."""
    a, b = span("1.2", 20, 100, w=14), span("Methods and the survey design", 52, 100, w=130)
    others = [x for y in (86.5, 113.5) for x in (span("1.1", 20, y, w=14), span("Sites of the survey campaign", 52, y, w=130))]
    assert not PageClassifier.gutter([a, b] + others, a, b, 11.0)


def test_short_cells_of_a_table_are_no_columns_of_prose():
    """Cells 1.5 em apart on every row keep their edges like columns do, but no words beside
    the gaps run on like prose (a table's rows stay lines for plain_tables)."""
    rows = [("North", "12.5", "3.1"), ("South", "9.8", "2.7"), ("East", "14.0", "3.3"), ("West", "11.2", "2.9")]
    spans = [span(t, x, 100 + 13.5 * i, w=w) for i, row in enumerate(rows)
             for t, x, w in zip(row, (20, 64.5, 101), (28, 20, 16))]
    a, b = spans[3], spans[4]  # 'South' | '9.8'
    assert not PageClassifier.gutter(spans, a, b, 11.0)


def test_a_short_last_word_with_columns_beside_is_a_gutter():
    spans = multicol()
    a, b = spans[1], spans[2]  # 'clear' | 'water ...'
    assert PageClassifier.gutter(spans, a, b, 11.0)


def test_a_subtitle_touching_a_heading_is_its_own_line():
    """The theme sets the subtitle at a fixed offset under the heading's node: a two-line
    heading overlaps it. Its 10 pt words stayed one line with the 25 pt 'bers' and lost their
    spaces ('bersAnnualreview2026')."""
    spans = [span("bers", 148.6, 144.0, 24.8, w=47), span("Annual", 148.6, 151.1, 9.96, w=29.2),
             span("review", 181.1, 151.1, 9.96, w=26), span("2026", 210.5, 151.1, 9.96, w=20)]
    lines = build_lines(spans)
    assert sorted(l.text for l in lines) == ["Annual review 2026", "bers"]


# -- paragraphs --------------------------------------------------------------------------------

def test_a_column_below_its_picture_stays_as_narrow():
    """A list item in a column beside a chart wraps its last word below the chart: nothing is
    beside that line any more, but the column above says how wide it is, so the word is not
    a new paragraph (it was set as one over the text below)."""
    spans = []
    words = ["Recovery between events now takes", "longer than the gap that separates",
             "one heatwave from the next one", "and the reefs have less time between"]
    for t, w in zip(words, (143, 136, 139, 144)):  # (ragged right: TeX-full lines of a list item)
        spans.append(span(t, 20, 90 + 13.5 * words.index(t), w=w))
    spans.append(span("them", 20, 90 + 13.5 * 4, w=22))
    for i, t in enumerate(["Mean coral cover fell by a third", "on the surveyed reefs of the north",
                           "between two thousand and today"]):
        spans.append(span(t, 200, 90 + 13.5 * i, w=130))
    pars = paragraphs(spans)
    assert any(t.endswith("between them") for t in map(text, pars)), [text(p) for p in pars]


def test_a_lone_item_that_happens_to_be_centred_keeps_its_siblings_edge():
    spans = [span("First item of the list", 60, 100, w=90), span("Second item here", 60, 113.5, w=110),
             span("A", 60, 127, w=7), span("last item exactly as long as to end where the margin mirrors", 70.3, 127, w=W - 130.3)]
    pars = paragraphs(spans)
    last = next(p for p in pars if text(p).startswith("A last item"))
    assert last["align"] == "left"


# -- Chinese and Japanese ----------------------------------------------------------------------

JA1 = "事前学習済みモデルを、ラベル付きデータでファインチューニン"
JA2 = "グする。"


def ja_paragraph() -> list[Span]:
    return [span(JA1, 20, 100, w=len(JA1) * 11, font=CJK_FONT), span(JA2, 20, 113.5, w=len(JA2) * 11, font=CJK_FONT)]


def test_japanese_lines_join_without_a_space():
    pars = paragraphs(ja_paragraph())
    assert [text(p) for p in pars] == [JA1 + JA2]


def test_a_japanese_line_may_break_after_any_character():
    """The next line's first word is its first character, not the whole line: a wrap limit past
    the page had Slides keep lines TeX broke."""
    first = span(JA2, 20, 113.5, w=len(JA2) * 11, font=CJK_FONT)
    assert abs(first_word_width(first) - 11) < 0.5
    assert 20 < first_word_width(span("BERT）を", 20, 100, w=60)) < 40  # the Latin word before them
    (par,) = paragraphs(ja_paragraph())
    assert par["wrap_limit"] <= W
    assert par["wrap_limit"] >= 20 + len(JA1) * 11
