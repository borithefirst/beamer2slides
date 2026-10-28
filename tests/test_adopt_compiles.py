"""What `adopt` writes compiles on the TeX installed here, for every deck we own.

The six showcase decks (docs/showcase.md, `tools/showcase.py fixture`) are read as they came back
from Slides - tables, right-to-left and CJK words, pictures, preset and freeform shapes, a master and
layouts - bootstrapped and compiled with the engine the source asks for. A macro the local LaTeX
lacks stops here rather than on a person's machine: TeX Live 2022's l3kernel had no `\\tl_set:Ne`,
and every `slidetable` stopped on it (reported with v0.7.0). CI runs this file on TeX Live 2022 too
(.github/workflows/texlive.yml).

The fonts are none by default (`$B2S_FONTS` names an empty folder: TeX Gyre and the scripts'
stand-ins, the same on every machine); `$B2S_TEST_FETCH_FONTS=1` lets adopt fetch the decks' own
Google Fonts instead, which takes the fontspec paths a deck's real faces take.
"""

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from beamer2slides import adopt
from beamer2slides.inverse import tex_env
from beamer2slides.json_types import Json, JsonObject

from .json_reads import jarr, jobj

SHOWCASE = Path(__file__).parent / "decks" / "foreign" / "showcase"
NAMES = ["bees", "hashing", "portfolio", "review", "talk", "water"]


def engine(text: str) -> str:
    """The engine a source asks for, as `texmap.Tree.engine` reads it."""
    m = re.search(r"%\s*!\s*(?:TEX\s+program|engine)\s*=\s*(\w+)", text[:400], re.I)
    if m:
        return m.group(1).lower()
    return "lualatex" if re.search(r"\\usepackage(\[[^\]]*\])?\{(fontspec|unicode-math)\}", text) else "pdflatex"


def target_of(name: str) -> JsonObject:
    """The fixture's target with its pictures' paths made whole again."""
    folder = SHOWCASE / name
    target = jobj(json.loads((folder / "target.json").read_text(encoding="utf-8")))

    def whole(node: Json) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("file", "background_file") and isinstance(v, str):
                    node[k] = str(folder / v)
                else:
                    whole(v)
        elif isinstance(node, list):
            for v in node:
                whole(v)
    whole(target)
    return target


@pytest.fixture
def fonts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    if os.environ.get("B2S_TEST_FETCH_FONTS"):
        monkeypatch.delenv("B2S_FONTS", raising=False)
        monkeypatch.setenv("B2S_FONT_FETCH", "1")
    else:
        monkeypatch.setenv("B2S_FONTS", str(tmp_path / "no-fonts-here"))
    adopt._FAMILIES.clear()
    yield
    adopt._FAMILIES.clear()


@pytest.mark.needs_decks(*(f"foreign/showcase/{n}/target.json" for n in NAMES))
@pytest.mark.parametrize("name", NAMES)
def test_the_source_adopt_writes_compiles(name: str, tmp_path: Path, fonts: None) -> None:
    target = target_of(name)
    main = tmp_path / "tree" / "main.tex"
    text = adopt.bootstrap(target, main, False, None)
    program = shutil.which(engine(text), path=tex_env()["PATH"])
    if program is None:
        # CI says it has TeX (`$B2S_REQUIRE_TEX`): a job that skipped every compile would pass
        (pytest.fail if os.environ.get("B2S_REQUIRE_TEX") else pytest.skip)(f"{engine(text)} not found")
    r = subprocess.run([program, "-interaction=nonstopmode", "-halt-on-error", main.name], cwd=main.parent,
                       capture_output=True, text=True, errors="replace", env=tex_env(), timeout=900)
    log = main.with_suffix(".log")
    tail = log.read_text(encoding="utf-8", errors="replace")[-4000:] if log.exists() else r.stdout[-4000:]
    assert r.returncode == 0, tail
    from beamer2slides import pdf
    doc = pdf.Document(main.with_suffix(".pdf"))
    try:
        assert len(doc) == len(jarr(target, "slides")), "a page per slide, and no notes pages"
    finally:
        doc.close()


def probe_frame(n: int) -> str:
    return f"\n\\begin{{frame}}[plain]PROBE{n:03d} bare words\\end{{frame}}\n"


@pytest.mark.needs_decks(*(f"foreign/showcase/{n}/target.json" for n in NAMES))
@pytest.mark.parametrize("name", NAMES)
def test_no_frame_leaks_its_colours_into_the_next(name: str, tmp_path: Path, fonts: None) -> None:
    """A frame of bare words after every frame of the source adopt writes: each has the colour of
    one before them all. A colour pushed in a box and popped after it - a `\\color` in an
    ungrouped `\\setbox...\\vbox` - coloured every later frame's own words (a glossary table's
    DarkBlue), and a frame compiled apart (`pieces.py`) starts from no such leak, so it differed."""
    from beamer2slides import pdf
    text = adopt.bootstrap(target_of(name), tmp_path / "tree" / "main.tex", False, None)
    program = shutil.which(engine(text), path=tex_env()["PATH"])
    if program is None:
        (pytest.fail if os.environ.get("B2S_REQUIRE_TEX") else pytest.skip)(f"{engine(text)} not found")
    head, body = text.split("\\begin{document}", 1)
    ends = iter(range(1, 1000))
    body = re.sub(r"\\end\{frame\}", lambda m: m.group(0) + probe_frame(next(ends)), body)
    main = tmp_path / "tree" / "probed.tex"
    main.write_text(head + "\\begin{document}" + probe_frame(0) + body, encoding="utf-8")
    r = subprocess.run([program, "-interaction=nonstopmode", "-halt-on-error", main.name], cwd=main.parent,
                       capture_output=True, text=True, errors="replace", env=tex_env(), timeout=900)
    assert r.returncode == 0, r.stdout[-4000:]
    doc = pdf.Document(main.with_suffix(".pdf"))
    try:
        colours = {}
        for i in range(len(doc)):
            chars = doc[i].chars()
            words = "".join(c.c for c in chars)
            at = words.find("PROBE")
            if at >= 0:
                colours[words[at:at + 8]] = chars[at].color
    finally:
        doc.close()
    assert len(colours) == len(jarr(target_of(name), "slides")) + 1
    first = colours["PROBE000"]
    assert {k: f"{v:06X}" for k, v in colours.items() if v != first} == {}, f"the first is {first:06X}"


# Probes for what slides.sty draws without TikZ (`adopt_shapes.PLAIN_MACRO`): each says on the
# terminal that it ran, and draws exactly what it drew.
FAST_PATHS = {"rect": "slides@plainrect", "fill": "slides@t@qfill", "rule": "slides@t@qrule", "cell": "slides@t@qnode"}
PROBES = "\\makeatletter\n" + "".join(
    f"\\let\\slidesprobe@{k}\\{m}\\def\\{m}{{\\typeout{{B2S-FAST {k}}}\\slidesprobe@{k}}}\n" for k, m in FAST_PATHS.items()) + \
    "\\makeatother\n"
# `every path` changed: nothing may then be drawn but by TikZ itself
TIKZ_ONLY = "\\tikzset{every path/.append style={}}\n"


def pages_of(pdf_path: Path) -> list[bytes]:
    from beamer2slides import pdf
    doc = pdf.Document(pdf_path)
    try:
        return [page.render(2.0, clip=None, transparent=False).tobytes() for page in doc]
    finally:
        doc.close()


def test_what_slides_sty_draws_without_tikz_is_what_tikz_draws(tmp_path: Path, fonts: None) -> None:
    """slides.sty draws a plain rectangle and a table's fills, rules and cells with pgf's system
    layer, the commands TikZ itself ends in (docs/compile-latency.md): the pages must be TikZ's,
    pixel for pixel. The same tree is compiled with probes saying the fast ones ran, and with
    `every path` changed, which sends every one of them to TikZ: a named look, a fill with an
    outline, an outline alone, a turned rectangle (TikZ's either way), and a table with merged,
    filled and middle-aligned cells and solid and dashed rules."""
    from beamer2slides.inverse import Workspace
    from .irs import deck_ir
    from .test_adopt import presentation, shape
    from .test_adopt_tables import table_of
    if shutil.which("lualatex", path=tex_env()["PATH"]) is None:
        (pytest.fail if os.environ.get("B2S_REQUIRE_TEX") else pytest.skip)("lualatex not found")
    pres = presentation()
    turned = shape("r5", "RECTANGLE", 300, 300, 50, 30, fill="00AA00", outline=None)
    turned["transform"] = {"scaleX": 0.9659, "shearX": -0.2588, "shearY": 0.2588, "scaleY": 0.9659,
                           "translateX": 300 * 12700, "translateY": 300 * 12700, "unit": "EMU"}
    elements: list[Json] = [shape(f"r{i}", "RECTANGLE", 20, 300 + 25 * i, 60, 20, fill="123456", outline=None)
                            for i in range(3)]
    elements += [shape("r3", "RECTANGLE", 120, 300, 80.3, 30.7, fill="FFCC00", outline="0000FF"),
                 shape("r4", "RECTANGLE", 220, 300, 50, 40, fill=None, outline="EA4335"),
                 turned, table_of("g1a2b3c_0_7", (100, 60, 60), (30, 30, 30))]
    jarr(pres, "slides", 0, "pageElements").extend(elements)
    main = tmp_path / "tree" / "main.tex"
    text = adopt.bootstrap(deck_ir(pres, foreign=True), main, False, None)
    assert "\\begin{slidetable}" in text and text.count("\\sliderect[") >= 6

    def compiled(name: str, preamble: str) -> tuple[list[bytes], str]:
        tree = tmp_path / name
        shutil.copytree(main.parent, tree)
        (tree / "main.tex").write_text(text.replace("\\begin{document}", preamble + "\\begin{document}", 1),
                                       encoding="utf-8")
        pdf, error = Workspace(tree / "main.tex", tmp_path / f"{name}-work", handout=False, engine="lualatex",
                               fresh=True).compile()
        assert pdf is not None, error
        return pages_of(pdf), pdf.with_suffix(".log").read_text(encoding="utf-8", errors="replace")

    fast, fast_log = compiled("fast", PROBES)
    tikz, tikz_log = compiled("tikz", PROBES + TIKZ_ONLY)
    for k in FAST_PATHS:
        assert f"B2S-FAST {k}" in fast_log, f"no {k} was drawn without TikZ"
    assert "B2S-FAST" not in tikz_log, "a changed `every path` sends everything to TikZ"
    assert len(fast) == len(tikz)
    moved = [i + 1 for i, (a, b) in enumerate(zip(fast, tikz)) if a != b]
    assert not moved, f"pages {moved} drawn without TikZ are not TikZ's pixels"
