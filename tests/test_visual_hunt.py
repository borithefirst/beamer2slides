"""The visual hunt's harness, offline: the skeptics' ledger and fidelity's pre-screen."""

import json
from pathlib import Path

from beamer2slides.devtools import visual_hunt
from beamer2slides.json_types import JsonObject


def finding(klass: str | None, verdict: str, severity: object, realism: object, deck: str) -> JsonObject:
    out: JsonObject = {"id": f"{deck}-{klass}", "deck": deck, "slide": 2, "verdict": verdict, "title": "t",
                       "mechanism": "m"}
    if klass is not None:
        out["class"] = klass
    for key, v in (("severity", severity), ("realism", realism)):
        if isinstance(v, (int, str)) or v is None:
            out[key] = v
    return out


def test_the_ledger_groups_confirmed_findings_by_class_worst_first(tmp_path: Path) -> None:
    (tmp_path / "h1.json").write_text(json.dumps([
        finding("wrap", "CONFIRMED", 2, "3", "a"), finding("wrap", "REJECTED", 5, 5, "b"),
        finding(None, "CONFIRMED", None, 1, "c")]), encoding="utf-8")
    (tmp_path / "h2.json").write_text(json.dumps([finding("drift", "CONFIRMED", 4, 2, "d"),
                                                   finding("wrap", "CONFIRMED", 1, 1, "e")]), encoding="utf-8")
    rows = [c.json() for c in visual_hunt.ledger(tmp_path)]
    assert [(c["class"], c["worst"]) for c in rows] == [("drift", 8), ("wrap", 6), ("?", 0)]
    assert rows[1]["findings"] == [
        {"hunter": "h1", "id": "a-wrap", "deck": "a", "slide": 2, "severity": 2, "realism": "3", "title": "t",
         "mechanism": "m"},
        {"hunter": "h2", "id": "e-wrap", "deck": "e", "slide": 2, "severity": 1, "realism": 1, "title": "t",
         "mechanism": "m"}]


def test_fidelitys_odd_elements_are_those_past_the_bar() -> None:
    fine: JsonObject = {"dx_pt": 1, "dy_top_pt": -2.5, "width_ratio": 1.05, "lines_ref": 2, "lines_slides": 2}
    moved: JsonObject = {**fine, "dx_pt": -3.5}
    wrapped: JsonObject = {**fine, "lines_slides": 3}
    missing: JsonObject = {"missing": True, "lines_ref": 1, "lines_slides": 1}
    assert visual_hunt.odd_elements({"elements": [fine, moved, wrapped, missing]}) == (moved, wrapped, missing)
    assert visual_hunt.odd_elements({}) == ()
