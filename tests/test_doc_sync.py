"""Offline tests for the Docs sync command: its state beside the file, and its report.

No Google calls — everything here is the part of `doc_sync` that only handles paths,
identifiers and the summary a person reads afterwards.
"""

import json

from beamer2slides import doc_sync

from test_doc_merge import live, para, table


def test_a_document_is_found_in_a_url_an_id_or_a_file():
    assert doc_sync.document_id("https://docs.google.com/document/d/1AbC_-x/edit#heading=h.2") == "1AbC_-x"
    assert doc_sync.document_id("https://docs.google.com/document/d/1AbC_-x") == "1AbC_-x"
    assert doc_sync.document_id(" 1AbC_-x ") == "1AbC_-x"


def test_the_state_sits_in_a_b2s_folder_beside_the_file(tmp_path):
    path = tmp_path / "book" / "chapter.html"
    assert doc_sync.base_path(path) == tmp_path / "book" / ".b2s" / "chapter.base.json"


def test_a_base_of_another_document_is_no_base(tmp_path):
    path = tmp_path / "doc.html"
    doc_sync.save_base(path, {"document": "one", "blocks": []})
    assert doc_sync.load_base(path, "one") == {"document": "one", "blocks": []}
    assert doc_sync.load_base(path, "another") is None
    assert doc_sync.load_base(tmp_path / "nothing.html", "one") is None


def test_the_report_keeps_its_name_beside_a_file_called_doc_html(tmp_path):
    path = tmp_path / "doc.html"
    info = {"url": "u", "requests": 2, "dry_run": True, "conflicts": [], "notes": ["a note"],
            "applied": ["`p:one` rewritten: 'hello'"], "kept": []}
    report = doc_sync.write_report(path, info)
    # Not `with_suffix`: `doc.sync-report` already looks suffixed, and the report would
    # land in `doc.md` — next to a file called doc.html, and overwriting it next time.
    assert report.name == "doc.sync-report.md"
    assert json.loads((tmp_path / ".b2s" / "doc.sync-report.json").read_text("utf-8"))["requests"] == 2
    assert "a note" in report.read_text(encoding="utf-8")


def test_the_report_says_what_each_side_contributed():
    applied, kept = doc_sync._summary({"blocks": [
        {"key": "p:new", "kind": "paragraph", "origin": "added by the source",
         "runs": [{"text": "written from the file"}]},
        {"key": "p:one", "kind": "paragraph", "origin": "merged", "runs": [{"text": "merged words"}]},
        {"key": "p:two", "kind": "paragraph", "origin": "kept from the document",
         "runs": [{"text": "typed by a reader"}]},
        {"key": "p:three", "kind": "paragraph", "runs": [{"text": "untouched"}]}]})
    assert applied == ["`p:new` added: 'written from the file'",
                       "`p:one` rewritten: 'merged words'"]
    assert kept == ["`p:two` says what the document says: 'typed by a reader'"]


def test_a_table_is_reported_by_its_cells():
    grid = table("t:one", [["Region", "Sales"], ["North", "1200"]])
    applied, kept = doc_sync._summary({"blocks": [grid | {"origin": "merged"}]})
    assert applied == ["`t:one` rewritten: 'Region | Sales | North | 1200'"]
    assert kept == []


def test_a_document_edit_inside_a_cell_is_reported_as_the_documents():
    """The table itself has no words, so what its cells did is what it did."""
    from beamer2slides import doc_merge
    base = live([table("t:one", [["Region", "Sales"], ["North", "1200"]])])
    theirs = live([table("t:one", [["Region", "Sales"], ["North", "1500"]])])
    result = doc_merge.plan(base, live(base["blocks"]), theirs)
    assert result["requests"] == []
    assert doc_sync._summary(result)[1] == ["`t:one` says what the document says: "
                                            "'Region | Sales | North | 1500'"]
