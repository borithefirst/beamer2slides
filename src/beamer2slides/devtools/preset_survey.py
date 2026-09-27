"""Phase 1 survey: how often does `adopt_shapes`' *default*-adjustment geometry for an adjustable
preset (`PARALLELOGRAM`, `CHEVRON`, `STAR_5`, a rounded rectangle...) disagree with what a foreign
deck's own thumbnail shows in that shape's box? The API never gives the adjustment a person
dragged (`preset_geometry`'s module docstring); this walks the whole adopt corpus and hunt decks
and scores every adjustable-preset shape on every slide (`preset_geometry.match_score`), offline,
against the thumbnails and presentation.json each deck's `deck-files/` already holds - no Google
call, no network.

  run [--roots DIR...] [--out FILE]   survey every deck under out/adopt-corpus and out/adopt-hunt
                                      (or the folders named) -> out/grind/analysis/presets/survey.json
  show FILE                          the per-preset summary of a saved survey.json, worst first

A result: {"kind", "iou" (intersection over union of our default polygon and the thumbnail's own
fill-coloured, visible pixels in the box), "p_in", "p_out", "n_true", "n_box", "deck", "slide",
"object", "inherited" (the layout/master object id a shape without one drew from, so a single
decoration used on every slide is not mistaken for many different disagreements)}. "disagrees"
counts a result whose iou is below DISAGREE_IOU.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

# This survey only reads a deck's own saved files (deck-files/): never a picture download.
os.environ.setdefault("B2S_NO_DOWNLOADS", "1")

from beamer2slides import deck_fills
from beamer2slides.devtools import preset_geometry as pg
from beamer2slides.deck_files import DeckFiles
from beamer2slides.deck_ir import dim, given_thumbnails, read_presentation
from beamer2slides.paths import CHECKOUT

DEFAULT_ROOTS = [CHECKOUT / "out" / "adopt-corpus", CHECKOUT / "out" / "adopt-hunt"]
DEFAULT_OUT = CHECKOUT / "out" / "grind" / "analysis" / "presets" / "survey.json"
DISAGREE_IOU = 0.5    # a result below this is counted as a clear disagreement, not a rounding blur


def deck_dirs(roots: list[Path]):
    """Every deck folder under `roots` that has files a `deck_files.DeckFiles` can load (a deck the
    corpus or the hunt saved with `deck-files`), name first."""
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in seen:
                continue
            if (d / "deck-files" / "presentation.json").is_file():
                seen.add(d.name)
                yield d


def survey_deck(deck_dir: Path, images_dir: Path) -> list[dict]:
    """Every adjustable-preset shape `preset_geometry.match_score` could judge, across every slide
    of the deck at `deck_dir` (its `deck-files/`), scored against that slide's own thumbnail."""
    files = DeckFiles.load(deck_dir / "deck-files")
    pres = json.loads(files.presentation.read_text(encoding="utf-8"))
    thumbnails, shots = given_thumbnails(pres, files.thumbnails, log=lambda *a: None)
    if not shots:
        return []
    target = read_presentation(pres, images_dir, thumbnails=thumbnails)
    page_w = dim(pres["pageSize"]["width"])
    out: list[dict] = []
    for n, slide in enumerate(target.get("slides", [])):
        thumb_path = thumbnails(n)
        if thumb_path is None:
            continue
        a = deck_fills.load(thumb_path)
        if a is None or page_w <= 0:
            continue
        px = a.shape[1] / page_w
        elements = slide.get("elements") or []
        for k, el in enumerate(elements):
            if el.get("kind") != "shape":
                continue
            score = pg.match_score(a, el, elements[k + 1:], px)
            if score is None:
                continue
            score.update(deck=deck_dir.name, slide=n + 1, object=el.get("object"),
                         inherited=el.get("inherited"))
            out.append(score)
    return out


def run(roots: list[Path], out_path: Path, log=print) -> dict:
    with tempfile.TemporaryDirectory(prefix="preset_survey_") as tmp:
        images_dir = Path(tmp)
        results: list[dict] = []
        errors: dict[str, str] = {}
        decks = list(deck_dirs(roots))
        log(f"{len(decks)} decks with saved files under {', '.join(str(r) for r in roots)}")
        for i, deck_dir in enumerate(decks, 1):
            t0 = time.time()
            try:
                got = survey_deck(deck_dir, images_dir)
            except Exception as exc:                                    # noqa: BLE001
                errors[deck_dir.name] = f"{exc.__class__.__name__}: {exc}"
                log(f"  [{i}/{len(decks)}] {deck_dir.name}: FAILED ({errors[deck_dir.name]})")
                if os.environ.get("B2S_SURVEY_TRACE"):
                    traceback.print_exc()
                continue
            results += got
            disagree = sum(1 for r in got if r["iou"] < DISAGREE_IOU)
            log(f"  [{i}/{len(decks)}] {deck_dir.name}: {len(got)} shapes judged, "
                f"{disagree} disagree ({time.time() - t0:.1f}s)")
    summary = summarize(results)
    doc = {"disagree_iou": DISAGREE_IOU, "decks_scanned": len(decks), "errors": errors,
          "results": results, "summary": summary}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    log(f"\nwrote {out_path} ({len(results)} judged shapes)")
    print_summary(summary, log)
    return doc


def summarize(results: list[dict]) -> dict:
    """Per preset kind: how many shapes were judged, how many disagree clearly, the decks and
    slides they disagree on (a slide counted once even if the same layout shape repeats there),
    and how many distinct decks and slides that is - what the phase 2 threshold (>=10 slides
    across >=3 decks) is checked against."""
    by_kind: dict[str, dict] = {}
    for r in results:
        s = by_kind.setdefault(r["kind"], {"judged": 0, "disagree": 0, "mean_iou": 0.0,
                                          "slides": set(), "decks": set(), "disagree_decks": set()})
        s["judged"] += 1
        s["mean_iou"] += r["iou"]
        s["decks"].add(r["deck"])
        if r["iou"] < DISAGREE_IOU:
            s["disagree"] += 1
            s["slides"].add((r["deck"], r["slide"]))
            s["disagree_decks"].add(r["deck"])
    out = {}
    for kind, s in by_kind.items():
        out[kind] = {
            "judged": s["judged"], "disagree": s["disagree"],
            "mean_iou": round(s["mean_iou"] / s["judged"], 3) if s["judged"] else None,
            "decks_seen": len(s["decks"]), "decks_disagreeing": len(s["disagree_decks"]),
            "slides_disagreeing": len(s["slides"]),
            "fitted_elsewhere": kind in pg.FITTED_ELSEWHERE,
            "example_decks": sorted(s["disagree_decks"])[:8],
        }
    return out


def print_summary(summary: dict, log=print) -> None:
    log(f"\n{'kind':<32}{'judged':>7}{'disagree':>9}{'mean iou':>10}{'decks':>7}{'slides':>8}  example decks")
    for kind, s in sorted(summary.items(), key=lambda kv: -kv[1]["slides_disagreeing"]):
        if not s["judged"]:
            continue
        flag = " (fitted elsewhere)" if s["fitted_elsewhere"] else ""
        log(f"{kind:<32}{s['judged']:>7}{s['disagree']:>9}{s['mean_iou']:>10.3f}"
            f"{s['decks_disagreeing']:>7}{s['slides_disagreeing']:>8}  {', '.join(s['example_decks'])}{flag}")
    qualifying = [k for k, s in summary.items()
                 if s["slides_disagreeing"] >= 10 and s["decks_disagreeing"] >= 3]
    log(f"\nPhase 2 threshold (>=10 slides across >=3 decks): {qualifying or 'none met'}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--roots", nargs="+", type=Path, default=None)
    r.add_argument("--out", type=Path, default=DEFAULT_OUT)
    s = sub.add_parser("show")
    s.add_argument("file", type=Path, nargs="?", default=DEFAULT_OUT)
    args = p.parse_args(argv)
    if args.cmd == "run":
        run(args.roots or DEFAULT_ROOTS, args.out)
    else:
        doc = json.loads(args.file.read_text(encoding="utf-8"))
        print_summary(doc["summary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
