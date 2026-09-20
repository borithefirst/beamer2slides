"""One journey, in a process of its own: the child `workbench.Workbench._journey` starts.

The whole protocol is three lines of JSON: a job on stdin, `{"progress": "<a line the library
printed>"}` out while it runs, and `{"result": {...}}` - a `Result` as `agent.types` serialises
one - at the end. Nothing else may reach stdout, so the real stdout is kept as a channel of its
own and `sys.stdout` is pointed at stderr: a stray `print` in a library being imported would
otherwise be read as protocol.

Why a subprocess at all is in `workbench.py`. What is here is the one thing that differs from
every other harness of the agent layer: **the credentials are the visitor's**, an access token
their own browser got from Google and handed over for this one journey. `auth.InjectedToken`
wants the authorized-user JSON, which carries a refresh token; the browser flow gives none by
design, and the host wants none. So `VisitorToken` is the third `GoogleAccess`: one token, no
refresh, nothing stored, gone when the process ends.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any

#: What the visitor's browser asked Google for. The same list the page signs in with
#: (`server.WEB_SCOPES`); a token cannot be widened here, so this only says what it is.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class VisitorToken:
    """An access token a visitor's browser got, for the length of one journey.

    Never written down, never refreshed (there is nothing to refresh with), and never printed:
    `describe` answers what a tool legitimately asks - is there access, whose kind - and
    nothing more.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def credentials(self) -> Any:
        from google.oauth2.credentials import Credentials
        return Credentials(token=self._token, scopes=SCOPES)

    def describe(self) -> dict:
        return {"available": True, "source": "the visitor's own Google sign-in", "scopes": list(SCOPES)}


def access(spec: dict):
    """Where this run's Google credentials come from, or `NoGoogle` when there are none."""
    from ..agent.auth import NoGoogle, default_access

    mode, token = spec.get("mode"), spec.get("token")
    if mode == "signin":
        return VisitorToken(token) if token else NoGoogle()
    if mode == "local":
        return default_access()
    return NoGoogle()


def main() -> int:
    # The protocol channel is the real stdout, taken before anything can print to it.
    channel = os.fdopen(os.dup(1), "w", encoding="utf-8", errors="replace", newline="\n")
    sys.stdout = sys.stderr
    lock = threading.Lock()   # the progress callback runs on whatever thread printed

    def emit(message: dict) -> None:
        with lock:
            channel.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
            channel.flush()

    job = json.loads(sys.stdin.read() or "{}")

    from ..agent.auth import NoGoogle
    from ..agent.context import ALL_ACTIONS, LOCAL_ONLY, AgentContext
    from ..agent.mcp import dispatch
    from ..agent.workspace import LocalWorkspace

    google = access(job.get("google") or {})
    ctx = AgentContext(workspace=LocalWorkspace(job["root"]),
                       google=google,
                       # A workspace with no account may still do everything local; the tools
                       # that need Google then refuse with `offline`, which says why.
                       allow=LOCAL_ONLY if isinstance(google, NoGoogle) else ALL_ACTIONS,
                       progress=lambda line: emit({"progress": line}))
    result = dispatch(ctx, job.get("tool") or "", job.get("args") or {})
    emit({"result": result.json()})
    return 0


if __name__ == "__main__":                                  # pragma: no cover - a subprocess
    raise SystemExit(main())
