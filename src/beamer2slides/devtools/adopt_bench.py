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
from collections.abc import Callable, Generator
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import TypedDict

import numpy as np
from PIL import Image

from beamer2slides.arrays import RGB, Mask, SignedRGB
from beamer2slides.deck_files import DeckFiles
from beamer2slides.json_types import (Json, JsonObject, JsonShapeError, as_array, as_int, as_object, as_objects,
                                      as_optional_str, as_str)
from beamer2slides.paths import CHECKOUT
# The scores live in the package: pull's frame guard (`frame_guard`) scores its rounds with them too,
# and the package never imports devtools.
from beamer2slides.page_score import covered_mask, element_boxes, ink_scores, overlap, page_ground  # noqa: F401

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


# ------------------------------------------------------------------------------------------ results
# A run's result.json, as `run_one` writes it and `report`/`losses` read it back (`bench_result`).
# The keys a result gains on its way (a crash, a cache hit, the loop) are the optional ones; results
# from before `offline` or the frame guard (`open_final`, `restored`) still read.

class Loss(TypedDict):
    """An element's share of its slide's 1 - boxes (`element_losses`)."""
    id: str | None
    kind: str
    role: str | None
    shape: str | None
    font: str | None
    text: str
    loss: float
    miss: float
    extra: float


class PageScore(TypedDict):
    """`score_page`: the ink scores of one page and where its `boxes` went."""
    boxes: float
    page: float
    pixels: float
    losses: list[Loss]


class _SlideScoreKeys(TypedDict):
    slide: int
    boxes: float
    page: float
    pixels: float


class SlideScore(_SlideScoreKeys, total=False):
    """One slide of `score_pdf`: `losses` for a page scored, `missing` for one the PDF lacks."""
    losses: list[Loss]
    missing: bool


class Means(TypedDict):
    boxes: float
    page: float
    pixels: float


class FrameError(TypedDict):
    frame: int
    error: str


class _LoopKeys(TypedDict):
    iterations: list[JsonObject]
    converged: bool
    unresolved: int


class LoopReport(_LoopKeys, total=False):
    """What `--iter N` left: the open residuals round by round, and what the frame guard put back."""
    open: list[int]
    open_final: int
    restored: list[JsonObject]


class _BenchKeys(TypedDict):
    deck: str
    tag: str
    iters: int
    flow: bool
    slides: str | None


class BenchResult(_BenchKeys, total=False):
    offline: bool
    n: int
    bootstrap_s: float
    frame_errors: list[FrameError]
    compile_s: float
    bootstrap: list[SlideScore]
    full_s: float
    cached: bool
    loop: LoopReport
    converged: list[SlideScore]
    error: str
    traceback: str
    seconds: float
    bootstrap_mean: Means
    converged_mean: Means


class CachedScore(TypedDict, total=False):
    """What a bootstrap score's cache entry keeps (`store`), put back into a result on a hit."""
    bootstrap: list[SlideScore]
    frame_errors: list[FrameError]
    full_s: float


def number(v: Json, where: str) -> float:
    """A JSON number as it is (an int stays an int, so what is written back is what was read)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise JsonShapeError(f"{where}: a number, not {v!r}")
    return v


def flag(v: Json, where: str) -> bool:
    if not isinstance(v, bool):
        raise JsonShapeError(f"{where}: true or false, not {v!r}")
    return v


def loss(o: JsonObject) -> Loss:
    return {"id": as_optional_str(o.get("id"), "a loss's id"), "kind": as_str(o["kind"], "a loss's kind"),
            "role": as_optional_str(o.get("role"), "a loss's role"),
            "shape": as_optional_str(o.get("shape"), "a loss's shape"),
            "font": as_optional_str(o.get("font"), "a loss's font"), "text": as_str(o["text"], "a loss's text"),
            "loss": number(o["loss"], "a loss"), "miss": number(o["miss"], "a loss's miss"),
            "extra": number(o["extra"], "a loss's extra")}


def slide_score(o: JsonObject) -> SlideScore:
    s: SlideScore = {"slide": as_int(o["slide"], "a score's slide"), "boxes": number(o["boxes"], "boxes"),
                     "page": number(o["page"], "page"), "pixels": number(o["pixels"], "pixels")}
    if "losses" in o:
        s["losses"] = [loss(e) for e in as_objects(o["losses"], "a slide's losses")]
    if "missing" in o:
        s["missing"] = flag(o["missing"], "a slide's missing")
    return s


def slide_scores(v: Json, where: str) -> list[SlideScore]:
    return [slide_score(s) for s in as_objects(v, where)]


def means(v: Json) -> Means:
    o = as_object(v, "a result's means")
    return {"boxes": number(o["boxes"], "mean boxes"), "page": number(o["page"], "mean page"),
            "pixels": number(o["pixels"], "mean pixels")}


def frame_errors(v: Json) -> list[FrameError]:
    return [{"frame": as_int(f["frame"], "a broken frame"), "error": as_str(f["error"], "a frame's error")}
            for f in as_objects(v, "a result's frame errors")]


def loop_report(v: Json) -> LoopReport:
    o = as_object(v, "a result's loop")
    lo: LoopReport = {"iterations": as_objects(o["iterations"], "the loop's iterations"),
                      "converged": flag(o["converged"], "the loop's converged"),
                      "unresolved": as_int(o["unresolved"], "the loop's unresolved")}
    if "open" in o:
        lo["open"] = [as_int(n, "the loop's open") for n in as_array(o["open"], "the loop's open")]
    if "open_final" in o:
        lo["open_final"] = as_int(o["open_final"], "the loop's open_final")
    if "restored" in o:
        lo["restored"] = as_objects(o["restored"], "the loop's restored")
    return lo


def cached_score(o: JsonObject) -> CachedScore:
    """A cache entry as `store` wrote it, in its order."""
    out: CachedScore = {}
    for key, v in o.items():
        if key == "bootstrap":
            out["bootstrap"] = slide_scores(v, "a cached score")
        elif key == "frame_errors":
            out["frame_errors"] = frame_errors(v)
        elif key == "full_s":
            out["full_s"] = number(v, "a cached score's full_s")
    return out


def bench_result(o: JsonObject) -> BenchResult:
    """A result.json read back; keys no run writes any more are left out."""
    slides = o.get("slides")
    res: BenchResult = {"deck": as_str(o["deck"], "a result's deck"), "tag": as_str(o["tag"], "a result's tag"),
                        "iters": as_int(o["iters"], "a result's iters"), "flow": flag(o["flow"], "a result's flow"),
                        "slides": as_optional_str(slides, "a result's slides")}
    for key, v in o.items():
        if key == "offline":
            res["offline"] = flag(v, "a result's offline")
        elif key == "n":
            res["n"] = as_int(v, "a result's n")
        elif key in ("bootstrap_s", "compile_s", "full_s", "seconds"):
            t = number(v, f"a result's {key}")
            if key == "bootstrap_s":
                res["bootstrap_s"] = t
            elif key == "compile_s":
                res["compile_s"] = t
            elif key == "full_s":
                res["full_s"] = t
            else:
                res["seconds"] = t
        elif key == "frame_errors":
            res["frame_errors"] = frame_errors(v)
        elif key == "bootstrap":
            res["bootstrap"] = slide_scores(v, "a result's bootstrap")
        elif key == "converged":
            res["converged"] = slide_scores(v, "a result's converged")
        elif key == "cached":
            res["cached"] = flag(v, "a result's cached")
        elif key == "loop":
            res["loop"] = loop_report(v)
        elif key == "error":
            res["error"] = as_str(v, "a result's error")
        elif key == "traceback":
            res["traceback"] = as_str(v, "a result's traceback")
        elif key == "bootstrap_mean":
            res["bootstrap_mean"] = means(v)
        elif key == "converged_mean":
            res["converged_mean"] = means(v)
    return res


def read_result(path: Path) -> BenchResult:
    return bench_result(as_object(json.loads(path.read_text(encoding="utf-8")), str(path)))


# ------------------------------------------------------------------------------------------ capture

def capture(pid: str, name: str, refresh: bool) -> Path:
    """Read deck `pid` once: presentation.json, slides/NNN.png (Google's LARGE thumbnails), and
    target.json = `deck_ir(foreign=True)` with its pictures in images/. Nothing is written to the deck."""
    from beamer2slides.deck_ir import fetch_url
    from beamer2slides.google_auth import credentials, slides_service
    from beamer2slides.google_types import as_json
    from beamer2slides.gslides import execute, save_thumbnail
    folder = CORPUS / name
    folder.mkdir(parents=True, exist_ok=True)
    # Always read again: picture contentUrls expire within the hour, so a cached answer can no
    # longer fetch its pictures (403). Thumbnails are kept unless --refresh.
    pres = as_json(execute(slides_service().presentations().get(presentationId=pid)), pid)
    (folder / "presentation.json").write_text(json.dumps(pres, indent=1), encoding="utf-8")
    creds = credentials()

    def thumb(item: tuple[int, JsonObject]) -> None:
        i, s = item
        path = folder / "slides" / f"{i:03d}.png"
        if refresh or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            for attempt in range(8):           # thumbnails are "expensive reads": 60 a minute per user
                try:
                    save_thumbnail(slides_service(creds), pid, as_str(s["objectId"], "a slide's id"), path)
                    return
                except Exception as exc:                          # noqa: BLE001
                    if "429" not in str(exc) or attempt == 7:
                        raise
                    time.sleep(20 + 10 * attempt)

    pages = as_objects(pres.get("slides", []), f"{name}'s slides")
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(thumb, enumerate(pages, 1)))

    def fetch(url: str) -> bytes:
        return fetch_url(url, None)
    write_target(folder, pres, fetch)
    print(f"{name}: {pres.get('title')!r}, {len(pages)} slides -> {folder}")
    return folder


Fetch = Callable[[str], bytes]


def build_target(folder: Path, pres: JsonObject | None, fetch: Fetch | None) -> JsonObject:
    """The IR adopt reads, from the cached presentation (`pres`: that presentation, already read).
    With `fetch` (capture) pictures are downloaded and their URLs recorded in urls.json; without it
    (every run) they come from that cache, and a URL capture never saw fails the way an expired one
    does in `adopt`."""
    import hashlib
    from beamer2slides.deck_ir import deck_ir
    saved = folder / "presentation.json"
    pres = pres or as_object(json.loads(saved.read_text(encoding="utf-8")), str(saved))
    known_path = folder / "urls.json"
    known: dict[str, str] = {url: as_str(sha, str(known_path)) for url, sha in
                             as_object(json.loads(known_path.read_text(encoding="utf-8")), str(known_path)).items()} \
        if known_path.exists() else {}
    get: Fetch
    if fetch is None:
        cache = {p.stem: p for p in (folder / "images").glob("*")} if (folder / "images").is_dir() else {}

        def cached(url: str) -> bytes:
            sha = known.get(url)
            if sha and sha[:16] in cache:
                return cache[sha[:16]].read_bytes()
            raise OSError("not captured")
        get = cached
    else:
        fetched = fetch

        def fetching(url: str) -> bytes:
            data = fetched(url)
            known[url] = hashlib.sha1(data).hexdigest()
            return data
        get = fetching

    def thumbnail(n: int) -> Path | None:
        # the LARGE thumbnails `capture` saved: fills the API reads as empty come from them, as live
        # `adopt` reads them through `deck_ir.slide_thumbnails`
        path = folder / "slides" / f"{n + 1:03d}.png"
        return path if path.exists() else None

    target = deck_ir(pres, None, None, get, folder / "images", True, thumbnail)
    if fetch is not None:
        known_path.write_text(json.dumps(known, indent=0), encoding="utf-8")
    return target


def write_target(folder: Path, pres: JsonObject | None, fetch: Fetch | None) -> JsonObject:
    target = build_target(folder, pres, fetch)
    (folder / "target.json").write_text(json.dumps(target, indent=1), encoding="utf-8")
    return target


# -------------------------------------------------------------------------------------------- score

def element_losses(m_ref: Mask, m_got: Mask, slide: JsonObject, top: int) -> list[Loss]:
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
    elements = as_objects(slide["elements"], "a slide's elements")
    n = len(elements)
    lm = np.bincount(label[miss], minlength=n)[:n]
    le = np.bincount(label[extra], minlength=n)[:n]
    out: list[Loss] = []
    for k in np.argsort(-(lm + le))[:top]:
        if lm[k] + le[k] == 0:
            break
        el = elements[k]
        runs = [r for p in as_objects(el.get("paragraphs", []), "an element's paragraphs")
                for r in as_objects(p["runs"], "a paragraph's runs")]
        out.append({"id": as_optional_str(el.get("id"), "an element's id"), "kind": as_str(el["kind"], "a kind"),
                    "role": as_optional_str(el.get("role"), "an element's role"),
                    "shape": as_optional_str(el.get("shape_type"), "an element's shape"),
                    "font": as_optional_str(runs[0].get("font"), "a run's font") if runs else None,
                    "text": "".join(as_str(r["text"], "a run's text") for r in runs)[:40],
                    "loss": round(float((lm[k] + le[k]) / total), 4),
                    "miss": round(float(lm[k] / total), 4), "extra": round(float(le[k] / total), 4)})
    return out


def score_page(ref: SignedRGB, got: SignedRGB, slide: JsonObject) -> tuple[PageScore, RGB]:
    h, w = ref.shape[:2]
    ink, m_ref, m_got = ink_scores(ref, got, slide)
    scores: PageScore = {"boxes": ink["boxes"], "page": ink["page"], "pixels": ink["pixels"],
                         "losses": element_losses(m_ref, m_got, slide, 6)}
    d = np.full((h, w, 3), 255, dtype=np.uint8)
    d[m_ref & ~m_got] = (220, 40, 40)
    d[m_got & ~m_ref] = (30, 110, 230)
    d[m_ref & m_got] = (0, 0, 0)
    return scores, d


def score_pdf(pdf: Path, folder: Path, target: JsonObject, sheets: Path | None) -> list[SlideScore]:
    from beamer2slides.fidelity import rgb_array
    from beamer2slides.pdf import Document
    refs = sorted((folder / "slides").glob("*.png"))
    slides = as_objects(target["slides"], "the target's slides")
    doc = Document(pdf)
    out: list[SlideScore] = []
    try:
        for i, ref_path in enumerate(refs):
            if i >= len(slides):
                break
            ref_img = Image.open(ref_path).convert("RGB")
            w, h = ref_img.size
            ref = rgb_array(ref_img)
            if i >= len(doc):
                out.append({"slide": i + 1, "boxes": 0.0, "page": 0.0, "pixels": 0.0, "missing": True})
                continue
            got_img = Image.fromarray(doc[i].render(w / doc[i].width)).convert("RGB").resize((w, h))
            got = rgb_array(got_img)
            scores, diff = score_page(ref, got, slides[i])
            out.append({"slide": i + 1, "boxes": scores["boxes"], "page": scores["page"], "pixels": scores["pixels"],
                        "losses": scores["losses"]})
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

OFFLINE = "deck-files"          # a capture as `deck-files` saves a deck (devtools/grind.py files)


def refuse(url: str) -> bytes:
    raise PermissionError(f"offline: no download ({url})")


@contextmanager
def sandbox(folder: Path) -> Generator[DeckFiles, None, None]:
    """What a sandbox handed the deck's files has, for the block: the recordings of `folder`'s
    deck-files answering every download and nothing else (no network), no font of this machine's,
    and a google/fonts cache only those recordings ever filled (kept per deck, so a second run does
    not cut the same variable fonts again)."""
    from beamer2slides import adopt, deck_files, google_auth
    files = deck_files.DeckFiles.load(folder / OFFLINE)
    with deck_files._environ(B2S_FONT_CACHE=str(folder / "offline-font-cache"), B2S_FONTS=None,
                             B2S_FONT_FETCH=None, B2S_FONT_SOURCE=None), \
            google_auth.use_fetcher(refuse), deck_files.replaying(files), adopt.no_machine_fonts():
        yield files


def quiet(_: str) -> None:
    pass


def load_target(folder: Path, slides: str | None, run: Path | None, files: DeckFiles | None) -> JsonObject:
    """The IR as *this* code reads the cached presentation (so a deck_ir change shows in the run),
    pictures from the capture's cache; written to the run folder (`run`), never over the corpus.
    With `files` (inside `sandbox`), read as `adopt --deck DIR` reads them: pictures from the
    recording. `slides`: a-b, a slide range of the deck."""
    if files is not None:
        from beamer2slides.deck_ir import given_thumbnails, read_presentation
        pres = as_object(json.loads(files.presentation.read_text(encoding="utf-8")), str(files.presentation))
        thumbs, _ = given_thumbnails(pres, files.thumbnails, quiet)
        from beamer2slides.deck_ir_types import target_json
        target = target_json(read_presentation(pres, (run or folder) / "target-images", None, None, thumbs, False))
        if run is not None:
            (run / "target.json").write_text(json.dumps(target, indent=1), encoding="utf-8")
    elif (folder / "presentation.json").exists():
        target = build_target(folder, None, None)
        if run is not None:
            (run / "target.json").write_text(json.dumps(target, indent=1), encoding="utf-8")
    else:
        saved = folder / "target.json"
        target = as_object(json.loads(saved.read_text(encoding="utf-8")), str(saved))
    if slides:
        a, _, b = slides.partition("-")
        lo, hi = int(a) - 1, int(b or a)
        target["slides"] = as_array(target["slides"], "the target's slides")[lo:hi]
        target["first_slide"] = lo
    return target


def run_one(name: str, iters: int, flow: bool, slides: str | None, tag: str | None, cache: bool, guard: bool,
            blind: bool, offline: bool) -> BenchResult:
    """Bootstrap (and optionally converge) one corpus deck and score it. Never raises: a crash or a
    compile error is the result, since finding those is half the point.

    The loop gets the thumbnails as a live adopt does (the target carries their paths, `deck_ir`), so
    its frame guard scores by ink; `blind` hides them (the guard falls back to residuals and words,
    as pull without thumbnails), `guard=False` runs the loop with no guard at all. `offline` reads the
    deck's files (`OFFLINE`) in a `sandbox`: no network, no font of this machine's, as a harness does."""
    folder = CORPUS / name
    tag = tag or ("flow" if flow else "abs") + (f"-it{iters}" if iters else "") + (f"-s{slides}" if slides else "") \
        + ("-off" if offline else "")
    run = folder / "runs" / tag
    shutil.rmtree(run, ignore_errors=True)
    run.mkdir(parents=True)
    res: BenchResult = {"deck": name, "tag": tag, "iters": iters, "flow": flow, "slides": slides, "offline": offline}
    t0 = time.perf_counter()
    if offline and not (folder / OFFLINE / "presentation.json").exists():
        res["error"] = f"offline: no {OFFLINE}/ in {folder} (devtools/grind.py files {name})"
        return finish(run, res, t0)
    with sandbox(folder) if offline else nullcontext() as files:
        return _run(name, folder, run, res, t0, iters, flow, slides, cache, guard, blind, files)


def _run(name: str, folder: Path, run: Path, res: BenchResult, t0: float, iters: int, flow: bool, slides: str | None,
         cache: bool, guard: bool, blind: bool, files: DeckFiles | None) -> BenchResult:
    from beamer2slides import adopt
    from beamer2slides.compare import TOL
    from beamer2slides.inverse import Workspace, converge
    try:
        target = load_target(folder, slides, run, files)
        n = res["n"] = len(as_array(target["slides"], "the target's slides"))
        tex = run / "tree" / "main.tex"
        adopt.bootstrap(target, tex, flow, None)
        res["bootstrap_s"] = round(time.perf_counter() - t0, 1)
        key = cache_key(tex.parent, target)
        hit = CORPUS / name / "cache" / key
        if cache and not iters and (hit / "result.json").exists():
            # the same source scored by the same scorer: its scores and sheets, no compile
            kept = cached_score(as_object(json.loads((hit / "result.json").read_text(encoding="utf-8")), str(hit)))
            if "bootstrap" in kept:
                res["bootstrap"] = kept["bootstrap"]
            if "frame_errors" in kept:
                res["frame_errors"] = kept["frame_errors"]
            if "full_s" in kept:
                res["full_s"] = kept["full_s"]
            res["cached"] = True
            if (hit / "sheets").is_dir():
                shutil.copytree(hit / "sheets", run / "sheets")
            return finish(run, res, t0)
        ws = Workspace(tex, run / "work")
        pdf, err = ws.compile()
        if pdf is None:
            # One broken frame should not hide the other slides: find the frames that do not compile
            # on their own, blank them, and score the rest (the errors are findings of their own).
            broken = res["frame_errors"] = broken_frames(tex, run / "frames", 4)
            if broken:
                blank_frames(tex, [f["frame"] for f in broken])
                ws = Workspace(tex, run / "work")
                pdf, err = ws.compile()
        res["compile_s"] = round(time.perf_counter() - t0, 1)
        if pdf is None:
            res["error"] = "compile: " + err[-1500:]
            return finish(run, res, t0)
        if slides:                               # thumbnails are numbered from the deck's first slide
            folder_view = run / "refs"
            (folder_view / "slides").mkdir(parents=True, exist_ok=True)
            lo = as_int(target["first_slide"], "the target's first slide")
            for k in range(n):
                shutil.copyfile(folder / "slides" / f"{lo + k + 1:03}.png", folder_view / "slides" / f"{k + 1:03}.png")
        else:
            folder_view = folder
        res["bootstrap"] = score_pdf(pdf, folder_view, target, run / "sheets")
        kept = CachedScore(bootstrap=res["bootstrap"])
        if "frame_errors" in res:
            kept["frame_errors"] = res["frame_errors"]
        kept["full_s"] = round(time.perf_counter() - t0, 1)
        store(hit, run, kept)
        if iters:
            result = converge(tex, target, run / "loop", max_iter=iters, handout=False, engine=None, tol=TOL,
                              log=quiet, guard=guard, thumbnails=False if blind else None)
            # the promise is convergence, so say it per deck: did it, and did the rounds bring the
            # open residuals down or up (the saudi-cats deck went 127 -> 138 and nothing said so)
            res["loop"] = {"iterations": result.iterations, "converged": result.converged,
                           "unresolved": len(result.unresolved),
                           "open": [as_int(i["open"], "an iteration's open") for i in result.iterations],
                           "open_final": len(result.residuals), "restored": result.restored}
            final = run / "loop" / "build" / "main.pdf"
            if final.exists():
                res["converged"] = score_pdf(final, folder_view, target, run / "sheets-loop")
    except Exception as exc:                                     # noqa: BLE001 - the crash is the finding
        res["error"] = f"{type(exc).__name__}: {exc}"[:1500]
        res["traceback"] = traceback.format_exc()[-4000:]
    return finish(run, res, t0)


CACHE_KEEP = 6          # results kept per deck, newest first


def cache_key(tree: Path, target: JsonObject) -> str:
    """What a bootstrap score depends on: every file of the source tree, the IR the boxes come from,
    and the scoring code. A change that leaves a deck's source alone then costs that deck no compile."""
    import hashlib
    h = hashlib.sha256()
    for p in sorted(q for q in tree.rglob("*") if q.is_file()):
        h.update(p.relative_to(tree).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    h.update(json.dumps(target, sort_keys=True, default=str).encode())
    from beamer2slides import fidelity, page_score
    for code in (Path(__file__), Path(fidelity.__file__), Path(page_score.__file__)):
        h.update(code.read_bytes())
    return h.hexdigest()[:24]


def store(hit: Path, run: Path, result: CachedScore) -> None:
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


def broken_frames(tex: Path, work: Path, jobs: int) -> list[FrameError]:
    """The frames that do not compile on their own, with TeX's error for each (`jobs` compiles at once)."""
    from beamer2slides.inverse import Workspace, copy_tree
    head, frames, tail = split_frames(tex.read_text(encoding="utf-8"))

    def one(item: tuple[int, str]) -> FrameError | None:
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


def mean_scores(scores: list[SlideScore]) -> Means:
    return {"boxes": round(sum(s["boxes"] for s in scores) / len(scores), 3),
            "page": round(sum(s["page"] for s in scores) / len(scores), 3),
            "pixels": round(sum(s["pixels"] for s in scores) / len(scores), 3)}


def finish(run: Path, res: BenchResult, t0: float) -> BenchResult:
    res["seconds"] = round(time.perf_counter() - t0, 1)
    bootstrap = res.get("bootstrap")
    if bootstrap:
        res["bootstrap_mean"] = mean_scores(bootstrap)
    converged = res.get("converged")
    if converged:
        res["converged_mean"] = mean_scores(converged)
    (run / "result.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def line(res: BenchResult) -> str:
    error = res.get("error")
    if error:
        return f"{res['deck']:<24} {res['tag']:<14} ERROR {error.splitlines()[0][:110]}"
    b = res.get("bootstrap_mean")
    c = res.get("converged_mean")
    broken_list = res.get("frame_errors")
    if broken_list:
        broken = f" [{len(broken_list)} frames broken: " + \
                 ",".join(str(f["frame"]) for f in broken_list[:8]) + "]"
    else:
        broken = ""
    tail = f"  loop -> boxes {c['boxes']:.3f} page {c['page']:.3f} pixels {c['pixels']:.3f}" if c else ""
    loop = res.get("loop")
    opened = loop.get("open") if loop else None
    if loop and opened:
        tail += "  " + ("CONVERGED" if loop["converged"] else "open " + " -> ".join(map(str, opened)))
        restored = loop.get("restored")
        if restored:
            tail += f"  guard put back {len(restored)} (open {loop.get('open_final')})"
    return (f"{res['deck']:<24} {res['tag']:<14} {res.get('n', 0):3} slides {res.get('seconds', 0.0):6.1f}s  "
            f"boxes {b['boxes'] if b else 0:.3f} page {b['page'] if b else 0:.3f} "
            f"pixels {b['pixels'] if b else 0:.3f}{tail}{broken}")


def last_seconds(name: str) -> float:
    """How long the deck's newest run took (0 when it never ran)."""
    runs: list[Path] = [p / "result.json" for p in (CORPUS / name / "runs").glob("*")] \
        if (CORPUS / name / "runs").is_dir() else []
    runs = [p for p in runs if p.exists()]
    if not runs:
        return 0.0
    newest = max(runs, key=lambda p: p.stat().st_mtime)
    res = as_object(json.loads(newest.read_text(encoding="utf-8")), str(newest))
    return float(number(res.get("full_s") or res.get("seconds") or 0, str(newest)))


def decks() -> list[str]:
    return sorted(p.name for p in CORPUS.iterdir() if (p / "presentation.json").exists()) if CORPUS.is_dir() else []


def built(rows: list[BenchResult]) -> tuple[int, int, Means]:
    """(decks built, their slides, the corpus means weighted by slides) of the rows with a score."""
    ok = [(r.get("n", 0), m) for r in rows if (m := r.get("bootstrap_mean"))]
    n = sum(k for k, _ in ok)
    return len(ok), n, {"boxes": sum(m["boxes"] * k for k, m in ok) / n if n else 0.0,
                        "page": sum(m["page"] * k for k, m in ok) / n if n else 0.0,
                        "pixels": sum(m["pixels"] * k for k, m in ok) / n if n else 0.0}


def report(tag: str, worst: int) -> None:
    rows: list[BenchResult] = []
    for name in decks():
        path = CORPUS / name / "runs" / tag / "result.json"
        if path.exists():
            rows.append(read_result(path))

    def boxes(r: BenchResult) -> float:
        m = r.get("bootstrap_mean")
        return m["boxes"] if m else -1
    for r in sorted(rows, key=boxes):
        print(line(r))
        for s in sorted(r.get("bootstrap", []), key=lambda s: s["boxes"])[:worst]:
            print(f"    slide {s['slide']:3}: boxes {s['boxes']:.2f} page {s['page']:.2f} pixels {s['pixels']:.2f}")
    ok, n, mean = built(rows)
    if ok:
        print(f"\n{ok}/{len(rows)} decks built, {n} slides: boxes {mean['boxes']:.3f} "
              f"page {mean['page']:.3f} pixels {mean['pixels']:.3f}")


class Charged(TypedDict):
    """An element's loss with the slide it was charged on (`losses`)."""
    loss: Loss
    deck: str
    slide: int


def losses(tag: str, top: int) -> None:
    """Where the corpus' `boxes` score goes, element by element (`element_losses`): each element's
    loss as points of the corpus mean (its slide's share / all slides), the worst first, then summed
    by deck, by kind and by font. `miss` = the deck's ink we lack, `extra` = ink of ours it lacks;
    both at once is usually the same ink set in the wrong place."""
    items: list[Charged] = []
    n = 0
    for name in decks():
        path = CORPUS / name / "runs" / tag / "result.json"
        if not path.exists():
            continue
        res = read_result(path)
        bootstrap = res.get("bootstrap")
        n += res.get("n", 0) if bootstrap else 0
        for s in bootstrap or []:
            for e in s.get("losses", []):
                items.append({"loss": e, "deck": name, "slide": s["slide"]})
    if not n:
        print(f"no results with losses under tag {tag!r}")
        return

    def pts(v: float) -> float:                                       # thousandths
        return 1000 * v / n

    def kind_of(c: Charged) -> str:
        shape = c["loss"]["shape"]
        return c["loss"]["kind"] + ("/" + shape if shape else "")

    def deck_of(c: Charged) -> str:
        return c["deck"]

    def font_of(c: Charged) -> str:
        return c["loss"]["font"] or "-"
    print(f"{n} slides; losses in thousandths of the corpus mean boxes score (1 = 0.001)\n")
    print(f"{'pts':>6} {'miss':>5} {'extra':>5}  {'deck':<22} {'sl':>3}  {'kind':<14} {'font':<18} text")
    for c in sorted(items, key=lambda c: -c["loss"]["loss"])[:top]:
        e = c["loss"]
        kind = kind_of(c)
        print(f"{pts(e['loss']):6.2f} {pts(e['miss']):5.2f} {pts(e['extra']):5.2f}  {c['deck']:<22} "
              f"{c['slide']:>3}  {kind[:14]:<14} {(e['font'] or '')[:18]:<18} {e['text']!r}")
    keys: tuple[tuple[str, Callable[[Charged], str]], ...] = (("deck", deck_of), ("kind", kind_of), ("font", font_of))
    for title, keyf in keys:
        agg: dict[str, tuple[float, float, float, int]] = {}
        for c in items:
            e = c["loss"]
            lo, mi, ex, count = agg.get(keyf(c), (0.0, 0.0, 0.0, 0))
            agg[keyf(c)] = (lo + e["loss"], mi + e["miss"], ex + e["extra"], count + 1)
        print(f"\nby {title}:")
        for k, (lo, mi, ex, count) in sorted(agg.items(), key=lambda kv: -kv[1][0])[:15]:
            print(f"  {pts(lo):6.2f}  miss {pts(mi):5.2f} extra {pts(ex):5.2f}  {count:5} elements  {k}")
    print(f"\ntotal charged: {pts(sum(c['loss']['loss'] for c in items)):.1f} (top {6} elements per slide)")


def main(argv: list[str] | None) -> None:
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
    r.add_argument("--no-guard", action="store_true", help="--iter without the loop's frame guard")
    r.add_argument("--blind", action="store_true", help="--iter with the thumbnails hidden from the loop: "
                                                        "the frame guard judges by residuals and words")
    r.add_argument("--offline", action="store_true", help="read each deck's deck-files/ as a sandbox does: no "
                                                          "network, no font of this machine's (devtools/grind.py files)")
    p = sub.add_parser("report")
    p.add_argument("--tag", default="abs")
    lo = sub.add_parser("losses", help="where the boxes score goes, by element, deck, kind and font")
    lo.add_argument("--tag", default="abs")
    lo.add_argument("--top", type=int, default=40)
    args = ap.parse_args(argv)
    if args.cmd == "capture":
        if args.manifest:
            for k, d in enumerate(as_objects(json.loads(MANIFEST.read_text(encoding="utf-8")), str(MANIFEST))):
                if args.shard and k % args.shard[1] != args.shard[0]:
                    continue
                name = as_str(d["name"], "a manifest deck's name")
                if not args.refresh and (CORPUS / name / "target.json").exists():
                    continue
                try:
                    capture(as_str(d["id"], "a manifest deck's id"), name, args.refresh)
                except Exception as exc:                          # noqa: BLE001
                    print(f"{name}: {type(exc).__name__}: {exc}", file=sys.stderr)
        else:
            capture(args.id, args.name, args.refresh)
    elif args.cmd == "target":
        for name in args.names or decks():
            write_target(CORPUS / name, None, None)
            print(f"{name}: target.json rebuilt")
    elif args.cmd == "run":
        names: list[str] = args.names + (micro_specs() if args.micro else []) or decks()
        # the slowest decks first, or the last one started alone decides the wall clock
        names = sorted(names, key=lambda n: -last_seconds(n.partition(":")[0]))
        done: list[BenchResult] = []

        with ProcessPoolExecutor(max(1, min(args.jobs, len(names)))) as pool:
            def submit(spec: str) -> Future[BenchResult]:
                name, _, sl = spec.partition(":")
                tag = f"{args.tag}-s{sl}" if args.tag and sl else args.tag
                return pool.submit(run_one, name, args.iter, args.flow, sl or args.slides, tag, not args.no_cache,
                                   not args.no_guard, args.blind, args.offline)

            futs = [submit(n) for n in names]
            for f in as_completed(futs):
                done.append(f.result())
                print(line(done[-1]), flush=True)
        ok, n, mean = built(done)
        if ok:
            print(f"\n{ok}/{len(done)} decks, {n} slides ({sum(1 for r in done if r.get('cached'))} cached): "
                  f"boxes {mean['boxes']:.4f} page {mean['page']:.4f} pixels {mean['pixels']:.4f}")
        loops = [(lo, opened) for r in done if (lo := r.get("loop")) and (opened := lo.get("open"))]
        if loops:
            # what the loop left, after the guard put frames back (the last round's count before it)
            end = [lo.get("open_final", opened[-1]) if lo.get("restored") else opened[-1] for lo, opened in loops]
            worse = sum(1 for (_, opened), e in zip(loops, end) if e > opened[0])
            print(f"converged {sum(1 for lo, _ in loops if lo['converged'])}/{len(loops)}; open residuals "
                  f"{sum(opened[0] for _, opened in loops)} -> {sum(end)}; "
                  f"{worse} deck(s) worse after the loop")
        # what the objective asks: did the loop leave any slide's ink below its first draft's
        pairs: list[tuple[float, float]] = []
        for r in done:
            first: list[SlideScore] = r.get("bootstrap", [])
            pairs += [(b["boxes"], c["boxes"]) for b, c in zip(first, r.get("converged", []))]
        if pairs:
            print(f"loop ink: boxes {sum(b for b, _ in pairs) / len(pairs):.4f} -> {sum(c for _, c in pairs) / len(pairs):.4f} "
                  f"over {len(pairs)} slides; {sum(1 for b, c in pairs if c < b)} slide(s) below their "
                  f"first draft, {sum(1 for b, c in pairs if c > b)} above; guard put back "
                  f"{sum(len(lo.get('restored') or []) for r in done if (lo := r.get('loop')))} frame(s)")
    elif args.cmd == "losses":
        losses(args.tag, args.top)
    else:
        report(args.tag, 3)


if __name__ == "__main__":
    main(None)
