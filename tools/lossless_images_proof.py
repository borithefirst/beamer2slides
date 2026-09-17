"""Proof on the live deck: what Google stores for a `\\includegraphics` picture.

Converts a deck (or reuses the one in the fixed output folder), reads the presentation back
with `presentations.get`, downloads every picture through its contentUrl (deck_ir does both)
and compares the bytes with the file the converter wrote and with the author's original.

    $env:B2S_CLIENT_SECRET="C:\\Dev\\beamer2slides\\client_secret.json"
    $env:B2S_TOKEN="C:\\Dev\\beamer2slides\\token.json"
    python tools/lossless_images_proof.py --convert
    python tools/lossless_images_proof.py tests/decks/out/07_images.pdf

Google keeps a stored picture byte for byte up to about 2046 px on the long side and re-encodes
bigger ones, so the verdict is either "the author's file" or "full resolution, re-encoded".
"""

import argparse
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from beamer2slides.deck_ir import read_deck  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = [ROOT / "tests" / "decks" / "out" / n for n in ("07_images.pdf", "23_raster_images.pdf")]
OUT = ROOT / "out" / "agent-images"
ORIGINALS = ROOT / "tests" / "decks" / "img"


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def size_of(data: bytes) -> tuple[int, int]:
    from PIL import Image
    try:
        return Image.open(io.BytesIO(data)).size
    except Exception:
        return (0, 0)


def convert(pdf: Path, out: Path) -> None:
    print(f"converting {pdf.name} -> {out}")
    r = subprocess.run([sys.executable, "-m", "beamer2slides", "convert", str(pdf), "--out", str(out)],
                       cwd=ROOT, text=True)
    if r.returncode:
        raise SystemExit(f"convert failed ({r.returncode})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="*", type=Path, default=DEFAULT)
    ap.add_argument("--convert", action="store_true", help="convert again instead of reading the deck as it is")
    args = ap.parse_args(argv)

    originals = {sha1(p.read_bytes()): p.name for p in ORIGINALS.iterdir() if p.is_file()}
    problems = 0
    for pdf in args.pdfs:
        out = OUT / pdf.stem
        if args.convert or not (out / "emit.json").exists():
            convert(pdf, out)
        deck = json.loads((out / "deck.json").read_text(encoding="utf-8"))
        live = read_deck(str(out), images=out / "deck-pictures")
        print(f"\n=== {pdf.name}  ({json.loads((out / 'emit.json').read_text(encoding='utf-8'))['url']})")
        for ours, theirs in zip(deck["slides"], live["slides"]):
            mine = [e for e in ours["elements"] if e["kind"] == "image"]
            got = [e for e in theirs["elements"] if e["kind"] == "image"]
            if len(mine) != len(got):
                print(f"  slide {ours['page'] + 1}: {len(mine)} pictures sent, {len(got)} read back")
                problems += 1
                continue
            for el, pic in zip(mine, got):
                sent = (out / el["file"]).read_bytes()
                stored = Path(pic["file"]).read_bytes() if pic.get("file") else b""
                route = el.get("picture") or "crop"
                same = sha1(sent) == sha1(stored)
                name = originals.get(sha1(stored))
                verdict = f"the author's {name}, byte for byte" if name else \
                    ("what we sent, byte for byte" if same else f"re-encoded by Google ({pic.get('format')})")
                print(f"  slide {ours['page'] + 1} {el['id']}: route {route}, sent {len(sent)} B "
                      f"{el.get('px')}, stored {len(stored)} B {list(size_of(stored))} -> {verdict}")
                if route in ("raw", "decoded") and max(size_of(stored)) < 0.9 * max(el["px"]) \
                        and max(size_of(stored)) < 2040:
                    print("    !! the stored picture is smaller than the image we sent")
                    problems += 1
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
