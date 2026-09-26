"""Pull back to the source (inverse.py, compare.py, texmap.py, deck_ir.py).

Default run (offline, no TeX): source map and visible text, residuals of synthetic deck edits, the
translators' planned edits on tests/decks/inverse/a.tex against its classified IR (a.deck.json), and
deck_ir on a model of the presentation emit would build (tests/slides_sim.py).

Opt-in (`python -m pytest -m inverse`, needs pdflatex): the compile loop on the source pairs
tests/decks/inverse/a.tex -> b_*.tex and on synthetic edits of 01_basic; iterations and residual
trends go to tests/decks/inverse/out/results.json.
"""

import copy
import gzip
import json
import shutil
import time
from pathlib import Path

import pytest

from . import inverse_edits as ed
from beamer2slides.compare import HOLE, bullet_sig, compare, match_slides, style_diffs, word_diff
from beamer2slides.inverse import (Candidate, Context, Planner, Workspace, balance_span, colour_name, enclosing_group,
                                   ensure_preamble, frame_latex, latex_escape, runs_latex, size_switch)
from beamer2slides.texmap import (OPAQUE, PARA, Source, build_visible, locate_words, mask_comments, page_frames,
                                  read_args, synctex_pages, visible_text)

TESTS = Path(__file__).resolve().parent
INV = TESTS / "decks" / "inverse"


def fixture_deck() -> dict:
    return json.loads((INV / "a.deck.json").read_text(encoding="utf-8"))


def slide_of(deck: dict, key: str) -> int:
    return next(i for i, s in enumerate(deck["slides"]) if s.get("key") == key)


# ---------------------------------------------------------------- texmap

def test_visible_text_of_commands_math_and_lists():
    assert visible_text(r"A \textbf{bold} and \emph{it} word~--~dash") == "A bold and it word – dash"
    assert visible_text(r"Let $x^2$ be \% of \href{http://x}{here}") == f"Let {OPAQUE} be % of here"
    latex = "\\begin{itemize}\n  \\item One \\alert<2>{two}\n  \\item[--] Three\n\\end{itemize}"
    vis = build_visible(latex, 0, len(latex))
    assert [it.level for it in vis.items] == [0, 0]
    assert "One two" in vis.text and "Three" in vis.text
    assert len(vis.lists) == 1 and vis.lists[0].env == "itemize"


def test_visible_maps_back_to_source():
    latex = r"Residuals turn into \emph{source edits} until done."
    vis = build_visible(latex, 0, len(latex))
    wm = locate_words("turn into source edits", vis)
    a, b = wm.span(2, 4)
    assert latex[vis.starts[a]:vis.ends[b - 1]] == "source edits"
    assert wm.score == 1.0


def test_read_args_and_comments():
    s = r"\begin{frame}[fragile,label=x]{Title}{Sub} rest"
    args, end = read_args(s, len(r"\begin{frame}"), "oMM")
    assert s[args[0][1]:args[0][2]] == "fragile,label=x"
    assert s[args[1][1]:args[1][2]] == "Title"
    assert s[end:].strip() == "rest"
    masked = mask_comments("a % comment\nb \\% not")
    assert "comment" not in masked and "\\% not" in masked and len(masked) == len("a % comment\nb \\% not")


def test_source_frames_inputs_and_labels(tmp_path):
    (tmp_path / "part.tex").write_text("\\begin{frame}{Second}\nText two\n\\end{frame}\n", encoding="utf-8")
    (tmp_path / "main.tex").write_text(
        "\\documentclass{beamer}\n\\begin{document}\n\\begin{frame}[label=one]{First}\nText one\n\\end{frame}\n"
        "\\input{part}\n% \\begin{frame}{Commented}\\end{frame}\n\\end{document}\n", encoding="utf-8")
    src = Source(tmp_path / "main.tex")
    assert [(f.label, f.title) for f in src.frames] == [("one", "First"), (None, "Second")]
    assert src.frames[1].file.name == "part.tex"
    assert src.engine() == "pdflatex"
    sync = tmp_path / "main.synctex.gz"
    with gzip.open(sync, "wt", encoding="utf-8") as fh:
        fh.write("SyncTeX Version:1\nInput:1:./main.tex\nInput:2:./part.tex\nUnit:1\nMagnification:1000\n"
                 "Content:\n{1\n(1,5:100,200:300,50,10\nx1,5:100,200\n}1\n{2\n(2,3:100,200:300,50,10\n}2\n")
    pages = synctex_pages(sync)
    assert len(pages) == 2 and pages[0].votes[("./main.tex", 5)] == 2
    x0, y0, x1, y1 = pages[0].boxes[0][2:]
    assert abs(x1 - x0 - 300 * 72 / 72.27 / 65536) < 1e-9
    assert [f.index for f in page_frames(src, pages, cwd=tmp_path)] == [0, 1]
    assert [f.index for f in page_frames(src, [], ["1", "1", "2"])] == [0, 0, 1]


# ---------------------------------------------------------------- compare

def test_self_comparison_is_clean():
    deck = fixture_deck()
    assert compare(deck, copy.deepcopy(deck)).open() == []


def residual_kinds(target: dict) -> dict:
    return compare(fixture_deck(), target).summary()


def test_residuals_of_synthetic_edits():
    d = fixture_deck()
    method, results, motivation = slide_of(d, "method"), slide_of(d, "results"), slide_of(d, "motivation")
    moved = ed.move(d, method, ed.element(d, method, text="Residuals turn"), 0, 40)
    geo = [r for r in compare(d, moved).open() if r["kind"] == "geometry"]
    assert len(geo) == 1 and abs(geo[0]["dy"] - 40) < 0.5 and abs(geo[0]["dx"]) < 0.5
    assert residual_kinds(ed.split_style(d, method, "classify", bold=True)) == {"style": 1}
    recoloured = compare(d, ed.split_style(d, method, "classify", color="#ff0000")).open()
    assert [(r["field"], r["tgt"], r["text"]) for r in recoloured] == [("color", "#ff0000", "classify")]
    text = compare(d, ed.reword(d, results, "second round", "third round")).open()
    assert [r["kind"] for r in text] == ["text"] and text[0]["ops"][0]["tgt"] == "third"
    # a new box is also a new paragraph of the slide's text
    assert residual_kinds(ed.add_text_box(d, method, "Added in Slides", 190, 230)) == {"element_missing": 1,
                                                                                       "paragraph_missing": 1}
    assert residual_kinds(ed.delete_paragraph(d, results, "Lists keep their items")) == {"paragraph_extra": 1}
    assert residual_kinds(ed.add_slide(d, method, "Limits", ["One", "Two"])) == {"slide_missing": 1}
    assert residual_kinds(ed.swap_slides(d, method, results)) == {"slide_order": 1}
    assert residual_kinds(ed.set_notes(d, motivation, "Ask the audience.")) == {"notes": 1}
    assert residual_kinds(ed.add_image(d, method, "x.png", [200, 150, 300, 220])) == {"element_missing": 1}


def test_slide_matching_by_key_and_content():
    d = fixture_deck()
    swapped = ed.swap_slides(d, 1, 2)
    pairs = dict((j, i) for i, j in match_slides(d["slides"], swapped["slides"]) if i is not None and j is not None)
    assert pairs[1] == 2 and pairs[2] == 1
    for s in swapped["slides"]:
        s["key"] = None
    pairs = dict((j, i) for i, j in match_slides(d["slides"], swapped["slides"]) if i is not None and j is not None)
    assert pairs[1] == 2 and pairs[2] == 1


def test_an_adopted_frame_pairs_with_its_slide_by_the_label_adopt_gave_it():
    """A deck nobody converted has no slide keys; adopt labels each frame from the slide's objectId.
    Untitled slides alike in words (jeb-arch: a footer and a diagram each) paired by nothing else,
    and the loop deleted their frames and wrote a bare one per slide it took for missing."""
    footer = lambda: {"kind": "text", "role": "body", "paragraphs": [{"runs": [{"text": "JEB PNF Software"}]}]}  # noqa: E731
    tgt = [{"objectId": oid, "key": None, "elements": [footer()]} for oid in ("g6f3c_0_9", "p5", "gbd4_1_0")]
    cur = [{"key": key, "elements": [footer()]} for key in ("gbd4-1-0", "g6f3c-0-9", "p5")]
    assert sorted((j, i) for i, j in match_slides(cur, tgt)) == [(0, 1), (1, 2), (2, 0)]


def test_a_title_that_is_a_number_is_compared_not_taken_for_a_frame_counter():
    """plain-fonts slide 5 is a big "7" over a line: left out as a counter, the loop deleted it from
    the source and called the slide converged."""
    d = fixture_deck()
    s = slide_of(d, "method")
    title = next(e for e in d["slides"][s]["elements"] if e["kind"] == "text" and e.get("role") == "title")
    title["paragraphs"] = [{**title["paragraphs"][0], "runs": [{**title["paragraphs"][0]["runs"][0], "text": "7"}]}]
    gone = copy.deepcopy(d)
    gone["slides"][s]["elements"] = [e for e in gone["slides"][s]["elements"] if e.get("role") != "title"]
    assert "paragraph_missing" in residual_kinds_between(gone, d)


def test_a_note_hyphenated_at_a_line_end_is_the_same_note():
    """saudi-cats' note page reads "hyper- modern" and "cul- tural" where Slides has neither break."""
    d = fixture_deck()
    s = slide_of(d, "method")
    d["slides"][s]["notes"] = "an ancient and hyper-modern cultural kingdom"
    cur = copy.deepcopy(d)
    cur["slides"][s]["notes"] = "an ancient and hyper- modern cul- tural kingdom"
    assert "notes" not in residual_kinds_between(cur, d)
    cur["slides"][s]["notes"] = "an ancient and modern cultural kingdom"
    assert "notes" in residual_kinds_between(cur, d)


def residual_kinds_between(cur: dict, tgt: dict) -> dict:
    from collections import Counter
    return dict(Counter(r["kind"] for r in compare(cur, tgt).open()))


def test_word_and_style_diffs():
    assert word_diff("a b c", "a x c") == [{"op": "replace", "cur": "b", "tgt": "x", "c": [1, 2], "t": [1, 2]}]
    run = {"text": "one two", "size": 10.9, "color": "#000000"}
    cur = {"runs": [run]}
    tgt = {"runs": [{**run, "text": "one "}, {**run, "text": "two", "bold": True}]}
    assert [(d["field"], d["text"]) for d in style_diffs(cur, tgt, {"font": 0.06, "color": 24})] == [("bold", "two")]
    # formula holes and script sizes aren't styles the source controls
    hole_c = {"runs": [{"text": "a ", "size": 10.9}, {"text": " ", "hole": 20, "size": 10.9}]}
    hole_t = {"runs": [{"text": "a ", "size": 10.9}, {"text": " ", "hole": 20, "size": 12.4, "family": "mono"}]}
    assert style_diffs(hole_c, hole_t, {"font": 0.06, "color": 24}) == []
    lone = {"paragraphs": [{"bullet": {"kind": "image"}, "level": 1}]}
    assert bullet_sig(lone["paragraphs"][0], lone) == ("bullet", 0)


# ---------------------------------------------------------------- translators (planned offline)

def test_latex_helpers():
    assert latex_escape("50% & $5_x") == r"50\% \& \$5\_x"
    defined = {}
    assert colour_name("#FF0000", defined) == "red" and defined == {}
    assert colour_name("#123456", defined) == "b2s123456" and defined == {"b2s123456": "123456"}
    assert size_switch(14.4, 11) == r"\Large" and size_switch(30, 11).startswith(r"\fontsize{30.0}")
    ctx = Context()
    base = {"size": 10.95, "color": "#000000", "bold": False, "italic": False, "family": "sans"}
    runs = [{"text": "plain "}, {"text": "bold", "bold": True}, {"text": " red", "color": "#ff0000"}]
    assert runs_latex(runs, base, ctx) == r"plain \textbf{bold} \textcolor{red}{red}"
    text = r"a \textbf{b \emph{c} d} e"
    g = enclosing_group(text, text.index("c"), text.index("c") + 1, ("emph",), 0)
    assert text[g[1]:g[2]] == "c" and g[4] == "emph"
    a, b = balance_span(text, text.index("c"), text.index(" d"), 0, len(text))
    assert text[a:b] == r"\emph{c}"
    slide = {"key": "limits", "notes": "Say it.", "elements": [
        {"kind": "text", "role": "title", "paragraphs": [{"runs": [{"text": "Limits"}]}]},
        {"kind": "text", "role": "body", "paragraphs": [
            {"bullet": {"kind": "glyph"}, "level": 0, "runs": [{"text": "One"}]},
            {"bullet": {"kind": "glyph"}, "level": 1, "runs": [{"text": "Two"}]}]}]}
    out = frame_latex(slide, lambda p: base, ctx)
    assert out.startswith("\\begin{frame}[label=limits]{Limits}") and "\\note{Say it.}" in out
    assert out.count("\\begin{itemize}") == 2
    assert "[label=" not in frame_latex({**slide, "key": "title:limits#1"}, lambda p: base, ctx)


def plan_offline(tmp_path: Path, target: dict) -> str:
    """One planning round on a.tex against `target` (no compile); the edited main file."""
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
    return ws.source.text(ws.main)


def test_translate_wording_and_styles(tmp_path):
    d = fixture_deck()
    method = slide_of(d, "method")
    target = ed.reword(d, method, "until nothing is left", "until no residual is left")
    target = ed.split_style(target, method, "classify", bold=True)
    target = ed.split_style(target, method, "box by box", color="#123456")
    src = plan_offline(tmp_path, target)
    assert "until no residual is left" in src
    assert r"\textbf{classify}" in src
    assert r"\textcolor{b2s123456}{box by box}" in src and r"\definecolor{b2s123456}{HTML}{123456}" in src
    assert r"\emph{source edits}" in src


def test_translate_lists_notes_and_boxes(tmp_path):
    d = fixture_deck()
    results, method, motivation = slide_of(d, "results"), slide_of(d, "method"), slide_of(d, "motivation")
    target = ed.delete_paragraph(d, results, "Lists keep their items")
    target = ed.set_notes(target, motivation, "Ask the audience.")
    target = ed.add_text_box(target, method, "Added in Slides", 190, 230, color="#0000ff")
    src = plan_offline(tmp_path, target)
    assert "Lists keep their items" not in src and "\\item Moves need a second round" in src
    assert "\\note{Ask the audience.}" in src
    assert "\\usepackage[absolute,overlay]{textpos}" in src and "\\textcolor{blue}{Added in Slides}" in src


def test_translate_slides(tmp_path):
    d = fixture_deck()
    method, results = slide_of(d, "method"), slide_of(d, "results")
    src = plan_offline(tmp_path / "add", ed.add_slide(d, results, "Limits", ["Tables stay pictures"], key="limits"))
    assert src.index("[label=limits]{Limits}") > src.index("[label=results]")
    assert "\\item Tables stay pictures" in src
    src = plan_offline(tmp_path / "swap", ed.swap_slides(d, method, results))
    assert src.index("[label=results]") < src.index("[label=method]")
    deleted = copy.deepcopy(d)
    del deleted["slides"][slide_of(d, "picture")]
    src = plan_offline(tmp_path / "del", deleted)
    assert "[label=picture]" not in src and "[label=results]" in src


# ---------------------------------------------------------------- deck_ir on a simulated presentation

def built_pdf(name: str) -> Path:
    pdf = TESTS / "decks" / "out" / f"{name}.pdf"
    if not pdf.exists():
        pytest.skip(f"{pdf.name} not built (python tests/decks/build.py)")
    return pdf


TEXT_KINDS = {"text", "style", "bullet", "align", "paragraph_order", "paragraph_extra", "notes", "slide_missing",
              "slide_extra", "slide_order"}


@pytest.mark.parametrize("name", ["01_basic", "05_overlays_notes", "09_metropolis_fira", "10_helvet", "12_metropolis_talk"])
def test_deck_ir_reads_back_what_emit_writes(name):
    from .slides_sim import simulate
    from beamer2slides.classify import classify
    from beamer2slides.deck_ir import deck_ir
    from beamer2slides.extract import extract, select_overlays
    deck = classify(select_overlays(extract(built_pdf(name)), "last"))
    ir = deck_ir(simulate(deck), deck["slides"][0]["size"])
    comp = compare(deck, ir)
    bad = [r for r in comp.open() if r["kind"] in TEXT_KINDS or (r["kind"] == "geometry" and "dw" not in r)]
    assert bad == []
    anchors = [r for r in comp.residuals if r["kind"] == "geometry" and "dw" not in r]
    assert all(abs(r["dx"]) < 0.05 and abs(r["dy"]) < 0.05 for r in anchors)


def test_deck_ir_sees_slides_edits():
    from .slides_sim import simulate
    from beamer2slides.classify import classify
    from beamer2slides.deck_ir import deck_ir
    from beamer2slides.extract import extract, select_overlays
    deck = classify(select_overlays(extract(built_pdf("01_basic")), "last"))
    pres = simulate(deck)
    si, box = next((si, pe) for si, s in enumerate(pres["slides"]) for pe in s["pageElements"]
                   if "Plain paragraph" in json.dumps(pe.get("shape", {}).get("text", {})))
    text = box["shape"]["text"]["textElements"]
    content = "".join(te.get("textRun", {}).get("content", "") for te in text)
    start = content.index("Plain")
    # bold "Plain" and move the box down 30 pt: what a Slides user does, as the API returns it
    for te in text:
        tr = te.get("textRun")
        if tr and "Plain" in tr["content"]:
            i = tr["content"].index("Plain")
            tr_style = tr["style"]
            te["textRun"] = {**tr, "content": tr["content"][:i]}
            k = text.index(te)
            text[k + 1:k + 1] = [{"textRun": {"content": "Plain", "style": {**tr_style, "bold": True}}},
                                 {"textRun": {"content": tr["content"][i + 5:], "style": tr_style}}]
            break
    assert start >= 0
    scale = pres["pageSize"]["width"]["magnitude"] / 12700 / deck["slides"][0]["size"][0]
    box["transform"]["translateY"] += 30 * scale * 12700
    comp = compare(deck, deck_ir(pres, deck["slides"][0]["size"]))
    styles = [r for r in comp.open() if r["kind"] == "style"]
    assert [(r["field"], r["text"]) for r in styles] == [("bold", "Plain")]
    geo = [r for r in comp.open() if r["kind"] == "geometry" and r["target_slide"] == si]
    assert len(geo) == 1 and abs(geo[0]["dy"] - 30) < 0.05


# ---------------------------------------------------------------- the compile loop (opt-in)

RESULTS = INV / "out" / "results.json"


def pdflatex_missing() -> str | None:
    from beamer2slides.inverse import tex_env
    return None if shutil.which("pdflatex", path=tex_env()["PATH"]) else "pdflatex not found"


def record(name: str, res, seconds: float) -> None:
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}
    data[name] = {"converged": res.converged, "rounds": len(res.iterations) - 1, "seconds": round(seconds, 1),
                  "trend": [it["open"] for it in res.iterations],
                  "geometry_error": [it["geometry_error"] for it in res.iterations],
                  "unresolved": [f"{u['kind']}: {u.get('why')}" for u in res.unresolved],
                  "patch_lines": sum(1 for l in res.patch.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---"))}
    RESULTS.write_text(json.dumps(data, indent=1), encoding="utf-8")


@pytest.mark.inverse
@pytest.mark.parametrize("name,rounds", [("b_text", 2), ("b_lists", 2), ("b_slides", 2), ("b_layout", 3)])
def test_converge_source_pairs(name, rounds, tmp_path):
    if reason := pdflatex_missing():
        pytest.skip(reason)
    from beamer2slides.inverse import converge, ir_from_tex
    target = ir_from_tex(INV / f"{name}.tex", tmp_path / "target")
    t = time.time()
    res = converge(INV / "a.tex", target, tmp_path / "loop", max_iter=8)
    record(f"pair:{name}", res, time.time() - t)
    assert res.converged, res.unresolved
    assert len(res.iterations) - 1 <= rounds


SYNTHETIC = {
    "move": (lambda d: ed.move(d, 3, ed.element(d, 3, text="Plain paragraph"), 0, 40), 3),
    "bold": (lambda d: ed.split_style(d, 3, "paragraph", bold=True), 1),
    "recolour": (lambda d: ed.split_style(d, 1, "level item", color="#ff0000"), 1),
    "reword": (lambda d: ed.reword(d, 1, "Back to first level", "Back to the top level"), 1),
    "add_text": (lambda d: ed.add_text_box(d, 4, "A note added in Slides", 40, 220, color="#0000ff"), 2),
    "delete_bullet": (lambda d: ed.delete_paragraph(d, 1, "Second level again"), 2),
    "add_slide": (lambda d: ed.add_slide(d, 2, "A new slide", ["First new point", "Second new point"]), 3),
    "swap": (lambda d: ed.swap_slides(d, 2, 3), 1),
    "notes": (lambda d: ed.set_notes(d, 1, "Say something about nesting."), 1),
    "image": (lambda d: ed.add_image(d, 4, str(INV / "img" / "plot.png"), [200, 150, 330, 240]), 2),
}


@pytest.fixture(scope="module")
def basic_deck(tmp_path_factory):
    if reason := pdflatex_missing():
        pytest.skip(reason)
    work = tmp_path_factory.mktemp("basic")
    built = Workspace(TESTS / "decks" / "01_basic.tex", work).build(work / "classify")
    assert not isinstance(built, str), built
    return built.deck


@pytest.mark.inverse
@pytest.mark.parametrize("name", list(SYNTHETIC))
def test_converge_synthetic_edits(name, basic_deck, tmp_path):
    from beamer2slides.inverse import converge
    make, rounds = SYNTHETIC[name]
    target = make(basic_deck)
    t = time.time()
    res = converge(TESTS / "decks" / "01_basic.tex", target, tmp_path / "loop", max_iter=8)
    record(f"synthetic:{name}", res, time.time() - t)
    assert res.converged, res.unresolved
    assert len(res.iterations) - 1 <= rounds


EMPTY_OUTLINES = """Package rerunfilecheck Warning: File `main.out' has changed.
(rerunfilecheck)                Rerun to get outlines right
(rerunfilecheck)                or use package `bookmark'.

Package rerunfilecheck Info: Checksums for `main.out':
(rerunfilecheck)             Before: <no file>
(rerunfilecheck)             After:  D41D8CD98F00B204E9800998ECF8427E;0.
"""


def test_an_outline_file_that_came_out_empty_asks_for_no_second_pass():
    """Every deck with no sections got a second lualatex pass for bookmarks it does not have."""
    from beamer2slides.inverse import needs_rerun
    assert not needs_rerun(EMPTY_OUTLINES)
    assert needs_rerun(EMPTY_OUTLINES.replace("D41D8CD98F00B204E9800998ECF8427E;0.", "0A1B2C;42."))
    assert needs_rerun(EMPTY_OUTLINES + "LaTeX Warning: Label(s) may have changed. Rerun to get cross-references right.\n")


TWO_FRAMES = """\\documentclass{beamer}
\\usetheme{Madrid}
\\begin{document}
\\begin{frame}{A}x\\end{frame}
\\begin{frame}{B}y\\end{frame}
\\end{document}
"""


def compiles(monkeypatch, build, states):
    """A TeX engine that writes `states[n]` into the build folder on its n-th run, and a log that
    never asks for anything - which is what a talk with no sections really produces."""
    import subprocess as sp
    runs = []

    def run(cmd, **kw):
        for name, text in states[min(len(runs), len(states) - 1)].items():
            (build / name).write_text(text, encoding="utf-8")
        (build / "main.log").write_text(EMPTY_OUTLINES, encoding="utf-8")
        runs.append(cmd)
        return sp.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("beamer2slides.inverse.subprocess.run", run)
    return runs


def test_a_compile_runs_again_while_the_auxiliary_files_move(tmp_path, monkeypatch):
    """The log is not the whole rule. A talk with no sections asks for no rerun after its first
    pass - rightly, there are no bookmarks to settle - while Madrid's footline still reads `2/1`,
    beamer's \\inserttotalframenumber coming out of the .nav the *next* pass reads. Measured: with
    the log alone, the pull loop's own compile of such a talk hands the page a wrong frame total
    and the loop then reads it as a residual."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.tex").write_text(TWO_FRAMES, encoding="utf-8")
    ws = Workspace(src / "main.tex", tmp_path / "work")
    runs = compiles(monkeypatch, ws.build_dir, [{"main.nav": "one"}, {"main.nav": "two"},
                                                {"main.nav": "two"}])
    ws.compile()
    assert len(runs) == 3
    # and a folder the turn before left settled costs one pass, which is the loop's common case
    runs = compiles(monkeypatch, ws.build_dir, [{"main.nav": "two"}])
    ws.compile()
    assert len(runs) == 1
