"""Pictures pulled back to the source (deck_ir.picture_props, compare.displayed_picture, inverse's
picture translators). Offline: no TeX, no Google. The compile loop on synthetic picture targets is
opt-in (`python -m pytest -m inverse -k pictures`)."""

import copy
import hashlib
import json
import math
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from beamer2slides.compare import Comparison, compare, displayed_picture, grey16, hash_distance, picture_hash
from beamer2slides.deck_ir import element_of, image_format, picture_props
from beamer2slides.inverse import (Candidate, Context, Picture, Planner, Workspace, ensure_preamble, picture_latex,
                                   picture_slug, picture_sources, reading_order)

EMU = 12700
TESTS = Path(__file__).resolve().parent

TEX = r"""\documentclass[aspectratio=169]{beamer}
\usepackage{tikz}
\begin{document}
\begin{frame}[label=plot]{Plot}
  \begin{columns}
    \column{0.5\textwidth}
    \includegraphics[width=0.9\linewidth]{figures/photo.png}
    \column{0.5\textwidth}
    \begin{tikzpicture}[scale=0.9]
      \draw[->] (0,0) -- (3,0);
      \draw[thick, blue] (0,0) -- (3,2);
    \end{tikzpicture}
  \end{columns}
\end{frame}
\end{document}
"""


def photo(path: Path, size=(300, 200), seed=1) -> Path:
    """A photo-like picture whose layout depends on the seed: a gradient in a random direction,
    big random blobs, noise."""
    rng = np.random.default_rng(seed)
    w, h = size
    y, x = np.mgrid[0:h, 0:w] / max(w, h)
    angle = rng.uniform(0, 2 * math.pi)
    ramp = (np.cos(angle) * x + np.sin(angle) * y) * 255
    arr = np.stack([ramp, 255 - ramp, 128 + 0 * ramp], -1)
    for _ in range(6):
        cx, cy, r = rng.uniform(0, w / max(w, h)), rng.uniform(0, h / max(w, h)), rng.uniform(0.08, 0.25)
        arr[(x - cx) ** 2 + (y - cy) ** 2 < r * r] = rng.uniform(0, 255, 3)
    arr += rng.normal(0, 8, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def image_element(w_pt, h_pt, transform, props=None):
    return {"objectId": "img1", "size": {"width": {"magnitude": w_pt * EMU, "unit": "EMU"},
                                         "height": {"magnitude": h_pt * EMU, "unit": "EMU"}},
            "transform": {"unit": "EMU", **transform}, "image": {"contentUrl": "https://x", "imageProperties": props or {}}}


# ---------------------------------------------------------------- deck_ir

def test_picture_props_read_rotation_crop_transparency_outline():
    from beamer2slides.deck_ir import StyleResolver, affine
    th = math.radians(30)
    w, h, scale = 100.0, 50.0, 2.0
    cx, cy = 300.0, 200.0
    a, b, d, e = math.cos(th), -math.sin(th), math.sin(th), math.cos(th)
    tr = {"scaleX": a, "shearX": b, "shearY": d, "scaleY": e,
          "translateX": (cx - (a * w / 2 + b * h / 2)) * EMU, "translateY": (cy - (d * w / 2 + e * h / 2)) * EMU}
    props = {"cropProperties": {"leftOffset": 0.1, "rightOffset": 0.3, "topOffset": 0.2, "bottomOffset": 0.05},
             "transparency": 0.5, "brightness": 0.25,
             "outline": {"outlineFill": {"solidFill": {"color": {"rgbColor": {"red": 1.0}}}},
                         "weight": {"magnitude": 38100, "unit": "EMU"}, "dashStyle": "SOLID"}}
    pe = image_element(w, h, tr, props)
    el = element_of(pe, affine(pe["transform"]), StyleResolver({}), None, scale, 720, None, None)
    assert el["rotation"] == 30.0 and not el.get("flip")
    assert el["box"] == [(cx - w / 2) / scale, (cy - h / 2) / scale, (cx + w / 2) / scale, (cy + h / 2) / scale]
    aabb_w = w * math.cos(th) + h * math.sin(th)
    assert abs((el["bbox"][2] - el["bbox"][0]) - aabb_w / scale) < 0.02
    assert el["crop"] == {"l": 0.1, "t": 0.2, "r": 0.3, "b": 0.05}
    assert el["opacity"] == 0.5 and el["brightness"] == 0.25
    assert el["outline"] == {"color": "#ff0000", "weight": 1.5, "dash": "SOLID"}
    # an untouched picture carries none of them; a mirrored one is a flip
    plain = picture_props(image_element(w, h, {"scaleX": 1, "scaleY": 1}), [1, 0, 0, 0, 1, 0], w, h, scale, {})
    assert set(plain) == {"box"}
    mirrored = picture_props({}, [-1, 0, 100, 0, 1, 0], w, h, scale, {})
    assert mirrored["flip"] and abs(mirrored.get("rotation", 0.0)) < 0.01
    upside = picture_props({}, [1, 0, 0, 0, -1, 50], w, h, scale, {})
    assert upside["flip"] and abs(abs(upside["rotation"]) - 180) < 0.01


def test_image_formats():
    assert image_format(b"\x89PNG\r\n") == "png" and image_format(b"\xff\xd8\xff") == "jpeg"
    assert image_format(b"GIF89a") == "gif" and image_format(b"RIFF\0\0\0\0WEBPVP8 ") == "webp"
    assert image_format(b"\x01\0\0\0" + b"\0" * 36 + b" EMF") == "emf"


# ---------------------------------------------------------------- LaTeX options

def test_picture_latex_crop_angle_opacity_outline():
    ctx = Context()
    pic = Picture("figures/photo-12345678.png", Path("x.png"), (300.0, 200.0))
    te = {"bbox": [10, 20, 110, 70], "box": [10, 20, 110, 70], "crop": {"l": 0.1, "t": 0.2, "r": 0.3, "b": 0.05}}
    assert picture_latex(te, pic, ctx) == \
        r"\includegraphics[trim=30.00 10.00 90.00 40.00,clip,width=100.0pt,height=50.0pt]{figures/photo-12345678.png}"
    turned = {**te, "rotation": 30.0}
    assert picture_latex(turned, pic, ctx).endswith("height=50.0pt,angle=-30]{figures/photo-12345678.png}")
    see_through = {**turned, "opacity": 0.5, "outline": {"color": "#ff0000", "weight": 3.0, "dash": "SOLID"}}
    out = picture_latex(see_through, pic, ctx)
    assert out.startswith(r"\rotatebox{-30}{\tikz\node[inner sep=0pt,text opacity=0.50,draw=red,line width=3.00pt]{")
    assert "angle" not in out and out.endswith("};}")
    assert r"\usepackage{tikz}" in ctx.packages
    assert picture_latex({**te, "crop": None, "flip": True}, pic, ctx).startswith(r"\reflectbox{\includegraphics[width=100.0pt")


def test_picture_slug():
    assert picture_slug("Team photo, 2026!") == "team-photo-2026"
    assert picture_slug(None) == "picture" and picture_slug("b2s_f3") == "picture"


def test_displayed_picture_crops_and_turns(tmp_path):
    f = photo(tmp_path / "p.png")
    el = {"file": str(f), "bbox": [0, 0, 60, 40], "box": [0, 0, 60, 40], "crop": {"l": 0.5, "t": 0.0, "r": 0.0, "b": 0.5}}
    shown = displayed_picture(el)
    quarter = Image.open(f).crop((150, 0, 300, 100))
    assert hash_distance(grey16(shown), grey16(quarter)) < 0.02
    assert hash_distance(grey16(shown), picture_hash(f)) > 0.05
    half = displayed_picture({**el, "crop": None, "opacity": 0.5})
    assert np.asarray(half.convert("L")).mean() > np.asarray(Image.open(f).convert("L")).mean() + 20
    turned = displayed_picture({**el, "crop": None, "rotation": 90.0})
    assert turned.size[0] < turned.size[1]


# ---------------------------------------------------------------- the planner

def planner_for(tmp_path: Path, cur: dict, target: dict, hashes: dict | None = None):
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "talk.tex").write_text(TEX, encoding="utf-8")
    photo(src / "figures" / "photo.png", (600, 400), seed=5)
    ws = Workspace(src / "talk.tex", tmp_path / "work")
    frames = [ws.source.frames[0] for _ in cur["slides"]]
    cand = Candidate(ws.source, tmp_path / "talk.pdf", cur, frames)
    ctx = Context()
    comp = compare(cur, target, None, hashes)
    return Planner(cand, comp, target, ctx, ws, set(), {}, hashes), ws, ctx


def deck_with(elements):
    return {"slides": [{"page": 0, "key": "plot", "size": [453.54, 255.12], "elements": elements}]}


def test_picture_sources_in_source_order(tmp_path):
    p, ws, _ = planner_for(tmp_path, deck_with([]), deck_with([]))
    text = p.cand.masked(ws.main)
    srcs = picture_sources(text, ws.source.frames[0])
    assert [s.kind for s in srcs] == ["graphics", "env"]
    assert text[srcs[1].inner[0]:srcs[1].inner[1]].endswith("\\end{tikzpicture}")
    left = {"kind": "image", "bbox": [20, 80, 200, 200]}
    right = {"kind": "image", "bbox": [240, 90, 420, 190]}
    wide = {"kind": "image", "bbox": [20, 30, 420, 60]}
    assert reading_order([right, left, wide]) == [wide, left, right]


def test_picture_file_names_reuse_and_formats(tmp_path):
    p, ws, ctx = planner_for(tmp_path, deck_with([]), deck_with([]))
    deck_files = tmp_path / "deck"
    deck_files.mkdir()
    # the deck's copy of photo.png: downscaled and re-encoded by Google -> the source's own file
    Image.open(ws.src / "figures" / "photo.png").resize((300, 200), Image.LANCZOS).save(deck_files / "down.jpg", quality=80)
    same = p.picture({"file": str(deck_files / "down.jpg"), "alt": None})
    assert same.rel == "figures/photo.png" and same.natural == (600.0, 400.0)
    # a new picture: the deck's bytes as they are, named by alt text and content
    new = photo(deck_files / "new.png", seed=9)
    pic = p.picture({"file": str(new), "alt": "Lab bench"})
    sha = hashlib.sha1(new.read_bytes()).hexdigest()
    assert pic.rel == f"figures/lab-bench-{sha[:8]}.png" and pic.path.read_bytes() == new.read_bytes()
    # the same bytes again (another slide): the file just written
    assert p.picture({"file": str(new), "alt": "Another name"}).rel == pic.rel
    # an animated GIF: first frame as PNG, with a note
    frames = [Image.new("RGB", (40, 30), c) for c in ((200, 0, 0), (0, 200, 0))]
    frames[0].save(deck_files / "anim.gif", save_all=True, append_images=frames[1:])
    gif = p.picture({"file": str(deck_files / "anim.gif"), "alt": None})
    assert gif.rel.endswith(".png") and Image.open(gif.path).getpixel((5, 5))[:3] == (200, 0, 0)
    assert any("first frame of 2" in n for n in ctx.notes)
    # brightness can't be an option: baked into a new file
    bright = p.picture({"file": str(new), "alt": "Lab bench", "brightness": 0.3})
    assert bright.rel != pic.rel and np.asarray(Image.open(bright.path).convert("L")).mean() > \
        np.asarray(Image.open(new).convert("L")).mean() + 15
    assert any("brightness baked" in n for n in ctx.notes)
    assert p.picture({"file": str(deck_files / "missing.png")}) is None


def test_replace_tikz_figure_keeps_it_commented(tmp_path):
    left = {"id": "p0f0", "kind": "image", "role": "figure", "bbox": [20, 80, 200, 200]}
    right = {"id": "p0f1", "kind": "image", "role": "figure", "bbox": [240, 90, 420, 190]}
    cur = deck_with([left, right])
    new = photo(tmp_path / "deck" / "chart.png", (360, 200), seed=3)
    target = deck_with([copy.deepcopy(left), {**copy.deepcopy(right), "file": str(new), "box": right["bbox"], "alt": "Chart"}])
    tikz_look = [255] * 200 + [0] * 56
    hashes = {id(cur["slides"][0]["elements"][0]): [128] * 256, id(target["slides"][0]["elements"][0]): [128] * 256,
              id(cur["slides"][0]["elements"][1]): tikz_look, id(target["slides"][0]["elements"][1]): picture_hash(new)}
    p, ws, ctx = planner_for(tmp_path, cur, target, hashes)
    assert [r["kind"] for r in p.comp.open()] == ["image"]
    edits, failed = p.plan()
    assert failed == [] and len(edits) == 1
    ws.write(edits)
    text = ws.source.text(ws.main)
    sha = hashlib.sha1(new.read_bytes()).hexdigest()[:8]
    assert f"    % b2s pull: replaced by figures/chart-{sha}.png\n    % \\begin{{tikzpicture}}[scale=0.9]\n" in text
    assert "      % \\draw[->] (0,0) -- (3,0);" in text and "    % \\end{tikzpicture}\n" in text
    assert f"    \\includegraphics[width=180.0pt,height=100.0pt]{{figures/chart-{sha}.png}}\n  \\end{{columns}}" in text
    assert "\\includegraphics[width=0.9\\linewidth]{figures/photo.png}" in text
    assert any("replaced in the deck" in n for n in ctx.notes)


def test_new_picture_over_a_diagram_replaces_it(tmp_path):
    diagram = {"id": "p0dg0", "kind": "diagram", "role": "figure", "bbox": [240, 90, 420, 190], "nodes": []}
    left = {"id": "p0f0", "kind": "image", "role": "figure", "bbox": [20, 80, 200, 200]}
    new = photo(tmp_path / "deck" / "d.png", seed=4)
    cur = deck_with([left, diagram])
    target = deck_with([copy.deepcopy(left), {"id": "img9", "kind": "image", "role": "figure", "bbox": [238, 92, 421, 189],
                                              "file": str(new)}])
    p, ws, ctx = planner_for(tmp_path, cur, target)
    kinds = sorted(r["kind"] for r in p.comp.open() if r["kind"] != "geometry")
    assert kinds == ["element_extra", "element_missing"]
    edits, failed = p.plan()
    assert failed == []
    ws.write(edits)
    text = ws.source.text(ws.main)
    assert "% b2s pull: replaced by figures/picture-" in text and "\\includegraphics[width=183.0pt,height=97.0pt]" in text


def test_translucent_turned_tikz_keeps_its_source(tmp_path):
    left = {"id": "p0f0", "kind": "image", "role": "figure", "bbox": [20, 80, 200, 200]}
    right = {"id": "p0f1", "kind": "image", "role": "figure", "bbox": [240, 90, 420, 190]}
    render = photo(tmp_path / "deck" / "render.png", (360, 200), seed=6)
    cur = deck_with([copy.deepcopy(left), copy.deepcopy(right)])
    target = deck_with([copy.deepcopy(left), {**right, "file": str(render), "opacity": 0.5, "rotation": 10.0,
                                              "box": [245, 100, 415, 180]}])
    hashes = {id(cur["slides"][0]["elements"][1]): picture_hash(render),
              id(target["slides"][0]["elements"][1]): [255] * 256,
              id(cur["slides"][0]["elements"][0]): [128] * 256, id(target["slides"][0]["elements"][0]): [128] * 256}
    p, ws, ctx = planner_for(tmp_path, cur, target, hashes)
    assert [r["kind"] for r in p.comp.open() if r["kind"] == "image"] == ["image"]
    p.comp.residuals = [r for r in p.comp.residuals if r["kind"] == "image"]
    edits, failed = p.plan()
    assert failed == []
    ws.write(edits)
    text = ws.source.text(ws.main)
    assert "\\rotatebox{-10}{\\begin{tikzpicture}[scale=0.9]\n      \\begin{scope}[transparency group,opacity=0.50]" in text
    assert "\\end{scope}\n    \\end{tikzpicture}}" in text
    # a second round with other values replaces the first edits instead of stacking them
    p2, _, _ = planner_for(tmp_path / "again", cur, target, hashes)
    p2.ws, p2.cand = ws, Candidate(ws.source, tmp_path / "talk.pdf", cur, [ws.source.frames[0]])
    target["slides"][0]["elements"][1].update(opacity=0.25, rotation=-5.0)
    p2.comp.residuals = [r for r in compare(cur, target, None, hashes).residuals if r["kind"] == "image"]
    p2.target, p2.tgt_slides = target, target["slides"]
    edits, failed = p2.plan()
    ws.write(edits)
    text = ws.source.text(ws.main)
    assert text.count("\\rotatebox") == 1 and "\\rotatebox{5}{" in text and text.count("transparency group") == 1
    assert "opacity=0.25" in text


def test_replaced_formula_picture_is_reported(tmp_path):
    math_el = {"id": "p0h0", "kind": "image", "role": "math", "bbox": [100, 100, 130, 112], "anchor": "p0t1"}
    cur = deck_with([math_el])
    new = photo(tmp_path / "deck" / "f.png", (60, 24), seed=8)
    target = deck_with([{**math_el, "file": str(new)}])
    hashes = {id(cur["slides"][0]["elements"][0]): [255] * 256, id(target["slides"][0]["elements"][0]): [0] * 256}
    p, ws, _ = planner_for(tmp_path, cur, target, hashes)
    edits, failed = p.plan()
    assert edits == [] and len(failed) == 1 and "formula" in failed[0]["why"]


def test_new_picture_with_edits_goes_into_a_textblock(tmp_path):
    new = photo(tmp_path / "deck" / "n.png", (400, 300), seed=2)
    target = deck_with([{"id": "i1", "kind": "image", "role": "figure", "bbox": [50, 60, 170, 150], "box": [60, 70, 160, 140],
                         "file": str(new), "rotation": 20.0, "crop": {"l": 0.25, "t": 0.0, "r": 0.0, "b": 0.0},
                         "alt": "Bench"}])
    p, ws, ctx = planner_for(tmp_path, deck_with([]), target)
    edits, failed = p.plan()
    assert failed == []
    ws.write(edits + ensure_preamble(ws, ctx))
    text = ws.source.text(ws.main)
    assert "\\begin{textblock*}{120.0pt}(50.0pt,60.0pt)" in text
    assert "trim=100.00 0.00 0.00 0.00,clip,width=100.0pt,height=70.0pt,angle=-20]{figures/bench-" in text
    assert "\\usepackage[absolute,overlay]{textpos}" in text


# ---------------------------------------------------------------- the compile loop (opt-in)

def pdflatex_missing() -> str | None:
    import shutil
    from beamer2slides.inverse import tex_env
    return None if shutil.which("pdflatex", path=tex_env()["PATH"]) else "pdflatex not found"


LOOP_TEX = r"""\documentclass[aspectratio=169]{beamer}
\usepackage{tikz}
\setbeamertemplate{navigation symbols}{}
\begin{document}
\begin{frame}[label=pics]{Pictures}
  \begin{columns}[c]
    \column{0.45\textwidth}
    \includegraphics[width=\linewidth]{figures/bench.png}
    \column{0.45\textwidth}
    \begin{tikzpicture}
      \draw[very thick, blue] plot[smooth, domain=0:4] (\x, {2.5*exp(-0.6*\x)});
      \draw[->] (0,0) -- (4.2,0);
      \draw[->] (0,0) -- (0,2.8);
    \end{tikzpicture}
  \end{columns}
\end{frame}
\end{document}
"""


@pytest.fixture(scope="module")
def loop_deck(tmp_path_factory):
    if reason := pdflatex_missing():
        pytest.skip(reason)
    root = tmp_path_factory.mktemp("pics")
    (root / "src").mkdir()
    (root / "src" / "talk.tex").write_text(LOOP_TEX, encoding="utf-8")
    photo(root / "src" / "figures" / "bench.png", (900, 600), seed=11)
    photo(root / "deck" / "new.png", (800, 500), seed=12)
    Image.open(root / "src" / "figures" / "bench.png").resize((450, 300)).save(root / "deck" / "bench-down.png")
    work = root / "base"
    built = Workspace(root / "src" / "talk.tex", work).build(work / "classify")
    assert not isinstance(built, str), built
    return root, built.deck


def record(name: str, res, seconds: float) -> None:
    out = TESTS / "decks" / "inverse" / "out" / "results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    data[name] = {"converged": res.converged, "rounds": len(res.iterations) - 1, "seconds": round(seconds, 1),
                  "trend": [it["open"] for it in res.iterations], "unresolved": [f"{u['kind']}: {u.get('why')}" for u in res.unresolved],
                  "pictures": res.notes}
    out.write_text(json.dumps(data, indent=1), encoding="utf-8")


def figure(deck):
    return [e for e in deck["slides"][0]["elements"] if e["kind"] == "image" and e.get("role") == "figure"]


def picture_targets(root: Path, deck: dict) -> dict:
    photo_el = min(figure(deck), key=lambda e: e["bbox"][0])
    plot_el = max(figure(deck), key=lambda e: e["bbox"][0])
    out = {}
    free = max(e["bbox"][3] for e in deck["slides"][0]["elements"]) + 4  # room under the content
    t = copy.deepcopy(deck)
    new = {"id": "new0", "kind": "image", "role": "figure", "file": str(root / "deck" / "new.png"), "alt": "Crop test",
           "crop": {"l": 0.2, "t": 0.1, "r": 0.1, "b": 0.3}}
    h = 250 - free
    w = h * (0.7 * 800) / (0.6 * 500)
    t["slides"][0]["elements"].append({**new, "bbox": [300, free, 300 + w, free + h], "box": [300, free, 300 + w, free + h]})
    out["cropped"] = t
    t = copy.deepcopy(deck)
    th = math.radians(25)
    h = 0.9 * (250 - free) / (math.sin(th) / 0.625 + math.cos(th))
    w = h / 0.625
    cx, cy = 300.0, free + (250 - free) / 2
    bw, bh = w * math.cos(th) + h * math.sin(th), w * math.sin(th) + h * math.cos(th)
    t["slides"][0]["elements"].append({"id": "new1", "kind": "image", "role": "figure", "file": str(root / "deck" / "new.png"),
                                       "bbox": [cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2],
                                       "box": [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], "rotation": 25.0})
    out["rotated"] = t
    t = copy.deepcopy(deck)
    el = next(e for e in t["slides"][0]["elements"] if e["id"] == plot_el["id"])
    el.update(file=str(root / "deck" / "new.png"), box=list(el["bbox"]), alt="Replacement")
    out["figure_replaced"] = t
    t = copy.deepcopy(deck)
    el = next(e for e in t["slides"][0]["elements"] if e["id"] == photo_el["id"])
    el.update(file=str(root / "deck" / "bench-down.png"), opacity=0.5, box=list(el["bbox"]))
    out["transparent"] = t
    return out


@pytest.mark.inverse
@pytest.mark.parametrize("name,rounds", [("cropped", 2), ("rotated", 2), ("figure_replaced", 2), ("transparent", 1)])
def test_converge_pictures(name, rounds, loop_deck, tmp_path):
    from beamer2slides.inverse import converge
    root, deck = loop_deck
    target = picture_targets(root, deck)[name]
    t = time.time()
    res = converge(root / "src" / "talk.tex", target, tmp_path / "loop", max_iter=8)
    record(f"pictures:{name}", res, time.time() - t)
    assert res.converged, res.unresolved
    assert len(res.iterations) - 1 <= rounds
    text = next(iter(v for k, v in res.files.items() if k.endswith("talk.tex")))
    if name == "cropped":
        assert "trim=160.00 150.00 80.00 50.00,clip" in text
    if name == "rotated":
        assert "angle=-25" in text
    if name == "figure_replaced":
        assert "% b2s pull: replaced by figures/replacement-" in text and "% \\begin{tikzpicture}" in text
    if name == "transparent":
        assert "text opacity=0.50" in text and "{figures/bench.png}" in text
