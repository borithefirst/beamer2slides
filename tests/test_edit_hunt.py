"""The edit hunt's harness, offline: what an editor is shown of a deck, how a sync's request
count is read back from its output, and the journey's state file."""

import json

from beamer2slides.devtools import edit_hunt
from beamer2slides.json_types import JsonObject

EMU = 12700


def box(x: float, y: float, w: float, h: float) -> JsonObject:
    return {"size": {"width": {"magnitude": w * EMU, "unit": "EMU"}, "height": {"magnitude": h * EMU, "unit": "EMU"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x * EMU, "translateY": y * EMU, "unit": "EMU"}}


def text(s: str) -> JsonObject:
    return {"textElements": [{"textRun": {"content": s}}]}


def cell(s: str) -> JsonObject:
    return {"text": text(s)}


PRES: JsonObject = {
    "presentationId": "P", "title": "talk",
    "pageSize": {"width": {"magnitude": 720 * EMU}, "height": {"magnitude": 540 * EMU}},
    "slides": [{"objectId": "s1", "slideProperties": {"layoutObjectId": "L1"}, "pageElements": [
        {"objectId": "t1", "title": "b2s:a/text/title/0", **box(10, 10, 600, 40),
         "shape": {"shapeType": "TEXT_BOX", "placeholder": {"type": "TITLE"}, "text": text("Results\n")}},
        {"objectId": "g1", "transform": {"scaleX": 1, "scaleY": 1, "translateX": 100 * EMU, "translateY": 0, "unit": "EMU"},
         "elementGroup": {"children": [
             {"objectId": "b1", **box(0, 100, 200, 50),
              "shape": {"shapeType": "TEXT_BOX", "text": text("first\nsecond\n")}}]}},
        {"objectId": "tb", **box(20, 300, 400, 80),
         "table": {"rows": 2, "columns": 2, "tableRows": [
             {"tableCells": [{**cell("a\n")}, {**cell("b\n")}]},
             {"tableCells": [{**cell("\n")}, {**cell("d\n")}]}]}},
        {"objectId": "p1", **box(0, 500, 10, 10), "shape": {"placeholder": {"type": "SLIDE_NUMBER"}}}]}]}


def test_the_dump_names_ids_absolute_boxes_and_paragraphs() -> None:
    out = edit_hunt.dump(PRES)
    assert "720.0 x 540.0 pt slide" in out
    assert "## slide 1  id=s1  layout=L1  title='Results'" in out
    assert "- t1 TEXT_BOX/TITLE [10.0, 10.0, 600.0, 40.0] alt=b2s:a/text/title/0: Results" in out
    # a group's child at its place on the page, indented under the group, paragraphs split by |
    assert "  - b1 TEXT_BOX [100.0, 100.0, 200.0, 50.0]: first | second" in out


def test_the_dump_names_a_tables_grid_and_its_cells() -> None:
    out = edit_hunt.dump(PRES)
    # an empty cell is left out; a cell is named by its row and column
    assert "- tb table 2x2 [20.0, 300.0, 400.0, 80.0]: [0,0] a; [0,1] b; [1,1] d" in out
    # a shape with no type is a "shape", a placeholder's type is said after it
    assert "- p1 shape/SLIDE_NUMBER [0.0, 500.0, 10.0, 10.0]" in out


def test_the_request_count_is_the_last_one_a_sync_printed() -> None:
    assert edit_hunt.requests_of("sync: 2 source changes applied, 1 deck edits kept, requests 87\n") == 87
    assert edit_hunt.requests_of("sync: ... requests 3\n...\nsync: ... requests 0") == 0
    assert edit_hunt.requests_of("refused") is None


STATE = """{
 "journey": "j1",
 "slot": "s1",
 "pid": "P",
 "out": "out/edithunt/s1",
 "pdf": "out/edithunt/pdfs/s1.pdf",
 "sources": [
  "v1.tex"
 ],
 "rounds": [
  {
   "n": 1,
   "edits": [
    {
     "edit": "bold",
     "args": {
      "slide": 1
     }
    }
   ],
   "synced": true
  }
 ]
}"""


def test_the_state_file_reads_back_as_it_was_written() -> None:
    state = edit_hunt.journey_state(json.loads(STATE), "state.json")
    assert json.dumps(state.json(), indent=1, ensure_ascii=False) == STATE


def test_a_round_opens_after_a_synced_one_and_stays_open_until_synced() -> None:
    state = edit_hunt.journey_state(json.loads(STATE), "state.json")
    opened = state.open_round()
    assert [(r.n, r.synced) for r in opened.rounds] == [(1, True), (2, False)]
    assert opened.open_round() is opened
    edited = opened.with_last(edit_hunt.Round(n=2, edits=({"raw": []},), synced=False))
    assert edited.rounds[0] == state.rounds[0] and edited.rounds[1].edits == ({"raw": []},)
