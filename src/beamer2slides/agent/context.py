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
  library through `google_auth.use_provider` for the length of the call - and with them, where
  the context has one, the fetcher every download of Google's content goes through
  (`fetch_google_content`, `google_auth.use_fetcher`).

What it makes is a `Tool`: the journey callable as `tool(ctx, **arguments)`, carrying its name,
what it needs and the undecorated body the schema is read from - declared attributes of a type,
not attributes set on a function after the fact.
"""

from __future__ import annotations

import functools
import io
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from ..json_types import JsonObject
from ..typing_compat import override
from .auth import GoogleAccess, NoGoogle, default_access
from .types import (READS, READS_GOOGLE, WRITES, WRITES_GOOGLE, Artifact, Code, Diagnostic, Level,
                    Need, Refused, Result)
from .workspace import LocalWorkspace, Workspace

if TYPE_CHECKING:
    from google.auth.credentials import Credentials
    from typing_extensions import TypedDict, Unpack

_LOCK = RLock()  # one journey at a time in this process; see the module docstring

ALL_ACTIONS: frozenset[Need] = frozenset({READS, READS_GOOGLE, WRITES, WRITES_GOOGLE})
READ_ONLY: frozenset[Need] = frozenset({READS, READS_GOOGLE})
LOCAL_ONLY: frozenset[Need] = frozenset({READS, WRITES})

#: How an artifact comes back: a name alone, or with its own content in it (`content.deliver`).
Delivery = Literal["refs", "inline"]
#: A fetcher: a URL in, its bytes out, or an exception.
Fetch = Callable[[str], bytes]


@dataclass
class AgentContext:
    """Everything a harness differs on, in one object handed to every tool.

    The harness's constructor, and so the one record here that keeps defaults: a harness names
    only what it differs on (`AgentContext.local(root)`, `AgentContext(deliver="inline")`), and
    `docs/agent-tools.md` documents exactly that. Every field is typed, so what it names is
    checked all the same.
    """

    workspace: Workspace
    google: GoogleAccess = field(default_factory=default_access)
    allow: frozenset[Need] = ALL_ACTIONS
    progress: Callable[[str], None] | None = None
    #: How many lines of a journey's own output to keep in `data["log"]`. The library prints a
    #: line per slide, which is useful when something went wrong and noise when it did not.
    log_lines: int = 40
    #: Whether an artifact comes back as a name alone (`"refs"`, the default) or with its own
    #: content in it (`"inline"`), which is what a harness with no filesystem reads. See
    #: `agent/content.py`; the caps are per artifact and per call.
    deliver: Delivery = "refs"
    inline_limit: int = 0         # 0 = content.INLINE_LIMIT
    inline_budget: int = 0        # 0 = content.INLINE_BUDGET
    #: How a `{"url": ...}` argument is fetched. None means it is refused by name: this library
    #: never opens a socket to a host a model chose. A harness that wants URL inputs passes the
    #: client it already trusts, with its own allow-list.
    fetch: Fetch | None = None
    #: How the library downloads what Google answered with - a deck's pictures (signed into
    #: every sync base, so a plain conversion downloads them), slide thumbnails, a Doc's inserted
    #: pictures - and the original of a picture inserted by URL. None: `urllib`, as at a terminal.
    #: A backend whose egress must go through its own reviewed client sets it; installed through
    #: `google_auth.use_fetcher` for the length of each call (`net`). Deliberately not `fetch`:
    #: that one fetches a *model's* URL, and a harness that refuses those must not have to open
    #: that door merely to download pictures.
    fetch_google_content: Fetch | None = None
    #: A local copy of github.com/google/fonts (the folder holding `ofl/`, `apache/`, `ufl/`):
    #: `deck_adopt` reads the deck's fonts from it and downloads none (`fontfetch.use_source`).
    #: None: they are downloaded from GitHub through `fetch_google_content` (or urllib), and a
    #: fetcher that refuses GitHub means the deck is set in stand-ins, which the result names.
    font_source: str | Path | None = None
    #: Where every file the library creates in Drive goes (`drive_folder`): `"auto"` for a
    #: "beamer2slides" folder of the app's own, `"none"` for My Drive's root and a base or backup
    #: beside its file, or a folder id the app can see. None: `$B2S_DRIVE_FOLDER`, else `"auto"`.
    drive_folder: str | None = None

    @property
    def ephemeral(self) -> bool:
        """Whether the workspace goes away with the call (`detached`): a file kept there as the
        way back before a destructive write would be deleted with it."""
        return isinstance(self.workspace, _Ephemeral) and self.workspace.ephemeral

    @classmethod
    def local(cls, root: Path | str, **kw: Unpack[ContextOptions]) -> AgentContext:
        """The common case: a directory on this machine and this machine's Google token."""
        return cls(workspace=LocalWorkspace(root, ()), **kw)

    @classmethod
    def offline(cls, root: Path | str, **kw: Unpack[ContextOptions]) -> AgentContext:
        """No Google, no writes to anyone's deck: what a benchmark's offline tier runs in."""
        kw.setdefault("google", NoGoogle("offline", None))
        kw.setdefault("allow", LOCAL_ONLY)
        return cls(workspace=LocalWorkspace(root, ()), **kw)

    @classmethod
    def detached(cls, **kw: Unpack[ContextOptions]) -> AgentContext:
        """No path in or out: content comes in inline and artifacts come back with their own.

        For the harness that has no filesystem to name. The workspace underneath is a private
        temporary directory, because the library needs one; nothing outside this object ever
        learns where it is, and `close()` removes it. Use it as a context manager.
        """
        from .content import MemoryWorkspace

        kw.setdefault("deliver", "inline")
        return cls(workspace=MemoryWorkspace("b2s-agent-"), **kw)

    def permits(self, *actions: Need) -> bool:
        return all(a in self.allow for a in actions)

    def close(self) -> None:
        """Release what the workspace holds. A no-op unless it is a temporary one."""
        if isinstance(self.workspace, _Closes):
            self.workspace.close()

    def __enter__(self) -> AgentContext:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


if TYPE_CHECKING:
    class ContextOptions(TypedDict, total=False, closed=True):
        """What `AgentContext.local` / `offline` / `detached` take besides the workspace: its
        fields, and nothing else (closed, so the checker lets it be unpacked into the
        constructor). For the checker only: it is never made at runtime."""

        google: GoogleAccess
        allow: frozenset[Need]
        progress: Callable[[str], None] | None
        log_lines: int
        deliver: Delivery
        inline_limit: int
        inline_budget: int
        fetch: Fetch | None
        fetch_google_content: Fetch | None
        font_source: str | Path | None
        drive_folder: str | None


@runtime_checkable
class _Ephemeral(Protocol):
    """A workspace that says whether it outlives the call (`content.MemoryWorkspace`)."""

    ephemeral: bool


@runtime_checkable
class _Closes(Protocol):
    def close(self) -> None: ...


class Job:
    """The journey in progress: what a tool fills in, and what the wrapper turns into a Result."""

    def __init__(self, tool: str, ctx: AgentContext) -> None:
        self.tool = tool
        self.ctx = ctx
        self.summary = ""
        self.data: JsonObject = {}
        self.artifacts: list[Artifact] = []
        self.diagnostics: list[Diagnostic] = []
        self.next_steps: list[str] = []
        self.ok = True
        self.code: Code | None = None
        self.seconds = 0.0
        self.log: list[str] = []

    # -- what a tool calls --------------------------------------------------------------

    def path(self, ref: str, *, write: bool) -> Path:
        return self.ctx.workspace.resolve(ref, write=write)

    def note(self, level: Level, message: str, where: str) -> None:
        """Say something that is not the result; `where` is "" when it is about no one place."""
        self.diagnostics.append(Diagnostic(level=level, message=message, where=where))

    def conflict(self, message: str, where: str) -> None:
        self.note("conflict", message, where)

    def warn(self, message: str, where: str) -> None:
        self.note("warning", message, where)

    def artifact(self, path: Path | str, kind: str, description: str) -> None:
        self.artifacts.append(Artifact.named(ref=self.ctx.workspace.ref(path), kind=kind,
                                             description=description))

    def suggest(self, *steps: str) -> None:
        self.next_steps.extend(s for s in steps if s not in self.next_steps)

    def credentials(self) -> Credentials:
        return self.ctx.google.credentials()

    def require(self, *actions: Need) -> None:
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
            data["log"] = list(self.log[-self.ctx.log_lines:])
        return Result(tool=self.tool, ok=self.ok, code=self.code, summary=self.summary,
                      data=data, artifacts=self.artifacts, diagnostics=self.diagnostics,
                      next_steps=self.next_steps, seconds=self.seconds)


class _Tee(io.TextIOBase):
    """Keeps what the library prints, and passes whole lines on to the harness as they come."""

    def __init__(self, job: Job) -> None:
        self._job = job
        self._partial = ""

    @override
    def write(self, text: str) -> int:
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._job.log.append(line)
            progress = self._job.ctx.progress
            if progress:
                try:
                    progress(line)
                except Exception:                                  # a harness sink is not our problem
                    pass
        return len(text)

    @override
    def flush(self) -> None:
        if self._partial:
            self.write("\n")


#: `local(job, arguments)`: whether this call makes no Google call at all.
LocalTest = Callable[[Job, Mapping[str, object]], bool]


class Journey(Protocol):
    """What a registry maps a name to: called with a context and keyword arguments, answering a
    `Result`. A `Tool` is one; a benchmark's scripted fake is another."""

    def __call__(self, ctx: AgentContext, /, **arguments: object) -> Result: ...


class Tool:
    """A journey: `tool(ctx, **arguments) -> Result`, never raising.

    `tool_name` is the name it is published under, `needs` what it does to the world, `body` the
    function it was written as (the schema is read from its signature and docstring), and `local`
    the test for a call that needs no Google. It wraps `body` the way `functools.wraps` would, so
    `inspect.signature` and `typing.get_type_hints` read the body through it.
    """

    def __init__(self, *, name: str, needs: tuple[Need, ...], local: LocalTest | None,
                 body: Callable[..., None]) -> None:
        self.tool_name = name
        self.needs = needs
        self.local = local
        self.body = body
        functools.update_wrapper(self, body)

    @override
    def __repr__(self) -> str:
        return f"<tool {self.tool_name}>"

    def __call__(self, ctx: AgentContext, *args: object, **kw: object) -> Result:
        name, needs, local = self.tool_name, self.needs, self.local
        job = Job(name, ctx)
        started = time.time()
        with _LOCK:
            try:
                # Inline content becomes a file in the workspace before anything else, so the
                # journey underneath sees the ordinary ref it has always seen. Cheap and
                # idempotent: a call whose arguments are all plain strings walks the dict once,
                # and a ref that has already been materialised is one.
                kw = _take_in(job, kw)
                offline = local is not None and local(job, kw)
                wanted = tuple(a for a in needs if a not in (READS_GOOGLE, WRITES_GOOGLE)) if offline else needs
                _gate(job, wanted)
                creds = job.credentials() if _wants_google(wanted) else None
                with redirect_stdout(_Tee(job)), ExitStack() as hooks:
                    if creds is not None or offline or ctx.fetch_google_content is not None:
                        from .. import google_auth
                        if creds is not None:
                            hooks.enter_context(google_auth.use_provider(_giving(creds)))
                        elif offline:
                            hooks.enter_context(google_auth.use_provider(_no_google(name)))
                        if ctx.fetch_google_content is not None:
                            hooks.enter_context(google_auth.use_fetcher(ctx.fetch_google_content))
                    if ctx.font_source:
                        from .. import fontfetch
                        hooks.enter_context(fontfetch.use_source(ctx.font_source))
                    if ctx.drive_folder:
                        from .. import drive_folder
                        hooks.enter_context(drive_folder.use_folder(ctx.drive_folder))
                    self.body(job, *args, **kw)
            except Refused as exc:
                _refuse(job, exc.code, str(exc), exc.data)
            except FileNotFoundError as exc:
                _refuse(job, "not_found", str(exc), {})
            except SystemExit as exc:
                # The library's own way of saying no: guard's refusal, a sync with no base, an
                # adopt onto an existing file. A tool that knows which refusal it is raises
                # `Refused` with the code; this is the honest fallback for the rest.
                said = str(exc.code) if exc.code not in (0, None) else "the library refused"
                _refuse(job, "refused", said, {})
            except TypeError as exc:
                if "argument" in str(exc):
                    _refuse(job, "bad_request", f"{type(exc).__name__}: {exc}", {})
                else:
                    _refuse(job, "failed", *_unforeseen(exc))
            except Exception as exc:
                code = "rate_limited" if _is_quota(exc) else _library_code(exc)
                if code == "failed":
                    _refuse(job, code, *_unforeseen(exc))
                else:
                    _refuse(job, code, f"{type(exc).__name__}: {exc}", {})
            finally:
                job.seconds = time.time() - started
        return _deliver(job.result, ctx)


def tool(name: str, *, needs: tuple[Need, ...],
         local: LocalTest | None) -> Callable[[Callable[..., None]], Tool]:
    """Make a journey out of a function that takes a `Job` and fills it in.

    The decorated function is called `f(ctx, **arguments)` and returns a `Result`; it never
    raises. `needs` names what the journey will do (`types.READS`, `WRITES`, `READS_GOOGLE`,
    `WRITES_GOOGLE`), which decides both the policy check and whether credentials are fetched -
    both **before** the body runs, so a forbidden or unauthenticated journey does no work at
    all rather than stopping halfway through someone's deck.

    `local(job, arguments)`: True for a call that makes no Google call at all (`deck_adopt` of a
    deck handed over as files). Such a call is gated on `needs` without the Google actions, fetches no
    credentials, and runs with a provider that refuses, so a Google call it did make would be a
    refusal, not a quiet use of whatever token the machine has. None: every call may need Google.
    """

    def wrap(fn: Callable[..., None]) -> Tool:
        return Tool(name=name, needs=needs, local=local, body=fn)

    return wrap


def _take_in(job: Job, arguments: dict[str, object]) -> dict[str, object]:
    """Materialise inline arguments, or refuse as this layer refuses anything else.

    Inside the `try`, so a malformed `base64` comes back as `bad_request` with the parameter
    named rather than as a traceback the harness has to catch.
    """
    from .content import holds_content, take_in

    if not any(holds_content(v) for v in arguments.values()):
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


def _gate(job: Job, needs: tuple[Need, ...]) -> None:
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


def _giving(creds: Credentials) -> Callable[[], Credentials]:
    def give() -> Credentials:
        return creds
    return give


def _no_google(name: str) -> Callable[[], Credentials]:
    def refuse() -> Credentials:
        raise Refused("offline", f"{name} was called to run without Google here, and something in "
                                 f"it asked for Google anyway.")
    return refuse


def _wants_google(needs: tuple[Need, ...]) -> bool:
    return READS_GOOGLE in needs or WRITES_GOOGLE in needs


def _refuse(job: Job, code: Code, message: str, data: JsonObject) -> None:
    job.ok = False
    job.code = code
    job.summary = message if not job.summary else f"{job.summary}\n{message}"
    job.data.update(data)


TRACEBACK_LIMIT = 8000


def _unforeseen(exc: BaseException) -> tuple[str, JsonObject]:
    """A failure nobody foresaw, said with where it happened: `data["where"]` is the innermost
    frame, `data["traceback"]` the stack (paths from the package down, capped). A bare
    `StopIteration` would otherwise reach the caller as "StopIteration: " and nothing else."""
    import traceback

    def short(path: str) -> str:
        path = path.replace("\\", "/")
        cut = path.rfind("/beamer2slides/")
        return path[cut + 1:] if cut >= 0 else path.rsplit("/", 1)[-1]

    frames = traceback.extract_tb(exc.__traceback__)
    where = f"{short(frames[-1].filename)}:{frames[-1].lineno} in {frames[-1].name}" if frames else ""
    lines = [f"{short(f.filename)}:{f.lineno} in {f.name}" + (f"\n    {f.line}" if f.line else "") for f in frames]
    lines.append("".join(traceback.format_exception_only(type(exc), exc)).strip())
    stack = "\n".join(lines)
    if len(stack) > TRACEBACK_LIMIT:
        stack = "...\n" + stack[-TRACEBACK_LIMIT:]
    said = str(exc) or (f"(no message, at {where})" if where else "(no message)")
    return f"{type(exc).__name__}: {said}", {"where": where, "traceback": stack}


def _library_code(exc: Exception) -> Code:
    """`guard.RebuildRefused` is the one exception type worth a code of its own."""
    if type(exc).__name__ == "RebuildRefused":
        return "deck_edited"
    return "failed"


def _is_quota(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status in (429, 403) and "quota" in str(exc).lower()
