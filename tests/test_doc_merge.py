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
        elif any(k in request for k in ("insertInlineImage", "insertPerson", "insertDate")):
            # A picture or a chip: one unit, which `block_text` shows as FROZEN.
            at = next(iter(request.values()))["location"]["index"] - 1
            assert 0 <= at < len(text), f"object index {at + 1} is not inside a paragraph"
            text = text[:at] + doc_merge.FROZEN + text[at:]
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


def test_a_source_that_would_rewrite_a_chip_the_document_also_edited_is_refused():
    base = live([chipped("due ", " please")])
    ours = live([{"kind": "paragraph", "key": "p:due",
                  "runs": [{"text": "due tomorrow please"}]}])
    theirs = live([chipped("due ", " please, really")])
    result = doc_merge.plan(base, ours, theirs)
    assert result["requests"] == []
    assert "left alone" in result["notes"][0]
    assert doc_merge.frozen_of(result["blocks"][0]) == (("date", "Sep 25, 2026",
                                                        "2026-09-25T12:00:00Z"),)


def test_a_chip_the_source_took_out_of_an_untouched_block_is_taken_out():
    """The document left the block as both sides agreed, so the file's change is the
    only one there is: the block is written again, without the chip."""
    base = live([chipped("due ", " please")])
    ours = live([{"kind": "paragraph", "key": "p:due",
                  "runs": [{"text": "due tomorrow please"}]}])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    assert result["blocks"][0]["rewrite"] and not result["notes"]
    assert applied(live(base["blocks"]), result["requests"]) == ["due tomorrow please"]


def test_an_equation_is_never_deleted_to_write_a_block_again():
    equation = {"chip": "equation", "frozen": True, "text": ""}
    base = live([{"kind": "paragraph", "key": "p:eq", "runs": [{"text": "so "}, dict(equation)]}])
    ours = live([{"kind": "paragraph", "key": "p:eq",
                  "runs": [{"text": "so "}, dict(equation), dict(CHIP)]}])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    assert result["requests"] == [] and "no request can write" in result["notes"][0]


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
    # the bold. The face and the size are named too, now that the file carries them —
    # a field named with no value puts the paragraph's own named style back, which is
    # what a run saying nothing about its face looks like on the page.
    assert style["fields"] == ",".join(doc_merge.MANAGED)
    assert {"weightedFontFamily", "fontSize", "smallCaps"} <= set(doc_merge.MANAGED)


def test_styling_a_second_sync_writes_nothing():
    base = live([styled("p:s", {"text": "one two three"})])
    ours = live([styled("p:s", {"text": "one "}, {"text": "two", "bold": True},
                        {"text": " three"})])
    # The document now says what the source says: the same run layout, read back.
    theirs = live([styled("p:s", {"text": "one "}, {"text": "two", "bold": True},
                          {"text": " three"})])
    assert doc_merge.plan(theirs, ours, theirs)["requests"] == []


def styles_written(result: dict) -> list[tuple[int, int, dict]]:
    return [(r["updateTextStyle"]["range"]["startIndex"], r["updateTextStyle"]["range"]["endIndex"],
             r["updateTextStyle"]["textStyle"]) for r in result["requests"] if "updateTextStyle" in r]


def test_a_word_the_source_restyled_is_styled_while_the_document_rewrote_another():
    base = live([styled("p:s", {"text": "one two three"})])
    ours = live([styled("p:s", {"text": "one "}, {"text": "two", "bold": True},
                        {"text": " three"})])
    theirs = live([styled("p:s", {"text": "one two THREE"})])
    result = doc_merge.plan(base, ours, theirs)
    at = theirs["blocks"][0]["span"][0]
    assert [(s - at, e - at, style) for s, e, style in styles_written(result)] == [
        (0, 4, {}), (4, 7, {"bold": True}), (7, 13, {})]
    assert result["notes"] == []
    runs = result["blocks"][0]["runs"]
    assert [(r["text"], r.get("bold", False)) for r in runs] == [
        ("one ", False), ("two", True), (" THREE", False)]


def test_a_word_the_source_restyled_and_the_document_rewrote_is_the_documents():
    base = live([styled("p:s", {"text": "one two three"})])
    ours = live([styled("p:s", {"text": "one "}, {"text": "two", "bold": True},
                        {"text": " three"})])
    theirs = live([styled("p:s", {"text": "one TWO three"})])
    result = doc_merge.plan(base, ours, theirs)
    assert all(style == {} for _, _, style in styles_written(result))
    assert "keep the document's styling" in result["notes"][0]


def test_the_source_restyles_and_rewords_while_the_document_adds_words():
    base = live([styled("p:s", {"text": "the plan is ready"})])
    ours = live([styled("p:s", {"text": "the "}, {"text": "new plan", "italic": True},
                        {"text": " is ready"})])
    theirs = live([styled("p:s", {"text": "the plan is ready today"})])
    result = doc_merge.plan(base, ours, theirs)
    runs = result["blocks"][0]["runs"]
    assert "".join(r["text"] for r in runs) == "the new plan is ready today"
    assert [r["text"] for r in runs if r.get("italic")] == ["new plan"]


def test_a_paragraph_the_source_turned_into_a_heading_is_written():
    base = live([para("p:h", "a heading")])
    ours = live([{"kind": "heading", "level": 2, "key": "p:h", "runs": [{"text": "a heading"}]}])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    style = [r["updateParagraphStyle"] for r in result["requests"]][0]
    assert style["paragraphStyle"]["namedStyleType"] == "HEADING_2"
    assert style["range"] == {"startIndex": 1, "endIndex": 11}


def test_a_title_the_source_moved_or_reworded_is_still_a_title():
    """`namedStyleType` is named on every paragraph the merge writes, so a style the
    dialect could not spell was one it wrote NORMAL_TEXT over. Docs has NORMAL_TEXT,
    TITLE, SUBTITLE and HEADING_1..6, and `doc_ir.NAMED_KINDS` covers the last two."""
    was = {"kind": "title", "key": "t:x", "runs": [{"text": "The Report"}]}
    base = live([was])
    # The source centres it — a shape change, so the whole paragraph style is written,
    # `namedStyleType` with it.
    result = doc_merge.plan(base, live([was | {"align": "center"}]), live(base["blocks"]))
    style = [r["updateParagraphStyle"] for r in result["requests"]][0]
    assert style["paragraphStyle"]["namedStyleType"] == "TITLE"
    assert style["paragraphStyle"]["alignment"] == "CENTER"
    # And the source moving it off the style still writes what it asked for.
    plain = doc_merge.plan(base, live([para("t:x", "The Report")]), live(base["blocks"]))
    assert [r["updateParagraphStyle"] for r in plain["requests"]][0][
        "paragraphStyle"]["namedStyleType"] == "NORMAL_TEXT"


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


def test_a_table_the_reader_typed_in_survives_the_source_dropping_it():
    """A document edit outranks a source delete, and `block_text` is empty for a
    table: every table read as untouched, so one the source dropped was deleted
    however much the reader had typed into it. Its cells say what it says.
    """
    ours = live([GRID["blocks"][0], GRID["blocks"][2]])
    theirs = live(GRID["blocks"])
    theirs["blocks"][1]["rows"][0][1][0]["runs"] = [{"text": "b ONE typed"}]
    result = doc_merge.plan(GRID, ours, live(theirs["blocks"]))
    assert [b.get("key") for b in result["blocks"]] == ["p:before", "t:grid", "p:after"]
    assert cell_text(result["blocks"][1], 0, 1) == "b ONE typed"
    assert any("dropped by the source but edited" in note for note in result["notes"])


def test_a_row_the_reader_added_survives_the_source_dropping_the_table():
    """The same, for a reader who changed the grid and not a word: two empty cells
    more read back as the same words in the same order."""
    ours = live([GRID["blocks"][0], GRID["blocks"][2]])
    theirs = live([GRID["blocks"][0],
                   table("t:grid", [["a one", "b one"], ["a two", "b two"], ["", ""]]),
                   GRID["blocks"][2]])
    result = doc_merge.plan(GRID, ours, theirs)
    assert [b.get("key") for b in result["blocks"]] == ["p:before", "t:grid", "p:after"]


def test_a_table_nobody_touched_still_goes_when_the_source_drops_it():
    ours = live([GRID["blocks"][0], GRID["blocks"][2]])
    result = doc_merge.plan(GRID, ours, live(GRID["blocks"]))
    assert [b.get("key") for b in result["blocks"]] == ["p:before", "p:after"]


def test_a_block_added_in_front_of_a_table_goes_after_the_paragraph_before_it():
    """Measured: nothing can be inserted at a table's own index."""
    ours = live([GRID["blocks"][0], para("p:new", "a new line"), *GRID["blocks"][1:]])
    result = doc_merge.plan(GRID, ours, live(GRID["blocks"]))
    insert = [r["insertText"] for r in result["requests"] if "insertText" in r]
    assert insert == [{"location": {"index": GRID["blocks"][1]["span"][0] - 1},
                       "text": "\na new line"}]


def test_a_body_that_starts_with_a_table_writes_in_front_of_it_into_its_lead():
    """A tab's first table has an empty paragraph in front of it that cannot go:
    it is not a block, and the first block written before the table is typed into it."""
    grid = live([para(None, ""), table("t:grid", [["a", "b"]]), para(None, "")])
    doc = {"body": {"content": [
        {"startIndex": b["span"][0], "endIndex": b["span"][1],
         **({"table": {"tableRows": [{"tableCells": [{"content": []}]}]}} if b["kind"] == "table"
            else {"paragraph": {"elements": [{"startIndex": b["span"][0], "endIndex": b["span"][1],
                                              "textRun": {"content": "\n"}}]}})}
        for b in grid["blocks"]]}}
    read = doc_ir.from_document(doc)
    assert [b["kind"] for b in read["blocks"]] == ["table"]
    assert read["lead"] == [1, 2] and read["trailer"] == grid["blocks"][2]["span"]
    theirs = {"blocks": [grid["blocks"][1]], "lead": [1, 2]}
    ours = {"blocks": [para("p:one", "one"), para("p:two", "two"), grid["blocks"][1]]}
    result = doc_merge.plan({"blocks": [grid["blocks"][1]]}, ours, theirs)
    assert [r["insertText"] for r in result["requests"] if "insertText" in r] == [
        {"location": {"index": 1}, "text": "\ntwo"}, {"location": {"index": 1}, "text": "one"}]


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


def grid_table(rows: list[list[str]], key: str = "t:grid") -> dict:
    """The GRID document with its table replaced by one of these cells."""
    return live([GRID["blocks"][0], table(key, rows), GRID["blocks"][2]])


def kinds(requests: list[dict]) -> list[str]:
    return [next(iter(r)) for r in requests]


def test_a_grid_both_sides_changed_merges_rows_from_one_and_columns_from_the_other():
    """The source added a column, the reader deleted a row: both stand."""
    ours = grid_table([["a one", "b one", "c one"], ["a two", "b two", "c two"]])
    theirs = grid_table([["a one", "b one"]])
    result = doc_merge.plan(GRID, ours, theirs)
    assert result["structure"] == [{"insertTableColumn": {
        "tableCellLocation": {"tableStartLocation": {"index": theirs["blocks"][1]["span"][0]},
                              "rowIndex": 0, "columnIndex": 1}, "insertRight": True}}]
    assert result["notes"] == []


def test_rows_and_columns_changed_at_once_are_both_written():
    """Rows are matched by their cells in the columns that match, so a row a column
    longer is still the same row."""
    result = regrid([["a one", "b one", "c one"]])
    at = GRID["blocks"][1]["span"][0]
    assert result["structure"] == [
        {"deleteTableRow": {"tableCellLocation": {"tableStartLocation": {"index": at},
                                                  "rowIndex": 1, "columnIndex": 0}}},
        {"insertTableColumn": {"tableCellLocation": {"tableStartLocation": {"index": at},
                                                     "rowIndex": 0, "columnIndex": 1},
                               "insertRight": True}}]


def test_a_row_the_source_deleted_but_the_document_wrote_in_is_kept():
    theirs = grid_table([["a one", "b one"], ["a two", "b TYPED"]])
    result = doc_merge.plan(GRID, grid_table([["a one", "b one"]]), theirs)
    assert result["structure"] == []
    assert "took away a row, but the document wrote in it" in result["notes"][0]


def test_a_row_the_document_added_stays_while_the_source_adds_a_column():
    theirs = grid_table([["a one", "b one"], ["a two", "b two"], ["a doc", "b doc"]])
    ours = grid_table([["a one", "b one", "c one"], ["a two", "b two", "c two"]])
    result = doc_merge.plan(GRID, ours, theirs)
    assert kinds(result["structure"]) == ["insertTableColumn"]
    assert result["structure"][0]["insertTableColumn"]["tableCellLocation"]["columnIndex"] == 1

    after = grid_table([["a one", "b one", ""], ["a two", "b two", ""],
                        ["a doc", "b doc", ""]])
    rebased = doc_merge.rebase_tables(GRID, after, result["shaped"])
    # The reader's row is not in the base: nothing agreed on it.
    assert doc_merge._grid(rebased["blocks"][1]) == (3, 3)
    assert rebased["blocks"][1]["aligned"]["row_live"] == [(0, 0), (1, 1)]
    again = doc_merge.plan(rebased, ours, after)
    assert again["structure"] == [] and again["notes"] == []
    assert [r["insertText"]["text"] for r in again["requests"]] == ["c two", "c one"]
    assert [cell_text(again["blocks"][1], r, 2) for r in range(3)] == ["c one", "c two", ""]


def test_a_column_and_a_row_the_source_added_get_their_words_on_the_next_pass():
    ours = grid_table([["a one", "NEW", "b one"], ["a two", "NEW2", "b two"],
                       ["a three", "x", "b three"]])
    theirs = grid_table([["a one", "b one"], ["a two", "b TYPED"]])
    result = doc_merge.plan(GRID, ours, theirs)
    at = theirs["blocks"][1]["span"][0]
    assert result["structure"] == [
        {"insertTableRow": {"tableCellLocation": {"tableStartLocation": {"index": at},
                                                  "rowIndex": 1, "columnIndex": 0},
                            "insertBelow": True}},
        {"insertTableColumn": {"tableCellLocation": {"tableStartLocation": {"index": at},
                                                     "rowIndex": 0, "columnIndex": 0},
                               "insertRight": True}}]

    after = grid_table([["a one", "", "b one"], ["a two", "", "b TYPED"], ["", "", ""]])
    rebased = doc_merge.rebase_tables(GRID, after, result["shaped"])
    again = doc_merge.plan(rebased, ours, after)
    assert again["structure"] == []
    merged = again["blocks"][1]
    assert [[doc_merge.block_text(c[0]) for c in row] for row in merged["rows"]] == [
        ["a one", "NEW", "b one"], ["a two", "NEW2", "b TYPED"], ["a three", "x", "b three"]]
    assert sorted(r["insertText"]["text"] for r in again["requests"]) == [
        "NEW", "NEW2", "a three", "b three", "x"]


def test_a_cell_the_source_split_into_two_paragraphs_is_written_with_the_break():
    ours = grid_table([["a one", "b one"], ["a two", "b two"]])
    ours["blocks"][1]["rows"][0][1] = [{"kind": "paragraph", "runs": [{"text": "b one"}]},
                                       {"kind": "paragraph", "runs": [{"text": "b more"}]}]
    theirs = grid_table([["a one", "b one"], ["a two", "b TYPED"]])
    result = doc_merge.plan(GRID, ours, theirs)
    assert result["structure"] == []
    cell = theirs["blocks"][1]["rows"][0][1][0]
    assert [r for r in result["requests"] if "insertText" in r] == [
        {"insertText": {"location": {"index": cell["span"][1] - 1}, "text": "\nb more"}}]


def test_a_cell_the_document_holds_two_paragraphs_in_merges_as_one_text():
    """The reader pressed Enter in a cell and the source rewrote a word of it."""
    ours = grid_table([["a ONE", "b one"], ["a two", "b two"]])
    typed = table("t:grid", [["a one", "b one"], ["a two", "b two"]])
    typed["rows"][0][0] = [{"kind": "paragraph", "runs": [{"text": "a one"}]},
                           {"kind": "paragraph", "runs": [{"text": "a typed"}]}]
    theirs = live([GRID["blocks"][0], typed, GRID["blocks"][2]])
    result = doc_merge.plan(GRID, ours, theirs)
    first = theirs["blocks"][1]["rows"][0][0][0]
    inserts = [r["insertText"] for r in result["requests"] if "insertText" in r]
    assert inserts == [{"location": {"index": first["span"][1] - 1}, "text": "ONE"}]
    assert result["blocks"][1]["rows"][0][0][0]["joined"]
    assert doc_merge.block_text(result["blocks"][1]["rows"][0][0][0]) == "a ONE\na typed"


MOVE = live([para("p:one", "one"), para("p:two", "two"),
             table("t:grid", [["a", "b"]]), para("p:three", "three")])


def test_a_table_the_source_moved_is_deleted_and_built_again_where_the_file_has_it():
    ours = live([MOVE["blocks"][2], MOVE["blocks"][0], MOVE["blocks"][1], MOVE["blocks"][3]])
    result = doc_merge.plan(MOVE, ours, live(MOVE["blocks"]))
    grid = MOVE["blocks"][2]["span"]
    assert result["structure"] == [
        {"deleteContentRange": {"range": {"startIndex": grid[0], "endIndex": grid[1]}}},
        {"insertTable": {"rows": 1, "columns": 2, "location": {"index": 1}}}]
    assert result["shaped"][0] | {"note": ""} == {"key": "t:grid", "after": None,
                                                  "moved": True, "note": ""}

    after = live([table(None, [["", ""]]), *MOVE["blocks"][:2], MOVE["blocks"][3]])
    after["blocks"][0]["key"] = None
    assert doc_merge.anchor_tables(after, result["shaped"]) == 1
    rebased = doc_merge.rebase_tables(MOVE, after, result["shaped"])
    assert [b["key"] for b in rebased["blocks"]] == ["t:grid", "p:one", "p:two", "p:three"]
    again = doc_merge.plan(rebased, ours, after)
    assert again["structure"] == []
    assert sorted(r["insertText"]["text"] for r in again["requests"]) == ["a", "b"]


def test_a_table_the_document_changed_is_not_moved():
    ours = live([MOVE["blocks"][2], MOVE["blocks"][0], MOVE["blocks"][1], MOVE["blocks"][3]])
    theirs = live([*MOVE["blocks"][:2], table("t:grid", [["a", "TYPED"]]), MOVE["blocks"][3]])
    result = doc_merge.plan(MOVE, ours, theirs)
    assert result["structure"] == []
    assert any("document changed it" in n for n in result["notes"])


def test_a_table_the_source_deleted_at_the_end_takes_the_empty_paragraph_after_it():
    """Measured: the mark in front of the table and the table leave `abc\\n` and no
    trailer; the table's own span alone would leave an empty paragraph behind."""
    was = live([para("p:one", "one"), table("t:grid", [["a"]])])
    was["trailer"] = [was["blocks"][1]["span"][1], was["blocks"][1]["span"][1] + 1]
    theirs = live(was["blocks"]) | {"trailer": was["trailer"]}
    result = doc_merge.plan(was, live([was["blocks"][0]]), theirs)
    span = was["blocks"][1]["span"]
    assert result["requests"] == [{"deleteContentRange": {"range": {
        "startIndex": span[0] - 1, "endIndex": span[1]}}}]


def test_a_table_the_source_deleted_at_the_start_takes_the_lead_with_it():
    was = live([table("t:grid", [["a"]]), para("p:one", "one")], start=2)
    theirs = live(was["blocks"], start=2) | {"lead": [1, 2]}
    result = doc_merge.plan(was, live([was["blocks"][1]]), theirs)
    assert result["requests"] == [{"deleteContentRange": {"range": {
        "startIndex": 1, "endIndex": was["blocks"][0]["span"][1]}}}]


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


def test_a_new_block_carrying_an_equation_is_reported_not_written():
    ours = live([BASE["blocks"][0],
                 {"kind": "paragraph", "key": "p:chip", "runs": [
                     {"text": "so "}, {"chip": "equation", "frozen": True, "text": ""}]}]
                + BASE["blocks"][1:])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    assert result["requests"] == []
    assert "no request can create" in result["notes"][0]


def test_a_new_block_carrying_a_date_or_a_person_is_written_with_them():
    """Measured: insertDate and insertPerson make the chip; a rich link is refused."""
    person = {"chip": "person", "frozen": True, "text": "Ada", "value": "ada@example.com"}
    ours = live([BASE["blocks"][0],
                 {"kind": "paragraph", "key": "p:chip", "runs": [
                     {"text": "due "}, dict(CHIP), {"text": " ask "}, person]}]
                + BASE["blocks"][1:])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    at = BASE["blocks"][1]["span"][0]
    kinds = [next(iter(r)) for r in result["requests"]]
    assert kinds[:3] == ["insertText", "insertPerson", "insertDate"]
    assert result["requests"][0]["insertText"]["text"] == "due  ask \n"
    # Back to front, each at its place in the words: the person after "due  ask ",
    # then the date after "due ", which pushes the person one unit right.
    assert result["requests"][1]["insertPerson"] == {
        "location": {"index": at + 9}, "personProperties": {"email": "ada@example.com"}}
    assert result["requests"][2]["insertDate"]["location"] == {"index": at + 4}
    assert applied(live(BASE["blocks"]), result["requests"]) == texts(result)


# ---------------------------------------------------------------- pictures

def picture(src: str, **rest) -> dict:
    return {"chip": "image", "frozen": True, "text": "", "src": src, **rest}


def figure(key: str, *runs: dict) -> dict:
    return {"kind": "paragraph", "key": key, "runs": [dict(r) for r in runs]}


def test_a_picture_the_source_added_is_staged_and_inserted():
    ours = live(BASE["blocks"][:1] + [figure("p:plot", picture("figures/plot.png", sha="s1",
                                                               size=[60, 40]))]
                + BASE["blocks"][1:])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    at = BASE["blocks"][1]["span"][0]
    assert result["requests"][0] == {"insertText": {"location": {"index": at}, "text": "\n"}}
    # The file's name stands in for a URL until `doc_sync.Stager` has one, and the
    # size goes in points: 60 × 40 px is what the importer makes 45 × 30 pt.
    assert result["requests"][1] == {"insertInlineImage": {
        "location": {"index": at}, "uri": doc_merge.STAGE + "figures/plot.png",
        "objectSize": {"width": {"magnitude": 45.0, "unit": "PT"},
                       "height": {"magnitude": 30.0, "unit": "PT"}}}}
    assert applied(live(BASE["blocks"]), result["requests"]) == texts(result)


def test_a_picture_among_words_goes_where_the_words_put_it():
    ours = live(BASE["blocks"] + [figure("p:inline", {"text": "see "}, picture("a.png", sha="a"),
                                         {"text": " and "}, picture("b.png", sha="b"))])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    assert applied(live(BASE["blocks"]), result["requests"]) == texts(result)
    assert texts(result)[-1] == f"see {doc_merge.FROZEN} and {doc_merge.FROZEN}"


def test_a_picture_the_source_regenerated_is_replaced():
    base = live([para("p:one", "one"), figure("p:plot", picture("plot.png", value="i.0", sha="old"))])
    theirs = live(base["blocks"])
    theirs["blocks"][1]["runs"][0]["uri"] = "https://lh7/old"
    ours = live([para("p:one", "one"), figure("p:plot", picture("plot.png", value="i.0", sha="new"))])
    result = doc_merge.plan(base, ours, theirs)
    start, end = theirs["blocks"][1]["span"]
    assert result["requests"][0] == {"deleteContentRange": {"range": {
        "startIndex": start, "endIndex": end - 1}}}
    assert result["requests"][1]["insertInlineImage"]["uri"] == doc_merge.STAGE + "plot.png"
    assert applied(theirs, result["requests"]) == texts(result)


def test_a_picture_the_source_only_renamed_is_no_change():
    base = live([figure("p:plot", picture("old-name.png", value="i.0", sha="same"))])
    ours = live([figure("p:plot", picture("new-name.png", value="i.0", sha="same"))])
    theirs = live([figure("p:plot", {"chip": "image", "frozen": True, "text": "", "value": "i.0"})])
    doc_merge.restore_pictures(theirs, base, ours)
    assert theirs["blocks"][0]["runs"][0]["src"] == "new-name.png"   # the file follows the rename
    assert doc_merge.plan(base, ours, theirs)["requests"] == []


def test_a_picture_file_that_is_not_checked_out_is_no_change():
    base = live([figure("p:plot", picture("plot.png", value="i.0", sha="s"))])
    ours = live([figure("p:plot", picture("plot.png", value="i.0", missing=True))])
    theirs = live(base["blocks"])
    assert doc_merge.plan(base, ours, theirs)["requests"] == []


def test_a_size_the_source_changes_is_said_out_loud_rather_than_dropped():
    """No request in the v1 API changes an embedded object, so a width edited in the
    file reaches nothing — and the settle then regenerates the file from the document,
    so the edit is taken back out of the file as well. Twice gone in silence is what
    the report is for. Deleting the picture from the file and writing it again is the
    way to have it at another size: a picture with no `data-object` is a new one and
    goes in with its `objectSize`."""
    base = live([figure("p:plot", picture("plot.png", value="i.0", sha="s", size=[60, 40],
                                          alt="A plot"))])
    ours = live([figure("p:plot", picture("plot.png", value="i.0", sha="s", size=[120, 80],
                                          alt="A bigger plot"))])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    assert result["requests"] == []
    assert [note.split(" — ")[0] for note in result["notes"]] == [
        "p:plot: the source gave the picture the size 120×80 and no request writes one",
        "p:plot: the source gave the picture the alt text 'A bigger plot' and no request "
        "writes one"]
    assert "60×40" in result["notes"][0] and "data-object" in result["notes"][0]


def test_a_size_that_goes_in_with_the_picture_the_sync_writes_is_not_reported():
    """A picture the source regenerated is inserted again from the file, `objectSize`
    and all, so that resize is written and there is nothing to say. A picture merely
    *moved* is written from the document's copy — the file's size is not on the run
    the plan writes, so that one is reported like any other. The alt text is reported
    either way: nothing carries one, at any size."""
    def plot(**rest):
        return figure("p:plot", {"text": "see "},
                      picture("plot.png", value="i.0", **rest))

    base = live([para("p:one", "one"), para("p:two", "two"),
                 plot(sha="s", size=[60, 40]), para("p:four", "four")])
    theirs = live(base["blocks"])
    theirs["blocks"][2]["runs"][1]["uri"] = "https://lh7/plot"
    ours = live(base["blocks"][:2] + [plot(sha="new", size=[120, 80], alt="A plot")]
                + base["blocks"][3:])
    result = doc_merge.plan(base, ours, theirs)
    images = [r["insertInlineImage"] for r in result["requests"] if "insertInlineImage" in r]
    assert images and images[0]["objectSize"]["width"]["magnitude"] == 90.0
    assert [note for note in result["notes"] if "the size" in note] == []
    assert [note for note in result["notes"] if "the alt text" in note]
    # Moved instead: the insert carries the document's 60 × 40, and it is said.
    ours = live([plot(sha="s", size=[120, 80])] + base["blocks"][:2] + base["blocks"][3:])
    result = doc_merge.plan(base, ours, live(theirs["blocks"]))
    images = [r["insertInlineImage"] for r in result["requests"] if "insertInlineImage" in r]
    assert images and images[0]["objectSize"]["width"]["magnitude"] == 45.0
    assert [note for note in result["notes"] if "the size" in note]


def test_a_picture_the_reader_replaced_is_the_documents():
    base = live([figure("p:plot", picture("plot.png", value="i.0", sha="s"))])
    theirs = live([figure("p:plot", {"chip": "image", "frozen": True, "text": "", "value": "kix.9"})])
    result = doc_merge.plan(base, live(base["blocks"]), theirs)
    assert result["requests"] == []


def test_a_moved_block_with_a_picture_is_written_from_the_documents_copy():
    was = live([para("p:alpha", "alpha one"), para("p:bravo", "bravo two"),
                para("p:charlie", "charlie three"),
                figure("p:plot", {"text": "see "}, picture("plot.png", value="i.0", sha="s")),
                para("p:echo", "echo five")])
    theirs = live(was["blocks"])
    theirs["blocks"][3]["runs"][1]["uri"] = "https://lh7/plot"
    ours = live([was["blocks"][i] for i in (A, D, B, C, E)])
    result = doc_merge.plan(was, ours, theirs)
    images = [r["insertInlineImage"] for r in result["requests"] if "insertInlineImage" in r]
    assert [i["uri"] for i in images] == ["https://lh7/plot"]    # no staging: Docs has it
    assert applied(theirs, result["requests"]) == texts(result)


def test_pictures_just_inserted_learn_their_files_by_place():
    planned = [figure("p:plot", {"text": "a "}, picture("a.png", sha="1", alt="first"),
                      picture("b.png", sha="2"))]
    back = {"blocks": [figure("p:plot", {"text": "a "},
                              {"chip": "image", "frozen": True, "text": "", "value": "kix.1"},
                              {"chip": "image", "frozen": True, "text": "", "value": "kix.2"})]}
    assert doc_merge.place_pictures(back, planned) == 2
    runs = back["blocks"][0]["runs"]
    assert (runs[1]["src"], runs[1]["alt"], runs[2]["src"]) == ("a.png", "first", "b.png")


# ---------------------------------------------------------------- add, delete, order

def test_a_block_the_source_added_is_inserted_with_its_styling():
    ours = live(BASE["blocks"][:1]
                + [{"kind": "heading", "level": 2, "key": "heading:new", "runs": [
                    {"text": "New "}, {"text": "heading", "bold": True}]}]
                + BASE["blocks"][1:])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    assert texts(result)[1] == "New heading"
    kinds = [next(iter(r)) for r in result["requests"]]
    # One updateTextStyle per run, the plain one included: text written from nothing
    # inherits the styling of the character in front of it, so every managed field is
    # named, or a block moved under an underlined heading comes out underlined.
    assert kinds == ["insertText", "deleteParagraphBullets", "updateParagraphStyle",
                     "updateTextStyle", "updateTextStyle"]
    insert = result["requests"][0]["insertText"]
    assert insert["text"] == "New heading\n"
    assert insert["location"]["index"] == BASE["blocks"][1]["span"][0]
    assert (result["requests"][2]["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"]
            == "HEADING_2")
    plain, bold = (r["updateTextStyle"] for r in result["requests"][3:])
    assert plain["textStyle"] == {} and "bold" in plain["fields"].split(",")
    assert bold["textStyle"] == {"bold": True}
    assert set(plain["fields"].split(",")) == set(doc_merge.MANAGED)


def test_a_list_item_gets_its_bullets_after_its_styling():
    ours = live(BASE["blocks"] + [{"kind": "item", "level": 0, "ordered": True,
                                   "key": "item:step", "runs": [{"text": "step one"}]}])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    kinds = [next(iter(r)) for r in result["requests"]]
    assert kinds == ["insertText", "updateParagraphStyle", "updateTextStyle",
                     "createParagraphBullets"]
    # A block that is not a list item says so, or it joins the list it was written into.
    assert "deleteParagraphBullets" not in kinds
    assert (result["requests"][-1]["createParagraphBullets"]["bulletPreset"]
            == "NUMBERED_DECIMAL_ALPHA_ROMAN")


def test_a_block_appended_at_the_end_puts_its_paragraph_break_first():
    ours = live(BASE["blocks"] + [para("p:delta", "delta seven")])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    tail = BASE["blocks"][-1]["span"][1] - 1
    kinds = [next(iter(r)) for r in result["requests"]]
    assert kinds == ["insertText", "deleteParagraphBullets", "updateParagraphStyle",
                     "updateTextStyle"]
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


def test_a_read_equation_takes_its_latex_from_the_base_and_the_merge_writes_nothing():
    """`documents.get` says nothing of an equation; its LaTeX came from the export when
    the base was read. Without it back, the block would read as changed."""
    eq = {"text": "", "frozen": True, "chip": "equation", "width": 10}
    theirs = live([{"kind": "paragraph", "key": "p:e", "runs": [{"text": "So "}, dict(eq)]}])
    base = live([{"kind": "paragraph", "key": "p:e",
                  "runs": [{"text": "So "}, dict(eq, text="E=m{c}^{2}")]}])
    doc_merge.restore_unreadable(theirs, base, base)
    assert theirs["blocks"][0]["runs"][1]["text"] == "E=m{c}^{2}"
    assert doc_merge.plan(base, base, theirs)["requests"] == []
    # A block that holds another number of equations than the base's is not guessed at.
    other = live([{"kind": "paragraph", "key": "p:e", "runs": [dict(eq), dict(eq)]}])
    doc_merge.restore_unreadable(other, base)
    assert [r["text"] for r in other["blocks"][0]["runs"]] == ["", ""]


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
                     "updateParagraphStyle", "updateTextStyle"]
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
                     "updateTextStyle", "deleteContentRange"]
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
                     "updateTextStyle", "createParagraphBullets"]
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


# ---------------------------------------------------------------- tabs

def tab(ident, title, *words, **rest):
    return {"tab": ident, "title": title, "blocks": [para(f"p:{w}", w) for w in words]} | rest


def tabbed(*tabs):
    return {"title": "t", "blocks": [para("p:front", "front")], "tabs": list(tabs)}


def test_a_tab_request_carries_its_tab_in_every_location_and_range():
    requests = [{"insertText": {"location": {"index": 3}, "text": "x"}},
                {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 2}}},
                {"insertTable": {"rows": 1, "columns": 2, "endOfSegmentLocation": {}}},
                {"insertTableRow": {"tableCellLocation": {"tableStartLocation": {"index": 7},
                                                          "rowIndex": 0, "columnIndex": 0}}}]
    assert doc_merge.on_tab(requests, None) is requests
    sent = doc_merge.on_tab(requests, "t.5")
    assert sent[0]["insertText"]["location"] == {"index": 3, "tabId": "t.5"}
    assert sent[1]["deleteContentRange"]["range"]["tabId"] == "t.5"
    assert sent[2]["insertTable"]["endOfSegmentLocation"] == {"tabId": "t.5"}
    cell = sent[3]["insertTableRow"]["tableCellLocation"]
    assert cell["tableStartLocation"] == {"index": 7, "tabId": "t.5"} and "tabId" not in cell
    assert "tabId" not in requests[0]["insertText"]["location"]      # the plan is untouched


def test_tabs_pair_by_id_and_the_source_can_add_rename_and_delete_them():
    base = tabbed(tab("t.1", "Notes", "a"), tab("t.2", "Old", "b"), tab("t.3", "Busy", "c"))
    ours = tabbed(tab("t.1", "Notes, renamed", "a", "more"), tab(None, "Appendix", "z"))
    theirs = tabbed(tab("t.1", "Notes", "a"), tab("t.2", "Old", "b"),
                    tab("t.3", "Busy", "c", "reader's"), tab("t.4", "Reader's own", "r"))
    out = doc_merge.pair_tabs(base, ours, theirs)
    assert [(t, mine["title"], was["title"]) for t, mine, was in out["pairs"]] == [
        ("t.1", "Notes, renamed", "Notes")]
    assert [p["title"] for p in out["create"]] == ["Appendix"]
    assert out["requests"] == [
        {"updateDocumentTabProperties": {"tabProperties": {"tabId": "t.1",
                                                           "title": "Notes, renamed"},
                                         "fields": "title"}},
        {"deleteTab": {"tabId": "t.2"}}]
    # t.3: deleted in the source, written in by the reader — theirs. t.4: theirs, and read.
    assert any("'Busy'" in n and "kept" in n for n in out["notes"])


def test_a_tab_both_sides_renamed_keeps_the_document_title():
    base = tabbed(tab("t.1", "Notes", "a"))
    out = doc_merge.pair_tabs(base, tabbed(tab("t.1", "Mine", "a")),
                              tabbed(tab("t.1", "Theirs", "a")))
    assert out["requests"] == [] and "renamed on both sides" in out["notes"][0]


def test_a_tab_the_reader_deleted_stays_deleted_and_a_source_edit_to_it_is_said():
    base = tabbed(tab("t.1", "Notes", "a"))
    out = doc_merge.pair_tabs(base, tabbed(tab("t.1", "Notes", "a", "b")), tabbed())
    assert out["pairs"] == [] and out["create"] == [] and out["requests"] == []
    assert "deleted in the document" in out["notes"][0]
    quiet = doc_merge.pair_tabs(base, tabbed(tab("t.1", "Notes", "a")), tabbed())
    assert quiet["notes"] == []


def test_a_new_tab_left_empty_by_a_sync_that_died_is_taken_not_made_twice():
    ours = tabbed(tab(None, "Appendix", "z"))
    out = doc_merge.pair_tabs(tabbed(), ours, tabbed(tab("t.7", "Appendix")))
    assert out["create"] == [] and out["pairs"][0][0] == "t.7"
    busy = doc_merge.pair_tabs(tabbed(), tabbed(tab(None, "Appendix", "z")),
                               tabbed(tab("t.7", "Appendix", "reader's")))
    assert [p["title"] for p in busy["create"]] == ["Appendix"]


# ---------------------------------------------------------------- faces and measures

def styled_run(text, **rest):
    return {"text": text, **rest}


def test_a_face_and_a_size_the_source_set_are_written_as_themselves():
    base = live([para("p:s", "one two")])
    ours = live([{"kind": "paragraph", "key": "p:s", "runs": [
        styled_run("one ", font="Roboto Mono", fontsize=9), styled_run("two")]}])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    style = [r["updateTextStyle"] for r in result["requests"] if "updateTextStyle" in r][0]
    assert style["textStyle"]["weightedFontFamily"] == {"fontFamily": "Roboto Mono"}
    assert style["textStyle"]["fontSize"] == {"magnitude": 9.0, "unit": "PT"}


def test_code_in_an_older_file_still_means_the_one_face_it_meant():
    assert doc_merge._text_style({"text": "x", "code": True})["weightedFontFamily"] == {
        "fontFamily": doc_ir.CODE_FAMILY}
    # A face of its own wins over the tag, so a file that says both says the face.
    both = doc_merge._text_style({"text": "x", "code": True, "font": "Consolas"})
    assert both["weightedFontFamily"] == {"fontFamily": "Consolas"}


def test_small_caps_is_a_mark_like_bold():
    assert doc_merge._text_style({"text": "x", "smallcaps": True})["smallCaps"] is True
    assert "smallCaps" in doc_merge.MANAGED


def test_a_raised_run_is_written_and_owned_like_the_rest_of_the_styling():
    """A superscript is content — the 2 of x², of a footnote marker, of a citation —
    and a block written again from nothing used to come back flat on the baseline
    with nothing saying so. The file can say it (`<sup>`), a read can see it
    (`baselineOffset`), which is exactly the bar for `MANAGED`."""
    assert "baselineOffset" in doc_merge.MANAGED
    for spelling, api in (("super", "SUPERSCRIPT"), ("sub", "SUBSCRIPT"),
                          ("none", "NONE")):
        assert doc_merge._text_style({"text": "2", "script": spelling})[
            "baselineOffset"] == api

    base = live([para("p:s", "x2 and more")])
    ours = live([{"kind": "paragraph", "key": "p:s", "runs": [
        styled_run("x"), styled_run("2", script="super"), styled_run(" and more")]}])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    styles = [r["updateTextStyle"] for r in result["requests"] if "updateTextStyle" in r]
    raised = [s for s in styles if s["textStyle"].get("baselineOffset")]
    assert len(raised) == 1
    assert raised[0]["textStyle"]["baselineOffset"] == "SUPERSCRIPT"
    assert raised[0]["range"] == {"startIndex": 2, "endIndex": 3}
    # Every managed field is named on every run of a restyle, so the runs beside it
    # are told they are *not* raised rather than left to inherit the one that is.
    assert all("baselineOffset" in s["fields"] for s in styles)


def test_a_superscript_the_source_took_away_is_named_with_no_value_so_it_goes():
    base = live([{"kind": "paragraph", "key": "p:s", "runs": [
        styled_run("x"), styled_run("2", script="super")]}])
    ours = live([para("p:s", "x2")])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    styles = [r["updateTextStyle"] for r in result["requests"] if "updateTextStyle" in r]
    assert styles and all("baselineOffset" not in s["textStyle"] for s in styles)
    assert all("baselineOffset" in s["fields"] for s in styles)


def test_a_raised_run_the_import_flattened_is_written_by_the_settle():
    """Drive's HTML importer does carry `<sup>`, but the same pass that repairs small
    caps has to ask: a run the plan raises and the read-back does not is a run to
    raise, and it is the only way a `data-script` file's word gets its place back."""
    planned = [{"kind": "paragraph", "key": "p:s", "runs": [
        styled_run("H"), styled_run("2", script="sub"), styled_run("O and x"),
        styled_run("2", script="super")]}]
    read = live([{"kind": "paragraph", "key": "p:s", "runs": [
        styled_run("H2O and x2")]}])
    assert doc_merge.adopt_keys(read, planned) == 0
    requests = [r["updateTextStyle"] for r in doc_merge.tidy_requests(read)]
    assert [(r["range"]["startIndex"], r["range"]["endIndex"],
             r["textStyle"]["baselineOffset"]) for r in requests] == [
        (10, 11, "SUPERSCRIPT"), (2, 3, "SUBSCRIPT")]
    assert all(r["fields"] == "baselineOffset" for r in requests)


def test_the_measurements_of_a_paragraph_are_written_and_named():
    base = live([para("p:s", "one two")])
    ours = live([para("p:s", "one two", indent=36.0, line_spacing=1.5,
                      space_above=12.0, shading="#fff2cc")])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    style = [r["updateParagraphStyle"] for r in result["requests"]
             if "updateParagraphStyle" in r][0]
    assert style["paragraphStyle"]["indentStart"] == {"magnitude": 36.0, "unit": "PT"}
    assert style["paragraphStyle"]["lineSpacing"] == 150.0
    assert style["paragraphStyle"]["shading"] == {"backgroundColor": {"color": {"rgbColor": {
        "red": 1.0, "green": 242 / 255, "blue": 204 / 255}}}}
    assert style["fields"] == ",".join(doc_merge.MANAGED_PARAGRAPH)


def test_a_measurement_the_source_dropped_is_named_with_no_value_so_it_goes():
    base = live([para("p:s", "one two", indent=36.0, align="center")])
    ours = live([para("p:s", "one two")])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    style = [r["updateParagraphStyle"] for r in result["requests"]
             if "updateParagraphStyle" in r][0]
    assert "indentStart" not in style["paragraphStyle"]     # named and unset: back to default
    assert "indentStart" in style["fields"]
    # The alignment goes exactly the same way, and is the reason a heading a theme
    # centres is not left-aligned by a source edit that never mentioned alignment.
    assert "alignment" not in style["paragraphStyle"]
    assert "alignment" in style["fields"]
    # And the merged block itself no longer carries what the source took away.
    assert "indent" not in result["blocks"][0] and "align" not in result["blocks"][0]


def test_an_alignment_somebody_chose_is_still_written_with_a_value():
    """The other half: leaving `alignment` unset means "whatever the named style
    says", so a block that does say something must not be left to it. `left` is a
    real answer — a heading the theme centres and the reader pulled back to the
    margin reads as `align: left` and has to be written as START."""
    for align, want in (("left", "START"), ("center", "CENTER")):
        base = live([para("p:s", "one two")])
        ours = live([para("p:s", "one two", align=align)])
        result = doc_merge.plan(base, ours, live(base["blocks"]))
        style = [r["updateParagraphStyle"] for r in result["requests"]
                 if "updateParagraphStyle" in r][0]
        assert style["paragraphStyle"]["alignment"] == want


def test_an_items_indents_are_left_to_the_bullet_preset():
    base = live([{"kind": "item", "key": "i:a", "level": 0, "ordered": False,
                  "runs": [{"text": "an item"}]}])
    ours = live([{"kind": "item", "key": "i:a", "level": 0, "ordered": False,
                  "line_spacing": 2.0, "runs": [{"text": "an item"}]}])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    style = [r["updateParagraphStyle"] for r in result["requests"]
             if "updateParagraphStyle" in r][0]
    assert "indentStart" not in style["fields"] and "indentFirstLine" not in style["fields"]
    assert style["paragraphStyle"]["lineSpacing"] == 200.0


def test_a_paragraph_only_the_document_dressed_is_left_alone():
    """The reader's own spacing is not a source change, so nothing is written."""
    base = live([para("p:s", "one two")])
    theirs = live([para("p:s", "one two", line_spacing=1.5, shading="#eeeeee")])
    result = doc_merge.plan(base, live(base["blocks"]), theirs)
    assert result["requests"] == []
    assert result["blocks"][0]["line_spacing"] == 1.5


# ------------------------------------------------- what no import can carry

def test_what_the_import_drops_is_written_by_the_settle_instead():
    """A push imports HTML, which carries neither shading nor small caps; the
    read-back is compared with the plan and `tidy_requests` writes the difference."""
    planned = [para("p:s", "one two", shading="#fff2cc", space_above=6.0),
               {"kind": "paragraph", "key": "p:t", "runs": [
                   styled_run("caps", smallcaps=True), styled_run(" and plain")]}]
    read = live([para("p:s", "one two"),
                 {"kind": "paragraph", "key": "p:t", "runs": [
                     styled_run("caps"), styled_run(" and plain")]}])
    assert doc_merge.adopt_keys(read, planned) == 0   # every block already has its key
    assert read["blocks"][0]["unimported"]["paragraph"] == {"shading": "#fff2cc",
                                                            "space_above": 6.0}
    requests = doc_merge.tidy_requests(read)
    style = requests[0]["updateParagraphStyle"]
    assert style["fields"] == "shading,spaceAbove"
    assert style["range"] == {"startIndex": 1, "endIndex": 9}
    caps = requests[1]["updateTextStyle"]
    assert caps == {"range": {"startIndex": 9, "endIndex": 13},
                    "textStyle": {"smallCaps": True}, "fields": "smallCaps"}


def test_a_document_that_already_says_it_is_written_again_for_nothing():
    planned = [para("p:s", "one", shading="#fff2cc")]
    read = live([para("p:s", "one", shading="#fff2cc")])
    doc_merge.adopt_keys(read, planned)
    assert doc_merge.tidy_requests(read) == []


def test_styling_the_plan_does_not_ask_for_is_never_taken_away():
    """In a read, "absent" is also what a reader who took the styling off looks
    like, and a settle must never undo that."""
    planned = [para("p:s", "one")]
    read = live([para("p:s", "one", shading="#fff2cc")])
    doc_merge.adopt_keys(read, planned)
    assert doc_merge.tidy_requests(read) == []


def test_small_caps_is_only_carried_where_the_words_came_through_whole():
    """An equation is several index units where a character is one: anywhere but a
    block of plain words an offset would be a guess."""
    planned = [{"kind": "paragraph", "key": "p:s", "runs": [
        styled_run("caps", smallcaps=True),
        {"chip": "equation", "frozen": True, "text": "x^2"}]}]
    read = live([{"kind": "paragraph", "key": "p:s", "runs": [
        styled_run("caps"), {"chip": "equation", "frozen": True, "text": "x^2"}]}])
    doc_merge.adopt_keys(read, planned)
    assert "unimported" not in read["blocks"][0]


def test_a_title_the_importer_flattened_is_put_back_by_the_settle():
    """`class="title"` reaches nothing in Drive's importer, so a pushed file's Title
    and Subtitle come back as body text. The settle writes the named style, exactly
    as it writes the shading the import drops."""
    planned = [{"kind": "title", "key": "title:t", "runs": [styled_run("The report")]},
               {"kind": "subtitle", "key": "subtitle:s", "runs": [styled_run("A draft")]},
               para("p:b", "one two")]
    read = live([para("title:t", "The report"), para("subtitle:s", "A draft"),
                 para("p:b", "one two")])
    doc_merge.adopt_keys(read, planned)
    assert read["blocks"][0]["unimported"]["named"] == "TITLE"
    assert read["blocks"][1]["unimported"]["named"] == "SUBTITLE"
    assert "unimported" not in read["blocks"][2]   # body text is what it already is
    styles = [r["updateParagraphStyle"] for r in doc_merge.tidy_requests(read)]
    assert [s["paragraphStyle"]["namedStyleType"] for s in styles] == ["TITLE", "SUBTITLE"]
    assert [s["fields"] for s in styles] == ["namedStyleType", "namedStyleType"]
    assert [s["range"] for s in styles] == [{"startIndex": 1, "endIndex": 12},
                                            {"startIndex": 12, "endIndex": 20}]


def test_a_bullet_a_delete_took_off_is_put_back_by_the_settle():
    """A list item and a plain paragraph are both NORMAL_TEXT, so the named style is
    blind to exactly the thing a delete takes away most often: Docs merges two
    paragraphs keeping the first one's style, so the item under a deleted paragraph
    comes back with no bullet, and the settle then wrote that plain paragraph into
    the file — the source's own list, quietly one item shorter."""
    planned = [{"kind": "item", "key": "item:a", "level": 0,
                "runs": [styled_run("lantern")]}]
    read = live([para("item:a", "lantern")])
    doc_merge.adopt_keys(read, planned)
    assert read["blocks"][0]["unimported"]["bullet"] == "unordered"
    assert doc_merge.tidy_requests(read) == [{"createParagraphBullets": {
        "range": {"startIndex": 1, "endIndex": 9},
        "bulletPreset": doc_merge.BULLETS[False]}}]
    # And the other way: a bullet the write put on a block that is no item.
    planned = [para("p:a", "lantern")]
    read = live([{"kind": "item", "key": "p:a", "level": 0,
                  "runs": [styled_run("lantern")]}])
    doc_merge.adopt_keys(read, planned)
    assert read["blocks"][0]["unimported"]["bullet"] == "none"
    assert doc_merge.tidy_requests(read) == [{"deleteParagraphBullets": {
        "range": {"startIndex": 1, "endIndex": 9}}}]


def test_a_block_whose_shape_the_write_changed_is_still_adopted_by_its_words():
    """`adopt_keys` matched shape and words together, so the very blocks a write
    mangles — a delete hands the block after it the shape of the one that went —
    were the ones that could never be adopted. And the repair that would put the
    shape back is keyed by the key this restores, so the two held each other up."""
    planned = [para("p:above", "kept"),
               {"kind": "item", "key": "item:a", "level": 0, "runs": [styled_run("lantern")]}]
    read = live([para("p:above", "kept"), para(None, "lantern")])
    assert doc_merge.adopt_keys(read, planned) == 1
    assert read["blocks"][1]["key"] == "item:a"
    # Never a guess: two blocks that say the same thing say nothing about which is which.
    planned = [{"kind": "item", "key": "item:a", "level": 0, "runs": [styled_run("same")]},
               {"kind": "item", "key": "item:b", "level": 0, "runs": [styled_run("same")]}]
    read = live([para(None, "same"), para(None, "same")])
    assert doc_merge.adopt_keys(read, planned) == 0


def test_a_later_tabs_blocks_are_adopted_before_anything_keys_them():
    """`doc_ir.key_blocks` recurses into the tabs, so adopting and keying one part at
    a time let the first part's keying name every later tab's blocks after their
    words — and `adopt_keys`, reaching that tab afterwards, found them keyed and left
    them alone. Here the write took a block's named range with it, so the read-back
    has no key and the plan is the only thing that knows one."""
    live = {"blocks": [para("p:first", "one two")],
            "tabs": [{"tab": "t.1", "blocks": [dict(para("", "quartz count"),
                                                    key=None)]}]}
    live["tabs"][0]["blocks"][0].pop("key")
    doc_merge.settle_keys(live, {"t.1": [para("t:year", "quartz count")]})
    assert [b["key"] for b in live["tabs"][0]["blocks"]] == ["t:year"]


def test_a_block_no_plan_knows_is_still_keyed_from_its_words():
    """The second pass is not optional: a block the merge never planned — one a
    reader added — has to come out of the settle with a key of some kind."""
    live = {"blocks": [dict(para("", "a reader wrote this"))], "tabs": []}
    live["blocks"][0].pop("key")
    doc_merge.settle_keys(live, {})
    assert live["blocks"][0]["key"] == "paragraph:a-reader-wrote-this"


def test_a_heading_the_reader_demoted_is_left_demoted():
    """The merged plan is document-wins, so a named style only reaches the settle
    when the document never chose it. A reader who made a heading body text has
    chosen, and the source did not say otherwise."""
    base = live([{"kind": "heading", "level": 1, "key": "h:t", "runs": [styled_run("Goals")]}])
    theirs = live([para("h:t", "Goals")])
    result = doc_merge.plan(base, live(base["blocks"]), theirs)
    doc_merge.adopt_keys(theirs, result["blocks"])
    assert doc_merge.tidy_requests(theirs) == []


def test_a_parent_tab_the_source_deleted_is_kept_while_a_child_is_still_wanted():
    base = tabbed(tab("t.1", "Parent", "a"), tab("t.2", "Child", "b", parent="t.1"))
    ours = tabbed(tab("t.2", "Child", "b", parent="t.1"))
    theirs = tabbed(tab("t.1", "Parent", "a"), tab("t.2", "Child", "b", parent="t.1"))
    out = doc_merge.pair_tabs(base, ours, theirs)
    assert out["requests"] == [] and "keeps tabs inside it" in out["notes"][0]
    assert doc_merge.add_tab_request({"title": "New", "parent": "t.1"}, {"t.1"}) == {
        "addDocumentTab": {"tabProperties": {"title": "New", "parentTabId": "t.1"}}}
