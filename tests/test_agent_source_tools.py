"""The agent layer's LaTeX-writing journeys (agent/source_tools.py).

Default run (offline, no TeX and no Google): what the three tools refuse before they do any work,
that every parameter carries the one-line description the schema generator reads, and that
`typing.get_type_hints` resolves on the wrappers `@tool` hands back - which is where a `from
__future__ import annotations` in the tool module would break, since the wrapper's globals are
context.py's.

Opt-in (`python -m pytest -m inverse`, needs pdflatex): the compile loop itself, converging
tests/decks/inverse/a.tex onto its own classified IR (a.deck.json), and a source that does not
compile coming back as `compile_failed` with the file and line in it.
"""

import inspect
import json
import shutil
from pathlib import Path
from typing import Annotated, get_type_hints

import pytest

from beamer2slides.agent import ALL_ACTIONS, LOCAL_ONLY, AgentContext, LocalWorkspace
from beamer2slides.agent.source_tools import SOURCE_TOOLS, deck_adopt, deck_pull, tex_converge

TESTS = Path(__file__).resolve().parent
INV = TESTS / "decks" / "inverse"


def pdflatex_missing() -> str | None:
    from beamer2slides.inverse import tex_env
    return None if shutil.which("pdflatex", path=tex_env()["PATH"]) else "pdflatex not found"


def fixture_tree(root: Path) -> Path:
    """A copy of the inverse fixtures inside a workspace (the loop writes beside the source)."""
    tree = root / "src"
    shutil.copytree(INV, tree, ignore=shutil.ignore_patterns("out", "b_*.tex"))
    return tree


# ---------------------------------------------------------------- refusals before any work

def test_an_offline_workspace_refuses_the_google_journeys(tmp_path):
    """`offline` either way: the gate says it when the context neither allows Google nor has any,
    and `NoGoogle` says it when the context allows Google and still has none. Both come before a
    path is resolved or a file is touched."""
    gated = AgentContext.offline(tmp_path)
    allowed = AgentContext.offline(tmp_path, allow=ALL_ACTIONS)
    for run in (deck_pull, deck_adopt):
        res = run(gated, deck="someid", tex="main.tex")
        assert not res.ok and res.code == "offline", res.json()
        assert res.data["needs"] == ["reads", "writes", "reads_google"]
        assert run(allowed, deck="someid", tex="main.tex").code == "offline"
    assert list(tmp_path.iterdir()) == []


def test_a_context_that_allows_no_google_refuses_even_holding_an_account(tmp_path):
    ctx = AgentContext(workspace=LocalWorkspace(tmp_path), google=FakeGoogle(), allow=LOCAL_ONLY)
    res = deck_pull(ctx, deck="someid", tex="main.tex")
    assert not res.ok and res.code == "forbidden", res.json()
    assert list(tmp_path.iterdir()) == []


def test_converge_refuses_a_target_that_is_not_there(tmp_path):
    (tmp_path / "main.tex").write_text("\\documentclass{beamer}\n", encoding="utf-8")
    res = tex_converge(AgentContext.offline(tmp_path), target="deck.json", tex="main.tex")
    assert not res.ok and res.code == "not_found", res.json()
    assert "deck.json" in res.summary


def test_converge_refuses_a_target_outside_the_workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "main.tex").write_text("\\documentclass{beamer}\n", encoding="utf-8")
    outside = tmp_path / "elsewhere" / "deck.json"
    outside.parent.mkdir()
    outside.write_text("{}", encoding="utf-8")
    res = tex_converge(AgentContext.offline(root), target=str(outside), tex="main.tex")
    assert not res.ok and res.code == "outside_workspace", res.json()
    assert res.data["root"] == str(root.resolve())


def test_converge_refuses_a_source_that_is_not_there(tmp_path):
    (tmp_path / "deck.json").write_text(json.dumps({"slides": []}), encoding="utf-8")
    res = tex_converge(AgentContext.offline(tmp_path), target="deck.json", tex="main.tex")
    assert not res.ok and res.code == "not_found" and "main.tex" in res.summary


def test_converge_refuses_a_target_that_is_not_a_deck(tmp_path):
    (tmp_path / "main.tex").write_text("\\documentclass{beamer}\n", encoding="utf-8")
    (tmp_path / "deck.json").write_text("not json at all", encoding="utf-8")
    res = tex_converge(AgentContext.offline(tmp_path), target="deck.json", tex="main.tex")
    assert not res.ok and res.code == "bad_request", res.json()
    (tmp_path / "deck.json").write_text(json.dumps({"pages": []}), encoding="utf-8")
    res = tex_converge(AgentContext.offline(tmp_path), target="deck.json", tex="main.tex")
    assert not res.ok and res.code == "bad_request" and "slides" in res.summary


class FakeGoogle:
    """Credentials nothing will call: enough to get past the wrapper, which fetches them before
    the body runs so that a dead token costs a millisecond rather than a two-minute journey."""

    def credentials(self):
        return object()

    def describe(self) -> dict:
        return {"available": True, "source": "test", "scopes": []}


def with_google(root: Path) -> AgentContext:
    return AgentContext(workspace=LocalWorkspace(root), google=FakeGoogle(), allow=ALL_ACTIONS)


def test_adopt_refuses_a_source_that_is_already_there(tmp_path):
    """Adopt writes a new tree; refusing now saves the minutes of thumbnails it would spend first."""
    (tmp_path / "main.tex").write_text("\\documentclass{beamer}\n", encoding="utf-8")
    res = deck_adopt(with_google(tmp_path), deck="someid", tex="main.tex")
    assert not res.ok and res.code == "source_exists", res.json()
    assert res.next_steps and "deck_pull" in res.next_steps[0]
    assert res.data["tex"] == "main.tex"


def test_pull_refuses_a_deck_that_names_nothing(tmp_path):
    """A ref with a path in it is a folder that has to be there; a bare word is a presentation id."""
    (tmp_path / "main.tex").write_text("\\documentclass{beamer}\n", encoding="utf-8")
    res = deck_pull(with_google(tmp_path), deck="out/talk", tex="main.tex")
    assert not res.ok and res.code == "not_found" and "out/talk" in res.summary
    res = deck_pull(with_google(tmp_path), deck="saved.json", tex="main.tex")
    assert not res.ok and res.code == "not_found", res.json()


def test_pull_refuses_a_source_that_is_not_there_before_reading_the_deck(tmp_path):
    res = deck_pull(with_google(tmp_path), deck="someid", tex="main.tex")
    assert not res.ok and res.code == "not_found" and "main.tex" in res.summary
    assert list(tmp_path.iterdir()) == []


def test_adopt_reads_a_local_json_target_without_google(tmp_path):
    """A .json deck is a local target - but `needs` is static, so the credentials are still asked
    for, which is why an offline context refuses this tool either way (the docstring says so)."""
    (tmp_path / "deck.json").write_text(json.dumps({"slides": []}), encoding="utf-8")
    res = deck_adopt(AgentContext.offline(tmp_path, allow=ALL_ACTIONS),
                     deck="deck.json", tex="new.tex")
    assert not res.ok and res.code == "offline", res.json()
    assert not (tmp_path / "new.tex").exists()


# ---------------------------------------------------------------- the schema the model reads

def test_every_parameter_carries_a_description():
    for fn in SOURCE_TOOLS:
        hints = get_type_hints(fn.body, include_extras=True)
        params = list(inspect.signature(fn.body).parameters)
        assert params[0] == "j", fn.tool_name
        for name in params[1:]:
            hint = hints[name]
            meta = getattr(hint, "__metadata__", ())
            assert meta and isinstance(meta[0], str) and len(meta[0]) > 20, \
                f"{fn.tool_name}.{name} has no description a model could read"
            assert hint.__origin__ is not None


def test_type_hints_resolve_on_the_wrappers(tmp_path):
    """`@tool` returns a function defined in context.py, so string annotations would not resolve
    there; `functools.wraps` copies the real `Annotated` objects over and these do."""
    for fn in SOURCE_TOOLS:
        hints = get_type_hints(fn, include_extras=True)
        assert set(hints) >= {"j", "tex"}
        assert hints["tex"].__metadata__[0]


def test_the_tools_declare_what_they_do():
    assert deck_pull.needs == ("reads", "writes", "reads_google")
    assert tex_converge.needs == ("reads", "writes")              # the offline twin: no Google
    assert deck_adopt.needs == ("reads", "writes", "reads_google")
    for fn in SOURCE_TOOLS:
        doc = (fn.body.__doc__ or "").strip()
        assert 3 <= len(doc.splitlines()) <= 8, fn.tool_name
        assert "compil" in doc.lower(), f"{fn.tool_name} does not say that it compiles LaTeX"


def test_default_iterations_follow_the_commands():
    defaults = {fn.tool_name: {p.name: p.default for p in inspect.signature(fn.body).parameters.values()}
                for fn in SOURCE_TOOLS}
    assert defaults["deck_pull"]["max_iter"] == 10
    assert defaults["tex_converge"]["max_iter"] == 10
    assert defaults["deck_adopt"]["max_iter"] == 6                # adopt's own default, not pull's


# ---------------------------------------------------------------- helpers

def test_residual_counting_and_stall_detection():
    from beamer2slides.agent import source_tools as st
    assert st._by_kind([{"kind": "text"}, {"kind": "text"}, {"kind": "geometry"}]) == \
        {"geometry": 1, "text": 2}

    class R:
        def __init__(self, opens):
            self.iterations = [{"open": n} for n in opens]
    assert st._stalled(R([7, 7, 7]))
    assert st._stalled(R([4]))
    assert not st._stalled(R([7, 3, 2]))
    assert not st._stalled(R([7, 0]))                             # converged is not stalled


def test_a_compile_failure_carries_the_file_and_line():
    from beamer2slides.agent import source_tools as st
    from beamer2slides.agent.types import Refused

    log = "the source does not compile:\n./main.tex:112: Undefined control sequence.\nl.112 \\slidepar"

    def boom():
        raise RuntimeError(log)
    with pytest.raises(Refused) as exc:
        st._loop(boom)
    assert exc.value.code == "compile_failed"
    assert exc.value.data["file"] == "./main.tex" and exc.value.data["line"] == 112
    assert exc.value.data["where"] == "./main.tex:112"


class FakeResult:
    """What `run_pull` hands back, without the twenty seconds of LaTeX it takes to get one."""

    def __init__(self, opens, unresolved=(), files=(), converged=None):
        self.iterations = [{"iteration": i, "open": n, "by_kind": {"geometry": n} if n else {},
                            "geometry_error": float(n)} for i, n in enumerate(opens)]
        self.residuals = [_geometry(i) for i in range(opens[-1])]
        self.unresolved = list(unresolved)
        self.files = {f: "" for f in files}
        self.patch = ""
        self.notes = []
        self.theme = []
        self.converged = opens[-1] == 0 if converged is None else converged


def _geometry(i: int) -> dict:
    return {"kind": "geometry", "target_slide": i, "target_element": f"text/{i}",
            "dx": 3.0, "dy": -1.0}


def finish(ctx, result, work: Path, **kw):
    from beamer2slides.agent import source_tools as st
    from beamer2slides.agent.context import Job
    from beamer2slides.agent.types import Refused

    j = Job("tex_converge", ctx)
    kw = {"apply": False, "max_iter": 4, "tool_name": "tex_converge", **kw}
    try:
        st._finish(j, result, work, None, kw["apply"], kw["max_iter"], kw["tool_name"])
    except Refused as exc:
        j.ok, j.code, j.summary = False, exc.code, str(exc)
        j.data.update(exc.data)
    return j.result


def test_residuals_left_after_a_loop_that_got_somewhere_are_a_warning_not_a_refusal(tmp_path):
    """The source compiles and edits.md says what is left: refusing would throw that away."""
    res = finish(AgentContext.offline(tmp_path),
                 FakeResult([9, 4, 2], unresolved=[{**_geometry(1), "why": "not converging",
                                                    "where": "main.tex:40-52"}]),
                 tmp_path, max_iter=2)
    assert res.ok and res.code is None, res.json()
    assert res.data["residuals_before"] == {"geometry": 9} and res.data["residuals_left"] == {"geometry": 2}
    warnings = [d for d in res.diagnostics if d.level == "warning"]
    assert len(warnings) == 1 and "not converging" in warnings[0].message
    assert warnings[0].where == "main.tex:40-52"
    assert "2 residual(s)" in res.summary and "edits.md" in res.summary


def test_a_loop_that_never_moved_is_not_converged(tmp_path):
    (tmp_path / "edits.md").write_text("# Pull report\n", encoding="utf-8")
    res = finish(AgentContext.offline(tmp_path), FakeResult([5, 5, 5]), tmp_path)
    assert not res.ok and res.code == "not_converged", res.json()
    assert res.data["rounds"] == 2 and res.data["residuals_left"] == {"geometry": 5}
    assert res.next_steps                                          # what to do instead
    assert [a.ref for a in res.artifacts] == ["edits.md"]          # a refusal keeps the report


def test_a_file_someone_edited_while_the_loop_ran_is_a_conflict(tmp_path):
    (tmp_path / "edits.json").write_text(json.dumps({"not_applied": [str(tmp_path / "main.tex")]}),
                                         encoding="utf-8")
    res = finish(AgentContext.offline(tmp_path), FakeResult([1, 0], files=[str(tmp_path / "main.tex")]),
                 tmp_path, apply=True)
    assert res.ok and res.code is None, res.json()
    conflicts = [d for d in res.diagnostics if d.level == "conflict"]
    assert len(conflicts) == 1 and ".b2s-new" in conflicts[0].message
    assert res.data["not_applied"] and res.data["files_changed"] == ["main.tex"]


def test_adopt_scores_the_source_it_wrote_for_readability(tmp_path):
    """`data["readability"]` is the signal for whether an adopted source is worth keeping;
    sources people wrote score 0.6-1.0, and a.tex is one."""
    from beamer2slides.agent import source_tools as st
    from beamer2slides.agent.context import Job

    tex = tmp_path / "main.tex"
    tex.write_text((INV / "a.tex").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "beamerthemeDemo.sty").write_text("\\mode<presentation>\n", encoding="utf-8")
    j = Job("deck_adopt", AgentContext.offline(tmp_path))
    st._readability(j, tex)
    assert 0.6 <= j.data["readability"] <= 1.0
    assert j.data["readability_detail"]["frames"] == 5


def test_an_unrelated_runtime_error_is_not_a_compile_failure():
    from beamer2slides.agent import source_tools as st
    with pytest.raises(RuntimeError):
        st._loop(lambda: (_ for _ in ()).throw(RuntimeError("something else")))


# ---------------------------------------------------------------- the loop itself (opt-in)

@pytest.mark.inverse
def test_converge_runs_the_loop_on_the_inverse_fixture(tmp_path):
    """a.deck.json is a.tex's own classification, so the loop should find little or nothing to do."""
    if reason := pdflatex_missing():
        pytest.skip(reason)
    tree = fixture_tree(tmp_path)
    res = tex_converge(AgentContext.offline(tmp_path), target="src/a.deck.json", tex="src/a.tex",
                       work="work", max_iter=2)
    assert res.ok or res.code == "not_converged", res.json()
    assert res.data["slides"] == len(json.loads((tree / "a.deck.json").read_text(encoding="utf-8"))["slides"])
    assert res.data["iterations"] and res.data["rounds"] <= 2
    assert set(res.data) >= {"converged", "residuals_before", "residuals_left", "files_changed"}
    kinds = {a.kind for a in res.artifacts}
    assert {"report", "json"} <= kinds, [a.json() for a in res.artifacts]
    assert (tmp_path / "work" / "edits.md").exists()
    assert res.data["applied"] is False and "not_applied" not in res.data


@pytest.mark.inverse
def test_a_source_that_does_not_compile_comes_back_as_compile_failed(tmp_path):
    if reason := pdflatex_missing():
        pytest.skip(reason)
    tree = fixture_tree(tmp_path)
    broken = (tree / "a.tex").read_text(encoding="utf-8").replace(
        "\\begin{document}", "\\begin{document}\n\\thisCommandDoesNotExist")
    (tree / "a.tex").write_text(broken, encoding="utf-8")
    res = tex_converge(AgentContext.offline(tmp_path), target="src/a.deck.json", tex="src/a.tex",
                       work="work", max_iter=1)
    assert not res.ok and res.code == "compile_failed", res.json()
    assert "thisCommandDoesNotExist" in res.summary or "Undefined control sequence" in res.summary
    assert res.data.get("line")
