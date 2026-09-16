"""Compile the test decks in this folder into out/.

Every deck is built twice: normally (one PDF page per overlay) and in beamer
handout mode (one page per frame). The engine is pdflatex unless the first
line of the .tex file says `% !engine = <name>`.

Usage: python tests/decks/build.py [deck-name ...]
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"


def engine_for(tex: Path) -> str:
    first = tex.read_text(encoding="utf-8").splitlines()[0]
    m = re.match(r"%\s*!engine\s*=\s*(\w+)", first)
    return m.group(1) if m else "pdflatex"


def compile_deck(tex: Path, handout: bool) -> Path:
    jobname = tex.stem + ("-handout" if handout else "")
    prefix = r"\PassOptionsToClass{handout}{beamer}" if handout else ""
    cmd = [
        engine_for(tex),
        "-interaction=nonstopmode",
        "-halt-on-error",
        f"-jobname={jobname}",
        f"-output-directory={OUT}",
        prefix + r"\input{" + tex.name + "}",
    ]
    # Two passes so frame numbers and navigation are resolved.
    for _ in range(2):
        result = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, errors="replace")
        if result.returncode != 0:
            log = OUT / f"{jobname}.log"
            tail = log.read_text(errors="replace")[-3000:] if log.exists() else result.stdout[-3000:]
            raise RuntimeError(f"{tex.name} ({'handout' if handout else 'normal'}) failed:\n{tail}")
    return OUT / f"{jobname}.pdf"


def main(names: list[str]) -> int:
    OUT.mkdir(exist_ok=True)
    decks = sorted(HERE.glob("*.tex"))
    if names:
        decks = [d for d in decks if d.stem in names]
    failed = 0
    for tex in decks:
        if not shutil.which(engine_for(tex)):
            print(f"SKIP {tex.name}: {engine_for(tex)} not found")
            continue
        for handout in (False, True):
            try:
                pdf = compile_deck(tex, handout)
                print(f"OK   {pdf.name}")
            except RuntimeError as e:
                failed += 1
                print(f"FAIL {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
