"""The Google Docs tasks of the agent benchmark, and the fixture they run against.

Offline, and not by pretending: `devtools.doc_world` is a Google Doc that lives in memory
and consumes the real `doc_merge.plan` requests under Docs' own index rules, so these five
tasks run the actual `doc_sync` journeys - plan, write, settle, regenerate the file, store
the base - and are graded on the document afterwards. No Google call, no quota, no network.

What is asserted here is what `tests/test_agent_bench.py` asserts of every other task, plus
the two things that are peculiar to these: that the fixture really is the library running
(a sync that changes nothing would make every grader vacuous), and that the process-wide
patches it installs - the two service factories and the `Job` that lends an account to a
context which has none - are off again afterwards. A benchmark fixture that leaks into the
next test is worse than no fixture.
"""

import pytest

from beamer2slides import doc_sync as docs
from beamer2slides.agent import ALL_ACTIONS, AgentContext, LOCAL_ONLY, LocalWorkspace
from beamer2slides.agent import context as agent_context
from beamer2slides.devtools import agent_bench as bench
from beamer2slides.devtools import agent_tasks as tasks

from .test_agent_bench import readable

DOCS_TASKS = [t for t in tasks.TASKS if t.id.startswith("docs-")]

#: The wrong policy of each task that destroys work rather than merely doing it badly.
HARMFUL = {"docs-open-comment-live": ("writes-through", "never-looks"),
           "docs-no-base-live": ("guesses",)}


@pytest.fixture(autouse=True)
def _no_fixture_left_installed():
    """Whatever a task does, the module's patches are off when the test ends.

    A grader takes them off itself, but a policy that raises never reaches its grader, and
    the next test would then be talking to the previous test's document.
    """
    was = (docs.docs_service, docs.drive_service, agent_context.Job)
    yield
    if tasks._INSTALLED is not None:
        tasks._INSTALLED.restore()
    docs.docs_service, docs.drive_service, agent_context.Job = was


# ------------------------------------------------------------------------------- the tasks

def test_there_are_five_of_them_and_they_run_in_the_default_tier():
    assert len(DOCS_TASKS) == 5
    for task in DOCS_TASKS:
        # `live` because the journeys really run; `offline` because they cost nothing, which
        # is what a tier says. Anything else would keep them out of the default run.
        assert (task.kind, task.tier) == ("live", "offline")
        assert task.needs_tools and task.setup and task.correct and task.wrong


@pytest.mark.parametrize("task", DOCS_TASKS, ids=lambda t: t.id)
def test_the_correct_policy_passes(task):
    run = bench.run_task(task, task.correct)
    assert run.status == "passed", f"{task.id}: {run.reason or run.failures}"
    assert run.harm == 0


@pytest.mark.parametrize("task,name", [(t, n) for t in DOCS_TASKS for n in t.wrong],
                         ids=lambda v: v if isinstance(v, str) else v.id)
def test_every_wrong_policy_fails_and_says_why(task, name):
    run = bench.run_task(task, task.wrong[name])
    assert run.status == "failed", f"{task.id}/{name} passed; the grader does not discriminate"
    readable(task, name, run)


@pytest.mark.parametrize("task_id,name", sorted((i, n) for i, names in HARMFUL.items()
                                                for n in names))
def test_the_policies_that_destroy_work_are_counted_as_harm(task_id, name):
    run = bench.run_task(tasks.BY_ID[task_id], tasks.BY_ID[task_id].wrong[name])
    assert run.harm >= 1, f"{task_id}/{name} destroyed somebody's work and was not counted"


@pytest.mark.parametrize("task", DOCS_TASKS, ids=lambda t: t.id)
def test_no_task_reaches_google(task, monkeypatch):
    """The seams are the only way out, and both are faked: nothing builds a real client."""
    def boom(*a, **kw):
        raise AssertionError("a Docs task tried to reach Google")

    monkeypatch.setattr("beamer2slides.google_auth.credentials", boom, raising=False)
    run = bench.run_task(task, task.correct)
    assert run.status == "passed"
    assert not any(c.writes_google() and c.tool == "doc_push" for c in run.calls)


# --------------------------------------------------------------- the fixture is the library

def test_the_core_task_really_merges_and_really_settles():
    """Both sides' words in the document, and a second sync with nothing to write.

    The graders say this in prose; said in numbers here, it is also a check on the fixture:
    a world that quietly refused every request would leave every grader vacuous.
    """
    task = tasks.BY_ID["docs-both-sides-moved"]
    run = bench.run_task(task, task.correct)
    first, again = [s.result for s in run.steps]
    assert first.ok and first.data["written"] and first.data["requests"] > 0
    assert again.data["requests"] == 0 and not again.data["written"]


def test_the_frozen_equation_survives_a_source_edit_to_it():
    """An equation is content no request can create: the block is left alone and reported."""
    task = tasks.BY_ID["docs-frozen-equation"]
    run = bench.run_task(task, task.correct)
    result = run.result_of("doc_sync")
    assert any("left alone" in d.message for d in result.diagnostics)
    assert result.data["applied"] == 1              # the other paragraph, which had nothing frozen


def test_guessing_a_side_with_no_base_really_destroys_the_reader_s_work():
    """The harm the task counts is the harm the document takes, not a grader's opinion."""
    task = tasks.BY_ID["docs-no-base-live"]
    honest = bench.run_task(task, task.correct)
    assert honest.facts["reader_kept"] in honest.facts["fixture"].said()
    guessed = bench.run_task(task, task.wrong["guesses"])
    assert guessed.facts["reader_kept"] not in guessed.facts["fixture"].said()


def test_adopt_writes_the_file_a_document_nobody_pushed_never_had():
    task = tasks.BY_ID["docs-adopt-then-settle"]
    run = bench.run_task(task, task.correct)
    adopted = run.result_of("doc_adopt")
    assert adopted.ok and adopted.data["anchored"] == adopted.data["blocks"] == 4
    assert [s.result.data["requests"] for s in run.steps[1:]] == [0]


# ------------------------------------------------------------------------------- the seams

def test_a_context_with_an_account_of_its_own_keeps_its_permissions(tmp_path):
    """The fixture lends an account; it never lends permission.

    `run_task` builds an `AgentContext.offline`, which has no account at all, and a Docs
    journey in it would be refused before its body ran - so the account is lent where the
    context first becomes reachable. A harness that really means read-only has an account
    and withholds the actions, and that must still be refused.
    """
    fixture = tasks._docs_fixture(LocalWorkspace(tmp_path), [{"blocks": tasks.CORE_BLOCKS}])
    try:
        ctx = AgentContext(workspace=fixture.ws, google=tasks._Account(), allow=LOCAL_ONLY)
        sync = bench.local_tools().get("doc_sync")
        if sync is None:
            pytest.skip("the registry has no doc_sync yet")
        was = fixture.world.revision
        refused = sync(ctx, file="doc.html")
        assert refused.code == "forbidden" and not refused.ok
        assert fixture.world.revision == was             # and it refused before doing anything
    finally:
        fixture.restore()


def test_the_lent_account_is_what_makes_a_docs_journey_run_at_all(tmp_path):
    """Without the lending Job, the benchmark's own context answers `offline`."""
    fixture = tasks._docs_fixture(LocalWorkspace(tmp_path), [{"blocks": tasks.CORE_BLOCKS}])
    try:
        sync = bench.local_tools().get("doc_sync")
        if sync is None:
            pytest.skip("the registry has no doc_sync yet")
        ctx = AgentContext.offline(tmp_path)
        assert sync(ctx, file="doc.html", dry_run=True).ok
        assert ctx.allow == ALL_ACTIONS                      # lent, and only because it had none
    finally:
        fixture.restore()


def test_installing_a_second_fixture_takes_the_first_one_off(tmp_path):
    """Which is what saves a run whose policy raised before its grader could restore."""
    was = (docs.docs_service, docs.drive_service, agent_context.Job)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = tasks._docs_fixture(LocalWorkspace(tmp_path / "a"), [{"blocks": tasks.CORE_BLOCKS}])
    second = tasks._docs_fixture(LocalWorkspace(tmp_path / "b"), [{"blocks": tasks.CORE_BLOCKS}])
    assert tasks._INSTALLED is second and first._was is None
    second.restore()
    assert tasks._INSTALLED is None
    assert (docs.docs_service, docs.drive_service, agent_context.Job) == was


@pytest.mark.parametrize("task", DOCS_TASKS, ids=lambda t: t.id)
def test_a_run_leaves_no_patch_behind(task):
    was = (docs.docs_service, docs.drive_service, agent_context.Job)
    bench.run_task(task, task.correct)
    assert (docs.docs_service, docs.drive_service, agent_context.Job) == was
    assert tasks._INSTALLED is None
