"""Offline tests for the Docs IR and the canonical HTML dialect (docs/google-docs.md).

No Google calls: the live-document fixtures are the shapes `documents.get` really
returned in `tools/probe_docs_chips.py`, trimmed to what the reader looks at.
"""

import pytest

from beamer2slides import doc_ir


def strip(ir: dict) -> dict:
    """The IR without what only a live document has, so both sides compare."""
    def clean(block):
        block = {k: v for k, v in block.items() if k not in ("span", "key")}
        if "runs" in block:
            block["runs"] = [{k: v for k, v in r.items() if k != "width"} for r in block["runs"]]
        if "rows" in block:
            block["rows"] = [[[clean(b) for b in cell] for cell in row] for row in block["rows"]]
        return block
    return {"title": ir["title"], "blocks": [clean(b) for b in ir["blocks"]]}


RICH = {
    "title": "A probe document",
    "blocks": [
        {"kind": "heading", "level": 1, "runs": [{"text": "Heading one"}]},
        {"kind": "paragraph", "runs": [
            {"text": "plain "},
            {"text": "bold", "bold": True},
            {"text": " and "},
            {"text": "italic", "italic": True},
            {"text": " and "},
            {"text": "both", "bold": True, "italic": True},
            {"text": "."},
        ]},
        {"kind": "paragraph", "align": "center", "runs": [
            {"text": "centred with "},
            {"text": "a link", "link": "https://example.com/x?a=1&b=2"},
            {"text": " and "},
            {"text": "red", "color": "#cc0000"},
            {"text": " and "},
            {"text": "marked", "highlight": "#ffff00"},
        ]},
        {"kind": "paragraph", "runs": [
            {"text": "struck", "strike": True},
            {"text": " "},
            {"text": "under", "underline": True},
            {"text": " "},
            {"text": "mono", "code": True},
        ]},
        {"kind": "item", "level": 0, "ordered": False, "runs": [{"text": "first bullet"}]},
        {"kind": "item", "level": 1, "ordered": False, "runs": [{"text": "nested bullet"}]},
        {"kind": "item", "level": 0, "ordered": False, "runs": [{"text": "third bullet"}]},
        {"kind": "item", "level": 0, "ordered": True, "runs": [{"text": "step one"}]},
        {"kind": "item", "level": 0, "ordered": True, "runs": [{"text": "step two"}]},
        {"kind": "paragraph", "runs": [
            {"text": "Due "},
            {"chip": "date", "frozen": True, "text": "Sep 25, 2026",
             "value": "2026-09-25T12:00:00Z", "format": "DATE_FORMAT_MONTH_DAY_YEAR_ABBREVIATED",
             "locale": "en"},
            {"text": ", owner "},
            {"chip": "person", "frozen": True, "text": "Boris Arnoux",
             "value": "person@example.com"},
            {"text": "."},
        ]},
        {"kind": "table", "rows": [
            [[{"kind": "paragraph", "runs": [{"text": "a"}]}],
             [{"kind": "paragraph", "runs": [{"text": "b"}]}]],
            [[{"kind": "paragraph", "runs": [{"text": "c"}]}],
             [{"kind": "paragraph", "runs": [{"text": "d"}]}]],
        ]},
        {"kind": "paragraph", "runs": [
            {"chip": "image", "frozen": True, "text": "", "src": "figures/plot.png",
             "alt": "a plot & its axes", "size": [60, 40], "value": "kix.abc"}]},
        {"kind": "paragraph", "runs": [
            {"text": "see "}, {"chip": "image", "frozen": True, "text": "", "src": "b.png"},
            {"text": " here"}]},
        {"kind": "paragraph", "runs": [{"text": "The end."}]},
    ],
}


def test_html_round_trip_is_the_identity():
    assert strip(doc_ir.from_html(doc_ir.to_html(RICH))) == strip(RICH)


def test_html_is_stable_on_a_second_pass():
    once = doc_ir.to_html(RICH)
    assert doc_ir.to_html(doc_ir.from_html(once)) == once


def test_a_list_item_carries_its_key_in_the_file_too():
    ir = doc_ir.key_blocks({"title": "", "blocks": [
        {"kind": "item", "level": 0, "ordered": False, "runs": [{"text": "an item"}]}]})
    html = doc_ir.to_html(ir)
    assert '<li id="item:an-item">an item</li>' in html
    assert doc_ir.from_html(html)["blocks"][0]["key"] == "item:an-item"


def test_one_block_per_line_so_diffs_read():
    html = doc_ir.to_html(RICH)
    assert "<h1>Heading one</h1>" in html.splitlines()
    assert any(line.strip() == "<li>nested bullet</li>" for line in html.splitlines())


def test_a_chip_keeps_its_value_through_the_file():
    html = doc_ir.to_html(RICH)
    assert 'data-chip="date"' in html and 'data-value="2026-09-25T12:00:00Z"' in html
    chips = [r for b in doc_ir.from_html(html)["blocks"]
             for r in b.get("runs", []) if r.get("frozen")]
    assert [c["chip"] for c in chips] == ["date", "person", "image", "image"]
    assert chips[0]["value"] == "2026-09-25T12:00:00Z"
    assert chips[1]["text"] == "Boris Arnoux"


def test_a_picture_is_an_img_that_names_its_file_and_its_object():
    html = doc_ir.to_html(RICH)
    assert ('<p><img src="figures/plot.png" alt="a plot &amp; its axes" width="60" height="40" '
            'data-object="kix.abc"></p>') in html.splitlines()
    # One outside any paragraph is a paragraph of its own.
    ir = doc_ir.from_html("<body><img src='x.png' width='30px' height='20'><p>after</p></body>")
    assert ir["blocks"][0]["runs"] == [{"chip": "image", "frozen": True, "text": "",
                                        "src": "x.png", "size": [30, 20]}]
    assert doc_ir.key_blocks(ir)["blocks"][0]["key"] == "paragraph:x"


# ---------------------------------------------------------------- live document

def test_a_picture_in_the_document_reads_with_its_size_alt_and_url():
    doc = {"body": {"content": [paragraph("x", 1, elements=[
        {"startIndex": 1, "endIndex": 2, "inlineObjectElement": {"inlineObjectId": "kix.1"}},
        {"startIndex": 2, "endIndex": 3, "textRun": {"content": "\n"}}])]},
        "inlineObjects": {"kix.1": {"inlineObjectProperties": {"embeddedObject": {
            "description": "the alt", "size": {"width": {"magnitude": 45, "unit": "PT"},
                                               "height": {"magnitude": 30, "unit": "PT"}},
            "imageProperties": {"contentUri": "https://lh7/x"}}}}}}
    run = doc_ir.from_document(doc)["blocks"][0]["runs"][0]
    assert run == {"chip": "image", "frozen": True, "text": "", "value": "kix.1", "width": 1,
                   "size": [60, 40], "alt": "the alt", "uri": "https://lh7/x"}
    # The URL dies within the hour and never reaches the file.
    assert "lh7" not in doc_ir.to_html({"title": "", "blocks": [{"kind": "paragraph",
                                                                  "runs": [run]}]})

def paragraph(text, start, style=None, bullet=None, elements=None):
    end = start + len(text) + 1
    return {"startIndex": start, "endIndex": end, "paragraph": {
        "elements": elements or [{"startIndex": start, "endIndex": end,
                                  "textRun": {"content": text + "\n", "textStyle": {}}}],
        "paragraphStyle": style or {"namedStyleType": "NORMAL_TEXT"},
        **({"bullet": bullet} if bullet else {})}}


LIVE = {
    "title": "live",
    "lists": {"kix.list1": {"listProperties": {"nestingLevels": [
        {"glyphSymbol": "●"}, {"glyphSymbol": "○"}]}},
        "kix.list2": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}}},
    "body": {"content": [
        {"startIndex": 0, "endIndex": 1, "sectionBreak": {"sectionStyle": {}}},
        paragraph("A heading", 1, {"namedStyleType": "HEADING_1"}),
        paragraph("centred", 11, {"namedStyleType": "NORMAL_TEXT", "alignment": "CENTER"}),
        paragraph("a bullet", 19, bullet={"listId": "kix.list1", "nestingLevel": 0}),
        paragraph("deeper", 28, bullet={"listId": "kix.list1", "nestingLevel": 1}),
        paragraph("a step", 35, bullet={"listId": "kix.list2", "nestingLevel": 0}),
        {"startIndex": 42, "endIndex": 72, "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [
                {"startIndex": 42, "endIndex": 46,
                 "textRun": {"content": "Due ", "textStyle": {"bold": True}}},
                {"startIndex": 46, "endIndex": 47, "dateElement": {
                    "dateId": "kix.d1", "dateElementProperties": {
                        "timestamp": "2026-09-25T12:00:00Z", "locale": "en",
                        "dateFormat": "DATE_FORMAT_MONTH_DAY_YEAR_ABBREVIATED",
                        "displayText": "Sep 25, 2026"}}},
                {"startIndex": 47, "endIndex": 48},  # a dropdown chip: no content key at all
                {"startIndex": 48, "endIndex": 49, "person": {
                    "personId": "kix.p1",
                    "personProperties": {"email": "a@b.c", "name": "A B"}}},
                {"startIndex": 49, "endIndex": 62, "equation": {}},
                {"startIndex": 62, "endIndex": 72, "textRun": {
                    "content": f" tail{doc_ir.OBJECT_SENTINEL}end\n", "textStyle": {}}},
            ]}},
    ]},
}


def test_reads_headings_alignment_and_both_kinds_of_list():
    ir = doc_ir.from_document(LIVE)
    kinds = [(b["kind"], b.get("level"), b.get("ordered")) for b in ir["blocks"]]
    assert kinds[:5] == [("heading", 1, None), ("paragraph", None, None),
                         ("item", 0, False), ("item", 1, False), ("item", 0, True)]
    assert ir["blocks"][1]["align"] == "center"


def test_every_chip_becomes_a_frozen_run():
    ir = doc_ir.from_document(LIVE)
    runs = ir["blocks"][-1]["runs"]
    assert [r.get("chip") for r in runs] == [
        None, "date", "unknown", "person", "equation", None, "object", None]
    assert all(r["frozen"] for r in runs if r.get("chip"))
    assert runs[1]["value"] == "2026-09-25T12:00:00Z"
    assert runs[3]["value"] == "a@b.c"
    # The dropdown and the equation say nothing but that they are there.
    assert runs[2]["text"] == "" and runs[4]["text"] == ""


def test_a_block_keeps_the_span_it_came_from():
    ir = doc_ir.from_document(LIVE)
    assert ir["blocks"][0]["span"] == [1, 11]
    assert ir["blocks"][-1]["span"] == [42, 72]


def test_a_run_knows_how_many_index_units_it_holds():
    """A chip is one unit however long its words look; the equation was thirteen."""
    runs = doc_ir.from_document(LIVE)["blocks"][-1]["runs"]
    assert [r["width"] for r in runs] == [4, 1, 1, 1, 13, 5, 1, 3]
    assert runs[1]["text"] == "Sep 25, 2026" and runs[1]["width"] == 1
    # The widths account for every index unit of the block, newline included.
    block = doc_ir.from_document(LIVE)["blocks"][-1]
    assert sum(r["width"] for r in block["runs"]) == block["span"][1] - block["span"][0] - 1


IMPORTED_LIST = {
    "title": "imported",
    # What Drive's HTML import really builds: the same, glyph-less definition for a
    # <ul> and an <ol> alike, though the editor renders them differently.
    "lists": {"kix.list.1": {"listProperties": {"nestingLevels": [
        {"glyphType": "GLYPH_TYPE_UNSPECIFIED", "startNumber": 1}]}}},
    "body": {"content": [
        paragraph("an item", 1, bullet={"listId": "kix.list.1", "nestingLevel": 0})]},
}


def test_an_imported_list_cannot_say_whether_it_is_numbered():
    block = doc_ir.from_document(IMPORTED_LIST)["blocks"][0]
    assert block["kind"] == "item" and block["ordered"] is None


def test_a_linked_run_drops_the_blue_underline_docs_paints_for_free():
    style = doc_ir._style_of({"link": {"url": "https://example.com/"}, "underline": True,
                              "foregroundColor": {"color": {"rgbColor": {"blue": 0.93333334}}}})
    assert style == {"link": "https://example.com/"}
    # A colour somebody chose is not free styling, and stays.
    green = doc_ir._style_of({"link": {"url": "u"}, "underline": True,
                              "foregroundColor": {"color": {"rgbColor": {"green": 1.0}}}})
    assert green == {"link": "u", "underline": True, "color": "#00ff00"}


def test_the_trailing_newline_is_the_paragraph_not_its_text():
    ir = doc_ir.from_document(LIVE)
    assert doc_ir.runs_text(ir["blocks"][0]["runs"]) == "A heading"


TABBED = {
    "title": "tabbed",
    "tabs": [
        {"tabProperties": {"tabId": "t.0", "title": "Tab 1", "index": 0},
         "documentTab": {"body": {"content": [paragraph("first tab", 1)]}, "lists": {}}},
        {"tabProperties": {"tabId": "t.a", "title": "Tab 2", "index": 1},
         "documentTab": {"body": {"content": [paragraph("second tab", 1)]}, "lists": {}},
         "childTabs": [
             {"tabProperties": {"tabId": "t.b", "title": "Tab 3", "parentTabId": "t.a"},
              "documentTab": {"body": {"content": [paragraph("a child tab", 1)]}, "lists": {}}}]},
    ],
}


@pytest.mark.parametrize("tab_id, words", [
    (None, "first tab"), ("t.0", "first tab"), ("t.a", "second tab"), ("t.b", "a child tab")])
def test_a_tabbed_read_has_no_body_and_every_tab_is_reachable(tab_id, words):
    ir = doc_ir.from_document(TABBED, tab_id)
    assert doc_ir.runs_text(ir["blocks"][0]["runs"]) == words
    assert ir["tab"] == (tab_id or "t.0")


def test_keys_are_readable_and_unique():
    ir = doc_ir.key_blocks(doc_ir.from_html(doc_ir.to_html(RICH)))
    keys = [b["key"] for b in ir["blocks"]]
    assert keys[0] == "heading:heading-one"
    assert len(set(keys)) == len(keys)


def test_the_key_rides_in_the_file_so_a_rewrite_keeps_its_identity():
    ir = doc_ir.key_blocks({"title": "", "blocks": [
        {"kind": "paragraph", "runs": [{"text": "the original wording"}]}]})
    html = doc_ir.to_html(ir)
    assert 'id="paragraph:the-original-wording"' in html
    # A human rewrites the words but leaves the id: the block is still that block.
    rewritten = doc_ir.from_html(html.replace("the original wording", "nothing alike now"))
    assert rewritten["blocks"][0]["key"] == "paragraph:the-original-wording"
    assert doc_ir.key_blocks(rewritten)["blocks"][0]["key"] == "paragraph:the-original-wording"


def test_a_new_block_never_takes_a_key_already_in_use():
    ir = doc_ir.key_blocks({"title": "", "blocks": [
        {"kind": "paragraph", "key": "paragraph:same", "runs": [{"text": "same"}]},
        {"kind": "paragraph", "runs": [{"text": "same"}]}]})
    assert [b["key"] for b in ir["blocks"]] == ["paragraph:same", "paragraph:same#2"]


# ---------------------------------------------------------------- named ranges

NAMED = {"b2s:heading:a-heading": {"name": "b2s:heading:a-heading", "namedRanges": [
             {"namedRangeId": "r1", "name": "b2s:heading:a-heading",
              "ranges": [{"startIndex": 1, "endIndex": 10}]}]},
         "b2s:paragraph:centred": {"name": "b2s:paragraph:centred", "namedRanges": [
             {"namedRangeId": "r2", "name": "b2s:paragraph:centred",
              "ranges": [{"startIndex": 11, "endIndex": 18}]}]},
         "kix.something-else": {"namedRanges": [
             {"namedRangeId": "r3", "ranges": [{"startIndex": 19, "endIndex": 20}]}]}}


def test_keys_come_back_from_the_named_ranges():
    ir = doc_ir.apply_keys(doc_ir.from_document(LIVE), NAMED)
    assert ir["blocks"][0]["key"] == "heading:a-heading"
    assert ir["blocks"][0]["rangeId"] == "r1"
    assert ir["blocks"][1]["key"] == "paragraph:centred"
    # A range that is not ours names nothing.
    assert "key" not in ir["blocks"][2]


def test_a_block_the_document_does_not_name_gets_a_range():
    ir = doc_ir.apply_keys(doc_ir.from_document(LIVE), NAMED)
    doc_ir.key_blocks(ir)
    requests = doc_ir.name_requests(ir)
    names = [r["createNamedRange"]["name"] for r in requests]
    assert "b2s:heading:a-heading" not in names  # it has one already
    assert names[0] == "b2s:item:a-bullet"
    # The range stops before the paragraph mark, so a joined paragraph cannot swallow it.
    assert requests[0]["createNamedRange"]["range"] == {"startIndex": 19, "endIndex": 27}


def test_the_named_ranges_of_a_tabbed_read_are_found_in_the_tab():
    tabbed = {"title": "t", "tabs": [
        {"tabProperties": {"tabId": "t.0"},
         "documentTab": {"body": {"content": [paragraph("named", 1)]},
                         "lists": {}, "namedRanges": NAMED}}]}
    ir = doc_ir.apply_keys(doc_ir.from_document(tabbed), doc_ir.named_ranges_of(tabbed))
    assert ir["blocks"][0]["key"] == "heading:a-heading"


def test_repeated_text_gets_an_occurrence_suffix():
    ir = doc_ir.key_blocks({"title": "", "blocks": [
        {"kind": "paragraph", "runs": [{"text": "same"}]},
        {"kind": "paragraph", "runs": [{"text": "same"}]}]})
    assert [b["key"] for b in ir["blocks"]] == ["paragraph:same", "paragraph:same#2"]
