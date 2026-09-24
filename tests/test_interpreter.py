"""The Python a child process starts in when `sys.executable` is empty (Google's test runner)."""

import os
import subprocess
import sys

import pytest

from beamer2slides import interpreter


def test_this_interpreter_when_it_has_a_name(monkeypatch):
    monkeypatch.delenv(interpreter.ENV, raising=False)
    assert interpreter.python() == sys.executable
    assert interpreter.env({"A": "1"}) == {"A": "1"}          # nothing added for our own Python


def test_the_hosts_choice_wins(monkeypatch, tmp_path):
    chosen = tmp_path / "python-of-the-host"
    chosen.write_bytes(b"")
    monkeypatch.setenv(interpreter.ENV, str(chosen))
    assert interpreter.python() == str(chosen)
    monkeypatch.setenv(interpreter.ENV, str(tmp_path / "not-there"))   # a stale setting is passed over
    assert interpreter.python() == sys.executable


def test_an_empty_sys_executable_still_starts_a_child_that_imports_the_package(monkeypatch, tmp_path):
    """The runner leaves `sys.executable` empty; the child is another Python, which is told where
    this process found beamer2slides."""
    real = sys.executable
    monkeypatch.delenv(interpreter.ENV, raising=False)
    monkeypatch.setattr(sys, "executable", "")
    monkeypatch.setattr(sys, "_base_executable", "", raising=False)
    monkeypatch.setenv("PATH", os.path.dirname(real) + os.pathsep + os.environ.get("PATH", ""))
    python = interpreter.python()
    assert python and os.path.isfile(python)
    env = interpreter.env({k: v for k, v in os.environ.items() if k != "PYTHONPATH"} | {"PYTHONPATH": "extra"})
    paths = env["PYTHONPATH"].split(os.pathsep)
    assert paths[-1] == "extra" and len(paths) > 1
    done = subprocess.run([python, "-c", "import beamer2slides.interpreter; print('ok')"],
                          capture_output=True, text=True, env=env, cwd=tmp_path)
    assert done.stdout.strip() == "ok", done.stderr[-2000:]


def test_no_python_anywhere_says_what_to_set(monkeypatch):
    monkeypatch.delenv(interpreter.ENV, raising=False)
    monkeypatch.setattr(sys, "executable", "")
    monkeypatch.setattr(sys, "_base_executable", "", raising=False)
    monkeypatch.setattr(interpreter.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match=interpreter.ENV):
        interpreter.python()
