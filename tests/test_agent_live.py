"""The agent tools against real Google (opt-in, markers `slides` and `docs`).

    python -m pytest -m slides tests/test_agent_live.py
    python -m pytest -m docs   tests/test_agent_live.py

Everything else about the agent layer is proved offline - the gate, the refusals, the
schemas, a whole `doc_sync` against `devtools/doc_world.py`. What no offline test can
say is whether a journey driven **through the tools** reaches Google and comes back
with a `Result` an agent can act on: the tools acquire credentials up front, redirect
the library's prints, and translate its `SystemExit`s, and each of those is a place a
live call can go wrong without any fake noticing.

So these are deliberately few. The one that matters is the middle one: a deck somebody
edited must refuse to be rebuilt, and must say `deck_sync` in `next_steps`. That is the
project's central promise, and here it is made against a deck a person really edited.

Tools are called **in process**, not through the CLI, because that is how a harness
calls them - and it is the only way to see that a refusal arrives as a code rather than
as an exit status.

Folders are fixed (`out/agent-live/` of the main checkout): the same deck is rebuilt on
every run rather than littering Drive. The document does not stay anywhere.
Skipped when the Google token needs a browser consent.
"""

import os
from pathlib import Path

import pytest

from .test_slides_alignment import MAIN, google_unavailable

OUT = Path(os.environ.get("B2S_AGENT_LIVE_OUT", MAIN / "out" / "agent-live"))
#: Small on purpose - this suite is about the seam, not about conversion fidelity.
DECK = "tests/decks/out/09_metropolis_fira.pdf"

DOCUMENT = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>What an agent pushed</title></head>
<body>
<h1 id="heading:top">What an agent pushed</h1>
<p id="paragraph:opening">The opening paragraph, which the source will reword.</p>
<p id="paragraph:closing">The closing paragraph, which nobody touches.</p>
</body>
</html>
"""


def _context(allow=None, root: Path | None = None):
    """A context over the main checkout (or a folder inside it), with this machine's token."""
    from beamer2slides.agent import ALL_ACTIONS, AgentContext, LocalWorkspace

    return AgentContext(workspace=LocalWorkspace(root or MAIN), allow=allow or ALL_ACTIONS)


def _call(tool: str, **args):
    # Not `name`: `doc_push` has an argument of that name, and a helper must never be the
    # reason a tool cannot be called the way its schema says.
    from beamer2slides.agent import tools

    return tools.TOOLS[tool](_context(), **args)


@pytest.fixture(scope="module", autouse=True)
def _google():
    if reason := google_unavailable():
        pytest.skip(reason)
    if not (MAIN / DECK).exists():
        pytest.skip(f"{DECK} is not built; run tests/decks/build.py")


# ---------------------------------------------------------------- the deck journeys

@pytest.mark.slides
def test_a_deck_is_converted_and_inspected_through_the_tools():
    """The plain case: the tool reaches Google, and says enough to act on afterwards."""
    ref = "out/agent-live/fira"
    converted = _call("deck_convert", pdf=DECK, out=ref, title="agent live: fira")
    assert converted.ok, converted.json()
    assert converted.data["url"].startswith("https://docs.google.com/presentation/")
    assert converted.data["slides"] >= 1
    assert any(a.kind == "json" for a in converted.artifacts)   # emit.json, to act on later

    looked = _call("deck_inspect", pdf=DECK, out=ref)
    assert looked.ok, looked.json()
    assert looked.data["slides"] == converted.data["slides"]


@pytest.mark.slides
def test_a_deck_somebody_edited_refuses_to_be_rebuilt_and_names_the_way_forward():
    """The one rule, live.

    `deck_convert` onto a folder rebuilds the deck it made last time. When a person has
    edited that deck since, the rebuild would throw their work away, and the guard
    refuses. An agent must hear `deck_edited` and be pointed at `deck_sync` - not be left
    to invent `force_rebuild`, which is the one move that destroys the work for good.
    """
    ref = "out/agent-live/edited"
    _untype(ref)
    first = _call("deck_convert", pdf=DECK, out=ref, title="agent live: edited")
    assert first.ok, first.json()

    _type_into(first.data["url"])

    refused = _call("deck_convert", pdf=DECK, out=ref)
    assert not refused.ok and refused.code == "deck_edited", refused.json()
    assert "deck_sync" in " ".join(refused.next_steps)
    assert first.data["url"] in refused.json() or refused.data.get("url")

    # And the way forward really is a way forward: the merge plans to keep what was typed,
    # and a dry run reaches that conclusion without writing a thing.
    merged = _call("deck_sync", pdf=DECK, deck=ref, dry_run=True)
    assert merged.ok, merged.json()
    assert merged.data["wrote"] is False
    assert merged.data["kept"] >= 1, "the typed word is a deck edit the merge must keep"


TYPED = "EDITED "


def _type_into(url: str) -> None:
    """Put a word into the deck's first text box, as a person would in Slides."""
    from beamer2slides.google_auth import credentials, slides_service

    api = slides_service(credentials())
    ident = _id(url)
    for _, element, _ in _texts(api, ident):
        api.presentations().batchUpdate(presentationId=ident, body={"requests": [
            {"insertText": {"objectId": element, "insertionIndex": 0, "text": TYPED}}]}).execute()
        return
    raise AssertionError("the converted deck has no text box to edit")


def _untype(ref: str) -> None:
    """Take back what an earlier run typed, so this one starts from a deck nobody edited.

    The folder is fixed and its deck is rebuilt every run, so without this the *first*
    convert of the next run is the one that gets refused - the test would pass once and
    then fail for the rest of time, which is worse than not having it.
    """
    import json

    emit = MAIN / ref / "emit.json"
    if not emit.exists():
        return
    url = json.loads(emit.read_text(encoding="utf-8")).get("url")
    if not url:
        return
    from beamer2slides.google_auth import credentials, slides_service

    api = slides_service(credentials())
    ident = _id(url)
    requests = []
    for _, element, text in _texts(api, ident):
        typed = 0
        while text.startswith(TYPED, typed):                    # several runs may have stacked
            typed += len(TYPED)
        if typed:
            requests.append({"deleteText": {"objectId": element, "textRange": {
                "type": "FIXED_RANGE", "startIndex": 0, "endIndex": typed}}})
    if requests:
        api.presentations().batchUpdate(presentationId=ident, body={"requests": requests}).execute()


def _id(url: str) -> str:
    return url.rstrip("/").split("/d/")[1].split("/")[0]


def _texts(api, ident: str):
    """(slide, objectId, text) for every shape on the deck that holds text."""
    deck = api.presentations().get(presentationId=ident).execute()
    for slide in deck["slides"]:
        for element in slide.get("pageElements", []):
            runs = element.get("shape", {}).get("text", {}).get("textElements", [])
            text = "".join(r.get("textRun", {}).get("content", "") for r in runs)
            if text:
                yield slide["objectId"], element["objectId"], text


# ---------------------------------------------------------------- the docs journeys

@pytest.mark.docs
def test_a_document_is_pushed_synced_and_then_has_nothing_left_to_say():
    """Push, change the source, sync, and check the second sync writes nothing.

    The last assertion is the whole Docs design in one line: after a sync the file is
    regenerated from the document, so file, document and base say one thing. Here it is
    asserted about a real document, through the tools rather than the CLI.
    """
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "pushed.html"
    path.write_text(DOCUMENT, encoding="utf-8")
    _forget(path)
    ref = str(path.relative_to(MAIN)).replace("\\", "/")

    pushed = _call("doc_push", file=ref, name="agent live: what an agent pushed")
    assert pushed.ok, pushed.json()
    ident = pushed.data["document"]
    try:
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("which the source will reword",
                                     "which the source has now reworded"), encoding="utf-8")

        synced = _call("doc_sync", file=ref)
        assert synced.ok, synced.json()
        assert synced.data["requests"] > 0                      # it really wrote something

        again = _call("doc_sync", file=ref)
        assert again.ok, again.json()
        assert again.data["requests"] == 0, "a settled pair must have nothing left to write"
    finally:
        _bin(ident)
        _forget(path)


FOREIGN = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>A document nobody pushed</title></head>
<body>
<h1>A document nobody pushed</h1>
<p>A team has been writing this for a year. There is no canonical file anywhere.</p>
<p>Adopting it has to write one, and it has to land inside the workspace.</p>
</body>
</html>
"""


@pytest.mark.docs
def test_a_document_nobody_pushed_is_adopted_into_the_workspace():
    """`doc_adopt` with no `file`, which is the case only a live run can reach.

    The name is a slug of the document's *title*, and a title is only known once the
    document has been read - so the library resolves it against whatever folder the
    process happens to be in. For a person at a terminal that is the folder they typed
    in; for a harness it is wherever it was started, which may be anywhere. The tool
    hands the workspace root down (`doc_sync.adopt(folder=...)`), and nothing offline can
    say whether that landed, because offline there is no document to take a title from.

    The workspace here is `out/agent-live/` rather than the checkout, so a file written to
    the process's own folder would fall outside it and the tool would refuse.
    """
    from beamer2slides.agent import tools as agent_tools

    OUT.mkdir(parents=True, exist_ok=True)
    ident = _import_html(FOREIGN, "agent live: a document nobody pushed")
    try:
        adopted = agent_tools.TOOLS["doc_adopt"](_context(root=OUT), doc=ident)
        assert adopted.ok, adopted.json()
        written = OUT / adopted.data["file"]
        assert written.is_file(), f"{adopted.data['file']} is not in the workspace"
        assert adopted.data["blocks"] >= 3                      # a heading and two paragraphs
        assert adopted.data["anchored"] == adopted.data["blocks"], "every block gets a range"
        said = written.read_text(encoding="utf-8")
        assert ident in said                                    # the file names its document

        # Idempotent: a second run plants no second set of anchors and writes the same file.
        again = agent_tools.TOOLS["doc_adopt"](_context(root=OUT), doc=ident)
        assert again.ok, again.json()
        assert again.data["anchored"] == adopted.data["anchored"]
        assert written.read_text(encoding="utf-8") == said

        # And it is an ordinary pair from here: nothing left to say on either side.
        ref = str(written.relative_to(MAIN)).replace("\\", "/")
        settled = _call("doc_sync", file=ref)
        assert settled.ok, settled.json()
        assert settled.data["requests"] == 0, "an adopted pair is settled the moment it exists"
    finally:
        _bin(ident)
        for leftover in OUT.glob("*.html"):
            if leftover.name != "pushed.html":
                _forget(leftover)
                leftover.unlink()


def _import_html(html: str, name: str) -> str:
    """Make a Google Doc the way a person would - no keys, no anchors, nothing of ours."""
    import io

    from googleapiclient.http import MediaIoBaseUpload

    from beamer2slides.google_auth import credentials, drive_service

    media = MediaIoBaseUpload(io.BytesIO(html.encode("utf-8")), mimetype="text/html")
    return drive_service(credentials()).files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.document"},
        media_body=media, fields="id").execute()["id"]


def _forget(path: Path) -> None:
    """Drop the state of an earlier run, so a push is a push and not a re-push."""
    for leftover in (path.parent / ".b2s").glob(f"{path.stem}.*"):
        leftover.unlink()


def _bin(ident: str) -> None:
    from beamer2slides.google_auth import credentials, drive_service

    try:
        drive_service(credentials()).files().delete(fileId=ident).execute()
    except Exception:                                           # a document already gone
        pass
