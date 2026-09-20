"""What a journey hands back: one shape for every tool, and the vocabulary of refusals.

A command line says what happened by printing and by exiting non-zero. Neither survives the
trip into an agent harness: the model gets a wall of text it has to parse, and a harness that
runs the library in-process gets a `SystemExit` through the heart. So every tool here returns
a `Result` - plain data, JSON all the way down - and the refusals the library already knows how
to make (a deck someone edited, a sync with no base, a document that needs a side chosen) come
back as a `code` the agent can branch on rather than a sentence it has to read.

`summary` is the one thing written for the model to read; `data` is written for it to act on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any

# Refusals, in the library's own terms. A tool returns exactly one of these when `ok` is False,
# and an agent is expected to branch on the code, not on the words.
CODES = {
    # Nothing is wrong with the request; something about this machine or this account is.
    "needs_consent": "Google sign-in has expired or was never given, and it needs a browser.",
    "no_credentials": "No OAuth client secret is installed, so no Google call can be made.",
    "offline": "This context has no Google access at all; the tool needs it.",
    "rate_limited": "Google refused for quota reasons. Wait and try again.",
    # The library refusing on purpose. Each one has a documented way forward.
    "deck_edited": "Someone edited the deck in Slides; rebuilding would destroy that work.",
    "no_way_back": "A forced rebuild was asked for but no backup could be made.",
    "no_base": "There is no sync base for this deck, so a three-way merge is impossible.",
    "base_choice_needed": "The document has no base; say which side to assume.",
    "already_pushed": "That file already names a document; pushing would make a second one.",
    "source_exists": "The source file is already there; adopt writes a new one.",
    "labels": "Frames are missing labels and the caller asked for that to be an error.",
    # The work itself did not come out.
    "compile_failed": "LaTeX did not compile the source.",
    "not_converged": "The loop ran out of iterations with residuals left.",
    "not_found": "A file, deck or document named in the request is not there.",
    # The harness boundary.
    "forbidden": "This context does not allow what the tool would do (writing a deck, say).",
    "outside_workspace": "A path reached outside the workspace.",
    "bad_request": "The arguments do not make sense together.",
    "refused": "The library refused for a reason it stated in the summary.",
    "failed": "Something unforeseen went wrong; the summary carries the exception.",
}

# What a tool does to the world. A harness that wants to ask before letting an agent act needs
# this more than it needs the tool's name: `writes_google` is the one that spends someone's deck.
READS = "reads"                  # local files only
READS_GOOGLE = "reads_google"     # fetches from Drive/Slides/Docs, changes nothing there
WRITES = "writes"                 # writes local files (source trees, out folders)
WRITES_GOOGLE = "writes_google"   # changes a deck or document someone may be looking at


@dataclass
class Artifact:
    """A file a journey produced, named the way the workspace names things."""

    ref: str                      # workspace-relative, the form the agent passes back in
    kind: str                     # json | image | pdf | tex | html | report | folder | pptx
    description: str = ""

    def json(self) -> dict:
        return asdict(self)


@dataclass
class Diagnostic:
    """Something worth saying that is not the result: a conflict, a warning, a note."""

    level: str                    # conflict | warning | note
    message: str
    where: str = ""               # slide 4, main.tex:112, the block a conflict is in

    def json(self) -> dict:
        return asdict(self)


@dataclass
class Result:
    """What every tool returns. Serialise with `.json()`; never raise across this boundary."""

    tool: str
    ok: bool = True
    code: str | None = None       # one of CODES when ok is False
    summary: str = ""             # for the model to read: what happened, in one paragraph
    data: dict[str, Any] = field(default_factory=dict)          # for the model to act on
    artifacts: list[Artifact] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)          # what to consider doing now
    seconds: float = 0.0

    def json(self) -> dict:
        return {
            "tool": self.tool,
            "ok": self.ok,
            **({"code": self.code} if self.code else {}),
            "summary": self.summary,
            "data": self.data,
            "artifacts": [a.json() for a in self.artifacts],
            "diagnostics": [d.json() for d in self.diagnostics],
            "next_steps": self.next_steps,
            "seconds": round(self.seconds, 2),
        }

    def text(self) -> str:
        """The form a harness with no structured channel can paste into the model's context."""
        return json.dumps(self.json(), indent=2, ensure_ascii=False, default=str)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.diagnostics:
            out[d.level] = out.get(d.level, 0) + 1
        return out


class Refused(Exception):
    """A refusal a tool makes on purpose, carrying the code an agent will branch on."""

    def __init__(self, code: str, message: str, **data: Any) -> None:
        super().__init__(message)
        self.code = code
        self.data = data
