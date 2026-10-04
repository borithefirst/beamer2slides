"""Inline formulas set in beamer's sans text faces (CMSSI letters, CMSSBX vectors, CMSS8 scripts:
no math font among them) are formulas: their relations get TeX's thick space (U+2008 in Lato) and
their spaces hold together; a formula too long to hold together keeps the breaks TeX allows, after
its relations and binary operators (`classify_text.formula_spans`, `formula_groups`)."""

from beamer2slides.classify import Rect, Span, classify_page, new_span
from beamer2slides.classify_text import NBSP, THICK_SPACE
from beamer2slides.fonts import font_info
from beamer2slides.ir import Run
from beamer2slides.raw_types import RawSpan

W, H = 362.83, 272.13
BODY, SCRIPT = 10.91, 7.97
BASE = 120.0
SUB = BASE + 0.15 * BODY  # TeX lowers a subscript 0.15 em
SANS, BOLD, ITALIC, SCRIPT_UPRIGHT, SCRIPT_ITALIC = "CMSS10", "CMSSBX10", "CMSSI10", "CMSS8", "CMSSI8"


class Line:
    """Spans laid out left to right, each `gap` pt after the one before."""

    def __init__(self, x0: float) -> None:
        self.x = x0
        self.spans: list[Span] = []

    def add(self, text: str, font: str, gap: float, sub: bool) -> "Line":
        size = SCRIPT if sub else BODY
        baseline = SUB if sub else BASE
        x0 = self.x + gap
        x1 = x0 + len(text) * 0.5 * size
        self.spans.append(new_span(id="", text=text, font=font, size=size, color="#000000",
                                   rect=Rect(x0, baseline - 0.75 * size, x1, baseline + 0.25 * size),
                                   baseline=baseline, horizontal=True, info=font_info(font), link=None, drawn=False,
                                   visual=None))
        self.x = x1
        return self


def runs(spans: list[Span]) -> list[Run]:
    raw: list[RawSpan] = [{"id": f"p0s{i}", "text": s.text, "font": s.font, "size": s.size, "color": s.color,
                           "alpha": 255, "origin": [s.rect.x0, s.baseline], "bbox": s.rect.as_list(), "dir": [1.0, 0.0],
                           "smallcaps": False} for i, s in enumerate(spans)]
    slide = classify_page({"index": 0, "label": "1", "size": [W, H], "spans": raw, "images": [], "drawings": [],
                           "links": [], "frame_label": None}, BODY)
    texts = [e for e in slide["elements"] if e["kind"] == "text"]
    assert len(texts) == 1, [[r["text"] for p in e["paragraphs"] for r in p["runs"]] for e in texts]
    (par,) = texts[0]["paragraphs"]
    return par["runs"]


def text(rs: list[Run]) -> str:
    return "".join(r["text"] for r in rs)


THIN = 0.0  # spans that touch
WORD = 0.33 * BODY
REL = 0.278 * BODY  # TeX's thick space around a relation


def test_a_formula_in_sans_text_faces_gets_thick_spaces():
    """real_linear-attention-a s46: W_[t] = T_[t]K_[t] - bold CMSSBX10 letters, CMSS8 brackets,
    CMSSI8 t, CMSS10's '='. No span is a math font's, and the spaces around '=' were breakable word
    spaces (0.19 em in Lato against TeX's 0.278)."""
    line = Line(40.0).add("Using the transform", SANS, 0.0, False)
    line.add("W", BOLD, WORD, False).add("[", SCRIPT_UPRIGHT, THIN, True).add("t", SCRIPT_ITALIC, THIN, True) \
        .add("]", SCRIPT_UPRIGHT, THIN, True).add("=", SANS, REL, False).add("T", BOLD, REL, False) \
        .add("[t]", SCRIPT_UPRIGHT, THIN, True).add("K", BOLD, THIN, False).add("[t]", SCRIPT_UPRIGHT, THIN, True) \
        .add("in practice, as we have seen.", SANS, WORD, False)
    rs = runs(line.spans)
    assert f"]{THICK_SPACE}={THICK_SPACE}T" in text(rs), text(rs)
    assert "\xa0in practice" not in text(rs)  # the word after the formula may still start a line
    assert text(rs).startswith("Using the transform W")


def test_a_relation_after_a_script_gets_its_thick_space():
    """s30: 'It is common to set β_t = 1 in practice' - the subscript t (CMSSI8) sat between the
    math β and the '=', so the formula was cut there and ' =' kept a word space."""
    line = Line(40.0).add("It is common to set", SANS, 0.0, False).add("β", "CMMIB10", WORD, False) \
        .add("t", SCRIPT_ITALIC, THIN, True).add("=", SANS, REL, False).add("1", BOLD, REL, False) \
        .add("in practice, examples: GLA and others.", SANS, WORD, False)
    rs = runs(line.spans)
    assert f"t{THICK_SPACE}={THICK_SPACE}1 in practice" in text(rs), text(rs)


def test_a_long_formula_breaks_only_where_tex_may():
    """s35: a formula wider than half its line is not held together, but the breaks it keeps are
    TeX's - after a relation or binary operator, never inside a script (S_{t-1}) - and the space
    before a relation is still its thick space."""
    line = Line(40.0).add("S", BOLD, 0.0, False).add("t", SCRIPT_ITALIC, THIN, True).add("=", SANS, REL, False) \
        .add("S", BOLD, REL, False).add("t−1", SCRIPT_ITALIC, THIN, True).add("−", "CMSY10", 0.222 * BODY, False) \
        .add("β", "CMMI10", 0.222 * BODY, False).add("t", SCRIPT_ITALIC, THIN, True) \
        .add("(Sk)", BOLD, 0.1 * BODY, False).add("+", SANS, 0.222 * BODY, False).add("vk", BOLD, 0.222 * BODY, False)
    rs = runs(line.spans)
    t = text(rs)
    assert f"t{THICK_SPACE}= S" in t, t           # a thick space before '=', a break after it
    assert f"1{NBSP}− β" in t, t                  # no break before '−', one after it
    assert "t−1" in t                             # the script's own '−' breaks nothing
    assert f"){NBSP}+ vk" in t, t


def test_prose_around_a_script_is_no_formula():
    """A short word touching a script is its nucleus (x_t, TC^0); one a word space away, or a
    longer word, is prose and keeps its breakable spaces."""
    line = Line(40.0).add("a function of", SANS, 0.0, False).add("x", BOLD, WORD, False) \
        .add("t", SCRIPT_ITALIC, THIN, True).add("is", ITALIC, WORD, False) \
        .add("useful", SANS, WORD, False).add("and more words to fill the line.", SANS, WORD, False)
    rs = runs(line.spans)
    t = text(rs)
    assert "t is useful" in t and NBSP not in t, t
