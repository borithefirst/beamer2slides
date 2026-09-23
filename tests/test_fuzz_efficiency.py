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

import random

import pytest

from beamer2slides.devtools import deck_edits as D
from beamer2slides.devtools import fuzz_reach as R
from beamer2slides.devtools import fuzz_sync as F
from beamer2slides.devtools.sync_check import Model

NBSP = chr(0xA0)
EMU = 12700


def _shape(oid, text, y=100, placeholder=None):
    shape = {"shapeType": "TEXT_BOX", "text": {"textElements": [{"textRun": {"content": text + "\n"}}]}}
    if placeholder:
        shape["placeholder"] = {"type": placeholder}
    return {"objectId": oid, "size": {"width": {"magnitude": 3000000, "unit": "EMU"},
                                      "height": {"magnitude": 3000000, "unit": "EMU"}},
            "transform": {"scaleX": 1, "scaleY": 0.2, "translateX": EMU * 50, "translateY": EMU * y, "unit": "EMU"},
            "shape": shape}


def _cell(text):
    return {"text": {"textElements": [{"textRun": {"content": text + "\n"}}]}}


def _pres():
    table = {"objectId": "t1", "size": {"width": {"magnitude": 3000000, "unit": "EMU"},
                                        "height": {"magnitude": 3000000, "unit": "EMU"}},
             "transform": {"scaleX": 1, "scaleY": 0.3, "translateX": EMU * 50, "translateY": EMU * 250, "unit": "EMU"},
             "table": {"rows": 2, "columns": 2, "tableRows": [{"tableCells": [_cell("Scenario"), _cell("Time")]},
                                                              {"tableCells": [_cell("Disjoint"), _cell("3.9 s")]}]}}
    return {"presentationId": "p", "slides": [
        {"objectId": "s1", "slideProperties": {"layoutObjectId": "L"}, "pageElements": [_shape("h1", "Merging text", 20, "TITLE"),
                                            _shape("a1", "First slide words here\nA second paragraph of it"),
                                            _shape("a2", f"More text with x{NBSP * 4} hole after it", 200)]},
        {"objectId": "s2", "slideProperties": {"layoutObjectId": "L"}, "pageElements": [_shape("h2", "Results", 20, "TITLE"),
                                            _shape("b1", "Second slide says things"), table]},
    ]}


class FakeApi:
    """presentations().batchUpdate/get(...).execute() recording what was sent; `refuse(reqs)` says
    which batches Google turns down (one refused request throws out its whole batch)."""

    def __init__(self, pres, refuse=lambda reqs: False):
        self.pres, self.refuse, self.sent, self.gets, self._next = pres, refuse, [], 0, None

    def presentations(self):
        return self

    def batchUpdate(self, presentationId, body):
        self._next = ("write", body["requests"])
        return self

    def get(self, presentationId, **kw):
        self._next = ("read", None)
        return self

    def execute(self, **kw):
        from beamer2slides.gapi import HttpError
        what, reqs = self._next
        if what == "read":
            self.gets += 1
            return self.pres
        if self.refuse(reqs):
            raise HttpError(type("Resp", (), {"status": 400, "reason": "Bad Request"})(), b"{}")
        self.sent.append(reqs)
        return {}


def _deck(refuse=lambda reqs: False, defer=True):
    pres = _pres()
    return D.LiveDeck("p", api=FakeApi(pres, refuse), pres=pres, defer=defer)


def test_deferred_edits_are_one_batch_and_mark_their_slides():
    deck = _deck()
    D.append_sentence(deck, "Merging text", "First slide words", "Added.")
    D.insert_table_row(deck, "Results", "Disjoint", ["Renames", "5.5 s"])
    assert deck.dirty == {"s1", "s2"} and not deck.reshaped and not deck.api.sent
    assert deck.flush() == []
    assert len(deck.api.sent) == 1 and deck.writes == 1
    kinds = [next(iter(r)) for r in deck.api.sent[0]]
    assert kinds == ["insertText", "insertTableRows", "insertText", "insertText"]


def test_a_refused_batch_costs_only_the_edit_google_refused():
    deck = _deck(refuse=lambda reqs: any("insertTableRows" in r for r in reqs))
    D.append_sentence(deck, "Merging text", "First slide words", "Added.")
    D.insert_table_row(deck, "Results", "Disjoint", ["Renames", "5.5 s"])
    D.bold(deck, "Results", "Second", "Second slide says things")
    assert deck.flush() == [1]
    assert [next(iter(reqs[0])) for reqs in deck.api.sent] == ["insertText", "updateTextStyle"]


def test_the_immediate_way_is_unchanged():
    deck = _deck(defer=False)
    D.append_sentence(deck, "Merging text", "First slide words", "Added.")
    assert len(deck.api.sent) == 1 and deck.api.gets == 1 and not deck.pending


def test_stale_reads_again_only_for_what_a_queued_edit_touched():
    deck = _deck()
    bold = {"edit": "bold", "args": {"slide": "Merging text", "word": "slide"}}
    other = {"edit": "bold", "args": {"slide": "Results", "word": "slide"}}
    assert not F.stale(deck, bold)                       # nothing queued
    D.append_sentence(deck, "Merging text", "First slide words", "Added.")
    assert F.stale(deck, bold) and not F.stale(deck, other)
    assert not F.stale(deck, {"edit": "move_slide", "args": {"slide": "Results", "after": {"index": 1}}})
    D.add_slide(deck, "Results", "A new slide")
    assert deck.reshaped
    assert F.stale(deck, {"edit": "move_slide", "args": {"slide": "Results", "after": {"index": 1}}})
    assert F.stale(deck, {"edit": "set_notes", "args": {"slide": {"index": 1}, "text": "x"}})
    assert F.stale(deck, {"edit": "set_notes", "args": {"slide": {"contains": "Second"}, "text": "x"}})


def test_new_text_kinds_write_where_they_say():
    deck = _deck()
    exp = D.add_paragraph(deck, "Merging text", "First slide words", "Typed by the person")
    (req,), = deck.pending
    assert req["insertText"]["text"] == "\nTyped by the person"
    assert req["insertText"]["insertionIndex"] == len("First slide words here")
    assert {c["text"] for c in exp["checks"]} == {"Typed by the person", "First slide words"}
    exp = D.insert_before_hole(deck, "Merging text", "More text with", "quite")
    req = deck.pending[-1][0]["insertText"]
    assert req == {"objectId": "a2", "text": "quite ", "insertionIndex": len("More text with x")}
    assert {"check": "text", "slide": "Merging text", "text": "x quite", "count": 1} in exp["checks"]
    with pytest.raises(D.CheckError):
        D.insert_before_hole(deck, "Merging text", "First slide words", "no hole here")


def test_table_kinds_insert_and_fill():
    deck = _deck()
    D.insert_table_column(deck, "Results", "Time", ["Runs", "x12", "ignored"])
    reqs = deck.pending[-1]
    assert reqs[0] == {"insertTableColumns": {"tableObjectId": "t1", "cellLocation": {"rowIndex": 0, "columnIndex": 1},
                                              "insertRight": True, "number": 1}}
    assert [(r["insertText"]["cellLocation"], r["insertText"]["text"]) for r in reqs[1:]] == [
        ({"rowIndex": 0, "columnIndex": 2}, "Runs"), ({"rowIndex": 1, "columnIndex": 2}, "x12")]
    with pytest.raises(D.CheckError):
        D.insert_table_row(deck, "Results", "Second slide says", ["a", "b"])   # not a cell


def test_every_edit_kind_is_in_the_catalogue_and_the_reach_table():
    kinds = {s["edit"] for s in D.catalogue("https://example.invalid/picture.png")}
    assert set(D.EDITS) <= kinds
    assert set(D.EDITS) - set(R.EDIT_FIELDS) <= {"add_slide", "duplicate_slide", "delete_slide", "move_slide",
                                                  "set_notes", "set_background"}


def test_focus_draws_aimed_edits_on_the_aimed_slides():
    model = Model(_pres())
    rng = random.Random(5)
    got = [F.random_spec(model, rng, None, "layout", {"s2"}) for _ in range(300)]
    aimed = [s for s in got if s and "aim" in s]
    assert len(aimed) > 150
    assert {s["aim"] for s in aimed} >= {"long_text", "new_paragraph", "hole", "move_text", "row", "column", "cell"}
    on_s2 = sum(1 for s in aimed if s["args"].get("slide") == {"title": "Results"})
    assert on_s2 > len(aimed) * 0.5
    # no aimed edit writes in a title or the frame counter
    assert not [s for s in aimed if "Merging text" == s["args"].get("text") or "Results" == s["args"].get("text")]
    # the default draw is untouched: the same seed draws the same specs as before focus existed
    a = [F.random_spec(model, random.Random(9)) for _ in range(1)]
    b = [F.random_spec(model, random.Random(9), None, None, ()) for _ in range(1)]
    assert a == b and not any("aim" in (s or {}) for s in a)


def test_hole_aim_types_in_front_of_the_hole():
    model = Model(_pres())
    rng = random.Random(1)
    specs = [F._aim_hole(rng, model, model.slides[0], {"title": "Merging text"}, None) for _ in range(20)]
    for spec in filter(None, specs):
        deck = _deck()
        D.apply(deck, spec)   # found by content in the same read
    assert {s["edit"] for s in specs if s} == {"insert_before_hole", "replace_word"}


def test_variant_weights_prefer_reflow_and_stay_uniform_by_default():
    build = F.sync_build()
    uniform = F.variant_weights(build, None)
    assert set(uniform.values()) == {1.0} and "v1" not in uniform
    focused = F.variant_weights(build, "layout")
    assert focused["table-row"] > focused.get("notes", 0.25)
    assert "Results" in F.variant_slides(build, "table-row")
    assert F.start_variant("layout") == F.start_variant(None) == "v1" and F.start_variant("probes") == "probes"
    assert set(F.variant_weights(build, "probes")) == {"probes-reword", "probes-push"}
    assert F.variant_slides(build, "probes-push") == {"Room to grow", "Two boxes", "Display math"}


def _unit(key, action="recreate", source=(), deck=()):
    return {"key": key, "action": action, "source": list(source), "deck": list(deck)}


def _step():
    """A slide where the person lengthened a text, typed in front of a hole, moved a text and grew
    the table, and the source re-laid all of them."""
    words = f"Holes x{NBSP * 3} here"
    base = {"slides": [{"key": "s", "objectId": "S", "groups": [], "elements": [
        {"key": "text/body/0", "kind": "text", "objects": ["T0"], "main": "T0", "readback": {"T0": {"text": "short"}}},
        {"key": "text/body/1", "kind": "text", "objects": ["T1"], "main": "T1", "readback": {"T1": {"text": words}}},
        {"key": "image/math/0", "kind": "image", "role": "math", "anchor": "text/body/1", "objects": ["M"], "main": "M"},
        {"key": "text/body/2", "kind": "text", "objects": ["T2"], "main": "T2", "readback": {"T2": {"text": "mover"}}},
        {"key": "table/0", "kind": "table", "objects": ["TB"], "main": "TB"}]}]}
    before = {"slides": [{"objectId": "S", "objects": {
        "T0": {"text": "short and then much longer", "box": [0, 0, 100, 20]},
        "T1": {"text": f"Holes quite x{NBSP * 3} here", "box": [0, 30, 100, 50]},
        "T2": {"text": "mover", "box": [0, 60, 100, 80]}, "TB": {"text": "", "box": [0, 100, 100, 150]}}}]}
    after = {"slides": [{"objectId": "S", "objects": {
        "n0": {"title": "b2s:s/text/body/0", "box": [0, 0, 100, 40]},
        "n2": {"title": "b2s:s/text/body/2", "box": [0, 35, 100, 55]}}}]}
    report = {"actions": [{"slide": "s", "units": [
        _unit("text/body/0", source=["text"], deck=["text"]),
        _unit("text/body/1", source=["size"], deck=["text"]),
        _unit("text/body/2", action="keep", deck=["geometry"]),
        _unit("table/0", action="keep", source=["text"], deck=["text"])]}]}
    return base, before, after, report


def test_reach_sees_each_precondition_it_names():
    base, before, after, report = _step()
    got = R.step_reach(base, before, after, report)
    # (the words typed in front of the hole made that text longer too)
    assert [w["unit"] for w in got["text_into_relaid"]] == ["text/body/0", "text/body/1"]
    assert [w["unit"] for w in got["hole_reworded"]] == ["text/body/1"]
    assert got["moved_vs_reflow"] and got["moved_overlapped"]
    assert [w["unit"] for w in got["table_grows"]] == ["table/0"]


def test_reach_needs_the_persons_side():
    base, before, after, report = _step()
    for a in report["actions"]:
        for u in a["units"]:
            u["deck"] = []
    before["slides"][0]["objects"]["T0"]["text"] = "short"
    before["slides"][0]["objects"]["T1"]["text"] = base["slides"][0]["elements"][1]["readback"]["T1"]["text"]
    assert not any(R.step_reach(base, before, after, report).values())


def test_execute_counts_without_changing_what_it_does(monkeypatch):
    from beamer2slides import gslides
    from beamer2slides.gapi import HttpError
    monkeypatch.setattr(gslides.time, "sleep", lambda s: None)
    mine = gslides.count_thread()

    class Req:
        methodId = "slides.presentations.get"
        calls = 0

        def execute(self, **kw):
            Req.calls += 1
            if Req.calls == 1:
                raise HttpError(type("Resp", (), {"status": 429, "reason": "Too Many"})(), b"{}")
            return {"ok": 1}

    assert gslides.execute(Req()) == {"ok": 1}
    assert mine["calls"] == 2 and mine["retries"] == 1 and mine["rate_limited"] == 1 and mine["backoff_s"] > 0
    assert mine["call slides.presentations.get"] == 2
