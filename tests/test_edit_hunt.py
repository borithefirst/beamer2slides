"""The edit hunt's harness, offline: what an editor is shown of a deck, and how a sync's request
count is read back from its output."""

from beamer2slides.devtools import edit_hunt

EMU = 12700


def box(x, y, w, h):
    return {"size": {"width": {"magnitude": w * EMU, "unit": "EMU"}, "height": {"magnitude": h * EMU, "unit": "EMU"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x * EMU, "translateY": y * EMU, "unit": "EMU"}}


def text(s):
    return {"textElements": [{"textRun": {"content": s}}]}


PRES = {"presentationId": "P", "title": "talk",
        "pageSize": {"width": {"magnitude": 720 * EMU}, "height": {"magnitude": 540 * EMU}},
        "slides": [{"objectId": "s1", "slideProperties": {"layoutObjectId": "L1"}, "pageElements": [
            {"objectId": "t1", "title": "b2s:a/text/title/0", **box(10, 10, 600, 40),
             "shape": {"shapeType": "TEXT_BOX", "placeholder": {"type": "TITLE"}, "text": text("Results\n")}},
            {"objectId": "g1", "transform": {"scaleX": 1, "scaleY": 1, "translateX": 100 * EMU, "translateY": 0, "unit": "EMU"},
             "elementGroup": {"children": [
                 {"objectId": "b1", **box(0, 100, 200, 50),
                  "shape": {"shapeType": "TEXT_BOX", "text": text("first\nsecond\n")}}]}}]}]}


def test_the_dump_names_ids_absolute_boxes_and_paragraphs():
    out = edit_hunt.dump(PRES)
    assert "720.0 x 540.0 pt slide" in out
    assert "## slide 1  id=s1  layout=L1  title='Results'" in out
    assert "- t1 TEXT_BOX/TITLE [10.0, 10.0, 600.0, 40.0] alt=b2s:a/text/title/0: Results" in out
    # a group's child at its place on the page, indented under the group, paragraphs split by |
    assert "  - b1 TEXT_BOX [100.0, 100.0, 200.0, 50.0]: first | second" in out


def test_the_request_count_is_the_last_one_a_sync_printed():
    assert edit_hunt.requests_of("sync: 2 source changes applied, 1 deck edits kept, requests 87\n") == 87
    assert edit_hunt.requests_of("sync: ... requests 3\n...\nsync: ... requests 0") == 0
    assert edit_hunt.requests_of("refused") is None
