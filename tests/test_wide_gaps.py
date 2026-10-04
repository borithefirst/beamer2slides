"""Word spaces TeX set wider than their line's others - after a sentence's end, a script, a
formula's italic letter - are a thick space and the word space in Lato (`classify_text.wide_gap`,
`widened`): a plain one is 0.192 em there against the PDF's 0.42-0.45 em, and the words beside it
ran together on Google's renderer (real-deck visual hunt)."""

from beamer2slides.classify_text import THICK_SPACE

from .test_inline_math_spacing import BODY, BOLD, SANS, SCRIPT_ITALIC, THIN, WORD, W, Line, runs, text

SENTENCE = 0.418 * BODY  # \nonfrenchspacing's space after '?' (real_ansible-meetup-201-beamer s26)
AFTER_SCRIPT = 0.432 * BODY  # a word space plus \scriptspace after S_t (real_linear-attention-a s33)


def script_line(x0: float) -> Line:
    return Line(x0).add("the state", SANS, 0.0, False).add("S", BOLD, WORD, False) \
        .add("t", SCRIPT_ITALIC, THIN, True).add("as", SANS, AFTER_SCRIPT, False) \
        .add("a matrix", SANS, WORD, False)


def test_the_space_after_a_script_keeps_its_width() -> None:
    first = script_line(0.0)
    line = script_line((W - first.x) / 2)  # (a centred line: its box grows both ways)
    t = text(runs(line.spans))
    assert f"t{THICK_SPACE} as a matrix" in t, t
    # (the thick space stays out of the script's run, and the words' own spaces are plain)
    assert t.count(THICK_SPACE) == 1, t


def test_a_line_with_no_room_keeps_its_plain_space() -> None:
    # the line ends at its paragraph's widest (alone, left-aligned, its right end near the page's
    # edge): what the thick space adds would widen its box past the page (27_text_fit p8)
    line = script_line(40.0)
    assert THICK_SPACE not in text(runs(line.spans))


def test_a_sentence_space_keeps_its_width() -> None:
    first = Line(0.0).add("Questions?", SANS, 0.0, False).add("Comments?", SANS, SENTENCE, False)
    line = Line((W - first.x) / 2).add("Questions?", SANS, 0.0, False).add("Comments?", SANS, SENTENCE, False)
    assert text(runs(line.spans)) == f"Questions?{THICK_SPACE} Comments?"


def centred(pieces: list[tuple[str, str, float]]) -> Line:
    """A line of (text, font, gap before) pieces, centred on the page (its box grows both ways)."""
    probe = Line(0.0)
    for words, font, gap in pieces:
        probe.add(words, font, gap, False)
    line = Line((W - probe.x) / 2)
    for words, font, gap in pieces:
        line.add(words, font, gap, False)
    return line


def test_ordinary_and_stretched_spaces_stay_plain() -> None:
    # a line whose every space is as wide (a justified line's stretched ones) has no wider gap
    wide = 0.45 * BODY
    line = centred([("one.", SANS, 0.0)] + [(w, SANS, wide) for w in ("two.", "three.", "four.", "five")])
    assert THICK_SPACE not in text(runs(line.spans))
    # a word space a little wider than the others is no sentence's
    line = centred([("one", SANS, 0.0), ("two", SANS, WORD), ("three:", SANS, WORD),
                    ("four", SANS, WORD + 0.04 * BODY), ("five", SANS, WORD)])
    assert THICK_SPACE not in text(runs(line.spans))
    # a wide gap TeX has no reason for (a stretched line not read as justified, a face whose own
    # space is wider: real_africa-remote-sens-30's Cyrillic captions) is no natural wider space
    line = centred([("рабочего", SANS, 0.0), ("процесса,", SANS, SENTENCE), ("Конго", SANS, SENTENCE)])
    assert THICK_SPACE not in text(runs(line.spans))


def test_serif_text_keeps_a_plain_space() -> None:
    # (Slides sets CM's serif in PT Serif, whose U+2008 emit does not measure)
    line = centred([("Questions?", "CMR10", 0.0), ("Comments?", "CMR10", SENTENCE)])
    assert text(runs(line.spans)) == "Questions? Comments?"