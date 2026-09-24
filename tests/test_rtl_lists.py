"""Right-to-left lists on synthetic pages: a Hebrew item's bullet hangs right of its words
(beamer with polyglossia/babel Hebrew), so it is found there, nested items step leftwards,
and two such columns of items side by side stay two lists of text, not a table.

Spans carry their text as PDFium's text page gives it (visual order, left to right), which
`classify` reads back into logical order (`bidi`, `read_lines`)."""

import pytest

from beamer2slides import bidi, emit, text_layout
from beamer2slides.classify import Rect, classify_page
from beamer2slides.gslides import EMU_PER_PT

from .test_emit_requests import FONTS, SCALE, run_of

W, H = 453.54, 255.12
FONT, BOLD = "ArialMT", "Arial-BoldMT"
SIZE = 10.91


def raw_span(i: int, logical: str, x0: float, baseline: float, size: float = SIZE, font: str = FONT,
             w: float | None = None) -> dict:
    w = len(logical) * 0.45 * size if w is None else w
    return {"id": f"p0s{i}", "text": logical[::-1], "font": font, "size": size, "color": "#000000",
            "alpha": 255, "origin": [x0, baseline], "bbox": [x0, baseline - 0.78 * size, x0 + w, baseline + 0.22 * size],
            "dir": [1.0, 0.0], "smallcaps": False}


def ball(i: int, x0: float, baseline: float) -> dict:
    """beamer's ball bullet: a 7 pt image whose middle is at x-height."""
    return {"id": f"p0i{i}", "bbox": [x0, baseline - 6.84, x0 + 7.0, baseline + 0.16], "px": [7, 7]}


def page(spans: list[dict], images: list[dict]) -> dict:
    for i, s in enumerate(spans):
        s["id"] = f"p0s{i}"
    return {"index": 0, "label": "1", "size": [W, H], "spans": spans, "images": images, "drawings": [],
            "links": [], "frame_label": None}


def texts(slide: dict) -> list[dict]:
    return [e for e in slide["elements"] if e["kind"] == "text"]


def words(p: dict) -> str:
    return "".join(r["text"] for r in p["runs"])


RIGHT = ["איזון מבטיח גובה לוגריתמי", "הכנסה עד שני סיבובים", "מחיקה עד הרבה סיבובים"]
LEFT = ["תרגיל בית ארבע הגשה", "קריאה פרקים אחרונים"]


def two_columns() -> dict:
    """The r3_scripts_rtl summary frame: a bold heading over each column, then its items, each
    with its ball right of its words (\\begin{columns} in a Hebrew frame)."""
    spans, images = [], []
    spans.append(raw_span(0, "למדנו", 415.05, 91.3, font=BOLD, w=25.47))
    spans.append(raw_span(0, "לשבוע הבא", 169.49, 91.3, font=BOLD, w=50.76))
    for k, t in enumerate(RIGHT):
        bl = 107.84 + 16.54 * k
        w = len(t) * 0.45 * SIZE
        spans.append(raw_span(0, t, 418.7 - w, bl, w=w))
        images.append(ball(len(images), 424.0, bl))
    for k, t in enumerate(LEFT):
        bl = 107.84 + 16.54 * k
        w = len(t) * 0.45 * SIZE
        spans.append(raw_span(0, t, 198.43 - w, bl, w=w))
        images.append(ball(len(images), 203.0, bl))
    return page(spans, images)


def test_the_ball_right_of_a_hebrew_item_is_its_bullet():
    slide = classify_page(two_columns(), SIZE)
    items = [p for e in texts(slide) for p in e["paragraphs"] if p["bullet"]]
    assert sorted(words(p) for p in items) == sorted(RIGHT + LEFT)
    assert all(p["bullet"]["kind"] == "image" and p.get("direction") == "rtl" for p in items)
    # (the ball is the one at the item's right end, not the other column's)
    for p in items:
        assert 0 < Rect.of(p["bullet"]["bbox"]).x0 - p["lines"][0]["x1"] <= 1.5 * SIZE


def test_two_columns_of_hebrew_items_are_no_table():
    slide = classify_page(two_columns(), SIZE)
    assert not [e for e in slide["elements"] if e["kind"] == "table"]
    # each column is a list of its own, its items one box
    boxes = [e for e in texts(slide) if any(p["bullet"] for p in e["paragraphs"])]
    assert sorted(len(e["paragraphs"]) for e in boxes) == [2, 3]
    assert all(p["align"] != "center" for e in texts(slide) for p in e["paragraphs"])


ENUM = ["מכניסים את המפתח כמו בעץ חיפוש רגיל.", "עולים מהעלה החדש לכיוון השורש ומעדכנים גבהים.",
        "בצומת הראשון שאינו מאוזן מבצעים סיבוב:"]
NESTED = ["סיבוב אחד בלבד", "שני סיבובים"]
LAST = "לכל היותר שני סיבובים בכל הכנסה!"


def enumerate_page() -> dict:
    """The r3_scripts_rtl enumerate: numbers on balls right of the items, a nested itemize
    whose smaller balls stand further left, then the last numbered item."""
    spans, images = [], []
    rows = [(t, 0) for t in ENUM] + [(t, 1) for t in NESTED] + [(LAST, 0)]
    bl, number = 100.0, 1
    # (the geometry of the deck's page 4: an 11 pt numbered ball, a 6 pt nested one)
    for t, level in rows:
        size = SIZE if level == 0 else 9.96
        right = 420.8 if level == 0 else 399.0
        w = len(t) * 0.45 * size
        spans.append(raw_span(0, t, right - w, bl, size=size, w=w))
        if level == 0:
            images.append({"id": "", "bbox": [426.0, bl - 9.0, 438.0, bl + 2.0], "px": [12, 11]})
            spans.append(raw_span(0, str(number), 430.3, bl - 1.5, size=5.98, w=3.3))
            number += 1
        else:
            images.append({"id": "", "bbox": [404.0, bl - 6.0, 410.0, bl], "px": [6, 6]})
        bl += 15.5 if level == 0 else 12.0
    for i, im in enumerate(images):
        im["id"] = f"p0i{i}"
    return page(spans, images)


def test_each_numbered_hebrew_item_is_its_own_paragraph():
    slide = classify_page(enumerate_page(), SIZE)
    paras = [p for e in texts(slide) for p in e["paragraphs"]]
    got = [words(p) for p in paras]
    for t in ENUM + NESTED + [LAST]:
        assert any(g.strip() == t for g in got), (t, got)
    # no ball digit is left inside an item's words
    assert not any(ch.isdigit() for g in got for ch in g)


def test_a_nested_hebrew_item_is_one_level_down():
    slide = classify_page(enumerate_page(), SIZE)
    paras = {words(p).strip(): p for e in texts(slide) for p in e["paragraphs"]}
    assert all(paras[t]["bullet"] and paras[t]["level"] == 1 for t in NESTED)


# -- what emit makes of them ------------------------------------------------------------------

def box_extent(el: dict) -> tuple[float, float]:
    """The text box emit creates for `el`, in PDF x."""
    reqs = emit.text_box_requests(el, "b2s_s001", "b2s_s001_t0", SCALE, FONTS)
    props = next(r["createShape"]["elementProperties"] for r in reqs if "createShape" in r)
    x = props["transform"]["translateX"] / EMU_PER_PT / SCALE
    return x, x + props["size"]["width"]["magnitude"] / EMU_PER_PT / SCALE


def test_a_hebrew_list_box_spans_its_words_and_not_only_its_bullet():
    # The bullet hangs right of the words, so it is the box's right edge, and the words its
    # left: a box from the bullet's left edge was 7 pt wide, and every item wrapped letter by letter.
    slide = classify_page(two_columns(), SIZE)
    box = next(e for e in texts(slide) if len(e["paragraphs"]) == 3)
    x0, x1 = box_extent(box)
    words_x0 = min(l["x0"] for p in box["paragraphs"] for l in p["lines"])
    bullet_x1 = max(p["bullet"]["bbox"][2] for p in box["paragraphs"])
    assert x0 <= words_x0 and x1 >= bullet_x1


@pytest.mark.parametrize("mark", sorted(bidi.MARKS))
def test_a_bidi_mark_takes_no_room(mark):
    # (bidi.logical_line writes an LRM or RLM where a formula starts with a number: counted as an
    # unmeasured letter, each one made the line 0.6 em wider than Slides sets it)
    plain, marked = run_of("O(1) abc"), run_of(f"{mark}O(1) abc{mark}")
    assert emit.slides_width([marked], SCALE, FONTS) == pytest.approx(emit.slides_width([plain], SCALE, FONTS))
    assert text_layout.advance(mark, {"fontFamily": "Arial"}, 10.0) == 0.0
