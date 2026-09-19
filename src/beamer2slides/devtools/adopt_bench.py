"""How close does `adopt` get to decks nobody converted? A corpus, a score, and the evidence.

`pull` has its residual count to say how far a source is from a deck; that count is the loop's own
view (ten squares read back as one `diagram` are ten `element_missing` while being pixel-perfect), so
this measures what the objective asks for instead: ink in the right place, the compiled source against
the deck's own slide images from Google's renderer.

  capture ID NAME        presentations.get + thumbnails + the foreign IR with its pictures, once
                         (out/adopt-corpus/NAME); everything after that is offline
  run [NAME...]          adopt's bootstrap -> compile -> score, per deck in parallel (--jobs);
                         --iter N also runs pull's loop N rounds and scores what it leaves
  report                 the latest result of every deck, worst slides first

Scores per slide (1.0 = the deck reproduced):
  boxes   ink overlap inside the deck's element boxes (with room around them): what adopt answers for
  page    ink overlap over the whole page: the backdrop and decoration included
  pixels  1 - mean |RGB difference| / 255 over the whole page: colours, fills, backgrounds

Evidence per slide: runs/<tag>/sheets/NNN.png = the deck | the source | ink diff (red only in the
deck, blue only in the source, black both). The corpus holds other people's decks and pictures, so it
lives under out/ (git-ignored); only its manifest (`tests/decks/foreign/corpus.json`) is committed.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.paths import CHECKOUT

# $B2S_ADOPT_CORPUS points a worktree at the main checkout's corpus (out/ is not shared); runs of
# different experiments stay apart by --tag.
CORPUS = Path(os.environ.get("B2S_ADOPT_CORPUS") or CHECKOUT / "out" / "adopt-corpus")
MANIFEST = CHECKOUT / "tests" / "decks" / "foreign" / "corpus.json"


# ------------------------------------------------------------------------------------------ capture

def capture(pid: str, name: str, refresh: bool = False) -> Path:
    """Read deck `pid` once: presentation.json, slides/NNN.png (Google's LARGE thumbnails), and
    target.json = `deck_ir(foreign=True)` with its pictures in images/. Nothing is written to the deck."""
    from beamer2slides.deck_ir import deck_ir, fetch_url
    from beamer2slides.google_auth import credentials, slides_service
    from beamer2slides.gslides import execute, save_thumbnail
    folder = CORPUS / name
    folder.mkdir(parents=True, exist_ok=True)
    # Always read again: picture contentUrls expire within the hour, so a cached answer can no
    # longer fetch its pictures (403). Thumbnails are kept unless --refresh.
    pres = execute(slides_service().presentations().get(presentationId=pid))
    (folder / "presentation.json").write_text(json.dumps(pres, indent=1), encoding="utf-8")
    creds = credentials()

    def thumb(item):
        i, s = item
        path = folder / "slides" / f"{i:03d}.png"
        if refresh or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            for attempt in range(8):           # thumbnails are "expensive reads": 60 a minute per user
                try:
                    save_thumbnail(slides_service(creds), pid, s["objectId"], path)
                    return
                except Exception as exc:                          # noqa: BLE001
                    if "429" not in str(exc) or attempt == 7:
                        raise
                    time.sleep(20 + 10 * attempt)

    with ThreadPoolExecutor(3) as pool:
        list(pool.map(thumb, enumerate(pres["slides"], 1)))
    write_target(folder, pres, fetch_url)
    print(f"{name}: {pres.get('title')!r}, {len(pres['slides'])} slides -> {folder}")
    return folder


def build_target(folder: Path, pres: dict | None = None, fetch=None) -> dict:
    """The IR adopt reads, from the cached presentation. With `fetch` (capture) pictures are
    downloaded and their URLs recorded in urls.json; without it (every run) they come from that
    cache, and a URL capture never saw fails the way an expired one does in `adopt`."""
    import hashlib
    from beamer2slides.deck_ir import deck_ir
    pres = pres or json.loads((folder / "presentation.json").read_text(encoding="utf-8"))
    known_path = folder / "urls.json"
    known = json.loads(known_path.read_text(encoding="utf-8")) if known_path.exists() else {}
    if fetch is None:
        cache = {p.stem: p for p in (folder / "images").glob("*")} if (folder / "images").is_dir() else {}

        def get(url):
            sha = known.get(url)
            if sha and sha[:16] in cache:
                return cache[sha[:16]].read_bytes()
            raise OSError("not captured")
    else:
        def get(url):
            data = fetch(url)
            known[url] = hashlib.sha1(data).hexdigest()
            return data
    def thumbnail(n):
        # the LARGE thumbnails `capture` saved: fills the API reads as empty come from them, as live
        # `adopt` reads them through `deck_ir.slide_thumbnails`
        path = folder / "slides" / f"{n + 1:03d}.png"
        return path if path.exists() else None

    target = deck_ir(pres, fetch=get, images=folder / "images", foreign=True, thumbnails=thumbnail)
    if fetch is not None:
        known_path.write_text(json.dumps(known, indent=0), encoding="utf-8")
    return target


def write_target(folder: Path, pres: dict | None = None, fetch=None) -> dict:
    target = build_target(folder, pres, fetch)
    (folder / "target.json").write_text(json.dumps(target, indent=1), encoding="utf-8")
    return target


# -------------------------------------------------------------------------------------------- score

def page_ground(a: np.ndarray) -> np.ndarray:
    """The colour the page mostly is, as the background ink is measured against."""
    flat = a.reshape(-1, 3).astype(np.int32)
    packed = (flat[:, 0] << 16) | (flat[:, 1] << 8) | flat[:, 2]   # np.unique(axis=0) took ~0.5 s a page
    top = int(np.bincount(packed, minlength=1 << 24).argmax())
    return np.broadcast_to(np.array([top >> 16, (top >> 8) & 255, top & 255], dtype=a.dtype), a.shape)


def covered_mask(slide: dict, w: int, h: int) -> np.ndarray:
    """The slide's element boxes, with room around them, on the reference's pixel grid. Scoring only
    the whole page would let one unreproduced backdrop hide everything else; `page` reports that."""
    m = np.zeros((h, w), dtype=bool)
    px = w / slide["size"][0]
    for el in slide["elements"]:
        size = max((r.get("size") or 4) for p in el.get("paragraphs", []) for r in p["runs"]) \
            if el.get("paragraphs") and any(p["runs"] for p in el["paragraphs"]) else 4.0
        x0, y0, x1, y1 = el["bbox"]
        a0, b0 = max(0, int((x0 - size) * px)), max(0, int((y0 - size) * px))
        a1, b1 = min(w, int((x1 + 2 * size) * px)), min(h, int((y1 + size) * px))
        if a1 > a0 and b1 > b0:                        # a box off the page: a negative end would
            m[b0:b1, a0:a1] = True                     # count from the far edge
    return m


def overlap(m_ref: np.ndarray, m_got: np.ndarray) -> float:
    from beamer2slides.fidelity import dilate
    inter = (dilate(m_ref) & m_got).sum() + (m_ref & dilate(m_got)).sum()
    total = m_ref.sum() + m_got.sum()
    return float(inter / total) if total else 1.0


def score_page(ref: np.ndarray, got: np.ndarray, slide: dict) -> tuple[dict, np.ndarray]:
    from beamer2slides.fidelity import text_mask
    h, w = ref.shape[:2]
    ground = page_ground(ref)
    m_ref, m_got = text_mask(ref, ground), text_mask(got, ground)
    covered = covered_mask(slide, w, h)
    scores = {"boxes": round(overlap(m_ref & covered, m_got & covered), 3),
              "page": round(overlap(m_ref, m_got), 3),
              "pixels": round(1 - float(np.abs(ref - got).mean()) / 255, 3)}
    d = np.full((h, w, 3), 255, dtype=np.uint8)
    d[m_ref & ~m_got] = (220, 40, 40)
    d[m_got & ~m_ref] = (30, 110, 230)
    d[m_ref & m_got] = (0, 0, 0)
    return scores, d


def score_pdf(pdf: Path, folder: Path, target: dict, sheets: Path | None) -> list[dict]:
    from beamer2slides.fidelity import rgb_array
    from beamer2slides.pdf import Document
    refs = sorted((folder / "slides").glob("*.png"))
    doc = Document(pdf)
    out = []
    try:
        for i, ref_path in enumerate(refs):
            if i >= len(target["slides"]):
                break
            ref_img = Image.open(ref_path).convert("RGB")
            w, h = ref_img.size
            ref = rgb_array(ref_img)
            if i >= len(doc):
                out.append({"slide": i + 1, "boxes": 0.0, "page": 0.0, "pixels": 0.0, "missing": True})
                continue
            got_img = Image.fromarray(doc[i].render(w / doc[i].width)).convert("RGB").resize((w, h))
            got = rgb_array(got_img)
            scores, diff = score_page(ref, got, target["slides"][i])
            out.append({"slide": i + 1, **scores})
            if sheets is not None:
                sheets.mkdir(parents=True, exist_ok=True)
                sheet = Image.new("RGB", (w * 3 + 20, h), "white")
                for k, im in enumerate((ref_img, got_img, Image.fromarray(diff))):
                    sheet.paste(im, (k * (w + 10), 0))
                sheet.resize((sheet.width // 2, h // 2)).save(sheets / f"{i + 1:03}.png")
    finally:
        doc.close()
    return out


# ---------------------------------------------------------------------------------------------- run

def load_target(folder: Path, slides: str | None, run: Path | None = None) -> dict:
    """The IR as *this* code reads the cached presentation (so a deck_ir change shows in the run),
    pictures from the capture's cache; written to the run folder, never over the corpus."""
    if (folder / "presentation.json").exists():
        target = build_target(folder)
        if run is not None:
            (run / "target.json").write_text(json.dumps(target, indent=1), encoding="utf-8")
    else:
        target = json.loads((folder / "target.json").read_text(encoding="utf-8"))
    if slides:
        a, _, b = slides.partition("-")
        lo, hi = int(a) - 1, int(b or a)
        target["slides"] = target["slides"][lo:hi]
        target["first_slide"] = lo
    return target


def run_one(name: str, iters: int = 0, flow: bool = False, slides: str | None = None,
            tag: str | None = None) -> dict:
    """Bootstrap (and optionally converge) one corpus deck and score it. Never raises: a crash or a
    compile error is the result, since finding those is half the point."""
    from beamer2slides import adopt
    from beamer2slides.inverse import Workspace, converge
    folder = CORPUS / name
    tag = tag or ("flow" if flow else "abs") + (f"-it{iters}" if iters else "") + (f"-s{slides}" if slides else "")
    run = folder / "runs" / tag
    shutil.rmtree(run, ignore_errors=True)
    run.mkdir(parents=True)
    res: dict = {"deck": name, "tag": tag, "iters": iters, "flow": flow, "slides": slides}
    t0 = time.perf_counter()
    try:
        target = load_target(folder, slides, run)
        res["n"] = len(target["slides"])
        tex = run / "tree" / "main.tex"
        adopt.bootstrap(target, tex, flow)
        res["bootstrap_s"] = round(time.perf_counter() - t0, 1)
        ws = Workspace(tex, run / "work")
        pdf, err = ws.compile()
        if pdf is None:
            # One broken frame should not hide the other slides: find the frames that do not compile
            # on their own, blank them, and score the rest (the errors are findings of their own).
            res["frame_errors"] = broken_frames(tex, run / "frames")
            if res["frame_errors"]:
                blank_frames(tex, [f["frame"] for f in res["frame_errors"]])
                ws = Workspace(tex, run / "work")
                pdf, err = ws.compile()
        res["compile_s"] = round(time.perf_counter() - t0, 1)
        if pdf is None:
            res["error"] = "compile: " + err[-1500:]
            return finish(run, res, t0)
        if slides:                               # thumbnails are numbered from the deck's first slide
            folder_view = run / "refs"
            (folder_view / "slides").mkdir(parents=True, exist_ok=True)
            lo = target["first_slide"]
            for k in range(res["n"]):
                shutil.copyfile(folder / "slides" / f"{lo + k + 1:03}.png", folder_view / "slides" / f"{k + 1:03}.png")
        else:
            folder_view = folder
        res["bootstrap"] = score_pdf(pdf, folder_view, target, run / "sheets")
        if iters:
            result = converge(tex, target, run / "loop", max_iter=iters, log=lambda *_: None)
            res["loop"] = {"iterations": result.iterations, "converged": result.converged,
                           "unresolved": len(result.unresolved)}
            final = run / "loop" / "build" / "main.pdf"
            if final.exists():
                res["converged"] = score_pdf(final, folder_view, target, run / "sheets-loop")
    except Exception as exc:                                     # noqa: BLE001 - the crash is the finding
        res["error"] = f"{type(exc).__name__}: {exc}"[:1500]
        res["traceback"] = traceback.format_exc()[-4000:]
    return finish(run, res, t0)


SLIDE_MARK = re.compile(r"^% slide (\d+)\n", re.M)


def split_frames(text: str) -> tuple[str, list[tuple[int, str]], str]:
    """(everything before the first frame, [(slide number, frame text)], the end) of an adopted source."""
    marks = list(SLIDE_MARK.finditer(text))
    end = text.rfind("\\end{document}")
    if not marks:
        return text, [], ""
    frames = [(int(m.group(1)), text[m.start():(marks[k + 1].start() if k + 1 < len(marks) else end)])
              for k, m in enumerate(marks)]
    return text[:marks[0].start()], frames, text[end:]


def broken_frames(tex: Path, work: Path, jobs: int = 4) -> list[dict]:
    """The frames that do not compile on their own, with TeX's error for each."""
    from beamer2slides.inverse import Workspace, copy_tree
    head, frames, tail = split_frames(tex.read_text(encoding="utf-8"))

    def one(item):
        n, body = item
        tree = work / f"{n:03}"
        shutil.rmtree(tree, ignore_errors=True)
        copy_tree(tex.parent, tree)
        (tree / tex.name).write_text(head + body + tail, encoding="utf-8")
        pdf, err = Workspace(tree / tex.name, tree / "work").compile()
        return None if pdf else {"frame": n, "error": err[-800:]}

    with ThreadPoolExecutor(jobs) as pool:
        out = [r for r in pool.map(one, frames) if r]
    shutil.rmtree(work, ignore_errors=True)
    return out


def blank_frames(tex: Path, numbers: list[int]) -> None:
    head, frames, tail = split_frames(tex.read_text(encoding="utf-8"))
    body = "".join(f"% slide {n}\n\\begin{{frame}}[plain]\\end{{frame}}\n" if n in numbers else text
                   for n, text in frames)
    tex.write_text(head + body + tail, encoding="utf-8")


def finish(run: Path, res: dict, t0: float) -> dict:
    res["seconds"] = round(time.perf_counter() - t0, 1)
    for key in ("bootstrap", "converged"):
        if res.get(key):
            res[f"{key}_mean"] = {m: round(sum(s[m] for s in res[key]) / len(res[key]), 3)
                                  for m in ("boxes", "page", "pixels")}
    (run / "result.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def line(res: dict) -> str:
    if res.get("error"):
        return f"{res['deck']:<24} {res['tag']:<14} ERROR {res['error'].splitlines()[0][:110]}"
    b = res.get("bootstrap_mean", {})
    c = res.get("converged_mean")
    if res.get("frame_errors"):
        broken = f" [{len(res['frame_errors'])} frames broken: " + \
                 ",".join(str(f["frame"]) for f in res["frame_errors"][:8]) + "]"
    else:
        broken = ""
    tail = f"  loop -> boxes {c['boxes']:.3f} page {c['page']:.3f} pixels {c['pixels']:.3f}" if c else ""
    return (f"{res['deck']:<24} {res['tag']:<14} {res.get('n', 0):3} slides {res['seconds']:6.1f}s  "
            f"boxes {b.get('boxes', 0):.3f} page {b.get('page', 0):.3f} pixels {b.get('pixels', 0):.3f}{tail}{broken}")


def decks() -> list[str]:
    return sorted(p.name for p in CORPUS.iterdir() if (p / "presentation.json").exists()) if CORPUS.is_dir() else []


def report(tag: str = "abs", worst: int = 3) -> None:
    rows = []
    for name in decks():
        path = CORPUS / name / "runs" / tag / "result.json"
        if path.exists():
            rows.append(json.loads(path.read_text(encoding="utf-8")))
    for r in sorted(rows, key=lambda r: r.get("bootstrap_mean", {}).get("boxes", -1)):
        print(line(r))
        for s in sorted(r.get("bootstrap", []), key=lambda s: s["boxes"])[:worst]:
            print(f"    slide {s['slide']:3}: boxes {s['boxes']:.2f} page {s['page']:.2f} pixels {s['pixels']:.2f}")
    ok = [r for r in rows if r.get("bootstrap_mean")]
    if ok:
        n = sum(r["n"] for r in ok)
        mean = {m: sum(r["bootstrap_mean"][m] * r["n"] for r in ok) / n for m in ("boxes", "page", "pixels")}
        print(f"\n{len(ok)}/{len(rows)} decks built, {n} slides: boxes {mean['boxes']:.3f} "
              f"page {mean['page']:.3f} pixels {mean['pixels']:.3f}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("id", nargs="?")
    c.add_argument("name", nargs="?")
    c.add_argument("--manifest", action="store_true", help="capture every deck in the manifest")
    c.add_argument("--refresh", action="store_true")
    c.add_argument("--shard", type=int, nargs=2, metavar=("K", "N"), help="only every N-th deck from K")
    t = sub.add_parser("target", help="rebuild target.json from the cached presentation (after a deck_ir change)")
    t.add_argument("names", nargs="*")
    r = sub.add_parser("run")
    r.add_argument("names", nargs="*")
    r.add_argument("--iter", type=int, default=0)
    r.add_argument("--flow", action="store_true")
    r.add_argument("--slides", help="a-b, 1-based")
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--tag")
    p = sub.add_parser("report")
    p.add_argument("--tag", default="abs")
    args = ap.parse_args(argv)
    if args.cmd == "capture":
        if args.manifest:
            for k, d in enumerate(json.loads(MANIFEST.read_text(encoding="utf-8"))):
                if args.shard and k % args.shard[1] != args.shard[0]:
                    continue
                if not args.refresh and (CORPUS / d["name"] / "target.json").exists():
                    continue
                try:
                    capture(d["id"], d["name"], args.refresh)
                except Exception as exc:                          # noqa: BLE001
                    print(f"{d['name']}: {type(exc).__name__}: {exc}", file=sys.stderr)
        else:
            capture(args.id, args.name, args.refresh)
    elif args.cmd == "target":
        for name in args.names or decks():
            write_target(CORPUS / name)
            print(f"{name}: target.json rebuilt")
    elif args.cmd == "run":
        names = args.names or decks()
        with ProcessPoolExecutor(max(1, min(args.jobs, len(names)))) as pool:
            futs = [pool.submit(run_one, n, args.iter, args.flow, args.slides, args.tag) for n in names]
            for f in futs:
                print(line(f.result()), flush=True)
    else:
        report(args.tag)


if __name__ == "__main__":
    main()
