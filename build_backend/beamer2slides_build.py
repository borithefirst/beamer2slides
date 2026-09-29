"""beamer2slides' build backend: setuptools', building nothing that does not type-check.

Every way the package is built comes through here - `pip install .`, `pip install -e .`, a wheel, an
sdist, `pip install git+https://...` - and each first runs pyrefly over the package under
pyproject.toml's [tool.pyrefly] (docs/typing.md). An error or a warning that typecheck/baseline.json
does not hold (the errors older than the rule; the list only shrinks) fails the build with the
checker's own words. There is no switch to skip it: a build that skipped it would be the one
nobody checked.

The checker reads the types of what the package imports from the interpreter building it, so the
build's requirements ([build-system] requires) carry the dependencies too, pinned: every machine
gets the same answer.

The hooks' signatures, defaults included, are PEP 517's, not ours: a frontend calls them with the
arguments it has."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional, Union

from setuptools import build_meta
from setuptools.build_meta import (get_requires_for_build_editable, get_requires_for_build_sdist,
                                   get_requires_for_build_wheel, prepare_metadata_for_build_editable,
                                   prepare_metadata_for_build_wheel)

ROOT = Path(__file__).parent.parent

ConfigSettings = Optional[dict[str, Union[str, list[str]]]]


class TypeCheckFailed(Exception):
    pass


def type_check() -> None:
    """pyrefly over the package, as tests/test_typecheck.py runs it; raises with every diagnostic
    it has, whatever its severity (the checker's own log lines, on stderr, are not diagnostics)."""
    done = subprocess.run([sys.executable, "-m", "pyrefly", "check", "--python-interpreter-path", sys.executable,
                           "--min-severity", "info", "--output-format", "json"],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    try:
        found: list[dict[str, object]] = json.loads(done.stdout)["errors"]
    except (ValueError, KeyError) as e:
        raise TypeCheckFailed(f"pyrefly gave no report ({e}):\n{done.stdout}\n{done.stderr}") from e
    if done.returncode != 0 or found:
        lines = [f"{d['path']}:{d['line']}: {d['severity']} [{d['name']}] {d['concise_description']}" for d in found]
        raise TypeCheckFailed("beamer2slides does not type-check, so it is not built (docs/typing.md):\n"
                              + "\n".join(lines or [done.stderr]))


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
