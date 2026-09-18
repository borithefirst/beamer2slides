"""Offline tests for the Docs three-way merge and the edits it plans.

No Google calls. The live side is built by `live()`, which lays blocks out in the
API's index space exactly as `documents.get` reports them: one unit per UTF-16
code unit, one for each chip however long its words look, and one for the
paragraph mark at the end of every block.
"""

import pytest

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


def applied(ir: dict, requests: list[dict]) -> list[str]:
    """What the requests do to the document, run in order on its live indices.

    Google's side of the bargain, in ten lines: one character per index unit, index 1
    being the first of them, a paragraph mark after every block. Styling is left out —
    what this catches is arithmetic, which is where every index bug lives. Tables have
    an index space of their own and are not modelled here.
    """
    assert all(b["kind"] != "table" for b in ir["blocks"]), "no index model for tables here"
    text = "".join(doc_merge.block_text(b) + "\n" for b in ir["blocks"])
    for request in requests:
        if "insertText" in request:
            at = request["insertText"]["location"]["index"] - 1
            assert 0 <= at <= len(text), f"index {at + 1} is outside the document"
            text = text[:at] + request["insertText"]["text"] + text[at:]
        elif "deleteContentRange" in request:
            span = request["deleteContentRange"]["range"]
            # The body's last newline is its own: Google refuses a range that holds it.
            assert span["endIndex"] <= len(text), "delete of the body's final newline"
            text = text[:span["startIndex"] - 1] + text[span["endIndex"] - 1:]
    return text.split("\n")[:-1]


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


def test_a_block_deleted_in_front_of_a_table_gives_up_the_mark_before_it():
    """The API refuses a delete that takes the newline in front of a table with it,
    and a refused request throws out the whole batch. Measured on a live document:
    deleting the mark of the block *before* it instead leaves the table with a
    paragraph in front of it, and that paragraph keeps its own style."""
    was = live([para("p:title", "the title"), para("p:before", "before the table"),
                table("t:grid", [["a one"]]), para("p:after", "after the table")])
    ours = live([was["blocks"][0], was["blocks"][2], was["blocks"][3]])
    result = doc_merge.plan(was, ours, live(was["blocks"]))
    assert result["requests"] == [{"deleteContentRange": {"range": {
        "startIndex": was["blocks"][1]["span"][0] - 1,
        "endIndex": was["blocks"][1]["span"][1] - 1}}}]


def test_two_blocks_deleted_in_front_of_a_table_do_not_want_one_mark_twice():
    """The borrowing passes leftwards: adjacent ranges, none of them overlapping."""
    was = live([para("p:title", "the title"), para("p:one", "one"), para("p:two", "two"),
                table("t:grid", [["a one"]])])
    result = doc_merge.plan(was, live([was["blocks"][0], was["blocks"][3]]),
                            live(was["blocks"]))
    spans = [r["deleteContentRange"]["range"] for r in result["requests"]]
    assert spans == [{"startIndex": was["blocks"][2]["span"][0] - 1,
                      "endIndex": was["blocks"][2]["span"][1] - 1},
                     {"startIndex": was["blocks"][1]["span"][0] - 1,
                      "endIndex": was["blocks"][1]["span"][1] - 1}]
    assert spans[1]["endIndex"] == spans[0]["startIndex"]


def test_a_block_deleted_between_two_tables_leaves_its_paragraph_behind():
    """There is no mark to borrow — and Docs wants a paragraph between two tables
    anyway, so the words go and the empty paragraph stays."""
    was = live([table("t:one", [["a"]]), para("p:between", "between the tables"),
                table("t:two", [["b"]])])
    result = doc_merge.plan(was, live([was["blocks"][0], was["blocks"][2]]),
                            live(was["blocks"]))
    assert result["requests"] == [{"deleteContentRange": {"range": {
        "startIndex": was["blocks"][1]["span"][0],
        "endIndex": was["blocks"][1]["span"][1] - 1}}}]


def test_a_table_the_source_deleted_takes_the_newline_question_with_it():
    """The table goes first (the higher index), so by the time the paragraph in front
    of it is deleted there is no table in front of which a newline must stay."""
    was = live([para("p:title", "the title"), para("p:before", "before the table"),
                table("t:grid", [["a one"]]), para("p:after", "after the table")])
    result = doc_merge.plan(was, live([was["blocks"][0], was["blocks"][3]]),
                            live(was["blocks"]))
    spans = [r["deleteContentRange"]["range"] for r in result["requests"]]
    assert spans == [{"startIndex": was["blocks"][2]["span"][0],
                      "endIndex": was["blocks"][2]["span"][1]},
                     {"startIndex": was["blocks"][1]["span"][0],
                      "endIndex": was["blocks"][1]["span"][1]}]


def regrid(rows: list[list[str]]) -> dict:
    """The plan for a source that gave the GRID table these rows and columns."""
    ours = live([GRID["blocks"][0], table("t:grid", rows), GRID["blocks"][2]])
    return doc_merge.plan(GRID, ours, live(GRID["blocks"]))


def test_a_column_the_source_added_is_written_before_the_words():
    result = regrid([["a one", "b one", "c one"], ["a two", "b two", "c two"]])
    assert result["structure"] == [{"insertTableColumn": {
        "tableCellLocation": {"tableStartLocation": {"index": GRID["blocks"][1]["span"][0]},
                              "rowIndex": 0, "columnIndex": 1}, "insertRight": True}}]
    # The words come on the pass after this one, against the grid the document
    # will then have: there is no index here for a cell that does not exist yet.
    assert result["requests"] == []
    assert result["shaped"][0]["note"] == "`t:grid`: inserts a column — the grid the source has"


def test_a_row_the_source_added_in_the_middle_goes_in_the_middle():
    result = regrid([["a one", "b one"], ["a new", "b new"], ["a two", "b two"]])
    assert result["structure"] == [{"insertTableRow": {
        "tableCellLocation": {"tableStartLocation": {"index": GRID["blocks"][1]["span"][0]},
                              "rowIndex": 0, "columnIndex": 0}, "insertBelow": True}}]


def test_a_row_the_source_added_at_the_top_is_written_above_the_first():
    result = regrid([["a new", "b new"], ["a one", "b one"], ["a two", "b two"]])
    assert result["structure"][0]["insertTableRow"]["insertBelow"] is False
    assert result["structure"][0]["insertTableRow"]["tableCellLocation"]["rowIndex"] == 0


def test_a_row_the_source_deleted_is_deleted():
    result = regrid([["a one", "b one"]])
    assert result["structure"] == [{"deleteTableRow": {"tableCellLocation": {
        "tableStartLocation": {"index": GRID["blocks"][1]["span"][0]},
        "rowIndex": 1, "columnIndex": 0}}}]


def test_a_row_the_source_rewrote_is_not_deleted_and_written_again():
    """Only the count a stretch is out by is a grid edit; the words are merged after,
    cell by cell, and a row deleted and rebuilt would lose what the document put in it."""
    result = regrid([["a ONE", "b ONE"], ["a two", "b two"], ["a new", "b new"]])
    assert [next(iter(r)) for r in result["structure"]] == ["insertTableRow"]
    assert result["structure"][0]["insertTableRow"]["tableCellLocation"]["rowIndex"] == 1


def test_a_grid_the_document_also_changed_is_left_alone():
    ours = live([GRID["blocks"][0], table("t:grid", [["a one", "b one", "c one"],
                                                     ["a two", "b two", "c two"]]),
                 GRID["blocks"][2]])
    theirs = live([GRID["blocks"][0], table("t:grid", [["a one", "b one"]]),
                   GRID["blocks"][2]])
    result = doc_merge.plan(GRID, ours, theirs)
    assert result["structure"] == [] and result["requests"] == []
    assert "rows and columns" in result["notes"][0]


def test_rows_and_columns_changed_at_once_are_left_alone():
    """Every row is a column longer, so there is nothing left to match rows on."""
    result = regrid([["a one", "b one", "c one"]])
    assert result["structure"] == [] and result["requests"] == []
    assert "rows and columns" in result["notes"][0]


def test_a_table_the_source_added_is_built_where_the_file_puts_it():
    was = live([para("p:one", "one"), para("p:two", "two")])
    ours = live([was["blocks"][0], table("t:new", [["x", "y"]]), was["blocks"][1]])
    result = doc_merge.plan(was, ours, live(was["blocks"]))
    at = was["blocks"][1]["span"][0]
    assert result["structure"] == [
        {"insertTable": {"rows": 1, "columns": 2, "location": {"index": at}}},
        # `insertTable` splits the paragraph it is written into, so an empty one is
        # left in front of the table. The mark before it goes instead, which merges
        # the two and leaves both blocks as the file has them.
        {"deleteContentRange": {"range": {"startIndex": at - 1, "endIndex": at}}}]
    assert result["shaped"] == [{"key": "t:new", "after": "p:one",
                                 "note": "`t:new`: a table of 1×2 added by the source"}]


def test_a_table_the_source_added_in_front_of_a_table_goes_at_the_mark_before_it():
    """There is no paragraph at a table's own start index, and `insertTable` needs one.
    The mark of the paragraph in front of it is the index next door that is inside one,
    and the empty half it leaves lands between the two tables, where Docs wants it."""
    ours = live([GRID["blocks"][0], table("t:new", [["x"]]), GRID["blocks"][1],
                 GRID["blocks"][2]])
    result = doc_merge.plan(GRID, ours, live(GRID["blocks"]))
    assert result["structure"] == [{"insertTable": {
        "rows": 1, "columns": 1,
        "location": {"index": GRID["blocks"][1]["span"][0] - 1}}}]


def test_a_table_the_source_added_at_the_end_goes_to_the_end_of_the_segment():
    ours = live(GRID["blocks"] + [table("t:new", [["x"], ["y"]])])
    result = doc_merge.plan(GRID, ours, live(GRID["blocks"]))
    assert result["structure"] == [{"insertTable": {"rows": 2, "columns": 1,
                                                    "endOfSegmentLocation": {}}}]


def test_a_table_the_document_built_is_found_by_what_it_follows():
    """After the structural batch the new table carries no named range, and a read
    cannot tell it from one a reader made. It is the keyless table after `p:before`."""
    built = live([GRID["blocks"][0], table(None, [["", ""]]), GRID["blocks"][1],
                  GRID["blocks"][2]])
    built["blocks"][1]["key"] = None
    assert doc_merge.anchor_tables(built, [{"key": "t:new", "after": "p:before",
                                            "note": ""}]) == 1
    assert built["blocks"][1]["key"] == "t:new"


def test_the_base_takes_the_row_that_was_just_written_and_nothing_else():
    """The new grid is no longer a difference between the sides: it is what this sync
    wrote on the source's behalf, so the base says it too. Only the grid, though — the
    reader's words in that table were never agreed on, and a base that swallowed them
    would make the next plan read them as words the source had taken away."""
    shaped = [{"key": "t:grid", "ops": [("row", "insert", 1)]}]
    after = live([GRID["blocks"][0], table("t:grid", [["a one", "b one"], ["", ""],
                                                      ["a two", "b TYPED"]]),
                  GRID["blocks"][2]])
    rebased = doc_merge.rebase_tables(GRID, after, shaped)
    assert [b["key"] for b in rebased["blocks"]] == ["p:before", "t:grid", "p:after"]
    assert doc_merge._grid(rebased["blocks"][1]) == doc_merge._grid(after["blocks"][1])
    assert cell_text(rebased["blocks"][1], 1, 0) == ""        # the row that was made
    assert cell_text(rebased["blocks"][1], 2, 1) == "b two"   # not what the reader typed

    ours = live([GRID["blocks"][0], table("t:grid", [["a one", "b one"], ["a new", "b new"],
                                                     ["a two", "b two"]]),
                 GRID["blocks"][2]])
    result = doc_merge.plan(rebased, ours, live(after["blocks"]))
    assert result["structure"] == []
    # The source's words go into the row that was just made, and the reader keeps theirs.
    assert [r["insertText"]["text"] for r in result["requests"]] == ["b new", "a new"]
    assert cell_text(result["blocks"][1], 2, 1) == "b TYPED"


def test_a_new_table_lands_in_the_base_where_the_document_has_it():
    after = live([GRID["blocks"][0], table("t:new", [["x"]]), GRID["blocks"][1],
                  GRID["blocks"][2]])
    rebased = doc_merge.rebase_tables(GRID, after, [{"key": "t:new"}])
    assert [b["key"] for b in rebased["blocks"]] == ["p:before", "t:new", "t:grid", "p:after"]


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
    assert applied(live(BASE["blocks"]), result["requests"]) == texts(result)


def test_two_blocks_appended_at_the_end_keep_their_order():
    ours = live(BASE["blocks"] + [para("p:delta", "delta seven"), para("p:echo", "echo eight")])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    inserts = [r["insertText"] for r in result["requests"] if "insertText" in r]
    # Both go in at the one index, and what is written last ends up in front of what
    # was written before it, so the later block is written first.
    assert [i["text"] for i in inserts] == ["\necho eight", "\ndelta seven"]
    assert {i["location"]["index"] for i in inserts} == {BASE["blocks"][-1]["span"][1] - 1}
    assert applied(live(BASE["blocks"]), result["requests"]) == texts(result)


def test_two_blocks_added_before_one_block_keep_their_order():
    ours = live(BASE["blocks"][:1] + [para("p:delta", "delta"), para("p:echo", "echo")]
                + BASE["blocks"][1:])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    inserts = [r["insertText"] for r in result["requests"] if "insertText" in r]
    assert [i["text"] for i in inserts] == ["echo\n", "delta\n"]
    assert {i["location"]["index"] for i in inserts} == {BASE["blocks"][1]["span"][0]}
    assert applied(live(BASE["blocks"]), result["requests"]) == texts(result)


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
    assert applied(live(BASE["blocks"]), result["requests"]) == texts(result)


def test_a_block_appended_while_the_last_one_goes_lands_after_the_delete():
    ours = live(BASE["blocks"][:2] + [para("p:delta", "delta seven")])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    # The deleted block is further down the document, so it goes first and leaves the
    # paragraph mark the append was planned at — the second last one — where it was.
    assert next(iter(result["requests"][0])) == "deleteContentRange"
    assert result["requests"][1]["insertText"] == {
        "location": {"index": BASE["blocks"][1]["span"][1] - 1}, "text": "\ndelta seven"}
    assert applied(live(BASE["blocks"]), result["requests"]) == texts(result)


def test_the_last_block_gives_up_the_mark_before_it_not_the_bodys_own():
    """The body's last newline cannot be deleted (measured: refused, and the batch with
    it), so the last paragraph goes with the mark of the one in front of it."""
    ours = live(BASE["blocks"][:2])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    start, end = BASE["blocks"][2]["span"]
    assert result["requests"] == [{"deleteContentRange": {"range": {
        "startIndex": start - 1, "endIndex": end - 1}}}]
    assert applied(live(BASE["blocks"]), result["requests"]) == texts(result)


def test_the_last_two_blocks_pass_the_mark_leftwards():
    ours = live(BASE["blocks"][:1])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    assert applied(live(BASE["blocks"]), result["requests"]) == ["alpha one two"]


def test_a_block_after_a_final_table_is_written_into_the_paragraph_the_body_keeps():
    """A body that ends on a table ends on an empty paragraph too, which the read
    leaves out. The first block appended after the table fills it; the next ones
    follow it, so no empty paragraph is left between the table and them."""
    base = live([para("p:one", "one"), table("t:grid", [["a", "b"]])])
    theirs = live(base["blocks"])
    theirs["trailer"] = [theirs["blocks"][-1]["span"][1], theirs["blocks"][-1]["span"][1] + 1]
    ours = live(base["blocks"] + [para("p:two", "two"), para("p:three", "three")])
    result = doc_merge.plan(base, ours, theirs)
    inserts = [r["insertText"] for r in result["requests"] if "insertText" in r]
    at = theirs["trailer"][0]
    assert inserts == [{"location": {"index": at}, "text": "\nthree"},
                       {"location": {"index": at}, "text": "two"}]


def test_the_empty_paragraph_after_a_final_table_is_not_a_block():
    doc = {"body": {"content": [
        {"startIndex": 1, "endIndex": 5, "paragraph": {"elements": [
            {"startIndex": 1, "endIndex": 5, "textRun": {"content": "one\n"}}]}},
        {"startIndex": 5, "endIndex": 12, "table": {"tableRows": [{"tableCells": [{"content": [
            {"startIndex": 7, "endIndex": 9, "paragraph": {"elements": [
                {"startIndex": 7, "endIndex": 9, "textRun": {"content": "a\n"}}]}}]}]}]}},
        {"startIndex": 12, "endIndex": 13, "paragraph": {"elements": [
            {"startIndex": 12, "endIndex": 13, "textRun": {"content": "\n"}}]}}]}}
    ir = doc_ir.from_document(doc)
    assert [b["kind"] for b in ir["blocks"]] == ["paragraph", "table"]
    assert ir["trailer"] == [12, 13]
    assert doc_merge.tidy_requests(ir) == []
    # Inserted after a list item, the table leaves that item's glyph on the paragraph
    # after it: still no block, but made a plain paragraph again.
    doc["body"]["content"][-1]["paragraph"]["bullet"] = {"listId": "l"}
    ir = doc_ir.from_document(doc)
    assert ir["trailer_kind"] == "item"
    assert [next(iter(r)) for r in doc_merge.tidy_requests(ir)] == [
        "deleteParagraphBullets", "updateParagraphStyle"]
    del doc["body"]["content"][-1]["paragraph"]["bullet"]
    # One a reader typed into is a block like any other.
    doc["body"]["content"][-1]["paragraph"]["elements"][0]["textRun"]["content"] = "typed\n"
    assert [b["kind"] for b in doc_ir.from_document(doc)["blocks"]] == ["paragraph", "table",
                                                                        "paragraph"]


def test_a_list_the_import_left_unreadable_gets_bullets_of_its_own():
    """Measured: createParagraphBullets over an imported list gives it real glyphs, so
    it reads back as bulleted or numbered from then on — a toolbar switch included."""
    theirs = live([para("p:top", "top"),
                   {"kind": "item", "key": "i:a", "level": 0, "runs": [{"text": "a"}]},
                   {"kind": "item", "key": "i:b", "level": 1, "runs": [{"text": "b"}]},
                   {"kind": "item", "key": "i:c", "level": 0, "runs": [{"text": "c"}]},
                   para("p:end", "end")])
    ours = live([dict(b, ordered=b["key"] == "i:c") for b in theirs["blocks"]])
    doc_merge.restore_unreadable(theirs, ours)
    a, b, c = theirs["blocks"][1:4]
    assert doc_merge.bullet_requests(theirs) == [
        {"createParagraphBullets": {"range": {"startIndex": a["span"][0], "endIndex": b["span"][1]},
                                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}},
        {"createParagraphBullets": {"range": {"startIndex": c["span"][0], "endIndex": c["span"][1]},
                                    "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN"}}]
    # A list that reads properly is left alone.
    readable = live([{k: v for k, v in b.items() if k != "guessed"} | {"ordered": False}
                     for b in theirs["blocks"] if b["kind"] == "item"])
    assert doc_merge.bullet_requests(doc_merge.restore_unreadable(readable)) == []


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


# ---------------------------------------------------------------- moves

# Five, so that moving one block across two others is the only cheapest answer: with
# four, swapping a neighbouring pair can be read as either of them having moved.
FIVE = live([para("p:alpha", "alpha one"), para("p:bravo", "bravo two"),
             para("p:charlie", "charlie three"), para("p:delta", "delta four"),
             para("p:echo", "echo five")])
A, B, C, D, E = range(5)


def order(*places: int) -> dict:
    return live([FIVE["blocks"][i] for i in places])


def test_a_block_the_source_moved_up_is_moved_in_the_document():
    result = doc_merge.plan(FIVE, order(A, D, B, C, E), live(FIVE["blocks"]))
    assert texts(result) == ["alpha one", "delta four", "bravo two", "charlie three",
                             "echo five"]
    assert [b["key"] for b in result["blocks"] if b.get("moved")] == ["p:delta"]
    kinds = [next(iter(r)) for r in result["requests"]]
    # Deleted where it was first (the higher index), then written where it belongs.
    assert kinds == ["deleteContentRange", "insertText", "deleteParagraphBullets",
                     "updateParagraphStyle"]
    assert result["requests"][0]["deleteContentRange"]["range"] == {
        "startIndex": FIVE["blocks"][D]["span"][0], "endIndex": FIVE["blocks"][D]["span"][1]}
    assert result["requests"][1]["insertText"] == {
        "location": {"index": FIVE["blocks"][B]["span"][0]}, "text": "delta four\n"}


def test_a_block_the_source_moved_down_is_written_before_it_is_deleted():
    result = doc_merge.plan(FIVE, order(A, C, D, B, E), live(FIVE["blocks"]))
    assert texts(result) == ["alpha one", "charlie three", "delta four", "bravo two",
                             "echo five"]
    kinds = [next(iter(r)) for r in result["requests"]]
    assert kinds == ["insertText", "deleteParagraphBullets", "updateParagraphStyle",
                     "deleteContentRange"]
    assert result["requests"][0]["insertText"] == {
        "location": {"index": FIVE["blocks"][E]["span"][0]}, "text": "bravo two\n"}


def test_a_moved_block_carries_the_words_both_sides_changed():
    ours = live([FIVE["blocks"][A], para("p:delta", "delta four SOURCE"),
                 FIVE["blocks"][B], FIVE["blocks"][C], FIVE["blocks"][E]])
    theirs = live([FIVE["blocks"][A], FIVE["blocks"][B], FIVE["blocks"][C],
                   para("p:delta", "READER delta four"), FIVE["blocks"][E]])
    result = doc_merge.plan(FIVE, ours, theirs)
    assert texts(result)[1] == "READER delta four SOURCE"
    assert [r["insertText"]["text"] for r in result["requests"] if "insertText" in r] == \
        ["READER delta four SOURCE\n"]


def test_a_moved_list_item_is_written_as_a_list_item():
    was = live([para("p:alpha", "alpha one"), para("p:bravo", "bravo two"),
                para("p:charlie", "charlie three"),
                {"kind": "item", "level": 0, "ordered": True, "key": "item:step",
                 "runs": [{"text": "step one"}]}, para("p:echo", "echo five")])
    ours = live([was["blocks"][i] for i in (A, D, B, C, E)])
    result = doc_merge.plan(was, ours, live(was["blocks"]))
    kinds = [next(iter(r)) for r in result["requests"]]
    assert kinds == ["deleteContentRange", "insertText", "updateParagraphStyle",
                     "createParagraphBullets"]
    assert (result["requests"][-1]["createParagraphBullets"]["bulletPreset"]
            == "NUMBERED_DECIMAL_ALPHA_ROMAN")


def test_a_reversal_moves_the_fewest_blocks_it_can():
    result = doc_merge.plan(FIVE, order(E, D, C, B, A), live(FIVE["blocks"]))
    assert texts(result) == ["echo five", "delta four", "charlie three", "bravo two",
                             "alpha one"]
    # One block of the five can stay where it is, and exactly one does.
    assert len([b for b in result["blocks"] if b.get("moved")]) == 4
    assert applied(live(FIVE["blocks"]), result["requests"]) == texts(result)


@pytest.mark.parametrize("places", [(A, D, B, C, E), (A, C, D, B, E), (E, D, C, B, A),
                                    (B, C, D, E, A), (A, B, E, C, D), (D, E, A, B, C)])
def test_whatever_the_source_reordered_the_requests_say_the_same(places):
    """The one that matters: run the plan against the document's own index space and
    the document ends up saying exactly what the merge says it should."""
    result = doc_merge.plan(FIVE, order(*places), live(FIVE["blocks"]))
    assert texts(result) == [doc_merge.block_text(FIVE["blocks"][i]) for i in places]
    assert applied(live(FIVE["blocks"]), result["requests"]) == texts(result)


def test_the_requests_say_the_same_when_blocks_are_added_moved_and_deleted_at_once():
    ours = live([FIVE["blocks"][A], para("p:new", "a new one"), FIVE["blocks"][D],
                 FIVE["blocks"][B], para("p:last", "another new one")])
    result = doc_merge.plan(FIVE, ours, live(FIVE["blocks"]))
    assert texts(result) == ["alpha one", "a new one", "delta four", "bravo two",
                             "another new one"]
    assert applied(live(FIVE["blocks"]), result["requests"]) == texts(result)


def test_both_sides_reordering_leaves_the_documents_order_alone():
    theirs = order(B, A, C, D, E)
    result = doc_merge.plan(FIVE, order(A, D, B, C, E), theirs)
    assert texts(result) == ["bravo two", "alpha one", "charlie three", "delta four",
                             "echo five"]
    assert result["requests"] == []
    assert "both sides moved blocks" in result["notes"][0]


def test_a_block_the_document_moved_stays_where_the_document_put_it():
    result = doc_merge.plan(FIVE, live(FIVE["blocks"]), order(A, D, B, C, E))
    assert texts(result) == ["alpha one", "delta four", "bravo two", "charlie three",
                             "echo five"]
    assert result["requests"] == []


def test_a_block_with_a_chip_in_it_is_not_moved():
    was = live([para("p:alpha", "alpha one"), para("p:bravo", "bravo two"),
                para("p:charlie", "charlie three"),
                {"kind": "paragraph", "key": "p:chip", "runs": [
                    {"text": "see "}, {"chip": "person", "frozen": True, "text": "Ada"}]},
                para("p:echo", "echo five")])
    ours = live([was["blocks"][i] for i in (A, D, B, C, E)])
    result = doc_merge.plan(was, ours, live(was["blocks"]))
    assert result["requests"] == []
    assert "cannot be written from nothing" in result["notes"][0]


def test_a_move_and_the_second_sync_that_writes_nothing():
    """What `settle` leaves behind: the file, regenerated from the document."""
    ours = order(A, D, B, C, E)
    result = doc_merge.plan(FIVE, ours, live(FIVE["blocks"]))
    settled = live([{k: v for k, v in b.items() if k not in ("span", "moved", "origin")}
                    for b in result["blocks"]])
    again = doc_merge.plan(settled, ours, live(settled["blocks"]))
    assert again["requests"] == [] and again["notes"] == []


def test_a_moved_block_keeps_its_key_after_the_write():
    """The delete takes the named range with it, so the read-back has no key for the
    block that moved. `settle` hands the plan back to it rather than naming it again
    from its own words, or a paragraph nobody touched would be renamed in the diff."""
    result = doc_merge.plan(FIVE, order(A, D, B, C, E), live(FIVE["blocks"]))
    read_back = live([{k: v for k, v in b.items()
                       if k not in ("span", "moved", "origin", "key")}
                      if b.get("moved") else {k: v for k, v in b.items() if k != "span"}
                      for b in result["blocks"]])
    assert doc_merge.adopt_keys(read_back, result["blocks"]) == 1
    assert [b["key"] for b in read_back["blocks"]] == [
        "p:alpha", "p:delta", "p:bravo", "p:charlie", "p:echo"]


def test_an_added_block_keeps_the_id_its_author_wrote():
    ours = live(BASE["blocks"] + [para("p:the-note", "a paragraph with an id of its own")])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    read_back = live([para(None, doc_merge.block_text(b)) for b in result["blocks"]])
    doc_merge.adopt_keys(read_back, result["blocks"])
    assert read_back["blocks"][-1]["key"] == "p:the-note"


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
