"""The adopt benchmark's own arithmetic (offline, no corpus)."""

import numpy as np

from beamer2slides.devtools import adopt_bench


def slide(*boxes) -> dict:
    return {"size": [100, 50], "elements": [{"kind": "shape", "id": f"e{k}", "bbox": list(b)}
                                            for k, b in enumerate(boxes)]}


def test_element_losses_split_one_minus_boxes_among_the_elements():
    """Each pixel the score counts against a slide is charged to the smallest box holding it, so the
    losses add up to 1 - boxes and the element that set its ink wrong carries it."""
    s = slide((0, 0, 100, 50), (10, 10, 30, 20))          # a backdrop and a small box on it
    ref = np.zeros((50, 100), dtype=bool)
    got = np.zeros((50, 100), dtype=bool)
    ref[12:18, 12:20] = True                              # the small box's words...
    got[12:18, 22:28] = True                              # ...set 10 px to the right
    ref[40:45, 60:90] = True                              # and the backdrop's, right
    got[40:45, 60:90] = True
    cov = adopt_bench.covered_mask(s, 100, 50)
    boxes = adopt_bench.overlap(ref & cov, got & cov)
    out = adopt_bench.element_losses(ref, got, s)
    assert [e["id"] for e in out] == ["e1"]
    assert abs(sum(e["loss"] for e in out) - (1 - boxes)) < 1e-3
    assert out[0]["miss"] > 0 and out[0]["extra"] > 0


def test_the_cache_key_follows_the_source_and_the_ir(tmp_path):
    (tmp_path / "main.tex").write_text("a", encoding="utf-8")
    k = adopt_bench.cache_key(tmp_path, {"slides": []})
    assert k == adopt_bench.cache_key(tmp_path, {"slides": []})
    assert k != adopt_bench.cache_key(tmp_path, {"slides": [1]})
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures" / "x.png").write_bytes(b"1")
    assert k != adopt_bench.cache_key(tmp_path, {"slides": []})
