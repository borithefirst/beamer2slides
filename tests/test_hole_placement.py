"""Hole pictures: gap detection in a marks thumbnail and the offline placement model (no Google API)."""

import numpy as np

from beamer2slides.emit import (FontMapper, SLIDE_W, find_marks, fit_holes, formula_shifts, hole_offset, ink_end,
                                mark_alpha, pick_gap, slide_holes, space_shift)

K = 1600 / SLIDE_W  # px per pt of a LARGE thumbnail


def paint(img, x0, y0, x1, y1, color):
    """A rectangle (pt) with anti-aliased left and right edges, like Google's renderer draws a highlight."""
    c = np.array(color, dtype=float)
    a, b = x0 * K, x1 * K
    for col in range(int(a), int(np.ceil(b))):
        cover = min(col + 1, b) - max(col, a)
        rows = slice(int(y0 * K), int(y1 * K))
        img[rows, col] = np.round(255 * (1 - cover) + c * cover)


def thumbnail():
    return np.full((900, 1600, 3), 255, dtype=np.uint8)


def test_marks_are_found_to_a_fraction_of_a_point():
    img = thumbnail()
    paint(img, 100.3, 50, 160.8, 66, (255, 0, 255))
    img[int(55 * K):int(60 * K), int(120 * K):int(125 * K)] = 0  # a glyph overhanging the highlight
    paint(img, 300.0, 50, 320.0, 66, (0, 255, 255))  # another hole's colour
    marks = find_marks(mark_alpha(img, "#ff00ff"), K)
    assert len(marks) == 1
    x0, y0, x1, y1 = marks[0]
    assert abs(x0 - 100.3) < 0.15 and abs(x1 - 160.8) < 0.15
    assert abs(y0 - 50) < 0.6 and abs(y1 - 66) < 0.6


def test_black_text_is_no_mark():
    img = thumbnail()
    img[100:140, 200:400] = 0
    img[200:240, 200:400] = 128  # anti-aliased grey glyph edges
    assert find_marks(mark_alpha(img, "#ffff00"), K) == []


def test_pick_gap_takes_the_mark_as_wide_as_the_hole_nearest_the_prediction():
    marks = [(90.0, 50.0, 130.0, 66.0), (200.0, 50.0, 240.0, 66.0), (95.0, 50.0, 110.0, 66.0)]
    dx, dy = pick_gap(marks, 100.0, 58.0, 40.0, 20.0)
    assert (dx, dy) == (-10.0, 0.0)
    assert pick_gap(marks, 100.0, 58.0, 60.0, 20.0) is None, "no mark of that width: keep the prediction"


def test_pick_gap_follows_a_hole_slides_wrapped_onto_the_next_line():
    marks = [(12.0, 70.5, 52.0, 86.5)]
    dx, dy = pick_gap(marks, 300.0, 58.0, 40.0, 20.0)
    assert (dx, dy) == (-288.0, 20.0)


def test_ink_end_finds_the_last_word_before_a_hanging_hole():
    img = thumbnail()
    img[int(52 * K):int(62 * K), int(40 * K):int(180.4 * K)] = 0
    img[int(80 * K):int(90 * K), int(40 * K):int(300 * K)] = 0  # the next line
    assert abs(ink_end(img, K, 54, 60, 0, 400) - 180.4) < 0.5


# Deck 19, slide 9: a circled word, then a boxed formula on the same line.
RUNS = [
    {"text": "A "},
    {"text": "\xa0", "font": "CMSS10", "family": "sans", "size": 10.91, "hole": 17.67, "hole_x0": 21.82,
     "before": [[7.26, "CMSS10", "sans", False, False, "A", 10.91]], "next_x0": 37.49},
    {"text": "text circle, a boxed "},
    {"text": "\xa0", "font": "CMSS10", "family": "sans", "size": 10.91, "hole": 40.71, "hole_x0": 128.17,
     "before": [[7.26, "CMSS10", "sans", False, False, "A", 10.91], [17.75, "CMSS10", "sans", False, False, "text", 37.49],
                [26.46, "CMSS10", "sans", False, False, "circle,", 58.89], [5.23, "CMSS10", "sans", False, False, "a", 88.99],
                [26.6, "CMSS10", "sans", False, False, "boxed", 97.85]], "next_x0": 163.39},
    {"text": "formula and here."},
]
for r in RUNS:
    r.update({k: v for k, v in {"font": "CMSS10", "family": "sans", "size": 10.91, "bold": False, "italic": False,
                                "smallcaps": False, "color": "#000000", "link": None}.items() if k not in r})


def slide(runs=RUNS):
    para = {"align": "left", "runs": runs, "lines": [{"baseline": 104.07, "x0": 10.91, "x1": 318.11}]}
    return {"size": [453.54, 255.12], "elements": [
        {"id": "p8h0", "kind": "image", "anchor": "p8t1", "bbox": [127.17, 92.73, 160.76, 109.16]},
        {"id": "p8h1", "kind": "image", "anchor": "p8t1", "bbox": [20.82, 94.2, 34.86, 108.07]},
        {"id": "p8t1", "kind": "text", "paragraphs": [para]},
    ]}


def test_an_earlier_hole_on_the_line_is_no_stretched_space():
    s = slide()
    em = FontMapper()(RUNS[3], SLIDE_W / 453.54)[1] / (SLIDE_W / 453.54)
    assert space_shift(RUNS[3], em) < -10, "the gap around the circled word taken for one stretched space"
    assert abs(space_shift(RUNS[3], em, [(21.82, 17.67)])) < 3
    shift = formula_shifts(s, SLIDE_W / 453.54, FontMapper())
    assert abs(shift["p8h0"]) < 3, "the circled word's gap counts as a space plus its hole, not one wide space"


def test_pictures_pair_with_their_holes():
    holes = slide_holes(slide())
    assert [pic["id"] for _, _, _, pic in holes] == ["p8h1", "p8h0"]


def test_holes_reach_to_the_next_word_and_pictures_share_the_spaces():
    scale = SLIDE_W / 453.54
    fonts = FontMapper()
    fitted = fit_holes(slide(), scale, fonts)
    _, p, run, pic = slide_holes(fitted)[1]
    space = 0.19 * fonts(run, scale)[1]
    # Slides: word space + hole == the PDF's room between "boxed" and "formula"
    assert abs(space / scale + run["hole"] - (163.39 - (97.85 + 26.6))) < 0.05
    o = hole_offset(p, run, pic, scale, fonts(run, scale)[1])
    left, right = space + o, (run["hole"] - (pic["bbox"][2] - pic["bbox"][0])) * scale - o
    pdf_left, pdf_right = pic["bbox"][0] - (97.85 + 26.6), 163.39 - pic["bbox"][2]
    assert abs(left / right - pdf_left / pdf_right) < 0.01
