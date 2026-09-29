"""The layout probes' measures, offline: two inks' overlap and a text's formula holes."""

import numpy as np

from beamer2slides.devtools import probe_layout as pl
from beamer2slides.json_types import JsonObject


def ink(h: int, w: int, rects: list[tuple[int, int, int, int]]) -> pl.Ink:
    mask = np.zeros((h, w), bool)
    for x0, y0, x1, y1 in rects:
        mask[y0:y1, x0:x1] = True
    return pl.Ink(mask=mask, per_pt=2.0)


def test_two_inks_apart_have_a_clearance_and_no_band() -> None:
    above, below = ink(100, 100, [(10, 10, 50, 30)]), ink(100, 100, [(20, 40, 60, 70)])
    found = pl.overlap_of(above, below, 6.0)
    assert found.band_pt == 0.0 and found.at is None and found.width_pt == 0.0
    assert found.clearance_pt == 5.0   # rows 30..40 between them, 2 px per pt
    assert pl.overlap(above, below) == found.json()


def test_inks_running_into_each_other_have_a_band_where_they_do() -> None:
    a, b = ink(100, 100, [(0, 10, 48, 40)]), ink(100, 100, [(24, 30, 100, 60)])
    found = pl.overlap_of(a, b, 6.0)
    assert found.band_pt == 5.0 and found.clearance_pt == -5.0
    assert found.at == (12.0, 15.0, 24.0, 20.0) and found.width_pt == 12.0
    assert found.pixels_pt2 == 60.0
    assert a.box == [0.0, 5.0, 24.0, 20.0]
    assert pl.Ink(mask=np.zeros((4, 4), bool), per_pt=1.0).box is None


def run(content: str, start: int, family: str) -> JsonObject:
    return {"startIndex": start, "endIndex": start + len(content),
            "textRun": {"content": content, "style": {"weightedFontFamily": {"fontFamily": family}}}}


def test_formula_holes_are_runs_of_no_break_spaces_in_roboto_mono_joined() -> None:
    nb = "\xa0"
    shape: JsonObject = {"text": {"textElements": [
        {"endIndex": 5, "paragraphMarker": {}},
        {**run("word ", 0, "Lato")}, {**run(nb * 3, 5, "Roboto Mono")}, {**run(nb * 2, 8, "Roboto Mono")},
        {**run(" and ", 10, "Roboto Mono")}, {**run(nb, 15, "Roboto Mono")}]}}
    assert pl.hole_runs(shape) == [(5, 10), (15, 16)]
    assert pl.hole_runs({}) == []
