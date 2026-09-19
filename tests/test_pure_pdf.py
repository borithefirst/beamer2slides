"""The pure Python PDF reader (beamer2slides/pdf/pure, docs/pdf-from-scratch.md) against PDFium.

It is meant to answer what PDFium answers, so PDFium is the oracle: every call of the backend
contract except rendering, value for value up to float32 noise, and then the pipeline's own
products - extract and classify through it write the deck.json PDFium's backend writes, and a
raw.json whose numbers differ at most in the last rounded digit (PDFium computes in float32; the
reader rounds where PDFium stores a float, but not after every operation)."""

import dataclasses
import json
from pathlib import Path

import pytest

from beamer2slides import pdf
from beamer2slides.pdf.api import OBJ_IMAGE, PdfError

HERE = Path(__file__).parent
OUT = HERE / "decks" / "out"
# blocks and shadings, raster images every way, links and labels, hyphens at line ends, a Type 3
# bullet (metropolis under pdflatex), inline math, overlays on text
DECKS = [OUT / f"{name}.pdf" for name in ("04_theme_blocks", "23_raster_images", "01_basic", "14_misc",
                                           "12_metropolis_talk", "13_inline_math", "22_overlays_on_text")]
built = pytest.mark.skipif(not all(p.exists() for p in DECKS), reason="no test PDFs built")
pytest.importorskip("fontTools", reason="the pure reader reads font programs through fontTools")

IMAGE_FIELDS = ("px", "box", "matrix", "filters", "colorspace", "bpp", "dpi", "raw", "decoded_size", "clipped",
                "upright", "blended", "transparent")


def close(a, b, where=""):
    """Equal, floats up to float32 noise (PDFium's values are C floats)."""
    if isinstance(a, dict):
        assert isinstance(b, dict) and a.keys() == b.keys(), where
        for k in a:
            close(a[k], b[k], f"{where}[{k!r}]")
    elif isinstance(a, (list, tuple)):
        assert isinstance(b, (list, tuple)) and len(a) == len(b), f"{where}: {len(a)} != {len(b)}"
        for k, (x, y) in enumerate(zip(a, b)):
            close(x, y, f"{where}[{k}]")
    elif isinstance(a, float) or isinstance(b, float):
        assert a is not None and b is not None and abs(a - b) <= 1e-3 * max(1.0, abs(b)), f"{where}: {a!r} != {b!r}"
    else:
        assert a == b, f"{where}: {a!r} != {b!r}"


@built
@pytest.mark.parametrize("path", DECKS, ids=lambda p: p.stem)
def test_the_pure_reader_answers_what_pdfium_answers(path):
    ours, theirs = pdf.resolve("pure").open(path), pdf.resolve("pdfium").open(path)
    try:
        assert len(ours) == len(theirs)
        close(ours.metadata, theirs.metadata, "metadata")
        close(ours.named_dests(), theirs.named_dests(), "named_dests")
        close([ours.label(i) for i in range(len(ours))], [theirs.label(i) for i in range(len(theirs))], "labels")
        for a, b in zip(ours, theirs):
            where = f"page {a.index}"
            close((a.width, a.height), (b.width, b.height), where)
            close([dataclasses.astuple(o) for o in a.objects()], [dataclasses.astuple(o) for o in b.objects()],
                  f"{where} objects")
            for call in ("object_bounds", "drawings", "images", "links"):
                close(getattr(a, call)(), getattr(b, call)(), f"{where} {call}")
            chars_a, chars_b = a.chars(), b.chars()
            drop = lambda c: {k: v for k, v in dataclasses.asdict(c).items() if k != "font_id"}  # noqa: E731
            close([drop(c) for c in chars_a], [drop(c) for c in chars_b], f"{where} chars")
            close(a.glyph_widths([(c.font_id, c.c, c.size) for c in chars_a]),
                  b.glyph_widths([(c.font_id, c.c, c.size) for c in chars_b]), f"{where} glyph_widths")
            for po in a.objects():
                if po.type == OBJ_IMAGE:
                    x, y = a.embedded_image(po.id), b.embedded_image(po.id)
                    close({k: getattr(x, k) for k in IMAGE_FIELDS}, {k: getattr(y, k) for k in IMAGE_FIELDS},
                          f"{where} image {po.id}")
                    assert x.pixels is None and x.rendered is None, "the pure reader decodes no pixels"
    finally:
        ours.close()
        theirs.close()


def numbers_apart(a, b, where="") -> int:
    """How many numbers of two JSON trees differ (each by at most 0.01); anything else must be equal."""
    if isinstance(a, dict):
        assert isinstance(b, dict) and a.keys() == b.keys(), where
        return sum(numbers_apart(a[k], b[k], f"{where}.{k}") for k in a)
    if isinstance(a, list):
        assert isinstance(b, list) and len(a) == len(b), where
        return sum(numbers_apart(x, y, f"{where}[{i}]") for i, (x, y) in enumerate(zip(a, b)))
    if isinstance(a, (int, float)) and not isinstance(a, bool) and a != b:
        assert isinstance(b, (int, float)) and abs(a - b) <= 0.0101, f"{where}: {a!r} != {b!r}"
        return 1
    assert a == b, f"{where}: {a!r} != {b!r}"
    return 0


@built
def test_extract_and_classify_on_the_pure_reader_write_pdfiums_deck():
    """Measured on all 48 test decks when the reader was written: every deck.json equal, 2 numbers
    of the raw.json files 0.01 apart (docs/pdf-from-scratch.md)."""
    from beamer2slides.classify import classify
    from beamer2slides.extract import extract

    apart = 0
    for path in DECKS:
        runs = []
        for spec in ("pure", "pdfium"):
            with pdf.use_backend(spec):
                raw = json.loads(json.dumps(extract(path), default=str))
                deck = json.loads(json.dumps(classify(raw), default=str))
            runs.append((raw, deck))
        assert runs[0][1] == runs[1][1], f"{path.stem}: deck.json"
        apart += numbers_apart(runs[0][0], runs[1][0], f"{path.stem} raw")
    assert apart <= 5


@built
def test_the_pure_reader_refuses_a_page_it_cannot_draw_exactly():
    """Text, soft masks and shadings are not drawn yet: the page raises instead of coming back
    without them."""
    doc = pdf.resolve("pure").open(DECKS[0])
    with pytest.raises(PdfError, match="cannot render .+ yet"):
        doc[0].render(1.0)


# ---------------------------------------------------------------------- rendering (paths so far)

# pages the torture harness (devtools/render_torture.py) found apart once, shrunk
RENDER_CASES = {
    # DrawFillStrokePath: a translucent stroke is a knockout over the fill, on a sub-bitmap
    "fill_stroke_knockout": (b"q 0.2 0.5 0.8 rg 0.9 0.1 0.1 RG 8 w /A0 gs 20 20 m 180 40 l 100 130 l h B Q", [], 1.37, False),
    "fill_stroke_knockout_clear": (b"q 0.2 0.5 0.8 rg 0.9 0.1 0.1 RG 8 w /A2 gs 20 20 m 180 40 l 100 130 l h b* Q", [], 2, True),
    # b* closes to the path start even after a lone m: a round-capped dot
    "bstar_dot": (b"q 1 J 12 w 0 0 1 RG /A0 gs 95.1502 58.3982 m b* Q", [], 1, False),
    # a form's /BBox clip keeps its corner order, and cm composes in float32 step by step
    "form_bbox_unnormalised": (b"/X0 Do", [(b"/BBox [150 140 10 5]", b"0.3 0.6 0.2 rg 0 0 200 150 re f")], 1.37, False),
    "form_matrix_float32": (b"/X0 Do", [(b"/BBox [-99999 -99999 99999 99999] /Matrix [0.0000 1.0000 0.0662 -1.0322 17.0277 32.5160]",
                                         b"-1.6336 1.0873 0.4387 -0.1223 39.137 121.943 cm\n20.000 w 0 J 0 j 1.00 M\n"
                                         b"206 148 m 181 103 61.8 3 2 124 c -17 70 38 14 v S")], 2, False),
    # broken syntax, as CPDF_StreamContentParser reads it (found by render_torture --mutate).
    # After a valid `m`, path operators take the first numbers, extras dropped, missing ones stale:
    "fast_path_extra_numbers": (b"4 w 146 20 m 4.08 -13.8 29.29 l S", [], 1.37, False),
    "fast_path_stale_params": (b"4 w 136 190 m 20 30 l c m s", [], 1.37, False),
    # FX_Number: integers overflow to 0, any word of digits, signs and dots is a number
    "integer_overflow": (b"4 w 10 10 m 99999999999999999999 100 l 150 -4294967297 l 2147483648 +2147483648 l S", [], 1.37, False),
    "odd_numbers": (b"4 w 10 10 m --50 5..5 l -.5e3 100 l 100 . l S", [], 1.37, False),
    # CPDF_StreamParser::ReadNextObject: a nested '[' is nothing, a keyword in an array is skipped,
    # a dictionary with a non-name key is nothing, a stray '>>' is an operand that is no object
    "nested_array_top_level": (b"4 w 10 10 m 100 100 l [1 [1e-9] 53.6138 re S 0 0 m 50 20 l S", [], 1.37, False),
    "stray_dict_end": (b"4 w >> 0.215 0.812 RG 21.0000 81.7487 m 150 120 l S", [], 1.37, False),
    "dict_bad_key": (b"4 w << l h 144.0000 90.1253 m 20 20 l S", [], 1.37, False),
    "stray_dict_end_in_path": (b"4 w 10 10 m >> 104.1404 m 7.0000 y 150 150 l S", [], 1.37, False),
    "array_keyword_inside": (b"4 w [ 3 x 5 ] 0 d 10 10 m 150 120 l S", [], 1.37, False),
    # past 16 operands PDFium's ring buffer overwrites the second oldest, and reads the oldest last
    "seventeen_operands": (b"1 2 3 4 5 6 7 8 9 10 11 12 13 20 20 150 150 re f", [], 1, False),
    "twenty_operands": (b"4 w 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 170 20 20 150 150 re f", [], 1, False),
}


@pytest.mark.parametrize("name", RENDER_CASES)
def test_the_pure_renderer_draws_pdfiums_pixels(name):
    from beamer2slides.devtools.render_torture import compare
    content, forms, zoom, transparent = RENDER_CASES[name]
    assert compare(content, zoom, transparent, forms)[0] == 0


@pytest.mark.parametrize("forms,page,mutated", [(False, False, False), (True, False, False), (False, True, False),
                                                (True, True, True)], ids=["pages", "forms", "geometry", "mutated"])
def test_the_pure_renderer_survives_torture_seeds(forms, page, mutated):
    """A slice of the random pages the renderer was made exact on (5,500 seeds of pages, 3,000
    with forms, when it was written; 2,000 mutated): any pixel apart fails."""
    from beamer2slides.devtools.render_torture import case, compare
    apart = {}
    for seed in range(40):
        content, fs, zoom, transparent, geometry = case(seed, forms, page, mutated)
        n = compare(content, zoom, transparent, fs, geometry)[0]
        if n:
            apart[seed] = n
    flags = " --forms" * forms + " --page" * page + " --mutate" * mutated
    assert not apart, f"seeds apart (python tools/render_torture.py SEED 1{flags}): {apart}"


# ---------------------------------------------------------------------- PDFium's rules, one by one


def test_mutated_pages_read_as_pdfium_reads_them():
    """The torture pages with junk in them (render_torture --mutate), through the extract side of
    the contract. 202, 244, 288, 442 and 462 were apart: a form's box is the union of its children's
    stroke bounds, and those decide which side of a join grows by comparing a point with a line in
    float32 - a Bezier ending on its own control point lies on that line (2,500 seeds since)."""
    from beamer2slides.devtools.render_torture import case, pdf_bytes
    for seed in [*range(30), 202, 244, 288, 442, 462]:
        content, forms, _, _, geometry = case(seed, seed % 2 == 0, seed % 3 == 0, True)
        g = {k: v for k, v in geometry.items() if k != "clip"}
        data = pdf_bytes([content], forms=forms, **g)
        a, b = pdf.resolve("pure").open(data)[0], pdf.resolve("pdfium").open(data)[0]
        where = f"seed {seed}"
        close([dataclasses.astuple(o) for o in a.objects()], [dataclasses.astuple(o) for o in b.objects()], where)
        for call in ("object_bounds", "drawings", "images", "links", "chars"):
            close(getattr(a, call)(), getattr(b, call)(), f"{where} {call}")


def test_content_operands_are_read_as_pdfiums_stream_parser_reads_them():
    from beamer2slides.pdf.pure.syntax import Name, operations

    def ops(data):
        return [(op, list(args)) for op, args in operations(data)]

    # a nested array at the top level is nothing: its ']' closes the outer one, the next is stray
    assert ops(b"[1 [2] 3] x") == [("x", [[1, 2], 3, None])]
    # a keyword inside an array is skipped
    assert ops(b"[1 foo 2] d") == [("d", [[1, 2]])]
    # a dictionary whose key is no name is nothing, and parsing goes on after the bad key
    assert ops(b"<< 1 2 >> BDC") == [("BDC", [None, 2, None])]
    assert ops(b"/P << /MCID 3 /A [1 [2]] >> BDC") == [("BDC", [Name("P"), {"MCID": 3, "A": [1, [2]]}])]
    # FX_Number: signs and dots anywhere make a number; integers overflow to 0
    # ("--5" has no dot: an integer, which the second '-' ends before any digit)
    assert ops(b"--5 5..5 99999999999999999999 -2147483649 m") == [("m", [0, 5.0, 0, 0])]
    assert ops(b"--.5 l") == [("l", [-0.5])]
    # 17 operands: the 17th overwrites the second oldest and the oldest is read last
    assert ops(b" ".join(b"%d" % i for i in range(1, 18)) + b" re")[0][1][-5:] == [13, 14, 15, 16, 1]


def test_reals_are_c_floats():
    """CPDF_Number keeps a real as a float: 387.695 is 387.69500732..., which rounds up."""
    from beamer2slides.pdf.pure.syntax import Lexer

    value = Lexer(b"387.695").next()
    assert value != 387.695 and round(value, 2) == 387.7
    assert Lexer(b"12").next() == 12 and isinstance(Lexer(b"12").next(), int)


def test_glyph_names_follow_freetypes_full_glyph_list():
    """FreeType's psnames table is the legacy AGL plus the Zapf Dingbats names, not AGLFN: a
    TeX font's built-in `fi` is U+FB01, pifont's `a44` (\\ding{81}) is U+2731."""
    from beamer2slides.pdf.pure.fonts import unicode_from_adobe_name

    assert unicode_from_adobe_name("fi") == 0xFB01 and unicode_from_adobe_name("ff") == 0xFB00
    assert unicode_from_adobe_name("a44") == 0x2731
    assert unicode_from_adobe_name("uni0041") == 0x41 and unicode_from_adobe_name("A.sc") == 0x41
    assert unicode_from_adobe_name("nonsense") == 0


def test_bidi_segments_are_cfx_bidistrings():
    from beamer2slides.pdf.pure.textpage import (BIDI_LEFT, BIDI_LEFT_WEAK, BIDI_NEUTRAL, BIDI_RIGHT,
                                                 bidi_segments)

    units = [ord(c) for c in "ab שלום 12"]
    segments, rtl = bidi_segments(units)
    # PDFium's list starts with an empty segment too; one right segment against one left one makes
    # the line right to left, so the segments come in reverse (digits are weak: they don't count)
    assert rtl and segments[::-1] == [(0, 0, BIDI_NEUTRAL), (0, 2, BIDI_LEFT), (2, 1, BIDI_NEUTRAL),
                                      (3, 4, BIDI_RIGHT), (7, 1, BIDI_NEUTRAL), (8, 2, BIDI_LEFT_WEAK)]
    segments, rtl = bidi_segments([ord(c) for c in "left to right"])
    assert not rtl and [s[2] for s in segments if s[1]] == [BIDI_LEFT, BIDI_NEUTRAL] * 2 + [BIDI_LEFT]


STRESS = HERE / "decks" / "stress" / "out" / "ambiguous.pdf"


@pytest.mark.skipif(not STRESS.exists(), reason="stress decks not built (tests/decks/stress)")
def test_a_right_to_left_line_comes_out_in_pdfiums_order():
    """The stress deck's Hebrew line: PDFium writes a right-to-left run backwards (CloseTempLine),
    and a space the text page generates inside it becomes a character of its own."""
    ours, theirs = pdf.resolve("pure").open(STRESS), pdf.resolve("pdfium").open(STRESS)
    try:
        for a, b in zip(ours, theirs):
            text_a, text_b = [c.c for c in a.chars()], [c.c for c in b.chars()]
            if any("֐" <= c <= "׿" for c in text_b):
                assert text_a == text_b, f"page {a.index}"
                close([c.origin for c in a.chars()], [c.origin for c in b.chars()], f"page {a.index}")
                break
        else:
            pytest.fail("no Hebrew in the stress deck")
    finally:
        ours.close()
        theirs.close()
