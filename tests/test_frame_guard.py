"""Pull's frame guard (frame_guard.py): a frame the loop made worse than its best round is put back.

Offline: the loop runs for real (`inverse.converge`) on a two-frame source, with the compile, the
read-back and the translators stood in for - a "compile" draws each frame's page as a black box, in
the right place while the frame says GOOD and elsewhere once a scripted edit made it say BAD. The guard
has to see that with Google's picture of the slide (ink) and without it (residuals and words).
"""

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from beamer2slides import inverse
from beamer2slides.frame_guard import FrameGuard, Seen, slide_words, why, word_error
from beamer2slides.inverse import Candidate, Edit, converge, report

W, H = 400, 225                     # page, pt (= PDF pixels at 72 dpi)
BOX = (40, 60, 360, 120)            # the text element's box
RIGHT = (60, 70, 200, 100)          # where the page's ink stands while the frame says GOOD
WRONG = (60, 170, 200, 200)         # ... and where a bad edit moves it: outside every box

SOURCE = r"""\documentclass{beamer}
\begin{document}
\begin{frame}[label=f1]
Alpha GOOD
\end{frame}
\begin{frame}[label=f2]
Beta GOOD
\end{frame}
\end{document}
"""


def text_el(eid: str, text: str) -> dict:
    x0, y0, x1, y1 = BOX
    run = {"text": text, "font": "Arial", "family": "sans", "size": 20.0, "bold": False, "italic": False,
           "color": "#000000", "underline": False, "strike": False, "script": None, "link": None, "highlight": None}
    return {"id": eid, "kind": "text", "role": "body", "bbox": list(BOX),
            "paragraphs": [{"align": "left", "level": 0, "bullet": None, "size": 20.0, "text_x0": x0,
                            "lines": [{"baseline": y0 + 20, "x0": x0, "x1": x1}], "runs": [run]}]}


def slide_ir(page: int, key: str, text: str) -> dict:
    return {"page": page, "size": [W, H], "key": key, "notes": None, "elements": [text_el(f"e{page}", text)]}


def page_image(scale: int, good: bool) -> Image.Image:
    img = Image.new("RGB", (W * scale, H * scale), "white")
    box = RIGHT if good else WRONG
    ImageDraw.Draw(img).rectangle(tuple(v * scale for v in box), fill="black")
    return img


def fake_compile(self):
    pages = [page_image(1, "GOOD" in self.source.text(f.file)[f.body:f.body_end]) for f in self.source.frames]
    pdf = self.build_dir / f"{self.main.stem}.pdf"
    pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=72.0)
    return pdf, ""


def fake_build(self, out, target_has_notes=False, compiled=None):
    """classify as a stand-in: one slide per frame, its words the frame's body."""
    pdf, _ = compiled if compiled is not None else self.compile()
    slides, words = [], {}
    for k, f in enumerate(self.source.frames):
        body = " ".join(self.source.text(f.file)[f.body:f.body_end].split())
        slides.append(slide_ir(k, f.label, body))
        words[k] = body.split()
    return Candidate(self.source, pdf, {"slides": slides}, list(self.source.frames), words=words)


@pytest.fixture
def stand_ins(monkeypatch):
    """Compile and read-back stood in for; the translators write one bad edit (GOOD -> BAD in the
    second frame) the first time they are asked, and nothing after."""
    monkeypatch.setattr(inverse.Workspace, "compile", fake_compile)
    monkeypatch.setattr(inverse.Workspace, "build", fake_build)
    calls = []

    def plan(self):
        calls.append(1)
        if len(calls) > 1:
            return [], []
        f = self.cand.source.frames[1]
        text = self.cand.source.text(f.file)
        at = text.index("GOOD", f.start)
        return [Edit(f.file, at, at + 4, "BAD", "text", ("text", 1, "e1", 0))], []

    monkeypatch.setattr(inverse.Planner, "plan", plan)
    return calls


def target(tmp_path: Path, thumbnails: bool) -> dict:
    # slide 1's words differ, so the loop has something open to plan for
    slides = [slide_ir(0, "f1", "Alpha GOOD more"), slide_ir(1, "f2", "Beta GOOD")]
    if thumbnails:
        for k, s in enumerate(slides):
            path = tmp_path / "thumbs" / f"{k + 1:03}.png"
            path.parent.mkdir(exist_ok=True)
            page_image(2, True).save(path)          # Google's picture: twice the size, the box right
            s["thumbnail"] = str(path)
    return {"slides": slides}


@pytest.mark.parametrize("thumbnails", [True, False], ids=["ink", "residuals"])
def test_a_frame_the_loop_made_worse_is_put_back(tmp_path, stand_ins, thumbnails):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "main.tex").write_text(SOURCE, encoding="utf-8")
    tgt = target(tmp_path, thumbnails)
    res = converge(tree / "main.tex", tgt, tmp_path / "loop", max_iter=3, log=lambda *_: None)

    assert len(stand_ins) == 2                                  # one bad round, then nothing to write
    assert [it["open"] for it in res.iterations] == [1, 2]      # the loop's own view: one more residual
    if thumbnails:
        assert [it["ink"] for it in res.iterations] == [1.0, 0.5]
    else:
        assert all("ink" not in it for it in res.iterations)
    # the second frame is back as it was, the source has no edit left, the report says why
    assert "Beta GOOD" in (tmp_path / "loop" / "src" / "main.tex").read_text(encoding="utf-8")
    assert res.files == {} and res.patch == ""
    assert len(res.residuals) == 1 and res.residuals[0]["target_slide"] == 0
    [r] = res.restored
    assert r["target_slides"] == [1] and r["frame_label"] == "f2" and r["iteration"] == 0
    assert r["mode"] == ("ink" if thumbnails else "residuals")
    assert "first draft" in r["why"] and r["where"].endswith("main.tex:6-8")
    assert r["score_now"] == r["score_best"]
    data, md = report(res, tgt)
    json.dumps(data)                                            # the agent layer's result stays data
    assert data["restored"][0]["frame_label"] == "f2" and "## Frames put back" in md


def test_without_the_guard_the_bad_edit_stays(tmp_path, stand_ins):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "main.tex").write_text(SOURCE, encoding="utf-8")
    res = converge(tree / "main.tex", target(tmp_path, True), tmp_path / "loop", max_iter=3,
                   log=lambda *_: None, guard=False)
    assert res.restored == [] and "Beta BAD" in next(iter(res.files.values()))


def seen(round_: int, text: str, score: float, mode: str = "residuals", penalty: float | None = None) -> Seen:
    if penalty is None:
        penalty = -score if mode == "residuals" else 0.0
    return Seen(round_, Path("main.tex"), (0, len(text)), text, (0,), mode, score, "f", penalty)


def guard_with(*history: Seen) -> FrameGuard:
    g = FrameGuard({"slides": []})
    g.history[("label", "f")] = list(history)
    g.last_round = history[-1].round
    return g


def test_gains_are_kept_and_ties_go_to_the_earliest_text():
    # the loop's rounds made it better: nothing to put back
    assert guard_with(seen(0, "a", -10), seen(1, "b", -4), seen(2, "c", -3)).plan() == []
    # float noise in the ink, and fewer residuals: the loop's text stays (a notes fix, say)
    assert guard_with(seen(0, "a", 0.9900, "ink", 5), seen(1, "b", 0.9897, "ink", 4)).plan() == []
    # a thousandth of ink lost is a worse page, whatever the residuals say (apps-edu-zh:9)
    [(_, best)] = guard_with(seen(0, "a", 0.987, "ink", 5), seen(1, "b", 0.986, "ink", 1)).plan()
    assert best.round == 0
    # worse than two rounds that scored the same: the earlier one (round 1 changed nothing the score
    # could see - sc-functions:8 lost ink there with the same residuals)
    [(final, best)] = guard_with(seen(0, "a", -5), seen(1, "b", -5), seen(2, "c", -9)).plan()
    assert final.text == "c" and best.round == 0
    # no worse, but no better either: the loop's edits earned nothing and go (arabic-training:6 read
    # 29 residuals and 29 while its ink fell from 0.960 to 0.805)
    [(final, best)] = guard_with(seen(0, "a", -29), seen(1, "b", -29)).plan()
    assert final.text == "b" and best.round == 0
    # the same ink and the same residuals: the same
    [(_, best)] = guard_with(seen(0, "a", 0.99, "ink", 3), seen(1, "b", 0.99, "ink", 3)).plan()
    assert best.round == 0
    # a round that really was better is the one taken
    [(_, best)] = guard_with(seen(0, "a", 0.90, "ink"), seen(1, "b", 0.97, "ink"), seen(2, "c", 0.5, "ink")).plan()
    assert best.round == 1


def test_the_reason_names_what_was_measured():
    assert "picture less (ink 0.500, 0.970" in why(seen(2, "c", 0.5, "ink", 3), seen(1, "b", 0.97, "ink", 3))
    assert "further from the deck (9 weighted" in why(seen(2, "c", -9), seen(0, "a", -5))
    assert "nothing better that can be measured (ink 0.990 and 3 weighted" in why(
        seen(1, "b", 0.99, "ink", 3), seen(0, "a", 0.99, "ink", 3))


def test_words_the_page_has_wrong():
    slide = {"elements": [text_el("t", "Hello, world (again)"),
                          {"kind": "table", "rows": [["One", "two"]], "bbox": [0, 0, 1, 1]},
                          {"kind": "image", "role": "math", "paragraphs": [{"runs": [{"text": "x"}]}]}]}
    assert slide_words(slide) == ["hello", "world", "again", "one", "two"]
    assert word_error(["a", "b", "b"], ["b", "c"]) == 3
