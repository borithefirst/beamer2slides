"""The agent benchmark, checked the way it asks its own tasks to be checked.

Two things are worth testing about a benchmark, and neither is "does it run". The first is that it
**discriminates**: every task ships a correct policy that passes and at least one wrong policy that
fails, and both are asserted here - a grader nobody has seen fail is not a grader. The second is
that **harm is counted apart from the pass rate**, because a benchmark that averages "forced the
rebuild" into a percentage hides the only failure that matters.

Everything here is offline and fast: the replay tier calls nothing, and the one live task in the
default run (`label-the-source`) writes a .tex fixture into a temporary workspace. The live task
that needs the built test decks is marked `inverse`, which the default run deselects.
"""

import json

import pytest

from beamer2slides.devtools import agent_bench as bench
from beamer2slides.devtools import agent_tasks as tasks

REPLAY = [t for t in tasks.TASKS if t.kind == "replay"]
DEFAULT = [t for t in tasks.TASKS if t.tier == "offline"]
SLOW = [t for t in tasks.TASKS if t.tier != "offline"]


# ------------------------------------------------------------------------------------- the task set

def test_tasks_are_uniquely_named_and_say_what_they_discriminate():
    ids = [t.id for t in tasks.TASKS]
    assert len(ids) == len(set(ids)), "task ids have to be unique: they name result folders"
    assert len(REPLAY) >= 10, "the replay tier carries the benchmark's weight"
    for t in tasks.TASKS:
        assert t.kind in ("replay", "live")
        assert t.tier in bench.TIERS
        assert t.prompt.strip() and t.title.strip()
        assert len(t.note) > 20, f"{t.id}: the table has to say what the task discriminates"
        assert callable(t.grade)
        if t.kind == "replay":
            assert t.script, f"{t.id}: a replay task is its script"
        else:
            assert t.needs_tools, f"{t.id}: a live task has to say which tools it needs"


def test_every_task_ships_a_correct_policy_and_a_wrong_one():
    for t in tasks.TASKS:
        assert t.correct is not None, f"{t.id}: no correct policy, so the task is unproven"
        assert t.wrong, f"{t.id}: no wrong policy, so the grader has never been seen to fail"


@pytest.mark.parametrize("task", DEFAULT, ids=lambda t: t.id)
def test_the_correct_policy_passes(task):
    run = bench.run_task(task, task.correct)
    assert run.status == "passed", f"{task.id}: {run.reason or run.failures}"
    assert run.harm == 0


@pytest.mark.parametrize("task,name", [(t, n) for t in DEFAULT for n in t.wrong],
                         ids=lambda v: v if isinstance(v, str) else v.id)
def test_every_wrong_policy_fails(task, name):
    run = bench.run_task(task, task.wrong[name])
    assert run.status == "failed", f"{task.id}/{name} passed; the grader does not discriminate"
    assert run.failures


def readable(task, name, run) -> None:
    """Every failure is one sentence a person can check, whatever tier it came from."""
    for f in run.failures:
        body = f[len(bench.HARM_PREFIX):] if f.startswith(bench.HARM_PREFIX) else f
        assert isinstance(f, str) and len(body) > 40, f"{task.id}/{name}: {f!r} says too little"
        assert body.endswith(".") or body.endswith(")"), f"{task.id}/{name}: {f!r}"


@pytest.mark.parametrize("task", DEFAULT, ids=lambda t: t.id)
def test_failures_are_written_for_a_person_to_read(task):
    for name, policy in task.wrong.items():
        readable(task, name, bench.run_task(task, policy))


# ------------------------------------------------------------------------------------------- harm

HARMFUL = {"dry-run-first": "writes-first",
           "respect-the-refusal": "forces",
           "no-base": "forces",
           "read-the-conflict": "writes-anyway",
           "open-comment": "never-looks",
           "assume-base": "guesses",
           "new-deck-not-rebuild": "rebuilds",
           "forced-rebuild-with-a-way-back": "no-way-back"}


@pytest.mark.parametrize("task_id,name", sorted(HARMFUL.items()))
def test_the_worst_policies_are_counted_as_harm(task_id, name):
    task = tasks.BY_ID[task_id]
    run = bench.run_task(task, task.wrong[name])
    assert run.harm >= 1, f"{task_id}/{name} destroys work and was not counted as harm"
    assert any(f.startswith(bench.HARM_PREFIX) for f in run.failures)


def test_harm_is_reported_apart_from_the_pass_rate(tmp_path):
    chosen = [tasks.BY_ID[i] for i in HARMFUL]
    summary = bench.run(chosen, policy={i: tasks.BY_ID[i].wrong[n] for i, n in HARMFUL.items()},
                        tag="test-harm", out=tmp_path, quiet=True)
    t = summary["totals"]
    assert t["passed"] == 0 and t["pass_rate"] == 0.0
    assert t["harm"] == len(HARMFUL)
    assert sorted(t["harmed_tasks"]) == sorted(HARMFUL)
    assert "HARM 8" in bench.table(summary)                        # its own line, not folded into a score


def test_a_clean_run_says_so_without_a_harm_line_of_numbers(tmp_path):
    summary = bench.run(DEFAULT, tag="test-clean", out=tmp_path, quiet=True)
    assert summary["totals"]["harm"] == 0
    assert summary["totals"]["passed"] == summary["totals"]["ran"] == len(DEFAULT)
    assert "no task failed in a way that would have destroyed work" in bench.table(summary)


# ------------------------------------------------------------------------------------- the runner

def test_the_summary_has_the_shape_a_history_document_can_quote(tmp_path):
    summary = bench.run(REPLAY[:3], tag="test-shape", out=tmp_path, quiet=True)
    assert set(summary) == {"tag", "when", "totals", "tasks"}
    for key in ("tasks", "ran", "passed", "failed", "skipped", "errors", "harm", "harmed_tasks",
                "calls", "redundant", "google_writes", "pass_rate", "seconds"):
        assert key in summary["totals"]
    row = summary["tasks"][0]
    for key in ("id", "title", "kind", "tier", "status", "failures", "calls", "redundant",
                "google_writes", "harm", "seconds"):
        assert key in row
    written = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert [r["task"] for r in written["runs"]] == [t.id for t in REPLAY[:3]]
    assert (tmp_path / "results.txt").exists()


def test_a_dry_run_is_not_counted_as_a_write():
    assert bench.call("deck_sync", dry_run=True).writes_google() is False
    assert bench.call("deck_sync").writes_google() is True
    assert bench.call("deck_inspect", pdf="talk.pdf").writes_google() is False
    assert bench.call("doc_push", file="notes.html").writes_google() is True


def test_repeating_a_call_is_counted_as_redundant():
    task = tasks.BY_ID["consent-expired"]
    twice = bench.Scripted(bench.call("deck_sync", deck=tasks.DECK),
                           bench.call("deck_sync", deck=tasks.DECK),
                           answer="x")
    run = bench.run_task(task, twice)
    assert run.counts()["calls"] == 2 and run.counts()["redundant"] == 1


def test_calling_a_tool_that_is_not_there_is_a_bad_request():
    task = tasks.BY_ID["read-the-conflict"]
    run = bench.run_task(task, bench.Scripted(bench.call("deck_publish"), answer="done"))
    assert run.results[0].ok is False and run.results[0].code == "bad_request"
    assert run.status == "failed"


def test_a_policy_that_never_answers_is_stopped_and_failed():
    task = tasks.BY_ID["consent-expired"]

    class Loop:
        def __call__(self, prompt, tools, history):
            return [bench.call("deck_sync", deck=tasks.DECK, attempt=len(history))]

    run = bench.run_task(task, Loop(), max_steps=5)
    assert run.truncated and run.status == "failed"
    assert len(run.steps) == 5
    assert any("never answered" in f for f in run.failures)


def test_a_broken_grader_is_an_error_not_a_pass():
    def explode(run):
        raise ValueError("this grader is broken")

    task = tasks.Task(id="broken", title="t", kind="replay", tier="offline", prompt="p",
                      script={}, grade=explode, note="x" * 30)
    run = bench.run_task(task, bench.Scripted(answer="hi"))
    assert run.status == "error" and "this grader is broken" in run.reason


def test_a_live_task_skips_when_the_registry_has_not_got_its_tool():
    task = tasks.BY_ID["label-the-source"]
    run = bench.run_task(task, task.correct, tools={})
    assert run.status == "skipped" and "tex_label" in run.reason
    assert run.harm == 0


def test_the_registry_is_imported_lazily_and_degrades_clearly():
    # The benchmark has to be usable before beamer2slides.agent.tools exists.
    table = bench.local_tools()
    for name in ("b2s_status", "deck_inspect", "tex_label"):
        assert name in table
    assert isinstance(bench.instructions(), str)


# ------------------------------------------------------------------------------- recorded transcripts

def test_a_recorded_transcript_replays_and_scores_the_same(tmp_path):
    task = tasks.BY_ID["dry-run-first"]
    first = bench.run_task(task, task.correct)
    path = tmp_path / f"{task.id}.json"
    path.write_text(json.dumps(first.json()), encoding="utf-8")
    again = bench.run_task(task, bench.Recorded(path))
    assert [c.json() for c in again.calls] == [c.json() for c in first.calls]
    assert again.answer == first.answer
    assert again.status == "passed" == first.status


def test_a_recorded_transcript_from_an_outside_harness_needs_only_tool_and_arguments(tmp_path):
    task = tasks.BY_ID["read-the-conflict"]
    record = {"steps": [{"tool": "deck_sync", "arguments": {"deck": tasks.DECK, "dry_run": True}}],
              "answer": "Nothing was written. Slide 4's heading and slide 9's bolded phrase both "
                        "conflict; say the word and I will sync for real."}
    (tmp_path / f"{task.id}.json").write_text(json.dumps(record), encoding="utf-8")
    loaded = bench.recorded_dir(tmp_path)
    assert set(loaded) == {task.id}
    run = bench.run_task(task, loaded[task.id])
    assert run.status == "passed"


def test_a_bundle_carries_what_an_outside_harness_needs():
    b = bench.bundle(tasks.BY_ID["assume-base"])
    assert b["prompt"] == tasks.BY_ID["assume-base"].prompt
    assert "doc_sync" in b["tools"]
    assert isinstance(b["instructions"], str)
    assert b["transcript_shape"]["steps"][0]["tool"] == "<name>"


# ------------------------------------------------------------------------------------ the live tier

@pytest.mark.inverse
@pytest.mark.parametrize("task", SLOW, ids=lambda t: t.id)
def test_the_live_tier_runs_the_library(task):
    run = bench.run_task(task, task.correct)
    if run.status == "skipped":
        pytest.skip(run.reason)
    assert run.status == "passed", run.failures
    for name, policy in task.wrong.items():
        bad = bench.run_task(task, policy)
        assert bad.status == "failed", f"{task.id}/{name} passed against the real tools"
        readable(task, name, bad)
