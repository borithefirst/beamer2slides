"""List numbering and bullets that Slides would draw differently from the PDF (wave-3 K2)."""

from beamer2slides import ir
from beamer2slides.classify import literal_list_numbers


def run(text: str) -> ir.Run:
    return {"text": text, "font": "LMSans10-Regular", "family": "sans", "size": 10.91, "bold": False,
            "italic": False, "smallcaps": False, "color": "#000000", "link": None, "script": None,
            "underline": False, "strike": False, "highlight": None}


def item(text: str, level: int, bullet: ir.Bullet | None) -> ir.Paragraph:
    return {"align": "left", "level": level, "bullet": bullet, "size": 10.91, "text_x0": 50.0 + 20 * level,
            "tab_x0": None, "lines": [{"baseline": 100.0, "x0": 50.0, "x1": 200.0}], "wrap_limit": None,
            "runs": [run(text)]}


def label(x0: float) -> ir.Label:
    return {"x0": x0, "baseline": 100.0, "font": "LMSans10-Regular", "family": "sans", "size": 10.91,
            "bold": False, "italic": False, "color": "#000000"}


def number(text: str) -> ir.NumberBullet:
    return {"kind": "number", "text": text, "color": "#000000", "bbox": [36.0, 92.0, 44.0, 101.0], "label": label(36.2)}


DOT: ir.GlyphBullet = {"kind": "glyph", "text": "•", "color": "#000000", "bbox": [58.0, 94.0, 62.0, 101.0],
                       "label": label(58.0)}


def slide_of(paragraphs: list[ir.Paragraph]) -> list[ir.Slide]:
    return [{"page": 0, "frame": "1", "label": None, "size": [362.83, 272.13], "notes": None, "left_in_background": [],
             "theme_texts": [], "panels": [], "figure_regions": [], "stats": {"chars": 0, "chars_native": 0},
             "elements": [{"id": "p2t1", "kind": "text", "role": "body", "bbox": [36.0, 90.0, 200.0, 200.0],
                           "paragraphs": paragraphs, "spans": []}]}]


def paragraphs_of(slides: list[ir.Slide]) -> list[ir.Paragraph]:
    (text,) = [e for e in slides[0]["elements"] if e["kind"] == "text"]
    return text["paragraphs"]


def test_numbers_continue_after_a_nested_glyph_list():
    """r1_ml_v1 s3: 1. (two nested bullets) 2. 3. came out 1, 1, 2: emit bullets each run of
    one preset on its own, so the numbers after the nested bullets were a new Slides list.
    Such numbers are written as literal labels, the nested bullets stay Slides bullets."""
    slides = slide_of([item("SparseFlow", 0, number("1.")), item("uses entropy", 1, DOT), item("budget", 1, DOT),
                       item("A fused kernel", 0, number("2.")), item("Evaluation", 0, number("3.")),
                       item("throughput", 1, DOT)])
    literal_list_numbers(slides)
    paras = paragraphs_of(slides)
    assert [p["runs"][0]["text"] for p in paras if p["level"] == 0] == ["1.\t", "2.\t", "3.\t"]
    assert all(p["bullet"] is None for p in paras if p["level"] == 0)
    assert all(p["bullet"] is DOT for p in paras if p["level"] == 1)


def test_a_plain_numbered_list_stays_a_slides_list():
    slides = slide_of([item("one", 0, number("1.")), item("two", 0, number("2.")), item("three", 0, number("3."))])
    literal_list_numbers(slides)
    assert [b["text"] for p in paragraphs_of(slides) if (b := p["bullet"]) is not None] == ["1.", "2.", "3."]


def test_a_numbered_list_nested_in_one_stays_a_slides_list():
    """1. a. b. 2.: one preset, one list, Slides numbers its levels 1., a., i. itself."""
    slides = slide_of([item("one", 0, number("1.")), item("sub", 1, number("a.")), item("sub", 1, number("b.")),
                       item("two", 0, number("2."))])
    literal_list_numbers(slides)
    assert all(p["bullet"] for p in paragraphs_of(slides))


def test_a_swatch_outlined_in_another_colour_is_no_slides_glyph():
    """r3_charts_v1 s9: legend swatches, filled squares with a black 0.8 pt outline, became
    square bullets: no outline, and smaller. With no glyph they become pictures by their text."""
    from beamer2slides.classify import bullet_shape
    swatch = {"type": "fs", "items": "re", "fill": "#1f77b4", "stroke": "#000000", "width": 0.8, "stroke_opacity": 1.0,
              "path": [["re", [[156.43, 88.95], [163.7, 96.22]]]]}
    assert bullet_shape(swatch) == {}
    assert bullet_shape({**swatch, "stroke": "#1f77b4"}) == {"shape": "square", "color": "#1f77b4"}
    assert bullet_shape({**swatch, "width": 0.1}) == {"shape": "square", "color": "#1f77b4"}, "a hairline shows no outline"
    assert bullet_shape({**swatch, "type": "f"}) == {"shape": "square", "color": "#1f77b4"}
