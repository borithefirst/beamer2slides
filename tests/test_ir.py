"""deck.json's contract (`beamer2slides.ir`) holds for what its producers write: every built deck
as classified, a handful after render, and a marked page (an adopted source's PDF) at both stages;
the lookup tables emit keeps by those types stay total over them; and a deck that breaks the
contract is told where.

The PDFs come from `python tests/decks/build.py` and `python tests/themes/sweep.py --build`."""

import copy
import tempfile
import time
import typing
from functools import lru_cache
from pathlib import Path

import pytest

from beamer2slides import classify as classify_mod
from beamer2slides import emit, emit_pptx, ir, pdf, render
from beamer2slides.classify import classify
from beamer2slides.emit_diagrams import CONNECTION_SITES, TEXT_RECT_WIDTH
from beamer2slides.emit_metrics import BULLET_SHAPES, FONT_FOR_FAMILY, GLYPH_SHAPES
from beamer2slides.extract import extract, select_overlays
from beamer2slides.notes import prepare

from .test_marks import INLINE_IMAGE, cell, element, paragraph, raw_of, text

TESTS = Path(__file__).parent


def pdfs() -> list[Path]:
    out = sorted((TESTS / "decks" / "out").glob("*.pdf")) + sorted((TESTS / "decks" / "out" / "notes").glob("*.pdf"))
    return out + sorted((TESTS / "themes" / "out").glob("*/talk.pdf"))


def read(pdf: Path, tmp: Path) -> tuple[dict, dict, Path]:
    """(raw, deck, the PDF read) as `convert` gets them: notes split off, overlays' last steps."""
    prepared = prepare(pdf, tmp)
    raw = extract(prepared.pdf, prepared.labels)
    for page in raw["pages"]:
        page["notes"] = prepared.notes.get(page["index"])
    raw = select_overlays(raw, "last")
    return raw, classify(raw), prepared.pdf


@lru_cache(maxsize=None)
def classified() -> tuple[tuple[str, dict], ...]:
    out = []
    for path in pdfs():
        with tempfile.TemporaryDirectory() as tmp:
            out.append((str(path.relative_to(TESTS)).replace("\\", "/"), read(path, Path(tmp))[1]))
    return tuple(out)


def report(found: list[str]) -> str:
    return f"{len(found)} problem(s):\n" + "\n".join(found[:40])


# ------------------------------------------------------------------------------ what producers write

@pytest.mark.needs_decks("out")
def test_every_built_deck_holds_to_the_classified_contract():
    decks = classified()
    if not decks:
        pytest.skip("no PDFs built")
    found = [f"{name}: {p}" for name, deck in decks for p in ir.problems(deck, "classified")]
    assert not found, report(found)


# Rendering every deck takes ~50 s (test_invariants does it already); these cover what render
# adds: crops and kept files (03, 07, 23), blocks' shapes and shadows (04, 18), glyph bullets' ink
# and balls' colours (21, Madrid), overlays and holes (22, 13), tables (16).
RENDERED = ["out/03_figures.pdf", "out/04_theme_blocks.pdf", "out/07_images.pdf", "out/13_inline_math.pdf",
            "out/16_colored_table.pdf", "out/21_bullet_shapes.pdf", "out/22_overlays_on_text.pdf",
            "../themes/out/Madrid/talk.pdf"]


@pytest.mark.parametrize("name", RENDERED)
def test_a_rendered_deck_holds_to_the_rendered_contract(name, tmp_path):
    pdf = TESTS / "decks" / name
    if not pdf.exists():
        pytest.skip(f"not built: {name}")
    raw, deck, read_pdf = read(pdf, tmp_path)
    render.render_backgrounds(read_pdf, raw, deck, tmp_path / "out")
    found = ir.problems(deck, "rendered")
    assert not found, report(found)
    assert all((tmp_path / "out" / e["file"]).is_file() for s in deck["slides"] for e in s["elements"] if e["kind"] == "image")


def marked_pages() -> list[bytes]:
    """Pages of every mark adopt writes: a filled rectangle with its outline, a freeform (`custom`),
    a line, an outline alone, a picture and a text box; then a table with its grid."""
    table = (b"0.9 g 20 110 80 20 re f 100 110 80 20 re f 0 g 0 0 0 RG 1 w 20 110 m 180 110 l S " +
             cell(0, 0, text(24, 116, b"Name")) + cell(0, 1, text(146, 116, b"Value")) +
             cell(1, 0, text(24, 96, b"alpha")) + cell(1, 1, text(162, 96, b"42")))
    return [element(b"r", b"shape", b"0 0 1 RG 2 w 0 1 0 rg 10 10 40 30 re B", b" /box (10 110 40 30)") +
            element(b"f", b"shape", b"1 0 0 rg 70 10 m 110 10 l 90 45 l h f") +
            element(b"l", b"shape", b"0 0 0 RG 1 w 120 20 m 180 60 l S") +
            element(b"o", b"shape", b"0 0 0 RG 1 w 20 70 50 30 re S") +
            element(b"pic", b"picture", b"q 30 0 0 20 150 110 cm " + INLINE_IMAGE + b"Q ") +
            element(b"t", b"text", paragraph(0, text(60, 120, b"Words"))),
            element(b"tab", b"table", table, b" /rows 2 /cols 2 /box (20 20 160 40) /xs (0 80 160) /ys (0 20 40)")]


@pytest.mark.parametrize("backend", ["pdfium", "pure"])
def test_marked_pages_hold_to_both_contracts(tmp_path, backend):
    """The read-back of an adopted PDF: its freeform and line are legal shapes as classified (compare
    reads them) and pictures once rendered (`marked.pictured_shapes`)."""
    with pdf.use_backend(backend):
        check_marked_pages(tmp_path)


def check_marked_pages(tmp_path):
    raw = raw_of(tmp_path, marked_pages())
    deck = classify(raw)
    shapes = {e["mark"]: e["shape"] for e in deck["slides"][0]["elements"] if e["kind"] == "shape"}
    assert {shapes["f"], shapes["l"]} == {"custom", "line"}
    assert {e["kind"] for s in deck["slides"] for e in s["elements"]} == {"text", "image", "shape", "table"}
    assert not (found := ir.problems(deck, "classified")), report(found)
    render.render_backgrounds(tmp_path / "p.pdf", raw, deck, tmp_path / "out")
    assert not (found := ir.problems(deck, "rendered")), report(found)


def test_a_marked_table_with_no_words_is_still_a_table_emit_can_lay_out(tmp_path):
    page = element(b"t", b"table", b"0 0 0 RG 1 w 20 110 m 180 110 l S ", b" /rows 2 /cols 2 /box (20 20 160 40)")
    deck = classify(raw_of(tmp_path, [page]))
    assert not (found := ir.problems(deck, "classified")), report(found)


# ------------------------------------------------------------------------------ lookup tables

def args(tp) -> set:
    return set(typing.get_args(tp))


def test_emits_lookup_tables_stay_total_over_the_contracts_values():
    """A value the contract allows is one emit has an entry for (a shape kind with no template
    was KeyError 'custom'), and a table's keys are values the contract names."""
    assert emit_pptx.TEMPLATE_KINDS == typing.get_args(ir.TemplateKind)
    assert set(emit_pptx.TEMPLATE_PRESETS) <= args(ir.TemplateKind)
    assert set(TEXT_RECT_WIDTH) | set(CONNECTION_SITES) <= args(ir.TemplateKind)
    assert args(ir.ShapeKind) <= set(emit_pptx.TEMPLATE_KINDS)
    assert not args(ir.ProducerShapeKind) & set(emit_pptx.TEMPLATE_KINDS)
    assert args(ir.BulletShape) <= set(BULLET_SHAPES)
    assert set(GLYPH_SHAPES.values()) <= set(BULLET_SHAPES)
    assert set(FONT_FOR_FAMILY) <= args(ir.Family)
    assert set(emit.PICTURE_TITLES) <= args(ir.ImageRole)
    assert args(ir.ElementKind) == {typing.get_args(typing.get_type_hints(t)["kind"])[0]
                                    for t in typing.get_args(ir.Element)}


def typeddicts() -> list[type]:
    return [v for v in vars(ir).values() if isinstance(v, type) and issubclass(v, dict) and hasattr(v, "__required_keys__")]


def test_every_type_resolves_and_says_each_key_once():
    """What ir.py documents is what the check reads: every hint resolves on this Python, and no
    key is both required and optional (3.10 counts a key declared twice in a hierarchy as both)."""
    assert len(typeddicts()) > 30
    for t in typeddicts():
        hints = typing.get_type_hints(t, include_extras=True)
        assert set(hints) == t.__required_keys__ | t.__optional_keys__, t.__name__
        assert not t.__required_keys__ & t.__optional_keys__, t.__name__
        ir.checker(t)  # compiles


# ------------------------------------------------------------------------------ what a problem says

def small_deck() -> dict:
    """A one-slide deck as classify writes one, by hand."""
    run = {"text": "Hello", "font": "CMSS10", "family": "sans", "size": 10.9, "bold": False, "italic": False,
           "smallcaps": False, "color": "#000000", "link": None, "script": None, "underline": False,
           "strike": False, "highlight": None}
    para = {"align": "left", "level": 0, "bullet": None, "size": 10.9, "text_x0": 20.0,
            "tab_x0": None, "lines": [{"baseline": 40.0, "x0": 20.0, "x1": 50.0}], "wrap_limit": None, "runs": [run]}
    return {
        "version": 1, "source": {"pdf": "d.pdf", "pages": 1, "producer": "pdfTeX", "title": "T"},
        "body_size": 10.9, "stats": {"chars": 5, "chars_native": 5, "native_share": 1.0}, "layout_texts": [],
        "slides": [{
            "page": 0, "frame": "1", "label": None, "size": [362.83, 272.13], "notes": None,
            "left_in_background": [], "theme_texts": [], "panels": [], "figure_regions": [],
            "stats": {"chars": 5, "chars_native": 5}, "on_layout": [],
            "elements": [
                {"id": "p0s0", "kind": "shape", "role": "panel", "bbox": [10.0, 10.0, 100.0, 60.0], "fill": "#e6e6ff",
                 "shape": "RECTANGLE", "flip": False, "radius": 0.0, "drawing": "p0d0", "spans": []},
                {"id": "p0t0", "kind": "text", "role": "body", "bbox": [20.0, 30.0, 50.0, 42.0], "panel": 0,
                 "paragraphs": [para], "code": False, "spans": ["s0"], "strokes": []},
            ]}],
    }


def rendered(deck: dict) -> dict:
    for s in deck["slides"]:
        s.update(background="backgrounds/bg-001.png", background_color=None)
    return deck


def test_a_well_formed_deck_has_no_problems():
    assert ir.problems(small_deck()) == []
    assert ir.problems(rendered(small_deck()), "rendered") == []
    ir.validate(small_deck())


def test_the_shape_marked_pages_wrote_before_688ebf4_is_a_problem():
    """What `marked.shape_element` wrote for a freeform before 688ebf4: `custom`, no `flip`, its
    outline a colour. emit's template shapes raised KeyError 'custom' on it; the check says so
    at the element, key by key."""
    deck = small_deck()
    deck["slides"][0]["elements"][0] = {
        "id": "p0m0", "kind": "shape", "role": "panel", "bbox": [70.0, 10.0, 110.0, 45.0], "fill": None,
        "outline": "#000000", "shape": "custom", "radius": 0.0, "drawings": ["p0d0"], "spans": [], "mark": "f"}
    found = ir.problems(deck, "classified")
    assert "slide page 0, element p0m0: lacks required key 'flip' (bool)" in found
    assert "slide page 0, element p0m0: outline: is '#000000' (str), expected Outline" in found
    assert len(found) == 2
    found = ir.problems(rendered(deck), "rendered")
    assert "slide page 0, element p0m0: shape: is 'custom', expected one of " \
           "'ROUND_RECTANGLE' | 'ROUND_2_SAME_RECTANGLE' | 'RECTANGLE' | 'ELLIPSE' | 'DIAMOND' | 'TRIANGLE'" in found
    assert "slide page 0, element p0m0: fill: is None (NoneType), expected a colour '#rrggbb', lowercase" in found
    with pytest.raises(ValueError, match="lacks required key 'flip'"):
        ir.validate(deck, "rendered")


def test_a_problem_names_its_slide_element_key_and_what_is_wrong():
    deck = small_deck()
    slide = deck["slides"][0]
    slide["page"] = 4
    text_el = slide["elements"][1]
    text_el["id"] = "p4t0"
    text_el["paragraphs"][0]["align"] = "justify"
    text_el["paragraphs"][0]["runs"][0]["color"] = "#FFF"
    text_el["paragraphs"][0]["bullet"] = {"kind": "glyph", "text": "•", "bbox": [1.0, 2.0, 3.0]}
    del text_el["bbox"]
    text_el["colour"] = "#000000"
    slide["elements"].append({"id": "p4x0", "kind": "blob"})
    del deck["body_size"]
    assert ir.problems(deck) == [
        "deck: lacks required key 'body_size' (float)",
        "slide page 4, element p4t0: lacks required key 'bbox' (Box)",
        "slide page 4, element p4t0: paragraphs[0].align: is 'justify', expected one of 'left' | 'center' | 'right'",
        "slide page 4, element p4t0: paragraphs[0].bullet.bbox: has 3 items, expected 4",
        "slide page 4, element p4t0: paragraphs[0].runs[0].color: is '#FFF', expected a colour '#rrggbb', lowercase",
        "slide page 4, element p4t0: has unknown key 'colour' (not in TextElement)",
        "slide page 4: elements[2].kind: is 'blob', expected one of 'text', 'image', 'shape', 'table', 'diagram'",
    ]
    assert not any("unknown key" in p for p in ir.problems(deck, unknown_keys=False))
    with pytest.raises(ValueError, match=r"7 problems"):
        ir.validate(deck)


def test_numbers_and_flags_are_told_apart():
    """JSON's numbers: an int is a float's value, but a bool is neither, nor a number a bool."""
    deck = small_deck()
    el = deck["slides"][0]["elements"][0]
    el["radius"] = 3          # fine: an int where a float goes
    el["flip"] = 0            # not a bool
    el["bbox"] = [10, 10, True, 60]
    assert ir.problems(deck) == [
        "slide page 0, element p0s0: bbox[2]: is True (bool), expected float",
        "slide page 0, element p0s0: flip: is 0 (int), expected bool",
    ]


def test_checking_a_long_deck_takes_well_under_a_second():
    deck = small_deck()
    slide = deck["slides"][0]
    para = slide["elements"][1]["paragraphs"][0]
    slide["elements"][1]["paragraphs"] = [copy.deepcopy(para) for _ in range(12)]
    slide["elements"] *= 10
    deck["slides"] = [dict(slide, page=i) for i in range(60)]
    ir.problems(deck)  # (the first call compiles the checks)
    start = time.perf_counter()
    assert ir.problems(deck) == []
    assert time.perf_counter() - start < 0.5


def test_a_stage_is_one_the_contract_knows():
    with pytest.raises(ValueError, match="stage 'emitted'"):
        ir.problems(small_deck(), "emitted")


def test_classify_page_output_is_a_slide_of_the_contract(tmp_path):
    """The slide `classify_page` returns alone (the read-back compare uses) is a contract slide."""
    raw = raw_of(tmp_path, [text(20, 100, b"First box words") + text(20, 86, b"Second box words")])
    slide = classify_mod.classify_page(raw["pages"][0], classify_mod.body_size(raw))
    slide.setdefault("on_layout", [])
    deck = small_deck()
    deck["slides"] = [slide]
    assert not (found := ir.problems(deck)), report(found)
