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

from .adopt_shapes import turned_text
from .inverse import (Context, TEXTPOS, body_style, colour_name, frame_latex, paragraphs_latex,
                      picture_block)

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


WINDOWS_STYLES = {"bi": "BoldItalicFont", "bd": "BoldFont", "z": "BoldItalicFont", "b": "BoldFont",
                  "i": "ItalicFont"}


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
        # Windows names a family's styles by a short suffix and no dash: arial.ttf, arialbd.ttf,
        # ariali.ttf, arialbi.ttf (georgiaz, verdanaz for bold italic). Read as families of their
        # own, Arial had no bold, fontspec set every bold word in the regular face, and the cs161
        # decks lost all their bold (slide 4's "Confidentiality").
        for stem in sorted(groups, key=len, reverse=True):
            for suffix, style in WINDOWS_STYLES.items():
                root = stem[:-len(suffix)]
                if stem.lower().endswith(suffix) and root in groups and root != stem \
                        and set(groups[stem]) == {"UprightFont"}:
                    groups[root].setdefault(style, groups.pop(stem)["UprightFont"])
                    break
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
        # The nearest name of the same kind: to the family already found for the rest of the deck, or
        # else to the one asked for ("Google Sans Text" -> GoogleSansFlex, not helvet). Four letters
        # at least, so "Arial" does not take whatever else starts with "A".
        shared = max(len(os.path.commonprefix([low, kin])) if kin else 0,
                     len(os.path.commonprefix([low, flat])) if flat else 0)
        if shared >= 4 and shared > fallback[0]:
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
            # Always fontspec, so the source is lualatex and Unicode throughout (a deck's text is
            # any script; pdflatex stops at the first letter it has no definition for): what the
            # machine has no family for is set in TeX Gyre, the metric clones of Helvetica, Times
            # and Courier that every TeX distribution carries. Sans is the document's family, so it
            # is always declared; the others only when the deck has words in them.
            if fam != "sans" and fam not in wanted:
                continue
            gyre = GYRE[fam]
            lines.append(f"\\{command}{{{gyre}}}[Extension=.otf,UprightFont=*-regular,BoldFont=*-bold,"
                         "ItalicFont=*-italic,BoldItalicFont=*-bolditalic]")
            continue
        stem = files.pop("stem")
        found = found or stem                           # what the rest of the deck is set in
        low, asked = flatten(stem), flatten(wanted[fam])
        if not (low.startswith(asked) or asked.startswith(low)):
            print(f"  {wanted[fam]}: not on this machine, set in {stem}")
        opts = [f"{k}=*-{files[k].stem.partition('-')[2]}" if "-" in files[k].stem else
                f"{k}=*" if files[k].stem == stem else f"{k}={files[k].stem}"
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
    return ["\\usepackage{fontspec}"] + lines


GYRE = {"sans": "texgyreheros", "serif": "texgyretermes", "mono": "texgyrecursor"}


def picture_of(el: dict, tree: Path | None):
    """The `Picture` for an element whose file the deck gave us, copied into the source tree so the
    tree stands on its own (the download sits in the work folder, which is scratch). None when the
    deck would not give the file: the loop then reports `element_missing`, which says so."""
    from .inverse import Picture, natural_size, picture_slug
    from .inverse import LATEX_PICTURES
    path = Path(el["file"]) if el.get("file") else None
    if path is None or not path.exists():
        return None
    suffix = path.suffix.lower()
    if suffix not in LATEX_PICTURES:
        # GIF, WebP, BMP, TIFF: what graphicx cannot read becomes a PNG of its first frame, as
        # `pull` does; SVG and the Windows metafiles Pillow cannot draw are left out and said so
        if suffix in (".svg", ".emf", ".wmf", ".img"):
            print(f"  {path.name}: {suffix[1:].upper()} pictures can't be included by LaTeX; left out")
            return None
        suffix = ".png"
    if tree is None:
        return Picture(path.name, path, natural_size(path))
    rel = f"figures/{picture_slug(el.get('alt'))}-{(el.get('sha1') or path.stem)[:8]}{suffix}"
    dest = tree / rel
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        if suffix == path.suffix.lower():
            shutil.copyfile(path, dest)
        else:
            from PIL import Image
            with Image.open(path) as img:
                img.seek(0)
                img.convert("RGBA").save(dest, "PNG")
    return Picture(rel, dest, natural_size(dest))


# ------------------------------------------------------------------------------------ text boxes
#
# Slides' text model, as emit calibrated it on Google's renderer (docs/calibration.md, Slides pt):
# a line of size z is a box ASCENT_EM z above its baseline and LINE_EM z - ASCENT_EM z below it;
# lineSpacing r >= 1 adds (r - 1) LINE_EM z under the line, r < 1 takes (1 - r) LINE_EM z away, three
# quarters of it above (`emit.extra_above` / `extra_below`). Lines stack box on box, so the pitch
# between two lines is the lower half of one plus the upper half of the next, and spaceBelow +
# spaceAbove between paragraphs - except between two list items when the paragraph's spacingMode
# collapses lists (the cs161 masters say COLLAPSE_LISTS and spaceBelow 12 pt, and the thumbnails
# show no gap between items). The first line's box starts BASELINE_A below the box top, the text
# PAD_X inside its left and right edges; a middle-aligned box centres the stack of line boxes (one
# line: baseline 0.362 em below the middle, tools/probe_middle.py). Measured again on cs161-tls
# slide 4's thumbnail: first baseline 122.2 pt for a box at 98.17 with 18 pt text (6.48 + 0.968 x 18
# = 122.07), 19.35 pt between two 14 pt lines at 115% (1.2 x 14 x 1.15 = 19.32), text starting at
# indentStart and the bullet glyph ending 0.08 em before indentFirstLine.
#
# TeX is made to follow it line by line rather than approximately: every line within a paragraph is
# placed by \baselineskip = the Slides pitch (\lineskiplimit -\maxdimen, so glyph heights never push
# a line down), and the step from one paragraph to the next is set through \prevdepth, which TeX
# reads when it puts the next paragraph's first line (a strut as tall as the Slides line box above
# its baseline) under the last one. Interword spaces are the font's own with no shrink and no
# hyphenation, so a line holds what a browser's line holds and breaks fall where Slides breaks them.

SLIDES_TEXT = (
    "\\newcommand{\\slidesize}[1]{\\fontsize{#1}{#1}\\selectfont"
    "\\spaceskip=\\fontdimen2\\font plus\\fontdimen3\\font\\relax}\n"
    "\\newcommand{\\slidesbox}{\\parindent=0pt\\parskip=0pt\\lineskip=0pt\\lineskiplimit=-\\maxdimen"
    "\\hyphenpenalty=10000\\exhyphenpenalty=50\\tolerance=9999\\emergencystretch=0pt\\frenchspacing"
    "\\hbadness=10000\\hfuzz=\\maxdimen\\vbadness=10000\\vfuzz=\\maxdimen}")
ULEM = "\\usepackage[normalem]{ulem}"
TIKZ = "\\usepackage{tikz}"
# Ink of Slides' own bullet glyphs per em of the bullet's size (emit.BULLET_SHAPES, measured with
# tools/probe_bullets.py; the lift off the baseline from cs161-tls slide 4): (height, gap to
# indentFirstLine, bottom above the baseline). Drawn rather than typed, because the deck's typeface
# may not have ● ○ ■ at all and a missing glyph in lualatex is nothing on the page.
BULLET_INK = {"●": (0.413, 0.08, 0.06), "○": (0.43, 0.08, 0.07), "■": (0.45, 0.07, 0.0)}
RING_EM = 0.06              # ○'s stroke
GLYPH_GAP = 1.9             # a typed bullet's box ends this far before indentFirstLine (emit.BULLET_GAP)


def line_box(z: float, r: float) -> tuple[float, float]:
    """(height above the baseline, depth below it) of a Slides line of size z at lineSpacing r."""
    from .emit import ASCENT_EM, LINE_EM
    if r >= 1:
        return ASCENT_EM * z, (LINE_EM - ASCENT_EM) * z + (r - 1) * LINE_EM * z
    return ASCENT_EM * z - (1 - r) * 0.75 * LINE_EM * z, (LINE_EM - ASCENT_EM) * z - (1 - r) * 0.25 * LINE_EM * z


def para_size(p: dict) -> float:
    return max((r.get("size") or 10.0) for r in p["runs"]) if p["runs"] else 10.0


def text_escape(text: str) -> str:
    """LaTeX for plain text as Slides shows it: runs of spaces are kept (Slides does not fold them)."""
    from .inverse import latex_escape
    out = latex_escape(text.replace("\t", " ").replace("\x0b", " "))
    while "  " in out:
        out = out.replace("  ", " \\ ")
    return out


def run_tex(r: dict, base: dict, text: str, ctx: Context) -> str:
    """One run's text with the style it has over `base`, both ways: a box whose base is bold writes
    `\\textmd` for its regular words, where `inverse.runs_latex` wrote nothing and left them bold."""
    lead = len(text) - len(text.lstrip(" "))
    trail = len(text) - len(text.rstrip(" "))
    core = text_escape(text.strip(" "))
    if not core:
        return text_escape(text)
    fam, bfam = r.get("family") or "sans", base.get("family") or "sans"
    if fam != bfam:
        core = {"mono": "\\texttt", "serif": "\\textrm", "sans": "\\textsf"}[fam if fam in ("mono", "serif") else "sans"] + f"{{{core}}}"
    if bool(r.get("bold")) != bool(base.get("bold")):
        core = ("\\textbf" if r.get("bold") else "\\textmd") + f"{{{core}}}"
    if bool(r.get("italic")) != bool(base.get("italic")):
        core = ("\\textit" if r.get("italic") else "\\textup") + f"{{{core}}}"
    if r.get("smallcaps"):
        core = f"\\textsc{{{core}}}"
    if r.get("underline") or r.get("strike"):
        ctx.packages.add(ULEM)
        core = ("\\uline" if r.get("underline") else "\\sout") + f"{{{core}}}"
    if r.get("script") == "super":
        core = f"\\textsuperscript{{{core}}}"
    elif r.get("script") == "sub":
        core = f"\\textsubscript{{{core}}}"
    if r.get("color") and (r["color"] or "").lower() != (base.get("color") or "").lower():
        core = f"\\textcolor{{{colour_name(r['color'], ctx.colours)}}}{{{core}}}"
    if r.get("size") and base.get("size") and abs(r["size"] - base["size"]) > 0.01:
        core = f"{{\\slidesize{{{r['size']:.2f}}}{core}}}"
    if r.get("link") and not str(r["link"]).startswith("#"):
        url = str(r["link"]).replace("\\", "/").replace("#", "\\#").replace("%", "\\%")
        core = f"\\href{{{url}}}{{{core}}}"
    return " " * lead + core + " " * trail


def paragraph_base(p: dict) -> dict:
    """The style most of a paragraph's letters are in: set once at its start."""
    counts: dict = {}
    for r in p["runs"]:
        k = (round(r.get("size") or 0, 2), (r.get("color") or "").lower() or None, r.get("family") or "sans",
             bool(r.get("bold")), bool(r.get("italic")))
        counts[k] = counts.get(k, 0) + len(r["text"])
    size, colour, family, bold, italic = max(counts, key=counts.get)
    return {"size": size or 10.0, "color": colour, "family": family, "bold": bold, "italic": italic}


def runs_tex(runs: list[dict], base: dict, ctx: Context, brk: str) -> str:
    """A paragraph's runs; a soft break (Shift+Enter, \\x0b) ends the line wherever it stands - inside
    a bold word, at the paragraph's start - since the paragraph is already in horizontal mode
    (`\\noindent`) and the break is written between the styled pieces, never inside one."""
    out = []
    for r in runs:
        if r.get("hole"):
            out.append(f"\\hskip{r['hole']:.2f}pt ")
            continue
        for k, piece in enumerate(r["text"].split("\x0b")):
            if k:
                out.append(brk)
            if piece:
                out.append(run_tex(r, base, piece, ctx))
    text = "".join(out)
    # TeX drops the spaces a paragraph opens with; Slides draws them
    lead = len(text) - len(text.lstrip(" "))
    return "\\ " * lead + text[lead:].rstrip(" ")


def bullet_tex(p: dict, ctx: Context, scale: float, right: float) -> str:
    """The bullet, its right edge `right` pt from where the line's text starts (negative: left of it)."""
    b = p["bullet"]
    glyph = (b.get("text") or "").strip()
    if not glyph:
        return ""                               # a list paragraph whose level shows no glyph
    z = b.get("size") or para_size(p)
    colour = colour_name(b["color"], ctx.colours) if b.get("color") else None
    fill = f"fill={colour}" if colour else "fill"
    if glyph in BULLET_INK:
        ctx.packages.add(TIKZ)
        height, gap, lift = BULLET_INK[glyph]
        d = height * z
        right -= gap * z
        if glyph == "●":
            pic = f"\\tikz[baseline={-lift * z:.2f}pt]\\path[{fill}] ({d / 2:.2f}pt,{d / 2:.2f}pt) circle[radius={d / 2:.2f}pt];"
        elif glyph == "○":
            t = RING_EM * z
            draw = f"draw={colour}" if colour else "draw"
            pic = (f"\\tikz[baseline={-lift * z:.2f}pt]\\path[{draw},line width={t:.2f}pt] "
                   f"({d / 2:.2f}pt,{d / 2:.2f}pt) circle[radius={(d - t) / 2:.2f}pt];")
        else:
            pic = f"\\tikz[baseline={-lift * z:.2f}pt]\\path[{fill}] (0pt,0pt) rectangle ({d:.2f}pt,{d:.2f}pt);"
    else:
        right -= GLYPH_GAP / scale
        style = {"mono": "\\ttfamily", "serif": "\\rmfamily"}.get(b.get("font_family"), "")
        style += "\\bfseries" if b.get("bold") else ""
        style += f"\\color{{{colour}}}" if colour else ""
        pic = f"{{\\slidesize{{{z:.2f}}}{style}{text_escape(glyph)}}}"
    return f"\\llap{{{pic}\\hskip{-right:.2f}pt}}"


def text_box_latex(el: dict, ctx: Context, ind: str) -> str:
    """A text box laid out as Slides lays it out: the element's own box, the vertical alignment done
    by TeX (`\\vbox to` its height with the slack above, below or both), each paragraph at its own
    size, pitch, spacing and indents, bullets drawn where Slides draws them. See the notes above."""
    from .emit import BASELINE_A, PAD_X
    box = el.get("box") or {}
    scale = box.get("scale") or 720 / 453.54
    x0, y0, x1, y1 = el["bbox"]
    pad, inset = PAD_X / scale, BASELINE_A / scale
    width, height = max(x1 - x0 - 2 * pad, 1.0), max(y1 - y0, 0.1)
    valign = box.get("valign", "top")
    paras = [p for p in el["paragraphs"] if p["runs"]]
    ctx.packages.add(TEXTPOS)
    ctx.packages.add(SLIDES_TEXT)
    out = [f"{ind}\\begin{{textblock*}}{{{width:.1f}pt}}({x0 + pad:.1f}pt,{y0:.1f}pt)",
           f"{ind}  \\vbox to {height:.1f}pt{{\\slidesbox",
           f"{ind}  " + ("\\vss" if valign in ("middle", "bottom") else f"\\vskip{inset:.2f}pt")]
    prev = None
    for p in paras:
        sl = p.get("slides") or {}
        z, r = para_size(p), sl.get("line_spacing") or 1.0
        above, below = line_box(z, r)
        pitch = above + below
        left, first = (sl.get("indent_start") or 0) / scale, (sl.get("indent_first") or 0) / scale
        end = (sl.get("indent_end") or 0) / scale
        glyph = p.get("bullet") and (p["bullet"].get("text") or "").strip()
        shift = max(0.0, first - left) if p.get("bullet") else first - left
        head = []
        if prev is None:
            # a box that grows to fit its text (SHAPE_AUTOFIT) draws its first line without the first
            # paragraph's spaceAbove: gdg24's body copy says 22 pt and starts 22 pt higher than
            # that, while ds-lecture's bodies (no autofit type) keep their master's 6 pt
            if sl.get("space_above") and not box.get("grows"):
                head.append(f"\\vskip{sl['space_above'] / scale:.2f}pt")
        else:
            psl, pz, pr = prev.get("slides") or {}, para_size(prev), (prev.get("slides") or {}).get("line_spacing") or 1.0
            gap = ((psl.get("space_below") or 0) + (sl.get("space_above") or 0)) / scale
            if prev.get("bullet") and p.get("bullet") and sl.get("spacing_mode") != "NEVER_COLLAPSE":
                gap = 0.0
            k = pitch - line_box(pz, pr)[1] - gap - above
            head.append(f"\\prevdepth=\\dimexpr\\prevdepth{k:+.2f}pt\\relax")
        # LuaTeX's skips are logical: in a right-to-left paragraph \leftskip is at its start, the
        # right edge, where Slides measures indentStart from too - so only the alignment flips.
        rtl = p.get("direction") == "rtl"
        align = p.get("align", "left")
        if rtl:
            align = {"left": "right", "right": "left"}.get(align, align)
        justified = bool(sl.get("justified")) and align == "left"
        lskip = f"{left:.2f}pt" + (" plus 1fil" if align in ("center", "right") else "")
        rskip = f"{end:.2f}pt" + (" plus 1fil" if align in ("center", "left") and not justified else "")
        fill = "0pt plus 1fil" if justified else "0pt"
        base = paragraph_base(p)
        lead = f"\\slidesize{{{base['size']:.2f}}}"
        lead += {"mono": "\\ttfamily", "serif": "\\rmfamily"}.get(base["family"], "")
        lead += "\\bfseries" if base["bold"] else ""
        lead += "\\itshape" if base["italic"] else ""
        lead += f"\\color{{{colour_name(base['color'], ctx.colours)}}}" if base["color"] else ""
        brk = "\\unskip\\hfil\\break " if justified else "\\unskip\\break "
        blank = not any(x["text"].strip() for x in p["runs"])
        body = "" if blank else runs_tex(p["runs"], base, ctx, brk)
        start = f"\\vrule width0pt height{above:.2f}pt depth0pt\\relax"
        if shift:
            start += f"\\hskip{shift:.2f}pt"
        if glyph:
            start += bullet_tex(p, ctx, scale, first - left - shift)
        elif "\t" in "".join(x["text"] for x in p["runs"]) and first < left and not p.get("bullet"):
            # a hanging label (`label<TAB>text`): the tab jumps to indentStart
            label, rest, seen = [], [], False
            for x in p["runs"]:
                if seen or "\t" not in x["text"]:
                    (rest if seen else label).append(x)
                    continue
                a, _, b = x["text"].partition("\t")
                label.append({**x, "text": a})
                rest.append({**x, "text": b})
                seen = True
            body = (f"\\hbox to{left - first:.2f}pt{{{runs_tex(label, base, ctx, brk)}\\hss}}"
                    + runs_tex(rest, base, ctx, brk))
        # a right-to-left paragraph is set in its language (scripts.py: babel's bidi, shaping)
        lang_in, lang_out = "", ""
        if rtl:
            from .scripts import rtl_language
            lang_in = f"\\begin{{otherlanguage}}{{{rtl_language(p)}}}"
            lang_out = "\\end{otherlanguage}"
        out.append(f"{ind}  " + "".join(head) + lang_in +
                   f"{{\\leftskip={lskip}\\relax\\rightskip={rskip}\\relax\\parfillskip={fill}\\relax")
        out.append(f"{ind}    \\noindent{lead}{start}" + ("% blank line" if blank else ""))
        if body:
            out.append(f"{ind}    {body}")
        # the pitch last: \selectfont (in \slidesize) resets \baselineskip, and TeX reads it at \par
        out.append(f"{ind}  \\baselineskip={pitch:.2f}pt\\par}}{lang_out}")
        prev = p
    if prev is not None:
        last = line_box(para_size(prev), (prev.get("slides") or {}).get("line_spacing") or 1.0)[1]
        out.append(f"{ind}  \\vskip\\dimexpr{last:.2f}pt-\\prevdepth\\relax")
    out.append(f"{ind}  " + ("\\vss" if valign in ("middle", "top") else f"\\vskip{inset:.2f}pt") + "}")
    out.append(f"{ind}\\end{{textblock*}}")
    return "\n".join(out)


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
    from . import adopt_shapes
    if el.get("role") == "line" and el.get("from") and el.get("to"):
        return adopt_shapes.line_block(el, ctx, ind)
    return adopt_shapes.shape_block(el, ctx, ind)


# What `table_block` writes calls these. A Slides row is a minimum height that grows until its
# tallest cell fits, and only TeX knows how tall a cell's text comes out, so the table is measured
# where it is drawn: every cell is set into a box first (`\adoptcell`, which grows the last row it
# spans until the rows hold it), `\adopttops` then adds the rows up, and the tikzpicture after it
# puts fills, borders and boxes at `\adopty{row}`.
#
# The insets are a guess, and Slides does not always wrap inside them: creandum-board's native
# table (left inset 7.2 pt, measured on the thumbnail) keeps "+1 months", 35 pt of 7 pt Arial Bold,
# on one line in a 40.2 pt column, and its row is stored 22.1 pt - one line. A stored row height is
# what Slides laid out, so a cell whose text would need more than its rows give it is set again
# without the insets, and kept that way when that takes fewer lines.
#
# Slides never hyphenates, so neither does a cell. (It does break a word between two letters when
# the word is wider than the cell; TeX lets it stick out instead, because a penalty between every
# two letters, tried, also split the numbers of creandum-board's narrow columns where Slides keeps
# them whole: the inset is a guess, and a word that sticks out costs less than a row that grows.)
TABLE_MACROS = r"""\makeatletter
\newcommand\adoptrow[2]{\expandafter\edef\csname adopt@row@#1\endcsname{\the\dimexpr#2\relax}}
\newcommand\adoptsetcell[2]{\vbox{\hsize=#1\relax\linewidth\hsize\parindent\z@
    \hyphenpenalty\@M\exhyphenpenalty\@M\everypar{\strut}#2\ifhmode\strut\fi}}
\newcommand\adoptcell[8]{% box, first row, last row, text width, vertical inset (both),
  % width with no insets, its shift, content
  \expandafter\ifx\csname adopt@box@#1\endcsname\relax\expandafter\newbox\csname adopt@box@#1\endcsname\fi
  \global\setbox\csname adopt@box@#1\endcsname\adoptsetcell{#4}{#8}%
  \dimen@\z@\@tempcnta#2\relax
  \loop\advance\dimen@\csname adopt@row@\the\@tempcnta\endcsname\relax
  \ifnum\@tempcnta<#3\relax\advance\@tempcnta\@ne\repeat
  \dimen@ii\dimexpr\ht\csname adopt@box@#1\endcsname+\dp\csname adopt@box@#1\endcsname+#5*2\relax
  \ifdim\dimen@ii>\dimen@
    \setbox\@tempboxa\adoptsetcell{#6}{#8}%
    \ifdim\dimexpr\ht\@tempboxa+\dp\@tempboxa\relax<\dimexpr\dimen@ii-#5*2\relax
      \global\setbox\csname adopt@box@#1\endcsname\hbox to #4{\kern#7\box\@tempboxa\hss}%
      \dimen@ii\dimexpr\ht\csname adopt@box@#1\endcsname+\dp\csname adopt@box@#1\endcsname+#5*2\relax
    \fi
  \fi
  \ifdim\dimen@ii>\dimen@
    \expandafter\edef\csname adopt@row@#3\endcsname{\the\dimexpr\csname adopt@row@#3\endcsname+\dimen@ii-\dimen@\relax}%
  \fi}
\newcommand\adopttops[1]{% \adopty{k}: the top of row k, and \adopty{#1} the table's foot
  \dimen@\z@\@tempcnta\z@
  \loop\expandafter\edef\csname adopt@y@\the\@tempcnta\endcsname{\the\dimen@}%
  \ifnum\@tempcnta<#1\relax
    \advance\dimen@\csname adopt@row@\the\@tempcnta\endcsname\relax\advance\@tempcnta\@ne\repeat}
\newcommand\adopty[1]{\csname adopt@y@#1\endcsname}
\newcommand\adoptbox[1]{\copy\csname adopt@box@#1\endcsname}
\makeatother"""

DASHES = {"DOT": "dotted", "DASH": "dashed", "DASH_DOT": "dash dot", "LONG_DASH": "dashed",
          "LONG_DASH_DOT": "dash dot"}


def table_segments(el: dict) -> list[tuple[tuple, list]]:
    """The borders to draw, joined into runs: [((dir, line, style), [(from, to)])]. A segment
    inside a merged cell is not a border (the API lists none there, but a pptx import may), and
    touching segments of one style along one line become one stroke, so dots and dashes run on."""
    n_rows, n_cols = len(el["row_heights"]), len(el["col_widths"])
    inside_h, inside_v = set(), set()
    for c in el.get("table_cells", []):
        for r in range(c["row"] + 1, c["row"] + c["rowspan"]):
            inside_h.update((r, k) for k in range(c["col"], c["col"] + c["colspan"]))
        for k in range(c["col"] + 1, c["col"] + c["colspan"]):
            inside_v.update((r, k) for r in range(c["row"], c["row"] + c["rowspan"]))
    runs: dict[tuple, list] = {}
    for b in el.get("table_borders", []):
        if b["dir"] == "h" and (b["row"] > n_rows or b["col"] >= n_cols or (b["row"], b["col"]) in inside_h):
            continue
        if b["dir"] == "v" and (b["row"] >= n_rows or b["col"] > n_cols or (b["row"], b["col"]) in inside_v):
            continue
        line, at = (b["row"], b["col"]) if b["dir"] == "h" else (b["col"], b["row"])
        key = (b["dir"], line, b["color"], b["alpha"], b["weight"], b["dash"])
        spans = runs.setdefault(key, [])
        if spans and spans[-1][1] == at:
            spans[-1] = (spans[-1][0], at + 1)
        else:
            spans.append((at, at + 1))
    return sorted(runs.items(), key=lambda kv: (kv[0][0], kv[0][1]))


def table_block(el: dict, ctx: Context, ind: str) -> str:
    """A table at its place and size: every cell's text set in a box as wide as its columns less
    Slides' insets, the rows grown until their cells fit (`TABLE_MACROS`), then one tikzpicture with
    the cell fills, the border segments and the boxes. A `tabular` would be the readable version,
    but it cannot say what a Slides table says: a colour, weight and dash per border segment, a
    row's minimum height, and text inset by Slides' own padding at an exact column width."""
    widths, heights = el.get("col_widths") or [], el.get("row_heights") or []
    if not widths or not heights:
        return ""
    ctx.packages.add("\\usepackage{tikz}")
    ctx.packages.add(TEXTPOS)
    ctx.packages.add(TABLE_MACROS)
    padx, pady = el.get("cell_pad") or (4.5, 4.5)
    xs = [0.0]
    for w in widths:
        xs.append(xs[-1] + w)
    n_rows = len(heights)
    x0, y0 = el["bbox"][0], el["bbox"][1]
    lines = [f"{ind}\\begin{{textblock*}}{{{xs[-1]:.1f}pt}}({x0:.1f}pt,{y0:.1f}pt)"]
    lines += [f"{ind}  \\adoptrow{{{r}}}{{{h:.2f}pt}}" for r, h in enumerate(heights)]
    cells = [c for c in el.get("table_cells", []) if c["row"] < n_rows and c["col"] < len(widths)]
    boxes: dict[int, int] = {}
    # rows that hold one cell grow first, so a merged cell only adds what they left it short of
    for k, c in sorted(enumerate(cells), key=lambda kc: (kc[1]["rowspan"], kc[0])):
        if not c["paragraphs"]:
            continue
        last_row = min(c["row"] + c["rowspan"], n_rows) - 1
        last_col = min(c["col"] + c["colspan"], len(widths))
        span = xs[last_col] - xs[c["col"]]
        width = max(span - 2 * padx, 1.0)
        # the insets let go of when the text would otherwise need more room than the stored row
        # height gives it (see TABLE_MACROS); the wider box keeps the paragraph's alignment
        shift = {"center": -padx, "right": -2 * padx}.get(c["paragraphs"][0].get("align"), 0.0)
        base = element_style(c)
        body = paragraphs_latex(c["paragraphs"], lambda _p, b=base: b, ctx, ind + "    ")
        boxes[k] = len(boxes) + 1
        lines.append(f"{ind}  \\adoptcell{{{boxes[k]}}}{{{c['row']}}}{{{last_row}}}{{{width:.2f}pt}}{{{pady:.2f}pt}}"
                     f"{{{span:.2f}pt}}{{{shift:.2f}pt}}{{%")
        lines.append(f"{ind}    \\raggedright{base_lead(base, ctx)}%")
        lines.append(body + "}")
    lines.append(f"{ind}  \\adopttops{{{n_rows}}}")
    lines.append(f"{ind}  \\begin{{tikzpicture}}[baseline=(current bounding box.north),inner sep=0pt,outer sep=0pt]")
    lines.append(f"{ind}    \\path[use as bounding box] (0pt,0pt) rectangle ({xs[-1]:.1f}pt,{{-\\adopty{{{n_rows}}}}});")
    for c in cells:
        if c.get("fill"):
            opacity = f",fill opacity={c['fill_alpha']:.2f}" if (c.get("fill_alpha") or 1) < 1 else ""
            r1 = min(c["row"] + c["rowspan"], n_rows)
            c1 = min(c["col"] + c["colspan"], len(widths))
            lines.append(f"{ind}    \\fill[{colour_name(c['fill'], ctx.colours)}{opacity}] ({xs[c['col']]:.1f}pt,{{-\\adopty{{{c['row']}}}}})"
                         f" rectangle ({xs[c1]:.1f}pt,{{-\\adopty{{{r1}}}}});")
    for (direction, line, colour, alpha, weight, dash), spans in table_segments(el):
        opts = [colour_name(colour or "#000000", ctx.colours), f"line width={weight:.2f}pt"]
        opts.append(DASHES.get(dash, "line cap=rect"))
        if alpha < 1:
            opts.append(f"draw opacity={alpha:.2f}")
        for a, b in spans:
            if direction == "h":
                path = f"({xs[a]:.1f}pt,{{-\\adopty{{{line}}}}}) -- ({xs[b]:.1f}pt,{{-\\adopty{{{line}}}}})"
            else:
                path = f"({xs[line]:.1f}pt,{{-\\adopty{{{a}}}}}) -- ({xs[line]:.1f}pt,{{-\\adopty{{{b}}}}})"
            lines.append(f"{ind}    \\draw[{','.join(opts)}] {path};")
    for k, c in enumerate(cells):
        if k not in boxes:
            continue
        r1 = min(c["row"] + c["rowspan"], n_rows)
        x = xs[c["col"]] + padx
        if c.get("valign") == "middle":
            where, anchor = f"({x:.1f}pt,{{-(\\adopty{{{c['row']}}}+\\adopty{{{r1}}})/2}})", "west"
        elif c.get("valign") == "bottom":
            where, anchor = f"({x:.1f}pt,{{-\\adopty{{{r1}}}+{pady:.2f}pt}})", "south west"
        else:
            where, anchor = f"({x:.1f}pt,{{-\\adopty{{{c['row']}}}-{pady:.2f}pt}})", "north west"
        lines.append(f"{ind}    \\node[anchor={anchor}] at {where} {{\\adoptbox{{{boxes[k]}}}}};")
    lines.append(f"{ind}  \\end{{tikzpicture}}")
    lines.append(f"{ind}\\end{{textblock*}}")
    return "\n".join(lines)


def slide_latex(s: dict, style_for, ctx: Context, flow: bool, tree: Path | None = None,
                deck_bg: str | None = None) -> str:
    """One deck slide as a frame. `flow` writes the readable version (`inverse.frame_latex`: a frame
    title and body text in the flow); otherwise every element keeps its own place."""
    if flow:
        return frame_latex(s, style_for, ctx)
    out = ["\\begin{frame}[plain]"]
    # The deck lists a page's elements in z-order, and a textblock written later is drawn on top:
    # in reading order a block's body panel, starting 2 pt under its title, was painted over it.
    for el in s["elements"]:
        if el.get("role") in ("math", "icon"):
            continue                                   # part of a text line, not an element of its own
        if el["kind"] == "shape":
            out.append(shape_block(el, ctx, "  ").rstrip("\n"))
        elif el["kind"] == "table":
            out.append(table_block(el, ctx, "  "))
        elif el["kind"] == "image":
            pic = picture_of(el, tree)
            if pic is not None:
                ctx.packages.add(TEXTPOS)
                out.append(picture_block(el, pic, ctx, "  ").rstrip("\n"))
        elif el["kind"] == "text" and el.get("paragraphs"):
            ctx.packages.add(TEXTPOS)
            # A node of a flow chart is one element: its box, then its label on top.
            out.append(shape_block(el, ctx, "  ").rstrip("\n"))
            # A turned text box: its words are written upright in the box it would have if it were
            # not turned, and that is then set turned about its centre (`adopt_shapes.turned_text`).
            upright = {**el, "bbox": el["frame"]["box"]} if el.get("frame") else el
            out.append(turned_text(text_box_latex(upright, ctx, "  "), el, ctx))
    if s.get("notes"):
        from .inverse import latex_escape
        out.append("  \\note{" + "\n\n".join(latex_escape(p) for p in s["notes"].split("\n") if p.strip()) + "}")
    out.append("\\end{frame}")
    text = "\n".join(x for x in out if x.strip()) + "\n"
    backdrop = picture_of({"file": s.get("background_file"), "alt": "background"}, tree) \
        if s.get("background_file") else None
    if backdrop is not None:
        # A stretched picture fill is the whole page under everything else, which is beamer's
        # background canvas; the colour under it no longer shows.
        ctx.packages.add("\\usepackage{graphicx}")
        return ("{\\setbeamertemplate{background canvas}{\\includegraphics[width=\\paperwidth,"
                f"height=\\paperheight]{{{backdrop.rel}}}}}\n" + text + "}\n")
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
    from .scripts import script_preamble
    fonts = font_preamble(target, tree)
    lines = [f"\\documentclass[{opt}]{{beamer}}" if opt else "\\documentclass{beamer}",
             "\\usetheme{default}",
             "\\setbeamertemplate{navigation symbols}{}",
             "\\setbeamertemplate{footline}{}",
             "\\setbeamertemplate{headline}{}",
             "\\setbeamercolor{background canvas}{bg=}",
             # languages, fallback fonts and shaping for scripts other than Latin: before the font
             # lines, whose fonts then carry the fallback chain (scripts.py)
             *script_preamble(target, tree),
             *fonts,
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
    # "% slide N" says which deck slide a frame is, for a person reading the source and for tools
    # that compile frames one at a time (devtools.adopt_bench finds the frames that break a build)
    frames = [f"% slide {n}\n" + slide_latex(s, style_for, ctx, flow, tex.parent, deck_bg)
              for n, s in enumerate(target["slides"], 1)]
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
