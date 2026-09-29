"""The tools over MCP: one stdio server a desktop agent can be pointed at.

MCP is the shape a harness wants when it is not written in Python - a subprocess speaking JSON-RPC
over stdin and stdout - and it is the only binding here that needs a third-party SDK. So the SDK
is kept at the edge: everything this server does apart from speaking the protocol is
`dispatch(ctx, name, arguments, tools)`, which is ordinary Python, is what the tests exercise, and
is what any other binding (an HTTP endpoint, a harness embedding the library in-process) should
call too. The SDK is imported inside `serve`, so this module imports and `dispatch` works on a
machine where the SDK was never installed.

The SDK is not a dependency the checker can see, so what this server uses of it is written down
as Protocols (`_McpTypes`, `_ServerV1`, `_Runs`, ...) and each part is checked for that shape as
it is imported (`_sdk`): an SDK that has moved something is `MissingSDK` naming what, rather than
an `AttributeError` halfway through a client's first call.

Two things the protocol makes easy to get wrong, and which are done here on purpose:

* **The instructions travel with the tools.** The rules this library lives by - a rebuild never
  destroys deck edits, a frame's label is its identity, sync before you rebuild - are not in any
  schema, and an agent handed the tools without them will eventually spend someone's deck. They
  go in the server's `instructions` field *and* as a resource, since which of those a client
  shows the model is the client's choice, not ours.
* **A refusal is not a crash.** Every tool returns a `Result` and never raises, so `isError` on
  an MCP call means `ok is False` - a deck someone edited, a token that needs consent - and the
  content is still the whole `Result` as JSON. A client that only shows the text on an error
  still shows the model the code and the way forward.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypedDict, TypeVar, runtime_checkable

from ..json_types import JsonObject
from . import content, schema
from .auth import default_access, offline as no_google
from .context import ALL_ACTIONS, LOCAL_ONLY, READ_ONLY, AgentContext, Tool
from .types import Code, Need, Refused, Result, refusal
from .workspace import LocalWorkspace

__all__ = ["MissingSDK", "dispatch", "build_context", "instructions", "tool_response",
           "serve", "cli", "main"]


class MissingSDK(RuntimeError):
    """`serve` was called on a machine with no `mcp` package. Carries the command to fix it."""

SERVER_NAME = "beamer2slides"
INSTRUCTIONS_URI = "b2s://instructions"

_NO_SDK = ("The MCP SDK is not installed in this interpreter, so the server cannot run. "
           "Install it with `pip install beamer2slides[mcp]` (or `pip install mcp`) and try "
           "again. Everything else in beamer2slides.agent works without it.")


# -- the part that is not the protocol -----------------------------------------------------

def dispatch(ctx: AgentContext, name: str, arguments: Mapping[str, object] | None,
             tools: Mapping[str, Tool] | None) -> Result:
    """Run one named tool with one argument dict, and never raise.

    The whole of a binding that is not protocol: look the tool up, check the arguments against
    its published schema, call it. An unknown name and a bad argument dict come back as ordinary
    `bad_request` results, because a model that guessed wrong should read the refusal and try
    again rather than see the connection drop. `arguments` None is a call that sent none;
    `tools` None is the real registry (`schema.registry`).
    """
    try:
        known = schema.registry(tools)
    except schema.SchemaError as exc:
        return _refusal(name, "failed", str(exc), {})

    fn = known.get(name)
    if fn is None:
        offer = ", ".join(sorted(known)) or "no tools at all"
        return _refusal(name, "bad_request",
                        f"There is no tool called {name!r}. This server has {offer}.",
                        {"tools": list(sorted(known))})
    try:
        # Before the schema check, not after: a content dict is not a publishable parameter
        # type, and by the time `validate` sees the argument it has to be the string ref the
        # schema says it is. `@tool` does this again for a caller who came in another way;
        # both passes are idempotent, since what comes out is a plain ref.
        taken = content.take_in(ctx.workspace, arguments if arguments is not None else {},
                                ctx.fetch)
        cleaned = schema.validate(fn, taken)
    except Refused as exc:
        return _refusal(name, exc.code, str(exc), exc.data)
    except schema.SchemaError as exc:                          # the tool itself is unpublishable
        return _refusal(name, "failed", str(exc), {})
    return fn(ctx, **cleaned)


def _refusal(tool: str, code: Code, summary: str, data: JsonObject) -> Result:
    # `data` is a mapping, not `**data`: a refusal's own data carries a `tool` key of its own.
    return refusal(tool=tool, code=code, summary=summary, data=dict(data))


def build_context(root: str | Path | None, *, read_only: bool, offline: bool,
                  progress: Callable[[str], None] | None,
                  allow: frozenset[Need] | None) -> AgentContext:
    """The context a server run works in.

    The workspace root is the argument, else `$B2S_AGENT_ROOT`, else the process's own folder -
    the last of which is what a desktop client gives a server it launched, so it is worth being
    explicit in the client's config. `read_only` intersects the allowance with
    `context.READ_ONLY`, so the local journeys and the Google *reads* stay reachable and nothing
    can write a file or a deck; `offline` intersects with `LOCAL_ONLY` and takes the
    credentials away as well, so a mistake cannot become a Google call. `allow` None is every
    action.
    """
    where = Path(root) if root else Path(os.environ.get("B2S_AGENT_ROOT") or Path.cwd())
    permitted = ALL_ACTIONS if allow is None else frozenset(allow)
    if read_only:
        permitted &= READ_ONLY
    if offline:
        permitted &= LOCAL_ONLY
    return AgentContext(workspace=LocalWorkspace(where, ()),
                        google=no_google() if offline else default_access(),
                        allow=permitted, progress=progress)


def instructions(tools: Mapping[str, Tool] | None) -> str:
    """`agent.tools.INSTRUCTIONS` for the real registry (`tools` None), else an honest stand-in:
    a registry of someone else's tools is not the one those rules were written for."""
    if tools is None:
        try:
            from . import tools as _tools
            if _tools.INSTRUCTIONS.strip():
                return _tools.INSTRUCTIONS
        except ImportError:
            pass
    return ("beamer2slides converts beamer PDFs into editable Google Slides decks and merges "
            "later source changes back into decks people have edited. Read every tool's "
            "description before calling it: several of them write to a deck or a document "
            "someone may be looking at, and the library refuses rather than destroying work - "
            "a refusal carries a `code` to branch on and a `next_steps` list saying what to do.")


class TextBlock(TypedDict):
    """One MCP content block, as the protocol spells it."""

    type: Literal["text"]
    text: str


def tool_response(result: Result) -> tuple[list[TextBlock], bool]:
    """One `Result` as MCP content blocks plus the `isError` flag.

    Plain dicts, so this is testable without the SDK; the binding turns them into the SDK's own
    `TextContent`. The whole result goes over as JSON - a client that shows only the text still
    shows the summary, the code and the next steps.
    """
    return [{"type": "text", "text": result.text()}], not result.ok


# -- the SDK, as far as this server uses it ----------------------------------------------------

_T = TypeVar("_T")
#: An SDK model's constructor: keyword arguments in, the model out.
_Model = Callable[..., object]
#: What a handler is to the SDK: an async function of whatever the request carries.
_Handler = Callable[..., Awaitable[object]]


@runtime_checkable
class _McpTypes(Protocol):
    """`mcp.types`: the models this server builds."""

    Tool: _Model
    Resource: _Model
    TextContent: _Model
    ListToolsResult: _Model
    CallToolResult: _Model
    ListResourcesResult: _Model
    ReadResourceResult: _Model
    TextResourceContents: _Model


@runtime_checkable
class _ToThread(Protocol):
    def run_sync(self, fn: Callable[[], _T], /) -> Awaitable[_T]: ...


@runtime_checkable
class _FromThread(Protocol):
    def run(self, fn: Callable[..., Awaitable[object]], /, *args: object) -> object: ...


@runtime_checkable
class _Session(Protocol):
    """The live session a tool call arrived on, which takes progress as log messages."""

    def send_log_message(self, level: str, data: str, logger: str, /) -> Awaitable[None]: ...


@runtime_checkable
class _InRequest(Protocol):
    """A 1.x server inside a request: `request_context` (which raises outside one)."""

    request_context: object


@runtime_checkable
class _ServerV1(Protocol):
    """The 1.x low-level server: each handler is registered through a decorator."""

    def list_tools(self) -> Callable[[_Handler], object]: ...
    def call_tool(self) -> Callable[[_Handler], object]: ...
    def list_resources(self) -> Callable[[_Handler], object]: ...
    def read_resource(self) -> Callable[[_Handler], object]: ...


@runtime_checkable
class _Runs(Protocol):
    """What both generations' servers do once built: run over a pair of streams."""

    def create_initialization_options(self) -> object: ...
    def run(self, read: object, write: object, options: object, /) -> Awaitable[None]: ...


@runtime_checkable
class _CallParams(Protocol):
    name: str
    arguments: object


@runtime_checkable
class _ResourceParams(Protocol):
    uri: object


@dataclass(frozen=True, kw_only=True)
class _Sdk:
    """The parts of `mcp` and `anyio` this server uses, each checked for its shape."""

    types: _McpTypes
    server: Callable[..., object]          # `mcp.server.lowlevel.Server`, a class
    generation: Literal[1, 2]             # 1.x has `list_tools` on the class; 2.x takes handlers
    stdio_server: Callable[[], object]
    to_thread: _ToThread
    from_thread: _FromThread
    run: Callable[[Callable[[], Coroutine[object, object, None]]], object]


def _part(module: str, name: str | None) -> object:
    """`module` imported, or its attribute `name`; `MissingSDK` when either is not there."""
    try:
        found: object = importlib.import_module(module)
    except ImportError as exc:
        # Not SystemExit: a harness that embeds this must not be killed by a missing extra.
        raise MissingSDK(f"{_NO_SDK} ({exc})") from None
    if name is None:
        return found
    if not hasattr(found, name):
        raise MissingSDK(f"{_NO_SDK} ({module} has no {name}: an SDK this server does not know)")
    part: object = getattr(found, name)
    return part


def _unknown(what: str) -> MissingSDK:
    return MissingSDK(f"{_NO_SDK} ({what} is not the shape this server was written against)")


def _sdk() -> _Sdk:
    """Import the SDK and check each part this server uses against the shape it is used in."""
    to_thread = _part("anyio", "to_thread")
    from_thread = _part("anyio", "from_thread")
    run = _part("anyio", "run")
    types_mod = _part("mcp.types", None)
    server = _part("mcp.server.lowlevel", "Server")
    stdio_server = _part("mcp.server.stdio", "stdio_server")
    if not isinstance(types_mod, _McpTypes):
        raise _unknown("mcp.types")
    if not callable(server):
        raise _unknown("mcp.server.lowlevel.Server")
    if not isinstance(to_thread, _ToThread) or not isinstance(from_thread, _FromThread):
        raise _unknown("anyio")
    if not callable(run):
        raise _unknown("anyio.run")

    def stdio() -> object:
        if not callable(stdio_server):
            raise _unknown("mcp.server.stdio.stdio_server")
        opened: object = stdio_server()
        return opened

    def run_main(main: Callable[[], Coroutine[object, object, None]]) -> object:
        ran: object = run(main)
        return ran

    return _Sdk(types=types_mod, server=server,
                generation=1 if hasattr(server, "list_tools") else 2,
                stdio_server=stdio, to_thread=to_thread, from_thread=from_thread, run=run_main)


def _arguments(value: object) -> dict[str, object] | None:
    """A call's arguments as the SDK handed them: a mapping, or None for a call with none."""
    if not isinstance(value, Mapping):
        return None
    given: dict[str, object] = {str(k): v for k, v in value.items()}
    return given


class _Live:
    """The session the tool call now running arrived on, for `progress` to talk to."""

    def __init__(self) -> None:
        self.current: _Session | None = None


# -- the protocol binding ------------------------------------------------------------------

def serve(root: str | Path | None, *, read_only: bool, offline: bool,
          tools: Mapping[str, Tool] | None, allow: frozenset[Need] | None) -> None:
    """Run the stdio server until the client closes it. Needs the `mcp` SDK.

    `tools` None serves the real registry, `allow` None every action (then narrowed by
    `read_only` and `offline`, as `build_context` says).

    Progress lines (the library prints one per slide, and a conversion takes half a minute) are
    forwarded to the client as MCP log notifications when the session is available; a client
    that does not take them loses nothing but the ticking.
    """
    sdk = _sdk()
    mcp_types = sdk.types

    known = schema.registry(tools)
    guide = instructions(tools)
    published = schema.all_schemas(known)                      # raises now if a tool is unpublishable

    live = _Live()

    def progress(line: str) -> None:                           # pragma: no cover - needs the SDK
        session = live.current
        if session is None:
            return
        try:
            sdk.from_thread.run(session.send_log_message, "info", line, SERVER_NAME)
        except Exception:
            pass                                               # a client that will not listen

    ctx = build_context(root, read_only=read_only, offline=offline, allow=allow, progress=progress)

    async def call(name: str, arguments: object, session: object) -> Result:
        """One tool call, whichever SDK asked for it.

        `dispatch` is ordinary blocking Python - a conversion takes half a minute - so it goes to
        a worker thread and the session is held for `progress` while it runs.
        """
        given = _arguments(arguments)
        live.current = session if isinstance(session, _Session) else None
        try:
            return await sdk.to_thread.run_sync(lambda: dispatch(ctx, name, given, known))
        finally:
            live.current = None

    def blocks(result: Result) -> list[object]:
        said, _ = tool_response(result)
        return [mcp_types.TextContent(type=b["type"], text=b["text"]) for b in said]

    def tool_models(key: Literal["inputSchema", "input_schema"]) -> list[object]:
        return [mcp_types.Tool(**{"name": s["name"], "description": s["description"],
                                  key: schema.input_schema_json(s["input_schema"])})
                for s in published]

    resource_name = "beamer2slides instructions"
    resource_description = "The rules these tools have to be used by."

    if sdk.generation == 1:
        server = _make_server(sdk.server, guide, {})
        if not isinstance(server, _ServerV1):
            raise _unknown("the 1.x Server")

        async def list_tools() -> list[object]:
            return tool_models("inputSchema")

        async def call_tool(name: str, arguments: object) -> list[object]:
            try:
                context: object = server.request_context if isinstance(server, _InRequest) else None
            except Exception:                                  # asked outside a request
                context = None
            session: object = getattr(context, "session", None)
            result = await call(name, arguments, session)
            if not result.ok:
                # The low-level server turns a raised exception into `isError: True`; the whole
                # Result rides along as the message, so nothing the model needs is lost.
                raise _ToolRefused(result.text())
            return blocks(result)

        async def list_resources() -> list[object]:
            return [mcp_types.Resource(uri=INSTRUCTIONS_URI, name=resource_name,
                                       description=resource_description, mimeType="text/markdown")]

        async def read_resource(uri: object) -> str:
            if str(uri) != INSTRUCTIONS_URI:
                raise ValueError(f"No such resource: {uri}")
            return guide

        server.list_tools()(list_tools)
        server.call_tool()(call_tool)
        server.list_resources()(list_resources)
        server.read_resource()(read_resource)
    else:
        # SDK 2.x: the handlers are constructor arguments, each taking the request's own context
        # and its params and answering with the whole result model. The protocol is the same, so
        # this is a binding difference and nothing else - and a refusal says `is_error` itself
        # rather than being raised, which is what that Result meant in the first place.
        async def on_list_tools(rctx: object, params: object) -> object:
            return mcp_types.ListToolsResult(tools=tool_models("input_schema"))

        async def on_call_tool(rctx: object, params: object) -> object:
            if not isinstance(params, _CallParams):
                raise ValueError(f"A tool call with no name: {params!r}")
            session: object = getattr(rctx, "session", None)
            result = await call(params.name, params.arguments, session)
            return mcp_types.CallToolResult(content=blocks(result), is_error=not result.ok)

        async def on_list_resources(rctx: object, params: object) -> object:
            return mcp_types.ListResourcesResult(
                resources=[mcp_types.Resource(uri=INSTRUCTIONS_URI, name=resource_name,
                                              description=resource_description,
                                              mime_type="text/markdown")])

        async def on_read_resource(rctx: object, params: object) -> object:
            uri: object = params.uri if isinstance(params, _ResourceParams) else None
            if str(uri) != INSTRUCTIONS_URI:
                raise ValueError(f"No such resource: {uri}")
            return mcp_types.ReadResourceResult(
                contents=[mcp_types.TextResourceContents(uri=uri, mime_type="text/markdown",
                                                         text=guide)])

        server = _make_server(sdk.server, guide,
                              {"on_list_tools": on_list_tools, "on_call_tool": on_call_tool,
                               "on_list_resources": on_list_resources,
                               "on_read_resource": on_read_resource})

    async def run() -> None:                                   # pragma: no cover - needs the SDK
        if not isinstance(server, _Runs):
            raise _unknown("the Server")
        opened = sdk.stdio_server()
        if not isinstance(opened, AbstractAsyncContextManager):
            raise _unknown("mcp.server.stdio.stdio_server")
        async with opened as streams:
            pair: object = streams
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise _unknown("the stdio streams")
            read, write = pair
            await server.run(read, write, server.create_initialization_options())

    sdk.run(run)


class _ToolRefused(Exception):
    """Carries a refused `Result`'s JSON to the SDK, which turns it into `isError: True`."""


def _make_server(server: Callable[..., object], guide: str,
                 handlers: Mapping[str, _Handler]) -> object:
    """`Server(name, instructions=..., **handlers)`, giving up each field the SDK has not got.

    `instructions` is where half the point of this server lives (the rules travel with the tools),
    so it is asked for on every SDK; the handlers are 2.x's way of taking what 1.x takes through
    decorators, and are passed only on the branch that built them.
    """
    try:
        return server(SERVER_NAME, instructions=guide, **handlers)
    except TypeError:
        if handlers:                    # an SDK that takes neither is one neither branch fits
            raise
        return server(SERVER_NAME)


def cli(argv: list[str]) -> int:
    """`python -m beamer2slides.agent.mcp` with these arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m beamer2slides.agent.mcp",
        description="Serve the beamer2slides agent tools over MCP on stdin/stdout.")
    parser.add_argument("--root", default=None,
                        help="The workspace: the only folder the tools may write into. "
                             "Defaults to $B2S_AGENT_ROOT, else the current folder.")
    parser.add_argument("--read-only", action="store_true",
                        help="Allow reads only: no file and no deck or document is written.")
    parser.add_argument("--offline", action="store_true",
                        help="No Google at all; only the local journeys are reachable.")
    args = parser.parse_args(argv)
    root: object = args.root
    try:
        serve(root if isinstance(root, str) else None, read_only=bool(args.read_only),
              offline=bool(args.offline), tools=None, allow=None)
    except MissingSDK as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def main() -> int:
    """The `beamer2slides-mcp` console script: `cli` over this process's own arguments."""
    return cli(sys.argv[1:])


if __name__ == "__main__":                                     # pragma: no cover - a subprocess
    raise SystemExit(main())
