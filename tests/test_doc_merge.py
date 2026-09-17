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
    # 1 (body start) + 4 ("due ") puts the chip at 5, the space at 6 and "soon" at 7.
    # Believing the display text would have put it at 18.
    assert insert["location"]["index"] == 7
    assert insert["text"] == "later"


def test_a_source_that_would_rewrite_a_chip_is_refused():
    base = live([chipped("due ", " please")])
    ours = live([{"kind": "paragraph", "key": "p:due",
                  "runs": [{"text": "due tomorrow please"}]}])
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    assert result["requests"] == []
    assert "left alone" in result["notes"][0]
    assert doc_merge.frozen_of(result["blocks"][0]) == (("date", "Sep 25, 2026",
                                                        "2026-09-25T12:00:00Z"),)


# ---------------------------------------------------------------- add, delete, order

def test_a_block_the_source_added_is_inserted_with_its_styling():
    ours = live(BASE["blocks"][:1]
                + [{"kind": "heading", "level": 2, "key": "heading:new", "runs": [
                    {"text": "New "}, {"text": "heading", "bold": True}]}]
                + BASE["blocks"][1:])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    assert texts(result)[1] == "New heading"
    kinds = [next(iter(r)) for r in result["requests"]]
    assert kinds[:3] == ["insertText", "updateParagraphStyle", "updateTextStyle"]
    insert = result["requests"][0]["insertText"]
    assert insert["text"] == "New heading\n"
    assert insert["location"]["index"] == BASE["blocks"][1]["span"][0]
    assert (result["requests"][1]["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"]
            == "HEADING_2")


def test_a_list_item_gets_its_bullets_after_its_styling():
    ours = live(BASE["blocks"] + [{"kind": "item", "level": 0, "ordered": True,
                                   "key": "item:step", "runs": [{"text": "step one"}]}])
    result = doc_merge.plan(BASE, ours, live(BASE["blocks"]))
    kinds = [next(iter(r)) for r in result["requests"]]
    assert kinds == ["insertText", "updateParagraphStyle", "createParagraphBullets"]
    assert (result["requests"][-1]["createParagraphBullets"]["bulletPreset"]
            == "NUMBERED_DECIMAL_ALPHA_ROMAN")


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


def test_keys_are_inherited_when_the_words_changed():
    ours = {"title": "t", "blocks": [
        {"kind": "paragraph", "runs": [{"text": "alpha one two"}]},
        {"kind": "paragraph", "runs": [{"text": "bravo three four but rewritten a lot"}]},
        {"kind": "paragraph", "runs": [{"text": "charlie five six"}]}]}
    doc_merge.inherit_keys(BASE, ours)
    assert [b["key"] for b in ours["blocks"]] == ["p:alpha", "p:bravo", "p:charlie"]
