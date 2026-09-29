"""Offline tests for the three Docs journeys an agent can take.

No Google calls. The document is `devtools.doc_world` — the in-memory Doc that consumes
the real `doc_merge.plan` requests, applies Docs' own index rules and throws out a whole
batch when one request is refused — wired into `doc_sync` through the two service
factories it builds its clients with. So the end-to-end test here exercises the real
`doc_sync.sync`: the plan, the write, the settle, the regenerated file and the base.

What is faked is exactly three things: the Docs client (`_Docs`, over a world), the Drive
client (`_Drive`, the storage fake from `test_doc_sync` plus a comments list) and the
context's credentials, which nothing here ever looks at.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
import inspect
import json
from typing import TYPE_CHECKING, Annotated, NoReturn, get_args, get_origin

import pytest

from beamer2slides import doc_ir
from beamer2slides import doc_sync as docs
from beamer2slides.agent import ALL_ACTIONS, AgentContext, LocalWorkspace, NoGoogle, Result
from beamer2slides.agent import doc_tools
from beamer2slides.agent.context import Job
from beamer2slides.agent.schema import type_hints
from beamer2slides.devtools import doc_world, fuzz_docs
from beamer2slides.devtools.agent_tasks import _DocsService, _DriveService, _Reply
from beamer2slides.google_types import json_object, part
from beamer2slides.json_types import JsonObject
from beamer2slides.typing_compat import override

from .json_reads import jint, jobj, jobjs

if TYPE_CHECKING:
    from google.auth.credentials import Credentials
    from typing_extensions import Unpack

    from beamer2slides.google_types import Comment, Document, DriveFile, UpdateFile

BLOCKS: list[doc_ir.Block] = [
    {"kind": "heading", "level": 1, "runs": [{"text": "The report"}]},
    {"kind": "paragraph", "runs": [{"text": "The first paragraph, untouched by anyone."}]},
    {"kind": "paragraph", "runs": [{"text": "The second paragraph, which the source rewrites."}]},
]


# ---------------------------------------------------------------- the fakes

class _Access:
    """Credentials the journey never looks at: the clients below ignore them."""

    def credentials(self) -> Credentials:
        from google.oauth2.credentials import Credentials as UserCredentials
        return UserCredentials(token=None)

    def describe(self) -> JsonObject:
        return {"available": True, "source": "the offline world"}


def comment(author: str, about: str, says: str) -> Comment:
    """One open comment, as Drive's comments API reports it."""
    return {"content": says, "resolved": False, "author": {"displayName": author},
            "quotedFileContent": {"value": about}, "replies": []}


class _Drive(_DriveService):
    """The Docs benchmark's Drive (the base storage, the exports, the comments), plus the one
    thing it does not keep: the names the document was given."""

    def __init__(self, world: doc_world.World, comments: Sequence[Comment]) -> None:
        super().__init__(world, "doc-1", comments)
        self.names: list[str] = []

    @override
    def update(self, **kw: Unpack[UpdateFile]) -> _Reply[DriveFile]:
        inner = super().update(**kw)
        body = kw.get("body", {})

        def run() -> DriveFile:
            answer = inner.execute()
            # Drive and Docs are two views of one document: a rename shows in both.
            if "name" in body:
                self.names.append(body["name"])
                if kw["fileId"] == self.document:
                    self.world.title = body["name"]
            return answer
        return _Reply(run)


def _pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, comments: Sequence[Comment],
          base: bool) -> tuple[doc_world.World, Path, _Drive, _DocsService]:
    """A document and the canonical file that says what it said last time (`BLOCKS`).

    `fuzz_docs.bootstrap` is what `docs push` leaves behind: every block keyed and named
    in the document, and a file that is the document's own read.
    """
    world = doc_world.build([{"blocks": BLOCKS}], title="The report")
    ir = fuzz_docs.bootstrap(world)
    path = tmp_path / "doc.html"
    docs.write_file(path, ir, "doc-1")
    if base:
        docs.save_base(path, json.loads(json.dumps(ir)) | {"document": "doc-1",
                                                           "generation": 1})
    drive, service = _Drive(world, comments), _DocsService(world)

    # (Answering however `doc_sync` asks - `docs_service(creds)` - and ignoring it.)
    def docs_service(creds: Credentials | None) -> _DocsService:
        return service

    def drive_service(creds: Credentials | None) -> _Drive:
        return drive

    monkeypatch.setattr(docs, "docs_service", docs_service)
    monkeypatch.setattr(docs, "drive_service", drive_service)
    return world, path, drive, service


def _ctx(tmp_path: Path) -> AgentContext:
    return AgentContext.local(tmp_path, google=_Access())


def _reword(path: Path, was: str, now: str) -> None:
    """A source edit, made the way a person makes one: in the file, in git."""
    text = path.read_text(encoding="utf-8")
    assert was in text, text
    path.write_text(text.replace(was, now), encoding="utf-8")


# ---------------------------------------------------------------- the shape of a tool

def test_every_parameter_says_what_it_is_for_and_the_hints_resolve():
    """`schema.py` reads these annotations; a bare `str` would publish a nameless field."""
    for journey in doc_tools.TOOLS:
        hints = type_hints(journey.body)
        params = list(inspect.signature(journey.body).parameters)
        assert params[0] == "j"
        for name in params[1:]:
            hint = hints[name]
            parts = get_args(hint)
            assert get_origin(hint) is Annotated and len(parts) > 1, f"{journey.tool_name}.{name}"
            said = parts[1]
            assert isinstance(said, str) and len(said) > 20, f"{journey.tool_name}.{name}"
        assert hints["return"] is type(None)
        assert journey.body.__doc__ and len(journey.body.__doc__.splitlines()) >= 3


def test_what_each_journey_does_to_the_world_is_declared():
    from beamer2slides.agent import READS, READS_GOOGLE, WRITES, WRITES_GOOGLE

    assert doc_tools.doc_push.needs == (READS, WRITES, WRITES_GOOGLE)
    # sync and adopt declare the *least* they do, so a read-only context can plan a merge;
    # a real sync asks for WRITES_GOOGLE in the body instead.
    assert doc_tools.doc_sync.needs == (READS, WRITES, READS_GOOGLE)
    assert doc_tools.doc_adopt.needs == (READS, WRITES, READS_GOOGLE)


# ---------------------------------------------------------------- with no Google at all

def test_with_no_google_every_docs_journey_refuses_and_writes_nothing(tmp_path: Path):
    """Two ways to have no Google, and both stop before the body runs.

    `AgentContext.offline` forbids the Google actions *and* has no account, and the gate
    answers the fact rather than the policy; a context that allows everything but has no
    credentials refuses when they are fetched. Either way the code is `offline` and
    nothing is read, written or sent.
    """
    (tmp_path / "doc.html").write_text(doc_ir.to_html({"blocks": [], "document": "doc-1"}),
                                       encoding="utf-8")
    before = (tmp_path / "doc.html").read_text(encoding="utf-8")
    for ctx in (AgentContext.offline(tmp_path),
                AgentContext(workspace=LocalWorkspace(tmp_path), google=NoGoogle(reason="offline", fix=None),
                             allow=ALL_ACTIONS)):
        for result in (doc_tools.doc_push(ctx, file="doc.html"),
                       doc_tools.doc_sync(ctx, file="doc.html"),
                       doc_tools.doc_adopt(ctx, doc="doc-1", file="other.html")):
            assert not result.ok and result.code == "offline", (result.tool, result.json())
            assert result.summary and not result.artifacts
    assert not (tmp_path / ".b2s").exists()
    assert not (tmp_path / "other.html").exists()
    assert (tmp_path / "doc.html").read_text(encoding="utf-8") == before


def test_a_read_only_context_can_plan_a_merge_but_not_write_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, path, _, service = _pair(tmp_path, monkeypatch, [], True)
    _reword(path, "which the source rewrites", "which the source has rewritten")
    from beamer2slides.agent import READS, READS_GOOGLE, WRITES

    ctx = AgentContext.local(tmp_path, google=_Access(), allow=frozenset({READS, WRITES, READS_GOOGLE}))
    planned = doc_tools.doc_sync(ctx, file="doc.html", dry_run=True)
    assert planned.ok and planned.data["requests"] and not planned.data["written"]
    assert service.sent() == 0
    refused = doc_tools.doc_sync(ctx, file="doc.html")
    assert not refused.ok and refused.code == "forbidden"
    assert service.sent() == 0


# ---------------------------------------------------------------- the translation layer

def _job(tmp_path: Path) -> Job:
    return Job("doc_sync", _ctx(tmp_path))


def _report() -> docs.SyncReport:
    """A report with something of every kind in it, fresh for each test to change."""
    return {
        "url": "https://docs.google.com/document/d/doc-1/edit", "document": "doc-1",
        "dry_run": False, "requests": 7, "base": "drive",
        "conflicts": [{"key": "p:one", "base": "the words", "ours": "the source words",
                       "theirs": "the reader's"},
                      {"key": "p:two", "base": "c", "ours": "a", "theirs": "b", "tab": "Appendix"}],
        "notes": ["`toc:1` holds a table of contents, which no request can create: left alone",
                  "900 requests were written in 2 batches of up to 500: unlike one batch, a run "
                  "that fails part-way leaves the batches before it in the document"],
        "comments": ["Ada on 'the second paragraph': 'is this still true?', and 2 replies"],
        "applied": ["`p:three` rewritten: 'new words'"], "kept": ["`p:four` is the document's own"],
        "removed": [], "replanned": 1,
    }


def test_a_report_becomes_diagnostics_at_the_right_levels(tmp_path: Path):
    j = _job(tmp_path)
    counts = doc_tools.report_diagnostics(j, _report())
    assert counts == doc_tools.SyncCounts(conflicts=2, notes=2, comments=1, chunked=True, lost=0)
    levels = [d.level for d in j.diagnostics]
    assert levels.count("conflict") == 2 and "note" not in levels
    clash = j.diagnostics[0]
    assert clash.where == "p:one" and "the document won" in clash.message
    assert j.diagnostics[1].where == "Appendix / p:two"      # a conflict knows its tab
    assert any("planned again 1 time(s)" in d.message for d in j.diagnostics)


def test_a_chunked_write_is_said_to_be_the_one_that_is_not_atomic(tmp_path: Path):
    """A single batch lands whole or not at all; several do not. An agent that reads
    'it failed' as 'nothing was written' would be wrong exactly here."""
    j = _job(tmp_path)
    doc_tools.report_diagnostics(j, _report())
    chunk = [d for d in j.diagnostics if d.where == "the write"]
    assert len(chunk) == 1 and "not atomic" in chunk[0].message
    unchunked = _report()
    unchunked["notes"] = []
    assert doc_tools.report_diagnostics(_job(tmp_path), unchunked).chunked is False


def test_the_one_note_that_is_a_loss_is_not_left_among_the_cautions(tmp_path: Path):
    """Most notes say what a sync left alone. This one says what it took away, and an
    agent reading fifteen warnings has no way to tell unless the levels differ: it is
    marked at the source (`doc_sync.LOSS_MARK`) rather than guessed at here, and it
    comes with the only thing left to do about it."""
    from beamer2slides import doc_sync
    lost = ("the paragraph 'Why this matters' is being moved, which drops "
            f"paragraphStyle.borderBetween — the document's, and {doc_sync.LOSS_MARK}")
    j = _job(tmp_path)
    report = _report()
    report["notes"] = [lost]
    counts = doc_tools.report_diagnostics(j, report)
    assert counts.lost == 1
    said = [d for d in j.diagnostics if d.where == "what is lost"]
    assert len(said) == 1 and "Why this matters" in said[0].message
    assert "no spelling for it" in said[0].message
    assert any("set the property again by hand" in step for step in j.next_steps)
    # And an ordinary note is still an ordinary note.
    assert doc_tools.report_diagnostics(_job(tmp_path), _report()).lost == 0


def test_an_open_comment_becomes_a_warning_because_nothing_else_can_see_one(tmp_path: Path):
    """A comment lives in Drive, not in the document's content: the merge is blind to it,
    and a sync that rewrites the passage answers it by accident."""
    j = _job(tmp_path)
    doc_tools.report_diagnostics(j, _report())
    said = [d for d in j.diagnostics if "open comment" in d.message]
    assert len(said) == 1 and said[0].level == "warning" and said[0].where == "the document"
    assert "Ada" in said[0].message and "is this still true?" in said[0].message
    assert any("read the open comments" in step for step in j.next_steps)
    uncommented = _report()
    uncommented["comments"] = []
    assert doc_tools.report_diagnostics(_job(tmp_path), uncommented).comments == 0


def test_a_merge_with_no_base_behind_it_says_so(tmp_path: Path):
    j = _job(tmp_path)
    report = _report()
    report["base"] = "none"
    report["notes"] = []
    report["comments"] = []
    doc_tools.report_diagnostics(j, report)
    assert any("assume_base decided the whole merge" in d.message for d in j.diagnostics)


# ---------------------------------------------------------------- the refusals

def test_pushing_a_file_that_already_names_a_document_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A second push makes a second document and leaves two halves of one story."""
    _, path, _, service = _pair(tmp_path, monkeypatch, [], True)
    result = doc_tools.doc_push(_ctx(tmp_path), file="doc.html")
    assert not result.ok and result.code == "already_pushed"
    assert result.data["document"] == "doc-1"
    assert any("doc_sync" in step for step in result.next_steps)
    assert service.batches == [] and not result.artifacts


def test_pushing_a_file_that_is_not_there_is_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _pair(tmp_path, monkeypatch, [], True)
    result = doc_tools.doc_push(_ctx(tmp_path), file="nowhere.html")
    assert not result.ok and result.code == "not_found"


def test_a_document_with_no_base_asks_which_side_to_assume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Both answers throw somebody's work away, so the answer is the person's to give —
    and the refusal has to carry what each one costs, not just the word 'no'."""
    _, path, _, service = _pair(tmp_path, monkeypatch, [], False)
    result = doc_tools.doc_sync(_ctx(tmp_path), file="doc.html")
    assert not result.ok and result.code == "base_choice_needed"
    assert [o["value"] for o in jobjs(result.data, "options")] == ["document-wins", "source-wins"]
    assert all(o["costs"] for o in jobjs(result.data, "options"))
    assert len(result.next_steps) == 2 and all("assume_base" in s for s in result.next_steps)
    assert service.sent() == 0                       # nothing was written before it asked


def test_an_assume_base_nobody_understands_is_a_bad_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _pair(tmp_path, monkeypatch, [], True)
    result = doc_tools.doc_sync(_ctx(tmp_path), file="doc.html", assume_base="mine")
    assert not result.ok and result.code == "bad_request" and result.data["options"]


def test_a_file_the_dialect_cannot_read_whole_stops_the_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """One mistyped tag and those words reach the document through no request at all,
    and the settle then writes the file again from the document and takes them out of
    the file too. Carrying on would say "0 requests, the two sides agree" about a file
    that says something the document has never heard."""
    _, path, _, service = _pair(tmp_path, monkeypatch, [], True)
    path.write_text(path.read_text(encoding="utf-8").replace(
        "</body>", "<ol><it id='item:5'>XX</li></ol></body>"), encoding="utf-8")
    result = doc_tools.doc_sync(_ctx(tmp_path), file="doc.html", dry_run=True)
    assert not result.ok and result.code == "bad_request", result.summary
    assert "'XX' outside any block" in result.summary
    assert any("<li>" in step for step in result.next_steps)
    assert service.sent() == 0


def test_adopt_refuses_to_write_over_a_file_that_names_another_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _pair(tmp_path, monkeypatch, [], True)
    (tmp_path / "other.html").write_text(
        doc_ir.to_html({"blocks": [], "document": "doc-9"}), encoding="utf-8")
    result = doc_tools.doc_adopt(_ctx(tmp_path), doc="doc-1", file="other.html")
    assert not result.ok and result.code == "already_pushed", result.summary
    assert "doc-9" in result.summary
    assert doc_ir.from_html((tmp_path / "other.html").read_text(encoding="utf-8")
                            ).get("document") == "doc-9"


def test_adopt_writes_the_file_a_document_nobody_pushed_never_had(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _pair(tmp_path, monkeypatch, [], True)
    result = doc_tools.doc_adopt(_ctx(tmp_path), doc="doc-1", file="taken.html")
    assert result.ok, result.summary
    assert result.data["blocks"] == len(BLOCKS) and result.data["tabs"] == 1
    assert result.data["file"] == "taken.html"
    assert (tmp_path / "taken.html").is_file()
    assert {a.kind for a in result.artifacts} >= {"html", "folder", "json"}
    assert "frozen runs" in result.summary                 # what adopt cannot recover
    assert any("doc_sync" in step for step in result.next_steps)


def test_adopt_names_the_block_an_edit_through_the_file_would_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A property the dialect never reads survives every ordinary edit and goes the
    moment its block is written again from nothing. The count is no use on its own —
    the agent is being handed a document and has to know *which* paragraph that is.

    `doc_world` holds only what the IR models, so the border is put on the read: it is
    what `documents.get` would answer for a document somebody built in the browser.
    """
    _, _, _, service = _pair(tmp_path, monkeypatch, [], True)
    plain = service.world.read

    def bordered() -> Document:
        doc = plain()
        # (A property no `google_types` read names: set through the JSON the read is.)
        body = jobjs(json_object(doc, "the read"), "tabs", 0, "documentTab", "body", "content")
        at = next(e for e in body
                  if "The second paragraph" in str(part(e.get("paragraph"), "e").get("elements")))
        jobj(at, "paragraph", "paragraphStyle")["borderBetween"] = {"width": {"magnitude": 1}}
        return doc

    monkeypatch.setattr(service.world, "read", bordered)
    result = doc_tools.doc_adopt(_ctx(tmp_path), doc="doc-1", file="taken.html")
    assert result.ok, result.summary
    said = [d.message for d in result.diagnostics]
    assert any("paragraphStyle.borderBetween" in line and "The second paragraph" in line
               and "would drop it" in line for line in said), said
    # And nothing is said about the paragraph that carries nothing.
    assert not any("The first paragraph" in line for line in said), said


def test_adopt_with_no_path_writes_inside_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`doc_sync.adopt` names the file after the document's title and resolves it against
    the current folder; the journey runs it in the workspace so it cannot land elsewhere."""
    _pair(tmp_path, monkeypatch, [], True)
    result = doc_tools.doc_adopt(_ctx(tmp_path), doc="doc-1")
    assert result.ok, result.summary
    assert result.data["file"] == "the-report.html"
    assert (tmp_path / "the-report.html").is_file()


# ---------------------------------------------------------------- end to end

def test_a_sync_writes_the_source_edit_and_then_has_nothing_left_to_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The whole journey against the offline world: plan, write, settle, regenerate.

    The property the Docs side is built on is the last assertion — after a sync the file,
    the document and the base all say the same thing, so the next sync writes nothing.
    """
    world, path, drive, service = _pair(
        tmp_path, monkeypatch, [comment("Ada", "The first paragraph", "is this still true?")], True)
    _reword(path, "which the source rewrites", "which the source rewrote, at some length")
    ctx = _ctx(tmp_path)

    result = doc_tools.doc_sync(ctx, file="doc.html")
    assert result.ok, result.summary
    assert result.data["written"] and jint(result.data, "requests") > 0
    assert jint(result.data, "applied") >= 1 and result.data["conflicts"] == 0
    assert result.data["open_comments"] == [
        "Ada on 'The first paragraph': 'is this still true?'"]
    assert any("open comment" in d.message for d in result.diagnostics)
    assert "0 requests" in result.summary                  # and what to check next
    # The words reached the document, and the file was regenerated from what it now says.
    said = doc_ir.runs_text(doc_world.read_ir(world, None, None)["blocks"][2].get("runs", []))
    assert said == "The second paragraph, which the source rewrote, at some length."
    assert "rewrote, at some length" in path.read_text(encoding="utf-8")
    kinds = {a.kind: a.ref for a in result.artifacts}
    assert kinds["html"] == "doc.html" and kinds["report"] == ".b2s/doc.sync-report.md"
    assert (tmp_path / ".b2s" / "doc.sync-report.json").is_file()
    assert result.data["base_file"] == ".b2s/doc.base.json"

    # And the base went to Drive, where the next checkout looks for it first.
    stored = docs.load_drive(drive, "doc-1", found=None, hint=None)
    assert stored is not None and stored.get("document") == "doc-1"
    again = doc_tools.doc_sync(ctx, file="doc.html")
    assert again.ok and again.data["requests"] == 0 and not again.data["written"]
    assert again.data["base"] == "drive"


def test_a_document_renamed_in_the_file_is_renamed_in_drive_and_stays_renamed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A Google Doc's title is its name in Drive and no `batchUpdate` request writes
    one, so the file's `<title>` was a dead letter: the sync dropped the rename and
    the settle then took it back out of the file, which reads the old name. It is the
    one thing the merge writes outside a batch."""
    world, path, drive, service = _pair(tmp_path, monkeypatch, [], True)
    _reword(path, "<title>The report</title>", "<title>The quarterly report</title>")
    ctx = _ctx(tmp_path)

    result = doc_tools.doc_sync(ctx, file="doc.html")
    assert result.ok, result.summary
    assert drive.names == ["The quarterly report"] and world.title == "The quarterly report"
    report = (tmp_path / ".b2s" / "doc.sync-report.md").read_text(encoding="utf-8")
    assert "renamed 'The quarterly report'" in report
    assert "<title>The quarterly report</title>" in path.read_text(encoding="utf-8")

    again = doc_tools.doc_sync(ctx, file="doc.html")
    assert again.ok and again.data["requests"] == 0
    assert drive.names == ["The quarterly report"]          # and never renamed twice


def test_a_dry_run_plans_the_same_edit_and_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    world, path, _, service = _pair(tmp_path, monkeypatch, [], True)
    _reword(path, "untouched by anyone", "untouched by anyone at all")
    before = json.dumps(world.read())

    result = doc_tools.doc_sync(_ctx(tmp_path), file="doc.html", dry_run=True)
    assert result.ok and result.data["dry_run"] and not result.data["written"]
    assert jint(result.data, "requests") > 0 and service.sent() == 0
    assert json.dumps(world.read()) == before
    assert any("dry_run=False" in step for step in result.next_steps)
    assert "Nothing was written" in result.summary
    assert "untouched by anyone at all" in path.read_text(encoding="utf-8")   # not rewritten
    plan = json.loads((tmp_path / ".b2s" / "doc.sync-report.json").read_text("utf-8"))
    assert plan["plan"] and plan["dry_run"]


def test_a_sync_with_nothing_to_do_says_the_two_sides_already_agree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _pair(tmp_path, monkeypatch, [], True)
    result = doc_tools.doc_sync(_ctx(tmp_path), file="doc.html", dry_run=True)
    assert result.ok and result.data["requests"] == 0
    assert "already say the same thing" in result.summary


def test_the_reader_wins_where_both_sides_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The bargain in one test: the document is what the reader says."""
    world, path, _, _ = _pair(tmp_path, monkeypatch, [], True)
    # Both sides change the same word, which is the one case a three-way merge cannot
    # take both of: the reader calls it theirs, the source calls it the third.
    at = doc_ir._span(doc_world.read_ir(world, None, None)["blocks"][2])[0] + len("The ")
    world.apply([
        {"deleteContentRange": {"range": {"startIndex": at, "endIndex": at + len("second")}}},
        {"insertText": {"location": {"index": at}, "text": "reader's"}}])
    _reword(path, "The second paragraph", "The third paragraph")

    result = doc_tools.doc_sync(_ctx(tmp_path), file="doc.html")
    assert result.ok, result.summary
    assert result.data["conflicts"] == 1
    clash = [d for d in result.diagnostics if d.level == "conflict"]
    assert len(clash) == 1 and "the document won" in clash[0].message
    said = doc_ir.runs_text(doc_world.read_ir(world, None, None)["blocks"][2].get("runs", []))
    assert said == "The reader's paragraph, which the source rewrites."
    # and the file was regenerated from the document, so it says the reader's word too.
    assert "paragraph, which the source rewrites." in path.read_text(encoding="utf-8")
    assert "The second paragraph" not in path.read_text(encoding="utf-8")


def test_a_paragraph_the_file_no_longer_has_is_deleted_and_said_out_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A source delete is a deletion in somebody's document, and nothing could undo it.

    An agent hears the count, the words and what to do about them. Measured on the
    playground, where an editor buffer older than the sync before it was saved back over
    the file: two paragraphs a reader had typed went, and the summary said "1 block(s)
    from the source, 0 kept from the document, 0 conflict(s)" — the rewrite, and not a
    word about the deletion (`doc_merge.deleted_blocks`).
    """
    world, path, _, _ = _pair(tmp_path, monkeypatch, [], True)
    text = path.read_text(encoding="utf-8")
    dropped = [line for line in text.splitlines() if "first paragraph" in line]
    path.write_text(text.replace(dropped[0] + "\n", ""), encoding="utf-8")

    result = doc_tools.doc_sync(_ctx(tmp_path), file="doc.html")
    assert result.ok, result.summary
    assert result.data["deleted"] == 1
    assert result.data["deleted_examples"] == \
        ["`paragraph:the-first-paragraph-untouched-by-anyone`: "
         "'The first paragraph, untouched by anyone.'"]
    assert "1 deleted from the document" in result.summary
    gone = [d for d in result.diagnostics if d.where == "what is gone"]
    assert len(gone) == 1 and gone[0].level == "warning"
    assert "deleted from the document" in gone[0].message
    assert "untouched by anyone" in gone[0].message           # the words, not just a count
    assert any("put them back into the canonical file" in step for step in result.next_steps)
    assert "untouched by anyone" not in json.dumps(doc_world.read_ir(world, None, None))
    report = (tmp_path / ".b2s" / "doc.sync-report.md").read_text(encoding="utf-8")
    assert "## Deleted from the document (no way back)" in report


@pytest.mark.parametrize("journey", [t.tool_name for t in doc_tools.TOOLS])
def test_a_journey_never_raises_across_the_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, journey: str):
    """Whatever goes wrong underneath, the agent gets a Result with a code it can branch
    on — never a traceback through the harness."""
    from beamer2slides.agent.types import CODES

    _pair(tmp_path, monkeypatch, [], True)

    def broken(creds: Credentials | None) -> NoReturn:
        raise RuntimeError("the Docs API fell over")

    monkeypatch.setattr(docs, "docs_service", broken)
    calls: dict[str, Callable[[AgentContext], Result]] = {"doc_push": lambda c: doc_tools.doc_push(c, file="doc.html", new_doc=True),
             "doc_sync": lambda c: doc_tools.doc_sync(c, file="doc.html"),
             "doc_adopt": lambda c: doc_tools.doc_adopt(c, doc="doc-1", file="x.html")}
    result = calls[journey](_ctx(tmp_path))
    assert result.tool == journey and "fell over" in result.summary
    assert not result.ok and result.code == "failed" and result.code in CODES
