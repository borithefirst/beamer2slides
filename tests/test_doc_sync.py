"""Offline tests for the Docs sync command: its state, its writes and its report.

No Google calls — everything here is the part of `doc_sync` that only handles paths,
identifiers, the batches it would send and the summary a person reads afterwards.
Drive and Docs are the fakes at the bottom of this module.
"""

import json

import pytest

from beamer2slides import doc_ir, doc_merge, doc_sync

from .test_doc_merge import live, para, table


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
    assert doc_sync.load_local(path, "one") == {"document": "one", "blocks": []}
    assert doc_sync.load_local(path, "another") is None
    assert doc_sync.load_local(tmp_path / "nothing.html", "one") is None
    assert doc_sync.base_problem({"document": "one", "blocks": []}, "two") == \
        "it belongs to document one"
    assert doc_sync.base_problem({"document": "one"}, "one") == "no blocks"
    assert doc_sync.base_problem("not json at all", None) == "not a JSON object"


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
    assert doc_sync.limits(ir) == ir["unsupported"]
    # What push hands the importer: the bytes, not a path it could not follow.
    sent = doc_sync.embedded(path, ir)
    assert sent["blocks"][1]["runs"][0]["src"].startswith("data:image/png;base64,")
    assert ir["blocks"][1]["runs"][0]["src"] == "figures/plot.png"   # the file's own is untouched


def test_a_base_forgets_the_urls_that_die_within_the_hour(tmp_path):
    path = tmp_path / "doc.html"
    doc_sync.save_base(path, {"document": "d", "blocks": [{"kind": "paragraph", "runs": [
        {"chip": "image", "frozen": True, "text": "", "value": "i.0", "uri": "https://lh7/x"}]}]})
    run = doc_sync.load_local(path, "d")["blocks"][0]["runs"][0]
    assert run["value"] == "i.0" and "uri" not in run


# ---------------------------------------------------------------- Drive, faked

def _http(status: int):
    from googleapiclient.errors import HttpError
    return HttpError(type("R", (), {"status": status, "reason": "no"})(), b"{}")


class _Reply:
    def __init__(self, run):
        self.run = run

    def execute(self):
        return self.run()


class _Storage:
    """Drive as the base storage and the backup see it: a document with
    `appProperties`, a blob per file id, and an export."""

    def __init__(self, document="doc-1"):
        self.document, self.props, self.blobs = document, {}, {}
        self.created, self.exports, self.export_error = [], [], None
        self.refuse_create = self.refuse_rename = False
        self.names: list[str] = []
        self.n = 0

    def files(self):
        return self

    def get(self, fileId, fields=""):
        def run():
            if fileId != self.document:
                raise _http(404)
            return {"name": "The report", "parents": ["folder"],
                    "appProperties": dict(self.props)}
        return _Reply(run)

    def get_media(self, fileId):
        def run():
            if fileId not in self.blobs:
                raise _http(404)
            return self.blobs[fileId]
        return _Reply(run)

    def create(self, body, fields="", media_body=None):
        def run():
            if self.refuse_create:
                raise _http(403)
            self.n += 1
            fid = f"base-{self.n}"
            self.blobs[fid] = media_body._fd.getvalue()
            self.created.append(body)
            return {"id": fid}
        return _Reply(run)

    def update(self, fileId, fields="", body=None, media_body=None):
        def run():
            if media_body is not None:
                if fileId not in self.blobs:
                    raise _http(404)
                self.blobs[fileId] = media_body._fd.getvalue()
            if body and body.get("appProperties"):
                self.props.update(body["appProperties"])
            if body and "name" in body:
                if self.refuse_rename:
                    raise _http(403)
                self.names.append(body["name"])
            return {"id": fileId}
        return _Reply(run)

    def export(self, fileId, mimeType):
        def run():
            if self.export_error:
                raise self.export_error
            self.exports.append((fileId, mimeType))
            return b"<html>the document as it was</html>"
        return _Reply(run)


def _base(generation: int, key: str) -> dict:
    return {"document": "doc-1", "generation": generation,
            "blocks": [para(key, "some words")]}


def test_a_checkout_with_no_state_folder_finds_the_base_in_drive(tmp_path):
    """The headline: `.b2s/` is scratch state a fresh clone does not have, and
    before the base went to Drive that dropped the sync into the `--assume-base`
    dialog, where both answers throw somebody's work away."""
    drive = _Storage()
    doc_sync.save_drive(drive, "doc-1", _base(2, "p:drive"))
    problems = []
    base, where = doc_sync.load_base(tmp_path / "doc.html", "doc-1", drive, problems)
    assert where == "drive" and base["blocks"][0]["key"] == "p:drive"
    assert problems == []
    assert drive.props[doc_sync.BASE_PROPERTY] == "base-1"   # the document names it


def test_the_base_in_drive_beats_a_stale_copy_beside_the_file(tmp_path):
    path = tmp_path / "doc.html"
    drive = _Storage()
    doc_sync.save_base(path, _base(3, "p:stale"))
    doc_sync.save_drive(drive, "doc-1", _base(5, "p:drive"))
    problems = []
    base, where = doc_sync.load_base(path, "doc-1", drive, problems)
    assert where == "drive" and base["blocks"][0]["key"] == "p:drive"
    assert any("another checkout has synced" in line for line in problems), problems


def test_a_copy_newer_than_drives_is_the_one_used(tmp_path):
    """What a sync whose Drive upload failed leaves behind: the cache is ahead."""
    path = tmp_path / "doc.html"
    drive = _Storage()
    doc_sync.save_drive(drive, "doc-1", _base(2, "p:drive"))
    doc_sync.save_base(path, _base(4, "p:local"))
    problems = []
    base, where = doc_sync.load_base(path, "doc-1", drive, problems)
    assert where == "local" and base["blocks"][0]["key"] == "p:local"
    assert any("older than the copy beside the file" in line for line in problems), problems


def test_the_copy_is_used_with_a_word_about_it_when_drive_has_no_base(tmp_path):
    path = tmp_path / "doc.html"
    drive = _Storage()
    doc_sync.save_base(path, _base(1, "p:local"))
    problems = []
    base, where = doc_sync.load_base(path, "doc-1", drive, problems)
    assert where == "local" and base["blocks"][0]["key"] == "p:local"
    assert any("Drive has none" in line for line in problems), problems


def test_a_base_drive_names_but_cannot_serve_is_said_out_loud(tmp_path):
    """Deleted, or somebody else's now. Nothing is lost by syncing from the copy —
    the document wins where both moved — but the copy may be older than the
    document, and that is the person's to know."""
    path = tmp_path / "doc.html"
    drive = _Storage()
    doc_sync.save_drive(drive, "doc-1", _base(2, "p:drive"))
    drive.blobs.clear()
    doc_sync.save_base(path, _base(1, "p:local"))
    problems = []
    base, where = doc_sync.load_base(path, "doc-1", drive, problems)
    assert where == "local" and base["blocks"][0]["key"] == "p:local"
    assert any("cannot be read" in line for line in problems), problems


def test_a_base_from_another_document_is_ignored_wherever_it_sits(tmp_path):
    path = tmp_path / "doc.html"
    drive = _Storage()
    doc_sync.save_drive(drive, "doc-1", {"document": "elsewhere", "blocks": []})
    doc_sync.save_base(path, {"document": "elsewhere", "blocks": []})
    problems = []
    assert doc_sync.load_base(path, "doc-1", drive, problems) == (None, "none")
    assert len(problems) == 2 and all("belongs to document elsewhere" in p for p in problems)


def test_with_no_base_anywhere_the_assume_base_dialog_is_what_is_left(tmp_path):
    path = tmp_path / "doc.html"
    assert doc_sync.load_base(path, "doc-1", _Storage(), []) == (None, "none")
    with pytest.raises(SystemExit) as raised:
        doc_sync._no_base(path, {"blocks": []}, {"blocks": []}, None)
    said = str(raised.value)
    assert "document-wins" in said and "source-wins" in said
    # and it says, for each answer, whose work it discards.
    assert "every edit made to the source since the last sync is discarded" in said
    assert "every edit a reader made there since the last sync is discarded" in said


def test_a_stored_base_goes_to_both_places_and_the_count_rises(tmp_path):
    path = tmp_path / "doc.html"
    drive = _Storage()
    assert doc_sync.store_base(path, _base(0, "p:one"), drive, "doc-1", previous=4) is None
    assert doc_sync.load_local(path, "doc-1")["generation"] == 5
    assert doc_sync.load_drive(drive, "doc-1")["generation"] == 5
    doc_sync.store_base(path, _base(0, "p:one"), drive, "doc-1", previous=5)
    assert len(drive.created) == 1                       # the same file, written again
    assert doc_sync.load_drive(drive, "doc-1")["generation"] == 6


def test_a_drive_write_that_fails_keeps_the_cache_and_says_why(tmp_path):
    """A Drive write that fails must never fail the sync: the document has already
    been written by then, and the cache is a base the next run can still use."""
    path = tmp_path / "doc.html"
    drive = _Storage()
    drive.refuse_create = True
    why = doc_sync.store_base(path, _base(0, "p:one"), drive, "doc-1")
    assert why and "HttpError" in why
    assert doc_sync.load_local(path, "doc-1")["generation"] == 1


def test_the_document_is_renamed_through_drive_and_a_refusal_says_so():
    """A Google Doc's title is its Drive name: no `batchUpdate` request writes one,
    so the file's `<title>` reaches the document only this way — and a rename Drive
    refuses fails nothing, as a base it refuses does not."""
    drive, problems = _Storage(), []
    assert doc_sync.rename_document(drive, "doc-1", "A better name", problems) == \
        "A better name"
    assert drive.names == ["A better name"] and problems == []
    drive.refuse_rename = True
    assert doc_sync.rename_document(drive, "doc-1", "Nope", problems) is None
    assert "could not be renamed 'Nope'" in problems[0]
    assert "keeps the name it has" in problems[0]


class _OneRead:
    """`documents.get` of a document with nothing in it but its name."""

    def __init__(self, title):
        self.title = title

    def documents(self):
        return self

    def get(self, documentId, includeTabsContent=False):
        return _Reply(lambda: {"title": self.title, "revisionId": "r1",
                               "body": {"content": []}})


def test_a_rename_drive_has_made_is_what_the_file_and_the_base_say(tmp_path):
    """`documents.get` need not have caught up with a Drive rename, and if the settle
    took the name it reads, the file would go straight back to the old one — the
    rename undone the moment it was made, and nothing to try again next time, since
    the base would agree with the file."""
    path = tmp_path / "doc.html"
    live = doc_sync.settle(_OneRead("The old name"), "doc-1", path,
                           {"blocks": []}, {"blocks": []}, renamed="A better name")
    assert live["title"] == "A better name"
    assert "<title>A better name</title>" in path.read_text(encoding="utf-8")
    assert doc_sync.load_local(path, "doc-1")["title"] == "A better name"
    # Without one, the settle says whatever the read said.
    doc_sync.settle(_OneRead("The old name"), "doc-1", path, {"blocks": []}, {"blocks": []})
    assert "<title>The old name</title>" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- --assume-base

def test_the_assume_base_names_say_which_side_loses(capsys):
    assert doc_sync.assume_mode(None) is None
    assert doc_sync.assume_mode("document-wins") == "document-wins"
    assert doc_sync.assume_mode("source-wins") == "source-wins"
    assert capsys.readouterr().out == ""
    # The old names read backwards — they named the side the base is taken *from*,
    # which is the side whose changes are thereby thrown away.
    assert doc_sync.assume_mode("file") == "document-wins"
    assert "old name" in capsys.readouterr().out
    assert doc_sync.assume_mode("document") == "source-wins"
    assert "reader made there" in capsys.readouterr().out


def test_the_destructive_direction_exports_the_document_first(tmp_path):
    path = tmp_path / "doc.html"
    drive = _Storage()
    ours, theirs = {"blocks": ["the file"]}, {"blocks": ["the document"]}
    base, kept = doc_sync._no_base(path, ours, theirs, "source-wins", drive, "doc-1")
    assert base is theirs                       # every difference is the source's
    assert kept and kept.parent == tmp_path / ".b2s" / "backups"
    assert kept.read_bytes() == b"<html>the document as it was</html>"
    assert drive.exports == [("doc-1", "text/html")]
    # The other direction writes nothing to the document, so it needs no way back.
    base, kept = doc_sync._no_base(path, ours, theirs, "document-wins", drive, "doc-1")
    assert base is ours and kept is None and len(drive.exports) == 1


def test_a_dry_run_of_the_destructive_direction_exports_nothing(tmp_path):
    """`--dry-run` is how one looks at that answer before giving it, and a look must
    leave nothing behind: the backup is taken for the write, and there is no write."""
    path = tmp_path / "doc.html"
    drive = _Storage()
    ours, theirs = {"blocks": ["the file"]}, {"blocks": ["the document"]}
    base, kept = doc_sync._no_base(path, ours, theirs, "source-wins", drive, "doc-1",
                                   dry_run=True)
    assert base is theirs and kept is None
    assert drive.exports == [] and not (tmp_path / ".b2s" / "backups").exists()


def test_a_backup_drive_refuses_stops_that_sync(tmp_path):
    """`guard.demand_way_back`'s principle: a write with no way back is something
    one asks for, and never something that happens because an export failed."""
    path = tmp_path / "doc.html"
    drive = _Storage()
    drive.export_error = _http(403)
    ours, theirs = {"blocks": ["the file"]}, {"blocks": ["the document"]}
    with pytest.raises(SystemExit) as raised:
        doc_sync._no_base(path, ours, theirs, "source-wins", drive, "doc-1")
    assert "--no-backup" in str(raised.value)
    base, kept = doc_sync._no_base(path, ours, theirs, "source-wins", drive, "doc-1",
                                   backup=False)
    assert base is theirs and kept is None


# ---------------------------------------------------------------- batching

class _Batches:
    """Docs' `batchUpdate`, remembering every body it was given."""

    def __init__(self, revisions=True):
        self.sent, self.revisions, self.n = [], revisions, 0

    def documents(self):
        return self

    def batchUpdate(self, documentId, body):
        def run():
            self.n += 1
            self.sent.append(body)
            out = {"replies": [{"n": i} for i in range(len(body["requests"]))]}
            if self.revisions:
                out["writeControl"] = {"requiredRevisionId": f"rev{self.n}"}
            return out
        return _Reply(run)


def _texts(count):
    return [{"insertText": {"location": {"index": i + 1}, "text": str(i)}}
            for i in range(count)]


def test_a_plan_up_to_the_threshold_is_one_batch_and_stays_atomic():
    docs, notes = _Batches(), []
    doc_sync.send(docs, "d", _texts(doc_sync.CHUNK), "rev0", notes)
    assert len(docs.sent) == 1 and notes == []
    assert docs.sent[0]["writeControl"] == {"requiredRevisionId": "rev0"}


def test_a_long_plan_is_cut_in_order_with_the_revision_chained():
    """Docs applies a batch in order, so consecutive batches write the same
    document — as long as nothing is reordered and no request crosses a boundary.
    The guard has to carry across: each batch requires the revision the one before
    it produced, so somebody typing half-way through the run is still refused."""
    docs, notes = _Batches(), []
    requests = _texts(doc_sync.CHUNK * 2 + 3)
    answer = doc_sync.send(docs, "d", requests, "rev0", notes)
    assert [len(body["requests"]) for body in docs.sent] == [doc_sync.CHUNK, doc_sync.CHUNK, 3]
    assert [r for body in docs.sent for r in body["requests"]] == requests
    assert [body["writeControl"]["requiredRevisionId"] for body in docs.sent] == \
        ["rev0", "rev1", "rev2"]
    assert len(answer["replies"]) == len(requests)
    # And the atomicity it costs is said, not hidden.
    assert notes and "3 batches" in notes[0] and "fails part-way" in notes[0]


def test_without_a_write_control_in_the_answer_the_later_batches_go_unguarded():
    """Unmeasured against the live API: whether the answer always carries one. If
    it does not, the chain stops guarding rather than sending a stale revision."""
    docs = _Batches(revisions=False)
    doc_sync.send(docs, "d", _texts(doc_sync.CHUNK + 1), "rev0")
    assert "writeControl" in docs.sent[0] and "writeControl" not in docs.sent[1]


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


def _tab(tab, title, words, ranges=None, children=()):
    body = [{"startIndex": 1, "endIndex": 2 + len(words), "paragraph": {"elements": [
        {"startIndex": 1, "endIndex": 2 + len(words), "textRun": {"content": words + "\n"}}]}}]
    return {"tabProperties": {"tabId": tab, "title": title},
            "documentTab": {"body": {"content": body}, "namedRanges": ranges or {}},
            "childTabs": list(children)}


def test_every_tab_is_read_with_its_own_keys_and_written_to_the_file():
    ranges = {"b2s:paragraph:notes": {"namedRanges": [{"namedRangeId": "r", "ranges": [
        {"startIndex": 1, "endIndex": 5, "tabId": "t.1"}]}]}}
    child = _tab("t.2", "Older notes", "old") | {"tabProperties": {
        "tabId": "t.2", "title": "Older notes", "parentTabId": "t.1"}}
    doc = {"title": "D", "tabs": [_tab("t.0", "Tab 1", "chapter"),
                                  _tab("t.1", "Notes", "notes", ranges, [child])]}
    ir = doc_sync.document_ir(doc, "d")
    assert [b["runs"][0]["text"] for b in ir["blocks"]] == ["chapter"]
    notes, older = ir["tabs"]
    assert (notes["tab"], notes["title"], notes["blocks"][0]["key"]) == ("t.1", "Notes",
                                                                        "paragraph:notes")
    assert (older["tab"], older["title"], older["parent"]) == ("t.2", "Older notes", "t.1")
    html = doc_ir.to_html(doc_ir.key_blocks(ir))
    assert '<section data-tab="t.2" title="Older notes" data-parent="t.1">' in html
    again = doc_ir.from_html(html)
    assert [(p.get("tab"), p.get("title")) for p in doc_ir.parts(again)[1:]] == [
        ("t.1", "Notes"), ("t.2", "Older notes")]
    assert again["blocks"][0]["key"] == ir["blocks"][0]["key"]
    assert again["tabs"][0]["blocks"][0]["key"] == "paragraph:notes"
    assert doc_ir.to_html(again | {"document": "d"}) == html


def test_a_tab_just_added_is_empty_and_what_is_written_there_goes_into_it():
    doc = {"tabs": [_tab("t.0", "Tab 1", "chapter"), _tab("t.9", "New", "")]}
    part = doc_ir.parts(doc_sync.document_ir(doc, "d"))[1]
    assert part["blocks"] == [] and part["trailer"] == [1, 2]


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


# ------------------------------------------- what the document has and the file cannot

def _unmodelled_doc(**style) -> dict:
    return {"documentId": "d", "body": {"content": [{"paragraph": {
        "elements": [], "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"} | style}}]}}


def test_a_sync_says_how_much_of_the_document_the_file_cannot_say():
    """One line, not fifteen: a sync report that repeats the whole list every run is
    a report nobody reads twice."""
    doc = _unmodelled_doc(keepWithNext=True, direction="LEFT_TO_RIGHT",
                          pageBreakBefore=True, borderLeft={"width": {"magnitude": 1}})
    notes = doc_sync.unmodelled_notes(doc)
    assert len(notes) == 1
    assert notes[0].startswith("4 kinds of document property this file cannot say "
                               "(structural.paragraph.paragraphStyle.borderLeft, ")
    assert "and 1 more" in notes[0]
    # The one number a sync can act on: naming the blocks is `adopt`'s job.
    assert "1 block(s) carry one" in notes[0]


def test_adopt_names_every_one_of_them():
    """A document somebody else wrote is handed over once, and that is the moment to
    say what will not survive being written again."""
    doc = _unmodelled_doc(keepWithNext=True, pageBreakBefore=True)
    notes = doc_sync.unmodelled_notes(doc, full=True)
    assert notes[:2] == [
        "the document has 1 × structural.paragraph.paragraphStyle.keepWithNext "
        "(e.g. True), which the canonical file cannot say",
        "the document has 1 × structural.paragraph.paragraphStyle.pageBreakBefore "
        "(e.g. True), which the canonical file cannot say"]


def test_a_document_the_dialect_covers_is_said_nothing_about():
    assert doc_sync.unmodelled_notes(_unmodelled_doc(alignment="CENTER"), full=True) == []


def _said_doc(*paragraphs: tuple[str, dict]) -> dict:
    """A body of paragraphs, each with its words and its paragraph style."""
    content, at = [], 1
    for text, style in paragraphs:
        end = at + len(text) + 1
        content.append({"startIndex": at, "endIndex": end, "paragraph": {
            "elements": [{"startIndex": at, "endIndex": end,
                          "textRun": {"content": text + "\n", "textStyle": {}}}],
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"} | style}})
        at = end
    return {"documentId": "d", "body": {"content": content}}


def test_the_block_that_would_lose_something_is_named_by_its_own_words():
    """The counts say the document has a border somewhere, which is true and of no
    use: the person about to edit a paragraph needs to know it is *that* one."""
    doc = _said_doc(("Plain enough", {}),
                    ("Why this matters", {"borderBottom": {"width": {"magnitude": 1}}}))
    notes = doc_sync.block_risk_notes(doc)
    assert notes == ["the paragraph 'Why this matters' carries "
                     "paragraphStyle.borderBottom; rewriting that block "
                     "through the file would drop it"]


def test_a_block_carrying_nothing_the_file_misses_is_not_named():
    """Or the report would list the whole document and say nothing."""
    assert doc_sync.block_risk_notes(_said_doc(("Plain enough", {}))) == []


def test_the_blocks_are_named_most_laden_first_and_the_tail_is_counted():
    doc = _said_doc(*[(f"line {n}", {"keepWithNext": True}) for n in range(10)],
                    ("the heavy one", {"keepWithNext": True, "pageBreakBefore": True,
                                       "borderLeft": {"width": {"magnitude": 1}}}))
    notes = doc_sync.block_risk_notes(doc, limit=3)
    assert notes[0].startswith("the paragraph 'the heavy one' carries "
                               "paragraphStyle.borderLeft, ")
    assert len(notes) == 4
    assert notes[-1].startswith("and 8 more blocks carry something the file cannot say")


def _planned(doc: dict, reorder: bool = False, reword: bool = False) -> list[dict]:
    """One tab's plan over a document, keyed as a synced document would be."""
    import copy

    from beamer2slides import doc_ir, doc_merge
    theirs = doc_ir.from_document(doc)
    for n, block in enumerate(theirs["blocks"]):
        block["key"] = f"p:{n}"
    base, ours = copy.deepcopy(theirs), copy.deepcopy(theirs)
    if reorder:
        ours["blocks"] = ours["blocks"][-1:] + ours["blocks"][:-1]
    if reword:
        ours["blocks"][-1]["runs"] = [{"text": "Bordered words, reworded"}]
    return [{"stamp": None, "label": None,
             "result": doc_merge.plan(base, ours, copy.deepcopy(theirs))}]


def test_the_block_this_sync_is_about_to_cost_something_is_named_as_a_loss():
    """The risk notes say what a rewrite *would* drop, which is a caution. Once the
    plan exists, the question has an answer: this run moves that very block, and a
    move is a delete and a write, so the border is going. Said before the write."""
    doc = _said_doc(("First words", {}), ("Second words", {}),
                    ("Bordered words", {"borderBottom": {"width": {"magnitude": 1}}}))
    assert doc_sync.rewrite_losses(doc, _planned(doc, reorder=True)) == [
        "the paragraph 'Bordered words' is being moved, which drops "
        "paragraphStyle.borderBottom — the document's, and in nothing the file can say"]


def test_a_block_whose_words_merely_change_costs_nothing_and_is_not_named():
    """The whole distinction: a request names the fields it writes, and no field the
    merge owns is a field nobody reads. A report that cried loss on every edit to a
    bordered paragraph would teach whoever reads it to skip the line."""
    doc = _said_doc(("First words", {}), ("Second words", {}),
                    ("Bordered words", {"borderBottom": {"width": {"magnitude": 1}}}))
    planned = _planned(doc, reword=True)
    assert planned[0]["result"]["requests"], "the source edit reached no request"
    assert doc_sync.rewrite_losses(doc, planned) == []


def test_a_document_with_nothing_unread_says_nothing_however_much_moves():
    doc = _said_doc(("First words", {}), ("Second words", {}), ("Third words", {}))
    assert doc_sync.rewrite_losses(doc, _planned(doc, reorder=True)) == []
