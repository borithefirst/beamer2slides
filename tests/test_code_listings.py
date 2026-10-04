"""Code listings in a monospaced face on columns wider than its glyphs (listings' columns=fixed,
basewidth 0.6 em, in CMTT or Latin Modern Mono, whose glyphs are 0.525 em): Roboto Mono is sized
to the columns, not to the glyphs, or every line came out 14-16% short of the PDF's
(28_frames_code, ansible-meetup). Verbatim, whose glyphs fill their columns, is sized to its face
as before. Synthetic pages, no PDF."""

import pytest

from beamer2slides import ir
from beamer2slides.emit_metrics import CMTT_ADVANCE_EM, ROBOTO_MONO_ADVANCE_EM, FontMapper
from beamer2slides.pdf import Char, char_box

from .test_code_columns import CODE, LEADING, LEFT, PITCH, SIZE, raw_spans, text_boxes, tokens

FONTS = FontMapper()
GLYPH = CMTT_ADVANCE_EM * SIZE  # LMMono10's every advance


def mono_char(c: str, x: float, y: float) -> Char:
    return Char(c=c, font="LMMono10-Regular", size=SIZE, color=0, alpha=255, origin=(x, y),
                box=char_box(x, y, 1.0, 0.0, GLYPH, SIZE, 0.8, -0.2), dir=(1.0, 0.0), obj=0,
                font_id=0, advance=GLYPH, synthetic=False, ascent=0.8, descent=-0.2, exact_advance=True)


def fixed_mono(lines: list[str]) -> list[Char]:
    """Code lines as columns=fixed sets them in a 0.525 em face on 0.6 em columns: a token of n
    characters fills n columns, its glyphs spread by one glue before, between and after them."""
    chars: list[Char] = []
    for i, line in enumerate(lines):
        y = 100.0 + i * LEADING
        for col, token in tokens(line):
            glue = len(token) * (PITCH - GLYPH) / (len(token) + 1)
            x = LEFT + col * PITCH + glue
            for c in token:
                chars.append(mono_char(c, x, y))
                x += GLYPH + glue
    return chars


def verbatim(lines: list[str], indent: float) -> list[Char]:
    """Code lines whose glyphs fill their columns (verbatim, columns=flexible), each leading
    space `indent` pt wide."""
    chars: list[Char] = []
    for i, line in enumerate(lines):
        body = line.lstrip(" ")
        x0 = LEFT + (len(line) - len(body)) * indent
        chars += [mono_char(c, x0 + k * GLYPH, 100.0 + i * LEADING) for k, c in enumerate(body) if c != " "]
    return chars


def code_runs(chars: list[Char]) -> list[ir.Run]:
    [box] = text_boxes(raw_spans(chars))
    assert box.get("code") is True
    assert [line.strip() for p in box["paragraphs"]
            for line in "".join(r["text"] for r in p["runs"]).split("\n")] == [line.strip() for line in CODE]
    return [r for p in box["paragraphs"] for r in p["runs"] if r["text"].strip()]


def test_a_fixed_columns_listing_in_a_mono_face_is_sized_to_its_columns() -> None:
    # 28_frames_code 2-6: lstlisting in LMMono on 0.6 em columns came out 0.84 of its PDF width
    runs = code_runs(fixed_mono(CODE))
    assert {r["family"] for r in runs} == {"mono"}
    for r in runs:
        assert r.get("pitch") == pytest.approx(0.6, abs=0.005)
        family, z = FONTS(ir.run_json(r), 1.0)
        assert family == "Roboto Mono"
        assert z * ROBOTO_MONO_ADVANCE_EM == pytest.approx(PITCH, abs=0.06)


def test_glyphs_that_fill_their_columns_keep_their_face() -> None:
    # verbatim (and a URL): sized to the face's own advance, no pitch
    runs = code_runs(verbatim(CODE, GLYPH))
    assert {r.get("pitch") for r in runs} == {None}
    for r in runs:
        _, z = FONTS(ir.run_json(r), 1.0)
        assert z * ROBOTO_MONO_ADVANCE_EM == pytest.approx(GLYPH, abs=0.06)


def test_a_flexible_listing_keeps_its_face_whatever_its_indentation() -> None:
    # 11_research_talk 2: columns=flexible indents by 0.6 em columns but sets its words at the
    # face's 0.525 em; a block pitch between the two (0.5475 em) is no column of its letters
    runs = code_runs(verbatim(CODE, PITCH))
    assert {r.get("pitch") for r in runs} == {None}
