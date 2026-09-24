"""Justified prose and the width of wrapped text boxes (visual hunt r7, fixer S): which paragraphs
are justified (a ragged pair with a formula's gap, a bold line over a regular one, a centred pair,
a reference's hanging label are not), a JUSTIFIED paragraph never ending past its PDF lines' edge,
each paragraph of a box breaking where TeX did (indentEnd), and a compound longer than the
column keeping its line break."""

from beamer2slides import emit
from beamer2slides.classify import Line, PageClassifier, Paragraph

from .test_columns import span
from .test_emit_hunt import box_right, column_list
from .test_emit_requests import FONTS, pt_of, run_of

REGULAR, BOLD = "LMSans10-Regular", "LMSans10-Bold"
SIZE = 10.0


def line(words: list[str], y: float, x0: float = 30.0, gaps: list[float] | None = None, end: float | None = None,
         font: str = REGULAR) -> Line:
    """Words as spans (0.5 em a letter) `gaps` em apart - one width for all, stretched to end at
    `end`, or TeX's natural 0.333 em."""
    widths = [len(w) * 0.5 * SIZE for w in words]
    if gaps is None:
        g = 0.333 if end is None else (end - x0 - sum(widths)) / (len(words) - 1) / SIZE
        gaps = [g] * (len(words) - 1)
    spans, x = [], x0
    for w, wd, g in zip(words, widths, gaps + [0.0]):
        spans.append(span(w, x, y, SIZE, wd, font))
        x += wd + g * SIZE
    return Line(spans=spans)


def justified(*lines: Line) -> bool:
    return PageClassifier.is_justified(Paragraph(lines=list(lines)))


# ---------------------------------------------------------------- which paragraphs are justified

def test_stretched_lines_ending_together_are_justified():
    assert justified(line("Reefs grow slowly in warm and clear water".split(), 90, end=250.0),
                     line("where the light reaches down to the".split(), 103.5, end=250.0),
                     line("sea floor.".split(), 117))


def test_a_ragged_pair_with_a_formulas_gap_is_not_justified():
    # sci v1 s6, math v3 s2, econ v3 s3: TeX's space around a formula or a relation (0.8 em) pulled
    # the first line's mean space above the second's, and two ragged lines were set JUSTIFIED.
    first = line("the rate k grows with T for every sample".split(), 90, gaps=[0.333, 0.333, 0.8, 0.333, 0.333, 0.333, 0.333, 0.333])
    assert not justified(first, line("in the series".split(), 103.5))


def test_a_bold_line_over_a_regular_one_is_not_justified():
    # textfx v3 s8, themes v4 s8: a bold face's natural space is wider than the regular one's.
    assert not justified(line("Mental accounting and the budget".split(), 90, gaps=[0.37] * 4, font=BOLD),
                         line("people keep money in".split(), 103.5))


def test_a_last_line_longer_than_the_first_is_not_justified():
    # econ v2 s6, sci v3 s8: a centred or ragged pair whose second line runs past the first.
    assert not justified(line("Identifying assumption holds".split(), 90, end=172.0),
                         line("absent the policy the trends would be parallel".split(), 103.5))


def test_a_reference_with_a_hanging_label_is_not_justified():
    # control b1 s17, themes v2 s10: '[1]' hangs left of the wrapped lines, 0.6 em before the authors.
    first = line("[1] Knuth, D. E. The TeXbook, Addison".split(), 90, x0=20.0, gaps=[0.6, 0.333, 0.333, 0.333, 0.333, 0.333])
    assert not justified(first, line("Wesley, Reading, 1984".split(), 103.5, x0=36.0))


# ---------------------------------------------------------------- where a justified box ends

X0 = 34.97
SCALE = emit.SLIDE_W / 362.83
QUOTE = ["Coral reefs recover between bleaching", "events only when the water stays cool", "for a decade."]


def quote(stretch: float) -> dict:
    """A justified paragraph whose full lines TeX stretched `stretch` pt past their natural width
    (Lato sets the first 8.3 pt wider than CM Sans, the second 2.7 pt)."""
    natural = [emit.pdf_width([run_of(t)]) for t in QUOTE]
    edge = X0 + max(natural[:-1]) + stretch
    lines = [{"baseline": 70.0 + 13.5 * i, "x0": X0, "x1": edge if i < len(QUOTE) - 1 else X0 + w}
             for i, w in enumerate(natural)]
    return {"id": "p0t0", "kind": "text", "role": "body", "code": False, "bbox": [X0, 60.0, edge, 110.0], "paragraphs": [
        {"align": "left", "level": 0, "size": 10.91, "text_x0": X0, "tab_x0": None, "wrap_limit": None, "bullet": None,
         "runs": [run_of(" ".join(QUOTE))], "lines": lines, "justified": True}]}


def styles(el: dict, scale: float = SCALE) -> list[dict]:
    return [r["updateParagraphStyle"]["style"] for r in emit.text_box_requests(el, "b2s_s003", "b2s_s003_t1", scale, FONTS)
            if "updateParagraphStyle" in r]


def text_rights(el: dict, scale: float = SCALE) -> list[float]:
    """Where each paragraph's text ends (Slides pt): the box's text edge less its indentEnd."""
    right = box_right(el, scale)
    return [right - s.get("indentEnd", {"magnitude": 0.0})["magnitude"] for s in styles(el, scale)]


def test_a_justified_paragraph_never_ends_past_its_pdf_lines():
    # econ v2 s6, layout v1 s9, themes v2 s6: the box ended LINE_MARGIN past the widest line as
    # Slides sets it, past the PDF's edge, and JUSTIFIED stretched every line out to it - through
    # the frame or panel the PDF's lines stop at.
    for stretch, want in ((0.5, "START"), (8.0, "JUSTIFIED"), (12.0, "JUSTIFIED")):
        el = quote(stretch)
        edge = el["paragraphs"][0]["lines"][0]["x1"] * SCALE
        got = styles(el)[0]["alignment"]
        assert got == want, stretch
        if got == "JUSTIFIED":
            assert text_rights(el)[0] <= edge + 0.01, stretch
    # (12 pt: past what the words' CM advances can say, so the lines are flowed into the edge)
    assert emit.slides_lines(quote(12.0)["paragraphs"][0], SCALE, FONTS) is None


def test_a_justified_paragraph_beside_a_ragged_one_ends_at_its_own_edge():
    # dense v2 s7, layout v3 s4: the box is as wide as a longer line beside it needs; the
    # justified paragraph keeps the difference free and still ends where its PDF lines do.
    el = quote(8.0)
    edge = el["paragraphs"][0]["lines"][0]["x1"]
    wide = "Office hours move to Wednesday at noon"
    x0 = edge + 10.0 - emit.slides_width([run_of(wide)], SCALE, FONTS) / SCALE
    el["paragraphs"].append({**el["paragraphs"][0], "justified": False, "text_x0": x0, "runs": [run_of(wide)],
                             "lines": [{"baseline": 120.0, "x0": x0, "x1": x0 + emit.pdf_width([run_of(wide)])}]})
    assert styles(el)[0]["alignment"] == "JUSTIFIED"
    assert abs(text_rights(el)[0] - edge * SCALE) <= 0.01
    assert box_right(el, SCALE) >= (edge + 10.0) * SCALE + emit.LINE_MARGIN - 0.01


# ---------------------------------------------------------------- one edge per paragraph

def test_each_paragraph_breaks_where_tex_did_when_one_edge_cannot():
    # dense v2 s3 ('Cambridge University / Press.') and s8 (dense-v11, '... before the IV
    # estimate.' on one line to the slide's edge): one paragraph's line, wider in Lato, set the
    # box's edge past where another paragraph's next word joins its line.
    el = column_list()
    measured = [emit.slides_lines(p, SCALE, FONTS) for p in el["paragraphs"]]
    wide = "Office hours move to Wednesday"
    x0 = (measured[1][1] + 2.0 - emit.slides_width([run_of(wide)], SCALE, FONTS)) / SCALE
    el["paragraphs"].append({**el["paragraphs"][1], "bullet": None, "text_x0": x0, "wrap_limit": None, "runs": [run_of(wide)],
                             "lines": [{"baseline": 170.0, "x0": x0, "x1": x0 + emit.pdf_width([run_of(wide)])}]})
    rights = text_rights(el)
    assert rights[2] >= measured[1][1] + 2.0 + emit.LINE_MARGIN - 0.01  # the wide line keeps its words
    for (widest, joins), right in zip(measured, rights):  # and every wrapped one its breaks
        assert widest + emit.LINE_MARGIN - 0.01 <= right <= joins - emit.LINE_MARGIN + 0.01
    assert "indentEnd" not in styles(el)[0]  # (only where it is needed)


def test_a_word_tex_hyphenated_does_not_cost_a_line():
    # design v1 s2 (design-v9): 'Robots de-' / 'ployed in 9 sites' centred in a KPI tile. Slides
    # hyphenates nothing, 'deployed' did not fit after 'Robots' in a box as wide as TeX's lines,
    # and the caption took three lines, 'sites' below the tile.
    scale = emit.SLIDE_W / 453.54
    first, second = "Robots de-", "ployed in 9 sites"
    w1, w2 = emit.pdf_width([run_of(first, 8.97)]), emit.pdf_width([run_of(second, 8.97)])
    centre = 226.74
    el = {"id": "p1t1", "kind": "text", "role": "body", "code": False, "bbox": [centre - w2 / 2, 140.0, centre + w2 / 2, 165.5],
          "paragraphs": [{"align": "center", "level": 0, "size": 8.97, "text_x0": centre - w2 / 2, "tab_x0": None, "wrap_limit": None,
                          "bullet": None, "runs": [run_of("Robots deployed in 9 sites", 8.97)],
                          "lines": [{"baseline": 150.0, "x0": centre - w1 / 2, "x1": centre + w1 / 2},
                                    {"baseline": 161.0, "x0": centre - w2 / 2, "x1": centre + w2 / 2}]}]}
    assert emit.pdf_line_breaks(el["paragraphs"][0]) is None  # (the hyphen: not measured line by line)
    props = next(r["createShape"] for r in emit.text_box_requests(el, "s", "b", scale, FONTS) if "createShape" in r)
    width = pt_of(props["elementProperties"]["size"]["width"]) - 2 * emit.PAD_X
    assert width >= emit.slides_width([run_of("Robots deployed", 8.97)], scale, FONTS) + emit.LINE_MARGIN
    assert width <= w2 * scale + 8.97 * scale + 4  # (an em at most past the room it had)


# ---------------------------------------------------------------- compounds

def test_a_compound_longer_than_its_column_keeps_its_line_break():
    # dense v3 s5: 'Datenschutz-' over 'Folgenabschätzung' in a narrow column became one word
    # Slides cannot fit and breaks in the middle ('Datenschutz-Folgenabsc / hätzung').
    def text(*lines: str) -> str:
        par = Paragraph(lines=[Line(spans=[span(t, 11.4, 100.0 + 11 * i, 9.0)]) for i, t in enumerate(lines)])
        return "".join(r["text"] for r in PageClassifier.runs(par))

    assert text("Datenschutz-", "Folgenabschätzung") == "Datenschutz-\vFolgenabschätzung"
    assert text("Cases of Covid-", "19 rose in spring") == "Cases of Covid-19 rose in spring"  # (it fits: joined)
