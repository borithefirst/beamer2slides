"""Unit tests for the request builders in emit (local only, no Google API)."""

from collections.abc import Sequence

from beamer2slides.emit import (MIDDLE_BASELINE_EM, block_groups, connection, label_inside, merge_blocks,
                                rule_groups, template_key, text_right_limit)
from beamer2slides.json_types import Json, JsonObject

from .json_reads import jarr

ROUND_TOP = "ROUND_2_SAME_RECTANGLE"  # a block's title bar: rounded at the top


def shape(bbox: Sequence[float], shape: str, flip: bool, **extra: Json) -> JsonObject:
    box: list[Json] = [v for v in bbox]
    el: JsonObject = {"id": f"s{bbox[1]}", "kind": "shape", "role": "panel", "bbox": box, "fill": "#262686",
                      "shape": shape, "flip": flip, "radius": 4.0, "spans": []}
    el.update(extra)
    return el


def text(bbox: Sequence[float]) -> JsonObject:
    box: list[Json] = [v for v in bbox]
    return {"id": "t", "kind": "text", "role": "body", "bbox": box, "spans": [],
            "paragraphs": [{"size": 11.0, "align": "left", "lines": [{"x1": bbox[2]}]}]}


BAR = shape([6.91, 70.54, 355.93, 85.33], ROUND_TOP, False, block=0)
BODY = shape([6.91, 87.82, 355.93, 103.01], ROUND_TOP, True, block=0, title_bar=BAR["bbox"], strips=[],
             shadow={"size": 4.0, "pieces": []})


def test_block_body_reaches_under_its_title_bar() -> None:
    body = merge_blocks([BODY, BAR])[0]
    assert body["bbox"] == [6.91, 70.54, 355.93, 103.01]
    assert merge_blocks([BAR, BODY])[1]["id"] == BAR["id"], "the title bar is created after (above) the body"
    assert (body["shape"], body["flip"]) == ("ROUND_RECTANGLE", False), "rounded at the top and the bottom"
    square_body: JsonObject = {**BODY, "shape": "RECTANGLE", "flip": False}
    square_bar: JsonObject = {**BAR, "shape": "RECTANGLE"}
    square = merge_blocks([square_body, square_bar])[0]
    assert square["shape"] == "RECTANGLE"


def test_template_keys() -> None:
    scale = 720 / 362.83
    body = merge_blocks([BODY, BAR])[0]
    key = template_key(body, scale)
    assert key is not None
    kind, adj, shadow = key
    assert kind == "ROUND_RECTANGLE" and adj == round(4.0 / 32.47, 2) and shadow == 8.0
    assert template_key({**BAR, "shape": "RECTANGLE"}, scale) is None, "plain rectangles are created directly"
    assert template_key(text([10, 10, 50, 20]), scale) is None


def test_block_group_holds_both_shapes_and_their_text() -> None:
    elements = merge_blocks([BODY, BAR]) + [text([10.9, 74, 80, 84]), text([10.9, 90, 150, 100]), text([10, 200, 50, 210])]
    ids = ["body", "bar", "title", "content", "outside"]
    assert block_groups(elements, ids, None) == [["body", "bar", "title", "content"]]


def test_progress_bar_and_track_group() -> None:
    track = shape([71.83, 140.8, 291.01, 141.19], "RECTANGLE", False, role="rule")
    bar = shape([71.83, 140.8, 203.34, 141.19], "RECTANGLE", False, role="rule")
    other = shape([10, 200, 50, 202], "RECTANGLE", False, role="rule")
    assert rule_groups([track, bar, other], ["t", "b", "o"]) == [["t", "b"]]


def test_text_right_limit() -> None:
    slide: JsonObject = {"size": [362.83, 272.13], "elements": [BODY]}
    inside = text([10.91, 90, 150, 100])
    jarr(slide, "elements").append(inside)
    limit = text_right_limit(inside, slide)
    assert limit is not None
    assert abs(limit - (355.93 - 4.0)) < 0.01, "mirrors the block's inner margin"
    left, right = text([20, 100, 120, 110]), text([190, 95, 300, 115])
    two_columns: JsonObject = {"size": [362.83, 272.13], "elements": [left, right]}
    assert text_right_limit(left, two_columns) == 190 - 11.0, "stops before the other column"
    assert text_right_limit(right, two_columns) == 362.83 - 20, "the page's text margin, mirrored"


def node(bbox: Sequence[float], shape: str, label_w: float, text: str) -> JsonObject:
    box: list[Json] = [v for v in bbox]
    paragraphs: list[Json] = [[{"text": text}]] if text else []
    return {"bbox": box, "shape": shape, "label_w": label_w, "paragraphs": paragraphs}


def test_labels_go_inside_nodes_that_fit_them() -> None:
    assert label_inside(node([0, 0, 40, 15], "RECTANGLE", 30.0, "Start"))
    assert not label_inside(node([0, 0, 40, 40], "ELLIPSE", 30.0, "Start")), \
        "an ellipse's text rectangle is its inscribed square"
    assert label_inside(node([0, 0, 40, 40], "ELLIPSE", 8, "A"))
    assert not label_inside(node([0, 0, 60, 30], "TRIANGLE", 30.0, "Start")), "a triangle's text sits in its lower half"
    assert not label_inside(node([0, 0, 40, 15], "RECTANGLE", 30.0, ""))


def test_edges_connect_to_node_sites() -> None:
    nodes: list[JsonObject] = [node([10, 10, 50, 30], "RECTANGLE", 30.0, "Start"), node([100, 0, 140, 40], "ELLIPSE", 30.0, "Start"),
             {"bbox": [60, 5, 90, 12], "shape": None}]
    oids = ["a", "b", "label"]
    assert connection([50, 20], nodes, oids) == {"connectedObjectId": "a", "connectionSiteIndex": 3}
    assert connection([100.5, 20], nodes, oids) == {"connectedObjectId": "b", "connectionSiteIndex": 2}
    assert connection([75, 20], nodes, oids) is None


def test_middle_alignment_model() -> None:
    assert abs(MIDDLE_BASELINE_EM - 0.362) < 0.01  # tools/probe_middle.py
