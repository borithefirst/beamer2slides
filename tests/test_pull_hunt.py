"""Two pull defects found by the edit-hunt harness (out/edithunt/j/h2*, out/edithunt/j/h6b):

- a slide duplicated in the deck makes pull write two frames with the same `\\label{}` (hyperref
  keeps only the first, silently breaking slide identity for the next sync) - `inverse.slide_missing`.
- a text/style rewrite that never converges is left half-applied in the source, mixing the
  author's words with garbled `\\alert{}` fragments - `inverse.converge`'s round loop and
  `inverse.char_span`'s sub-word narrowing (used only by `style()`).
"""

import copy
import re
from pathlib import Path

import pytest

from . import inverse_edits as ed
from .test_inverse import INV, fixture_deck, pdflatex_missing, slide_of
from beamer2slides.compare import compare
from beamer2slides.inverse import Candidate, Context, Planner, Workspace, converge, ensure_preamble

TESTS = Path(__file__).resolve().parent


def plan_offline_ctx(tmp_path: Path, target: dict) -> tuple[str, Context, list]:
    """Like test_inverse.plan_offline, but also returns the Context (for ctx.label_notes) and the
    unresolved residuals `plan()` gave up on."""
    ws = Workspace(INV / "a.tex", tmp_path)
    deck = fixture_deck()
    frames = [ws.source.frames[s["frame_index"]] for s in deck["slides"]]
    cand = Candidate(ws.source, tmp_path / "a.pdf", deck, frames)
    ctx = Context()
    edits, failed = Planner(cand, compare(deck, target), target, ctx, ws, set(), {}).plan()
    assert edits, failed
    ws.write(edits)
    pre = ensure_preamble(ws, ctx)
    if pre:
        ws.write(pre)
    return ws.source.text(ws.main), ctx, failed


# ---------------------------------------------------------------- h2: duplicated slide, duplicate label

def test_a_new_frame_never_repeats_an_existing_label(tmp_path):
    """A slide duplicated onto an *existing*, unmatched frame's key (h2a: the deck carries two
    slides tagged "results", one of them the untouched original) must not produce two
    `[label=results]` frames."""
    d = fixture_deck()
    method = slide_of(d, "method")
    target = ed.add_slide(d, method, "Duplicate results", ["An item"], key="results")
    src, ctx, _ = plan_offline_ctx(tmp_path, target)
    assert src.count("[label=results]") == 1
    assert "[label=results-2]" in src
    assert ctx.label_notes and "results" in ctx.label_notes[0]


def test_two_new_frames_sharing_a_key_get_different_labels(tmp_path):
    """Two slides duplicated onto a key that exists nowhere in the source yet (h2b: both copies are
    "slide_missing") must still not collide with each other. They anchor after different existing
    frames (results, picture) so Workspace.write's same-position dedup - a separate, correct
    mechanism for two structural inserts landing on the same point in one round - never enters
    into it; a real converge() run gives the second copy its own anchor once the first has landed
    and been recompiled, exactly like this."""
    d = fixture_deck()
    results = slide_of(d, "results")
    picture = slide_of(d, "picture")
    target = ed.add_slide(d, picture, "Copy two", ["Second"], key="experiments")
    target = ed.add_slide(target, results, "Copy one", ["First"], key="experiments")
    src, ctx, _ = plan_offline_ctx(tmp_path, target)
    assert src.count("[label=experiments]") == 1
    assert "[label=experiments-2]" in src
    assert len(ctx.label_notes) == 1


def test_an_unlabelled_new_slide_is_left_alone(tmp_path):
    """A slide_missing whose key is one of sync's synthetic keys (title:..., page:N - no real
    \\label in the deck) is never treated as a label collision."""
    d = fixture_deck()
    method = slide_of(d, "method")
    target = ed.add_slide(d, method, "No real label", ["An item"], key="title:no-real-label#1")
    src, ctx, _ = plan_offline_ctx(tmp_path, target)
    assert "\\begin{frame}{No real label}" in src
    assert not ctx.label_notes


# ---------------------------------------------------------------- h6: a style rewrite that never converges

def assert_no_word_is_split(text: str) -> None:
    """None of `text`'s `\\alert{...}` wraps starts or ends inside a word - a colour command a
    person writes always covers whole words, never `e\\alert{fficient}ly`."""
    for m in re.finditer(r"\\alert\{[^{}]*\}", text):
        before = text[m.start() - 1] if m.start() > 0 else " "
        after = text[m.end()] if m.end() < len(text) else " "
        assert not before.isalpha(), f"alert starts mid-word: ...{text[max(0, m.start() - 15):m.start() + 15]}..."
        assert not after.isalpha(), f"alert ends mid-word: ...{text[max(0, m.start() - 15):m.end() + 15]}..."


B_REPRO_TEX = """\\documentclass{beamer}
\\usepackage[T1]{fontenc}
\\begin{document}

\\begin{frame}[label=title]
\\titlepage
\\end{frame}

\\begin{frame}[label=main]{Main}
\\begin{itemize}
\\item Long-context transformers spend most of memory on attention maps
\\item We ask: can attention maps be compressed \\alert{efficiently, with a bounded residual}?
\\item Target: $4\\times$ memory reduction with no accuracy drop
\\end{itemize}
\\end{frame}

\\end{document}
"""


@pytest.mark.inverse
def test_a_colour_rewrite_that_cannot_converge_never_splits_a_word(tmp_path):
    """h6b: the deck kept "efficiently, with a bounded residual" but wanted it reworded and
    un-alerted at the same time - words the new text can't be matched to word-for-word (the
    residual `converge` reports as "the words span LaTeX commands"), so the \\alert{} coloured
    range is compared character by character against the (unrelated) new wording, and used to
    "explain" the mismatch with sub-word colour residuals. `char_span` no longer lets `style()`
    turn one of these into a colour command wrapped around less than a whole word: the real hunt's
    patch had `e\\alert{fficient}ly\\alert{, with a b}o\\alert{unded residual}` - garbage no person
    would write."""
    if reason := pdflatex_missing():
        pytest.skip(reason)
    tex = tmp_path / "b_repro.tex"
    tex.write_text(B_REPRO_TEX, encoding="utf-8")
    built = Workspace(tex, tmp_path / "base").build(tmp_path / "classify")
    assert not isinstance(built, str), built
    target = copy.deepcopy(built.deck)
    found = False
    for s in target["slides"]:
        for e in s["elements"]:
            for p in e.get("paragraphs", []):
                for r in p["runs"]:
                    if "efficiently, with a bounded residual" in r["text"]:
                        r["text"] = r["text"].replace("efficiently, with a bounded residual", "cheaply for replay")
                        r["color"] = "#000000"
                        found = True
    assert found
    res = converge(tex, target, tmp_path / "loop", max_iter=8)
    assert_no_word_is_split(res.patch)
    assert any(u["kind"] == "text" for u in res.unresolved)


@pytest.mark.inverse
def test_a_colour_edit_that_can_never_converge_is_reverted_not_left_half_applied(tmp_path):
    """A colour residual that keeps coming back unchanged (here: the target wants only part of a
    word - "rst" inside "First" - recoloured, which a whole-word `\\textcolor{}` can never satisfy)
    is reverted to the author's original text once `converge` gives up on it, rather than left as
    whatever the last, unconverged attempt wrote (h6b: the same failure mode left
    `e\\alert{fficient}ly...` sitting in the pulled source)."""
    if reason := pdflatex_missing():
        pytest.skip(reason)
    work = tmp_path / "basic"
    built = Workspace(TESTS / "decks" / "01_basic.tex", work).build(work / "classify")
    assert not isinstance(built, str), built
    target = ed.split_style(built.deck, 1, "rst", color="#ff0000")
    res = converge(TESTS / "decks" / "01_basic.tex", target, tmp_path / "loop", max_iter=8)
    assert not res.converged
    assert res.patch == ""
    reverted = [u for u in res.unresolved if u["kind"] == "style" and "reverted" in u.get("why", "")]
    assert reverted, res.unresolved
