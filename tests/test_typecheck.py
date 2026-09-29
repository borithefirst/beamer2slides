"""The type checker's gate: pyrefly over the package under pyproject.toml's [tool.pyrefly], and the
modules in GATED below must check clean (CI: .github/workflows/typecheck.yml).

Why types: 688ebf4. deck.json elements are plain dicts, and marked.py produced ones emit could not
take - a shape kind no Slides preset has, no `flip`, an outline as a bare colour, a table without
its layout - found only when a person's upload died on `KeyError: 'custom'`. A TypedDict says what
an element holds; the checker finds the producer or the consumer that breaks it.

Why the gated list is here and not an include list in the config: pyrefly infers an unannotated
function's return type only inside the files it checks, so a module's errors depend on which other
modules are checked with it. One run over the whole package, read module by module, gives every
module the answer it has in an editor. (Check a git worktree with its own Python path in mind: an
absolute `import beamer2slides...` there resolves through the editable install to the main
checkout, and devtools/agent_tasks_google.py's 7 googleapiclient `Resource` errors went unseen.)

Why a test besides the workflow: the gate runs where the suite runs, in about two seconds, so a
module that stops checking clean is caught before a push; and it holds three rules pyrefly's config
cannot say. The ratchet: a module that checks clean is in GATED (it only grows). The element
contract over the whole package: a TypedDict error anywhere fails, gated module or not, against a
list of known ones that fails when it goes stale (like tests/invariants_allow.json). And the config
still sees the bug: an optional key read by subscript is an error.

Skipped where pyrefly is not installed (it is in the [dev] extra), where it is not the pinned
version (a new release brings new checks), and where the tree has no pyproject.toml (Google's
monorepo) - unless $B2S_TYPECHECK_REQUIRED is set, as in CI, where a skip would be a gate that
silently let everything through. pyrefly reads site-packages from the Python it is pointed at, so
every run names this one: an unactivated venv would otherwise hand it the system Python's packages,
and the answers change (numpy, PIL and python-pptx types)."""

import json
import os
import re
import shutil
import subprocess
from collections import Counter
from importlib.util import find_spec
from pathlib import Path

import pytest

from beamer2slides import google_auth, interpreter

try:
    import tomllib
except ImportError:  # 3.10: pytest brings tomli there
    import tomli as tomllib

SRC = Path(google_auth.__file__).parent   # (not resolved: see tests/test_gapi.py)
ROOT = SRC.parent.parent
PYPROJECT = ROOT / "pyproject.toml"

# The modules that check clean, as paths under src/beamer2slides. It only grows: a module whose
# last error is fixed, or a new one with none, must join (test_a_module_that_checks_clean_is_gated).
# The first 59 (2026-09-29) are the clean ones of 180, and ir.py.
GATED = (
    "__init__.py", "adopt_sync.py", "adopt_theme.py",
    "agent/__init__.py", "agent/auth.py", "agent/content.py", "agent/context.py",
    "agent/doc_tools.py", "agent/source_tools.py", "agent/tools.py", "agent/types.py",
    "agent/workspace.py",
    "checks.py", "classify.py", "classify_model.py", "debug.py",
    "devtools/__init__.py", "devtools/adopt_replay.py",
    "devtools/doc_world.py", "devtools/docs_bench.py", "devtools/fuzz_reach.py",
    "devtools/marked_content_torture.py", "devtools/platform_check.py",
    "devtools/preset_geometry.py", "devtools/preset_survey.py", "devtools/pure_bench.py",
    "devtools/readability.py", "devtools/readability_calib.py", "devtools/showcase/__init__.py",
    "devtools/showcase/__main__.py", "devtools/torture_kit.py", "devtools/visual_hunt.py",
    "faults.py", "fidelity.py", "fontfetch.py", "fontfiles.py", "fonts.py", "gapi.py",
    "google_auth.py", "ink.py", "interpreter.py", "ir.py", "labels.py", "net.py", "page_score.py",
    "paths.py",
    "pdf/pure/__init__.py", "pdf/pure/cie.py", "pdf/pure/cmyk_table.py", "pdf/pure/encodings.py",
    "pdf/pure/filters.py", "pdf/pure/foxit.py", "pdf/pure/ftgrays.py", "pdf/pure/psnames_data.py",
    "pdf/pure/raster.py", "pdf/pure/ttinterp.py", "pdf/pure/unicode_data.py", "pdf/wire.py",
    "playground/__init__.py", "playground/runner.py",
)

# The errors that are about a TypedDict whatever the code around them: a key it does not have or
# a required one missing (bad-typed-dict-key), an optional one read by subscript
# (not-required-key-access), a malformed definition (bad-typed-dict). Errors of other kinds count
# when they name a TypedDict ("... is not assignable to TypedDict key `shape`": a Literal value
# outside its kinds, a colour string where {color, width} goes).
TYPED_DICT_KINDS = {"bad-typed-dict", "bad-typed-dict-key", "not-required-key-access"}

# TypedDict errors that are known, by (module, kind): count. Lower a count when it drops (a stale
# entry fails). Empty since pdf/api.py's Drawing got a total base and Link became PageLink |
# UriLink (every backend always wrote those keys): keep it so.
KNOWN: dict[tuple[str, str], int] = {}

# The 688ebf4 read: `flip` is optional, and shape_requests read it by subscript.
PROBE = '''from typing import List, Literal, TypedDict


class _ShapeKeys(TypedDict):
    kind: Literal["shape"]
    box: List[float]


class Shape(_ShapeKeys, total=False):
    flip: bool


def flipped(el: Shape) -> bool:
    return el["flip"]
'''


def skip(reason: str):
    if os.environ.get("B2S_TYPECHECK_REQUIRED"):
        pytest.fail(f"the type check is required here and cannot run: {reason}")
    pytest.skip(reason)


def config() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def pinned() -> str:
    dev = config()["project"]["optional-dependencies"]["dev"]
    return next(re.fullmatch(r"pyrefly==(\S+)", d).group(1) for d in dev if d.startswith("pyrefly=="))


def pyrefly_command() -> list[str]:
    if not PYPROJECT.exists():
        skip("no pyproject.toml beside the package (the checker is configured there)")
    if find_spec("pyrefly") is not None:
        command = [interpreter.python(), "-m", "pyrefly"]
    elif shutil.which("pyrefly"):
        command = [shutil.which("pyrefly")]
    else:
        skip("pyrefly is not installed (pip install -e .[dev])")
    version = subprocess.run([*command, "--version"], capture_output=True, text=True, timeout=60,
                             env=interpreter.env()).stdout.split()
    if version[-1:] != [pinned()]:
        skip(f"pyrefly {' '.join(version[1:])} here, the gate is pinned to {pinned()}")
    return command


def check(command: list[str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*command, "check", "--python-interpreter-path", interpreter.python(),
                           "--summary=none", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", timeout=600, env=interpreter.env())


def module(path: str) -> str:
    """"src/beamer2slides/pdf/api.py" (or with backslashes, or absolute) -> "pdf/api.py"."""
    path = path.replace("\\", "/")
    return path[path.rindex("src/beamer2slides/") + len("src/beamer2slides/"):]


def line(e: dict) -> str:
    return f"{module(e['path'])}:{e['line']} [{e['name']}] {e['concise_description']}"


@pytest.fixture(scope="module")
def command():
    return pyrefly_command()


@pytest.fixture(scope="module")
def whole(command) -> list[dict]:
    """Every error in the package under the config's checks."""
    done = check(command, "--output-format", "json", "src/beamer2slides")
    assert done.stdout.strip(), done.stderr
    return [e for e in json.loads(done.stdout)["errors"] if e["severity"] == "error"]


def test_the_gated_modules_check_clean(whole):
    errors = [line(e) for e in whole if module(e["path"]) in set(GATED)]
    assert not errors, "\n".join(errors)


def test_a_module_that_checks_clean_is_gated(whole):
    """The ratchet: fixing a module's last error, or adding a module with none, puts it in the
    gate - so it stays clean."""
    present = {p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py") if "__pycache__" not in p.parts}
    failing = {module(e["path"]) for e in whole}
    assert not sorted(set(GATED) - present), "gated modules that no longer exist: take them out"
    assert not sorted(present - failing - set(GATED)), "these modules check clean: add them to GATED"


def test_no_typed_dict_error_anywhere(whole):
    """A TypedDict's contract holds in every module, not only the gated ones: an element type
    (ir.py) is broken wherever its producer or consumer lives."""
    typed = [e for e in whole if e["name"] in TYPED_DICT_KINDS or "TypedDict" in e["description"]]
    found = Counter((module(e["path"]), e["name"]) for e in typed)
    new = [line(e) for e in typed if (module(e["path"]), e["name"]) not in KNOWN]
    changed = [f"KNOWN[{m!r}, {kind!r}] is {n}, found {found[m, kind]}"
               for (m, kind), n in KNOWN.items() if found[m, kind] != n]
    assert not new + changed, "\n".join(new + changed)


def test_the_config_sees_an_optional_key_read_by_subscript(command, tmp_path):
    """A measurement that can see what it is for: 688ebf4's `el["flip"]` is an error under the
    config (pyrefly's own presets leave not-required-key-access off)."""
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    done = check(command, "--config", str(PYPROJECT), "--output-format", "json", str(probe))
    names = [e["name"] for e in json.loads(done.stdout)["errors"]]
    assert names == ["not-required-key-access"], done.stdout + done.stderr
