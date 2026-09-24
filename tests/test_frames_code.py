"""Frames and code listings (tests/decks/28_frames_code.tex): a box's frame stays its outline or
its rules, and code keeps its columns in one text box. Local only, no Google API."""

from beamer2slides import emit

from .test_classify import deck, paragraph_text, texts


def shapes(slide: dict, role: str) -> list[dict]:
    return [e for e in slide["elements"] if e["kind"] == "shape" and e.get("role") == role]


def body(slide: dict) -> list[list[str]]:
    return [[paragraph_text(p) for p in e["paragraphs"]] for e in texts(slide) if e.get("role") == "body"]


def inside(el: dict, box: list[float]) -> bool:
    x0, y0, x1, y1 = el["bbox"]
    return box[0] <= x0 and box[1] <= y0 and x1 <= box[2] and y1 <= box[3]


def test_a_framed_box_keeps_its_frame_as_the_outline_of_its_panel():
    slide = deck("28_frames_code")["slides"][0]
    panels = {p["fill"]: p for p in shapes(slide, "panel")}
    assert panels["#e6e6ff"]["outline"] == {"color": "#0000ff", "width": 0.4}  # \fcolorbox
    assert panels["#fff2f2"]["outline"]["color"] == "#990000"  # tcolorbox
    assert abs(panels["#fff2f2"]["outline"]["width"] - 1.0) < 0.05
    assert not [e for e in slide["elements"] if e["kind"] in ("diagram", "image", "table")]
    for panel in panels.values():  # (one text box on each, inside its frame)
        assert len([t for t in texts(slide) if inside(t, panel["bbox"])]) == 1


def test_a_numbered_listing_is_one_box_in_its_columns_beside_its_numbers():
    slide = deck("28_frames_code")["slides"][1]
    [panel] = shapes(slide, "panel")
    assert panel["fill"] == "#f5f5f5" and panel["outline"]["color"] == "#999999"
    numbers, code = body(slide)
    assert numbers == ["1", "2", "3", "4", "5", "6"]
    assert code == ["static long memo[N + 1];   /* zero */", "long fib(int n) {", "    if (n < 2) return n;",
                    "    return memo[n] = fib(n - 1) + fib(n - 2);", "}"]


def test_a_line_whose_longest_span_opens_with_a_lowered_star_keeps_its_baseline():
    # (listings lowers '*' and raises '_': "*)NULL);" and "_exit(127);" put r2_code_v3 s2's
    # lines 6 and 7 2 pt off and Slides set them almost touching)
    from beamer2slides.classify import Line
    from .test_columns import span
    mono = "BeraSansMono-Roman"
    words = [span("execlp(", 48.0, 125.13, 8.47, font=mono), span('"ls"', 84.0, 125.13, 8.47, font=mono),
             span("char", 180.8, 125.13, 8.47, font=mono), span("*)NULL);", 206.3, 126.82, 8.47, font=mono)]
    assert Line(words).baseline == 125.13
    assert Line([span("_exit(127);", 48.0, 135.08, 8.47, font=mono), span("/*", 180.8, 137.08, 8.47, font=mono),
                 span("only", 196.0, 137.08, 8.47, font=mono), span("reached", 221.6, 137.08, 8.47, font=mono),
                 span("failed", 303.1, 137.08, 8.47, font=mono)]).baseline == 137.08


def test_a_repl_line_is_code_not_a_formula():
    slide = deck("28_frames_code")["slides"][9]
    assert not [e for e in slide["elements"] if e["kind"] == "image"]
    [code] = [e for e in texts(slide) if e.get("code")]
    assert [paragraph_text(p) for p in code["paragraphs"]] == [">>> a + b", "Money(12, ’EUR’)"]  # (no upquote)


def test_a_cell_s_space_before_code_words_is_the_prose_font_s_when_the_code_span_brings_it():
    # (Inconsolata's ' __exit__' comes out of the PDF with its space: 'paired with  __exit__')
    from beamer2slides.classify import span_runs
    from .test_columns import span
    runs = span_runs([span("paired with", 226.2, 165.0, 9.96, font="LMSans10-Regular"),
                      span(" __exit__", 272.3, 165.0, 9.96, font="Inconsolatazi4-Regular")])
    assert [(r["text"], r["family"]) for r in runs] == [("paired with ", "sans"), ("__exit__", "mono")]


def test_rules_above_and_below_a_listing_are_rule_shapes():
    slide = deck("28_frames_code")["slides"][2]
    rules = shapes(slide, "rule")
    assert len(rules) == 2 and all(r["fill"] == "#999999" for r in rules)
    assert not [e for e in slide["elements"] if e["kind"] in ("diagram", "image")]
    assert body(slide) == [["int main(void) {", '    printf("%d\\n", fib(N));', "    return 0;", "}"]]


def test_a_frame_with_no_fill_is_four_rules_round_one_code_box():
    slide = deck("28_frames_code")["slides"][3]
    assert len(shapes(slide, "rule")) == 4 and not shapes(slide, "panel")
    assert body(slide) == [["def discount(price, percent):", "    if percent  < 0:",
                            "        raise ValueError(percent)", "    return price * (1 - percent / 100)"]]


def test_a_labelled_verbatim_frame_leaves_its_label_a_gap_and_its_coloured_lines_are_text_once():
    slide = deck("28_frames_code")["slides"][4]
    rules = shapes(slide, "rule")
    [label, code] = [e for e in texts(slide) if e.get("role") == "body"]
    assert paragraph_text(label["paragraphs"][0]) == "money.py"
    top = sorted((r for r in rules if r["bbox"][3] - r["bbox"][1] < 1 and r["bbox"][1] < label["bbox"][3]),
                 key=lambda r: r["bbox"][0])
    assert len(top) == 2 and top[0]["bbox"][2] < label["bbox"][0] and label["bbox"][2] < top[1]["bbox"][0]
    assert len(rules) == 5
    assert not [e for e in slide["elements"] if e["kind"] in ("diagram", "image")]
    assert [paragraph_text(p) for p in code["paragraphs"]] == [
        "     def add(self, other):", "-        return Money(self.amount)",
        "+        return Money(self.amount, self.currency)"]


def test_code_on_a_block_is_one_box_across_its_blank_line():
    slide = deck("28_frames_code")["slides"][5]
    assert body(slide)[1] == ["def fib(n):", "    if n < 2:", "        return n", "    return fib(n - 1) + fib(n - 2)"]


def test_the_space_before_code_words_in_prose_is_the_prose_font_s():
    slide = deck("28_frames_code")["slides"][6]
    [table] = [e for e in slide["elements"] if e["kind"] == "table"]
    runs = table["cells"][0][0]
    assert [(r["text"], r["family"]) for r in runs] == [("Call ", "sans"), ("fib(n)", "mono"), (" twice", "sans")]


def test_an_underline_strike_or_highlight_ends_before_the_punctuation_after_it():
    slide = deck("28_frames_code")["slides"][7]
    [text] = body(slide)
    runs = [r for e in texts(slide) if e.get("role") == "body" for p in e["paragraphs"] for r in p["runs"]]
    marked = [(r["text"], "u" if r["underline"] else "s" if r["strike"] else "h" if r["highlight"] else "")
              for r in runs]
    assert [m for m in marked if m[1]] == [("matches", "u"), ("about 30 hours", "u"), ("significantly", "s"),
                                           ("the single claim", "h"), ("under 4 hours on a single GPU", "u")]
    assert text == ["Accepted: matches, about 30 hours and significantly, then the single claim. "
                    "And under 4 hours on a single GPU."]


def test_a_colorbox_keeps_its_padding_in_a_line_and_is_a_panel_on_a_line_of_its_own():
    slide = deck("28_frames_code")["slides"][8]
    runs = [r for e in texts(slide) if e.get("role") == "body" for p in e["paragraphs"] for r in p["runs"]]
    [box] = [r for r in runs if r["highlight"]]
    assert box["text"] == " 94.2 % "  # (\fboxsep either side, highlighted)
    [panel] = shapes(slide, "panel")
    assert panel["fill"] == "#ebebeb"
    [note] = [t for t in texts(slide) if inside(t, panel["bbox"])]
    assert paragraph_text(note["paragraphs"][0]) == "Free-text answers from the end-of-term survey, n = 312"


def test_a_framed_panel_is_written_with_its_outline():
    plan = emit.plan_offline(deck("28_frames_code"))["plan"]
    slide = plan.deck["slides"][0]
    [panel] = [e for e in slide["elements"] if e.get("fill") == "#e6e6ff"]
    [update] = [r["updateShapeProperties"] for r in emit.shape_requests(panel, "s", "o", plan.scale)
                if "updateShapeProperties" in r]
    outline = update["shapeProperties"]["outline"]
    assert outline["propertyState"] == "RENDERED"
    assert outline["outlineFill"]["solidFill"]["color"]["rgbColor"] == {"red": 0.0, "green": 0.0, "blue": 1.0}
    assert "outline.weight" in update["fields"]


def test_a_block_whose_words_keep_room_in_slides_keeps_the_pdf_width():
    # ('Memoised' comes out 3 pt longer in Lato, 290 pt short of the bar's end: r2_themes_v5 s3,
    # r3_dense_v1 s10 grew every such block 5 pt)
    plan = emit.plan_offline(deck("28_frames_code"))["plan"]
    slide = plan.deck["slides"][5]
    grown = emit.grown_panels(slide, plan.scale, plan.fonts)
    assert all(a is b for a, b in zip(slide["elements"], grown["elements"]))


def narrowed_block(slide: dict, right: float) -> dict:
    """Slide 6 of 28_frames_code with its block (title bar, body, shadow) ending at `right` and a
    frame-coloured panel drawn round it first, 1.4 pt wider, as a tcolorbox draws its frame."""
    els = []
    for e in slide["elements"]:
        if e["kind"] == "shape":
            x1 = right + (4.0 if e.get("block") is None else 0.0)  # (the shadow lies 4 pt right)
            e = {**e, "bbox": [e["bbox"][0], e["bbox"][1], x1, e["bbox"][3]]}
            if e.get("title_bar"):
                e["title_bar"] = [*e["title_bar"][:2], right, e["title_bar"][3]]
        els.append(e)
    frame = {"id": "frame", "kind": "shape", "role": "panel", "bbox": [5.5, 86.0, right + 1.4, 183.0],
             "fill": "#990000", "shape": "RECTANGLE", "flip": False, "radius": 0.0, "spans": []}
    return {**slide, "elements": [frame, *els]}


def test_a_block_whose_words_reach_its_edge_in_slides_grows_with_its_shadow_and_frame():
    plan = emit.plan_offline(deck("28_frames_code"))["plan"]
    [title] = [e for e in plan.deck["slides"][5]["elements"] if e["kind"] == "text"
               and paragraph_text(e["paragraphs"][0]) == "Memoised"]
    slide = narrowed_block(plan.deck["slides"][5], title["bbox"][2] + 1.0)  # (1 pt from the bar's end)
    grown = emit.grown_panels(slide, plan.scale, plan.fonts)
    before = {e["id"]: e for e in slide["elements"]}
    after = {e["id"]: e for e in grown["elements"]}
    block = [i for i, e in before.items() if e["kind"] == "shape" and e.get("block") == 0]
    others = [i for i, e in before.items() if e["kind"] == "shape" and e.get("block") is None]  # (shadow, frame)
    growth = {round(after[i]["bbox"][2] - before[i]["bbox"][2], 6) for i in block + others}
    assert len(growth) == 1 and 1.0 < min(growth) < 6.0  # (Lato's 'Memoised' is ~3 pt longer)
    assert all(after[i]["bbox"][2] <= slide["size"][0] - 1 + 1e-6 for i in block + others)
    assert all(after[i] is before[i] for i in before if i not in block + others)
