"""How much slower the pure reader is than PDFium, per page, on the built test decks.

The two numbers CLAUDE.md records: extraction (the raw.json path - `extract.extract`, which asks
for objects, chars, drawings, images and links) and whole-page rendering (`page.render(zoom)`).
Both are measured warm (the file is read once, the caches are warm after the first repeat) and
reported as the **minimum** of the repeats, because the machine this runs on is usually busy with
something else and the minimum is the only robust statistic under an unrelated load.

    python -m beamer2slides.devtools.pure_bench                  # both, the default deck set
    python -m beamer2slides.devtools.pure_bench --what render --repeat 5
    python -m beamer2slides.devtools.pure_bench --decks 01_basic 04_theme_blocks --json out.json

`--baseline f.json` prints each deck's change against an earlier run's file, which is how an
optimisation is measured: the same decks, the same repeats, one number per deck plus the total.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from beamer2slides import pdf
from beamer2slides.pdf.api import PdfError

# the decks test_pure_pdf.py renders, plus a few the sweep found slow (figures, tables, a long talk)
DEFAULT = ("04_theme_blocks", "23_raster_images", "01_basic", "14_misc", "12_metropolis_talk",
           "13_inline_math", "22_overlays_on_text", "03_figures", "16_colored_table",
           "11_research_talk", "26_truetype_fonts")
ZOOM = 1.37                                  # test_whole_beamer_pages_render_as_pdfium_renders_them


def decks_dir() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        out = parent / "tests" / "decks" / "out"
        if out.is_dir():
            return out
    raise SystemExit("no tests/decks/out (build the test decks)")


def time_extract(spec: str, path: Path) -> tuple[float, int]:
    """Seconds for `extract` over the whole file, and how many pages that was."""
    from beamer2slides.extract import extract
    with pdf.use_backend(spec):
        t = time.perf_counter()
        raw = extract(path)
        return time.perf_counter() - t, len(raw["pages"])


def time_render(spec: str, path: Path) -> tuple[float, int]:
    """Seconds spent inside `render` alone (opening the document is not counted), and the number
    of pages drawn: a page the pure reader refuses is drawn by neither."""
    doc = pdf.resolve(spec).open(path)
    try:
        total, drawn = 0.0, 0
        for i in range(len(doc)):
            page = doc[i]
            t = time.perf_counter()
            try:
                page.render(ZOOM)
            except PdfError:
                continue
            total += time.perf_counter() - t
            drawn += 1
        return total, drawn
    finally:
        doc.close()


def refused(path: Path) -> set[int]:
    """Pages the pure reader cannot draw: PDFium must not be charged for them either."""
    doc = pdf.resolve("pure").open(path)
    try:
        out = set()
        for i in range(len(doc)):
            try:
                doc[i].render(ZOOM)
            except PdfError:
                out.add(i)
        return out
    finally:
        doc.close()


def time_render_pages(spec: str, path: Path, skip: set[int]) -> tuple[float, int]:
    doc = pdf.resolve(spec).open(path)
    try:
        total, drawn = 0.0, 0
        for i in range(len(doc)):
            if i in skip:
                continue
            page = doc[i]
            t = time.perf_counter()
            page.render(ZOOM)
            total += time.perf_counter() - t
            drawn += 1
        return total, drawn
    finally:
        doc.close()


def bench(what: str, decks: list[Path], repeat: int, only: str | None) -> dict:
    out: dict[str, dict] = {}
    specs = [only] if only else ["pure", "pdfium"]
    for path in decks:
        row: dict[str, object] = {}
        skip = refused(path) if what == "render" else set()
        for spec in specs:
            runs, pages = [], 0
            for _ in range(repeat):
                if what == "extract":
                    t, pages = time_extract(spec, path)
                else:
                    t, pages = time_render_pages(spec, path, skip)
                runs.append(t)
            row[spec] = {"best": min(runs), "median": statistics.median(runs), "pages": pages}
        row["pages"] = row[specs[0]]["pages"]
        if len(specs) == 2:
            row["ratio"] = row["pure"]["best"] / row["pdfium"]["best"]
        out[path.stem] = row
    return out


def report(what: str, rows: dict, baseline: dict | None) -> None:
    head = f"{'deck':<22}{'pages':>6}{'pure ms/pg':>12}{'pdfium ms/pg':>14}{'ratio':>8}"
    if baseline:
        head += f"{'was':>10}{'change':>9}"
    print(f"\n{what}  (best of the repeats)")
    print(head)
    print("-" * len(head))
    tot_pure = tot_ref = tot_was = 0.0
    for name, row in rows.items():
        n = max(1, row["pages"])
        pure = row.get("pure", {}).get("best")
        ref = row.get("pdfium", {}).get("best")
        line = f"{name:<22}{row['pages']:>6}"
        line += f"{pure / n * 1000:>12.2f}" if pure is not None else f"{'-':>12}"
        line += f"{ref / n * 1000:>14.2f}" if ref is not None else f"{'-':>14}"
        line += f"{row['ratio']:>8.2f}" if "ratio" in row else f"{'-':>8}"
        if pure is not None:
            tot_pure += pure
        if ref is not None:
            tot_ref += ref
        if baseline:
            old = baseline.get(name, {}).get("pure", {}).get("best")
            if old is not None and pure is not None:
                tot_was += old
                line += f"{old / n * 1000:>10.2f}{(pure / old - 1) * 100:>8.1f}%"
            else:
                line += f"{'-':>10}{'-':>9}"
        print(line)
    print("-" * len(head))
    tail = f"{'total':<22}{'':>6}{tot_pure * 1000:>12.1f}{tot_ref * 1000:>14.1f}"
    tail += f"{tot_pure / tot_ref:>8.2f}" if tot_ref else f"{'-':>8}"
    if baseline and tot_was:
        tail += f"{tot_was * 1000:>10.1f}{(tot_pure / tot_was - 1) * 100:>8.1f}%"
    print(tail + "   (ms, whole run)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--what", choices=("extract", "render", "both"), default="both")
    ap.add_argument("--decks", nargs="*", default=list(DEFAULT))
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--only", choices=("pure", "pdfium"), help="time one backend only")
    ap.add_argument("--json", type=Path, help="write the numbers here")
    ap.add_argument("--baseline", type=Path, help="an earlier --json file to compare against")
    args = ap.parse_args(argv)

    out = decks_dir()
    decks = [out / f"{n}.pdf" for n in args.decks]
    missing = [p.name for p in decks if not p.exists()]
    if missing:
        raise SystemExit(f"no such decks in {out}: {', '.join(missing)}")

    base = json.loads(args.baseline.read_text()) if args.baseline else {}
    result = {}
    for what in (("extract", "render") if args.what == "both" else (args.what,)):
        rows = bench(what, decks, args.repeat, args.only)
        result[what] = rows
        report(what, rows, base.get(what))
    if args.json:
        args.json.write_text(json.dumps(result, indent=1))
        print(f"\n-> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
