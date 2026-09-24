"""The Python a child process of the library runs in, where `sys.executable` says nothing.

The PDF sandbox's worker (`pdf.sandbox`) and the playground's journeys (`playground.workbench`)
are `python -m beamer2slides...` in a process of their own. `sys.executable` names that Python,
except where the interpreter is embedded or launched by a test runner that leaves it empty (Google's
does), and `[None, "-m", ...]` fails in `Popen` before anything useful is said.

So: `$B2S_PYTHON` when the host names one, else `sys.executable`, else the interpreter this one
was made from, else `python3` / `python` on the PATH (`python` first on Windows). A Python that is not this process's own does
not know where the package was imported from, so `env` hands it this process's `sys.path` in
`PYTHONPATH`; with `sys.executable` the environment is left as it is.
"""

from __future__ import annotations

import os
import shutil
import sys

ENV = "B2S_PYTHON"


def python() -> str:
    """The Python to start a child in. Raises RuntimeError when there is none to be found."""
    for candidate in (os.environ.get(ENV), sys.executable, getattr(sys, "_base_executable", None)):
        if candidate and os.path.isfile(candidate):
            return candidate
    # Windows: `python` first - a `python3` on the PATH there is often the Store's stub, which
    # prints an advert and exits.
    for name in ("python", "python3") if os.name == "nt" else ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(f"no Python to start a child process in: sys.executable is empty and neither "
                       f"python3 nor python is on the PATH; set ${ENV}")


def env(base: dict | None = None) -> dict:
    """`base` (default: this process's environment) for a child started with `python()`: when that
    is not this very interpreter, with this process's import path in front of `PYTHONPATH`."""
    out = dict(os.environ if base is None else base)
    if sys.executable and python() == sys.executable:
        return out
    ours = [p for p in sys.path if p and os.path.exists(p)]
    if out.get("PYTHONPATH"):
        ours.append(out["PYTHONPATH"])
    out["PYTHONPATH"] = os.pathsep.join(ours)
    return out
