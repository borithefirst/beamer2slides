"""Two batches of one conversion, and the one Google commits LAST wins.

`tools/probe_batch_parallelism.py` measured that several `batchUpdate` calls may be in flight on
one presentation without either losing anything - which is true of the objects they create, and
was read as a licence to write the layouts on a thread of its own while the slides are written.
It is not true of a *placeholder*: a slide's TITLE placeholder inherits its layout placeholder's
transform until it is given one of its own, so the layout batch and the content batch that gives
it one are writing the same box from two sides, and the later commit wins. A layout batch that
lands second takes every title's own box away again - silently, with no warning and every request
accepted - and the deck's titles all sit at the layout's.

This converts one PDF into N folders at once (each its own process: PDFium is not thread-safe)
and counts, per deck, the title placeholders whose transform is still their layout parent's.

Measured before the fix, three conversions of `tests/decks/sync/out/chain.pdf` at once, with
every request naming a title placeholder logged with its thread and the time it landed:

    titlerace0     10/10 titles left at the layout's box   layout batch done after the first
                                                           content batch (2.79 s vs 2.33 s)
    titlerace1      0/10                                   layout batch done first
    titlerace2      0/10                                   layout batch done first

The requests were identical in all three; only the order they landed in differed. With
`build_deck` joining the layout future before it dispatches any content batch, all three are 0/10.
"""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from beamer2slides.google_auth import slides_service  # noqa: E402
from beamer2slides.gslides import execute  # noqa: E402

DEFAULT_PDF = ROOT / "tests" / "decks" / "sync" / "out" / "chain.pdf"


def convert(pdf: Path, folder: Path) -> None:
    log = folder.with_suffix(".log")
    folder.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as out:
        subprocess.run([sys.executable, "-m", "beamer2slides", "convert", str(pdf), "--out", str(folder)],
                       cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, check=True)


def inherited_titles(pid: str, api) -> tuple[int, int]:
    """How many of the deck's title placeholders still sit at their layout parent's transform?"""
    pres = execute(api.presentations().get(presentationId=pid))
    lay = {p["objectId"]: {pe["objectId"]: pe.get("transform") for pe in p.get("pageElements", [])}
           for p in pres["layouts"] + pres["masters"]}
    same = total = 0
    for s in pres["slides"]:
        page = s.get("slideProperties", {}).get("layoutObjectId")
        for pe in s.get("pageElements", []):
            ph = (pe.get("shape") or {}).get("placeholder", {})
            if ph.get("type") in ("TITLE", "CENTERED_TITLE"):
                total += 1
                same += pe.get("transform") == lay.get(page, {}).get(ph.get("parentObjectId"))
    return same, total


def main() -> None:
    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    folders = [ROOT / "out" / f"titlerace{i}" for i in range(n)]  # fixed names: the decks are rebuilt
    print(f"{n} conversions of {pdf.name} at once")
    with ThreadPoolExecutor(n) as pool:
        list(pool.map(lambda f: convert(pdf, f), folders))

    api = slides_service()
    for folder in folders:
        pid = json.loads((folder / "emit.json").read_text(encoding="utf-8"))["presentationId"]
        same, total = inherited_titles(pid, api)
        print(f"{folder.name:14s} {same}/{total} titles left at the layout's box")


if __name__ == "__main__":
    main()
