"""The readability calibration harness: a deterministic sample, and agreement maths that is right.

The judging itself is not tested here - it is done by people (or by readers standing in for them),
once, and its answers are committed under `tests/decks/foreign/readability_calib/verdicts`. What has
to hold is that the sample can be drawn again exactly as it was, and that the numbers the harness
prints about a set of verdicts are the numbers that set of verdicts says.
"""

from __future__ import annotations

import json
import math
import re

import pytest

from beamer2slides.devtools import readability_calib as rc


# ------------------------------------------------------------------------------------- the sample

def test_the_drawn_sample_is_the_one_that_was_judged():
    """The committed sample is what the corpus gives back: the calibration can be replayed."""
    path = rc.DATA / "sample.json"
    if not path.exists() or not rc.CORPUS.is_dir():
        pytest.skip("no corpus (out/adopt-corpus) or no committed sample")
    saved = json.loads(path.read_text(encoding="utf-8"))
    drawn = rc.sample(rc.CORPUS, saved["seed"])
    assert [{k: s[k] for k in ("deck", "index", "kind")} for s in drawn] == saved["slides"]
    pairs = rc.build_pairs(drawn, rc.human_frames(), saved["seed"])
    assert [p["id"] for p in pairs] == [p["id"] for p in saved["pairs"]]
    assert [p["left"]["what"] for p in pairs] == [p["left"] for p in saved["pairs"]]


def test_the_sample_is_stratified_and_spread_over_the_decks():
    if not rc.CORPUS.is_dir():
        pytest.skip("no corpus")
    drawn = rc.sample()
    for kind, want in rc.QUOTA.items():
        here = [s for s in drawn if s["kind"] == kind]
        assert len(here) == want, kind
        for deck in {s["deck"] for s in here}:
            assert len([s for s in here if s["deck"] == deck]) <= rc.PER_DECK
    assert len({(s["deck"], s["index"]) for s in drawn}) == len(drawn)


def test_a_frame_is_named_by_what_it_is_made_of():
    assert rc.frame_kind("[plain]\n\\begin{slidetable}[]{1,2}{3}\na & b \\\\\n\\end{slidetable}") == "table"
    assert rc.frame_kind("[plain,layout=title-slide]\n\\slidetext{1,2,3,4}{title}{Hello}") == "title"
    assert rc.frame_kind("\\slidepicture{1,2,3,4}{a.png}\n\\slidepicture{1,2,3,4}{b.png}") == "picture"
    assert rc.frame_kind("\\sliderect{1,2,3,4}\n\\slideline{1,2}{3,4}\n\\slideshape{1,2,3,4}{x}") == "shape"
    assert rc.frame_kind("\\begin{itemize}\n\\item one\n\\item two\n\\end{itemize}") == "list"
    assert rc.frame_kind("\\slidetext{1,2,3,4}{body}{Some words on a slide}") == "prose"
    # the same kinds, in the vocabulary a person writes
    assert rc.frame_kind("\\begin{tabular}{ll}\na & b \\\\\nc & d \\\\\n\\end{tabular}") == "table"
    assert rc.frame_kind("\\titlepage") == "title"


def test_a_frame_is_shown_whole_with_what_wraps_it():
    side = {"body": "[plain]\n  words\n", "lead": "{\\setbeamercolor{x}{bg=red}", "tail": "}"}
    shown = rc.show(side)
    assert shown.splitlines()[0] == "{\\setbeamercolor{x}{bg=red}"
    assert "\\begin{frame}[plain]" in shown and shown.splitlines()[-1] == "}"


def test_a_frame_too_long_to_read_is_elided_and_says_how_much_was_left_out():
    side = {"body": "[plain]\n" + "\n".join(f"  line {i}" for i in range(400)), "lead": "", "tail": ""}
    shown = rc.show(side, max_lines=50)
    assert len(shown.splitlines()) == 51                      # the elision is one line of its own
    assert "further lines" in shown and "line 399" in shown


def test_the_wrapper_of_a_frame_is_what_stands_between_the_frames():
    text = ("\\begin{document}\n% slide 1\n{\\setbeamercolor{background canvas}{bg=red}\n"
            "\\begin{frame}[plain]\n  words\n\\end{frame}\n}\n\n"
            "\\begin{frame}\n  more\n\\end{frame}\n")
    got = rc.frames_with_wrapper(text)
    assert len(got) == 2
    assert got[0]["lead"] == "{\\setbeamercolor{background canvas}{bg=red}" and got[0]["tail"] == "}"
    assert got[1]["tail"] == ""                               # a lone closing brace opens nothing


# --------------------------------------------------------------------------------- what a judge sees

def test_nothing_in_a_prompt_says_which_form_a_source_is():
    text = ("%% slides.sty - written by beamer2slides adopt, with main.tex.\n"
            "% \\slidepar: a paragraph Google Slides would draw\n"
            "\\newcommand\\slidepar[1][]{x}% as adopt wrote it\n"
            "\\def\\a{100\\% of it}\n")
    out = rc.scrub(text)
    assert "beamer2slides" not in out and "adopt" not in out
    assert "\\newcommand\\slidepar[1][]{x}" in out            # the code itself is untouched
    assert "\\def\\a{100\\% of it}" in out                    # an escaped per cent is not a comment
    assert "the deck" in out


def test_a_vocabulary_is_the_signatures_and_their_comments_not_the_package():
    if not rc.CORPUS.is_dir():
        pytest.skip("no corpus")
    deck = rc.decks()[0]
    text = rc.vocabulary_reference(rc.TAGS[-1], [deck])
    assert "\\newcommand\\slidepicture" in text or "\\newcommand\\slidetext" in text
    # a package's insides are not vocabulary: no line here defines one
    assert not re.search(r"(?m)^\\(?:newcommand|def|newenvironment)\{?\\?slides@", text)


def test_the_vocabulary_of_a_batch_is_said_once_per_form():
    if not rc.CORPUS.is_dir():
        pytest.skip("no corpus")
    pairs = rc.rebuild_pairs()
    text = rc.prompt(rc.batches(pairs)[0])
    assert text.count("### Vocabulary ") <= 3                 # two forms and the hand-written note
    for p in rc.batches(pairs)[0]:
        assert f"### Pair {p['id']}" in text
        assert f"**{p['id']} source A**" in text and f"**{p['id']} source B**" in text


# ---------------------------------------------------------------------------------- the agreement

def _pair(pid, kind="form", left="m6-a", right="ls-a"):
    return {"id": pid, "kind": kind, "slide_kind": "prose", "deck": "d", "index": int(pid[1:]),
            "human_source": "talk", "human_index": 0,
            "left": {"what": left}, "right": {"what": right}}


def _verdicts(table: dict[str, list[str]]) -> dict:
    return {f"j{i + 1}": {pid: {"prefer": v[i], "reason": ""} for pid, v in table.items()}
            for i in range(len(next(iter(table.values()))))}


def test_the_majority_of_three_votes():
    assert rc.majority(["A", "A", "B"]) == "A"
    assert rc.majority(["B", "tie", "B"]) == "B"
    assert rc.majority(["A", "B", "tie"]) == "tie"            # nobody won: the pair does not count
    assert rc.majority(["A", "tie", "tie"]) == "A"


def test_agreement_counts_only_the_pairs_the_judges_settled():
    pairs = [_pair("f1"), _pair("f2"), _pair("f3")]
    verdicts = _verdicts({"f1": ["A", "A", "B"],      # majority A
                          "f2": ["B", "B", "B"],      # majority B
                          "f3": ["A", "B", "tie"]})   # no majority
    scores = {"f1": {"A": 0.6, "B": 0.4, "prefer": "A", "margin": 0.2},
              "f2": {"A": 0.6, "B": 0.4, "prefer": "A", "margin": 0.2},
              "f3": {"A": 0.5, "B": 0.4, "prefer": "A", "margin": 0.1}}
    a = rc.agreement(pairs, verdicts, scores)
    assert a["form"]["pairs"] == 2 and a["form"]["no_majority"] == 1
    assert a["form"]["agreement"] == 0.5                      # right on f1, wrong on f2
    assert math.isnan(a["human"]["agreement"])


def test_the_inter_judge_numbers_are_the_ceiling_they_claim_to_be():
    pairs = [_pair(f"f{i}") for i in range(1, 5)]
    verdicts = _verdicts({"f1": ["A", "A", "A"], "f2": ["A", "A", "B"],
                          "f3": ["B", "B", "B"], "f4": ["A", "B", "A"]})
    scores = {p["id"]: {"A": 1.0, "B": 0.0, "prefer": "A", "margin": 1.0} for p in pairs}
    j = rc.agreement(pairs, verdicts, scores)["judges"]
    assert j["unanimous"] == 0.5                              # f1 and f3
    # 12 judge-judge comparisons, the split pairs cost two each
    assert j["pairwise"] == pytest.approx(8 / 12)
    # each judge against the other two, over the pairs where those two agreed at all
    assert j["with_majority"]["j1"] == 1.0                    # f2 and f4 split without it
    assert j["with_majority"]["j2"] == pytest.approx(2 / 3)   # alone on f4
    assert j["with_majority"]["j3"] == pytest.approx(2 / 3)   # alone on f2


def test_spearman_is_spearman():
    assert rc.spearman([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)
    assert rc.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert rc.spearman([1, 2, 3, 4], [1, 3, 2, 4]) == pytest.approx(0.8)
    assert rc.spearman([1, 1, 2, 2], [1, 2, 1, 2]) == pytest.approx(0.0)   # ties share their rank
    assert math.isnan(rc.spearman([1, 2], [1, 2]))


def test_a_frame_wins_its_share_of_the_votes_it_was_in():
    pairs = [_pair("f1"), _pair("h1", kind="human", left="ls-a", right="human")]
    pairs[1]["index"] = 1
    verdicts = _verdicts({"f1": ["B", "B", "A"], "h1": ["B", "B", "tie"]})
    scores = {"f1": {"A": 0.2, "B": 0.7, "prefer": "B", "margin": -0.5},
              "h1": {"A": 0.7, "B": 0.9, "prefer": "B", "margin": -0.2}}
    f = rc.agreement(pairs, verdicts, scores)["frames"]
    # the same frame stands in both pairs, so its wins are pooled: 2 of 3 as B, 0.5 of 3 as A
    assert f["rate"]["ls-a/d/1"] == pytest.approx(2.5 / 6)
    assert f["rate"]["human/talk/0"] == pytest.approx(2.5 / 3)
    assert f["rate"]["m6-a/d/1"] == pytest.approx(1 / 3)
    assert f["by_form"]["human"] == pytest.approx(2.5 / 3)


def test_the_committed_verdicts_are_the_sample_s_pairs():
    folder = rc.DATA / "verdicts"
    if not folder.is_dir() or not list(folder.glob("*.json")):
        pytest.skip("not judged yet")
    saved = json.loads((rc.DATA / "sample.json").read_text(encoding="utf-8"))
    ids = {p["id"] for p in saved["pairs"]}
    verdicts = rc.load_verdicts(folder)
    assert len(verdicts) >= 3
    for judge, rows in verdicts.items():
        assert set(rows) == ids, judge
        for pid, row in rows.items():
            assert row["prefer"] in ("A", "B", "tie"), (judge, pid)
            assert row.get("reason")
