"""python tools/showcase.py decks [NAME...] | capture [NAME...] | run [--tag T] | gallery OUT

  decks     build the showcase decks in Drive (in place when decks.json has them), share them view-only
  capture   adopt_bench capture of each deck into out/showcase-corpus
  run       adopt_bench run over that corpus (tag `showcase` unless --tag)
  gallery   the site: OUT/index.html + OUT/img/ (+ each deck's adopted source as a .zip)
  swipe     the README's GIF from a built site: SITE [DEST, default docs/media/adopt-swipe.gif]
  fixture   the captured targets as tests/decks/foreign/showcase, pictures shrunk (test_adopt_compiles)
"""

import argparse
import json
import os
from pathlib import Path

from beamer2slides.devtools.showcase.decks import CORPUS

FIXTURE = Path("tests/decks/foreign/showcase")
FIXTURE_SIDE = 64          # px: the compile test needs a picture of the right kind, not its detail


def write_fixture(out: Path = FIXTURE, names=None) -> None:
    """Each captured deck's target.json with its pictures beside it as `images/<name>`, shrunk to
    FIXTURE_SIDE and named relative to the deck's folder: every deck we own and publish, for a
    test that compiles what adopt writes on whatever TeX is installed (docs/showcase.md)."""
    from PIL import Image
    for deck in sorted(p for p in CORPUS.iterdir() if (p / "target.json").exists()):
        if names and deck.name not in names:
            continue
        target = json.loads((deck / "target.json").read_text(encoding="utf-8"))
        target["source"] = {"title": target.get("source", {}).get("title")}
        dest = out / deck.name
        (dest / "images").mkdir(parents=True, exist_ok=True)

        def shrink(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in ("file", "background_file") and isinstance(v, str) and Path(v).exists():
                        src = Path(v)
                        with Image.open(src) as img:
                            img.seek(0)
                            small = img.copy()
                        small.thumbnail((FIXTURE_SIDE, FIXTURE_SIDE))
                        if src.suffix.lower() in (".jpg", ".jpeg") and small.mode not in ("RGB", "L"):
                            small = small.convert("RGB")
                        small.save(dest / "images" / src.name)
                        node[k] = f"images/{src.name}"
                    else:
                        shrink(v)
            elif isinstance(node, list):
                for v in node:
                    shrink(v)
        shrink(target)
        (dest / "target.json").write_text(json.dumps(target, ensure_ascii=False, indent=1) + "\n",
                                          encoding="utf-8")
        print(f"{dest}: {len(target['slides'])} slides")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("decks").add_argument("names", nargs="*")
    sub.add_parser("capture").add_argument("names", nargs="*")
    r = sub.add_parser("run")
    r.add_argument("names", nargs="*")
    r.add_argument("--tag", default="showcase")
    r.add_argument("--jobs", type=int, default=3)
    g = sub.add_parser("gallery")
    g.add_argument("out")
    g.add_argument("--tag", default="showcase")
    s = sub.add_parser("swipe")
    s.add_argument("site")
    s.add_argument("dest", nargs="?", default="docs/media/adopt-swipe.gif")
    sub.add_parser("fixture").add_argument("names", nargs="*")
    a = ap.parse_args(argv)
    if a.cmd == "fixture":
        write_fixture(FIXTURE, a.names or None)
        return
    # the adopt bench reads its corpus folder at import
    os.environ["B2S_ADOPT_CORPUS"] = str(CORPUS)
    if a.cmd == "decks":
        from beamer2slides.devtools.showcase.decks import build
        build(a.names or None)
    elif a.cmd == "capture":
        import json

        from beamer2slides.devtools import adopt_bench
        from beamer2slides.devtools.showcase.decks import MANIFEST
        for name, d in json.loads(MANIFEST.read_text(encoding="utf-8")).items():
            if not a.names or name in a.names:
                adopt_bench.capture(d["id"], name, refresh=True)
    elif a.cmd == "run":
        from beamer2slides.devtools import adopt_bench
        adopt_bench.main(["run", *a.names, "--tag", a.tag, "--jobs", str(a.jobs)])
    elif a.cmd == "swipe":
        from beamer2slides.devtools.showcase.gallery import swipe_gif
        swipe_gif(Path(a.site), Path(a.dest))
    else:
        from beamer2slides.devtools.showcase.gallery import write
        write(Path(a.out), a.tag)


if __name__ == "__main__":
    main()
