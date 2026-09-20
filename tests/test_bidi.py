"""Hebrew and Arabic come back from the PDF the wrong way round (`beamer2slides.bidi`).

A page draws glyphs at places and says nothing about reading order, so a right-to-left run
reaches the pipeline written left to right - whether the writer drew it that way or PDFium's text
page turned it round. These tests are synthetic: they need the *characters*, not a font, so they
say nothing about whether this machine can set Hebrew, and they run everywhere.

The deck the defect was measured on is `hebrew-lesson` in the adopt corpus, whose word 'בסיפור'
came out 'רופיסב' with the geometry perfectly right and no check in this project able to see it.
"""

from beamer2slides import bidi, classify
from beamer2slides.fonts import font_info

ALEF, BET, GIMEL, DALET = "א", "ב", "ג", "ד"
# The word the defect was measured on, as the text page hands it over: left to right.
DRAWN = "".join(chr(c) for c in (0x5e8, 0x5d5, 0x5e4, 0x5d9, 0x5e1, 0x5d1))
READS = "".join(chr(c) for c in (0x5d1, 0x5e1, 0x5d9, 0x5e4, 0x5d5, 0x5e8))


def test_a_hebrew_word_is_put_back_the_way_it_is_read():
    assert bidi.logical_text(DRAWN) == READS


def test_putting_a_word_back_twice_is_where_it_started():
    # The transform is its own inverse, which is why it is right whichever side reversed the run:
    # PDFium's bidi pass, or a writer that drew the glyphs in visual order.
    assert bidi.logical_text(bidi.logical_text(DRAWN)) == DRAWN


def test_text_with_no_right_to_left_letters_is_left_exactly_as_it_is():
    for text in ("plain english 123", "", "   ", "f(x) = [a, b] <= 2", "é中文"):
        assert bidi.logical_text(text) == text


def test_the_words_of_a_line_drawn_as_one_span_turn_round_too():
    # A line whose words the extraction never split (the gaps are under the word-gap threshold)
    # is one span holding spaces, and reversing each word in place would leave the sentence
    # backwards - which is what it did until the two sizes were given one rule.
    drawn = f"{GIMEL}{BET} {DALET} {ALEF}"
    assert bidi.logical_text(drawn) == f"{ALEF} {DALET} {BET}{GIMEL}"


def test_a_number_inside_a_right_to_left_run_still_reads_forwards():
    assert bidi.logical_text(f"{BET}{ALEF} 1948") == f"1948 {ALEF}{BET}"


def test_a_latin_island_in_a_right_to_left_line_keeps_its_own_direction():
    assert bidi.logical_text(f"{GIMEL}{BET} PDF {ALEF}") == f"{ALEF} PDF {BET}{GIMEL}"


def test_a_right_to_left_island_in_a_latin_line_is_the_only_thing_that_turns():
    assert bidi.logical_text(f"see {BET}{ALEF} here") == f"see {ALEF}{BET} here"


def test_a_bracket_around_a_right_to_left_run_is_mirrored_back():
    assert bidi.logical_text(f"({BET}{ALEF})") == f"({ALEF}{BET})"


def test_a_bracket_in_a_latin_line_is_left_alone():
    assert bidi.logical_text(f"(see {BET}{ALEF})") == f"(see {ALEF}{BET})"


def test_a_combining_mark_stays_on_its_letter():
    # A vowel point is drawn over the letter before it and goes wherever that letter goes.
    drawn = BET + "ַ" + ALEF
    assert bidi.logical_text(drawn) == ALEF + BET + "ַ"


def test_looks_rtl_counts_letters_and_not_punctuation():
    assert bidi.looks_rtl(f"{ALEF}{BET}, 1948!")
    assert not bidi.looks_rtl("plain, 1948!")
    assert not bidi.looks_rtl("")


# ---------------------------------------------------------------- lines of spans

class Piece:
    """The least a span needs to be put in order: its text."""

    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return self.text


def order(*texts) -> list[str]:
    return [p.text for p in bidi.logical_spans([Piece(t) for t in texts])]


def test_the_spans_of_a_right_to_left_line_are_read_from_the_right():
    assert order(GIMEL, BET, ALEF) == [ALEF, BET, GIMEL]


def test_the_spans_of_a_latin_line_are_left_exactly_as_they_are():
    assert order("a", "b", "c") == ["a", "b", "c"]
    assert order("a") == ["a"]
    assert order() == []


def test_a_latin_span_inside_a_right_to_left_line_is_an_island():
    assert order(DALET, GIMEL, "PDF", BET, ALEF) == [ALEF, BET, "PDF", GIMEL, DALET]


def test_two_latin_spans_inside_a_right_to_left_line_keep_their_own_order():
    assert order(BET, "Google", "Slides", ALEF) == [ALEF, "Google", "Slides", BET]


def test_a_right_to_left_span_in_a_latin_line_is_the_only_thing_that_turns():
    assert order("a", BET, ALEF, "b") == ["a", ALEF, BET, "b"]


def test_a_neutral_span_between_two_hebrew_ones_goes_with_them():
    # A dash between two Hebrew words is Hebrew, so it stays between them rather than
    # ending up at the end of the line.
    assert order(GIMEL, "-", BET, ALEF) == [ALEF, BET, "-", GIMEL]


# ---------------------------------------------------------------- classify's seams

def span(text: str, x0: float, x1: float) -> classify.Span:
    return classify.Span(id="s", text=text, font="Arial", size=10.0,
                         color="#000000", rect=classify.Rect(x0, 0.0, x1, 10.0),
                         baseline=10.0, horizontal=True, info=font_info("Arial"))


def test_a_line_of_hebrew_spans_says_what_it_reads():
    line = classify.Line([span(GIMEL, 0, 10), span(BET, 12, 22), span(ALEF, 24, 34)])
    assert line.text == f"{ALEF} {BET} {GIMEL}"


def test_a_line_of_latin_spans_says_what_it_always_said():
    line = classify.Line([span("one", 0, 10), span("two", 12, 22)])
    assert line.text == "one two"


def test_runs_for_a_short_piece_of_hebrew_read_right_to_left():
    runs = classify.span_runs([span(GIMEL, 0, 10), span(BET, 12, 22), span(ALEF, 24, 34)])
    assert "".join(r["text"] for r in runs) == f"{ALEF} {BET} {GIMEL}"


def test_the_gap_between_two_spans_is_what_it_always_was_for_a_left_to_right_pair():
    # `gap_between` replaced `b.x0 - a.x1`, and their difference is 2 * (b.cx - a.cx), which is
    # positive for every pair the page draws in order: no left-to-right line can change.
    a, b = span("a", 0, 10), span("b", 14, 24)
    assert classify.gap_between(a, b) == b.rect.x0 - a.rect.x1 == 4
    # overlapping (a kerned pair, an accent): still the old answer
    c = span("c", 8, 18)
    assert classify.gap_between(a, c) == c.rect.x0 - a.rect.x1 == -2


def test_the_gap_on_a_right_to_left_line_is_measured_the_other_way():
    right, left = span(BET, 24, 34), span(ALEF, 0, 10)
    assert classify.gap_between(right, left) == 14
