"""Where a paragraph's lines break on synthetic pages: lines a person broke by hand stay apart (a
column measured by its neighbours beside a picture, Japanese lines, two short lines or two lines of
one word ending together),
a list's lone subitem centred on the page by chance stays a left-aligned item of its list,
a wrapped line opening on a word that looks like a label stays in its paragraph, an aligned
formula's description column is a gutter whatever paragraph lies under it, and a centred column of
contact lines stays centred with its breaks."""

from beamer2slides import ir
from beamer2slides.classify import Line, PageClassifier, Rect, Span, classify_page, new_span
from beamer2slides.fonts import font_info
from beamer2slides.raw_types import RawPage, RawSpan

W, H = 362.83, 272.13
SIZE = 11.0
FONT = "LMSans10-Regular"
BOLD = "CMSSBX10"
SANS = "CMSS10"
CJK_FONT = "HaranoAjiGothic-Medium"
VT = "\x0b"


def span(text: str, x0: float, baseline: float, w: float, font: str) -> Span:
    return new_span(id=f"s{x0:.0f}-{baseline:.0f}", text=text, font=font, size=SIZE, color="#000000",
                    rect=Rect(x0, baseline - 0.75 * SIZE, x0 + w, baseline + 0.25 * SIZE),
                    baseline=baseline, horizontal=True, info=font_info(font), link=None, drawn=False, visual=None)


def words(text: str, x0: float, baseline: float, w: float) -> Span:
    """A run of prose in the sans face, `w` wide."""
    return span(text, x0, baseline, w, FONT)


def raw(s: Span) -> RawSpan:
    return {"id": s.id, "text": s.text, "font": s.font, "size": s.size, "color": s.color, "alpha": 255,
            "origin": [s.rect.x0, s.baseline], "bbox": s.rect.as_list(), "dir": [1.0, 0.0], "smallcaps": False}


def page(spans: list[Span]) -> RawPage:
    for i, s in enumerate(spans):
        s.id = f"p0s{i}"
    return {"index": 0, "label": "1", "size": [W, H], "spans": [raw(s) for s in spans], "images": [],
            "drawings": [], "links": [], "frame_label": None}


def paragraphs(spans: list[Span]) -> list[ir.Paragraph]:
    slide = classify_page(page(spans), SIZE)
    return [p for el in slide["elements"] if el["kind"] == "text" for p in el["paragraphs"]]


def text(p: ir.Paragraph) -> str:
    return "".join(r["text"] for r in p["runs"])


def texts(spans: list[Span]) -> list[str]:
    return [text(p) for p in paragraphs(spans)]


def beside(top: float, rows: int) -> list[Span]:
    """A column of text right of the page's middle, its rows between the left column's."""
    return [words("the right column runs on here", 230, top + 13.5 * i + 6, 110) for i in range(rows)]


# -- a column measured by its neighbours ---------------------------------------------------------

def test_lines_broken_by_hand_beside_a_column_stay_apart():
    """An item's two lines broken by \\\\, set in under lines of the same column that run 50 pt
    further: the column reaches that far (`column_mates`), so 'for' would have fitted after the
    first line and the second is a line of its own (real_slide-20250221 s7). By the item's own
    lines alone the column ended where its first line does."""
    column = [words("The tunnel network carries packets between", 20, 50.5 + 13.5 * i, 180) for i in range(4)]
    first = words("Malleable Mutual Tunneling Network", 30, 110, 120)
    second = words("for Experimental Technologies", 30, 123.5, 110)
    out = texts(column + [first, second] + beside(46, 7))
    assert first.text in out and second.text in out, out


def test_japanese_lines_broken_by_hand_stay_apart():
    """Chinese and Japanese join with no space: one more character fitted where the line above
    ends, so a line ending 13 pt short of its paragraph's widest one was broken by hand
    (real_slide-20250221 s24) - a Latin word space before it said it would not have fitted."""
    ja = ["日本語の文章を書きます。", "検索すると再び見つか", "外線の番号も同じです"]
    spans = [span(ja[0], 20, 100, 12 * SIZE, CJK_FONT), span(ja[1], 20, 113.5, 152 - 13 - 20, CJK_FONT),
             span(ja[2], 20, 127, 10 * SIZE, CJK_FONT)]
    out = texts(spans + beside(96, 4))
    assert ja[2] in out and not any(ja[1] in t and ja[2] in t for t in out), out


def test_two_short_lines_ending_together_are_no_measure():
    """'d = 0.1 (b),' and 'd = 0.5 (c),' end at one edge, as a justified paragraph's lines would,
    but the paragraph's first line runs 260 pt further: two lines broken by hand, ending
    together by chance (real_presentation-biore s63)."""
    spans = [words("The flow was measured at three distances from the inlet of the chamber", 20, 100, 320),
             words("d = 0.1 (b),", 20, 113.5, 60), words("d = 0.5 (c),", 20, 127, 60)]
    out = texts(spans)
    assert out[-1] == "d = 0.5 (c),", out


def test_lines_of_one_word_ending_together_are_no_measure():
    """An enumerate's empty items: '5.' over '6.', one word each, end together because the words
    are as wide, not because TeX stretched them to a measure; each is a line of its own
    (real_c-error-handling s3, s27: '5. 6.' and '▶ ▶ ▶' on one line in Slides)."""
    spans = [words("4.", 38, 100, 12), words("Embedded error indicator in data type", 57, 100, 240),
             words("5.", 38, 114, 12), words("6.", 38, 128, 12), words("7.", 38, 142, 12),
             words("Code design choices with return codes", 57, 142, 246)]
    out = texts(spans)
    assert "5." in out and "6." in out, out


# -- a lone item of a list -------------------------------------------------------------------------

def test_a_lone_subitem_centred_by_chance_stays_in_its_list():
    """A subitem's one line, ▶ and words, whose middle falls within 2 pt of the page's (by chance:
    it runs to near the right margin): its list's items left-aligned around it say it is one of
    them, not a centred line (real_postgres-on-the-wire s27, real_c-error-handling s27: written
    CENTER in a box of its own, its bullet drifting off the list's and onto its words)."""
    def item(x: float, baseline: float, said: str, w: float) -> list[Span]:
        return [span("▶", x, baseline, 8.5, "MSAM10"), words(said, x + 13.9, baseline, w)]
    spans = (item(18.8, 66.7, "the ReadyForQuery message includes transaction status", 253.3)
             + item(18.8, 83.2, "this is useful for things like prompts or, more importantly,", 287.2)
             + [words("pgbouncer", 32.7, 96.7, 47.2)]
             + item(18.8, 111.7, "the transaction status only got included in protocol 3.0", 251.2)
             + item(41.3, 125.6, "for 2.0 libpq does string comparison to try and track the status", 263.7))
    lone = spans[-1].text
    assert abs((41.3 + spans[-1].rect.x1) / 2 - W / 2) <= 2  # (centred on the page by its rect)
    slide = classify_page(page(spans), SIZE)
    boxes = [el for el in slide["elements"] if el["kind"] == "text" and any(lone in text(p) for p in el["paragraphs"])]
    assert len(boxes) == 1
    pars = boxes[0]["paragraphs"]
    assert len(pars) == 4, [text(p) for p in pars]
    assert pars[-1].get("align") != "center"


# -- a word that looks like a label ----------------------------------------------------------------

def label_page(gap: float) -> list[Span]:
    """A paragraph whose second line opens on '(a)' with `gap` before its next word."""
    return [words("The cells grew in static culture for the first twenty culture days", 20, 100, 318),
            words("(a)", 20, 113.5, 15), words("and for the last week in the perfusion reactor, as in", 35 + gap, 113.5, 300),
            words("as the figure shows.", 20, 127, 90)]


def test_a_wrapped_line_opening_on_a_lettered_word_stays_in_its_paragraph():
    """'(a)' followed by a word space (under \\labelsep, as wide as the line's other spaces) is a
    word of the sentence wrapped to open the line, not an item's label (`label_is_word`): no tab
    in the paragraph, no new item (real_presentation-biore s57)."""
    pars = paragraphs(label_page(3.3))
    assert len(pars) == 1 and "(a) and for" in text(pars[0]) and pars[0].get("tab_x0") is None, \
        [text(p) for p in pars]


def labelled_line(gap: float) -> Line:
    spans = label_page(gap)
    classifier = PageClassifier(page(spans), SIZE)
    line = next(l for l in classifier.build_lines(spans) if l.text.startswith("(a)"))
    classifier.detect_bullet(line)
    return line


def test_a_label_set_off_by_labelsep_still_opens_an_item():
    """Both lines' '(a)' is a label by its shape (`detect_bullet`); only the one set off by more
    than \\labelsep's share of an em, wider than a word space, hangs an item."""
    word, label = labelled_line(3.3), labelled_line(6.0)
    assert word.tab is not None and PageClassifier.label_is_word(word)
    assert label.tab is not None and not PageClassifier.label_is_word(label)


# -- an aligned formula's columns ------------------------------------------------------------------

def align_rows(below: float) -> tuple[list[Span], Span, Span]:
    """Two rows of an align*: a formula, a 2 em gap, its description; and a paragraph whose words
    run across the gap, `below` pt under the second row's baseline. (The pair asked about: the
    first row's formula and description.)"""
    a = words("Q[i] = X W_Q in R", 40, 100, 120)
    b = words("the query block of chunk i.", 182, 100, 120)
    rows = [words("K[i] = X W_K in R", 40, 113.5, 112), words("the key block of chunk i.", 182, 113.5, 112)]
    under = words("We define K, V, O in a similar way for every chunk of the sequence.", 30, 113.5 + below, 300)
    return [a, b] + rows + [under], a, b


def test_a_paragraph_under_an_aligned_block_does_not_close_its_gutter():
    """The words of a paragraph 2.7 em under the rows cross the gap between formula and
    description, but they are another block (`block_of`): the gap is still the rows' gutter
    (real_linear-attention-a s18, where the second row's description was joined to its formula
    and moved with the formula's narrower letters)."""
    spans, a, b = align_rows(2.7 * SIZE)
    assert PageClassifier.gutter(spans, a, b, SIZE)


def test_a_line_of_the_block_crossing_the_gap_still_closes_it():
    spans, a, b = align_rows(13.5)
    assert not PageClassifier.gutter(spans, a, b, SIZE)


# -- a centred column of contact lines -------------------------------------------------------------

def contacts() -> list[Span]:
    """Two centred columns of four contact lines each, broken by \\\\, the left one setting the
    page's leftmost edge; its third line starts 0.7 pt from its first by chance (geometry of
    real_dstalk-datascience-ta s17, words made up)."""
    left = [("Ada Lovelace,", BOLD, 51.6, 42.7, 55.3), ("PhD", BOLD, 110.9, 42.7, 22.5),
            ("Data", SANS, 60.2, 56.2, 22.3), ("Scientist", SANS, 86.1, 56.2, 38.7),
            ("ada@example.org", SANS, 52.4, 69.8, 80.2), ("@adacodes", SANS, 67.0, 83.3, 51.0)]
    right = [("Bob Byron,", BOLD, 226.5, 44.9, 61.1), ("PhD", BOLD, 291.6, 44.9, 22.5),
             ("Research", SANS, 216.2, 58.4, 40.0), ("Data", SANS, 259.8, 58.4, 22.3),
             ("Scientist", SANS, 285.7, 58.4, 38.7), ("bob.byron.science@example.org", SANS, 205.4, 72.0, 129.7),
             ("@bobby3", SANS, 251.2, 85.5, 38.2)]
    return [span(t, x, y, w, f) for t, f, x, y, w in left + right]


def test_a_centred_column_of_lines_broken_by_hand_stays_centred():
    """The lines are centred on each other, so the third starting near the first is no left
    alignment (`continues`); and the page's margin, set by this column alone, gives no measure
    (`free_width`): the column is one centred paragraph broken where the page breaks it."""
    pars = paragraphs(contacts())
    first = next(p for p in pars if text(p).startswith("Ada"))
    assert first["align"] == "center", [text(p) for p in pars]
    assert text(first).count(VT) == 3, text(first)
