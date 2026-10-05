"""Offline tests for the Docs sync command: its state, its writes and its report.

No Google calls — everything here is the part of `doc_sync` that only handles paths,
identifiers, the batches it would send and the summary a person reads afterwards.
Drive and Docs are the fakes at the bottom of this module.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, TypeVar

import pytest

from beamer2slides import doc_ir, doc_merge, doc_sync, drive_folder
from beamer2slides.doc_ir import Ir
from beamer2slides.doc_sync import StoredBase, SyncReport
from beamer2slides.google_types import (CommentList, Comments, DocsBatchUpdateBody, DocsBatchUpdateResponse,
                                        DocsNamedRanges, DocsParagraphStyle, DocsRequest, DocsStructuralElement,
                                        DocsTab, Document, Documents, DriveFile, DriveService, Empty, FileBody,
                                        Files, MediaBody, Request)
from beamer2slides.json_types import Json, JsonObject
from beamer2slides.typing_compat import override

from .doc_records import images_in, key_of, runs_of, tabs_of
from .fake_google import Answer, Later, NoComments, NoDocs, NoDocuments, NoDrive, NoFiles
from .test_doc_merge import live, para, table

if TYPE_CHECKING:
    from googleapiclient.errors import HttpError
    from typing_extensions import Unpack

    from beamer2slides.google_types import (CreateFile, ExportFile, FileId, GetDocument, GetFile, ListComments,
                                            UpdateDocument, UpdateFile)

T = TypeVar("T")


def _problem(data: Json, document: str | None) -> str:
    """Why `data` is no base of `document`, where the test knows it is none."""
    problem = doc_sync.base_problem(data, document)
    assert problem is not None, f"a base of {document}: {data}"
    return problem


def _local(path: Path, document: str) -> StoredBase:
    """The cache beside the file, where the test knows there is one."""
    base = doc_sync.load_local(path, document)
    assert base is not None, f"no base of {document} beside {path}"
    return base


def _found(base: StoredBase | None) -> StoredBase:
    assert base is not None, "no base was found"
    return base


def test_a_document_is_found_in_a_url_an_id_or_a_file() -> None:
    assert doc_sync.document_id("https://docs.google.com/document/d/1AbC_-x/edit#heading=h.2") == "1AbC_-x"
    assert doc_sync.document_id("https://docs.google.com/document/d/1AbC_-x") == "1AbC_-x"
    assert doc_sync.document_id(" 1AbC_-x ") == "1AbC_-x"


def test_the_state_sits_in_a_b2s_folder_beside_the_file(tmp_path: Path) -> None:
    path = tmp_path / "book" / "chapter.html"
    assert doc_sync.base_path(path) == tmp_path / "book" / ".b2s" / "chapter.base.json"


def test_a_base_of_another_document_is_no_base(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    doc_sync.save_base(path, {"document": "one", "blocks": []})
    assert doc_sync.load_local(path, "one") == {"document": "one", "blocks": []}
    assert doc_sync.load_local(path, "another") is None
    assert doc_sync.load_local(tmp_path / "nothing.html", "one") is None
    assert doc_sync.base_problem({"document": "one", "blocks": []}, "two") == \
        "it belongs to document one"
    assert doc_sync.base_problem({"document": "one"}, "one") == "no blocks"
    assert doc_sync.base_problem("not json at all", None) == "not a JSON object"


def test_a_malformed_base_fails_where_it_is_read_and_says_where(tmp_path: Path) -> None:
    """A base whose values are of another shape than its keys hold is refused by the
    parse that reads it, naming the value, and so never reaches the merge - where it
    broke before as a KeyError or a TypeError deep in the plan, with the file and the
    document half settled."""
    path = tmp_path / "doc.html"
    bad: JsonObject = {"document": "d", "blocks": [{"kind": "paragraph", "runs": [{"text": 3}]}]}
    assert "the base.blocks[0].runs[0].text" in _problem(bad, "d")
    assert "the base.blocks[0].kind" in _problem(
        {"document": "d", "blocks": [{"runs": []}]}, "d")
    assert "the base.generation" in _problem(
        {"document": "d", "blocks": [], "generation": "several"}, "d")
    doc_sync.base_path(path).parent.mkdir(parents=True)
    doc_sync.base_path(path).write_text(json.dumps(bad), encoding="utf-8")
    problems: list[str] = []
    assert doc_sync.load_base(path, "d", None, problems, None) == (None, "none")
    assert len(problems) == 1 and "the base beside the file was ignored" in problems[0]
    assert "blocks[0].runs[0].text" in problems[0]


def test_a_new_tab_whose_answer_names_no_id_is_a_shape_error_not_an_index_error() -> None:
    from beamer2slides.json_types import JsonShapeError
    made: DocsBatchUpdateResponse = {"replies": [{"addDocumentTab": {"tabProperties": {"tabId": "t.2"}}}]}
    assert doc_sync._new_tab_id(made) == "t.2"
    answers: tuple[tuple[DocsBatchUpdateResponse, str], ...] = (
        ({}, "no reply"), ({"replies": []}, "no reply"),
        ({"replies": [{}]}, "the addDocumentTab reply"),
        ({"replies": [{"addDocumentTab": {}}]}, "tabProperties"),
        ({"replies": [{"addDocumentTab": {"tabProperties": {"tabId": 7}}}]}, "tabId"))
    for answer, said in answers:
        with pytest.raises(JsonShapeError, match=said):
            doc_sync._new_tab_id(answer)


def _report(requests: int, dry_run: bool, notes: list[str], applied: list[str],
            comments: list[str], removed: list[str]) -> SyncReport:
    """A report with no conflicts and nothing kept, as `write_report` is handed one."""
    return {"document": "d", "url": "u", "requests": requests, "dry_run": dry_run, "conflicts": [],
            "notes": notes, "applied": applied, "kept": [], "comments": comments, "removed": removed}


def test_the_report_keeps_its_name_beside_a_file_called_doc_html(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    info = _report(2, True, ["a note"], ["`p:one` rewritten: 'hello'"], [], [])
    report = doc_sync.write_report(path, info)
    # Not `with_suffix`: `doc.sync-report` already looks suffixed, and the report would
    # land in `doc.md` — next to a file called doc.html, and overwriting it next time.
    assert report.name == "doc.sync-report.md"
    assert json.loads((tmp_path / ".b2s" / "doc.sync-report.json").read_text("utf-8"))["requests"] == 2
    assert "a note" in report.read_text(encoding="utf-8")


def test_a_picture_file_is_digested_and_a_missing_one_is_reported(tmp_path: Path) -> None:
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures" / "plot.png").write_bytes(b"pixels")
    path = tmp_path / "doc.html"
    path.write_text("<html><body><p>before</p><p><img src='figures/plot.png'></p>"
                    "<p><img src='figures/gone.png'></p>"
                    "<p><img src='https://example.com/x.png'></p></body></html>", encoding="utf-8")
    ir = doc_sync.read_file(path)
    plot, gone, remote = (runs_of(b)[0] for b in ir["blocks"][1:])
    unsupported = ir.get("unsupported")
    assert unsupported is not None, "the missing picture is said"
    assert plot.get("sha") == doc_sync.digest(b"pixels") and not plot.get("missing")
    assert gone.get("missing") and "figures/gone.png" in unsupported[0]
    assert "sha" not in remote and not remote.get("missing")      # a URL: Google fetches it
    assert doc_sync.limits(ir) == unsupported
    # What push hands the importer: the bytes, not a path it could not follow.
    sent = doc_sync.embedded(path, ir)
    assert (runs_of(sent["blocks"][1])[0].get("src") or "").startswith("data:image/png;base64,")
    assert runs_of(ir["blocks"][1])[0].get("src") == "figures/plot.png"   # the file's own is untouched


def test_words_outside_every_block_stop_the_sync_rather_than_disappearing(tmp_path: Path) -> None:
    # One letter wrong - `<it>` for `<li>` - and the item is read by nothing: it reaches
    # the document through no request, and the settle then writes the file again from the
    # document and takes the words out of the file too. Measured on the playground, where
    # a run said "0 requests, the two sides already say the same thing" about a list item
    # somebody had just typed.
    path = tmp_path / "doc.html"
    path.write_text("<html><body><p id='paragraph:one'>Hello world</p><ol>"
                    "<li id='item:1'>1</li><it id='item:5'>XX</li></ol></body></html>",
                    encoding="utf-8")
    assert doc_ir.from_html(path.read_text(encoding="utf-8")).get("stray") == ["XX"]
    with pytest.raises(SystemExit) as refused:
        doc_sync.read_file(path)
    said = str(refused.value)
    assert "'XX' outside any block" in said and "<li>" in said
    # And a file the dialect can read whole says nothing of the kind.
    path.write_text(path.read_text(encoding="utf-8").replace("<it ", "<li "), encoding="utf-8")
    assert "stray" not in doc_sync.read_file(path)


def test_a_table_the_file_left_open_loses_no_words_in_silence(tmp_path: Path) -> None:
    # The rule above, one level down. A table is the one block the dialect writes over
    # several lines, so half a move (or half a paste) leaves rows with no `<table>` to
    # go in - and their words sit inside `<p>` tags, where `handle_data` sees a block
    # and says nothing, while `</tr>` drops the row. The sync would then read the table
    # as one the source dropped and delete it from the document, which is the one change
    # there is no way back from.
    path = tmp_path / "doc.html"
    whole = doc_ir.to_html({"title": "t", "document": "d", "blocks": [
        para("p:one", "hello"), table("t:one", [["Region", "Sales"], ["North", "1200"]])]})
    lines = whole.split("\n")
    beheaded = [l for l in lines if not l.lstrip().startswith("<table")]
    path.write_text("\n".join(beheaded), encoding="utf-8")
    assert doc_ir.from_html("\n".join(beheaded)).get("stray") == [
        "Region", "Sales", "North", "1200"]
    with pytest.raises(SystemExit) as refused:
        doc_sync.read_file(path)
    assert "'Region', 'Sales', 'North' and 1 more outside any block" in str(refused.value)
    # And the other half: a table whose `</table>` never comes keeps every row it read.
    open_ended = "\n".join(l for l in lines if "</table>" not in l)
    assert doc_ir.from_html(open_ended).get("stray") == ["Region", "Sales", "North", "1200"]
    assert "stray" not in doc_ir.from_html(whole)


def test_the_white_space_between_the_dialects_own_lines_is_not_words(tmp_path: Path) -> None:
    # The writer breaks lines outside any block on purpose (`_table_html`), so the reader
    # must not read its own layout as text nobody carries - and a `<style>` is markup too.
    path = tmp_path / "doc.html"
    path.write_text(doc_ir.to_html({"title": "t", "document": "d", "blocks": [
        para("p:one", "hello"), table("t:one", [["a", "b"]])]}), encoding="utf-8")
    assert "stray" not in doc_sync.read_file(path)
    styled = "<html><head><style>p { color: red }</style></head><body><p>x</p></body></html>"
    assert "stray" not in doc_ir.from_html(styled)


def test_a_base_forgets_the_urls_that_die_within_the_hour(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    doc_sync.save_base(path, {"document": "d", "blocks": [{"kind": "paragraph", "runs": [
        {"chip": "image", "frozen": True, "text": "", "value": "i.0", "uri": "https://lh7/x"}]}]})
    run = runs_of(_local(path, "d")["blocks"][0])[0]
    assert run.get("value") == "i.0" and "uri" not in run


# ---------------------------------------------------------------- Drive, faked

def _http(status: int) -> HttpError:
    from googleapiclient.errors import HttpError
    return HttpError(SimpleNamespace(status=status, reason="no"), b"{}")


class _Reply(Later[T]):
    """A call whose answer `run` works out when it is executed (`fake_google.Later`)."""


def test_a_read_is_made_again_through_a_blip_and_a_write_never_is(monkeypatch: pytest.MonkeyPatch) -> None:
    # An SSL EOF or a 429 in the middle of a read ends the run otherwise - after the
    # write batch that leaves the document written and the file and base not settled,
    # and in `load_base` it leaves a sync with no base, which is the `--assume-base`
    # dialog where both answers throw work away. Reading twice costs a round trip.
    import inspect
    import ssl

    def no_sleep(seconds: float) -> None:
        return None
    monkeypatch.setattr(doc_sync.time, "sleep", no_sleep)
    calls: list[int] = []

    def flaky(*errors: Exception) -> _Reply[dict[str, bool]]:
        left = list(errors)

        def run() -> dict[str, bool]:
            calls.append(len(left))
            if left:
                raise left.pop(0)
            return {"ok": True}
        return _Reply(run)

    assert doc_sync._read(flaky(ssl.SSLEOFError("EOF"), _http(503))) == {"ok": True}
    assert len(calls) == 3                        # two blips, then the answer
    calls.clear()
    forbidden = _http(403)
    with pytest.raises(type(forbidden)):
        doc_sync._read(flaky(forbidden))          # not transient: asked once, and out
    assert len(calls) == 1
    calls.clear()
    with pytest.raises(ssl.SSLEOFError):
        doc_sync._read_tries(flaky(*[ssl.SSLEOFError("EOF")] * 9), 3)
    assert len(calls) == 3                        # and it gives up rather than looping
    # A write is never made again: a batch whose answer was lost may have been applied.
    assert "_read" not in inspect.getsource(doc_sync.send)


def _uploaded(media_body: MediaBody | None) -> bytes:
    """What an upload carries, read back as the library's own upload reads it."""
    assert media_body is not None, "an upload with nothing in it"
    return media_body.getbytes(0, media_body.size())


class _Storage:
    """Drive as the base storage and the backup see it: a document with
    `appProperties`, a blob per file id, and an export.

    Its calls take their arguments in order (`test_agent_doc_tools`' Drive extends
    `update` that way); `files()` is the Drive face, which hands them on."""

    def __init__(self, document: str) -> None:
        self.document = document
        self.props: dict[str, str] = {}
        self.blobs: dict[str, bytes] = {}
        self.created: list[FileBody] = []
        self.exports: list[tuple[str, str]] = []
        self.export_error: Exception | None = None
        self.refuse_create = self.refuse_rename = False
        self.names: list[str] = []
        self.n = 0
        self.hidden: set[str] = set()     # files in the app's hidden space
        self.refuse_hidden = False        # (no drive.appdata grant)
        self.trashed: list[str] = []

    def files(self) -> Files:
        return _StorageFiles(self)

    def drive(self) -> DriveService:
        """This storage as a Drive client: its files, and no comments or permissions."""
        return _StorageDrive(self)

    def get(self, fileId: str) -> Later[DriveFile]:
        def run() -> DriveFile:
            if fileId != self.document:
                raise _http(404)
            return {"name": "The report", "parents": ["folder"],
                    "appProperties": dict(self.props)}
        return _Reply(run)

    def get_media(self, fileId: str) -> Later[bytes]:
        def run() -> bytes:
            if fileId not in self.blobs:
                raise _http(404)
            return self.blobs[fileId]
        return _Reply(run)

    def create(self, body: FileBody, media_body: MediaBody | None) -> Later[DriveFile]:
        def run() -> DriveFile:
            if self.refuse_create:
                raise _http(403)
            in_hiding = body.get("parents") == [drive_folder.APPDATA]
            if in_hiding and self.refuse_hidden:
                raise _http(403)
            self.n += 1
            fid = f"base-{self.n}"
            if in_hiding:
                self.hidden.add(fid)
            self.blobs[fid] = _uploaded(media_body)
            self.created.append(body)
            return {"id": fid}
        return _Reply(run)

    def update(self, fileId: str, fields: str | None, body: FileBody | None,
               media_body: MediaBody | None) -> Later[DriveFile]:
        def run() -> DriveFile:
            if media_body is not None:
                if fileId not in self.blobs:
                    raise _http(404)
                self.blobs[fileId] = _uploaded(media_body)
            if body and (props := body.get("appProperties")):
                for name, value in props.items():
                    if value is None:
                        self.props.pop(name, None)
                    else:
                        self.props[name] = value
            # (the base file is renamed to its own name as it is stored: no rename of the document)
            if body and (name := body.get("name")) is not None and fileId == self.document:
                if self.refuse_rename:
                    raise _http(403)
                self.names.append(name)
            if body and body.get("trashed"):
                self.trashed.append(fileId)
            return {"id": fileId, "spaces": [drive_folder.APPDATA if fileId in self.hidden else "drive"]}
        return _Reply(run)

    def export(self, fileId: str, mimeType: str) -> Later[bytes]:
        def run() -> bytes:
            if self.export_error:
                raise self.export_error
            self.exports.append((fileId, mimeType))
            return b"<html>the document as it was</html>"
        return _Reply(run)


class _StorageFiles(NoFiles):
    """`files()` of a `_Storage`: the calls Drive's client takes, handed to it in order."""

    def __init__(self, storage: _Storage) -> None:
        self.storage = storage

    @override
    def get(self, **kw: Unpack[GetFile]) -> Request[DriveFile]:
        return self.storage.get(kw["fileId"])

    @override
    def get_media(self, **kw: Unpack[FileId]) -> Request[bytes]:
        return self.storage.get_media(kw["fileId"])

    @override
    def create(self, **kw: Unpack[CreateFile]) -> Request[DriveFile]:
        return self.storage.create(kw["body"], kw.get("media_body"))

    @override
    def update(self, **kw: Unpack[UpdateFile]) -> Request[DriveFile]:
        return self.storage.update(kw["fileId"], kw.get("fields"), kw.get("body"), kw.get("media_body"))

    @override
    def export(self, **kw: Unpack[ExportFile]) -> Request[bytes]:
        return self.storage.export(kw["fileId"], kw["mimeType"])


class _StorageDrive(NoDrive):
    def __init__(self, storage: _Storage) -> None:
        self.storage = storage

    @override
    def files(self) -> Files:
        return self.storage.files()


def _base(generation: int, key: str) -> StoredBase:
    return {"document": "doc-1", "generation": generation,
            "blocks": [para(key, "some words")]}


def test_a_checkout_with_no_state_folder_finds_the_base_in_drive(tmp_path: Path) -> None:
    """The headline: `.b2s/` is scratch state a fresh clone does not have, and
    before the base went to Drive that dropped the sync into the `--assume-base`
    dialog, where both answers throw somebody's work away."""
    drive = _Storage("doc-1")
    doc_sync.save_drive(drive.drive(), "doc-1", _base(2, "p:drive"), known_fid=None)
    problems: list[str] = []
    base, where = doc_sync.load_base(tmp_path / "doc.html", "doc-1", drive.drive(), problems, found=None)
    assert where == "drive" and key_of(_found(base)["blocks"][0]) == "p:drive"
    assert problems == []
    assert drive.props[doc_sync.BASE_PROPERTY] == "base-1"   # the document names it


def test_the_base_file_is_named_for_the_app_not_the_document() -> None:
    """Searching Drive for "The report" found its base beside it, named after it."""
    drive = _Storage("doc-1")
    doc_sync.save_drive(drive.drive(), "doc-1", _base(2, "p:drive"), known_fid=None)
    assert drive.created[0].get("name") == doc_sync.base_name("doc-1") == "beamer2slides docs base (doc-1).json"
    assert drive.names == [], "the document is not renamed"


def test_a_hidden_docs_base_goes_into_the_apps_hidden_space() -> None:
    drive = _Storage("doc-1")
    with drive_folder.use_hidden("deck"):
        fid = doc_sync.save_drive(drive.drive(), "doc-1", _base(2, "p:drive"), known_fid=None)
    assert drive.created[0].get("parents") == [drive_folder.APPDATA]
    assert drive.props == {doc_sync.BASE_PROPERTY: fid, doc_sync.BASE_HIDDEN_PROPERTY: "1"}
    with drive_folder.use_hidden("deck"):   # (the next store: in place, by the id the run found)
        assert doc_sync.save_drive(drive.drive(), "doc-1", _base(3, "p:drive"), known_fid=fid) == fid
    assert len(drive.created) == 1


def test_a_visible_docs_base_moves_into_hiding_and_the_old_one_to_the_trash(tmp_path: Path) -> None:
    drive = _Storage("doc-1")
    old = doc_sync.save_drive(drive.drive(), "doc-1", _base(2, "p:old"), known_fid=None)
    with drive_folder.use_hidden("pptx"):
        new = doc_sync.save_drive(drive.drive(), "doc-1", _base(3, "p:new"), known_fid=old)
    assert new != old and new in drive.hidden and drive.trashed == [old]
    assert drive.props == {doc_sync.BASE_PROPERTY: new, doc_sync.BASE_HIDDEN_PROPERTY: "1"}
    base, where = doc_sync.load_base(tmp_path / "doc.html", "doc-1", drive.drive(), [], found=None)
    assert where == "drive" and key_of(_found(base)["blocks"][0]) == "p:new"


def test_a_hidden_space_that_refuses_keeps_the_docs_base_visible() -> None:
    drive = _Storage("doc-1")
    drive.refuse_hidden = True
    with drive_folder.use_hidden("deck"):
        fid = doc_sync.save_drive(drive.drive(), "doc-1", _base(2, "p:drive"), known_fid=None)
        assert doc_sync.save_drive(drive.drive(), "doc-1", _base(3, "p:drive"), known_fid=fid) == fid
    assert drive.created[0].get("parents") == ["folder"] and len(drive.created) == 1
    assert drive.props == {doc_sync.BASE_PROPERTY: fid} and drive.trashed == []


def test_the_base_in_drive_beats_a_stale_copy_beside_the_file(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    drive = _Storage("doc-1")
    doc_sync.save_base(path, _base(3, "p:stale"))
    doc_sync.save_drive(drive.drive(), "doc-1", _base(5, "p:drive"), known_fid=None)
    problems: list[str] = []
    base, where = doc_sync.load_base(path, "doc-1", drive.drive(), problems, found=None)
    assert where == "drive" and key_of(_found(base)["blocks"][0]) == "p:drive"
    assert any("another checkout has synced" in line for line in problems), problems


def test_a_copy_newer_than_drives_is_the_one_used(tmp_path: Path) -> None:
    """What a sync whose Drive upload failed leaves behind: the cache is ahead."""
    path = tmp_path / "doc.html"
    drive = _Storage("doc-1")
    doc_sync.save_drive(drive.drive(), "doc-1", _base(2, "p:drive"), known_fid=None)
    doc_sync.save_base(path, _base(4, "p:local"))
    problems: list[str] = []
    base, where = doc_sync.load_base(path, "doc-1", drive.drive(), problems, found=None)
    assert where == "local" and key_of(_found(base)["blocks"][0]) == "p:local"
    assert any("older than the copy beside the file" in line for line in problems), problems


def test_the_copy_is_used_with_a_word_about_it_when_drive_has_no_base(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    drive = _Storage("doc-1")
    doc_sync.save_base(path, _base(1, "p:local"))
    problems: list[str] = []
    base, where = doc_sync.load_base(path, "doc-1", drive.drive(), problems, found=None)
    assert where == "local" and key_of(_found(base)["blocks"][0]) == "p:local"
    assert any("Drive has none" in line for line in problems), problems


def test_a_base_drive_names_but_cannot_serve_is_said_out_loud(tmp_path: Path) -> None:
    """Deleted, or somebody else's now. Nothing is lost by syncing from the copy —
    the document wins where both moved — but the copy may be older than the
    document, and that is the person's to know."""
    path = tmp_path / "doc.html"
    drive = _Storage("doc-1")
    doc_sync.save_drive(drive.drive(), "doc-1", _base(2, "p:drive"), known_fid=None)
    drive.blobs.clear()
    doc_sync.save_base(path, _base(1, "p:local"))
    problems: list[str] = []
    base, where = doc_sync.load_base(path, "doc-1", drive.drive(), problems, found=None)
    assert where == "local" and key_of(_found(base)["blocks"][0]) == "p:local"
    assert any("cannot be read" in line for line in problems), problems


def test_a_base_from_another_document_is_ignored_wherever_it_sits(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    drive = _Storage("doc-1")
    doc_sync.save_drive(drive.drive(), "doc-1", {"document": "elsewhere", "blocks": []},
                        known_fid=None)
    doc_sync.save_base(path, {"document": "elsewhere", "blocks": []})
    problems: list[str] = []
    assert doc_sync.load_base(path, "doc-1", drive.drive(), problems, found=None) == (None, "none")
    assert len(problems) == 2 and all("belongs to document elsewhere" in p for p in problems)


def test_with_no_base_anywhere_the_assume_base_dialog_is_what_is_left(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    assert doc_sync.load_base(path, "doc-1", _Storage("doc-1").drive(), [], found=None) == (None, "none")
    with pytest.raises(SystemExit) as raised:
        doc_sync._no_base(path, {"blocks": []}, {"blocks": []}, None, drive=None, document=None, backup=True,
                          dry_run=False)
    said = str(raised.value)
    assert "document-wins" in said and "source-wins" in said
    # and it says, for each answer, whose work it discards.
    assert "every edit made to the source since the last sync is discarded" in said
    assert "every edit a reader made there since the last sync is discarded" in said


def test_a_stored_base_goes_to_both_places_and_the_count_rises(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    drive = _Storage("doc-1")
    assert doc_sync.store_base(path, _base(0, "p:one"), drive.drive(), "doc-1", previous=4,
                               base_fid=None) is None
    assert _local(path, "doc-1").get("generation") == 5
    assert _found(doc_sync.load_drive(drive.drive(), "doc-1", found=None, hint=None)).get("generation") == 5
    doc_sync.store_base(path, _base(0, "p:one"), drive.drive(), "doc-1", previous=5, base_fid=None)
    assert len(drive.created) == 1                       # the same file, written again
    assert _found(doc_sync.load_drive(drive.drive(), "doc-1", found=None, hint=None)).get("generation") == 6


def test_the_cache_remembers_where_the_base_is_and_keeps_remembering(tmp_path: Path) -> None:
    """The base file is the same file for the document's life, so the cache beside the
    file can say which one it is and save the next sync Drive's own lookup. It has to
    keep saying it: a run that writes the base and leaves the id out of the cache makes
    the run after it ask again, and the saving alternates away."""
    path = tmp_path / "doc.html"
    drive = _Storage("doc-1")
    assert doc_sync.BASE_FID == "base_fid"               # the key the cache keeps it under
    doc_sync.store_base(path, _base(0, "p:one"), drive.drive(), "doc-1", previous=0, base_fid=None)
    fid = _local(path, "doc-1").get("base_fid")
    assert fid == "base-1" and doc_sync.BASE_FID not in _found(
        doc_sync.load_drive(drive.drive(), "doc-1", found=None, hint=None))
    doc_sync.store_base(path, _base(0, "p:two"), drive.drive(), "doc-1", base_fid=fid, previous=0)
    assert _local(path, "doc-1").get("base_fid") == fid

    # And with it, the base is fetched without asking the document where it is.
    def never(drive: DriveService, document: str) -> str | None:
        raise AssertionError("the base was looked up although the cache knew where it is")

    was, doc_sync.base_file_id = doc_sync.base_file_id, never
    try:
        found: dict[str, str] = {}
        base, where = doc_sync.load_base(path, "doc-1", drive.drive(), [], found)
    finally:
        doc_sync.base_file_id = was
    assert where == "drive" and key_of(_found(base)["blocks"][0]) == "p:two"
    assert found["fid"] == fid


def test_a_drive_write_that_fails_keeps_the_cache_and_says_why(tmp_path: Path) -> None:
    """A Drive write that fails must never fail the sync: the document has already
    been written by then, and the cache is a base the next run can still use."""
    path = tmp_path / "doc.html"
    drive = _Storage("doc-1")
    drive.refuse_create = True
    why = doc_sync.store_base(path, _base(0, "p:one"), drive.drive(), "doc-1", previous=0, base_fid=None)
    assert why and "HttpError" in why
    assert _local(path, "doc-1").get("generation") == 1


def test_the_document_is_renamed_through_drive_and_a_refusal_says_so() -> None:
    """A Google Doc's title is its Drive name: no `batchUpdate` request writes one,
    so the file's `<title>` reaches the document only this way — and a rename Drive
    refuses fails nothing, as a base it refuses does not."""
    drive = _Storage("doc-1")
    problems: list[str] = []
    assert doc_sync.rename_document(drive.drive(), "doc-1", "A better name", problems) == \
        "A better name"
    assert drive.names == ["A better name"] and problems == []
    drive.refuse_rename = True
    assert doc_sync.rename_document(drive.drive(), "doc-1", "Nope", problems) is None
    assert "could not be renamed 'Nope'" in problems[0]
    assert "keeps the name it has" in problems[0]


class _OneRead(NoDocs, NoDocuments):
    """`documents.get` of a document with nothing in it but its name."""

    def __init__(self, title: str) -> None:
        self.title = title

    @override
    def documents(self) -> Documents:
        return self

    @override
    def get(self, **kw: Unpack[GetDocument]) -> Request[Document]:
        doc: Document = {"title": self.title, "revisionId": "r1", "body": {"content": []}}
        return Answer(doc)


def test_a_rename_drive_has_made_is_what_the_file_and_the_base_say(tmp_path: Path) -> None:
    """`documents.get` need not have caught up with a Drive rename, and if the settle
    took the name it reads, the file would go straight back to the old one — the
    rename undone the moment it was made, and nothing to try again next time, since
    the base would agree with the file."""
    path = tmp_path / "doc.html"
    live = doc_sync.settle(_OneRead("The old name"), "doc-1", path,
                           {"blocks": []}, {"blocks": []}, renamed="A better name", planned=None, drive=None,
                           problems=None, name_unmodelled=False, base_fid=None, read=None)
    assert live.get("title") == "A better name"
    assert "<title>A better name</title>" in path.read_text(encoding="utf-8")
    assert _local(path, "doc-1").get("title") == "A better name"
    # Without one, the settle says whatever the read said.
    doc_sync.settle(_OneRead("The old name"), "doc-1", path, {"blocks": []}, {"blocks": []}, planned=None,
                    drive=None, problems=None, name_unmodelled=False, renamed=None, base_fid=None, read=None)
    assert "<title>The old name</title>" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- --assume-base

def test_the_assume_base_names_say_which_side_loses(capsys: pytest.CaptureFixture[str]) -> None:
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


def _sides() -> tuple[Ir, Ir]:
    """(the file, the document): two sides with nothing in common but their shape."""
    return {"blocks": [para(None, "the file")]}, {"blocks": [para(None, "the document")]}


def test_the_destructive_direction_exports_the_document_first(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    drive = _Storage("doc-1")
    ours, theirs = _sides()
    base, kept = doc_sync._no_base(path, ours, theirs, "source-wins", drive.drive(), "doc-1", backup=True,
                                   dry_run=False)
    assert base is theirs                       # every difference is the source's
    assert kept and kept.parent == tmp_path / ".b2s" / "backups"
    assert kept.read_bytes() == b"<html>the document as it was</html>"
    assert drive.exports == [("doc-1", "text/html")]
    # The other direction writes nothing to the document, so it needs no way back.
    base, kept = doc_sync._no_base(path, ours, theirs, "document-wins", drive.drive(), "doc-1", backup=True,
                                   dry_run=False)
    assert base is ours and kept is None and len(drive.exports) == 1


def test_a_dry_run_of_the_destructive_direction_exports_nothing(tmp_path: Path) -> None:
    """`--dry-run` is how one looks at that answer before giving it, and a look must
    leave nothing behind: the backup is taken for the write, and there is no write."""
    path = tmp_path / "doc.html"
    drive = _Storage("doc-1")
    ours, theirs = _sides()
    base, kept = doc_sync._no_base(path, ours, theirs, "source-wins", drive.drive(), "doc-1",
                                   dry_run=True, backup=True)
    assert base is theirs and kept is None
    assert drive.exports == [] and not (tmp_path / ".b2s" / "backups").exists()


def test_a_backup_drive_refuses_stops_that_sync(tmp_path: Path) -> None:
    """`guard.demand_way_back`'s principle: a write with no way back is something
    one asks for, and never something that happens because an export failed."""
    path = tmp_path / "doc.html"
    drive = _Storage("doc-1")
    drive.export_error = _http(403)
    ours, theirs = _sides()
    with pytest.raises(SystemExit) as raised:
        doc_sync._no_base(path, ours, theirs, "source-wins", drive.drive(), "doc-1", backup=True, dry_run=False)
    assert "--no-backup" in str(raised.value)
    base, kept = doc_sync._no_base(path, ours, theirs, "source-wins", drive.drive(), "doc-1",
                                   backup=False, dry_run=False)
    assert base is theirs and kept is None


# ---------------------------------------------------------------- batching

class _Batches(NoDocs, NoDocuments):
    """Docs' `batchUpdate`, remembering every body it was given."""

    def __init__(self, revisions: bool) -> None:
        self.sent: list[DocsBatchUpdateBody] = []
        self.revisions, self.n = revisions, 0

    @override
    def documents(self) -> Documents:
        return self

    @override
    def batchUpdate(self, **kw: Unpack[UpdateDocument]) -> Request[DocsBatchUpdateResponse]:
        body = kw["body"]

        def run() -> DocsBatchUpdateResponse:
            self.n += 1
            self.sent.append(body)
            out: DocsBatchUpdateResponse = {"replies": [{"n": i} for i in range(len(body["requests"]))]}
            if self.revisions:
                out["writeControl"] = {"requiredRevisionId": f"rev{self.n}"}
            return out
        return _Reply(run)


def _texts(count: int) -> list[DocsRequest]:
    return [{"insertText": {"location": {"index": i + 1}, "text": str(i)}}
            for i in range(count)]


def _required(body: DocsBatchUpdateBody) -> str | None:
    """The revision a batch was sent against, if any."""
    return body.get("writeControl", {}).get("requiredRevisionId")


def test_a_plan_up_to_the_threshold_is_one_batch_and_stays_atomic() -> None:
    docs = _Batches(True)
    notes: list[str] = []
    doc_sync.send(docs, "d", _texts(doc_sync.CHUNK), "rev0", notes)
    assert len(docs.sent) == 1 and notes == []
    assert docs.sent[0].get("writeControl") == {"requiredRevisionId": "rev0"}


def test_a_long_plan_is_cut_in_order_with_the_revision_chained() -> None:
    """Docs applies a batch in order, so consecutive batches write the same
    document — as long as nothing is reordered and no request crosses a boundary.
    The guard has to carry across: each batch requires the revision the one before
    it produced, so somebody typing half-way through the run is still refused."""
    docs = _Batches(True)
    notes: list[str] = []
    requests = _texts(doc_sync.CHUNK * 2 + 3)
    answer = doc_sync.send(docs, "d", requests, "rev0", notes)
    assert [len(body["requests"]) for body in docs.sent] == [doc_sync.CHUNK, doc_sync.CHUNK, 3]
    assert [r for body in docs.sent for r in body["requests"]] == requests
    assert [_required(body) for body in docs.sent] == \
        ["rev0", "rev1", "rev2"]
    assert len(answer.get("replies", [])) == len(requests)
    # And the atomicity it costs is said, not hidden.
    assert notes and "3 batches" in notes[0] and "fails part-way" in notes[0]


def test_without_a_write_control_in_the_answer_the_later_batches_go_unguarded() -> None:
    """Unmeasured against the live API: whether the answer always carries one. If
    it does not, the chain stops guarding rather than sending a stale revision."""
    docs = _Batches(False)
    doc_sync.send(docs, "d", _texts(doc_sync.CHUNK + 1), "rev0", notes=None)
    assert "writeControl" in docs.sent[0] and "writeControl" not in docs.sent[1]


class _Staging(NoDrive, NoFiles, NoDocs):
    """Drive and Docs as the stager sees them: an import, a read, a delete."""

    def __init__(self) -> None:
        self.created: list[FileBody] = []
        self.deleted: list[str] = []
        self.html = ""

    @override
    def files(self) -> Files:
        return self

    @override
    def documents(self) -> Documents:
        return _StagingDocuments(self)

    @override
    def create(self, **kw: Unpack[CreateFile]) -> Request[DriveFile]:
        self.html = _uploaded(kw.get("media_body")).decode()
        self.created.append(kw["body"])
        made: DriveFile = {"id": "staging"}
        return Answer(made)

    @override
    def delete(self, **kw: Unpack[FileId]) -> Request[Empty]:
        self.deleted.append(kw["fileId"])
        nothing: Empty = {}
        return Answer(nothing)

    def imported(self) -> Document:
        """The staging document as Docs reads it back: a paragraph per picture."""
        import re
        content: list[DocsStructuralElement] = []
        at = 1
        for label in re.findall(r"<p>(\d+):", self.html):
            content.append({"startIndex": at, "endIndex": at + len(label) + 3, "paragraph": {
                "elements": [{"startIndex": at, "endIndex": at + len(label) + 1,
                              "textRun": {"content": label + ":"}},
                             {"startIndex": at + len(label) + 1, "endIndex": at + len(label) + 2,
                              "inlineObjectElement": {"inlineObjectId": f"i.{label}"}},
                             {"startIndex": at + len(label) + 2, "endIndex": at + len(label) + 3,
                              "textRun": {"content": "\n"}}]}})
            at += len(label) + 3
        return {"body": {"content": content},
                "inlineObjects": {f"i.{n}": {"inlineObjectProperties": {"embeddedObject": {
                    "imageProperties": {"contentUri": f"https://lh7/{n}"}}}}
                    for n in range(len(content))}}


class _StagingDocuments(NoDocuments):
    def __init__(self, staging: _Staging) -> None:
        self.staging = staging

    @override
    def get(self, **kw: Unpack[GetDocument]) -> Request[Document]:
        return Later(self.staging.imported)


def test_pictures_are_staged_once_and_the_staging_document_goes(tmp_path: Path) -> None:
    for name in ("a.png", "b.png"):
        (tmp_path / name).write_bytes(name.encode())
    google = _Staging()
    stager = doc_sync.Stager(google, google, tmp_path / "doc.html")
    requests: list[DocsRequest] = [
        {"insertInlineImage": {"location": {"index": 5}, "uri": doc_merge.STAGE + "b.png"}},
        {"insertText": {"location": {"index": 1}, "text": "x"}},
        {"insertInlineImage": {"location": {"index": 2}, "uri": doc_merge.STAGE + "a.png"}},
        {"insertInlineImage": {"location": {"index": 1}, "uri": "https://example.com/c.png"}}]
    sent = stager.resolve(requests)
    assert [image["uri"] for image in images_in(sent)] == [
        "https://lh7/1", "https://lh7/0", "https://example.com/c.png"]
    assert "data:image/png;base64," in google.html and len(google.created) == 1
    stager.resolve(requests)                       # a re-plan stages nothing new
    assert len(google.created) == 1
    stager.close()
    assert google.deleted == ["staging"]


def _tab(tab: str, title: str, words: str, ranges: dict[str, DocsNamedRanges],
         children: list[DocsTab]) -> DocsTab:
    body: list[DocsStructuralElement] = [
        {"startIndex": 1, "endIndex": 2 + len(words), "paragraph": {"elements": [
            {"startIndex": 1, "endIndex": 2 + len(words), "textRun": {"content": words + "\n"}}]}}]
    return {"tabProperties": {"tabId": tab, "title": title},
            "documentTab": {"body": {"content": body}, "namedRanges": ranges},
            "childTabs": list(children)}


def test_every_tab_is_read_with_its_own_keys_and_written_to_the_file() -> None:
    ranges: dict[str, DocsNamedRanges] = {"b2s:paragraph:notes": {"namedRanges": [
        {"namedRangeId": "r", "ranges": [{"startIndex": 1, "endIndex": 5, "tabId": "t.1"}]}]}}
    child: DocsTab = {**_tab("t.2", "Older notes", "old", {}, []), "tabProperties": {
        "tabId": "t.2", "title": "Older notes", "parentTabId": "t.1"}}
    doc: Document = {"title": "D", "tabs": [_tab("t.0", "Tab 1", "chapter", {}, []),
                                            _tab("t.1", "Notes", "notes", ranges, [child])]}
    ir = doc_sync.document_ir(doc, "d", ours=None, base=None)
    assert [runs_of(b)[0]["text"] for b in ir["blocks"]] == ["chapter"]
    notes, older = tabs_of(ir)
    assert (notes.get("tab"), notes.get("title"), key_of(notes["blocks"][0])) == ("t.1", "Notes",
                                                                                   "paragraph:notes")
    assert (older.get("tab"), older.get("title"), older.get("parent")) == ("t.2", "Older notes", "t.1")
    html = doc_ir.to_html(doc_ir.key_blocks(ir))
    assert '<section data-tab="t.2" title="Older notes" data-parent="t.1">' in html
    again = doc_ir.from_html(html)
    assert [(p.get("tab"), p.get("title")) for p in doc_ir.parts(again)[1:]] == [
        ("t.1", "Notes"), ("t.2", "Older notes")]
    assert key_of(again["blocks"][0]) == key_of(ir["blocks"][0])
    assert key_of(tabs_of(again)[0]["blocks"][0]) == "paragraph:notes"
    assert doc_ir.to_html({**again, "document": "d"}) == html
    # The first tab's `title` is the document's name; its own is `tab_title`, and the
    # file says it in a meta, since the body has no `<section>` to hang it on.
    assert (ir.get("title"), ir.get("tab"), ir.get("tab_title")) == ("D", "t.0", "Tab 1")
    assert '<meta name="b2s-tab" content="Tab 1">' in html
    assert again.get("tab_title") == "Tab 1"


def test_a_tab_just_added_is_empty_and_what_is_written_there_goes_into_it() -> None:
    doc: Document = {"tabs": [_tab("t.0", "Tab 1", "chapter", {}, []), _tab("t.9", "New", "", {}, [])]}
    part = doc_ir.parts(doc_sync.document_ir(doc, "d", ours=None, base=None))[1]
    assert part["blocks"] == [] and part.get("trailer") == [1, 2]


class _Drive(NoDrive, NoComments):
    """Drive's comments endpoint, or the error it raises instead."""

    def __init__(self, payload: CommentList, error: Exception | None) -> None:
        self.payload, self.error = payload, error
        self.asked: ListComments | None = None

    @override
    def comments(self) -> Comments:
        return self

    @override
    def list(self, **kw: Unpack[ListComments]) -> Request[CommentList]:
        self.asked = kw
        return Answer(self.error or self.payload)


def test_the_open_comments_are_read_and_the_resolved_ones_left_out() -> None:
    """A comment is a question about a passage, and the merge cannot see one: it lives
    in Drive, not in the document's content. So the report says they are there."""
    drive = _Drive({"comments": [
        {"content": "is this still true?", "author": {"displayName": "Ada"},
         "quotedFileContent": {"value": "The closing paragraph."},
         "replies": [{"content": "checking"}]},
        {"content": "fixed", "author": {"displayName": "Ada"}, "resolved": True}]}, None)
    assert doc_sync.open_comments(drive, "id") == [
        "Ada on 'The closing paragraph.': 'is this still true?', and 1 reply"]
    assert drive.asked is not None
    assert drive.asked["fileId"] == "id" and drive.asked.get("includeDeleted") is False


def test_comments_that_cannot_be_read_are_said_not_raised() -> None:
    assert doc_sync.open_comments(_Drive({}, _http(403)), "id") == [
        "the document's comments could not be read (403)"]


def test_the_report_has_a_section_for_the_open_comments(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    report = doc_sync.write_report(path, _report(0, True, [], [], ["Ada on 'a passage': 'why?'"], []))
    text = report.read_text(encoding="utf-8")
    assert "## Open comments in the document" in text
    assert "- Ada on 'a passage': 'why?'" in text


def test_the_report_says_what_each_side_contributed() -> None:
    applied, kept, gone = doc_sync._summary([
        {"key": "p:new", "kind": "paragraph", "origin": "added by the source",
         "runs": [{"text": "written from the file"}]},
        {"key": "p:one", "kind": "paragraph", "origin": "merged", "runs": [{"text": "merged words"}]},
        {"key": "p:two", "kind": "paragraph", "origin": "kept from the document",
         "runs": [{"text": "typed by a reader"}]},
        {"key": "p:three", "kind": "paragraph", "runs": [{"text": "untouched"}]}],
        [{"key": "p:four", "kind": "paragraph", "runs": [{"text": "the file dropped this"}]}])
    assert applied == ["`p:new` added: 'written from the file'",
                       "`p:one` rewritten: 'merged words'"]
    assert kept == ["`p:two` says what the document says: 'typed by a reader'"]
    assert gone == ["`p:four`: 'the file dropped this'"]


def test_a_block_the_file_no_longer_has_is_named_as_deleted(tmp_path: Path) -> None:
    """The one change a sync makes that no second sync can undo, so it is said out loud.

    Measured on the playground: the workbench's editor had a buffer older than the sync
    before it, saving it dropped two paragraphs a reader had typed, and the report said
    "1 block(s) from the source, 0 kept from the document, 0 conflict(s)" and listed the
    rewrite. `doc_merge.deleted_blocks` asks what `requests` will really delete, after
    every restoration above it, and it travels to the heading a person reads first.
    """
    base = live([para("p:one", "hello"), para("p:two", "LAlalalalaa")])
    ours = live([para("p:one", "Gello")])                     # the stale buffer, saved
    result = doc_merge.plan(base, ours, live(base["blocks"]))
    assert [doc_merge.block_text(b) for b in result.removed] == ["LAlalalalaa"]
    assert doc_sync._summary(result.blocks, result.removed)[2] == ["`p:two`: 'LAlalalalaa'"]

    path = tmp_path / "doc.html"
    info = _report(2, False, [], ["`p:one` rewritten: 'Gello'"], [], ["`p:two`: 'LAlalalalaa'"])
    text = doc_sync.write_report(path, info).read_text(encoding="utf-8")
    assert "## Deleted from the document (no way back)" in text
    assert "- `p:two`: 'LAlalalalaa'" in text
    # The words themselves, and above everything else the report says about this sync.
    assert text.index("LAlalalalaa") < text.index("Gello")


def test_a_table_is_reported_by_its_cells() -> None:
    grid = table("t:one", [["Region", "Sales"], ["North", "1200"]])
    applied, kept, gone = doc_sync._summary([{**grid, "origin": "merged"}], [])
    assert applied == ["`t:one` rewritten: 'Region | Sales | North | 1200'"]
    assert kept == [] and gone == []


def test_a_document_edit_inside_a_cell_is_reported_as_the_documents() -> None:
    """The table itself has no words, so what its cells did is what it did."""
    base = live([table("t:one", [["Region", "Sales"], ["North", "1200"]])])
    theirs = live([table("t:one", [["Region", "Sales"], ["North", "1500"]])])
    result = doc_merge.plan(base, live(base["blocks"]), theirs)
    assert result.requests == []
    assert doc_sync._summary(result.blocks, result.removed)[1] == ["`t:one` says what the document says: "
                                            "'Region | Sales | North | 1500'"]


# ------------------------------------------- what the document has and the file cannot

class _Unmodelled(DocsParagraphStyle, total=False):
    """A paragraph style as Docs answers it: with the properties the dialect never reads."""
    avoidWidowAndOrphan: bool
    direction: str
    tabStops: list[JsonObject]
    borderBetween: JsonObject


def _unmodelled_doc(style: _Unmodelled) -> Document:
    full: _Unmodelled = {"namedStyleType": "NORMAL_TEXT", **style}
    return {"documentId": "d", "body": {"content": [{"paragraph": {
        "elements": [], "paragraphStyle": full}}]}}


def test_a_sync_says_how_much_of_the_document_the_file_cannot_say() -> None:
    """One line, not fifteen: a sync report that repeats the whole list every run is
    a report nobody reads twice."""
    doc = _unmodelled_doc({"avoidWidowAndOrphan": True, "direction": "LEFT_TO_RIGHT",
                           "tabStops": [{"offset": {"magnitude": 36}}],
                           "borderBetween": {"width": {"magnitude": 1}}})
    notes = doc_sync.unmodelled_notes(doc, full=False)
    assert len(notes) == 1
    assert notes[0].startswith(
        "4 kinds of document property this file cannot say "
        "(structural.paragraph.paragraphStyle.avoidWidowAndOrphan, ")
    assert "and 1 more" in notes[0]
    # The one number a sync can act on: naming the blocks is `adopt`'s job.
    assert "1 block(s) carry one" in notes[0]


def test_adopt_names_every_one_of_them() -> None:
    """A document somebody else wrote is handed over once, and that is the moment to
    say what will not survive being written again."""
    doc = _unmodelled_doc({"avoidWidowAndOrphan": True, "direction": "LEFT_TO_RIGHT"})
    notes = doc_sync.unmodelled_notes(doc, full=True)
    assert notes[:2] == [
        "the document has 1 × structural.paragraph.paragraphStyle.avoidWidowAndOrphan "
        "(e.g. True), which the canonical file cannot say",
        "the document has 1 × structural.paragraph.paragraphStyle.direction "
        "(e.g. LEFT_TO_RIGHT), which the canonical file cannot say"]


def test_a_document_the_dialect_covers_is_said_nothing_about() -> None:
    assert doc_sync.unmodelled_notes(_unmodelled_doc({"alignment": "CENTER"}), full=True) == []


def _said_doc(*paragraphs: tuple[str, _Unmodelled]) -> Document:
    """A body of paragraphs, each with its words and its paragraph style."""
    content: list[DocsStructuralElement] = []
    at = 1
    for text, style in paragraphs:
        end = at + len(text) + 1
        full: _Unmodelled = {"namedStyleType": "NORMAL_TEXT", **style}
        content.append({"startIndex": at, "endIndex": end, "paragraph": {
            "elements": [{"startIndex": at, "endIndex": end,
                          "textRun": {"content": text + "\n", "textStyle": {}}}],
            "paragraphStyle": full}})
        at = end
    return {"documentId": "d", "body": {"content": content}}


def test_the_block_that_would_lose_something_is_named_by_its_own_words() -> None:
    """The counts say the document has a border somewhere, which is true and of no
    use: the person about to edit a paragraph needs to know it is *that* one."""
    doc = _said_doc(("Plain enough", {}),
                    ("Why this matters", {"borderBetween": {"width": {"magnitude": 1}}}))
    notes = doc_sync.block_risk_notes(doc, limit=doc_sync.RISKY_BLOCKS)
    assert notes == ["the paragraph 'Why this matters' carries "
                     "paragraphStyle.borderBetween; rewriting that block "
                     "through the file would drop it"]


def test_a_block_carrying_nothing_the_file_misses_is_not_named() -> None:
    """Or the report would list the whole document and say nothing."""
    assert doc_sync.block_risk_notes(_said_doc(("Plain enough", {})), limit=doc_sync.RISKY_BLOCKS) == []


def test_the_blocks_are_named_most_laden_first_and_the_tail_is_counted() -> None:
    laden: list[tuple[str, _Unmodelled]] = [(f"line {n}", {"avoidWidowAndOrphan": True}) for n in range(10)]
    doc = _said_doc(*laden,
                    ("the heavy one", {"avoidWidowAndOrphan": True,
                                       "direction": "LEFT_TO_RIGHT",
                                       "borderBetween": {"width": {"magnitude": 1}}}))
    notes = doc_sync.block_risk_notes(doc, limit=3)
    assert notes[0].startswith("the paragraph 'the heavy one' carries "
                               "paragraphStyle.avoidWidowAndOrphan, ")
    assert len(notes) == 4
    assert notes[-1].startswith("and 8 more blocks carry something the file cannot say")


def _planned(doc: Document, reorder: bool, reword: bool) -> list[doc_sync.Written]:
    """One tab's plan over a document, keyed as a synced document would be."""
    import copy

    theirs = doc_ir.from_document(doc, tab_id=None)
    for n, block in enumerate(theirs["blocks"]):
        block["key"] = f"p:{n}"
    base, ours = copy.deepcopy(theirs), copy.deepcopy(theirs)
    if reorder:
        ours["blocks"] = ours["blocks"][-1:] + ours["blocks"][:-1]
    if reword:
        ours["blocks"][-1]["runs"] = [{"text": "Bordered words, reworded"}]
    return [doc_sync._dry(None, None, doc_merge.plan(base, ours, copy.deepcopy(theirs)))]


def test_the_block_this_sync_is_about_to_cost_something_is_named_as_a_loss() -> None:
    """The risk notes say what a rewrite *would* drop, which is a caution. Once the
    plan exists, the question has an answer: this run moves that very block, and a
    move is a delete and a write, so the border is going. Said before the write."""
    doc = _said_doc(("First words", {}), ("Second words", {}),
                    ("Bordered words", {"borderBetween": {"width": {"magnitude": 1}}}))
    assert doc_sync.rewrite_losses(doc, _planned(doc, reorder=True, reword=False)) == [
        "the paragraph 'Bordered words' is being moved, which drops "
        "paragraphStyle.borderBetween — the document's, and in nothing the file can say"]


def test_a_block_whose_words_merely_change_costs_nothing_and_is_not_named() -> None:
    """The whole distinction: a request names the fields it writes, and no field the
    merge owns is a field nobody reads. A report that cried loss on every edit to a
    bordered paragraph would teach whoever reads it to skip the line."""
    doc = _said_doc(("First words", {}), ("Second words", {}),
                    ("Bordered words", {"borderBetween": {"width": {"magnitude": 1}}}))
    planned = _planned(doc, reorder=False, reword=True)
    assert planned[0].result.requests, "the source edit reached no request"
    assert doc_sync.rewrite_losses(doc, planned) == []


def test_a_document_with_nothing_unread_says_nothing_however_much_moves() -> None:
    doc = _said_doc(("First words", {}), ("Second words", {}), ("Third words", {}))
    assert doc_sync.rewrite_losses(doc, _planned(doc, reorder=True, reword=False)) == []
