"""The live sync fuzzer's cheaper and better-aimed ways, offline (no Google).

- `deck_edits.LiveDeck(defer=True)` queues edits, knows which slides they touched, and sends them
  as one batchUpdate - each alone when Google refuses the lot;
- `fuzz_sync.stale` says when a queued deck must be read again before the next edit;
- the new edit kinds write what they claim (`add_paragraph`, `insert_before_hole`, table rows and
  columns);
- `--focus layout` draws aimed edits, and variants by how much they re-lay;
- `fuzz_reach.step_reach` sees each layout precondition on a hand-built step, and nothing on a step
  without the person's side;
- `gslides.execute` counts calls and backoff without changing what it does.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Generic, TypeVar

import pytest

from beamer2slides.devtools import deck_edits as D
from beamer2slides.devtools import fuzz_reach as R
from beamer2slides.devtools import fuzz_sync as F
from beamer2slides.devtools.sync_check import Model
from beamer2slides.gapi import HttpError
from beamer2slides.google_types import (BatchUpdateResponse, Pages, Presentation, Presentations, Request,
                                        json_object, presentation)
from beamer2slides.json_types import Json, JsonObject, as_array, as_object, as_str

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from beamer2slides.google_types import CreatePresentation, ExecuteOptions, GetPresentation, UpdatePresentation

NBSP = chr(0xA0)
EMU = 12700
T = TypeVar("T")
Sent = Sequence[Mapping[str, object]]


def _shape(oid: str, text: str, y: float, placeholder: str | None) -> JsonObject:
    shape: JsonObject = {"shapeType": "TEXT_BOX", "text": {"textElements": [{"textRun": {"content": text + "\n"}}]}}
    if placeholder:
        shape["placeholder"] = {"type": placeholder}
    return {"objectId": oid, "size": {"width": {"magnitude": 3000000, "unit": "EMU"},
                                      "height": {"magnitude": 3000000, "unit": "EMU"}},
            "transform": {"scaleX": 1, "scaleY": 0.2, "translateX": EMU * 50, "translateY": EMU * y, "unit": "EMU"},
            "shape": shape}


def _cell(text: str) -> JsonObject:
    return {"text": {"textElements": [{"textRun": {"content": text + "\n"}}]}}


def _pres() -> JsonObject:
    table: JsonObject = {
        "objectId": "t1", "size": {"width": {"magnitude": 3000000, "unit": "EMU"},
                                   "height": {"magnitude": 3000000, "unit": "EMU"}},
        "transform": {"scaleX": 1, "scaleY": 0.3, "translateX": EMU * 50, "translateY": EMU * 250, "unit": "EMU"},
        "table": {"rows": 2, "columns": 2, "tableRows": [{"tableCells": [_cell("Scenario"), _cell("Time")]},
                                                         {"tableCells": [_cell("Disjoint"), _cell("3.9 s")]}]}}
    return {"presentationId": "p", "slides": [
        {"objectId": "s1", "slideProperties": {"layoutObjectId": "L"}, "pageElements": [
            _shape("h1", "Merging text", 20, "TITLE"),
            _shape("a1", "First slide words here\nA second paragraph of it", 100, None),
            _shape("a2", f"More text with x{NBSP * 4} hole after it", 200, None)]},
        {"objectId": "s2", "slideProperties": {"layoutObjectId": "L"}, "pageElements": [
            _shape("h2", "Results", 20, "TITLE"), _shape("b1", "Second slide says things", 100, None), table]},
    ]}


class _Answer(Generic[T]):
    """A request: `execute` runs it."""

    def __init__(self, run: Callable[[], T]) -> None:
        self.run = run

    def execute(self, **options: Unpack[ExecuteOptions]) -> T:
        return self.run()


class FakeSlides:
    """presentations().batchUpdate/get(...).execute() recording what was sent; `refuse(reqs)` says
    which batches Google turns down (one refused request throws out its whole batch)."""

    def __init__(self, pres: JsonObject, refuse: Callable[[Sent], bool]) -> None:
        self.pres, self.refuse = pres, refuse
        self.sent: list[Sent] = []
        self.gets = 0

    def presentations(self) -> Presentations:
        return self

    def pages(self) -> Pages:
        raise AssertionError("no page is read here")

    def create(self, **kw: Unpack[CreatePresentation]) -> Request[Presentation]:
        raise AssertionError("no deck is made here")

    def get(self, **kw: Unpack[GetPresentation]) -> Request[Presentation]:
        def run() -> Presentation:
            self.gets += 1
            return presentation(self.pres, "the fake deck")
        return _Answer(run)

    def batchUpdate(self, **kw: Unpack[UpdatePresentation]) -> Request[BatchUpdateResponse]:
        reqs = kw["body"]["requests"]

        def run() -> BatchUpdateResponse:
            if self.refuse(reqs):
                raise HttpError(SimpleNamespace(status=400, reason="Bad Request"), b"{}")
            self.sent.append(reqs)
            return BatchUpdateResponse()
        return _Answer(run)


def _accept(reqs: Sent) -> bool:
    return False


def _deck(refuse: Callable[[Sent], bool], defer: bool) -> tuple[D.LiveDeck, FakeSlides]:
    pres = _pres()
    api = FakeSlides(pres, refuse)
    return D.LiveDeck("p", api, pres, defer), api


def _body(req: Mapping[str, object], name: str) -> JsonObject:
    """The body of a request we made (always JSON)."""
    return json_object(req[name], name)


def _args(spec: JsonObject) -> JsonObject:
    return as_object(spec["args"], "spec args")


def test_deferred_edits_are_one_batch_and_mark_their_slides() -> None:
    deck, api = _deck(_accept, True)
    D.append_sentence(deck, "Merging text", "First slide words", "Added.")
    D.insert_table_row(deck, "Results", "Disjoint", ["Renames", "5.5 s"], None)
    assert deck.dirty == {"s1", "s2"} and not deck.reshaped and not api.sent
    assert deck.flush() == []
    assert len(api.sent) == 1 and deck.writes == 1
    kinds = [next(iter(r)) for r in api.sent[0]]
    assert kinds == ["insertText", "insertTableRows", "insertText", "insertText"]


def test_a_refused_batch_costs_only_the_edit_google_refused() -> None:
    deck, api = _deck(lambda reqs: any("insertTableRows" in r for r in reqs), True)
    D.append_sentence(deck, "Merging text", "First slide words", "Added.")
    D.insert_table_row(deck, "Results", "Disjoint", ["Renames", "5.5 s"], None)
    D.bold(deck, "Results", "Second", "Second slide says things")
    assert deck.flush() == [1]
    assert [next(iter(reqs[0])) for reqs in api.sent] == ["insertText", "updateTextStyle"]


def test_the_immediate_way_is_unchanged() -> None:
    deck, api = _deck(_accept, False)
    D.append_sentence(deck, "Merging text", "First slide words", "Added.")
    assert len(api.sent) == 1 and api.gets == 1 and not deck.pending


def test_open_deck_reads_the_deck_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from beamer2slides import google_auth
    api = FakeSlides(_pres(), _accept)
    monkeypatch.setattr(google_auth, "slides_service", lambda: api)
    deck = D.open_deck("p", defer=True)
    assert api.gets == 1 and deck.reads == 1 and [s.id for s in deck.model.slides] == ["s1", "s2"]


def test_stale_reads_again_only_for_what_a_queued_edit_touched() -> None:
    deck, _ = _deck(_accept, True)
    bold: JsonObject = {"edit": "bold", "args": {"slide": "Merging text", "word": "slide"}}
    other: JsonObject = {"edit": "bold", "args": {"slide": "Results", "word": "slide"}}
    move: JsonObject = {"edit": "move_slide", "args": {"slide": "Results", "after": {"index": 1}}}
    assert not F.stale(deck, bold)                       # nothing queued
    D.append_sentence(deck, "Merging text", "First slide words", "Added.")
    assert F.stale(deck, bold) and not F.stale(deck, other)
    assert not F.stale(deck, move)
    D.add_slide(deck, "Results", "A new slide", None)
    assert deck.reshaped
    assert F.stale(deck, move)
    assert F.stale(deck, {"edit": "set_notes", "args": {"slide": {"index": 1}, "text": "x"}})
    assert F.stale(deck, {"edit": "set_notes", "args": {"slide": {"contains": "Second"}, "text": "x"}})


def test_new_text_kinds_write_where_they_say() -> None:
    deck, _ = _deck(_accept, True)
    exp = D.add_paragraph(deck, "Merging text", "First slide words", "Typed by the person")
    (req,), = deck.pending
    assert _body(req, "insertText")["text"] == "\nTyped by the person"
    assert _body(req, "insertText")["insertionIndex"] == len("First slide words here")
    assert {as_str(c["text"], "check text") for c in exp.checks} == {"Typed by the person", "First slide words"}
    exp = D.insert_before_hole(deck, "Merging text", "More text with", "quite")
    assert _body(deck.pending[-1][0], "insertText") == {"objectId": "a2", "text": "quite ",
                                                        "insertionIndex": len("More text with x")}
    assert {"check": "text", "slide": "Merging text", "text": "x quite", "count": 1} in exp.checks
    with pytest.raises(D.CheckError):
        D.insert_before_hole(deck, "Merging text", "First slide words", "no hole here")


def test_table_kinds_insert_and_fill() -> None:
    deck, _ = _deck(_accept, True)
    D.insert_table_column(deck, "Results", "Time", ["Runs", "x12", "ignored"], None)
    reqs = deck.pending[-1]
    assert reqs[0] == {"insertTableColumns": {"tableObjectId": "t1", "cellLocation": {"rowIndex": 0, "columnIndex": 1},
                                              "insertRight": True, "number": 1}}
    assert [(_body(r, "insertText")["cellLocation"], _body(r, "insertText")["text"]) for r in reqs[1:]] == [
        ({"rowIndex": 0, "columnIndex": 2}, "Runs"), ({"rowIndex": 1, "columnIndex": 2}, "x12")]
    with pytest.raises(D.CheckError):
        D.insert_table_row(deck, "Results", "Second slide says", ["a", "b"], None)   # not a cell


def test_notes_on_a_slide_without_a_notes_page_are_refused_by_name() -> None:
    """`set_notes` on a slide whose read has no speaker notes shape was a TypeError deep inside
    (`None["objectId"]`); the types said the shape may be missing, and it is now a CheckError the
    fuzzer's edit loop catches like any edit that no longer fits."""
    deck, _ = _deck(_accept, True)
    with pytest.raises(D.CheckError, match="no speaker notes shape"):
        D.set_notes(deck, "Results", "Mention it")
    assert not deck.pending


def test_edits_given_as_json_take_their_documented_values() -> None:
    deck, _ = _deck(_accept, True)
    exp = D.apply(deck, {"edit": "duplicate", "args": {"slide": "Results", "target": {"text": "Second slide says"}}})
    assert exp.json()["args"] == {"slide": "Results", "target": {"text": "Second slide says"}, "dx": 12, "dy": 12}
    exp = D.apply(deck, {"edit": "resize", "args": {"slide": "Results", "target": {"text": "Results"}, "sx": 2}})
    assert _args(exp.json())["sy"] == 2
    with pytest.raises(D.CheckError, match="unknown edit"):
        D.apply(deck, {"edit": "paint", "args": {}})


def test_every_edit_kind_is_in_the_catalogue_and_the_reach_table() -> None:
    kinds = {s["edit"] for s in D.catalogue("https://example.invalid/picture.png")}
    assert set(D.EDITS) <= kinds
    assert set(D.EDITS) - set(R.EDIT_FIELDS) <= {"add_slide", "duplicate_slide", "delete_slide", "move_slide",
                                                  "set_notes", "set_background"}


def test_focus_draws_aimed_edits_on_the_aimed_slides() -> None:
    model = Model(_pres())
    rng = random.Random(5)
    got = [F.random_spec(model, rng, None, "layout", {"s2"}) for _ in range(300)]
    aimed = [s for s in got if s and "aim" in s]
    assert len(aimed) > 150
    assert {s["aim"] for s in aimed} >= {"long_text", "new_paragraph", "hole", "move_text", "row", "column", "cell"}
    on_s2 = sum(1 for s in aimed if _args(s).get("slide") == {"title": "Results"})
    assert on_s2 > len(aimed) * 0.5
    # no aimed edit writes in a title or the frame counter
    assert not [s for s in aimed if "Merging text" == _args(s).get("text") or "Results" == _args(s).get("text")]
    # the default draw is unaimed: no focus, no aimed edit
    a = [F.random_spec(model, random.Random(9), None, None, ()) for _ in range(1)]
    assert not any("aim" in (s or {}) for s in a)


def test_hole_aim_types_in_front_of_the_hole() -> None:
    model = Model(_pres())
    rng = random.Random(1)
    specs = [F._aim_hole(rng, model, model.slides[0], {"title": "Merging text"}, None) for _ in range(20)]
    for spec in specs:
        if spec is not None:
            deck, _ = _deck(_accept, True)
            D.apply(deck, spec)   # found by content in the same read
    assert {s["edit"] for s in specs if s} == {"insert_before_hole", "replace_word"}


@pytest.mark.needs_decks("sync/build.py")
def test_variant_weights_prefer_reflow_and_stay_uniform_by_default() -> None:
    build = F.sync_build()
    uniform = F.variant_weights(build, None)
    assert set(uniform.values()) == {1.0} and "v1" not in uniform
    focused = F.variant_weights(build, "layout")
    assert focused["table-row"] > focused.get("notes", 0.25)
    assert "Results" in F.variant_slides(build, "table-row")
    assert F.start_variant("layout") == F.start_variant(None) == "v1" and F.start_variant("probes") == "probes"
    assert set(F.variant_weights(build, "probes")) == {"probes-reword", "probes-push"}
    assert F.variant_slides(build, "probes-push") == {"Room to grow", "Two boxes", "Display math"}


def _unit(key: str, action: str, source: Sequence[str], deck: Sequence[str]) -> JsonObject:
    return {"key": key, "action": action, "source": [s for s in source], "deck": [d for d in deck]}


def _step() -> tuple[JsonObject, JsonObject, JsonObject, JsonObject]:
    """A slide where the person lengthened a text, typed in front of a hole, moved a text and grew
    the table, and the source re-laid all of them."""
    words = f"Holes x{NBSP * 3} here"
    base: JsonObject = {"slides": [{"key": "s", "objectId": "S", "groups": [], "elements": [
        {"key": "text/body/0", "kind": "text", "objects": ["T0"], "main": "T0", "readback": {"T0": {"text": "short"}}},
        {"key": "text/body/1", "kind": "text", "objects": ["T1"], "main": "T1", "readback": {"T1": {"text": words}}},
        {"key": "image/math/0", "kind": "image", "role": "math", "anchor": "text/body/1", "objects": ["M"], "main": "M"},
        {"key": "text/body/2", "kind": "text", "objects": ["T2"], "main": "T2", "readback": {"T2": {"text": "mover"}}},
        {"key": "table/0", "kind": "table", "objects": ["TB"], "main": "TB"}]}]}
    before: JsonObject = {"slides": [{"objectId": "S", "objects": {
        "T0": {"text": "short and then much longer", "box": [0, 0, 100, 20]},
        "T1": {"text": f"Holes quite x{NBSP * 3} here", "box": [0, 30, 100, 50]},
        "T2": {"text": "mover", "box": [0, 60, 100, 80]}, "TB": {"text": "", "box": [0, 100, 100, 150]}}}]}
    after: JsonObject = {"slides": [{"objectId": "S", "objects": {
        "n0": {"title": "b2s:s/text/body/0", "box": [0, 0, 100, 40]},
        "n2": {"title": "b2s:s/text/body/2", "box": [0, 35, 100, 55]}}}]}
    units: list[Json] = [
        _unit("text/body/0", "recreate", ["text"], ["text"]),
        _unit("text/body/1", "recreate", ["size"], ["text"]),
        _unit("text/body/2", "keep", [], ["geometry"]),
        _unit("table/0", "keep", ["text"], ["text"])]
    report: JsonObject = {"actions": [{"slide": "s", "units": units}]}
    return base, before, after, report


def test_reach_sees_each_precondition_it_names() -> None:
    base, before, after, report = _step()
    got = R.step_reach(base, before, after, report)
    # (the words typed in front of the hole made that text longer too)
    assert [w.unit for w in got["text_into_relaid"]] == ["text/body/0", "text/body/1"]
    assert [w.unit for w in got["hole_reworded"]] == ["text/body/1"]
    assert got["moved_vs_reflow"] and got["moved_overlapped"]
    assert [w.unit for w in got["table_grows"]] == ["table/0"]


def _at(o: JsonObject, *path: str | int) -> JsonObject:
    """The object at `path` (keys and list indices) inside `o`."""
    here: Json = o
    for step in path:
        here = as_array(here, str(step))[step] if isinstance(step, int) else as_object(here, step)[step]
    return as_object(here, "path end")


def test_reach_needs_the_persons_side() -> None:
    base, before, after, report = _step()
    for a in as_array(report["actions"], "actions"):
        for u in as_array(as_object(a, "action")["units"], "units"):
            as_object(u, "unit")["deck"] = []
    _at(before, "slides", 0, "objects", "T0")["text"] = "short"
    _at(before, "slides", 0, "objects", "T1")["text"] = _at(base, "slides", 0, "elements", 1, "readback", "T1")["text"]
    assert not any(R.step_reach(base, before, after, report).values())


def test_the_fuzzer_never_asks_for_a_browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A campaign outliving its token failed each round's sync into a browser consent tab and
    waited there (2026-09-23); `counted` makes a dead token an error instead."""
    pytest.importorskip("google.oauth2.credentials")
    from beamer2slides import google_auth
    from beamer2slides.devtools import counted
    monkeypatch.setattr(google_auth, "TOKEN", tmp_path / "token.json")
    monkeypatch.setattr(google_auth, "credentials", google_auth.credentials)   # (restored afterwards)
    with pytest.raises(counted.NeedsConsent):
        counted.quiet_credentials()
    counted.never_interactive()
    with pytest.raises(counted.NeedsConsent):
        google_auth.credentials()


def _no_sleep(seconds: float) -> None:
    pass


def test_execute_counts_without_changing_what_it_does(monkeypatch: pytest.MonkeyPatch) -> None:
    from beamer2slides import gslides
    monkeypatch.setattr(gslides.time, "sleep", _no_sleep)
    mine = gslides.count_thread()

    class Req:
        methodId = "slides.presentations.get"
        calls = 0

        def execute(self, **options: Unpack[ExecuteOptions]) -> dict[str, int]:
            Req.calls += 1
            if Req.calls == 1:
                raise HttpError(SimpleNamespace(status=429, reason="Too Many"), b"{}")
            return {"ok": 1}

    assert gslides.execute(Req()) == {"ok": 1}
    assert mine["calls"] == 2 and mine["retries"] == 1 and mine["rate_limited"] == 1 and mine["backoff_s"] > 0
    assert mine["call slides.presentations.get"] == 2 and mine["retry slides.presentations.get 429"] == 1
    assert F.Cost.kept("retry slides.presentations.get 429") and F.Cost.kept(F.WRITE) and not F.Cost.kept("other")
