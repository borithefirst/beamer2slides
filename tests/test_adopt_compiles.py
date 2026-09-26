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
from pathlib import Path

import pytest

from beamer2slides import adopt
from beamer2slides.inverse import tex_env

SHOWCASE = Path(__file__).parent / "decks" / "foreign" / "showcase"
NAMES = ["bees", "hashing", "portfolio", "review", "talk", "water"]


def engine(text: str) -> str:
    """The engine a source asks for, as `texmap.Tree.engine` reads it."""
    m = re.search(r"%\s*!\s*(?:TEX\s+program|engine)\s*=\s*(\w+)", text[:400], re.I)
    if m:
        return m.group(1).lower()
    return "lualatex" if re.search(r"\\usepackage(\[[^\]]*\])?\{(fontspec|unicode-math)\}", text) else "pdflatex"


def target_of(name: str) -> dict:
    """The fixture's target with its pictures' paths made whole again."""
    folder = SHOWCASE / name
    target = json.loads((folder / "target.json").read_text(encoding="utf-8"))

    def whole(node):
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
def fonts(monkeypatch, tmp_path):
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
def test_the_source_adopt_writes_compiles(name, tmp_path, fonts):
    target = target_of(name)
    main = tmp_path / "tree" / "main.tex"
    text = adopt.bootstrap(target, main)
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
        assert len(doc) == len(target["slides"]), "a page per slide, and no notes pages"
    finally:
        doc.close()
