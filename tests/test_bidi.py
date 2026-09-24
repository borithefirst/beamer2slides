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


def test_reads_rtl_asks_the_first_strong_letter():
    # Unicode's P2 over words already in reading order: which way the paragraph reads, which
    # is what Slides has to be told (emit writes direction RIGHT_TO_LEFT).
    assert bidi.reads_rtl(f"{ALEF}{BET} PDF {GIMEL}")
    assert not bidi.reads_rtl(f"PDF {ALEF}{BET} {GIMEL}"), "a Latin sentence with a Hebrew phrase in it"
    assert not bidi.reads_rtl("1948 (plain)") and not bidi.reads_rtl("")


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


def test_a_hebrew_paragraph_says_that_it_reads_right_to_left():
    line = classify.Line([span(GIMEL, 0, 10), span(BET, 12, 22), span(ALEF, 24, 34)])
    assert classify.Paragraph([line]).direction == "rtl"


def test_a_latin_paragraph_says_nothing_at_all():
    # The key is only there where it is true, as `deck_ir` writes it of a deck read back:
    # every deck this project had until now is a left-to-right one.
    line = classify.Line([span("one", 0, 10), span("two", 12, 22)])
    assert classify.Paragraph([line]).direction is None


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


# ---------------------------------------------------------------- a line read as Unicode shows it
# (visual hunt, rtl family: r3_scripts_rtl, lang_v2, lang_v3). The logical text written is the one
# whose Unicode display (UAX #9, which Slides runs) is the line the page shows: reversing words and
# islands one by one wrote ':7' for '7:', '13-12' for '12-13' and scrambled a formula's numbers.

def visual_to_logical(visual: str) -> str:
    return bidi.logical_line([visual], bidi.RIGHT)[0][1]


def test_a_number_before_a_colon_reads_the_way_the_title_shows_it():
    shown = "שופיח יצע :7 האצרה"
    assert visual_to_logical(shown) == "הרצאה 7: עצי חיפוש"


def test_a_range_of_numbers_is_not_turned_round():
    assert visual_to_logical("רפסב 13–12 םידומע") == "עמודים 12–13 בספר"


def test_a_number_at_the_end_inside_brackets_stays_inside_them():
    assert visual_to_logical("(20.11 ףיעס ואר)") == "(ראו סעיף 20.11)"


def test_a_formula_in_a_hebrew_line_is_one_left_to_right_island():
    assert visual_to_logical("םיבוביס O(log n) דע :הקיחמ") == "מחיקה: עד O(log n) סיבובים"


def test_a_formula_that_starts_with_a_number_gets_a_mark_to_keep_its_place():
    # Unicode would read '1.44' as a number of the Hebrew run and set it right of the formula; an
    # LRM ties it to the formula, and the sentence's full stop stays at its left end.
    got = visual_to_logical(".1.44 log2(n + 2) רתויה לכל אוה")
    assert got == "הוא לכל היותר ‎1.44 log2(n + 2)."
    assert bidi.display(got, bidi.RIGHT) == ".1.44 log2(n + 2) רתויה לכל אוה"


def test_latin_words_ending_in_a_full_stop_keep_it():
    got = visual_to_logical("תישילש הרודהמ ,Cormen et al.")
    assert got == "Cormen et al.‎, מהדורה שלישית"
    assert bidi.display(got, bidi.RIGHT) == "תישילש הרודהמ ,Cormen et al."


def test_brackets_around_a_transliteration_in_an_arabic_line():
    assert visual_to_logical("ةملك (raḥma) ةمحر") == "رحمة (raḥma) كلمة"


def test_a_line_opening_on_a_latin_name_reads_as_its_page_does():
    # 'AVL tree' in a Hebrew title: one strong letter each way, so the line alone could read either
    # way; the page, Hebrew throughout, says right to left.
    assert bidi.line_base(["AVL", "ץע"]) == bidi.LEFT
    prior = bidi.page_direction(["םולש םלוע", "AVL", "שופיח ץע"])
    assert prior == bidi.RIGHT
    assert bidi.line_base(["AVL", "ץע"], prior) == bidi.RIGHT


def test_display_is_what_logical_line_inverts():
    for logical in ("הרצאה 7: עצי חיפוש", "עץ AVL", "מחיקה: עד O(log n) סיבובים", "plain"):
        shown = bidi.display(logical, bidi.RIGHT)
        assert bidi.display(visual_to_logical(shown), bidi.RIGHT) == shown


def test_reads_rtl_counts_a_right_to_left_mark_as_strong():
    assert bidi.reads_rtl("‏O(1) בממוצע")
    assert not bidi.reads_rtl("O(1) בממוצע")


# ---------------------------------------------------------------- classify's lines of spans

def drawn(visual: str, x0: float, x1: float) -> classify.Span:
    s = span(bidi.logical_text(visual), x0, x1)
    s.visual = visual
    return s


def removal_line() -> list[classify.Span]:
    """'Deletion: up to O(log n) rotations', drawn as the page draws it, left to right."""
    return [drawn("םיבוביס", 0, 35), drawn("O", 38, 45), drawn("(", 45, 48), drawn("log", 48, 60),
            drawn(" n", 60, 68), drawn(")", 68, 71), drawn("דע", 74, 84), drawn(":הקיחמ", 87, 117)]


def test_the_spans_of_a_hebrew_line_with_a_formula_read_as_the_line_does():
    spans = removal_line()
    classify.PageClassifier.read_lines([classify.Line(spans)], spans)
    text = "".join(r["text"] for r in classify.span_runs(spans))
    assert text == "מחיקה: עד O(log n) סיבובים"
    assert " " not in text, "logical neighbours that are not page neighbours are not a wide gap"


def test_the_room_between_two_spans_read_in_turn_is_the_join_between_them():
    spans = removal_line()
    classify.PageClassifier.read_lines([classify.Line(spans)], spans)
    # 'עד' is read just before 'O', eight spans away: the room is the gap left of 'עד' (after ')')
    assert classify.gap_between(spans[6], spans[1]) == 3
    assert classify.gap_between(spans[2], spans[3]) == 0  # '(' then 'log': no join between them


def test_a_latin_word_opening_a_hebrew_title_leaves_it_right_to_left():
    title = [drawn("AVL", 0, 20), drawn("ץע", 24, 34)]
    other = [drawn("שופיח", 0, 30), drawn("ץע", 34, 44)]
    lines = [classify.Line(title), classify.Line(other)]
    classify.PageClassifier.read_lines(lines, title + other)
    assert classify.Line(title).text == "עץ AVL"
    assert classify.Paragraph([classify.Line(title)]).direction == "rtl"


def test_a_cell_of_a_hebrew_line_that_starts_with_latin_is_marked_right_to_left():
    # emit asks a cell's own text which way it reads (Unicode's P2): 'O(1) on average' would be
    # set left to right with the Hebrew word at its right end.
    cell = [drawn("עצוממב", 0, 30), drawn("O(1)", 34, 54)]
    classify.PageClassifier.read_lines([classify.Line(cell)], cell)
    text = "".join(r["text"] for r in classify.span_runs(cell))
    assert text == "‏O(1) בממוצע"
    assert bidi.lead_mark([span("plain", 0, 10)]) == ""


# ---------------------------------------------------------------- what the text page did

from beamer2slides.pdf import Char, char_box  # noqa: E402


def char(c: str, x: float, advance: float = 5.0, obj: int = 1, font: str = "Frank") -> Char:
    return Char(c, font, 10.0, 0, 255, (x, 50.0), char_box(x, 50.0, 1.0, 0.0, advance, 10.0, 0.8, -0.2),
                (1.0, 0.0), obj, 1, advance)


def test_a_word_whose_letters_come_back_at_falling_x_is_put_left_to_right():
    # XeTeX draws a Hebrew word in visual order; PDFium's text page turns the run round, so its
    # letters come back from the right.
    chars = [char(ALEF, 30), char(BET, 20), char(GIMEL, 10)]
    assert [ch.c for ch in bidi.visual_chars(chars)] == [GIMEL, BET, ALEF]


def test_a_bracket_the_text_page_mirrored_after_hebrew_is_given_back():
    chars = [char(BET, 10), char(")", 20), char("a", 25)]
    assert [ch.c for ch in bidi.visual_chars(chars)] == [BET, "(", "a"]


def test_a_page_with_no_right_to_left_letters_is_left_alone():
    chars = [char("b", 20), char(")", 10)]
    assert bidi.visual_chars(chars) is chars


def test_arabic_letters_drawn_one_by_one_touch_again():
    # LuaTeX draws a word as it is read, at falling x; PDFium gives each Arabic glyph of a font
    # with no widths for them 0.21 em, and the gaps that left made every letter a word.
    word = "رحمة"
    chars = [char(c, 30 - 5 * i, advance=2.26) for i, c in enumerate(word)]
    out = bidi.visual_chars(chars)
    assert "".join(ch.c for ch in out) == word[::-1]
    assert [round(ch.advance, 2) for ch in out] == [5.0, 5.0, 5.0, 2.26]


def test_a_space_on_the_line_still_ends_a_word():
    chars = [char("ر", 30, advance=2.26), char("ح", 25, advance=2.26), char(" ", 22, advance=0),
             char("م", 18, advance=2.26), char("ة", 13, advance=2.26)]
    out = [ch for ch in bidi.visual_chars(chars) if ch.c.strip()]
    advances = {ch.c: round(ch.advance, 2) for ch in out}
    assert advances["م"] == 2.26, "the space between the words keeps them apart"
    assert advances["ح"] == 5.0 and advances["ة"] == 5.0
