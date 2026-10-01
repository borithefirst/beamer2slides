"""Offline tests for the Docs IR and the canonical HTML dialect (docs/google-docs.md).

No Google calls: the live-document fixtures are the shapes `documents.get` really
returned in `tools/probe_docs_chips.py`, trimmed to what the reader looks at.
"""

import pytest

from beamer2slides import doc_ir
from beamer2slides.doc_ir import Block, Ir, Run
from beamer2slides.google_types import (DocsAlignment, DocsBullet, DocsDateElement, DocsDimension,
                                        DocsEmbeddedObject, DocsImageProperties, DocsNamedRanges,
                                        DocsNamedStyleType, DocsNestingLevel, DocsParagraph,
                                        DocsParagraphElement, DocsParagraphStyle, DocsPerson,
                                        DocsStructuralElement, DocsTable, DocsTableCell, DocsTableRow,
                                        DocsTextStyle, Document)
from beamer2slides.json_types import JsonObject

from .doc_records import (create_range_of, key_of, paragraph_of, paragraph_style_of, rows_of, runs_of, span_of,
                          u16s)

# ------------------------------------------------ what Docs answers past `google_types`
# The fixtures are documents as `documents.get` returned them, and a few carry what the
# reader never reads (an id beside a chip, a list's start number, the properties
# `unmodelled` names): the answer's own types, each the declared one and the rest.


class _DateElement(DocsDateElement, total=False):
    dateId: str


class _Person(DocsPerson, total=False):
    personId: str


class _NestingLevel(DocsNestingLevel, total=False):
    startNumber: int
    indentStart: DocsDimension
    textStyle: DocsTextStyle


class _ParagraphStyle(DocsParagraphStyle, total=False):
    borderBetween: JsonObject
    tabStops: list[JsonObject]
    direction: str


class _TableCell(DocsTableCell, total=False):
    tableCellStyle: JsonObject


class _ImageProperties(DocsImageProperties, total=False):
    angle: float
    brightness: float
    cropProperties: JsonObject


class _EmbeddedObject(DocsEmbeddedObject, total=False):
    marginTop: DocsDimension
    embeddedObjectBorder: JsonObject


def _clean(block: Block) -> dict[str, object]:
    out: dict[str, object] = {k: v for k, v in block.items() if k not in ("span", "key")}
    if (runs := block.get("runs")) is not None:
        out["runs"] = [{k: v for k, v in r.items() if k != "width"} for r in runs]
    if (rows := block.get("rows")) is not None:
        out["rows"] = [[[_clean(b) for b in cell] for cell in row] for row in rows]
    return out


def strip(ir: Ir) -> dict[str, object]:
    """The IR without what only a live document has, so both sides compare."""
    return {"title": ir.get("title"), "blocks": [_clean(b) for b in ir["blocks"]]}


def _width(run: Run) -> int:
    width = run.get("width")
    assert width is not None, f"a run read from no document: {run}"
    return width


def _content(doc: Document) -> list[DocsStructuralElement]:
    content = doc.get("body", {}).get("content")
    assert content is not None, f"a document with no body: {sorted(doc)}"
    return content


RICH: Ir = {
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


def test_html_round_trip_is_the_identity() -> None:
    assert strip(doc_ir.from_html(doc_ir.to_html(RICH))) == strip(RICH)


def test_html_is_stable_on_a_second_pass() -> None:
    once = doc_ir.to_html(RICH)
    assert doc_ir.to_html(doc_ir.from_html(once)) == once


def test_a_list_item_carries_its_key_in_the_file_too() -> None:
    ir = doc_ir.key_blocks({"title": "", "blocks": [
        {"kind": "item", "level": 0, "ordered": False, "runs": [{"text": "an item"}]}]})
    html = doc_ir.to_html(ir)
    assert '<li id="item:an-item">an item</li>' in html
    assert key_of(doc_ir.from_html(html)["blocks"][0]) == "item:an-item"


def test_one_block_per_line_so_diffs_read() -> None:
    html = doc_ir.to_html(RICH)
    assert "<h1>Heading one</h1>" in html.splitlines()
    assert any(line.strip() == "<li>nested bullet</li>" for line in html.splitlines())


def test_a_chip_keeps_its_value_through_the_file() -> None:
    html = doc_ir.to_html(RICH)
    assert 'data-chip="date"' in html and 'data-value="2026-09-25T12:00:00Z"' in html
    chips = [r for b in doc_ir.from_html(html)["blocks"]
             for r in b.get("runs") or () if r.get("frozen")]
    assert [c.get("chip") for c in chips] == ["date", "person", "image", "image"]
    assert chips[0].get("value") == "2026-09-25T12:00:00Z"
    assert chips[1]["text"] == "Boris Arnoux"


def test_a_picture_is_an_img_that_names_its_file_and_its_object() -> None:
    html = doc_ir.to_html(RICH)
    assert ('<p><img src="figures/plot.png" alt="a plot &amp; its axes" width="60" height="40" '
            'data-object="kix.abc"></p>') in html.splitlines()
    # One outside any paragraph is a paragraph of its own.
    ir = doc_ir.from_html("<body><img src='x.png' width='30px' height='20'><p>after</p></body>")
    assert runs_of(ir["blocks"][0]) == [{"chip": "image", "frozen": True, "text": "",
                                         "src": "x.png", "size": [30, 20]}]
    assert key_of(doc_ir.key_blocks(ir)["blocks"][0]) == "paragraph:x"


# ---------------------------------------------------------------- live document

def test_a_picture_in_the_document_reads_with_its_size_alt_and_url() -> None:
    doc: Document = {"body": {"content": [paragraph("x", 1, None, None, [
        {"startIndex": 1, "endIndex": 2, "inlineObjectElement": {"inlineObjectId": "kix.1"}},
        {"startIndex": 2, "endIndex": 3, "textRun": {"content": "\n"}}])]},
        "inlineObjects": {"kix.1": {"inlineObjectProperties": {"embeddedObject": {
            "description": "the alt", "size": {"width": {"magnitude": 45, "unit": "PT"},
                                               "height": {"magnitude": 30, "unit": "PT"}},
            "imageProperties": {"contentUri": "https://lh7/x"}}}}}}
    run = runs_of(doc_ir.from_document(doc, tab_id=None)["blocks"][0])[0]
    assert run == {"chip": "image", "frozen": True, "text": "", "value": "kix.1", "width": 1,
                   "size": [60, 40], "alt": "the alt", "uri": "https://lh7/x"}
    # The URL dies within the hour and never reaches the file.
    assert "lh7" not in doc_ir.to_html({"title": "", "blocks": [{"kind": "paragraph",
                                                                  "runs": [run]}]})

def paragraph(text: str, start: int, style: DocsParagraphStyle | None, bullet: DocsBullet | None,
              elements: list[DocsParagraphElement] | None) -> DocsStructuralElement:
    """A paragraph as a read gives it: its words (or `elements`), its style and its bullet."""
    end = start + len(text) + 1
    words: list[DocsParagraphElement] = [{"startIndex": start, "endIndex": end,
                                          "textRun": {"content": text + "\n", "textStyle": {}}}]
    normal: DocsParagraphStyle = {"namedStyleType": "NORMAL_TEXT"}
    body: DocsParagraph = {"elements": elements or words, "paragraphStyle": style or normal}
    if bullet:
        body["bullet"] = bullet
    return {"startIndex": start, "endIndex": end, "paragraph": body}


_DUE: _DateElement = {
    "dateId": "kix.d1", "dateElementProperties": {
        "timestamp": "2026-09-25T12:00:00Z", "locale": "en",
        "dateFormat": "DATE_FORMAT_MONTH_DAY_YEAR_ABBREVIATED",
        "displayText": "Sep 25, 2026"}}
_OWNER: _Person = {"personId": "kix.p1", "personProperties": {"email": "a@b.c", "name": "A B"}}

LIVE: Document = {
    "title": "live",
    "lists": {"kix.list1": {"listProperties": {"nestingLevels": [
        {"glyphSymbol": "●"}, {"glyphSymbol": "○"}]}},
        "kix.list2": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}}},
    "body": {"content": [
        {"startIndex": 0, "endIndex": 1, "sectionBreak": {"sectionStyle": {}}},
        paragraph("A heading", 1, {"namedStyleType": "HEADING_1"}, None, None),
        paragraph("centred", 11, {"namedStyleType": "NORMAL_TEXT", "alignment": "CENTER"}, None, None),
        paragraph("a bullet", 19, None, {"listId": "kix.list1", "nestingLevel": 0}, None),
        paragraph("deeper", 28, None, {"listId": "kix.list1", "nestingLevel": 1}, None),
        paragraph("a step", 35, None, {"listId": "kix.list2", "nestingLevel": 0}, None),
        {"startIndex": 42, "endIndex": 72, "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [
                {"startIndex": 42, "endIndex": 46,
                 "textRun": {"content": "Due ", "textStyle": {"bold": True}}},
                {"startIndex": 46, "endIndex": 47, "dateElement": _DUE},
                {"startIndex": 47, "endIndex": 48},  # a dropdown chip: no content key at all
                {"startIndex": 48, "endIndex": 49, "person": _OWNER},
                {"startIndex": 49, "endIndex": 62, "equation": {}},
                {"startIndex": 62, "endIndex": 72, "textRun": {
                    "content": f" tail{doc_ir.OBJECT_SENTINEL}end\n", "textStyle": {}}},
            ]}},
    ]},
}


def test_reads_headings_alignment_and_both_kinds_of_list() -> None:
    ir = doc_ir.from_document(LIVE, tab_id=None)
    kinds = [(b["kind"], b.get("level"), b.get("ordered")) for b in ir["blocks"]]
    assert kinds[:5] == [("heading", 1, None), ("paragraph", None, None),
                         ("item", 0, False), ("item", 1, False), ("item", 0, True)]
    assert ir["blocks"][1].get("align") == "center"


def test_every_chip_becomes_a_frozen_run() -> None:
    ir = doc_ir.from_document(LIVE, tab_id=None)
    runs = runs_of(ir["blocks"][-1])
    assert [r.get("chip") for r in runs] == [
        None, "date", "unknown", "person", "equation", None, "object", None]
    assert all(r.get("frozen") for r in runs if r.get("chip"))
    assert runs[1].get("value") == "2026-09-25T12:00:00Z"
    assert runs[3].get("value") == "a@b.c"
    # The dropdown and the equation say nothing but that they are there.
    assert runs[2]["text"] == "" and runs[4]["text"] == ""


def test_a_block_keeps_the_span_it_came_from() -> None:
    ir = doc_ir.from_document(LIVE, tab_id=None)
    assert span_of(ir["blocks"][0]) == [1, 11]
    assert span_of(ir["blocks"][-1]) == [42, 72]


def test_a_run_knows_how_many_index_units_it_holds() -> None:
    """A chip is one unit however long its words look; the equation was thirteen."""
    runs = runs_of(doc_ir.from_document(LIVE, tab_id=None)["blocks"][-1])
    assert [_width(r) for r in runs] == [4, 1, 1, 1, 13, 5, 1, 3]
    assert runs[1]["text"] == "Sep 25, 2026" and _width(runs[1]) == 1
    # The widths account for every index unit of the block, newline included.
    block = doc_ir.from_document(LIVE, tab_id=None)["blocks"][-1]
    assert sum(_width(r) for r in runs_of(block)) == span_of(block)[1] - span_of(block)[0] - 1


_UNSPECIFIED: _NestingLevel = {"glyphType": "GLYPH_TYPE_UNSPECIFIED", "startNumber": 1}

IMPORTED_LIST: Document = {
    "title": "imported",
    # What Drive's HTML import really builds: the same, glyph-less definition for a
    # <ul> and an <ol> alike, though the editor renders them differently.
    "lists": {"kix.list.1": {"listProperties": {"nestingLevels": [_UNSPECIFIED]}}},
    "body": {"content": [
        paragraph("an item", 1, None, {"listId": "kix.list.1", "nestingLevel": 0}, None)]},
}


def test_an_imported_list_cannot_say_whether_it_is_numbered() -> None:
    block = doc_ir.from_document(IMPORTED_LIST, tab_id=None)["blocks"][0]
    assert block["kind"] == "item" and "ordered" in block and block.get("ordered") is None


def test_a_linked_run_drops_the_blue_underline_docs_paints_for_free() -> None:
    style = doc_ir._style_of({"link": {"url": "https://example.com/"}, "underline": True,
                              "foregroundColor": {"color": {"rgbColor": {"blue": 0.93333334}}}}, default=None)
    assert style == {"link": "https://example.com/"}
    # A colour somebody chose is not free styling, and stays.
    green = doc_ir._style_of({"link": {"url": "u"}, "underline": True,
                              "foregroundColor": {"color": {"rgbColor": {"green": 1.0}}}}, default=None)
    assert green == {"link": "u", "underline": True, "color": "#00ff00"}


def test_the_trailing_newline_is_the_paragraph_not_its_text() -> None:
    ir = doc_ir.from_document(LIVE, tab_id=None)
    assert doc_ir.runs_text(runs_of(ir["blocks"][0])) == "A heading"


TABBED: Document = {
    "title": "tabbed",
    "tabs": [
        {"tabProperties": {"tabId": "t.0", "title": "Tab 1", "index": 0},
         "documentTab": {"body": {"content": [paragraph("first tab", 1, None, None, None)]}, "lists": {}}},
        {"tabProperties": {"tabId": "t.a", "title": "Tab 2", "index": 1},
         "documentTab": {"body": {"content": [paragraph("second tab", 1, None, None, None)]}, "lists": {}},
         "childTabs": [
             {"tabProperties": {"tabId": "t.b", "title": "Tab 3", "parentTabId": "t.a"},
              "documentTab": {"body": {"content": [paragraph("a child tab", 1, None, None, None)]},
                              "lists": {}}}]},
    ],
}


@pytest.mark.parametrize("tab_id, words", [
    (None, "first tab"), ("t.0", "first tab"), ("t.a", "second tab"), ("t.b", "a child tab")])
def test_a_tabbed_read_has_no_body_and_every_tab_is_reachable(tab_id: str | None, words: str) -> None:
    ir = doc_ir.from_document(TABBED, tab_id)
    assert doc_ir.runs_text(runs_of(ir["blocks"][0])) == words
    assert ir.get("tab") == (tab_id or "t.0")


def test_the_first_tabs_own_name_survives_the_file() -> None:
    """The file's `<title>` is the document's name; the first tab has a name of its
    own, and a document of one tab has both."""
    html = doc_ir.to_html({"title": "The Quarterly Report", "tab_title": "Chapter one",
                           "blocks": [{"kind": "paragraph", "runs": [{"text": "x"}]}]})
    assert '<meta name="b2s-tab" content="Chapter one">' in html
    assert "<title>The Quarterly Report</title>" in html
    back = doc_ir.from_html(html)
    assert back.get("tab_title") == "Chapter one" and back.get("title") == "The Quarterly Report"
    # A file that says nothing says nothing: no meta, and no key in the IR.
    assert "b2s-tab" not in doc_ir.to_html({"title": "t", "blocks": []})


def test_keys_are_readable_and_unique() -> None:
    ir = doc_ir.key_blocks(doc_ir.from_html(doc_ir.to_html(RICH)))
    keys = [key_of(b) for b in ir["blocks"]]
    assert keys[0] == "heading:heading-one"
    assert len(set(keys)) == len(keys)


def test_the_key_rides_in_the_file_so_a_rewrite_keeps_its_identity() -> None:
    ir = doc_ir.key_blocks({"title": "", "blocks": [
        {"kind": "paragraph", "runs": [{"text": "the original wording"}]}]})
    html = doc_ir.to_html(ir)
    assert 'id="paragraph:the-original-wording"' in html
    # A human rewrites the words but leaves the id: the block is still that block.
    rewritten = doc_ir.from_html(html.replace("the original wording", "nothing alike now"))
    assert key_of(rewritten["blocks"][0]) == "paragraph:the-original-wording"
    assert key_of(doc_ir.key_blocks(rewritten)["blocks"][0]) == "paragraph:the-original-wording"


def test_a_new_block_never_takes_a_key_already_in_use() -> None:
    ir = doc_ir.key_blocks({"title": "", "blocks": [
        {"kind": "paragraph", "key": "paragraph:same", "runs": [{"text": "same"}]},
        {"kind": "paragraph", "runs": [{"text": "same"}]}]})
    assert [key_of(b) for b in ir["blocks"]] == ["paragraph:same", "paragraph:same#2"]


# ---------------------------------------------------------------- named ranges

NAMED: dict[str, DocsNamedRanges] = {"b2s:heading:a-heading": {"name": "b2s:heading:a-heading", "namedRanges": [
             {"namedRangeId": "r1", "name": "b2s:heading:a-heading",
              "ranges": [{"startIndex": 1, "endIndex": 10}]}]},
         "b2s:paragraph:centred": {"name": "b2s:paragraph:centred", "namedRanges": [
             {"namedRangeId": "r2", "name": "b2s:paragraph:centred",
              "ranges": [{"startIndex": 11, "endIndex": 18}]}]},
         "kix.something-else": {"namedRanges": [
             {"namedRangeId": "r3", "ranges": [{"startIndex": 19, "endIndex": 20}]}]}}


def test_keys_come_back_from_the_named_ranges() -> None:
    ir = doc_ir.apply_keys(doc_ir.from_document(LIVE, tab_id=None), NAMED)
    assert key_of(ir["blocks"][0]) == "heading:a-heading"
    assert ir["blocks"][0].get("rangeId") == "r1"
    assert key_of(ir["blocks"][1]) == "paragraph:centred"
    # A range that is not ours names nothing.
    assert "key" not in ir["blocks"][2]


def test_a_block_the_document_does_not_name_gets_a_range() -> None:
    ir = doc_ir.apply_keys(doc_ir.from_document(LIVE, tab_id=None), NAMED)
    doc_ir.key_blocks(ir)
    requests = doc_ir.name_requests(ir)
    names = [create_range_of(r)["name"] for r in requests]
    assert "b2s:heading:a-heading" not in names  # it has one already
    assert names[0] == "b2s:item:a-bullet"
    # The range stops before the paragraph mark, so a joined paragraph cannot swallow it.
    assert create_range_of(requests[0])["range"] == {"startIndex": 19, "endIndex": 27}


def test_a_range_that_drifted_is_planted_again_where_it_belongs() -> None:
    """`apply_keys` records where a range *is*; `name_requests` compares that with
    where `anchor_range` puts it. A range drifts when text is written at its first
    index (Docs pushes it along): the old one goes and a fresh one is planted, in that
    order, so a name is never carried twice — and a range where it belongs, or a
    block with none yet, gets no delete."""
    drifted: dict[str, DocsNamedRanges] = {"b2s:heading:a-heading": {
        "name": "b2s:heading:a-heading", "namedRanges": [
            {"namedRangeId": "r1", "name": "b2s:heading:a-heading",
             "ranges": [{"startIndex": 9, "endIndex": 10}]}]}}
    ir = doc_ir.apply_keys(doc_ir.from_document(LIVE, tab_id=None), NAMED | drifted)
    assert ir["blocks"][0].get("range") == [9, 10]
    assert ir["blocks"][1].get("range") == [11, 18]
    anchor = doc_ir.anchor_range(ir["blocks"][0])
    assert anchor is not None, "a keyed block with no anchor"
    low, high = anchor
    assert doc_ir.replant_requests(ir) == [
        {"deleteNamedRange": {"namedRangeId": "r1"}},
        {"createNamedRange": {"name": "b2s:heading:a-heading",
                              "range": {"startIndex": low, "endIndex": high}}}]
    doc_ir.key_blocks(ir)
    requests = doc_ir.name_requests(ir)
    assert requests[:2] == doc_ir.replant_requests(ir)
    assert [next(iter(r)) for r in requests[2:]] == ["createNamedRange"] * (len(requests) - 2)
    assert "b2s:paragraph:centred" not in [
        create_range_of(r)["name"] for r in requests if "createNamedRange" in r]


def test_the_named_ranges_of_a_tabbed_read_are_found_in_the_tab() -> None:
    tabbed: Document = {"title": "t", "tabs": [
        {"tabProperties": {"tabId": "t.0"},
         "documentTab": {"body": {"content": [paragraph("named", 1, None, None, None)]},
                         "lists": {}, "namedRanges": NAMED}}]}
    ir = doc_ir.apply_keys(doc_ir.from_document(tabbed, tab_id=None), doc_ir.named_ranges_of(tabbed, tab_id=None))
    assert key_of(ir["blocks"][0]) == "heading:a-heading"


def pieces_paragraph(start: int, *pieces: str | int) -> DocsStructuralElement:
    """A paragraph of text runs (str) and equations (int: their width)."""
    elements: list[DocsParagraphElement] = []
    at = start
    for piece in pieces:
        if isinstance(piece, int):
            elements.append({"startIndex": at, "endIndex": at + piece, "equation": {}})
            at += piece
        else:
            elements.append({"startIndex": at, "endIndex": at + len(piece),
                             "textRun": {"content": piece, "textStyle": {}}})
            at += len(piece)
    return {"startIndex": start, "endIndex": at, "paragraph": {
        "elements": elements, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}}


# The document and its Markdown export as `probe_equation` measured them: an OMML
# import, a second tab, and dollars in the text that nobody escapes.
EQUATIONS: Document = {"title": "eq", "tabs": [
    {"tabProperties": {"tabId": "t.0", "title": "Tab 1"}, "documentTab": {"body": {"content": [
        pieces_paragraph(1, "Inline ", 10, " and a fraction ", 7, " end.\n"),
        pieces_paragraph(47, "It costs $5, not $6 or \\$ and _x_ ", 12, "\n"),
        pieces_paragraph(94, 9, "\n"),
        pieces_paragraph(104, "Second paragraph.\n")]}}},
    {"tabProperties": {"tabId": "t.1", "title": "Two"}, "documentTab": {"body": {"content": [
        pieces_paragraph(1, "In tab two: costs $7.\n")]}}}]}
EQUATIONS_MD = (
    "# **Tab 1**\n\n"
    "Inline $E=m{c}^{2}$ and a fraction $\\frac{a}{b}$ end.  \n"
    "It costs $5, not $6 or \\\\$ and \\_x\\_ ${x}_{1}+α_$$  \n"
    "$$y=\\sqrt{z}$$  \n"
    "Second paragraph.\n\n"
    "# **Two**\n\n"
    "In tab two: costs $7.\n")


def test_each_equation_gets_its_latex_from_the_markdown_export() -> None:
    spots = doc_ir.equation_spots(EQUATIONS)
    assert [s["start"] for s in spots] == [8, 34, 81, 94]
    found = doc_ir.latex_of(spots, EQUATIONS_MD)
    assert found == {("t.0", 8): "E=m{c}^{2}", ("t.0", 34): "\\frac{a}{b}",
                     ("t.0", 81): "{x}_{1}+α_$", ("t.0", 94): "y=\\sqrt{z}"}


def test_an_equation_the_export_does_not_show_gets_no_latex() -> None:
    spots = doc_ir.equation_spots(EQUATIONS)
    assert doc_ir.latex_of(spots, "# **Tab 1**\n\nInline and a fraction end.\n") == {}


def test_two_equations_side_by_side_are_found_one_after_the_other() -> None:
    doc: Document = {"title": "", "tabs": [{"tabProperties": {"tabId": "t.0"}, "documentTab": {
        "body": {"content": [pieces_paragraph(1, "So ", 4, 5, " holds.\n")]}}}]}
    found = doc_ir.latex_of(doc_ir.equation_spots(doc), "So $a$$b+c$ holds.\n")
    assert found == {("t.0", 4): "a", ("t.0", 8): "b+c"}


def test_the_latex_reaches_the_file_and_comes_back_from_it() -> None:
    from beamer2slides import doc_sync
    ir = doc_sync.document_ir(EQUATIONS, "ident", ours=None, base=None)
    found = doc_ir.latex_of(doc_ir.equation_spots(EQUATIONS), EQUATIONS_MD)
    assert doc_ir.attach_latex(ir, found) == 4
    html = doc_ir.to_html(ir)
    assert 'data-chip="equation">E=m{c}^{2}</span>' in html
    back = doc_ir.from_html(html)
    runs = [r for b in back["blocks"] for r in runs_of(b) if r.get("chip") == "equation"]
    assert [r["text"] for r in runs] == ["E=m{c}^{2}", "\\frac{a}{b}", "{x}_{1}+α_$",
                                         "y=\\sqrt{z}"]


def test_repeated_text_gets_an_occurrence_suffix() -> None:
    ir = doc_ir.key_blocks({"title": "", "blocks": [
        {"kind": "paragraph", "runs": [{"text": "same"}]},
        {"kind": "paragraph", "runs": [{"text": "same"}]}]})
    assert [key_of(b) for b in ir["blocks"]] == ["paragraph:same", "paragraph:same#2"]


# ---------------------------------------------------------------- tables

def test_a_table_is_written_one_line_per_row_so_a_diff_reads() -> None:
    """A row changed shows as one changed line, not as a whole table rewritten."""
    lines = doc_ir.to_html(RICH).splitlines()
    assert "<table>" in lines and "</table>" in lines
    assert " <tr><td><p>a</p></td><td><p>b</p></td></tr>" in lines
    assert " <tr><td><p>c</p></td><td><p>d</p></td></tr>" in lines


def test_the_line_breaks_in_a_table_are_not_words_in_it() -> None:
    """They sit between `</tr>` and `<tr>` and inside the table's own tags, where an
    HTML parser has nowhere to put text. A row stays whole: inside a `<td>` the white
    space *would* be content. (Measured here on `from_html`; Drive's own importer
    wants confirming on a live document.)"""
    blocks = doc_ir.from_html(doc_ir.to_html(RICH))["blocks"]
    table = [b for b in blocks if b["kind"] == "table"][0]
    assert [[[doc_ir.runs_text(runs_of(b)) for b in cell] for cell in row]
            for row in rows_of(table)] == [[["a"], ["b"]], [["c"], ["d"]]]
    # And no block of white space arrived beside the table either.
    assert [b["kind"] for b in blocks].count("table") == 1
    assert not [b for b in blocks
                if (runs := b.get("runs")) and not doc_ir.runs_text(runs).strip()
                and not any(r.get("frozen") for r in runs)]


# ---------------------------------------------------------------- faces and measures

DRESSED: Ir = {
    "title": "dressed",
    "blocks": [
        {"kind": "paragraph", "indent": 36.0, "indent_first": 18.0, "line_spacing": 1.5,
         "space_above": 12.0, "space_below": 6.0, "shading": "#fff2cc", "runs": [
             {"text": "set in "},
             {"text": "Consolas", "font": "Consolas", "fontsize": 9},
             {"text": " and "},
             {"text": "Roboto Mono", "font": "Roboto Mono", "fontsize": 9},
             {"text": ", "},
             {"text": "Small Caps", "smallcaps": True},
             {"text": ", x"},
             {"text": "2", "script": "super"},
             {"text": " and H"},
             {"text": "2", "script": "sub"},
             {"text": "O and "},
             {"text": "big and bold", "font": "Comic Sans MS", "fontsize": 18, "bold": True},
             {"text": "."}]},
        {"kind": "item", "level": 0, "ordered": False, "line_spacing": 2.0,
         "space_below": 3.0, "runs": [{"text": "a roomy item"}]},
        {"kind": "heading", "level": 2, "align": "center", "shading": "#eeeeee",
         "runs": [{"text": "A shaded heading"}]},
    ],
}


def test_a_dressed_document_round_trips_through_the_file() -> None:
    assert strip(doc_ir.from_html(doc_ir.to_html(DRESSED))) == strip(DRESSED)
    once = doc_ir.to_html(DRESSED)
    assert doc_ir.to_html(doc_ir.from_html(once)) == once


def test_a_title_and_a_subtitle_are_kinds_of_their_own() -> None:
    """Docs' named styles are NORMAL_TEXT, TITLE, SUBTITLE and HEADING_1..6. The first
    six were a block kind and a level; these two are a kind each, because they are not
    a level — and the merge names `namedStyleType` on every paragraph it writes, so a
    style the file cannot spell is one a sync writes body text over."""
    def document(named: DocsNamedStyleType) -> Document:
        return {"body": {"content": [{"paragraph": {
            "paragraphStyle": {"namedStyleType": named},
            "elements": [{"startIndex": 1, "endIndex": 5,
                          "textRun": {"content": "hi\n", "textStyle": {}}}]}}]}}

    assert [doc_ir.from_document(document(n), tab_id=None)["blocks"][0]["kind"]
            for n in ("TITLE", "SUBTITLE", "NORMAL_TEXT")] == \
        ["title", "subtitle", "paragraph"]

    ir = doc_ir.key_blocks({"title": "", "blocks": [
        {"kind": "title", "runs": [{"text": "The Report"}]},
        {"kind": "subtitle", "runs": [{"text": "a sub"}]}]})
    html = doc_ir.to_html(ir)
    assert 'data-style="title"' in html and 'data-style="subtitle"' in html
    assert [b["kind"] for b in doc_ir.from_html(html)["blocks"]] == ["title", "subtitle"]
    assert doc_ir.to_html(doc_ir.from_html(html)) == html


def test_two_monospaced_faces_stay_two_faces() -> None:
    """`<code>` flattened every mono face into one; a face is now carried as itself."""
    html = doc_ir.to_html(DRESSED)
    assert "font-family:Consolas" in html and "font-family:Roboto Mono" in html
    runs = runs_of(doc_ir.from_html(html)["blocks"][0])
    assert [r.get("font") for r in runs if r.get("font")] == [
        "Consolas", "Roboto Mono", "Comic Sans MS"]
    # One name, unquoted, no fallback list: a list arrives at the importer as `Geo`.
    assert "," not in html.split("font-family:")[1].split(";")[0].split('"')[0]


def test_code_is_still_written_and_still_understood() -> None:
    """Files written before faces were carried keep working, and say what they said."""
    ir = doc_ir.from_html("<body><p><code>mono</code></p></body>")
    assert runs_of(ir["blocks"][0]) == [{"text": "mono", "code": True}]
    assert "<code>mono</code>" in doc_ir.to_html(ir)


def test_small_caps_travels_as_an_attribute_because_no_css_carries_it() -> None:
    html = doc_ir.to_html(DRESSED)
    assert '<span data-smallcaps="1">Small Caps</span>' in html
    assert "font-variant" not in html


def test_a_raised_or_lowered_run_travels_as_sup_and_sub() -> None:
    """`baselineOffset` is content, not decoration — the 2 of x² — and HTML has had
    the two tags for it since the beginning, so the file says them."""
    html = doc_ir.to_html(DRESSED)
    assert "<sup>2</sup>" in html and "<sub>2</sub>" in html
    runs = runs_of(doc_ir.from_html(html)["blocks"][0])
    assert [r["text"] for r in runs if r.get("script")] == ["2", "2"]
    assert [r.get("script") for r in runs if r.get("script")] == ["super", "sub"]


def test_a_run_a_raising_named_style_puts_back_on_the_baseline_says_so() -> None:
    """The `data-off` reasoning, one value wider: a theme could raise a whole named
    style, and a reader who puts one word back on the baseline must be able to say it
    — "none" is a third value, not the absence of the other two. Saying nothing would
    leave the source's next restyle, which names `baselineOffset` with no value, to
    undo the reader's choice."""
    raised: Document = {"namedStyles": {"styles": [
        {"namedStyleType": "NORMAL_TEXT",
         "textStyle": {"baselineOffset": "SUPERSCRIPT"}, "paragraphStyle": {}}]},
        "body": {"content": [{"startIndex": 1, "endIndex": 10, "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [
                {"startIndex": 1, "endIndex": 5, "textRun": {
                    "content": "up  ", "textStyle": {"baselineOffset": "SUPERSCRIPT"}}},
                {"startIndex": 5, "endIndex": 10, "textRun": {
                    "content": "down\n", "textStyle": {"baselineOffset": "NONE"}}}]}}]}}
    runs = runs_of(doc_ir.from_document(raised, tab_id=None)["blocks"][0])
    # A run that says it is raised says so whether or not the theme raises it too —
    # a mark is carried the same way, and only the *refusal* needs the named style to
    # be read at all. The file has an attribute for what no tag can spell.
    assert [r.get("script") for r in runs] == ["super", "none"]
    html = doc_ir.to_html(doc_ir.key_blocks(doc_ir.from_document(raised, tab_id=None)))
    assert 'data-script="none"' in html
    assert [r.get("script") for r in runs_of(doc_ir.from_html(html)["blocks"][0])] == \
        ["super", "none"]


def test_the_indents_and_the_line_height_are_css_the_importer_keeps() -> None:
    line = [l for l in doc_ir.to_html(DRESSED).splitlines() if "set in" in l][0]
    assert 'style="margin-left:36pt;text-indent:18pt;line-height:1.5"' in line


def test_paragraph_shading_is_never_written_as_css() -> None:
    """`background-color` on a `<p>` becomes a character highlight on its runs
    (measured), so the CSS spelling would be a lie: the file says `data-shading`."""
    line = [l for l in doc_ir.to_html(DRESSED).splitlines() if "shaded heading" in l][0]
    assert 'data-shading="#eeeeee"' in line and "background-color" not in line


def test_the_space_around_a_paragraph_is_ours_to_write_not_the_importers() -> None:
    line = [l for l in doc_ir.to_html(DRESSED).splitlines() if "set in" in l][0]
    assert 'data-space-above="12"' in line and 'data-space-below="6"' in line
    assert "margin-top" not in line and "margin-bottom" not in line


BORDERED_LIVE: Document = {
    "title": "bordered",
    "body": {"content": [
        {"startIndex": 1, "endIndex": 8, "paragraph": {
            "paragraphStyle": {
                "namedStyleType": "HEADING_1", "pageBreakBefore": True,
                "keepWithNext": True,
                "borderBottom": {"width": {"magnitude": 1.5, "unit": "PT"},
                                 "padding": {"magnitude": 5, "unit": "PT"},
                                 "dashStyle": "SOLID",
                                 "color": {"color": {"rgbColor": {"red": 0.8}}}},
                "borderTop": {"width": {"magnitude": 0, "unit": "PT"},
                              "dashStyle": "SOLID",
                              "color": {"color": {"rgbColor": {}}}},
                "borderLeft": {"width": {"magnitude": 3, "unit": "PT"},
                               "dashStyle": "DASH"}},
            "elements": [{"startIndex": 1, "endIndex": 8,
                          "textRun": {"content": "Part I\n", "textStyle": {}}}]}}]},
}


def test_a_paragraphs_rules_and_its_page_break_are_read() -> None:
    """Borders sit in the very dialog that sets shading, and shading round-tripped
    while the rule beside it vanished on the first rewrite."""
    block = doc_ir.from_document(BORDERED_LIVE, tab_id=None)["blocks"][0]
    assert block.get("border_bottom") == "1.5pt solid #cc0000 pad 5pt"
    assert block.get("border_left") == "3pt dashed #000000"
    assert block.get("page_break") is True and block.get("keep_with_next") is True
    # A rule of no width is a rule somebody took off, colour and all: not `0pt`,
    # which would read back as setting one.
    assert "border_top" not in block and "border_right" not in block


def test_a_rule_and_a_page_break_survive_the_file() -> None:
    ir = doc_ir.from_document(BORDERED_LIVE, tab_id=None)
    line = [l for l in doc_ir.to_html(ir).splitlines() if "Part I" in l][0]
    assert 'data-border-bottom="1.5pt solid #cc0000 pad 5pt"' in line
    assert 'data-border-left="3pt dashed #000000"' in line
    assert 'data-page-break="1"' in line and 'data-keep-with-next="1"' in line
    back = doc_ir.from_html(doc_ir.to_html(ir))["blocks"][0]
    assert {k: v for k, v in back.items() if k.startswith(("border", "page", "keep"))} == \
        {"border_bottom": "1.5pt solid #cc0000 pad 5pt",
         "border_left": "3pt dashed #000000", "page_break": True,
         "keep_with_next": True}


def test_a_rule_this_dialect_cannot_spell_is_no_rule_at_all() -> None:
    """As a length in an unknown unit is no length: a guess would put a rule the
    person never asked for on the paragraph."""
    said = ('<html><body><p data-border-bottom="thick ridge rebeccapurple">a</p>'
            '<p data-border-top="2pt dotted #00ff00 pad 0pt">b</p></body></html>')
    blocks = doc_ir.from_html(said)["blocks"]
    assert "border_bottom" not in blocks[0]
    assert blocks[1].get("border_top") == "2pt dotted #00ff00"      # a padding of 0 is none


STYLED_LIVE: Document = {
    "title": "styled",
    "namedStyles": {"styles": [
        {"namedStyleType": "NORMAL_TEXT",
         "textStyle": {"weightedFontFamily": {"fontFamily": "Arial"},
                       "fontSize": {"magnitude": 11, "unit": "PT"}},
         "paragraphStyle": {"lineSpacing": 100}}]},
    "body": {"content": [
        {"startIndex": 1, "endIndex": 14, "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT", "lineSpacing": 150,
                               "indentStart": {"magnitude": 36, "unit": "PT"},
                               "indentFirstLine": {"magnitude": 36, "unit": "PT"},
                               "spaceAbove": {"magnitude": 12, "unit": "PT"},
                               "shading": {"backgroundColor": {"color": {"rgbColor": {
                                   "red": 1.0, "green": 0.9490196, "blue": 0.8}}}}},
            "elements": [
                {"startIndex": 1, "endIndex": 6, "textRun": {
                    "content": "same ", "textStyle": {
                        "weightedFontFamily": {"fontFamily": "Arial"},
                        "fontSize": {"magnitude": 11, "unit": "PT"}}}},
                {"startIndex": 6, "endIndex": 14, "textRun": {
                    "content": "diff\n", "textStyle": {
                        "weightedFontFamily": {"fontFamily": "Courier New"},
                        "fontSize": {"magnitude": 7.5, "unit": "PT"},
                        "smallCaps": True}}}]}}]},
}


def test_a_run_that_only_repeats_its_named_style_says_nothing() -> None:
    """Subtracting the named style is what keeps the file from being a wall of spans:
    an import sets a face and a size on every run it writes."""
    runs = runs_of(doc_ir.from_document(STYLED_LIVE, tab_id=None)["blocks"][0])
    assert runs[0] == {"text": "same ", "width": 5}
    assert runs[1] == {"text": "diff", "width": 4, "font": "Courier New",
                       "fontsize": 7.5, "smallcaps": True}


def test_a_paragraph_reports_only_what_it_sets_itself() -> None:
    block = doc_ir.from_document(STYLED_LIVE, tab_id=None)["blocks"][0]
    assert block.get("line_spacing") == 1.5 and block.get("indent") == 36.0
    assert block.get("space_above") == 12.0 and block.get("shading") == "#fff2cc"
    # Nothing it does not set, and nothing that only repeats the named style.
    assert "indent_first" not in block and "space_below" not in block


def _magnitude(dimension: DocsDimension | None) -> float | None:
    return None if dimension is None else dimension.get("magnitude")


def test_a_first_line_indent_is_css_s_from_the_margin_left_and_docs_from_the_page() -> None:
    """`text-indent` is from `margin-left`, `indentFirstLine` from the page margin
    (measured 2026-09-24: `margin-left:36pt; text-indent:18pt` imports as 36 / 54). Read
    one way and written the other, a pushed file came back saying `text-indent:54pt`."""
    from beamer2slides import doc_merge

    def read(start: float | None, first: float | None) -> tuple[float | None, float | None]:
        """A paragraph whose `indentStart` and `indentFirstLine` are these, where given."""
        style: DocsParagraphStyle = {"namedStyleType": "NORMAL_TEXT"}
        if start is not None:
            style["indentStart"] = {"magnitude": start, "unit": "PT"}
        if first is not None:
            style["indentFirstLine"] = {"magnitude": first, "unit": "PT"}
        doc: Document = {"body": {"content": [{"startIndex": 1, "endIndex": 3, "paragraph": {
            "paragraphStyle": style,
            "elements": [{"startIndex": 1, "endIndex": 3, "textRun": {"content": "a\n"}}]}}]}}
        block = doc_ir.from_document(doc, tab_id=None)["blocks"][0]
        return block.get("indent"), block.get("indent_first")

    assert read(36, 54) == (36.0, 18.0)
    assert read(36, 36) == (36.0, None)
    assert read(None, 18) == (None, 18.0)
    assert read(36, 18) == (36.0, -18.0), "a hanging first line"
    assert read(36, None) == (36.0, -36.0), "a first line left at the page margin"

    def written(indent: float | None, indent_first: float | None) -> tuple[float | None, float | None]:
        """What a paragraph block of these measures is written as, where it has them."""
        block: Block = {"kind": "paragraph"}
        if indent is not None:
            block["indent"] = indent
        if indent_first is not None:
            block["indent_first"] = indent_first
        style, _ = doc_merge.paragraph_style(block)
        return _magnitude(style.get("indentStart")), _magnitude(style.get("indentFirstLine"))

    assert written(36.0, 18.0) == (36.0, 54.0)
    assert written(36.0, None) == (36.0, 36.0)
    assert written(None, 18.0) == (None, 18.0)
    assert written(None, None) == (None, None)
    assert "indentFirstLine" not in doc_merge.paragraph_style(
        {"kind": "item", "indent": 36.0, "indent_first": 18.0})[0], "an item's are the preset's"


def test_a_hanging_first_line_the_import_dropped_is_written_after_it() -> None:
    """Drive's importer drops a negative `text-indent` (measured: 36 - 18 arrives as
    36 / 36); the settle writes it, from the page margin."""
    from beamer2slides import doc_merge

    live: Ir = {"blocks": [{"key": "p", "kind": "paragraph", "span": u16s(1, 3), "indent": 36.0,
                            "runs": [{"text": "a"}]}]}
    planned: list[Block] = [{"key": "p", "kind": "paragraph", "indent": 36.0, "indent_first": -18.0,
                             "runs": [{"text": "a"}]}]
    doc_merge.carry_unimported(live, planned)
    [request] = [r for r in doc_merge.unimported_requests(live) if "updateParagraphStyle" in r]
    style = paragraph_style_of(request)["paragraphStyle"]
    assert _magnitude(style.get("indentFirstLine")) == 18.0 and _magnitude(style.get("indentStart")) == 36.0


def test_a_heading_the_theme_centres_says_nothing_about_its_alignment() -> None:
    """The centring belongs to the document's HEADING_1, not to this paragraph, and a
    file that claimed it would hand it to the write side as a value — where it stops
    being the theme's and becomes ours, left-aligned the moment a source edit drops it.
    The reader's own choice still counts: a heading somebody left-aligned in the
    browser differs from the style and is carried."""
    def doc(alignment: DocsAlignment | None) -> Document:
        style: DocsParagraphStyle = {"namedStyleType": "HEADING_1"}
        if alignment:
            style["alignment"] = alignment
        return {"namedStyles": {"styles": [
            {"namedStyleType": "HEADING_1",
             "paragraphStyle": {"alignment": "CENTER"}}]},
            "body": {"content": [paragraph("A heading", 1, style, None, None)]}}

    assert "align" not in doc_ir.from_document(doc(None), tab_id=None)["blocks"][0]
    assert "align" not in doc_ir.from_document(doc("CENTER"), tab_id=None)["blocks"][0]
    assert doc_ir.from_document(doc("START"), tab_id=None)["blocks"][0].get("align") == "left"


def test_a_bullets_own_indents_are_the_presets_and_never_the_files() -> None:
    """`createParagraphBullets` owns them: a value written back would fight it."""
    first = _content(STYLED_LIVE)[0]
    doc: Document = {**STYLED_LIVE, "body": {"content": [
        {**first, "paragraph": {**paragraph_of(first), "bullet": {"listId": "kix.l", "nestingLevel": 0}}}]}}
    block = doc_ir.from_document(doc, tab_id=None)["blocks"][0]
    assert block["kind"] == "item" and "indent" not in block
    assert block.get("line_spacing") == 1.5   # everything else is still carried


# ------------------------------------------------- what the reader does not read

# A document carrying one of everything the dialect leaves behind. Each value here
# was put in because Docs really returns it, and the test below is the claim: this
# is the whole of what a canonical file cannot say. A property Docs adds later shows
# up as a failure rather than as silence — which is the only way it ever would,
# since a sync converges on the IR and the IR is exactly what cannot see it.
_UNREAD_STYLE: _ParagraphStyle = {
    "namedStyleType": "NORMAL_TEXT", "keepWithNext": True,
    "borderLeft": {"width": {"magnitude": 1}},
    "borderBetween": {"width": {"magnitude": 1}},
    "tabStops": [{"offset": {"magnitude": 36}}],
    "direction": "LEFT_TO_RIGHT", "pageBreakBefore": True}
_UNREAD_CELL: _TableCell = {"startIndex": 10, "endIndex": 20, "content": [],
                            "tableCellStyle": {"rowSpan": 1, "columnSpan": 2}}
_UNREAD_ROW: DocsTableRow = {"startIndex": 9, "endIndex": 40,
                          "tableRowStyle": {"minRowHeight": {"magnitude": 20}},
                          "tableCells": [_UNREAD_CELL]}
_UNREAD_TABLE: DocsTable = {
    "rows": 1, "columns": 2,
    "tableStyle": {"tableColumnProperties": [{"width": {"magnitude": 100}}]},
    "tableRows": [_UNREAD_ROW]}
_UNREAD_IMAGE: _ImageProperties = {"contentUri": "https://x", "angle": 0.3,
                                   "brightness": 0.1,
                                   "cropProperties": {"offsetLeft": 0.2}}
_UNREAD_OBJECT: _EmbeddedObject = {"title": "t",
                                   "imageProperties": _UNREAD_IMAGE,
                                   "marginTop": {"magnitude": 9},
                                   "embeddedObjectBorder": {"width": {"magnitude": 1}}}
_UNREAD_LEVEL: _NestingLevel = {"glyphSymbol": "-", "startNumber": 7, "indentStart": {"magnitude": 36},
                                "textStyle": {"bold": True}}

UNMODELLED_LIVE: Document = {
    "documentId": "d1", "title": "Report", "revisionId": "r1",
    "documentStyle": {"marginTop": {"magnitude": 72, "unit": "PT"}},
    "headers": {"h1": {"content": []}},
    "footnotes": {"f1": {"content": []}},
    "positionedObjects": {"p1": {"objectId": "p1"}},
    "body": {"content": [
        {"startIndex": 1, "endIndex": 9, "paragraph": {
            "elements": [{"startIndex": 1, "endIndex": 9, "textRun": {
                "content": "one two\n",
                "textStyle": {"bold": True, "baselineOffset": "SUPERSCRIPT"}}}],
            "paragraphStyle": _UNREAD_STYLE}},
        {"startIndex": 9, "endIndex": 40, "table": _UNREAD_TABLE},
        {"startIndex": 40, "endIndex": 41, "sectionBreak": {"sectionStyle": {}}}]},
    "inlineObjects": {"i1": {"objectId": "i1", "inlineObjectProperties": {
        "embeddedObject": _UNREAD_OBJECT}}},
    "lists": {"l1": {"listProperties": {"nestingLevels": [_UNREAD_LEVEL]}}},
    "namedStyles": {"styles": [
        {"namedStyleType": "HEADING_1",
         "textStyle": {"weightedFontFamily": {"fontFamily": "Arial"}, "bold": True,
                       "foregroundColor": {"color": {"rgbColor": {"blue": 1.0}}}},
         "paragraphStyle": {"alignment": "CENTER", "spaceAbove": {"magnitude": 12}}}]},
}

UNMODELLED = {
    # Page-level structure, which has no place in the file at all.
    "documentStyle", "headers", "footnotes", "positionedObjects",
    "structural.sectionBreak",
    # Paragraph properties the dialect has no spelling for. No *run* property is
    # here any more: `baselineOffset` was the last field of `TextStyle` the dialect
    # could not say, and `<sup>`/`<sub>` say it, so the reader now carries the whole
    # of a run's styling. The fixture still puts one on, which is what makes this a
    # test and not a tautology — a field Docs adds to TextStyle later fails here.
    # The four sides of a border are read; `borderBetween` is not, and is no
    # oversight: it is a rule *between* consecutive paragraphs that share a style,
    # and a block model where one paragraph is one block, written on its own, has
    # nowhere honest to put it.
    "structural.paragraph.paragraphStyle.borderBetween",
    "structural.paragraph.paragraphStyle.direction",
    "structural.paragraph.paragraphStyle.tabStops",
    # A table's geometry and its merged cells: the ragged-table limit, named.
    "structural.table.tableStyle", "tableRow.tableRowStyle", "tableCell.tableCellStyle",
    # A picture's own editing, which `pull` reads off a deck and this does not.
    "inlineObject.inlineObjectProperties.embeddedObject.embeddedObjectBorder",
    "inlineObject.inlineObjectProperties.embeddedObject.marginTop",
    "inlineObject.inlineObjectProperties.embeddedObject.imageProperties.angle",
    "inlineObject.inlineObjectProperties.embeddedObject.imageProperties.brightness",
    "inlineObject.inlineObjectProperties.embeddedObject.imageProperties.cropProperties",
    # A list's own look, its start number among it.
    "nestingLevel.indentStart", "nestingLevel.startNumber", "nestingLevel.textStyle",
    # The document's theme: a named style's face, alignment, measures and marks are
    # read — the marks so that a run saying one of them *off* can be told from a run
    # inheriting it (`doc_ir.MARK_FIELDS`). Its colour is not: nothing subtracts it,
    # and a heading's blue survives a rewrite because no request names it.
    "namedStyle.textStyle.foregroundColor",
}


def test_what_the_reader_never_reads_is_named_one_by_one() -> None:
    """The other half of the convergence check. "A second sync writes 0 requests" is
    measured on the IR, so it proves the IR round-trips and says nothing at all about
    what the IR never looked at; this is the walker that says it."""
    found = doc_ir.unmodelled(UNMODELLED_LIVE)
    assert set(found) == UNMODELLED
    assert found["structural.paragraph.paragraphStyle.direction"] == {
        "count": 1, "example": "LEFT_TO_RIGHT"}
    assert found["nestingLevel.startNumber"]["example"] == "7"
    assert list(found) == sorted(found)


def test_a_document_of_nothing_but_what_we_read_reports_nothing() -> None:
    """Every path in the map, proved to be a path and not a hope: the fixture the
    rest of this file reads is walked and comes back empty."""
    assert doc_ir.unmodelled(STYLED_LIVE) == {}


def test_an_empty_struct_says_nothing_and_is_not_reported() -> None:
    """Docs leaves plenty of those about, and a report of them would be noise."""
    doc: JsonObject = {"documentId": "d", "documentStyle": {}, "headers": {},
           "body": {"content": [{"paragraph": {"elements": [], "paragraphStyle": {
               "borderBetween": {}, "tabStops": []}}}]}}
    assert doc_ir.unmodelled(doc) == {}


def test_what_a_rewrite_of_one_block_would_drop_is_named_block_by_block() -> None:
    """The document-wide count has no address, and an address is the whole use of it:
    a person deciding whether to edit a document through the file has to know which
    paragraph is the one with the border on it."""
    risky = doc_ir.unread_blocks(UNMODELLED_LIVE)
    # The paragraph carries three, the table three (its own style, a row's, a cell's),
    # and the section break is no block at all: nothing rewrites one.
    assert [b["kind"] for b in risky] == ["paragraph", "table"]
    assert set(risky[1]["unread"]) == {"structural.table.tableStyle",
                                       "tableRow.tableRowStyle",
                                       "tableCell.tableCellStyle"}
    assert risky[0]["words"] == "one two"
    assert set(risky[0]["unread"]) == {
        "structural.paragraph.paragraphStyle.borderBetween",
        "structural.paragraph.paragraphStyle.direction",
        "structural.paragraph.paragraphStyle.tabStops"}
    assert risky[0]["span"] == [1, 9]


def test_a_block_the_dialect_reads_whole_carries_no_risk() -> None:
    """The fixture the rest of this file reads: every block of it comes back clean,
    which is what makes a block that does not stand out."""
    assert doc_ir.unread_blocks(STYLED_LIVE) == []


def test_a_property_outside_the_blocks_is_no_block_s_risk() -> None:
    """`documentStyle`, `headers`, `footnotes` are the page, not a paragraph: no
    rewrite of a block can drop them, and naming a block for them would be a lie."""
    doc: Document = {"documentId": "d", "documentStyle": {"marginTop": {"magnitude": 72}},
           "headers": {"h1": {"content": []}},
           "body": {"content": [{"startIndex": 1, "endIndex": 5, "paragraph": {
               "elements": [{"startIndex": 1, "endIndex": 5,
                             "textRun": {"content": "abc\n", "textStyle": {}}}],
               "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}}]}}
    assert set(doc_ir.unmodelled(doc)) == {"documentStyle", "headers"}
    assert doc_ir.unread_blocks(doc) == []


def test_every_node_the_map_descends_into_is_a_node() -> None:
    """A map that names a node it has not got would walk into a KeyError on the one
    document that reaches it, which is no way to find out."""
    for node, (_, into) in doc_ir._NODES.items():
        for key, target in into.items():
            assert target.rstrip("[]{}") in doc_ir._NODES, f"{node}.{key} -> {target}"
