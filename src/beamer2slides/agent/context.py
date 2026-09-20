"""The context a journey runs in, and the one wrapper every tool is written inside.

`AgentContext` is the whole of what a harness plugs in: where files are (`workspace`), where
Google access comes from (`google`), what the agent is allowed to do (`allow`), and where
progress should go while a journey that takes half a minute is running (`progress`). Nothing
else in the agent layer reads the environment or the filesystem on its own.

`@tool(...)` is the wrapper every journey is written inside. It does four things no tool should
have to repeat:

* **Serialises.** One journey at a time per process, because the library underneath is full of
  process-wide state that two journeys would share: `contextlib.redirect_stdout` is global,
  `pdf.use_backend` sets a module variable, `checks.convert_locally` monkey-patches
  `render.save_png`, and the pure backend's font mapper carries PDFium's own process-wide
  multiple-master blend, so even the *order* documents are opened in can change what is
  rendered. A harness that wants two journeys at once runs two processes.
* **Catches.** The library says no by raising `SystemExit` or `RebuildRefused` and says how by
  printing. Neither may cross this boundary: prints become the log, refusals become a `code`,
  and an unforeseen exception becomes `failed` rather than taking the harness down.
* **Gates.** `allow` is checked before any work, so a context that forbids writing to Google
  refuses the journey instead of discovering the policy halfway through a rebuild.
* **Supplies credentials.** Acquired up front, so a dead token is `needs_consent` in a
  millisecond rather than a traceback after a two-minute conversion, and injected into the
  library through `google_auth.use_provider` for the length of the call.
"""

from __future__ import annotations

import functools
import io
import time
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from .auth import GoogleAccess, NoGoogle, default_access
from .types import READS, READS_GOOGLE, WRITES, WRITES_GOOGLE, Artifact, Diagnostic, Refused, Result
from .workspace import LocalWorkspace, Workspace

_LOCK = RLock()  # one journey at a time in this process; see the module docstring

ALL_ACTIONS = frozenset({READS, READS_GOOGLE, WRITES, WRITES_GOOGLE})
READ_ONLY = frozenset({READS, READS_GOOGLE})
LOCAL_ONLY = frozenset({READS, WRITES})


@dataclass
class AgentContext:
    """Everything a harness differs on, in one object handed to every tool."""

    workspace: Workspace
    google: GoogleAccess = field(default_factory=default_access)
    allow: frozenset[str] = ALL_ACTIONS
    progress: Callable[[str], None] | None = None
    #: How many lines of a journey's own output to keep in `data["log"]`. The library prints a
    #: line per slide, which is useful when something went wrong and noise when it did not.
    log_lines: int = 40
    #: Whether an artifact comes back as a name alone (`"refs"`, the default) or with its own
    #: content in it (`"inline"`), which is what a harness with no filesystem reads. See
    #: `agent/content.py`; the caps are per artifact and per call.
    deliver: str = "refs"
    inline_limit: int = 0         # 0 = content.INLINE_LIMIT
    inline_budget: int = 0        # 0 = content.INLINE_BUDGET
    #: How a `{"url": ...}` argument is fetched. None means it is refused by name: this library
    #: never opens a socket to a host a model chose. A harness that wants URL inputs passes the
    #: client it already trusts, with its own allow-list.
    fetch: Callable[[str], bytes] | None = None

    @classmethod
    def local(cls, root: Path | str, **kw: Any) -> "AgentContext":
        """The common case: a directory on this machine and this machine's Google token."""
        return cls(workspace=LocalWorkspace(root), **kw)

    @classmethod
    def offline(cls, root: Path | str, **kw: Any) -> "AgentContext":
        """No Google, no writes to anyone's deck: what a benchmark's offline tier runs in."""
        kw.setdefault("google", NoGoogle())
        kw.setdefault("allow", LOCAL_ONLY)
        return cls(workspace=LocalWorkspace(root), **kw)

    @classmethod
    def detached(cls, **kw: Any) -> "AgentContext":
        """No path in or out: content comes in inline and artifacts come back with their own.

        For the harness that has no filesystem to name. The workspace underneath is a private
        temporary directory, because the library needs one; nothing outside this object ever
        learns where it is, and `close()` removes it. Use it as a context manager.
        """
        from .content import MemoryWorkspace

        kw.setdefault("deliver", "inline")
        return cls(workspace=MemoryWorkspace(), **kw)

    def permits(self, *actions: str) -> bool:
        return all(a in self.allow for a in actions)

    def close(self) -> None:
        """Release what the workspace holds. A no-op unless it is a temporary one."""
        closing = getattr(self.workspace, "close", None)
        if callable(closing):
            closing()

    def __enter__(self) -> "AgentContext":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class Job:
    """The journey in progress: what a tool fills in, and what the wrapper turns into a Result."""

    def __init__(self, tool: str, ctx: AgentContext) -> None:
        self.tool = tool
        self.ctx = ctx
        self.summary = ""
        self.data: dict[str, Any] = {}
        self.artifacts: list[Artifact] = []
        self.diagnostics: list[Diagnostic] = []
        self.next_steps: list[str] = []
        self.ok = True
        self.code: str | None = None
        self.seconds = 0.0
        self.log: list[str] = []

    # -- what a tool calls --------------------------------------------------------------

    def path(self, ref: str, *, write: bool = False) -> Path:
        return self.ctx.workspace.resolve(ref, write=write)

    def note(self, level: str, message: str, where: str = "") -> None:
        self.diagnostics.append(Diagnostic(level, message, where))

    def conflict(self, message: str, where: str = "") -> None:
        self.note("conflict", message, where)

    def warn(self, message: str, where: str = "") -> None:
        self.note("warning", message, where)

    def artifact(self, path: Path | str, kind: str, description: str = "") -> None:
        self.artifacts.append(Artifact(self.ctx.workspace.ref(path), kind, description))

    def suggest(self, *steps: str) -> None:
        self.next_steps.extend(s for s in steps if s not in self.next_steps)

    def credentials(self) -> Any:
        return self.ctx.google.credentials()

    def require(self, *actions: str) -> None:
        """Refuse now if the context does not allow what is about to happen.

        `@tool` declares the *least* a journey does, so that a read-only context can still run
        `deck_sync(dry_run=True)`. A body that is about to write for real says so here, before
        the first request, and gets the same `forbidden` refusal the gate would have given.
        """
        _gate(self, tuple(actions))

    # -- what the wrapper calls ---------------------------------------------------------

    @property
    def result(self) -> Result:
        data = dict(self.data)
        if self.log:
            data["log"] = self.log[-self.ctx.log_lines:]
        return Result(tool=self.tool, ok=self.ok, code=self.code, summary=self.summary,
                      data=data, artifacts=self.artifacts, diagnostics=self.diagnostics,
                      next_steps=self.next_steps, seconds=self.seconds)


class _Tee(io.TextIOBase):
    """Keeps what the library prints, and passes whole lines on to the harness as they come."""

    def __init__(self, job: Job) -> None:
        self._job = job
        self._partial = ""

    def write(self, text: str) -> int:
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._job.log.append(line)
            if self._job.ctx.progress:
                try:
                    self._job.ctx.progress(line)
                except Exception:                                  # a harness sink is not our problem
                    pass
        return len(text)

    def flush(self) -> None:
        if self._partial:
            self.write("\n")


def tool(name: str, needs: tuple[str, ...] = (READS,)):
    """Make a journey out of a function that takes a `Job` and fills it in.

    The decorated function is called `f(ctx, **arguments)` and returns a `Result`; it never
    raises. `needs` names what the journey will do (`types.READS`, `WRITES`, `READS_GOOGLE`,
    `WRITES_GOOGLE`), which decides both the policy check and whether credentials are fetched -
    both **before** the body runs, so a forbidden or unauthenticated journey does no work at
    all rather than stopping halfway through someone's deck.
    """

    def wrap(fn: Callable[..., None]) -> Callable[..., Result]:
        @functools.wraps(fn)
        def call(ctx: AgentContext, *args: Any, **kw: Any) -> Result:
            job = Job(name, ctx)
            started = time.time()
            with _LOCK:
                try:
                    # Inline content becomes a file in the workspace before anything else, so
                    # the journey underneath sees the ordinary ref it has always seen. Cheap
                    # and idempotent: a call whose arguments are all plain strings walks the
                    # dict once, and a ref that has already been materialised is one.
                    kw = _take_in(job, kw)
                    _gate(job, needs)
                    creds = job.credentials() if _wants_google(needs) else None
                    with redirect_stdout(_Tee(job)):
                        if creds is None:
                            fn(job, *args, **kw)
                        else:
                            from .. import google_auth
                            with google_auth.use_provider(lambda: creds):
                                fn(job, *args, **kw)
                except Refused as exc:
                    _refuse(job, exc.code, str(exc), exc.data)
                except FileNotFoundError as exc:
                    _refuse(job, "not_found", str(exc), {})
                except SystemExit as exc:
                    # The library's own way of saying no: guard's refusal, a sync with no base,
                    # an adopt onto an existing file. A tool that knows which refusal it is
                    # raises `Refused` with the code; this is the honest fallback for the rest.
                    said = str(exc.code) if exc.code not in (0, None) else "the library refused"
                    _refuse(job, "refused", said, {})
                except TypeError as exc:
                    _refuse(job, "bad_request" if "argument" in str(exc) else "failed",
                            f"{type(exc).__name__}: {exc}", {})
                except Exception as exc:
                    code = "rate_limited" if _is_quota(exc) else _library_code(exc)
                    _refuse(job, code, f"{type(exc).__name__}: {exc}", {})
                finally:
                    job.seconds = time.time() - started
            return _deliver(job.result, ctx)

        call.tool_name = name                                      # type: ignore[attr-defined]
        call.needs = needs                                         # type: ignore[attr-defined]
        call.body = fn                                             # type: ignore[attr-defined]
        return call

    return wrap


def _take_in(job: Job, arguments: dict[str, Any]) -> dict[str, Any]:
    """Materialise inline arguments, or refuse as this layer refuses anything else.

    Inside the `try`, so a malformed `base64` comes back as `bad_request` with the parameter
    named rather than as a traceback the harness has to catch.
    """
    from .content import is_content, take_in

    if not any(is_content(v) for v in arguments.values()):
        return arguments
    return take_in(job.ctx.workspace, arguments, job.ctx.fetch)


def _deliver(result: Result, ctx: AgentContext) -> Result:
    """Put the artifacts' content into them when the context asked for that.

    Outside the lock and outside the `try`: reading back what a journey already wrote is not
    part of the journey, and a file that vanished between the two is skipped rather than
    turning a finished conversion into a failure.
    """
    if ctx.deliver != "inline" or not result.artifacts:
        return result
    from . import content

    return content.deliver(ctx.workspace, result,
                           limit=ctx.inline_limit or content.INLINE_LIMIT,
                           budget=ctx.inline_budget or content.INLINE_BUDGET)


def _gate(job: Job, needs: tuple[str, ...]) -> None:
    missing = [a for a in needs if a not in job.ctx.allow]
    if not missing:
        return
    if all(a in (READS_GOOGLE, WRITES_GOOGLE) for a in missing) and not _has_google(job.ctx):
        # Policy and fact agree, and the fact is the more useful of the two: a context with no
        # account is not withholding permission, it has nothing to give. `AgentContext.offline`
        # is both at once, and would otherwise answer `forbidden` to a question about Google.
        raise Refused("offline",
                      f"{job.tool} needs Google and this context has none. Local journeys still "
                      f"work.", needs=list(needs))
    raise Refused("forbidden",
                  f"{job.tool} needs to {', '.join(missing)} and this context allows only "
                  f"{', '.join(sorted(job.ctx.allow)) or 'nothing'}.", needs=list(needs))


def _has_google(ctx: AgentContext) -> bool:
    try:
        return bool(ctx.google.describe().get("available"))
    except Exception:                                          # a source that cannot even say
        return False


def _wants_google(needs: tuple[str, ...]) -> bool:
    return READS_GOOGLE in needs or WRITES_GOOGLE in needs


def _refuse(job: Job, code: str, message: str, data: dict) -> None:
    job.ok = False
    job.code = code
    job.summary = message if not job.summary else f"{job.summary}\n{message}"
    job.data.update(data)


def _library_code(exc: Exception) -> str:
    """`guard.RebuildRefused` is the one exception type worth a code of its own."""
    if type(exc).__name__ == "RebuildRefused":
        return "deck_edited"
    return "failed"


def _is_quota(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status in (429, 403) and "quota" in str(exc).lower()
