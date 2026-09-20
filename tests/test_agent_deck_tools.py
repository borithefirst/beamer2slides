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

import shutil
from pathlib import Path
from typing import Annotated, get_args, get_origin, get_type_hints

import pytest

from beamer2slides.agent import READS, READS_GOOGLE, WRITES, AgentContext, LocalWorkspace
from beamer2slides.agent.deck_tools import deck_convert, deck_inspect, deck_sync, tex_label

TESTS = Path(__file__).resolve().parent
DECKS = TESTS / "decks" / "out"
TOOLS = (deck_inspect, deck_convert, deck_sync, tex_label)
GOOGLE_TOOLS = (deck_convert, deck_sync)


def deck_pdf() -> Path:
    for name in ("01_basic.pdf", "14_misc.pdf", "07_images.pdf"):
        if (DECKS / name).is_file():
            return DECKS / name
    pytest.skip("no test PDFs built (run python tests/decks/build.py)")


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> Path:
    """A workspace with one built deck in it. Copied, because a workspace confines reads."""
    root = tmp_path_factory.mktemp("agent-ws")
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    return root


@pytest.fixture(scope="module")
def inspected(workspace):
    return deck_inspect(AgentContext.offline(workspace), pdf="talk.pdf", checks=False,
                        debug_images=True)


class FakeGoogle:
    """Credentials that exist and are never used: the wrapper fetches them before the body runs,
    so a test of what the *body* refuses cannot use `NoGoogle`."""

    def credentials(self):
        return object()

    def describe(self) -> dict:
        return {"available": True, "source": "test", "scopes": []}


def may_read_google(root: Path) -> AgentContext:
    """Local work plus reading Google: what `deck_sync(dry_run=True)` is meant to be reachable from."""
    return AgentContext(workspace=LocalWorkspace(root), google=FakeGoogle(),
                        allow=frozenset({READS, WRITES, READS_GOOGLE}))


# ---------------------------------------------------------------- deck_inspect


def test_deck_inspect_classifies_a_pdf_and_writes_the_ir(inspected, workspace):
    import json

    assert inspected.ok, inspected.summary
    deck = json.loads((workspace / "out" / "talk" / "deck.json").read_text(encoding="utf-8"))
    assert inspected.data["slides"] == len(deck["slides"]) > 0
    assert (workspace / "out" / "talk" / "raw.json").exists()
    refs = {a.ref for a in inspected.artifacts}
    assert {"out/talk/raw.json", "out/talk/deck.json", "out/talk/debug"} <= refs
    for artifact in inspected.artifacts:
        assert (workspace / artifact.ref).exists(), artifact.ref


def test_deck_inspect_reports_what_the_agent_has_to_decide_on(inspected):
    data = inspected.data
    assert data["elements"], "no element kinds counted"
    assert len(data["slides_detail"]) == data["slides"]
    assert len(data["titles"]) == data["slides"]
    assert data["fonts"], "no fonts seen in the PDF"
    assert "unlabelled" in data["labels"] and "duplicates" in data["labels"]
    assert data["overlays"]["mode"] == "last"
    assert 2 <= len(inspected.summary.split()) < 200
    # Unlabelled frames are the thing that decides whether a later sync can work: they are in
    # `data` and said out loud, never only one of the two.
    if data["labels"]["unlabelled"]:
        assert any(d.level == "warning" for d in inspected.diagnostics)
        assert any("tex_label" in s for s in inspected.next_steps)


def test_deck_inspect_with_checks_finds_no_invariant_problems_on_a_clean_deck(workspace):
    result = deck_inspect(AgentContext.offline(workspace), pdf="talk.pdf", checks=True)
    assert result.ok, result.summary
    assert result.data["checks"]["ran"] is True
    assert result.data["checks"]["findings"] == 0, result.data["checks"]["by_check"]
    assert "deck_convert" in " ".join(result.next_steps)


def test_deck_inspect_refuses_a_path_outside_the_workspace(tmp_path):
    result = deck_inspect(AgentContext.offline(tmp_path / "ws"), pdf=str(deck_pdf()))
    assert not result.ok
    assert result.code == "outside_workspace", result.summary


def test_deck_inspect_refuses_something_that_is_not_a_built_pdf(workspace):
    ctx = AgentContext.offline(workspace)
    assert deck_inspect(ctx, pdf="nowhere.pdf").code == "not_found"
    (Path(workspace) / "main.tex").write_text("hello", encoding="utf-8")
    assert deck_inspect(ctx, pdf="main.tex").code == "bad_request"


# ---------------------------------------------------------------- the Google boundary


@pytest.mark.parametrize("fn", GOOGLE_TOOLS, ids=lambda f: f.tool_name)
def test_a_google_journey_does_nothing_at_all_in_an_offline_context(fn, tmp_path):
    """`AgentContext.offline` both forbids and has no credentials, and the tool must come back
    without having made a file, a request or an exception - whichever of the two says no first."""
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    ctx = AgentContext.offline(root)
    before = sorted(p.name for p in root.rglob("*"))
    result = fn(ctx, pdf="talk.pdf", **({"deck": "some-deck-id"} if fn is deck_sync else {}))
    assert not result.ok
    # The gate runs before credentials are fetched, so a context that is offline *and* forbids
    # writing says `forbidden`; one that merely has no Google says `offline`.
    assert result.code in ("forbidden", "offline"), result.summary
    assert sorted(p.name for p in root.rglob("*")) == before, "an offline journey touched the disk"


@pytest.mark.parametrize("fn", GOOGLE_TOOLS, ids=lambda f: f.tool_name)
def test_a_google_journey_with_no_credentials_refuses_with_offline(fn, tmp_path):
    from beamer2slides.agent import ALL_ACTIONS, NoGoogle

    root = tmp_path / "ws"
    root.mkdir(parents=True)
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    ctx = AgentContext(workspace=LocalWorkspace(root), google=NoGoogle(), allow=ALL_ACTIONS)
    result = fn(ctx, pdf="talk.pdf", **({"deck": "some-deck-id"} if fn is deck_sync else {}))
    assert not result.ok and result.code == "offline", result.summary


def test_a_dry_run_sync_is_reachable_without_permission_to_write_google(tmp_path):
    """The whole point of `needs=(READS, WRITES, READS_GOOGLE)`: an agent that may look at a deck
    but not change it can still plan the merge and show the person what it would do."""
    from beamer2slides.agent import ALL_ACTIONS, NoGoogle

    root = tmp_path / "ws"
    root.mkdir(parents=True)
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    ctx = AgentContext(workspace=LocalWorkspace(root), google=NoGoogle(),
                       allow=frozenset({READS, WRITES, READS_GOOGLE}))
    result = deck_sync(ctx, pdf="talk.pdf", deck="some-deck-id", dry_run=True)
    assert not result.ok
    # It got past the gate: what stops it is this machine's credentials, not the policy.
    assert result.code != "forbidden"
    assert result.code in ("offline", "no_credentials", "needs_consent", "no_base"), result.summary


def test_a_real_sync_from_that_same_context_is_forbidden_before_anything_happens(tmp_path):
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    before = sorted(p.name for p in root.rglob("*"))
    result = deck_sync(may_read_google(root), pdf="talk.pdf", deck="some-deck-id", dry_run=False)
    assert not result.ok and result.code == "forbidden", result.summary
    assert "writes_google" in result.summary
    assert sorted(p.name for p in root.rglob("*")) == before, "a forbidden sync made a folder"


def test_take_source_is_published_as_a_list_of_ids_a_person_chose(tmp_path):
    """`--take-source` writes over what somebody wrote in the deck, so the tool has to be able to
    carry several ids at once (a person reads one report and answers it once) and its description
    has to say whose decision it is - INSTRUCTIONS.md says the same thing at greater length."""
    from beamer2slides.agent.schema import all_schemas

    schema = next(s for s in all_schemas() if s["name"] == "deck_sync")
    prop = schema["input_schema"]["properties"]["take_source"]
    assert "array" in prop["type"] and prop["items"]["type"] == "string"
    assert "take_source" not in schema["input_schema"].get("required", [])
    assert "never pick an id yourself" in prop["description"]
    # and it reaches the journey: a context that may not write refuses before the ids matter
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    result = deck_sync(may_read_google(root), pdf="talk.pdf", deck="d", take_source=["abc12345"])
    assert not result.ok and result.code == "forbidden", result.summary


def test_a_bad_backup_mode_is_a_bad_request_not_a_crash(tmp_path):
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    shutil.copy2(deck_pdf(), root / "talk.pdf")
    result = deck_sync(may_read_google(root), pdf="talk.pdf", deck="d", dry_run=True, backup="maybe")
    assert result.code == "bad_request" and "maybe" in result.summary
    assert result.data["allowed"] == ["auto", "none", "file", "drive", "both"]


def test_a_refused_rebuild_comes_back_as_a_code_with_the_edits_named(tmp_path):
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
    survey = {"presentationId": "PID123", "revisionId": "r7", "reason": "edited", "edited": True,
              "examples": ["slide 3: the text of `b2s_s003_t0` was changed",
                           "slide 8 was added in Slides"],
              "counts": {"text": 1}, "slides_added": 1, "slides_deleted": 0, "reordered": False}
    with pytest.raises(Refused) as caught:
        _refuse_rebuild(job, RebuildRefused("refusing to rebuild: ...", survey), root / "talk.pdf",
                        root / "out" / "talk")
    assert caught.value.code == "deck_edited"
    assert caught.value.data["examples"] == survey["examples"]
    assert caught.value.data["url"].endswith("PID123/edit")
    assert caught.value.data["revisionId"] == "r7"
    steps = " ".join(job.next_steps)
    assert "deck_sync" in steps and "new_deck=True" in steps and "force_rebuild=True" in steps


def test_a_rebuild_with_no_way_back_is_its_own_refusal(tmp_path):
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


def test_a_refusal_never_hands_the_agent_a_shell_command(tmp_path):
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
    survey = {"presentationId": "PID123", "revisionId": "r7", "reason": "edited", "edited": True,
              "examples": ["slide 3: the text of `b2s_s003_t0` was changed"], "slides": ["b2s_s003"],
              "counts": {"text": 1}, "slides_added": 0, "slides_deleted": 0, "reordered": False}
    # The real prose, not a stand-in: this test is about what the library actually raises.
    cli = refusal_message("PID123", out, root / "talk.pdf", survey, "edited")
    assert "--force-rebuild" in cli, "the message this test guards against has changed"

    for reason, code in (("edited", "deck_edited"), ("backup-failed", "no_way_back")):
        job = Job("deck_convert", AgentContext.offline(root))
        with pytest.raises(Refused) as caught:
            _refuse_rebuild(job, RebuildRefused(cli, dict(survey, reason=reason)),
                            root / "talk.pdf", out)
        assert caught.value.code == code
        said = str(caught.value)                    # what becomes `Result.summary`
        for shell in ("python -m beamer2slides", "--force-rebuild", "--new-deck", "--backup"):
            assert shell not in said, f"{code} hands the agent `{shell}`"
        assert "Nothing was written." in said
    # Forcing is still reachable - as an argument to this tool, and named as somebody's decision.
    assert any("force_rebuild=True" in s for s in job.next_steps)


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
def source(tmp_path) -> Path:
    root = tmp_path / "talk"
    root.mkdir()
    (root / "main.tex").write_text(FRAMES, encoding="utf-8")
    return root


def test_tex_label_plans_without_writing(source):
    result = tex_label(AgentContext.offline(source), tex="main.tex")
    assert result.ok, result.summary
    assert result.data["frames"] == 3 and result.data["labelled"] == 1
    assert result.data["planned"] == 2
    assert [e["label"] for e in result.data["edits"]] and all(e["title"] for e in result.data["edits"])
    assert result.data["applied"] is False
    assert (source / "main.tex").read_text(encoding="utf-8") == FRAMES, "apply=False wrote"
    assert not list(source.glob("*.bak"))
    assert any("apply=True" in s for s in result.next_steps)


def test_tex_label_applies_and_keeps_what_was_there(source):
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


def test_tex_label_reports_a_label_written_twice_as_a_conflict(source):
    """A duplicate reaches the PDF once (hyperref keeps the first destination), so the second
    frame looks unlabelled from there on. The .tex is the only place it can be seen."""
    (source / "main.tex").write_text(FRAMES.replace("[fragile]{Results", "[label=summary]{Results"),
                                     encoding="utf-8")
    result = tex_label(AgentContext.offline(source), tex="main.tex")
    assert result.ok, result.summary
    assert [d["label"] for d in result.data["duplicates"]] == ["summary"]
    assert any(d.level == "conflict" and "summary" in d.message for d in result.diagnostics)


def test_tex_label_refuses_a_file_that_is_not_a_beamer_document(source):
    (source / "notes.tex").write_text("\\documentclass{article}\nnothing here\n", encoding="utf-8")
    ctx = AgentContext.offline(source)
    assert tex_label(ctx, tex="notes.tex").code == "bad_request"
    assert tex_label(ctx, tex="missing.tex").code == "not_found"


# ---------------------------------------------------------------- the tools as an interface


@pytest.mark.parametrize("fn", TOOLS, ids=lambda f: f.tool_name)
def test_every_parameter_is_annotated_with_a_description(fn):
    """`schema.py` turns these annotations into JSON Schema, so an undescribed parameter is an
    undocumented one - the model would have to guess what it means from its name."""
    hints = get_type_hints(fn.body, include_extras=True)
    names = [n for n in fn.body.__code__.co_varnames[:fn.body.__code__.co_argcount] if n != "j"]
    assert names, f"{fn.tool_name} takes no arguments"
    for name in names:
        hint = hints[name]
        assert get_origin(hint) is Annotated, f"{fn.tool_name}({name}) is not Annotated"
        description = get_args(hint)[1]
        assert isinstance(description, str) and len(description.split()) >= 4, \
            f"{fn.tool_name}({name}): {description!r} is not a description"


@pytest.mark.parametrize("fn", TOOLS, ids=lambda f: f.tool_name)
def test_the_annotations_resolve_through_the_wrapper_too(fn):
    """`functools.wraps` copies `__annotations__` but not `__globals__`, so a module using
    `from __future__ import annotations` would hand the schema generator unresolvable strings."""
    for target in (fn, fn.body):
        hints = get_type_hints(target, include_extras=True)
        assert all(get_origin(h) is Annotated for n, h in hints.items() if n not in ("j", "return"))


@pytest.mark.parametrize("fn", TOOLS, ids=lambda f: f.tool_name)
def test_every_tool_says_what_it_does_and_what_it_costs(fn):
    assert fn.tool_name and fn.needs
    assert READS in fn.needs
    doc = (fn.body.__doc__ or "").strip().splitlines()
    assert 3 <= len([line for line in doc if line.strip()]) <= 8, f"{fn.tool_name}: {len(doc)} lines"
    assert any(w in " ".join(doc).lower() for w in ("cost", "second", "s ", "google")), \
        f"{fn.tool_name}'s docstring says nothing about what it costs"
