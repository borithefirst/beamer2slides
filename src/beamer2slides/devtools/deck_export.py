"""A deck as .pptx files in a folder, in parts where Drive will not export it whole.

    python tools/deck_export.py --deck <url|id|out folder> --out DIR [--slides-per-part N] [--workers 3]

Drive refuses `files.export` of a deck past about 10 MB (many photos) and a slow one can outlast
the connection (`gslides.SLOW_EXPORT`); `beamer2slides.deck_export` then exports Drive copies cut
down to some of the slides, halving a part Drive still refuses, down to one slide. Without
`--slides-per-part` the whole deck is tried first and written as `deck.pptx`; with it the deck
goes straight into parts of that many slides. A part is written as `slides-001-004.pptx` (its
slides, 1-based and inclusive), and `export.json` says which slides came out and which did not.
Every temporary copy is deleted before this returns; one Drive would not delete is named, and
`tools/drive_usage.py --delete-staging` finds it again by its `b2sStaging` tag.

Only decks this app made or was opened with are reachable (the drive.file scope).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from beamer2slides.deck_export import WORKERS, Export


@dataclass(frozen=True, kw_only=True)
class Exported:
    """What `export_to` wrote: the export and the .pptx files, in slide order."""

    done: Export
    files: list[str]


def export_to(pid: str, out: Path, per_part: int | None, workers: int) -> Exported:
    """Export the deck into `out` (`per_part`: start from parts of that many slides, None: the
    whole deck first), writing `export.json` (`Export.summary()` with the files written)."""
    from beamer2slides.deck_export import export_deck
    from beamer2slides.google_auth import drive_service, slides_service
    from beamer2slides.gslides import execute

    slides, drive = slides_service(None), drive_service(None)
    pres = execute(slides.presentations().get(presentationId=pid, fields="presentationId,slides.objectId"))
    done = export_deck(drive, slides, pres, per_part=per_part, workers=workers, clients=None, only=None)
    out.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    for part in done.parts:
        path = out / ("deck.pptx" if done.whole else f"{part.name}.pptx")
        path.write_bytes(part.data)
        files.append(str(path))
    summary = {"presentationId": pid, **done.summary(), "files": files}
    (out / "export.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return Exported(done=done, files=files)


def main(argv: list[str] | None) -> int:
    ap = argparse.ArgumentParser(prog="deck_export", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deck", required=True, help="presentation URL or id, or a convert output folder")
    ap.add_argument("--out", required=True, type=Path, help="the folder the .pptx files go into")
    ap.add_argument("--slides-per-part", type=int, help="start from parts of this many slides (default: "
                                                        "the whole deck, then halves while Drive refuses)")
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help=f"parts in the air at once (default {WORKERS}: the write quota)")
    args = ap.parse_args(argv)
    from beamer2slides.sync import resolve_deck
    pid, _ = resolve_deck(args.deck)
    per_part: int | None = args.slides_per_part
    workers: int = args.workers
    out: Path = args.out
    s = export_to(pid, out, per_part, workers)
    done = s.done
    for f in s.files:
        print(f"  {f}")
    size = sum(len(p.data) for p in done.parts)
    print(f"{done.slides} slide(s) in {len(s.files)} file(s), {size / 1e6:.2f} MB; {done.exports} export(s), "
          f"{done.copies} temporary copies, {done.deleted} deleted")
    for m in done.missing:
        print(f"  slides {m['slides'][0]}-{m['slides'][1]} not exported: {m['reason']}")
    for c in done.leftovers:
        print(f"  warning: the temporary copy {c} could not be deleted (tools/drive_usage.py --delete-staging)")
    return 1 if done.missing or done.leftovers else 0


if __name__ == "__main__":
    sys.exit(main(None))
