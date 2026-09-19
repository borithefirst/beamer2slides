"""The pure Python PDF reader (beamer2slides/pdf/pure, docs/pdf-from-scratch.md) against PDFium.

It is meant to answer what PDFium answers, so PDFium is the oracle: every call of the backend
contract except rendering, value for value up to float32 noise, and then the pipeline's own
products - extract and classify through it write the deck.json PDFium's backend writes, and a
raw.json whose numbers differ at most in the last rounded digit (PDFium computes in float32; the
reader rounds where PDFium stores a float, but not after every operation)."""

import dataclasses
import json
import re
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
    """Images are not drawn yet: the page raises instead of coming back without them."""
    doc = pdf.resolve("pure").open(DECKS[1])
    with pytest.raises(PdfError, match="cannot render images yet"):
        doc[0].render(1.0)


@built
def test_whole_beamer_pages_render_as_pdfium_renders_them():
    """Text, paths, forms, soft masks and shadings together: every page of the test decks that the
    reader does not refuse is PDFium's bitmap byte for byte (all four pages of 04_theme_blocks, the
    first whole pages, when text and shadings met)."""
    import numpy as np
    drawn = 0
    for path in DECKS:
        ref, pure = pdf.resolve("pdfium").open(path), pdf.resolve("pure").open(path)
        try:
            for i in range(len(ref)):
                try:
                    ours = pure[i].render(1.37)
                except PdfError:
                    continue
                assert np.array_equal(ours, ref[i].render(1.37)), f"{path.stem} page {i}"
                drawn += 1
        finally:
            ref.close()
            pure.close()
    assert drawn >= 4


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


# ---------------------------------------------------------------------- transparency

_GROUND = b"q 0.9 0.6 0.1 rg 10 10 150 110 re f Q\n"
# (page content, items, zoom, transparent): items as devtools/render_torture_transparency.pdf_bytes
# takes them; /A0-/A4 are constant alphas 0.5 0.25 0.8 0 1, /B<k> the blend mode BLENDS[k]
TRANSPARENCY_CASES = {
    # CPDF_ContentParser::CheckClip: a form's /BBox that holds its fill drops the clip, which moves
    # the fill's edge pixels where both edges coincide (torture seeds 172 and 241)
    "check_clip": (b"q\n0.6381 -1.2322 0.0402 -1.8177 83.337 100.751 cm\n/X0 Do\nQ",
                   [("X", b"/BBox [0 0 200 150]", b"q\n0.0000 42.3000 60.0000 90.2640 re f\nQ", b"")], 1, False),
    "check_clip_group": (b"q\n1.0470 1.0474 -0.5703 -0.6611 48.129 2.196 cm\n/X0 Do\nQ",
                         [("X", b"/BBox [0 0 200 150] /Group << /S /Transparency >>",
                           b"q\n112.3008 51.2495 82.7500 41.3097 re f\nQ", b"")], 0.5, False),
    "luminosity_mask": (_GROUND + b"q /S0 gs 0.1 0.5 0.2 rg 0 0 200 150 re f Q",
                        [("S", b"/BBox [0 0 200 150] /Group << /S /Transparency /CS /DeviceRGB >>",
                          b"0.8 0.3 0.1 rg 30 20 120 90 re f 1 g 60 40 m 170 60 l 90 140 l f",
                          b"/S /Luminosity /BC [0.2 0.2 0.2]")], 1, False),
    "alpha_mask": (_GROUND + b"q /S0 gs 0 0 1 rg 20 20 160 110 re f Q",
                   [("S", b"/BBox [0 0 150 120] /Matrix [1 0 0.4 1 -10 0] /Group << /S /Transparency /CS /DeviceGray /I true >>",
                     b"/A1 gs 0.5 g 40 30 90 60 re f", b"/S /Alpha")], 2, True),
    "isolated_group_alpha": (_GROUND + b"q /A0 gs /X0 Do Q",
                             [("X", b"/BBox [0 0 200 150] /Group << /S /Transparency /I true >>",
                               b"0 0.6 0.3 rg 30 30 100 60 re f 0.8 0 0 rg 90 50 m 180 140 l 60 120 l f", b"")], 1.37, False),
    # a non-isolated group starts from the device's pixels and blends into them
    "non_isolated_group_blend": (_GROUND + b"/X0 Do",
                                 [("X", b"/BBox [0 0 200 150] /Group << /S /Transparency >>",
                                   b"q /B1 gs 0.2 0.3 0.9 rg 30 30 100 60 re f Q "
                                   b"q /B10 gs /A2 gs 0.9 0.9 0.2 rg 70 50 90 80 re f Q", b"")], 1, False),
    # a blend on an opaque page: GetBackdrop draws the page again up to the object
    "page_blend_backdrop": (_GROUND + b"q /B7 gs 0.3 0.6 0.9 rg 40 30 120 90 re f Q q /B3 gs 0.5 g 8 w 20 20 m 180 130 l S Q",
                            [], 1.37, False),
    "page_blend_transparent": (_GROUND + b"q /B9 gs /A0 gs 0.3 0.6 0.9 rg 40 30 120 90 re f Q", [], 1, True),
    "non_separable": (_GROUND + b"q /B12 gs 0.1 0.8 0.4 rg 20 20 70 100 re f Q q /B13 gs 0.9 0.1 0.6 rg 60 20 70 100 re f Q "
                      b"q /B14 gs 0.2 0.2 0.9 rg 100 20 70 100 re f Q q /B15 gs 0.6 0.9 0.1 rg 140 20 50 100 re f Q", [], 1, False),
    # the group's bitmap is the form's rect, the union of its children's stroke boxes: a `v` whose
    # first control is the current point joins at a doubled point, and only float32 rounding
    # decides which side the miter goes (torture seeds 797 and 2849)
    "stroke_box_float32": (b"/X0 Do", [("X", b"/BBox [-5000 -5000 5000 5000] /Group << /S /Transparency /I true >>",
                                       b"32.0000 198.0000 m -1422.6323 155.5000 80.5374 124.9622 101.1379 11.6032 c "
                                       b"121.0000 76.5000 184.4316 78.0000 v S", b"")], 1.37, False),
}


@pytest.mark.parametrize("name", TRANSPARENCY_CASES)
def test_the_pure_renderer_draws_pdfiums_transparency(name):
    from beamer2slides.devtools.render_torture_transparency import compare
    content, items, zoom, transparent = TRANSPARENCY_CASES[name]
    assert compare(content, zoom, transparent, items)[0] == 0


@pytest.mark.parametrize("page", [False, True], ids=["pages", "geometry"])
def test_the_pure_renderer_survives_transparency_torture_seeds(page):
    """A slice of the random pages with soft masks, groups, alphas and blend modes the renderer
    was made exact on (6,000 seeds, half with page geometry, when it was written)."""
    from beamer2slides.devtools.render_torture_transparency import case, compare
    apart = {}
    for seed in range(40):
        content, items, zoom, transparent, geometry = case(seed, page)
        n = compare(content, zoom, transparent, items, geometry)[0]
        if n:
            apart[seed] = n
    flags = " --page" * page
    assert not apart, f"seeds apart (python tools/render_torture_transparency.py SEED 1{flags}): {apart}"


def test_the_pure_renderer_refuses_what_it_cannot_draw_exactly_yet():
    """Transfer functions (/TR on the ExtGState or on a soft mask) are not ported: refused, not ignored."""
    from beamer2slides.devtools.render_torture_transparency import pdf_bytes
    from beamer2slides.pdf.pure.backend import PureBackend
    mask = [("S", b"/BBox [0 0 200 150] /Group << /S /Transparency /CS /DeviceGray >>", b"0.5 g 0 0 100 100 re f",
             b"/S /Luminosity /TR << /FunctionType 2 /Domain [0 1] /C0 [1] /C1 [0] /N 1 >>")]
    page = PureBackend().open(pdf_bytes(b"q /S0 gs 0 0 1 rg 0 0 200 150 re f Q", mask))[0]
    with pytest.raises(PdfError, match="transfer functions"):
        page.render(1.0)


# ---------------------------------------------------------------------- shadings

_TINT = b"<< /FunctionType 2 /Domain [0 1] /C0 [0.1] /C1 [1] /N 1.7 >>"
_PS = b"{ dup 360 mul sin abs 1 index 3 exp 2 index 0.5 gt { 0.2 } { 0.9 } ifelse 4 -1 roll pop }"
_SAMPLES = bytes(range(0, 256, 37)) * 3
# (page content, objects, resources, zoom, transparent, forms): as render_torture_shading.pdf_bytes
# takes them. Objects: 1 a sampled function, 2 a PostScript one, 3 a stitching of exponentials.
_SHADING_OBJECTS = [
    b"<< /FunctionType 0 /Domain [0 1] /Range [0 1 0 1 0 1] /Size [7] /BitsPerSample 8 /Decode [0 1 1 0 0.2 0.9]"
    b" /Length %d >>\nstream\n" % len(_SAMPLES[:21]) + _SAMPLES[:21] + b"\nendstream",
    b"<< /FunctionType 4 /Domain [0 1] /Range [0 1 0 1 0 1] /Length %d >>\nstream\n" % len(_PS) + _PS + b"\nendstream",
    b"<< /FunctionType 3 /Domain [0 1] /Functions [<< /FunctionType 2 /Domain [0 1] /C0 [0 0.2 1 0] /C1 [1 0 0 0.3]"
    b" /N 1 >> << /FunctionType 2 /Domain [0 1] /C0 [0 1 0 0] /C1 [0 0 0 1] /N 2 >>] /Bounds [0.4] /Encode [0 1 1 0] >>",
    b"<< /FunctionType 4 /Domain [0 1 0 1] /Range [0 1 0 1 0 1] /Length 19 >>\nstream\n{ 2 copy add 2 div }\nendstream",
]


def _shading_resources(shadings: dict, patterns: dict | None = None) -> bytes:
    out = b" /Shading << " + b" ".join(b"/%s %s" % kv for kv in shadings.items()) + b" >>"
    if patterns:
        out += b" /Pattern << " + b" ".join(b"/%s %s" % kv for kv in patterns.items()) + b" >>"
    return out


SHADING_CASES = {
    # the radial Draw loop: a shrinking circle is `bDecreasing` only when the radius falls by more
    # than the *truncated* distance between the centres (static_cast<int>(hypotf(dx, dy))): here
    # 3.2 against 3.5, which is decreasing to PDFium and would not be with the distance kept whole
    "radial_decreasing_by_the_truncated_distance": (
        b"q 1 0 0 1 20 10 cm /S0 sh Q", _SHADING_OBJECTS, _shading_resources({
            b"S0": b"<< /ShadingType 3 /ColorSpace /DeviceCMYK /Coords [80 60 13.2 83.5 60 10] /Extend [true true]"
                   b" /Function 3 0 R >>"}), 3.1, False, ()),
    # a sampled function (Decode, 7 samples) and a PostScript one (sin, exp, roll, ifelse) through
    # a Separation and a DeviceN colour space, clipped, on a clear bitmap
    "sampled_and_postscript_functions": (
        b"q 20 20 160 110 re W n /S0 sh Q q 0.8 0 0.3 0.7 30 20 cm /S1 sh Q", _SHADING_OBJECTS, _shading_resources({
            b"S0": b"<< /ShadingType 2 /ColorSpace [/Separation /Ink /DeviceRGB 1 0 R] /Coords [10 10 190 140]"
                   b" /Function << /FunctionType 2 /Domain [0 1] /C0 [0] /C1 [1] /N 1 >> >>",
            b"S1": b"<< /ShadingType 3 /ColorSpace [/DeviceN [/A] /DeviceRGB 2 0 R] /Coords [60 60 0 90 70 50]"
                   b" /Domain [0.2 0.9] /Function %s /Extend [false true] >>" % _TINT}), 1.37, True, ()),
    # shading patterns: a fill with /Matrix and /Background and a /BBox, a translucent stroke, and
    # a pattern used inside a form, whose /Matrix is the pattern's parent matrix
    "patterns_filled_stroked_and_in_forms": (
        b"q /Pattern cs /P0 scn 10 10 180 130 re f Q q /A0 gs /Pattern CS /P1 SCN 9 w 1 J 20 120 m 180 30 l S Q"
        b" q /X0 Do Q", _SHADING_OBJECTS, _shading_resources({}, {
            b"P0": b"<< /PatternType 2 /Matrix [0.7 0.2 -0.3 0.9 20 5] /Shading << /ShadingType 2 /ColorSpace /DeviceGray"
                   b" /Coords [0 0 120 0] /Function << /FunctionType 2 /Domain [0 1] /C0 [0.1] /C1 [0.9] /N 1 >>"
                   b" /Background [0.4] /BBox [10 10 150 100] >> >>",
            b"P1": b"<< /PatternType 2 /Shading << /ShadingType 3 /ColorSpace /DeviceCMYK /Coords [100 75 5 100 75 90]"
                   b" /Function 3 0 R >> >>"}),
        2.0, False, [(b"/BBox [0 0 100 80] /Matrix [0.5 0.3 -0.2 1.1 40 20]",
                      b"/Pattern cs /P1 scn 0 0 100 80 re f")]),
}


@pytest.mark.parametrize("name", sorted(SHADING_CASES))
def test_the_pure_renderer_draws_pdfiums_shadings(name):
    from beamer2slides.devtools.render_torture_shading import compare
    content, objects, resources, zoom, transparent, forms = SHADING_CASES[name]
    n, a, _, _ = compare(content, objects, resources, zoom, transparent, forms)
    assert n == 0
    assert len({tuple(p) for p in a.reshape(-1, a.shape[2])}) > 10   # a gradient, not a blank page


def test_the_pure_renderer_survives_shading_torture_seeds():
    """A slice of the random shading pages the renderer was made exact on (12,000 seeds when it
    was written, 8% of them refused): any pixel apart fails."""
    from beamer2slides.devtools.render_torture_shading import run
    stats = run(0, 60, verbose=False)
    assert not stats["failed"], f"seeds apart (python tools/render_torture_shading.py SEED 1): {stats['failed']}"
    assert stats["drawn"] >= 50


@built
def test_the_pure_renderer_draws_the_test_decks_shadings():
    """Beamer's shadings (block title bars, balls, shadows: `sh` in forms under soft masks), with
    only paths, forms and shadings left on: every page that holds one, pixel for pixel (41 pages of
    14 decks when this was written; one deck here)."""
    import numpy as np

    from beamer2slides.pdf.api import OBJ_FORM, OBJ_PATH, OBJ_SHADING
    data = DECKS[0].read_bytes()
    ref, pure = pdf.resolve("pdfium").open(data), pdf.resolve("pure").open(data)
    seen = 0
    for i in range(len(ref)):
        objs = ref[i].objects()
        if not any(o.type == OBJ_SHADING for o in objs):
            continue
        off = [k for k, o in enumerate(objs) if o.type not in (OBJ_PATH, OBJ_FORM, OBJ_SHADING)]
        for page in (ref[i], pure[i]):
            page.set_active(off, False)
        a, b = ref[i].render(1.37), pure[i].render(1.37)
        assert np.array_equal(a, b), f"page {i}"
        seen += 1
    assert seen


_FAILS_VALIDATION = b"<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 200 0] /Function [3 0 R 3 0 R] >>"


@pytest.mark.parametrize("shading, reason", [
    # Validate fails (2 functions for 3 components): PDFium's Load returns false once and true on a
    # second call (shading_type_ is kept), so the second `sh` is drawn from a half-loaded pattern
    (_FAILS_VALIDATION, "fails validation"),
    (b"<< /ShadingType 2 /ColorSpace [/Lab << /WhitePoint [0.95 1 1.09] >>] /Coords [0 0 200 0] /Function 3 0 R >>",
     "Lab colour spaces"),
    (b"<< /ShadingType 1 /ColorSpace /DeviceRGB /Function 4 0 R >>", "function-based and mesh shadings"),
])
def test_the_pure_renderer_refuses_shadings_it_cannot_draw_exactly(shading, reason):
    from beamer2slides.devtools.render_torture_shading import pdf_bytes
    from beamer2slides.pdf.pure.backend import PureBackend
    data = pdf_bytes([b"/S0 sh /S0 sh"], _SHADING_OBJECTS, _shading_resources({b"S0": shading}))
    with pytest.raises(PdfError, match=reason):
        PureBackend().open(data)[0].render(1.0)


def test_a_shading_that_fails_validation_is_dropped_at_its_first_sh_only():
    """CPDF_ShadingPattern::Load sets the shading type before Validate, and the document keeps the
    pattern: the first `sh` of a shading that fails Validate makes no page object, the second does
    (found by the whole-file fuzz on a mutated beamer ball, seed 437)."""
    from beamer2slides.devtools.render_torture_shading import pdf_bytes
    from beamer2slides.pdf.api import OBJ_SHADING
    for content, count in ((b"/S0 sh", 0), (b"/S0 sh /S0 sh", 1), (b"/S0 sh /S0 sh /S0 sh", 2)):
        data = pdf_bytes([content], _SHADING_OBJECTS, _shading_resources({b"S0": _FAILS_VALIDATION}))
        for name in ("pdfium", "pure"):
            doc = pdf.resolve(name).open(data)
            objs = doc[0].objects()
            doc.close()
            assert sum(o.type == OBJ_SHADING for o in objs) == count, (name, content)


# ---------------------------------------------------------------------- text

def _text_font(stem, base):
    """A font the text torture harvests from the test decks (devtools/render_torture_text.py),
    found by its deck and base name (the subset tag changes whenever the deck does)."""
    from beamer2slides.devtools.render_torture_text import harvest
    for spec in harvest():
        if spec.name.startswith(stem + "/") and spec.name.endswith("+" + base):
            return spec
    pytest.skip(f"no {stem}/{base} font (build the test decks)")


# (page content, fonts as (deck, base name), zoom, transparent): pages found apart once, shrunk
TEXT_CASES = {
    # a TJ with no string still scales its kerning by Tz: GetHorizontalTextSize
    "tj_kerning_alone_under_tz": (b"BT /F0 20 Tf 50 Tz 10 60 Td [-800] TJ <2c2d> Tj [-1500] TJ <2e2f> Tj ET",
                                  [("08_serif", "CMR10")], 1.37, False),
    # stroked text under a skewed cm: the text object's position is ctm.Transform(tm.Transform(pos))
    # in float32, op by op (torture seed 8; a double-precision position moved 57 pixels by one level)
    "stroke_under_skewed_cm": (b"q\n0.6145 -1.2444 -0.7577 1.4974 1.410 53.443 cm\nBT\n/F0 13.891 Tf\n57.535 92.956 Td\n1 Tr\n"
                               b"<01eb028c021b023300e7015702af00a1> Tj [<02dd0271027c> 132 <021b027c> -354 "
                               b"<028c0195021b0195002c> -50] TJ\nET\nQ",
                               [("09_metropolis_fira", "FiraSans-Bold-Identity-H")], 3.1, True),
}


@pytest.mark.parametrize("name", TEXT_CASES)
def test_the_pure_renderer_draws_pdfiums_text(name):
    from beamer2slides.devtools.render_torture_text import compare
    content, fonts, zoom, transparent = TEXT_CASES[name]
    assert compare(content, [_text_font(*f) for f in fonts], zoom, transparent)[0] == 0


@pytest.mark.parametrize("simple", [1, 2], ids=["plain", "anything"])
def test_the_pure_renderer_survives_text_torture_seeds(simple):
    """A slice of the random text pages the renderer was made exact on (2,500 seeds when it was
    written: Tm, cm, clips, Tz/Tc/Tw/Ts, Tr 0..7, alpha, zooms). Char widths rounded once instead
    of after every float32 operation move a glyph by one ulp, which is enough to change coverage:
    seeds 1, 27 and 61 fail then."""
    from beamer2slides.devtools.render_torture_text import case, compare, harvest
    if not harvest():
        pytest.skip("no fonts to harvest (build the test decks)")
    apart, refused = {}, 0
    for seed in range(80):
        content, fonts, zoom, transparent = case(seed, "any", simple)
        try:
            n = compare(content, fonts, zoom, transparent)[0]
        except PdfError:
            refused += 1                     # Type 3 text: refused, never drawn wrong
            continue
        if n:
            apart[seed] = n
    assert not apart, f"seeds apart (python tools/render_torture_text.py SEED 1 --simple {simple}): {apart}"
    assert refused < 10


def test_the_pure_renderer_refuses_text_it_cannot_draw_exactly_yet():
    """Fonts not embedded (PDFium draws a system substitute) and Type 3 text are refused, not guessed."""
    from beamer2slides.devtools.render_torture_text import FontSpec, harvest, pdf_bytes
    from beamer2slides.pdf.pure.backend import PureBackend
    helvetica = FontSpec("standard", "type1", [b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"], [65])
    page = PureBackend().open(pdf_bytes(b"BT /F0 12 Tf 10 10 Td (A) Tj ET", [helvetica]))[0]
    with pytest.raises(PdfError, match="not embedded"):
        page.render(1.0)
    type3 = [s for s in harvest() if s.kind == "type3"]
    if type3:
        body = b"BT /F0 12 Tf 10 10 Td <%02x> Tj ET" % type3[0].codes[0]
        with pytest.raises(PdfError, match="Type 3"):
            PureBackend().open(pdf_bytes(body, type3[:1]))[0].render(1.0)


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


def test_a_damaged_flate_stream_keeps_what_decoded_before_the_damage():
    """FlateUncompress keeps inflate's output up to the byte it stops at (a flipped bit, a cut, junk
    inserted); keeping only whole 4 KB chunks lost up to 4 KB of content: 40 pages of 60 apart."""
    import random
    import zlib

    def one_page(stream):
        objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 150] /Contents 4 0 R >>",
                b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream"]
        out, offsets = bytearray(b"%PDF-1.7\n"), []
        for i, o in enumerate(objs):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % (i + 1) + o + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 5\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offsets)
        return bytes(out + b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref)

    r = random.Random(1)
    compressed = zlib.compress(b"\n".join(b"%d %d m %d %d l S" % (r.randrange(200), r.randrange(150), r.randrange(200),
                                                                   r.randrange(150)) for _ in range(3000)))
    for trial in range(12):
        damaged = bytearray(compressed)
        k = r.randrange(2, len(damaged))
        if trial % 3 == 0:
            damaged[k] ^= 1 << r.randrange(8)
        elif trial % 3 == 1:
            del damaged[k:]
        else:
            damaged[k:k] = b">>"
        data = one_page(bytes(damaged))
        ours, theirs = pdf.resolve("pure").open(data)[0], pdf.resolve("pdfium").open(data)[0]
        assert len(ours.objects()) == len(theirs.objects()), f"trial {trial}: damage at {k}"


def _objects_pdf(objs: dict, root: int = 1, trailer: bytes = b"") -> bytes:
    """A PDF of these object bodies with a correct cross-reference table (`trailer`: more entries)."""
    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for n, body in objs.items():
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref, size = len(out), max(objs) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for n in range(1, size):
        out += b"%010d 00000 n \n" % offsets[n] if n in offsets else b"0000000000 65535 f \n"
    return bytes(out + b"trailer\n<< /Size %d /Root %d 0 R %s>>\nstartxref\n%d\n%%%%EOF\n"
                 % (size, root, trailer, xref))


def _pages_said(backend, data, order):
    """(page count, [(index, width or None when the page does not load)]), or the refusal."""
    try:
        doc = pdf.resolve(backend).open(data)
    except PdfError:
        return "refused"
    said = []
    try:
        for i in order:
            if i < len(doc):
                try:
                    said.append((i, round(doc[i].width)))
                except PdfError:
                    said.append((i, None))
        return len(doc), said
    finally:
        doc.close()


_CAT = b"<< /Type /Catalog /Pages 2 0 R >>"
_LEAF = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d 100] >>"
PAGE_TREES = {
    # /Count is believed when 0 < Count < 0xFFFFF, else the kids are counted
    "count_more": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 3 >>", 3: _LEAF % 10},
    "count_less": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 1 >>", 3: _LEAF % 10, 4: _LEAF % 20},
    "count_zero": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 0 >>", 3: _LEAF % 10, 4: _LEAF % 20},
    "count_real": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 2.9 >>", 3: _LEAF % 10},
    "count_ref": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 4 0 R >>", 3: _LEAF % 10, 4: b"3"},
    "count_string": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count (x) >>", 3: _LEAF % 10, 4: _LEAF % 20},
    # a kid that is no dictionary uses up a page; a node that is its own kid is skipped
    "number_kid": {1: _CAT, 2: b"<< /Type /Pages /Kids [5 3 0 R] /Count 2 >>", 3: _LEAF % 10},
    "self_kid": {1: _CAT, 2: b"<< /Type /Pages /Kids [2 0 R 3 0 R] /Count 2 >>", 3: _LEAF % 10},
    "mixed": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R 7 4 0 R 5 0 R] /Count 4 >>", 3: _LEAF % 10,
              4: _LEAF % 20, 5: b"<< /Type /Pages /Kids [6 0 R] >>", 6: _LEAF % 30},
    "nested_wrong_count": {1: _CAT, 2: b"<< /Type /Pages /Kids [5 0 R 4 0 R] /Count 2 >>",
                           5: b"<< /Type /Pages /Kids [3 0 R] /Count 7 >>", 3: _LEAF % 10, 4: _LEAF % 20},
    "dup_kid": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R 3 0 R] >>", 3: _LEAF % 10},
    "cycle": {1: _CAT, 2: b"<< /Type /Pages /Kids [5 0 R 3 0 R] >>", 3: _LEAF % 10,
              5: b"<< /Type /Pages /Kids [2 0 R 3 0 R] >>"},
    # a node without /Kids is a page, whatever its /Count
    "no_kids": {1: _CAT, 2: b"<< /Type /Pages /MediaBox [0 0 30 30] /Count 4 >>"},
    "kids_not_array": {1: _CAT, 2: b"<< /Type /Pages /Kids 3 0 R /Count 1 >>", 3: _LEAF % 10},
    # a page loads when /Type is absent or resolves to /Page
    "no_type": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", 3: b"<< /Parent 2 0 R /MediaBox [0 0 10 100] >>"},
    "type_pages": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   3: b"<< /Type /Pages /Parent 2 0 R /MediaBox [0 0 10 100] >>"},
    "type_string": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                    3: b"<< /Type (Page) /Parent 2 0 R /MediaBox [0 0 10 100] >>"},
    "type_null": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                  3: b"<< /Type null /Parent 2 0 R /MediaBox [0 0 10 100] >>"},
    "type_ref": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                 3: b"<< /Type 4 0 R /Parent 2 0 R /MediaBox [0 0 10 100] >>", 4: b"/Page"},
    "inherited_box": {1: _CAT, 2: b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
                      3: b"<< /Type /Pages /MediaBox [0 0 77 77] >>", 4: b"<< /Type /Page /Parent 3 0 R >>"},
    # ... unless the kids were counted (no sane /Count): GetNodeType then writes /Type /Page into
    # every leaf, and a root without /Kids is a page unless it says /Pages
    "counted_foo": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R 4 0 R] >>",
                    3: b"<< /Type /Foo /Parent 2 0 R /MediaBox [0 0 10 100] >>", 4: _LEAF % 20},
    "counted_foo_believed": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
                             3: b"<< /Type /Foo /Parent 2 0 R /MediaBox [0 0 10 100] >>", 4: _LEAF % 20},
    "counted_page_with_kids": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R 4 0 R] >>",
                               3: b"<< /Type /Page /Kids [4 0 R] /MediaBox [0 0 10 100] >>", 4: _LEAF % 20},
    "root_no_kids_foo": {1: _CAT, 2: b"<< /Type /Foo /MediaBox [0 0 30 30] >>"},
    "root_no_kids_string": {1: _CAT, 2: b"<< /Type (x) /MediaBox [0 0 30 30] >>"},
    "stream_kid": {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 100] /Length 0 >>\nstream\n\nendstream"},
}


@pytest.mark.parametrize("name", PAGE_TREES)
def test_page_trees_are_walked_as_pdfium_walks_them(name):
    """CPDF_Document's page count and TraversePDFPages: which dictionary is page i depends on /Count,
    on kids that are no dictionaries, and on the order the pages were asked for (the traversal is
    stateful and caches what it passed)."""
    data = _objects_pdf(PAGE_TREES[name])
    for order in (range(8), range(7, -1, -1), [1, 0, 2, 1, 3, 0]):
        assert _pages_said("pure", data, order) == _pages_said("pdfium", data, order), list(order)


def _nav_pdf(catalog=b"", annots=b"", objs=None, trailer=b"", last=False) -> bytes:
    """Three pages; `annots` go on the first page (on the last one with `last`, so that a link's
    destination is looked up before the pages it names were loaded)."""
    extras = [b"", b"", b""]
    extras[2 if last else 0] = b"/Annots [" + annots + b"]" if annots else b""
    o = {1: b"<< /Type /Catalog /Pages 2 0 R " + catalog + b" >>",
         2: b"<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Count 3 >>"}
    for i, extra in enumerate(extras):
        o[3 + i] = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] " + extra + b" >>"
    o.update(objs or {})
    return _objects_pdf(o, trailer=trailer)


def _link(dest=b"", action=b"", rect=b"[10 20 30 40]", subtype=b"/Link") -> bytes:
    return (b"<< /Subtype " + subtype + b" /Rect " + rect + (b" /Dest " + dest if dest else b"")
            + (b" /A " + action if action else b"") + b" >>")


def _stream(data: bytes, extra: bytes = b"") -> bytes:
    return b"<< /Length %d %s >>\nstream\n%s\nendstream" % (len(data), extra, data)


def _uri(uri: bytes) -> bytes:
    return b"<< /S /URI /URI " + uri + b" >>"


def _names(tree: bytes) -> bytes:
    return b"/Names << /Dests " + tree + b" >>"


_TREE_OBJS = {30: b"[3 0 R /Fit]", 31: b"[4 0 R /Fit]", 32: b"[5 0 R /Fit]"}
NAVIGATION = {
    # FPDFLink_GetAnnotRect: GetRectFor needs four numbers and normalises nothing; the rect's items
    # are read raw (a reference counts 0), a boolean is 0, numbers are C floats
    "rect_reversed": {"annots": _link(b"[3 0 R]", rect=b"[30 40 10 20]")},
    "rect_three": {"annots": _link(b"[3 0 R]", rect=b"[1 2 3]")},
    "rect_ref_item": {"annots": _link(b"[3 0 R]", rect=b"[10 0 R 2 3 4]"), "objs": {10: b"7.5"}},
    "rect_ref": {"annots": _link(b"[3 0 R]", rect=b"10 0 R"), "objs": {10: b"[5 6 7 8]"}},
    "rect_bool": {"annots": _link(b"[3 0 R]", rect=b"[true 2 3 4]")},
    # FPDFLink_Enumerate: /Subtype read as a byte string (a string or a reference counts), a stream's
    # dictionary is an annotation too
    "subtype_string": {"annots": _link(b"[3 0 R]", subtype=b"(Link)")},
    "subtype_ref": {"annots": _link(b"[3 0 R]", subtype=b"10 0 R"), "objs": {10: b"/Link"}},
    "subtype_widget": {"annots": _link(b"[3 0 R]", subtype=b"/Widget")},
    "annot_stream": {"annots": b"10 0 R", "objs": {10: _stream(b"x", b"/Subtype /Link /Rect [1 2 3 4] /Dest [4 0 R]")}},
    # FPDFDest_GetDestPageIndex: a number is the index as is, a dictionary is found by object number
    # (a direct one has number 0: the first page not loaded yet), anything else is -1
    "dest_number": {"annots": _link(b"[5 /Fit]")},
    "dest_negative": {"annots": _link(b"[-7 /Fit]")},
    "dest_real": {"annots": _link(b"[1.7 /Fit]")},
    "dest_uint": {"annots": _link(b"[4294967295 /Fit]")},
    "dest_direct_page": {"annots": _link(b"[<< /Type /Page >> /Fit]")},
    "dest_direct_page_late": {"annots": _link(b"[<< /Type /Page >> /Fit]"), "last": True},
    "dest_untyped_page": {"annots": _link(b"[10 0 R]"), "objs": {10: b"<< /Parent 2 0 R >>"}},
    "dest_pages_node": {"annots": _link(b"[2 0 R /Fit]")},
    "dest_ref_to_ref": {"annots": _link(b"[10 0 R]"), "objs": {10: b"4 0 R"}},
    "dest_stream": {"annots": _link(b"[10 0 R]"), "objs": {10: _stream(b"x", b"/Type /Page")}},
    # an empty destination is still one: no URI fallback
    "dest_empty": {"annots": _link(b"[]", _uri(b"(http://x)"))},
    "dest_ref_array": {"annots": _link(b"10 0 R"), "objs": {10: b"[5 0 R /Fit]"}},
    "dest_dict": {"annots": _link(b"<< /D [4 0 R] >>")},
    # FPDFAction_GetType / GetDest: /Type, when there, must be /Action; only GoTo, GoToR and GoToE
    # carry a destination; /S read raw
    "action_goto": {"annots": _link(action=b"<< /S /GoTo /D [4 0 R] >>")},
    "action_gotor": {"annots": _link(action=b"<< /S /GoToR /D [4 0 R] /F (x.pdf) >>")},
    "action_launch": {"annots": _link(action=b"<< /S /Launch /D [4 0 R] >>")},
    "action_bad_type": {"annots": _link(action=b"<< /Type /Foo /S /GoTo /D [4 0 R] >>")},
    "action_string_s": {"annots": _link(action=b"<< /S (GoTo) /D [4 0 R] >>")},
    "action_ref": {"annots": _link(action=b"10 0 R"), "objs": {10: b"<< /S /GoTo /D [5 0 R] >>"}},
    "action_named": {"annots": _link(action=b"<< /S /GoTo /D (a) >>"), "catalog": _names(b"<< /Names [(a) 30 0 R] >>"),
                     "objs": _TREE_OBJS},
    # FPDFAction_GetURIPath: GetString of any object (numbers as FormatInteger / SkFloatToDecimal)
    "uri_real": {"annots": _link(action=_uri(b"0.000012345"))},
    "uri_uint": {"annots": _link(action=_uri(b"4294967295"))},
    "uri_bool": {"annots": _link(action=_uri(b"true"))},
    "uri_name": {"annots": _link(action=_uri(b"/abc"))},
    "uri_ref": {"annots": _link(action=_uri(b"10 0 R")), "objs": {10: b"(http://ref)"}},
    "uri_nul": {"annots": _link(action=_uri(b"(a\\000b)"))},
    # ... and the catalog's /URI /Base goes in front when the URI has no ':' past its first byte
    "uri_base": {"annots": _link(action=_uri(b"(a.html)")), "catalog": b"/URI << /Base (http://b/) >>"},
    "uri_base_colon0": {"annots": _link(action=_uri(b"(:x)")), "catalog": b"/URI << /Base (http://b/) >>"},
    "uri_base_scheme": {"annots": _link(action=_uri(b"(mailto:x)")), "catalog": b"/URI << /Base (http://b/) >>"},
    "uri_base_name": {"annots": _link(action=_uri(b"(a)")), "catalog": b"/URI << /Base /http >>"},
    "uri_base_stream": {"annots": _link(action=_uri(b"(a)")), "catalog": b"/URI << /Base 10 0 R >>",
                        "objs": {10: _stream(b"http://s/")}},
    "uri_base_ref": {"annots": _link(action=_uri(b"(a)")), "catalog": b"/URI << /Base 10 0 R >>",
                     "objs": {10: b"(http://r/)"}},
    # CPDF_NameTree: keys compared as UTF-16 units (Windows wchar_t), Limits padded and swapped in
    # place, a null value ends the search, cycles cut by object number
    "tree_unsorted": {"catalog": _names(b"<< /Names [(c) 32 0 R (a) 30 0 R (b) 31 0 R] >>"), "objs": _TREE_OBJS,
                      "annots": _link(b"(b)") + _link(b"/c")},
    "tree_limits_reversed": {"catalog": _names(b"<< /Kids [<< /Limits [(b) (a)] /Names [(a) 30 0 R (b) 31 0 R] >>] >>"),
                             "objs": _TREE_OBJS, "annots": _link(b"(a)") + _link(b"(b)")},
    "tree_limits_short": {"catalog": _names(b"<< /Kids [<< /Limits [(b)] /Names [(a) 30 0 R (b) 31 0 R] >>] >>"),
                          "objs": _TREE_OBJS, "annots": _link(b"(a)") + _link(b"(b)")},
    "tree_limits_wrong": {"catalog": _names(b"<< /Kids [<< /Limits [(x) (z)] /Names [(a) 30 0 R] >> "
                                            b"<< /Names [(a) 32 0 R] >>] >>"), "objs": _TREE_OBJS,
                          "annots": _link(b"(a)")},
    "tree_null_first": {"catalog": _names(b"<< /Kids [<< /Names [(a) null] >> << /Names [(a) 30 0 R] >>] >>"),
                        "objs": _TREE_OBJS, "annots": _link(b"(a)")},
    "tree_cycle": {"catalog": _names(b"<< /Kids [10 0 R] >>"), "objs": {**_TREE_OBJS, 10: b"<< /Kids [10 0 R 11 0 R] >>",
                   11: b"<< /Names [(c) 32 0 R] >>"}, "annots": _link(b"(c)")},
    # names compare as UTF-16 code units (wchar_t on Windows): an astral name sorts below U+FF01
    "tree_utf16_order": {"catalog": _names(b"<< /Kids [<< /Limits [<FEFFD83DDE00> <FEFFFF01>] "
                                           b"/Names [<FEFFD83DDE00> 30 0 R <FEFFFF01> 31 0 R] >>] >>"),
                         "objs": _TREE_OBJS, "annots": _link(b"<FEFFFF01>") + _link(b"<FEFFD83DDE00>")},
    "tree_shared_kid": {"catalog": _names(b"<< /Kids [10 0 R 10 0 R] >>"),
                        "objs": {**_TREE_OBJS, 10: b"<< /Names [(a) 30 0 R] >>"}},
    "tree_names_and_kids": {"catalog": _names(b"<< /Names [(a) 30 0 R] /Kids [<< /Names [(c) 32 0 R] >>] >>"),
                            "objs": _TREE_OBJS, "annots": _link(b"(c)")},
    "tree_values": {"catalog": _names(b"<< /Names [(a) null (b) 10 0 R (c) << /D 31 0 R >> (d) /x (e) 11 0 R "
                                      b"(f) 12 0 R] >>"),
                    "objs": {**_TREE_OBJS, 10: b"[4 0 R]", 11: b"null", 12: b"10 0 R"}},
    "tree_keys": {"catalog": _names(b"<< /Names [<FEFF00E9> 30 0 R /nm 31 0 R 5 32 0 R <EFBBBF41C3A9> 31 0 R "
                                    b"<FEFF001B656E001B0041> 32 0 R (\\200\\237\\255) 30 0 R <FEFFD800> 31 0 R "
                                    b"(a\\000) 30 0 R <FEFF00> 32 0 R] >>"), "objs": _TREE_OBJS},
    # the old /Dests dictionary: entries in key order, a reference value skipped by FPDF_GetNamedDest
    "old_dests": {"catalog": b"/Dests << /zeta [3 0 R] /Alpha [4 0 R] /ref 10 0 R /dict << /D [5 0 R] >> /num 5 >>",
                  "objs": {10: b"[3 0 R]"}, "annots": _link(b"/ref") + _link(b"(zeta)")},
    # CPDF_PageLabel: the lower bound in the number tree, St wrapping to int32, the styles' own limits
    "labels_styles": {"catalog": b"/PageLabels << /Nums [0 << /S /r >> 1 << /S /D /St 5 >> 2 << /P (x-) /S /A >>] >>"},
    "labels_roman_big": {"catalog": b"/PageLabels << /Nums [0 << /S /R /St 1003999 >>] >>"},
    "labels_roman_negative": {"catalog": b"/PageLabels << /Nums [0 << /S /r /St -4 >>] >>"},
    "labels_letters": {"catalog": b"/PageLabels << /Nums [0 << /S /a /St 26 >> 1 << /S /A /St 27 >> "
                                  b"2 << /S /a /St 25974 >>] >>"},
    "labels_letters_negative": {"catalog": b"/PageLabels << /Nums [0 << /S /a /St -5 >> 1 << /S /A /St -30 >>] >>"},
    "labels_start_forms": {"catalog": b"/PageLabels << /Nums [0 << /S /D /St 2.9 >> 1 << /S /D /St 4294967295 >> "
                                      b"2 << /S /D /St null >>] >>"},
    "labels_style_forms": {"catalog": b"/PageLabels << /Nums [0 << /S (D) >> 1 << /S /Q /P (only) >> "
                                      b"2 << /S null /P (p) >>] >>"},
    "labels_prefix_forms": {"catalog": b"/PageLabels << /Nums [0 << /P /name >> 1 << /P <FEFF0041> /S /D >> "
                                       b"2 << /P 10 0 R /S /r >>] >>", "objs": {10: b"11 0 R", 11: b"(ref)"}},
    "labels_unsorted": {"catalog": b"/PageLabels << /Nums [2 << /S /r >> 0 << /S /D >>] >>"},
    "labels_late_start": {"catalog": b"/PageLabels << /Nums [1 << /S /r >>] >>"},
    "labels_values": {"catalog": b"/PageLabels << /Nums [0 null 1 10 0 R 2 11 0 R] >>",
                      "objs": {10: b"<< /S /D /St 9 >>", 11: b"10 0 R"}},
    "labels_kids": {"catalog": b"/PageLabels << /Kids [<< /Limits [0 0] /Nums [0 << /S /r >>] >> "
                               b"<< /Limits [1 2] /Nums [1 << /S /D >> 2 << /S /a >>] >>] >>"},
    "labels_stream": {"catalog": b"/PageLabels 10 0 R", "objs": {10: _stream(b"", b"/Nums [0 << /S /A >>]")}},
    # FPDF_GetMetaText: /Info must be a reference to a dictionary; values decoded like PDF text
    "info": {"trailer": b"/Info 10 0 R ", "objs": {10: b"<< /Title (Hello) /Producer <FEFF00480069D83DDE00> >>"}},
    "info_pdfdoc": {"trailer": b"/Info 10 0 R ", "objs": {10: b"<< /Title (\\177\\237\\255\\200\\030\\240) >>"}},
    "info_utf8": {"trailer": b"/Info 10 0 R ", "objs": {10: b"<< /Title <EFBBBF41C3A9F09F9880> >>"}},
    "info_lone_surrogate": {"trailer": b"/Info 10 0 R ", "objs": {10: b"<< /Title <FEFFD800> >>"}},
    "info_language": {"trailer": b"/Info 10 0 R ", "objs": {10: b"<< /Title <FEFF001B656E001B0041> >>"}},
    "info_forms": {"trailer": b"/Info 10 0 R ", "objs": {10: b"<< /Title /Nm /Producer 11 0 R >>", 11: b"(ref)"}},
    "info_direct": {"trailer": b"/Info << /Title (direct) >> "},
    "info_ref_to_ref": {"trailer": b"/Info 11 0 R ", "objs": {10: b"<< /Title (x) >>", 11: b"10 0 R"}},
}


def _navigation_said(backend, data):
    doc = pdf.resolve(backend).open(data)
    try:
        return {"links": [page.links() for page in doc], "named_dests": doc.named_dests(),
                "labels": [doc.label(i) for i in range(len(doc))], "metadata": doc.metadata}
    finally:
        doc.close()


@pytest.mark.parametrize("name", NAVIGATION)
def test_links_destinations_labels_and_metadata_are_read_as_pdfium_reads_them(name):
    """The document-level calls (pure/navigation.py): FPDFLink_*, FPDFAction_*, FPDFDest_GetDestPageIndex,
    CPDF_NameTree, FPDF_GetNamedDest, CPDF_PageLabel and FPDF_GetMetaText, quirks included."""
    data = _nav_pdf(**NAVIGATION[name])
    close(_navigation_said("pure", data), _navigation_said("pdfium", data), name)


def test_a_broken_file_is_rebuilt_as_pdfium_rebuilds_it():
    """RebuildCrossRef: a table whose first object is not where it says is not believed, the file is
    scanned word by word (strings skipped, so an `obj` in a string is none), damaged objects end
    where CPDF_SyntaxParser ends them, and a catalog is all a rebuilt file needs."""
    good = _objects_pdf({1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", 3: _LEAF % 10,
                         4: b"<< /S (9 0 obj << /Type /Catalog >> endobj) >>"})
    cases = {
        "first_offset_wrong": good.replace(b"0000000009 00000 n", b"0000000019 00000 n"),
        "no_xref": good[:good.index(b"xref")] + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n",
        "damaged_dict": good.replace(b"/Count 1 >>", b"/Count 1 /Rect [1 0.9 360.ype/Link >>"),
        "name_then_comment": good.replace(b"/Type /Catalog /Pages", b"/Type /% x\n/Catalog /Pages"),
        "no_pages": good.replace(b"/Pages 2 0 R", b"/Pages 9 0 R").replace(b"0000000009 00000 n", b"0000000000 00000 n"),
        "no_trailer_root": good.replace(b"/Root 1 0 R", b"/Root 1"),
    }
    for name, data in cases.items():
        order = range(3)
        assert _pages_said("pure", data, order) == _pages_said("pdfium", data, order), name


def _xref_stream_file(objs: dict, packed: dict, extra: dict | None = None, size: int | None = None) -> bytes:
    """A PDF 1.5 file: `objs` as plain objects, `packed` (number -> body) in one object stream
    (number 20), found through a cross-reference stream (number 21, W [1 4 2], uncompressed)."""
    out, rows = bytearray(b"%PDF-1.5\n"), {}
    for n, body in objs.items():
        rows[n] = (1, len(out), 0)
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    parts, offs = [], []
    for n, body in packed.items():
        offs.append(sum(len(p) + 1 for p in parts))
        parts.append(body)
    head = b" ".join(b"%d %d" % (n, o) for n, o in zip(packed, offs)) + b" "
    data = head + b" ".join(parts)
    rows[20] = (1, len(out), 0)
    out += (b"20 0 obj\n<< /Type /ObjStm /N %d /First %d /Length %d >>\nstream\n" % (len(packed), len(head), len(data))
            + data + b"\nendstream\nendobj\n")
    for i, n in enumerate(packed):
        rows[n] = (2, 20, i)
    for n, row in (extra or {}).items():
        rows[n] = row
    rows[21] = (1, len(out), 0)
    size = size if size is not None else max(rows) + 1
    table = b"".join(bytes([t]) + a.to_bytes(4, "big") + b.to_bytes(2, "big")
                     for t, a, b in (rows.get(n, (0, 0, 0)) for n in range(size)))
    xref = len(out)
    out += (b"21 0 obj\n<< /Type /XRef /Size %d /W [1 4 2] /Root 1 0 R /Length %d >>\nstream\n" % (size, len(table))
            + table + b"\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n" % xref)
    return bytes(out)


def test_cross_references_are_read_as_pdfium_reads_them():
    """CPDF_Parser's cross-reference loading, ported rather than approximated: 20-byte table rows
    (the generation read by StringToInt, `f` at byte 17 is all a free row needs), positions counted
    from the `%PDF` header, `startxref` found as a whole word from the end, /Prev chains where the
    newest entry wins, object streams (object number 0 in one is no object) and a rebuild when any
    of it fails. The whole-file fuzz found the old reader refusing files PDFium opens (seed 680: one
    damaged row made the table unbelieved, and the rebuild then lost the trailer)."""
    tree = {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>", 3: _LEAF % 10, 4: _LEAF % 20}
    good = _objects_pdf(tree)
    row3 = b"%010d 00000 n" % good.index(b"3 0 obj")

    def update(base: bytes, body: bytes, prev: int | None = None) -> bytes:
        prev = base.rindex(b"xref\n") if prev is None else prev
        at = len(base)
        piece = base + b"3 0 obj\n" + body + b"\nendobj\n"
        x = len(piece)
        return piece + (b"xref\n3 1\n%010d 00000 n \ntrailer\n<< /Size 5 /Root 1 0 R /Prev %d >>\nstartxref\n%d\n%%%%EOF\n"
                        % (at, prev, x))

    cases = {
        "header_offset": b"garbage before the header\n" + good,
        "header_far": b"x" * 1100 + good,
        "row_generation_junk": good.replace(row3, row3[:11] + b"0a0z0 n"),
        "row_offset_junk": good.replace(row3, row3[:4] + b"z" + row3[5:]),
        "row_free_by_f": good.replace(row3 + b" ", row3[:17] + b"f "),
        "row_short_zero": good.replace(row3, b"0 " + b"0" * 9 + b" n"),
        "startxref_not_a_word": good.replace(b"startxref", b"xstartxref"),
        "startxref_small": good[:good.rindex(b"startxref")] + b"startxref\n5\n%%EOF\n",
        "startxref_far": good + b" " * 5000,
        "prev_chain": update(good, _LEAF % 30),
        "prev_chain_twice": update(update(good, _LEAF % 30), _LEAF % 40),
        "prev_wrong": update(good, _LEAF % 30, prev=7),
        "prev_negative": update(good, _LEAF % 30, prev=-4),
        "objstm": _xref_stream_file({1: _CAT, 2: tree[2]}, {3: tree[3], 4: tree[4]}),
        "objstm_zero": _xref_stream_file({1: _CAT, 2: tree[2]}, {0: _LEAF % 50, 3: tree[3], 4: tree[4]}),
        "objstm_newer_plain": _xref_stream_file({1: _CAT, 2: tree[2], 3: _LEAF % 70}, {3: tree[3], 4: tree[4]}),
        "objstm_bad_archive": _xref_stream_file({1: _CAT, 2: tree[2]}, {3: tree[3], 4: tree[4]}, {4: (2, 99, 0)}),
        "xref_size_small": _xref_stream_file({1: _CAT, 2: tree[2]}, {3: tree[3], 4: tree[4]}, size=3),
    }
    for name, data in cases.items():
        order = range(3)
        assert _pages_said("pure", data, order) == _pages_said("pdfium", data, order), name


def _text_page(content: bytes, resources: bytes, extra: dict | None = None, stream_dict: bytes = b"") -> bytes:
    body = b"<< /Length %d %s >>\nstream\n" % (len(content), stream_dict) + content + b"\nendstream"
    objs = {1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources %s /Contents 4 0 R >>" % resources,
            4: body, **(extra or {})}
    return _objects_pdf(objs)


_HELV = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
_BT = b"BT /F1 12 Tf 20 100 Td (Hello, World) Tj ET"
FONT_AND_FILTER_CASES = {
    # FindFont: a missing (or non-dictionary) font is the stock Helvetica, the size is set anyway
    "missing_font": _text_page(b"BT /F9 12 Tf 20 100 Td (Hello) Tj ET", b"<< /Font << /F1 5 0 R >> >>", {5: _HELV}),
    "stream_font": _text_page(_BT, b"<< /Font << /F1 5 0 R >> >>", {5: b"<< /Length 0 >>\nstream\n\nendstream"}),
    # a form with a Font dictionary of its own does not look in the page's
    "form_font_not_inherited": _text_page(
        b"/X1 Do", b"<< /Font << /F1 5 0 R >> /XObject << /X1 6 0 R >> >>",
        {5: _HELV, 6: b"<< /Subtype /Form /BBox [0 0 300 200] /Resources << /Font << /F2 5 0 R >> >> /Length %d >>\n"
                      b"stream\n%s\nendstream" % (len(_BT), _BT)}),
    # /Widths are uint16: -1502 is 64034
    "negative_width": _text_page(_BT, b"<< /Font << /F1 5 0 R >> >>", {
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /FirstChar 72 /LastChar 72 /Widths [-1502] >>"}),
    # a filter PDFium does not decode is an image codec: the stored bytes; so is a /Filter that is no name
    "unknown_filter": _text_page(_BT, b"<< /Font << /F1 5 0 R >> >>", {5: _HELV}, b"/Filter /Foo"),
    "filter_not_a_name": _text_page(_BT, b"<< /Font << /F1 5 0 R >> >>", {5: _HELV}, b"/Filter 7"),
    "failing_then_codec": _text_page(_BT, b"<< /Font << /F1 5 0 R >> >>", {5: _HELV}, b"/Filter [/DCTDecode /FlateDecode]"),
    # a zero length is checked like any other: no endstream after it, the end is searched for
    "zero_length_stray_word": _text_page(_BT, b"<< /Font << /F1 5 0 R >> >>", {5: _HELV}).replace(
        b"/Length %d  >>" % len(_BT), b"/Length 0 R1 >>"),
}


@pytest.mark.parametrize("name", FONT_AND_FILTER_CASES)
def test_fonts_and_filters_resolve_as_pdfium_resolves_them(name):
    """Fonts that are missing, not inherited or carry impossible widths, and streams whose filters
    PDFium won't decode. A non-embedded base-14 font is drawn with the system's Arial / Times New
    Roman / Courier New (CFX_Win32FontInfo), so outside Windows only the object list is compared."""
    import sys
    data = FONT_AND_FILTER_CASES[name]
    a, b = pdf.resolve("pure").open(data)[0], pdf.resolve("pdfium").open(data)[0]
    close([dataclasses.astuple(o) for o in a.objects()], [dataclasses.astuple(o) for o in b.objects()], name)
    if sys.platform == "win32":
        for call in ("object_bounds", "chars"):
            close(getattr(a, call)(), getattr(b, call)(), f"{name} {call}")


def _chars_and_bounds(page):
    chars = [{k: v for k, v in dataclasses.asdict(c).items() if k != "font_id"} for c in page.chars()]
    return chars, page.object_bounds()


@built
def test_chars_and_object_boxes_are_pdfiums_to_the_last_bit():
    """Not float32 noise: the same bits. PDFium computes text positions, char boxes, loose boxes,
    glyph widths and matrix products in C floats, rounding after every product and every sum
    (`w * size / 1000` twice, a CFX_Matrix product once per term), so the port does too; one rounding
    in double was one ulp apart on a rotated axis label and after a TJ kern. With a font descriptor
    missing its /FontBBox, CheckFontMetrics takes the program's box and, for Type 1 and CFF, FreeType's
    ascender and descender (the box's yMax and yMin) - bfuzz seed 452, 66 pt apart before."""
    for path in DECKS[:3] + [OUT / "03_figures.pdf"]:
        data = path.read_bytes()
        variants = {path.name: data, path.name + " without FontBBox": data.replace(b"/FontBBox", b"/FontBBoX")}
        for name, variant in variants.items():
            ref, pure = pdf.resolve("pdfium").open(variant), pdf.resolve("pure").open(variant)
            try:
                for i in range(len(ref)):
                    assert _chars_and_bounds(pure[i]) == _chars_and_bounds(ref[i]), f"{name} page {i}"
            finally:
                ref.close()
                pure.close()


# Whole decks with every font dictionary edited the same way: each one reached a rule the port had
# approximated. MSAM10 read as non-symbolic finds no glyph by name, and FreeType made it no Unicode
# charmap (no glyph name maps to Unicode), so the code is looked up in its builtin encoding
# (bfuzz seed 246); a CID font with no /ToUnicode answers FPDFFont_GetGlyphWidth with code 0
# (Identity is kCID with no CID-to-Unicode map); a Type0 font without /Encoding fails to load, so
# its text is stock Helvetica; a Type 3 font without /FontBBox takes the union of its char boxes.
FONT_VARIANTS = {
    "nonsymbolic": ("01_basic", lambda d: re.sub(rb"/Flags \d+", b"/Flags 32", d)),
    "no_flags": ("01_basic", lambda d: d.replace(b"/Flags", b"/FlagX")),
    "cid_no_tounicode": ("06_wide_lualatex", lambda d: d.replace(b"/ToUnicode", b"/ToUnicodX")),
    "cid_no_encoding": ("06_wide_lualatex", lambda d: d.replace(b"/Encoding", b"/EncodinX")),
    "cid_no_widths": ("06_wide_lualatex", lambda d: d.replace(b"/W [", b"/X [").replace(b"/DW", b"/DX")),
    "type3_no_fontbbox": ("19_labels_on_graphics", lambda d: d.replace(b"/FontBBox", b"/FontBBoX")),
}


@pytest.mark.parametrize("name", FONT_VARIANTS)
def test_font_dictionaries_edited_deck_wide_read_as_pdfium_reads_them(name):
    deck, edit = FONT_VARIANTS[name]
    path = OUT / f"{deck}.pdf"
    if not path.exists():
        pytest.skip("no test PDFs built")
    data = edit(path.read_bytes())
    ref, pure = pdf.resolve("pdfium").open(data), pdf.resolve("pure").open(data)
    try:
        for i in range(len(ref)):
            assert _chars_and_bounds(pure[i]) == _chars_and_bounds(ref[i]), f"{name} page {i}"
    finally:
        ref.close()
        pure.close()


def test_text_torture_pages_extract_as_pdfium_to_the_last_bit():
    """The render torture's random text pages, read rather than drawn (objects, chars, boxes). Seeds
    0, 7, 20, 21, 28, 45 and 51 were apart: a stroked text object's box is inflated by half the line
    width in floats (CFX_FloatRect::Inflate), Type 3 text included - the object keeps the real Tr,
    only its glyph drawing is forced to fill - and a Tr outside 0..7 leaves the mode as it was."""
    from beamer2slides.devtools.render_torture_text import case, harvest, pdf_bytes
    if not harvest():
        pytest.skip("no fonts to harvest (build the test decks)")
    apart = []
    for seed in [0, 7, 20, 21, 28, 45, 51, *range(100, 130)]:
        content, fonts, _, _ = case(seed, "any")
        data = pdf_bytes(content, fonts)
        ref, pure = pdf.resolve("pdfium").open(data), pdf.resolve("pure").open(data)
        try:
            if _chars_and_bounds(pure[0]) != _chars_and_bounds(ref[0]):
                apart.append(seed)
        finally:
            ref.close()
            pure.close()
    assert not apart, f"seeds apart (scratch: xtext.py SEED 1): {apart}"
    for kind in ["type3"]:
        for seed in range(20):
            content, fonts, _, _ = case(seed, kind)
            data = pdf_bytes(content, fonts)
            ref, pure = pdf.resolve("pdfium").open(data), pdf.resolve("pure").open(data)
            try:
                assert _chars_and_bounds(pure[0]) == _chars_and_bounds(ref[0]), f"{kind} seed {seed}"
            finally:
                ref.close()
                pure.close()


_TWO_PAGES ={1: _CAT, 2: b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>", 3: _LEAF % 10, 4: _LEAF % 20}


def _entry(data: bytes, num: int, new: bytes) -> bytes:
    """`data` with object num's 20-byte cross-reference entry replaced by `new` (as long)."""
    table = data.rindex(b"\nxref\n")
    at = data.index(b" f \n", table) + 4 + (num - 1) * 20
    assert len(new) == 20
    return data[:at] + new + data[at + 20:]


def _obj(num: int, body: bytes, gen: int = 0) -> bytes:
    return b"%d %d obj\n%s\nendobj\n" % (num, gen, body)


def _stream(d: bytes, data: bytes) -> bytes:
    return b"<< %s /Length %d >>\nstream\n%s\nendstream" % (d, len(data), data)


def _objstm(members: dict, d: bytes = b"/Type /ObjStm /N %(n)d /First %(first)d", lead: bytes = b"") -> bytes:
    """An object stream holding `members` (number -> body), uncompressed."""
    bodies, header = b"", lead
    for num, body in members.items():
        header += b"%d %d " % (num, len(bodies))
        bodies += body + b" "
    return _stream(d % {b"n": len(members), b"first": len(header)}, header + bodies)


def _xref_stream_pdf(objs: dict, packed: dict, w=(1, 2, 1), override=None, index=None,
                     objstm: dict | None = None, size: int | None = None) -> bytes:
    """A PDF whose cross-reference is a stream: `objs` as plain objects, `packed` in an object
    stream (numbered after them, the stream last); `override` replaces entries (number -> (type,
    field 2, field 3)), `index` is the /Index subsections (else one from 0)."""
    out, entries = bytearray(b"%PDF-1.5\n"), {}
    for num, body in objs.items():
        entries[num] = (1, len(out), 0)
        out += _obj(num, body)
    archive = max(list(objs) + list(packed)) + 1
    entries[archive] = (1, len(out), 0)
    out += _obj(archive, _objstm(packed, **(objstm or {})))
    for i, num in enumerate(packed):
        entries[num] = (2, archive, i)
    xref = archive + 1
    entries[xref] = (1, len(out), 0)
    entries.update(override or {})
    index = index or [0, xref + 1]
    numbers = [n for start, count in zip(index[::2], index[1::2]) for n in range(start, start + count)]
    rows = b"".join(b"".join(v.to_bytes(width, "big") for v, width in zip(entries.get(n, (0, 0, 0)), w) if width)
                    for n in numbers)
    words = b"/W [%s] /Index [%s]" % (b" ".join(b"%d" % x for x in w), b" ".join(b"%d" % x for x in index))
    out += _obj(xref, _stream(b"/Type /XRef /Size %d %s /Root 1 0 R" % (size or xref + 1, words), rows))
    return bytes(out + b"startxref\n%d\n%%%%EOF\n" % entries[xref][1])


def _updated(first: bytes, objs: dict, trailer: bytes = b"") -> bytes:
    """An incremental update of `first` rewriting `objs` (number -> (generation, body)), its
    table in one section per object and its trailer pointing back with /Prev."""
    prev = int(first[first.rindex(b"startxref") + 10:].split()[0])
    out, table = bytearray(first), b""
    for num, (gen, body) in objs.items():
        table += b"%d 1\n%010d %05d n \n" % (num, len(out), gen)
        out += _obj(num, body, gen)
    xref = len(out)
    size = max(max(objs) + 1, 5)
    return bytes(out + b"xref\n" + table + b"trailer\n<< /Size %d /Root 1 0 R /Prev %d %s>>\nstartxref\n%d\n%%%%EOF\n"
                 % (size, prev, trailer, xref))


def _rebuilt_pdf(*parts: bytes) -> bytes:
    """Objects in file order and a trailer, with no table: only a rebuild reads it."""
    return b"%PDF-1.4\n" + b"".join(parts) + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"


_HIDDEN = _stream(b"", _obj(4, _LEAF % 20))   # object 4 written inside a stream's data


def _xref_cases() -> dict:
    good = _objects_pdf(_TWO_PAGES)
    older = _objects_pdf({**_TWO_PAGES, 5: b"<< /Title (older) >>"}, trailer=b"/Info 5 0 R")
    at = good.index(b"xref")   # the first table
    updated = _updated(good, {4: (0, _LEAF % 30)})
    hidden = _objects_pdf({1: _CAT, 2: _TWO_PAGES[2], 3: _LEAF % 10, 5: _HIDDEN})
    into_stream = _entry(hidden, 4, b"%010d 00000 n \n" % hidden.index(b"4 0 obj"))
    rows = good[good.index(b"xref"):good.index(b"trailer")]
    packed = {3: _LEAF % 10, 4: _LEAF % 20}
    tree = {1: _CAT, 2: _TWO_PAGES[2]}
    plain = _xref_stream_pdf(_TWO_PAGES, {})
    newer = _updated(older, {6: (0, b"<< /Title (newer) >>")}, trailer=b"/Info 6 0 R")
    return {
        # ParseAndAppendCrossRefSubsectionData: 20 bytes an entry, read blind. The offset is
        # FXSYS_atoi64 of what the entry starts with (a letter ends it); only the first object
        # placed is checked; an offset of 0 needs ten digits; byte 17 'f' frees the entry
        "entry_offset_letters": _entry(good, 4, b"00000002f337 00000 n"[:18] + b" \n"),
        "entry_offset_zero_letters": _entry(good, 4, b"0000000x00 00000 n \n"),
        "entry_free_byte_17": _entry(good, 4, b"%010d 00000 f \n" % good.index(b"4 0 obj")),
        "entry_lines_19_bytes": good.replace(rows, rows.replace(b" \n", b"\n")),
        "entry_first_wrong": _entry(good, 1, b"%010d 00000 n \n" % good.index(b"2 0 obj")),
        # the /Prev chain: the oldest table first, an entry of a lower generation than one known
        # ignored; the newer trailer's keys over the older one's, which keeps what it alone has
        "prev_lower_generation_ignored": _updated(_updated(good, {3: (5, _LEAF % 30)}), {3: (0, _LEAF % 40)}),
        "prev_higher_generation_wins": _updated(_updated(good, {3: (0, _LEAF % 30)}), {3: (2, _LEAF % 40)}),
        "prev_older_trailer_info": _updated(older, {4: (0, _LEAF % 30)}),
        "prev_newer_trailer_info": newer,
        "prev_loop": updated.replace(b"/Prev %d" % at, b"/Prev %d" % (updated.rindex(b"\nxref\n") + 1)),
        "prev_to_nothing": updated.replace(b"/Prev %d" % at, b"/Prev 3"),
        # cross-reference streams and object streams (CPDF_ObjectStream::Create and Init)
        "stream_packed": _xref_stream_pdf(tree, packed),
        "stream_type_3_ignored": _xref_stream_pdf(_TWO_PAGES, {}, override={4: (3, plain.index(b"4 0 obj"), 0)}),
        "stream_archive_past_last": _xref_stream_pdf(tree, packed, override={4: (2, 99, 1)}),
        # an archive is checked against the numbers known so far (/Size, then each subsection):
        # here it comes in a later subsection
        "stream_archive_later": _xref_stream_pdf({**tree, 3: _LEAF % 10}, {4: _LEAF % 20}, index=[0, 5, 5, 2],
                                                 size=5),
        "stream_archive_earlier": _xref_stream_pdf({**tree, 3: _LEAF % 10}, {4: _LEAF % 20}, index=[5, 2, 0, 5],
                                                   size=5),
        "stream_generation_past_16_bits": _xref_stream_pdf({**tree, 3: _LEAF % 10}, {4: _LEAF % 20}, w=(1, 2, 3),
                                                           override={3: (1, 0, 0x10000)}),
        "stream_two_widths": _xref_stream_pdf(tree, packed, w=(1, 2)),
        "stream_no_type_field": _xref_stream_pdf({**tree, **packed}, {}, w=(0, 2, 1)),   # all type 1
        "objstm_n_real": _xref_stream_pdf(tree, packed, objstm={"d": b"/Type /ObjStm /N %(n)d.0 /First %(first)d"}),
        "objstm_no_type": _xref_stream_pdf(tree, packed, objstm={"d": b"/N %(n)d /First %(first)d"}),
        "objstm_member_zero": _xref_stream_pdf(tree, packed, objstm={"lead": b"0 0 "}),
        "objstm_n_short": _xref_stream_pdf(tree, packed, objstm={"d": b"/Type /ObjStm /N 1 /First %(first)d"}),
        # RebuildCrossRef: each object added as a table adds it, the object stream's members
        # after it; the table rebuilt goes over the one read, which keeps what the scan missed
        "table_into_stream": into_stream,
        "rebuild_over_table": _entry(into_stream, 2, b"%010d 00000 n \n" % hidden.index(b"3 0 obj")),
        "rebuild_packed_then_plain": _rebuilt_pdf(_obj(1, _CAT), _obj(2, _TWO_PAGES[2]), _obj(5, _objstm(packed)),
                                                  _obj(3, _LEAF % 30)),
        "rebuild_plain_then_packed": _rebuilt_pdf(_obj(1, _CAT), _obj(2, _TWO_PAGES[2]), _obj(3, _LEAF % 30),
                                                  _obj(5, _objstm(packed))),
        # an object stream is never made a member of another one
        "rebuild_objstm_in_objstm": _rebuilt_pdf(_obj(1, _CAT), _obj(2, _TWO_PAGES[2]), _obj(5, _objstm(packed)),
                                                 _obj(6, _objstm({5: b"<< >>"}))),
        "rebuild_higher_generation_first": _rebuilt_pdf(_obj(1, _CAT), _obj(2, _TWO_PAGES[2]), _obj(3, _LEAF % 10, 5),
                                                        _obj(3, _LEAF % 30), _obj(4, _LEAF % 20)),
    }


def _xref_said(backend, data):
    said = _pages_said(backend, data, range(3))
    if said == "refused":
        return said
    doc = pdf.resolve(backend).open(data)
    try:
        return said, doc.metadata.get("title")
    finally:
        doc.close()


@pytest.mark.parametrize("name", list(_xref_cases()))
def test_cross_references_are_loaded_as_pdfium_loads_them(name):
    """CPDF_Parser::LoadAllCrossRefTablesAndStreams, CPDF_CrossRefTable, RebuildCrossRef and
    CPDF_ObjectStream as PDFium 7999 has them (document.py)."""
    data = _xref_cases()[name]
    assert _xref_said("pure", data) == _xref_said("pdfium", data)


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
