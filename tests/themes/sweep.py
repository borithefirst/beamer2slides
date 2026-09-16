"""Compile content.tex with many beamer themes and classify each result.

Local only (no Google API): reports per theme how much text becomes native, how many
pictures/shapes are found, and any crash. Use it to spot classification weaknesses.

Usage: python tests/themes/sweep.py [--build] [theme ...]
"""

import argparse
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
sys.path.insert(0, str(HERE.parents[1] / "src"))

from beamer2slides.classify import classify  # noqa: E402
from beamer2slides.debug import render_debug  # noqa: E402
from beamer2slides.extract import extract  # noqa: E402

THEMES = [
    "default", "AnnArbor", "Antibes", "Bergen", "Berkeley", "Berlin", "Boadilla", "CambridgeUS",
    "Copenhagen", "Darmstadt", "Dresden", "Frankfurt", "Goettingen", "Hannover", "Ilmenau",
    "JuanLesPins", "Luebeck", "Madrid", "Malmoe", "Marburg", "Montpellier", "PaloAlto",
    "Pittsburgh", "Rochester", "Singapore", "Szeged", "Warsaw", "metropolis",
]


def pdflatex() -> str:
    found = shutil.which("pdflatex")
    if found:
        return found
    return str(Path(os.environ["LOCALAPPDATA"]) / "Programs/MiKTeX/miktex/bin/x64/pdflatex.exe")


def build(theme: str) -> Path:
    work = OUT / theme
    work.mkdir(parents=True, exist_ok=True)
    tex = work / "talk.tex"
    tex.write_text("\\documentclass[handout]{beamer}\n\\usetheme{" + theme + "}\n"
                   + (HERE / "content.tex").read_text(encoding="utf-8"), encoding="utf-8")
    for _ in range(2):
        r = subprocess.run([pdflatex(), "-interaction=nonstopmode", "-halt-on-error", "talk.tex"],
                           cwd=work, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            raise RuntimeError(r.stdout[-1500:])
    return work / "talk.pdf"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="(re)compile the PDFs")
    ap.add_argument("themes", nargs="*")
    args = ap.parse_args()
    themes = args.themes or THEMES
    print(f"{'theme':<13}{'slides':>7}{'native':>8}{'text':>6}{'pics':>6}{'shapes':>7}  background reasons")
    failures = 0
    for theme in themes:
        pdf = OUT / theme / "talk.pdf"
        try:
            if args.build or not pdf.exists():
                pdf = build(theme)
            deck = classify(extract(pdf))
            render_debug(pdf, deck, OUT / theme / "debug", zoom=2.0)
            kinds = [e["kind"] for s in deck["slides"] for e in s["elements"]]
            reasons: dict[str, int] = {}
            for s in deck["slides"]:
                for left in s["left_in_background"]:
                    reasons[left["reason"]] = reasons.get(left["reason"], 0) + len(left["spans"])
            print(f"{theme:<13}{len(deck['slides']):>7}{deck['stats']['native_share']:>8.0%}"
                  f"{kinds.count('text'):>6}{kinds.count('image'):>6}{kinds.count('shape'):>7}  "
                  + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
        except Exception as e:  # keep sweeping
            failures += 1
            print(f"{theme:<13} FAILED: {type(e).__name__}: {str(e)[-300:]}")
            traceback.print_exc(limit=3)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
