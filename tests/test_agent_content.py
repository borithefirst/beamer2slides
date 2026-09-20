"""Content in and content out: the seam a harness with no filesystem plugs into.

Every promise `agent/content.py` makes is broken on purpose here, because each one is a way
the layer could go quietly wrong rather than loudly: a Drive URL fetched as if it were a file,
a name that climbs out of the workspace, a conversion handing a model thirty PNGs, a refusal
that says "failed" instead of naming the argument. The one the rest depend on is the first:
**a plain string is never content**, or `deck_sync(deck=<a presentation URL>)` stops working.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Annotated

import pytest

from beamer2slides.agent import AgentContext, LocalWorkspace, MemoryWorkspace
from beamer2slides.agent import content as C
from beamer2slides.agent.types import Artifact, READS, Refused, Result, WRITES
from beamer2slides.agent.context import Job, tool

LOCAL = frozenset({READS, WRITES})


@pytest.fixture()
def ws(tmp_path):
    return LocalWorkspace(tmp_path / "work")


# -- what is content and what is an identifier ------------------------------------------------

@pytest.mark.parametrize("value", [
    "talk.pdf",                                              # an ordinary ref
    "out/talk/deck.json",
    "https://docs.google.com/presentation/d/1AbC/edit",      # a deck, resolved by the journey
    "https://docs.google.com/document/d/1AbC/edit",
    "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",                 # a bare Drive id
    "database:notes",                                        # a colon is not a scheme
    "",
    True, False, 3, None, ["a", "b"],
])
def test_a_plain_value_is_an_identifier_and_is_never_touched(ws, value):
    """The promise the whole layer rests on: only `data:` and a dict are content.

    A Drive URL is an identifier the journey resolves itself. If this layer ever decided to
    fetch one, `deck_sync(deck=<url>)` would start downloading the deck's HTML into the
    workspace and syncing against it.
    """
    assert not C.is_content(value)
    assert C.take_in(ws, {"deck": value}) == {"deck": value}
    assert not list(ws.root.rglob("*")) or not (ws.root / C.INBOX).exists()


def test_a_data_uri_is_content_in_both_of_its_forms(ws):
    raw = b"%PDF-1.7\nhello"
    encoded = "data:application/pdf;base64," + base64.b64encode(raw).decode()
    plain = "data:text/plain,hello%20there"

    out = C.take_in(ws, {"pdf": encoded, "file": plain})
    assert ws.read_bytes(out["pdf"]) == raw
    assert ws.read_text(out["file"]) == "hello there"
    assert out["pdf"].endswith(".pdf") and out["file"].endswith(".txt")


@pytest.mark.parametrize("given, expect", [
    ({"name": "notes.html", "text": "<p>hi</p>"}, b"<p>hi</p>"),
    ({"name": "talk.pdf", "base64": base64.b64encode(b"%PDF-1.7").decode()}, b"%PDF-1.7"),
    ({"name": "raw.bin", "bytes": b"\x00\x01\x02"}, b"\x00\x01\x02"),
    ({"name": "d.json", "content": base64.b64encode(b"{}").decode()}, b"{}"),
])
def test_a_content_dict_becomes_a_file_with_the_name_it_asked_for(ws, given, expect):
    out = C.take_in(ws, {"file": given})
    assert out["file"] == f"{C.INBOX}/{given['name']}"
    assert ws.read_bytes(out["file"]) == expect


def test_content_with_no_name_is_named_after_the_argument_and_sniffed(ws):
    out = C.take_in(ws, {"pdf": {"base64": base64.b64encode(b"%PDF-1.7 x").decode()},
                         "tex": {"text": "\\documentclass{beamer}"},
                         "page": {"text": "<!DOCTYPE html><p>x</p>"},
                         "blob": {"bytes": b"\xff\xfe\x00\x01"}})
    assert out["pdf"] == f"{C.INBOX}/pdf.pdf"
    assert out["tex"] == f"{C.INBOX}/tex.tex"
    assert out["page"] == f"{C.INBOX}/page.html"
    assert out["blob"] == f"{C.INBOX}/blob.bin"


def test_take_in_is_idempotent_so_two_front_doors_cannot_double_materialise(ws):
    """`dispatch` materialises before the schema check and `@tool` again for a direct caller."""
    once = C.take_in(ws, {"pdf": {"name": "t.pdf", "text": "x"}})
    twice = C.take_in(ws, once)
    assert twice == once
    assert len(list((ws.root / C.INBOX).iterdir())) == 1


def test_a_name_that_climbs_lands_inside_the_workspace_anyway(ws):
    out = C.take_in(ws, {"pdf": {"name": "../../etc/passwd", "text": "x"}})
    landed = ws.resolve(out["pdf"])
    assert ws.root in landed.parents, f"{landed} escaped {ws.root}"
    assert landed.name == "passwd"


# -- refusals that name the argument -----------------------------------------------------------

def test_bad_base64_is_a_bad_request_that_says_which_argument(ws):
    with pytest.raises(Refused) as caught:
        C.take_in(ws, {"pdf": {"base64": "not base64 at all!!"}})
    assert caught.value.code == "bad_request"
    assert caught.value.data["parameter"] == "pdf"
    assert "base64" in str(caught.value)


@pytest.mark.parametrize("given", [{"text": 12}, {"base64": 12}, {"bytes": "x"}])
def test_content_of_the_wrong_python_type_is_refused_by_name(ws, given):
    with pytest.raises(Refused) as caught:
        C.take_in(ws, {"pdf": given})
    assert caught.value.code == "bad_request"
    assert caught.value.data["parameter"] == "pdf"


def test_a_url_with_no_fetcher_is_refused_rather_than_fetched(ws):
    """This library never opens a socket to a host a model chose.

    The refusal has to say so and offer the way forward, or a harness author reads it as a bug
    and the next version grows a `urlopen` - which is an egress path from a model's argument to
    an arbitrary host, inside a call the harness thought was local.
    """
    with pytest.raises(Refused) as caught:
        C.take_in(ws, {"pdf": {"url": "https://example.com/talk.pdf"}})
    said = str(caught.value)
    assert caught.value.code == "bad_request"
    assert "https://example.com/talk.pdf" in said
    assert "base64" in said and "fetch" in said


def test_a_url_is_fetched_by_the_context_s_own_fetcher(ws):
    asked: list[str] = []

    def fetch(url: str) -> bytes:
        asked.append(url)
        return b"%PDF-1.7 fetched"

    out = C.take_in(ws, {"pdf": {"url": "https://example.com/a/talk.pdf"}}, fetch)
    assert asked == ["https://example.com/a/talk.pdf"]
    assert ws.read_bytes(out["pdf"]) == b"%PDF-1.7 fetched"
    assert out["pdf"] == f"{C.INBOX}/talk.pdf", "the URL's own last segment names the file"


def test_a_fetcher_that_raises_becomes_not_found_and_keeps_the_url(ws):
    def fetch(url: str) -> bytes:
        raise ConnectionError("no route to host")

    with pytest.raises(Refused) as caught:
        C.take_in(ws, {"pdf": {"url": "https://example.com/t.pdf"}}, fetch)
    assert caught.value.code == "not_found"
    assert caught.value.data["url"] == "https://example.com/t.pdf"


# -- going out ---------------------------------------------------------------------------------

def _result(ws, *artifacts: Artifact) -> Result:
    return Result(tool="t", artifacts=list(artifacts))


def test_text_comes_back_as_text_and_bytes_as_base64(ws):
    ws.write_text("deck.json", json.dumps({"slides": [1, 2]}))
    ws.write_bytes("shot.png", b"\x89PNG\r\n\x1a\n\x00rest")
    out = C.deliver(ws, _result(ws, Artifact("deck.json", "json"), Artifact("shot.png", "image")))

    deck, shot = (a.json() for a in out.artifacts)
    assert json.loads(deck["text"])["slides"] == [1, 2]
    assert "base64" not in deck
    assert base64.b64decode(shot["base64"]) == b"\x89PNG\r\n\x1a\n\x00rest"
    assert "text" not in shot
    for art in (deck, shot):
        assert art["bytes"] > 0 and len(art["sha256"]) == 64


def test_an_artifact_over_the_cap_says_so_and_still_carries_its_size_and_digest(ws):
    ws.write_bytes("big.json", b"x" * 5000)
    out = C.deliver(ws, _result(ws, Artifact("big.json", "json")), limit=1000)
    art = out.artifacts[0].json()
    assert art["truncated"] is True
    assert art["bytes"] == 5000 and len(art["sha256"]) == 64
    assert "text" not in art and "base64" not in art, "the cap has to actually withhold it"


def test_the_budget_stops_a_conversion_handing_a_model_thirty_pictures(ws):
    for i in range(6):
        ws.write_bytes(f"bg-{i}.png", b"\x89PNG" + bytes(400))
    out = C.deliver(ws, _result(ws, *(Artifact(f"bg-{i}.png", "image") for i in range(6))),
                    limit=10_000, budget=1200)
    carried = [a for a in out.artifacts if a.base64]
    assert 0 < len(carried) < 6
    assert all(a.bytes and a.sha256 for a in out.artifacts), "every one is still described"


def test_a_folder_is_named_and_never_inlined(ws):
    (ws.root / "out" / "talk").mkdir(parents=True)
    out = C.deliver(ws, _result(ws, Artifact("out/talk", "folder")))
    art = out.artifacts[0].json()
    assert art == {"ref": "out/talk", "kind": "folder", "description": ""}


def test_an_artifact_that_vanished_is_skipped_not_raised(ws):
    out = C.deliver(ws, _result(ws, Artifact("gone.json", "json"), Artifact("../out.json", "json")))
    assert [a.json() for a in out.artifacts] == [
        {"ref": "gone.json", "kind": "json", "description": ""},
        {"ref": "../out.json", "kind": "json", "description": ""}]


def test_an_unknown_kind_is_decided_by_the_bytes(ws):
    ws.write_bytes("thing.dat", "hello wörld".encode("utf-8"))
    ws.write_bytes("other.dat", b"\x00\x01\x02\x03")
    out = C.deliver(ws, _result(ws, Artifact("thing.dat", "dat"), Artifact("other.dat", "dat")))
    assert out.artifacts[0].text == "hello wörld"
    assert out.artifacts[1].base64 and out.artifacts[1].text is None


def test_an_artifact_with_no_content_delivered_has_the_shape_it_always_had():
    """A harness written before any of this reads `ref`, `kind`, `description` and finds them."""
    assert Artifact("a.json", "json", "why").json() == {
        "ref": "a.json", "kind": "json", "description": "why"}


# -- the workspace nobody can name ---------------------------------------------------------

def test_a_memory_workspace_is_a_real_directory_that_goes_away():
    ws = MemoryWorkspace()
    root = ws.root
    ref = ws.put("talk.pdf", b"%PDF-1.7")
    assert root.is_dir() and ws.get(ref) == b"%PDF-1.7"
    ws.close()
    assert not root.exists(), "a detached run must leave nothing behind"


def test_a_detached_context_closes_its_workspace_and_delivers_inline():
    with AgentContext.detached(allow=LOCAL) as ctx:
        root = ctx.workspace.root
        assert ctx.deliver == "inline"
        assert root.is_dir()
    assert not root.exists()


# -- the whole way through a tool ------------------------------------------------------------

@tool("fake_journey", needs=(READS, WRITES))
def fake_journey(
    j: Job,
    file: Annotated[str, "Workspace ref of a file to read, or the file itself."],
    note: Annotated[str, "Anything to record beside it."] = "",
) -> None:
    """A journey that reads one file and writes one, so the seam can be tested without PDFium."""
    path = j.path(file)
    j.data["said"] = path.read_text(encoding="utf-8")
    j.data["ref"] = file
    out = j.path("out/answer.json", write=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"said": j.data["said"], "note": note}), encoding="utf-8")
    j.artifact(out, "json", "what the journey decided")
    j.summary = "done"


def test_a_journey_takes_content_in_and_hands_content_back_with_no_path_either_way():
    """The whole point, end to end: nothing a harness cannot serialise crosses the boundary."""
    with AgentContext.detached(allow=LOCAL) as ctx:
        result = fake_journey(ctx, file={"name": "notes.html", "text": "<p>hello</p>"},
                              note="n")
        assert result.ok, result.summary
        assert result.data["said"] == "<p>hello</p>"
        assert result.data["ref"] == "inbox/notes.html"
        [art] = result.artifacts
        assert json.loads(art.text)["said"] == "<p>hello</p>"
        # Serialisable all the way down: what the harness actually sends the model.
        json.dumps(result.json())


def test_by_default_an_artifact_is_still_a_name_alone(tmp_path):
    """`deliver="refs"` is the default, so nothing already running starts carrying payloads."""
    ctx = AgentContext(workspace=LocalWorkspace(tmp_path), allow=LOCAL)
    result = fake_journey(ctx, file={"name": "n.html", "text": "<p>x</p>"})
    assert result.ok
    assert result.artifacts[0].json() == {
        "ref": "out/answer.json", "kind": "json", "description": "what the journey decided"}


def test_malformed_content_comes_back_as_a_refusal_not_an_exception():
    with AgentContext.detached(allow=LOCAL) as ctx:
        result = fake_journey(ctx, file={"base64": "!!!"})
        assert not result.ok and result.code == "bad_request"
        assert result.data["parameter"] == "file"


def test_the_gate_still_runs_even_though_content_was_materialised_first(tmp_path):
    """Materialising is not doing the journey - a forbidden call still does no work.

    It does cost a file in the inbox, which is the price of refusing with the argument named.
    """
    ctx = AgentContext(workspace=LocalWorkspace(tmp_path), allow=frozenset({READS}))
    result = fake_journey(ctx, file={"name": "n.html", "text": "x"})
    assert not result.ok and result.code == "forbidden"
    assert not (Path(tmp_path) / "out" / "answer.json").exists()


def test_dispatch_takes_content_before_the_schema_check():
    """A content dict is not a publishable parameter type, so it must be gone before `validate`."""
    from beamer2slides.agent import mcp

    with AgentContext.detached(allow=LOCAL) as ctx:
        result = mcp.dispatch(ctx, "fake_journey", {"file": {"name": "n.html", "text": "<p>q</p>"}},
                              tools={"fake_journey": fake_journey})
        assert result.ok, result.summary
        assert result.data["said"] == "<p>q</p>"
        assert result.artifacts[0].text
