"""The visual hunt: realistic beamer decks that look fine in the PDF and bad once in Google Slides.

A hunt round is a set of hand-written (or agent-written) decks, each run through the real pipeline
and laid out for a judge who looks at pictures:

    python -m beamer2slides.devtools.visual_hunt run hunt/decks/r1_ml_talk.tex --slot s1
    python -m beamer2slides.devtools.visual_hunt compose out/hunt/s1 --pdf <pdf> --into <archive dir>

`run` compiles the .tex beside itself (engine from a `% !engine = xelatex` first line, as
tests/decks/build.py; a .pdf is taken as built), copies the PDF to `out/hunt/pdfs/<slot>.pdf`, converts it into
`out/hunt/<slot>` (the same slot is rebuilt in place, so a campaign leaves one Drive deck per slot,
not one per deck), runs `fidelity` and `text_fit` on it, and writes the archive
`out/hunt/archive/<deck stem>/`:

- `cmp-NNN.png`: the PDF page (left) and Google's thumbnail of the slide (right), 1000 px each.
  What a judge reads first.
- `pdf-NNN.png`, `slides-NNN.png`: both at Google's 1600 px, for zooming in.
- `summary.json`: the deck URL and, per slide, what the deterministic judges saw - fidelity's
  text overlap and elements whose ink moved, grew or wrapped differently, text_fit's findings, and
  the element kinds the converter chose, and `checks.py`'s offline invariants (stray_ink,
  lost_ink, junk_text...). A pre-screen for the eye, never a verdict: fidelity only sees text over
  the background, text_fit only lines of converted text, the invariants only the local render.
- the .tex and the PDF, and what the converter made of them (`deck.json`, `emit.json`, `debug/`),
  so a finding can be explained and reproduced after the slot is rebuilt.

The judges are agents reading these pictures (out/hunt/BRIEF.md, JUDGE.md, SKEPTIC.md in a
campaign's folder): hunters write decks and report candidates, a blind judge looks without the
hunters' claims, a skeptic tries to knock every candidate down. A doctored archive with planted
defects goes into the judges' batches unannounced, so a judge that misses it is known to be blind.

Google calls go through `devtools.counted` (never interactive: a dead token fails the run).
Blind spots: the judge sees Google's thumbnail, not the editor (selection boxes, editability, alt
text, speaker notes are invisible); a thumbnail is 1600 px, so sub-pt drift is below its eye.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[3]
HUNT = ROOT / "out" / "hunt"
MIKTEX = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64"
RERUN = re.compile(r"Rerun to get|rerun LaTeX|Label\(s\) may have changed|may have changed\. Rerun", re.I)
PANEL = 1000


def engine_for(tex: Path) -> str:
    first = tex.read_text(encoding="utf-8").splitlines()[0]
    m = re.match(r"%\s*!engine\s*=\s*(\w+)", first)
    return m.group(1) if m else "pdflatex"


def compile_tex(tex: Path) -> Path:
    env = dict(os.environ)
    if MIKTEX.exists():
        env["PATH"] = f"{MIKTEX}{os.pathsep}{env.get('PATH', '')}"
    cmd = [engine_for(tex), "-interaction=nonstopmode", "-halt-on-error", tex.name]
    log, aux = tex.with_suffix(".log"), tex.with_suffix(".aux")
    before = None
    for n in range(5):
        result = subprocess.run(cmd, cwd=tex.parent, capture_output=True, text=True, errors="replace",
                                env=env, stdin=subprocess.DEVNULL, timeout=600)
        text = log.read_text(errors="replace") if log.exists() else result.stdout
        if result.returncode != 0:
            raise RuntimeError(f"{tex.name} failed to compile:\n{text[-3000:]}")
        after = aux.read_bytes() if aux.exists() else b""
        if n >= 1 and after == before and not RERUN.search(text):
            break
        before = after
    return tex.with_suffix(".pdf")


GOOGLE_SLOTS = int(os.environ.get("B2S_HUNT_GOOGLE", "4"))  # runs at Google at once, under the write quota


class google_turn:
    """A lock file per concurrent Google run, so parallel hunters stay under the write quota.
    A lock whose process is gone is taken over. `folder`: where the locks live (another campaign's
    own, `edit_hunt`)."""

    def __init__(self, folder: Path | None = None):
        self.folder = folder

    def __enter__(self):
        locks = self.folder or HUNT / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        while True:
            for k in range(GOOGLE_SLOTS):
                path = locks / f"google-{k}.lock"
                try:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    try:
                        pid = int(path.read_text() or 0)
                    except (OSError, ValueError):
                        continue
                    if pid and not _alive(pid):
                        path.unlink(missing_ok=True)
                    continue
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.path = path
                return self
            time.sleep(5)

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def b2s(*args: str) -> subprocess.CompletedProcess:
    """One `python -m beamer2slides ...` under devtools.counted: never interactive."""
    cmd = [sys.executable, "-m", "beamer2slides.devtools.counted", *args]
    with google_turn():
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, errors="replace",
                              stdin=subprocess.DEVNULL, timeout=3600)


def labelled(image: Image.Image, label: str) -> Image.Image:
    image = image.convert("RGB")
    image = image.resize((PANEL, round(image.height * PANEL / image.width)), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (PANEL, image.height + 28), (40, 40, 40))
    canvas.paste(image, (0, 28))
    ImageDraw.Draw(canvas).text((8, 6), label, fill=(255, 255, 255))
    return canvas


def compose(out: Path, pdf: Path, into: Path) -> dict:
    """The archive for one converted deck: side-by-side pictures and the deterministic pre-screen."""
    from ..pdf import Document

    into.mkdir(parents=True, exist_ok=True)
    deck = json.loads((out / "deck.json").read_text(encoding="utf-8"))
    emit = json.loads((out / "emit.json").read_text(encoding="utf-8"))
    fid = json.loads((out / "fidelity.json").read_text(encoding="utf-8")) if (out / "fidelity.json").exists() else {}
    fit = json.loads((out / "text_fit.json").read_text(encoding="utf-8")) if (out / "text_fit.json").exists() else {}
    fid_by_page = {s["page"]: s for s in fid.get("slides", [])}
    fit_by_page = {s["page"]: s for s in fit.get("slides", [])}
    doc = Document(pdf)
    slides = []
    for i, slide in enumerate(deck["slides"]):
        n = slide["page"]
        thumb_path = out / "fidelity" / f"slides-{n + 1:03}.png"
        if not thumb_path.exists():
            continue
        thumb = Image.open(thumb_path).convert("RGB")
        page = doc[n]
        ref = Image.fromarray(page.render(thumb.width / page.width)).convert("RGB").resize(thumb.size)
        ref.save(into / f"pdf-{i + 1:03}.png")
        thumb.save(into / f"slides-{i + 1:03}.png")
        a, b = labelled(ref, f"PDF page {n + 1}"), labelled(thumb, f"GOOGLE SLIDES slide {i + 1}")
        pair = Image.new("RGB", (2 * PANEL + 12, max(a.height, b.height)), (255, 0, 255))
        pair.paste(a, (0, 0))
        pair.paste(b, (PANEL + 12, 0))
        pair.save(into / f"cmp-{i + 1:03}.png")
        kinds: dict[str, int] = {}
        for el in slide["elements"]:
            kinds[el["kind"]] = kinds.get(el["kind"], 0) + 1
        f = fid_by_page.get(n, {})
        odd = [e for e in f.get("elements", [])
               if e.get("missing") or abs(e.get("dx_pt", 0)) > 3 or abs(e.get("dy_top_pt", 0)) > 3
               or abs(e.get("dy_bottom_pt", 0)) > 3 or abs(e.get("width_ratio", 1) - 1) > 0.06
               or e.get("lines_ref") != e.get("lines_slides")]
        slides.append({"slide": i + 1, "page": n + 1, "kinds": kinds,
                       "text_overlap": f.get("text_overlap"), "fidelity_odd": odd,
                       "text_fit": fit_by_page.get(n, {}).get("findings", [])})
    # The slot is rebuilt by the next deck: keep what the converter made of this one.
    for name in ("deck.json", "emit.json", "fidelity.json", "text_fit.json"):
        if (out / name).exists():
            shutil.copyfile(out / name, into / name)
    if (out / "debug").is_dir():
        shutil.copytree(out / "debug", into / "debug", dirs_exist_ok=True)
    summary = {"url": f"https://docs.google.com/presentation/d/{emit['presentationId']}/edit",
               "pdf": pdf.name, "slides": slides}
    (into / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    return summary


def add_invariants(summary: dict, pdf: Path, into: Path) -> dict:
    """checks.py's offline invariants (stray_ink, lost_ink, junk_text...) per slide of the summary."""
    from .. import checks

    found = checks.run_checks(checks.convert_locally(pdf))
    by_page: dict[int, list] = {}
    for f in found:
        by_page.setdefault(f["page"] + 1, []).append(
            {k: f.get(k) for k in ("check", "element", "bbox", "detail")})
    for s in summary["slides"]:
        s["invariants"] = by_page.get(s["page"], [])
    (into / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=str),
                                       encoding="utf-8")
    return summary


def run(tex: Path, slot: str, skip_convert: bool = False) -> int:
    tex = tex.resolve()
    t0 = time.time()
    # A built PDF is taken as it is (an archived one whose fonts today's TeX would not reproduce,
    # e.g. Type 3 bitmap fonts from before a font map change).
    built = tex if tex.suffix.lower() == ".pdf" else compile_tex(tex)
    (HUNT / "pdfs").mkdir(parents=True, exist_ok=True)
    pdf = HUNT / "pdfs" / f"{slot}.pdf"
    shutil.copyfile(built, pdf)
    out = HUNT / slot
    into = HUNT / "archive" / tex.stem
    into.mkdir(parents=True, exist_ok=True)
    if built is not tex:
        shutil.copyfile(tex, into / tex.name)
    shutil.copyfile(built, into / built.name)
    log = []
    if not skip_convert:
        for args in (["convert", str(pdf), "--out", str(out), "--title", f"hunt {tex.stem}"],
                     ["fidelity", str(pdf), "--out", str(out)]):
            r = b2s(*args)
            log.append({"cmd": args[0], "code": r.returncode, "stdout": r.stdout[-4000:], "stderr": r.stderr[-4000:]})
            if r.returncode != 0:
                (into / "run.json").write_text(json.dumps(log, indent=1), encoding="utf-8")
                print(f"{args[0]} failed ({r.returncode}); see {into / 'run.json'}\n{r.stderr[-2000:]}")
                return 1
        r = subprocess.run([sys.executable, "-m", "beamer2slides.devtools.text_fit", str(pdf), "--out", str(out)],
                           cwd=ROOT, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL)
        log.append({"cmd": "text_fit", "code": r.returncode, "stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:]})
    (into / "run.json").write_text(json.dumps(log, indent=1), encoding="utf-8")
    summary = compose(out, pdf, into)
    try:
        summary = add_invariants(summary, pdf, into)
    except Exception as e:  # a pre-screen: never lose the run over it
        print(f"invariants skipped: {type(e).__name__}: {e}")
    print(f"{tex.stem}: {len(summary['slides'])} slides in {time.time() - t0:.0f}s -> {into}\n{summary['url']}")
    return 0


def ledger(verified: Path) -> list[dict]:
    """Confirmed findings of every skeptic file, grouped by class, worst first."""
    classes: dict[str, dict] = {}
    for f in sorted(verified.glob("*.json")):
        for x in json.loads(f.read_text(encoding="utf-8")):
            if x.get("verdict") != "CONFIRMED":
                continue
            c = classes.setdefault(x.get("class") or "?", {"class": x.get("class") or "?", "findings": []})
            c["findings"].append({"hunter": f.stem, **{k: x.get(k) for k in
                                  ("id", "deck", "slide", "severity", "realism", "title", "mechanism")}})
    for c in classes.values():
        c["worst"] = max((int(x["severity"] or 0) * int(x["realism"] or 0) for x in c["findings"]), default=0)
    return sorted(classes.values(), key=lambda c: (-c["worst"], -len(c["findings"]), c["class"]))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="compile, convert, fidelity, text_fit and compose one deck")
    r.add_argument("tex", type=Path)
    r.add_argument("--slot", required=True, help="the out/hunt/<slot> folder (and Drive deck) to rebuild")
    r.add_argument("--skip-convert", action="store_true", help="re-compose what the slot already has")
    c = sub.add_parser("compose", help="only the archive, from a converted output folder")
    c.add_argument("out", type=Path)
    c.add_argument("--pdf", type=Path, required=True)
    c.add_argument("--into", type=Path, required=True)
    g = sub.add_parser("ledger", help="confirmed findings of out/hunt/verified/*.json by class")
    g.add_argument("--verified", type=Path, default=HUNT / "verified")
    a = p.parse_args(argv)
    if a.cmd == "run":
        return run(a.tex, a.slot, a.skip_convert)
    if a.cmd == "ledger":
        rows = ledger(a.verified)
        (a.verified.parent / "ledger.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
        for c in rows:
            where = ", ".join(sorted({f"{x['deck']}#{x['slide']}" for x in c["findings"]}))[:90]
            print(f"{c['worst']:>2} {len(c['findings']):>2}  {c['class'][:44]:<44} {where}")
        return 0
    compose(a.out, a.pdf, a.into)
    return 0


if __name__ == "__main__":
    sys.exit(main())
