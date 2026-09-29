"""What a journey hands back: one shape for every tool, and the vocabulary of refusals.

A command line says what happened by printing and by exiting non-zero. Neither survives the
trip into an agent harness: the model gets a wall of text it has to parse, and a harness that
runs the library in-process gets a `SystemExit` through the heart. So every tool here returns
a `Result` - plain data, JSON all the way down - and the refusals the library already knows how
to make (a deck someone edited, a sync with no base, a document that needs a side chosen) come
back as a `code` the agent can branch on rather than a sentence it has to read.

`summary` is the one thing written for the model to read; `data` is written for it to act on.

The records are frozen and every field is said where one is made: an artifact whose content was
delivered is a new artifact (`Artifact.named`, `dataclasses.replace`), and a result is made once,
by the wrapper (`context.Job.result`). `data` is a `JsonObject`, so what a tool puts there is
serialisable by construction rather than by `json.dumps(default=str)` at the edge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal, get_args

from ..json_types import Json, JsonObject

#: The refusal codes, a closed set: `Result.code`, `Refused.code` and every place that makes one
#: are checked against it, and `CODES` below must say each in words (`tests/test_agent_core.py`).
Code = Literal[
    "needs_consent", "no_credentials", "offline", "rate_limited",
    "deck_edited", "no_way_back", "no_base", "base_mismatch", "base_choice_needed",
    "already_pushed", "source_exists", "labels",
    "compile_failed", "not_converged", "not_found",
    "forbidden", "outside_workspace", "bad_request", "refused", "failed",
]

# Refusals, in the library's own terms. A tool returns exactly one of these when `ok` is False,
# and an agent is expected to branch on the code, not on the words.
CODES: dict[Code, str] = {
    # Nothing is wrong with the request; something about this machine or this account is.
    "needs_consent": "Google sign-in has expired or was never given, and it needs a browser.",
    "no_credentials": "No OAuth client secret is installed, so no Google call can be made.",
    "offline": "This context has no Google access at all; the tool needs it.",
    "rate_limited": "Google refused for quota reasons. Wait and try again.",
    # The library refusing on purpose. Each one has a documented way forward.
    "deck_edited": "Someone edited the deck in Slides; rebuilding would destroy that work.",
    "no_way_back": "A forced rebuild was asked for but no backup could be made.",
    "no_base": "There is no sync base for this deck, so a three-way merge is impossible.",
    "base_mismatch": "The sync base describes none of this deck's slides: another copy's, or the "
                     "deck was rebuilt outside sync. Converting would make a second deck.",
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


def code_of(value: str) -> Code | None:
    """`value` as a refusal code, or None when it is not one (a recorded transcript's)."""
    for known in get_args(Code):
        if known == value:
            return known
    return None


# What a tool does to the world. A harness that wants to ask before letting an agent act needs
# this more than it needs the tool's name: `writes_google` is the one that spends someone's deck.
Need = Literal["reads", "reads_google", "writes", "writes_google"]
READS: Final = "reads"                  # local files only
READS_GOOGLE: Final = "reads_google"     # fetches from Drive/Slides/Docs, changes nothing there
WRITES: Final = "writes"                 # writes local files (source trees, out folders)
WRITES_GOOGLE: Final = "writes_google"   # changes a deck or document someone may be looking at

#: What a diagnostic is.
Level = Literal["conflict", "warning", "note"]


def level_of(value: str) -> Level | None:
    """`value` as a diagnostic level, or None when it is not one."""
    for known in get_args(Level):
        if known == value:
            return known
    return None


@dataclass(frozen=True, kw_only=True)
class Artifact:
    """A file a journey produced, named the way the workspace names things.

    The name is always there. The *content* is there when the context asked for it
    (`content.deliver`), which is what a harness with no filesystem needs: it never opens the
    file, it reads `text` or `base64` off the artifact. What was too big to carry says so in
    `truncated` and still carries its size and digest, so the harness can ask for it by ref.
    """

    ref: str                      # workspace-relative, the form the agent passes back in
    kind: str                     # json | image | pdf | tex | html | report | folder | pptx
    description: str
    #: Filled in only when the context delivers content inline; see `agent/content.py`.
    text: str | None
    base64: str | None
    bytes: int | None             # the file's size, whether or not its content is carried
    sha256: str | None
    truncated: bool               # over the cap: read it with `workspace.read_bytes(ref)`

    @classmethod
    def named(cls, *, ref: str, kind: str, description: str) -> Artifact:
        """An artifact that is a name alone: what every journey hands back before delivery."""
        return cls(ref=ref, kind=kind, description=description, text=None, base64=None,
                   bytes=None, sha256=None, truncated=False)

    def json(self) -> JsonObject:
        # The content fields are left out rather than sent as nulls: a conversion's thirty
        # artifacts would otherwise carry five empty keys each into the model's context. The
        # three original fields are always there, so a reader written before this still works.
        out: JsonObject = {"ref": self.ref, "kind": self.kind, "description": self.description}
        if self.text is not None:
            out["text"] = self.text
        if self.base64 is not None:
            out["base64"] = self.base64
        if self.bytes is not None:
            out["bytes"] = self.bytes
        if self.sha256 is not None:
            out["sha256"] = self.sha256
        if self.truncated:
            out["truncated"] = True
        return out


@dataclass(frozen=True, kw_only=True)
class Diagnostic:
    """Something worth saying that is not the result: a conflict, a warning, a note."""

    level: Level
    message: str
    where: str                    # slide 4, main.tex:112, the block a conflict is in; "" if none

    def json(self) -> JsonObject:
        return {"level": self.level, "message": self.message, "where": self.where}


@dataclass(frozen=True, kw_only=True)
class Result:
    """What every tool returns. Serialise with `.json()`; never raise across this boundary."""

    tool: str
    ok: bool
    code: Code | None             # one of CODES when ok is False
    summary: str                  # for the model to read: what happened, in one paragraph
    data: JsonObject              # for the model to act on
    artifacts: list[Artifact]
    diagnostics: list[Diagnostic]
    next_steps: list[str]         # what to consider doing now
    seconds: float

    def json(self) -> JsonObject:
        out: JsonObject = {"tool": self.tool, "ok": self.ok}
        if self.code:
            out["code"] = self.code
        out["summary"] = self.summary
        out["data"] = self.data
        out["artifacts"] = [a.json() for a in self.artifacts]
        out["diagnostics"] = [d.json() for d in self.diagnostics]
        out["next_steps"] = list(self.next_steps)
        out["seconds"] = round(self.seconds, 2)
        return out

    def text(self) -> str:
        """The form a harness with no structured channel can paste into the model's context."""
        return json.dumps(self.json(), indent=2, ensure_ascii=False, default=str)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.diagnostics:
            out[d.level] = out.get(d.level, 0) + 1
        return out


def refusal(*, tool: str, code: Code, summary: str, data: JsonObject) -> Result:
    """A result that says no and did nothing: no artifacts, no diagnostics, no time."""
    return Result(tool=tool, ok=False, code=code, summary=summary, data=data, artifacts=[],
                  diagnostics=[], next_steps=[], seconds=0.0)


class Refused(Exception):
    """A refusal a tool makes on purpose, carrying the code an agent will branch on."""

    def __init__(self, code: Code, message: str, **data: Json) -> None:
        super().__init__(message)
        self.code: Code = code
        self.data: JsonObject = data
