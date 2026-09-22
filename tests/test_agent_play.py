"""The shell door onto the benchmark, checked where it could quietly lie.

`agent_play` exists to let a model that is running *now* play a task a call at a time and be
graded for it. Two properties make that worth anything, and both are asserted here rather than
assumed:

* **The verdict is the benchmark's.** A run played through this CLI and the same call sequence
  handed to `agent_bench` as a `Recorded` policy come back with the same status, the same failure
  sentences and the same harm count - for every replay task and every policy those tasks ship.
  If they ever diverge, a score from this door stops meaning what `docs/agent-bench.md` says it
  means.
* **A model cannot reach past the task.** A tool the task did not offer runs nothing, a live
  task's Google journeys refuse before their bodies run, and a `live_google` task cannot be
  started without saying so out loud.

The rest is what a confused or dishonest model does: scoring early, answering twice, calling
after the answer, sending arguments no shell could have meant, starting a second task in a folder
already playing one. Each has to be a sentence saying what to do instead - a crash is a
transcript nobody can score.

Offline and fast: the replay tier calls nothing, and the one live task writes a .tex fixture into
a temporary run dir.
"""

import ast
import inspect
import json
from pathlib import Path

import pytest

from beamer2slides.devtools import agent_bench as bench
from beamer2slides.devtools import agent_play as play
from beamer2slides.devtools import agent_tasks as tasks

REPLAY = [t for t in tasks.TASKS if t.kind == "replay"]
POLICIES = ([(t, "correct", t.correct) for t in REPLAY] +
            [(t, name, pol) for t in REPLAY for name, pol in t.wrong.items()])


def play_through(task, policy, run_dir: Path):
    """Drive the CLI's own API the way a model drives its commands: start, call*, answer, score."""
    play.start(task.id, run_dir)
    for one in policy.calls:
        play.call(run_dir, one.tool, one.arguments)
    play.answer(run_dir, policy.answer or "(nothing)")
    return play.score(run_dir)


# --------------------------------------------------------------- the verdict is the benchmark's

@pytest.mark.parametrize("task,name,policy", POLICIES,
                         ids=[f"{t.id}-{n}" for t, n, _ in POLICIES])
def test_a_played_run_scores_exactly_as_the_benchmark_scores_it(task, name, policy, tmp_path):
    expected = bench.run_task(task, policy)
    run, verdict = play_through(task, policy, tmp_path / f"{task.id}-{name}")
    assert run.status == expected.status
    assert run.failures == expected.failures, "the graders must be the benchmark's own, not a copy"
    assert run.harm == expected.harm == verdict["harm"]
    assert [c.json() for c in run.calls] == [c.json() for c in expected.calls]


def test_the_transcript_is_the_shape_recorded_already_reads(tmp_path):
    task = tasks.BY_ID["dry-run-first"]
    run_dir = tmp_path / "run"
    play_through(task, task.correct, run_dir)

    # `recorded_dir` globs the folder and keys by file stem, so a run dir *is* a transcript
    # folder: the file can be scored here, replayed with --policy recorded:DIR, or committed.
    loaded = bench.recorded_dir(run_dir)
    assert set(loaded) == {task.id}
    again = bench.run_task(task, loaded[task.id])
    assert again.status == "passed" and again.answer == task.correct.answer

    written = json.loads((run_dir / f"{task.id}.json").read_text(encoding="utf-8"))
    assert written["task"] == task.id and written["answer"]
    assert [s["tool"] for s in written["steps"]] == [c.tool for c in task.correct.calls]
    assert all({"tool", "arguments", "result"} <= set(s) for s in written["steps"])
    assert written["play"]["finished"] is True          # this CLI's own state stays out of the way


def test_scoring_twice_says_the_same_thing(tmp_path):
    task = tasks.BY_ID["read-the-conflict"]
    run_dir = tmp_path / "run"
    _, first = play_through(task, task.correct, run_dir)
    _, second = play.score(run_dir)
    # Everything but the clock: `seconds` is measured afresh by each scoring, and under a loaded
    # machine the two measurements of the same replay differ in the second decimal.
    assert {k: v for k, v in first.items() if k != "seconds"} == \
           {k: v for k, v in second.items() if k != "seconds"}
    assert (run_dir / "verdict.txt").read_text(encoding="utf-8").startswith(f"{task.id}: PASSED")


def test_a_harmful_run_is_counted_as_harm_and_exits_on_it(tmp_path):
    task = tasks.BY_ID["respect-the-refusal"]
    run, verdict = play_through(task, task.wrong["forces"], tmp_path / "run")
    assert run.harm == 1 and verdict["status"] == "failed"
    assert any(f.startswith(bench.HARM_PREFIX) for f in verdict["failures"])
    assert play.exit_code(verdict) == play.EXIT_HARM
    assert "HARM 1" in play.verdict_text(verdict)


def test_the_exit_codes_tell_a_harness_which_kind_of_outcome_it_was():
    assert play.exit_code({"status": "passed", "harm": 0}) == play.EXIT_OK
    assert play.exit_code({"status": "failed", "harm": 0}) == play.EXIT_FAILED
    assert play.exit_code({"status": "failed", "harm": 2}) == play.EXIT_HARM
    assert play.exit_code({"status": "skipped", "harm": 0}) == play.EXIT_UNGRADED
    assert play.exit_code({"status": "error", "harm": 0}) == play.EXIT_UNGRADED


def test_the_step_budget_is_the_one_the_benchmark_replays_under():
    # A transcript this CLI accepted but `run_task` truncates would fail for a reason the model
    # was never told, so the two numbers are one number.
    assert inspect.signature(bench.run_task).parameters["max_steps"].default == play.MAX_STEPS


# --------------------------------------------------------------------- a model cannot reach past

def test_a_tool_the_task_did_not_offer_runs_nothing(tmp_path):
    task = tasks.BY_ID["read-the-conflict"]                 # its script has deck_sync alone
    play.start(task.id, tmp_path / "run")
    result = play.call(tmp_path / "run", "deck_convert", {"pdf": "talk.pdf"})
    assert result.ok is False and result.code == "bad_request"
    assert result.json() == bench.missing_tool("deck_convert").json()


def test_a_live_task_has_no_google_at_all(tmp_path):
    task = tasks.BY_ID["label-the-source"]                  # its tools really run
    run_dir = tmp_path / "run"
    play.start(task.id, run_dir)
    session = play.load(run_dir)
    ctx = play._context(session)
    assert ctx.permits("writes_google") is False
    assert ctx.google.describe()["available"] is False
    refused = play.call(run_dir, "deck_convert", {"pdf": "talk.pdf"})
    assert refused.ok is False and refused.code == "offline"


def test_a_live_tasks_fixture_is_built_where_the_run_can_keep_it(tmp_path):
    run_dir = tmp_path / "run"
    session, _ = play.start("label-the-source", run_dir)
    assert (run_dir / "workspace" / "talk" / "main.tex").exists()
    assert session.play["facts"]["tex"] == "talk/main.tex"
    written = play.call(run_dir, "tex_label", {"tex": "talk/main.tex", "apply": True})
    assert written.ok and "label=introduction" in \
        (run_dir / "workspace" / "talk" / "main.tex").read_text(encoding="utf-8")


def test_a_google_writing_tier_cannot_be_started_without_saying_so(tmp_path, monkeypatch):
    spendthrift = tasks.Task(id="spends-a-deck", title="t", kind="replay", tier=play.GATED_TIER,
                             prompt="p", script={}, grade=lambda run: [], note="x" * 30)
    monkeypatch.setitem(tasks.BY_ID, spendthrift.id, spendthrift)
    with pytest.raises(play.PlayError) as refusal:
        play.start(spendthrift.id, tmp_path / "run")
    assert "--allow-google" in str(refusal.value)
    assert not list((tmp_path / "run").glob("*.json")), "a refused start leaves no session behind"
    session, _ = play.start(spendthrift.id, tmp_path / "run", allow_google=True)
    assert session.play["allow_google"] is True


def test_a_run_that_wrote_to_google_is_scored_without_being_run_again(tmp_path, monkeypatch):
    """The one run that cannot be graded by replaying it into the tools.

    Every other transcript is scored by running its calls again - that is what makes a run made
    in another harness gradeable here. A `live_google` run's calls went to somebody's real deck,
    so scoring it that way would either write a second time or, against a fresh workspace,
    build the whole fixture again. Its own answers are the registry, and its own fixture stands.
    """
    built = []

    def setup(ws):
        built.append(ws)
        path = Path(ws.root) / "talk" / "main.tex"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\\begin{frame}{Introduction}\\end{frame}", encoding="utf-8")
        return {"tex": "talk/main.tex", "deck": "a real deck id"}

    def grade(run):
        assert run.facts["deck"] == "a real deck id", "the fixture is the one the run was played on"
        return [] if run.result_of("tex_label") and run.said("labelled") else ["nothing happened"]

    spendthrift = tasks.Task(id="spends-a-deck-live", title="t", kind="live",
                             tier=play.GATED_TIER, prompt="p", grade=grade, setup=setup,
                             needs_tools=("tex_label", "deck_sync"), note="x" * 30)
    monkeypatch.setitem(tasks.BY_ID, spendthrift.id, spendthrift)
    run_dir = tmp_path / "run"
    play.start(spendthrift.id, run_dir, allow_google=True)
    assert len(built) == 1
    play.call(run_dir, "tex_label", {"tex": "talk/main.tex", "apply": True})
    play.answer(run_dir, "I labelled the frame that had none.")

    run, verdict = play.score(run_dir)
    assert verdict["status"] == "passed", run.failures
    assert len(built) == 1, "scoring must not build the fixture a second time"
    assert run.results[0].ok, "the result the run really got is what the grader sees"


def test_a_task_whose_fixture_dies_with_the_process_is_refused_rather_than_played(tmp_path):
    """One process per turn is the design, and it is also what these tasks' world does not survive.

    The Google Docs tasks hold their document in `devtools.doc_world` and lend the account by
    patching `agent.context.Job`, both of which `run_task` keeps for the length of a run and this
    CLI throws away at the end of `start`. Played here, every journey would answer `offline` and
    the transcript would look like a model that could not work its own tools.
    """
    for task in tasks.TASKS:
        if not getattr(task, "process_bound", False):
            continue
        with pytest.raises(play.PlayError) as refusal:
            play.start(task.id, tmp_path / task.id)
        said = str(refusal.value)
        assert "agent_bench run" in said, said              # where it *can* be scored
        assert refusal.value.code == play.EXIT_UNGRADED     # not a failure of the model's
        assert not list((tmp_path / task.id).glob("*.json"))
    assert any(getattr(t, "process_bound", False) for t in tasks.TASKS), "nothing was tested"


def test_no_model_is_called_from_this_repo():
    """The property the whole design is for: this module talks to no model and to no network."""
    source = Path(play.__file__).read_text(encoding="utf-8")
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    assert not roots & {"anthropic", "openai", "google", "requests", "httpx", "aiohttp",
                        "socket", "urllib", "http", "ssl"}, roots
    assert "No model is called from this repo" in (play.__doc__ or "")


# ------------------------------------------------------------- what a confused model does instead

def test_scoring_before_answering_is_refused_unless_asked_for(tmp_path):
    task = tasks.BY_ID["dry-run-first"]
    run_dir = tmp_path / "run"
    play.start(task.id, run_dir)
    play.call(run_dir, "b2s_status", {})
    with pytest.raises(play.PlayError) as refusal:
        play.score(run_dir)
    assert "answer" in str(refusal.value)
    run, verdict = play.score(run_dir, unfinished=True)     # graded as it stands, and it fails
    assert run.status == "failed" and verdict["answered"] is False
    assert "no answer was given" in play.verdict_text(verdict)


def test_a_run_ends_once(tmp_path):
    run_dir = tmp_path / "run"
    play.start("dry-run-first", run_dir)
    play.answer(run_dir, "Nothing was written; the deck is untouched.")
    with pytest.raises(play.PlayError, match="already been answered"):
        play.answer(run_dir, "actually, something else")
    with pytest.raises(play.PlayError, match="takes no more calls"):
        play.call(run_dir, "b2s_status", {})
    assert len(play.load(run_dir).steps) == 0


def test_an_empty_answer_is_not_an_answer(tmp_path):
    run_dir = tmp_path / "run"
    play.start("dry-run-first", run_dir)
    with pytest.raises(play.PlayError, match="not an answer"):
        play.answer(run_dir, "   ")
    assert play.load(run_dir).finished is False


def test_a_second_task_in_the_same_run_dir_is_refused_and_force_starts_again(tmp_path):
    run_dir = tmp_path / "run"
    play.start("dry-run-first", run_dir)
    play.call(run_dir, "b2s_status", {})
    with pytest.raises(play.PlayError, match="already playing dry-run-first"):
        play.start("no-base", run_dir)
    assert play.load(run_dir).task == "dry-run-first"
    play.start("no-base", run_dir, force=True)
    session = play.load(run_dir)
    assert session.task == "no-base" and session.steps == []
    assert sorted(p.name for p in run_dir.glob("*.json")) == ["no-base.json"]


def test_the_budget_stops_a_run_that_is_getting_nowhere(tmp_path):
    run_dir = tmp_path / "run"
    play.start("consent-expired", run_dir)
    for _ in range(play.MAX_STEPS):
        play.call(run_dir, "deck_sync", {"pdf": "talk.pdf", "deck": tasks.DECK})
    with pytest.raises(play.PlayError, match="budget"):
        play.call(run_dir, "deck_sync", {"pdf": "talk.pdf", "deck": tasks.DECK})


def test_a_run_dir_holding_two_transcripts_is_named_rather_than_guessed_at(tmp_path):
    run_dir = tmp_path / "run"
    play.start("dry-run-first", run_dir)
    (run_dir / "smuggled.json").write_text("{}", encoding="utf-8")
    with pytest.raises(play.PlayError, match="holds 2 transcripts"):
        play.load(run_dir)


def test_an_unknown_task_and_an_unstarted_folder_say_what_to_do(tmp_path):
    with pytest.raises(play.PlayError, match="There is no task called"):
        play.task_by_id("convert-everything")
    with pytest.raises(play.PlayError, match="Start a task first"):
        play.load(tmp_path / "nothing-here")


# ------------------------------------------------------------------------------ the arguments

def test_arguments_are_read_as_json_where_they_can_be_and_as_strings_where_they_cannot():
    got = play.parse_arguments(["pdf=talk.pdf", "dry_run=true", "deck=" + tasks.DECK, "n=3"],
                               None, None)
    assert got == {"pdf": "talk.pdf", "dry_run": True, "deck": tasks.DECK, "n": 3}
    assert play.parse_arguments([], '{"pdf": "talk.pdf", "dry_run": true}', None) == \
        {"pdf": "talk.pdf", "dry_run": True}
    assert play.parse_arguments([], None, None) == {}


def test_arguments_no_shell_could_have_meant_are_refused_by_name(tmp_path):
    with pytest.raises(play.PlayError, match="not JSON"):
        play.parse_arguments([], "{pdf: talk.pdf}", None)
    with pytest.raises(play.PlayError, match="has to be a JSON object"):
        play.parse_arguments([], '["talk.pdf"]', None)
    with pytest.raises(play.PlayError, match="name=value"):
        play.parse_arguments(["talk.pdf"], None, None)
    with pytest.raises(play.PlayError, match="Give the arguments once"):
        play.parse_arguments(["pdf=a"], '{"pdf": "b"}', None)
    with pytest.raises(play.PlayError, match="could not be read"):
        play.parse_arguments([], None, str(tmp_path / "gone.json"))


def test_an_argument_a_scripted_tool_cannot_take_is_a_bad_request_not_a_crash(tmp_path):
    # The fakes have no `@tool` wrapper to catch this; a real tool's own wrapper does.
    run_dir = tmp_path / "run"
    play.start("read-the-conflict", run_dir)
    result = play.call(run_dir, "deck_sync", {"ctx": "not a chance"})
    assert result.ok is False and result.code == "bad_request"


# -------------------------------------------------------------- the replayed script keeps counting

def test_a_canned_answer_that_counts_its_calls_still_counts_across_processes(tmp_path):
    # `second-sync-is-quiet` answers the *second* doc_sync with 0 requests, and that counter died
    # with the process that made the first call. Replaying the transcript is what rebuilds it.
    run_dir = tmp_path / "run"
    play.start("second-sync-is-quiet", run_dir)
    first = play.call(run_dir, "doc_sync", {"file": "notes.html"})
    second = play.call(run_dir, "doc_sync", {"file": "notes.html", "dry_run": True})
    assert first.data["requests"] == 24 and second.data["requests"] == 0


# ---------------------------------------------------------------------------- what `start` prints

def test_the_briefing_carries_everything_an_operator_pastes_into_a_harness(tmp_path):
    task = tasks.BY_ID["assume-base"]
    session, started = play.start(task.id, tmp_path / "run")
    text = play.briefing(started, session)
    assert task.prompt in " ".join(text.split())            # verbatim, modulo the wrapping
    assert "Never rebuild a deck somebody has edited" in text
    assert "base_choice_needed" in text                      # the refusal vocabulary, in full
    for code in play.CODES:
        assert code in text
    assert "The one rule" in text                            # INSTRUCTIONS.md itself, not a summary
    assert str(tmp_path / "run") in text                     # every command already filled in
    for name in play.offered(started):
        assert name in text


def test_the_briefing_publishes_the_schema_the_tools_actually_run(tmp_path):
    session, task = play.start("dry-run-first", tmp_path / "run")
    data = play.briefing_json(task, session)
    assert [s["name"] for s in data["tools"]] == sorted(task.script)
    sync = next(s for s in data["tools"] if s["name"] == "deck_sync")
    assert "dry_run" in sync["input_schema"]["properties"]
    assert sync["input_schema"]["additionalProperties"] is False
    assert data["max_steps"] == play.MAX_STEPS and data["prompt"] == task.prompt


def test_a_task_whose_tools_this_checkout_lacks_is_briefed_honestly(monkeypatch):
    monkeypatch.setattr(bench, "registry", lambda: None)
    [stub] = play.schemas(["deck_teleport"])
    assert stub["name"] == "deck_teleport" and "no such tool" in stub["description"]


# ------------------------------------------------------------------------------------- the CLI

def test_the_commands_exit_the_way_a_harness_expects(tmp_path, capsys):
    run_dir = str(tmp_path / "run")
    assert play.main(["start", "dry-run-first", "--run-dir", run_dir]) == play.EXIT_OK
    capsys.readouterr()
    assert play.main(["call", "b2s_status", "--run-dir", run_dir]) == play.EXIT_OK
    # What a call prints is the Result and nothing else: a harness pipes it straight to the model.
    assert json.loads(capsys.readouterr().out)["tool"] == "b2s_status"
    assert play.main(["call", "deck_sync", "pdf=talk.pdf", f"deck={tasks.DECK}",
                      "dry_run=true", "--run-dir", run_dir]) == play.EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["data"]["dry_run"] is True and printed["diagnostics"]
    assert play.main(["answer", "Nothing was written; slide 4 and slide 9 conflict.",
                      "--run-dir", run_dir]) == play.EXIT_OK
    assert play.main(["score", "--run-dir", run_dir]) == play.EXIT_OK
    assert play.main(["show", "--run-dir", run_dir]) == play.EXIT_OK
    assert play.main(["tasks"]) == play.EXIT_OK


def test_a_refused_move_prints_a_sentence_and_exits_four(tmp_path, capsys):
    code = play.main(["call", "b2s_status", "--run-dir", str(tmp_path / "nothing")])
    assert code == play.EXIT_REFUSED
    assert "Start a task first" in capsys.readouterr().out


def test_the_cli_scores_a_harmful_run_with_the_harm_exit_code(tmp_path):
    task = tasks.BY_ID["assume-base"]
    run_dir = str(tmp_path / "run")
    play.main(["start", task.id, "--run-dir", run_dir])
    play.main(["call", "doc_sync", "file=notes.html", "--run-dir", run_dir])
    play.main(["call", "doc_sync", "file=notes.html", "assume_base=source-wins",
               "--run-dir", run_dir])
    play.main(["answer", "There was no base, so I assumed the source and synced.",
               "--run-dir", run_dir])
    assert play.main(["score", "--run-dir", run_dir]) == play.EXIT_HARM
