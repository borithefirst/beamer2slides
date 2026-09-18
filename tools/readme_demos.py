"""The live runs behind the front page's sync and adopt pictures (tools/readme_images.py draws them).

    python tools/readme_demos.py sync convert|edit|rewrite|sync     one step at a time, or `all`
    python tools/readme_demos.py adopt                                adopt the demo deck's Pipeline slide

sync: the demo talk (examples/demo/demo.tex) is converted into out/demo-sync (a fixed folder: a re-run
rebuilds the same deck), the Pipeline slide is edited the way a colleague would (tools/deck_edits.py),
the talk is rewritten on the same slide, and `sync` merges the two. A thumbnail and the
presentations.get answer of the slide are kept after each step in out/demo-sync/shots.

adopt: the deck of out/demo (a `convert` of the demo talk) is read back as a deck nobody converted,
`adopt.bootstrap` writes a source for its Pipeline slide into out/demo-adopt/tree, and pdflatex
compiles it. The read-back is cached in out/demo-adopt/pres.json, so a re-run makes no Google call.

Needs the Google credentials and a TeX distribution on PATH.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tools")]
SYNC = ROOT / "out" / "demo-sync"
ADOPT = ROOT / "out" / "demo-adopt"
SLIDE = {"title": "Pipeline"}
BODY = "Text, lists, tables, blocks and simple diagrams become Slides elements."
REWRITE = [("Display math and complex figures become pictures you can still move.",
            "Display math and complex figures become pictures you can still move and resize."),
           ("\\begin{alertblock}{Pictures where it is not}", "\\begin{alertblock}{Pictures where it is not (yet)}")]


def beamer2slides(*args) -> None:
    print(">", "beamer2slides", *args)
    if subprocess.run([sys.executable, "-m", "beamer2slides", *map(str, args)], cwd=ROOT).returncode:
        raise SystemExit(f"beamer2slides {args[0]} failed")


def pdflatex(tex: Path, runs: int = 2) -> None:
    for _ in range(runs):
        r = subprocess.run(["pdflatex", "-interaction=nonstopmode", tex.name], cwd=tex.parent,
                           capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"pdflatex {tex} failed:\n{r.stdout[-1500:]}")


def shot(name: str) -> None:
    """Thumbnail and read-back of the slide, as they are now."""
    from beamer2slides.devtools.deck_edits import LiveDeck
    from beamer2slides.devtools.sync_check import presentation_id
    from beamer2slides.gslides import save_thumbnail
    deck = LiveDeck(presentation_id(str(SYNC)))
    (SYNC / "shots").mkdir(parents=True, exist_ok=True)
    save_thumbnail(deck.api, deck.pid, deck.model.one(SLIDE).id, SYNC / "shots" / f"{name}.png")
    (SYNC / "shots" / f"{name}.json").write_text(json.dumps(deck.model.pres), encoding="utf-8")


def sync_step(step: str) -> None:
    src = SYNC / "src"
    if step == "convert":
        src.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "examples" / "demo" / "demo.tex", src / "demo.tex")
        pdflatex(src / "demo.tex")
        # the folder's deck is this script's own: rebuilding it needs no way back
        beamer2slides("convert", src / "demo.pdf", "--out", SYNC, "--force-rebuild", "--backup", "none")
        shot("1-converted")
    elif step == "edit":
        from beamer2slides.devtools.deck_edits import LiveDeck, add_text_box, bold, recolour
        from beamer2slides.devtools.sync_check import presentation_id
        deck = LiveDeck(presentation_id(str(SYNC)))
        recolour(deck, SLIDE, "Slides elements", "#188038", BODY)
        bold(deck, SLIDE, "Slides elements", BODY)
        add_text_box(deck, SLIDE, "Love this slide! — Sam", [470, 322, 230, 30])
        shot("2-edited")
    elif step == "rewrite":
        tex = (src / "demo.tex").read_text(encoding="utf-8")
        for old, new in REWRITE:
            if old not in tex:
                raise SystemExit(f"not in the talk (run `convert` first): {old}")
            tex = tex.replace(old, new)
        (src / "demo.tex").write_text(tex, encoding="utf-8")
        pdflatex(src / "demo.tex")
    elif step == "sync":
        beamer2slides("sync", src / "demo.pdf", "--deck", SYNC)
        shot("3-synced")


def adopt_slide() -> None:
    from beamer2slides import adopt
    from beamer2slides.deck_ir import deck_ir, fetch_url, presentation_id
    cache = ADOPT / "pres.json"
    if not cache.exists():
        from beamer2slides.google_auth import slides_service
        from beamer2slides.gslides import execute
        ADOPT.mkdir(parents=True, exist_ok=True)
        pid = presentation_id(str(ROOT / "out" / "demo"))
        cache.write_text(json.dumps(execute(slides_service().presentations().get(presentationId=pid))), encoding="utf-8")
    pres = json.loads(cache.read_text(encoding="utf-8"))
    from beamer2slides.devtools.sync_check import Model
    keep = Model(pres).one(SLIDE).index
    pres["slides"] = [pres["slides"][keep]]
    size = json.loads((ROOT / "out" / "demo" / "deck.json").read_text(encoding="utf-8"))["slides"][0]["size"]
    ir = deck_ir(pres, size, None, fetch_url, ADOPT / "images", foreign=True)
    shutil.rmtree(ADOPT / "tree", ignore_errors=True)
    tex = ADOPT / "tree" / "main.tex"
    adopt.bootstrap(ir, tex)
    pdflatex(tex, 1)
    print("wrote", tex.relative_to(ROOT), "and main.pdf")


def main() -> None:
    what = sys.argv[1:] or ["-h"]
    if what[0] == "adopt":
        adopt_slide()
    elif what[0] == "sync" and len(what) == 2:
        for step in (["convert", "edit", "rewrite", "sync"] if what[1] == "all" else [what[1]]):
            sync_step(step)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
