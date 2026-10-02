"""Speaker notes (`notes.py`, docs/speaker-notes.md): what reaches each slide's notes.

`tests/decks/30_speaker_notes.tex` writes a note per frame in every way beamer allows, and
`tests/decks/build.py` compiles it in every way beamer shows notes (its VARIANTS). `EXPECTED` is
what each frame's note must read, by hand; `BOARD` pins, per build, how the notes were found and
which frames still miss their note (none: a frame that starts to miss fails, one that starts to
match updates BOARD on purpose). Then the reading rules one by one on made-up lines, and the
`--tex` path: the source compiled once more with its notes shown, paired with a PDF without them.
"""

import os
import shutil
from pathlib import Path

import pytest

from beamer2slides import notes
from beamer2slides.devtools import notes_score
from beamer2slides.inverse import tex_env
from beamer2slides.notes import NoteLine, NotesMode, Prepared, SourceNotes
from beamer2slides.pdf import Char, char_box

DECKS = Path(__file__).parent / "decks"
TEX = DECKS / "30_speaker_notes.tex"
PLAIN_BUILD = DECKS / "out" / "30_speaker_notes.pdf"
HANDOUT_BUILD = DECKS / "out" / "30_speaker_notes-handout.pdf"

LONG = ("This note is long on purpose. It runs past the bottom of its note page, because beamer sets every "
        "note into one page and never breaks it onto a second one. The words that run off the page are still "
        "in the PDF, drawn below its edge, and a person who wrote them wants to see them in the speaker notes "
        "all the same. So the reader takes every word the note holds, on the page or below it. The rest of "
        "this note is filler that says the same thing again and again until it is long enough to overflow. "
        + " ".join(["One two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                    "sixteen seventeen eighteen nineteen twenty."] * 10)
        + " The last sentence of the long note.")

# frame title -> its note, as the slide's speaker notes must read it (None: no note)
EXPECTED: dict[str, str | None] = {
    "A plain sentence": "Remember to greet the audience before the first slide.",
    "Several paragraphs": "The first paragraph sets the scene for the talk and says why the topic matters to the "
                          "people in the room today.\nThe second paragraph gives the one number to remember.\n"
                          "The third paragraph is a short reminder to slow down.",
    "Lists in a note": "Points to make:\n• the parser reads the PDF\n• the classifier groups the lines\n"
                       "Then in order:\n1. compile\n2. convert\n3. sync",
    "Note items": "1. Ask who has used beamer before.\n2. Show the converted deck.\n3. Mention the speaker notes.",
    "Bold, italic and emphasis": "This is bold, this is italic and this is emphasised; a word halfway styled "
                                 "stays one word.",
    "Inline math": "The circle is x² + y² = r², the angle is α and aᵢ ≤ bᵢ for every i.",
    "Accents and quotes": "Café, naïve, Müller, Straße and señor — “curly quotes” and “TeX quotes”, 1–2 pages, "
                          "an efficient office workflow.",
    "Links": "See the talk page (https://example.com/talk) and https://example.org/notes.",
    "A long note": LONG,
    "Overlays": "A note on every step. Only on the second step.",
    "No note": None,
    "A note after the frame": "Written outside the frame, after it.",
    "Not numbered": "A note on a frame that takes no number.",
    "The last frame": "Thank the audience.",
}

# build -> (how prepare found the notes, the frames whose note is not EXPECTED's)
BOARD: dict[str, tuple[NotesMode | None, set[str]]] = {
    "notes": ("note pages", set()),
    "notes-right": ("second screen", set()),
    "notes-plain": ("note pages", set()),
    "notes-compressed": ("note pages", set()),
    "notes-xelatex": ("note pages", set()),
}


def build(variant: str) -> Path:
    return DECKS / "out" / "notes" / f"30_speaker_notes-{variant}.pdf"


def missed(board: list[notes_score.FrameNote]) -> set[str]:
    """The frames of EXPECTED whose note the board does not carry as written there."""
    got = {f.title: f.note for f in board}
    assert set(got) == set(EXPECTED), f"frames: {sorted(set(got) ^ set(EXPECTED))}"
    return {title for title, note in EXPECTED.items() if got[title] != note}


@pytest.mark.parametrize("variant", sorted(BOARD))
def test_the_notes_board(variant: str, tmp_path: Path) -> None:
    pdf = build(variant)
    if not pdf.exists():
        pytest.skip(f"{pdf.name} not built (tests/decks/build.py 30_speaker_notes)")
    mode, misses = BOARD[variant]
    assert notes.prepare(pdf, tmp_path).mode == mode
    board = notes_score.score(pdf, None)
    assert missed(board) == misses
    again = notes_score.board_of(notes_score.board_json(board), "board")
    assert again == board


@pytest.mark.needs_decks("out/30_speaker_notes.pdf")
def test_a_pdf_without_notes_has_none(tmp_path: Path) -> None:
    prepared = notes.prepare(PLAIN_BUILD, tmp_path)
    assert (prepared.mode, prepared.notes, prepared.pdf) == (None, {}, PLAIN_BUILD)
    assert prepared.kept == list(range(16))


# --- reading rules, on made-up lines ------------------------------------------------------------------

def line(text: str, *, x0: float, x1: float, y: float, label: str | None, text_x: float,
         first_word: float) -> NoteLine:
    return NoteLine(x0=x0, x1=x1, baseline=y, size=10.0, label=label, text_x=text_x, first_word=first_word, text=text)


def prose(text: str, x1: float, y: float) -> NoteLine:
    return line(text, x0=28.0, x1=x1, y=y, label=None, text_x=28.0, first_word=20.0)


def test_a_short_line_ends_its_paragraph() -> None:
    """Notes are justified without indent or room between paragraphs: a line ends a paragraph
    when the next line's first word would have fitted after it."""
    lines = [prose("The first paragraph runs to", 334.0, 84.0), prose("the next line.", 120.0, 97.5),
             prose("The second one is short.", 200.0, 111.0), prose("A third.", 60.0, 124.5)]
    assert notes.note_text(lines) == "The first paragraph runs to the next line.\nThe second one is short.\nA third."


def test_a_hyphenated_word_is_one_word_and_items_are_lines() -> None:
    lines = [prose("Points to make, all of them impor-", 334.0, 84.0), prose("tant:", 60.0, 97.5),
             line("• the first", x0=39.7, x1=120.0, y=115.0, label="•", text_x=47.0, first_word=5.0),
             line("• the second, which is long enough to run on to a", x0=39.7, x1=334.0, y=133.0, label="•",
                  text_x=47.0, first_word=5.0),
             line("second line", x0=47.0, x1=100.0, y=146.5, label=None, text_x=47.0, first_word=30.0),
             line("◦ nested under it", x0=56.0, x1=150.0, y=164.0, label="◦", text_x=63.0, first_word=5.0),
             prose("And prose again.", 120.0, 182.0)]
    assert notes.note_text(lines) == ("Points to make, all of them important:\n• the first\n"
                                      "• the second, which is long enough to run on to a second line\n"
                                      "  ◦ nested under it\nAnd prose again.")


def test_note_items_are_numbered_lines_and_a_number_starting_prose_is_not() -> None:
    """`\\note[item]`s stand at the note's left edge: their labels line up, so they are items. A
    lone line starting with a number is prose."""
    items = [line(f"{n}. Item {n}.", x0=37.0, x1=150.0, y=55.0 + 12 * n, label=f"{n}.", text_x=46.0,
                  first_word=8.0) for n in (1, 2)]
    assert notes.note_text(items) == "1. Item 1.\n2. Item 2."
    lone = [line("3. is the answer.", x0=28.0, x1=120.0, y=84.0, label="3.", text_x=36.0, first_word=8.0)]
    assert notes.paragraphs(lone)[0].item is False


def test_two_notes_on_one_frame_are_two_sentences() -> None:
    """Beamer sets two \\note commands of a frame one after the other with nothing between them."""
    assert notes.note_text([prose("A note on every step.Only on the second step.", 300.0, 84.0)]) == \
        "A note on every step. Only on the second step."


def test_scripts_are_unicode_where_unicode_has_them() -> None:
    assert (notes.superscript("2"), notes.superscript("n+1"), notes.superscript("*"), notes.superscript("′")) == \
        ("²", "ⁿ⁺¹", "*", "′")
    assert (notes.subscript("i"), notes.subscript("12"), notes.subscript("max"), notes.subscript("q")) == \
        ("ᵢ", "₁₂", "ₘₐₓ", "_q")
    assert notes.superscript("Ω") == "^Ω" and notes.superscript("qz") == "^(qz)"


def char(c: str, x: float, *, y: float, size: float, font: str) -> Char:
    advance = 0.5 * size
    return Char(c=c, font=font, size=size, color=0, alpha=255, origin=(x, y),
                box=char_box(x, y, 1.0, 0.0, advance, size, 0.75, -0.25), dir=(1.0, 0.0), obj=1, font_id=1,
                advance=advance, synthetic=False, ascent=0.75, descent=-0.25, exact_advance=True)


def word(text: str, x: float, *, y: float, size: float, font: str) -> list[Char]:
    return [char(c, x + 0.5 * size * k, y=y, size=size, font=font) for k, c in enumerate(text)]


def test_a_line_reads_its_scripts_accents_ligatures_and_style_changes() -> None:
    """x² is no 'x 2', OT1's accent before its letter is the accented letter, a ligature is its
    letters, and a word half in bold is one word."""
    glyphs = (word("x", 10.0, y=100.0, size=10.0, font="CMMI10") + word("2", 15.5, y=96.0, size=7.0, font="CMR7")
              + word("Caf´e", 30.0, y=100.0, size=10.0, font="CMR10")
              + word("e\ufb03", 60.0, y=100.0, size=10.0, font="CMR10")
              + word("half", 80.0, y=100.0, size=10.0, font="CMBX10") + word("way", 100.0, y=100.0, size=10.0,
                                                                              font="CMR10"))
    [only] = notes.split_lines(glyphs)
    assert notes.read_line(only, {}).text == "x² Café effi halfway"


def test_lines_split_where_the_pen_goes_down_or_back() -> None:
    glyphs = word("one", 10.0, y=100.0, size=10.0, font="CMR10") + word("two", 10.0, y=113.5, size=10.0, font="CMR10")
    assert [notes.read_line(l, {}).text for l in notes.split_lines(glyphs)] == ["one", "two"]


def test_a_link_says_its_address_when_its_words_do_not() -> None:
    glyphs = word("see", 10.0, y=100.0, size=10.0, font="CMR10") + word("here", 30.0, y=100.0, size=10.0,
                                                                        font="CMR10") \
        + word("x.org", 60.0, y=100.0, size=10.0, font="CMR10")
    links = notes.link_marks(glyphs, [((29.0, 90.0, 51.0, 102.0), "https://example.com/a"),
                                      ((59.0, 90.0, 85.0, 102.0), "https://x.org/")])
    [only] = notes.split_lines(glyphs)
    assert notes.read_line(only, links).text == "see here (https://example.com/a) x.org"


# --- pages ----------------------------------------------------------------------------------------

def test_plain_note_pages_are_told_by_their_labels() -> None:
    """Slides count frames, note pages their page number: ['1', '2'(note), '2', '4'(note)]."""
    assert notes.plain_notes([1, 3], ["1", "2", "2", "4"]) == [1, 3]
    # a deck without notes: only its first pages, in a row, carry their own number
    assert notes.plain_notes([1, 2], ["1", "2", "3", "3"]) == []
    assert notes.plain_notes([1], ["1", "2", "2", "3"]) == []
    # no labels at all: nothing to tell them by
    assert notes.plain_notes([1, 3], ["", "", "", ""]) == []


def test_a_frames_last_step_carries_every_steps_note() -> None:
    view = ("Overlays", ["First", "step", "Second", "step"])
    other = ("Elsewhere", ["Something", "else"])
    got = notes.carried({0: "Every step.", 1: "Every step. Only the second.", 2: "Every step.", 3: "Mine."},
                        ["9", "9", "9", "9"], {0: view, 1: view, 2: view, 3: other})
    assert got == {0: "Every step.", 1: "Every step. Only the second.", 2: "Every step. Only the second.",
                   3: "Mine."}
    assert notes.union("A.", "B.") == "A.\nB."


def test_pages_pair_in_order_or_frame_by_frame() -> None:
    assert notes.pair_pages(["1", "2", "2"], ["1", "2", "2"]) == [0, 1, 2]
    # a handout: one page per frame, the frame's last step
    assert notes.pair_pages(["1", "2", "3"], ["1", "2", "2", "2", "3"]) == [0, 3, 4]
    assert notes.pair_pages(["1", "2", "4"], ["1", "2", "2", "3"]) is None
    assert notes.word_share(["a", "b", "c", "d"], ["a", "b", "c", "x"]) == 0.75
    assert notes.word_share([], []) == 1.0


# --- notes from the source ------------------------------------------------------------------------

def require_tex(engine: str) -> None:
    if shutil.which(engine, path=tex_env()["PATH"]) is None:
        # CI says it has TeX (`$B2S_REQUIRE_TEX`): a job that skipped every compile would pass
        (pytest.fail if os.environ.get("B2S_REQUIRE_TEX") else pytest.skip)(f"{engine} not found")


def source_copy(tmp_path: Path, text: str) -> Path:
    tex = tmp_path / "src" / "talk.tex"
    tex.parent.mkdir(parents=True)
    tex.write_text(text, encoding="utf-8")
    return tex


@pytest.mark.needs_decks("30_speaker_notes.tex", "out/30_speaker_notes.pdf", "out/30_speaker_notes-handout.pdf",
                         "out/01_basic.pdf")
def test_notes_come_from_the_source_when_the_pdf_has_none(tmp_path: Path) -> None:
    """`convert deck.pdf --tex deck.tex`: the source compiled once more with notes shown, its note
    pages paired with the PDF's slides - the PDF as built, its handout (one page per frame, the
    last step's notes), and not another deck's PDF."""
    require_tex("pdflatex")
    tex = source_copy(tmp_path, TEX.read_text(encoding="utf-8"))
    compiled = notes.compile_notes(tex, tmp_path / "work")
    assert isinstance(compiled, Prepared), compiled
    for pdf in (PLAIN_BUILD, HANDOUT_BUILD):
        found = notes.match_notes(pdf, compiled, tex)
        assert found.outcome == "found", found.message
        frames = notes_score.score(pdf, None)
        got = {f.title: found.notes.get(f.page) for f in frames}
        assert got == EXPECTED
    other = notes.match_notes(DECKS / "out" / "01_basic.pdf", compiled, tex)
    assert other.outcome == "mismatch" and "01_basic.pdf" in other.message


def test_a_source_without_notes_or_engine_is_said_so(tmp_path: Path) -> None:
    body = "\\documentclass{beamer}\n\\begin{document}\n\\begin{frame}{A}Hi\\end{frame}\n\\end{document}\n"
    none = notes.notes_from_source(source_copy(tmp_path / "a", body), PLAIN_BUILD, tmp_path / "wa")
    assert (none.outcome, none.notes) == ("no-notes", {})
    missing = source_copy(tmp_path / "b", "% !TEX program = nosuchtex\n" + body.replace("Hi", "Hi\\note{x}"))
    found = notes.notes_from_source(missing, PLAIN_BUILD, tmp_path / "wb")
    assert isinstance(found, SourceNotes) and found.outcome == "no-engine" and "nosuchtex" in found.message


def test_only_a_named_source_is_compiled(tmp_path: Path) -> None:
    """A .tex beside the PDF is named as a hint, never compiled unasked."""
    pdf = tmp_path / "talk.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert notes.source_beside(pdf) is None
    (tmp_path / "talk.tex").write_text("% \\note{commented}\n\\begin{frame}\\end{frame}\n", encoding="utf-8")
    assert notes.source_beside(pdf) is None
    (tmp_path / "talk.tex").write_text("\\begin{frame}\\note{Say hi.}\\end{frame}\n", encoding="utf-8")
    assert notes.source_beside(pdf) == tmp_path / "talk.tex"
