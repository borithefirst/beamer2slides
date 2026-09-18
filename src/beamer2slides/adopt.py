"""A deck nobody converted, as a beamer source: the starting point `pull`/`converge` never had.

`pull` refines a source until its conversion matches a deck, which needs a source to begin with.
For a deck this repository produced that is the .tex it came from; for a foreign deck - one a person
built in Slides - there is none, and the loop cannot make one: a document with no frames compiles to
a PDF with no pages, and one empty frame per slide leaves `slide_missing` guessing which slide is
which (measured: it oscillates and never converges).

So this writes the skeleton and the loop does the rest. The skeleton places every element where the
deck has it - a `textblock*` per text box, per picture, per panel - because that is what a foreign
deck's geometry *is*: boxes the person dragged, not flow text a theme laid out. Idiomatic beamer
(frame titles, `itemize` in the flow) would be a guess about intent, and every guess that misses
costs the loop a `geometry` round to escalate back into a textblock (`inverse.Planner.geometry`).
Absolute first is cheaper and closer; `--flow` asks for the readable version instead, which is worth
it when the deck really is a talk laid out by a template.

What the loop is left to do: the drift between where a textblock puts a baseline and where the deck
wants it (it corrects textblocks by the measured error), the words, and the pictures it can fetch.
"""

import json
import os
import shutil
from pathlib import Path

from .inverse import (Context, TEXTPOS, body_style, colour_name, frame_latex, picture_block,
                      reading_order, textblock_latex)

# beamer's own page sizes, by the class option that asks for them (`deck_ir.BEAMER_SIZES`).
ASPECTS = {(453.54, 255.12): "aspectratio=169", (453.54, 283.46): "aspectratio=1610",
           (362.83, 272.13): ""}


def page_option(size: list[float]) -> str:
    """The class option for a page this size, the nearest beamer offers (a deck is 16:9 unless the
    person changed it, and Slides only ever makes 16:9 through the API)."""
    if not size:
        return "aspectratio=169"
    best = min(ASPECTS, key=lambda s: abs(s[0] / s[1] - size[0] / size[1]))
    return ASPECTS[best]


def level_style(target: dict):
    """The base style new text takes at a list level, read off the target itself. The loop reads
    this off the candidate (`Planner.level_style`), which a bootstrap does not have yet."""
    def style_for(p: dict) -> dict:
        level = p.get("level", 0) if p.get("bullet") else None
        counts: dict = {}
        for s in target["slides"]:
            for e in s["elements"]:
                if e["kind"] != "text" or e.get("role") not in ("body", None):
                    continue
                for q in e["paragraphs"]:
                    if (q.get("level", 0) if q.get("bullet") else None) != level:
                        continue
                    for run in q["runs"]:
                        k = (round(run.get("size") or 0, 1), run.get("color"), run.get("family"))
                        counts[k] = counts.get(k, 0) + len(run["text"])
        if not counts:
            return body_style(target)
        size, colour, family = max(counts, key=counts.get)
        return {"size": size, "color": colour, "family": family, "bold": False, "italic": False}
    return style_for


def element_style(el: dict) -> dict:
    """The style most of a text box's letters are in: its own base, not the deck's.

    `inverse.runs_latex` writes a run's style only where it differs from a base, on the assumption
    that the document around it already sets that base - true for a source being refined, false for
    one being written from nothing. A foreign deck has no normal text: the pink instruction slides
    are 9 pt crimson, the section titles 26 pt blue, and a deck-wide base would leave one of them
    unstated and therefore black."""
    counts: dict = {}
    for p in el.get("paragraphs", []):
        for r in p["runs"]:
            k = (round(r.get("size") or 0, 1), r.get("color"), r.get("family"), bool(r.get("bold")))
            counts[k] = counts.get(k, 0) + len(r["text"])
    if not counts:
        return {"size": None, "color": None, "family": "sans", "bold": False, "italic": False}
    size, colour, family, bold = max(counts, key=counts.get)
    return {"size": size, "color": colour, "family": family, "bold": bold, "italic": False}


def base_lead(style: dict, ctx: Context) -> str:
    """The switches that make `style` the base inside a textblock."""
    from .inverse import size_switch
    out = []
    if style.get("size"):
        out.append(size_switch(style["size"], ctx.pt_option))
    if style.get("family") == "mono":
        out.append("\\ttfamily")
    elif style.get("family") == "serif":
        out.append("\\rmfamily")
    if style.get("bold"):
        out.append("\\bfseries")
    if style.get("color"):
        out.append(f"\\color{{{colour_name(style['color'], ctx.colours)}}}")
    return "".join(out)


# fontspec's key for a style, by the suffix a font file's name ends in.
FONT_STYLES = {"regular": "UprightFont", "": "UprightFont", "bold": "BoldFont",
               "italic": "ItalicFont", "oblique": "ItalicFont",
               "bolditalic": "BoldItalicFont", "boldoblique": "BoldItalicFont"}


def font_dirs() -> list[Path]:
    """Where to look for the typefaces a deck names: the ones this repository ships with its themes,
    then the machine's own - or only `$B2S_FONTS`, when it is set, which is how one points adopt at
    a folder of the deck's own fonts (and how the tests get an answer that does not depend on what
    this machine happens to have installed)."""
    only = [Path(p.strip()) for p in os.environ.get("B2S_FONTS", "").split(os.pathsep) if p.strip()]
    if only:
        return [p for p in only if p.is_dir()]
    out: list[Path] = []
    themes = Path(__file__).resolve().parents[2] / "themes"
    if themes.is_dir():                                 # not in an installed wheel
        out += sorted(p for p in themes.glob("*/fonts") if p.is_dir())
    if os.name == "nt":
        out += [Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts",
                Path(os.environ.get("LOCALAPPDATA", ".")) / "Microsoft" / "Windows" / "Fonts"]
    else:
        out += [Path.home() / ".fonts", Path.home() / ".local" / "share" / "fonts",
                Path.home() / "Library" / "Fonts", Path("/Library/Fonts"), Path("/usr/share/fonts")]
    return [p for p in out if p.is_dir()]


_FAMILIES: dict[tuple, dict[str, dict[str, Path]]] = {}


def font_candidates() -> dict[str, dict[str, Path]]:
    """Every family the folders offer, by the stem its files share, each keyed by fontspec's name
    for the style. Read once per set of folders: they are large and the answer does not change."""
    dirs = tuple(font_dirs())
    if dirs not in _FAMILIES:
        groups: dict[str, dict[str, Path]] = {}
        for folder in dirs:
            for f in sorted(list(folder.glob("*.tt[fc]")) + list(folder.glob("*.otf"))
                            + list(folder.glob("*/*.tt[fc]")) + list(folder.glob("*/*.otf"))):
                stem, _, suffix = f.stem.partition("-")
                style = FONT_STYLES.get("".join(c for c in suffix.lower() if c.isalpha()))
                if style is not None:
                    groups.setdefault(stem, {}).setdefault(style, f)
        _FAMILIES[dirs] = {k: v for k, v in groups.items() if "UprightFont" in v}
    return _FAMILIES[dirs]


def flatten(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())


def font_family(name: str, want: str, near: str = "") -> dict[str, Path]:
    """The files of the family a deck font names, keyed by fontspec's style, or {}.

    A family is the one the deck asked for when its stem is the deck's font name with something
    after it (the deck says "Google Sans", the files are `GoogleSansFlex-*.ttf`) **and** its own
    name reads as the same kind of typeface: `GoogleSansCode` begins with "Google Sans" too, and is
    a monospace, so without that test a deck's prose would be set in its code face.

    When the machine does not have it, the nearest family of the same kind to one that *was* found
    (`near`) stands in, rather than LaTeX's own. That is not cosmetic: the DevFest template's quote
    slides are Space Mono, which is on no machine here, and Latin Modern Mono is narrow enough to
    break every one of their lines in another place - 0.42 ink overlap against 0.68 for Google Sans
    Code, which at least is the same kind of face as the rest of the deck."""
    from .deck_ir import family_of
    flat, kin = flatten(name), flatten(near)
    asked: tuple[int, str, dict] = (10 ** 6, "", {})
    fallback: tuple[int, str, dict] = (0, "", {})
    for stem, files in font_candidates().items():
        low = flatten(stem)
        if family_of(stem) != want:
            continue
        if flat and (low.startswith(flat) or flat.startswith(low)) and abs(len(low) - len(flat)) < asked[0]:
            asked = (abs(len(low) - len(flat)), stem, files)
        shared = len(os.path.commonprefix([low, kin])) if kin else 0
        if shared > fallback[0]:
            fallback = (shared, stem, files)
    _, stem, files = asked if asked[2] else fallback
    return {"stem": stem, **files} if files else {}


def font_preamble(target: dict, tree: Path | None) -> list[str]:
    """fontspec lines for the typefaces the deck is written in, and the files beside the source.

    A foreign deck is written in the person's fonts, not the converter's three, and helvet in place
    of them is ink in the wrong shape on every slide that has words (measured on the DevFest
    template: 0.702 -> 0.720 ink overlap, the text-only slides moving most). What is not on this
    machine keeps its substitute, which is what the loop reports as a style it cannot close."""
    counts: dict = {}
    for s in target["slides"]:
        for e in s["elements"]:
            for p in e.get("paragraphs", []):
                for r in p["runs"]:
                    k = (r.get("family") or "sans", r.get("font") or "")
                    counts[k] = counts.get(k, 0) + len(r["text"])
    wanted: dict[str, str] = {}
    for (fam, font), _n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if font and fam not in wanted:
            wanted[fam] = font
    lines, found = [], ""
    for fam, command in (("sans", "setsansfont"), ("serif", "setmainfont"), ("mono", "setmonofont")):
        files = font_family(wanted[fam], fam, found) if fam in wanted else {}
        if not files:
            continue
        stem = files.pop("stem")
        found = found or stem                           # what the rest of the deck is set in
        low, asked = flatten(stem), flatten(wanted[fam])
        if not (low.startswith(asked) or asked.startswith(low)):
            print(f"  {wanted[fam]}: not on this machine, set in {stem}")
        opts = [f"{k}=*-{files[k].stem.partition('-')[2]}" if "-" in files[k].stem else f"{k}=*"
                for k in ("UprightFont", "BoldFont", "ItalicFont", "BoldItalicFont") if k in files]
        if tree is not None:
            for f in files.values():
                dest = tree / "fonts" / f.name
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(f, dest)
        where = "Path=fonts/," if tree is not None else \
            "Path=" + next(iter(files.values())).parent.as_posix().rstrip("/") + "/,"
        ext = next(iter(files.values())).suffix
        lines.append(f"\\{command}{{{stem}}}[{where}Extension={ext},{','.join(opts)}]")
    return ["\\usepackage{fontspec}"] + lines if lines else []


def picture_of(el: dict, tree: Path | None):
    """The `Picture` for an element whose file the deck gave us, copied into the source tree so the
    tree stands on its own (the download sits in the work folder, which is scratch). None when the
    deck would not give the file: the loop then reports `element_missing`, which says so."""
    from .inverse import Picture, natural_size, picture_slug
    path = Path(el["file"]) if el.get("file") else None
    if path is None or not path.exists():
        return None
    if tree is None:
        return Picture(path.name, path, natural_size(path))
    rel = f"figures/{picture_slug(el.get('alt'))}-{(el.get('sha1') or path.stem)[:8]}{path.suffix}"
    dest = tree / rel
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    return Picture(rel, dest, natural_size(dest))


ROUNDED = ("round", "pill", "flow_chart_terminator")


def tikz_block(body: str, x0: float, y0: float, w: float, h: float, ind: str) -> str:
    """A tikzpicture at page coordinates, hanging from the top of its textblock the way a picture
    does: everything is drawn below the origin, so the picture has no height and the whole of it is
    depth, which puts the origin on the block's first baseline and therefore at (x0, y0)."""
    return (f"{ind}\\begin{{textblock*}}{{{max(w, 0.1):.1f}pt}}({x0:.1f}pt,{y0:.1f}pt)\n"
            f"{ind}  \\begin{{tikzpicture}}[baseline=(current bounding box.north),inner sep=0pt,outer sep=0pt]\n"
            f"{ind}    {body}\n"
            f"{ind}  \\end{{tikzpicture}}\n"
            f"{ind}\\end{{textblock*}}\n")


def shape_block(el: dict, ctx: Context, ind: str) -> str:
    """A panel, a node of a flow chart or a connector, at its place on the page.

    tikz rather than `\\rule`, because a foreign deck's shapes are not only filled rectangles: the
    flow charts of both templates are outlined boxes with rounded corners, and a rule can say
    neither. A shape with no fill and no outline draws nothing and is left out, so the ink is the
    deck's and nothing else."""
    fill, stroke = el.get("fill"), el.get("outline") or el.get("outline_color")
    if not fill and not stroke:
        return ""
    ctx.packages.add("\\usepackage{tikz}")
    weight = el.get("weight") or 1.0
    opts = []
    if fill:
        opts.append(f"fill={colour_name(fill, ctx.colours)}")
    if stroke:
        opts += [f"draw={colour_name(stroke, ctx.colours)}", f"line width={weight:.2f}pt"]
    if el.get("role") == "line" and el.get("from") and el.get("to"):
        (ax, ay), (bx, by) = el["from"], el["to"]
        x0, y0 = min(ax, bx), min(ay, by)
        tip = "->" if el.get("arrow") and not el.get("arrow_start") else \
              "<-" if el.get("arrow_start") and not el.get("arrow") else \
              "<->" if el.get("arrow_start") else "-"
        body = (f"\\path[{tip},{','.join(o for o in opts if not o.startswith('fill'))}] "
                f"({ax - x0:.1f}pt,{-(ay - y0):.1f}pt) -- ({bx - x0:.1f}pt,{-(by - y0):.1f}pt);")
        return tikz_block(body, x0, y0, abs(bx - ax), abs(by - ay), ind)
    x0, y0, x1, y1 = el["bbox"]
    w, h = max(x1 - x0, 0.1), max(y1 - y0, 0.1)
    shape = (el.get("shape") or el.get("shape_type") or "rectangle").lower()
    if "ellipse" in shape or "oval" in shape:
        body = f"\\path[{','.join(opts)}] ({w / 2:.1f}pt,{-h / 2:.1f}pt) ellipse ({w / 2:.1f}pt and {h / 2:.1f}pt);"
    else:
        radius = f",rounded corners={min(w, h) / 4:.1f}pt" if any(r in shape for r in ROUNDED) else ""
        body = f"\\path[{','.join(opts)}{radius}] (0pt,0pt) rectangle ({w:.1f}pt,{-h:.1f}pt);"
    return tikz_block(body, x0, y0, w, h, ind)


def slide_latex(s: dict, style_for, ctx: Context, flow: bool, tree: Path | None = None,
                deck_bg: str | None = None) -> str:
    """One deck slide as a frame. `flow` writes the readable version (`inverse.frame_latex`: a frame
    title and body text in the flow); otherwise every element keeps its own place."""
    if flow:
        return frame_latex(s, style_for, ctx)
    out = ["\\begin{frame}[plain]"]
    for el in reading_order(s["elements"]):
        if el.get("role") in ("math", "icon"):
            continue                                   # part of a text line, not an element of its own
        if el["kind"] == "shape":
            out.append(shape_block(el, ctx, "  ").rstrip("\n"))
        elif el["kind"] == "image":
            pic = picture_of(el, tree)
            if pic is not None:
                ctx.packages.add(TEXTPOS)
                out.append(picture_block(el, pic, ctx, "  ").rstrip("\n"))
        elif el["kind"] == "text" and el.get("paragraphs"):
            ctx.packages.add(TEXTPOS)
            # A node of a flow chart is one element: its box, then its label on top.
            out.append(shape_block(el, ctx, "  ").rstrip("\n"))
            base = element_style(el)
            out.append(textblock_latex(el, lambda _p, b=base: b, ctx, "  ", reset=True,
                                       lead=base_lead(base, ctx)))
    if s.get("notes"):
        from .inverse import latex_escape
        out.append("  \\note{" + "\n\n".join(latex_escape(p) for p in s["notes"].split("\n") if p.strip()) + "}")
    out.append("\\end{frame}")
    text = "\n".join(x for x in out if x.strip()) + "\n"
    if s.get("background_color") and s["background_color"] != deck_bg:
        # The colour this one slide sits on, in a group so it ends with the frame - the same shape
        # the loop's own `background` translator writes. A deck's decoration is often a picture with
        # transparency (the DevFest backdrop is white dots on nothing), so the colour under it is
        # not a detail: get it wrong and every such slide is the wrong colour end to end.
        name = colour_name(s["background_color"], ctx.colours)
        text = "{\\setbeamercolor{background canvas}{bg=" + name + "}\n" + text + "}\n"
    return text


def preamble(target: dict, ctx: Context, flow: bool, tree: Path | None = None) -> str:
    """A theme that draws nothing. A foreign deck carries its own decoration in its elements, so
    anything beamer adds by itself (navigation bar, headline, footline, frame title style) is ink
    the deck does not have, and every pixel of it is a residual the loop cannot remove."""
    opt = page_option(target["slides"][0].get("size") if target["slides"] else None)
    fonts = font_preamble(target, tree)
    lines = [f"\\documentclass[{opt}]{{beamer}}" if opt else "\\documentclass{beamer}",
             "\\usetheme{default}",
             "\\setbeamertemplate{navigation symbols}{}",
             "\\setbeamertemplate{footline}{}",
             "\\setbeamertemplate{headline}{}",
             "\\setbeamercolor{background canvas}{bg=}",
             *(fonts or ["\\usepackage[T1]{fontenc}", "\\usepackage{helvet}"]),
             "\\renewcommand{\\familydefault}{\\sfdefault}"]
    if not flow:
        lines.append("\\setbeamertemplate{frametitle}{}")
    bg = background_colour(target)
    if bg:
        lines.append(f"\\definecolor{{deckbg}}{{HTML}}{{{bg.lstrip('#').upper()}}}")
        lines.append("\\setbeamercolor{background canvas}{bg=deckbg}")
    return "\n".join(lines)


def background_colour(target: dict) -> str | None:
    """The colour most of the deck's slides sit on: the class carries it, and a slide that differs
    is the loop's `background` residual (`\\setbeamercolor{background canvas}` around that frame)."""
    counts: dict = {}
    for s in target["slides"]:
        c = s.get("background_color")
        if isinstance(c, str):
            counts[c] = counts.get(c, 0) + 1
    return max(counts, key=counts.get) if counts else None


def bootstrap(target: dict, tex: Path, flow: bool = False) -> str:
    """Write `tex` (and return it): a compilable beamer source with a frame per deck slide."""
    ctx = Context()
    style_for = level_style(target)
    tex.parent.mkdir(parents=True, exist_ok=True)
    deck_bg = background_colour(target)
    frames = [slide_latex(s, style_for, ctx, flow, tex.parent, deck_bg) for s in target["slides"]]
    head = preamble(target, ctx, flow, tex.parent)
    extra = sorted(ctx.packages) + [f"\\definecolor{{{n}}}{{HTML}}{{{v}}}" for n, v in sorted(ctx.colours.items())]
    text = head + "\n" + "\n".join(extra) + "\n\n\\begin{document}\n\n" + "\n".join(frames) + "\n\\end{document}\n"
    tex.parent.mkdir(parents=True, exist_ok=True)
    tex.write_text(text, encoding="utf-8")
    return text


def cmd_adopt(deck: str, tex: Path, work: Path | None, apply: bool, out: Path | None, max_iter: int,
              engine: str | None, flow: bool, target_path: Path | None = None):
    """Read a foreign deck, write a source for it, then converge that source onto the deck."""
    from .inverse import run_pull
    tex = Path(tex).resolve()
    work = Path(work).resolve() if work else tex.parent / "out" / "adopt"
    if target_path is not None:
        target = json.loads(Path(target_path).read_text(encoding="utf-8"))
    else:
        from .deck_ir import read_deck
        target = read_deck(deck, images=work / "target-images", foreign=True)
    print(f"deck: {len(target['slides'])} slides read")
    if tex.exists():
        raise SystemExit(f"{tex} exists already: adopt writes a new source tree (use `pull` to refine one)")
    bootstrap(target, tex, flow)
    print(f"wrote {tex} ({len(target['slides'])} frames)")
    return run_pull(target, tex, work, apply, out, max_iter, False, engine)
