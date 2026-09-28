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
from pathlib import Path


def export_to(pid: str, out: Path, per_part: int | None = None, workers: int = 3) -> dict:
    """Export the deck into `out`; returns `Export.summary()` with the files written."""
    from beamer2slides.deck_export import export_deck
    from beamer2slides.google_auth import drive_service, slides_service
    from beamer2slides.gslides import execute

    slides, drive = slides_service(), drive_service()
    pres = execute(slides.presentations().get(presentationId=pid, fields="presentationId,slides.objectId"))
    done = export_deck(drive, slides, pres, per_part=per_part, workers=workers)
    out.mkdir(parents=True, exist_ok=True)
    files = []
    for part in done.parts:
        path = out / ("deck.pptx" if done.whole else f"{part.name}.pptx")
        path.write_bytes(part.data)
        files.append(str(path))
    summary = {"presentationId": pid, **done.summary(), "files": files}
    (out / "export.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deck_export", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deck", required=True, help="presentation URL or id, or a convert output folder")
    ap.add_argument("--out", required=True, type=Path, help="the folder the .pptx files go into")
    ap.add_argument("--slides-per-part", type=int, help="start from parts of this many slides (default: "
                                                        "the whole deck, then halves while Drive refuses)")
    ap.add_argument("--workers", type=int, default=3, help="parts in the air at once (default 3: the write quota)")
    args = ap.parse_args(argv)
    from beamer2slides.sync import resolve_deck
    pid, _ = resolve_deck(args.deck)
    s = export_to(pid, args.out, args.slides_per_part, args.workers)
    for f in s["files"]:
        print(f"  {f}")
    print(f"{s['slides']} slide(s) in {len(s['files'])} file(s), {s['bytes'] / 1e6:.2f} MB; {s['exports']} export(s), "
          f"{s['copies']} temporary copies, {s['deleted']} deleted")
    for m in s["missing"]:
        print(f"  slides {m['slides'][0]}-{m['slides'][1]} not exported: {m['reason']}")
    for c in s["leftovers"]:
        print(f"  warning: the temporary copy {c} could not be deleted (tools/drive_usage.py --delete-staging)")
    return 1 if s["missing"] or s["leftovers"] else 0


if __name__ == "__main__":
    sys.exit(main())
