"""The registry: every journey as a tool, and the guide that has to travel with them.

A harness publishing these should publish `INSTRUCTIONS` with them. The schemas say what the
arguments are; they cannot say that a rebuild destroys a deck somebody edited, that a frame's
label is the only part of its identity that survives compiling, or that an open comment on a
Google Doc is invisible to the merge. An agent given the tools without the rules will eventually
do the one thing this library exists to prevent.

`b2s_status` lives here rather than in one of the journey modules because it is the only tool
that looks at all of them at once.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Callable

from . import deck_tools, doc_tools, source_tools
from .context import Job, tool
from .types import READS, Result

# How many of a kind to look at before saying "and more": a workspace may be someone's whole
# talks folder, and an agent does not need every file in it to decide what to do next.
SURVEY_LIMIT = 40


def _collect(*modules: Any) -> dict[str, Callable[..., Result]]:
    found: dict[str, Callable[..., Result]] = {}
    for module in modules:
        for name in dir(module):
            fn = getattr(module, name)
            if callable(fn) and getattr(fn, "tool_name", None):
                if fn.tool_name in found and found[fn.tool_name] is not fn:
                    raise RuntimeError(f"two tools are called {fn.tool_name}")
                found[fn.tool_name] = fn
    return found


@tool("b2s_status", needs=(READS,))
def b2s_status(j: Job,
               out: Annotated[str | None, "A single conversion folder to report on, instead of "
                                          "surveying the whole workspace."] = None,
               ) -> None:
    """What is in this workspace and what state it is in: the cheap first call.

    Lists the talks, sources and documents it can see, which of them have been converted, which
    have a sync base (and so can be merged rather than rebuilt), and whether Google is reachable
    at all - without making a single Google call, so it costs nothing and cannot fail on an
    expired token. Start here when you do not already know what you are looking at.
    """
    ws = j.ctx.workspace
    j.data["workspace"] = str(ws.root)
    j.data["allows"] = sorted(j.ctx.allow)
    j.data["google"] = access = j.ctx.google.describe()
    j.data["instructions_available"] = True

    folders = [j.path(out)] if out else _conversion_folders(ws)
    j.data["conversions"] = decks = [_conversion(ws, f) for f in folders[:SURVEY_LIMIT]]
    j.data["sources"] = sources = _sources(ws)
    j.data["documents"] = documents = _documents(ws)
    j.data["pdfs"] = pdfs = [r for r in ws.glob("*.pdf")][:SURVEY_LIMIT]

    parts = [f"{len(decks)} converted deck(s), {len(sources)} LaTeX source(s), "
             f"{len(documents)} canonical document file(s) and {len(pdfs)} loose PDF(s) "
             f"in {ws.root}."]
    if not access.get("available"):
        reason = access.get("reason", "no Google access")
        parts.append(f"Google is not reachable ({reason}); local journeys - deck_inspect, "
                     f"tex_label, tex_converge - still work.")
        if access.get("fix"):
            parts.append(access["fix"])
        if access.get("command"):
            parts.append(f"A person has to run `{access['command']}` at a terminal to fix it.")
        j.warn(f"Google is not reachable: {reason}")
    elif access.get("expired"):
        # `available` is true on a refresh token alone, and an expired access token is the
        # ordinary state between calls - the next journey refreshes it. Saying "good until
        # <a time already past>" reads like a problem, and an agent that believes it goes
        # looking for consent nobody needs to give.
        parts.append("The Google access token has expired and will be refreshed on the next "
                     "call; no consent is needed unless that refresh is refused.")
    elif access.get("expires"):
        parts.append(f"Google access is good until {access['expires']}.")

    unbased = [d["out"] for d in decks if d["converted"] and not d["has_base"]]
    if unbased:
        j.warn("A deck with no sync base can only be rebuilt, never merged: "
               + ", ".join(unbased[:3]))
    j.summary = " ".join(parts)

    if decks:
        j.suggest("deck_sync with dry_run=True to see what a recompiled source would change")
    if pdfs and not decks:
        j.suggest("deck_inspect on a PDF to see what it would convert to")
    if sources:
        j.suggest("tex_label to check that every frame carries a label")


def _conversion_folders(ws) -> list[Path]:
    root = ws.root / "out"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "deck.json").exists())


def _conversion(ws, folder: Path) -> dict:
    """One out folder, read from its own files only - nothing here talks to Drive."""
    state: dict[str, Any] = {"out": ws.ref(folder), "name": folder.name,
                             "classified": (folder / "deck.json").exists(),
                             "converted": (folder / "emit.json").exists(),
                             "has_base": (folder / "sync" / "base.json").exists(),
                             "has_backups": (folder / "backups" / "backups.json").exists()}
    emit = folder / "emit.json"
    if emit.exists():
        try:
            data = json.loads(emit.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state["unreadable"] = "emit.json could not be read"
            return state
        for key in ("url", "presentationId", "title"):
            if data.get(key):
                state[key] = data[key]
        if isinstance(data.get("slides"), list):
            state["slides"] = len(data["slides"])
    if state["classified"] and "slides" not in state:
        try:
            deck = json.loads((folder / "deck.json").read_text(encoding="utf-8"))
            state["slides"] = len(deck.get("slides", []))
        except (OSError, ValueError):
            pass
    return state


def _sources(ws) -> list[dict]:
    out = []
    for ref in ws.glob("**/*.tex"):
        if ref.startswith("out/") or "/out/" in ref or "/.b2s/" in ref:
            continue
        path = ws.resolve(ref)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\\begin{frame}" not in text and "\\begin{document}" not in text:
            continue
        out.append({"tex": ref,
                    "frames": text.count("\\begin{frame}"),
                    "labels": text.count("label="),
                    "beamer": "beamer" in text[:4000]})
        if len(out) >= SURVEY_LIMIT:
            break
    return out


def _documents(ws) -> list[dict]:
    out = []
    for ref in ws.glob("**/*.html"):
        if "/.b2s/" in ref or ref.startswith(".b2s/"):
            continue
        path = ws.resolve(ref)
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        pushed = "b2s-document" in head
        stem = Path(ref).stem
        base = path.parent / ".b2s" / f"{stem}.base.json"
        out.append({"file": ref, "pushed": pushed, "has_base": base.exists()})
        if len(out) >= SURVEY_LIMIT:
            break
    return out


def instructions() -> str:
    """The guide, read from package data so a wheel or a zip import finds it too."""
    return (resources.files("beamer2slides.agent") / "INSTRUCTIONS.md").read_text(encoding="utf-8")


INSTRUCTIONS = instructions()

#: The order of operations of INSTRUCTIONS.md, which is also the order a caller meets the tools
#: in: a model's tool list, the MCP catalogue and the workbench's dropdown all read top to
#: bottom, and what they read there is advice. Iterating the modules put `deck_convert` above
#: `deck_inspect` and `doc_adopt` above `doc_push` - the two orders this file spends a section
#: telling people not to follow. `_collect` stays the truth about *which* tools exist, so a
#: journey added to a module and forgotten here fails loudly instead of sorting itself last.
ORDER = ("b2s_status", "deck_inspect", "tex_label", "deck_convert", "deck_sync",
         "deck_pull", "deck_adopt", "tex_converge", "doc_push", "doc_sync", "doc_adopt")


def _ordered(found: dict[str, Callable[..., Result]]) -> dict[str, Callable[..., Result]]:
    unnamed, unknown = sorted(set(found) - set(ORDER)), sorted(set(ORDER) - set(found))
    if unnamed or unknown:
        raise RuntimeError("agent.tools.ORDER and the journeys disagree: "
                           + "; ".join(([f"not in ORDER: {', '.join(unnamed)}"] if unnamed else [])
                                       + ([f"no such tool: {', '.join(unknown)}"] if unknown else [])))
    return {name: found[name] for name in ORDER}


TOOLS: dict[str, Callable[..., Result]] = _ordered({
    "b2s_status": b2s_status,
    **_collect(deck_tools, source_tools, doc_tools),
})

__all__ = ["TOOLS", "ORDER", "INSTRUCTIONS", "instructions", "b2s_status"]
