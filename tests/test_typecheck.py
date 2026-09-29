"""The type check's gate (docs/typing.md): the same check the build runs, plus the ratchet that makes
the legacy baseline only shrink.

The build (build_backend/beamer2slides_build.py) refuses to build a package pyrefly does not pass
under pyproject.toml's [tool.pyrefly]: strict preset, every warning an error, no suppression comment
honoured, and typecheck/baseline.json holding the errors older than the rule. This file runs that
check where the suite runs (a module that stops type-checking fails before a push, in seconds) and
adds what the build cannot say:
  * the baseline holds no stale entry: an error fixed is pruned from it (`python
    build_backend/beamer2slides_build.py prune`), so the list only shrinks, and CEILING below goes
    down with it (a count per error, not pyrefly's --baseline, which lets one entry excuse many);
  * the baseline never grows: it holds exactly CEILING entries, so adding one is a visible edit of
    this file, not a quiet regeneration;
  * no baseline entry is about a TypedDict (688ebf4: the element contract holds everywhere);
  * the config sees the bug classes it is for: 688ebf4's optional key read by subscript, a record
    built without one of its fields, a case left out of an exhaustive match.

The checker's answers depend on the types of the packages it reads, so they are checked against one
environment: the versions the build pins ([build-system] requires). Where this interpreter has other
versions the test skips - unless $B2S_TYPECHECK_REQUIRED is set, as in CI, where a skip would be a
gate that silently let everything through. Skipped too where the tree has no pyproject.toml (Google's
monorepo, which checks with its own tools)."""

import json
import os
import shutil
import subprocess
import tempfile
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec, module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import pytest
from packaging.requirements import Requirement

from beamer2slides import google_auth, interpreter

try:
    import tomllib
except ImportError:  # 3.10: pytest brings tomli there
    import tomli as tomllib

SRC = Path(google_auth.__file__).parent   # (not resolved: see tests/test_gapi.py)
ROOT = SRC.parent.parent
PYPROJECT = ROOT / "pyproject.toml"
BASELINE = ROOT / "typecheck" / "baseline.json"

# The legacy errors typecheck/baseline.json holds: lower it with every prune, never raise it.
CEILING = 7919

TYPED_DICT_KINDS = {"bad-typed-dict", "bad-typed-dict-key", "not-required-key-access"}

# Each probe is a bug class the config must see, with the error kinds it must give.
PROBES = {
    # 688ebf4: `flip` is optional, and shape_requests read it by subscript
    "optional_key": ('''from typing import List, Literal, TypedDict


class _ShapeKeys(TypedDict):
    kind: Literal["shape"]
    box: List[float]


class Shape(_ShapeKeys, total=False):
    flip: bool


def flipped(el: Shape) -> bool:
    return el["flip"]
''', ["not-required-key-access"]),
    # 688ebf4 again, as the code is meant to be written: a record built without one of its fields
    "missing_field": ('''from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class Shape:
    kind: str
    flip: bool


def shape() -> Shape:
    return Shape(kind="rect")
''', ["missing-argument"]),
    # a case added to a closed set and not handled
    "unhandled_case": ('''from typing import Literal

from beamer2slides.typing_compat import assert_never

Kind = Literal["text", "image", "table"]


def emitted(kind: Kind) -> str:
    if kind == "text":
        return "shape"
    elif kind == "image":
        return "picture"
    else:
        assert_never(kind)
''', ["bad-argument-type"]),
}


def skip(reason: str) -> None:
    if os.environ.get("B2S_TYPECHECK_REQUIRED"):
        pytest.fail(f"the type check is required here and cannot run: {reason}")
    pytest.skip(reason)


def config() -> dict[str, object]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def build_pins() -> list[Requirement]:
    """The build's pinned requirements that apply to this interpreter."""
    build = config()["build-system"]
    assert isinstance(build, dict)
    pins = [Requirement(r) for r in build["requires"]]
    return [r for r in pins if (r.marker is None or r.marker.evaluate()) and str(r.specifier).startswith("==")]


def environment_mismatches() -> list[str]:
    out = []
    for r in build_pins():
        try:
            here = version(r.name)
        except PackageNotFoundError:
            out.append(f"{r.name} is not installed (the build pins {r.specifier})")
            continue
        if not r.specifier.contains(here, prereleases=True):
            out.append(f"{r.name} {here} here, the build pins {r.specifier}")
    return out


def pyrefly_command() -> list[str]:
    if not PYPROJECT.exists():
        skip("no pyproject.toml beside the package (the checker is configured there)")
    mismatches = environment_mismatches()
    if mismatches:
        skip("the checker's answers depend on the versions it reads, and these are not the build's: "
             + "; ".join(mismatches) + " (pip install the [build-system] requires pins)")
    if find_spec("pyrefly") is not None:
        return [interpreter.python(), "-m", "pyrefly"]
    found = shutil.which("pyrefly")
    if found is None:
        skip("pyrefly is not installed (pip install -e .[dev])")
    return [str(found)]


def check(command: list[str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*command, "check", "--python-interpreter-path", interpreter.python(), *args],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=600,
                          env=interpreter.env())


@pytest.fixture(scope="module")
def command() -> list[str]:
    return pyrefly_command()


def backend() -> ModuleType:
    """build_backend/beamer2slides_build.py, whose comparison the build refuses by."""
    spec = spec_from_file_location("beamer2slides_build", ROOT / "build_backend" / "beamer2slides_build.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def found(command: list[str]) -> list[dict[str, object]]:
    """Every diagnostic, with no baseline (as the build asks for them)."""
    empty = Path(tempfile.mkdtemp()) / "baseline.json"
    empty.write_text('{"errors": []}', encoding="utf-8")
    done = check(command, "--baseline", str(empty), "--min-severity", "info", "--output-format", "json")
    assert done.stdout.strip(), done.stderr
    errors: list[dict[str, object]] = json.loads(done.stdout)["errors"]
    return errors


def test_the_package_type_checks_as_the_build_checks_it(found: list[dict[str, object]]) -> None:
    """Every diagnostic, whatever its severity, past what the baseline lists: the build's own refusal."""
    build = backend()
    fresh = build.new_errors(found, build.baseline())
    assert not fresh, "\n".join(f"{d['path']}:{d['line']} [{d['name']}] {d['concise_description']}" for d in fresh)


def test_a_baseline_entry_excuses_one_error_not_all_its_kind() -> None:
    """pyrefly's own --baseline lets one entry excuse every error of its path, column, kind and
    message: a new bare `dict` at an old one's column went through. The build counts instead, per file
    and message - so an old error a retyped line moved to another column is still the old one."""
    build = backend()
    old = {"path": "src/beamer2slides/m.py", "column": 5, "name": "implicit-any-type-argument",
           "concise_description": "Cannot determine `_KT`", "severity": "error"}
    again = dict(old, line=40)
    assert build.new_errors([dict(old, line=3), again], [old]) == [again]
    assert build.new_errors([dict(old, line=3, column=31)], [old]) == []
    assert build.stale_entries([], [old]) == [old]


def test_the_baseline_only_shrinks(found: list[dict[str, object]]) -> None:
    """An error fixed leaves the baseline (`python build_backend/beamer2slides_build.py prune`), and
    CEILING follows it down; nothing is ever added to it."""
    build = backend()
    stale = build.stale_entries(found, build.baseline())
    assert not stale, (f"the baseline holds {len(stale)} errors that are fixed: run `python "
                       "build_backend/beamer2slides_build.py prune` and lower CEILING")
    entries = json.loads(BASELINE.read_text(encoding="utf-8"))["errors"]
    assert len(entries) <= CEILING, (f"the baseline grew to {len(entries)} entries: new code type-checks, "
                                     "it is never added to the baseline")
    assert len(entries) == CEILING, f"the baseline shrank to {len(entries)}: lower CEILING to match"


def test_no_baseline_entry_is_about_a_typed_dict() -> None:
    """A TypedDict's contract holds in every module: an element type (ir.py) is broken wherever its
    producer or consumer lives, and none of that is legacy."""
    entries = json.loads(BASELINE.read_text(encoding="utf-8"))["errors"]
    typed = [e for e in entries if e["name"] in TYPED_DICT_KINDS or "TypedDict" in e["concise_description"]]
    assert not typed, typed


@pytest.mark.parametrize("probe", sorted(PROBES))
def test_the_config_sees_the_bug_class(command: list[str], tmp_path: Path, probe: str) -> None:
    """A measurement that can see what it is for: each probe holds one bug class the checker is
    configured to find, and it must find exactly that."""
    source, expected = PROBES[probe]
    path = tmp_path / f"{probe}.py"
    path.write_text(source, encoding="utf-8")
    empty = tmp_path / "baseline.json"
    empty.write_text('{"errors": []}', encoding="utf-8")
    done = check(command, "--config", str(PYPROJECT), "--baseline", str(empty), "--output-format", "json", str(path))
    names = [e["name"] for e in json.loads(done.stdout)["errors"]]
    assert names == expected, done.stdout + done.stderr
