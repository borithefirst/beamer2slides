"""devtools.text_fit on synthetic pages: each finding, and silence where Slides drew what the PDF did.

Ink is drawn as striped bars on white, one bar per text line (5 px per pt, 10 pt text)."""
import numpy as np

from beamer2slides.devtools.text_fit import measure_slide

SCALE = 5.0  # px per PDF pt
W, H = 200, 120  # pt


def page() -> np.ndarray:
    return np.full((int(H * SCALE), int(W * SCALE), 3), 255, np.uint8)


def ink(img: np.ndarray, x0: float, y0: float, x1: float, y1: float) -> None:
    """Glyph-like ink: stems two pixels wide, two apart (a solid bar would read as a rule)."""
    img[int(y0 * SCALE):int(y1 * SCALE), int(x0 * SCALE):int(x1 * SCALE)][:, (np.arange(int(x1 * SCALE) - int(x0 * SCALE)) % 4) < 2] = 0


def lines(img: np.ndarray, x0: float, top: float, widths: list[float], pitch: float = 12.0) -> None:
    for i, w in enumerate(widths):
        ink(img, x0, top + i * pitch, x0 + w, top + i * pitch + 7)


def text(eid: str, bbox: list[float], align: str = "left") -> dict:
    return {"id": eid, "kind": "text", "bbox": bbox, "paragraphs": [
        {"size": 10.0, "align": align, "runs": [{"text": eid}]}]}


def slide(*elements: dict) -> dict:
    return {"page": 0, "size": [W, H], "elements": list(elements)}


def kinds(report: dict) -> list[str]:
    return sorted(f["kind"] for f in report["findings"])


def test_a_page_slides_draws_as_the_pdf_does_has_no_finding():
    ref = page()
    lines(ref, 20, 20, [120, 80])
    assert kinds(measure_slide(slide(text("t", [20, 20, 140, 39])), ref, ref.copy(), SCALE)) == []


def test_a_word_dropped_onto_a_new_line_is_a_wrap():
    ref, sl = page(), page()
    lines(ref, 20, 20, [150])
    lines(sl, 20, 20, [140, 20])  # the last word went down
    assert kinds(measure_slide(slide(text("t", [20, 20, 170, 27])), ref, sl, SCALE)) == ["wrap"]


def test_lines_that_touch_without_a_new_line_are_crowded_not_wrapped():
    ref, sl = page(), page()
    lines(ref, 20, 20, [120, 120])
    lines(sl, 20, 20, [120, 120])
    ink(sl, 60, 27, 64, 32)  # a subscript reaching down into the next line
    assert kinds(measure_slide(slide(text("t", [20, 20, 140, 39])), ref, sl, SCALE)) == ["crowded"]


def test_a_centred_line_is_judged_by_its_centre():
    ref, sl = page(), page()
    ink(ref, 50, 20, 150, 27)
    ink(sl, 48, 20, 152, 27)  # 4% wider, centred on the same point: no drift
    assert kinds(measure_slide(slide(text("t", [50, 20, 150, 27], "center")), ref, sl, SCALE)) == []
    sl2 = page()
    ink(sl2, 50, 20, 156, 27)  # 6% wider, left edge kept: its centre moved 3 pt
    report = measure_slide(slide(text("t", [50, 20, 150, 27], "center")), ref, sl2, SCALE)
    assert kinds(report) == ["drift"] and abs(report["findings"][0]["pt"] - 3.0) <= 0.4  # (stripes end short)


def test_a_table_that_grows_over_its_caption_is_grown_and_touches_it():
    ref, sl = page(), page()
    for img, pitch in ((ref, 12.0), (sl, 16.0)):
        lines(img, 20, 20, [100, 100, 100], pitch)  # rows of a table
    lines(ref, 30, 60, [80])  # the caption, 6 pt under the last row
    lines(sl, 30, 60, [80])
    table = {"id": "tab", "kind": "table", "bbox": [20, 20, 120, 51]}
    report = measure_slide(slide(table, text("cap", [30, 60, 110, 67])), ref, sl, SCALE)
    assert kinds(report) == ["grown", "touch"]
    touch = next(f for f in report["findings"] if f["kind"] == "touch")
    assert touch["element"].startswith("tab") and "cap" in touch["other"]


def test_a_line_narrower_than_the_pdf_is_a_width_finding():
    # Small caps the substitute sets smaller: one line of three, 20% narrower.
    ref, sl = page(), page()
    lines(ref, 20, 20, [120, 120, 120])
    lines(sl, 20, 20, [120, 96, 120])
    report = measure_slide(slide(text("t", [20, 20, 140, 51])), ref, sl, SCALE)
    assert kinds(report) == ["width"] and report["findings"][0]["line"] == 2


def test_a_neighbours_word_is_not_counted_as_this_texts_spill():
    # Right-aligned columns: the next column's wider number reaches back into the gap.
    ref, sl = page(), page()
    ink(ref, 20, 20, 60, 27)
    ink(ref, 90, 20, 110, 27)
    ink(sl, 20, 20, 60, 27)
    ink(sl, 84, 20, 110, 27)
    report = measure_slide(slide(text("a", [20, 20, 60, 27], "right"), text("b", [90, 20, 110, 27], "right")),
                           ref, sl, SCALE)
    assert kinds(report) == []
