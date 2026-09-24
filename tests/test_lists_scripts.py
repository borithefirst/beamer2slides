"""List numbering and bullets that Slides would draw differently from the PDF (wave-3 K2)."""

from beamer2slides.classify import literal_list_numbers


def run(text: str) -> dict:
    return {"text": text, "font": "LMSans10-Regular", "family": "sans", "size": 10.91, "bold": False,
            "italic": False, "smallcaps": False, "color": "#000000", "link": None, "script": None,
            "underline": False, "strike": False, "highlight": None}


def item(text: str, level: int, bullet: dict | None) -> dict:
    return {"align": "left", "level": level, "bullet": bullet, "size": 10.91, "text_x0": 50.0 + 20 * level,
            "tab_x0": None, "lines": [{"baseline": 100.0, "x0": 50.0, "x1": 200.0}], "wrap_limit": None,
            "runs": [run(text)]}


def number(label: str) -> dict:
    return {"kind": "number", "text": label, "color": "#000000", "bbox": [36.0, 92.0, 44.0, 101.0], "spans": [],
            "label": {"x0": 36.2, "baseline": 100.0, "font": "LMSans10-Regular", "family": "sans", "size": 10.91,
                      "bold": False, "italic": False, "color": "#000000"}}


DOT = {"kind": "glyph", "text": "•", "color": "#000000", "bbox": [58.0, 94.0, 62.0, 101.0], "spans": [], "label": {"size": 10.91}}


def slide_of(paragraphs: list[dict]) -> list[dict]:
    return [{"elements": [{"id": "p2t1", "kind": "text", "role": "body", "paragraphs": paragraphs}]}]


def test_numbers_continue_after_a_nested_glyph_list():
    """r1_ml_v1 s3: 1. (two nested bullets) 2. 3. came out 1, 1, 2: emit bullets each run of
    one preset on its own, so the numbers after the nested bullets were a new Slides list.
    Such numbers are written as literal labels, the nested bullets stay Slides bullets."""
    slides = slide_of([item("SparseFlow", 0, number("1.")), item("uses entropy", 1, DOT), item("budget", 1, DOT),
                       item("A fused kernel", 0, number("2.")), item("Evaluation", 0, number("3.")),
                       item("throughput", 1, DOT)])
    literal_list_numbers(slides)
    paras = slides[0]["elements"][0]["paragraphs"]
    assert [p["runs"][0]["text"] for p in paras if p["level"] == 0] == ["1.\t", "2.\t", "3.\t"]
    assert all(p["bullet"] is None for p in paras if p["level"] == 0)
    assert all(p["bullet"] is DOT for p in paras if p["level"] == 1)


def test_a_plain_numbered_list_stays_a_slides_list():
    slides = slide_of([item("one", 0, number("1.")), item("two", 0, number("2.")), item("three", 0, number("3."))])
    literal_list_numbers(slides)
    assert [p["bullet"]["text"] for p in slides[0]["elements"][0]["paragraphs"]] == ["1.", "2.", "3."]


def test_a_numbered_list_nested_in_one_stays_a_slides_list():
    """1. a. b. 2.: one preset, one list, Slides numbers its levels 1., a., i. itself."""
    slides = slide_of([item("one", 0, number("1.")), item("sub", 1, number("a.")), item("sub", 1, number("b.")),
                       item("two", 0, number("2."))])
    literal_list_numbers(slides)
    assert all(p["bullet"] for p in slides[0]["elements"][0]["paragraphs"])
