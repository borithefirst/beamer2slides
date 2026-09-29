"""beamer2slides' build backend: setuptools', building nothing that does not type-check.

Every way the package is built comes through here - `pip install .`, `pip install -e .`, a wheel, an
sdist, `pip install git+https://...` - and each first runs pyrefly over the package under
pyproject.toml's [tool.pyrefly] (docs/typing.md). An error or a warning that typecheck/baseline.json
does not hold (the errors older than the rule; the list only shrinks) fails the build with the
checker's own words. There is no switch to skip it: a build that skipped it would be the one
nobody checked.

The baseline is compared here, as a count, not by pyrefly's own `--baseline`: pyrefly lets one
entry excuse every error of the same path, column, kind and message, so a new bare `dict` written
at the column of an old one passed unseen, while retyping a line moved its old errors to other
columns, where they read as new. The check runs with an empty baseline, and in each file each kind
of error (its message) may occur at most as often as the baseline lists it.

The checker reads the types of what the package imports from the interpreter building it, so the
build's requirements ([build-system] requires) carry the dependencies too, pinned: every machine
gets the same answer.

The hooks' signatures, defaults included, are PEP 517's, not ours: a frontend calls them with the
arguments it has.

`python build_backend/beamer2slides_build.py prune` drops from the baseline what is fixed (never
adds); tests/test_typecheck.py runs the same comparison.

tests/ is a second target, with a baseline of its own (typecheck/tests_baseline.json, `prune
tests`): the suite is not built, so tests/test_typecheck.py is its gate, and it reads pytest's types,
pinned in [tool.beamer2slides.typecheck] tests-requires rather than among the build's requirements.
Checked apart from the package, a test calling a function whose signature changed is an error where
the call is, before the suite runs."""

import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Literal, Optional, Union

from setuptools import build_meta
from setuptools.build_meta import (get_requires_for_build_editable, get_requires_for_build_sdist,
                                   get_requires_for_build_wheel, prepare_metadata_for_build_editable,
                                   prepare_metadata_for_build_wheel)

ROOT = Path(__file__).parent.parent

# What is checked: the package (with build_backend/, as [tool.pyrefly] includes it), or the tests.
Target = Literal["package", "tests"]
BASELINES: dict[Target, Path] = {"package": ROOT / "typecheck" / "baseline.json",
                                 "tests": ROOT / "typecheck" / "tests_baseline.json"}
# The tests are handed to pyrefly by name, which replaces the config's includes; `.` makes them the
# package `tests`, whose relative imports then resolve.
ARGUMENTS: dict[Target, list[str]] = {"package": [],
                                      "tests": ["--config", "pyproject.toml", "--search-path", "src",
                                                "--search-path", ".", "tests"]}

ConfigSettings = Optional[dict[str, Union[str, list[str]]]]

# What an error is counted by: its file, kind and message. Not its line or column, which move when a
# line is edited or retyped: moving a legacy error is no new one, and a new one raises its count.
Key = tuple[str, str, str, str]


class TypeCheckFailed(Exception):
    pass


def key(d: dict[str, object]) -> Key:
    return (str(d["path"]).replace("\\", "/"), str(d["name"]), str(d["concise_description"]), str(d["severity"]))


def diagnostics(python: str, target: Target) -> list[dict[str, object]]:
    """Every diagnostic pyrefly has for the target, whatever its severity, with no baseline (the
    checker's own log lines, on stderr, are not diagnostics)."""
    with tempfile.TemporaryDirectory() as scratch:
        empty = Path(scratch) / "baseline.json"
        empty.write_text('{"errors": []}', encoding="utf-8")
        done = subprocess.run([python, "-m", "pyrefly", "check", "--python-interpreter-path", python,
                               "--baseline", str(empty), "--min-severity", "info", "--output-format", "json",
                               *ARGUMENTS[target]],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    try:
        found: list[dict[str, object]] = json.loads(done.stdout)["errors"]
    except (ValueError, KeyError) as e:
        raise TypeCheckFailed(f"pyrefly gave no report ({e}):\n{done.stdout}\n{done.stderr}") from e
    if done.returncode != 0 and not found:
        raise TypeCheckFailed(f"pyrefly failed without a diagnostic:\n{done.stderr}")
    return found


def baseline(target: Target) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = json.loads(BASELINES[target].read_text(encoding="utf-8"))["errors"]
    return entries


def new_errors(found: list[dict[str, object]], entries: list[dict[str, object]]) -> list[dict[str, object]]:
    """The diagnostics the baseline does not excuse: of each key, those past the baseline's count."""
    allowed = Counter(key(e) for e in entries)
    seen: Counter[Key] = Counter()
    out: list[dict[str, object]] = []
    for d in found:
        k = key(d)
        seen[k] += 1
        if seen[k] > allowed[k]:
            out.append(d)
    return out


def stale_entries(found: list[dict[str, object]], entries: list[dict[str, object]]) -> list[dict[str, object]]:
    """The baseline entries no diagnostic uses any more: fixed errors, to be pruned."""
    left = Counter(key(d) for d in found)
    out: list[dict[str, object]] = []
    for e in entries:
        k = key(e)
        if left[k]:
            left[k] -= 1
        else:
            out.append(e)
    return out


def type_check() -> None:
    fresh = new_errors(diagnostics(sys.executable, "package"), baseline("package"))
    if fresh:
        lines = [f"{d['path']}:{d['line']}: {d['severity']} [{d['name']}] {d['concise_description']}" for d in fresh]
        raise TypeCheckFailed("beamer2slides does not type-check, so it is not built (docs/typing.md):\n"
                              + "\n".join(lines))


def prune(target: Target) -> int:
    """Drops the fixed errors from the target's baseline, keeping its order; returns how many."""
    entries = baseline(target)
    stale = {id(e) for e in stale_entries(diagnostics(sys.executable, target), entries)}
    kept = [e for e in entries if id(e) not in stale]
    BASELINES[target].write_text(json.dumps({"errors": kept}, indent=2) + "\n", encoding="utf-8")
    return len(stale)


def build_wheel(wheel_directory: str, config_settings: ConfigSettings = None,
                metadata_directory: Optional[str] = None) -> str:
    type_check()
    return build_meta.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_editable(wheel_directory: str, config_settings: ConfigSettings = None,
                   metadata_directory: Optional[str] = None) -> str:
    type_check()
    return build_meta.build_editable(wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory: str, config_settings: ConfigSettings = None) -> str:
    type_check()
    return build_meta.build_sdist(sdist_directory, config_settings)


__all__ = ["build_editable", "build_sdist", "build_wheel", "get_requires_for_build_editable",
           "get_requires_for_build_sdist", "get_requires_for_build_wheel",
           "prepare_metadata_for_build_editable", "prepare_metadata_for_build_wheel"]


if __name__ == "__main__":
    if sys.argv[1:] == ["prune"]:
        n = prune("package")
        print(f"{n} fixed errors left the baseline; {len(baseline('package'))} remain "
              "(lower CEILING in tests/test_typecheck.py)")
    elif sys.argv[1:] == ["prune", "tests"]:
        n = prune("tests")
        print(f"{n} fixed errors left the tests' baseline; {len(baseline('tests'))} remain "
              "(lower TESTS_CEILING in tests/test_typecheck.py)")
    else:
        sys.exit("usage: python build_backend/beamer2slides_build.py prune [tests]")
