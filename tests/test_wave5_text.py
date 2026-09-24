"""Wave 5, fixer C (text, formulas, layout): the regressions wave 4 brought and two open ones."""

from beamer2slides import emit
from beamer2slides.emit import SLIDE_W

from .test_columns import paragraphs, span, text
from .test_emit_requests import FONTS, run_of


# -- a hyphen is where Slides may break ---------------------------------------------------------

def test_wrap_limit_takes_the_next_word_only_to_its_hyphen():
    """r3_scripts_ruxe s1: the next line opens with 'Санкт-Петербургский'. TeX's check (would the
    whole word have fitted?) joins the lines; but Slides breaks after the hyphen, so the box must
    end before the line above plus 'Санкт-', not plus the whole word (it took '2Санкт-' up)."""
    size = 11.0
    word = "Saint-Petersburg"
    spans = [span("The institute of physics and technology of the", 30.0, 100.0, size, w=250.0),
             span(word + " State University", 30.0, 113.5, size, w=180.0)]
    got = paragraphs(spans)
    assert len(got) == 1 and "the Saint-Petersburg" in text(got[0]), [text(p) for p in got]
    whole = 180.0 * len(word) / len(word + " State University")
    limit = got[0]["wrap_limit"]
    assert limit < 30.0 + 250.0 + 0.25 * size + 0.6 * whole, limit


def test_a_word_whose_hyphen_comes_before_a_digit_is_taken_whole():
    from beamer2slides.classify import hyphen_cut
    assert hyphen_cut("Saint-Petersburg") == 6
    assert hyphen_cut("COVID-19") is None and hyphen_cut("-5") is None and hyphen_cut("word-") is None


def test_slides_lines_joins_the_next_line_up_to_its_hyphen():
    """emit's measured box ends between the widest line and where a line would take its next
    word; Slides takes 'Saint-' alone."""
    scale = SLIDE_W / 362.83
    first, second = "The institute of physics and technology of the ", "Saint-Petersburg State University"
    p = {"lines": [{"x0": 30.0, "x1": 250.0, "baseline": 100.0}, {"x0": 30.0, "x1": 180.0, "baseline": 113.5}],
         "runs": [run_of(first + second)], "line_starts": [len(first), len(first + second)]}
    widest, joins = emit.slides_lines(p, scale, FONTS)
    upto = emit.slides_width([run_of(first + "Saint-")], scale, FONTS)
    assert abs(joins - (30.0 * scale + upto)) < 0.01, (joins, 30.0 * scale + upto)
    assert emit.first_break("a b-c d", 2, 7) == 4 and emit.first_break("a b-5 d", 2, 7) == 5


# -- a short inline formula is not broken by Slides ---------------------------------------------

MI, SY, RM = "LMMathItalic10-Regular", "LMMathSymbols10-Regular", "LMRoman10-Regular"


def formula_line(x: float = 30.0, y: float = 100.0, size: float = 11.0) -> list:
    """'and the mean wait is W = C − λ.': prose, then math spans 3 pt (0.27 em) apart."""
    out = [span("and the mean wait is", x, y, size, w=100.0)]
    x += 103.0
    for t, font in (("W", MI), ("=", RM), ("C", MI), ("−", SY), ("λ", MI), (".", "LMSans10-Regular")):
        out.append(span(t, x, y, size, w=7.0, font=font))
        x += 10.0 if t not in "λ" else 7.0
    return out


def test_spaces_inside_a_short_formula_are_no_break_spaces():
    """r2_fonts_segoe s3: Slides broke 'W_q = C/(cμ' from '− λ).' at the space before the minus,
    which TeX never does; TeX kept the formula on one line, so Slides must too."""
    got = [text(p) for p in paragraphs(formula_line())]
    assert got == ["and the mean wait is W = C − λ."], got


def test_a_formula_as_wide_as_half_the_line_keeps_its_spaces():
    """A long formula keeps breakable spaces, or Slides would cut it inside a word."""
    spans = formula_line(x=30.0)[:1]
    x = 133.0
    for t, font in (("W", MI), ("=", RM), ("C", MI), ("−", SY), ("λ", MI)):
        spans.append(span(t, x, 100.0, 11.0, w=30.0, font=font))
        x += 33.0
    got = [text(p) for p in paragraphs(spans)]
    assert len(got) == 1 and " " not in got[0], got


def test_text_italic_letters_inside_a_formula_are_in_its_hole():
    """r2_fonts_helvet s3: helvet's math sets its letters in the text italic, 'b N' one span of
    two letters; the formula hole stopped at the minus before it and ' b N)' became words, a gap
    each side of them."""
    size = 11.0
    y = 100.0
    spans = [span("We fit the capacity as", 30.0, y, size, w=110.0),
             span("Q", 143.0, y, size, w=7.0, font=MI), span("0", 150.0, y + 2.0, 5.0, w=3.0, font=RM),
             span("−", 156.0, y, size, w=8.0, font=SY),
             span(" b N", 164.0, y, size, w=16.0, font="LMSans10-Oblique"),
             span(")", 180.5, y, size, w=4.0, font=RM),
             span(", where the time is", 184.5, y, size, w=90.0)]
    got = paragraphs(spans)
    assert len(got) == 1, [text(p) for p in got]
    holes = [r for r in got[0]["runs"] if r.get("hole")]
    assert len(holes) == 1 and "b N" not in text(got[0]), text(got[0])
    assert holes[0]["hole_x0"] + holes[0]["hole"] >= 184.0, holes


def test_one_math_letter_among_words_keeps_the_word_spaces_around_it():
    spans = [span("each of", 30.0, 100.0, 11.0, w=40.0), span("c", 73.0, 100.0, 11.0, w=5.0, font=MI),
             span("physicians treats", 81.0, 100.0, 11.0, w=90.0)]
    assert [text(p) for p in paragraphs(spans)] == ["each of c physicians treats"]
