"""What a Google Docs round trip spends its time on, call by call.

    python -m beamer2slides.devtools.docs_bench prepare [--blocks N]
    python -m beamer2slides.devtools.docs_bench sync [--rounds N]
    python -m beamer2slides.devtools.docs_bench adopt
    python -m beamer2slides.devtools.docs_bench calls          # the last run's calls

The loop this measures is the one an agent pays for: a document becomes canonical
HTML, something edits the HTML, `docs sync` merges it back. The Slides side's lesson
(CLAUDE.md, "A conversion is round trips, not work") is the reason this exists at all
- a round trip costs about a second whatever it carries, so what a phase *does* says
much less about the clock than how many calls it waits for, one after another.

Every Google call is timed by wrapping `HttpRequest.execute` (the Slides side's own
instrument), so the table says which call, on which thread, for how long, and how
much of the wall clock had *some* request open. A phase that is 95% "a request is
open" has nothing left to overlap; one at 60% has a second of waiting nobody needs.

It reuses one document in a fixed folder (`out/docs-bench/`), so a hundred runs leave
one file in Drive rather than a hundred. `--fresh` makes a new one.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .. import doc_sync
from ..paths import out_root

FOLDER = out_root() / "docs-bench"
PAPER = "paper.html"
CALLS = "calls.json"

WORDS = ("signal thicket meadow vellum harbour lantern quarry ribbon cobble "
         "tessera furrow galley pennant thimble wicket").split()


# ---------------------------------------------------------------- the fixture

def paper(blocks: int = 40, seed: int = 0) -> str:
    """A canonical file of about `blocks` blocks: headings, prose, a list, a table.

    Shaped like something a person would keep in git - long enough that a read and a
    write carry real bytes, small enough that a round is a minute and not ten.
    """
    import random

    rng = random.Random(seed)
    out = ['<!DOCTYPE html>', '<html>', '<head>', '<meta charset="utf-8">',
           '<title>The quarterly handbook</title>', '</head>', '<body>']
    for n in range(blocks):
        words = " ".join(rng.choice(WORDS) for _ in range(rng.randint(12, 28)))
        if n % 8 == 0:
            out.append(f'<h1 id="heading:section-{n}">Section {n}: {rng.choice(WORDS)}</h1>')
        elif n % 8 == 5:
            out.append('<ul>')
            for k in range(3):
                out.append(f' <li id="item:{n}-{k}">the {k} point, {rng.choice(WORDS)}</li>')
            out.append('</ul>')
        elif n % 8 == 6:
            rows = "".join(
                "<tr>" + "".join(f"<td><p>{rng.choice(WORDS)}</p></td>" for _ in range(3)) + "</tr>"
                for _ in range(3))
            out.append(f'<table id="table:grid-{n}">{rows}</table>')
        else:
            out.append(f'<p id="paragraph:p-{n}">{words}</p>')
    out += ['</body>', '</html>', '']
    return "\n".join(out)


def reword(path: Path, round_no: int) -> int:
    """Edit the file the way a person or an agent would: reword a few paragraphs.

    Returns how many blocks it changed. The wording depends on the round, so no two
    syncs in a row are handed the same edit and none of them is a no-op.
    """
    text = path.read_text(encoding="utf-8")
    hits = 0

    def edit(match: re.Match) -> str:
        nonlocal hits
        if hits >= 3:
            return match.group(0)
        hits += 1
        word = WORDS[(round_no * 3 + hits) % len(WORDS)]
        return f"{match.group(1)}round {round_no} {word}, {match.group(2)}"

    text = re.sub(r'(<p id="paragraph:p-\d+">)(\w)', edit, text)
    path.write_text(text, encoding="utf-8")
    return hits


# ---------------------------------------------------------------- the instrument

class Calls:
    """Every Google call of a run: when it started, how long it took, what it was."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.lock = threading.Lock()
        self.t0 = time.perf_counter()

    def add(self, name: str, start: float, end: float) -> None:
        with self.lock:
            self.rows.append({"call": name, "at": round(start - self.t0, 3),
                              "seconds": round(end - start, 3),
                              "thread": threading.current_thread().name})

    # -- what the rows say

    def busy(self) -> float:
        """Seconds of wall clock with at least one request open (overlaps counted once)."""
        spans = sorted((r["at"], r["at"] + r["seconds"]) for r in self.rows)
        total, end = 0.0, -1.0
        for a, b in spans:
            a = max(a, end)
            if b > a:
                total += b - a
                end = b
        return total

    def by_call(self) -> list[tuple[str, int, float]]:
        out: dict[str, list[float]] = {}
        for row in self.rows:
            out.setdefault(row["call"], []).append(row["seconds"])
        return sorted(((k, len(v), sum(v)) for k, v in out.items()),
                      key=lambda r: -r[2])


def name_of(uri: str, method: str) -> str:
    """A call's short name: the API, the collection and the verb."""
    path = re.sub(r"\?.*$", "", uri).replace("https://", "")
    path = re.sub(r"/v\d+/", "/", path, count=1)
    # Ids are noise; the shape of the call is not.
    path = re.sub(r"/[A-Za-z0-9_-]{20,}", "/ID", path)
    path = path.replace("docs.googleapis.com/", "docs.").replace(
        "www.googleapis.com/drive/", "drive.").replace(
        "docs.googleapis.com", "docs").replace("www.googleapis.com/", "")
    return f"{method} {path}"


@contextmanager
def profile():
    """Time every Google call made inside the block (`HttpRequest.execute`)."""
    from googleapiclient.http import HttpRequest

    calls = Calls()
    original = HttpRequest.execute

    def timed(self, *args, **kwargs):
        start = time.perf_counter()
        try:
            return original(self, *args, **kwargs)
        finally:
            calls.add(name_of(self.uri, self.method), start, time.perf_counter())

    HttpRequest.execute = timed
    try:
        yield calls
    finally:
        HttpRequest.execute = original


def report(label: str, calls: Calls, seconds: float, extra: dict | None = None) -> dict:
    """Print the table and give back what a caller would put in a JSON line."""
    busy = calls.busy()
    print(f"\n{label}: {seconds:.2f} s, {len(calls.rows)} Google calls, "
          f"{busy:.2f} s with a request open ({busy / seconds:.0%})")
    for name, count, total in calls.by_call():
        print(f"   {total:6.2f} s  x{count:<3} {name}")
    if extra:
        print("   " + "  ".join(f"{k}={v}" for k, v in extra.items()))
    return {"label": label, "seconds": round(seconds, 2), "calls": len(calls.rows),
            "busy": round(busy, 2), "rows": calls.rows} | (extra or {})


# ---------------------------------------------------------------- the commands

def prepare(folder: Path, blocks: int, fresh: bool) -> dict:
    """The document this bench measures against, made once and reused."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / PAPER
    if path.exists() and not fresh:
        ours = doc_sync.read_file(path)
        if ours.get("document"):
            print(f"{path} already names document {ours['document']}")
            return {"document": ours["document"], "file": str(path)}
    path.write_text(paper(blocks), encoding="utf-8")
    start = time.perf_counter()
    with profile() as calls:
        info = doc_sync.push(path)
    report("push", calls, time.perf_counter() - start, {"blocks": info["blocks"]})
    return info


def one_sync(path: Path, round_no: int) -> dict:
    changed = reword(path, round_no)
    start = time.perf_counter()
    with profile() as calls:
        info = doc_sync.sync(path)
    return report(f"sync {round_no}", calls, time.perf_counter() - start,
                  {"requests": info["requests"], "edited": changed})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["prepare", "sync", "adopt", "calls"])
    ap.add_argument("--folder", type=Path, default=FOLDER)
    ap.add_argument("--blocks", type=int, default=40)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--fresh", action="store_true", help="a new document, not the one reused")
    ap.add_argument("--json", type=Path, help="write the measurement there as a JSON line")
    args = ap.parse_args(argv)

    folder: Path = args.folder
    if args.what == "calls":
        print((folder / CALLS).read_text(encoding="utf-8"))
        return 0

    runs = []
    if args.what == "prepare":
        prepare(folder, args.blocks, args.fresh)
        return 0
    if args.what == "adopt":
        info = prepare(folder, args.blocks, args.fresh)
        target = folder / "adopted.html"
        start = time.perf_counter()
        with profile() as calls:
            got = doc_sync.adopt(info["document"], target, force=True)
        runs.append(report("adopt", calls, time.perf_counter() - start,
                           {"blocks": got["blocks"]}))
    else:
        info = prepare(folder, args.blocks, args.fresh)
        path = Path(info.get("file") or folder / PAPER)
        for n in range(args.rounds):
            runs.append(one_sync(path, n + 1))

    (folder / CALLS).write_text(json.dumps(runs, indent=1), encoding="utf-8")
    if args.json:
        with args.json.open("a", encoding="utf-8") as fh:
            for run in runs:
                fh.write(json.dumps({k: v for k, v in run.items() if k != "rows"}) + "\n")
    print(f"\nwrote {folder / CALLS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
