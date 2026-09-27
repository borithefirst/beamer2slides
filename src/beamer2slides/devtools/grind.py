"""The adopt fidelity grind: the loop that finds what adopt gets wrong on decks other people made,
through the path a sandbox takes (a deck handed over as files, no network, no fonts installed).

  files [NAME...]        turn bench captures into the files `deck-files` saves (<capture>/deck-files/):
                         their presentation, thumbnails and cached pictures, plus the google/fonts
                         files adopt fetches when it may look at no installed font (recorded now)
  round TAG              one round over every corpus: bench run --offline, slide metrics, the
                         worst-slides page against the last one; each stage timed into the ledger
  show [--tag T]         the worst slides of the newest round (or T), marked new against the last page
  log STAGE ...          a ledger line for work done outside this driver (judges: tokens, seconds)
  ledger                 time and tokens by stage and by round

The loop itself (docs/adopt-grind.md): round -> judge the worst new slides blind (cheap models) ->
trace the families that hold up to a mechanism -> fix -> round again, A/B against the last tag.
Everything lands under out/grind/ (git-ignored): the corpora are other people's decks.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from beamer2slides.paths import CHECKOUT

OUT = CHECKOUT / "out" / "grind"
LEDGER = OUT / "ledger.jsonl"
CORPORA = [CHECKOUT / "out" / "adopt-corpus", CHECKOUT / "out" / "adopt-hunt"]
CALIBRATION = CHECKOUT / "out" / "adopt-corpus" / "judged" / "hunt0" / "calibration.json"


# ------------------------------------------------------------------------------------------ ledger

def log(stage: str, **fields) -> dict:
    """One line of the ledger: what a stage cost (seconds, tokens, agents) and what it found."""
    entry = {"when": time.strftime("%Y-%m-%dT%H:%M:%S"), "stage": stage, **fields}
    OUT.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def entries() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]


def summary() -> None:
    by_stage: dict = defaultdict(lambda: [0, 0.0, 0])
    by_round: dict = defaultdict(lambda: [0.0, 0])
    for e in entries():
        s = by_stage[e["stage"]]
        s[0] += 1
        s[1] += e.get("seconds") or 0
        s[2] += e.get("tokens") or 0
        if e.get("tag"):
            by_round[e["tag"]][0] += e.get("seconds") or 0
            by_round[e["tag"]][1] += e.get("tokens") or 0
    print(f"{'stage':<18} {'runs':>5} {'minutes':>8} {'tokens':>10}")
    for k, (n, sec, tok) in sorted(by_stage.items(), key=lambda kv: -kv[1][1]):
        print(f"{k:<18} {n:>5} {sec / 60:8.1f} {tok:>10}")
    if by_round:
        print(f"\n{'round':<18} {'minutes':>8} {'tokens':>10}")
        for k, (sec, tok) in by_round.items():
            print(f"{k:<18} {sec / 60:8.1f} {tok:>10}")


# ------------------------------------------------------------------------------------------- files

def make_files(folder: Path, refresh: bool = False) -> str:
    """A bench capture as the files `deck-files` saves, in <capture>/deck-files/. The pictures are
    the capture's cache (the URLs it saw); the fonts are fetched now, as `deck_files.save` does."""
    from beamer2slides import deck_files
    from beamer2slides.devtools.adopt_bench import OFFLINE
    out = folder / OFFLINE
    if (out / deck_files.MANIFEST).exists() and not refresh:
        return f"{folder.name}: kept"
    t0 = time.perf_counter()
    part = folder / (OFFLINE + ".part")
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir()
    shutil.copyfile(folder / "presentation.json", part / deck_files.FOLDERS["presentation"])
    if (folder / "slides").is_dir():
        shutil.copytree(folder / "slides", part / deck_files.FOLDERS["thumbnails"])
    pres = json.loads((folder / "presentation.json").read_text(encoding="utf-8"))
    known = json.loads((folder / "urls.json").read_text(encoding="utf-8")) if (folder / "urls.json").exists() else {}
    cache = {p.stem: p for p in (folder / "images").glob("*")} if (folder / "images").is_dir() else {}

    def fetch(url: str) -> bytes:
        sha = known.get(url)
        if sha and sha[:16] in cache:
            return cache[sha[:16]].read_bytes()
        raise OSError("not captured")

    def thumbs(n: int):
        p = part / deck_files.FOLDERS["thumbnails"] / f"{n + 1:03d}.png"
        return p if p.exists() else None

    lines: list[str] = []
    manifest = deck_files.record(part, pres, thumbs, fetch, None, lines.append)
    shutil.rmtree(out, ignore_errors=True)
    os.replace(part, out)
    c = manifest["counts"]
    return (f"{folder.name}: {c['slides']} slides, {c['pictures']} pictures, {c['google_fonts']} font files"
            + (f", stood in: {', '.join(manifest['fonts_missing'])}" if manifest["fonts_missing"] else "")
            + f" ({time.perf_counter() - t0:.0f} s)")


def captures(corpora: list[Path], names: list[str]) -> list[Path]:
    out = []
    for corpus in corpora:
        for p in sorted(corpus.iterdir()) if corpus.is_dir() else []:
            if (p / "presentation.json").exists() and (not names or p.name in names):
                out.append(p)
    return out


def files(corpora: list[Path], names: list[str], jobs: int, refresh: bool) -> None:
    t0 = time.perf_counter()
    folders = captures(corpora, names)
    made = 0
    with ProcessPoolExecutor(jobs) as pool:
        for msg in pool.map(make_files, folders, [refresh] * len(folders)):
            made += not msg.endswith("kept")
            print(msg, flush=True)
    log("files", seconds=round(time.perf_counter() - t0, 1), decks=made)


# ------------------------------------------------------------------------------------------- round

def child(module: str, *args: str, corpus: Path) -> float:
    """Run a devtools module over one corpus in its own process; its seconds."""
    from beamer2slides import interpreter
    t0 = time.perf_counter()
    env = {**interpreter.env(), "B2S_ADOPT_CORPUS": str(corpus), "PYTHONIOENCODING": "utf-8"}
    subprocess.run([interpreter.python(), "-m", f"beamer2slides.devtools.{module}", *args], env=env, check=False,
                   stdin=subprocess.DEVNULL)
    return round(time.perf_counter() - t0, 1)


def round_(tag: str, corpora: list[Path], jobs: int, gpu: bool, names: list[str], python: str | None) -> Path:
    for corpus in corpora:
        sec = child("adopt_bench", "run", *names, "--offline", "--tag", tag, "--jobs", str(jobs), corpus=corpus)
        results = [json.loads(p.read_text(encoding="utf-8")) for p in corpus.glob(f"*/runs/{tag}/result.json")]
        log("bench", tag=tag, corpus=corpus.name, seconds=sec, decks=len(results),
            cached=sum(1 for r in results if r.get("cached")), errors=sum(1 for r in results if r.get("error")))
        t0 = time.perf_counter()
        # the torch metrics live in their own venv (docs/adopt-bench.md "Metrics")
        cmd = [python or sys.executable, "-m", "beamer2slides.devtools.slide_metrics", "run", tag, *names,
               "--jobs", str(jobs)] + (["--gpu"] if gpu else [])
        from beamer2slides import interpreter
        subprocess.run(cmd, env={**interpreter.env(), "B2S_ADOPT_CORPUS": str(corpus)}, check=False,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        log("metrics", tag=tag, corpus=corpus.name, seconds=round(time.perf_counter() - t0, 1), gpu=gpu)
    return show(tag, corpora)


# -------------------------------------------------------------------------------------------- show

def pages() -> list[Path]:
    return sorted(OUT.glob("worst-*.json"), key=lambda p: p.stat().st_mtime)


def latest_tag() -> str | None:
    rounds = [e["tag"] for e in entries() if e["stage"] == "metrics" and e.get("tag")]
    if rounds:
        return rounds[-1]
    old = pages()
    return json.loads(old[-1].read_text(encoding="utf-8"))["tag"] if old else None


def show(tag: str | None, corpora: list[Path], n: int = 20, per_deck: int = 2) -> Path:
    """The worst-slides page of `tag` (else the newest round's), against the newest page before it;
    prints what came onto the list and what left it."""
    from beamer2slides.devtools.slide_metrics import gallery
    tag = tag or latest_tag()
    if tag is None:
        raise SystemExit("no round yet: grind round TAG")
    t0 = time.perf_counter()
    old = pages()
    previous = old[-1] if old else None
    cal = json.loads(CALIBRATION.read_text(encoding="utf-8"))
    path = gallery(tag, cal["thresholds"], corpora, n, OUT, previous, per_deck, cal.get("severity_weights"))
    now = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    was = json.loads(previous.read_text(encoding="utf-8")) if previous else {"worst": [], "tag": None}
    key = lambda s: f"{s['deck']}:{s['slide']}"                          # noqa: E731
    came = [key(s) for s in now["worst"] if key(s) not in {key(t) for t in was["worst"]}]
    left = [key(s) for s in was["worst"] if key(s) not in {key(t) for t in now["worst"]}]
    print(path)
    print(f"run {tag}: {now['flagged']} of {now['slides']} slides flagged"
          + (f" (was {was['flagged']} of {was['slides']} in {was['tag']})" if previous else ""))
    print(f"new on the list: {', '.join(came) or 'none'}")
    print(f"off the list: {', '.join(left) or 'none'}")
    log("show", tag=tag, seconds=round(time.perf_counter() - t0, 1), flagged=now["flagged"], slides=now["slides"],
        new=len(came), left=len(left))
    return path


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", type=Path, action="append", help="a bench corpus (repeat); default both")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("files")
    f.add_argument("names", nargs="*")
    f.add_argument("--jobs", type=int, default=6)
    f.add_argument("--refresh", action="store_true")
    r = sub.add_parser("round")
    r.add_argument("tag")
    r.add_argument("names", nargs="*", help="only these decks (a quick A/B)")
    r.add_argument("--jobs", type=int, default=6)
    r.add_argument("--gpu", action="store_true", help="torch metrics too, run by --python")
    r.add_argument("--python", help="the interpreter of the metrics venv (out/metrics-venv)")
    s = sub.add_parser("show")
    s.add_argument("--tag")
    s.add_argument("-n", type=int, default=20)
    s.add_argument("--per-deck", type=int, default=2)
    lg = sub.add_parser("log")
    lg.add_argument("stage")
    lg.add_argument("--tag")
    lg.add_argument("--seconds", type=float)
    lg.add_argument("--tokens", type=int)
    lg.add_argument("--agents", type=int)
    lg.add_argument("--model")
    lg.add_argument("--note")
    sub.add_parser("ledger")
    a = ap.parse_args(argv)
    corpora = a.corpus or CORPORA
    if a.cmd == "files":
        files(corpora, a.names, a.jobs, a.refresh)
    elif a.cmd == "round":
        round_(a.tag, corpora, a.jobs, a.gpu, a.names, a.python)
    elif a.cmd == "show":
        show(a.tag, corpora, a.n, a.per_deck)
    elif a.cmd == "log":
        print(log(a.stage, **{k: v for k, v in vars(a).items() if k in ("tag", "seconds", "tokens", "agents", "model", "note")
                             and v is not None}))
    else:
        summary()


if __name__ == "__main__":
    sys.exit(main())
