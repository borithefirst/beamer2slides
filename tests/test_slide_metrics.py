"""The numpy slide metrics each see their own kind of defect (devtools/slide_metrics.py)."""

import numpy as np

from beamer2slides.devtools.slide_metrics import auc, distance_to, numpy_metrics

W, H = 400, 225
SLIDE = {"size": [W, H], "elements": [{"bbox": [0, 0, W, H]}]}


def page(words=((40, 40), (40, 100), (40, 160)), colour=(0, 0, 0), ground=(255, 255, 255)):
    """A white page with a few 'words', 120 x 10 px: a row of 2 px stems 6 px apart, like letters."""
    a = np.empty((H, W, 3), np.int16)
    a[:] = ground
    for x, y in words:
        for s in range(x, x + 120, 6):
            a[y:y + 10, s:s + 2] = colour
    return a


def test_distance_to_counts_pixels_up_to_its_reach():
    m = np.zeros((20, 40), bool)
    m[10, 5] = True
    d = distance_to(m, reach=6)
    assert d[10, 5] == 0 and d[10, 8] == 3 and d[10, 30] == 7


def test_the_same_page_scores_zero_everywhere():
    s, _ = numpy_metrics(page(), page(), SLIDE)
    assert all(v == 0 for v in s.values()), s


def test_a_small_shift_is_drift_not_missing_ink():
    s, _ = numpy_metrics(page(), page(((44, 43), (44, 103), (44, 163))), SLIDE)
    assert s["overlap"] > 0.2                    # overlap's cliff: 3-4 px off reads as ink gone
    assert s["missing"] == 0 and s["extra"] == 0
    assert 1 < s["drift"] < 6
    assert s["graded"] < s["overlap"] / 2


def test_a_lost_word_is_missing_and_one_too_many_is_extra():
    s, _ = numpy_metrics(page(), page(((40, 40), (40, 100))), SLIDE)
    assert 0.3 < s["missing"] < 0.36 and s["extra"] == 0
    s, _ = numpy_metrics(page(), page(((40, 40), (40, 100), (40, 160), (240, 100))), SLIDE)
    assert s["missing"] == 0 and 0.22 < s["extra"] < 0.28


def test_a_recoloured_word_shows_in_ink_de_only():
    s, _ = numpy_metrics(page(), page(colour=(160, 0, 0)), SLIDE)
    assert s["overlap"] == 0 and s["missing"] == 0
    assert s["ink_de"] > 30 and s["ground_de"] == 0


def test_a_tinted_page_shows_in_ground_de():
    s, _ = numpy_metrics(page(), page(ground=(235, 240, 255)), SLIDE)
    assert s["ground_de"] > 5 and s["ink_de"] == 0


def test_words_moved_on_a_panel_are_local_ink_moved():
    """A yellow panel is ink against the white page, so the words on it hide inside it; against
    their own surroundings they are words that moved."""
    def on_panel(words):
        a = page(())
        a[30:200, 20:380] = (250, 190, 0)
        for x, y in words:
            for s in range(x, x + 120, 6):
                a[y:y + 10, s:s + 2] = 0
        return a
    s, _ = numpy_metrics(on_panel(((40, 60), (40, 120))), on_panel(((40, 68), (40, 128))), SLIDE)
    assert s["missing"] < 0.02 and s["extra"] < 0.02 and s["graded"] < 0.02
    assert s["local_graded"] > 0.1 and s["local_drift"] > 2
    s, _ = numpy_metrics(on_panel(((40, 60), (40, 120))), on_panel(((40, 60),)), SLIDE)
    assert s["missing"] < 0.02 and 0.4 < s["local_missing"] < 0.6


def test_the_worst_tile_finds_one_wrong_word():
    words = [(x, y) for x in (20, 200) for y in (20, 60, 100, 140, 180)]
    moved = words[:-1] + [(215, 195)]
    s, _ = numpy_metrics(page(words), page(moved), SLIDE)
    assert s["tile_overlap"] > 3 * s["overlap"]


def test_auc_ranks_positives_above_negatives():
    assert auc([3, 4], [1, 2]) == 1.0
    assert auc([1, 2], [3, 4]) == 0.0
    assert auc([1, 1], [1, 1]) == 0.5
    assert auc([], [1]) is None
