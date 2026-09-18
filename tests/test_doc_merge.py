"""Offline tests for the Docs three-way merge and the edits it plans.

No Google calls. The live side is built by `live()`, which lays blocks out in the
API's index space exactly as `documents.get` reports them: one unit per UTF-16
code unit, one for each chip however long its words look, and one for the
paragraph mark at the end of every block.
"""

from beamer2slides import doc_ir, doc_merge


def live(blocks: list[dict], start: int = 1) -> dict:
    """An IR as if read back from a document: spans, widths and keys in place."""
    out = []
    for block in blocks:
        block = {k: (list(v) if isinstance(v, list) else v) for k, v in block.items()}
        if block.get("kind") == "table":
            # A table opens two index units before its first cell's first paragraph.
            table_start, start = start, start + 2
            rows = []
            for row in block["rows"]:
                cells = []
                for cell in row:
                    laid = live(cell, start)["blocks"]
                    start = laid[-1]["span"][1] if laid else start
                    cells.append(laid)
                rows.append(cells)
            block["rows"] = rows
            block["span"] = [table_start, start + 1]
            start = block["span"][1]
            out.append(block)
            continue
        runs, width = [], 0
        for run in block.get("runs", []):
            run = dict(run)
            run["width"] = 1 if run.get("frozen") else doc_ir.utf16_len(run["text"])
            width += run["width"]
            runs.append(run)
        block["runs"] = runs
        block["span"] = [start, start + width + 1]
        start = block["span"][1]
        out.append(block)
    return {"title": "t", "blocks": out}


def para(key: str, text: str, **rest) -> dict:
    return {"kind": "paragraph", "key": key, "runs": [{"text": text}], **rest}


def texts(result: dict) -> list[str]:
    return [doc_merge.block_text(b) for b in result["blocks"]]


BASE = live([para("p:alpha", "alpha one two"), para("p:bravo", "bravo three four"),
             para("p:charlie", "charlie five six")])


def test_nothing_changed_writes_nothing():
    result = doc_merge.plan(BASE, live(BASE["blocks"]), live(BASE["blocks"]))
    assert result["requests"] == [] and result["conflicts"] == []


def test_a_document_edit_survives_an_untouched_source():
    theirs = live([para("p:alpha", "alpha one two"), para("p:bravo", "bravo THREE four"),
                   para("p:charlie", "charlie five six")])
    result = doc_merge.plan(BASE, live(BASE["blocks"]), theirs)
    assert texts(result)[1] == "bravo THREE four"
    assert result["requests"] == []  # the document already says it


def test_a_source_edit_is_written_to_the_document():
    ours = live([para("p:alpha", "alpha one two"), para("p:bravo", "bravo three FOUR"),
                 para("p:charlie", "charlie five six")])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    assert texts(result)[1] == "bravo three FOUR"
    assert [r for r in result["requests"] if "insertText" in r][0]["insertText"]["text"] == "FOUR"


def test_a_rewritten_word_inherits_the_style_of_the_words_it_replaces():
    """Docs styles inserted text like the character in front of it, so the new words
    go in at the end of the hunk — inside the bold run — and the old ones leave after."""
    base = live([{"kind": "paragraph", "key": "p:styled", "runs": [
        {"text": "the "}, {"text": "canonical file", "bold": True}]}])
    ours = live([{"kind": "paragraph", "key": "p:styled", "runs": [
        {"text": "the "}, {"text": "canonic file", "bold": True}]}])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    kinds = [next(iter(r)) for r in result["requests"]]
    assert kinds == ["insertText", "deleteContentRange"]
    # "canonical" sits at 5..14; the replacement goes in at 14, after its last letter.
    assert result["requests"][0]["insertText"]["location"]["index"] == 14
    assert result["requests"][1]["deleteContentRange"]["range"]["startIndex"] == 5


def test_both_sides_editing_different_words_merge():
    ours = live([para("p:alpha", "alpha ONE two"), para("p:bravo", "bravo three four"),
                 para("p:charlie", "charlie five six")])
    theirs = live([para("p:alpha", "alpha one TWO"), para("p:bravo", "bravo three four"),
                   para("p:charlie", "charlie five six")])
    result = doc_merge.plan(BASE, ours, theirs)
    assert texts(result)[0] == "alpha ONE TWO"
    assert result["conflicts"] == []


def test_both_sides_editing_the_same_words_conflict_and_the_document_wins():
    ours = live([para("p:alpha", "alpha SOURCE two")] + BASE["blocks"][1:])
    theirs = live([para("p:alpha", "alpha DOCUMENT two")] + BASE["blocks"][1:])
    result = doc_merge.plan(BASE, ours, theirs)
    assert texts(result)[0] == "alpha DOCUMENT two"
    assert result["conflicts"][0]["key"] == "p:alpha"
    assert result["conflicts"][0]["ours"] == "SOURCE"
    assert result["conflicts"][0]["theirs"] == "DOCUMENT"


# ---------------------------------------------------------------- frozen runs

CHIP = {"chip": "date", "frozen": True, "text": "Sep 25, 2026",
        "value": "2026-09-25T12:00:00Z"}


def chipped(before: str, after: str) -> dict:
    return {"kind": "paragraph", "key": "p:due",
            "runs": [{"text": before}, dict(CHIP), {"text": after}]}


def test_words_around_a_chip_are_edited_and_the_chip_is_not():
    base = live([chipped("due ", " please")])
    ours = live([chipped("due ", " PLEASE")])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    assert doc_merge.frozen_of(result["blocks"][0]) == doc_merge.frozen_of(base["blocks"][0])
    edits = result["requests"]
    assert [r["insertText"]["text"] for r in edits if "insertText" in r] == ["PLEASE"]
    # The chip sits at index 5 and holds one unit: no edit may touch it.
    for request in edits:
        if "deleteContentRange" in request:
            span = request["deleteContentRange"]["range"]
            assert not (span["startIndex"] <= 5 < span["endIndex"])


def test_index_arithmetic_counts_a_chip_as_one_unit_not_its_words():
    """The chip shows twelve characters and occupies one index; edits after it
    would land twelve units late if the reader believed the display text."""
    base = live([chipped("due ", " soon")])
    ours = live([chipped("due ", " later")])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    insert = next(r["insertText"] for r in result["requests"] if "insertText" in r)
    # 1 (body start) + 4 ("due ") puts the chip at 5, the space at 6 and "soon" at
    # 7..11, so the new word goes in at the end of what it replaces: 11. Believing
    # the chip's display text would have put it at 22.
    assert insert["location"]["index"] == 11
    assert insert["text"] == "later"
    assert result["requests"][1]["deleteContentRange"]["range"] == {"startIndex": 7,
                                                                   "endIndex": 11}


def test_a_source_that_would_rewrite_a_chip_is_refused():
    base = live([chipped("due ", " please")])
    ours = live([{"kind": "paragraph", "key": "p:due",
                  "runs": [{"text": "due tomorrow please"}]}])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    assert result["requests"] == []
    assert "left alone" in result["notes"][0]
    assert doc_merge.frozen_of(result["blocks"][0]) == (("date", "Sep 25, 2026",
                                                        "2026-09-25T12:00:00Z"),)


# ---------------------------------------------------------------- styling

def styled(key: str, *runs: dict) -> dict:
    return {"kind": "paragraph", "key": key, "runs": [dict(r) for r in runs]}


def test_a_mark_the_source_added_is_written_to_the_document():
    base = live([styled("p:s", {"text": "one two three"})])
    ours = live([styled("p:s", {"text": "one "}, {"text": "two", "bold": True},
                        {"text": " three"})])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    styles = [r["updateTextStyle"] for r in result["requests"] if "updateTextStyle" in r]
    assert [s["range"] for s in styles] == [{"startIndex": 1, "endIndex": 5},
                                            {"startIndex": 5, "endIndex": 8},
                                            {"startIndex": 8, "endIndex": 14}]
    assert styles[1]["textStyle"] == {"bold": True}
    # No words changed, so nothing is inserted or deleted.
    assert not [r for r in result["requests"] if "insertText" in r or "deleteContentRange" in r]


def test_a_mark_the_source_took_away_is_named_so_it_goes_away():
    base = live([styled("p:s", {"text": "one "}, {"text": "two", "bold": True})])
    ours = live([styled("p:s", {"text": "one two"})])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    style = [r["updateTextStyle"] for r in result["requests"] if "updateTextStyle" in r][0]
    assert style["textStyle"] == {}
    # The fields are named although the run carries none of them: that is what clears
    # the bold. The font is not among them — it is not this merge's to reset.
    assert style["fields"] == ",".join(doc_merge.MANAGED)
    assert "weightedFontFamily" not in style["fields"]


def test_styling_a_second_sync_writes_nothing():
    base = live([styled("p:s", {"text": "one two three"})])
    ours = live([styled("p:s", {"text": "one "}, {"text": "two", "bold": True},
                        {"text": " three"})])
    # The document now says what the source says: the same run layout, read back.
    theirs = live([styled("p:s", {"text": "one "}, {"text": "two", "bold": True},
                          {"text": " three"})])
    assert doc_merge.plan(theirs, ours, theirs)["requests"] == []


def test_the_document_keeps_its_styling_when_it_rewrote_the_words():
    base = live([styled("p:s", {"text": "one two three"})])
    ours = live([styled("p:s", {"text": "one "}, {"text": "two", "bold": True},
                        {"text": " three"})])
    theirs = live([styled("p:s", {"text": "one two THREE"})])
    result = doc_merge.plan(base, ours, theirs)
    assert not [r for r in result["requests"] if "updateTextStyle" in r]
    assert "styling left alone" in result["notes"][0]


def test_a_paragraph_the_source_turned_into_a_heading_is_written():
    base = live([para("p:h", "a heading")])
    ours = live([{"kind": "heading", "level": 2, "key": "p:h", "runs": [{"text": "a heading"}]}])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    style = [r["updateParagraphStyle"] for r in result["requests"]][0]
    assert style["paragraphStyle"]["namedStyleType"] == "HEADING_2"
    assert style["range"] == {"startIndex": 1, "endIndex": 11}


def test_a_list_item_the_source_made_a_paragraph_loses_its_bullet():
    base = live([{"kind": "item", "level": 0, "ordered": False, "key": "i:x",
                  "runs": [{"text": "an item"}]}])
    ours = live([para("i:x", "an item")])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    kinds = [next(iter(r)) for r in result["requests"]]
    assert kinds == ["deleteParagraphBullets", "updateParagraphStyle"]


def test_a_paragraph_the_source_made_a_list_item_gets_its_bullet_last():
    base = live([para("i:x", "an item")])
    ours = live([{"kind": "item", "level": 0, "ordered": True, "key": "i:x",
                  "runs": [{"text": "an item", "bold": True}]}])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    kinds = [next(iter(r)) for r in result["requests"]]
    assert kinds == ["updateParagraphStyle", "updateTextStyle", "createParagraphBullets"]


# ---------------------------------------------------------------- tables

def table(key: str, rows: list[list[str]]) -> dict:
    return {"kind": "table", "key": key, "rows": [
        [[{"kind": "paragraph", "runs": [{"text": text}]}] for text in row] for row in rows]}


def cell_text(block: dict, row: int, cell: int) -> str:
    return doc_merge.block_text(block["rows"][row][cell][0])


GRID = live([para("p:before", "before the table"),
             table("t:grid", [["a one", "b one"], ["a two", "b two"]]),
             para("p:after", "after the table")])


def test_a_cell_the_source_edited_is_written():
    ours = live([b for b in GRID["blocks"]])
    ours["blocks"][1]["rows"][0][1][0]["runs"] = [{"text": "b ONE", "width": 5}]
    result = doc_merge.plan(GRID, ours, live(GRID["blocks"]))
    merged = result["blocks"][1]
    assert cell_text(merged, 0, 1) == "b ONE"
    insert = [r["insertText"] for r in result["requests"] if "insertText" in r]
    assert [i["text"] for i in insert] == ["ONE"]
    # The edit lands inside that cell, not at the table's own index.
    assert insert[0]["location"]["index"] == GRID["blocks"][1]["rows"][0][1][0]["span"][1] - 1


def test_two_cells_of_one_table_edited_on_both_sides_merge():
    ours = live([b for b in GRID["blocks"]])
    ours["blocks"][1]["rows"][0][0][0]["runs"] = [{"text": "a ONE", "width": 5}]
    theirs = live([b for b in GRID["blocks"]])
    theirs["blocks"][1]["rows"][1][1][0]["runs"] = [{"text": "b TWO", "width": 5}]
    result = doc_merge.plan(GRID, ours, theirs)
    merged = result["blocks"][1]
    assert cell_text(merged, 0, 0) == "a ONE" and cell_text(merged, 1, 1) == "b TWO"
    assert result["conflicts"] == []


def test_the_same_cell_edited_on_both_sides_conflicts_and_the_document_wins():
    ours = live([b for b in GRID["blocks"]])
    ours["blocks"][1]["rows"][0][0][0]["runs"] = [{"text": "a SOURCE", "width": 8}]
    theirs = live([b for b in GRID["blocks"]])
    theirs["blocks"][1]["rows"][0][0][0]["runs"] = [{"text": "a DOCUMENT", "width": 10}]
    result = doc_merge.plan(GRID, ours, theirs)
    assert cell_text(result["blocks"][1], 0, 0) == "a DOCUMENT"
    assert result["conflicts"][0]["theirs"] == "DOCUMENT"
    assert result["requests"] == []


def test_a_table_whose_grid_the_source_changed_is_left_alone():
    ours = live([GRID["blocks"][0], table("t:grid", [["a one", "b one", "c one"],
                                                     ["a two", "b two", "c two"]]),
                 GRID["blocks"][2]])
    result = doc_merge.plan(GRID, ours, live(GRID["blocks"]))
    assert result["requests"] == []
    assert "rows and columns" in result["notes"][0]


def test_a_table_the_source_added_is_reported_not_written():
    ours = live([GRID["blocks"][0], GRID["blocks"][1],
                 table("t:new", [["x"]]), GRID["blocks"][2]])
    result = doc_merge.plan(GRID, ours, live(GRID["blocks"]))
    assert result["requests"] == []
    assert "cannot be written" in result["notes"][0]


def test_a_new_block_carrying_a_chip_is_reported_not_written():
    ours = live([BASE["blocks"][0],
                 {"kind": "paragraph", "key": "p:chip", "runs": [{"text": "due "}, dict(CHIP)]}]
                + BASE["blocks"][1:])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    assert result["requests"] == []
    assert "no import can create one" in result["notes"][0]


# ---------------------------------------------------------------- add, delete, order

def test_a_block_the_source_added_is_inserted_with_its_styling():
    ours = live(BASE["blocks"][:1]
                + [{"kind": "heading", "level": 2, "key": "heading:new", "runs": [
                    {"text": "New "}, {"text": "heading", "bold": True}]}]
                + BASE["blocks"][1:])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    assert texts(result)[1] == "New heading"
    kinds = [next(iter(r)) for r in result["requests"]]
    assert kinds == ["insertText", "deleteParagraphBullets", "updateParagraphStyle",
                     "updateTextStyle"]
    insert = result["requests"][0]["insertText"]
    assert insert["text"] == "New heading\n"
    assert insert["location"]["index"] == BASE["blocks"][1]["span"][0]
    assert (result["requests"][2]["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"]
            == "HEADING_2")


def test_a_list_item_gets_its_bullets_after_its_styling():
    ours = live(BASE["blocks"] + [{"kind": "item", "level": 0, "ordered": True,
                                   "key": "item:step", "runs": [{"text": "step one"}]}])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    kinds = [next(iter(r)) for r in result["requests"]]
    assert kinds == ["insertText", "updateParagraphStyle", "createParagraphBullets"]
    # A block that is not a list item says so, or it joins the list it was written into.
    assert "deleteParagraphBullets" not in kinds
    assert (result["requests"][-1]["createParagraphBullets"]["bulletPreset"]
            == "NUMBERED_DECIMAL_ALPHA_ROMAN")


def test_a_block_appended_at_the_end_puts_its_paragraph_break_first():
    ours = live(BASE["blocks"] + [para("p:delta", "delta seven")])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    tail = BASE["blocks"][-1]["span"][1] - 1
    kinds = [next(iter(r)) for r in result["requests"]]
    assert kinds == ["insertText", "deleteParagraphBullets", "updateParagraphStyle"]
    insert = result["requests"][0]["insertText"]
    # The body's last newline cannot be written past, so the break goes in before
    # the words: "delta seven\n" at `tail` would join the last paragraph instead and
    # leave an empty one behind it.
    assert insert == {"location": {"index": tail}, "text": "\ndelta seven"}
    assert (result["requests"][2]["updateParagraphStyle"]["range"]
            == {"startIndex": tail + 1, "endIndex": tail + 1 + len("delta seven") + 1})


def test_two_blocks_appended_at_the_end_keep_their_order():
    ours = live(BASE["blocks"] + [para("p:delta", "delta seven"), para("p:echo", "echo eight")])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    inserts = [r["insertText"] for r in result["requests"] if "insertText" in r]
    # Both go in at the one index, and what is written last ends up in front of what
    # was written before it, so the later block is written first.
    assert [i["text"] for i in inserts] == ["\necho eight", "\ndelta seven"]
    assert {i["location"]["index"] for i in inserts} == {BASE["blocks"][-1]["span"][1] - 1}


def test_two_blocks_added_before_one_block_keep_their_order():
    ours = live(BASE["blocks"][:1] + [para("p:delta", "delta"), para("p:echo", "echo")]
                + BASE["blocks"][1:])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    inserts = [r["insertText"] for r in result["requests"] if "insertText" in r]
    assert [i["text"] for i in inserts] == ["echo\n", "delta\n"]
    assert {i["location"]["index"] for i in inserts} == {BASE["blocks"][1]["span"][0]}


def test_an_append_is_written_before_the_edits_of_the_paragraph_it_follows():
    ours = live(BASE["blocks"][:2] + [para("p:charlie", "charlie five SIX"),
                                      para("p:delta", "delta seven")])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    tail = BASE["blocks"][-1]["span"][1] - 1
    # The edit ends where the appended block begins. Written the other way round, the
    # index the append was planned at would be inside the rewritten word.
    assert result["requests"][0]["insertText"] == {"location": {"index": tail},
                                                   "text": "\ndelta seven"}
    assert [r["insertText"]["text"] for r in result["requests"] if "insertText" in r][1] == "SIX"


def test_a_block_appended_while_the_last_one_goes_lands_after_the_delete():
    ours = live(BASE["blocks"][:2] + [para("p:delta", "delta seven")])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    # The deleted block is further down the document, so it goes first and leaves the
    # paragraph mark the append was planned at — the second last one — where it was.
    assert next(iter(result["requests"][0])) == "deleteContentRange"
    assert result["requests"][1]["insertText"] == {
        "location": {"index": BASE["blocks"][1]["span"][1] - 1}, "text": "\ndelta seven"}


def test_a_block_the_source_deleted_is_deleted_in_the_document():
    ours = live([BASE["blocks"][0], BASE["blocks"][2]])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    assert texts(result) == ["alpha one two", "charlie five six"]
    assert result["requests"] == [{"deleteContentRange": {"range": {
        "startIndex": BASE["blocks"][1]["span"][0],
        "endIndex": BASE["blocks"][1]["span"][1]}}}]


def test_a_source_delete_loses_to_a_document_edit():
    ours = live([BASE["blocks"][0], BASE["blocks"][2]])
    theirs = live([para("p:alpha", "alpha one two"), para("p:bravo", "bravo THREE four"),
                   para("p:charlie", "charlie five six")])
    result = doc_merge.plan(BASE, ours, theirs)
    assert texts(result)[1] == "bravo THREE four"
    assert result["requests"] == []
    assert "kept" in result["notes"][0]


def test_a_block_deleted_in_the_document_stays_deleted():
    theirs = live([BASE["blocks"][0], BASE["blocks"][2]])
    result = doc_merge.plan(BASE, live(BASE["blocks"]), theirs)
    assert texts(result) == ["alpha one two", "charlie five six"]
    assert result["requests"] == []


def test_a_block_added_in_the_document_is_left_where_it_is():
    theirs = live(BASE["blocks"][:1] + [{"kind": "paragraph", "runs": [{"text": "typed here"}]}]
                  + BASE["blocks"][1:])
    result = doc_merge.plan(BASE, live(BASE["blocks"]), theirs)
    assert texts(result)[1] == "typed here"
    assert result["requests"] == []


def test_edits_are_ordered_back_to_front_so_indices_stay_valid():
    ours = live([para("p:alpha", "alpha ONE two"), para("p:bravo", "bravo THREE four"),
                 para("p:charlie", "charlie FIVE six")])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    touched = [next(iter(r.values()))["range"]["startIndex"] if "deleteContentRange" in r
               else r["insertText"]["location"]["index"] for r in result["requests"]]
    assert touched == sorted(touched, reverse=True)


def test_the_file_says_what_an_imported_list_cannot():
    """A read comes back `ordered: None`; the file is then the only authority."""
    ours = live([{"kind": "item", "level": 0, "ordered": True, "key": "item:one",
                  "runs": [{"text": "one"}]}])
    theirs = live([{"kind": "item", "level": 0, "ordered": None, "key": "item:one",
                    "runs": [{"text": "one"}]}])
    doc_merge.restore_unreadable(theirs, ours)
    assert theirs["blocks"][0]["ordered"] is True
    # A list item nobody knows falls back to bullets rather than staying unknown.
    stray = live([{"kind": "item", "level": 0, "ordered": None, "runs": [{"text": "x"}]}])
    doc_merge.restore_unreadable(stray, ours)
    assert stray["blocks"][0]["ordered"] is False


def test_a_list_item_keeps_its_key_although_the_document_forgot_the_glyphs():
    base = live([{"kind": "item", "level": 0, "ordered": None, "key": "item:one",
                  "runs": [{"text": "one"}]}])
    ours = {"title": "t", "blocks": [
        {"kind": "item", "level": 0, "ordered": True, "runs": [{"text": "one"}]}]}
    doc_merge.inherit_keys(base, ours)
    assert ours["blocks"][0]["key"] == "item:one"


def test_an_insert_is_sent_after_the_edits_of_the_block_it_pushes_down():
    """Both land on the same index: the edits were measured before the insert."""
    ours = live([BASE["blocks"][0], para("p:new", "brand new line"),
                 para("p:bravo", "bravo three FOUR"), BASE["blocks"][2]])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    where = {r["insertText"]["text"]: i for i, r in enumerate(result["requests"])
             if "insertText" in r}
    assert where["FOUR"] < where["brand new line\n"]
    assert (result["requests"][where["brand new line\n"]]["insertText"]["location"]["index"]
            == BASE["blocks"][1]["span"][0])


def test_keys_are_inherited_when_the_words_changed():
    ours = {"title": "t", "blocks": [
        {"kind": "paragraph", "runs": [{"text": "alpha one two"}]},
        {"kind": "paragraph", "runs": [{"text": "bravo three four but rewritten a lot"}]},
        {"kind": "paragraph", "runs": [{"text": "charlie five six"}]}]}
    doc_merge.inherit_keys(BASE, ours)
    assert [b["key"] for b in ours["blocks"]] == ["p:alpha", "p:bravo", "p:charlie"]
