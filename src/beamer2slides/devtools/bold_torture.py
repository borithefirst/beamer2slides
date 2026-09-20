"""Does a style the deck says is bold, or italic, actually draw bold or slanted?

A source can lose an emphasis without failing. `\\setsansfont{NTR}[Path=..,Extension=.ttf,
UprightFont=*-Regular]` names no `BoldFont`, so fontspec looks for no second file and the bold series
*is* the upright: `\\textbf` draws the same glyphs, lualatex says nothing, the slide looks finished
and the emphasis is gone. `devtools/edit_robustness` found it on 6 of 24 restyle edits. Nothing in
the fidelity bench can see it either - a word drawn upright where it should be bold costs a handful
of pixels on one slide - so it needs its own judge, and the judge has to be the ink.

What it does, per corpus deck: build the source's real font preamble (`scripts.script_preamble` +
`adopt.font_preamble`, the writers under test - no bootstrap, no slides), then compile one probe page
per declared family with the same words set four ways (upright, `\\bfseries`, `\\itshape`, both), and
measure what came out:

  stroke   2 x ink area / ink outline: the mean width of the strokes, in em. A bold run is
           measurably heavier than the same words upright - or it is not bold.
  slant    the shear that lines the ink up into columns (the classic projection-profile deskew):
           about 0 for an upright face, > 0.1 for one leaning right.

A family fails `bold` when its bold weighs no more than its upright, `italic` when its italic leans
no further, and `bolditalic` when either holds for the two together. Every family the preamble
declares is probed, not only the styles the deck's own text asks for: a person restyling an adopted
slide in Slides reaches the rest (that is how `edit_robustness` found this), and `asked` records
which is which.

  run [NAME...] --jobs N --tag T     probe every deck, one compile each
  report --tag T                     what failed, by deck and by family

What it cannot see: whether the weight is the *right* one (a faked bold is not the family's real
bold, only heavier than its own upright, and a family whose real bold this machine cannot fetch is
still a pass); whether a run the deck sets at weight 600 got 600 rather than 700; whether the face
a style names has the deck's own glyphs at all (arabic-training's `\\textit` picks Arial Italic,
which has no Arabic in it - the probe reads half the ink and calls it "not slanted", which is a
finding but not the one the name says); and anything about a font the machine does not have at all,
which is set in a TeX Gyre stand-in that has all four faces.

What it does see, and this writer does not fix: the styles a *fallback* chain draws. A CJK or
Arabic deck's letters come from `luaotfload.add_fallback`, and `scripts.script_preamble` names one
chain for upright and one for bold, so `\\textit` over Japanese draws the upright chain (jruby-ja).
Giving the chain an italic would mean a third and fourth chain and per-shape `RawFeature`s in a
`\\defaultfontfeatures` that every family reads - and a `FakeSlant` there would slant the real
italic of every family that has one, twice.
The PDF's own font names are no help here and are not used: a static instance cut from a variable
font reports the name it was cut from (every weight of Open Sans says `OpenSans-Regular`), and the
four styles of a .ttc face share one font id, so the names agree while the ink does not.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from .adopt_bench import CORPUS, decks, load_target

SIZE = 40.0        # big enough that one rendered pixel is not the whole difference
ZOOM = 2.0
PROBE_CHARS = 12   # distinct letters per probe word, from the deck's own text

# A bold that weighs no more than this over its upright, or an italic that leans no further than
# this past it, is not drawn. The defect gives exactly 1.00 and 0.00; a fake gives 1.3-1.5 and
# ~0.20, a real bold or italic as much or more.
BOLD_MIN = 1.10
SLANT_MIN = 0.08

STYLES = (("upright", ""), ("bold", "\\bfseries "), ("italic", "\\itshape "),
          ("bolditalic", "\\bfseries\\itshape "))


# ---------------------------------------------------------------------------------------- measuring

def bands(ink: np.ndarray) -> list[tuple[int, int]]:
    """The rows of ink, split where a blank row separates them: one band per probe line."""
    rows = ink.any(axis=1)
    out, start = [], None
    for y, v in enumerate(rows):
        if v and start is None:
            start = y
        elif not v and start is not None:
            out.append((start, y))
            start = None
    if start is not None:
        out.append((start, len(rows)))
    return out


def stroke(m: np.ndarray) -> float:
    """Mean stroke width in pixels: a stroke w wide and L long has area wL and an outline of 2L, so
    2 x area / outline is w. `deck_thumbs.stroke_em` measures the deck's own thumbnails the same way,
    which is what makes the two comparable."""
    area = int(m.sum())
    inner = m.copy()
    inner[1:, :] &= m[:-1, :]
    inner[:-1, :] &= m[1:, :]
    inner[:, 1:] &= m[:, :-1]
    inner[:, :-1] &= m[:, 1:]
    return 2 * area / max(area - int(inner.sum()), 1)


def slant(m: np.ndarray) -> float:
    """The shear that lines the ink up into columns: ~0 for an upright face, > 0 for one leaning
    right. Argmax over the sum of squares of the column histogram - the classic deskew, which reads
    a shape and not a centroid, so a letter that happens to be top-heavy cannot fake it."""
    h, w = m.shape
    ys = np.nonzero(m.any(axis=1))[0]
    if not len(ys):
        return 0.0
    mid = (ys.min() + ys.max()) / 2
    best, arg = -1.0, 0.0
    for k in range(-40, 41):
        s = k / 100
        pad = int(abs(s) * h) + 2
        acc = np.zeros(w + 2 * pad)
        for y in range(h):
            shift = int(round(s * (y - mid)))
            acc[pad + shift:pad + shift + w] += m[y]
        v = float((acc ** 2).sum())
        if v > best:
            best, arg = v, s
    return arg


def measure(pdf: Path, pages: int) -> list[list[dict]]:
    """Per page, one reading per band: ink, width, stroke (em) and slant."""
    from beamer2slides.pdf import Document
    doc = Document(str(pdf))
    out = []
    try:
        for i in range(min(pages, len(doc))):
            a = np.asarray(doc[i].render(ZOOM))[:, :, :3].astype(np.int16)
            ink = np.abs(a - 255).max(axis=-1) > 127
            page = []
            for y0, y1 in bands(ink):
                m = ink[y0:y1]
                xs = np.nonzero(m.any(axis=0))[0]
                page.append({"ink": int(m.sum()), "width": int(xs.max() - xs.min() + 1) if len(xs) else 0,
                             "stroke": round(stroke(m) / ZOOM / SIZE, 4), "slant": round(slant(m), 3)})
            out.append(page)
    finally:
        doc.close()
    return out


# ------------------------------------------------------------------------------------------- probes

def deck_letters(target: dict) -> dict[str, dict[str, int]]:
    """Which characters the deck sets in each of its fonts, and how often."""
    seen: dict[str, dict[str, int]] = {}
    for s in target["slides"]:
        for e in s["elements"]:
            for p in e.get("paragraphs", []):
                for r in p.get("runs", []):
                    d = seen.setdefault(r.get("font") or "", {})
                    for c in r["text"]:
                        if not c.isspace():
                            d[c] = d.get(c, 0) + 1
    return seen


def probe_text(letters: dict[str, int]) -> str:
    """The words a family is probed with: the deck's own most used characters for it, so what is
    measured is the letters that deck actually sets - a Hebrew or CJK deck's letters go to the babel
    font or the fallback chain, and that is the chain the reader sees. Latin when the deck says
    nothing.

    Always with two Latin letters on the end: lualatex refuses to embed a face no glyph is taken
    from ("there are no glyphs in the subset"), and a family whose letters all come from elsewhere -
    arabic-training's Arial against an all-Arabic deck - has exactly that for all four of its faces."""
    chars = [c for c, _ in sorted(letters.items(), key=lambda kv: -kv[1])
             if c.isprintable() and c not in "\\{}$&#^_~%"][:PROBE_CHARS]
    if len([c for c in chars if c.isalnum()]) < 4:
        return "Hamburgefonstiv"
    return "".join(chars) + "Hn"


def asked_styles(target: dict) -> dict[str, set[str]]:
    """Which of the four styles the deck's own runs ask for, per font name."""
    out: dict[str, set[str]] = {}
    for s in target["slides"]:
        for e in s["elements"]:
            for p in e.get("paragraphs", []):
                for r in p.get("runs", []):
                    if not r["text"].strip():
                        continue
                    name = ("bolditalic" if r.get("bold") and r.get("italic") else
                            "bold" if r.get("bold") else "italic" if r.get("italic") else "upright")
                    out.setdefault(r.get("font") or "", set()).add(name)
    return out


def declared(target: dict, tree: Path) -> tuple[list[str], list[tuple[str, str, str]]]:
    """(preamble lines, [(family label, LaTeX switch, deck font name)]) for one deck: the real
    writers, into a real tree, so what is probed is exactly what `adopt` would have written."""
    from beamer2slides import adopt, scripts
    from beamer2slides.inverse import Context
    ctx = Context()
    lines = scripts.script_preamble(target, tree) + adopt.font_preamble(target, tree, ctx)
    # which deck font each of the document's three kinds was set in: font_preamble's own ranking
    counts: dict = {}
    for s in target["slides"]:
        for e in s["elements"]:
            for p in e.get("paragraphs", []):
                for r in p.get("runs", []):
                    k = (r.get("family") or "sans", r.get("font") or "")
                    counts[k] = counts.get(k, 0) + len(r["text"])
    main: dict[str, str] = {}
    for (fam, font), _n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if font:
            main.setdefault(fam, font)
    fams = []
    for fam, command, marker in (("sans", "\\sffamily ", "setsansfont"),
                                 ("serif", "\\rmfamily ", "setmainfont"),
                                 ("mono", "\\ttfamily ", "setmonofont")):
        if any(line.startswith("\\" + marker) for line in lines):
            fams.append((fam, command, main.get(fam, "")))
    for font, command in sorted((getattr(ctx, "font_switches", None) or {}).items()):
        fams.append((f"switch {font}", command + " ", font))
    return lines, fams


def probe_tex(lines: list[str], fams: list[tuple[str, str, str]], texts: dict[str, str]) -> str:
    """One page per family, one band per style, the same words in all four."""
    head = [r"\documentclass{article}",
            r"\usepackage[paperwidth=40cm,paperheight=18cm,margin=10mm]{geometry}",
            *lines, r"\pagestyle{empty}", r"\begin{document}",
            r"\fontsize{%g}{%g}\selectfont\noindent" % (SIZE, SIZE * 1.25)]
    body = []
    for k, (_label, command, font) in enumerate(fams):
        if k:
            body.append(r"\newpage\noindent")
        for _name, switch in STYLES:
            body.append("{%s%s%s}\\par\\vspace{10mm}\\noindent" % (command, switch, texts[font]))
    return "\n".join(head + body + [r"\end{document}"]) + "\n"


# ---------------------------------------------------------------------------------------------- run

def verdicts(fams: list[tuple[str, str, str]], pages: list[list[dict]], asked: dict) -> list[dict]:
    """One row per (family, style): what came out, and whether it counts as drawn."""
    rows = []
    for (label, _cmd, font), page in zip(fams, pages):
        if len(page) != len(STYLES) or page[0]["ink"] < 200:
            rows.append({"family": label, "font": font, "style": "-", "verdict": "unreadable",
                         "bands": len(page)})
            continue
        base = page[0]
        for (name, _switch), got in zip(STYLES, page):
            if name == "upright":
                continue
            row = {"family": label, "font": font, "style": name,
                   "asked": name in asked.get(font, ()),
                   "stroke": got["stroke"], "upright_stroke": base["stroke"],
                   "heavier": round(got["stroke"] / base["stroke"], 3) if base["stroke"] else 0.0,
                   "slant": got["slant"], "upright_slant": base["slant"],
                   "leans": round(got["slant"] - base["slant"], 3)}
            bad = []
            if "bold" in name and row["heavier"] < BOLD_MIN:
                bad.append("not bold")
            if "italic" in name and row["leans"] < SLANT_MIN:
                bad.append("not slanted")
            row["verdict"] = ", ".join(bad) if bad else "drawn"
            rows.append(row)
    return rows


def run_one(name: str, tag: str) -> dict:
    """Probe one deck. Never raises: a crash is the result."""
    folder = CORPUS / name
    run = folder / "torture" / tag
    shutil.rmtree(run, ignore_errors=True)
    run.mkdir(parents=True)
    res: dict = {"deck": name, "tag": tag}
    t0 = time.perf_counter()
    try:
        target = load_target(folder, None)
        tree = run / "tree"
        tree.mkdir(parents=True, exist_ok=True)
        lines, fams = declared(target, tree)
        res["families"] = len(fams)
        if not fams:
            res["rows"] = []
            return finish(run, res, t0)
        letters = deck_letters(target)
        texts = {font: probe_text(letters.get(font, {})) for _l, _c, font in fams}
        tex = tree / "probe.tex"
        tex.write_text(probe_tex(lines, fams, texts), encoding="utf-8")
        # TeX logs are not the console's code page: decode them as UTF-8 and never let a byte
        # cp1252 has no character for pass for a compile failure
        subprocess.run(["lualatex", "-interaction=nonstopmode", "-halt-on-error", tex.name],
                       cwd=tex.parent, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600)
        pdf = tex.with_suffix(".pdf")
        if not pdf.exists():
            log = tex.with_suffix(".log")
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            errors = [ln for ln in text.splitlines() if ln.startswith("!")]
            res["error"] = "compile: " + ("; ".join(errors[:4]) or text[-1500:])
            return finish(run, res, t0)
        res["rows"] = verdicts(fams, measure(pdf, len(fams)), asked_styles(target))
    except Exception as exc:                                     # noqa: BLE001 - the crash is a finding
        res["error"] = f"{type(exc).__name__}: {exc}"[:1500]
        res["traceback"] = traceback.format_exc()[-3000:]
    return finish(run, res, t0)


def finish(run: Path, res: dict, t0: float) -> dict:
    res["seconds"] = round(time.perf_counter() - t0, 1)
    rows = res.get("rows") or []
    res["failed"] = sum(1 for r in rows if r["verdict"] not in ("drawn", "unreadable"))
    res["drawn"] = sum(1 for r in rows if r["verdict"] == "drawn")
    (run / "result.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def line(res: dict) -> str:
    if res.get("error"):
        return f"{res['deck']:<24} {res['seconds']:5.1f}s  ERROR {res['error'][:90]}"
    bad = [r for r in res.get("rows") or [] if r["verdict"] not in ("drawn", "unreadable")]
    mark = f"FAIL {len(bad)}" if bad else "ok"
    return (f"{res['deck']:<24} {res['seconds']:5.1f}s  {res.get('families', 0):2} families  "
            f"{res.get('drawn', 0):2} drawn  {mark}")


def report(tag: str) -> None:
    rows, decks_run, failed_decks = [], 0, []
    for name in decks():
        path = CORPUS / name / "torture" / tag / "result.json"
        if not path.exists():
            continue
        res = json.loads(path.read_text(encoding="utf-8"))
        decks_run += 1
        print(line(res))
        bad = [r for r in res.get("rows") or [] if r["verdict"] not in ("drawn", "unreadable")]
        if bad:
            failed_decks.append(name)
        for r in bad:
            print(f"    {r['family'][:28]:<28} {r['style']:<10} {r['verdict']:<22} "
                  f"heavier x{r['heavier']:.2f}  leans {r['leans']:+.2f}"
                  f"{'  (the deck asks for it)' if r.get('asked') else ''}")
        rows += res.get("rows") or []
    bad = [r for r in rows if r["verdict"] not in ("drawn", "unreadable")]
    print(f"\n{decks_run} decks, {len(rows)} (family, style) pairs: "
          f"{sum(1 for r in rows if r['verdict'] == 'drawn')} drawn, {len(bad)} not "
          f"({sum(1 for r in bad if r.get('asked'))} of them the decks' own text asks for), "
          f"{sum(1 for r in rows if r['verdict'] == 'unreadable')} unreadable")
    if failed_decks:
        print("failing decks: " + " ".join(failed_decks))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("names", nargs="*")
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--tag", default="torture")
    p = sub.add_parser("report")
    p.add_argument("--tag", default="torture")
    args = ap.parse_args(argv)
    if args.cmd == "report":
        report(args.tag)
        return
    names = args.names or decks()
    if not names:
        print(f"no corpus decks under {CORPUS}", file=sys.stderr)
        return
    bad = 0
    with ProcessPoolExecutor(max(1, args.jobs)) as pool:
        futures = {pool.submit(run_one, n, args.tag): n for n in names}
        for f in as_completed(futures):
            res = f.result()
            print(line(res), flush=True)
            bad += res.get("failed", 0) + (1 if res.get("error") else 0)
    print(f"\n{len(names)} decks probed under tag {args.tag!r}: {bad} findings")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
