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


def test_a_block_grows_with_its_shadow_as_far_as_its_words_run_longer_in_slides():
    plan = emit.plan_offline(deck("28_frames_code"))["plan"]
    slide = plan.deck["slides"][5]
    grown = emit.grown_panels(slide, plan.scale, plan.fonts)
    before = {e["id"]: e for e in slide["elements"]}
    after = {e["id"]: e for e in grown["elements"]}
    block = [i for i, e in before.items() if e["kind"] == "shape" and e.get("block") == 0]
    shadow = [i for i, e in before.items() if e["kind"] == "shape" and e.get("block") is None]
    growth = {after[i]["bbox"][2] - before[i]["bbox"][2] for i in block}
    assert len(growth) == 1 and min(growth) > 1.0  # (Roboto Mono runs longer than LM Mono here)
    assert all(after[i]["bbox"][2] > before[i]["bbox"][2] for i in shadow)
    assert all(after[i]["bbox"][2] <= slide["size"][0] - 1 + 1e-6 for i in block + shadow)
    assert all(after[i] is before[i] for i in before if i not in block + shadow)
