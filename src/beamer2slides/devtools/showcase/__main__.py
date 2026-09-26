"""python tools/showcase.py decks [NAME...] | capture [NAME...] | run [--tag T] | gallery OUT

  decks     build the showcase decks in Drive (in place when decks.json has them), share them view-only
  capture   adopt_bench capture of each deck into out/showcase-corpus
  run       adopt_bench run over that corpus (tag `showcase` unless --tag)
  gallery   the site: OUT/index.html + OUT/img/ (+ each deck's adopted source as a .zip)
"""

import argparse
import os

from beamer2slides.devtools.showcase.decks import CORPUS


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
    a = ap.parse_args(argv)
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
    else:
        from pathlib import Path

        from beamer2slides.devtools.showcase.gallery import write
        write(Path(a.out), a.tag)


if __name__ == "__main__":
    main()
