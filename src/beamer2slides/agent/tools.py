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
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import ModuleType
from typing import Annotated, TypeVar

from ..json_types import Json, JsonObject
from . import deck_tools, doc_tools, source_tools
from .context import Job, Tool, tool
from .types import READS
from .workspace import Workspace

# How many of a kind to look at before saying "and more": a workspace may be someone's whole
# talks folder, and an agent does not need every file in it to decide what to do next.
SURVEY_LIMIT = 40


def _collect(*modules: ModuleType) -> dict[str, Tool[...]]:
    found: dict[str, Tool[...]] = {}
    for module in modules:
        for value in vars(module).values():
            if isinstance(value, Tool):
                if value.tool_name in found and found[value.tool_name] is not value:
                    raise RuntimeError(f"two tools are called {value.tool_name}")
                found[value.tool_name] = value
    return found


@tool("b2s_status", needs=(READS,), local=None)
def b2s_status(j: Job, *,
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
    j.data["allows"] = list[Json](sorted(j.ctx.allow))
    j.data["google"] = access = j.ctx.google.describe()
    j.data["instructions_available"] = True

    folders = [j.path(out, write=False)] if out else _conversion_folders(ws)
    decks = [_conversion(ws, f) for f in folders[:SURVEY_LIMIT]]
    j.data["conversions"] = [d.json() for d in decks]
    sources = _sources(ws)
    j.data["sources"] = [s.json() for s in sources]
    documents = _documents(ws)
    j.data["documents"] = [d.json() for d in documents]
    pdfs = ws.glob("*.pdf")[:SURVEY_LIMIT]
    j.data["pdfs"] = list[Json](pdfs)

    parts = [f"{len(decks)} converted deck(s), {len(sources)} LaTeX source(s), "
             f"{len(documents)} canonical document file(s) and {len(pdfs)} loose PDF(s) "
             f"in {ws.root}."]
    if not access.get("available"):
        reason = access.get("reason", "no Google access")
        parts.append(f"Google is not reachable ({reason}); local journeys - deck_inspect, "
                     f"tex_label, tex_converge - still work.")
        fix = access.get("fix")
        if isinstance(fix, str) and fix:
            parts.append(fix)
        if access.get("command"):
            parts.append(f"A person has to run `{access['command']}` at a terminal to fix it.")
        j.warn(f"Google is not reachable: {reason}", "")
    elif access.get("expired"):
        # `available` is true on a refresh token alone, and an expired access token is the
        # ordinary state between calls - the next journey refreshes it. Saying "good until
        # <a time already past>" reads like a problem, and an agent that believes it goes
        # looking for consent nobody needs to give.
        parts.append("The Google access token has expired and will be refreshed on the next "
                     "call; no consent is needed unless that refresh is refused.")
    elif access.get("expires"):
        parts.append(f"Google access is good until {access['expires']}.")

    unbased = [d.out for d in decks if d.converted and not d.has_base]
    if unbased:
        j.warn("A deck with no sync base can only be rebuilt, never merged: "
               + ", ".join(unbased[:3]), "")
    j.summary = " ".join(parts)

    if decks:
        j.suggest("deck_sync with dry_run=True to see what a recompiled source would change")
    if pdfs and not decks:
        j.suggest("deck_inspect on a PDF to see what it would convert to")
    if sources:
        j.suggest("tex_label to check that every frame carries a label")


def _conversion_folders(ws: Workspace) -> list[Path]:
    root = ws.root / "out"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "deck.json").exists())


@dataclass(frozen=True, kw_only=True)
class Conversion:
    """One out folder as `b2s_status` reports it."""

    out: str
    name: str
    classified: bool
    converted: bool
    has_base: bool
    has_backups: bool
    #: emit.json's `url`, `presentationId` and `title`, those it has.
    emitted: JsonObject
    slides: int | None
    unreadable: str | None        # why emit.json could not be read; None when it could

    def json(self) -> JsonObject:
        out: JsonObject = {"out": self.out, "name": self.name, "classified": self.classified,
                           "converted": self.converted, "has_base": self.has_base,
                           "has_backups": self.has_backups}
        if self.unreadable is not None:
            out["unreadable"] = self.unreadable
            return out
        out.update(self.emitted)
        if self.slides is not None:
            out["slides"] = self.slides
        return out


def _read_json(path: Path) -> Json:
    loaded: Json = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _conversion(ws: Workspace, folder: Path) -> Conversion:
    """One out folder, read from its own files only - nothing here talks to Drive."""
    classified = (folder / "deck.json").exists()
    emit = folder / "emit.json"
    emitted: JsonObject = {}
    slides: int | None = None
    unreadable: str | None = None
    if emit.exists():
        try:
            data = _read_json(emit)
        except (OSError, ValueError):
            data = None
            unreadable = "emit.json could not be read"
        if isinstance(data, dict):
            for key in ("url", "presentationId", "title"):
                value = data.get(key)
                if value:
                    emitted[key] = value
            listed = data.get("slides")
            if isinstance(listed, list):
                slides = len(listed)
    if unreadable is None and classified and slides is None:
        try:
            deck = _read_json(folder / "deck.json")
        except (OSError, ValueError):
            deck = None
        if isinstance(deck, dict):
            listed = deck.get("slides", [])
            if isinstance(listed, list):
                slides = len(listed)
    return Conversion(out=ws.ref(folder), name=folder.name, classified=classified,
                      converted=emit.exists(),
                      has_base=(folder / "sync" / "base.json").exists(),
                      has_backups=(folder / "backups" / "backups.json").exists(),
                      emitted=emitted, slides=slides, unreadable=unreadable)


@dataclass(frozen=True, kw_only=True)
class Source:
    """A LaTeX source `b2s_status` found."""

    tex: str
    frames: int
    labels: int
    beamer: bool

    def json(self) -> JsonObject:
        return {"tex": self.tex, "frames": self.frames, "labels": self.labels,
                "beamer": self.beamer}


def _sources(ws: Workspace) -> list[Source]:
    out: list[Source] = []
    for ref in ws.glob("**/*.tex"):
        if ref.startswith("out/") or "/out/" in ref or "/.b2s/" in ref:
            continue
        path = ws.resolve(ref, write=False)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\\begin{frame}" not in text and "\\begin{document}" not in text:
            continue
        out.append(Source(tex=ref, frames=text.count("\\begin{frame}"),
                          labels=text.count("label="), beamer="beamer" in text[:4000]))
        if len(out) >= SURVEY_LIMIT:
            break
    return out


@dataclass(frozen=True, kw_only=True)
class Document:
    """A canonical HTML file `b2s_status` found."""

    file: str
    pushed: bool
    has_base: bool

    def json(self) -> JsonObject:
        return {"file": self.file, "pushed": self.pushed, "has_base": self.has_base}


def _documents(ws: Workspace) -> list[Document]:
    out: list[Document] = []
    for ref in ws.glob("**/*.html"):
        if "/.b2s/" in ref or ref.startswith(".b2s/"):
            continue
        path = ws.resolve(ref, write=False)
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        stem = Path(ref).stem
        base = path.parent / ".b2s" / f"{stem}.base.json"
        out.append(Document(file=ref, pushed="b2s-document" in head, has_base=base.exists()))
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
#: `deck_prepare` and `deck_upload` come after `deck_convert` and not in its place: they are
#: that one journey cut in half for a caller whose local work and Google work happen in
#: different places, and a model reading top to bottom should meet the whole journey first.
ORDER = ("b2s_status", "deck_inspect", "tex_label", "deck_convert", "deck_prepare", "deck_upload",
         "deck_sync", "deck_pull", "deck_adopt", "tex_converge", "doc_push", "doc_sync",
         "doc_adopt")

_T = TypeVar("_T")


def _ordered(found: Mapping[str, _T]) -> dict[str, _T]:
    unnamed, unknown = sorted(set(found) - set(ORDER)), sorted(set(ORDER) - set(found))
    if unnamed or unknown:
        raise RuntimeError("agent.tools.ORDER and the journeys disagree: "
                           + "; ".join(([f"not in ORDER: {', '.join(unnamed)}"] if unnamed else [])
                                       + ([f"no such tool: {', '.join(unknown)}"] if unknown else [])))
    return {name: found[name] for name in ORDER}


TOOLS: dict[str, Tool[...]] = _ordered({
    "b2s_status": b2s_status,
    **_collect(deck_tools, source_tools, doc_tools),
})

__all__ = ["TOOLS", "ORDER", "INSTRUCTIONS", "instructions", "b2s_status"]
