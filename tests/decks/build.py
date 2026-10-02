"""Compile the test decks in this folder into out/.

Every deck is built twice: normally (one PDF page per overlay) and in beamer
handout mode (one page per frame). The engine is pdflatex unless the first
line of the .tex file says `% !engine = <name>`.

A deck may ask for more builds in one of its first lines, `% !variants = notes notes-right ...`
(names from VARIANTS): each goes to out/notes/<deck>-<variant>.pdf. They are the ways beamer
shows speaker notes (docs/speaker-notes.md), compiled from the same source.

Usage: python tests/decks/build.py [deck-name ...]
"""

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
NOTES = OUT / "notes"
RERUN = re.compile(r"Rerun to get|rerun LaTeX|Label\(s\) may have changed|may have changed\. Rerun", re.I)

SHOW_NOTES = r"\PassOptionsToClass{notes=show}{beamer}"


@dataclass(frozen=True, kw_only=True)
class Build:
    """One way of compiling a deck: its job name's suffix, where it goes, the TeX read before the
    deck's own first line, and the engine when it is not the deck's own."""
    suffix: str
    folder: Path
    prefix: str
    engine: str | None


NORMAL = Build(suffix="", folder=OUT, prefix="", engine=None)
HANDOUT = Build(suffix="-handout", folder=OUT, prefix=r"\PassOptionsToClass{handout}{beamer}", engine=None)
VARIANTS = {
    "notes": Build(suffix="-notes", folder=NOTES, prefix=SHOW_NOTES, engine=None),
    "notes-right": Build(suffix="-notes-right", folder=NOTES,
                         prefix=r"\AtBeginDocument{\setbeameroption{show notes on second screen=right}}", engine=None),
    "notes-plain": Build(suffix="-notes-plain", folder=NOTES,
                         prefix=SHOW_NOTES + r"\AtBeginDocument{\setbeamertemplate{note page}[plain]}", engine=None),
    "notes-compressed": Build(suffix="-notes-compressed", folder=NOTES,
                              prefix=SHOW_NOTES + r"\AtBeginDocument{\setbeamertemplate{note page}[compressed]}",
                              engine=None),
    "notes-xelatex": Build(suffix="-notes-xelatex", folder=NOTES, prefix=SHOW_NOTES, engine="xelatex"),
}


def engine_for(tex: Path) -> str:
    first = tex.read_text(encoding="utf-8").splitlines()[0]
    m = re.match(r"%\s*!engine\s*=\s*(\w+)", first)
    return m.group(1) if m else "pdflatex"


def variants_of(tex: Path) -> list[Build]:
    """The builds a deck asks for beyond its normal and handout ones (`% !variants = a b`)."""
    for line in tex.read_text(encoding="utf-8").splitlines()[:5]:
        m = re.match(r"%\s*!variants\s*=\s*(.+)", line)
        if m:
            return [VARIANTS[name] for name in m.group(1).split()]
    return []


def compile_deck(tex: Path, build: Build) -> Path:
    jobname = tex.stem + build.suffix
    build.folder.mkdir(parents=True, exist_ok=True)
    cmd = [
        build.engine or engine_for(tex),
        "-interaction=nonstopmode",
        "-halt-on-error",
        f"-jobname={jobname}",
        f"-output-directory={build.folder}",
        build.prefix + r"\input{" + tex.name + "}",
    ]
    # At least two passes so frame numbers and navigation are resolved, then more while
    # the log asks for one: TeX Live's tikzmark needs a third before `remember picture`
    # drawings land on their marks (else deck 19's arrow stands at the end of its line).
    # The .aux settling is the test, as in latexmk: tikzmark does not always say so.
    log, aux = build.folder / f"{jobname}.log", build.folder / f"{jobname}.aux"
    before = None
    for n in range(5):
        result = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, errors="replace")
        text = log.read_text(errors="replace") if log.exists() else result.stdout
        if result.returncode != 0:
            raise RuntimeError(f"{tex.name} ({jobname}) failed:\n{text[-3000:]}")
        after = aux.read_bytes() if aux.exists() else b""
        if n >= 1 and after == before and not RERUN.search(text):
            break
        before = after
    return build.folder / f"{jobname}.pdf"


def main(names: list[str]) -> int:
    OUT.mkdir(exist_ok=True)
    decks = sorted(HERE.glob("*.tex"))
    if names:
        decks = [d for d in decks if d.stem in names]
    failed = 0
    for tex in decks:
        for build in (NORMAL, HANDOUT, *variants_of(tex)):
            engine = build.engine or engine_for(tex)
            if not shutil.which(engine):
                print(f"SKIP {tex.stem}{build.suffix}: {engine} not found")
                continue
            try:
                pdf = compile_deck(tex, build)
                print(f"OK   {pdf.name}")
            except RuntimeError as e:
                failed += 1
                print(f"FAIL {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
