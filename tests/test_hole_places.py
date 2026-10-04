"""Formula pictures over their holes: where Slides puts a hole (the advances of what its box holds
before it, a hanging label's tab stop), a box measured with its holes so a line ending on one keeps
its words, a framed formula with one span of prose beside it, and a radical sign set above its
formula's baseline in the hole's crop (real_linear-attention-a s35/s38, real_beamer-monodromy).
Offline: the expected places are the ones measured live on Google's thumbnails."""

from beamer2slides.classify import PageClassifier
from beamer2slides.emit_holes import fit_holes, formula_shifts, hole_slide_dicts, slide_holes_of, slides_hole_x
from beamer2slides.emit_metrics import FontMapper
from beamer2slides.emit_text import held_paragraph
from beamer2slides.emit_widths import paragraph_dict, slides_lines_of
from beamer2slides.json_types import Json, JsonObject
from beamer2slides.pdf import Char
from beamer2slides.raw_types import RawDrawing, RawSpan
from beamer2slides.render import owned_by

FONTS = FontMapper()
PAGE = [362.83, 272.13]
SCALE = 720 / PAGE[0]

Run = tuple[str, str, str, bool, bool, str | None]
"""(text, font, family, bold, italic, script)."""


def run(r: Run, size: float) -> JsonObject:
    text, font, family, bold, italic, script = r
    return {"text": text, "font": font, "family": family, "size": size, "bold": bold, "italic": italic,
            "smallcaps": False, "color": "#000000", "link": None, "script": script, "underline": False,
            "strike": False, "highlight": None}


def hole(font: str, size: float, width: float, x0: float, next_x0: float | None,
         before: list[tuple[float, str, str, bool, bool, str, float]]) -> JsonObject:
    """A hole run as classify writes it; `before`: (PDF width, font, family, bold, italic, word, x0)."""
    words: list[Json] = [[w, f, fam, b, i, t, x] for w, f, fam, b, i, t, x in before]
    return {**run(("", font, "sans", False, False, None), size), "hole": width, "hole_x0": x0,
            "before": words, "next_x0": next_x0}


def paragraph(runs: list[JsonObject], text_x0: float, tab_x0: float | None, lines: list[tuple[float, float, float]]
              ) -> JsonObject:
    """A left-aligned paragraph; `lines`: (baseline, x0, x1)."""
    rows: list[Json] = [{"baseline": b, "x0": x0, "x1": x1} for b, x0, x1 in lines]
    body: list[Json] = list(runs)
    return {"align": "left", "level": 0, "bullet": None, "size": 10.91, "text_x0": text_x0, "tab_x0": tab_x0,
            "lines": rows, "wrap_limit": None, "runs": body}


def slide(pictures: list[tuple[str, list[float]]], paragraphs: list[JsonObject]) -> JsonObject:
    """A slide of one text box `t` and hole pictures anchored to it."""
    elements: list[Json] = [{"id": pid, "kind": "image", "role": "math", "anchor": "t", "bbox": list(bbox)}
                            for pid, bbox in pictures]
    paras: list[Json] = list(paragraphs)
    elements.append({"id": "t", "kind": "text", "role": "body", "paragraphs": paras})
    return {"size": list(PAGE), "elements": elements}


SANS, BOLD, SUB = ("CMSS10", "sans", False, False), ("CMSSBX10", "sans", True, False), "sub"


def cm(text: str, face: tuple[str, str, bool, bool]) -> Run:
    font, family, bold, italic = face
    return (text, font, family, bold, italic, None)


def t_sub() -> Run:
    return ("t", "CMSSI8", "sans", False, True, SUB)


def minus_one() -> list[Run]:
    return [("−", "CMSY8", "sans", False, False, SUB), ("1", "CMSS8", "sans", False, False, SUB)]


# real_linear-attention-a s35: "Hebbian update rule: S_t = S_{t-1} + v_t k" and a stacked
# superscript/subscript hole ending the line. The words before it are sub- and superscripts that
# Slides sets at its own sizes: the picture stayed at its PDF place, 30 pt right of its gap.
HEBBIAN: list[Run] = [cm("Hebbian update rule: ", SANS), cm("S", BOLD), t_sub(), cm(" =", SANS), cm(" S", BOLD), t_sub(),
                      *minus_one(), cm("\xa0+", SANS), cm(" v", BOLD), t_sub(), cm("k", BOLD)]
HEBBIAN_BEFORE = [(37.28, "CMSS10", "sans", False, False, "Hebbian", 50.17),
                  (31.21, "CMSS10", "sans", False, False, "update", 91.09),
                  (19.81, "CMSS10", "sans", False, False, "rule:", 125.93),
                  (6.67, "CMSSBX10", "sans", True, False, "S", 150.59), (3.05, "CMSSI8", "sans", False, True, "t", 157.38),
                  (8.47, "CMSS10", "sans", False, False, "=", 164.53), (9.7, "CMSSBX10", "sans", True, False, " S", 173.0),
                  (3.05, "CMSSI8", "sans", False, True, "t", 182.71), (6.58, "CMSY8", "math", False, False, "−", 186.33),
                  (4.23, "CMSS8", "sans", False, False, "1", 192.91), (8.47, "CMSS10", "sans", False, False, "+", 200.08),
                  (7.88, "CMSSBX10", "sans", True, False, " v", 208.55), (3.05, "CMSSI8", "sans", False, True, "t", 216.44),
                  (5.78, "CMSSBX10", "sans", True, False, "k", 220.56)]


def hebbian() -> JsonObject:
    runs = [run(r, 10.91) for r in HEBBIAN] + [hole("CMSS10", 10.91, 8.58, 226.35, None, HEBBIAN_BEFORE)]
    return slide([("p34h0", [225.35, 172.96, 233.93, 189.69])], [paragraph(runs, 50.17, None, [(184.25, 50.17, 232.93)])])


# real_linear-attention-a s38: "SGD update:<TAB>S_t = ..." - a hanging label, the line's x0 where
# the label starts, the formula from the tab stop on; its last hole 92 pt left of its PDF place.
SGD: list[Run] = [cm("SGD update:\tS", BOLD), t_sub(), cm(" =", SANS), cm(" S", BOLD), t_sub(), *minus_one(),
                  ("\xa0−", "CMSY10", "sans", False, False, None), ("\xa0β", "CMMI10", "serif", False, True, None),
                  t_sub(), ("∇ℒ", "CMSY10", "sans", False, False, None), t_sub(), cm("(", SANS), cm("S", BOLD),
                  t_sub(), *minus_one(), cm(")\xa0=", SANS), cm(" S", BOLD), t_sub(), *minus_one(),
                  ("\xa0−", "CMSY10", "sans", False, False, None), ("\xa0β", "CMMI10", "serif", False, True, None),
                  t_sub(), cm("(", SANS), cm("S", BOLD), t_sub(), *minus_one(), cm("k", BOLD), t_sub(),
                  (" −", "CMSY10", "sans", False, False, None), cm(" v", BOLD), t_sub(), cm(")", SANS), cm("k", BOLD)]
SGD_BEFORE = [(23.32, "CMSSBX10", "sans", True, False, "SGD", 28.35), (37.73, "CMSSBX10", "sans", True, False, "update:", 55.67),
              (6.67, "CMSSBX10", "sans", True, False, "S", 98.73), (3.06, "CMSSI8", "sans", False, True, "t", 105.42),
              (8.48, "CMSS10", "sans", False, False, "=", 112.57), (9.7, "CMSSBX10", "sans", True, False, " S", 121.05),
              (3.05, "CMSSI8", "sans", False, True, "t", 130.76), (6.59, "CMSY8", "math", False, False, "−", 134.37),
              (4.23, "CMSS8", "sans", False, False, "1", 140.96), (8.48, "CMSY10", "math", False, False, "−", 148.12),
              (8.58, "CMMI10", "math", False, False, " β", 156.6), (3.05, "CMSSI8", "sans", False, True, "t", 165.2),
              (16.61, "CMSY10", "math", False, False, "∇ℒ", 169.32), (3.05, "CMSSI8", "sans", False, True, "t", 185.94),
              (4.23, "CMSS10", "sans", False, False, "(", 190.06), (6.67, "CMSSBX10", "sans", True, False, "S", 194.29),
              (3.05, "CMSSI8", "sans", False, True, "t", 200.97), (6.58, "CMSY8", "math", False, False, "−", 204.59),
              (4.23, "CMSS8", "sans", False, False, "1", 211.17), (15.74, "CMSS10", "sans", False, False, ") =", 215.91),
              (9.69, "CMSSBX10", "sans", True, False, " S", 231.65), (3.06, "CMSSI8", "sans", False, True, "t", 241.36),
              (6.58, "CMSY8", "math", False, False, "−", 244.98), (4.24, "CMSS8", "sans", False, False, "1", 251.56),
              (8.48, "CMSY10", "math", False, False, "−", 258.73), (8.58, "CMMI10", "math", False, False, " β", 267.21),
              (3.05, "CMSSI8", "sans", False, True, "t", 275.81), (4.23, "CMSS10", "sans", False, False, "(", 279.93),
              (6.67, "CMSSBX10", "sans", True, False, "S", 284.16), (3.05, "CMSSI8", "sans", False, True, "t", 290.84),
              (6.58, "CMSY8", "math", False, False, "−", 294.46), (4.23, "CMSS8", "sans", False, False, "1", 301.04),
              (5.78, "CMSSBX10", "sans", True, False, "k", 305.78), (3.05, "CMSSI8", "sans", False, True, "t", 311.57),
              (8.48, "CMSY10", "math", False, False, "−", 318.11), (7.88, "CMSSBX10", "sans", True, False, " v", 326.59),
              (3.05, "CMSSI8", "sans", False, True, "t", 334.48), (4.23, "CMSS10", "sans", False, False, ")", 338.6),
              (5.78, "CMSSBX10", "sans", True, False, "k", 342.83)]


def sgd() -> JsonObject:
    runs = [run(r, 10.91) for r in SGD] + [hole("CMSSBX10", 10.91, 8.58, 348.63, None, SGD_BEFORE)]
    return slide([("p37h0", [347.63, 192.67, 356.21, 209.95])], [paragraph(runs, 28.35, 98.73, [(204.51, 28.35, 355.21)])])


def predicted(s: JsonObject) -> tuple[float, float]:
    """(where Slides starts the slide's one hole, how far its picture moves), slide pt."""
    fitted = fit_holes(s, SCALE, FONTS)
    holes = [h for h in slide_holes_of(hole_slide_dicts(fitted)[0]) if h.picture is not None]
    assert len(holes) == 1
    x = slides_hole_x(holes[0], SCALE, FONTS)
    assert x is not None, "the hole's place is measured from the advances of the words before it"
    pic = holes[0].picture
    assert pic is not None
    return x, formula_shifts(fitted, SCALE, FONTS).get(pic.id, 0.0) * SCALE


def test_a_hole_after_scripts_is_where_slides_sets_their_advances() -> None:
    # Live (s35, the scratch slide's thumbnail): the words end and the gap starts at 416.7 pt;
    # the picture is drawn at 447.2 pt in the PDF, so it moves 30.5 pt left.
    x, shift = predicted(hebbian())
    assert abs(x - 416.7) < 3, f"gap predicted at {x:.1f} pt"
    assert abs(shift + 30.5) < 3, f"picture moved {shift:.1f} pt"


def test_a_hole_after_a_hanging_label_is_measured_from_the_tab_stop() -> None:
    # Live (s38, ℒ in STIX Two Math: fonts.letter_face): the k before the gap ends its ink at
    # 598.0 pt and the moved picture's ink starts at 601.2 pt; the PDF's picture is at 689.8 pt.
    # (With ℒ from Lato's fallback the gap started at 595.4 pt.)
    x, shift = predicted(sgd())
    assert abs(x - 598.6) < 3, f"gap predicted at {x:.1f} pt"
    assert abs(shift + 91.2) < 3, f"picture moved {shift:.1f} pt"


# real_beamer-monodromy s17: "4)<TAB>These two maps are mutually inverse, so <framed formula>."
ITEM: list[JsonObject] = [
    run(("4)\t", "LinBiolinumT", "sans", False, False, None), 10.91),
    run(("These two maps are mutually inverse, so ", "LinBiolinumT", "sans", False, False, None), 10.91),
    {**hole("LinBiolinumT", 10.91, 100.58, 233.51, 332.09,
            [(180.78, "LinBiolinumT", "sans", False, False, "These two maps are mutually inverse, so", 50.17)]),
     "text": "\xa0"},
    run((".", "LinBiolinumT", "sans", False, False, None), 10.91)]


def test_a_line_ending_on_a_framed_formula_is_measured_with_its_hole() -> None:
    """The item's box is sized from where Slides ends the line, its hole and its label included:
    left at the PDF's extent, Lato's words took a little more, and the formula wrapped onto a
    line of its own, pushing the items under it down while their pictures stayed."""
    p = held_paragraph(paragraph_dict(paragraph(ITEM, 36.37, 50.17, [(124.92, 50.17, 334.49)])), SCALE, FONTS)
    got = slides_lines_of(p, SCALE, FONTS)
    assert got is not None, "a line with a hole and a hanging label is measured"
    widest, _ = got
    hole_w = 100.58 * SCALE
    assert widest > 50.17 * SCALE + hole_w + 150 * SCALE, "the tab stop, the words and the hole all count"
    assert widest > 334.49 * SCALE, "Slides sets the words wider than the PDF's line: the box must grow"


def test_line_starts_that_do_not_fit_their_lines_measure_nothing() -> None:
    """Starts recorded before a framed formula became one hole put most of the paragraph on its
    first line: such a line is not that line, and the box keeps its other sizing (s15's caption
    ran 250 pt past the slide)."""
    words = "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore"
    runs = [run((words + " ", "LinBiolinumT", "sans", False, False, None), 10.91),
            {**hole("LinBiolinumT", 10.91, 40.0, 0.0, 200.0, []), "text": "\xa0"},
            run((" et dolore.", "LinBiolinumT", "sans", False, False, None), 10.91)]
    p = paragraph(runs, 50.0, None, [(100.0, 50.0, 150.0), (113.0, 50.0, 150.0)])
    text = words + " \xa0 et dolore."
    p["line_starts"] = [text.index("et dolore."), len(text)]
    assert slides_lines_of(held_paragraph(paragraph_dict(p), SCALE, FONTS), SCALE, FONTS) is None
    good = text.index("consectetur")
    p["line_starts"] = [good, len(text)]
    p["lines"] = [{"baseline": 100.0, "x0": 50.0, "x1": 205.0}, {"baseline": 113.0, "x0": 50.0, "x1": 330.0}]
    assert slides_lines_of(held_paragraph(paragraph_dict(p), SCALE, FONTS), SCALE, FONTS) is not None


# real_beamer-monodromy s21: "the points ±√z" - txfonts set the radical sign from an origin 7 pt
# above the formula's baseline, inside the span's box.
def char(c: str, font: str, origin: tuple[float, float], box: tuple[float, float, float, float]) -> Char:
    return Char(c=c, font=font, size=10.909, color=0, alpha=255, origin=origin, box=box, dir=(1.0, 0.0), obj=0,
                font_id=0, advance=box[2] - box[0], synthetic=False, ascent=0.8, descent=-0.2, exact_advance=True)


def test_a_radical_raised_off_its_baseline_is_its_formulas() -> None:
    span: RawSpan = {"id": "p71s19", "text": " ±√", "font": "txsys", "size": 10.909, "color": "#000000",
                     "alpha": 255, "origin": [73.7, 50.24], "bbox": [73.7, 34.17, 91.03, 52.17], "dir": [1.0, -0.0],
                     "smallcaps": False}
    owns = owned_by([span])
    assert owns(char("±", "txsys", (76.42, 50.24), (76.42, 41.26, 83.36, 52.17)))
    assert owns(char("√", "txsys", (83.36, 43.15), (83.36, 34.17, 91.03, 45.08))), \
        "the radical sign belongs to the hole's crop"
    assert not owns(char("→", "txsys", (75.93, 36.69), (75.93, 27.72, 87.1, 38.62))), \
        "an arrow of the line above, in the same font, is not the formula's"


# real_beamer-monodromy s18: a framed formula, then "by Galois correspondence." as one span.
def raw_span(i: int, text: str, x0: float, x1: float, baseline: float, font: str) -> RawSpan:
    return {"id": f"p0s{i}", "text": text, "font": font, "size": 10.909, "color": "#000000", "alpha": 255,
            "origin": [x0, baseline], "bbox": [x0, baseline - 8.03, x1, baseline + 2.88], "dir": [1.0, 0.0],
            "smallcaps": False}


def rule(i: int, x0: float, y0: float, x1: float, y1: float) -> RawDrawing:
    return {"id": f"p0d{i}", "type": "s", "items": "l", "bbox": [x0, y0, x1, y1], "fill": None, "stroke": "#000000",
            "width": 0.4, "fill_opacity": 1.0, "stroke_opacity": 1.0, "soft_mask": False, "corners": {},
            "path": [("l", [[x0, y0], [x1, y1]])]}


def test_a_framed_formula_beside_one_span_of_prose_is_a_hole() -> None:
    spans = [raw_span(0, "Then the map is an isomorphism of groups, and", 50.16, 260.0, 112.0, "LinBiolinumT"),
             raw_span(1, "Y", 53.55, 59.57, 137.28, "LinLibertineTI"),
             raw_span(2, "=", 63.82, 70.76, 137.28, "txmiaX"),
             raw_span(3, " Aut", 70.76, 89.76, 137.28, "LinLibertineT"),
             raw_span(4, "(", 90.3, 93.94, 137.28, "txsys"),
             raw_span(5, "X", 94.37, 101.22, 137.28, "LinLibertineTI"),
             raw_span(6, "x", 101.22, 105.01, 138.92, "LinLibertineTI"),
             raw_span(7, "|", 109.46, 111.64, 137.28, "txsys"),
             raw_span(8, "Y", 115.11, 121.13, 137.28, "LinLibertineTI"),
             raw_span(9, ")\\", 122.34, 131.54, 137.28, "txsys"),
             raw_span(10, "X", 131.54, 138.39, 137.28, "LinLibertineTI"),
             raw_span(11, "x", 138.39, 142.18, 138.92, "LinLibertineTI"),
             raw_span(12, "by Galois correspondence.", 149.27, 266.86, 137.28, "LinBiolinumT"),
             raw_span(13, "which ends the proof of the second part of the theorem.", 50.16, 300.0, 162.0, "LinBiolinumT")]
    frame = [rule(0, 50.16, 124.14, 146.54, 124.14), rule(1, 50.36, 124.14, 50.36, 142.61),
             rule(2, 146.35, 124.14, 146.35, 142.61), rule(3, 50.16, 142.61, 146.54, 142.61)]
    page = PageClassifier({"index": 0, "label": "1", "size": list(PAGE), "spans": spans, "images": [], "drawings": frame,
                           "links": []}, 10.909)
    holes = [e for e in page.classify()["elements"] if e["kind"] == "image" and e.get("anchor")]
    assert len(holes) == 1, "the frame and its formula became one picture over a hole in the line"
    assert holes[0]["bbox"][2] >= 146.35, "the frame is in the hole's picture"


# real_beamer-monodromy s12: "surjective. Hence <\boxed{H ↦ (H\Y → X) ↦ Aut(Y | H\Y) ≅ H}>." - the
# box is wider than half the page, and the line above has words from its left edge to its right.
BOXED_LINE: list[tuple[str, str, list[float]]] = [
    ("is even,", "LinBiolinumT", [50.16, 133.45, 83.09, 144.36]),
    (" \U0001d711", "LibertineMathMI", [83.09, 130.84, 91.64, 141.74]),
    ("is injective. By “Maps into covering spaces” it is also", "LinBiolinumT", [94.99, 133.45, 329.39, 144.36]),
    ("surjective. Hence", "LinBiolinumT", [50.16, 148.13, 127.16, 159.04]),
    ("H", "LinLibertineTI", [133.27, 148.0, 140.61, 158.91]), ("↦→", "txsys", [144.78, 147.19, 155.95, 158.09]),
    ("(", "txsys", [159.53, 147.19, 163.16, 158.09]), ("H", "LinLibertineTI", [163.6, 148.0, 170.94, 158.91]),
    ("\\", "txsys", [172.07, 147.19, 177.09, 158.09]), ("Y", "LinLibertineTI", [177.09, 148.0, 183.11, 158.91]),
    ("→", "txsys", [187.36, 147.19, 198.53, 158.09]), (" X", "LinLibertineTI", [198.53, 148.0, 208.41, 158.91]),
    (")", "txsys", [209.55, 147.19, 213.18, 158.09]), ("↦→", "txsys", [216.76, 147.19, 227.93, 158.09]),
    (" Aut", "LinLibertineT", [227.93, 147.98, 247.41, 158.89]), ("(", "txsys", [247.96, 147.19, 251.59, 158.09]),
    ("Y", "LinLibertineTI", [252.03, 148.0, 258.05, 158.91]), ("|", "txsys", [262.73, 147.19, 264.91, 158.09]),
    ("H", "LinLibertineTI", [268.38, 148.0, 275.72, 158.91]), ("\\", "txsys", [276.86, 147.19, 281.87, 158.09]),
    ("Y", "LinLibertineTI", [281.87, 148.0, 287.9, 158.91]), (")", "txsys", [289.11, 147.19, 292.74, 158.09]),
    ("≅", "txsyc", [296.65, 145.25, 303.58, 156.16]), ("H", "LinLibertineTI", [306.94, 148.0, 314.29, 158.91]),
    (".", "LinBiolinumT", [318.8, 148.13, 321.2, 159.04])]


def test_a_boxed_formula_wider_than_half_the_page_keeps_its_box() -> None:
    """The box's top and bottom edges are no theme hairlines across half the page, and a box
    hugging its formula is its frame however wide: the formula and its whole box are one picture
    over a hole. (Its top edge stayed in the background, its sides went elsewhere, one into a
    figure with a glyph of the line above, and the formula moved with its words without them.)"""
    spans: list[RawSpan] = []
    for i, (text, font, box) in enumerate(BOXED_LINE):
        baseline = 141.48 if box[1] < 145 else 156.16
        spans.append({"id": f"p0s{i}", "text": text, "font": font, "size": 10.909, "color": "#000000", "alpha": 255,
                      "origin": [box[0], baseline], "bbox": list(box), "dir": [1.0, 0.0], "smallcaps": False})
    frame = [rule(0, 129.88, 145.26, 318.8, 145.26), rule(1, 130.08, 145.26, 130.08, 161.5),
             rule(2, 318.61, 145.26, 318.61, 161.5), rule(3, 129.88, 161.5, 318.8, 161.5)]
    page = PageClassifier({"index": 0, "label": "1", "size": list(PAGE), "spans": spans, "images": [], "drawings": frame,
                           "links": []}, 10.909)
    elements = page.classify()["elements"]
    holes = [e for e in elements if e["kind"] == "image" and e.get("anchor")]
    assert len(holes) == 1, f"one hole picture, found {[e['id'] for e in elements]}"
    x0, y0, x1, y1 = holes[0]["bbox"]
    assert x0 <= 129.88 and y0 <= 145.26 and x1 >= 318.8 and y1 >= 161.5, f"the whole box is the hole's: {holes[0]['bbox']}"
    assert not [e for e in elements if e["kind"] == "image" and e.get("role") == "figure"], "no side of it is a figure"
