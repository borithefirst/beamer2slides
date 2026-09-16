"""beamer2slides command line.

  python -m beamer2slides classify deck.pdf [--out DIR]
      writes DIR/raw.json, DIR/deck.json and DIR/debug/slide-NNN.png
"""

import argparse
import json
from pathlib import Path

from .classify import classify
from .debug import render_debug
from .extract import extract

ROOT = Path(__file__).resolve().parents[2]


def cmd_classify(pdf: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    raw = extract(pdf)
    (out / "raw.json").write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
    deck = classify(raw)
    (out / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
    render_debug(pdf, deck, out / "debug")
    s = deck["stats"]
    print(f"{pdf.name}: {len(deck['slides'])} slides, {s['chars_native']}/{s['chars']} chars native "
          f"({s['native_share']:.0%}) -> {out}")
    for slide in deck["slides"]:
        left = ", ".join(f"{l['reason']} {len(l['spans'])}" for l in slide["left_in_background"])
        print(f"  slide {slide['page'] + 1:>2}: {len(slide['elements'])} text boxes; background: {left or '-'}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="beamer2slides")
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("classify", help="extract + classify a PDF, with debug images")
    c.add_argument("pdf", type=Path)
    c.add_argument("--out", type=Path)
    args = ap.parse_args()
    if args.command == "classify":
        cmd_classify(args.pdf, args.out or ROOT / "out" / args.pdf.stem)


if __name__ == "__main__":
    main()
