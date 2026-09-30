"""How much slower the pure reader is than PDFium, per page, on the built test decks.

The two numbers CLAUDE.md records: extraction (the raw.json path - `extract.extract`, which asks
for objects, chars, drawings, images and links) and whole-page rendering (`page.render(zoom)`).
Both are measured warm (the file is read once, the caches are warm after the first repeat) and
reported as the **minimum** of the repeats, because the machine this runs on is usually busy with
something else and the minimum is the only robust statistic under an unrelated load. The clock is
`time.process_time` - this process's own CPU - not the wall clock: a second heavy job on the
machine moved a wall-clock total by 35% between two runs of the same code, which is more than any
optimisation here is worth. `--wall` asks for the wall clock instead.

    python -m beamer2slides.devtools.pure_bench                  # both, the default deck set
    python -m beamer2slides.devtools.pure_bench --what render --repeat 5
    python -m beamer2slides.devtools.pure_bench --decks 01_basic 04_theme_blocks --json out.json

`--baseline f.json` prints each deck's change against an earlier run's file, which is how an
optimisation is measured: the same decks, the same repeats, one number per deck plus the total.

**Measure A against B interleaved**, alternating the two versions of the file back to back for
several rounds and comparing the minima, never a run today against a run an hour ago: the same
unchanged code measured 2484 ms and 1922 ms on this machine within the hour, so even the process's
own CPU time drifts ~8% with the machine's state (frequency scaling, another job in the caches).
The ratio to PDFium drifts the same way and worse, because PDFium's time is C and does not suffer
the contention Python does, and because its total is only ~230 ms with a 15.6 ms clock granularity:
read the ratio to one significant digit, or from the quietest pass of a long run.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypedDict

from beamer2slides import pdf
from beamer2slides.json_types import Json
from beamer2slides.pdf.api import PdfError

# the decks test_pure_pdf.py renders, plus a few the sweep found slow (figures, tables, a long talk)
DEFAULT = ("04_theme_blocks", "23_raster_images", "01_basic", "14_misc", "12_metropolis_talk",
           "13_inline_math", "22_overlays_on_text", "03_figures", "16_colored_table",
           "11_research_talk", "26_truetype_fonts")
ZOOM = 1.37                                  # test_whole_beamer_pages_render_as_pdfium_renders_them
Clock = Callable[[], float]
"""`time.process_time`, or `time.perf_counter` under --wall."""


class Timing(TypedDict):
    best: float
    runs: list[float]


class Row(TypedDict, total=False):
    """A deck's row (`pages`, a `Timing` per backend timed, `ratio` when both were), or the
    total's (no `pages`)."""
    pages: int
    pure: Timing
    pdfium: Timing
    ratio: float


Rows = dict[str, Row]
Spec = Literal["pure", "pdfium"]


def decks_dir() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        out = parent / "tests" / "decks" / "out"
        if out.is_dir():
            return out
    raise SystemExit("no tests/decks/out (build the test decks)")


def time_extract(spec: str, path: Path, clock: Clock) -> tuple[float, int]:
    """Seconds for `extract` over the whole file, and how many pages that was.

    One clock reading for the whole file, never one per page: `process_time` on Windows moves in
    15.6 ms steps, so a sum of per-page differences is mostly quantisation noise."""
    from beamer2slides.extract import extract
    with pdf.use_backend(spec):
        t = clock()
        raw = extract(path, None)
        return clock() - t, len(raw["pages"])


def refused(path: Path) -> set[int]:
    """Pages the pure reader cannot draw: PDFium must not be charged for them either."""
    doc = pdf.resolve("pure").open(path)
    try:
        out: set[int] = set()
        for i in range(len(doc)):
            try:
                doc[i].render(ZOOM, clip=None, transparent=False)
            except PdfError:
                out.add(i)
        return out
    finally:
        doc.close()


def draw_pages(spec: str, path: Path, skip: set[int]) -> int:
    """Draw every page the pure reader does not refuse; the document is opened and closed around
    the drawing, which the caller times."""
    doc = pdf.resolve(spec).open(path)
    try:
        pages = [doc[i] for i in range(len(doc)) if i not in skip]
        for page in pages:
            page.render(ZOOM, clip=None, transparent=False)
        return len(pages)
    finally:
        doc.close()


def bench(what: str, decks: list[Path], repeat: int, only: Spec | None, clock: Clock) -> Rows:
    """One clock reading per pass over the *whole* deck set, `repeat` passes, the shortest kept.

    A pass is about two seconds, so the clock's 15.6 ms step is under 1%; a per-deck reading is a
    tenth of that and mostly quantisation. Per-deck seconds are the median of the same passes and
    are there to say where the time goes, not to be compared at a few percent."""
    specs: list[Spec] = [only] if only else ["pure", "pdfium"]
    skips = {p: refused(p) if what == "render" else set[int]() for p in decks}
    pages = {p.stem: 0 for p in decks}
    timed: dict[str, dict[Spec, Timing]] = {p.stem: {} for p in decks}
    totals: dict[Spec, Timing] = {}
    for spec in specs:
        passes: list[float] = []
        each: dict[str, list[float]] = {p.stem: [] for p in decks}
        for _ in range(repeat):
            t0 = clock()
            for p in decks:
                t = clock()
                if what == "extract":
                    with pdf.use_backend(spec):
                        from beamer2slides.extract import extract
                        pages[p.stem] = len(extract(p, None)["pages"])
                else:
                    pages[p.stem] = draw_pages(spec, p, skips[p])
                each[p.stem].append(clock() - t)
            passes.append(clock() - t0)
        for p in decks:
            timed[p.stem][spec] = {"best": statistics.median(each[p.stem]), "runs": each[p.stem]}
        totals[spec] = {"best": min(passes), "runs": passes}
    rows: Rows = {}
    for p in decks:
        row: Row = {"pages": pages[p.stem]}
        for spec in specs:
            row[spec] = timed[p.stem][spec]
        if len(specs) == 2:
            row["ratio"] = timed[p.stem]["pure"]["best"] / max(1e-9, timed[p.stem]["pdfium"]["best"])
        rows[p.stem] = row
    total: Row = {}
    for spec in specs:
        total[spec] = totals[spec]
    if len(specs) == 2:
        total["ratio"] = totals["pure"]["best"] / totals["pdfium"]["best"]
    rows["_total"] = total
    return rows


def report(what: str, rows: Rows, baseline: Json) -> None:
    head = f"{'deck':<22}{'pages':>6}{'pure ms/pg':>12}{'pdfium ms/pg':>14}{'ratio':>8}"
    if baseline:
        head += f"{'was':>10}{'change':>9}"
    print(f"\n{what}  (per deck the median pass, the total the shortest whole pass)")
    print(head)
    print("-" * len(head))
    for name, row in rows.items():
        total = name == "_total"
        n = 1 if total else max(1, row.get("pages", 0))
        pure_t, ref_t = row.get("pure"), row.get("pdfium")
        pure = pure_t["best"] if pure_t is not None else None
        ref = ref_t["best"] if ref_t is not None else None
        if total:
            print("-" * len(head))
        line = f"{('total' if total else name):<22}{('' if total else row.get('pages', 0)):>6}"
        line += f"{pure / n * 1000:>12.2f}" if pure is not None else f"{'-':>12}"
        line += f"{ref / n * 1000:>14.2f}" if ref is not None else f"{'-':>14}"
        ratio = row.get("ratio")
        line += f"{ratio:>8.2f}" if ratio is not None else f"{'-':>8}"
        if baseline:
            old = _best(baseline, name)
            if old is not None and pure is not None:
                line += f"{old / n * 1000:>10.2f}{(pure / old - 1) * 100:>8.1f}%"
            else:
                line += f"{'-':>10}{'-':>9}"
        print(line + ("   (ms, whole pass)" if total else ""))


def _best(baseline: Json, name: str) -> float | None:
    """The pure reader's best seconds for `name` in an earlier run's rows, if it has them."""
    row = baseline.get(name) if isinstance(baseline, dict) else None
    pure = row.get("pure") if isinstance(row, dict) else None
    best = pure.get("best") if isinstance(pure, dict) else None
    return float(best) if isinstance(best, (int, float)) else None


def main(argv: list[str] | None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--what", choices=("extract", "render", "both"), default="both")
    ap.add_argument("--decks", nargs="*", default=list(DEFAULT))
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--only", choices=("pure", "pdfium"), help="time one backend only")
    ap.add_argument("--json", type=Path, help="write the numbers here")
    ap.add_argument("--baseline", type=Path, help="an earlier --json file to compare against")
    ap.add_argument("--wall", action="store_true", help="wall clock instead of this process's CPU")
    args = ap.parse_args(argv)
    clock: Clock = time.perf_counter if args.wall else time.process_time

    out = decks_dir()
    decks = [out / f"{n}.pdf" for n in args.decks]
    missing = [p.name for p in decks if not p.exists()]
    if missing:
        raise SystemExit(f"no such decks in {out}: {', '.join(missing)}")

    base: Json = json.loads(args.baseline.read_text()) if args.baseline else {}
    result: dict[str, Rows] = {}
    for what in (("extract", "render") if args.what == "both" else (args.what,)):
        rows = bench(what, decks, args.repeat, args.only, clock)
        result[what] = rows
        report(what, rows, base.get(what) if isinstance(base, dict) else None)
    if args.json:
        args.json.write_text(json.dumps(result, indent=1))
        print(f"\n-> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main(None))
