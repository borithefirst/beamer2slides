"""Dot leaders: rows of spaced dots TeX fills to the measure (\\dotfill, a `\\dotline`), each its own
line at its PDF length, a bullet glyph opening one a bullet (real_defense-defense 8, 28, 41: three
'•. . . .' rows were one wrapped paragraph with its bullets as text, and a PT Serif row came out 12%
short). Synthetic pages, no PDF."""

import pytest

from beamer2slides.classify import PageClassifier
from beamer2slides.classify_lines import is_leader
from beamer2slides.compare import read_back_size
from beamer2slides.emit_metrics import LEADER_PITCH_EM, FontMapper
from beamer2slides.emit_model import run_of
from beamer2slides.emit_widths import slides_width_of
from beamer2slides.ir import Paragraph, TextElement
from beamer2slides.json_types import JsonObject
from beamer2slides.raw_types import RawSpan

SIZE = 10.909
ROMAN = "SFRM1095"
LEFT, RIGHT = 34.32, 335.69   # the defense deck's \dotfill rows
PITCH = 13.55                 # its baselines
SCALE = 720 / 362.83          # a 4:3 beamer page in Slides points


def leader(dots: int) -> str:
    return " ".join("." * dots)


def span(i: int, text: str, x0: float, x1: float, y: float) -> RawSpan:
    return {"id": f"p0s{i}", "text": text, "font": ROMAN, "size": SIZE, "color": "#000000", "alpha": 255,
            "origin": [x0, y], "bbox": [x0, y - 8.49, x1, y + 2.42], "dir": [1.0, 0.0], "smallcaps": False}


def text_boxes(spans: list[RawSpan]) -> list[TextElement]:
    page = PageClassifier({"index": 0, "label": "1", "size": [362.83, 272.13], "spans": spans, "images": [],
                           "drawings": [], "links": []}, SIZE)
    return [e for e in page.classify()["elements"] if e["kind"] == "text"]


def paragraphs(spans: list[RawSpan]) -> list[Paragraph]:
    return [p for box in text_boxes(spans) for p in box["paragraphs"]]


def words(p: Paragraph) -> str:
    return "".join(r["text"] for r in p["runs"])


def test_bulleted_leader_rows_are_items_of_their_own() -> None:
    # defense 8: '•. . . . (55 dots)' three times, one span each, the bullet against the first dot
    rows = [span(i, "•" + leader(55), LEFT, RIGHT, 68.72 + i * PITCH) for i in range(3)]
    ps = paragraphs(rows)
    assert len(ps) == 3
    for p in ps:
        bullet = p.get("bullet")
        assert bullet is not None and bullet["kind"] == "glyph" and bullet["text"] == "•"
        assert is_leader(words(p)) and "•" not in words(p)
        assert len(p["lines"]) == 1
        line = p["lines"][0]
        assert line["x1"] == pytest.approx(RIGHT, abs=0.1)


def test_leader_rows_are_never_joined() -> None:
    # defense 28: two \dotfill rows one under the other were one paragraph of two lines
    rows = [span(i, leader(49), 35.94, 299.08, 120.0 + i * PITCH) for i in range(2)]
    ps = paragraphs(rows)
    assert [len(p["lines"]) for p in ps] == [1, 1]
    assert all(is_leader(words(p)) for p in ps)


def test_a_line_ending_in_a_leader_takes_no_next_line() -> None:
    # 'Comments: . . . .' filled to the measure, words under it: TeX ended the row there
    rows = [span(0, "Comments: " + leader(40), LEFT, RIGHT, 100.0),
            span(1, "and here the next words of the page", LEFT, 210.0, 100.0 + PITCH)]
    ps = paragraphs(rows)
    assert [words(p).split(" ")[0] for p in ps] == ["Comments:", "and"]


def test_a_leader_row_carries_its_dot_pitch() -> None:
    dots = 45
    rows = [span(0, leader(dots), 36.83, 278.29, 95.82)]
    [p] = paragraphs(rows)
    [run] = p["runs"]
    # first dot to last over the dots between, in em of the run's size (a dot ~0.278 em wide)
    expected = (278.29 - 36.83 - 0.278 * SIZE) / (dots - 1) / SIZE
    assert run.get("pitch") == pytest.approx(expected, abs=0.002)
    # words among the dots are no leader row: no pitch
    [q] = paragraphs([span(0, "Motivation " + leader(dots), 36.83, 278.29, 95.82)])
    assert all(r.get("pitch") is None for r in q["runs"])


def run_dict(text: str, pitch: float | None, in_sentence: bool) -> JsonObject:
    out: JsonObject = {"text": text, "font": ROMAN, "family": "serif", "size": SIZE, "in_sentence": in_sentence}
    if pitch is not None:
        out["pitch"] = pitch
    return out


def test_a_leader_is_sized_to_its_pdf_pitch() -> None:
    # defense 41: 45 dots 0.4967 em apart in the PDF; PT Serif sets ". " 0.442 em apart
    fonts = FontMapper()
    pdf_pitch = 0.4967
    family, size = fonts.size_of(run_of(run_dict(leader(45), pdf_pitch, False)), SCALE)
    assert family == "PT Serif"
    slides_pitch = LEADER_PITCH_EM[("PT Serif", "regular")] * size   # Slides pt
    assert slides_pitch == pytest.approx(pdf_pitch * SIZE * SCALE, rel=0.005)
    # sized like words: dots 12% short (what the deck had)
    _, words_size = fonts.size_of(run_of(run_dict(leader(45), None, False)), SCALE)
    assert words_size < size * 0.9
    # a leader among other runs keeps their size
    _, among = fonts.size_of(run_of(run_dict(leader(45), pdf_pitch, True)), SCALE)
    assert among == words_size


@pytest.mark.parametrize("face", sorted(LEADER_PITCH_EM))
def test_every_measured_face_spaces_its_dots_as_the_pdf(face: tuple[str, str]) -> None:
    # PT Serif kerns a space against a period (~-0.1 em) in every face: bold and italic leaders
    # sized by ADVANCES came out as short as the regular one
    family, style = face
    font, kind = ("SFRM1095", "serif") if family == "PT Serif" else ("SFSS1095", "sans")
    run: JsonObject = {"text": leader(45), "font": font, "family": kind, "size": SIZE, "pitch": 0.4967,
                       "bold": "bold" in style, "italic": "italic" in style}
    got, size = FontMapper().size_of(run_of(run), SCALE)
    assert got == family
    assert LEADER_PITCH_EM[face] * size == pytest.approx(0.4967 * SIZE * SCALE, rel=0.005)


def test_a_sized_leader_is_measured_at_its_dot_pitch() -> None:
    # a box measured by ADVANCES' ". " (0.525 em in PT Serif) ran 19% past the row, off the slide
    pdf_pitch, dots = 0.4967, 45
    width = slides_width_of([run_of(run_dict(leader(dots), pdf_pitch, False))], SCALE, FontMapper())
    assert width == pytest.approx(((dots - 1) * pdf_pitch + 0.278) * SIZE * SCALE, rel=0.01)


def test_a_leader_compares_at_the_size_the_deck_reads_back() -> None:
    # pull's compare reads a sized leader as the deck says it, not as the PDF's 10.9
    pitched = read_back_size(run_dict(leader(45), 0.4967, False), SIZE)
    plain = read_back_size(run_dict(leader(45), None, False), SIZE)
    assert plain == SIZE
    assert pitched is not None and pitched > SIZE * 1.05
