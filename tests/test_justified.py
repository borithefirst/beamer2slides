"""Justified prose and the width of wrapped text boxes (visual hunt r7, fixer S): which paragraphs
are justified (a ragged pair with a formula's gap, a bold line over a regular one, a centred pair,
a reference's hanging label are not), a JUSTIFIED paragraph never ending past its PDF lines' edge,
each paragraph of a box breaking where TeX did (indentEnd), and a compound longer than the
column keeping its line break."""

from collections.abc import Sequence

from beamer2slides import emit
from beamer2slides.classify import Line, PageClassifier, Span, new_line, new_paragraph
from beamer2slides.emit_model import JsonMap
from beamer2slides.json_types import Json, JsonObject

from .json_reads import jarr, jnum, jobj, jobjs
from .test_columns import span
from .test_emit_hunt import box_right, column_list_of
from .test_emit_requests import BODY, FONTS, pt_of, text_run

REGULAR, BOLD = "LMSans10-Regular", "LMSans10-Bold"
SIZE = 10.0
LEFT = 30.0  # where a line starts unless it says


def line(words: Sequence[str], y: float, *, x0: float, gaps: list[float] | None, end: float | None,
         font: str) -> Line:
    """Words as spans (0.5 em a letter) `gaps` em apart - one width for all, stretched to end at
    `end`, or TeX's natural 0.333 em."""
    widths = [len(w) * 0.5 * SIZE for w in words]
    if gaps is None:
        g = 0.333 if end is None else (end - x0 - sum(widths)) / (len(words) - 1) / SIZE
        gaps = [g] * (len(words) - 1)
    spans: list[Span] = []
    x = x0
    for w, wd, g in zip(words, widths, gaps + [0.0]):
        spans.append(span(w, x, y, SIZE, wd, font))
        x += wd + g * SIZE
    return new_line(spans)


def justified(*lines: Line) -> bool:
    return PageClassifier.is_justified(new_paragraph(list(lines), align="left", reason=None))


# ---------------------------------------------------------------- which paragraphs are justified

def test_stretched_lines_ending_together_are_justified() -> None:
    assert justified(line("Reefs grow slowly in warm and clear water".split(), 90,
                          x0=LEFT, gaps=None, end=250.0, font=REGULAR),
                     line("where the light reaches down to the".split(), 103.5,
                          x0=LEFT, gaps=None, end=250.0, font=REGULAR),
                     line("sea floor.".split(), 117, x0=LEFT, gaps=None, end=None, font=REGULAR))


def test_a_ragged_pair_with_a_formulas_gap_is_not_justified() -> None:
    # sci v1 s6, math v3 s2, econ v3 s3: TeX's space around a formula or a relation (0.8 em) pulled
    # the first line's mean space above the second's, and two ragged lines were set JUSTIFIED.
    first = line("the rate k grows with T for every sample".split(), 90,
                 x0=LEFT, gaps=[0.333, 0.333, 0.8, 0.333, 0.333, 0.333, 0.333, 0.333], end=None, font=REGULAR)
    assert not justified(first, line("in the series".split(), 103.5, x0=LEFT, gaps=None, end=None, font=REGULAR))


def test_a_bold_line_over_a_regular_one_is_not_justified() -> None:
    # textfx v3 s8, themes v4 s8: a bold face's natural space is wider than the regular one's.
    assert not justified(line("Mental accounting and the budget".split(), 90,
                              x0=LEFT, gaps=[0.37] * 4, end=None, font=BOLD),
                         line("people keep money in".split(), 103.5, x0=LEFT, gaps=None, end=None, font=REGULAR))


def test_a_last_line_longer_than_the_first_is_not_justified() -> None:
    # econ v2 s6, sci v3 s8: a centred or ragged pair whose second line runs past the first.
    assert not justified(line("Identifying assumption holds".split(), 90, x0=LEFT, gaps=None, end=172.0, font=REGULAR),
                         line("absent the policy the trends would be parallel".split(), 103.5,
                              x0=LEFT, gaps=None, end=None, font=REGULAR))


def test_a_reference_with_a_hanging_label_is_not_justified() -> None:
    # control b1 s17, themes v2 s10: '[1]' hangs left of the wrapped lines, 0.6 em before the authors.
    first = line("[1] Knuth, D. E. The TeXbook, Addison".split(), 90,
                 x0=20.0, gaps=[0.6, 0.333, 0.333, 0.333, 0.333, 0.333], end=None, font=REGULAR)
    assert not justified(first, line("Wesley, Reading, 1984".split(), 103.5, x0=36.0, gaps=None, end=None, font=REGULAR))


# ---------------------------------------------------------------- where a justified box ends

X0 = 34.97
SCALE = emit.SLIDE_W / 362.83
QUOTE = ["Coral reefs recover between bleaching", "events only when the water stays cool", "for a decade."]


def pdf_w(runs: Sequence[JsonMap]) -> float:
    """`emit.pdf_width`, which these runs always have."""
    w = emit.pdf_width(runs)
    assert w is not None
    return w


def slides_w(runs: Sequence[JsonMap], scale: float) -> float:
    """`emit.slides_width`, which these runs always have."""
    w = emit.slides_width(runs, scale, FONTS)
    assert w is not None
    return w


def slides_lines(p: JsonMap) -> tuple[float, float]:
    """`emit.slides_lines` of a paragraph these tests know Slides measures."""
    got = emit.slides_lines(p, SCALE, FONTS)
    assert got is not None
    return got


def quote(stretch: float) -> JsonObject:
    """A justified paragraph whose full lines TeX stretched `stretch` pt past their natural width
    (Lato sets the first 8.3 pt wider than CM Sans, the second 2.7 pt)."""
    natural = [pdf_w([text_run(t, BODY)]) for t in QUOTE]
    edge = X0 + max(natural[:-1]) + stretch
    lines: list[Json] = [{"baseline": 70.0 + 13.5 * i, "x0": X0, "x1": edge if i < len(QUOTE) - 1 else X0 + w}
             for i, w in enumerate(natural)]
    return {"id": "p0t0", "kind": "text", "role": "body", "code": False, "bbox": [X0, 60.0, edge, 110.0], "paragraphs": [
        {"align": "left", "level": 0, "size": 10.91, "text_x0": X0, "tab_x0": None, "wrap_limit": None, "bullet": None,
         "runs": [text_run(" ".join(QUOTE), BODY)], "lines": lines, "justified": True}]}


def styles(el: JsonMap, scale: float) -> list[JsonObject]:
    return [jobj(r, "updateParagraphStyle", "style") for r in emit.text_box_requests(el, "b2s_s003", "b2s_s003_t1", scale, FONTS)
            if "updateParagraphStyle" in r]


def text_rights(el: JsonMap, scale: float) -> list[float]:
    """Where each paragraph's text ends (Slides pt): the box's text edge less its indentEnd."""
    right = box_right(el, scale)
    return [right - (jnum(s, "indentEnd", "magnitude") if "indentEnd" in s else 0.0) for s in styles(el, scale)]


def test_a_justified_paragraph_never_ends_past_its_pdf_lines() -> None:
    # econ v2 s6, layout v1 s9, themes v2 s6: the box ended LINE_MARGIN past the widest line as
    # Slides sets it, past the PDF's edge, and JUSTIFIED stretched every line out to it - through
    # the frame or panel the PDF's lines stop at.
    for stretch, want in ((0.5, "START"), (8.0, "JUSTIFIED"), (12.0, "JUSTIFIED")):
        el = quote(stretch)
        edge = jnum(el, "paragraphs", 0, "lines", 0, "x1") * SCALE
        got = styles(el, SCALE)[0]["alignment"]
        assert got == want, stretch
        if got == "JUSTIFIED":
            assert text_rights(el, SCALE)[0] <= edge + 0.01, stretch
    # (12 pt: past what the words' CM advances can say, so the lines are flowed into the edge)
    assert emit.slides_lines(jobj(quote(12.0), "paragraphs", 0), SCALE, FONTS) is None


def test_a_justified_paragraph_beside_a_ragged_one_ends_at_its_own_edge() -> None:
    # dense v2 s7, layout v3 s4: the box is as wide as a longer line beside it needs; the
    # justified paragraph keeps the difference free and still ends where its PDF lines do.
    el = quote(8.0)
    edge = jnum(el, "paragraphs", 0, "lines", 0, "x1")
    wide = "Office hours move to Wednesday at noon"
    x0 = edge + 10.0 - slides_w([text_run(wide, BODY)], SCALE) / SCALE
    jarr(el, "paragraphs").append({**jobj(el, "paragraphs", 0), "justified": False, "text_x0": x0, "runs": [text_run(wide, BODY)],
                                  "lines": [{"baseline": 120.0, "x0": x0, "x1": x0 + pdf_w([text_run(wide, BODY)])}]})
    assert styles(el, SCALE)[0]["alignment"] == "JUSTIFIED"
    assert abs(text_rights(el, SCALE)[0] - edge * SCALE) <= 0.01
    assert box_right(el, SCALE) >= (edge + 10.0) * SCALE + emit.LINE_MARGIN - 0.01


# ---------------------------------------------------------------- one edge per paragraph

def test_each_paragraph_breaks_where_tex_did_when_one_edge_cannot() -> None:
    # dense v2 s3 ('Cambridge University / Press.') and s8 (dense-v11, '... before the IV
    # estimate.' on one line to the slide's edge): one paragraph's line, wider in Lato, set the
    # box's edge past where another paragraph's next word joins its line.
    el = column_list_of()
    measured = [slides_lines(p) for p in jobjs(el, "paragraphs")]
    wide = "Office hours move to Wednesday"
    x0 = (measured[1][1] + 2.0 - slides_w([text_run(wide, BODY)], SCALE)) / SCALE
    jarr(el, "paragraphs").append({**jobj(el, "paragraphs", 1), "bullet": None, "text_x0": x0, "wrap_limit": None,
                                  "runs": [text_run(wide, BODY)],
                                  "lines": [{"baseline": 170.0, "x0": x0, "x1": x0 + pdf_w([text_run(wide, BODY)])}]})
    rights = text_rights(el, SCALE)
    assert rights[2] >= measured[1][1] + 2.0 + emit.LINE_MARGIN - 0.01  # the wide line keeps its words
    for (widest, joins), right in zip(measured, rights):  # and every wrapped one its breaks
        assert widest + emit.LINE_MARGIN - 0.01 <= right <= joins - emit.LINE_MARGIN + 0.01
    assert "indentEnd" not in styles(el, SCALE)[0]  # (only where it is needed)


def test_a_word_tex_hyphenated_does_not_cost_a_line() -> None:
    # design v1 s2 (design-v9): 'Robots de-' / 'ployed in 9 sites' centred in a KPI tile. Slides
    # hyphenates nothing, 'deployed' did not fit after 'Robots' in a box as wide as TeX's lines,
    # and the caption took three lines, 'sites' below the tile.
    scale = emit.SLIDE_W / 453.54
    first, second = "Robots de-", "ployed in 9 sites"
    w1, w2 = pdf_w([text_run(first, 8.97)]), pdf_w([text_run(second, 8.97)])
    centre = 226.74
    el: JsonObject = {"id": "p1t1", "kind": "text", "role": "body", "code": False, "bbox": [centre - w2 / 2, 140.0, centre + w2 / 2, 165.5],
          "paragraphs": [{"align": "center", "level": 0, "size": 8.97, "text_x0": centre - w2 / 2, "tab_x0": None, "wrap_limit": None,
                          "bullet": None, "runs": [text_run("Robots deployed in 9 sites", 8.97)],
                          "lines": [{"baseline": 150.0, "x0": centre - w1 / 2, "x1": centre + w1 / 2},
                                    {"baseline": 161.0, "x0": centre - w2 / 2, "x1": centre + w2 / 2}]}]}
    assert emit.pdf_line_breaks(jobj(el, "paragraphs", 0), None, None) is None  # (the hyphen: not measured line by line)
    props = next(jobj(r, "createShape") for r in emit.text_box_requests(el, "s", "b", scale, FONTS) if "createShape" in r)
    width = pt_of(jobj(props, "elementProperties", "size", "width")) - 2 * emit.PAD_X
    assert width >= slides_w([text_run("Robots deployed", 8.97)], scale) + emit.LINE_MARGIN
    assert width <= w2 * scale + 8.97 * scale + 4  # (an em at most past the room it had)


# ---------------------------------------------------------------- compounds

def test_a_compound_longer_than_its_column_keeps_its_line_break() -> None:
    # dense v3 s5: 'Datenschutz-' over 'Folgenabschätzung' in a narrow column became one word
    # Slides cannot fit and breaks in the middle ('Datenschutz-Folgenabsc / hätzung').
    def text(*lines: str) -> str:
        par = new_paragraph([new_line([span(t, 11.4, 100.0 + 11 * i, 9.0)]) for i, t in enumerate(lines)], align="left", reason=None)
        return "".join(r["text"] for r in PageClassifier.runs(par, "", False, None, 0.0))

    assert text("Datenschutz-", "Folgenabschätzung") == "Datenschutz-\vFolgenabschätzung"
    assert text("Cases of Covid-", "19 rose in spring") == "Cases of Covid-19 rose in spring"  # (it fits: joined)
