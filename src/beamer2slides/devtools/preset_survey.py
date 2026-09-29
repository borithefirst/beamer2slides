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
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# This survey only reads a deck's own saved files (deck-files/): never a picture download.
os.environ.setdefault("B2S_NO_DOWNLOADS", "1")

from beamer2slides import deck_fills
from beamer2slides.devtools import preset_geometry as pg
from beamer2slides.deck_files import DeckFiles
from beamer2slides.deck_ir import dim, given_thumbnails, read_presentation
from beamer2slides.deck_ir_types import TargetShape
from beamer2slides.json_types import Json, JsonObject, JsonShapeError, as_array, as_int, as_object, as_str
from beamer2slides.paths import CHECKOUT

DEFAULT_ROOTS = [CHECKOUT / "out" / "adopt-corpus", CHECKOUT / "out" / "adopt-hunt"]
DEFAULT_OUT = CHECKOUT / "out" / "grind" / "analysis" / "presets" / "survey.json"
DISAGREE_IOU = 0.5    # a result below this is counted as a clear disagreement, not a rounding blur

Log = Callable[[str], None]


@dataclass(frozen=True, kw_only=True)
class Judged:
    """One shape `preset_geometry.match_score` could judge, and where it is."""

    score: pg.Score
    deck: str
    slide: int              # 1-based
    object: str | None
    inherited: str | None   # the layout/master object id a shape without one drew from

    def json(self) -> JsonObject:
        return {**self.score.json(), "deck": self.deck, "slide": self.slide, "object": self.object,
                "inherited": self.inherited}


@dataclass(frozen=True, kw_only=True)
class KindSummary:
    """One preset kind across the survey (`summarize`)."""

    judged: int
    disagree: int
    mean_iou: float | None
    decks_seen: int
    decks_disagreeing: int
    slides_disagreeing: int
    fitted_elsewhere: bool
    example_decks: list[str]

    def json(self) -> JsonObject:
        return {"judged": self.judged, "disagree": self.disagree, "mean_iou": self.mean_iou,
                "decks_seen": self.decks_seen, "decks_disagreeing": self.decks_disagreeing,
                "slides_disagreeing": self.slides_disagreeing, "fitted_elsewhere": self.fitted_elsewhere,
                "example_decks": list[Json](self.example_decks)}


def _number(value: Json, where: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise JsonShapeError(f"{where}: a number was expected")


def kind_summary(o: JsonObject, where: str) -> KindSummary:
    """A saved survey.json's summary entry, back as a record."""
    mean = o["mean_iou"]
    fitted = o["fitted_elsewhere"]
    if not isinstance(fitted, bool):
        raise JsonShapeError(f"{where}.fitted_elsewhere: a boolean was expected")
    return KindSummary(judged=as_int(o["judged"], f"{where}.judged"),
                       disagree=as_int(o["disagree"], f"{where}.disagree"),
                       mean_iou=None if mean is None else _number(mean, f"{where}.mean_iou"),
                       decks_seen=as_int(o["decks_seen"], f"{where}.decks_seen"),
                       decks_disagreeing=as_int(o["decks_disagreeing"], f"{where}.decks_disagreeing"),
                       slides_disagreeing=as_int(o["slides_disagreeing"], f"{where}.slides_disagreeing"),
                       fitted_elsewhere=fitted,
                       example_decks=[as_str(d, f"{where}.example_decks")
                                      for d in as_array(o["example_decks"], f"{where}.example_decks")])


def deck_dirs(roots: Sequence[Path]) -> Iterator[Path]:
    """Every deck folder under `roots` that has files a `deck_files.DeckFiles` can load (a deck the
    corpus or the hunt saved with `deck-files`), name first."""
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in seen:
                continue
            if (d / "deck-files" / "presentation.json").is_file():
                seen.add(d.name)
                yield d


def _quiet(_: str) -> None:
    """given_thumbnails' log, unheard."""


def survey_deck(deck_dir: Path, images_dir: Path) -> list[Judged]:
    """Every adjustable-preset shape `preset_geometry.match_score` could judge, across every slide
    of the deck at `deck_dir` (its `deck-files/`), scored against that slide's own thumbnail."""
    files = DeckFiles.load(deck_dir / "deck-files")
    where = str(files.presentation)
    pres = as_object(json.loads(files.presentation.read_text(encoding="utf-8")), where)
    thumbnails, shots = given_thumbnails(pres, files.thumbnails, _quiet)
    if not shots:
        return []
    target = read_presentation(pres, images_dir, None, None, thumbnails, True)
    page_w = dim(as_object(pres["pageSize"], f"{where}: pageSize")["width"])
    out: list[Judged] = []
    for n, slide in enumerate(target.slides):
        thumb_path = thumbnails(n)
        if thumb_path is None:
            continue
        a = deck_fills.load(thumb_path)
        if a is None or page_w <= 0:
            continue
        px = a.shape[1] / page_w
        elements = slide.elements
        for k, el in enumerate(elements):
            if not isinstance(el, TargetShape):
                continue
            score = pg.match_score(a, el, elements[k + 1:], px, False)
            if score is None:
                continue
            out.append(Judged(score=score, deck=deck_dir.name, slide=n + 1, object=el.object, inherited=el.inherited))
    return out


def run(roots: Sequence[Path], out_path: Path, log: Log) -> JsonObject:
    """Survey every deck under `roots` into `out_path`; returns what it wrote."""
    with tempfile.TemporaryDirectory(prefix="preset_survey_") as tmp:
        images_dir = Path(tmp)
        results: list[Judged] = []
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
            disagree = sum(1 for r in got if r.score.iou < DISAGREE_IOU)
            log(f"  [{i}/{len(decks)}] {deck_dir.name}: {len(got)} shapes judged, "
                f"{disagree} disagree ({time.time() - t0:.1f}s)")
    summary = summarize(results)
    doc: JsonObject = {"disagree_iou": DISAGREE_IOU, "decks_scanned": len(decks),
                       "errors": {k: v for k, v in errors.items()},
                       "results": [r.json() for r in results],
                       "summary": {k: s.json() for k, s in summary.items()}}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    log(f"\nwrote {out_path} ({len(results)} judged shapes)")
    print_summary(summary, log)
    return doc


class _Tally:
    """One kind's counts while `summarize` walks the results."""

    def __init__(self) -> None:
        self.judged = 0
        self.disagree = 0
        self.iou_sum = 0.0
        self.slides: set[tuple[str, int]] = set()
        self.decks: set[str] = set()
        self.disagree_decks: set[str] = set()


def summarize(results: Sequence[Judged]) -> dict[str, KindSummary]:
    """Per preset kind: how many shapes were judged, how many disagree clearly, the decks and
    slides they disagree on (a slide counted once even if the same layout shape repeats there),
    and how many distinct decks and slides that is - what the phase 2 threshold (>=10 slides
    across >=3 decks) is checked against."""
    by_kind: dict[str, _Tally] = {}
    for r in results:
        s = by_kind.setdefault(r.score.kind, _Tally())
        s.judged += 1
        s.iou_sum += r.score.iou
        s.decks.add(r.deck)
        if r.score.iou < DISAGREE_IOU:
            s.disagree += 1
            s.slides.add((r.deck, r.slide))
            s.disagree_decks.add(r.deck)
    return {kind: KindSummary(judged=s.judged, disagree=s.disagree,
                              mean_iou=round(s.iou_sum / s.judged, 3) if s.judged else None,
                              decks_seen=len(s.decks), decks_disagreeing=len(s.disagree_decks),
                              slides_disagreeing=len(s.slides), fitted_elsewhere=kind in pg.FITTED_ELSEWHERE,
                              example_decks=sorted(s.disagree_decks)[:8])
            for kind, s in by_kind.items()}


def print_summary(summary: Mapping[str, KindSummary], log: Log) -> None:
    log(f"\n{'kind':<32}{'judged':>7}{'disagree':>9}{'mean iou':>10}{'decks':>7}{'slides':>8}  example decks")
    for kind, s in sorted(summary.items(), key=lambda kv: -kv[1].slides_disagreeing):
        if not s.judged or s.mean_iou is None:
            continue
        flag = " (fitted elsewhere)" if s.fitted_elsewhere else ""
        log(f"{kind:<32}{s.judged:>7}{s.disagree:>9}{s.mean_iou:>10.3f}"
            f"{s.decks_disagreeing:>7}{s.slides_disagreeing:>8}  {', '.join(s.example_decks)}{flag}")
    qualifying = [k for k, s in summary.items()
                  if s.slides_disagreeing >= 10 and s.decks_disagreeing >= 3]
    log(f"\nPhase 2 threshold (>=10 slides across >=3 decks): {qualifying or 'none met'}")


def main(argv: list[str] | None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--roots", nargs="+", type=Path, default=None)
    r.add_argument("--out", type=Path, default=DEFAULT_OUT)
    s = sub.add_parser("show")
    s.add_argument("file", type=Path, nargs="?", default=DEFAULT_OUT)
    args = p.parse_args(argv)
    if args.cmd == "run":
        roots: list[Path] | None = args.roots
        out: Path = args.out
        run(roots or DEFAULT_ROOTS, out, print)
    else:
        file: Path = args.file
        doc = as_object(json.loads(file.read_text(encoding="utf-8")), str(file))
        summary = as_object(doc["summary"], f"{file}: summary")
        print_summary({k: kind_summary(as_object(v, f"{file}: summary.{k}"), f"summary.{k}")
                       for k, v in summary.items()}, print)
    return 0


if __name__ == "__main__":
    sys.exit(main(None))
