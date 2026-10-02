"""What speaker notes reach the deck: a scoreboard per frame.

    python tools/notes_score.py tests/decks/out/notes/30_speaker_notes-notes.pdf [--tex T] [--save F] [--against F]

Reads the PDF as `classify` does (`notes.prepare`, `extract`, the last overlay step of each frame)
and prints, per frame kept, its title and the note text the slide would carry - the one string
emit writes into the slide's speaker notes. With `--tex`, a PDF without note pages takes its
notes from that source, compiled once more with notes shown (`notes.notes_from_source`, as
`convert --tex` does). `--save` writes the board as JSON; `--against` an earlier one prints only
the frames whose note changed. No Google. Test deck: `tests/decks/30_speaker_notes.tex`, one way
of writing a note per frame, built in every way beamer shows notes (`tests/decks/build.py`
VARIANTS); what each note must read is `tests/test_speaker_notes.py`'s EXPECTED.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from ..extract import extract, select_overlays
from ..json_types import Json, JsonObject, as_int, as_object, as_objects, as_str
from ..notes import Prepared, notes_from_source, prepare
from ..raw_types import RawPage


@dataclass(frozen=True, kw_only=True)
class FrameNote:
    """A frame as `convert` keeps it (its last overlay step) and the note its slide carries."""
    page: int
    """0-based, as raw.json counts pages after note pages are taken out."""
    label: str
    title: str
    note: str | None


def title_of(page: RawPage) -> str:
    """The frame title as `extract.select_overlays` finds it: the largest words in the top fifth."""
    band = [s for s in page["spans"] if s["bbox"][3] < 0.2 * page["size"][1] and s["text"].strip()]
    if not band:
        return ""
    big = max(s["size"] for s in band)
    return " ".join(s["text"].strip() for s in sorted(band, key=lambda s: (round(s["origin"][1]), s["bbox"][0]))
                    if s["size"] >= 0.9 * big)


def with_source(pdf: Path, prepared: Prepared, tex: Path | None, work: Path) -> tuple[Prepared, str | None]:
    """`prepared` with the notes of `tex` when the PDF has none of its own (and a message)."""
    if tex is None or prepared.mode is not None:
        return prepared, None
    found = notes_from_source(tex, pdf, work)
    return (replace(prepared, notes=found.notes) if found.outcome == "found" else prepared), found.message


def score(pdf: Path, tex: Path | None) -> list[FrameNote]:
    """Every frame of `pdf` (its last overlay step) and its note, as `classify` reads them."""
    with tempfile.TemporaryDirectory() as tmp:
        prepared, _ = with_source(pdf, prepare(pdf, Path(tmp)), tex, Path(tmp) / "source")
        raw = extract(prepared.pdf, prepared.labels)
    for page in raw["pages"]:
        page["notes"] = prepared.notes.get(page["index"])
    kept = select_overlays(raw, "last")
    return [FrameNote(page=p["index"], label=p["label"], title=title_of(p), note=p.get("notes")) for p in kept["pages"]]


# --- JSON ---------------------------------------------------------------------------------------

def board_json(board: list[FrameNote]) -> JsonObject:
    frames: list[Json] = [{"page": f.page, "label": f.label, "title": f.title, "note": f.note} for f in board]
    return {"frames": frames}


def board_of(o: JsonObject, where: str) -> list[FrameNote]:
    out: list[FrameNote] = []
    for i, f in enumerate(as_objects(o.get("frames"), f"{where}.frames")):
        at = f"{where}.frames[{i}]"
        note = f.get("note")
        out.append(FrameNote(page=as_int(f.get("page"), f"{at}.page"), label=as_str(f.get("label"), f"{at}.label"),
                             title=as_str(f.get("title"), f"{at}.title"),
                             note=None if note is None else as_str(note, f"{at}.note")))
    return out


# --- printing -------------------------------------------------------------------------------------

def describe(f: FrameNote) -> list[str]:
    head = f"{f.page + 1:3d}  [{f.label}]  {f.title[:60]}"
    if f.note is None:
        return [head, "       (no note)"]
    return [head] + [f"       | {line}" for line in f.note.split("\n")]


def changes(before: list[FrameNote], after: list[FrameNote]) -> list[str]:
    """Frames (paired by title, else by page) whose note changed."""
    earlier = {f.title or f"page {f.page}": f for f in before}
    out: list[str] = []
    for f in after:
        old = earlier.get(f.title or f"page {f.page}")
        if old is None:
            out += ["new"] + describe(f)
        elif old.note != f.note:
            out += ["was"] + describe(old) + ["now"] + describe(f)
    return out


def main(argv: list[str] | None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--tex", type=Path, help="the source, compiled with notes shown when the PDF has none")
    ap.add_argument("--save", type=Path, help="write the board as JSON here")
    ap.add_argument("--against", type=Path, help="an earlier --save: print only what changed")
    a = ap.parse_args(argv)
    pdf: Path = a.pdf
    tex: Path | None = a.tex
    save: Path | None = a.save
    against: Path | None = a.against
    board = score(pdf, tex)
    if against is not None:
        before = board_of(as_object(json.loads(against.read_text(encoding="utf-8")), str(against)), str(against))
        print("\n".join(changes(before, board)) or "no note changed")
    else:
        for f in board:
            print("\n".join(describe(f)))
    print(f"{sum(f.note is not None for f in board)} of {len(board)} frames carry a note")
    if save is not None:
        save.parent.mkdir(parents=True, exist_ok=True)
        save.write_text(json.dumps(board_json(board), indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(None))
