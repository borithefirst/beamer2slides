"""The conversion's local half made faster without changing a byte, second round: notes and extract
reading one open PDF (`extract.Reading`), a fill's path flattened only when a glyph is asked about
under it, and `Line.main` worked out again only when what it reads changed."""

from pathlib import Path

import pytest

from beamer2slides.classify_model import Line, Rect, Span, new_line, new_span
from beamer2slides.extract import Reading, Visibility, _flatten, extract, extract_read
from beamer2slides.fonts import font_info
from beamer2slides.notes import prepare, prepare_read

HERE = Path(__file__).parent
DECKS = HERE / "decks" / "out"


def span(text: str, x0: float, size: float, baseline: float, font: str) -> Span:
    return new_span(id=text, text=text, font=font, size=size, color="#000000",
                    rect=Rect(x0, baseline - size, x0 + 5.0 * len(text), baseline), baseline=baseline,
                    horizontal=True, info=font_info(font), link=None, drawn=False, visual=None)


def fresh(line: Line) -> Span:
    """`Line.main` as it was before it remembered: worked out from scratch."""
    return line._find_main()


def test_main_follows_every_change_to_what_it_reads() -> None:
    a, b, c = span("short", 0.0, 10.0, 100.0, "CMR10"), span("a longer word", 30.0, 10.0, 102.0, "CMR10"), \
        span("x", 100.0, 10.0, 100.0, "CMR10")
    line = new_line([a, b, c])
    assert line.main is fresh(line) and line.main is b
    b.text = "w"  # (classify_lines rewrites a span's text in place)
    assert line.main is fresh(line)
    a.size = 20.0
    assert line.main is fresh(line) is a
    line.spans.append(span("an even longer span here", 200.0, 20.0, 104.0, "CMR10"))
    assert line.main is fresh(line)
    line.spans.remove(a)
    assert line.main is fresh(line)
    line.spans = list(reversed(line.spans))  # ties go to the first of equals: order counts
    assert line.main is fresh(line)
    big = span("∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑∑", 300.0, 21.0, 110.0, "CMEX10")  # (hangs off the baseline)
    line.spans.append(big)
    assert line.main is fresh(line) and line.main is not big
    big.font = "CMR10"
    assert line.main is fresh(line) is big
    big.baseline = 50.0
    assert line.main is fresh(line)
    assert line.size == fresh(line).size and line.baseline == fresh(line).baseline


def test_an_unchanged_line_answers_from_memory() -> None:
    line = new_line([span("one", 0.0, 10.0, 100.0, "CMR10"), span("two words", 20.0, 10.0, 100.0, "CMR10")])
    first = line.main
    memo = line._main_memo
    assert line.main is first and line._main_memo is memo


@pytest.mark.needs_decks("out/04_theme_blocks.pdf", "out/05_overlays_notes.pdf", "out/30_speaker_notes.pdf")
@pytest.mark.parametrize("name", ["04_theme_blocks.pdf", "05_overlays_notes.pdf", "30_speaker_notes.pdf"])
def test_notes_then_extract_on_one_reading_is_the_two_apart(name: str, tmp_path: Path) -> None:
    pdf = DECKS / name
    apart = prepare(pdf, tmp_path / "apart")
    want = extract(apart.pdf, apart.labels)
    with Reading(pdf) as reading:
        together = prepare_read(reading, tmp_path / "together")
        got = extract_read(reading, together.labels) if together.pdf == pdf else extract(together.pdf, together.labels)
        assert reading.page_chars(reading.doc[0]) == reading.page_chars(reading.doc[0])
        assert reading.page_chars(reading.doc[0])[0] is not reading.page_chars(reading.doc[0])[0]
    assert (together.notes, together.mode, together.labels, together.kept) == \
        (apart.notes, apart.mode, apart.labels, apart.kept)
    got["source"]["pdf"] = want["source"]["pdf"]  # (the notes-free PDF of each is its own folder's)
    assert got == want


@pytest.mark.needs_decks("out/04_theme_blocks.pdf")
def test_a_cover_is_flattened_once_and_as_before() -> None:
    with Reading(DECKS / "04_theme_blocks.pdf") as reading:
        for page in reading.doc:
            sight = Visibility(page)
            for k, cover in enumerate(sight.covers):
                if cover.paints == "path":
                    assert sight._polygons(k) == _flatten(cover.items)
                    assert sight._polygons(k) is sight._polygons(k)
