"""Inline math glyphs as classify writes them: which TeX math fonts hold blackboard bold and which
calligraphic capitals (bold cuts too), and a script's own script (f_{s'}: the prime over its
subscript) joined to its script's run. Script capitals are written in a math face of their own
(STIX Two Math, RSFS's in Libertinus Math) and read back as their run's; a relation's thick space
in Lato is U+2008 (tools/probe_math_glyphs.py)."""

import pytest

from beamer2slides.classify import Rect, Span, classify_page, new_span, span_runs
from beamer2slides.classify_text import THICK_SPACE, math_text, script_in_script, thick_spaces
from beamer2slides.deck_ir import ReadRun, capitals_as_their_runs, merge_runs
from beamer2slides.emit import FontMapper, text_box_requests
from beamer2slides.emit_widths import set_runs_of, slides_width
from beamer2slides.fonts import font_info, letter_face
from beamer2slides.google_types import slides_json
from beamer2slides.ir import Family, Run, Script
from beamer2slides.json_types import JsonObject
from beamer2slides.raw_types import RawSpan
from beamer2slides.text_layout import advance, wrap

from .deck_records import runs as target_runs
from .json_reads import jint, jobj, jstr

W, H = 362.83, 272.13
SANS, SANS_ITALIC, SCRIPT_SANS, SYMBOLS6 = "CMSS10", "CMSSI10", "CMSSI8", "CMSY6"


def span(text: str, x0: float, baseline: float, size: float, font: str) -> Span:
    w = len(text) * 0.5 * size
    return new_span(id="", text=text, font=font, size=size, color="#000000",
                    rect=Rect(x0, baseline - 0.75 * size, x0 + w, baseline + 0.25 * size),
                    baseline=baseline, horizontal=True, info=font_info(font), link=None, drawn=False, visual=None)


def after(s: Span, text: str, baseline: float, size: float, gap: float, font: str) -> Span:
    return span(text, s.rect.x1 + gap, baseline, size, font)


def runs(spans: list[Span]) -> list[Run]:
    raw: list[RawSpan] = [{"id": f"p0s{i}", "text": s.text, "font": s.font, "size": s.size, "color": s.color,
                           "alpha": 255, "origin": [s.rect.x0, s.baseline], "bbox": s.rect.as_list(), "dir": [1.0, 0.0],
                           "smallcaps": False} for i, s in enumerate(spans)]
    slide = classify_page({"index": 0, "label": "1", "size": [W, H], "spans": raw, "images": [], "drawings": [],
                           "links": [], "frame_label": None}, 10.91)
    texts = [e for e in slide["elements"] if e["kind"] == "text"]
    assert len(texts) == 1, [[r["text"] for p in e["paragraphs"] for r in p["runs"]] for e in texts]
    (par,) = texts[0]["paragraphs"]
    return par["runs"]


@pytest.mark.parametrize("font", ["txbsym", "TXBSYB", "txbsyb5", "PXBSYB", "txsym", "MSBM10"])
def test_blackboard_capitals_of_every_cut_are_double_struck(font: str):
    """newtx's bold \\mathbb (txbsym, \\boldsymbol\\mathbb{C}) and the bold txfonts/pxfonts AMS-b
    cuts hold msbm's layout (their 'a' is msbm's Gmir): C is ℂ, not a C or a script 𝒞."""
    assert math_text(font, "C Q Z P A")[0] == "ℂ ℚ ℤ ℙ 𝔸"


@pytest.mark.parametrize("font", ["txbsys", "txbsy7", "TXBSY", "pxbsys", "CMBSY10", "txsys", "CMSY10"])
def test_calligraphic_capitals_of_every_cut_are_script_letters(font: str):
    """The bold symbol fonts of newtx and txfonts hold cmsy's layout (their 'a' is turnstileright):
    \\boldsymbol{\\mathcal{L}} is ℒ, as CMBSY's is, not a plain L."""
    assert math_text(font, "L O")[0] == "ℒ 𝒪"


def test_a_prime_over_its_subscript_joins_the_subscript():
    """big-o-for-weighted s10: f_{s'}(w). TeX raises the prime (CMSY6) over its subscript s, so on
    the line it is 0.04 em high: neither a script of the line nor at its size. It was written
    unscripted at 6 pt, at the line's foot beside the subscript; it joins the subscript run."""
    f = span("f", 133.0, 89.2, 10.91, SANS_ITALIC)
    s = span("s", 136.65, 91.03, 7.97, SCRIPT_SANS)
    prime = span("′", 140.5, 88.76, 5.98, SYMBOLS6)
    w = span("(w)", 143.5, 89.2, 10.91, SANS)
    rs = runs([span("we have", 90.0, 89.2, 10.91, SANS), f, s, prime, w])
    by_text = {r["text"].strip(): r for r in rs}
    assert by_text["s"]["script"] == "sub"
    assert by_text["′"]["script"] == "sub", [(r["text"], r["script"]) for r in rs]
    # (6 pt is less than 0.6 of the line, which made a script's script as small as TeX set it:
    # a prime is as large as its subscript, ′'s tick being under half CMSY's prime: prime_size)
    assert by_text["′"]["size"] == by_text["s"]["size"] == 10.91
    assert by_text["(w)"]["script"] is None


def test_a_script_of_a_script_other_than_a_prime_keeps_its_own_size():
    """monodromy s9's p^{-1} in a subscript is drawn as small as TeX set it (script_size)."""
    bar = span("p|", 100.0, 89.2, 10.91, "CMMI10")
    p = span("p", 108.0, 91.6, 7.97, "CMMI8")
    minus = span("−", 112.2, 88.2, 6.08, "CMSY6")
    rs = runs([span("is true for", 50.0, 89.2, 10.91, SANS), bar, p, minus])
    assert next(r for r in rs if "−" in r["text"])["size"] == round(6.08 / 0.665, 2)


def test_a_prime_after_a_letter_of_the_line_is_set_at_the_line_size():
    """s' (CMSY8 raised 0.4 em after a body letter) is a superscript of the line, not a script's
    script; as a SUPERSCRIPT run Slides drew its ′ (a tick 0.48-0.72 em high, where TeX raises
    CMSY's prime to) 2/3 as large and 0.37 em higher: a speck over the s (big-o-for-weighted s6).
    Unscripted at the line's size, as a raised ring or asterisk is (RAISED_MARKS)."""
    s = span("s", 100.0, 89.2, 10.91, SANS_ITALIC)
    prime = span("′", 104.0, 84.8, 7.97, "CMSY8")
    assert script_in_script(prime, s, None, s) is None
    rs = span_runs([s, prime])
    assert [(r["text"], r["script"], r["size"]) for r in rs][-1] == ("′", None, 10.91)
    line = runs([span("then", 75.0, 89.2, 10.91, SANS), s, prime, span("is fixed", 108.0, 89.2, 10.91, SANS)])
    primed = next(r for r in line if "′" in r["text"])
    assert (primed["script"], primed["size"]) == (None, 10.91), [(r["text"], r["script"]) for r in line]


@pytest.mark.parametrize("marks", ["″", "‴", "′′"])
def test_every_prime_mark_is_unscripted(marks: str):
    s = span("f", 100.0, 89.2, 10.91, SANS_ITALIC)
    prime = span(marks, 104.0, 84.8, 7.97, "CMSY8")
    assert [r["script"] for r in span_runs([s, prime])] == [None, None]


def test_a_superscript_holding_more_than_primes_stays_a_superscript():
    s = span("f", 100.0, 89.2, 10.91, SANS_ITALIC)
    sup = span("′2", 104.0, 84.8, 7.97, "CMSY8")
    assert [r["script"] for r in span_runs([s, sup])] == [None, "super"]


def script_of_second(rise_em: float, gap: float) -> Script | None:
    line = span("x", 100.0, 100.0, 10.0, SANS_ITALIC)
    sub = span("i", 105.0, 101.7, 7.0, SCRIPT_SANS)
    mark = span("′", sub.rect.x1 + gap, sub.baseline - rise_em * sub.size, 5.0, SYMBOLS6)
    return script_in_script(mark, sub, "sub", line)


def test_a_script_of_a_script_must_follow_it_and_be_raised_or_lowered_from_it():
    assert script_of_second(0.3, 0.3) == "sub"
    assert script_of_second(0.0, 0.3) is None   # at the script's own height: not its script
    assert script_of_second(0.3, 5.0) is None   # well apart: not its neighbour
    assert script_of_second(1.5, 0.3) is None   # a line away


def test_what_follows_a_script_of_a_script_on_its_baseline_joins_it_too():
    """monodromy s9: p|_{p^{-1}(U)}. The − and the 1 of the subscript's superscript are both 6 pt
    on one baseline; the − joined the subscript and the 1 was left unscripted at 6 pt."""
    bar = span("p|", 100.0, 89.2, 10.91, "CMMI10")
    p = span("p", 108.0, 91.6, 7.97, "CMMI8")
    minus = span("−", 112.2, 88.2, 6.08, "CMSY6")
    one = span("1", minus.rect.x1 + 0.1, 88.2, 6.08, "CMR6")
    u = span("(U)", one.rect.x1 + 0.2, 91.6, 7.97, "CMR8")
    rs = runs([span("is true for", 50.0, 89.2, 10.91, SANS), bar, p, minus, one, u])
    scripts = {c: r["script"] for r in rs for c in r["text"]}
    assert scripts["p"] == scripts["−"] == scripts["1"] == scripts["U"] == "sub", \
        [(r["text"], r["script"]) for r in rs]


# ------------------------------------------------------------------ script capitals in a face of their own


@pytest.mark.parametrize("font", ["CMSY10", "ABCDEF+CMSY7", "EUSM10", "txsys", "TXBSY", "pxsys", "CMBSY10"])
def test_calligraphic_capitals_are_written_in_stix_two_math(font: str):
    """On Google's renderer STIX Two Math's script capitals are CMSY10's within 2% of advance and
    stroke and 6% of height; Lato's fallback drew them in a mix of heavy swash faces."""
    assert letter_face(font) == "STIX Two Math"


def test_rsfs_capitals_are_written_in_libertinus_math():
    """\\mathscr (RSFS10: 0.830 em, ink 0.886, stroke 0.031): Libertinus Math is the nearest served
    face (0.790, 0.886, 0.037); STIX Two Math's are CMSY's shape, Lato's fallback 15% narrower."""
    assert letter_face("RSFS10") == letter_face("ABCDEF+RSFS7") == "Libertinus Math"


def test_a_google_math_font_keeps_its_own_capitals():
    assert letter_face("STIXTwoMath-Regular") == "STIX Two Math"
    assert letter_face("LibertinusMath-Regular") == "Libertinus Math"


def math_run(text: str, font: str, family: Family, script: Script | None) -> Run:
    return {"text": text, "font": font, "family": family, "size": 10.91, "bold": False, "italic": False,
            "smallcaps": False, "color": "#000000", "link": None, "script": script}


def run_json(text: str, font: str, family: str) -> JsonObject:
    return {"text": text, "font": font, "family": family, "size": 10.91, "bold": False, "italic": False,
            "smallcaps": False, "color": "#000000", "link": None, "script": None}


SCALE = 1.98


def styled(reqs: list[JsonObject], field: str) -> list[JsonObject]:
    return [jobj(r, "updateTextStyle") for r in reqs
            if "updateTextStyle" in r and jstr(r, "updateTextStyle", "fields") == field]


def test_emit_writes_a_runs_script_capitals_in_their_face():
    """→ ℒ𝒪 (CMSY10): the run in its family's face, then ℒ𝒪 (three UTF-16 units: 𝒪 is astral)
    alone in STIX Two Math at the run's weight (→ stays in the run's: fonts.OPERATOR_FACES)."""
    words, rel = run_json("Let x", "CMSS10", "sans"), run_json("\xa0→\xa0ℒ𝒪 here", "CMSY10", "sans")
    par: JsonObject = {"align": "left", "level": 0, "size": 10.91, "text_x0": 30.0, "tab_x0": None, "wrap_limit": None,
                       "bullet": None, "lines": [{"baseline": 100.0, "x0": 30.0, "x1": 120.0}],
                       "runs": [words, rel]}
    el: JsonObject = {"id": "t", "kind": "text", "role": "body", "paragraphs": [par], "bbox": [30.0, 90.0, 120.0, 104.0]}
    reqs = [slides_json(r) for r in text_box_requests(el, "s", "b", SCALE, FontMapper())]
    inserted = "".join(jstr(r, "insertText", "text") for r in reqs if "insertText" in r)
    at = len(inserted[:inserted.index("ℒ")].encode("utf-16-le")) // 2
    (face,) = styled(reqs, "weightedFontFamily")
    assert jstr(face, "style", "weightedFontFamily", "fontFamily") == "STIX Two Math"
    assert jint(face, "style", "weightedFontFamily", "weight") == 400
    assert (jint(face, "textRange", "startIndex"), jint(face, "textRange", "endIndex")) == (at, at + 3)


def test_a_run_with_no_script_capital_gets_no_face_of_its_own():
    rel = run_json("x\xa0→\xa0y", "CMSY10", "sans")  # (→: no operator face either, fonts.OPERATOR_FACES)
    par: JsonObject = {"align": "left", "level": 0, "size": 10.91, "text_x0": 30.0, "tab_x0": None, "wrap_limit": None,
                       "bullet": None, "lines": [{"baseline": 100.0, "x0": 30.0, "x1": 80.0}], "runs": [rel]}
    el: JsonObject = {"id": "t", "kind": "text", "role": "body", "paragraphs": [par], "bbox": [30.0, 90.0, 80.0, 104.0]}
    assert styled([slides_json(r) for r in text_box_requests(el, "s", "b", SCALE, FontMapper())],
                  "weightedFontFamily") == []


def test_a_script_capital_is_as_wide_as_its_face_draws_it():
    plain = slides_width([run_json("x", "CMSY10", "serif")], SCALE, FontMapper())
    calligraphic = slides_width([run_json("xℒ", "CMSY10", "serif")], SCALE, FontMapper())
    assert plain is not None and calligraphic is not None
    (run,) = set_runs_of([run_json("x", "CMSY10", "serif")])
    size = FontMapper().size_of(run, SCALE)[1]
    assert calligraphic - plain == pytest.approx(0.736 * size, abs=0.01)
    assert advance("ℒ", {"fontFamily": "STIX Two Math"}, 10.0) == pytest.approx(7.36)


def read_run(text: str, face: str, size: float, font: str) -> ReadRun:
    (r,) = target_runs([{"text": text, "font": font, "family": "serif", "size": size}])
    return ReadRun(run=r, slides_font=face, slides_size=21.6)


def test_deck_ir_reads_a_script_capital_back_as_its_runs():
    """What emit wrote as one run ⊆ ℒ (CMSY10 in PT Serif, ℒ in STIX Two Math) reads back as two;
    the capital takes its neighbour's face and TeX font, so pull sees one run and no font change."""
    read = [read_run("x\xa0⊆\xa0", "PT Serif", 10.91, "CMR10"), read_run("ℒ𝒪", "STIX Two Math", 10.91, "STIXTwoMath"),
            read_run(" here", "PT Serif", 10.91, "CMR10")]
    (one,) = merge_runs(capitals_as_their_runs(read, FontMapper(), SCALE))
    assert (one.run.text, one.run.font, one.slides_font) == ("x\xa0⊆\xa0ℒ𝒪 here", "CMR10", "PT Serif")


def test_deck_ir_keeps_a_persons_words_in_a_letter_face():
    read = [read_run("x ", "PT Serif", 10.91, "CMR10"), read_run("Fourier", "STIX Two Math", 10.91, "STIXTwoMath")]
    assert [r.slides_font for r in capitals_as_their_runs(read, FontMapper(), SCALE)] == ["PT Serif", "STIX Two Math"]


# ------------------------------------------------------------------ a relation's thick space


def test_a_relations_no_break_spaces_in_lato_are_thick_spaces():
    """x < y in CMSS: TeX's thick space is 0.278 em; Lato's no-break space is 0.194, its U+2008
    0.278 (it does not break and draws nothing)."""
    rs = [math_run("x\xa0<\xa0y", "CMSS10", "sans", None)]
    thick_spaces(rs)
    assert rs[0]["text"] == f"x{THICK_SPACE}<{THICK_SPACE}y"


def test_thick_spaces_only_where_lato_sets_them():
    serif = [math_run("x\xa0<\xa0y", "CMR10", "serif", None)]        # PT Serif's U+2008 is 0.250 em
    script = [math_run("i\xa0<\xa0n", "CMSS8", "sans", "sub")]
    google = [math_run("x\xa0<\xa0y", "FiraSans-Regular", "sans", None)]
    apart = [math_run("x\xa0+\xa0y", "CMSS10", "sans", None)]       # a binary operator: a medium space
    breakable = [math_run("x\xa0= y", "CMSS10", "sans", None)]      # TeX may break after a relation
    for rs in (serif, script, google, apart, breakable):
        thick_spaces(rs)
    assert breakable[0]["text"] == f"x{THICK_SPACE}= y"
    assert [r[0]["text"] for r in (serif, script, google, apart)] == \
        ["x\xa0<\xa0y", "i\xa0<\xa0n", "x\xa0<\xa0y", "x\xa0+\xa0y"]


def test_a_thick_space_holds_to_the_space_before_it_as_a_no_break_space_does():
    """text_layout.wrap: Slides keeps a space and the no-break spaces after it together."""
    style: JsonObject = {"fontFamily": "Lato", "fontSize": 10.0}
    text = f"cccc aaaa {THICK_SPACE}bbbb"
    room = sum(advance(c, style, 10.0) for c in text[5:]) + 1.0
    lines = wrap(text, [style] * len(text), room)
    assert [text[a:b].strip() for a, b, _ in lines] == ["cccc", f"aaaa {THICK_SPACE}bbbb".strip()]
    assert advance(THICK_SPACE, style, 10.0) == pytest.approx(2.78)
