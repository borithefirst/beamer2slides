"""Unit tests for the request builders in emit (local only, no Google API)."""

from beamer2slides.emit import (MIDDLE_BASELINE_EM, block_groups, connection, label_inside, merge_blocks,
                                rule_groups, template_key, text_right_limit)


def shape(bbox, shape="ROUND_2_SAME_RECTANGLE", flip=False, **extra):
    return {"id": f"s{bbox[1]}", "kind": "shape", "role": "panel", "bbox": bbox, "fill": "#262686",
            "shape": shape, "flip": flip, "radius": 4.0, "spans": [], **extra}


def text(bbox, x1=None):
    return {"id": "t", "kind": "text", "role": "body", "bbox": bbox, "spans": [],
            "paragraphs": [{"size": 11.0, "align": "left", "lines": [{"x1": x1 or bbox[2]}]}]}


BAR = shape([6.91, 70.54, 355.93, 85.33], block=0)
BODY = shape([6.91, 87.82, 355.93, 103.01], flip=True, block=0, title_bar=BAR["bbox"], strips=[],
             shadow={"size": 4.0, "pieces": []})


def test_block_body_reaches_under_its_title_bar():
    body = merge_blocks([BODY, BAR])[0]
    assert body["bbox"] == [6.91, 70.54, 355.93, 103.01]
    assert merge_blocks([BAR, BODY])[1]["id"] == BAR["id"], "the title bar is created after (above) the body"
    assert (body["shape"], body["flip"]) == ("ROUND_RECTANGLE", False), "rounded at the top and the bottom"
    square = merge_blocks([{**BODY, "shape": "RECTANGLE", "flip": False}, {**BAR, "shape": "RECTANGLE"}])[0]
    assert square["shape"] == "RECTANGLE"


def test_template_keys():
    scale = 720 / 362.83
    body = merge_blocks([BODY, BAR])[0]
    kind, adj, shadow = template_key(body, scale)
    assert kind == "ROUND_RECTANGLE" and adj == round(4.0 / 32.47, 2) and shadow == 8.0
    assert template_key({**BAR, "shape": "RECTANGLE"}, scale) is None, "plain rectangles are created directly"
    assert template_key(text([10, 10, 50, 20]), scale) is None


def test_block_group_holds_both_shapes_and_their_text():
    elements = merge_blocks([BODY, BAR]) + [text([10.9, 74, 80, 84]), text([10.9, 90, 150, 100]), text([10, 200, 50, 210])]
    ids = ["body", "bar", "title", "content", "outside"]
    assert block_groups(elements, ids, None) == [["body", "bar", "title", "content"]]


def test_progress_bar_and_track_group():
    track = shape([71.83, 140.8, 291.01, 141.19], shape="RECTANGLE", role="rule")
    bar = shape([71.83, 140.8, 203.34, 141.19], shape="RECTANGLE", role="rule")
    other = shape([10, 200, 50, 202], shape="RECTANGLE", role="rule")
    assert rule_groups([track, bar, other], ["t", "b", "o"]) == [["t", "b"]]


def test_text_right_limit():
    slide = {"size": [362.83, 272.13], "elements": [BODY]}
    inside = text([10.91, 90, 150, 100])
    slide["elements"].append(inside)
    assert abs(text_right_limit(inside, slide) - (355.93 - 4.0)) < 0.01, "mirrors the block's inner margin"
    left, right = text([20, 100, 120, 110]), text([190, 95, 300, 115])
    two_columns = {"size": [362.83, 272.13], "elements": [left, right]}
    assert text_right_limit(left, two_columns) == 190 - 11.0, "stops before the other column"
    assert text_right_limit(right, two_columns) == 362.83 - 20, "the page's text margin, mirrored"


def node(bbox, shape="RECTANGLE", label_w=30.0, text="Start"):
    return {"bbox": bbox, "shape": shape, "label_w": label_w,
            "paragraphs": [[{"text": text}]] if text else []}


def test_labels_go_inside_nodes_that_fit_them():
    assert label_inside(node([0, 0, 40, 15]))
    assert not label_inside(node([0, 0, 40, 40], "ELLIPSE")), "an ellipse's text rectangle is its inscribed square"
    assert label_inside(node([0, 0, 40, 40], "ELLIPSE", label_w=8, text="A"))
    assert not label_inside(node([0, 0, 60, 30], "TRIANGLE")), "a triangle's text sits in its lower half"
    assert not label_inside(node([0, 0, 40, 15], text=""))


def test_edges_connect_to_node_sites():
    nodes = [node([10, 10, 50, 30]), node([100, 0, 140, 40], "ELLIPSE"), {"bbox": [60, 5, 90, 12], "shape": None}]
    oids = ["a", "b", "label"]
    assert connection([50, 20], nodes, oids) == {"connectedObjectId": "a", "connectionSiteIndex": 3}
    assert connection([100.5, 20], nodes, oids) == {"connectedObjectId": "b", "connectionSiteIndex": 2}
    assert connection([75, 20], nodes, oids) is None


def test_middle_alignment_model():
    assert abs(MIDDLE_BASELINE_EM - 0.362) < 0.01  # tools/probe_middle.py
