"""beamer2slides command line.

  python -m beamer2slides classify deck.pdf [--out DIR]
      DIR/raw.json, DIR/deck.json and DIR/debug/slide-NNN.png
  python -m beamer2slides convert deck.pdf [--out DIR] [--title TITLE]
      classify + backgrounds + Google Slides deck (DIR/emit.json)
  python -m beamer2slides pull --deck URL|ID|DIR --tex main.tex [--apply | --out SRC] [--max-iter N]
      edit the source until its conversion matches the (edited) deck: WORK/pull.patch, edits.md/json
  python -m beamer2slides converge --target deck.json --tex main.tex [...]
      the same against a deck.json-shaped target, offline
"""

import argparse
import json
import time
from pathlib import Path

from .classify import classify
from .debug import render_debug
from .extract import extract, select_overlays
from .notes import prepare as prepare_notes
from .paths import out_root

BACKUP_MODES = ("auto", "none", "file", "drive", "both")  # = guard.BACKUP_MODES (imported lazily)


def cmd_classify(pdf: Path, out: Path, overlays: str = "last") -> tuple[Path, dict, dict]:
    out.mkdir(parents=True, exist_ok=True)
    prepared = prepare_notes(pdf, out)
    pdf = prepared.pdf
    if prepared.mode:
        print(f"speaker notes ({prepared.mode}): found notes for {len(prepared.notes)} pages")
    elif (out / "slides.pdf").exists():
        (out / "slides.pdf").unlink()  # stale from an earlier run of a PDF that had notes
    raw = extract(pdf, prepared.labels)
    for page in raw["pages"]:
        page["notes"] = prepared.notes.get(page["index"])
    raw = select_overlays(raw, overlays)
    if raw.get("overlays", {}).get("dropped"):
        print(f"overlays: kept the last step of each frame, skipped {raw['overlays']['dropped']} pages")
    (out / "raw.json").write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
    deck = classify(raw)
    (out / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
    render_debug(pdf, deck, out / "debug")
    s = deck["stats"]
    print(f"{pdf.name}: {len(deck['slides'])} slides, {s['chars_native']}/{s['chars']} chars native "
          f"({s['native_share']:.0%}) -> {out}")
    for slide in deck["slides"]:
        left = ", ".join(f"{l['reason']} {len(l['spans'])}" for l in slide["left_in_background"])
        kinds = [e["kind"] for e in slide["elements"]]
        print(f"  slide {slide['page'] + 1:>2}: {kinds.count('text')} text boxes, {kinds.count('image')} pictures,"
              f" {kinds.count('shape')} shape candidates, {kinds.count('table')} tables,"
              f" {kinds.count('diagram')} diagrams; background: {left or '-'}")
    return pdf, raw, deck


def cmd_convert(pdf: Path, out: Path, title: str | None, new_deck: bool, overlays: str, measure: bool = True,
                force_rebuild: bool = False, backup: str = "auto") -> None:
    from .emit import emit, preflight_rebuild
    from .render import render_backgrounds

    source = pdf
    # Whether this folder's deck may be replaced is decided before any work (and again in emit).
    preflight_rebuild(out, source, new_deck, force_rebuild)
    pdf, raw, deck = cmd_classify(pdf, out, overlays)  # pdf: without note pages, if there were any
    render_backgrounds(pdf, raw, deck, out)
    (out / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
    title = title or raw["source"]["title"] or source.stem
    state = emit(deck, out, title, new_deck, measure, force_rebuild, backup, source)
    print(f"Google Slides: {state['url']}")
    from .snapshot import snapshot_after_convert
    try:
        base = snapshot_after_convert(state["deck"], out, state, source, overlays)
        print(f"sync base: {len(base['slides'])} slides recorded ({out / 'sync' / 'base.json'})")
    except Exception as e:  # the deck is complete; only a later sync needs the base
        print(f"warning: could not record the sync base ({type(e).__name__}: {e})")


def record_sync_point(pdf: Path, deck: str, out: Path | None, backup: str) -> dict | None:
    """Before sync's first write: the deck's revision (and a backup, `--backup`) so the state
    sync is about to change can be restored (docs/sync.md). Never fails the sync."""
    from .guard import backup_deck, deck_url, record
    from .google_auth import drive_service, slides_service
    from .gslides import execute
    from .sync import resolve_deck
    try:
        pid, folder = resolve_deck(str(deck))
        out = out or folder or out_root() / pdf.stem
        rev = execute(slides_service().presentations().get(presentationId=pid, fields="revisionId"))["revisionId"]
        drive = drive_service()
        info = execute(drive.files().get(fileId=pid, fields="modifiedTime"))
        # A sync only ever rewrites the parts the source changed, but the deck as a whole can only
        # be recovered from a file: Drive's version history is not readable back (docs/sync.md).
        entry = {"presentationId": pid, "url": deck_url(pid), "action": "synced", "revisionId": rev,
                 "modifiedTime": info.get("modifiedTime"), "out": str(out),
                 "checked": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": f"sync {pdf.name}",
                 "backup": backup_deck(drive, pid, Path(out), "file" if backup == "auto" else backup,
                                       fallback=False)}
        record(Path(out), entry)
        return {"out": str(out), "entry": entry}
    except Exception as e:  # noqa: BLE001 (a missing recovery note is no reason not to sync)
        print(f"warning: could not record the deck's revision before syncing ({type(e).__name__}: {e})")
        return None


def add_recovery(note: dict, info: dict) -> None:
    """The recovery note on screen and in sync's report."""
    from .guard import restore_hint
    entry = note["entry"]
    print("recovery:")
    for line in restore_hint(entry, "sync"):
        print(line)
    info.setdefault("recovery", entry)
    path = Path(note["out"]) / "sync" / "sync-report.json"
    if path.exists():
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            report["recovery"] = entry
            path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError):
            pass


def main() -> None:
    ap = argparse.ArgumentParser(prog="beamer2slides")
    sub = ap.add_subparsers(dest="command", required=True)
    for name, help_text in (("classify", "extract + classify a PDF, with debug images"),
                            ("convert", "full conversion into a Google Slides deck"),
                            ("fidelity", "compare the emitted deck with the PDF")):
        c = sub.add_parser(name, help=help_text)
        c.add_argument("pdf", type=Path)
        c.add_argument("--out", type=Path)
        if name in ("classify", "convert"):
            c.add_argument("--overlays", choices=["last", "all"], default="last",
                           help="for PDFs with overlay steps: keep the last step of each frame (default) or all pages")
        if name == "convert":
            c.add_argument("--title")
            c.add_argument("--new-deck", action="store_true",
                           help="create a new presentation instead of rebuilding the previous one")
            c.add_argument("--force-rebuild", action="store_true",
                           help="rebuild the previous deck even though it was edited in Slides "
                                "(its content is replaced; a backup is kept first)")
            c.add_argument("--backup", choices=list(BACKUP_MODES), default="auto",
                           help="what to keep before replacing a deck's content: auto (a .pptx when the "
                                "rebuild is forced), none, file (.pptx in <out>/backups), drive (a copy "
                                "of the presentation), both")
            c.add_argument("--predict-places", "--predict-holes", dest="predict_places", action="store_true",
                           help="place inline formula and overlay pictures by prediction only, without measuring "
                                "their gaps and words on scratch slides")
        if name == "fidelity":
            c.add_argument("--refresh", action="store_true", help="re-export slide thumbnails")
    c = sub.add_parser("sync", help="merge a changed PDF into the edited deck (docs/sync.md)")
    c.add_argument("pdf", type=Path)
    c.add_argument("--deck", required=True, help="presentation URL or id, or the output folder of its convert")
    c.add_argument("--out", type=Path, help="where the new conversion, base and reports go (default: the deck's folder)")
    c.add_argument("--dry-run", action="store_true", help="plan and report without writing to the deck")
    c.add_argument("--overlays", choices=["last", "all"], default=None,
                   help="which overlay steps to keep (default: the ones the deck was converted with)")
    c.add_argument("--predict-places", dest="predict_places", action="store_true")
    c.add_argument("--backup", choices=list(BACKUP_MODES), default="auto",
                   help="keep a way back before sync's first write: auto = file (a .pptx in <out>/backups), "
                        "none, drive (a copy of the presentation), both")
    for name, help_text in (("pull", "edit the beamer source until its conversion matches an edited deck"),
                            ("converge", "offline pull: edit the source until its conversion matches a deck.json")):
        c = sub.add_parser(name, help=help_text)
        if name == "pull":
            c.add_argument("--deck", required=True, help="deck URL, presentation id or convert output folder")
        else:
            c.add_argument("--target", required=True, type=Path, help="deck.json-shaped target IR")
        c.add_argument("--tex", required=True, type=Path)
        c.add_argument("--apply", action="store_true", help="patch the source in place (what was there is kept as <file>.bak, .bak2, ...)")
        c.add_argument("--out", type=Path, help="write the edited source tree here instead")
        c.add_argument("--work", type=Path, help="loop folder and reports (default: <deck folder>/pull)")
        c.add_argument("--max-iter", type=int, default=10)
        c.add_argument("--handout", action="store_true", help="compile in handout mode (one page per frame)")
        c.add_argument("--engine", help="pdflatex, xelatex or lualatex (default: from the source)")
    args = ap.parse_args()
    if args.command in ("pull", "converge"):
        from .inverse import cmd_converge, cmd_pull
        if args.command == "pull":
            cmd_pull(args.deck, args.tex, args.work, args.apply, args.out, args.max_iter, args.handout, args.engine)
        else:
            cmd_converge(args.target, args.tex, args.work, args.apply, args.out, args.max_iter, args.handout, args.engine)
        return
    if args.command == "sync":
        from .sync import sync
        note = None if args.dry_run else record_sync_point(args.pdf, args.deck, args.out, args.backup)
        info = sync(args.pdf, args.deck, args.out, args.dry_run, args.overlays, not args.predict_places)
        if note:
            add_recovery(note, info)
        r = info["report"]
        print(f"sync{' (dry run)' if args.dry_run else ''}: {len(r['applied'])} source changes applied, "
              f"{len(r['overrides'])} deck edits kept, {len(r['conflicts'])} conflicts, requests {info['requests'] or 0}")
        for c in r["conflicts"]:
            print(f"  conflict: {c['slide']} / {c['element']}: {c['field']} ({c['resolution']})")
        for wmsg in r["warnings"]:
            print(f"  warning: {wmsg}")
        print(f"Google Slides: {info['url']}")
        return
    out = args.out or out_root() / args.pdf.stem
    if args.command == "classify":
        cmd_classify(args.pdf, out, args.overlays)
    elif args.command == "convert":
        from .guard import RebuildRefused
        try:
            cmd_convert(args.pdf, out, args.title, args.new_deck, args.overlays, not args.predict_places,
                        args.force_rebuild, args.backup)
        except RebuildRefused as refused:
            raise SystemExit(str(refused)) from None
    elif args.command == "fidelity":
        from .fidelity import measure, print_report
        prepared = out / "slides.pdf"  # the notes-free PDF the deck was built from
        print_report(measure(prepared if prepared.exists() else args.pdf, out, args.refresh))


if __name__ == "__main__":
    main()
