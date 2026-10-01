"""Which figures become native diagrams, and why the others stay pictures: a scoreboard per frame.

    python tools/diagram_score.py tests/decks/out/29_tikz_diagrams.pdf [--save F] [--against F]

classify keeps, per page, every figure cluster `diagram_from` refused and why
(`classify_state.DiagramRefusal`); this reads them for each frame (the last overlay step, as
`convert` keeps) beside what the frame became:

- `native`     only diagrams;
- `mixed`      diagrams and figure pictures;
- `picture`    figure pictures only, each with the refusal that kept it one;
- `background` a figure left in the background (`too_large`);
- `none`       no figure at all.

`--save` writes the board as JSON; `--against` an earlier one prints only the frames whose
outcome or reasons changed, and the counts before and after. Classification only: no Google.
Test deck: `tests/decks/29_tikz_diagrams.tex`, one way of drawing per frame.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..classify import PageClassifier
from ..classify_model import Rect
from ..classify_state import REFUSALS, DiagramRefusal, Refusal
from ..classify_text import body_size
from ..extract import extract, select_overlays
from ..ir import Slide
from ..json_types import Json, JsonObject, as_int, as_object, as_objects, as_str
from ..notes import prepare
from ..typing_compat import assert_never

Outcome = Literal["native", "mixed", "picture", "background", "none"]
OUTCOMES: tuple[Outcome, ...] = ("native", "mixed", "picture", "background", "none")


@dataclass(frozen=True, kw_only=True)
class Refused:
    """One refusal on a frame: its reason and the drawing or label that decided it."""
    reason: Refusal
    detail: str


@dataclass(frozen=True, kw_only=True)
class FrameScore:
    """What one frame's figures became."""
    page: int
    """0-based, as raw.json counts pages."""
    title: str
    diagrams: int
    pictures: int
    refusals: list[Refused]

    @property
    def outcome(self) -> Outcome:
        if self.diagrams and self.pictures:
            return "mixed"
        if self.diagrams:
            return "native"
        if self.pictures:
            return "picture"
        if any(r.reason == "too_large" for r in self.refusals):
            return "background"
        return "none"


def title_of(slide: Slide) -> str:
    for e in slide["elements"]:
        if e["kind"] == "text" and e["role"] == "title":
            return " ".join("".join(r["text"] for r in p["runs"]) for p in e["paragraphs"]).strip()
    return ""


def frame_score(page: int, slide: Slide, refusals: list[DiagramRefusal]) -> FrameScore:
    """A frame's score from its slide and the refusals classify kept: a refusal counts when a
    figure picture holds its cluster (one that became a table or an overlay has no picture), or
    when it left the figure in the background."""
    pictures = [Rect.of(e["bbox"]) for e in slide["elements"] if e["kind"] == "image" and e["role"] == "figure"]
    kept = [r for r in refusals if r.reason == "too_large"
            or any(p.expand(1.5).contains(r.box.cx, r.box.cy) for p in pictures)]
    return FrameScore(page=page, title=title_of(slide),
                      diagrams=sum(e["kind"] == "diagram" for e in slide["elements"]), pictures=len(pictures),
                      refusals=[Refused(reason=r.reason, detail=r.detail) for r in kept])


def score(pdf: Path) -> list[FrameScore]:
    """Every frame of `pdf` (its last overlay step), classified as `convert` would."""
    with tempfile.TemporaryDirectory() as tmp:
        prepared = prepare(pdf, Path(tmp))
        raw = select_overlays(extract(prepared.pdf, prepared.labels), "last")
    body = body_size(raw)
    out: list[FrameScore] = []
    for page in raw["pages"]:
        classifier = PageClassifier(page, body)
        out.append(frame_score(page["index"], classifier.classify(), classifier.diagram_refusals))
    return out


# --- JSON ---------------------------------------------------------------------------------------

def board_json(board: list[FrameScore]) -> JsonObject:
    frames: list[Json] = [{
        "page": f.page, "title": f.title, "outcome": f.outcome, "diagrams": f.diagrams, "pictures": f.pictures,
        "refusals": [{"reason": r.reason, "detail": r.detail} for r in f.refusals]} for f in board]
    return {"frames": frames}


def refusal_of(text: str, where: str) -> Refusal:
    for r in REFUSALS:
        if r == text:
            return r
    raise ValueError(f"{where}: no refusal {text!r}")


def board_of(o: JsonObject, where: str) -> list[FrameScore]:
    out: list[FrameScore] = []
    for i, f in enumerate(as_objects(o.get("frames"), f"{where}.frames")):
        at = f"{where}.frames[{i}]"
        out.append(FrameScore(
            page=as_int(f.get("page"), f"{at}.page"), title=as_str(f.get("title"), f"{at}.title"),
            diagrams=as_int(f.get("diagrams"), f"{at}.diagrams"), pictures=as_int(f.get("pictures"), f"{at}.pictures"),
            refusals=[Refused(reason=refusal_of(as_str(r.get("reason"), f"{at}.reason"), at),
                              detail=as_str(r.get("detail"), f"{at}.detail"))
                      for r in as_objects(f.get("refusals"), f"{at}.refusals")]))
    return out


# --- printing -------------------------------------------------------------------------------------

def line_of(f: FrameScore) -> str:
    reasons = ", ".join(sorted({r.reason for r in f.refusals}))
    return f"{f.page + 1:3d}  {f.outcome:10s}  {f.title[:44]:44s}  {reasons}"


def counts(board: list[FrameScore]) -> str:
    by = Counter(f.outcome for f in board)
    return "  ".join(f"{o} {by[o]}" for o in OUTCOMES if by[o])


def reasons_count(board: list[FrameScore]) -> str:
    # (a frame counts once per reason: one picture of three curves is one curve frame)
    by = Counter(r for f in board for r in {r.reason for r in f.refusals})
    return "  ".join(f"{r} {n}" for r, n in by.most_common())


def describe(f: FrameScore) -> list[str]:
    match f.outcome:
        case "native" | "none":
            return [line_of(f)]
        case "mixed" | "picture" | "background":
            return [line_of(f)] + [f"       {r.reason}: {r.detail}" for r in f.refusals]
        case _:
            assert_never(f.outcome)


def changes(before: list[FrameScore], after: list[FrameScore]) -> list[str]:
    """Frames (paired by title, else by page) whose outcome or reasons changed."""
    earlier = {f.title or f"page {f.page}": f for f in before}
    out: list[str] = []
    for f in after:
        old = earlier.get(f.title or f"page {f.page}")
        reasons = sorted(r.reason for r in f.refusals)
        if old is None:
            out.append(f"new  {line_of(f)}")
        elif old.outcome != f.outcome or sorted(r.reason for r in old.refusals) != reasons:
            out.append(f"was  {line_of(old)}")
            out.append(f"now  {line_of(f)}")
    return out


def main(argv: list[str] | None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--save", type=Path, help="write the board as JSON here")
    ap.add_argument("--against", type=Path, help="an earlier --save: print only what changed")
    a = ap.parse_args(argv)
    pdf: Path = a.pdf
    save: Path | None = a.save
    against: Path | None = a.against
    board = score(pdf)
    if against is not None:
        before = board_of(as_object(json.loads(against.read_text(encoding="utf-8")), str(against)), str(against))
        print("\n".join(changes(before, board)) or "no frame changed")
        print(f"before: {counts(before)}")
        print(f"after:  {counts(board)}")
    else:
        for f in board:
            print("\n".join(describe(f)))
        print(counts(board))
        print(f"frames per reason: {reasons_count(board)}")
    if save is not None:
        save.parent.mkdir(parents=True, exist_ok=True)
        save.write_text(json.dumps(board_json(board), indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(None))
