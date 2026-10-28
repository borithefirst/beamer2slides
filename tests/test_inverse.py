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
import subprocess as sp
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from . import inverse_edits as ed
from .json_reads import jarr, jint, jnum, jobj, jobjs, jstr
from beamer2slides.compare import (TOL, BoxGeometry, Comparison, Current, TextGeometry, WordOp, bullet_sig, compare,
                                   match_slides, residual_json, style_diffs, target_slide_of, without_keys, word_diff)
from beamer2slides.inverse import (BEAMER_PT, TITLE_STYLE, Candidate, Planner, Result, TextStyle, Workspace, balance_span, colour_name,
                                   enclosing_group, ensure_preamble, frame_latex, fresh_context, latex_escape, loop_run, loop_texts,
                                   runs_latex, size_switch)
from beamer2slides.json_types import Json, JsonObject, as_optional_str
from beamer2slides import pieces
from beamer2slides.texmap import (OPAQUE, Frame, Source, build_visible, locate_words, mask_comments, page_frames,
                                  read_args, synctex_pages, visible_text)

TESTS = Path(__file__).resolve().parent
INV = TESTS / "decks" / "inverse"


def fixture_deck() -> JsonObject:
    return jobj(json.loads((INV / "a.deck.json").read_text(encoding="utf-8")))


def slide_of(deck: JsonObject, key: str) -> int:
    return next(i for i, s in enumerate(jobjs(deck, "slides")) if s.get("key") == key)


def current(deck: JsonObject) -> Current:
    """A fixture deck as the loop's current side: its slides known by the keys the fixture gives
    them (the loop's own come from the frames' labels, `Candidate.keys`)."""
    return Current(deck=deck, keys=tuple(as_optional_str(s.get("key"), f"slide {i}: key")
                                         for i, s in enumerate(jobjs(deck, "slides"))))


def compared(cur: JsonObject, tgt: JsonObject) -> Comparison:
    return compare(current(cur), tgt, TOL, {})


# ---------------------------------------------------------------- texmap

def test_visible_text_of_commands_math_and_lists():
    assert visible_text(r"A \textbf{bold} and \emph{it} word~--~dash") == "A bold and it word – dash"
    assert visible_text(r"Let $x^2$ be \% of \href{http://x}{here}") == f"Let {OPAQUE} be % of here"
    latex = "\\begin{itemize}\n  \\item One \\alert<2>{two}\n  \\item[--] Three\n\\end{itemize}"
    vis = build_visible(latex, 0, len(latex), title_frame=False)
    assert [it.level for it in vis.items] == [0, 0]
    assert "One two" in vis.text and "Three" in vis.text
    assert len(vis.lists) == 1 and vis.lists[0].env == "itemize"


def test_visible_maps_back_to_source():
    latex = r"Residuals turn into \emph{source edits} until done."
    vis = build_visible(latex, 0, len(latex), title_frame=False)
    wm = locate_words("turn into source edits", vis)
    span = wm.span(2, 4)
    assert span is not None
    a, b = span
    assert latex[vis.starts[a]:vis.ends[b - 1]] == "source edits"
    assert wm.score == 1.0


def test_words_after_an_ellipsis_map_to_their_own_letters():
    """NORMALISE makes "…" three characters: offsets counted after it ran two letters late, and
    past the visible text for a frame's last word (test_ir_matrix's hashing-ellipsis)."""
    for latex in ("Wait… Enough said", "Wait… E", "ﬁne… then"):
        vis = build_visible(latex, 0, len(latex), title_frame=False)
        last = latex.split()[-1]
        found = locate_words(last, vis).vis[0]
        assert found is not None
        a, b = found
        assert vis.text[a:b] == last and b <= len(vis.starts)


def test_a_note_opening_on_a_soft_break_leaves_vertical_mode_first():
    """`\\\\` opening a paragraph has no line to end (test_ir_matrix's hashing-note_break)."""
    def title_style(_p: object) -> TextStyle:
        return TITLE_STYLE
    out = frame_latex([], "k", "\x0bHeads up\nand \x0b more", title_style, fresh_context(BEAMER_PT), None)
    assert "\\note{\\leavevmode\\\\ Heads up\n\nand \\\\  more}" in out


def test_read_args_and_comments():
    s = r"\begin{frame}[fragile,label=x]{Title}{Sub} rest"
    args, end = read_args(s, len(r"\begin{frame}"), "oMM")
    options, title = args[0], args[1]
    assert options is not None and title is not None
    assert s[options[1]:options[2]] == "fragile,label=x"
    assert s[title[1]:title[2]] == "Title"
    assert s[end:].strip() == "rest"
    masked = mask_comments("a % comment\nb \\% not")
    assert "comment" not in masked and "\\% not" in masked and len(masked) == len("a % comment\nb \\% not")


def test_a_backslash_ending_a_line_or_the_text_is_one_token():
    """texmap.read_args: a mandatory argument that is `\\` and a line end (a control space), or a
    backslash ending the text, crashed the visible text (`re.match` with `.` found nothing)."""
    assert read_args("\\textbf\\\nx", 7, "m") == ([("m", 7, 9)], 9)
    assert read_args("ab\\", 2, "m") == ([("m", 2, 3)], 3)
    assert visible_text("a \\textbf\\\nb") == "a b"


def test_a_frame_title_nothing_closes_is_read_as_body():
    """texmap.build_visible: a frame title or subtitle whose `{` the rest of the file never closes
    crashed the frame's visible text; it is no title now, its words read as the body's."""
    s = "\\begin{frame}{Title\nBody words\n\\end{frame}"
    v = build_visible(s, 0, len(s), title_frame=True)
    assert v.title is None and v.text.split() == ["Title", "Body", "words"]
    s = "\\begin{frame}{T}{Sub\nBody\n\\end{frame}"
    v = build_visible(s, 0, len(s), title_frame=True)
    assert v.title == (0, 1) and v.text.split() == ["T", "Sub", "Body"]


def test_source_frames_inputs_and_labels(tmp_path: Path):
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
    assert [frame_index(f) for f in page_frames(src, pages, None, cwd=tmp_path)] == [0, 1]
    assert [frame_index(f) for f in page_frames(src, [], ["1", "1", "2"], None)] == [0, 0, 1]


def frame_index(f: Frame | None) -> int | None:
    return None if f is None else f.index


# ---------------------------------------------------------------- compare

def test_self_comparison_is_clean():
    deck = fixture_deck()
    assert compared(deck, copy.deepcopy(deck)).open() == []


def residual_kinds(target: JsonObject) -> dict[str, int]:
    return compared(fixture_deck(), target).summary()


def test_residuals_of_synthetic_edits():
    d = fixture_deck()
    method, results, motivation = slide_of(d, "method"), slide_of(d, "results"), slide_of(d, "motivation")
    moved = ed.move(d, method, ed.element(d, method, None, text="Residuals turn"), 0, 40)
    geo = [r for r in compared(d, moved).open() if isinstance(r, (TextGeometry, BoxGeometry))]
    assert len(geo) == 1 and abs(geo[0].dy - 40) < 0.5 and abs(geo[0].dx) < 0.5
    assert residual_kinds(ed.split_style(d, method, "classify", bold=True)) == {"style": 1}
    recoloured = [residual_json(r) for r in compared(d, ed.split_style(d, method, "classify", color="#ff0000")).open()]
    assert [(r["field"], r["tgt"], r["text"]) for r in recoloured] == [("color", "#ff0000", "classify")]
    text = [residual_json(r) for r in compared(d, ed.reword(d, results, "second round", "third round")).open()]
    assert [r["kind"] for r in text] == ["text"] and jstr(text[0], "ops", 0, "tgt") == "third"
    # a new box is also a new paragraph of the slide's text
    assert residual_kinds(ed.add_text_box(d, method, "Added in Slides", 190, 230, size=10.91, color="#000000",
                                           width=120.0)) == {"element_missing": 1,
                                                                                       "paragraph_missing": 1}
    assert residual_kinds(ed.delete_paragraph(d, results, "Lists keep their items")) == {"paragraph_extra": 1}
    assert residual_kinds(ed.add_slide(d, method, "Limits", ["One", "Two"], key=None)) == {"slide_missing": 1}
    assert residual_kinds(ed.swap_slides(d, method, results)) == {"slide_order": 1}
    assert residual_kinds(ed.set_notes(d, motivation, "Ask the audience.")) == {"notes": 1}
    assert residual_kinds(ed.add_image(d, method, "x.png", [200, 150, 300, 220])) == {"element_missing": 1}


def test_slide_matching_by_key_and_content():
    d = fixture_deck()
    swapped = ed.swap_slides(d, 1, 2)
    pairs = dict((j, i) for i, j in match_slides(jobjs(d, "slides"), jobjs(swapped, "slides"))
                 if i is not None and j is not None)
    assert pairs[1] == 2 and pairs[2] == 1
    for s in jobjs(swapped, "slides"):
        s["key"] = None
    pairs = dict((j, i) for i, j in match_slides(jobjs(d, "slides"), jobjs(swapped, "slides"))
                 if i is not None and j is not None)
    assert pairs[1] == 2 and pairs[2] == 1


def test_an_adopted_frame_pairs_with_its_slide_by_the_label_adopt_gave_it():
    """A deck nobody converted has no slide keys; adopt labels each frame from the slide's objectId.
    Untitled slides alike in words (jeb-arch: a footer and a diagram each) paired by nothing else,
    and the loop deleted their frames and wrote a bare one per slide it took for missing."""
    def footer() -> JsonObject:
        return {"kind": "text", "role": "body", "paragraphs": [{"runs": [{"text": "JEB PNF Software"}]}]}
    tgt: list[JsonObject] = [{"objectId": oid, "key": None, "elements": [footer()]}
                             for oid in ("g6f3c_0_9", "p5", "gbd4_1_0")]
    cur: list[JsonObject] = [{"key": key, "elements": [footer()]} for key in ("gbd4-1-0", "g6f3c-0-9", "p5")]
    assert sorted((j, i) for i, j in match_slides(cur, tgt)) == [(0, 1), (1, 2), (2, 0)]


def test_a_title_that_is_a_number_is_compared_not_taken_for_a_frame_counter():
    """plain-fonts slide 5 is a big "7" over a line: left out as a counter, the loop deleted it from
    the source and called the slide converged."""
    d = fixture_deck()
    s = slide_of(d, "method")
    title = next(e for e in jobjs(d, "slides", s, "elements") if e["kind"] == "text" and e.get("role") == "title")
    first = jobj(title, "paragraphs", 0)
    title["paragraphs"] = [{**first, "runs": [{**jobj(first, "runs", 0), "text": "7"}]}]
    gone = copy.deepcopy(d)
    kept: list[Json] = [e for e in jobjs(gone, "slides", s, "elements") if e.get("role") != "title"]
    jobj(gone, "slides", s)["elements"] = kept
    assert "paragraph_missing" in residual_kinds_between(gone, d)


def test_a_note_hyphenated_at_a_line_end_is_the_same_note():
    """saudi-cats' note page reads "hyper- modern" and "cul- tural" where Slides has neither break."""
    d = fixture_deck()
    s = slide_of(d, "method")
    jobj(d, "slides", s)["notes"] = "an ancient and hyper-modern cultural kingdom"
    cur = copy.deepcopy(d)
    jobj(cur, "slides", s)["notes"] = "an ancient and hyper- modern cul- tural kingdom"
    assert "notes" not in residual_kinds_between(cur, d)
    jobj(cur, "slides", s)["notes"] = "an ancient and modern cultural kingdom"
    assert "notes" in residual_kinds_between(cur, d)


def residual_kinds_between(cur: JsonObject, tgt: JsonObject) -> dict[str, int]:
    from collections import Counter
    kinds: Counter[str] = Counter(r.kind for r in compared(cur, tgt).open())
    return dict(kinds)


def test_word_and_style_diffs():
    assert word_diff("a b c", "a x c") == [WordOp(op="replace", cur="b", tgt="x", c=(1, 2), t=(1, 2))]
    run: JsonObject = {"text": "one two", "size": 10.9, "color": "#000000"}
    cur: JsonObject = {"runs": [run]}
    tgt: JsonObject = {"runs": [{**run, "text": "one "}, {**run, "text": "two", "bold": True}]}
    assert [(d.change.field, d.text) for d in style_diffs(cur, tgt, {"font": 0.06, "color": 24})] == [("bold", "two")]
    # formula holes and script sizes aren't styles the source controls
    hole_c: JsonObject = {"runs": [{"text": "a ", "size": 10.9}, {"text": " ", "hole": 20, "size": 10.9}]}
    hole_t: JsonObject = {"runs": [{"text": "a ", "size": 10.9}, {"text": " ", "hole": 20, "size": 12.4, "family": "mono"}]}
    assert style_diffs(hole_c, hole_t, {"font": 0.06, "color": 24}) == []
    lone: JsonObject = {"paragraphs": [{"bullet": {"kind": "image"}, "level": 1}]}
    assert bullet_sig(jobj(lone, "paragraphs", 0), lone) == ("bullet", 0)


# ---------------------------------------------------------------- translators (planned offline)

def test_latex_helpers():
    assert latex_escape("50% & $5_x") == r"50\% \& \$5\_x"
    assert latex_escape("ends.  Then") == "ends. Then"  # a wide gap is one TeX space
    defined: dict[str, str] = {}
    assert colour_name("#FF0000", defined) == "red" and defined == {}
    assert colour_name("#123456", defined) == "b2s123456" and defined == {"b2s123456": "123456"}
    assert size_switch(14.4, 11) == r"\Large" and size_switch(30, 11).startswith(r"\fontsize{30.0}")
    ctx = fresh_context(BEAMER_PT)
    base = TextStyle(size=10.95, color="#000000", bold=False, italic=False, family="sans", font=None, weight=None)
    said: list[JsonObject] = [{"text": "plain "}, {"text": "bold", "bold": True}, {"text": " red", "color": "#ff0000"}]
    runs = [loop_run(r, "test") for r in said]
    assert runs_latex(runs, base, ctx) == r"plain \textbf{bold} \textcolor{red}{red}"
    text = r"a \textbf{b \emph{c} d} e"
    g = enclosing_group(text, text.index("c"), text.index("c") + 1, ("emph",), 0)
    assert g is not None
    assert text[g[1]:g[2]] == "c" and g[4] == "emph"
    a, b = balance_span(text, text.index("c"), text.index(" d"), 0, len(text))
    assert text[a:b] == r"\emph{c}"
    slide: JsonObject = {"key": "limits", "notes": "Say it.", "elements": [
        {"kind": "text", "role": "title", "paragraphs": [{"runs": [{"text": "Limits"}]}]},
        {"kind": "text", "role": "body", "paragraphs": [
            {"bullet": {"kind": "glyph"}, "level": 0, "runs": [{"text": "One"}]},
            {"bullet": {"kind": "glyph"}, "level": 1, "runs": [{"text": "Two"}]}]}]}
    texts = loop_texts(slide, "test")

    def style_for(_p: object) -> TextStyle:
        return base
    out = frame_latex(texts, "limits", "Say it.", style_for, ctx, None)
    assert out.startswith("\\begin{frame}[label=limits]{Limits}") and "\\note{Say it.}" in out
    assert out.count("\\begin{itemize}") == 2
    assert "[label=" not in frame_latex(texts, "title:limits#1", "Say it.", style_for, ctx, None)


def plan_offline(tmp_path: Path, target: JsonObject) -> str:
    """One planning round on a.tex against `target` (no compile); the edited main file."""
    ws = Workspace(INV / "a.tex", tmp_path, handout=False, engine=None, fresh=True)
    deck = fixture_deck()
    frames: list[Frame | None] = [ws.source.frames[jint(s, "frame_index")] for s in jobjs(deck, "slides")]
    cand = Candidate(ws.source, tmp_path / "a.pdf", deck, frames, locs={}, text_masked={}, words={})
    ctx = fresh_context(BEAMER_PT)
    edits, failed = Planner(cand, compared(deck, target), target, ctx, ws, set(), {}, {}).plan()
    assert edits, failed
    ws.write(edits)
    pre = ensure_preamble(ws, ctx)
    if pre:
        ws.write(pre)
    return ws.source.text(ws.main)


def test_translate_wording_and_styles(tmp_path: Path):
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


def test_translate_lists_notes_and_boxes(tmp_path: Path):
    d = fixture_deck()
    results, method, motivation = slide_of(d, "results"), slide_of(d, "method"), slide_of(d, "motivation")
    target = ed.delete_paragraph(d, results, "Lists keep their items")
    target = ed.set_notes(target, motivation, "Ask the audience.")
    target = ed.add_text_box(target, method, "Added in Slides", 190, 230, size=10.91, color="#0000ff", width=120.0)
    src = plan_offline(tmp_path, target)
    assert "Lists keep their items" not in src and "\\item Moves need a second round" in src
    assert "\\note{Ask the audience.}" in src
    assert "\\usepackage[absolute,overlay]{textpos}" in src and "\\textcolor{blue}{Added in Slides}" in src


def test_translate_slides(tmp_path: Path):
    d = fixture_deck()
    method, results = slide_of(d, "method"), slide_of(d, "results")
    src = plan_offline(tmp_path / "add", ed.add_slide(d, results, "Limits", ["Tables stay pictures"], key="limits"))
    assert src.index("[label=limits]{Limits}") > src.index("[label=results]")
    assert "\\item Tables stay pictures" in src
    src = plan_offline(tmp_path / "swap", ed.swap_slides(d, method, results))
    assert src.index("[label=results]") < src.index("[label=method]")
    deleted = copy.deepcopy(d)
    del jarr(deleted, "slides")[slide_of(d, "picture")]
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
def test_deck_ir_reads_back_what_emit_writes(name: str):
    from .slides_sim import simulate
    from beamer2slides.classify import classify
    from .irs import deck_ir
    from beamer2slides.extract import extract, select_overlays
    from beamer2slides.ir import deck_json
    classified = classify(select_overlays(extract(built_pdf(name), None), "last"))
    deck = deck_json(classified)
    ir = deck_ir(simulate(deck), classified["slides"][0]["size"])
    comp = compare(without_keys(deck), ir, TOL, {})
    bad = [r for r in comp.open() if r.kind in TEXT_KINDS or isinstance(r, TextGeometry)]
    assert bad == []
    anchors = [r for r in comp.residuals if isinstance(r, TextGeometry)]
    assert all(abs(r.dx) < 0.05 and abs(r.dy) < 0.05 for r in anchors)


def test_deck_ir_sees_slides_edits():
    from .slides_sim import simulate
    from beamer2slides.classify import classify
    from .irs import deck_ir
    from beamer2slides.extract import extract, select_overlays
    from beamer2slides.ir import deck_json
    classified = classify(select_overlays(extract(built_pdf("01_basic"), None), "last"))
    deck = deck_json(classified)
    size = classified["slides"][0]["size"]
    pres = simulate(deck)
    si, box = next((si, pe) for si, s in enumerate(jobjs(pres, "slides")) for pe in jobjs(s, "pageElements")
                   if "shape" in pe and "Plain paragraph" in json.dumps(jobj(pe, "shape").get("text", {})))
    text = jarr(box, "shape", "text", "textElements")
    runs = [(k, jobj(te, "textRun")) for k, te in enumerate(jobjs(box, "shape", "text", "textElements"))
            if "textRun" in jobj(te)]
    content = "".join(jstr(tr, "content") for _, tr in runs if "content" in tr)
    start = content.index("Plain")
    # bold "Plain" and move the box down 30 pt: what a Slides user does, as the API returns it
    for k, tr in runs:
        words = jstr(tr, "content")
        if "Plain" in words:
            i = words.index("Plain")
            tr_style = jobj(tr, "style")
            jobj(text, k)["textRun"] = {**tr, "content": words[:i]}
            text[k + 1:k + 1] = [{"textRun": {"content": "Plain", "style": {**tr_style, "bold": True}}},
                                 {"textRun": {"content": words[i + 5:], "style": tr_style}}]
            break
    assert start >= 0
    scale = jnum(pres, "pageSize", "width", "magnitude") / 12700 / size[0]
    transform = jobj(box, "transform")
    transform["translateY"] = jnum(transform, "translateY") + 30 * scale * 12700
    comp = compare(without_keys(deck), deck_ir(pres, size), TOL, {})
    styles = [residual_json(r) for r in comp.open() if r.kind == "style"]
    assert [(r["field"], r["text"]) for r in styles] == [("bold", "Plain")]
    geo = [r for r in comp.open() if isinstance(r, (TextGeometry, BoxGeometry)) and target_slide_of(r) == si]
    assert len(geo) == 1 and abs(geo[0].dy - 30) < 0.05


# ---------------------------------------------------------------- the compile loop (opt-in)

RESULTS = INV / "out" / "results.json"


def pdflatex_missing() -> str | None:
    from beamer2slides.inverse import tex_env
    return None if shutil.which("pdflatex", path=tex_env()["PATH"]) else "pdflatex not found"


def record(name: str, res: Result, seconds: float) -> None:
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    data: JsonObject = jobj(json.loads(RESULTS.read_text(encoding="utf-8"))) if RESULTS.exists() else {}
    data[name] = {"converged": res.converged, "rounds": len(res.iterations) - 1, "seconds": round(seconds, 1),
                  "trend": [it["open"] for it in res.iterations],
                  "geometry_error": [it["geometry_error"] for it in res.iterations],
                  "unresolved": [f"{u['kind']}: {u.get('why')}" for u in res.unresolved],
                  "patch_lines": sum(1 for l in res.patch.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---"))}
    RESULTS.write_text(json.dumps(data, indent=1), encoding="utf-8")


@pytest.mark.inverse
@pytest.mark.parametrize("name,rounds", [("b_text", 2), ("b_lists", 2), ("b_slides", 2), ("b_layout", 3)])
def test_converge_source_pairs(name: str, rounds: int, tmp_path: Path):
    if reason := pdflatex_missing():
        pytest.skip(reason)
    from beamer2slides.inverse import converge, ir_from_tex
    target = ir_from_tex(INV / f"{name}.tex", tmp_path / "target", False)
    t = time.time()
    res = converge(INV / "a.tex", target, tmp_path / "loop", 8, False, None, TOL, print, True, None)
    record(f"pair:{name}", res, time.time() - t)
    assert res.converged, res.unresolved
    assert len(res.iterations) - 1 <= rounds


SYNTHETIC: dict[str, tuple[Callable[[JsonObject], JsonObject], int]] = {
    "move": (lambda d: ed.move(d, 3, ed.element(d, 3, None, text="Plain paragraph"), 0, 40), 3),
    "bold": (lambda d: ed.split_style(d, 3, "paragraph", bold=True), 1),
    "recolour": (lambda d: ed.split_style(d, 1, "level item", color="#ff0000"), 1),
    "reword": (lambda d: ed.reword(d, 1, "Back to first level", "Back to the top level"), 1),
    "add_text": (lambda d: ed.add_text_box(d, 4, "A note added in Slides", 40, 220, size=10.91,
                                                color="#0000ff", width=120.0), 2),
    "delete_bullet": (lambda d: ed.delete_paragraph(d, 1, "Second level again"), 2),
    "add_slide": (lambda d: ed.add_slide(d, 2, "A new slide", ["First new point", "Second new point"], key=None), 3),
    "swap": (lambda d: ed.swap_slides(d, 2, 3), 1),
    "notes": (lambda d: ed.set_notes(d, 1, "Say something about nesting."), 1),
    "image": (lambda d: ed.add_image(d, 4, str(INV / "img" / "plot.png"), [200, 150, 330, 240]), 2),
}


@pytest.fixture(scope="module")
def basic_deck(tmp_path_factory: pytest.TempPathFactory) -> JsonObject:
    if reason := pdflatex_missing():
        pytest.skip(reason)
    work = tmp_path_factory.mktemp("basic")
    built = Workspace(TESTS / "decks" / "01_basic.tex", work, handout=False, engine=None, fresh=True).build(work / "classify", False, None)
    assert not isinstance(built, str), built
    return built.target()


@pytest.mark.inverse
@pytest.mark.parametrize("name", list(SYNTHETIC))
def test_converge_synthetic_edits(name: str, basic_deck: JsonObject, tmp_path: Path):
    from beamer2slides.inverse import converge
    make, rounds = SYNTHETIC[name]
    target = make(basic_deck)
    t = time.time()
    res = converge(TESTS / "decks" / "01_basic.tex", target, tmp_path / "loop", 8, False, None, TOL, print, True, None)
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


def compiles(monkeypatch: pytest.MonkeyPatch, build: Path, states: Sequence[dict[str, str]]) -> list[Sequence[str]]:
    """A TeX engine that writes `states[n]` into the build folder on its n-th run, and a log that
    never asks for anything - which is what a talk with no sections really produces."""
    runs: list[Sequence[str]] = []

    def run(cmd: Sequence[str], **kw: object) -> "sp.CompletedProcess[str]":
        for name, text in states[min(len(runs), len(states) - 1)].items():
            (build / name).write_text(text, encoding="utf-8")
        (build / "main.log").write_text(EMPTY_OUTLINES, encoding="utf-8")
        runs.append(cmd)
        return sp.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("beamer2slides.inverse.subprocess.run", run)
    return runs


def test_a_compile_runs_again_while_the_auxiliary_files_move(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The log is not the whole rule. A talk with no sections asks for no rerun after its first
    pass - rightly, there are no bookmarks to settle - while Madrid's footline still reads `2/1`,
    beamer's \\inserttotalframenumber coming out of the .nav the *next* pass reads. Measured: with
    the log alone, the pull loop's own compile of such a talk hands the page a wrong frame total
    and the loop then reads it as a residual."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.tex").write_text(TWO_FRAMES, encoding="utf-8")
    ws = Workspace(src / "main.tex", tmp_path / "work", handout=False, engine=None, fresh=True)
    runs = compiles(monkeypatch, ws.build_dir, [{"main.nav": "one"}, {"main.nav": "two"},
                                                {"main.nav": "two"}])
    ws.compile()
    assert len(runs) == 3
    # and a folder the turn before left settled costs one pass, which is the loop's common case
    runs = compiles(monkeypatch, ws.build_dir, [{"main.nav": "two"}])
    ws.compile()
    assert len(runs) == 1


def lualatex_missing() -> str | None:
    from beamer2slides.inverse import tex_env
    return None if shutil.which("lualatex", path=tex_env()["PATH"]) else "lualatex not found"


# CRLF lines (a CR kept would be a line more in the verbatim) and trailing spaces, as LuaTeX's own
# reader is given them; a preamble reading another file
WARM_MAIN = ("\\documentclass{article}   \r\n\\input{defs}\r\n\\newcommand\\other{A}\r\n\\begin{document}\r\n"
             "BODY \\word{} \\other   \r\n\\begin{verbatim}\r\nx\r\ny\r\n\\end{verbatim}\r\nz\r\n\\end{document}\r\n")


def pages_of(pdf_path: Path) -> list[bytes]:
    from beamer2slides import pdf
    doc = pdf.Document(pdf_path)
    try:
        return [page.render(2.0, clip=None, transparent=False).tobytes() for page in doc]
    finally:
        doc.close()


@pytest.mark.skipif(lualatex_missing() is not None, reason="lualatex not found")
def test_an_engine_waiting_on_the_preamble_compiles_what_a_cold_one_compiles(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch):
    """The converge loop's passes are run by a lualatex started early on the tree's preamble
    (`Workspace.start_warm`). Each tree below is compiled warm, then cold in a workspace of its
    own, and the pages must be the same pixels: a body edit (the engine used), and a preamble
    changed in main or in a file it reads (the engine refused, or the page would say the old
    preamble's words)."""
    from beamer2slides.inverse import Warm
    src = tmp_path / "src"
    src.mkdir()
    main = src / "main.tex"
    used: list[Path] = []
    real = Workspace.run_warm

    def counted(self: Workspace, warm: Warm, job: str) -> int | None:
        used.append(warm.folder)
        return real(self, warm, job)

    monkeypatch.setattr(Workspace, "run_warm", counted)
    trees = [("one", "\\newcommand\\other{A}", "BODY"), ("one", "\\newcommand\\other{A}", "EDITED"),
             ("one", "\\newcommand\\other{B}", "EDITED"), ("two", "\\newcommand\\other{B}", "EDITED")]

    def write(folder: Path, word: str, other: str, body: str) -> None:
        """The tree, each file written only when its words change (as the loop's edits are)."""
        for name, text in (("main.tex", WARM_MAIN.replace("\\newcommand\\other{A}", other).replace("BODY", body)),
                           ("defs.tex", f"\\newcommand\\word{{{word}}}\n")):
            if not (folder / name).is_file() or (folder / name).read_bytes() != text.encode():
                (folder / name).write_text(text, encoding="utf-8", newline="")

    write(src, *trees[0])
    ws = Workspace(main, tmp_path / "work", handout=False, engine="lualatex", fresh=True)
    ws.warm_up = True
    try:
        for n, (word, other, body) in enumerate(trees):
            write(src, word, other, body)   # what the cold workspace copies
            write(ws.src, word, other, body)   # the loop's working copy, as `Workspace.write` edits it
            warm, error = ws.compile()
            assert warm is not None, error
            cold_ws = Workspace(main, tmp_path / f"cold{n}", handout=False, engine="lualatex", fresh=True)
            cold, error = cold_ws.compile()
            assert cold is not None, error
            assert pages_of(warm) == pages_of(cold), f"tree {n} {(word, other, body)}"
            if n == 1:
                assert len(used) >= 2, "a body edit is compiled by the engine waiting on its preamble"
    finally:
        ws.stop_warm()


# a deck whose pages show what a piece must carry over from the frames it skips: page and frame
# numbers, sections in the outline and the contents, a reference to a frame another piece typesets,
# a fragile frame
PIECES_MAIN = r"""\documentclass{beamer}
\setbeamertemplate{footline}[frame number]
\begin{document}
\begin{frame}[label=title]{Contents}\tableofcontents\end{frame}
\section{One}
\begin{frame}[label=a]{A}Points to \ref{late} on page \pageref{late}.\end{frame}
\begin{frame}[label=b]{B}\only<1>{first}\only<2>{second}\end{frame}
\begin{frame}[fragile,label=c]{C}
\begin{verbatim}
x = 1
\end{verbatim}
\end{frame}
\section{Two}
\begin{frame}[label=d]{D}\begin{equation}\label{late}e = m\end{equation}\end{frame}
\begin{frame}[label=e]{E}BODY\end{frame}
\section{Three}
\begin{frame}[label=f]{F}words\end{frame}
\begin{frame}[label=g]{G}\pause more \pause words\end{frame}
\begin{frame}[label=h]{H}last\end{frame}
\end{document}
"""


def outline_pages(path: Path) -> list[tuple[int, bytes, int]]:
    """A PDF's outline: (depth, title, the page its named destination is on)."""
    from beamer2slides.pdf.pure.document import read
    from beamer2slides.pdf.pure.join import leaves, outline
    from beamer2slides.pdf.pure.syntax import Ref, String
    doc = read(str(path))
    refs: list[int] = []
    for i in range(doc.page_count):
        found = doc.page_dict(i)
        refs.append(found[0] if found is not None and found[0] is not None else -1)
    names = doc.resolve(doc.catalog.get("Names"))
    dests = dict(leaves(doc, names.get("Dests"), "Names")) if isinstance(names, dict) else {}
    items = outline(doc)
    assert items is not None
    out: list[tuple[int, bytes, int]] = []
    for depth, title, name in items:
        dest = doc.resolve(dests.get(String(name)))
        if isinstance(dest, dict):
            dest = doc.resolve(dest.get("D"))
        page = dest[0] if isinstance(dest, list) and dest else None
        out.append((depth, title, refs.index(page.num) if isinstance(page, Ref) else -1))
    return out


@pytest.mark.skipif(lualatex_missing() is not None, reason="lualatex not found")
def test_a_compile_in_pieces_is_the_pages_of_a_whole_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The converge loop cuts a lualatex compile into pieces on as many cores (`pieces`,
    `Workspace.compile_in_pieces`). Each tree below is compiled so, then whole and cold in a
    workspace of its own: the same pages to the pixel, the same outline, and SyncTeX's pages
    for every page. The first compile's first pass is whole (no .aux to cut by) and its second
    in pieces, settled at once. A body edit, which only its frame's piece typesets again
    (`compile_patched`), and a frame and a section coming in ahead of every cut (the pieces after
    it begin a page off, and passes go on until they read what they write)."""
    passes: list[int] = []
    piece_pass = Workspace.piece_pass

    def counted(self: Workspace, cmd: list[str], job: str, plan: list[pieces.Cut]) -> tuple[str, bool, bool]:
        passes.append(len(plan))
        return piece_pass(self, cmd, job, plan)
    monkeypatch.setattr(Workspace, "piece_pass", counted)
    src = tmp_path / "src"
    src.mkdir()
    main = src / "main.tex"
    trees = [PIECES_MAIN, PIECES_MAIN.replace("BODY", "edited words"),
             PIECES_MAIN.replace("BODY", "edited words").replace(
                 "\\section{One}", "\\begin{frame}{New}new\\end{frame}\n\\section{Zero}\n\\section{One}")]
    main.write_text(trees[0], encoding="utf-8")
    ws = Workspace(main, tmp_path / "work", handout=False, engine="lualatex", fresh=True)
    ws.warm_up = True
    ws.pieces = 3
    try:
        for n, tree in enumerate(trees):
            main.write_text(tree, encoding="utf-8")
            ws.main.write_text(tree, encoding="utf-8")
            split, error = ws.compile()
            assert split is not None, error
            if n == 1:
                assert ws.patched == 1, f"a body edit typesets its frame again alone ({ws.patch_refused})"
                assert ws.piece_syncs is None
            else:
                assert ws.piece_syncs is not None, f"tree {n} was compiled in pieces ({ws.split_refused})"
            if n == 0:
                assert passes == [3], "one pass in pieces after the whole first one"
            if n == 2:
                assert ws.patched == 1 and "structure" in ws.patch_refused, ws.patch_refused
                assert passes == [3, 3, 3, 3], "a frame and a section ahead of every cut: three passes"
            cold, error = Workspace(main, tmp_path / f"cold{n}", handout=False, engine="lualatex",
                                    fresh=True).compile()
            assert cold is not None, error
            pages = pages_of(split)
            assert pages == pages_of(cold), f"tree {n}"
            assert len(ws.sync_pages()) == len(pages)
            outlines = [outline_pages(path) for path in (split, cold)]
            assert outlines[0] == outlines[1] and len(outlines[0]) >= 3, f"tree {n}"
        assert ws.split_refused == ""
    finally:
        ws.stop_warm()


def stat_font() -> bytes:
    """A static font that keeps a STAT table, as every instance cut from a google/fonts variable
    font does (`fontfetch`): no fvar, so no axes."""
    import io
    from fontTools.fontBuilder import FontBuilder
    from fontTools.otlLib.builder import buildStatTable
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    fb = FontBuilder(1000, isTTF=True)
    glyphs = [".notdef", "A"]
    fb.setupGlyphOrder(glyphs)
    fb.setupCharacterMap({ord(c): "A" for c in map(chr, range(33, 127))})
    pen = TTGlyphPen(None)
    pen.moveTo((0, 0)), pen.lineTo((0, 500)), pen.lineTo((500, 0)), pen.closePath()
    glyph = pen.glyph()
    fb.setupGlyf({g: glyph for g in glyphs})
    fb.setupHorizontalMetrics({g: (600, 0) for g in glyphs})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "Stat Sans", "styleName": "Regular"})
    fb.setupOS2(usWeightClass=400)
    fb.setupPost()
    buildStatTable(fb.font, [{"tag": "wght", "name": "Weight",
                              "values": [{"value": 400, "name": "Regular", "flags": 2}]}])
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


@pytest.mark.skipif(lualatex_missing() is not None, reason="lualatex not found")
def test_a_static_font_with_a_stat_table_is_written_from_its_own_file(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch):
    """luaotfload takes a STAT table for variation data and gives the font a glyph stream
    provider, which loads the whole-font streams cache at PDF write: a quarter of a fetched-font
    deck's pass. The loop's lualatex runs take that away (`inverse.NO_GLYPH_STREAMS`); the font
    the page says it used reports the provider, and the pages are the same pixels either way."""
    from beamer2slides import inverse
    src = tmp_path / "src"
    src.mkdir()
    (src / "StatSans-Regular.ttf").write_bytes(stat_font())
    main = src / "main.tex"
    main.write_text("\\documentclass{article}\\usepackage{fontspec}\\setmainfont{StatSans-Regular.ttf}[Path=./]\n"
                    "\\begin{document}ABC\\directlua{texio.write_nl('STREAM ' .. "
                    "tostring(fonts.hashes.identifiers[font.current()].streamprovider))}\\end{document}\n",
                    encoding="utf-8")

    def compiled(name: str) -> tuple[str, list[bytes]]:
        ws = Workspace(main, tmp_path / name, handout=False, engine="lualatex", fresh=True)
        pdf, error = ws.compile()
        assert pdf is not None, error
        log = (pdf.with_suffix(".log")).read_text(encoding="utf-8", errors="replace")
        said = [line.split()[1] for line in log.splitlines() if line.startswith("STREAM ")]
        assert said, "the page reported its font"
        return said[0], pages_of(pdf)

    fixed = compiled("fixed")
    monkeypatch.setattr(inverse, "NO_GLYPH_STREAMS", "")
    plain = compiled("plain")
    if plain[0] == "nil":
        pytest.skip("this luaotfload gives a static font no stream provider")
    assert plain[0] == "2"
    assert fixed[0] == "nil"
    assert fixed[1] == plain[1]


def drawn_font(family: str, shapes: dict[str, list[tuple[int, int]]]) -> bytes:
    """A font drawing each of `shapes`' characters as its own polygon, and nothing else."""
    import io
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    names = {c: f"g{ord(c)}" for c in shapes}
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", *names.values()])
    fb.setupCharacterMap({ord(c): n for c, n in names.items()})
    glyphs = {}
    for c, points in [("", [(0, 0), (0, 10), (10, 0)]), *shapes.items()]:
        pen = TTGlyphPen(None)
        pen.moveTo(points[0])
        for point in points[1:]:
            pen.lineTo(point)
        pen.closePath()
        glyphs[names.get(c, ".notdef")] = pen.glyph()
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({g: (600, 0) for g in glyphs})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": family, "styleName": "Regular"})
    fb.setupOS2(usWeightClass=400)
    fb.setupPost()
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


@pytest.mark.skipif(lualatex_missing() is not None, reason="lualatex not found")
def test_the_fallback_chain_defined_on_demand_draws_what_luaotfloads_own_draws(tmp_path: Path):
    """`scripts.LAZY_FALLBACK` defines a chain font only where a glyph first needs it, where
    luaotfload's `fallback=` defines the whole chain for every face at every size. The letters a
    face lacks must still come from the first chain font that has them, at the face's size: the
    pages are luaotfload's own, pixel for pixel, and not a page with the letters missing. B is in
    both chain fonts, drawn differently, so taking it from the second would show."""
    from beamer2slides import scripts
    src = tmp_path / "src"
    (src / "fonts").mkdir(parents=True)
    (src / "fonts" / "Base.ttf").write_bytes(drawn_font("Base", {"A": [(0, 0), (0, 500), (500, 0)]}))
    (src / "fonts" / "First.ttf").write_bytes(drawn_font("First", {"B": [(0, 0), (0, 500), (500, 500), (500, 0)]}))
    (src / "fonts" / "Second.ttf").write_bytes(drawn_font("Second", {"B": [(0, 0), (250, 700), (500, 0)],
                                                                     "C": [(0, 0), (0, 700), (100, 0)]}))
    chain = '{"[fonts/First.ttf]:mode=node;", "[fonts/Second.ttf]:mode=node;"}'
    body = "\\pagestyle{empty}\\begin{document}ABC {\\Large ABC} {\\tiny CBA}\\end{document}\n"

    def compiled(name: str, preamble: str, feature: str) -> list[bytes]:
        main = src / f"{name}.tex"
        main.write_text("\\documentclass{article}\\usepackage{fontspec}\n" + preamble +
                        f"\\setmainfont{{Base.ttf}}[Path=./fonts/{feature}]\n" + body, encoding="utf-8")
        pdf, error = Workspace(main, tmp_path / name, handout=False, engine="lualatex", fresh=True).compile()
        assert pdf is not None, error
        missing = "Missing character" in pdf.with_suffix(".log").read_text(encoding="utf-8", errors="replace")
        assert missing == (not feature), name
        return pages_of(pdf)

    lazy = compiled("lazy", scripts.LAZY_FALLBACK + f"\\directlua{{b2sfallbacks[\"chain\"] = {chain}}}\n",
                    ",RawFeature={b2sfallback=chain}")
    own = compiled("own", f"\\directlua{{luaotfload.add_fallback(\"chain\", {chain})}}\n", ",RawFeature={fallback=chain}")
    bare = compiled("bare", "", "")
    assert lazy == own
    assert lazy != bare
