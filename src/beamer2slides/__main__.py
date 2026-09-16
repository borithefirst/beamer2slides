"""beamer2slides command line.

  python -m beamer2slides classify deck.pdf [--out DIR]
      DIR/raw.json, DIR/deck.json and DIR/debug/slide-NNN.png
  python -m beamer2slides convert deck.pdf [--out DIR] [--title TITLE]
      classify + backgrounds + Google Slides deck (DIR/emit.json)
"""

import argparse
import json
from pathlib import Path

import pymupdf

from .classify import classify
from .debug import render_debug
from .extract import extract, select_overlays
from .notes import prepare as prepare_notes

ROOT = Path(__file__).resolve().parents[2]


def cmd_classify(pdf: Path, out: Path, overlays: str = "last") -> tuple[Path, dict, dict]:
    out.mkdir(parents=True, exist_ok=True)
    pdf, notes, notes_mode = prepare_notes(pdf, out)
    if notes_mode:
        print(f"speaker notes ({notes_mode}): found notes for {len(notes)} pages")
    elif (out / "slides.pdf").exists():
        (out / "slides.pdf").unlink()  # stale from an earlier run of a PDF that had notes
    raw = extract(pdf)
    for page in raw["pages"]:
        page["notes"] = notes.get(page["index"])
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


def cmd_convert(pdf: Path, out: Path, title: str | None, new_deck: bool, overlays: str) -> None:
    from .emit import emit
    from .render import render_backgrounds

    source = pdf
    pdf, raw, deck = cmd_classify(pdf, out, overlays)  # pdf: without note pages, if there were any
    render_backgrounds(pdf, raw, deck, out)
    (out / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
    title = title or pymupdf.open(pdf).metadata.get("title") or source.stem
    state = emit(deck, out, title, new_deck)
    print(f"Google Slides: {state['url']}")


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
        if name == "fidelity":
            c.add_argument("--refresh", action="store_true", help="re-export slide thumbnails")
    args = ap.parse_args()
    out = args.out or ROOT / "out" / args.pdf.stem
    if args.command == "classify":
        cmd_classify(args.pdf, out, args.overlays)
    elif args.command == "convert":
        cmd_convert(args.pdf, out, args.title, args.new_deck, args.overlays)
    elif args.command == "fidelity":
        from .fidelity import measure, print_report
        prepared = out / "slides.pdf"  # the notes-free PDF the deck was built from
        print_report(measure(prepared if prepared.exists() else args.pdf, out, args.refresh))


if __name__ == "__main__":
    main()
