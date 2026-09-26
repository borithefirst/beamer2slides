"""How close does `adopt` get to decks nobody converted? A corpus, a score, and the evidence.

`pull` has its residual count to say how far a source is from a deck; that count is the loop's own
view (ten squares read back as one `diagram` are ten `element_missing` while being pixel-perfect), so
this measures what the objective asks for instead: ink in the right place, the compiled source against
the deck's own slide images from Google's renderer.

  capture ID NAME        presentations.get + thumbnails + the foreign IR with its pictures, once
                         (out/adopt-corpus/NAME); everything after that is offline
  run [NAME...]          adopt's bootstrap -> compile -> score, per deck in parallel (--jobs);
                         --iter N also runs pull's loop N rounds and scores what it leaves;
                         NAME:a-b a slide range of one deck, --micro the MICRO slides
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
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.paths import CHECKOUT

# $B2S_ADOPT_CORPUS points a worktree at the main checkout's corpus (out/ is not shared); runs of
# different experiments stay apart by --tag.
CORPUS = Path(os.environ.get("B2S_ADOPT_CORPUS") or CHECKOUT / "out" / "adopt-corpus")
MANIFEST = CHECKOUT / "tests" / "decks" / "foreign" / "corpus.json"

# The micro-corpus: one slide per way the loop's round 0 went wrong on 2026-09-26 (a slide the first
# draft already inked >= 0.97 that the read-back still found tens to hundreds of residuals on). A
# one-frame source compiles in seconds, so `--micro` runs them all in a minute or two; a family
# someone fixes should drop out of every slide named for it. Only decks the manifest names.
MICRO = (
    "sc-memphis:2",            # 150 shapes, 40 pictures: shapes read back as background
    "sc-memphis:9",            # a table among shapes
    "sc-memphis:16",           # 290 shapes over a full-page picture
    "devfest2020:35",          # a full-page picture, 47 middle-anchored boxes over it
    "devfest2020:39",          # 43 pictures, 46 boxes
    "gdg24:4",
    "gdg24:84",                # stacked text boxes over shapes
    "drawing-workshop:1",      # a full-page picture swallows the words on it
    "drawing-workshop:6",
    "drawings-basics:13",
    "cs161-net:15",            # speaker notes: notes pages read back as slides
    "cs161-tls:9",             # notes, few elements
    "intro-lecture:33",        # notes
    "arabic-training:6",       # right-to-left
    "hebrew-lesson:9",         # right-to-left in a table
    "journey-maps:15",         # one table, four elements, 60 residuals
    "journey-maps:2",
    "sc-functions:8",          # a table over a full-page picture
    "sc-functions:11",
    "sc-dark-modern:17",       # three tables
    "sc-dark-modern:18",
    "sc-aesthetic-school:21",
    "sc-dark-minimal:3",
    "supercharge-slides:31",
    "jruby-ja:16",             # Japanese
    "apps-edu-zh:9",           # Chinese
    "ap-bio-stats:40",
    "creandum-board:22",
    "comic-strips:7",
    "jeb-arch:3",
    "sc-river-a4:2",           # A4 page
    "instagram:2",
    "solidity-survey:34",
    "ds-lecture:13",
    "plain-layouts:6",         # Slides' own layouts, a bottom-anchored title
    "plain-fonts:5",           # a variable font's weights
)


def micro_specs() -> list[str]:
    """The MICRO slides of the decks this corpus holds."""
    held = set(decks())
    return [s for s in MICRO if s.partition(":")[0] in held]


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


def element_boxes(slide: dict, w: int, h: int) -> list[tuple[int, tuple[int, int, int, int]]]:
    """(element index, pixel box) of the slide's elements, with room around them, on the reference's
    pixel grid; boxes wholly off the page left out."""
    out = []
    px = w / slide["size"][0]
    for k, el in enumerate(slide["elements"]):
        size = max((r.get("size") or 4) for p in el.get("paragraphs", []) for r in p["runs"]) \
            if el.get("paragraphs") and any(p["runs"] for p in el["paragraphs"]) else 4.0
        x0, y0, x1, y1 = el["bbox"]
        a0, b0 = max(0, int((x0 - size) * px)), max(0, int((y0 - size) * px))
        a1, b1 = min(w, int((x1 + 2 * size) * px)), min(h, int((y1 + size) * px))
        if a1 > a0 and b1 > b0:                        # a box off the page: a negative end would
            out.append((k, (a0, b0, a1, b1)))          # count from the far edge
    return out


def covered_mask(slide: dict, w: int, h: int) -> np.ndarray:
    """The slide's element boxes, with room around them. Scoring only the whole page would let one
    unreproduced backdrop hide everything else; `page` reports that."""
    m = np.zeros((h, w), dtype=bool)
    for _, (a0, b0, a1, b1) in element_boxes(slide, w, h):
        m[b0:b1, a0:a1] = True
    return m


def element_losses(m_ref: np.ndarray, m_got: np.ndarray, slide: dict, top: int = 6) -> list[dict]:
    """Where a slide's `boxes` score went: every pixel `overlap` counts against it (ink of the deck
    with none of ours near it = `miss`, ours with none of the deck's near it = `extra`) is charged to
    the smallest element box holding it, so an element's `loss` is exactly its share of 1 - boxes."""
    from beamer2slides.fidelity import dilate
    h, w = m_ref.shape
    label = np.full((h, w), -1, dtype=np.int32)
    boxes = element_boxes(slide, w, h)
    for k, (a0, b0, a1, b1) in sorted(boxes, key=lambda kb: -(kb[1][2] - kb[1][0]) * (kb[1][3] - kb[1][1])):
        label[b0:b1, a0:a1] = k                        # smaller boxes painted last: they win
    inside = label >= 0
    total = (m_ref & inside).sum() + (m_got & inside).sum()
    if not total:
        return []
    miss = m_ref & inside & ~dilate(m_got & inside)
    extra = m_got & inside & ~dilate(m_ref & inside)
    n = len(slide["elements"])
    lm = np.bincount(label[miss], minlength=n)[:n]
    le = np.bincount(label[extra], minlength=n)[:n]
    out = []
    for k in np.argsort(-(lm + le))[:top]:
        if lm[k] + le[k] == 0:
            break
        el = slide["elements"][k]
        runs = [r for p in el.get("paragraphs", []) for r in p["runs"]]
        out.append({"id": el.get("id"), "kind": el["kind"], "role": el.get("role"),
                    "shape": el.get("shape_type"), "font": runs[0].get("font") if runs else None,
                    "text": "".join(r["text"] for r in runs)[:40],
                    "loss": round(float((lm[k] + le[k]) / total), 4),
                    "miss": round(float(lm[k] / total), 4), "extra": round(float(le[k] / total), 4)})
    return out


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
              "pixels": round(1 - float(np.abs(ref - got).mean()) / 255, 3),
              "losses": element_losses(m_ref, m_got, slide)}
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
            tag: str | None = None, cache: bool = True) -> dict:
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
        key = cache_key(tex.parent, target)
        hit = CORPUS / name / "cache" / key
        if cache and not iters and (hit / "result.json").exists():
            # the same source scored by the same scorer: its scores and sheets, no compile
            res.update(json.loads((hit / "result.json").read_text(encoding="utf-8")), cached=True)
            if (hit / "sheets").is_dir():
                shutil.copytree(hit / "sheets", run / "sheets")
            return finish(run, res, t0)
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
        store(hit, run, {**{k: res[k] for k in ("bootstrap", "frame_errors") if k in res},
                         "full_s": round(time.perf_counter() - t0, 1)})
        if iters:
            result = converge(tex, target, run / "loop", max_iter=iters, log=lambda *_: None)
            # the promise is convergence, so say it per deck: did it, and did the rounds bring the
            # open residuals down or up (the saudi-cats deck went 127 -> 138 and nothing said so)
            res["loop"] = {"iterations": result.iterations, "converged": result.converged,
                           "unresolved": len(result.unresolved),
                           "open": [i["open"] for i in result.iterations]}
            final = run / "loop" / "build" / "main.pdf"
            if final.exists():
                res["converged"] = score_pdf(final, folder_view, target, run / "sheets-loop")
    except Exception as exc:                                     # noqa: BLE001 - the crash is the finding
        res["error"] = f"{type(exc).__name__}: {exc}"[:1500]
        res["traceback"] = traceback.format_exc()[-4000:]
    return finish(run, res, t0)


CACHE_KEEP = 6          # results kept per deck, newest first


def cache_key(tree: Path, target: dict) -> str:
    """What a bootstrap score depends on: every file of the source tree, the IR the boxes come from,
    and the scoring code. A change that leaves a deck's source alone then costs that deck no compile."""
    import hashlib
    h = hashlib.sha256()
    for p in sorted(q for q in tree.rglob("*") if q.is_file()):
        h.update(p.relative_to(tree).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    h.update(json.dumps(target, sort_keys=True, default=str).encode())
    from beamer2slides import fidelity
    for code in (Path(__file__), Path(fidelity.__file__)):
        h.update(code.read_bytes())
    return h.hexdigest()[:24]


def store(hit: Path, run: Path, result: dict) -> None:
    try:
        tmp = hit.with_name(hit.name + f".{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        (tmp / "result.json").write_text(json.dumps(result), encoding="utf-8")
        if (run / "sheets").is_dir():
            shutil.copytree(run / "sheets", tmp / "sheets")
        shutil.rmtree(hit, ignore_errors=True)
        os.replace(tmp, hit)
        old = sorted((p for p in hit.parent.iterdir() if p.is_dir()), key=lambda p: -p.stat().st_mtime)
        for p in old[CACHE_KEEP:]:
            shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass                                   # a cache that cannot be written only costs time


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
    loop = res.get("loop")
    if loop and loop.get("open"):
        tail += "  " + ("CONVERGED" if loop["converged"] else "open " + " -> ".join(map(str, loop["open"])))
    return (f"{res['deck']:<24} {res['tag']:<14} {res.get('n', 0):3} slides {res['seconds']:6.1f}s  "
            f"boxes {b.get('boxes', 0):.3f} page {b.get('page', 0):.3f} pixels {b.get('pixels', 0):.3f}{tail}{broken}")


def last_seconds(name: str) -> float:
    """How long the deck's newest run took (0 when it never ran)."""
    runs = [p / "result.json" for p in (CORPUS / name / "runs").glob("*")] if (CORPUS / name / "runs").is_dir() else []
    runs = [p for p in runs if p.exists()]
    if not runs:
        return 0.0
    res = json.loads(max(runs, key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8"))
    return float(res.get("full_s") or res.get("seconds") or 0)


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


def losses(tag: str, top: int = 40) -> None:
    """Where the corpus' `boxes` score goes, element by element (`element_losses`): each element's
    loss as points of the corpus mean (its slide's share / all slides), the worst first, then summed
    by deck, by kind and by font. `miss` = the deck's ink we lack, `extra` = ink of ours it lacks;
    both at once is usually the same ink set in the wrong place."""
    from collections import defaultdict
    items, n = [], 0
    for name in decks():
        path = CORPUS / name / "runs" / tag / "result.json"
        if not path.exists():
            continue
        res = json.loads(path.read_text(encoding="utf-8"))
        n += res.get("n", 0) if res.get("bootstrap") else 0
        for s in res.get("bootstrap") or []:
            for e in s.get("losses", []):
                items.append({**e, "deck": name, "slide": s["slide"]})
    if not n:
        print(f"no results with losses under tag {tag!r}")
        return
    pts = lambda v: 1000 * v / n                                         # noqa: E731 - thousandths
    print(f"{n} slides; losses in thousandths of the corpus mean boxes score (1 = 0.001)\n")
    print(f"{'pts':>6} {'miss':>5} {'extra':>5}  {'deck':<22} {'sl':>3}  {'kind':<14} {'font':<18} text")
    for e in sorted(items, key=lambda e: -e["loss"])[:top]:
        kind = e["kind"] + ("/" + e["shape"] if e.get("shape") else "")
        print(f"{pts(e['loss']):6.2f} {pts(e['miss']):5.2f} {pts(e['extra']):5.2f}  {e['deck']:<22} "
              f"{e['slide']:>3}  {kind[:14]:<14} {(e.get('font') or '')[:18]:<18} {e['text']!r}")
    for title, keyf in (("deck", lambda e: e["deck"]),
                        ("kind", lambda e: e["kind"] + ("/" + e["shape"] if e.get("shape") else "")),
                        ("font", lambda e: e.get("font") or "-")):
        agg = defaultdict(lambda: [0.0, 0.0, 0.0, 0])
        for e in items:
            a = agg[keyf(e)]
            a[0] += e["loss"]; a[1] += e["miss"]; a[2] += e["extra"]; a[3] += 1   # noqa: E702
        print(f"\nby {title}:")
        for k, (lo, mi, ex, c) in sorted(agg.items(), key=lambda kv: -kv[1][0])[:15]:
            print(f"  {pts(lo):6.2f}  miss {pts(mi):5.2f} extra {pts(ex):5.2f}  {c:5} elements  {k}")
    print(f"\ntotal charged: {pts(sum(e['loss'] for e in items)):.1f} (top {6} elements per slide)")


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
    r.add_argument("names", nargs="*", help="decks, or NAME:a-b for a slide range of one")
    r.add_argument("--micro", action="store_true", help="the micro-corpus (MICRO): one slide per failure family")
    r.add_argument("--iter", type=int, default=0)
    r.add_argument("--flow", action="store_true")
    r.add_argument("--slides", help="a-b, 1-based")
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--tag")
    r.add_argument("--no-cache", action="store_true", help="compile every deck, even one whose source is unchanged")
    p = sub.add_parser("report")
    p.add_argument("--tag", default="abs")
    lo = sub.add_parser("losses", help="where the boxes score goes, by element, deck, kind and font")
    lo.add_argument("--tag", default="abs")
    lo.add_argument("--top", type=int, default=40)
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
        names = args.names + (micro_specs() if args.micro else []) or decks()
        # the slowest decks first, or the last one started alone decides the wall clock
        names = sorted(names, key=lambda n: -last_seconds(n.partition(":")[0]))
        done = []

        def job(spec: str) -> tuple:
            name, _, sl = spec.partition(":")
            tag = f"{args.tag}-s{sl}" if args.tag and sl else args.tag
            return name, args.iter, args.flow, sl or args.slides, tag, not args.no_cache

        with ProcessPoolExecutor(max(1, min(args.jobs, len(names)))) as pool:
            futs = [pool.submit(run_one, *job(n)) for n in names]
            for f in as_completed(futs):
                done.append(f.result())
                print(line(done[-1]), flush=True)
        ok = [r for r in done if r.get("bootstrap_mean")]
        if ok:
            n = sum(r["n"] for r in ok)
            mean = {m: sum(r["bootstrap_mean"][m] * r["n"] for r in ok) / n for m in ("boxes", "page", "pixels")}
            print(f"\n{len(ok)}/{len(done)} decks, {n} slides ({sum(1 for r in done if r.get('cached'))} cached): "
                  f"boxes {mean['boxes']:.4f} page {mean['page']:.4f} pixels {mean['pixels']:.4f}")
        loops = [r["loop"] for r in done if r.get("loop", {}).get("open")]
        if loops:
            worse = sum(1 for lo in loops if lo["open"][-1] > lo["open"][0])
            print(f"converged {sum(1 for lo in loops if lo['converged'])}/{len(loops)}; open residuals "
                  f"{sum(lo['open'][0] for lo in loops)} -> {sum(lo['open'][-1] for lo in loops)}; "
                  f"{worse} deck(s) worse after the loop")
    elif args.cmd == "losses":
        losses(args.tag, args.top)
    else:
        report(args.tag)


if __name__ == "__main__":
    main()
