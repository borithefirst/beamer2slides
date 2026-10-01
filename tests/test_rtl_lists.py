"""Right-to-left lists on synthetic pages: a Hebrew item's bullet hangs right of its words
(beamer with polyglossia/babel Hebrew), so it is found there, nested items step leftwards,
and two such columns of items side by side stay two lists of text, not a table.

Spans carry their text as PDFium's text page gives it (visual order, left to right), which
`classify` reads back into logical order (`bidi`, `read_lines`)."""

import pytest

from beamer2slides import bidi, emit, text_layout
from beamer2slides.classify import Rect, classify_page
from beamer2slides.gslides import EMU_PER_PT
from beamer2slides.ir import slide_json
from beamer2slides.json_types import JsonObject
from beamer2slides.raw_types import RawImage, RawPage, RawSpan
from beamer2slides.google_types import slides_json

from .json_reads import jnum, jnums, jobj, jobjs, jstr
from .test_emit_requests import BODY, FONTS, SCALE, text_run

W, H = 453.54, 255.12
FONT, BOLD = "ArialMT", "Arial-BoldMT"
SIZE = 10.91


def raw_span(i: int, logical: str, x0: float, baseline: float, *, size: float, font: str, w: float) -> RawSpan:
    return {"id": f"p0s{i}", "text": logical[::-1], "font": font, "size": size, "color": "#000000",
            "alpha": 255, "origin": [x0, baseline], "bbox": [x0, baseline - 0.78 * size, x0 + w, baseline + 0.22 * size],
            "dir": [1.0, 0.0], "smallcaps": False}


def ball(i: int, x0: float, baseline: float) -> RawImage:
    """beamer's ball bullet: a 7 pt image whose middle is at x-height."""
    return {"id": f"p0i{i}", "bbox": [x0, baseline - 6.84, x0 + 7.0, baseline + 0.16], "px": [7, 7]}


def page(spans: list[RawSpan], images: list[RawImage]) -> RawPage:
    for i, s in enumerate(spans):
        s["id"] = f"p0s{i}"
    return {"index": 0, "label": "1", "size": [W, H], "spans": spans, "images": images, "drawings": [],
            "links": [], "frame_label": None}


def texts(slide: JsonObject) -> list[JsonObject]:
    return [e for e in jobjs(slide, "elements") if e["kind"] == "text"]


def paragraphs(el: JsonObject) -> list[JsonObject]:
    return jobjs(el, "paragraphs")


def words(p: JsonObject) -> str:
    return "".join(jstr(r, "text") for r in jobjs(p, "runs"))


RIGHT = ["איזון מבטיח גובה לוגריתמי", "הכנסה עד שני סיבובים", "מחיקה עד הרבה סיבובים"]
LEFT = ["תרגיל בית ארבע הגשה", "קריאה פרקים אחרונים"]


def two_columns() -> RawPage:
    """The r3_scripts_rtl summary frame: a bold heading over each column, then its items, each
    with its ball right of its words (\\begin{columns} in a Hebrew frame)."""
    spans: list[RawSpan] = []
    images: list[RawImage] = []
    spans.append(raw_span(0, "למדנו", 415.05, 91.3, size=SIZE, font=BOLD, w=25.47))
    spans.append(raw_span(0, "לשבוע הבא", 169.49, 91.3, size=SIZE, font=BOLD, w=50.76))
    for k, t in enumerate(RIGHT):
        bl = 107.84 + 16.54 * k
        w = len(t) * 0.45 * SIZE
        spans.append(raw_span(0, t, 418.7 - w, bl, size=SIZE, font=FONT, w=w))
        images.append(ball(len(images), 424.0, bl))
    for k, t in enumerate(LEFT):
        bl = 107.84 + 16.54 * k
        w = len(t) * 0.45 * SIZE
        spans.append(raw_span(0, t, 198.43 - w, bl, size=SIZE, font=FONT, w=w))
        images.append(ball(len(images), 203.0, bl))
    return page(spans, images)


def test_the_ball_right_of_a_hebrew_item_is_its_bullet():
    slide = slide_json(classify_page(two_columns(), SIZE))
    items = [p for e in texts(slide) for p in paragraphs(e) if p["bullet"]]
    assert sorted(words(p) for p in items) == sorted(RIGHT + LEFT)
    assert all(jobj(p, "bullet")["kind"] == "image" and p.get("direction") == "rtl" for p in items)
    # (the ball is the one at the item's right end, not the other column's)
    for p in items:
        assert 0 < Rect.of(jnums(p, "bullet", "bbox")).x0 - jnum(p, "lines", 0, "x1") <= 1.5 * SIZE


def test_two_columns_of_hebrew_items_are_no_table():
    slide = slide_json(classify_page(two_columns(), SIZE))
    assert not [e for e in jobjs(slide, "elements") if e["kind"] == "table"]
    # each column is a list of its own, its items one box
    boxes = [e for e in texts(slide) if any(p["bullet"] for p in paragraphs(e))]
    assert sorted(len(paragraphs(e)) for e in boxes) == [2, 3]
    assert all(p["align"] != "center" for e in texts(slide) for p in paragraphs(e))


ENUM = ["מכניסים את המפתח כמו בעץ חיפוש רגיל.", "עולים מהעלה החדש לכיוון השורש ומעדכנים גבהים.",
        "בצומת הראשון שאינו מאוזן מבצעים סיבוב:"]
NESTED = ["סיבוב אחד בלבד", "שני סיבובים"]
LAST = "לכל היותר שני סיבובים בכל הכנסה!"


def enumerate_page() -> RawPage:
    """The r3_scripts_rtl enumerate: numbers on balls right of the items, a nested itemize
    whose smaller balls stand further left, then the last numbered item."""
    spans: list[RawSpan] = []
    images: list[RawImage] = []
    rows = [(t, 0) for t in ENUM] + [(t, 1) for t in NESTED] + [(LAST, 0)]
    bl, number = 100.0, 1
    # (the geometry of the deck's page 4: an 11 pt numbered ball, a 6 pt nested one)
    for t, level in rows:
        size = SIZE if level == 0 else 9.96
        right = 420.8 if level == 0 else 399.0
        w = len(t) * 0.45 * size
        spans.append(raw_span(0, t, right - w, bl, size=size, font=FONT, w=w))
        if level == 0:
            images.append({"id": "", "bbox": [426.0, bl - 9.0, 438.0, bl + 2.0], "px": [12, 11]})
            spans.append(raw_span(0, str(number), 430.3, bl - 1.5, size=5.98, font=FONT, w=3.3))
            number += 1
        else:
            images.append({"id": "", "bbox": [404.0, bl - 6.0, 410.0, bl], "px": [6, 6]})
        bl += 15.5 if level == 0 else 12.0
    for i, im in enumerate(images):
        im["id"] = f"p0i{i}"
    return page(spans, images)


def test_each_numbered_hebrew_item_is_its_own_paragraph():
    slide = slide_json(classify_page(enumerate_page(), SIZE))
    paras = [p for e in texts(slide) for p in paragraphs(e)]
    got = [words(p) for p in paras]
    for t in ENUM + NESTED + [LAST]:
        assert any(g.strip() == t for g in got), (t, got)
    # no ball digit is left inside an item's words
    assert not any(ch.isdigit() for g in got for ch in g)


def test_a_nested_hebrew_item_is_one_level_down():
    slide = slide_json(classify_page(enumerate_page(), SIZE))
    paras = {words(p).strip(): p for e in texts(slide) for p in paragraphs(e)}
    assert all(paras[t]["bullet"] and paras[t]["level"] == 1 for t in NESTED)


# -- what emit makes of them ------------------------------------------------------------------

def box_extent(el: JsonObject) -> tuple[float, float]:
    """The text box emit creates for `el`, in PDF x."""
    reqs = [slides_json(r) for r in emit.text_box_requests(el, "b2s_s001", "b2s_s001_t0", SCALE, FONTS)]
    props = next(jobj(r, "createShape", "elementProperties") for r in reqs if "createShape" in r)
    x = jnum(props, "transform", "translateX") / EMU_PER_PT / SCALE
    return x, x + jnum(props, "size", "width", "magnitude") / EMU_PER_PT / SCALE


def test_a_hebrew_list_box_spans_its_words_and_not_only_its_bullet():
    # The bullet hangs right of the words, so it is the box's right edge, and the words its
    # left: a box from the bullet's left edge was 7 pt wide, and every item wrapped letter by letter.
    slide = slide_json(classify_page(two_columns(), SIZE))
    box = next(e for e in texts(slide) if len(paragraphs(e)) == 3)
    x0, x1 = box_extent(box)
    words_x0 = min(jnum(l, "x0") for p in paragraphs(box) for l in jobjs(p, "lines"))
    bullet_x1 = max(jnum(p, "bullet", "bbox", 2) for p in paragraphs(box))
    assert x0 <= words_x0 and x1 >= bullet_x1


@pytest.mark.parametrize("mark", sorted(bidi.MARKS))
def test_a_bidi_mark_takes_no_room(mark: str) -> None:
    # (bidi.logical_line writes an LRM or RLM where a formula starts with a number: counted as an
    # unmeasured letter, each one made the line 0.6 em wider than Slides sets it)
    plain, marked = text_run("O(1) abc", BODY), text_run(f"{mark}O(1) abc{mark}", BODY)
    assert emit.slides_width([marked], SCALE, FONTS) == pytest.approx(emit.slides_width([plain], SCALE, FONTS))
    assert text_layout.advance(mark, {"fontFamily": "Arial"}, 10.0) == 0.0
