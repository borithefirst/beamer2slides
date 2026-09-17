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
from pathlib import Path

from .classify import classify
from .debug import render_debug
from .extract import extract, select_overlays
from .notes import prepare as prepare_notes

ROOT = Path(__file__).resolve().parents[2]


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


def cmd_convert(pdf: Path, out: Path, title: str | None, new_deck: bool, overlays: str, measure: bool = True) -> None:
    from .emit import emit
    from .render import render_backgrounds

    source = pdf
    pdf, raw, deck = cmd_classify(pdf, out, overlays)  # pdf: without note pages, if there were any
    render_backgrounds(pdf, raw, deck, out)
    (out / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
    title = title or raw["source"]["title"] or source.stem
    state = emit(deck, out, title, new_deck, measure)
    print(f"Google Slides: {state['url']}")
    from .snapshot import snapshot_after_convert
    try:
        base = snapshot_after_convert(state["deck"], out, state, source)
        print(f"sync base: {len(base['slides'])} slides recorded ({out / 'sync' / 'base.json'})")
    except Exception as e:  # the deck is complete; only a later sync needs the base
        print(f"warning: could not record the sync base ({type(e).__name__}: {e})")


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
    c.add_argument("--overlays", choices=["last", "all"], default="last")
    c.add_argument("--predict-places", dest="predict_places", action="store_true")
    for name, help_text in (("pull", "edit the beamer source until its conversion matches an edited deck"),
                            ("converge", "offline pull: edit the source until its conversion matches a deck.json")):
        c = sub.add_parser(name, help=help_text)
        if name == "pull":
            c.add_argument("--deck", required=True, help="deck URL, presentation id or convert output folder")
        else:
            c.add_argument("--target", required=True, type=Path, help="deck.json-shaped target IR")
        c.add_argument("--tex", required=True, type=Path)
        c.add_argument("--apply", action="store_true", help="patch the source in place (.bak backups)")
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
        info = sync(args.pdf, args.deck, args.out, args.dry_run, args.overlays, not args.predict_places)
        r = info["report"]
        print(f"sync{' (dry run)' if args.dry_run else ''}: {len(r['applied'])} source changes applied, "
              f"{len(r['overrides'])} deck edits kept, {len(r['conflicts'])} conflicts, requests {info['requests'] or 0}")
        for c in r["conflicts"]:
            print(f"  conflict: {c['slide']} / {c['element']}: {c['field']} ({c['resolution']})")
        for wmsg in r["warnings"]:
            print(f"  warning: {wmsg}")
        print(f"Google Slides: {info['url']}")
        return
    out = args.out or ROOT / "out" / args.pdf.stem
    if args.command == "classify":
        cmd_classify(args.pdf, out, args.overlays)
    elif args.command == "convert":
        cmd_convert(args.pdf, out, args.title, args.new_deck, args.overlays, not args.predict_places)
    elif args.command == "fidelity":
        from .fidelity import measure, print_report
        prepared = out / "slides.pdf"  # the notes-free PDF the deck was built from
        print_report(measure(prepared if prepared.exists() else args.pdf, out, args.refresh))


if __name__ == "__main__":
    main()
