"""beamer2slides for an agent: the journeys as tools, and the seams a harness plugs into.

The library's journeys are written for a person at a terminal - they print what they are doing
and exit non-zero when they refuse. An agent needs the same journeys with three differences:
results that are data rather than prose, refusals it can branch on, and no step that waits for
a human at a browser. That is all this package is.

    from beamer2slides.agent import AgentContext, TOOLS, deck_inspect

    ctx = AgentContext.local("C:/talks")
    result = deck_inspect(ctx, pdf="talk.pdf")
    result.json()          # what a harness hands the model

`TOOLS` is the registry a harness walks to publish them - `schema.py` turns it into JSON
Schema, `mcp.py` serves it over MCP - and `INSTRUCTIONS` is the guide that has to travel with
them, because the rules this library lives by (a rebuild never destroys deck edits, a frame's
label is its identity, sync before you rebuild) are not deducible from the schemas.
"""

from .auth import GoogleAccess, InjectedToken, NoGoogle, TokenFile, default_access
from .context import ALL_ACTIONS, LOCAL_ONLY, READ_ONLY, AgentContext, Job, tool
from .types import (CODES, READS, READS_GOOGLE, WRITES, WRITES_GOOGLE, Artifact, Diagnostic,
                    Refused, Result)
from .workspace import LocalWorkspace, Workspace

__all__ = [
    "AgentContext", "Job", "tool",
    "Workspace", "LocalWorkspace",
    "GoogleAccess", "TokenFile", "InjectedToken", "NoGoogle", "default_access",
    "Result", "Artifact", "Diagnostic", "Refused", "CODES",
    "READS", "READS_GOOGLE", "WRITES", "WRITES_GOOGLE",
    "ALL_ACTIONS", "READ_ONLY", "LOCAL_ONLY",
]


def __getattr__(name: str):
    """The tools themselves, imported lazily: `tools` pulls in the whole pipeline.

    `import_module`, not `from . import tools`: a failure inside `tools` leaves the submodule
    attribute unset, and the plain form then asks this function for it again, forever. The
    import error is what the caller wants to see.
    """
    import importlib

    if name.startswith("_"):
        raise AttributeError(name)
    registry = importlib.import_module(".tools", __name__)
    try:
        return getattr(registry, name)
    except AttributeError:
        raise AttributeError(name) from None
