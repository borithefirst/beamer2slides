"""Probe: what does a beamer *view* of a deck cost, with no compile and no durable folder?

`adopt.bootstrap` writes a whole source tree out of the deck's IR alone - no LaTeX, no
subprocess, no Google beyond the read that made the IR - so a deck can be *read* as beamer for
the price of writing some text. docs/direct-editing.md is the design this measures for; the
three questions it asks are:

  cost      how long the bootstrap takes, and how many files it makes
  weight    how much of that tree is the text somebody would edit, against the pictures and
            fonts that are re-fetchable from the deck and from google/fonts and therefore do
            not have to travel with it
  grain     how big one frame is, which is what decides whether a model can be handed a slide
            rather than a document

Read-only: it runs off the adopt corpus's cached `target.json` files (`$B2S_ADOPT_CORPUS`, or
`out/adopt-corpus`), makes its tree in a temporary directory and deletes it again.

    python tools/probe_deck_view.py [deck ...]
"""
from __future__ import annotations

import gzip
import json
import re
import sys
import tempfile
import time
from pathlib import Path

from beamer2slides import adopt
from beamer2slides.devtools import adopt_bench

TEXT = {".tex", ".sty", ".cls", ".bib", ".txt"}
DEFAULT = ("gdg24", "creandum-board", "intro-lecture", "hebrew-lesson", "ds-lecture")


def one(name: str) -> dict | None:
    """Bootstrap one corpus deck in a temporary directory and measure what came out."""
    folder = adopt_bench.CORPUS / name
    if not (folder / "target.json").exists():
        return None
    with tempfile.TemporaryDirectory(prefix="b2s-view-") as tmp:
        target = json.loads((folder / "target.json").read_text(encoding="utf-8"))
        tex = Path(tmp) / "tree" / "main.tex"
        t0 = time.perf_counter()
        adopt.bootstrap(target, tex, False)
        secs = time.perf_counter() - t0
        files = [p for p in sorted(tex.parent.rglob("*")) if p.is_file()]
        text = [p for p in files if p.suffix in TEXT]
        blob = b"".join(p.read_bytes() for p in text)
        main = tex.read_text(encoding="utf-8", errors="replace")
        frames = re.findall(r"\\begin\{frame\}.*?\\end\{frame\}", main, re.S)
        sizes = sorted(len(f) for f in frames) or [0]
        return {
            "deck": name, "slides": len(target.get("slides") or []), "secs": secs,
            "files": len(files), "text_kb": len(blob) / 1024,
            "gzip_kb": len(gzip.compress(blob, 9)) / 1024,
            "bin_mb": sum(p.stat().st_size for p in files if p.suffix not in TEXT) / 1024 / 1024,
            "frames": len(frames), "median": sizes[len(sizes) // 2],
        }


def main(decks: tuple[str, ...]) -> int:
    print(f"{'deck':16} {'slides':>6} {'boot s':>7} {'files':>5} {'textKB':>7} {'gzipKB':>7} "
          f"{'binMB':>6} {'frames':>6} {'medianF':>8}")
    rows = []
    for name in decks:
        row = one(name)
        if row is None:
            print(f"{name:16} (no cached target - run `adopt_bench capture {name}` first)")
            continue
        rows.append(row)
        print(f"{row['deck']:16} {row['slides']:6} {row['secs']:7.1f} {row['files']:5} "
              f"{row['text_kb']:7.0f} {row['gzip_kb']:7.0f} {row['bin_mb']:6.1f} "
              f"{row['frames']:6} {row['median']:8}")
    if not rows:
        return 1
    print(f"\n{len(rows)} decks: the text is {sum(r['gzip_kb'] for r in rows) / len(rows):.0f} kB "
          f"gzipped on average against {sum(r['bin_mb'] for r in rows) / len(rows):.1f} MB of "
          f"pictures and fonts; bootstrap {min(r['secs'] for r in rows):.1f}-"
          f"{max(r['secs'] for r in rows):.1f} s, no LaTeX")
    return 0


if __name__ == "__main__":
    sys.exit(main(tuple(sys.argv[1:]) or DEFAULT))
