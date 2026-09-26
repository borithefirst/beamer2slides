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
from pathlib import Path

from beamer2slides.devtools.adopt_bench import CORPUS, decks, load_target, micro_specs, score_pdf
from beamer2slides.paths import CHECKOUT

SUSPECT_INK = 0.97
OUT = CHECKOUT / "out" / "adopt-replay"
KEEP = 4                      # compiled sources kept per deck


def tree_hash(tree: Path, notes: bool) -> str:
    h = hashlib.sha256(b"notes" if notes else b"slides")
    for p in sorted(q for q in tree.rglob("*") if q.is_file()):
        h.update(p.relative_to(tree).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()[:24]


def compiled(ws, cache: Path) -> tuple[Path | None, str, bool]:
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
TARGET_MODULES = ("deck_ir", "deck_fills", "deck_freeforms", "deck_thumbs", "emit", "fonts", "fontfetch",
                  "bidi", "scripts", "gslides", "identity", "snapshot", "labels", "paths")


def target_for(folder: Path, slides: str | None, fresh: bool) -> dict:
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
        return json.loads(path.read_text(encoding="utf-8"))
    target = load_target(folder, slides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(target), encoding="utf-8")
    prune(path.parent)
    return target


def replay(spec: str, fresh: bool = False) -> dict:
    """Round 0 of the loop on one deck (or a slide range of it). Never raises."""
    from beamer2slides import adopt
    from beamer2slides.compare import compare
    from beamer2slides.inverse import Workspace, picture_hashes, uses_notes
    from beamer2slides.texmap import Source
    name, _, slides = spec.partition(":")
    folder = CORPUS / name
    tag = slides.replace("-", "_") or "all"
    home = folder / "replay" / tag
    res: dict = {"deck": spec}
    times: dict = {}
    t = time.perf_counter()
    try:
        target = target_for(folder, slides or None, fresh)
        times["target"] = time.perf_counter() - t
        t = time.perf_counter()
        shutil.rmtree(home, ignore_errors=True)
        tex = home / "tree" / "main.tex"
        adopt.bootstrap(target, tex, False)
        notes = any(s.get("notes") for s in target["slides"]) or uses_notes(Source(tex))
        times["bootstrap"] = time.perf_counter() - t
        t = time.perf_counter()
        ws = Workspace(tex, home / "work")
        ws.notes = notes
        cache = folder / "compiled" / tree_hash(tex.parent, notes)
        pdf, err, cached = compiled(ws, cache)
        times["compile"] = time.perf_counter() - t
        res.update(n=len(target["slides"]), notes=notes, cached=cached)
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
            for k in range(res["n"]):
                shutil.copyfile(folder / "slides" / f"{target['first_slide'] + k + 1:03}.png",
                                view / "slides" / f"{k + 1:03}.png")
        # the score reads the target's boxes too: one file per target
        score_file = cache / f"ink-{hashlib.sha256(json.dumps(target, sort_keys=True, default=str).encode()).hexdigest()[:16]}.json"
        if score_file.exists():
            ink = json.loads(score_file.read_text(encoding="utf-8"))
        else:
            # what a person sees is the slides alone: with notes, the ink is scored on a compile
            # without them (the loop's own PDF may still hold notes pages it failed to take out)
            shown = cand.pdf
            if notes:
                plain = Workspace(tex, home / "plain")
                shown, err, _ = compiled(plain, folder / "compiled" / tree_hash(tex.parent, False))
                if shown is None:
                    raise RuntimeError(f"compile without notes: {err[-300:]}")
            ink = [s["boxes"] for s in score_pdf(shown, view, target, None)]
            if cache.is_dir():
                score_file.write_text(json.dumps(ink), encoding="utf-8")
        times["ink"] = time.perf_counter() - t
        t = time.perf_counter()
        comp = compare(cand.deck, target, None, picture_hashes(cand, target, home / "work"))
        found = comp.open()
        times["compare"] = time.perf_counter() - t
        per_slide = Counter(r.get("target_slide", r.get("slide")) for r in found)
        suspect = [r for r in found if (j := r.get("target_slide", r.get("slide"))) is not None
                   and j < len(ink) and ink[j] >= SUSPECT_INK]
        res.update(pages=len(cand.deck["slides"]), ink=round(sum(ink) / max(1, len(ink)), 3),
                   open=len(found), suspect=len(suspect),
                   kinds=dict(Counter(r["kind"] for r in found).most_common()),
                   suspect_kinds=dict(Counter(r["kind"] for r in suspect).most_common()),
                   slides=[{"slide": j + 1, "ink": ink[j] if j < len(ink) else None, "open": per_slide.get(j, 0)}
                           for j in range(res["n"])])
    except Exception as exc:                            # noqa: BLE001 - the crash is the finding
        res["error"] = f"{type(exc).__name__}: {exc}"[:600]
        res["traceback"] = traceback.format_exc()[-3000:]
    finally:
        res["seconds"] = {k: round(v, 1) for k, v in times.items()}
    return res


def line(r: dict, was: dict | None = None) -> str:
    if r.get("error"):
        return f"{r['deck']:<24} ERROR {r['error'].splitlines()[0][:100]}"
    pages = "" if r["pages"] == r["n"] else f"  PAGES {r['pages']} != {r['n']} slides"
    delta = ""
    if was and not was.get("error"):
        delta = f"  (was open {was['open']}, suspect {was['suspect']})"
    top = ", ".join(f"{k} {v}" for k, v in list(r["suspect_kinds"].items())[:4])
    secs = sum(r["seconds"].values())
    return (f"{r['deck']:<24} {r['n']:3} sl  ink {r['ink']:.3f}  open {r['open']:5}  suspect {r['suspect']:5}"
            f"{delta}  {secs:5.1f}s{'' if not r.get('cached') else ' (compiled before)'}{pages}  [{top}]")


def main(argv=None) -> None:
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
    specs = args.specs + (micro_specs() if args.micro else []) or decks()
    before = {}
    if args.against:
        before = {x["deck"]: x for x in json.loads((OUT / f"{args.against}.json").read_text(encoding="utf-8"))}
    t0 = time.perf_counter()
    done = []
    with ProcessPoolExecutor(max(1, min(args.jobs, len(specs)))) as pool:
        for f in as_completed([pool.submit(replay, s, args.fresh) for s in specs]):
            done.append(f.result())
            print(line(done[-1], before.get(done[-1]["deck"])), flush=True)
    ok = [x for x in done if not x.get("error")]
    n = sum(x["n"] for x in ok)
    print(f"\n{len(ok)}/{len(done)} decks, {n} slides in {time.perf_counter() - t0:.0f}s: "
          f"ink {sum(x['ink'] * x['n'] for x in ok) / max(1, n):.3f}, open {sum(x['open'] for x in ok)}, "
          f"suspect {sum(x['suspect'] for x in ok)} on slides inked >= {SUSPECT_INK}; "
          f"{sum(1 for x in ok if x['pages'] != x['n'])} deck(s) read back with another page count")
    kinds = Counter()
    for x in ok:
        kinds.update(x["suspect_kinds"])
    print("suspect by kind: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common()))
    if before:
        common = [x for x in ok if x["deck"] in before and not before[x["deck"]].get("error")]
        print(f"against {args.against}: open {sum(before[x['deck']]['open'] for x in common)} -> "
              f"{sum(x['open'] for x in common)}, suspect {sum(before[x['deck']]['suspect'] for x in common)} -> "
              f"{sum(x['suspect'] for x in common)} over {len(common)} decks")
    if args.save:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{args.save}.json").write_text(json.dumps(sorted(done, key=lambda x: x["deck"]), indent=1),
                                              encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
