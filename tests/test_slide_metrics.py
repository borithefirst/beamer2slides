"""The numpy slide metrics each see their own kind of defect (devtools/slide_metrics.py)."""

import json

import numpy as np
import pytest

from beamer2slides.devtools.slide_metrics import (SEVERITY, SEVERITY_CAP, auc, auc_within, content_lost_auc,
                                                   content_lost_split, distance_to, fit_missing_weight,
                                                   numpy_metrics, severity)


def test_severity_counts_what_passes_its_threshold_each_capped():
    """A gradient's ground_de sixty times its threshold is one defect, not sixty: each metric counts
    at most SEVERITY_CAP, and one below its threshold counts nothing."""
    th = {"ground_de": 1.0, "missing": 0.1, "dino": 0.2}
    total, over = severity({"ground_de": 60.0, "missing": 0.3, "dino": 0.1, "unrelated": 9.0}, th)
    assert over == {"ground_de": SEVERITY_CAP, "missing": pytest.approx(3.0)}
    assert total == pytest.approx(SEVERITY_CAP + 3.0)


def test_severity_weights_scale_a_ratio_before_its_cap():
    """weights=None (the default) is exactly the unweighted 1x sum; a weight on one metric scales its
    ratio before SEVERITY_CAP, so a big enough weight can push it past the cap while an unweighted
    metric elsewhere is unaffected."""
    th = {"missing": 0.1, "graded": 0.1}
    r = {"missing": 0.3, "graded": 0.3}                        # each 3x its threshold, unweighted
    total0, over0 = severity(r, th)
    assert over0 == {"missing": pytest.approx(3.0), "graded": pytest.approx(3.0)}
    total1, over1 = severity(r, th, {"missing": 4.0})
    assert over1 == {"missing": SEVERITY_CAP, "graded": pytest.approx(3.0)}       # 3 x 4 = 12, capped
    assert total1 > total0


def test_content_lost_split_separates_words_or_a_picture_gone_from_other_defects_and_identical():
    """CONTENT_LOST (text_missing, picture_missing) slides are `pos`; some other named defect is `neg`;
    a judged-identical slide (an empty category set) is neither - a threshold already separates those."""
    labels = {("d", 1): {"text_missing"}, ("d", 2): {"picture_missing", "shape"},
              ("d", 3): {"line_breaks"}, ("d", 4): set()}
    pos, neg = content_lost_split(list(labels), labels)
    assert set(pos) == {("d", 1), ("d", 2)}
    assert neg == [("d", 3)]


def test_fit_missing_weight_ranks_a_content_lost_slide_over_a_drift_one():
    """Two slides cross their thresholds by the same total at 1x - one because words are gone
    (missing, local_missing), the other only from drift (graded, extra). Unweighted, severity ranks
    the drift one worse (`SEVERITY`'s plain sum has no opinion about which metric it came from);
    `fit_missing_weight` should find a weight that flips this, since that ordering is exactly what it
    is calibrated on."""
    thresholds = {"missing": 0.1, "local_missing": 0.1, "graded": 0.1, "extra": 0.1}
    rows = {("a", 1): {"missing": 0.4, "local_missing": 0.4, "graded": 0.0, "extra": 0.0},
            ("b", 1): {"missing": 0.0, "local_missing": 0.0, "graded": 0.5, "extra": 0.5}}
    labels = {("a", 1): {"text_missing"}, ("b", 1): {"line_breaks"}}
    assert severity(rows[("a", 1)], thresholds)[0] < severity(rows[("b", 1)], thresholds)[0]     # before: loses

    weight, auc_after, pairs = fit_missing_weight(rows, list(rows), labels, thresholds)
    assert weight > 1.0
    assert auc_after == pytest.approx(1.0)
    assert pairs == 1                                    # no same-deck pair: fell back to the one pair there is

    weighted = {m: weight for m in ("missing", "local_missing")}
    assert severity(rows[("a", 1)], thresholds, weighted)[0] > severity(rows[("b", 1)], thresholds, weighted)[0]


def test_fit_missing_weight_needs_a_content_lost_example_to_calibrate_against():
    """With no CONTENT_LOST-labelled slide (or nothing to rank it over) there is nothing to fit: the
    weight stays 1x rather than guessed."""
    thresholds = {"missing": 0.1}
    rows = {("a", 1): {"missing": 0.5}, ("b", 1): {"missing": 0.5}}
    weight, a, pairs = fit_missing_weight(rows, list(rows), {("a", 1): {"line_breaks"}, ("b", 1): {"shape"}},
                                          thresholds)
    assert (weight, a, pairs) == (1.0, None, 0)
    weight, a, pairs = fit_missing_weight(rows, list(rows), {("a", 1): {"text_missing"}, ("b", 1): set()},
                                          thresholds)
    assert (weight, a, pairs) == (1.0, None, 0)               # ("b", 1) is identical, not "some other defect"


def test_content_lost_auc_falls_back_to_every_pair_without_a_same_deck_one():
    thresholds = {"missing": 0.1}
    rows = {("a", 1): {"missing": 1.0}, ("b", 1): {"missing": 0.0}}
    a, pairs = content_lost_auc(rows, [("a", 1)], [("b", 1)], thresholds, None)
    assert a == 1.0 and pairs == 1


def _write_metrics(root, deck, tag, slides):
    d = root / deck / "runs" / tag
    d.mkdir(parents=True)
    (d / "metrics.json").write_text(json.dumps({"deck": deck, "tag": tag, "slides": slides}), encoding="utf-8")


def _write_verdict(root, deck, sheets):
    d = root / "judging" / deck
    d.mkdir(parents=True)
    (d / "verdict-1.json").write_text(json.dumps({"sheets": sheets}), encoding="utf-8")


def test_calibrate_fits_a_missing_weight_and_reports_before_and_after(tmp_path, monkeypatch):
    """A miniature corpus: one deck's worst slide is real content loss (heavy missing/local_missing,
    nothing else off), another's is pure drift (graded/extra, no missing), and a handful of identical
    decks to set thresholds near zero. `calibrate` should learn a MISSING_METRICS weight from the
    judges' verdicts (persian-lit:20's own shape) and report the content-lost-vs-other-defect AUC
    improving, without needing to know anything about persian-lit itself."""
    from beamer2slides.devtools import slide_metrics as sm

    # identical decks sit at 0.01 on every SEVERITY metric, so each threshold lands there too (a
    # constant array's 95th percentile is the constant); the two defect rows cross it 5x on their own
    # metrics only, a tie at 1x (10 + 10) that a weight on missing/local_missing alone should break.
    zero = {m: 0.01 for m in SEVERITY}
    corpus = tmp_path / "corpus"
    _write_metrics(corpus, "content-lost", "t", [{"slide": 1, **zero, "missing": 0.05, "local_missing": 0.05}])
    _write_metrics(corpus, "drift-only", "t", [{"slide": 1, **zero, "graded": 0.05, "extra": 0.05}])
    for i in range(6):
        _write_metrics(corpus, f"identical{i}", "t", [{"slide": 1, **zero}])

    verdicts = tmp_path / "verdicts"
    _write_verdict(verdicts, "content-lost", [{"sheet": 1, "same": False}])
    _write_verdict(verdicts, "drift-only", [{"sheet": 1, "same": False}])
    for i in range(6):
        _write_verdict(verdicts, f"identical{i}", [{"sheet": 1, "same": True}])
    (verdicts / "findings.json").write_text(json.dumps({"trusted": [
        {"deck": "content-lost", "sheet": 1, "category": "text_missing"},
        {"deck": "drift-only", "sheet": 1, "category": "line_breaks"}]}), encoding="utf-8")

    monkeypatch.setattr(sm, "corpus_dir", lambda: corpus)
    table = sm.calibrate("t", verdicts)

    assert table["severity_weights"]["missing"] > 1.0
    assert table["severity_weights"]["local_missing"] > 1.0
    calib = table["severity_weight_calibration"]
    assert calib["n_content_lost"] == 1 and calib["n_other_defect"] == 1
    assert calib["after"]["auc"] > calib["before"]["auc"]
    assert calib["before"]["auc"] == pytest.approx(0.5)        # unweighted, a tie (10 + 10 either way)
    assert calib["after"]["auc"] == pytest.approx(1.0)         # weighted, content-lost now clearly wins

    # and the "any" judged-bad vs judged-identical check (the coarser one) did not fall apart:
    any_cat = table["categories"]["any"]
    assert any_cat["severity"]["after"]["auc"] >= any_cat["severity"]["before"]["auc"] - 1e-9

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


def test_transport_is_debiased_so_the_same_page_has_moved_nowhere():
    """Entropy alone smears a page onto itself by about a cell; the shift takes that off."""
    pytest.importorskip("torch")
    from beamer2slides.devtools.slide_metrics import Gpu
    gpu = Gpu(device="cpu")
    ink = page()[..., 0] < 128
    same = gpu.ot(ink, ink)
    assert same == {"ot_shift": 0.0, "ot_missing": 0.0, "ot_extra": 0.0}
    down = gpu.ot(ink, page(((40, 56), (40, 116), (40, 176)))[..., 0] < 128)
    assert 10 < down["ot_shift"] < 22 and down["ot_missing"] < 0.01


def test_auc_ranks_positives_above_negatives():
    assert auc([3, 4], [1, 2]) == 1.0
    assert auc([1, 2], [3, 4]) == 0.0
    assert auc([1, 1], [1, 1]) == 0.5
    assert auc([], [1]) is None


def test_auc_within_decks_does_not_score_a_decks_style():
    """Deck b's slides all score high, its defects included: across decks that looks like skill."""
    score = {("a", 1): 1, ("a", 2): 2, ("a", 3): 3, ("b", 1): 9, ("b", 2): 8, ("b", 3): 9}.get
    pos, neg = [("b", 1), ("a", 3)], [("a", 1), ("a", 2), ("b", 2)]
    assert auc([score(k) for k in pos], [score(k) for k in neg]) > 0.8
    assert auc_within([("b", 2)], [("b", 1), ("b", 3), ("a", 1)], score) == (0.0, 2)
    assert auc_within(pos, neg, score) == (1.0, 3)
    assert auc_within([("c", 1)], neg, lambda k: 0) == (None, 0)
