"""Offline tests for the Docs sync command: its state beside the file, and its report.

No Google calls — everything here is the part of `doc_sync` that only handles paths,
identifiers and the summary a person reads afterwards.
"""

import json

from beamer2slides import doc_merge, doc_sync

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


def test_a_picture_file_is_digested_and_a_missing_one_is_reported(tmp_path):
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures" / "plot.png").write_bytes(b"pixels")
    path = tmp_path / "doc.html"
    path.write_text("<html><body><p>before</p><p><img src='figures/plot.png'></p>"
                    "<p><img src='figures/gone.png'></p>"
                    "<p><img src='https://example.com/x.png'></p></body></html>", encoding="utf-8")
    ir = doc_sync.read_file(path)
    plot, gone, remote = (b["runs"][0] for b in ir["blocks"][1:])
    assert plot["sha"] == doc_sync.digest(b"pixels") and not plot.get("missing")
    assert gone["missing"] and "figures/gone.png" in ir["unsupported"][0]
    assert "sha" not in remote and not remote.get("missing")      # a URL: Google fetches it
    assert doc_sync.limits(ir, {}) == ir["unsupported"]
    # What push hands the importer: the bytes, not a path it could not follow.
    sent = doc_sync.embedded(path, ir)
    assert sent["blocks"][1]["runs"][0]["src"].startswith("data:image/png;base64,")
    assert ir["blocks"][1]["runs"][0]["src"] == "figures/plot.png"   # the file's own is untouched


def test_a_base_forgets_the_urls_that_die_within_the_hour(tmp_path):
    path = tmp_path / "doc.html"
    doc_sync.save_base(path, {"document": "d", "blocks": [{"kind": "paragraph", "runs": [
        {"chip": "image", "frozen": True, "text": "", "value": "i.0", "uri": "https://lh7/x"}]}]})
    run = doc_sync.load_base(path, "d")["blocks"][0]["runs"][0]
    assert run["value"] == "i.0" and "uri" not in run


class _Staging:
    """Drive and Docs as the stager sees them: an import, a read, a delete."""

    def __init__(self):
        self.created, self.deleted, self.html = [], [], ""

    def files(self):
        return self

    def documents(self):
        return self

    def create(self, body, media_body, fields):
        self.html = media_body._fd.getvalue().decode()
        self.created.append(body)
        return self

    def delete(self, fileId):
        self.deleted.append(fileId)
        return self

    def get(self, documentId, includeTabsContent):
        self.got = documentId
        return self

    def execute(self):
        if self.html and not self.created[-1].get("done"):
            self.created[-1]["done"] = True
            return {"id": "staging"}
        if getattr(self, "got", None):
            import re
            content, at = [], 1
            for label in re.findall(r"<p>(\d+):", self.html):
                content.append({"startIndex": at, "endIndex": at + len(label) + 3, "paragraph": {
                    "elements": [{"startIndex": at, "endIndex": at + len(label) + 1,
                                  "textRun": {"content": label + ":"}},
                                 {"startIndex": at + len(label) + 1, "endIndex": at + len(label) + 2,
                                  "inlineObjectElement": {"inlineObjectId": f"i.{label}"}},
                                 {"startIndex": at + len(label) + 2, "endIndex": at + len(label) + 3,
                                  "textRun": {"content": "\n"}}]}})
                at += len(label) + 3
            objects = {f"i.{n}": {"inlineObjectProperties": {"embeddedObject": {
                "imageProperties": {"contentUri": f"https://lh7/{n}"}}}}
                for n in range(len(content))}
            self.got = None
            return {"body": {"content": content}, "inlineObjects": objects}
        return {}


def test_pictures_are_staged_once_and_the_staging_document_goes(tmp_path):
    from beamer2slides import doc_merge
    for name in ("a.png", "b.png"):
        (tmp_path / name).write_bytes(name.encode())
    google = _Staging()
    stager = doc_sync.Stager(google, google, tmp_path / "doc.html")
    requests = [{"insertInlineImage": {"location": {"index": 5}, "uri": doc_merge.STAGE + "b.png"}},
                {"insertText": {"location": {"index": 1}, "text": "x"}},
                {"insertInlineImage": {"location": {"index": 2}, "uri": doc_merge.STAGE + "a.png"}},
                {"insertInlineImage": {"location": {"index": 1}, "uri": "https://example.com/c.png"}}]
    sent = stager.resolve(requests)
    assert [r["insertInlineImage"]["uri"] for r in sent if "insertInlineImage" in r] == [
        "https://lh7/1", "https://lh7/0", "https://example.com/c.png"]
    assert "data:image/png;base64," in google.html and len(google.created) == 1
    stager.resolve(requests)                       # a re-plan stages nothing new
    assert len(google.created) == 1
    stager.close()
    assert google.deleted == ["staging"]


def test_only_the_first_tab_is_synced_and_the_others_are_named():
    doc = {"tabs": [{"tabProperties": {"tabId": "t.0", "title": "The chapter"}},
                    {"tabProperties": {"tabId": "t.1", "title": "Notes"},
                     "childTabs": [{"tabProperties": {"tabId": "t.2", "title": "Older notes"}}]}]}
    notes = doc_sync.limits({"blocks": []}, doc)
    assert notes == ["the document has 3 tabs; only the first one is synced "
                     "(left alone: Notes, Older notes)"]
    assert doc_sync.limits({"blocks": []}, {"tabs": [doc["tabs"][0]]}) == []


class _Drive:
    """Drive's comments endpoint, or the error it raises instead."""

    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.asked = payload, error, None

    def comments(self):
        return self

    def list(self, **kwargs):
        self.asked = kwargs
        return self

    def execute(self):
        if self.error:
            raise self.error
        return self.payload


def test_the_open_comments_are_read_and_the_resolved_ones_left_out():
    """A comment is a question about a passage, and the merge cannot see one: it lives
    in Drive, not in the document's content. So the report says they are there."""
    drive = _Drive({"comments": [
        {"content": "is this still true?", "author": {"displayName": "Ada"},
         "quotedFileContent": {"value": "The closing paragraph."},
         "replies": [{"content": "checking"}]},
        {"content": "fixed", "author": {"displayName": "Ada"}, "resolved": True}]})
    assert doc_sync.open_comments(drive, "id") == [
        "Ada on 'The closing paragraph.': 'is this still true?', and 1 reply"]
    assert drive.asked["fileId"] == "id" and drive.asked["includeDeleted"] is False


def test_comments_that_cannot_be_read_are_said_not_raised():
    from googleapiclient.errors import HttpError
    error = HttpError(type("R", (), {"status": 403, "reason": "no"})(), b"{}")
    assert doc_sync.open_comments(_Drive(error=error), "id") == [
        "the document's comments could not be read (403)"]


def test_the_report_has_a_section_for_the_open_comments(tmp_path):
    path = tmp_path / "doc.html"
    report = doc_sync.write_report(path, {
        "url": "u", "requests": 0, "dry_run": True, "conflicts": [], "notes": [],
        "applied": [], "kept": [], "comments": ["Ada on 'a passage': 'why?'"]})
    text = report.read_text(encoding="utf-8")
    assert "## Open comments in the document" in text
    assert "- Ada on 'a passage': 'why?'" in text


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
