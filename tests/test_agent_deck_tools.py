"""Offline tests of the four deck journeys as agent tools (src/beamer2slides/agent/deck_tools.py).

No Google and no network: what is checked here is the boundary the agent layer draws, not the
pipeline behind it (which `test_invariants` and `test_emit_requests` already cover). Three things
matter enough to be pinned:

* a journey that touches Google **never starts** without permission and credentials - the refusal
  comes back as a code, before any file or request;
* `deck_sync(dry_run=True)` is reachable from a context that may read Google but not write it,
  which is what makes "plan the merge, then decide" possible at all;
* every parameter carries a description, because those annotations *are* the documentation the
  model reads when it chooses a tool.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, get_args, get_origin

import pytest

from beamer2slides.agent import READS, READS_GOOGLE, WRITES, AgentContext, Job, LocalWorkspace, Result
from beamer2slides.agent.context import Tool
from beamer2slides.agent.deck_tools import (deck_convert, deck_inspect, deck_prepare, deck_sync,
                                            deck_upload, tex_label)
from beamer2slides.agent.schema import type_hints
from beamer2slides.json_types import JsonObject

from .json_reads import jarr, jat, jint, jobj, jobjs, jstr, jstrs

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

    from beamer2slides import sync
    from beamer2slides.emit import Preflight
    from beamer2slides.emit_model import ObjectMap
    from beamer2slides.emit_state import Emitted, EmitState

TESTS = Path(__file__).resolve().parent
DECKS = TESTS / "decks" / "out"
TOOLS = (deck_inspect, deck_convert, deck_prepare, deck_upload, deck_sync, tex_label)
GOOGLE_TOOLS = (deck_convert, deck_upload, deck_sync)


def google_args(fn: Tool) -> dict[str, str]:
    """The least each Google journey needs to be called at all, so a test of the *gate* is not
    a test of argument binding."""
    if fn is deck_sync:
        return {"pdf": "talk.pdf", "deck": "some-deck-id"}
    if fn is deck_upload:
        return {"out": "out/talk"}
    return {"pdf": "talk.pdf"}


def deck_pdf() -> Path:
    for name in ("01_basic.pdf", "14_misc.pdf", "07_images.pdf"):
        if (DECKS / name).is_file():
            return DECKS / name
    pytest.skip("no test PDFs built (run python tests/decks/build.py)")


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A workspace with one built deck in it. Copied, because a workspace confines reads."""
    root = tmp_path_factory.mktemp("agent-ws")
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    return root


@pytest.fixture(scope="module")
def inspected(workspace: Path) -> Result:
    return deck_inspect(AgentContext.offline(workspace), pdf="talk.pdf", checks=False,
                        debug_images=True)


class FakeGoogle:
    """Credentials that exist and are never used: the wrapper fetches them before the body runs,
    so a test of what the *body* refuses cannot use `NoGoogle`."""

    def credentials(self) -> Credentials:
        from google.oauth2.credentials import Credentials as UserCredentials
        return UserCredentials(token=None)

    def describe(self) -> JsonObject:
        return {"available": True, "source": "test", "scopes": []}


def may_read_google(root: Path) -> AgentContext:
    """Local work plus reading Google: what `deck_sync(dry_run=True)` is meant to be reachable from."""
    return AgentContext(workspace=LocalWorkspace(root), google=FakeGoogle(),
                        allow=frozenset({READS, WRITES, READS_GOOGLE}))


# ---------------------------------------------------------------- deck_inspect


def test_deck_inspect_classifies_a_pdf_and_writes_the_ir(inspected: Result, workspace: Path):
    import json

    assert inspected.ok, inspected.summary
    deck = json.loads((workspace / "out" / "talk" / "deck.json").read_text(encoding="utf-8"))
    assert inspected.data["slides"] == len(deck["slides"]) > 0
    assert (workspace / "out" / "talk" / "raw.json").exists()
    refs = {a.ref for a in inspected.artifacts}
    assert {"out/talk/raw.json", "out/talk/deck.json", "out/talk/debug"} <= refs
    for artifact in inspected.artifacts:
        assert (workspace / artifact.ref).exists(), artifact.ref


def test_deck_inspect_reports_what_the_agent_has_to_decide_on(inspected: Result):
    data = inspected.data
    assert data["elements"], "no element kinds counted"
    assert len(jarr(data, "slides_detail")) == data["slides"]
    assert len(jarr(data, "titles")) == data["slides"]
    assert data["fonts"], "no fonts seen in the PDF"
    labels = jobj(data, "labels")
    assert "unlabelled" in labels and "duplicates" in labels
    assert jat(data, "overlays", "mode") == "last"
    assert 2 <= len(inspected.summary.split()) < 200
    # Unlabelled frames are the thing that decides whether a later sync can work: they are in
    # `data` and said out loud, never only one of the two.
    if labels["unlabelled"]:
        assert any(d.level == "warning" for d in inspected.diagnostics)
        assert any("tex_label" in s for s in inspected.next_steps)


def test_deck_inspect_with_checks_finds_no_invariant_problems_on_a_clean_deck(workspace: Path):
    result = deck_inspect(AgentContext.offline(workspace), pdf="talk.pdf", checks=True)
    assert result.ok, result.summary
    assert jat(result.data, "checks", "ran") is True
    assert jat(result.data, "checks", "findings") == 0, jat(result.data, "checks", "by_check")
    assert "deck_convert" in " ".join(result.next_steps)


def test_deck_inspect_refuses_a_path_outside_the_workspace(tmp_path: Path):
    result = deck_inspect(AgentContext.offline(tmp_path / "ws"), pdf=str(deck_pdf()))
    assert not result.ok
    assert result.code == "outside_workspace", result.summary


def test_deck_inspect_refuses_something_that_is_not_a_built_pdf(workspace: Path):
    ctx = AgentContext.offline(workspace)
    assert deck_inspect(ctx, pdf="nowhere.pdf").code == "not_found"
    (Path(workspace) / "main.tex").write_text("hello", encoding="utf-8")
    assert deck_inspect(ctx, pdf="main.tex").code == "bad_request"


# ---------------------------------------------------------------- the Google boundary


@pytest.mark.parametrize("fn", GOOGLE_TOOLS, ids=lambda f: f.tool_name)
def test_a_google_journey_does_nothing_at_all_in_an_offline_context(fn: Tool, tmp_path: Path):
    """`AgentContext.offline` both forbids and has no credentials, and the tool must come back
    without having made a file, a request or an exception - whichever of the two says no first."""
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    ctx = AgentContext.offline(root)
    before = sorted(p.name for p in root.rglob("*"))
    result = fn(ctx, **google_args(fn))
    assert not result.ok
    # The gate runs before credentials are fetched, so a context that is offline *and* forbids
    # writing says `forbidden`; one that merely has no Google says `offline`.
    assert result.code in ("forbidden", "offline"), result.summary
    assert sorted(p.name for p in root.rglob("*")) == before, "an offline journey touched the disk"


@pytest.mark.parametrize("fn", GOOGLE_TOOLS, ids=lambda f: f.tool_name)
def test_a_google_journey_with_no_credentials_refuses_with_offline(fn: Tool, tmp_path: Path):
    from beamer2slides.agent import ALL_ACTIONS, NoGoogle

    root = tmp_path / "ws"
    root.mkdir(parents=True)
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    ctx = AgentContext(workspace=LocalWorkspace(root), google=NoGoogle(reason="offline", fix=None), allow=ALL_ACTIONS)
    result = fn(ctx, **google_args(fn))
    assert not result.ok and result.code == "offline", result.summary


def test_a_dry_run_sync_is_reachable_without_permission_to_write_google(tmp_path: Path):
    """The whole point of `needs=(READS, WRITES, READS_GOOGLE)`: an agent that may look at a deck
    but not change it can still plan the merge and show the person what it would do."""
    from beamer2slides.agent import NoGoogle

    root = tmp_path / "ws"
    root.mkdir(parents=True)
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    ctx = AgentContext(workspace=LocalWorkspace(root), google=NoGoogle(reason="offline", fix=None),
                       allow=frozenset({READS, WRITES, READS_GOOGLE}))
    result = deck_sync(ctx, pdf="talk.pdf", deck="some-deck-id", dry_run=True)
    assert not result.ok
    # It got past the gate: what stops it is this machine's credentials, not the policy.
    assert result.code != "forbidden"
    assert result.code in ("offline", "no_credentials", "needs_consent", "no_base"), result.summary


def test_a_real_sync_from_that_same_context_is_forbidden_before_anything_happens(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    before = sorted(p.name for p in root.rglob("*"))
    result = deck_sync(may_read_google(root), pdf="talk.pdf", deck="some-deck-id", dry_run=False)
    assert not result.ok and result.code == "forbidden", result.summary
    assert "writes_google" in result.summary
    assert sorted(p.name for p in root.rglob("*")) == before, "a forbidden sync made a folder"


def test_take_source_is_published_as_a_list_of_ids_a_person_chose(tmp_path: Path):
    """`--take-source` writes over what somebody wrote in the deck, so the tool has to be able to
    carry several ids at once (a person reads one report and answers it once) and its description
    has to say whose decision it is - INSTRUCTIONS.md says the same thing at greater length."""
    from beamer2slides.agent.schema import all_schemas

    schema = next(s for s in all_schemas() if s["name"] == "deck_sync")
    prop = schema["input_schema"]["properties"]["take_source"]
    assert "array" in jstrs(prop, "type") and jstr(prop, "items", "type") == "string"
    assert "take_source" not in schema["input_schema"].get("required", [])
    assert "never pick an id yourself" in jstr(prop, "description")
    # and it reaches the journey: a context that may not write refuses before the ids matter
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    result = deck_sync(may_read_google(root), pdf="talk.pdf", deck="d", take_source=["abc12345"])
    assert not result.ok and result.code == "forbidden", result.summary


def test_a_bad_backup_mode_is_a_bad_request_not_a_crash(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    result = deck_sync(may_read_google(root), pdf="talk.pdf", deck="d", dry_run=True, backup="maybe")
    assert result.code == "bad_request" and "maybe" in result.summary
    assert result.data["allowed"] == ["auto", "none", "file", "drive", "both"]


def test_a_refused_rebuild_comes_back_as_a_code_with_the_edits_named(tmp_path: Path):
    """The single most important behaviour in the library: a rebuild never destroys deck edits.

    At a terminal that is a paragraph of prose ending in three commands. An agent gets the code,
    the named edits in `data`, and the three ways forward as steps - so it can say *which* slides
    someone worked on instead of quoting the paragraph back.
    """
    from beamer2slides.agent.context import Job
    from beamer2slides.agent.deck_tools import _refuse_rebuild
    from beamer2slides.agent.types import Refused
    from beamer2slides.guard import RebuildRefused

    root = tmp_path / "ws"
    root.mkdir()
    ctx = AgentContext.offline(root)
    job = Job("deck_convert", ctx)
    survey: JsonObject = {
        "presentationId": "PID123", "revisionId": "r7", "reason": "edited", "edited": True,
              "examples": ["slide 3: the text of `b2s_s003_t0` was changed",
                           "slide 8 was added in Slides"],
              "counts": {"text": 1}, "slides_added": 1, "slides_deleted": 0, "reordered": False}
    with pytest.raises(Refused) as caught:
        _refuse_rebuild(job, RebuildRefused("refusing to rebuild: ...", survey), root / "talk.pdf",
                        root / "out" / "talk")
    assert caught.value.code == "deck_edited"
    assert caught.value.data["examples"] == survey["examples"]
    assert jstr(caught.value.data, "url").endswith("PID123/edit")
    assert caught.value.data["revisionId"] == "r7"
    steps = " ".join(job.next_steps)
    assert "deck_sync" in steps and "new_deck=True" in steps and "force_rebuild=True" in steps


def test_a_rebuild_with_no_way_back_is_its_own_refusal(tmp_path: Path):
    """`guard` raises the same exception when a forced rebuild could not keep a backup, which is a
    different decision: there is nothing to protect, only nothing to go back to."""
    from beamer2slides.agent.context import Job
    from beamer2slides.agent.deck_tools import _refuse_rebuild
    from beamer2slides.agent.types import Refused
    from beamer2slides.guard import RebuildRefused

    root = tmp_path / "ws"
    root.mkdir()
    job = Job("deck_convert", AgentContext.offline(root))
    with pytest.raises(Refused) as caught:
        _refuse_rebuild(job, RebuildRefused("could not export the deck", {"reason": "backup-failed"}),
                        root / "talk.pdf", root / "out" / "talk")
    assert caught.value.code == "no_way_back"
    assert any("backup='none'" in s for s in job.next_steps)


def test_a_refusal_never_hands_the_agent_a_shell_command(tmp_path: Path):
    """The library's own refusal ends in three commands, `--force-rebuild` among them.

    It is written for a person at a terminal, where offering the escape hatch beside the two
    safe routes is honest. Handed to a model it is the opposite: prose is the first thing read,
    so the refusal would be teaching the one move it exists to prevent - and offering a shell as
    the way round a tool that has just said no. The facts belong in `data` and the ways forward
    in `next_steps`, both as calls that can actually be made.
    """
    from beamer2slides.agent.context import Job
    from beamer2slides.agent.deck_tools import _refuse_rebuild
    from beamer2slides.agent.types import Refused
    from beamer2slides.guard import RebuildRefused, refusal_message

    root = tmp_path / "ws"
    root.mkdir()
    out = root / "out" / "talk"
    survey: JsonObject = {
        "presentationId": "PID123", "revisionId": "r7", "reason": "edited", "edited": True,
              "examples": ["slide 3: the text of `b2s_s003_t0` was changed"], "slides": ["b2s_s003"],
              "counts": {"text": 1}, "slides_added": 0, "slides_deleted": 0, "reordered": False}
    # The real prose, not a stand-in: this test is about what the library actually raises.
    cli = refusal_message("PID123", out, root / "talk.pdf", survey, "edited")
    assert "--force-rebuild" in cli, "the message this test guards against has changed"

    for reason, code in (("edited", "deck_edited"), ("backup-failed", "no_way_back")):
        job = Job("deck_convert", AgentContext.offline(root))
        with pytest.raises(Refused) as caught:
            _refuse_rebuild(job, RebuildRefused(cli, {**survey, "reason": reason}),
                            root / "talk.pdf", out)
        assert caught.value.code == code
        said = str(caught.value)                    # what becomes `Result.summary`
        for shell in ("python -m beamer2slides", "--force-rebuild", "--new-deck", "--backup"):
            assert shell not in said, f"{code} hands the agent `{shell}`"
        assert "Nothing was written." in said
    # Forcing is still reachable - as an argument to this tool, and named as somebody's decision.
    assert any("force_rebuild=True" in s for s in job.next_steps)


# ------------------------------------------------- deck_prepare / deck_upload: the two halves


@pytest.fixture(scope="module")
def prepared(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Result]:
    """One `deck_prepare` in a context with no Google at all, reused by the tests below."""
    root = tmp_path_factory.mktemp("agent-prepare")
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    return root, deck_prepare(AgentContext.offline(root), pdf="talk.pdf")


class Uploaded:
    """What the stand-in `emit` and `snapshot_after_convert` were handed (`fake_google`)."""

    def __init__(self, base_problems: list[str]) -> None:
        self.base_problems = base_problems          # what the base's Drive write says went wrong
        self.emitted = False
        self.named: Path | None = None
        self.slides = 0
        self.backgrounds: list[str] = []
        self.backup = ""
        self.based = False
        self.pdf: Path | str | JsonObject | None = None
        self.overlays = ""


def fake_google(monkeypatch: pytest.MonkeyPatch, base_problems: list[str]) -> Uploaded:
    """`emit` and `snapshot_after_convert` replaced by two functions that only remember what they
    were handed. Both are imported inside `_upload`, so the module attribute is what is called."""

    from beamer2slides.emit_state import EmitState, Emitted

    seen = Uploaded(base_problems)

    def emit(deck: ObjectMap, out_dir: Path, name: str, new_deck: bool, measure: bool, force_rebuild: bool,
             backup: str, named: Path | None, checked: Preflight | None) -> Emitted:
        slides = deck["slides"]
        assert isinstance(slides, list)
        seen.emitted, seen.named, seen.slides, seen.backup = True, named, len(slides), backup
        seen.backgrounds = sorted(p.name for p in (out_dir / "backgrounds").glob("*"))
        state = EmitState(presentation_id="PID123", url="https://docs.google.com/presentation/d/PID123/edit",
                          scale=1.0, slides=(), contained=None, theme=None, previous=None)
        return Emitted(state=state, deck={"presentationId": "PID123", "slides": []})

    def snapshot_after_convert(deck: JsonObject, out: Path, state: EmitState, pdf: Path | str | JsonObject | None,
                               overlays: str, problems: list[str] | None) -> JsonObject:
        seen.based, seen.pdf, seen.overlays = True, pdf, overlays
        if problems is not None:
            problems.extend(seen.base_problems)
        return {"slides": [{}, {}]}

    monkeypatch.setattr("beamer2slides.emit.emit", emit)
    monkeypatch.setattr("beamer2slides.snapshot.snapshot_after_convert", snapshot_after_convert)
    return seen


def test_deck_prepare_writes_the_folder_the_upload_half_builds_from(prepared: tuple[Path, Result]):
    """The local half of a conversion, in a context that has no account and is not allowed one."""
    import json

    from beamer2slides import identity

    root, result = prepared
    assert result.ok, result.summary
    out = root / "out" / "talk"
    assert (out / "deck.json").is_file()
    assert list((out / "backgrounds").glob("*.png")), "no background pictures rendered"
    assert not (out / "emit.json").exists() and not (out / "sync").exists()

    facts = json.loads((out / "prepared.json").read_text(encoding="utf-8"))
    assert facts["version"] == 1 and facts["overlays"] == "last"
    assert facts["name"] == "talk.pdf"
    assert facts["source"]["sha1"] == identity.sha1((root / "talk.pdf").read_bytes())
    assert facts["facts"]["slides"] == jint(result.data, "slides") > 0
    assert "deck_upload" in " ".join(result.next_steps)


def test_deck_upload_needs_nothing_from_the_workspace_but_the_prepared_folder(prepared: tuple[Path, Result],
                                                                              tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch):
    """The minimal input set, which is the whole point of the split: the folder and nothing else.

    The PDF's *bytes* are wanted by exactly one code path - `emit.fallback_pictures`, the retry
    that crops a refused element's region out of the page - and by neither the guard (which reads
    the file's **name**, to catch a folder whose deck came from another PDF) nor the base (which
    wants a name and a digest, both measured where the file was). So a workspace holding the
    prepared folder alone converts, and says that the one retry is out of reach.
    """
    import json

    from beamer2slides import identity
    from beamer2slides.agent import ALL_ACTIONS

    root, _ = prepared
    lonely = tmp_path / "uploader"
    shutil.copytree(root / "out" / "talk", lonely / "out" / "talk")
    assert not list(lonely.rglob("*.pdf")), "the PDF must not cross the boundary"
    # deck.json says where the PDF was on the machine that classified it; here that is nothing.
    where = lonely / "out" / "talk" / "deck.json"
    deck = json.loads(where.read_text(encoding="utf-8"))
    deck["source"]["pdf"] = "/prepared/on/another/machine/talk.pdf"
    where.write_text(json.dumps(deck), encoding="utf-8")

    seen = fake_google(monkeypatch, [])

    ctx = AgentContext(workspace=LocalWorkspace(lonely), google=FakeGoogle(), allow=ALL_ACTIONS)
    result = deck_upload(ctx, out="out/talk")
    assert result.ok, result.summary

    prepared_json = json.loads((lonely / "out" / "talk" / "prepared.json").read_text(encoding="utf-8"))
    # The guard gets the name, not the file.
    assert seen.emitted and str(seen.named) == "talk.pdf"
    assert seen.slides == prepared_json["facts"]["slides"]
    assert seen.backgrounds, "the pictures came with the folder"
    # The base records the digest measured on the machine that had the PDF.
    assert seen.based and seen.pdf == {"pdf": prepared_json["source"]["pdf"],
                                       "sha1": identity.sha1((root / "talk.pdf").read_bytes())}
    assert seen.overlays == prepared_json["overlays"]
    assert jstr(result.data, "url").endswith("PID123/edit") and result.data["base_slides"] == 2
    # And the one thing the folder alone cannot do is said, rather than found out inside a retry.
    assert result.data["can_crop_refused_elements"] is False


def test_deck_upload_says_where_the_pdf_is_now_when_it_is_handed_one(prepared: tuple[Path, Result], tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch):
    """deck.json records where the PDF was when it was classified, which on the uploading machine
    is a path to nothing. Handed the file, the upload half says where it is now - or
    `fallback_pictures` would crop a refused element's region out of a file that is not there."""
    import json

    from beamer2slides.agent import ALL_ACTIONS

    root, _ = prepared
    other = tmp_path / "both"
    shutil.copytree(root / "out" / "talk", other / "out" / "talk")
    shutil.copy2(root / "talk.pdf", other / "slides.pdf")

    seen = fake_google(monkeypatch, [])

    ctx = AgentContext(workspace=LocalWorkspace(other), google=FakeGoogle(), allow=ALL_ACTIONS)
    result = deck_upload(ctx, out="out/talk", pdf="slides.pdf")
    assert result.ok, result.summary
    assert result.data["can_crop_refused_elements"] is True
    deck = json.loads((other / "out" / "talk" / "deck.json").read_text(encoding="utf-8"))
    assert deck["source"]["pdf"] == str(other / "slides.pdf")
    assert seen.named == other / "slides.pdf"


def test_a_folder_prepared_before_the_split_still_uploads(prepared: tuple[Path, Result], tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch):
    """`deck_convert` wrote these folders for a year before `prepared.json` existed. deck.json
    carries the source block classify copied out of the PDF, so the title and the name are both
    there; only the digest is not, and that costs a later interrupted sync one conservative
    branch, not a loss. It is said out loud and nothing is refused."""
    from beamer2slides.agent import ALL_ACTIONS

    root, _ = prepared
    old = tmp_path / "old"
    shutil.copytree(root / "out" / "talk", old / "out" / "talk")
    (old / "out" / "talk" / "prepared.json").unlink()

    seen = fake_google(monkeypatch, [])

    ctx = AgentContext(workspace=LocalWorkspace(old), google=FakeGoogle(), allow=ALL_ACTIONS)
    result = deck_upload(ctx, out="out/talk")
    assert result.ok, result.summary
    assert seen.emitted and str(seen.named) == "talk.pdf", "the name still comes out of deck.json"
    assert isinstance(seen.pdf, dict) and seen.pdf["sha1"] is None
    assert any("prepared.json" in d.message for d in result.diagnostics)


def test_deck_upload_refuses_a_folder_nobody_prepared(tmp_path: Path):
    from beamer2slides.agent import ALL_ACTIONS

    root = tmp_path / "ws"
    (root / "out" / "talk").mkdir(parents=True)
    ctx = AgentContext(workspace=LocalWorkspace(root), google=FakeGoogle(), allow=ALL_ACTIONS)
    assert deck_upload(ctx, out="out/talk").code == "not_found"
    (root / "out" / "talk" / "deck.json").write_text('{"slides": []}', encoding="utf-8")
    (root / "out" / "talk" / "prepared.json").write_text('{"version": 99}', encoding="utf-8")
    result = deck_upload(ctx, out="out/talk")
    assert result.code == "bad_request" and "another version" in result.summary


def test_deck_convert_is_those_two_halves_and_nothing_else(prepared: tuple[Path, Result], tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch):
    """The split is a refactor of `deck_convert`'s own body, so the whole journey must still write
    the same folder - with prepared.json in it, since a folder is a folder either way."""
    import json

    from beamer2slides.agent import ALL_ACTIONS

    root, _ = prepared
    whole = tmp_path / "whole"
    whole.mkdir()
    shutil.copy2(root / "talk.pdf", whole / "talk.pdf")

    seen = fake_google(monkeypatch, [])

    ctx = AgentContext(workspace=LocalWorkspace(whole), google=FakeGoogle(), allow=ALL_ACTIONS)
    result = deck_convert(ctx, pdf="talk.pdf")
    assert result.ok, result.summary

    halves = json.loads((root / "out" / "talk" / "prepared.json").read_text(encoding="utf-8"))
    once = json.loads((whole / "out" / "talk" / "prepared.json").read_text(encoding="utf-8"))
    assert once["facts"] == halves["facts"] and once["labels"] == halves["labels"]
    assert once["source"]["sha1"] == halves["source"]["sha1"]
    assert json.loads((whole / "out" / "talk" / "deck.json").read_text(encoding="utf-8")) \
        != {}, "deck.json was written"
    assert seen.based and seen.pdf == whole / "talk.pdf", "the whole journey has the file itself"
    assert result.data["can_crop_refused_elements"] is True


def test_the_split_halves_declare_the_least_each_one_does(prepared: tuple[Path, Result]):
    """Mail item 1's real content: `deck_prepare` must be callable where `deck_convert` is not,
    and `deck_upload` must want Google before it does anything at all."""
    from beamer2slides.agent.context import LOCAL_ONLY

    assert set(deck_prepare.needs) == set(LOCAL_ONLY)
    assert "writes_google" in deck_upload.needs and "writes_google" not in deck_prepare.needs
    assert set(deck_convert.needs) == set(deck_upload.needs) | set(deck_prepare.needs)


def test_a_base_drive_would_not_take_is_a_warning_not_a_line_in_the_log(prepared: tuple[Path, Result], tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch):
    """A conversion that kept its base only in the folder used to report plain success: the
    warning was a print, and prints are the log. Drive is where the next sync looks first, so in a
    detached context the person's first edit came back `no_base` with nothing here to say why."""
    from beamer2slides.agent import ALL_ACTIONS

    root, _ = prepared
    ws = tmp_path / "ws"
    shutil.copytree(root / "out" / "talk", ws / "out" / "talk")
    fake_google(monkeypatch, ["could not store the sync base in Drive (HttpError 403); it was "
                              "kept only in out/talk/sync/base.json"])
    result = deck_upload(AgentContext(workspace=LocalWorkspace(ws), google=FakeGoogle(), allow=ALL_ACTIONS),
                         out="out/talk")
    assert result.ok, result.summary
    warned = [d for d in result.diagnostics if d.where == "sync base"]
    assert len(warned) == 1 and "HttpError 403" in warned[0].message


def test_a_forced_rebuild_in_a_detached_context_keeps_its_way_back_in_drive(prepared: tuple[Path, Result],
                                                                            monkeypatch: pytest.MonkeyPatch):
    """`auto` is a .pptx in the workspace, which a detached context deletes when the call returns:
    the backup that makes a forced rebuild acceptable would be written and destroyed."""
    from beamer2slides.agent import ALL_ACTIONS

    root, _ = prepared
    seen = fake_google(monkeypatch, [])
    with AgentContext.detached(google=FakeGoogle(), allow=ALL_ACTIONS) as ctx:
        assert ctx.ephemeral
        shutil.copytree(root / "out" / "talk", ctx.workspace.root / "out" / "talk")
        assert deck_upload(ctx, out="out/talk", force_rebuild=True).ok
        assert seen.backup == "drive"
        assert deck_upload(ctx, out="out/talk").ok          # not forced: auto keeps its meaning
        assert seen.backup == "auto"
        assert deck_upload(ctx, out="out/talk", force_rebuild=True, backup="file").ok
        assert seen.backup == "file", "a mode somebody named is never changed"
    local = AgentContext(workspace=LocalWorkspace(root), google=FakeGoogle(), allow=ALL_ACTIONS)
    assert not local.ephemeral


# ---------------------------------------------------------------- deck_sync's refusals and way back


class Point:
    """What `record_sync_point` hands `sync`: a way back, collected when asked. `asked`: the
    stand-in sync wrote (it reports requests), so it asked for it before its first write."""

    def __init__(self, note: JsonObject | None, asked: bool) -> None:
        self.note, self.asked = note, asked

    def result(self) -> JsonObject | None:
        self.asked = True
        return self.note

    def kept(self) -> JsonObject | None:
        return self.note if self.asked else None


def sync_world(monkeypatch: pytest.MonkeyPatch, *, raises: BaseException | None, note: JsonObject | None,
               asked: bool) -> dict[str, str]:
    """`sync.sync` and `record_sync_point` replaced; returns the backup modes they were handed.

    `raises`: what the stand-in sync raises instead of answering; `note`: the way back the point
    keeps; `asked`: whether the sync asked for it (it did when it wrote)."""
    seen: dict[str, str] = {}

    def record_sync_point(pdf: Path, deck: str, out: Path | None, backup: str) -> Point:
        seen["point_backup"] = backup
        return Point(note, asked)

    def run_sync(pdf: Path, deck: str, out: Path | None, dry_run: bool, overlays: str | None, measure: bool,
                 way_back: object, backup: str, force_adopted: bool, follow_labels: bool,
                 take_source: Sequence[str]) -> sync.SyncResult:
        from beamer2slides import sync
        seen["sync_backup"] = backup
        if raises is not None:
            raise raises
        said = sync.SlidesSaid(created=[], deleted=[], moved=[], kept=[], held=[], user_added=[])
        report = sync.SyncReport(applied=[], overrides=[], conflicts=[], resolved=[], converged=[], user_objects=[],
                                 slides=said, warnings=[], contained=None, base_forms=None, overruns=[], refit=[])
        return sync.SyncResult(pdf=str(pdf), presentation_id="PID", url="https://docs.google.com/presentation/d/PID/edit",
                               dry_run=dry_run, base_from="local", generation=1, overlays="last", attempts=1,
                               requests={"text": 3}, seconds=0.5, report=report, actions=[], recovery=None)

    monkeypatch.setattr("beamer2slides.__main__.record_sync_point", record_sync_point)
    monkeypatch.setattr("beamer2slides.sync.sync", run_sync)
    return seen


def sync_ctx(root: Path) -> AgentContext:
    from beamer2slides.agent import ALL_ACTIONS

    root.mkdir(parents=True, exist_ok=True)
    (root / "talk.pdf").write_bytes(b"%PDF-1.5 stand-in")
    return AgentContext(workspace=LocalWorkspace(root), google=FakeGoogle(), allow=ALL_ACTIONS)


def test_only_a_missing_base_is_no_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Three places in sync say no by exiting, and each wants a different next step. `no_base`
    suggests converting; for a base that describes another copy of the deck that is the one move
    that makes a second deck beside somebody's edited one."""
    from beamer2slides.sync import BaseMismatch, NoSyncBase

    sync_world(monkeypatch, raises=NoSyncBase("no sync base for presentation PID"), note=None, asked=True)
    missing = deck_sync(sync_ctx(tmp_path / "a"), pdf="talk.pdf", deck="PID")
    assert missing.code == "no_base"
    assert any("deck_convert" in s for s in missing.next_steps)

    sync_world(monkeypatch, raises=BaseMismatch("the sync base describes none of the slides... Convert "
                                                "the PDF again (python -m beamer2slides convert)"),
               note=None, asked=True)
    other = deck_sync(sync_ctx(tmp_path / "b"), pdf="talk.pdf", deck="PID")
    assert other.code == "base_mismatch"
    assert not any("deck_convert" in s for s in other.next_steps)
    assert "python -m beamer2slides" not in other.summary, "the CLI's advice is not relayed"
    assert "Do not convert" in other.summary

    # Still what the CLI stops on.
    assert issubclass(NoSyncBase, SystemExit) and issubclass(BaseMismatch, SystemExit)


def test_a_folder_that_names_no_deck_is_not_found_rather_than_no_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sync_world(monkeypatch, raises=None, note=None, asked=True)
    ctx = sync_ctx(tmp_path / "ws")
    (tmp_path / "ws" / "empty").mkdir()
    result = deck_sync(ctx, pdf="talk.pdf", deck="empty")
    assert result.code == "not_found" and "names no deck" in result.summary


def test_the_way_back_is_reported_where_a_caller_can_branch_on_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "ws"
    kept = root / "out" / "talk" / "backups" / "20260923-before.pptx"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"pptx")
    note: JsonObject = {"out": str(root / "out" / "talk"),
            "entry": {"presentationId": "PID", "revisionId": "r7",
                      "backup": {"mode": "both", "file": str(kept),
                                 "drive": {"presentationId": "COPY", "url": "https://copy"},
                                 "warnings": ["could not copy the deck in Drive (quota)"]}}}
    sync_world(monkeypatch, raises=None, note=note, asked=True)
    result = deck_sync(sync_ctx(root), pdf="talk.pdf", deck="PID")
    assert result.ok, result.summary
    assert [d.message for d in result.diagnostics if d.where == "backup"] == \
        ["could not copy the deck in Drive (quota)"]
    pptx = [a for a in result.artifacts if a.kind == "pptx"]
    assert [a.ref for a in pptx] == ["out/talk/backups/20260923-before.pptx"]
    assert jat(result.data, "backup_copy", "url") == "https://copy" and result.data["backup_mode"] == "both"
    assert jat(result.data, "recovery", "revisionId") == "r7"


def test_a_sync_that_recorded_no_way_back_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sync_world(monkeypatch, raises=None, note=None, asked=True)
    result = deck_sync(sync_ctx(tmp_path / "ws"), pdf="talk.pdf", deck="PID")
    assert result.ok, result.summary
    assert any(d.where == "backup" and "no way back was recorded" in d.message for d in result.diagnostics)
    dry = deck_sync(sync_ctx(tmp_path / "dry"), pdf="talk.pdf", deck="PID", dry_run=True)
    assert not [d for d in dry.diagnostics if d.where == "backup"], "a dry run keeps none and writes none"


def test_a_sync_that_never_asked_for_its_way_back_neither_waits_nor_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A sync that wrote nothing never reaches `Sync.before_write`: its backup was not needed,
    and waiting for it cost two minutes a sync on a CJK deck."""
    sync_world(monkeypatch, raises=None, note={"out": "x", "entry": {"backup": {"warnings": ["slow"]}}}, asked=False)
    result = deck_sync(sync_ctx(tmp_path / "ws"), pdf="talk.pdf", deck="PID")
    assert result.ok, result.summary
    assert not [d for d in result.diagnostics if d.where == "backup"]
    assert "recovery" not in result.data


def test_auto_means_a_drive_copy_where_the_workspace_goes_away(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from beamer2slides.agent import ALL_ACTIONS

    from beamer2slides.agent.content import MemoryWorkspace

    seen = sync_world(monkeypatch, raises=None, note=None, asked=True)
    with AgentContext.detached(google=FakeGoogle(), allow=ALL_ACTIONS) as ctx:
        assert isinstance(ctx.workspace, MemoryWorkspace)
        pdf = ctx.workspace.put("talk.pdf", b"%PDF-1.5 stand-in")
        assert deck_sync(ctx, pdf=pdf, deck="PID").ok
        assert seen["point_backup"] == seen["sync_backup"] == "drive"
        assert deck_sync(ctx, pdf=pdf, deck="PID", backup="none").ok
        assert seen["point_backup"] == "none"
    assert deck_sync(sync_ctx(tmp_path / "ws"), pdf="talk.pdf", deck="PID").ok
    assert seen["point_backup"] == "auto", "a workspace that stays keeps its .pptx"


# ---------------------------------------------------------------- the content fetcher


def test_the_wrapper_installs_the_context_s_content_fetcher_and_never_the_model_s(tmp_path: Path):
    """`fetch_google_content` is what the library downloads Google's pictures with; `fetch` is
    what a *model's* URL argument is fetched with, and a harness that refuses those must not have
    to open that door to download pictures. The wrapper installs the first and only the first."""
    from beamer2slides import google_auth, net
    from beamer2slides.agent.context import tool

    seen: list[object] = []

    @tool("probe_fetcher", needs=(READS,), local=None)
    def probe(j: Job) -> None:
        seen.append(google_auth.fetcher_for_threads())

    def pictures(url: str) -> bytes:
        return b"picture"

    def models(url: str) -> bytes:
        return b"model"

    probe(AgentContext.offline(tmp_path, fetch=models, fetch_google_content=pictures))
    probe(AgentContext.offline(tmp_path, fetch=models))
    assert seen == [pictures, net.urllib_fetch]
    assert google_auth.fetcher_for_threads() is net.urllib_fetch, "nothing is left installed"


# ---------------------------------------------------------------- tex_label


FRAMES = r"""\documentclass{beamer}
\begin{document}
\begin{frame}{Introduction}
  Hello.
\end{frame}
\begin{frame}[fragile]{Results and more}
  Numbers.
\end{frame}
\begin{frame}[label=summary]{Summary}
  Done.
\end{frame}
\end{document}
"""


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "talk"
    root.mkdir()
    (root / "main.tex").write_text(FRAMES, encoding="utf-8")
    return root


def test_tex_label_plans_without_writing(source: Path):
    result = tex_label(AgentContext.offline(source), tex="main.tex")
    assert result.ok, result.summary
    assert result.data["frames"] == 3 and result.data["labelled"] == 1
    assert result.data["planned"] == 2
    assert [e["label"] for e in jobjs(result.data, "edits")] and all(e["title"] for e in jobjs(result.data, "edits"))
    assert result.data["applied"] is False
    assert (source / "main.tex").read_text(encoding="utf-8") == FRAMES, "apply=False wrote"
    assert not list(source.glob("*.bak"))
    assert any("apply=True" in s for s in result.next_steps)


def test_tex_label_applies_and_keeps_what_was_there(source: Path):
    result = tex_label(AgentContext.offline(source), tex="main.tex", apply=True)
    assert result.ok, result.summary
    assert result.data["applied"] is True
    text = (source / "main.tex").read_text(encoding="utf-8")
    assert "label=introduction" in text and "label=summary" in text
    assert text.count("label=") == 3
    bak = source / "main.tex.bak"
    assert bak.exists() and bak.read_text(encoding="utf-8") == FRAMES
    assert {a.ref for a in result.artifacts} >= {"main.tex", "main.tex.bak"}
    # A second run has nothing to do, and must not touch the labels the first one wrote.
    again = tex_label(AgentContext.offline(source), tex="main.tex", apply=True)
    assert again.ok and again.data["planned"] == 0
    assert (source / "main.tex").read_text(encoding="utf-8") == text


def test_tex_label_reports_a_label_written_twice_as_a_conflict(source: Path):
    """A duplicate reaches the PDF once (hyperref keeps the first destination), so the second
    frame looks unlabelled from there on. The .tex is the only place it can be seen."""
    (source / "main.tex").write_text(FRAMES.replace("[fragile]{Results", "[label=summary]{Results"),
                                     encoding="utf-8")
    result = tex_label(AgentContext.offline(source), tex="main.tex")
    assert result.ok, result.summary
    assert [d["label"] for d in jobjs(result.data, "duplicates")] == ["summary"]
    assert any(d.level == "conflict" and "summary" in d.message for d in result.diagnostics)


def test_tex_label_refuses_a_file_that_is_not_a_beamer_document(source: Path):
    (source / "notes.tex").write_text("\\documentclass{article}\nnothing here\n", encoding="utf-8")
    ctx = AgentContext.offline(source)
    assert tex_label(ctx, tex="notes.tex").code == "bad_request"
    assert tex_label(ctx, tex="missing.tex").code == "not_found"


# ---------------------------------------------------------------- the tools as an interface


@pytest.mark.parametrize("fn", TOOLS, ids=lambda f: f.tool_name)
def test_every_parameter_is_annotated_with_a_description(fn: Tool):
    """`schema.py` turns these annotations into JSON Schema, so an undescribed parameter is an
    undocumented one - the model would have to guess what it means from its name."""
    hints = type_hints(fn.body)
    names = [n for n in fn.body.__code__.co_varnames[:fn.body.__code__.co_argcount] if n != "j"]
    assert names, f"{fn.tool_name} takes no arguments"
    for name in names:
        hint = hints[name]
        assert get_origin(hint) is Annotated, f"{fn.tool_name}({name}) is not Annotated"
        description = get_args(hint)[1]
        assert isinstance(description, str) and len(description.split()) >= 4, \
            f"{fn.tool_name}({name}): {description!r} is not a description"


@pytest.mark.parametrize("fn", TOOLS, ids=lambda f: f.tool_name)
def test_the_annotations_resolve_through_the_wrapper_too(fn: Tool):
    """`functools.wraps` copies `__annotations__` but not `__globals__`, so a module using
    `from __future__ import annotations` would hand the schema generator unresolvable strings."""
    for target in (fn, fn.body):
        hints = type_hints(target)
        assert all(get_origin(h) is Annotated for n, h in hints.items() if n not in ("j", "return"))


@pytest.mark.parametrize("fn", TOOLS, ids=lambda f: f.tool_name)
def test_every_tool_says_what_it_does_and_what_it_costs(fn: Tool):
    assert fn.tool_name and fn.needs
    assert READS in fn.needs
    doc = (fn.body.__doc__ or "").strip().splitlines()
    assert 3 <= len([line for line in doc if line.strip()]) <= 8, f"{fn.tool_name}: {len(doc)} lines"
    assert any(w in " ".join(doc).lower() for w in ("cost", "second", "s ", "google")), \
        f"{fn.tool_name}'s docstring says nothing about what it costs"
