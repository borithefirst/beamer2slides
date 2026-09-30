"""The adopt loop's first round over the corpus, in seconds rather than hours.

`adopt_bench run --iter N` compiles, reads back and edits every deck N times; on the 32 corpus decks
that is well over an hour, and most of what it found on 2026-09-26 was already wrong in round 0:
the read-back of a first draft Google's renderer scores 0.99 reported thousands of differences
(slides that did not pair, pictures that swallowed the boxes over them, notes pages read as
slides). This measures only that: bootstrap the source, compile it once (cached by the source's
bytes), read it back as the loop does (`Workspace.build`, with notes when the loop would) and
compare it with the deck (`compare`). A change to classify, compare or the read-back costs no TeX.

  run [DECK[:a-b] ...]    every corpus deck (or those named, a slide range after a colon)
      --micro             the micro-corpus: one slide per failure family (adopt_bench.MICRO)
      --save NAME         keep the result as out/adopt-replay/NAME.json
      --against NAME      and print each deck's change against a saved one
      --fresh             rebuild every target (kept under a hash of deck_ir's modules otherwise)

What is kept, per corpus deck: the target (`replay/targets/`, by the presentation and TARGET_MODULES)
and the compiled first draft (`compiled/`, by the source's bytes; a compile error too). A cold run
of the whole corpus took 4 minutes, a warm one the time of the read-back and the comparison.

Per deck: the first draft's ink score (`adopt_bench.score_page` "boxes", mean), the open
residuals of round 0, and **suspect** residuals: those on a slide whose ink score is at least
SUSPECT_INK. A slide that already looks like the deck has little left to fix, so what the loop
finds there is mostly the read-back's own blindness - and every one of them is an edit the loop
would write into a page that was right.
"""

import argparse
import hashlib
import json
import shutil
import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict

from beamer2slides.devtools.adopt_bench import CORPUS, decks, flag, load_target, micro_specs, number, score_pdf
from beamer2slides.json_types import JsonObject, as_array, as_int, as_object, as_objects, as_str
from beamer2slides.paths import CHECKOUT

SUSPECT_INK = 0.97
OUT = CHECKOUT / "out" / "adopt-replay"
KEEP = 4                      # compiled sources kept per deck


class SlideOpen(TypedDict):
    slide: int
    ink: float | None
    open: int


class _ReplayKeys(TypedDict):
    deck: str


class Replayed(_ReplayKeys, total=False):
    """One deck's round 0 as `replay` returns it and `--save` keeps it: a failure has `error` (and a
    crash its `traceback`), a deck read back the numbers from `pages` to `slides`."""
    n: int
    notes: bool
    cached: bool
    error: str
    pages: int
    ink: float
    open: int
    suspect: int
    kinds: dict[str, int]
    suspect_kinds: dict[str, int]
    slides: list[SlideOpen]
    traceback: str
    seconds: dict[str, float]


@dataclass(frozen=True, kw_only=True)
class Round0:
    """What the report reads of a deck that was read back (`round0`, `saved_round0`)."""
    deck: str
    n: int
    pages: int
    ink: float
    open: int
    suspect: int
    suspect_kinds: dict[str, int]
    cached: bool
    seconds: float


def round0(r: Replayed) -> Round0 | None:
    """The numbers of a deck read back; None for one that failed."""
    if r.get("error") or "n" not in r or "pages" not in r or "ink" not in r or "open" not in r \
            or "suspect" not in r or "suspect_kinds" not in r:
        return None
    return Round0(deck=r["deck"], n=r["n"], pages=r["pages"], ink=r["ink"], open=r["open"], suspect=r["suspect"],
                  suspect_kinds=r["suspect_kinds"], cached=r.get("cached", False),
                  seconds=sum(r.get("seconds", {}).values()))


def saved_round0(o: JsonObject) -> Round0 | None:
    """`round0` of a deck in a saved run (`--against`)."""
    if o.get("error"):
        return None
    where = f"the saved replay of {o.get('deck')}"
    return Round0(deck=as_str(o["deck"], where), n=as_int(o["n"], where), pages=as_int(o["pages"], where),
                  ink=number(o["ink"], where), open=as_int(o["open"], where), suspect=as_int(o["suspect"], where),
                  suspect_kinds={k: as_int(v, where) for k, v in as_object(o["suspect_kinds"], where).items()},
                  cached=flag(o.get("cached", False), where),
                  seconds=sum(number(v, where) for v in as_object(o.get("seconds", {}), where).values()))


class Compiling(Protocol):
    """A Workspace as far as `compiled` uses it."""
    build_dir: Path
    main: Path

    def compile(self) -> tuple[Path | None, str]: ...


def tree_hash(tree: Path, notes: bool) -> str:
    h = hashlib.sha256(b"notes" if notes else b"slides")
    for p in sorted(q for q in tree.rglob("*") if q.is_file()):
        h.update(p.relative_to(tree).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()[:24]


def compiled(ws: Compiling, cache: Path) -> tuple[Path | None, str, bool]:
    """(pdf, error, from the cache) for the workspace's source: the build folder of an earlier
    compile of the same bytes is copied back (the PDF, SyncTeX and aux files the read-back uses),
    and a source that did not compile fails again at once."""
    if (cache / "error.txt").exists():
        return None, (cache / "error.txt").read_text(encoding="utf-8"), True
    if (cache / "main.pdf").exists():
        for f in cache.iterdir():
            if f.is_file():
                shutil.copy2(f, ws.build_dir / f.name)
        return ws.build_dir / f"{ws.main.stem}.pdf", "", True
    pdf, err = ws.compile()
    tmp = cache.with_name(cache.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    if pdf is None:
        (tmp / "error.txt").write_text(err, encoding="utf-8")
    else:
        for f in ws.build_dir.iterdir():
            if f.is_file() and f.suffix in (".pdf", ".gz", ".aux", ".nav", ".snm", ".toc", ".out", ".log"):
                shutil.copy2(f, tmp / f.name)
    shutil.rmtree(cache, ignore_errors=True)
    tmp.rename(cache)
    prune(cache.parent)
    return pdf, err, False


def prune(folder: Path) -> None:
    old = sorted((p for p in folder.iterdir() if not p.name.endswith(".part")), key=lambda p: -p.stat().st_mtime)
    for p in old[KEEP:]:
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


# What `deck_ir` reads a presentation with. The target is kept per deck under a hash of these and the
# presentation, so a change to the read-back or the comparison costs no deck_ir; `--fresh` rebuilds
# it anyway, for a change elsewhere that reaches it.
TARGET_MODULES = ("deck_ir", "deck_fills", "deck_freeforms", "deck_thumbs", "emit", "emit_metrics", "emit_widths",
                  "emit_text", "emit_pptx", "emit_tables", "emit_diagrams", "emit_holes", "emit_places", "emit_theme",
                  "fonts", "fontfetch", "bidi", "scripts", "gslides", "identity", "snapshot", "labels", "paths")


def target_for(folder: Path, slides: str | None, fresh: bool) -> JsonObject:
    import beamer2slides
    root = Path(beamer2slides.__file__).parent
    h = hashlib.sha256((slides or "").encode())
    for name in ("presentation.json", "urls.json"):
        if (folder / name).exists():
            h.update(hashlib.sha256((folder / name).read_bytes()).digest())
    for mod in TARGET_MODULES:
        h.update(hashlib.sha256((root / f"{mod}.py").read_bytes()).digest())
    for f in sorted(root.rglob("*.json")):                  # calibration tables
        h.update(hashlib.sha256(f.read_bytes()).digest())
    path = folder / "replay" / "targets" / f"{h.hexdigest()[:24]}.json"
    if path.exists() and not fresh:
        return as_object(json.loads(path.read_text(encoding="utf-8")), str(path))
    target = load_target(folder, slides, None, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(target), encoding="utf-8")
    prune(path.parent)
    return target


def replay(spec: str, fresh: bool) -> Replayed:
    """Round 0 of the loop on one deck (or a slide range of it). Never raises."""
    from beamer2slides import adopt
    from beamer2slides.compare import TOL, SlideExtra, compare, target_slide_of
    from beamer2slides.inverse import Workspace, picture_hashes, typed_target, uses_notes
    from beamer2slides.texmap import Source
    name, _, slides = spec.partition(":")
    folder = CORPUS / name
    tag = slides.replace("-", "_") or "all"
    home = folder / "replay" / tag
    res: Replayed = {"deck": spec}
    times: dict[str, float] = {}
    t = time.perf_counter()
    try:
        target = target_for(folder, slides or None, fresh)
        pages = as_objects(target["slides"], "the target's slides")
        times["target"] = time.perf_counter() - t
        t = time.perf_counter()
        shutil.rmtree(home, ignore_errors=True)
        tex = home / "tree" / "main.tex"
        adopt.bootstrap(target, tex, False, None)
        notes = any(s.get("notes") for s in pages) or uses_notes(Source(tex))
        times["bootstrap"] = time.perf_counter() - t
        t = time.perf_counter()
        ws = Workspace(tex, home / "work", handout=False, engine=None, fresh=True)
        ws.notes = notes
        cache = folder / "compiled" / tree_hash(tex.parent, notes)
        pdf, err, cached = compiled(ws, cache)
        times["compile"] = time.perf_counter() - t
        n = res["n"] = len(pages)
        res["notes"] = notes
        res["cached"] = cached
        if pdf is None:
            res["error"] = "compile: " + err[-600:]
            return res
        t = time.perf_counter()
        cand = ws.build(home / "classify", notes, compiled=(pdf, ""))
        if isinstance(cand, str):
            res["error"] = "build: " + cand[-600:]
            return res
        times["read-back"] = time.perf_counter() - t
        t = time.perf_counter()
        view = folder
        if slides:                                     # thumbnails are numbered from the deck's first slide
            view = home / "refs"
            (view / "slides").mkdir(parents=True, exist_ok=True)
            first = as_int(target["first_slide"], "the target's first slide")
            for k in range(n):
                shutil.copyfile(folder / "slides" / f"{first + k + 1:03}.png",
                                view / "slides" / f"{k + 1:03}.png")
        # the score reads the target's boxes too: one file per target
        score_file = cache / f"ink-{hashlib.sha256(json.dumps(target, sort_keys=True, default=str).encode()).hexdigest()[:16]}.json"
        ink: list[float]
        if score_file.exists():
            ink = [number(v, str(score_file))
                   for v in as_array(json.loads(score_file.read_text(encoding="utf-8")), str(score_file))]
        else:
            # what a person sees is the slides alone: with notes, the ink is scored on a compile
            # without them (the loop's own PDF may still hold notes pages it failed to take out)
            shown = cand.pdf
            if notes:
                plain = Workspace(tex, home / "plain", handout=False, engine=None, fresh=True)
                shown, err, _ = compiled(plain, folder / "compiled" / tree_hash(tex.parent, False))
                if shown is None:
                    raise RuntimeError(f"compile without notes: {err[-300:]}")
            ink = [s["boxes"] for s in score_pdf(shown, view, target, None)]
            if cache.is_dir():
                score_file.write_text(json.dumps(ink), encoding="utf-8")
        times["ink"] = time.perf_counter() - t
        t = time.perf_counter()
        typed = typed_target(target)
        comp = compare(cand.current(), typed, TOL, picture_hashes(cand, typed, home / "work"))
        found = comp.open()
        times["compare"] = time.perf_counter() - t
        # a residual's slide: the target's, or for a page the target lacks, the page's own index
        per_slide = Counter(r.slide if isinstance(r, SlideExtra) else target_slide_of(r) for r in found)
        suspect = [r for r in found if (j := r.slide if isinstance(r, SlideExtra) else target_slide_of(r)) is not None
                   and j < len(ink) and ink[j] >= SUSPECT_INK]
        kinds: Counter[str] = Counter(r.kind for r in found)
        suspect_kinds: Counter[str] = Counter(r.kind for r in suspect)
        res["pages"] = len(cand.slides())
        res["ink"] = round(sum(ink) / max(1, len(ink)), 3)
        res["open"] = len(found)
        res["suspect"] = len(suspect)
        res["kinds"] = dict(kinds.most_common())
        res["suspect_kinds"] = dict(suspect_kinds.most_common())
        res["slides"] = [{"slide": j + 1, "ink": ink[j] if j < len(ink) else None, "open": per_slide.get(j, 0)}
                         for j in range(n)]
    except Exception as exc:                            # noqa: BLE001 - the crash is the finding
        res["error"] = f"{type(exc).__name__}: {exc}"[:600]
        res["traceback"] = traceback.format_exc()[-3000:]
    finally:
        res["seconds"] = {k: round(v, 1) for k, v in times.items()}
    return res


def line(r: Replayed, was: Round0 | None) -> str:
    """One deck's line; `was`: the same deck in the saved run compared against (None: none, or failed)."""
    error = r.get("error")
    got = round0(r)
    if error or got is None:
        return f"{r['deck']:<24} ERROR {(error or 'no result').splitlines()[0][:100]}"
    pages = "" if got.pages == got.n else f"  PAGES {got.pages} != {got.n} slides"
    delta = ""
    if was:
        delta = f"  (was open {was.open}, suspect {was.suspect})"
    top = ", ".join(f"{k} {v}" for k, v in list(got.suspect_kinds.items())[:4])
    return (f"{got.deck:<24} {got.n:3} sl  ink {got.ink:.3f}  open {got.open:5}  suspect {got.suspect:5}"
            f"{delta}  {got.seconds:5.1f}s{'' if not got.cached else ' (compiled before)'}{pages}  [{top}]")


def main(argv: list[str] | None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("specs", nargs="*")
    r.add_argument("--micro", action="store_true", help="the micro-corpus (adopt_bench.MICRO)")
    r.add_argument("--jobs", type=int, default=8)
    r.add_argument("--save")
    r.add_argument("--against")
    r.add_argument("--fresh", action="store_true", help="rebuild every target (deck_ir) whatever its key says")
    args = ap.parse_args(argv)
    specs: list[str] = args.specs + (micro_specs() if args.micro else []) or decks()
    # the saved run's decks, None for one that failed there
    before: dict[str, Round0 | None] = {}
    if args.against:
        saved = OUT / f"{args.against}.json"
        before = {as_str(x["deck"], str(saved)): saved_round0(x)
                  for x in as_objects(json.loads(saved.read_text(encoding="utf-8")), str(saved))}
    t0 = time.perf_counter()
    done: list[Replayed] = []
    with ProcessPoolExecutor(max(1, min(args.jobs, len(specs)))) as pool:
        for f in as_completed([pool.submit(replay, s, args.fresh) for s in specs]):
            done.append(f.result())
            print(line(done[-1], before.get(done[-1]["deck"])), flush=True)
    ok = [got for x in done if (got := round0(x))]
    n = sum(x.n for x in ok)
    print(f"\n{len(ok)}/{len(done)} decks, {n} slides in {time.perf_counter() - t0:.0f}s: "
          f"ink {sum(x.ink * x.n for x in ok) / max(1, n):.3f}, open {sum(x.open for x in ok)}, "
          f"suspect {sum(x.suspect for x in ok)} on slides inked >= {SUSPECT_INK}; "
          f"{sum(1 for x in ok if x.pages != x.n)} deck(s) read back with another page count")
    kinds: Counter[str] = Counter()
    for x in ok:
        kinds.update(x.suspect_kinds)
    print("suspect by kind: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common()))
    if before:
        common = [(x, was) for x in ok if (was := before.get(x.deck))]
        print(f"against {args.against}: open {sum(was.open for _, was in common)} -> "
              f"{sum(x.open for x, _ in common)}, suspect {sum(was.suspect for _, was in common)} -> "
              f"{sum(x.suspect for x, _ in common)} over {len(common)} decks")
    if args.save:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{args.save}.json").write_text(json.dumps(sorted(done, key=lambda x: x["deck"]), indent=1),
                                              encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main(None))
