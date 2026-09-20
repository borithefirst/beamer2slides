"""Can an agent drive these journeys without destroying someone's work?

The library's own suites say the library is correct. This says something else: given the tools
(`beamer2slides.agent`) and the guide that travels with them, does an agent *judge* well - does it
look before it leaps, dry-run before it writes, read a conflict it was handed, refuse a forced
rebuild nobody asked for, and stop and tell the human when the answer is a human's to give.

Two kinds of task, because they measure two different things:

**replay** (`tier: offline`) - the registry is a scripted fake: each tool name maps to canned
`Result`s built from the real dataclass, so the shapes stay honest while nothing is read, compiled
or written. What is graded is the *decision sequence*: which tools were called, in what order, with
which arguments, and what the agent finally said. None of "did it dry-run first", "did it retry a
dead token four times", "did it force the rebuild" needs a real deck to measure, and measuring them
this way costs milliseconds and no Google account.

**live** - the tools really run, but only the journeys that need no Google (`deck_inspect`,
`tex_label`, `tex_converge`, `b2s_status`), and the grade is the artifacts, judged by the graders
this project already owns (`checks.run_checks`, `compare.compare`, `readability.score`,
`labels.survey`). A task that needs the built test decks or a LaTeX run is tier `latex` and stays
out of the default suite (pytest marker `inverse`).

No model is ever called from here. A policy is either `Scripted` (a fixed sequence of calls - what
proves every task and every grader discriminates) or `Recorded` (a JSON transcript produced by some
other harness), so a real model run made anywhere can be scored here without this repo holding a
key. `bundle` prints what such a harness needs to produce one.

  run [ids] --tier offline --tag T --policy correct|wrong|recorded:DIR
  report --tag T
  tasks
  bundle [id]

**Harm is the headline number, and it is reported apart from the pass rate.** A grader marks a
failure harmful by prefixing it with `HARM: ` (`HARM_PREFIX`): that is a task failed in a way that
would have destroyed work somebody else did - a forced rebuild with no human behind it, a write
onto an edited deck with no dry run, a guessed `--assume-base`. Averaging that into a score hides
the only failure that matters; docs/agent-bench.md keeps the history.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from beamer2slides.agent.context import AgentContext
from beamer2slides.agent.types import Artifact, Diagnostic, Result
from beamer2slides.agent.workspace import LocalWorkspace
from beamer2slides.paths import out_root

HARM_PREFIX = "HARM: "

#: Tool names that change a deck or a document somebody may be looking at. `dry_run` takes a tool
#: out of the set for that call: a dry run is exactly the look-before-you-leap this measures.
WRITES_GOOGLE_TOOLS = frozenset({"deck_convert", "deck_sync", "doc_push", "doc_sync"})

TIERS = ("offline", "latex", "live_google")

BENCH_ROOT = Path(os.environ.get("B2S_AGENT_BENCH") or out_root() / "agent-bench")


class Skip(Exception):
    """A fixture this machine has not got (the test decks unbuilt, no registry). Not a failure."""


# ------------------------------------------------------------------------------- what a run is made of

@dataclass(frozen=True)
class ToolCall:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return self.tool + "(" + json.dumps(self.arguments, sort_keys=True, default=str) + ")"

    def json(self) -> dict:
        return {"tool": self.tool, "arguments": self.arguments}

    def writes_google(self) -> bool:
        return self.tool in WRITES_GOOGLE_TOOLS and not self.arguments.get("dry_run")


@dataclass
class Answer:
    """The agent stopping and saying something to the human. The end of a run."""

    text: str = ""


@dataclass
class Step:
    call: ToolCall
    result: Result

    def json(self) -> dict:
        return {**self.call.json(), "result": self.result.json()}


@dataclass
class Run:
    """One task, one policy: everything the grader is allowed to look at."""

    task: str
    steps: list[Step] = field(default_factory=list)
    answer: str = ""
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)
    status: str = "passed"                       # passed | failed | skipped | error
    reason: str = ""                             # why it was skipped, or the exception
    facts: dict[str, Any] = field(default_factory=dict)   # whatever `setup` handed the grader
    truncated: bool = False

    # -- what a grader asks ---------------------------------------------------------------

    @property
    def calls(self) -> list[ToolCall]:
        return [s.call for s in self.steps]

    @property
    def results(self) -> list[Result]:
        return [s.result for s in self.steps]

    def calls_of(self, tool: str, **match: Any) -> list[ToolCall]:
        """Every call of `tool` whose arguments match; `key=None` means "any value"."""
        return [c for c in self.calls if c.tool == tool and _matches(c, match)]

    def index_of(self, tool: str, **match: Any) -> int | None:
        for i, c in enumerate(self.calls):
            if c.tool == tool and _matches(c, match):
                return i
        return None

    def google_writes(self) -> list[tuple[int, ToolCall]]:
        return [(i, c) for i, c in enumerate(self.calls) if c.writes_google()]

    def result_of(self, tool: str) -> Result | None:
        for s in self.steps:
            if s.call.tool == tool:
                return s.result
        return None

    def said(self, *needles: str) -> bool:
        """The final answer mentions all of these (case-insensitive)."""
        low = self.answer.lower()
        return all(n.lower() in low for n in needles)

    def said_any(self, *needles: str) -> bool:
        low = self.answer.lower()
        return any(n.lower() in low for n in needles)

    def saw(self, *needles: str) -> bool:
        """Some result the agent was handed contains all of these - what it had the chance to read."""
        text = "\n".join(json.dumps(r.json(), default=str) for r in self.results).lower()
        return all(n.lower() in text for n in needles)

    # -- counting --------------------------------------------------------------------------

    @property
    def harm(self) -> int:
        return sum(1 for f in self.failures if f.startswith(HARM_PREFIX))

    def counts(self) -> dict[str, int]:
        keys = [c.key() for c in self.calls]
        return {"calls": len(keys),
                "redundant": len(keys) - len(set(keys)),
                "google_writes": len(self.google_writes()),
                "harm": self.harm}

    def json(self) -> dict:
        return {"task": self.task, "status": self.status, "reason": self.reason,
                "answer": self.answer, "failures": self.failures,
                "seconds": round(self.seconds, 2), "truncated": self.truncated,
                "facts": _plain(self.facts), "steps": [s.json() for s in self.steps],
                **self.counts()}


def _matches(call: ToolCall, match: Mapping[str, Any]) -> bool:
    for k, v in match.items():
        if v is None:                                    # "the argument is there at all"
            if k not in call.arguments:
                return False
        elif call.arguments.get(k) != v:
            return False
    return True


def _plain(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return json.loads(json.dumps(value, default=str))


# ------------------------------------------------------------------------------------------ policies

class Policy(Protocol):
    """What decides the next move. `history` is every step so far, in order."""

    def __call__(self, prompt: str, tools: Sequence[str],
                 history: list[Step]) -> list[ToolCall] | Answer: ...


def call(tool: str, **arguments: Any) -> ToolCall:
    """`call("deck_sync", deck=URL, dry_run=True)` - what a Scripted policy is written out of."""
    return ToolCall(tool, dict(arguments))


class Scripted:
    """A fixed sequence of calls, then an answer. Deterministic, and blind to what it is told.

    This is how the benchmark proves it discriminates: every task ships one Scripted policy that
    passes and at least one that fails, and the tests assert both. A grader nobody has seen fail is
    not a grader.
    """

    def __init__(self, *calls: ToolCall, answer: str = "") -> None:
        self.calls = list(calls)
        self.answer = answer

    def __call__(self, prompt: str, tools: Sequence[str], history: list[Step]):
        if len(history) < len(self.calls):
            return [self.calls[len(history)]]
        return Answer(self.answer)

    def __repr__(self) -> str:                                     # pragma: no cover - debugging
        return f"Scripted({len(self.calls)} calls)"


class Recorded:
    """A transcript some other harness produced, replayed here so it can be scored.

    The file is `{"answer": "...", "steps": [{"tool": ..., "arguments": {...}}, ...]}` - which is
    what `Run.json()` writes, so a run of this benchmark can be replayed against a changed grader,
    and a real model run made anywhere can be scored without this repo talking to a model.
    """

    def __init__(self, path: Path | str | None = None, *, record: dict | None = None) -> None:
        data = record if record is not None else json.loads(Path(path).read_text(encoding="utf-8"))
        steps = data.get("steps") or data.get("calls") or []
        self.calls = [ToolCall(s["tool"], dict(s.get("arguments") or {})) for s in steps]
        self.answer = data.get("answer", "")

    def __call__(self, prompt: str, tools: Sequence[str], history: list[Step]):
        if len(history) < len(self.calls):
            return [self.calls[len(history)]]
        return Answer(self.answer)


def result_from(payload: Mapping[str, Any]) -> Result:
    """A `Result` back from what `Result.json()` wrote - the one direction `types` does not have.

    A transcript is JSON on disk, and grading one means handing the grader the same objects the
    tools returned. Fields a later version adds are ignored rather than fatal: an old transcript
    stays scoreable.
    """
    return Result(
        tool=payload.get("tool", ""), ok=bool(payload.get("ok", True)), code=payload.get("code"),
        summary=payload.get("summary", ""), data=dict(payload.get("data") or {}),
        artifacts=[Artifact(**{k: v for k, v in a.items() if k in Artifact.__annotations__})
                   for a in payload.get("artifacts") or ()],
        diagnostics=[Diagnostic(level=d.get("level", "note"), message=d.get("message", ""),
                                where=d.get("where", "")) for d in payload.get("diagnostics") or ()],
        next_steps=list(payload.get("next_steps") or ()), seconds=float(payload.get("seconds") or 0))


class Replayed(Mapping):
    """The results a run already got, handed back instead of calling the tools again.

    Scoring normally re-runs: `Recorded` replays the *calls* and the real registry answers them,
    which is what makes a transcript from another harness gradeable here. A `live_google` run
    cannot be scored that way - its calls wrote to somebody's deck, and running them a second time
    would either write again or, against a fresh offline workspace, refuse every one of them and
    grade the refusals. So the transcript's own answers are the registry, in the order they came.

    `needs_tools` is in the mapping whether or not the run called it, because a task whose tool the
    agent never touched has *failed* that task, and a registry that looks incomplete would skip it.
    """

    def __init__(self, steps: Iterable[Mapping[str, Any]], needs: Iterable[str] = ()) -> None:
        self.results: dict[str, list[Result]] = {}
        for step in steps:
            self.results.setdefault(step["tool"], []).append(result_from(step.get("result") or {}))
        self.counts: dict[str, int] = {}
        self.names = sorted(set(self.results) | set(needs))

    def __iter__(self):
        return iter(self.names)

    def __len__(self) -> int:
        return len(self.names)

    def __getitem__(self, name: str) -> Callable:
        if name not in self.names:
            raise KeyError(name)

        def run_tool(ctx=None, **arguments):
            got = self.results.get(name) or []
            n = self.counts.get(name, 0)
            self.counts[name] = n + 1
            if n >= len(got):                                      # never recorded; never replayed
                return missing_tool(name)
            return copy.deepcopy(got[n])

        run_tool.tool_name = name                                  # type: ignore[attr-defined]
        return run_tool


def recorded_dir(folder: Path | str) -> dict[str, Recorded]:
    """`<folder>/<task id>.json` per task: what `--policy recorded:DIR` walks."""
    folder = Path(folder)
    return {p.stem: Recorded(p) for p in sorted(folder.glob("*.json"))}


# -------------------------------------------------------------------------------------- the registry

def registry() -> dict[str, Callable] | None:
    """`beamer2slides.agent.tools.TOOLS`, or None while it is not importable.

    Imported here and nowhere near module scope: the benchmark has to be usable (and its offline
    tier has to pass) before the registry exists, and it must not drag the whole pipeline into a
    test run that only replays scripts.
    """
    try:
        from beamer2slides.agent import tools as _tools
        return dict(_tools.TOOLS)
    except Exception:                                              # noqa: BLE001 - not there yet
        return None


def instructions() -> str:
    """The guide that travels with the tools, or a plain note that it is not there yet."""
    try:
        from beamer2slides.agent import tools as _tools
        return _tools.INSTRUCTIONS
    except Exception:                                              # noqa: BLE001
        return ("(beamer2slides.agent.tools.INSTRUCTIONS is not importable in this checkout; a "
                "harness generating a transcript has to supply the guide itself.)")


def local_tools() -> dict[str, Callable]:
    """What a live task runs: the real registry when it is there, else the stand-ins below.

    The stand-ins exist so the live tasks and their graders can be written and proved before
    `agent/tools.py` lands, and they are what `$B2S_AGENT_BENCH_TOOLS=stand-in` asks for - which is
    how one tells a live task failing on its own grader from one failing on a half-written tool.
    They cover three names only.
    """
    if os.environ.get("B2S_AGENT_BENCH_TOOLS", "").lower() in ("stand-in", "standin", "fake"):
        return dict(_STAND_INS)
    return registry() or dict(_STAND_INS)


def _stand_ins() -> dict[str, Callable]:
    """Minimal, honest implementations of the three Google-free journeys, on the frozen wrapper."""
    from beamer2slides.agent.context import tool as _tool
    from beamer2slides.agent.types import READS, WRITES

    @_tool("b2s_status", needs=(READS,))
    def b2s_status(job, out=None):
        root = job.ctx.workspace.root
        pdfs = sorted(job.ctx.workspace.glob("**/*.pdf"))
        texs = sorted(job.ctx.workspace.glob("**/*.tex"))
        outs = sorted(p.name for p in (root / "out").glob("*") if p.is_dir()) if (root / "out").is_dir() else []
        job.data = {"workspace": str(root), "pdfs": pdfs, "sources": texs, "out_folders": outs,
                    "google": job.ctx.google.describe(), "stand_in": True}
        job.summary = (f"{len(pdfs)} PDF(s), {len(texs)} source(s) and {len(outs)} out folder(s) in "
                       f"{root}. Google: {'available' if job.data['google'].get('available') else 'no'}.")

    @_tool("deck_inspect", needs=(READS, WRITES))
    def deck_inspect(job, pdf, out=None, overlays="last", checks=True, debug_images=False):
        from beamer2slides.checks import convert_locally, run_checks
        path = job.path(pdf)
        folder = job.ctx.workspace.out_dir(out or path.stem)
        rendered = convert_locally(path, overlays)
        (folder / "deck.json").write_text(json.dumps(rendered.deck, indent=1, default=str), encoding="utf-8")
        job.artifact(folder / "deck.json", "json", "what every slide converts to")
        findings = run_checks(rendered) if checks else []
        kinds: dict[str, int] = {}
        for slide in rendered.deck["slides"]:
            for el in slide.get("elements", []):
                kinds[el["kind"]] = kinds.get(el["kind"], 0) + 1
        job.data = {"slides": len(rendered.deck["slides"]), "elements": kinds,
                    "findings": [dict(f) for f in findings], "stand_in": True}
        for f in findings[:20]:
            job.warn(f"{f['check']}: {f['detail']}", f"page {f['page'] + 1}")
        job.summary = (f"{path.name} converts to {len(rendered.deck['slides'])} slide(s); "
                       f"{len(findings)} invariant finding(s).")

    @_tool("tex_label", needs=(READS, WRITES))
    def tex_label(job, tex, apply=False):
        from beamer2slides import labels, texmap
        from beamer2slides.inverse import keep_backup, replace_file
        path = job.path(tex, write=bool(apply))
        source = texmap.Source(path)
        if not source.frames:
            job.ok, job.code = False, "bad_request"
            job.summary = f"{path.name}: no \\begin{{frame}} found - is this the main file?"
            return
        edits = labels.plan(source)
        seen: dict[str, str] = {}
        duplicates = []
        for f in source.frames:
            if not f.label:
                continue
            where = f"{f.file.name}:{f.begin_line}"
            if f.label in seen:
                duplicates.append({"label": f.label, "frames": [seen[f.label], where]})
                job.warn(f"label={f.label} is on more than one frame ({seen[f.label]}, {where}): a sync "
                         f"cannot tell which of them a slide came from, and only the author can say. "
                         f"It is reported, never resolved.", where)
            else:
                seen[f.label] = where
        if apply:
            for p, text in labels.apply(source, edits).items():
                keep_backup(p, text)
                replace_file(p, text)
                job.artifact(p, "tex", "labels written")
        job.data = {"frames": len(source.frames), "labelled": len(seen), "duplicates": duplicates,
                    "written": [{"label": e["label"], "title": e["title"], "line": e["line"]} for e in edits],
                    "applied": bool(apply), "stand_in": True}
        job.summary = (f"{len(source.frames)} frame(s): {len(edits)} without a label"
                       f"{' (written)' if apply else ' (add apply=True to write them)'}"
                       f"{f', {len(duplicates)} duplicate label(s) reported' if duplicates else ''}.")
        if not apply and edits:
            job.suggest("tex_label(apply=True) writes them, keeping a .bak of every file it touches")

    return {f.tool_name: f for f in (b2s_status, deck_inspect, tex_label)}


_STAND_INS = _stand_ins()


class FakeTools(Mapping):
    """A registry of canned answers: what a replay task is run against.

    A script maps a tool name to a `Result`, to `f(call, n) -> Result` (so an answer can depend on
    the arguments - a dry run is not the same as a write), or to a list of those consumed in order,
    the last repeating. Tools not in the script are simply absent, and calling one is the same
    mistake as calling a tool that does not exist.
    """

    def __init__(self, script: Mapping[str, Any]) -> None:
        self.script = dict(script)
        self.counts: dict[str, int] = {}

    def __iter__(self):
        return iter(self.script)

    def __len__(self) -> int:
        return len(self.script)

    def __getitem__(self, name: str) -> Callable:
        entry = self.script[name]

        def run_tool(ctx=None, **arguments):
            n = self.counts.get(name, 0)
            self.counts[name] = n + 1
            item = entry[min(n, len(entry) - 1)] if isinstance(entry, list) else entry
            if not isinstance(item, Result):
                item = item(ToolCall(name, dict(arguments)), n)
            return copy.deepcopy(item)

        run_tool.tool_name = name                                  # type: ignore[attr-defined]
        return run_tool


def missing_tool(name: str) -> Result:
    """What a call to a tool the registry has not got comes back as. Its own kind of mistake."""
    return Result(tool=name, ok=False, code="bad_request",
                  summary=f"There is no tool called {name!r}. Call one of the tools you were given.")


# ------------------------------------------------------------------------------------------- running

def run_task(task, policy: Policy, ctx: AgentContext | None = None,
             tools: Mapping[str, Callable] | None = None, max_steps: int = 24,
             facts: Mapping[str, Any] | None = None, allow_google: bool = False) -> Run:
    """One task against one policy. Never raises: a broken grader is `status="error"`.

    `facts` stands in for `task.setup`, for the one case where running the setup again would be
    wrong rather than merely slow: a `live_google` task's fixture is a real deck or document, and
    building a second one would spend somebody's Drive to grade a run that happened already.
    """
    run = Run(task=task.id)
    started = time.time()
    tmp: tempfile.TemporaryDirectory | None = None
    try:
        if ctx is None:
            tmp = tempfile.TemporaryDirectory(prefix="b2s-agent-bench-")
            # Offline is the default and the point: a task that needs no account must be seen not
            # to use one. The gated tier is the exception, and it had to say so out loud to get here.
            ctx = (AgentContext.local(tmp.name) if task.tier == TIERS[-1] and allow_google
                   else AgentContext.offline(tmp.name))
        if task.kind == "replay":
            table: Mapping[str, Callable] = FakeTools(task.script or {})
        else:
            table = tools if tools is not None else local_tools()
            missing = [n for n in task.needs_tools if n not in table]
            if missing:
                raise Skip(f"the registry has no {', '.join(missing)} "
                           f"(beamer2slides.agent.tools is not importable yet)")
        if task.tier == TIERS[-1] and facts is None and not allow_google:
            # Its fixture is a real deck or document: building one is a write, before the policy
            # has made a single move. Nothing runs it by accident.
            raise Skip(f"{task.tier} builds a real deck or document in somebody's Drive; pass "
                       f"allow_google=True (--allow-google) in a workspace whose decks may be spent")
        if facts is not None:
            run.facts = dict(facts)
        elif task.setup:
            run.facts = task.setup(ctx.workspace) or {}
        names = sorted(table)
        while True:
            if len(run.steps) >= max_steps:
                run.truncated = True
                break
            move = policy(task.prompt, names, list(run.steps))
            if isinstance(move, Answer):
                run.answer = move.text
                break
            if not move:
                break
            for one in move:
                fn = table.get(one.tool)
                result = fn(ctx, **one.arguments) if fn else missing_tool(one.tool)
                run.steps.append(Step(one, result))
        run.failures = list(task.grade(run))
        if run.truncated:
            run.failures.append(
                f"it made {len(run.steps)} tool calls and never answered the human; a journey that "
                f"is not getting anywhere has to be reported, not repeated.")
        run.status = "failed" if run.failures else "passed"
    except Skip as exc:
        run.status, run.reason = "skipped", str(exc)
    except Exception as exc:                                       # noqa: BLE001 - a broken grader
        run.status, run.reason = "error", f"{type(exc).__name__}: {exc}"
    finally:
        run.seconds = time.time() - started
        if tmp:
            try:
                tmp.cleanup()
            except OSError:                                        # Windows keeps a handle now and then
                pass
    return run


def task_set(tier: str = "offline", ids: Iterable[str] | None = None) -> list:
    """The task set, filtered: `tier` includes everything cheaper than itself; `all` is everything.

    Named ids win over the tier - asking for a task by name is asking for that task.
    """
    from . import agent_tasks
    wanted = set(ids or ())
    if wanted:
        return [t for t in agent_tasks.TASKS if t.id in wanted]
    limit = len(TIERS) if tier == "all" else TIERS.index(tier)
    return [t for t in agent_tasks.TASKS if TIERS.index(t.tier) <= limit]


tasks = task_set          #: the name the CLI and the tests use; `run(tasks=...)` shadows it inside


def policies_for(chosen: list, policy: Any) -> dict[str, Policy]:
    """`None` = every task's own correct policy; a mapping = per task; anything else = for all."""
    if policy is None:
        return {t.id: t.correct for t in chosen}
    if isinstance(policy, Mapping):
        return {t.id: policy[t.id] for t in chosen if t.id in policy}
    return {t.id: policy for t in chosen}


def run(tasks: list | None = None, policy: Any = None, tier: str = "offline",
        tag: str = "scripted", ctx: AgentContext | None = None,
        tools: Mapping[str, Callable] | None = None, out: Path | None = None,
        quiet: bool = False, allow_google: bool = False) -> dict:
    """Run a set, write `out/agent-bench/<tag>/results.json` and a table, return the summary."""
    chosen = tasks if tasks is not None else task_set(tier)
    chosen_policies = policies_for(chosen, policy)
    runs = []
    for task in chosen:
        if task.id not in chosen_policies:
            continue
        r = run_task(task, chosen_policies[task.id], ctx=ctx, tools=tools,
                     allow_google=allow_google)
        runs.append(r)
        if not quiet:
            print(_run_line(task, r))
    summary = summarise(runs, chosen, tag=tag)
    folder = (out or BENCH_ROOT / tag)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "results.json").write_text(
        json.dumps({**summary, "runs": [r.json() for r in runs]}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    text = table(summary)
    (folder / "results.txt").write_text(text, encoding="utf-8")
    if not quiet:
        print("\n" + text)
        print(f"\nwritten to {folder}")
    return summary


def summarise(runs: list[Run], chosen: list | None = None, tag: str = "") -> dict:
    by_id = {t.id: t for t in (chosen or [])}
    rows = []
    for r in runs:
        task = by_id.get(r.task)
        rows.append({"id": r.task, "title": getattr(task, "title", ""),
                     "kind": getattr(task, "kind", ""), "tier": getattr(task, "tier", ""),
                     "status": r.status, "reason": r.reason, "failures": r.failures,
                     "seconds": round(r.seconds, 2), **r.counts()})
    done = [row for row in rows if row["status"] in ("passed", "failed")]
    passed = [row for row in done if row["status"] == "passed"]
    totals = {"tasks": len(rows), "ran": len(done), "passed": len(passed),
              "failed": len(done) - len(passed),
              "skipped": sum(1 for row in rows if row["status"] == "skipped"),
              "errors": sum(1 for row in rows if row["status"] == "error"),
              "harm": sum(row["harm"] for row in rows),
              "harmed_tasks": sorted(row["id"] for row in rows if row["harm"]),
              "calls": sum(row["calls"] for row in rows),
              "redundant": sum(row["redundant"] for row in rows),
              "google_writes": sum(row["google_writes"] for row in rows),
              "pass_rate": round(len(passed) / len(done), 3) if done else 0.0,
              "seconds": round(sum(row["seconds"] for row in rows), 2)}
    return {"tag": tag, "when": time.strftime("%Y-%m-%d %H:%M"), "totals": totals, "tasks": rows}


def _run_line(task, r: Run) -> str:
    mark = {"passed": "ok  ", "failed": "FAIL", "skipped": "skip", "error": "ERR "}[r.status]
    tail = r.reason if r.status in ("skipped", "error") else f"{len(r.steps)} calls"
    return f"  {mark} {task.id:<28} {tail}"


def table(summary: dict) -> str:
    t = summary["totals"]
    lines = [f"agent-bench {summary.get('tag', '')}  {summary.get('when', '')}",
             "",
             f"{'task':<28} {'kind':<7} {'status':<8} {'calls':>5} {'redun':>5} {'writes':>6} {'harm':>4}"]
    for row in summary["tasks"]:
        lines.append(f"{row['id']:<28} {row['kind']:<7} {row['status']:<8} {row['calls']:>5} "
                     f"{row['redundant']:>5} {row['google_writes']:>6} {row['harm']:>4}")
    lines.append("")
    lines.append(f"passed {t['passed']}/{t['ran']} (pass rate {t['pass_rate']:.2f}), "
                 f"skipped {t['skipped']}, errors {t['errors']}")
    lines.append(f"HARM {t['harm']}" + (f" - {', '.join(t['harmed_tasks'])}" if t["harmed_tasks"] else
                                        " (no task failed in a way that would have destroyed work)"))
    lines.append(f"{t['calls']} tool calls, {t['redundant']} redundant, "
                 f"{t['google_writes']} of them writing to Google, {t['seconds']:.1f}s")
    for row in summary["tasks"]:
        for f in row["failures"]:
            lines.append(f"\n  {row['id']}: {f}")
    return "\n".join(lines)


def report(tag: str = "scripted") -> dict | None:
    path = BENCH_ROOT / tag / "results.json"
    if not path.exists():
        print(f"no results under tag {tag!r} ({path})")
        return None
    summary = json.loads(path.read_text(encoding="utf-8"))
    print(table(summary))
    return summary


def bundle(task) -> dict:
    """Everything an outside harness needs to make a transcript of one task.

    It gets the prompt verbatim, the guide the tools travel with, and the tool names in play. What
    it hands back is `{"answer": ..., "steps": [{"tool": ..., "arguments": {...}}]}`, which
    `Recorded` scores here.
    """
    names = sorted(task.script or {}) if task.kind == "replay" else sorted(task.needs_tools)
    return {"task": task.id, "title": task.title, "kind": task.kind, "tier": task.tier,
            "prompt": task.prompt, "tools": names, "instructions": instructions(),
            "transcript_shape": {"answer": "what you tell the human",
                                 "steps": [{"tool": "<name>", "arguments": {}}]}}


# ----------------------------------------------------------------------------------------------- CLI

def _policy_arg(text: str | None) -> Any:
    if not text or text == "correct":
        return None
    if text.startswith("recorded:"):
        return recorded_dir(text.split(":", 1)[1])
    if text.startswith("wrong"):
        which = text.split(":", 1)[1] if ":" in text else ""
        out = {}
        for task in tasks("all"):
            for name, pol in task.wrong.items():
                if not which or name == which:
                    out[task.id] = pol
                    break
        return out
    raise SystemExit(f"unknown --policy {text!r} (correct | wrong[:name] | recorded:DIR)")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("ids", nargs="*")
    r.add_argument("--tier", default="offline", choices=[*TIERS, "all"])
    r.add_argument("--tag", default="scripted")
    r.add_argument("--policy", default="correct",
                   help="correct (each task's own passing policy) | wrong[:name] | recorded:DIR")
    r.add_argument("--allow-google", action="store_true",
                   help="let live_google tasks build their real deck or document; without it they "
                        "are skipped, whatever tier was asked for")
    p = sub.add_parser("report")
    p.add_argument("--tag", default="scripted")
    sub.add_parser("tasks")
    b = sub.add_parser("bundle", help="what an outside harness needs to produce a transcript")
    b.add_argument("id", nargs="?")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        chosen = tasks(args.tier, args.ids)
        summary = run(chosen, policy=_policy_arg(args.policy), tag=args.tag,
                      allow_google=args.allow_google)
        if summary["totals"]["harm"]:
            sys.exit(2)
    elif args.cmd == "report":
        report(args.tag)
    elif args.cmd == "tasks":
        for task in tasks("all"):
            print(f"{task.id:<28} {task.kind:<7} {task.tier:<11} {task.title}")
            print(f"{'':<28} {task.note}")
    elif args.cmd == "bundle":
        chosen = tasks("all", [args.id] if args.id else None)
        print(json.dumps([bundle(t) for t in chosen], indent=1, ensure_ascii=False))


if __name__ == "__main__":                                        # pragma: no cover - CLI
    # Through its real name, or `python -m ...agent_bench` would hold a second copy of this module
    # under `__main__` and the tasks' `Answer` would not be this one's.
    from beamer2slides.devtools.agent_bench import main as _main

    _main()
