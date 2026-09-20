"""The tools over MCP: one stdio server a desktop agent can be pointed at.

MCP is the shape a harness wants when it is not written in Python - a subprocess speaking JSON-RPC
over stdin and stdout - and it is the only binding here that needs a third-party SDK. So the SDK
is kept at the edge: everything this server does apart from speaking the protocol is
`dispatch(ctx, name, arguments)`, which is ordinary Python, is what the tests exercise, and is
what any other binding (an HTTP endpoint, a harness embedding the library in-process) should call
too. `import mcp` happens inside `serve`, so this module imports and `dispatch` works on a machine
where the SDK was never installed.

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
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from . import schema
from .auth import NoGoogle, default_access
from .context import ALL_ACTIONS, LOCAL_ONLY, READ_ONLY, AgentContext
from .types import Refused, Result
from .workspace import LocalWorkspace

__all__ = ["MissingSDK", "dispatch", "build_context", "instructions", "tool_response",
           "serve", "main"]


class MissingSDK(RuntimeError):
    """`serve` was called on a machine with no `mcp` package. Carries the command to fix it."""

SERVER_NAME = "beamer2slides"
INSTRUCTIONS_URI = "b2s://instructions"

_NO_SDK = ("The MCP SDK is not installed in this interpreter, so the server cannot run. "
           "Install it with `pip install beamer2slides[mcp]` (or `pip install mcp`) and try "
           "again. Everything else in beamer2slides.agent works without it.")


# -- the part that is not the protocol -----------------------------------------------------

def dispatch(ctx: AgentContext, name: str, arguments: Mapping[str, Any] | None = None,
             tools: Mapping[str, Callable] | None = None) -> Result:
    """Run one named tool with one argument dict, and never raise.

    The whole of a binding that is not protocol: look the tool up, check the arguments against
    its published schema, call it. An unknown name and a bad argument dict come back as ordinary
    `bad_request` results, because a model that guessed wrong should read the refusal and try
    again rather than see the connection drop.
    """
    try:
        known = schema.registry(tools)
    except schema.SchemaError as exc:
        return _refusal(name, "failed", str(exc))

    fn = known.get(name)
    if fn is None:
        offer = ", ".join(sorted(known)) or "no tools at all"
        return _refusal(name, "bad_request",
                        f"There is no tool called {name!r}. This server has {offer}.",
                        {"tools": sorted(known)})
    try:
        cleaned = schema.validate(fn, arguments)
    except Refused as exc:
        return _refusal(name, exc.code, str(exc), exc.data)
    except schema.SchemaError as exc:                          # the tool itself is unpublishable
        return _refusal(name, "failed", str(exc))
    return fn(ctx, **cleaned)


def _refusal(tool: str, code: str, summary: str, data: Mapping[str, Any] | None = None) -> Result:
    # `data` is a mapping, not `**data`: a refusal's own data carries a `tool` key of its own.
    return Result(tool=tool, ok=False, code=code, summary=summary, data=dict(data or {}))


def build_context(root: str | Path | None = None, *, read_only: bool = False,
                  offline: bool = False, progress: Callable[[str], None] | None = None,
                  allow: frozenset[str] | None = None) -> AgentContext:
    """The context a server run works in.

    The workspace root is the argument, else `$B2S_AGENT_ROOT`, else the process's own folder -
    the last of which is what a desktop client gives a server it launched, so it is worth being
    explicit in the client's config. `--read-only` intersects the allowance with
    `context.READ_ONLY`, so the local journeys and the Google *reads* stay reachable and nothing
    can write a file or a deck; `--offline` intersects with `LOCAL_ONLY` and takes the
    credentials away as well, so a mistake cannot become a Google call.
    """
    where = Path(root) if root else Path(os.environ.get("B2S_AGENT_ROOT") or Path.cwd())
    permitted = ALL_ACTIONS if allow is None else frozenset(allow)
    if read_only:
        permitted &= READ_ONLY
    if offline:
        permitted &= LOCAL_ONLY
    return AgentContext(workspace=LocalWorkspace(where),
                        google=NoGoogle() if offline else default_access(),
                        allow=permitted, progress=progress)


def instructions(tools: Mapping[str, Callable] | None = None) -> str:
    """`agent.tools.INSTRUCTIONS`, or an honest stand-in if the registry has none yet."""
    if tools is None:
        try:
            from . import tools as _tools
            found = getattr(_tools, "INSTRUCTIONS", None)
            if isinstance(found, str) and found.strip():
                return found
        except ImportError:
            pass
    return ("beamer2slides converts beamer PDFs into editable Google Slides decks and merges "
            "later source changes back into decks people have edited. Read every tool's "
            "description before calling it: several of them write to a deck or a document "
            "someone may be looking at, and the library refuses rather than destroying work - "
            "a refusal carries a `code` to branch on and a `next_steps` list saying what to do.")


def tool_response(result: Result) -> tuple[list[dict], bool]:
    """One `Result` as MCP content blocks plus the `isError` flag.

    Plain dicts, so this is testable without the SDK; the binding turns them into the SDK's own
    `TextContent`. The whole result goes over as JSON - a client that shows only the text still
    shows the summary, the code and the next steps.
    """
    return [{"type": "text", "text": result.text()}], not result.ok


# -- the protocol binding ------------------------------------------------------------------

def serve(root: str | Path | None = None, *, read_only: bool = False, offline: bool = False,
          tools: Mapping[str, Callable] | None = None,
          allow: frozenset[str] | None = None) -> None:
    """Run the stdio server until the client closes it. Needs the `mcp` SDK.

    Progress lines (the library prints one per slide, and a conversion takes half a minute) are
    forwarded to the client as MCP log notifications when the session is available; a client
    that does not take them loses nothing but the ticking.
    """
    try:
        import anyio
        from mcp import types as mcp_types
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server
    except ImportError as exc:
        # Not SystemExit: a harness that embeds this must not be killed by a missing extra.
        raise MissingSDK(f"{_NO_SDK} ({exc})") from None

    known = schema.registry(tools)
    guide = instructions(tools)
    published = schema.all_schemas(known)                      # raises now if a tool is unpublishable

    server = _make_server(Server, guide)
    session: dict[str, Any] = {"current": None}

    def progress(line: str) -> None:                           # pragma: no cover - needs the SDK
        live = session.get("current")
        if live is None:
            return
        try:
            anyio.from_thread.run(live.send_log_message, "info", line, SERVER_NAME)
        except Exception:
            pass                                               # a client that will not listen

    ctx = build_context(root, read_only=read_only, offline=offline, allow=allow, progress=progress)

    @server.list_tools()
    async def list_tools() -> list[Any]:                       # pragma: no cover - needs the SDK
        return [mcp_types.Tool(name=s["name"], description=s["description"],
                               inputSchema=s["input_schema"]) for s in published]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None = None) -> list[Any]:
        # pragma: no cover - needs the SDK
        try:
            session["current"] = server.request_context.session
        except Exception:
            session["current"] = None
        result = await anyio.to_thread.run_sync(lambda: dispatch(ctx, name, arguments, known))
        session["current"] = None
        blocks, is_error = tool_response(result)
        content = [mcp_types.TextContent(**b) for b in blocks]
        if is_error:
            # The low-level server turns a raised exception into `isError: True`; the whole
            # Result rides along as the message, so nothing the model needs is lost.
            raise _ToolRefused(content[0].text)
        return content

    @server.list_resources()
    async def list_resources() -> list[Any]:                   # pragma: no cover - needs the SDK
        return [mcp_types.Resource(uri=INSTRUCTIONS_URI, name="beamer2slides instructions",
                                   description="The rules these tools have to be used by.",
                                   mimeType="text/markdown")]

    @server.read_resource()
    async def read_resource(uri: Any) -> str:                  # pragma: no cover - needs the SDK
        if str(uri) != INSTRUCTIONS_URI:
            raise ValueError(f"No such resource: {uri}")
        return guide

    async def run() -> None:                                   # pragma: no cover - needs the SDK
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)


class _ToolRefused(Exception):
    """Carries a refused `Result`'s JSON to the SDK, which turns it into `isError: True`."""


def _make_server(Server: Any, guide: str) -> Any:              # pragma: no cover - needs the SDK
    """`Server(name, instructions=...)`, falling back for SDK versions that have no such field."""
    try:
        return Server(SERVER_NAME, instructions=guide)
    except TypeError:
        return Server(SERVER_NAME)


def main(argv: list[str] | None = None) -> int:
    """`python -m beamer2slides.agent.mcp`."""
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
    try:
        serve(args.root, read_only=args.read_only, offline=args.offline)
    except MissingSDK as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":                                     # pragma: no cover - a subprocess
    raise SystemExit(main())
