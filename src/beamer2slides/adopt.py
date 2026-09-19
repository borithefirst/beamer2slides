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

import hashlib
import json
import os
import re
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


def page_setup(size: list[float] | None) -> tuple[str, str]:
    """(class option, preamble line) for a page exactly `size` - the IR's page, whose aspect is the
    deck's. beamer's own sizes are said with its class option; any other page (A4 portrait, a
    phone-shaped story, a poster) with `\\geometry{papersize=...}`, which beamer takes after the
    class: the nearest beamer ratio would draw a portrait deck on a landscape page, squeezed."""
    if not size:
        return "aspectratio=169", ""
    for (w, h), opt in ASPECTS.items():
        if abs(w - size[0]) < 0.5 and abs(h - size[1]) < 0.5:
            return opt, ""
    return "", f"\\geometry{{papersize={{{size[0]:.2f}bp,{size[1]:.2f}bp}}}}"


# The IR's lengths are PDF points (bp, 72 to the inch) and the writers below spell them TeX pt
# (72.27): on beamer's 16:9 page, 160 mm = 453.54 bp, every element came out 0.37% too close to the
# top-left corner - 1.7 pt at the right edge, and 40% of the ink of a text box there (page score
# 0.750 -> 0.806 over the corpus). `to_bp` rewrites a written frame's lengths; the deck's own words
# were escaped with `inverse.GUARD_UNITS` on, so a "12pt" typed on a slide reads 12{}pt and stays.
LENGTH_PT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)pt(?![A-Za-z])")
FONTSIZE = re.compile(r"\\fontsize\{(\d+(?:\.\d+)?)\}\{(\d+(?:\.\d+)?)\}")


def to_bp(text: str) -> str:
    text = FONTSIZE.sub(r"\\fontsize{\1bp}{\2bp}", text)
    return LENGTH_PT.sub(r"\1bp", text)


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
    fonts: dict = {}
    for p in el.get("paragraphs", []):
        for r in p["runs"]:
            if (r.get("family") or "sans") == family and r.get("font"):
                fonts[r["font"]] = fonts.get(r["font"], 0) + len(r["text"])
    return {"size": size, "color": colour, "family": family, "bold": bold, "italic": False,
            "font": max(fonts, key=fonts.get) if fonts else None}


def base_lead(style: dict, ctx: Context) -> str:
    """The switches that make `style` the base inside a textblock."""
    from .inverse import size_switch
    out = []
    if style.get("size"):
        out.append(size_switch(style["size"], ctx.pt_option))
    switch = getattr(ctx, "font_switches", {}).get(style.get("font") or "")
    if switch:
        out.append(switch)                  # the deck's second face of this kind (`font_preamble`)
    elif style.get("family") == "mono":
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
    if fetching():
        from .fontfetch import cache_dir
        out.append(cache_dir())                         # families fetched from google/fonts
    if os.name == "nt":
        out += [Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts",
                Path(os.environ.get("LOCALAPPDATA", ".")) / "Microsoft" / "Windows" / "Fonts"]
    else:
        out += [Path.home() / ".fonts", Path.home() / ".local" / "share" / "fonts",
                Path.home() / "Library" / "Fonts", Path("/Library/Fonts"), Path("/usr/share/fonts")]
    return [p for p in out if p.is_dir()]


def fetching() -> bool:
    """Whether a family the machine lacks may be fetched from google/fonts (`fontfetch`): not while
    `$B2S_FONTS` names the only folders to use - the tests' answer must not depend on the network -
    nor with `$B2S_FONT_FETCH=0`."""
    from .fontfetch import enabled
    return enabled() and not os.environ.get("B2S_FONTS", "").strip()


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
        groups = {k: v for k, v in groups.items() if "UprightFont" in v}
        # Windows' own files are named by abbreviation (cour.ttf is Courier New, ariblk.ttf Arial
        # Black, pala.ttf Palatino Linotype): the family a deck names is only in the font's name
        # table. Unknown by it, comps-analysis's Courier New was fetched as Courier Prime and
        # ap-bio-stats' Arial Black was set in Arial. The group is also listed under each family
        # name it gives itself that no file stem already spells (`_font_family` sets it in the
        # group's own file stem, which is what fontspec's `*` expands to).
        uprights = {v["UprightFont"]: v for v in groups.values()}
        flat_stems = {flatten(k) for k in groups}
        # A collection's other faces are families of their own, set by their number in the file
        # (`FontIndex`): "MS PGothic" is face 2 of msgothic.ttc, and apps-edu-zh's English lines in
        # it were set in Arial, 5% wider than the proportional Gothic Slides draws them in.
        try:
            from .scripts import faces
            named = sorted((f for f in faces() if f.path in uprights), key=lambda f: f.index)
        except ImportError:                             # no fontTools: file stems only
            named = []
        for face in named:
            for fam in face.families:
                if fam not in flat_stems:
                    groups.setdefault(fam, uprights[face.path] if face.index == 0 else
                                      {"UprightFont": face.path, "FontIndex": face.index})
        _FAMILIES[dirs] = groups
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
    Code, which at least is the same kind of face as the rest of the deck.

    Before settling for a stand-in, a family that is not on the machine under its own name is
    fetched from google/fonts (`fontfetch.fetch_family`), where nearly every font Slides offers
    lives: of the corpus's letters in fonts no machine here has, most are Open Sans, Montserrat,
    Delius, Inter, Yanone Kaffeesatz, Alegreya, Work Sans...

    A CJK face Slides does not have is not drawn in it at all (`slides_lacks_cjk`): its Latin is set
    in Times New Roman, which is what the family is then given."""
    if name and slides_lacks_cjk(name):
        got = font_family(SLIDES_DEFAULT, "serif")
        if got:
            return {**got, "match": name}
    if name and not _have(name) and not _fetch(name):
        for sub in SUBSTITUTES.get(flatten(name), []):
            if _have(sub) or _fetch(sub):
                got = _font_family(sub, want)
                if got and flatten(got["match"]) == flatten(sub):
                    return {**got, "match": name}       # the deck's font, in all but its files
    return _font_family(name, want, near)


# Families drawn to the same metrics as a font the machine does not have and google/fonts does not
# carry (the proprietary ones), tried after a fetch of the font itself: set in its stand-in, a line
# breaks where the deck's does. Arimo, Tinos and Cousine are Arial, Times New Roman and Courier New
# glyph for glyph in advance; Carlito and Caladea the same for Calibri and Cambria, Gelasio for
# Georgia. The rest are the same design, not the same widths.
SUBSTITUTES = {
    "arial": ["Arimo"], "helvetica": ["Arimo"], "timesnewroman": ["Tinos"], "times": ["Tinos"],
    "couriernew": ["Cousine"], "courier": ["Cousine"], "calibri": ["Carlito"], "cambria": ["Caladea"],
    "georgia": ["Gelasio"], "arialblack": ["Archivo Black"], "bodoni": ["Libre Bodoni", "Bodoni Moda"],
    "droidsans": ["Noto Sans"], "droidserif": ["Noto Serif"], "droidsansmono": ["Noto Sans Mono"],
    "bookantiqua": ["Palatino Linotype"], "googlesansmono": ["Google Sans Code"],
}


# What Slides draws a CJK font in when it does not have it: apps-edu-zh's Microsoft JhengHei (a .pptx
# import) shows Times New Roman's Latin and Noto Sans TC's ideographs with palt in the thumbnails -
# its lines measured 0.995-1.021 of that model's widths, 0.885-0.944 of JhengHei's own. The CJK faces
# Slides does have besides google/fonts' (MS PGothic's Latin is drawn as itself in the same deck).
SLIDES_DEFAULT = "Times New Roman"
SLIDES_CJK = {"mspgothic"}
_LACKS: dict[str, bool] = {}


def slides_lacks_cjk(name: str) -> bool:
    """Whether `name` is a CJK font Slides cannot draw: not one of its own (`SLIDES_CJK`), not on
    google/fonts (fetching must be on to tell), not a font `SUBSTITUTES` stands in for, and a face on
    this machine that has ideographs. Only CJK faces: a Latin font Slides lacks is drawn otherwise
    (intro-lecture's CMTT9 in a monospace)."""
    flat = flatten(name or "")
    if not flat or flat in SLIDES_CJK or flat in SUBSTITUTES or not fetching():
        return False
    if flat not in _LACKS:
        from .deck_ir import family_of
        from .fontfetch import _missing, fetch_family, folder_name
        files = _font_family(name, family_of(name))
        # google/fonts must have said no (a fetch that failed for want of a network says nothing)
        _LACKS[flat] = bool(files) and flatten(files["match"]) == flat and \
            font_coverage(files["UprightFont"], {"中": 1, "文": 1}, files.get("FontIndex") or 0, strict=True) == 1.0 \
            and not fetch_family(name, log=lambda *a: None) and folder_name(name) in _missing()
    return _LACKS[flat]


def _have(name: str) -> bool:
    return any(flatten(stem) == flatten(name) for stem in font_candidates())


def _fetch(name: str) -> bool:
    if not fetching():
        return False
    from .fontfetch import fetch_family
    if fetch_family(name):
        _FAMILIES.clear()                               # the cache folder has a new family in it
        return True
    return False


def _font_family(name: str, want: str, near: str = "") -> dict[str, Path]:
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
    if not files:
        return {}
    # `stem` is what the family was found under (a name-table alias for cour.ttf is "couriernew"),
    # `match` that, and the family name fontspec is given is the files' own stem
    return {"stem": files["UprightFont"].stem.partition("-")[0], "match": stem, **files}


def font_files_latex(files: dict, tree: Path | None) -> str:
    """fontspec's options for a family's files (`font_family`'s answer, without its stem), the files
    copied into `<tree>/fonts/` with the licence that came with them."""
    index = files.get("FontIndex")
    if index:
        # one face of a collection, the only one of its family (MS PGothic): no other styles
        return font_files_latex({"UprightFont": files["UprightFont"]}, tree) + f",FontIndex={index}"
    files = {k: v for k, v in files.items() if isinstance(v, Path)}
    # Windows' own files have no dash and a name per style (arialbd.ttf beside arial.ttf)
    upright = files["UprightFont"].stem
    opts = [f"{k}=*-{files[k].stem.partition('-')[2]}" if "-" in files[k].stem else
            f"{k}=*" if files[k].stem == upright else f"{k}={files[k].stem}"
            for k in ("UprightFont", "BoldFont", "ItalicFont", "BoldItalicFont") if k in files]
    if tree is not None:
        first = next(iter(files.values()))
        licence = first.parent / f"{first.stem.partition('-')[0]}-LICENSE.txt"
        for f in list(files.values()) + ([licence] if licence.exists() else []):
            dest = tree / "fonts" / f.name
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dest)
    where = "Path=fonts/," if tree is not None else \
        "Path=" + next(iter(files.values())).parent.as_posix().rstrip("/") + "/,"
    exts = {f.suffix for f in files.values()}
    if len(exts) > 1:
        # one family in two formats (Windows' cambria.ttc beside cambriab.ttf): every file by its
        # full name, since fontspec's Extension is one for all
        opts = [f"{k}={files[k].name}" for k in ("UprightFont", "BoldFont", "ItalicFont", "BoldItalicFont")
                if k in files]
        return f"{where}{','.join(opts)}"
    return f"{where}Extension={exts.pop()},{','.join(opts)}"


# A weight other than regular and bold gets a face of its own when it sets this many letters of a font.
WEIGHT_MIN_LETTERS = 20


def weight_faces(font: str, files: dict, used: dict, tree: Path | None, font_weights: dict) -> str:
    """fontspec `FontFace` options for the weights a deck sets `font` in besides 400 and 700 (`used`:
    (weight, italic) -> letters), each an instance `fontfetch.weight_file` cuts from the variable
    font the family was fetched as, under the NFSS series `w<weight>` (`series`). Slides draws a
    weight per run: gdg24's headings are Google Sans 600 ("This is a Headline." 2.4% narrower than
    the bold that stood in for it) and 500, journey-maps' text Montserrat 300 and 500, sc-dark-minimal's
    Inter 300. What cannot be cut (a family on the machine, a static one, a weight off its axis)
    keeps `bold`'s rounding. The (weight, italic) pairs given faces go into `font_weights[font]`."""
    upright = files.get("UprightFont")
    if not upright or not used or any(f.suffix.lower() != ".ttf" for f in files.values() if isinstance(f, Path)):
        return ""
    from .fontfetch import weight_file
    opts, got = [], set()
    for (w, italic), n in sorted(used.items()):
        if n < WEIGHT_MIN_LETTERS:
            continue
        path = weight_file(upright, w, italic)
        if path is None:
            continue
        if tree is not None:
            dest = tree / "fonts" / path.name
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
        opts.append(f"FontFace={{w{w}}}{{{'it' if italic else 'n'}}}{{Font={path.stem}}}")
        got.add((w, italic))
    if got:
        font_weights.setdefault(font, set()).update(got)
    return "".join("," + o for o in opts)


def series(r: dict, ctx: Context) -> str:
    """The NFSS series a run (or a base style) is set in: `w<weight>` where `weight_faces` gave its
    font that weight, else bold or not."""
    w = r.get("weight")
    if w and (int(w), bool(r.get("italic"))) in (getattr(ctx, "font_weights", None) or {}).get(r.get("font") or "", ()):
        return f"w{int(w)}"
    return "b" if r.get("bold") else "m"


def series_switch(s: str) -> str:
    return {"b": "\\bfseries ", "m": "\\mdseries "}.get(s) or f"\\fontseries{{{s}}}\\selectfont "


# A deck's second, third... typeface of one kind gets a switch of its own when it sets this many
# letters: a heading face and a body face (Montserrat over Open Sans) are both the deck's look.
# Or covers as much of the page as that many letters at AREA_SIZE pt: sc-memphis' section numbers
# are six digits in all, at 166 pt, and set in the body face they came out a third too small.
EXTRA_FONT_MIN = 40
EXTRA_FONTS_MAX = 12
AREA_SIZE = 12.0


def font_preamble(target: dict, tree: Path | None, ctx: Context | None = None) -> list[str]:
    """fontspec lines for the typefaces the deck is written in, and the files beside the source.

    A foreign deck is written in the person's fonts, not the converter's three, and helvet in place
    of them is ink in the wrong shape on every slide that has words (measured on the DevFest
    template: 0.702 -> 0.720 ink overlap, the text-only slides moving most). What is not on this
    machine keeps its substitute, which is what the loop reports as a style it cannot close.

    The most used font of each kind is the document's (\\setsansfont, ...). Every other font the deck
    uses enough of and that exists here gets a `\\newfontfamily` switch, recorded in
    `ctx.font_switches` (deck font name -> command); `base_lead` puts it at the top of each text box
    whose letters are mostly in that font. A deck has a heading face and a body face more often than
    not (journey-maps: Montserrat titles over Open Sans text), and one family per kind set both in
    whichever was used more."""
    counts: dict = {}
    area: dict = {}
    letters: dict[str, dict[str, int]] = {}
    weights: dict[str, dict[tuple[int, bool], int]] = {}
    for s in target["slides"]:
        for e in s["elements"]:
            for p in e.get("paragraphs", []):
                for r in p["runs"]:
                    k = (r.get("family") or "sans", r.get("font") or "")
                    counts[k] = counts.get(k, 0) + len(r["text"])
                    area[k] = area.get(k, 0.0) + len(r["text"].strip()) * ((r.get("size") or 0) / AREA_SIZE) ** 2
                    if r.get("weight"):
                        w = weights.setdefault(k[1], {})
                        wk = (int(r["weight"]), bool(r.get("italic")))
                        w[wk] = w.get(wk, 0) + len(r["text"].strip())
                    seen = letters.setdefault(k[1], {})
                    for c in r["text"]:
                        if not c.isspace() and not chain_letter(c):
                            seen[c] = seen.get(c, 0) + 1
    ranked: dict[str, list[str]] = {}
    for (fam, font), _n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if font:
            ranked.setdefault(fam, []).append(font)
    wanted: dict[str, str] = {fam: fonts[0] for fam, fonts in ranked.items()}
    lines, found = [], ""
    font_weights: dict[str, set[tuple[int, bool]]] = {}
    for fam, command in (("sans", "setsansfont"), ("serif", "setmainfont"), ("mono", "setmonofont")):
        files: dict = {}
        # The kind's most used font, unless it has glyphs for few of the letters set in it: the letters
        # are then in a script Slides draws with a fallback of its own (hebrew-lesson's Hebrew typed
        # "in" Noto Sans Symbols, jruby-ja's Japanese "in" Arial, 7%), and a frame whose words are all
        # such letters embeds the font with no glyph, which lualatex refuses. The next font of the
        # kind is tried, then the stand-in as before.
        for font in ranked.get(fam, [])[:MAIN_CANDIDATES]:
            files = font_family(font, fam, found)
            if files and font_coverage(files["UprightFont"], letters.get(font, {}), files.get("FontIndex") or 0) >= MIN_MAIN_COVERAGE:
                wanted[fam] = font
                break
            files = {}
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
        stem, match = files.pop("stem"), files.pop("match")
        found = found or match                          # what the rest of the deck is set in
        low, asked = flatten(match), flatten(wanted[fam])
        faces = ""
        if not (low.startswith(asked) or asked.startswith(low)):
            print(f"  {wanted[fam]}: not on this machine, set in {stem}")
        else:
            faces = weight_faces(wanted[fam], files, weights.get(wanted[fam], {}), tree, font_weights)
        lines.append(f"\\{command}{{{stem}}}[{font_files_latex(files, tree)}{faces}"
                     f"{stretch(wanted[fam], stem, files, target)}]")
    if ctx is not None:
        ctx.font_weights = font_weights
        switches: dict[str, str] = {}
        main = set(wanted.values())
        for (fam, font), n in sorted(counts.items(), key=lambda kv: -kv[1]):
            if not font or font in main or font in switches or len(switches) >= EXTRA_FONTS_MAX or \
                    n < EXTRA_FONT_MIN and area.get((fam, font), 0.0) < EXTRA_FONT_MIN:
                continue
            files = font_family(font, fam)
            if not files:
                continue
            stem, match = files.pop("stem"), files.pop("match")
            low, asked = flatten(match), flatten(font)
            if not (low.startswith(asked) or asked.startswith(low)):
                continue                                # a stand-in: the kind's main font already is one
            if font_coverage(files["UprightFont"], letters.get(font, {}), files.get("FontIndex") or 0) < MIN_COVERAGE:
                # Slides draws what the font lacks in a fallback of its own: Hebrew typed "in" Noto
                # Sans Symbols, Japanese "in" Arial. Switching to the font would set nothing at all
                # (and lualatex refuses a font it embeds with no glyph), so those boxes keep the
                # document's font and whatever fallback the preamble gives it.
                continue
            command = "\\adoptfont" + "".join(chr(ord("A") + int(d)) for d in str(len(switches)))
            switches[font] = command
            faces = weight_faces(font, files, weights.get(font, {}), tree, font_weights)
            lines.append(f"\\newfontfamily{command}{{{stem}}}[{font_files_latex(files, tree)}{faces}"
                         f"{stretch(font, stem, files, target)}]")
        ctx.font_switches = switches
    return ["\\usepackage{fontspec}"] + lines


# A stand-in set within this share of the deck's widths is left alone; outside, it is condensed or
# extended to them, never by more than WIDTH_LIMIT
WIDTH_TOLERANCE = 0.02
WIDTH_LIMIT = 0.15
WIDTH_SPREAD = 0.04


def font_widths(font: str, files: dict, target: dict) -> float | None:
    """How much wider or narrower the deck's thumbnails show words in `font` than `files` set them
    (the median of measured / predicted over its lone one-line boxes, `deck_ir.ink_widths`), or None
    when that is within WIDTH_TOLERANCE, measured fewer than twice, or the measures disagree.

    A stand-in has the deck font's name and not its widths: Libre Bodoni sets comps-analysis's
    "Bodoni" titles 5% wider than Slides draws them, and each one wrapped a word onto a line of its
    own. The prediction is the words' advances less the first glyph's left bearing and the last one's
    right bearing, which is what a thumbnail's first and last ink columns show."""
    try:
        from fontTools.pens.boundsPen import BoundsPen
        from fontTools.ttLib import TTFont
    except ImportError:
        return None
    loaded: dict = {}
    ratios = []
    for s in target["slides"]:
        for e in s["elements"]:
            if not e.get("ink_width"):
                continue
            runs = next(p for p in e["paragraphs"] if p["runs"])["runs"]    # the line measured
            r0 = next(r for r in runs if r["text"].strip())
            if (r0.get("font") or "") != font:
                continue
            style = ("BoldItalicFont" if r0.get("italic") else "BoldFont") if r0.get("bold") else \
                ("ItalicFont" if r0.get("italic") else "UprightFont")
            path = files.get(style) or files["UprightFont"]
            if path not in loaded:
                try:
                    f = TTFont(path, fontNumber=files.get("FontIndex") or 0, lazy=True)
                    loaded[path] = (f, f.getBestCmap(), f.getGlyphSet(), f["hmtx"], f["head"].unitsPerEm)
                except Exception:
                    loaded[path] = None
            if loaded[path] is None:
                continue
            f, cmap, glyphs, hmtx, upem = loaded[path]
            text = "".join(r["text"] for r in runs).strip()
            names = [cmap.get(ord(c)) for c in text]
            if None in names:
                continue

            def bounds(g):
                pen = BoundsPen(glyphs)
                glyphs[g].draw(pen)
                return pen.bounds
            first, last = bounds(names[0]), bounds(names[-1])
            if first is None or last is None:
                continue
            ink = sum(hmtx[g][0] for g in names) - first[0] - (hmtx[names[-1]][0] - last[2])
            predicted = ink / upem * (r0.get("size") or 0)
            # a word or two of small type says little; a line far shorter than its words is the
            # first of several (comps-analysis's long titles, 0.6-0.8), which is not a width
            if predicted > 20 and abs(e["ink_width"] / predicted - 1) <= WIDTH_LIMIT:
                ratios.append(e["ink_width"] / predicted)
    if len(ratios) < 2:
        return None
    ratios.sort()
    mid = ratios[len(ratios) // 2] if len(ratios) % 2 else sum(ratios[len(ratios) // 2 - 1:len(ratios) // 2 + 1]) / 2
    near = [r for r in ratios if abs(r - mid) <= WIDTH_SPREAD]
    if len(near) < max(2, 0.6 * len(ratios)) or abs(mid - 1) <= WIDTH_TOLERANCE:
        return None
    return round(mid, 3)


def stretch(font: str, stem: str, files: dict, target: dict) -> str:
    """fontspec's FakeStretch for `font_widths`, or nothing. Only for a stand-in (`stem`, the files'
    family, is not the deck's `font`): a deck's own font is set as Slides sets it, and what the
    thumbnails show of it differs from its advances by its kerning alone - Pacifico's script
    measured 3% narrow, and condensed by that it set sc-aesthetic-school's titles longer, not shorter."""
    asked, have = flatten(font), flatten(stem)
    if have.startswith(asked) or asked.startswith(have) or files.get("FontIndex"):
        return ""                               # (a collection's face is found by its own name)
    ratio = font_widths(font, files, target)
    if ratio is None:
        return ""
    print(f"  {font}: set {ratio:.3f} wide to match the deck's slides")
    return f",FakeStretch={ratio}"


GYRE = {"sans": "texgyreheros", "serif": "texgyretermes", "mono": "texgyrecursor"}

# A second face gets its switch only if it has glyphs for this share of the letters set in it; the
# document's face for a kind is the first of its MAIN_CANDIDATES most used with at least half.
MIN_COVERAGE = 0.9
MIN_MAIN_COVERAGE = 0.5
MAIN_CANDIDATES = 3


def chain_letter(c: str) -> bool:
    """Whether a letter comes from `scripts`' fallback chain whatever font sets it: CJK. Such letters
    say nothing about the font they are typed in - jruby-ja's Arial runs are mostly Japanese, and
    counting them sent its Arial title words to Tahoma, 4% wider (measured: `InvokeDynamic` in the
    deck's thumbnails has Arial Bold's widths). A font whose own letters are all such ones still
    compiles: the chain sets them and the font is embedded as it is."""
    if os.environ.get("B2S_NO_SCRIPTS"):
        return False
    from .scripts import group_of, script_of
    sc = script_of(c)
    return sc is not None and group_of(sc) == "cjk"


def font_coverage(path: Path, letters: dict[str, int], index: int = 0, strict: bool = False) -> float:
    """The share of `letters` (character -> count) the font file has a glyph for: 1.0 when it cannot
    be told (no fontTools, a file fontTools cannot read), 0.0 then if `strict`."""
    total = sum(letters.values())
    if not total:
        return 1.0
    try:
        from fontTools.ttLib import TTFont
        cmap = TTFont(path, lazy=True, fontNumber=index).getBestCmap() or {}
    except Exception:                                   # noqa: BLE001 - ImportError, or any broken file
        return 0.0 if strict else 1.0
    return sum(n for c, n in letters.items() if ord(c) in cmap) / total


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
    # Brightness, contrast and recolour are properties LaTeX has no option for: baked into the file
    # (`compare.adjusted_picture`), as `pull` does - intro-lecture's title photos are dimmed to half
    bake = {k: el[k] for k in ("brightness", "contrast", "recolor") if el.get(k)}
    if bake and suffix not in (".png", ".jpg", ".jpeg"):
        bake = {}
    if tree is None and not bake:
        return Picture(path.name, path, natural_size(path))
    tag = hashlib.sha1(json.dumps(bake, sort_keys=True).encode()).hexdigest()[:4] if bake else ""
    rel = f"figures/{picture_slug(el.get('alt'))}-{(el.get('sha1') or path.stem)[:8]}{tag}{suffix}"
    dest = (tree if tree is not None else path.parent) / rel
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        if bake:
            from PIL import Image
            from .compare import adjusted_picture
            with Image.open(path) as img:
                img.seek(0)
                out = adjusted_picture(img, bake)
            if suffix == ".png":
                out.save(dest, "PNG")
            else:
                out.convert("RGB").save(dest, "JPEG", quality=92)
        elif suffix == path.suffix.lower():
            shutil.copyfile(path, dest)
        else:
            from PIL import Image
            with Image.open(path) as img:
                img.seek(0)
                img.convert("RGBA").save(dest, "PNG")
    return Picture(rel if tree is not None else dest.name, dest, natural_size(dest))


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

#
# The interword space is the font's own with no shrink - and it has to be *each* font's own:
# \spaceskip set once by \slidesize stays what the font then selected said, so a paragraph set in
# \ttfamily after it took Fira Sans' 0.26 em spaces where Courier Prime's are 0.6 em (sc-dark-modern's
# typewriter quotes came out 8% narrow and broke their lines elsewhere). Every font selection inside a
# text box takes its own space again (LaTeX's selectfont hook).
#
# The frames say this through a small vocabulary (`SLIDES_TEXT`, in the tree's slides.sty) rather than
# spelling every skip out: `\begin{slidebox}[...]{x,y,w,h}` is the box, `\slidepar[...]{style}{words}`
# a paragraph in a text style the preamble names once (`\slidestyle`), `\slidelabel` / `\slidebullet`
# its bullet. Each macro expands to exactly the primitives this writer used to write out, in the same
# order (the bench's pages are pixel-identical: docs/adopt-bench.md, "Readable sources"); only what
# was a default (a zero indent, a zero step between paragraphs, the deck's usual inset) goes unsaid.
SLIDES_TEXT = r"""% --- Text boxes laid out as Google Slides lays them out ------------------------------------
% \slidesize{z}: z bp type on z bp lines, with the font's own interword space and no shrink.
\newif\ifslidesspace
\AddToHook{selectfont}{\ifslidesspace\spaceskip=\fontdimen2\font plus\fontdimen3\font\relax\fi}
\newcommand{\slidesize}[1]{\fontsize{#1bp}{#1bp}\selectfont\spaceskip=\fontdimen2\font plus\fontdimen3\font\relax}
\newcommand{\slidesbox}{\slidesspacetrue\parindent=0pt\parskip=0pt\lineskip=0pt\lineskiplimit=-\maxdimen\hyphenpenalty=10000\exhyphenpenalty=50\tolerance=9999\emergencystretch=0pt\frenchspacing\hbadness=10000\hfuzz=\maxdimen\vbadness=10000\vfuzz=\maxdimen}
%
% \slidestyle{name}{size=, family=mono|serif, face=\fontswitch, weight=bold|w<NNN>, italic, color=,
%   ascent=, pitch=, depth=}: a text style. size in bp; ascent, pitch and depth are the Slides line
%   box of a paragraph in it (its first line's height above the baseline, the distance between
%   baselines, and what the box ends under its last line). A bullet's style needs no line box.
\def\slides@mono{mono}\def\slides@serif{serif}\def\slides@bold{bold}
\define@key{slidestyle}{size}{\def\slides@k@size{#1}}
\define@key{slidestyle}{family}{\def\slides@k@family{#1}}
\define@key{slidestyle}{face}{\def\slides@k@face{#1}}
\define@key{slidestyle}{weight}{\def\slides@k@weight{#1}}
\define@key{slidestyle}{italic}[]{\def\slides@k@italic{1}}
\define@key{slidestyle}{color}{\def\slides@k@color{#1}}
\define@key{slidestyle}{ascent}{\def\slides@k@ascent{#1}}
\define@key{slidestyle}{pitch}{\def\slides@k@pitch{#1}}
\define@key{slidestyle}{depth}{\def\slides@k@depth{#1}}
\newcommand\slidestyle[2]{%
  \let\slides@k@size\@empty\let\slides@k@family\@empty\let\slides@k@face\@empty
  \let\slides@k@weight\@empty\let\slides@k@italic\@empty\let\slides@k@color\@empty
  \let\slides@k@ascent\@empty\let\slides@k@pitch\@empty\let\slides@k@depth\@empty
  \setkeys{slidestyle}{#2}%
  \expandafter\edef\csname slides@s@#1\endcsname{%
    \ifx\slides@k@size\@empty\else\noexpand\slidesize{\slides@k@size}\fi
    \ifx\slides@k@family\slides@mono\noexpand\ttfamily\fi
    \ifx\slides@k@family\slides@serif\noexpand\rmfamily\fi
    \unexpanded\expandafter{\slides@k@face}%
    \ifx\slides@k@weight\@empty\else\ifx\slides@k@weight\slides@bold\noexpand\bfseries
      \else\noexpand\fontseries{\slides@k@weight}\noexpand\selectfont\fi\fi
    \ifx\slides@k@italic\@empty\else\noexpand\itshape\fi
    \ifx\slides@k@color\@empty\else\noexpand\color{\slides@k@color}\fi}%
  \expandafter\let\csname slides@s@#1@ascent\endcsname\slides@k@ascent
  \expandafter\let\csname slides@s@#1@pitch\endcsname\slides@k@pitch
  \expandafter\let\csname slides@s@#1@depth\endcsname\slides@k@depth}
\def\slides@use#1{\@ifundefined{slides@s@#1}{\PackageError{slides}{Unknown text style `#1'}%
    {Define it with \string\slidestyle.}}{}%
  \expandafter\let\expandafter\slides@lead\csname slides@s@#1\endcsname
  \expandafter\let\expandafter\slides@ascent\csname slides@s@#1@ascent\endcsname
  \expandafter\let\expandafter\slides@pitch\csname slides@s@#1@pitch\endcsname
  \expandafter\let\expandafter\slides@depth\csname slides@s@#1@depth\endcsname}
%
% \begin{slidebox}[options]{x,y,w,h} ... \end{slidebox}: a text box whose text starts x bp from the
%   page's left edge, y bp from its top, lines broken at w bp, in a box h bp tall. Options: middle,
%   bottom (where the text stands in the box; top by default), inset= (bp above a top-aligned box's
%   first line or under a bottom-aligned one's last: \setslideinset gives the default), tail= (bp
%   stacked under a middle- or bottom-aligned box's last line: its last paragraph's space below), and
%   any of \slidepar's style=, left, center, right, justify, indent=, rindent=, first=, lang=, space=, which
%   its paragraphs and list items then take unless they say otherwise.
\newif\ifslides@top
\newif\ifslides@mixed
\newif\ifslides@open
\newif\ifslides@item
\def\slidesinset{0}
\newcommand\setslideinset[1]{\def\slidesinset{#1}}
% \setslidepar{options}: what every paragraph of the deck takes unless its box or itself says otherwise
\def\slides@deckpar{}
\newcommand\setslidepar[1]{\def\slides@deckpar{#1}}
\def\slides@addto#1#2{\ifx#1\@empty\def#1{#2}\else\expandafter\def\expandafter#1\expandafter{#1,#2}\fi}
\def\slides@keys#1{\expandafter\slides@keys@\expandafter{#1}}
\def\slides@keys@#1{\setkeys{slidepar}{#1}}
\def\slides@xywh#1,#2,#3,#4\@nil{\def\slides@x{#1}\def\slides@y{#2}\def\slides@w{#3}\def\slides@h{#4}}
\define@key{slidebox}{top}[]{\def\slides@valign{t}}
\define@key{slidebox}{middle}[]{\def\slides@valign{m}}
\define@key{slidebox}{bottom}[]{\def\slides@valign{b}}
\define@key{slidebox}{inset}{\def\slides@inset{#1}}
\define@key{slidebox}{tail}{\def\slides@tail{#1}}
\def\slides@boxkey#1{\define@key{slidebox}{#1}{\slides@addto\slides@boxpar{#1={##1}}}}
\def\slides@boxflag#1{\define@key{slidebox}{#1}[]{\slides@addto\slides@boxpar{#1}}}
\slides@boxkey{style}\slides@boxkey{indent}\slides@boxkey{rindent}\slides@boxkey{first}\slides@boxkey{lang}
\slides@boxkey{space}
\slides@boxflag{left}\slides@boxflag{center}\slides@boxflag{right}\slides@boxflag{justify}
\newenvironment{slidebox}[2][]{%
  \def\slides@valign{t}\let\slides@inset\slidesinset\let\slides@tail\@empty\let\slides@boxpar\@empty
  \setkeys{slidebox}{#1}\slides@xywh#2\@nil
  \global\let\slides@end\@empty\global\slides@toptrue\global\slides@openfalse
  \edef\slides@basefont{\noexpand\fontfamily{\f@family}\noexpand\fontseries{\f@series}%
    \noexpand\fontshape{\f@shape}\noexpand\selectfont}\let\slides@basecolor\current@color
  \let\itemize\slides@itemize\let\enditemize\slides@listend
  \let\enumerate\slides@enumerate\let\endenumerate\slides@listend
  \edef\slides@block{\noexpand\begin{textblock*}{\slides@w bp}(\slides@x bp,\slides@y bp)}\slides@block
  \vbox to\slides@h bp\bgroup\slidesbox
  \if t\slides@valign\vskip\slides@inset bp\relax\else\vss\fi}{%
  \slides@end
  \ifx\slides@tail\@empty\else\vskip\slides@tail bp\relax\fi
  \if b\slides@valign\vskip\slides@inset bp\relax\else\vss\fi
  \egroup\end{textblock*}}
%
% \slidepar[options]{words}: a paragraph of a slidebox. Options: style= (a \slidestyle), left, center,
%   right, justify; indent=, rindent= (bp from the box's left and right edges), first= (bp the first
%   line starts past indent), space= (bp between this paragraph and the one before beyond their line
%   boxes, which the two styles' line boxes give: Slides' space above or below), lang= (the babel
%   language of a right-to-left paragraph), mixed (words of several sizes, each carrying its line
%   box: \slidestrut), prevdepth= (bp: where the paragraph after a mixed one takes its line from).
\define@key{slidepar}{style}{\def\slides@style{#1}}
\define@key{slidepar}{left}[]{\def\slides@align{left}}
\define@key{slidepar}{center}[]{\def\slides@align{center}}
\define@key{slidepar}{right}[]{\def\slides@align{right}}
\define@key{slidepar}{justify}[]{\def\slides@align{justify}}
\define@key{slidepar}{indent}{\def\slides@indent{#1}}
\define@key{slidepar}{rindent}{\def\slides@rindent{#1}}
\define@key{slidepar}{first}{\ifdim#1bp=\z@\let\slides@first\@empty\else\def\slides@first{#1}\fi}
\define@key{slidepar}{space}{\def\slides@space{#1}}
\define@key{slidepar}{prevdepth}{\def\slides@prevdepth{#1}}
\define@key{slidepar}{mixed}[]{\slides@mixedtrue}
\define@key{slidepar}{lang}{\def\slides@lang{#1}}
% a list item's bullet: mark= (a \slidemark), or label= (typed, in labelstyle=; in an enumerate
%   \arabic*, \alph*, \Alph*, \roman*, \Roman* stand for the item's number), gap= (bp between its
%   right edge and the text); start= (an enumerate's first number)
\define@key{slidepar}{mark}{\def\slides@mark{#1}\let\slides@label\@empty}
\define@key{slidepar}{label}{\def\slides@label{#1}\let\slides@mark\@empty}
\define@key{slidepar}{labelstyle}{\def\slides@labelstyle{#1}}
\define@key{slidepar}{gap}{\def\slides@gap{#1}}
\define@key{slidepar}{start}{\def\slides@start{#1}}
\def\slides@reset{\def\slides@align{left}\def\slides@indent{0}\def\slides@rindent{0}\let\slides@first\@empty
  \let\slides@space\@empty\let\slides@prevdepth\@empty\slides@mixedfalse\let\slides@lang\@empty
  \let\slides@style\@empty\let\slides@mark\@empty\let\slides@label\@empty\let\slides@labelstyle\@empty
  \def\slides@gap{0}}
\def\slides@align@left{\def\slides@lfil{}\def\slides@rfil{ plus 1fil}\def\slides@pfil{}}
\def\slides@align@center{\def\slides@lfil{ plus 1fil}\def\slides@rfil{ plus 1fil}\def\slides@pfil{}}
\def\slides@align@right{\def\slides@lfil{ plus 1fil}\def\slides@rfil{}\def\slides@pfil{}}
\def\slides@align@justify{\def\slides@lfil{}\def\slides@rfil{}\def\slides@pfil{ plus 1fil}}
\newcommand\slidepar[1][]{%
  \slides@reset\slides@keys\slides@deckpar\slides@keys\slides@boxpar\setkeys{slidepar}{#1}%
  \slides@parstart
  \bgroup\aftergroup\slides@close\let\slides@next=}
% (the words are a group, not an argument: they are read with the catcodes they are set in)
\def\slides@parstart{\slides@use\slides@style\csname slides@align@\slides@align\endcsname
  \ifslides@top
    \ifx\slides@space\@empty\else\vskip\slides@space bp\relax\fi
  \else\ifx\slides@prevdepth\@empty
    \prevdepth=\dimexpr\prevdepth-(\slides@pbelow+\slides@ascent bp-\slides@pitch bp%
      \ifx\slides@space\@empty\else+\slides@space bp\fi)\relax
  \else\prevdepth=\slides@prevdepth bp\relax\fi\fi
  \global\slides@topfalse
  \ifx\slides@lang\@empty\else
    \edef\slides@begin{\noexpand\begin{otherlanguage}{\slides@lang}}\expandafter\slides@begin\fi
  \bgroup\leftskip=\slides@indent bp\slides@lfil\relax\rightskip=\slides@rindent bp\slides@rfil\relax
  \parfillskip=0bp\slides@pfil\relax\ifslides@mixed\lineskiplimit=0bp\relax\fi
  \noindent\slides@lead\vrule width0bp height\slides@ascent bp depth0bp\relax
  \ifx\slides@first\@empty\else\hskip\slides@first bp\relax\fi}
\def\slides@close{\baselineskip=\slides@pitch bp\par\egroup
  \ifx\slides@lang\@empty\else\end{otherlanguage}\fi
  \slides@noteend\slides@after}
% what the next paragraph and the box's end take from this one: its line box under the baseline
\def\slides@noteend{\ifslides@mixed\global\let\slides@end\@empty
  \else\xdef\slides@end{\noexpand\vskip\noexpand\dimexpr\slides@depth bp-\noexpand\prevdepth\noexpand\relax}\fi
  \xdef\slides@pbelow{\slides@pitch bp-\slides@ascent bp}}
\let\slides@after\relax
%
% itemize and enumerate in a slidebox: \item[options] words, where each item is a \slidepar with a
%   bullet. What an item of a list level looks like is said once for the deck,
%   \setslidelist{itemize|enumerate}{level}{options}, a list may say what its own items differ in
%   (\begin{itemize}[options]), and an item what it alone differs in.
\newcount\slides@n
\newcount\slides@level
\newcommand\setslidelist[3]{\@namedef{slides@L@#1@#2}{#3}}
\def\slides@itemize{\@ifnextchar[{\slides@list{itemize}}{\slides@list{itemize}[]}}
\def\slides@enumerate{\@ifnextchar[{\slides@list{enumerate}}{\slides@list{enumerate}[]}}
% (a list nested in an item starts from the box's own font and colour, not its item's)
\def\slides@list#1[#2]{\slides@finish
  \ifslides@item\slides@basefont\ifx\current@color\slides@basecolor\else
    \let\current@color\slides@basecolor\set@color\fi\fi
  \advance\slides@level\@ne\slides@itemfalse\let\item\slides@item
  \def\slides@env{#1}\def\slides@envopts{#2}%
  \def\slides@start{1}\setkeys{slidepar}{#2}\slides@n=\numexpr\slides@start-1\relax}
\def\slides@listend{\ifslides@item\slides@itemclose\fi}
\def\slides@item{\@ifnextchar[\slides@item@{\slides@item@[]}}
\def\slides@item@[#1]{\ifslides@item\slides@itemclose\fi
  \slides@itemtrue\advance\slides@n\@ne
  \begingroup\slides@reset\slides@keys\slides@deckpar\slides@keys\slides@boxpar
  \@ifundefined{slides@L@\slides@env @\the\slides@level}{}%
    {\expandafter\slides@keys\csname slides@L@\slides@env @\the\slides@level\endcsname}%
  \slides@keys\slides@envopts\setkeys{slidepar}{#1}%
  \slides@parstart\global\slides@opentrue\slides@marker\ignorespaces}
% an item's paragraph ends at the next \item, at \end of its list, or where a list nested in it begins
\def\slides@finish{\ifslides@open\global\slides@openfalse\baselineskip=\slides@pitch bp\par\slides@noteend\fi}
\def\slides@itemclose{\slides@finish\egroup\ifx\slides@lang\@empty\else\end{otherlanguage}\fi\endgroup}
\def\slides@marker{\ifx\slides@mark\@empty\ifx\slides@label\@empty\else
    \slidelabel{\slides@labelstyle}{\slides@labelbody}{\slides@gap}\fi
  \else\slidebullet{\slides@mark}{\slides@gap}\fi}
\def\slides@labelbody{\let\arabic\slides@arabic\let\alph\slides@alph\let\Alph\slides@Alph
  \let\roman\slides@roman\let\Roman\slides@Roman\slides@label}
\def\slides@arabic*{\number\slides@n}
\def\slides@alph*{\@alph\slides@n}
\def\slides@Alph*{\@Alph\slides@n}
\def\slides@roman*{\@roman\slides@n}
\def\slides@Roman*{\@Roman\slides@n}
%
% \slidetext[options]{x,y,w,h}{style}{words}: a slidebox holding one \slidepar, the options of both
%   in one list.
\define@key{slidetext}{top}[]{\slides@addto\slides@bo{top}}
\define@key{slidetext}{middle}[]{\slides@addto\slides@bo{middle}}
\define@key{slidetext}{bottom}[]{\slides@addto\slides@bo{bottom}}
\define@key{slidetext}{inset}{\slides@addto\slides@bo{inset=#1}}
\define@key{slidetext}{tail}{\slides@addto\slides@bo{tail=#1}}
\define@key{slidetext}{center}[]{\slides@addto\slides@po{center}}
\define@key{slidetext}{right}[]{\slides@addto\slides@po{right}}
\define@key{slidetext}{justify}[]{\slides@addto\slides@po{justify}}
\define@key{slidetext}{indent}{\slides@addto\slides@po{indent=#1}}
\define@key{slidetext}{rindent}{\slides@addto\slides@po{rindent=#1}}
\define@key{slidetext}{first}{\slides@addto\slides@po{first=#1}}
\define@key{slidetext}{space}{\slides@addto\slides@po{space=#1}}
\define@key{slidetext}{mixed}[]{\slides@addto\slides@po{mixed}}
\define@key{slidetext}{lang}{\slides@addto\slides@po{lang=#1}}
\newcommand\slidetext[3][]{\let\slides@bo\@empty\def\slides@po{style=#3}\setkeys{slidetext}{#1}%
  \edef\slides@go{\noexpand\begin{slidebox}[\slides@bo]{#2}%
    \noexpand\def\noexpand\slides@after{\noexpand\end{slidebox}}%
    \noexpand\slidepar[\slides@po]}%
  \slides@go}
%
% \slidelabel{style}{text}{gap}: a typed bullet (or number) in a style, ending gap bp before the
%   text (negative: into it). \slidebullet{name}{gap}: a drawn one, named by \slidemark.
%   \slidestrut{height}{depth}: a word's own line box in a paragraph of several sizes.
\newcommand\slidelabel[3]{\expandafter\let\expandafter\slides@llead\csname slides@s@#1\endcsname
  \llap{{\slides@llead #2}\hskip#3bp}}
\newcommand\slidemark[2]{\expandafter\def\csname slides@m@#1\endcsname{#2}}
\newcommand\slidebullet[2]{\llap{\csname slides@m@#1\endcsname\hskip#2bp}}
\newcommand\slidestrut[2]{\vrule width0bp height#1bp depth#2bp\relax}
% \slidebreak: a soft line break (Shift+Enter); \slidefillbreak: the same in a justified paragraph,
%   whose broken line stays unjustified.
\newcommand\slidebreak{\unskip\break}
\newcommand\slidefillbreak{\unskip\hfil\break}"""
ULEM = "\\usepackage[normalem]{ulem}"
TIKZ = "\\usepackage{tikz}"
# Ink of Slides' own bullet glyphs per em of the bullet's size (emit.BULLET_SHAPES, measured with
# tools/probe_bullets.py; the lift off the baseline from cs161-tls slide 4): (height, gap to
# indentFirstLine, bottom above the baseline). Drawn rather than typed, because the deck's typeface
# may not have ● ○ ■ at all and a missing glyph in lualatex is nothing on the page.
BULLET_INK = {"●": (0.413, 0.08, 0.06), "○": (0.43, 0.08, 0.07), "■": (0.45, 0.07, 0.0)}
RING_EM = 0.06              # ○'s stroke
GLYPH_GAP = 1.9             # a typed bullet's box ends this far before indentFirstLine (emit.BULLET_GAP)
WIDE_SPACING = 1.25         # lineSpacing from which the last line's extra space is not in the stack


def line_box(z: float, r: float) -> tuple[float, float]:
    """(height above the baseline, depth below it) of a Slides line of size z at lineSpacing r."""
    from .emit import ASCENT_EM, LINE_EM
    if r >= 1:
        return ASCENT_EM * z, (LINE_EM - ASCENT_EM) * z + (r - 1) * LINE_EM * z
    return ASCENT_EM * z - (1 - r) * 0.75 * LINE_EM * z, (LINE_EM - ASCENT_EM) * z - (1 - r) * 0.25 * LINE_EM * z


SNAP_FROM = 16.0            # Slides pt: single-spaced lines this big or bigger are a whole number of pixels apart


def snapped_line_box(z: float, r: float, scale: float, snap_on: bool = True) -> tuple[float, float]:
    """`line_box`, with the pitch of single-spaced lines on whole CSS pixels (`emit.snap`, in Slides pt:
    the IR's are `scale` times smaller). The thumbnails show it at lineSpacing 100%: 24 pt lines 28.5 pt
    apart, not 28.8 (34 boxes in 7 decks, hebrew-lesson's and arabic-training's right-to-left bodies
    among them), 18 pt ones 21.75, 22 pt ones 26.25, 16 pt ones 19.5; the depth takes the difference.
    Not below `SNAP_FROM` (gdg24's 14 pt Google Sans Text, journey-maps' 14 pt Montserrat, cs161-net's
    8 pt Arial stand 1.2 em apart; Arial at 14 pt snaps, so the line is drawn where the data is), not on
    pages wider than `deck_ir.SNAP_PAGE` (`snap_on`: the 1440 pt SlidesCarnival decks' 18 pt Inter
    and 36 pt NTR lines are 1.2 em apart) and not at other spacings (gdg24's 14 pt at 115% stand
    19.35 pt apart: unsnapped 19.32, snapped 19.5)."""
    from .emit import snap
    above, below = line_box(z, r)
    if snap_on and r == 1 and z * scale >= SNAP_FROM:
        below = snap((above + below) * scale) / scale - above
    return above, below


def para_size(p: dict) -> float:
    return max((r.get("size") or 10.0) for r in p["runs"]) if p["runs"] else 10.0


def mixed_sizes(p: dict) -> bool:
    """Is the paragraph in several sizes (`text_box_latex` then spaces it line by line)?"""
    return len({round(r.get("size") or 0, 2) for r in p["runs"] if r["text"].strip()}) > 1


def text_escape(text: str) -> str:
    """LaTeX for plain text as Slides shows it: runs of spaces are kept (Slides does not fold them),
    and straight quotes, backquotes and double hyphens stay what they are - fontspec's TeX ligatures
    would turn ds-lecture's "objects" into curly quotes and -- into an en dash."""
    from .inverse import latex_escape
    out = latex_escape(text.replace("\t", " ").replace("\x0b", " "))
    out = out.replace('"', "\\symbol{34}").replace("'", "\\symbol{39}").replace("`", "\\symbol{96}")
    out = out.replace("--", "-{}-").replace("--", "-{}-")
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
    # the spaces around the styled core are Slides' too, however many: ap-bio-stats' literal "•  "
    # bullets stand their text two Arial spaces off, where one TeX space folded them into one
    spaces = (lambda n: " " + "\\ " * (n - 1) if n else "")
    struts = getattr(ctx, "line_struts", None)
    outer = ""
    if struts is not None and r.get("script"):
        # a raised strut would make its line as much taller: ap-bio-stats' "E = mc²" line stood 4 pt low
        a, b = line_box(r.get("size") or base.get("size") or 10.0, struts)
        outer = f"\\slidestrut{{{num(a)}}}{{{num(b)}}}"
    elif struts is not None:
        # a paragraph of several sizes: every word carries its own line box (text_box_latex)
        a, b = line_box(r.get("size") or base.get("size") or 10.0, struts)
        strut = f"\\slidestrut{{{num(a)}}}{{{num(b)}}}"
        core = strut + (core if r.get("underline") or r.get("strike") else core.replace(" ", " " + strut))
    fam, bfam = r.get("family") or "sans", base.get("family") or "sans"
    sr, sb = series(r, ctx), series(base, ctx)
    if (lead or trail) and ((r.get("font") or "") != (base.get("font") or "") or fam != bfam
                            or sr != sb or bool(r.get("italic")) != bool(base.get("italic"))
                            or r.get("smallcaps") or r.get("script")
                            or abs((r.get("size") or 0) - (base.get("size") or 0)) > 0.01):
        # and they are as wide as the run's own font makes them: that bullet run is Arial in a Calibri
        # paragraph, and its spaces set in Carlito put every item's text 2 pt short (slides 3-7, 24 and
        # 35 gain too; only slide 1's "adopted in part by " before 6.3 pt text looks narrower)
        core = "\\ " * lead + core + "\\ " * trail
        lead = trail = 0
    if (r.get("font") or "") != (base.get("font") or "") and \
            (font_switch(r.get("font"), ctx) or font_switch(base.get("font"), ctx)):
        # a run in another of the deck's typefaces: its own switch, or the kind's document face
        cmd = font_switch(r.get("font"), ctx) or \
            {"mono": "\\ttfamily", "serif": "\\rmfamily"}.get(fam, "\\sffamily")
        core = f"{{{cmd} {core}}}"
    elif fam != bfam:
        core = {"mono": "\\texttt", "serif": "\\textrm", "sans": "\\textsf"}[fam if fam in ("mono", "serif") else "sans"] + f"{{{core}}}"
    if sr != sb:
        if sr in ("b", "m") and sb in ("b", "m"):
            core = ("\\textbf" if sr == "b" else "\\textmd") + f"{{{core}}}"
        else:
            core = f"{{{series_switch(sr)}{core}}}"
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
        core = f"{{\\slidesize{{{num(r['size'])}}}{core}}}"
    if r.get("link") and not str(r["link"]).startswith("#"):
        url = str(r["link"]).replace("\\", "/").replace("#", "\\#").replace("%", "\\%")
        core = f"\\href{{{url}}}{{{core}}}"
    return spaces(lead) + outer + core + spaces(trail)


def paragraph_base(p: dict) -> dict:
    """The style most of a paragraph's letters are in: set once at its start."""
    counts: dict = {}
    for r in p["runs"]:
        k = (round(r.get("size") or 0, 2), (r.get("color") or "").lower() or None, r.get("family") or "sans",
             bool(r.get("bold")), bool(r.get("italic")), r.get("weight"))
        counts[k] = counts.get(k, 0) + len(r["text"])
    size, colour, family, bold, italic, weight = max(counts, key=counts.get)
    fonts: dict = {}
    for r in p["runs"]:
        fonts[r.get("font") or ""] = fonts.get(r.get("font") or "", 0) + len(r["text"])
    out = {"size": size or 10.0, "color": colour, "family": family, "bold": bold, "italic": italic,
           "font": max(fonts, key=fonts.get) if fonts else ""}
    if weight:
        out["weight"] = weight
    return out


def font_switch(font: str, ctx: Context) -> str:
    """The `\\newfontfamily` command `font_preamble` made for a deck's second typeface, or ''."""
    return (getattr(ctx, "font_switches", None) or {}).get(font or "", "")


def runs_tex(runs: list[dict], base: dict, ctx: Context, brk: str) -> str:
    """A paragraph's runs; a soft break (Shift+Enter, \\x0b) ends the line wherever it stands - inside
    a bold word, at the paragraph's start - since the paragraph is already in horizontal mode
    (`\\noindent`) and the break is written between the styled pieces, never inside one."""
    out = []
    runs = list(runs)
    while runs and not runs[-1].get("hole") and not runs[-1]["text"].strip(" "):
        runs.pop()                          # a paragraph's trailing spaces show nowhere
    if runs and not runs[-1].get("hole"):
        runs[-1] = {**runs[-1], "text": runs[-1]["text"].rstrip(" ")}
    items: list[list] = []
    for r in runs:
        if r.get("hole"):
            items.append(["hole", r])
            continue
        for k, piece in enumerate(r["text"].split("\x0b")):
            if k:
                items.append(["brk"])
            if piece:
                items.append(["text", r, piece])
    for item in items:
        if item[0] == "hole":
            out.append(f"\\hskip{item[1]['hole']:.2f}pt ")
        elif item[0] == "brk":
            out.append(brk)
        else:
            tex = run_tex(item[1], base, item[2], ctx)
            if tex.startswith(" ") and out and out[-1].endswith(" ") and out[-1] != brk \
                    and not out[-1].endswith("\\ "):
                tex = "\\" + tex            # "a " + " b": two spaces, which TeX would fold into one
            out.append(tex)
    text = "".join(out)
    # TeX drops the spaces a paragraph opens with; Slides draws them
    lead = len(text) - len(text.lstrip(" "))
    text = text[lead:]
    while True:                             # nor does a paragraph end on a space, a kept one included
        text = text.rstrip(" ")
        if not (text.endswith("\\") and not text.endswith("\\\\")):
            break
        text = text[:-1]
    return "\\ " * lead + text


# Tab stops. The API reports none, and Slides' default ones stand every half inch from the text's left
# edge: creandum-board's agenda is "09:00<TAB><TAB>CEO Update<TAB>x7Board", its columns at 1 and 5 inches
# on the thumbnail. A tab written as a space ran the three columns together. TeX does not know where
# on a line it is, so the text before each tab is boxed and measured (\slidesx: the pen, from the box's
# text edge) and the tab is the glue to the next multiple of the stop.
TAB_STOP = 36.0             # Slides pt
SLIDES_TABS = (
    "\\newdimen\\slidesx\n"
    "\\newcount\\slidestabn\n"
    "\\newcommand\\slidestab[2]{\\setbox0\\hbox{#2}\\global\\advance\\slidesx\\wd0 \\unhbox0 "
    "\\slidestabn=\\numexpr\\slidesx/\\dimexpr#1\\relax\\relax"
    "\\ifdim\\slidestabn\\dimexpr#1\\relax>\\slidesx \\advance\\slidestabn-1 \\fi"
    "\\advance\\slidestabn1 "
    "\\hskip\\dimexpr\\slidestabn\\dimexpr#1\\relax-\\slidesx\\relax"
    "\\global\\slidesx=\\slidestabn\\dimexpr#1\\relax}")


def tabbed_tex(runs: list[dict], base: dict, ctx: Context, brk: str, start: float, stop: float) -> str | None:
    """A paragraph with tabs set on Slides' default stops (`stop` pt apart, the pen `start` pt from the
    text edge where the line begins), or None when a soft break would move the pen elsewhere."""
    if any("\x0b" in r["text"] for r in runs):
        return None
    segments: list[list[dict]] = [[]]
    for r in runs:
        for k, piece in enumerate(r["text"].split("\t")):
            if k:
                segments.append([])
            if piece:
                segments[-1].append({**r, "text": piece})
    ctx.packages.add(SLIDES_TABS)
    out = [f"\\global\\slidesx={start:.2f}pt"]
    for seg in segments[:-1]:
        text = runs_tex(seg, base, ctx, brk) if seg else ""
        trail = len("".join(r["text"] for r in seg)) - len("".join(r["text"] for r in seg).rstrip(" "))
        spaces = "\\ " * trail                  # the spaces before a tab move the pen too
        out.append(f"\\slidestab{{{stop:.2f}pt}}{{{text}{spaces}}}")
    # a paragraph ending on a tab keeps the tab's glue: a space after it is what \par takes away
    out.append((runs_tex(segments[-1], base, ctx, brk) if segments[-1] else "") or " ")
    return "".join(out)


def num(v: float, digits: int = 2) -> str:
    """`v` to `digits` decimals without the zeros a fixed format pads it with: the same length to TeX
    (12.50 and 12.5 are the same number of sp), fewer characters to read."""
    s = f"{v:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-", "-0") else s


def colour_word(hex_colour: str) -> str:
    """A name a person would give a colour (`Blue`, `LightGrey`, `DarkRed`): the hue, with Light or
    Dark where the lightness says so; near-greys by their lightness."""
    import colorsys
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    if max(r, g, b) - min(r, g, b) < 0.08:
        for top, word in ((0.15, "NearBlack"), (0.35, "DarkGrey"), (0.65, "Grey"), (0.9, "LightGrey")):
            if light < top:
                return word
        return "OffWhite"
    deg = hue * 360
    for top, word in ((12, "Red"), (40, "Orange"), (65, "Yellow"), (90, "Lime"), (150, "Green"), (185, "Teal"),
                      (200, "Cyan"), (255, "Blue"), (285, "Purple"), (320, "Magenta"), (345, "Pink"), (361, "Red")):
        if deg < top:
            break
    if word in ("Orange", "Red") and light < 0.35:
        word = "Brown" if word == "Orange" else "DarkRed"
    elif word == "Red" and light > 0.75:
        word = "Pink"
    elif light > 0.8:
        word = "Pale" + word
    elif light > 0.65:
        word = "Light" + word
    elif light < 0.25:
        word = "Dark" + word
    return word


SIZE_WORDS = ((2.0, "title"), (1.4, "heading"), (1.0001, "large"), (0.75, "small"), (0.0, "tiny"))
WEIGHT_WORDS = {100: "thin", 200: "extralight", 300: "light", 400: "regular", 500: "medium", 600: "semibold",
                700: "bold", 800: "extrabold", 900: "black"}


def text_style(ctx: Context, size: float, family: str = "", face: str = "", weight: str = "",
               italic: bool = False, colour: str | None = None, metrics: tuple | None = None) -> str:
    """The name of a text style (`\\slidestyle` in the preamble), made on first use: its size next to
    the deck's body size (title, heading, large, body, small, tiny), then what sets it apart - its
    typeface, weight, slant and colour where that is not the deck's usual text colour. `metrics` is
    the paragraph's line box (ascent, pitch, depth; bp strings), None for a bullet's style."""
    colour_tex = colour_name(colour, ctx.colours) if colour else ""
    key = (num(size), family, face, weight, italic, colour_tex, metrics)
    styles = ctx.__dict__.setdefault("text_styles", {})
    ctx.__dict__["last_style_key"] = key            # what `box_parts` records: names are made per context
    if key in styles:
        return styles[key]
    body = getattr(ctx, "body_size", None) or size
    if abs(size - body) < 0.05:
        words = ["body"]
    else:
        words = [next(w for top, w in SIZE_WORDS if size / body >= top)]
    if face:
        font = next((f for f, s in (getattr(ctx, "font_switches", None) or {}).items() if s == face), "")
        words.append(re.sub(r"[^a-z0-9]", "", font.lower())[:16] or "face")
    elif family in ("mono", "serif"):
        words.append(family)
    if weight:
        words.append("bold" if weight == "bold" else WEIGHT_WORDS.get(int(weight[1:]), weight))
    if italic:
        words.append("italic")
    if colour and colour.lower() != (getattr(ctx, "main_colour", None) or "").lower():
        words.append(NAMED_WORDS.get(colour.lower()) or colour_word(colour).lower())
    if metrics is None:
        words[0] = "label"
    name = "-".join(words)
    taken = set(styles.values())
    if name in taken:
        name = f"{name}-{num(size, 1)}"
    base, k = name, 2
    while name in taken:
        name, k = f"{base}-{k}", k + 1
    styles[key] = name
    return name


NAMED_WORDS = {"#000000": "black", "#ffffff": "white"}


def style_definitions(ctx: Context) -> list[str]:
    """`\\slidestyle` lines for the styles the frames use, in the order they were first used."""
    out = []
    for (size, family, face, weight, italic, colour, metrics), name in (getattr(ctx, "text_styles", None) or {}).items():
        keys = [f"size={size}"]
        keys += [f"family={family}"] if family in ("mono", "serif") else []
        keys += [f"face={face}"] if face else []
        keys += [f"weight={weight}"] if weight else []
        keys += ["italic"] if italic else []
        keys += [f"color={colour}"] if colour else []
        if metrics:
            keys += [f"ascent={metrics[0]}", f"pitch={metrics[1]}", f"depth={metrics[2]}"]
        out.append(f"\\slidestyle{{{name}}}{{{', '.join(keys)}}}")
    for code, name in (getattr(ctx, "bullet_marks", None) or {}).items():
        out.append(f"\\slidemark{{{name}}}{{{code}}}")
    return out


def bullet_mark(ctx: Context, glyph: str, z: float, colour: str | None, code: str) -> str:
    """The name a drawn bullet (`\\slidemark`) goes by: dot, ring or square, its colour where that is
    not the deck's usual text colour, its size when the deck draws it in more than one."""
    marks = ctx.__dict__.setdefault("bullet_marks", {})
    if code in marks:
        return marks[code]
    words = [{"●": "dot", "○": "ring", "■": "square"}[glyph]]
    if colour and colour.lower() != (getattr(ctx, "main_colour", None) or "").lower():
        words.append(NAMED_WORDS.get(colour.lower()) or colour_word(colour).lower())
    name = "-".join(words)
    taken = set(marks.values())
    if name in taken:
        name = f"{name}-{num(z, 1)}"
    base, k = name, 2
    while name in taken:
        name, k = f"{base}-{k}", k + 1
    marks[code] = name
    return name


def bullet_tex(p: dict, ctx: Context, scale: float, right: float) -> str:
    """The bullet, its right edge `right` pt from where the line's text starts (negative: left of it):
    `\\slidebullet` for the ● ○ ■ Slides draws, `\\slidelabel` for a typed one."""
    spec = bullet_spec(p, ctx, scale, right)
    if spec is None:
        return ""                               # a list paragraph whose level shows no glyph
    if spec["mark"]:
        return f"\\slidebullet{{{ctx.bullet_marks[spec['mark'][1]]}}}{{{spec['gap']}}}"
    return f"\\slidelabel{{{ctx.text_styles[spec['labelstyle'][1]]}}}{{{spec['literal']}}}{{{spec['gap']}}}"


def bullet_spec(p: dict, ctx: Context, scale: float, right: float) -> dict | None:
    """What `bullet_tex` draws, as a list item's keys (`\\item`, `\\setslidelist`): `mark` a drawn
    bullet ("M", its \\slidemark code), or `label` typed text in `labelstyle` ("S", the style's key);
    `gap` bp from its right edge to the text; `literal` the typed text. None: no glyph."""
    b = p["bullet"]
    glyph = (b.get("text") or "").strip()
    if not glyph:
        return None
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
        code = to_bp(pic)
        bullet_mark(ctx, glyph, z, b.get("color"), code)
        return {"mark": ("M", code), "label": "", "labelstyle": "", "gap": num(-right), "literal": ""}
    right -= GLYPH_GAP / scale
    family = b.get("font_family") if b.get("font_family") in ("mono", "serif") else ""
    text_style(ctx, float(f"{z:.2f}"), family, "", "bold" if b.get("bold") else "", False, b.get("color"))
    literal = text_escape(glyph)
    return {"mark": "", "label": literal, "labelstyle": ("S", ctx.last_style_key), "gap": num(-right),
            "literal": literal}


def box_insets(el: dict) -> tuple[float, float]:
    """(side inset, top/bottom inset) of a text box, page pt: Slides' own, a PowerPoint deck's, or
    none (`deck_ir.zero_insets`)."""
    from .emit import BASELINE_A, PAD_X
    box = el.get("box") or {}
    scale = box.get("scale") or 720 / 453.54
    pad, inset = (0.0, 0.0) if box.get("insets") == 0 else (PAD_X / scale, BASELINE_A / scale)
    if box.get("inset_y") is not None and box.get("insets") != 0:
        # PowerPoint's own top and bottom insets, which a deck's thumbnails showed (deck_ir.pptx_insets)
        inset = (BASELINE_A - (SLIDES_INSET_Y - box["inset_y"])) / scale
    if box.get("inset_x") is not None and box.get("insets") != 0:
        pad = box["inset_x"] / scale                    # the same deck's side insets
    return pad, inset


SLIDES_INSET_Y = 7.2        # Slides pt: Slides' own top and bottom text insets, which BASELINE_A includes
FIT_SLACK = 0.01            # page pt: the box's edges are read to 0.01 pt


def measure(width: float, paras: list[dict], scale: float) -> float:
    """The measure (page pt) a text box's lines are broken at: its width, scaled as its words are.
    The IR gives sizes in page pt to 0.01, so TeX sets words up to 0.05% wider or narrower than Slides
    does, and Slides keeps a line exactly as wide as its box on it: gdg24's code listing (69 characters
    of 0.6 em at 15 pt = 621.0 slide pt in a 621.0 pt box, 9.44875 page pt written 9.45) broke one
    more line in TeX than on the thumbnail, while sc-dark-minimal's "About Us." (27.714 pt written
    27.71), 0.04 pt wider than its box in Slides, fitted once the measure was 0.06 pt wider."""
    for p in paras:
        true = ((p.get("slides") or {}).get("size") or 0) / scale
        if true and p["runs"]:
            ratio = para_size(p) / true
            if abs(ratio - 1) < 0.002:
                return width * ratio + FIT_SLACK
            break
    return width + FIT_SLACK


def trailing_space(last: dict, valign: str, shape: str | None = None) -> float:
    """The last paragraph's spaceBelow (slide pt) that a middle- or bottom-aligned box stacks under its
    last line: Slides places the stack with it, so the lines stand that much (half of it, centred)
    higher than their own height puts them. intro-lecture's "add(add(6, ...))" / "???" title (30 pt
    and 48 pt, 10 pt below each) stood 5 pt low without it, jeb-arch's "Unit 1" labels 2-4 pt. A
    top-aligned box shows none of it, and neither do gdg24's turned ELLIPSE stickers (10 pt below
    too): their words stood within 0.8 pt of the thumbnail's without it and 1.2-1.9 pt high with it."""
    if valign not in ("middle", "bottom") or shape == "ELLIPSE":
        return 0.0
    return float((last.get("slides") or {}).get("space_below") or 0.0)


def text_box_latex(el: dict, ctx: Context, ind: str) -> str:
    """A text box laid out as Slides lays it out: the element's own box, the vertical alignment done
    by TeX (`\\vbox to` its height with the slack above, below or both), each paragraph at its own
    size, pitch, spacing and indents, bullets drawn where Slides draws them. See the notes above.
    Written as a `slidebox` of `\\slidepar`s and lists (`SLIDES_TEXT`): `box_parts` works out what
    each paragraph is, `box_source` says it with the deck's, the box's and the list's defaults."""
    return box_source(box_parts(el, ctx), ctx, ind)


# A paragraph's keys (`\slidepar`, `\item`, `slidebox` defaults, `\setslidepar`, `\setslidelist`) and
# what each is when nothing says it: `style` has no default until the deck names one.
PAR_KEYS = {"style": None, "align": "left", "indent": "0", "rindent": "0", "first": "0", "lang": "",
            "space": "0"}
# an item's bullet is one key here, ("M", a drawn mark's code) or ("L", typed label text), and says
# `mark=` or `label=` (each of which clears the other); a drawn mark has no `labelstyle` (None:
# whatever it inherits)
ITEM_KEYS = {"bullet": "", "labelstyle": "", "gap": "0"}
# what a list level says for its items (`\setslidelist`): a box's defaults never reach them there
LEVEL_KEYS = ("style", "indent", "first", "labelstyle", "bullet", "gap")
KEY_ORDER = ("style", "align", "indent", "rindent", "first", "lang", "labelstyle", "bullet", "gap", "space")


def box_parts(el: dict, ctx: Context) -> dict:
    """What `text_box_latex` writes, before it is said: the box's options and geometry, and per
    paragraph its full keys (`keys`, styles as ("S", key) and drawn bullets as ("M", code), since
    names are made per context), what only it says (`extra`: space=, prevdepth=, mixed), its words,
    and for a list item its list (`item`: env, level, the number its glyph reads as). `single` is the
    old one-paragraph form (`\\slidetext`), options in their order and the bullet in the words."""
    box = el.get("box") or {}
    scale = box.get("scale") or 720 / 453.54
    x0, y0, x1, y1 = el["bbox"]
    # a box with no insets (deck_ir.zero_insets) sets its text against its edges
    pad, inset = box_insets(el)
    width, height = max(x1 - x0 - 2 * pad, 1.0), max(y1 - y0, 0.1)
    valign = box.get("valign", "top")
    if valign not in ("middle", "bottom"):
        valign = "top"
    paras = [p for p in el["paragraphs"] if p["runs"]]
    ctx.packages.add(TEXTPOS)
    ctx.packages.add(SLIDES_TEXT)
    box_opts = [valign] if valign != "top" else []
    if valign != "middle" and f"{inset:.2f}" != getattr(ctx, "slide_inset", None):
        box_opts.append(f"inset={num(inset)}")
    recs = []
    prev = None
    prev_metrics = None
    for p in paras:
        sl = p.get("slides") or {}
        z, r = para_size(p), sl.get("line_spacing") or 1.0
        above, below = snapped_line_box(z, r, scale, bool(box.get("snap")))
        pitch = above + below
        # Slides spaces each line by the sizes on that line: comps-analysis's "First step:" at 26.7 pt
        # leads 21.3 pt words, and the line they wrap onto is 21.3 pt apart, where one \baselineskip
        # for the paragraph set it 26.7 pt apart. Such a paragraph's words carry their own line box
        # (`run_tex`), the skip is its smallest size's, and a line with bigger words grows by them.
        mixed = mixed_sizes(p)
        if mixed:
            pitch = sum(line_box(min(x.get("size") or z for x in p["runs"] if x["text"].strip()), r))
        left, first = (sl.get("indent_start") or 0) / scale, (sl.get("indent_first") or 0) / scale
        end = (sl.get("indent_end") or 0) / scale
        glyph = p.get("bullet") and (p["bullet"].get("text") or "").strip()
        shift = max(0.0, first - left) if p.get("bullet") else first - left
        space = None            # (key, value) as the one-paragraph form writes it
        target = 0.0            # the step \prevdepth takes (bp), where it is one
        if prev is None:
            # a box that grows to fit its text (SHAPE_AUTOFIT) draws its first line without the first
            # paragraph's spaceAbove: gdg24's body copy says 22 pt and starts 22 pt higher than
            # that, while ds-lecture's bodies (no autofit type) keep their master's 6 pt
            if sl.get("space_above") and not box.get("grows"):
                space = ("space", num(sl["space_above"] / scale))
        else:
            psl, pz, pr = prev.get("slides") or {}, para_size(prev), (prev.get("slides") or {}).get("line_spacing") or 1.0
            # between two list items each paragraph's own spacingMode says whether its side of the gap
            # collapses: creandum-board's first item (NEVER_COLLAPSE, 3 pt below) keeps its 3 pt above
            # the next one (COLLAPSE_LISTS), measured 3.6 pt lower on the thumbnail than with no gap
            listed = bool(prev.get("bullet") and p.get("bullet"))
            below = 0 if listed and psl.get("spacing_mode") != "NEVER_COLLAPSE" else psl.get("space_below") or 0
            above_ = 0 if listed and sl.get("spacing_mode") != "NEVER_COLLAPSE" else sl.get("space_above") or 0
            # and the two sides overlap, the bigger one wins: ap-bio-stats' slide 52 (11 pt below, 11 pt
            # above) stands its second paragraph 11 pt apart on the thumbnail, not 22
            gap = max(below, above_) / scale
            if mixed_sizes(prev):
                # its last line's depth is its own words' (their struts), which TeX has in \prevdepth
                space = ("prevdepth", num(pitch - gap - above))
            else:
                # \prevdepth less the space between the two line boxes (a negative space overlaps them)
                k = pitch - snapped_line_box(pz, pr, scale, bool(box.get("snap")))[1] - gap - above
                if num(k) != "0":
                    target = -float(f"{k:.2f}")
                    space = ("space", num(target))
        # LuaTeX's skips are logical: in a right-to-left paragraph \leftskip is at its start, the
        # right edge, where Slides measures indentStart from too - so only the alignment flips.
        rtl = p.get("direction") == "rtl"
        align = p.get("align", "left")
        if rtl:
            align = {"left": "right", "right": "left"}.get(align, align)
        justified = bool(sl.get("justified")) and align == "left"
        keys = dict(PAR_KEYS)
        opts = []
        if justified:
            opts.append("justify")
            keys["align"] = "justify"
        elif align in ("center", "right"):
            opts.append(align)
            keys["align"] = align
        if num(left) != "0":
            opts.append(f"indent={num(left)}")
            keys["indent"] = num(left)
        if num(end) != "0":
            opts.append(f"rindent={num(end)}")
            keys["rindent"] = num(end)
        if shift and num(shift) != "0":
            opts.append(f"first={num(shift)}")
            keys["first"] = num(shift)
        if space:
            opts.append(f"{space[0]}={space[1]}")
        if mixed:
            opts.append("mixed")
        base = paragraph_base(p)
        weight = series(base, ctx)
        pr_ = sl.get("line_spacing") or 1.0
        last = line_box(z, 1.0 if pr_ >= WIDE_SPACING else pr_)[1]
        metrics = (num(above), num(pitch), num(last))
        style = text_style(ctx, float(f"{base['size']:.2f}"),
                           base["family"] if base["family"] in ("mono", "serif") else "",
                           font_switch(base.get("font"), ctx),
                           {"m": "", "b": "bold"}.get(weight, weight), bool(base["italic"]), base["color"],
                           metrics)
        keys["style"] = ("S", ctx.last_style_key)
        brk = "\\slidefillbreak " if justified else "\\slidebreak "
        blank = not any(x["text"].strip() for x in p["runs"])
        ctx.line_struts = r if mixed else None
        body = "" if blank else runs_tex(p["runs"], base, ctx, brk)
        mark = ""
        spec = None
        if glyph:
            spec = bullet_spec(p, ctx, scale, first - left - shift)
            mark = bullet_tex(p, ctx, scale, first - left - shift) if spec else ""
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
        if "\t" in "".join(x["text"] for x in p["runs"]) and not rtl and align == "left" and not blank \
                and (glyph or not p.get("bullet") and first >= left):
            # a bulleted line's text starts at indentStart (or where the bullet pushed it), and its tabs
            # count from the text edge like any other: creandum-board's "DD/MM/YY XX am<TAB><TAB>Other
            # important date" items stand their second column at 180 pt, not one space after "am"
            pen = max(left, first) if glyph else first
            body = tabbed_tex(p["runs"], base, ctx, brk, pen, TAB_STOP / scale) or body
        ctx.line_struts = None
        # a right-to-left paragraph is set in its language (scripts.py: babel's bidi, shaping)
        if rtl:
            from .scripts import rtl_language
            opts.append(f"lang={rtl_language(p)}")
            keys["lang"] = rtl_language(p)
        words = mark + body
        if re.search(r"(~|\\ |\s|\\[A-Za-z@]+)\}*$", words):
            # \par takes the last glue off the paragraph: a space after words that end in a tie or a
            # control space (arabic-training's "Meeting~~~", comps-analysis' underlined "Pros ~ ~}")
            # is what it takes, as the line end after them did when each paragraph was spelled out
            words += " "
        # Beyond the top, the step between two paragraphs is the macro's to work out from the two
        # styles' line boxes (the one before ends pitch - ascent under its baseline, this one starts
        # ascent above its own and is pitch from it); what is left is Slides' space between them.
        # (a key like the others, which a box or list may say once; at the top it is the space above
        # the first line, and after a mixed paragraph `prevdepth=` says it all)
        extra = []
        if space and space[0] == "prevdepth":
            extra.append(f"prevdepth={space[1]}")
            keys["space"] = None
        elif prev is not None:
            auto = float(prev_metrics[1]) - float(prev_metrics[0]) + float(metrics[0]) - float(metrics[1])
            keys["space"] = num(target - auto)
        elif space:
            keys["space"] = space[1]
        if mixed:
            extra.append("mixed")
        item = None
        if spec is not None:
            keys["bullet"] = spec["mark"] or ("L", spec["label"])
            keys["labelstyle"] = None if spec["mark"] else spec["labelstyle"]
            keys["gap"] = spec["gap"]
            kind = "enumerate" if (p["bullet"].get("kind") == "number") else "itemize"
            number = None
            if kind == "enumerate":
                parsed = number_format(glyph)
                if parsed:
                    keys["bullet"], number = ("L", parsed[0]), parsed[1]
            item = {"env": kind, "level": int(p.get("level") or 0), "number": number, "literal": spec["literal"],
                    "words": body}
        recs.append({"keys": keys, "extra": extra, "words": words, "item": item,
                     "single": (opts, style, words)})
        prev, prev_metrics = p, metrics
    if prev is not None:
        # The space a wide lineSpacing adds under a line is not under the stack's last one: a middle-
        # aligned box centres the lines without it (sc-dark-modern's quotes at 170% sat 7 pt high,
        # sc-aesthetic-school's 150% numbers 14 pt). At 115% it is there all the same (firebase-jam,
        # apps-edu-zh, ap-bio-stats: measured to the pixel both ways), hence the threshold: the
        # style's depth, which `slidebox` ends the stack on (a mixed paragraph's words end it).
        tail = trailing_space(prev, valign, el.get("shape_type")) / scale
        if num(tail) != "0":
            box_opts.append(f"tail={num(tail)}")
    geometry = ",".join((num(x0 + pad, 1), num(y0, 1), num(measure(width, paras, scale)), num(height, 1)))
    return {"box_opts": box_opts, "geometry": geometry, "recs": recs}


ROMAN = (("m", 1000), ("cm", 900), ("d", 500), ("cd", 400), ("c", 100), ("xc", 90), ("l", 50), ("xl", 40),
         ("x", 10), ("ix", 9), ("v", 5), ("iv", 4), ("i", 1))


def roman(n: int) -> str:
    out = ""
    for s, v in ROMAN:
        while n >= v:
            out, n = out + s, n - v
    return out


def number_format(glyph: str) -> tuple[str, int] | None:
    """A numbered item's glyph as an enumerate label and the number it reads: "3." -> ("\\\\arabic*.",
    3), "B)" -> ("\\\\Alph*)", 2), "iv." -> ("\\\\roman*.", 4). A single letter is a letter (Slides' ALPHA),
    several a roman numeral where they spell one; None where no counter prints it (01., 27 letters)."""
    m = re.fullmatch(r"(\(?)([0-9]+|[a-z]+|[A-Z]+)([.)]?)", glyph)
    if not m:
        return None
    pre, tok, post = m.groups()
    if tok.isdigit():
        if len(tok) > 1 and tok.startswith("0"):
            return None
        return f"{pre}\\arabic*{post}", int(tok)
    lower = tok.lower()
    if len(tok) == 1:
        return f"{pre}\\{'alph' if tok.islower() else 'Alph'}*{post}", ord(lower) - 96
    for n in range(1, 400):
        if roman(n) == lower:
            return f"{pre}\\{'roman' if tok.islower() else 'Roman'}*{post}", n
    return None


def number_text(label: str, n: int) -> str | None:
    """What an enumerate label prints for number `n`, or None when the label is typed text."""
    m = re.fullmatch(r"(\(?)\\(arabic|alph|Alph|roman|Roman)\*([.)]?)", label)
    if not m:
        return None
    kind = m[2]
    if kind in ("alph", "Alph") and not 1 <= n <= 26:
        return None
    tok = {"arabic": str(n), "alph": chr(96 + n) if n > 0 else "", "Alph": chr(64 + n) if n > 0 else "",
           "roman": roman(n), "Roman": roman(n).upper()}[kind]
    return m[1] + tok + m[3]


def key_text(k: str, v, ctx: Context) -> str:
    """One key as the macros read it."""
    if k == "align":
        return v
    if k == "bullet":
        if not v:
            return "label={}"
        return f"mark={ctx.bullet_marks[v[1]]}" if v[0] == "M" else f"label={{{v[1]}}}"
    if isinstance(v, tuple):
        v = ctx.text_styles[v[1]]
    return f"{k}={v}"


def keys_text(keys: dict, ctx: Context) -> list[str]:
    return [key_text(k, keys[k], ctx) for k in KEY_ORDER if k in keys and keys[k] is not None]


def item_own(keys: dict, base: dict) -> dict:
    """What a paragraph or item says for itself over what it inherits (`base`)."""
    return {k: v for k, v in keys.items() if v is not None and base.get(k) != v}


def majority(values: list):
    """The most common value, the first seen winning a tie; None for none."""
    counts: dict = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get) if counts else None


def choose_defaults(recs: list[dict], bases: list[dict], keys) -> dict:
    """The defaults a box or a list says once for `recs`, each of which would otherwise inherit its
    `bases` entry: per key the value most of them have, where saying it once and the exceptions
    costs fewer keys than saying each exception to the inherited value."""
    out = {}
    for k in keys:
        pairs = [(r[k], b.get(k)) for r, b in zip(recs, bases) if r.get(k) is not None]
        if not pairs:
            continue
        v = majority([r for r, _ in pairs])
        unset = sum(1 for r, b in pairs if r != b)
        if 1 + sum(1 for r, _ in pairs if r != v) < unset:
            out[k] = v
    return out


def level_keys(items: list[dict]) -> dict:
    """A list level as most of its items are."""
    return {k: majority([r[k] for r in items if r.get(k) is not None]) for k in LEVEL_KEYS}


def deck_level(ctx: Context, env: str, depth: int, items: list[dict]) -> dict:
    """What `\\setslidelist{env}{depth}` says: the deck's majority (`deck_text_survey`), or when no
    survey was made, what most of these items say."""
    levels = ctx.__dict__.setdefault("list_levels", {})
    if (env, depth) not in levels:
        levels[(env, depth)] = level_keys([r["keys"] for r in items])
    return levels[(env, depth)]


def level_definitions(ctx: Context) -> list[str]:
    """`\\setslidepar` and `\\setslidelist` lines for the deck's paragraph and list-level defaults."""
    out = []
    if getattr(ctx, "deck_style", None):
        out.append(f"\\setslidepar{{{key_text('style', ctx.deck_style, ctx)}}}")
    for (env, depth), level in sorted((getattr(ctx, "list_levels", None) or {}).items(),
                                      key=lambda kv: (kv[0][0] != "itemize", kv[0])):
        said = {k: v for k, v in level.items()
                if v is not None and (k == "style" or v != {**PAR_KEYS, **ITEM_KEYS}[k])}
        out.append(f"\\setslidelist{{{env}}}{{{depth}}}{{{','.join(keys_text(said, ctx))}}}")
    return out


def deck_text_survey(target: dict, ctx: Context) -> None:
    """The deck's paragraph style (`\\setslidepar`) and its list levels (`\\setslidelist`): what most
    paragraphs of its multi-paragraph boxes, and most items of each level, are. Worked out on a copy
    of the context, the real one naming the styles as the frames first use them."""
    import copy
    scratch = copy.deepcopy(ctx)
    styles, levels = [], {}
    for s in target.get("slides") or []:
        for el in text_elements(s.get("elements") or []):
            if el.get("kind") != "text" or el.get("wordart") or not el.get("bbox"):
                continue
            recs = box_parts(el, scratch)["recs"]
            if len(recs) == 1 and recs[0]["item"] is None:
                continue
            for r in recs:
                it = r["item"]
                if it is None:
                    styles.append(r["keys"]["style"])
                else:
                    levels.setdefault((it["env"], it["level"] + 1), []).append(r["keys"])
    style = majority(styles)
    if style is not None and styles.count(style) > 1:
        ctx.deck_style = style
    ctx.list_levels = {k: level_keys(v) for k, v in levels.items()}


def box_source(parts: dict, ctx: Context, ind: str) -> str:
    """`box_parts` said as briefly as the macros allow: one paragraph as `\\slidetext`; else a
    slidebox whose options carry what most of its paragraphs share, `\\slidepar`s saying what they
    alone say, and list items as `itemize` / `enumerate` whose level (`\\setslidelist`) and list
    options say what their items share."""
    recs, box_opts, geometry = parts["recs"], parts["box_opts"], parts["geometry"]
    brackets = (lambda o: f"[{','.join(o)}]" if o else "")
    if len(recs) == 1 and recs[0]["item"] is None:
        # one paragraph: the box and it on one line
        opts, style, words = recs[0]["single"]
        return f"{ind}\\slidetext{brackets(box_opts + opts)}{{{geometry}}}{{{style}}}{{{words}}}"
    deck = {**PAR_KEYS, **ITEM_KEYS, "style": getattr(ctx, "deck_style", None)}
    has_items = any(r["item"] for r in recs)
    # the box's own defaults, for its paragraphs and its items alike: what a list level says
    # (`\setslidelist`) is not the box's to say where it holds a list
    box_keys = [k for k in PAR_KEYS if not (has_items and k in LEVEL_KEYS)]
    box_layer = choose_defaults([r["keys"] for r in recs], [deck] * len(recs), box_keys)
    inherited = {**deck, **box_layer}
    out = [f"{ind}\\begin{{slidebox}}{brackets(box_opts + keys_text(box_layer, ctx))}{{{geometry}}}"]
    # the lists: each item's run of list levels, opened and closed around it
    stack: list[dict] = []          # {"env", "depth", "items": [rec], "lines": [...], "at": out index}
    body: list = []                 # lines and list nodes in order

    def close():
        node = stack.pop()
        (stack[-1]["body"] if stack else body).append(node)

    for r in recs:
        it = r["item"]
        if it is None:
            while stack:
                close()
            body.append(r)
            continue
        depth = it["level"] + 1
        while stack and (stack[-1]["depth"] > depth or (stack[-1]["depth"] == depth and stack[-1]["env"] != it["env"])):
            close()
        while not stack or stack[-1]["depth"] < depth:
            stack.append({"env": it["env"], "depth": (stack[-1]["depth"] + 1) if stack else 1, "body": []})
        stack[-1]["body"].append(r)
    while stack:
        close()

    def items_of(node):
        return [x for x in node["body"] if isinstance(x, dict) and "keys" in x]

    def write_par(r, pad):
        own = {k: v for k, v in r["keys"].items() if k in PAR_KEYS and inherited.get(k) != v}
        opts = keys_text(own, ctx) + r["extra"]
        out.append(f"{pad}\\slidepar{brackets(opts)}{{{r['words']}}}")

    def write_list(node, pad):
        items = items_of(node)
        level = deck_level(ctx, node["env"], node["depth"], items) if items else {}
        base = {**inherited, **level}
        env_opts = []
        start = 1
        if items:
            env_layer = choose_defaults([r["keys"] for r in items], [base] * len(items), KEY_ORDER)
            base = {**base, **env_layer}
            if node["env"] == "enumerate":
                first = items[0]
                if first["keys"]["bullet"] == base["bullet"] and first["item"]["number"] is not None:
                    start = first["item"]["number"]
                for k, r in enumerate(items):
                    # a number the counter would not print is typed as it reads
                    bullet = r["keys"]["bullet"]
                    if bullet and bullet[0] == "L" and bullet[1] != r["item"]["literal"] and \
                            number_text(bullet[1], start + k) != r["item"]["literal"]:
                        r["keys"]["bullet"] = ("L", r["item"]["literal"])
            env_opts = keys_text(env_layer, ctx) + ([f"start={start}"] if start != 1 else [])
        out.append(f"{pad}\\begin{{{node['env']}}}{brackets(env_opts)}")
        for x in node["body"]:
            if "keys" in x:
                opts = keys_text(item_own(x["keys"], base), ctx) + x["extra"]
                words = x["item"]["words"]
                if words.startswith("["):
                    words = "{}" + words           # not the item's options
                out.append(f"{pad}  \\item{brackets(opts)}" + (f" {words}" if words else ""))
            else:
                write_list(x, pad + "  ")
        out.append(f"{pad}\\end{{{node['env']}}}")

    for x in body:
        if "keys" in x:
            write_par(x, ind + "  ")
        else:
            write_list(x, ind + "  ")
    out.append(f"{ind}\\end{{slidebox}}")
    return "\n".join(out)


def url_latex(url: str) -> str:
    """A URL as `\\href`'s first argument takes it inside a frame body."""
    return url.replace("\\", "/").replace("%", "\\%").replace("#", "\\#").replace("{", "%7B").replace("}", "%7D")


def letterboxed(src: Path, w: float, h: float, dest: Path) -> Path:
    """A video's poster frame as the player shows it in a `w` x `h` box: the frame itself (YouTube's
    thumbnail carries a 16:9 video letterboxed into 4:3; the black bars are found and cut off) fitted
    into the box, on black. A 4:3 box gives the thumbnail back as it was; a 16:9 box shows no bars."""
    from PIL import Image
    if dest.exists():
        return dest
    with Image.open(src) as img:
        img = img.convert("RGB")
        width, height = img.size
        ink = img.convert("L").point(lambda v: 255 if v > 40 else 0).getbbox()   # rows that are not bars
        if ink and ink[3] - ink[1] > height // 3:
            img = img.crop((0, ink[1], width, ink[3]))
        cw, ch = img.size
        out_w = 960
        out_h = max(1, round(out_w * h / max(w, 0.1)))
        k = min(out_w / cw, out_h / ch)
        frame = img.resize((max(1, round(cw * k)), max(1, round(ch * k))), Image.LANCZOS)
        canvas = Image.new("RGB", (out_w, out_h), (0, 0, 0))
        canvas.paste(frame, ((out_w - frame.width) // 2, (out_h - frame.height) // 2))
        dest.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(dest, "PNG")
    return dest


def video_block(el: dict, ctx: Context, ind: str, tree: Path | None) -> str:
    """A video: the frame Slides shows before it plays, linked to where it plays. With no poster
    frame to show (a Drive video: no API gives one) a dark panel with a play symbol stands in."""
    video = el["video"]
    x0, y0, x1, y1 = el.get("box") or el["bbox"]
    w, h = max(x1 - x0, 0.1), max(y1 - y0, 0.1)
    link = video.get("url")
    body = None
    if el.get("file") and Path(el["file"]).exists() and tree is not None:
        dest = tree / "figures" / f"video-{flatten(video.get('id') or 'x')[:24]}-{round(w)}x{round(h)}.png"
        try:
            # a frame read off the slide's thumbnail is already as the player shows it
            if el.get("poster") == "thumbnail":
                dest.parent.mkdir(parents=True, exist_ok=True)
            framed = Path(shutil.copyfile(el["file"], dest)) if el.get("poster") == "thumbnail" else \
                letterboxed(Path(el["file"]), w, h, dest)
        except OSError:
            framed = None
        if framed is not None:
            from .inverse import Picture, natural_size
            pic = Picture(framed.relative_to(tree).as_posix(), framed, natural_size(framed))
            body = picture_block({**{k: v for k, v in el.items() if k != "crop"}}, pic, ctx, ind)
    if body is None:
        ctx.packages.add("\\usepackage{tikz}")
        grey, white = colour_name("#212121", ctx.colours), colour_name("#ffffff", ctx.colours)
        r = min(w, h) / 6
        cx, cy = w / 2, -h / 2
        tri = (f"({cx - r * 0.45:.1f}pt,{cy + r * 0.6:.1f}pt) -- ({cx + r * 0.65:.1f}pt,{cy:.1f}pt) -- "
               f"({cx - r * 0.45:.1f}pt,{cy - r * 0.6:.1f}pt) -- cycle")
        outline = el.get("outline")
        draw = f",draw={colour_name(outline['color'], ctx.colours)},line width={outline['weight']:.2f}pt" \
            if outline else ""
        body = tikz_block(f"\\path[fill={grey}{draw}] (0pt,0pt) rectangle ({w:.1f}pt,{-h:.1f}pt);\n"
                          f"{ind}    \\path[draw={white},line width={max(r / 8, 0.4):.2f}pt] ({cx:.1f}pt,{cy:.1f}pt) circle ({r:.1f}pt);\n"
                          f"{ind}    \\path[fill={white}] {tri};", x0, y0, w, h, ind)
    if not link:
        return body
    # the whole block's content is the link: the picture or the panel, clicked, plays the video
    head, _, rest = body.partition("\n")
    inner, _, tail = rest.rpartition(f"{ind}\\end{{textblock*}}")
    return f"{head}\n{ind}  \\href{{{url_latex(link)}}}{{%\n{inner}{ind}  }}%\n{ind}\\end{{textblock*}}{tail}"


def wordart_block(el: dict, ctx: Context, ind: str) -> str:
    """WordArt: its words stretched to its box, as Slides draws them (Slides keeps no size for them,
    only the box), turned with the element. Fill and outline are not in the API: the text colour."""
    from .inverse import latex_escape
    x0, y0, x1, y1 = el.get("box") or el["bbox"]
    w, h = max(x1 - x0, 0.1), max(y1 - y0, 0.1)
    lines = ["".join(r["text"] for r in p["runs"]) for p in el["paragraphs"]]
    colour = next((r.get("color") for p in el["paragraphs"] for r in p["runs"] if r.get("color")), None)
    text = latex_escape(lines[0]) if len(lines) == 1 else \
        "\\begin{tabular}{@{}c@{}}" + "\\\\".join(latex_escape(t) for t in lines) + "\\end{tabular}"
    ctx.packages.add("\\usepackage{graphicx}")
    ctx.packages.add(TEXTPOS)
    # bold: Slides draws WordArt with a heavy outline around the letters, so thin strokes read wrong
    body = f"\\resizebox*{{{w:.1f}pt}}{{{h:.1f}pt}}{{\\bfseries {text}}}"
    if colour:
        body = f"\\textcolor{{{colour_name(colour, ctx.colours)}}}{{{body}}}"
    if el.get("rotation"):
        body = f"\\rotatebox[origin=c]{{{-el['rotation']:g}}}{{{body}}}"
    bx0, by0, bx1, _ = el["bbox"]
    return (f"{ind}\\begin{{textblock*}}{{{bx1 - bx0:.1f}pt}}({bx0:.1f}pt,{by0:.1f}pt)\n"
            f"{ind}  \\noindent{body}\n{ind}\\end{{textblock*}}\n")


# \slidepicture: a picture on one line, its Slides edits as options, drawn as `inverse.picture_block`
# spelled it out (the options go into \includegraphics, a \tikz node, \reflectbox and \rotatebox in
# the same order, so the page is the same to the pixel).
PICTURE_MACRO = r"""% --- Pictures ---------------------------------------------------------------------------------
% \slidepicture[options]{x,y,w,h}{file}: a picture w by h bp whose top left corner is x bp from the
%   page's left edge and y bp from its top. Options, the picture's edits in Slides:
%   trim=l b r t   bp of the file cut off its left, bottom, right and top (graphicx's trim, clipped);
%   angle=a        turned a degrees counter-clockwise; x,y is then the corner of the turned picture's bounds;
%   flip           mirrored left to right;
%   opacity=o      see-through, 0..1;
%   outline=colour, outline width=bp, dash=dotted|dashed: a line drawn on the picture's edge; x,y is then
%                  the line's outer corner, half its width up and left of the picture's.
\define@key{slidepicture}{trim}{\def\slides@p@trim{trim=#1,clip,}}
\define@key{slidepicture}{angle}{\def\slides@p@angle{#1}}
\define@key{slidepicture}{flip}[]{\def\slides@p@flip{1}}
\define@key{slidepicture}{opacity}{\def\slides@p@opacity{#1}}
\define@key{slidepicture}{outline}{\def\slides@p@outline{#1}}
\define@key{slidepicture}{outline width}{\def\slides@p@weight{#1}}
\define@key{slidepicture}{dash}{\def\slides@p@dash{,#1}}
\newcommand\slidepicture[3][]{\slides@xywh#2\@nil
  \let\slides@p@trim\@empty\let\slides@p@angle\@empty\let\slides@p@flip\@empty\let\slides@p@opacity\@empty
  \let\slides@p@outline\@empty\def\slides@p@weight{0.75}\let\slides@p@dash\@empty
  \setkeys{slidepicture}{#1}%
  \let\slides@p@node\@empty
  \ifx\slides@p@opacity\@empty\else\edef\slides@p@node{,text opacity=\slides@p@opacity}\fi
  \ifx\slides@p@outline\@empty\else
    \edef\slides@p@node{\slides@p@node,draw=\slides@p@outline,line width=\slides@p@weight bp\slides@p@dash}\fi
  % turned by graphicx itself unless a node or a mirror wraps the picture: then by \rotatebox
  \let\slides@p@turn\@firstofone\let\slides@p@gangle\@empty
  \ifx\slides@p@angle\@empty\else
    \ifx\slides@p@node\@empty\ifx\slides@p@flip\@empty\def\slides@p@gangle{,angle=\slides@p@angle}\fi\fi
    \ifx\slides@p@gangle\@empty\edef\slides@p@turn{\noexpand\rotatebox{\slides@p@angle}}\fi\fi
  \edef\slides@p@opts{\slides@p@trim width=\slides@w bp,height=\slides@h bp\slides@p@gangle}%
  \ifx\slides@p@flip\@empty\let\slides@p@mirror\@firstofone\else\let\slides@p@mirror\reflectbox\fi
  \ifx\slides@p@node\@empty\let\slides@p@frame\@firstofone\else\let\slides@p@frame\slides@p@tikz\fi
  \edef\slides@block{\noexpand\begin{textblock*}{\slides@w bp}(\slides@x bp,\slides@y bp)}\slides@block
  \slides@p@turn{\slides@p@frame{\slides@p@mirror{\expandafter\includegraphics\expandafter[\slides@p@opts]{#3}}}}%
  \end{textblock*}}
\def\slides@p@tikz#1{\edef\slides@p@go{\noexpand\tikz\noexpand\node[inner sep=0bp\slides@p@node]}\slides@p@go{#1};}
\def\slides@xywh#1,#2,#3,#4\@nil{\def\slides@x{#1}\def\slides@y{#2}\def\slides@w{#3}\def\slides@h{#4}}"""


def slide_picture(te: dict, pic, ctx: Context, ind: str) -> str:
    """A deck picture at its place, one `\\slidepicture` line: the numbers `inverse.picture_block`
    wrote (its block's corner, the picture's size), the edits as named options."""
    from .adopt_shapes import pt1
    x0, y0, _, _ = te["bbox"]
    bx0, by0, bx1, by1 = te.get("box") or te["bbox"]
    outline = te.get("outline")
    pad = outline["weight"] / 2 if outline and not te.get("rotation") else 0.0
    opts = []
    crop = te.get("crop")
    if crop:
        nw, nh = pic.natural
        trim = (crop["l"] * nw, crop["b"] * nh, crop["r"] * nw, crop["t"] * nh)
        opts.append("trim=" + " ".join(num(max(0.0, v)) for v in trim))
    angle = round(-(te.get("rotation") or 0.0), 2)
    if angle:
        opts.append(f"angle={angle:g}")
    if te.get("flip"):
        opts.append("flip")
    if te.get("opacity") is not None and te["opacity"] < 0.995:
        opts.append(f"opacity={num(te['opacity'])}")
    if outline:
        opts.append(f"outline={colour_name(outline['color'], ctx.colours)}")
        if num(outline["weight"]) != "0.75":
            opts.append(f"outline width={num(outline['weight'])}")
        if outline.get("dash", "SOLID") != "SOLID":
            opts.append("dash=" + ("dotted" if "DOT" in outline["dash"] and "DASH" not in outline["dash"] else "dashed"))
    if outline or te.get("opacity") is not None and te["opacity"] < 0.995:
        ctx.packages.add(TIKZ)
    ctx.packages.add("\\usepackage{graphicx}")
    ctx.packages.add(TEXTPOS)
    ctx.packages.add(PICTURE_MACRO)
    box = ",".join(pt1(v) for v in (x0 - pad, y0 - pad, bx1 - bx0, by1 - by0))
    return f"{ind}\\slidepicture{'[' + ','.join(opts) + ']' if opts else ''}{{{box}}}{{{pic.rel}}}\n"


def tikz_block(body: str, x0: float, y0: float, w: float, h: float, ind: str) -> str:
    """A tikzpicture at page coordinates, hanging from the top of its textblock the way a picture
    does: everything is drawn below the origin, so the picture has no height and the whole of it is
    depth, which puts the origin on the block's first baseline and therefore at (x0, y0)."""
    return (f"{ind}\\begin{{textblock*}}{{{max(w, 0.1):.1f}pt}}({x0:.1f}pt,{y0:.1f}pt)\n"
            f"{ind}  \\begin{{tikzpicture}}[baseline=(current bounding box.north),inner sep=0pt,outer sep=0pt]\n"
            f"{ind}    {body}\n"
            f"{ind}  \\end{{tikzpicture}}\n"
            f"{ind}\\end{{textblock*}}\n")


def shape_block(el: dict, ctx: Context, ind: str, tree: Path | None = None) -> str:
    """A panel, a node of a flow chart or a connector, at its place on the page.

    tikz rather than `\\rule`, because a foreign deck's shapes are not only filled rectangles: the
    flow charts of both templates are outlined boxes with rounded corners, and a rule can say
    neither. A shape with no fill and no outline draws nothing and is left out, so the ink is the
    deck's and nothing else."""
    from . import adopt_shapes
    if el.get("role") == "line" and el.get("from") and el.get("to"):
        return adopt_shapes.line_block(el, ctx, ind, tree)
    return adopt_shapes.shape_block(el, ctx, ind, tree)


# `table_block` writes a `slidetable`, which reads like a tabular and is drawn by these. A Slides row
# is a minimum height that grows until its tallest cell fits, and only TeX knows how tall a cell's
# text comes out, so the table is measured where it is drawn: every cell is set into a box first
# (`\slides@t@grow`, which grows the last row it spans until the rows hold it), `\slides@t@tops` then
# adds the rows up, and the tikzpicture after it puts fills, borders and boxes at `\slides@t@y{row}`.
# Horizontal places are decimal sums of the column widths (l3fp); lines and fills go to the tenth of a
# point, as the grid of tikz commands this replaces spelled them (pgf turns `90.7bp` into pt with a
# truncated factor, so the same number must reach it the same way for the same pixels).
#
# The insets are a guess, and Slides does not always wrap inside them: creandum-board's native
# table (left inset 7.2 pt, measured on the thumbnail) keeps "+1 months", 35 pt of 7 pt Arial Bold,
# on one line in a 40.2 pt column, and its row is stored 22.1 pt - one line. A stored row height is
# what Slides laid out, so a cell whose text would need more than its rows give it is set again
# without the insets, and kept that way when that takes fewer lines.
#
# A line stands in its cell as in a Slides line box, 0.968 em of its 1.2 above the baseline (emit's
# ASCENT_EM), not as in LaTeX's 70/30 strut: a middle-aligned cell's baseline is 0.368 em under the
# middle, where the strut put creandum-board's P&L 1 pt high in every row. So the text is set with the
# strut, which decides how rows grow (a Slides-shaped strut of its own grew comps-analysis' tight rows
# under their underlined headings), and a middle-aligned cell's is drawn 10.67% of the strut lower
# (`\slides@t@drop`), where a Slides line box puts it. Top and bottom ones stay: their place depends on the
# vertical inset, which is inferred (`deck_ir.cell_pad`), and comps-analysis' top-aligned cells sat
# within 0.3 pt with the strut and 0.8 pt low with the drop.
#
# Slides never hyphenates, so neither does a cell. (It does break a word between two letters when
# the word is wider than the cell; TeX lets it stick out instead, because a penalty between every
# two letters, tried, also split the numbers of creandum-board's narrow columns where Slides keeps
# them whole: the inset is a guess, and a word that sticks out costs less than a row that grows.)
#
# One word wider than the room the insets leave is set with no insets at all, in its alignment:
# creandum-board's P&L puts 25 pt numbers into 33 pt columns (18.6 pt inside the insets); the
# thumbnail shows the centred ones centred on the cell and a left-aligned one starting 1 pt from the
# cell's edge, where a paragraph left them all sticking out to the right from the left inset, 4 pt
# off in every one of its 200 cells. TeX finds such a word itself (`\slides@t@check`: a cell of one
# paragraph that came out one line that does not fit the room), so the source says nothing about it; a
# cell whose one line has no place to break for another reason (a box, no break at all) says `wrap`.
# "Does not fit" is what TeX itself says when it boxes the line to the room, not whether the line is
# wider unset: a line of several words that fits by shrinking its spaces fits, and journey-maps' 10^6
# * X, 0.2 pt over its column and 0.1 pt of shrink in each of its two spaces, is not a wide word.
TABLE_MACROS = r"""% --- Tables ---------------------------------------------------------------------------------
% \begin{slidetable}[options]{x,y}{w1,...,wn} rows \end{slidetable}: a table whose top left corner is
%   x bp from the page's left edge and y bp from its top, its columns w1...wn bp wide. A row is written
%   as in a tabular, `a & b & c \\`; a cell is its text, or \cell[options]{text} (text holding \\ or
%   several paragraphs), or \multicell{k}[options]{text} over k columns (rows=r: and r rows; the rows
%   under it then leave its columns out). \row[options] opens a row. After the last row,
%   \hborder{i}[a-b]{style} restyles the line above row i (\vborder{j}...: left of column j), from
%   cell a to cell b of it or all of it; `none` takes it away.
%   A row is as tall as it says unless its cells need more (Slides' rule), or `fixed`.
%   Options, of the table, a \row or a cell (the nearest one says):
%     inset=d, inset x=d, inset y=d  the text's distance from the cell's edges, bp (table only)
%     border=style                   TikZ options of every border line (table only); none
%     h=d, fixed | grow              the row's height, bp; fixed: kept whatever its text needs
%     fill=colour, fill opacity=o    the cell's fill; none
%     valign=top|middle|bottom, align=left|center|right
%     aligns={a1,...,an}             each column's align (table only; a cell's own still wins)
%     style=switches, pitch=d        the text's font and colour; its lines d bp apart
%     baseline=d                     the first baseline of a top-aligned cell d bp under its top (the
%                                    last of a bottom-aligned one d bp over its bottom); empty: the
%                                    inset places the text
%     lang=language                  the text is right to left, in that babel language
%     word | wrap                    one word too wide for the insets is set without them (word, the
%                                    default), or sticks out like any line (wrap)
\newbox\slides@t@split
\newbox\slides@t@cellbox
\newcount\slides@t@paras
\newif\ifslides@t@over
\def\slides@t@val#1#2{\csname slides@t@#1@#2\endcsname}% cell #1's value of #2
\def\slides@t@setcell#1#2{% cell #2 set #1 wide, into \slides@t@cellbox
  \global\slides@t@overfalse
  \setbox\slides@t@cellbox\vbox{\slides@t@begin{#1}{#2}\slides@t@val{#2}{body}\slides@t@end{#2}\slides@t@check{#2}}%
  \ifslides@t@over
    \setbox\slides@t@cellbox\vbox{\slides@t@begin{#1}{#2}\slides@t@word{#2}\slides@t@end{#2}}%
  \fi}
\def\slides@t@begin#1#2{\hsize=#1\relax\linewidth\hsize\parindent\z@
  \hyphenpenalty\@M\exhyphenpenalty\@M\global\slides@t@paras\z@
  \everypar{\global\advance\slides@t@paras\@ne\strut}%
  \raggedright\slides@t@val{#2}{style}%
  \slides@t@ifempty{#2}{pitch}{}{\baselineskip=\slides@t@val{#2}{pitch}bp\relax}%
  \slides@t@ifempty{#2}{lang}{\slides@t@ltr{#2}}{% babel wants the language's name, not a macro holding it
    \edef\slides@t@lang{\noexpand\begin{otherlanguage}{\slides@t@val{#2}{lang}}}\slides@t@lang\slides@t@rtl{#2}}}
\def\slides@t@end#1{\slides@t@ifempty{#1}{lang}{}{\par\end{otherlanguage}}%
  \ifhmode\strut\fi
  \xdef\slides@t@lastdrop{\the\dimexpr(\ht\strutbox+\dp\strutbox)*1067/10000\relax}}
% one paragraph set in one line that does not fit its room (TeX is asked to box the line to it: a
% line that fits by shrinking its spaces fits): a word to set again without the insets
\def\slides@t@check#1{\ifnum0\slides@t@val{#1}{word}=\@ne
  \par
  \ifnum\slides@t@paras=\@ne\ifnum\prevgraf=\@ne
    \setbox\z@\lastbox\hfuzz\maxdimen\hbadness\@M
    \setbox\tw@\hbox to\hsize{\unhcopy\z@}%
    \ifnum\badness>\@M\global\slides@t@overtrue\fi
    \nointerlineskip\box\z@
  \fi\fi\fi}
\def\slides@t@word#1{\noindent\hbox to\linewidth{\setbox\z@\hbox{\slides@t@val{#1}{body}}%
  \ifdim\wd\z@>\linewidth\kern-\slides@t@ix bp\hbox to\dimexpr\linewidth+\slides@t@ixx bp{\slides@t@lf{#1}\box\z@\slides@t@rf{#1}}%
  \kern-\slides@t@ix bp\else\slides@t@lf{#1}\box\z@\slides@t@rf{#1}\fi}}
% cell #1, from row #2 to row #3, its text #4 wide with the vertical inset #5; #6 wide with no insets,
% shifted by #7: its box, the rows it ends grown to hold it
\def\slides@t@grow#1#2#3#4#5#6#7{%
  \expandafter\ifx\csname slides@t@box@#1\endcsname\relax\expandafter\newbox\csname slides@t@box@#1\endcsname\fi
  \slides@t@setcell{#4}{#1}\global\setbox\csname slides@t@box@#1\endcsname\box\slides@t@cellbox
  \expandafter\slides@t@first\csname slides@t@box@#1\endcsname
  \expandafter\xdef\csname slides@t@drop@#1\endcsname{\slides@t@lastdrop}%
  \dimen@\z@\@tempcnta#2\relax
  \loop\advance\dimen@\csname slides@t@h@\the\@tempcnta\endcsname\relax
  \ifnum\@tempcnta<#3\relax\advance\@tempcnta\@ne\repeat
  \dimen@ii\dimexpr\ht\csname slides@t@box@#1\endcsname+\dp\csname slides@t@box@#1\endcsname+#5*2\relax
  % a measured row is what Slides laid out, to the pixel: text that fills it exactly is no sign of
  % text set without insets
  \if1\csname slides@t@fix@#3\endcsname\skip@\p@\else\skip@\z@\fi
  \ifdim\dimen@ii>\dimexpr\dimen@+\skip@\relax
    \slides@t@setcell{#6}{#1}\setbox\@tempboxa\box\slides@t@cellbox
    \ifdim\dimexpr\ht\@tempboxa+\dp\@tempboxa\relax<\dimexpr\dimen@ii-#5*2\relax
      \slides@t@first\@tempboxa
      \global\setbox\csname slides@t@box@#1\endcsname\hbox to #4{\kern#7\box\@tempboxa\hss}%
      \dimen@ii\dimexpr\ht\csname slides@t@box@#1\endcsname+\dp\csname slides@t@box@#1\endcsname+#5*2\relax
    \fi
  \fi
  \if1\csname slides@t@fix@#3\endcsname\else
    \ifdim\dimen@ii>\dimen@
      \expandafter\edef\csname slides@t@h@#3\endcsname{\the\dimexpr\csname slides@t@h@#3\endcsname+\dimen@ii-\dimen@\relax}%
    \fi
  \fi
  \expandafter\let\csname slides@t@ht@#1\endcsname\slides@t@fht
  \expandafter\xdef\csname slides@t@dp@#1\endcsname{\the\dp\csname slides@t@box@#1\endcsname}}
\def\slides@t@first#1{% from a vbox's top to its first baseline (its height reaches its last, and a
  % colour's whatsit on either end keeps \lastbox from counting lines): pieces split off the top until
  % one holds a line
  {\setbox\slides@t@split\copy#1\vbadness\@M\vfuzz\maxdimen\splittopskip\z@\splitmaxdepth\maxdimen
   \dimen@\z@\@tempswatrue
   \loop\setbox\z@\vsplit\slides@t@split to\z@\setbox\z@\vbox{\unvbox\z@}%
     \ifdim\ht\z@>\z@\@tempswafalse\advance\dimen@\ht\z@\else\advance\dimen@\dp\z@\fi
     \ifvoid\slides@t@split\@tempswafalse\fi
   \if@tempswa\repeat
   \xdef\slides@t@fht{\the\dimen@}}}
\def\slides@t@tops#1{% \slides@t@y{k}: the top of row k, and \slides@t@y{#1} the table's foot
  \dimen@\z@\@tempcnta\z@
  \loop\expandafter\edef\csname slides@t@y@\the\@tempcnta\endcsname{\the\dimen@}%
  \ifnum\@tempcnta<#1\relax
    \advance\dimen@\csname slides@t@h@\the\@tempcnta\endcsname\relax\advance\@tempcnta\@ne\repeat}
\def\slides@t@y#1{\csname slides@t@y@#1\endcsname}
\def\slides@t@drop#1{\csname slides@t@drop@#1\endcsname}% a middle cell's line box under TeX's strut
\def\slides@t@ht#1{\csname slides@t@ht@#1\endcsname}% the height of a cell's first line
\def\slides@t@dp#1{\csname slides@t@dp@#1\endcsname}% the depth of its last
\def\slides@t@picture#1#2{\begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0pt,outer sep=0pt]
  \path[use as bounding box] (0bp,0bp) rectangle (#1bp,{-\slides@t@y{#2}});}
\def\slides@t@fill#1#2#3#4#5{\fill[#1] (#2bp,{-\slides@t@y{#3}}) rectangle (#4bp,{-\slides@t@y{#5}});}
\def\slides@t@hdraw#1#2#3#4{\draw[line cap=rect,#1] (#2bp,{-\slides@t@y{#4}}) -- (#3bp,{-\slides@t@y{#4}});}
\def\slides@t@vdraw#1#2#3#4{\draw[line cap=rect,#1] (#2bp,{-\slides@t@y{#3}}) -- (#2bp,{-\slides@t@y{#4}});}
\def\slides@t@node#1#2#3#4{\node[anchor=#1] at (#2bp,{#3}) {\copy\csname slides@t@box@#4\endcsname};}
\ExplSyntaxOn
\tl_new:N \l__slides_t_ix_tl
\tl_new:N \l__slides_t_iy_tl
\tl_new:N \l__slides_t_border_tl
\tl_new:N \l__slides_t_h_tl
\tl_new:N \l__slides_t_fill_tl
\tl_new:N \l__slides_t_op_tl
\tl_new:N \l__slides_t_valign_tl
\tl_new:N \l__slides_t_align_tl
\tl_new:N \l__slides_t_style_tl
\tl_new:N \l__slides_t_pitch_tl
\tl_new:N \l__slides_t_base_tl
\tl_new:N \l__slides_t_lang_tl
\tl_new:N \l__slides_t_row_tl
\tl_new:N \l__slides_t_body_tl
\tl_new:N \l__slides_t_tail_tl
\tl_new:N \l__slides_t_k_tl
\tl_new:N \l__slides_t_x_tl
\tl_new:N \l__slides_t_seg_tl
\tl_new:N \l__slides_t_now_tl
\tl_new:N \l__slides_t_anchor_tl
\tl_new:N \l__slides_t_where_tl
\bool_new:N \l__slides_t_fixed_bool
\bool_new:N \l__slides_t_word_bool
\int_new:N \l__slides_t_m_int
\int_new:N \l__slides_t_span_int
\int_new:N \l__slides_t_rs_int
\int_new:N \l__slides_t_len_int
\int_new:N \l__slides_t_a_int
\int_new:N \l__slides_t_r_int
\int_new:N \l__slides_t_c_int
\int_new:N \l__slides_t_r_one_int
\int_new:N \l__slides_t_c_one_int
\int_new:N \g__slides_t_r_int
\int_new:N \g__slides_t_c_int
\int_new:N \g__slides_t_n_int
\int_new:N \g__slides_t_rs_int
\prop_new:N \g__slides_t_occ_prop
\prop_new:N \g__slides_t_seg_prop
\prop_new:N \g__slides_t_in_prop
\seq_new:N \l__slides_t_rows_seq
\seq_new:N \l__slides_t_cells_seq
\seq_new:N \l__slides_t_w_seq
\seq_new:N \l__slides_t_styles_seq
\clist_new:N \l__slides_t_xy_clist
\clist_new:N \l__slides_t_aligns_clist
\cs_generate_variant:Nn \seq_set_split_keep_spaces:Nnn { NnV }
\keys_define:nn { slides/table }
  {
    inset .code:n = { \tl_set:Nn \l__slides_t_ix_tl {#1} \tl_set:Nn \l__slides_t_iy_tl {#1} } ,
    inset~x .tl_set:N = \l__slides_t_ix_tl ,
    inset~y .tl_set:N = \l__slides_t_iy_tl ,
    border .tl_set:N = \l__slides_t_border_tl ,
    h .tl_set:N = \l__slides_t_h_tl ,
    fixed .bool_set:N = \l__slides_t_fixed_bool ,
    grow .bool_set_inverse:N = \l__slides_t_fixed_bool ,
    fill .tl_set:N = \l__slides_t_fill_tl ,
    fill~opacity .tl_set:N = \l__slides_t_op_tl ,
    valign .choices:nn = { top , middle , bottom } { \tl_set_eq:NN \l__slides_t_valign_tl \l_keys_choice_tl } ,
    align .choices:nn = { left , center , right } { \tl_set_eq:NN \l__slides_t_align_tl \l_keys_choice_tl } ,
    aligns .clist_set:N = \l__slides_t_aligns_clist ,
    style .tl_set:N = \l__slides_t_style_tl ,
    pitch .tl_set:N = \l__slides_t_pitch_tl ,
    baseline .tl_set:N = \l__slides_t_base_tl ,
    lang .tl_set:N = \l__slides_t_lang_tl ,
    word .bool_set:N = \l__slides_t_word_bool ,
    wrap .bool_set_inverse:N = \l__slides_t_word_bool ,
    rows .int_set:N = \l__slides_t_rs_int ,
  }
\cs_new:Npn \slides@t@ifempty #1#2 { \tl_if_empty:cTF { slides@t@ #1 @ #2 } }
\cs_new:Npn \slides@t@ltr #1
  { \str_case_e:nn { \tl_use:c { slides@t@ #1 @align } } { { center } { \centering } { right } { \raggedleft } } }
\cs_new:Npn \slides@t@rtl #1
  { \str_case_e:nn { \tl_use:c { slides@t@ #1 @align } }
      { { center } { \centering } { left } { \raggedleft } { right } { \raggedright } } }
\cs_new:Npn \slides@t@lf #1
  { \str_case_e:nn { \tl_use:c { slides@t@ #1 @align } } { { center } { \hss } { right } { \hss } } }
\cs_new:Npn \slides@t@rf #1
  { \str_case_e:nn { \tl_use:c { slides@t@ #1 @align } } { { center } { \hss } { left } { \hss } } }
% markers, told apart by meaning
\cs_new_protected:Npn \__slides_t_row_mark: { \msg_error:nnn { slides } { misplaced } { row } }
\cs_new_protected:Npn \__slides_t_cell_mark: { \msg_error:nnn { slides } { misplaced } { cell } }
\cs_new_protected:Npn \__slides_t_multi_mark: { \msg_error:nnn { slides } { misplaced } { multicell } }
\msg_new:nnn { slides } { misplaced } { \iow_char:N \\#1~belongs~in~a~slidetable. }
\NewDocumentCommand \__slides_t_hborder:w { m o m } { \__slides_t_border:nnnn { h } {#1} {#2} {#3} }
\NewDocumentCommand \__slides_t_vborder:w { m o m } { \__slides_t_border:nnnn { v } {#1} {#2} {#3} }
\cs_new_protected:Npn \__slides_t_border:nnnn #1#2#3#4
  {
    \IfNoValueTF {#3}
      {
        \int_set:Nn \l__slides_t_a_int { 0 }
        \int_set:Nn \l__slides_t_len_int
          { \str_if_eq:nnTF {#1} { h } { \l__slides_t_m_int } { \g__slides_t_r_int } - 1 }
      }
      {
        \seq_set_split:Nnn \l__slides_t_styles_seq { - } {#3}
        \int_set:Nn \l__slides_t_a_int { \seq_item:Nn \l__slides_t_styles_seq { 1 } }
        \int_set:Nn \l__slides_t_len_int { \seq_item:Nn \l__slides_t_styles_seq { -1 } }
      }
    \int_step_inline:nnn { \l__slides_t_a_int } { \l__slides_t_len_int }
      { \prop_gput:Nnn \g__slides_t_seg_prop { #1/#2/##1 } {#4} }
  }
\NewDocumentEnvironment { slidetable } { O{} m m +b }
  { \__slides_t_table:nnnn {#1} {#2} {#3} {#4} } { }
\cs_new_protected:Npn \__slides_t_table:nnnn #1#2#3#4
  {
    \group_begin:
    \cs_set_eq:NN \row \__slides_t_row_mark:
    \cs_set_eq:NN \cell \__slides_t_cell_mark:
    \cs_set_eq:NN \multicell \__slides_t_multi_mark:
    \cs_set_eq:NN \hborder \__slides_t_hborder:w
    \cs_set_eq:NN \vborder \__slides_t_vborder:w
    \keys_set:nn { slides/table }
      {
        inset = 0 , border = none , h = 0 , grow , fill = none , fill~opacity = , valign = top ,
        align = left , aligns = , style = , pitch = , baseline = , lang = , word , rows = 1
      }
    \keys_set:nn { slides/table } {#1}
    % the insets to the hundredth where they make lengths, as the tikz grid this replaces wrote them
    \tl_set:Ne \slides@t@ix { \fp_eval:n { round ( \l__slides_t_ix_tl , 2 ) } }
    \tl_set:Ne \slides@t@ixx { \fp_eval:n { round ( 2 * ( \l__slides_t_ix_tl ) , 2 ) } }
    \tl_set:Ne \slides@t@iy { \fp_eval:n { round ( \l__slides_t_iy_tl , 2 ) } }
    \clist_set:Nn \l__slides_t_xy_clist {#2}
    \seq_set_from_clist:Nn \l__slides_t_w_seq {#3}
    \int_set:Nn \l__slides_t_m_int { \seq_count:N \l__slides_t_w_seq }
    \tl_set:Nn \l__slides_t_x_tl { 0 }
    \tl_set:cn { slides@t@x@0 } { 0 }
    \tl_set:cn { slides@t@X@0 } { 0 }
    \int_zero:N \l__slides_t_a_int
    \seq_map_inline:Nn \l__slides_t_w_seq
      {
        \int_incr:N \l__slides_t_a_int
        \tl_set:Ne \l__slides_t_x_tl { \fp_eval:n { \l__slides_t_x_tl + ##1 } }
        \tl_set_eq:cN { slides@t@x@ \int_use:N \l__slides_t_a_int } \l__slides_t_x_tl
        % where lines and fills go: to a tenth of a point, as the tikz grid this replaces had them
        \tl_set:ce { slides@t@X@ \int_use:N \l__slides_t_a_int } { \fp_eval:n { round ( \l__slides_t_x_tl , 1 ) } }
      }
    \int_gzero:N \g__slides_t_r_int
    \int_gzero:N \g__slides_t_n_int
    \int_gset:Nn \g__slides_t_rs_int { 1 }
    \prop_gclear:N \g__slides_t_occ_prop
    \prop_gclear:N \g__slides_t_seg_prop
    \prop_gclear:N \g__slides_t_in_prop
    \seq_set_split_keep_spaces:Nnn \l__slides_t_rows_seq { \\ } {#4}
    \seq_pop_right:NN \l__slides_t_rows_seq \l__slides_t_tail_tl
    \seq_map_function:NN \l__slides_t_rows_seq \__slides_t_row:n
    \tl_trim_spaces:N \l__slides_t_tail_tl
    \tl_if_blank:VF \l__slides_t_tail_tl
      {
        \bool_lazy_or:nnTF
          { \exp_args:NV \tl_if_head_eq_meaning_p:nN \l__slides_t_tail_tl \hborder }
          { \exp_args:NV \tl_if_head_eq_meaning_p:nN \l__slides_t_tail_tl \vborder }
          { \tl_use:N \l__slides_t_tail_tl }
          { \exp_args:NV \__slides_t_row:n \l__slides_t_tail_tl }
      }
    \int_step_inline:nn { \g__slides_t_n_int } { \__slides_t_inside:n {##1} }
    \use:e
      {
        \exp_not:N \begin { textblock* } { \tl_use:c { slides@t@X@ \int_use:N \l__slides_t_m_int } bp }
          ( \clist_item:Nn \l__slides_t_xy_clist { 1 } bp , \clist_item:Nn \l__slides_t_xy_clist { 2 } bp )
      }
    % rows holding one cell grow first, so a merged cell only adds what they left it short of
    \int_step_inline:nn { \g__slides_t_rs_int }
      { \int_step_inline:nn { \g__slides_t_n_int } { \__slides_t_grow:nn {##1} {####1} } }
    \slides@t@tops { \int_use:N \g__slides_t_r_int }
    \use:e
      {
        \exp_not:N \slides@t@picture { \tl_use:c { slides@t@X@ \int_use:N \l__slides_t_m_int } }
          { \int_use:N \g__slides_t_r_int }
      }
    \int_step_inline:nn { \g__slides_t_n_int } { \__slides_t_fill:n {##1} }
    \int_step_inline:nnn { 0 } { \g__slides_t_r_int } { \__slides_t_line:nn { h } {##1} }
    \int_step_inline:nnn { 0 } { \l__slides_t_m_int } { \__slides_t_line:nn { v } {##1} }
    \int_step_inline:nn { \g__slides_t_n_int } { \__slides_t_node:n {##1} }
    \end{tikzpicture}
    \end{textblock*}
    \group_end:
  }
\cs_new_protected:Npn \__slides_t_row:n #1
  {
    \group_begin:
      \tl_set:Nn \l__slides_t_row_tl {#1}
      \tl_trim_spaces:N \l__slides_t_row_tl
      \exp_args:NV \tl_if_head_eq_meaning:nNT \l__slides_t_row_tl \row
        { \exp_after:wN \__slides_t_rowopts:w \l__slides_t_row_tl \q_stop }
      \tl_gset:ce { slides@t@h@ \int_use:N \g__slides_t_r_int } { \dim_eval:n { \l__slides_t_h_tl bp } }
      \tl_gset:ce { slides@t@fix@ \int_use:N \g__slides_t_r_int } { \bool_if:NTF \l__slides_t_fixed_bool { 1 } { 0 } }
      \int_gzero:N \g__slides_t_c_int
      \seq_set_split_keep_spaces:NnV \l__slides_t_cells_seq { & } \l__slides_t_row_tl
      \seq_map_function:NN \l__slides_t_cells_seq \__slides_t_cell:n
    \group_end:
    \int_gincr:N \g__slides_t_r_int
  }
\cs_new_protected:Npn \__slides_t_rowopts:w \row [#1] #2 \q_stop
  { \keys_set:nn { slides/table } {#1} \tl_set:Nn \l__slides_t_row_tl {#2} }
\cs_new_protected:Npn \__slides_t_cell:n #1
  {
    \group_begin:
      \tl_set:Nn \l__slides_t_body_tl {#1}
      \tl_trim_spaces:N \l__slides_t_body_tl
      \int_set:Nn \l__slides_t_span_int { 1 }
      \__slides_t_skip:
      % the column's alignment, then what the cell says
      \tl_set:Ne \l__slides_t_now_tl
        { \clist_item:Nn \l__slides_t_aligns_clist { \g__slides_t_c_int + 1 } }
      \tl_if_blank:VF \l__slides_t_now_tl
        { \use:e { \keys_set:nn { slides/table } { align = \l__slides_t_now_tl } } }
      \exp_args:NV \tl_if_head_eq_meaning:nNTF \l__slides_t_body_tl \cell
        { \exp_after:wN \__slides_t_cellopts:w \l__slides_t_body_tl \q_mark \q_stop }
        {
          \exp_args:NV \tl_if_head_eq_meaning:nNT \l__slides_t_body_tl \multicell
            { \exp_after:wN \__slides_t_multi:w \l__slides_t_body_tl \q_mark \q_stop }
        }
      \int_gincr:N \g__slides_t_n_int
      \tl_set:Ne \l__slides_t_k_tl { \int_use:N \g__slides_t_n_int }
      \__slides_t_put:ne { row } { \int_use:N \g__slides_t_r_int }
      \__slides_t_put:ne { col } { \int_use:N \g__slides_t_c_int }
      \__slides_t_put:ne { span } { \int_use:N \l__slides_t_span_int }
      \__slides_t_put:ne { rows } { \int_use:N \l__slides_t_rs_int }
      \__slides_t_put:nV { fill } \l__slides_t_fill_tl
      \__slides_t_put:nV { op } \l__slides_t_op_tl
      \__slides_t_put:nV { valign } \l__slides_t_valign_tl
      \__slides_t_put:nV { align } \l__slides_t_align_tl
      \__slides_t_put:nV { style } \l__slides_t_style_tl
      \__slides_t_put:nV { pitch } \l__slides_t_pitch_tl
      \__slides_t_put:nV { base } \l__slides_t_base_tl
      \__slides_t_put:nV { lang } \l__slides_t_lang_tl
      \__slides_t_put:ne { word }
        { \bool_lazy_and:nnTF { \l__slides_t_word_bool } { \tl_if_empty_p:N \l__slides_t_lang_tl } { 1 } { 0 } }
      \__slides_t_put:nV { body } \l__slides_t_body_tl
      \__slides_t_put:ne { boxed } { \tl_if_blank:VTF \l__slides_t_body_tl { 0 } { 1 } }
      \int_compare:nNnT { \l__slides_t_rs_int } > { \g__slides_t_rs_int }
        { \int_gset_eq:NN \g__slides_t_rs_int \l__slides_t_rs_int }
      \int_step_inline:nnn { \g__slides_t_r_int + 1 } { \g__slides_t_r_int + \l__slides_t_rs_int - 1 }
        {
          \int_step_inline:nnn { \g__slides_t_c_int } { \g__slides_t_c_int + \l__slides_t_span_int - 1 }
            { \prop_gput:Nnn \g__slides_t_occ_prop { ##1/####1 } { } }
        }
      \int_gadd:Nn \g__slides_t_c_int { \l__slides_t_span_int }
    \group_end:
  }
\cs_new_protected:Npn \__slides_t_put:nn #1#2 { \tl_gset:cn { slides@t@ \l__slides_t_k_tl @ #1 } {#2} }
\cs_generate_variant:Nn \__slides_t_put:nn { ne , nV }
\cs_new_protected:Npn \__slides_t_skip:
  {
    \exp_args:NNe \prop_if_in:NnT \g__slides_t_occ_prop
      { \int_use:N \g__slides_t_r_int / \int_use:N \g__slides_t_c_int }
      { \int_gincr:N \g__slides_t_c_int \__slides_t_skip: }
  }
\cs_new_protected:Npn \__slides_t_cellopts:w \cell #1 \q_stop { \__slides_t_optbody:n {#1} }
\cs_new_protected:Npn \__slides_t_multi:w \multicell #1 #2 \q_stop
  { \int_set:Nn \l__slides_t_span_int {#1} \__slides_t_optbody:n {#2} }
\cs_new_protected:Npn \__slides_t_optbody:n #1
  { \tl_if_head_eq_charcode:nNTF {#1} [ { \__slides_t_ob:w #1 \q_stop } { \__slides_t_ob:w [] #1 \q_stop } }
\cs_new_protected:Npn \__slides_t_ob:w [#1] #2 #3 \q_stop
  {
    \keys_set:nn { slides/table } {#1}
    \tl_set:Nn \l__slides_t_body_tl {#2}
    \tl_trim_spaces:N \l__slides_t_body_tl
  }
% the border segments a merged cell covers
\cs_new_protected:Npn \__slides_t_inside:n #1
  {
    \int_set:Nn \l__slides_t_r_int { \tl_use:c { slides@t@ #1 @row } }
    \int_set:Nn \l__slides_t_c_int { \tl_use:c { slides@t@ #1 @col } }
    \int_set:Nn \l__slides_t_r_one_int { \l__slides_t_r_int + \tl_use:c { slides@t@ #1 @rows } - 1 }
    \int_set:Nn \l__slides_t_c_one_int { \l__slides_t_c_int + \tl_use:c { slides@t@ #1 @span } - 1 }
    \int_step_inline:nnn { \l__slides_t_r_int + 1 } { \l__slides_t_r_one_int }
      {
        \int_step_inline:nnn { \l__slides_t_c_int } { \l__slides_t_c_one_int }
          { \prop_gput:Nnn \g__slides_t_in_prop { h/##1/####1 } { } }
      }
    \int_step_inline:nnn { \l__slides_t_c_int + 1 } { \l__slides_t_c_one_int }
      {
        \int_step_inline:nnn { \l__slides_t_r_int } { \l__slides_t_r_one_int }
          { \prop_gput:Nnn \g__slides_t_in_prop { v/##1/####1 } { } }
      }
  }
% where cell #1 ends: \l__slides_t_r_one_int = its last row + 1, \l__slides_t_c_one_int = last column + 1
\cs_new_protected:Npn \__slides_t_ends:n #1
  {
    \int_set:Nn \l__slides_t_r_int { \tl_use:c { slides@t@ #1 @row } }
    \int_set:Nn \l__slides_t_c_int { \tl_use:c { slides@t@ #1 @col } }
    \int_set:Nn \l__slides_t_r_one_int
      { \int_min:nn { \l__slides_t_r_int + \tl_use:c { slides@t@ #1 @rows } } { \g__slides_t_r_int } }
    \int_set:Nn \l__slides_t_c_one_int
      { \int_min:nn { \l__slides_t_c_int + \tl_use:c { slides@t@ #1 @span } } { \l__slides_t_m_int } }
  }
\cs_new_protected:Npn \__slides_t_grow:nn #1#2
  {
    \bool_lazy_and:nnT
      { \int_compare_p:nNn { \tl_use:c { slides@t@ #2 @rows } } = {#1} }
      { \int_compare_p:nNn { \tl_use:c { slides@t@ #2 @boxed } } = { 1 } }
      {
        \__slides_t_ends:n {#2}
        \tl_set:Ne \l__slides_t_x_tl
          {
            \fp_eval:n
              {
                \tl_use:c { slides@t@x@ \int_use:N \l__slides_t_c_one_int }
                - \tl_use:c { slides@t@x@ \int_use:N \l__slides_t_c_int }
              }
          }
        \use:e
          {
            \exp_not:N \slides@t@grow {#2} { \int_use:N \l__slides_t_r_int }
              { \int_eval:n { \l__slides_t_r_one_int - 1 } }
              { \fp_eval:n { round ( max ( \l__slides_t_x_tl - 2 * ( \l__slides_t_ix_tl ) , 1 ) , 2 ) } bp }
              { \slides@t@iy bp } { \fp_eval:n { round ( \l__slides_t_x_tl , 2 ) } bp }
              {
                \str_case_e:nnF { \tl_use:c { slides@t@ #2 @align } }
                  {
                    { center } { - \slides@t@ix }
                    { right } { - \slides@t@ixx }
                  }
                  { 0 }
                bp
              }
          }
      }
  }
\cs_new_protected:Npn \__slides_t_fill:n #1
  {
    \exp_args:Nv \str_if_eq:nnF { slides@t@ #1 @fill } { none }
      {
        \__slides_t_ends:n {#1}
        \use:e
          {
            \exp_not:N \slides@t@fill
              {
                \exp_not:v { slides@t@ #1 @fill }
                \tl_if_empty:cF { slides@t@ #1 @op } { ,fill~opacity= \exp_not:v { slides@t@ #1 @op } }
              }
              { \tl_use:c { slides@t@X@ \int_use:N \l__slides_t_c_int } } { \int_use:N \l__slides_t_r_int }
              { \tl_use:c { slides@t@X@ \int_use:N \l__slides_t_c_one_int } } { \int_use:N \l__slides_t_r_one_int }
          }
      }
  }
% segment #3 of line #2 (h: above row #2, v: left of column #2): its style in \l__slides_t_seg_tl
\cs_new_protected:Npn \__slides_t_seg:nnn #1#2#3
  {
    \prop_if_in:NnTF \g__slides_t_in_prop { #1/#2/#3 }
      { \tl_set:Nn \l__slides_t_seg_tl { none } }
      {
        \prop_get:NnNF \g__slides_t_seg_prop { #1/#2/#3 } \l__slides_t_seg_tl
          { \tl_set_eq:NN \l__slides_t_seg_tl \l__slides_t_border_tl }
      }
  }
% a line's styles in the order they first appear along it, each drawn in runs of touching segments
\cs_new_protected:Npn \__slides_t_line:nn #1#2
  {
    \int_set:Nn \l__slides_t_len_int { \str_if_eq:nnTF {#1} { h } { \l__slides_t_m_int } { \g__slides_t_r_int } }
    \seq_clear:N \l__slides_t_styles_seq
    \int_step_inline:nnn { 0 } { \l__slides_t_len_int - 1 }
      {
        \__slides_t_seg:nnn {#1} {#2} {##1}
        \str_if_eq:VnF \l__slides_t_seg_tl { none }
          {
            \seq_if_in:NVF \l__slides_t_styles_seq \l__slides_t_seg_tl
              { \seq_put_right:NV \l__slides_t_styles_seq \l__slides_t_seg_tl }
          }
      }
    \seq_map_inline:Nn \l__slides_t_styles_seq { \__slides_t_runs:nnn {#1} {#2} {##1} }
  }
\cs_new_protected:Npn \__slides_t_runs:nnn #1#2#3
  {
    \tl_set:Nn \l__slides_t_now_tl {#3}
    \int_set:Nn \l__slides_t_a_int { -1 }
    \int_step_inline:nnn { 0 } { \l__slides_t_len_int }
      {
        \int_compare:nNnTF {##1} < { \l__slides_t_len_int }
          { \__slides_t_seg:nnn {#1} {#2} {##1} }
          { \tl_set:Nn \l__slides_t_seg_tl { none } }
        \tl_if_eq:NNTF \l__slides_t_seg_tl \l__slides_t_now_tl
          { \int_compare:nNnT { \l__slides_t_a_int } < { 0 } { \int_set:Nn \l__slides_t_a_int {##1} } }
          {
            \int_compare:nNnF { \l__slides_t_a_int } < { 0 }
              {
                \str_if_eq:nnTF {#1} { h }
                  {
                    \use:e
                      {
                        \exp_not:N \slides@t@hdraw { \exp_not:n {#3} }
                          { \tl_use:c { slides@t@X@ \int_use:N \l__slides_t_a_int } }
                          { \tl_use:c { slides@t@X@ ##1 } } {#2}
                      }
                  }
                  {
                    \use:e
                      {
                        \exp_not:N \slides@t@vdraw { \exp_not:n {#3} } { \tl_use:c { slides@t@X@ #2 } }
                          { \int_use:N \l__slides_t_a_int } {##1}
                      }
                  }
                \int_set:Nn \l__slides_t_a_int { -1 }
              }
          }
      }
  }
\cs_new_protected:Npn \__slides_t_node:n #1
  {
    \int_compare:nNnT { \tl_use:c { slides@t@ #1 @boxed } } = { 1 }
      {
        \__slides_t_ends:n {#1}
        \str_case_e:nnF { \tl_use:c { slides@t@ #1 @valign } }
          {
            { middle }
              {
                \tl_set:Nn \l__slides_t_anchor_tl { west,yshift=-\slides@t@drop{#1} }
                \tl_set:Ne \l__slides_t_where_tl
                  {
                    -( \exp_not:N \slides@t@y { \int_use:N \l__slides_t_r_int }
                    + \exp_not:N \slides@t@y { \int_use:N \l__slides_t_r_one_int } )/2
                  }
              }
            { bottom }
              {
                \tl_set:Nn \l__slides_t_anchor_tl { south~west }
                \tl_set:Ne \l__slides_t_where_tl
                  {
                    - \exp_not:N \slides@t@y { \int_use:N \l__slides_t_r_one_int }
                    \tl_if_empty:cTF { slides@t@ #1 @base }
                      { + \slides@t@iy bp }
                      { + \tl_use:c { slides@t@ #1 @base } bp - \exp_not:N \slides@t@dp {#1} }
                  }
              }
          }
          {
            \tl_set:Nn \l__slides_t_anchor_tl { north~west }
            \tl_set:Ne \l__slides_t_where_tl
              {
                - \exp_not:N \slides@t@y { \int_use:N \l__slides_t_r_int }
                \tl_if_empty:cTF { slides@t@ #1 @base }
                  { - \slides@t@iy bp }
                  { - \tl_use:c { slides@t@ #1 @base } bp + \exp_not:N \slides@t@ht {#1} }
              }
          }
        \use:e
          {
            \exp_not:N \slides@t@node { \exp_not:V \l__slides_t_anchor_tl }
              { \fp_eval:n { round ( \tl_use:c { slides@t@x@ \int_use:N \l__slides_t_c_int } + \l__slides_t_ix_tl , 1 ) } }
              { \exp_not:V \l__slides_t_where_tl } {#1}
          }
      }
  }
\ExplSyntaxOff"""

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


def cell_lead(base: dict, cell: dict, ctx: Context) -> str:
    """A table cell's base style with its lines as far apart as Slides sets them (LINE_EM x
    lineSpacing). The size switch alone spaced them by the class's leading: hebrew-lesson's cells
    stood 14.0 pt apart where the thumbnail shows 14.45. Only the distance between lines: the strut
    every cell line carries (`TABLE_MACROS`) stays the size switch's, since a one-line cell is as
    tall as its row already says - a strut of the whole pitch grew comps-analysis's rows past the
    deck's (0.429 -> 0.427), and Slides' text-box ascent (0.968 em) moved its lines down (0.405)."""
    if not base.get("size"):
        return base_lead(base, ctx)
    z = base["size"]
    r = next((p.get("line_spacing") for p in cell.get("paragraphs", []) if p.get("line_spacing")), 1.0)
    pitch = sum(line_box(z, r))
    return base_lead(base, ctx) + f"\\baselineskip={pitch:.2f}pt\\relax"


def cell_line_place(cell: dict, text_y: float) -> float | None:
    """How far from its row's top (bottom) a top- (bottom-) aligned cell's first (last) baseline
    stands: the table's measured text inset (`deck_thumbs.thumbnail_cell_text`) plus the ascent
    (descent) of that line's Slides line box. None for a cell with no text or another alignment."""
    paras = [p for p in cell.get("paragraphs", []) if p.get("runs")]
    if not paras or cell.get("valign") not in ("top", "bottom"):
        return None
    p = paras[0] if cell.get("valign") == "top" else paras[-1]
    z = max((r.get("size") or 0) for r in p["runs"]) or p.get("size") or 0
    if not z:
        return None
    above, below = line_box(z, p.get("line_spacing") or 1.0)
    return text_y + (above if cell.get("valign") == "top" else below)


# What a `slidetable` cell option means when nobody writes it (`TABLE_MACROS`).
CELL_DEFAULTS = {"fill": "none", "op": "", "valign": "top", "align": "left", "style": "", "pitch": "",
                 "baseline": "", "word": True}
CELL_KEYS = {"fill": "fill", "op": "fill opacity", "valign": "valign", "align": "align", "style": "style",
             "pitch": "pitch", "baseline": "baseline"}
# the macros a run of text sets in a box of its own, where a space is no place to break a line
UNBREAKABLE_RUN = ("underline", "script")


def breakable(p: dict) -> bool:
    """Whether a paragraph has a space TeX may break its line at (`inverse.runs_latex`: a run's
    spaces inside an underline or a script are in a box; those around its words are not)."""
    text = "".join(r["text"] for r in p["runs"] if not r.get("hole"))
    start, end = len(text) - len(text.lstrip()), len(text.rstrip())
    if "\x0b" in text[start:end]:
        return True                        # a soft break: two lines already
    at = 0
    for r in p["runs"]:
        if r.get("hole"):
            continue
        t = r["text"]
        core = (len(t) - len(t.lstrip(" ")), len(t.rstrip(" ")))
        boxed = any(r.get(k) for k in UNBREAKABLE_RUN)
        for i, ch in enumerate(t):
            if ch in " \t" and start <= at + i < end and not (boxed and core[0] <= i < core[1]):
                return True
        at += len(t)
    return False


def table_cell(c: dict, ctx: Context, ind: str, text_y) -> tuple[dict, str]:
    """A cell's options (None: whatever its row says) and its LaTeX, in `slidetable`'s terms."""
    from .scripts import block_rtl, rtl_language
    opts: dict = {k: None for k in CELL_DEFAULTS}
    opts["fill"] = colour_name(c["fill"], ctx.colours) if c.get("fill") else "none"
    if c.get("fill") and (c.get("fill_alpha") or 1) < 1:
        opts["op"] = num(c["fill_alpha"])
    elif c.get("fill"):
        opts["op"] = ""
    paras = c.get("paragraphs") or []
    if not paras:
        return opts, ""
    base = element_style(c)
    body = paragraphs_latex(paras, lambda _p, b=base: b, ctx, ind)
    p0 = paras[0]
    a0 = p0.get("align")
    opts["valign"] = c.get("valign") if c.get("valign") in ("middle", "bottom") else "top"
    opts["style"] = base_lead(base, ctx)
    opts["pitch"] = ""
    if base.get("size"):
        r = next((p.get("line_spacing") for p in paras if p.get("line_spacing")), 1.0)
        opts["pitch"] = num(sum(line_box(base["size"], r)))
    place = cell_line_place(c, text_y) if text_y is not None else None
    opts["baseline"] = None if opts["valign"] == "middle" else ("" if place is None else num(place))
    # the insets a cell lets go of keep its first paragraph's alignment (`\slides@t@grow`)
    opts["align"] = a0 if a0 in ("center", "right") else "left"
    wrapper = f"{ind}\\begin{{otherlanguage}}{{"
    rtl_switch = {"center": "\\centering ", "left": "\\raggedleft ", "right": "\\raggedright "}.get(a0)
    lang = ""
    if block_rtl(paras) and not p0.get("bullet") and rtl_switch and body.startswith(wrapper):
        # a right-to-left cell: the language and the alignment are options, the words its text
        lang = rtl_language({"runs": [r for p in paras for r in p.get("runs", [])]})
        inner = body.split("\n", 1)[1].rsplit("\\par\n", 1)[0]
        if inner.startswith(ind + rtl_switch):
            body, opts["align"] = ind + inner[len(ind + rtl_switch):], a0
        else:
            lang = ""
    opts["lang"] = lang
    if not lang:
        switch = {"center": "\\centering ", "right": "\\raggedleft "}.get(opts["align"], "")
        if switch and body.startswith(ind + switch):
            body = ind + body[len(ind + switch):]
        elif switch:
            # the option aligns the first paragraph, which is a list or right to left: as it was
            body = ind + "\\raggedright " + body.lstrip(" ")
    if not body.strip():
        body = ind + "{}"                  # a box nonetheless: an empty one still holds its insets
    words = "".join(r["text"] for r in p0["runs"]).strip()
    real = [p for p in paras if any(r.get("text") and not r.get("hole") for r in p["runs"])]
    if len(paras) == 1 and not p0.get("bullet") and not p0.get("direction") \
            and words and not any(ch.isspace() for ch in words):
        opts["word"] = True                # one word: TeX checks whether it fits (`\slides@t@check`)
    elif lang or len(real) >= 2 or (not p0.get("bullet") and not p0.get("direction") and breakable(p0)):
        opts["word"] = None                # TeX would find no one word too wide here, either way
    else:
        opts["word"] = False
    return opts, body


def cascade(rows: list[list[dict]], key: str) -> tuple:
    """The table's value of a cell option and each row's where it differs: the most common one,
    the option's default on a tie (None where no cell cares)."""
    def pick(values):
        counts: dict = {}
        for v in values:
            if v is not None:
                counts[v] = counts.get(v, 0) + 1
        if not counts:
            return None
        return max(counts, key=lambda v: (counts[v], v == CELL_DEFAULTS.get(key)))
    table = pick(v[key] for row in rows for v in row)
    if table is None:
        table = CELL_DEFAULTS.get(key)
    per_row = []
    for row in rows:
        here = pick(v[key] for v in row)
        per_row.append(here if here is not None and here != table else None)
    return table, per_row


def option_text(key: str, value) -> str:
    if key == "word":
        return "word" if value else "wrap"
    if key == "style":
        return f"style={{{value}}}"
    return f"{CELL_KEYS[key]}={value}"


def border_style(b: dict, ctx: Context) -> str:
    """A border segment as TikZ options (`\\hborder`, `border=`)."""
    opts = [colour_name(b["color"] or "#000000", ctx.colours), f"line width={num(b['weight'])}pt"]
    if b["dash"] in DASHES:
        opts += [DASHES[b["dash"]], "line cap=butt"]
    if b["alpha"] < 1:
        opts.append(f"draw opacity={num(b['alpha'])}")
    return ",".join(opts)


def table_borders(el: dict, ctx: Context) -> tuple[str, list[str]]:
    """The table's border style and the `\\hborder` / `\\vborder` lines that say where the lines
    differ from it. A segment inside a merged cell is no border whatever the deck lists there
    (`table_segments`), so it goes with whichever style its neighbours have."""
    n_rows, n_cols = len(el["row_heights"]), len(el["col_widths"])
    inside = set()
    for c in el.get("table_cells", []):
        for r in range(c["row"] + 1, c["row"] + c["rowspan"]):
            inside.update(("h", r, k) for k in range(c["col"], c["col"] + c["colspan"]))
        for k in range(c["col"] + 1, c["col"] + c["colspan"]):
            inside.update(("v", k, r) for r in range(c["row"], c["row"] + c["rowspan"]))
    styles = {}
    for b in el.get("table_borders", []):
        line, at = (b["row"], b["col"]) if b["dir"] == "h" else (b["col"], b["row"])
        styles[(b["dir"], line, at)] = border_style(b, ctx)
    grid = [("h", i, n_cols) for i in range(n_rows + 1)] + [("v", j, n_rows) for j in range(n_cols + 1)]
    seg = {(d, i, a): (None if (d, i, a) in inside else styles.get((d, i, a), "none"))
           for d, i, n in grid for a in range(n)}

    def common(values, prefer):
        counts: dict = {}
        for v in values:
            if v is not None:
                counts[v] = counts.get(v, 0) + 1
        return max(counts, key=lambda v: (counts[v], v == prefer)) if counts else prefer
    default = common(seg.values(), "none")
    out = []
    for d, i, n in grid:
        values = [seg[(d, i, a)] for a in range(n)]
        here = common(values, default)
        cmd = "\\hborder" if d == "h" else "\\vborder"
        if here != default:
            out.append(f"{cmd}{{{i}}}{{{here}}}")
        # runs of one style other than the line's, a merged cell's segments joining either side
        a = 0
        while a < n:
            v = values[a]
            if v is None or v == here:
                a += 1
                continue
            b = a + 1
            while b < n and values[b] in (v, None):
                b += 1
            end = b
            while values[end - 1] is None:
                end -= 1
            out.append(f"{cmd}{{{i}}}[{a}]{{{v}}}" if end == a + 1 else f"{cmd}{{{i}}}[{a}-{end - 1}]{{{v}}}")
            a = b
    return default, out


def table_places(widths: list[float], padx: float, pady: float) -> tuple[str, str, list]:
    """The insets and column edges a `slidetable` is written with, to the thousandth as the deck
    reads them (`deck_ir`), each width then the step between two edges so that the edges TeX adds
    up are the deck's however many columns come before.

    TeX rounds them where they make lengths (`TABLE_MACROS`: edges and text to the tenth, insets to
    the hundredth, as the tikz grid of commands this replaces wrote them), and l3fp rounds a decimal
    tie to even where Python's format rounded the binary number nearest to it: 63.15 went down, it
    goes up. A thousandth either way settles such a tie as it was settled before, so the lines and
    the words land on the same pixels."""
    from decimal import Decimal, ROUND_HALF_EVEN

    def tex(d, n):
        return d.quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_EVEN)

    def near(value, *oks):
        # the first test any nearby value passes wins: the old roundings may not all agree on one
        d = Decimal(f"{value:.3f}")
        for ok in oks:
            for step in (0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5, -6, 6, -7, 7, -8, 8, -9, 9):
                cand = d + Decimal(step).scaleb(-3)
                if ok(cand):
                    return cand
        return d
    ix = near(padx, lambda d: tex(d, 2) == Decimal(f"{padx:.2f}") and tex(2 * d, 2) == Decimal(f"{2 * padx:.2f}"))
    iy = near(pady, lambda d: tex(d, 2) == Decimal(f"{pady:.2f}"))
    xs, ats = [Decimal(0)], [0.0]
    for w in widths:
        ats.append(ats[-1] + w)
        a, span = ats[-1], ats[-1] - ats[-2]

        def edge(d, a=a):
            # the edge and the text's place from it: what lines and left-aligned words show
            return tex(d, 1) == Decimal(f"{a:.1f}") and tex(d + ix, 1) == Decimal(f"{a + padx:.1f}")

        def ok(d, span=span, left=xs[-1]):
            # and the width of a cell one column wide, which centres and right-aligns its words
            return (edge(d) and tex(d - left, 2) == Decimal(f"{span:.2f}")
                    and tex(max(d - left - 2 * ix, Decimal(1)), 2) == Decimal(f"{max(span - 2 * padx, 1.0):.2f}"))
        xs.append(near(a, ok, edge))
    return num(float(ix), 3), num(float(iy), 3), xs


def table_block(el: dict, ctx: Context, ind: str) -> str:
    """A table at its place and size, written as a `slidetable`: columns once, then rows of cells
    as in a tabular, with what Slides says that a tabular cannot - fills, a row's minimum height,
    Slides' insets, a colour, weight and dash per border segment - as options that name only where a
    row or a cell differs from the table (`TABLE_MACROS` measures and draws it)."""
    widths, heights = el.get("col_widths") or [], el.get("row_heights") or []
    if not widths or not heights:
        return ""
    ctx.packages.add("\\usepackage{tikz}")
    ctx.packages.add(TEXTPOS)
    ctx.packages.add(TABLE_MACROS)
    padx, pady = el.get("cell_pad") or (4.5, 4.5)
    text_y = el.get("cell_text_y")
    n_rows, n_cols = len(heights), len(widths)
    ix, iy, xs = table_places(widths, padx, pady)
    cols = ",".join(num(float(xs[k + 1] - xs[k]), 3) for k in range(n_cols))
    x0, y0 = el["bbox"][0], el["bbox"][1]
    heads = {(c["row"], c["col"]): c for c in el.get("table_cells", []) if c["row"] < n_rows and c["col"] < n_cols}
    covered: set = set()
    grid: list[list[tuple]] = []                 # per row: (cell, colspan, rowspan, options, body)
    cell_ind = ind + "      "
    for r in range(n_rows):
        row, c = [], 0
        while c < n_cols:
            if (r, c) in covered:
                c += 1
                continue
            cell = heads.get((r, c), {"row": r, "col": c, "rowspan": 1, "colspan": 1, "paragraphs": []})
            span = max(1, min(cell["colspan"], n_cols - c))
            rows = max(1, min(cell["rowspan"], n_rows - r))
            if any((r, k) in covered for k in range(c, c + span)):
                span = 1
            covered.update((rr, k) for rr in range(r, r + rows) for k in range(c, c + span))
            opts, body = table_cell(cell, ctx, cell_ind, text_y)
            row.append((c, span, rows, opts, body))
            c += span
        grid.append(row)
    options = [[o for _c, _s, _r, o, _b in row] for row in grid]
    table_opts, row_opts = [], [[] for _ in grid]
    effective = [dict() for _ in grid]
    # alignment goes by column, as a tabular's does: each column's most common, then the cells that
    # differ from their column's
    aligns = []
    for k in range(n_cols):
        counts: dict = {}
        for row in grid:
            for c, _s, _r, o, _b in row:
                if c == k and o["align"] is not None:
                    counts[o["align"]] = counts.get(o["align"], 0) + 1
        aligns.append(max(counts, key=lambda v: (counts[v], v == "left")) if counts else "left")
    if len(set(aligns)) > 1:
        table_opts.append(f"aligns={{{','.join(aligns)}}}")
    elif aligns[0] != "left":
        table_opts.append(f"align={aligns[0]}")
    for r, row in enumerate(grid):
        effective[r]["align"] = {c: aligns[c] for c, *_rest in row}
    for key in CELL_DEFAULTS:
        if key == "align":
            continue
        table, per_row = cascade(options, key)
        if table != CELL_DEFAULTS[key]:
            table_opts.append(option_text(key, table))
        for r, v in enumerate(per_row):
            if v is not None:
                row_opts[r].append(option_text(key, v))
            effective[r][key] = table if v is None else v
    # row heights and the rows the thumbnail measured (`deck_ir.thumbnail_rows`), which keep their
    # height whatever TeX makes of their text
    fixed = set(el.get("rows_fixed", []))
    hs = [num(h) for h in heights]
    h_table = max(hs, key=hs.count)
    fix_table = sum(r in fixed for r in range(n_rows)) * 2 > n_rows
    for r in range(n_rows):
        extra = []
        if hs[r] != h_table:
            extra.append(f"h={hs[r]}")
        if (r in fixed) != fix_table:
            extra.append("fixed" if r in fixed else "grow")
        row_opts[r] = extra + row_opts[r]
    inset = f"inset={ix}" if ix == iy else f"inset x={ix}, inset y={iy}"
    border, border_lines = table_borders(el, ctx)
    head = [inset] + ([f"border={{{border}}}"] if border != "none" else []) + [f"h={h_table}"] \
        + (["fixed"] if fix_table else []) + table_opts
    lines = [f"{ind}\\begin{{slidetable}}[{', '.join(head)}]{{{num(x0, 1)},{num(y0, 1)}}}{{{cols}}}"]
    for r, row in enumerate(grid):
        texts = []
        for c, span, rows, opts, body in row:
            here = {**effective[r], "align": effective[r]["align"][c]}
            own = [option_text(k, opts[k]) for k in CELL_DEFAULTS
                   if opts[k] is not None and opts[k] != here[k]
                   and not (k == "op" and opts["fill"] == "none")]
            if opts.get("lang"):
                own.append(f"lang={opts['lang']}")
            if rows > 1:
                own.append(f"rows={rows}")
            text = body.strip()
            one_line = "\n" not in text and "\\\\" not in text
            if span > 1 or rows > 1:
                texts.append(f"\\multicell{{{span}}}" + (f"[{', '.join(own)}]" if own else "")
                             + (f"{{{text}}}" if one_line else f"{{\n{body}}}"))
            elif own or not one_line or text.startswith("["):
                texts.append("\\cell" + (f"[{', '.join(own)}]" if own else "")
                             + (f"{{{text}}}" if one_line else f"{{\n{body}}}"))
            else:
                texts.append(text)
        lead = f"\\row[{', '.join(row_opts[r])}] " if row_opts[r] else ""
        cells_text = texts[0] + "".join(" &" + (f" {t}" if t else "") for t in texts[1:])
        lines.append(f"{ind}  {lead}{cells_text.strip()} \\\\")
    lines += [f"{ind}  {b}" for b in border_lines]
    lines.append(f"{ind}\\end{{slidetable}}")
    return "\n".join(lines)


def element_latex(el: dict, ctx: Context, tree: Path | None = None, ind: str = "  ") -> str:
    """What one IR element draws: its textblocks, "" when it draws nothing of its own."""
    out = []
    if el.get("role") in ("math", "icon"):
        return ""                                      # part of a text line, not an element of its own
    if el["kind"] == "shape":
        out.append(shape_block(el, ctx, ind, tree).rstrip("\n"))
    elif el["kind"] == "table":
        out.append(table_block(el, ctx, ind))
    elif el["kind"] == "image" and el.get("video"):
        ctx.packages.add(TEXTPOS)
        out.append(video_block(el, ctx, ind, tree).rstrip("\n"))
    elif el["kind"] == "text" and el.get("wordart"):
        out.append(wordart_block(el, ctx, ind).rstrip("\n"))
    elif el["kind"] == "image":
        pic = picture_of(el, tree)
        if pic is not None:
            ctx.packages.add(TEXTPOS)
            out.append(slide_picture(el, pic, ctx, ind).rstrip("\n"))
    elif el["kind"] == "text" and el.get("paragraphs"):
        ctx.packages.add(TEXTPOS)
        # A node of a flow chart is one element: its box, then its label on top.
        out.append(shape_block(el, ctx, ind, tree).rstrip("\n"))
        # A turned text box: its words are written upright in the box it would have if it were
        # not turned, and that is then set turned about its centre (`adopt_shapes.turned_text`).
        upright = {**el, "bbox": el["frame"]["box"]} if el.get("frame") else el
        out.append(turned_text(text_box_latex(upright, ctx, ind), el, ctx))
    return "\n".join(x for x in out if x.strip())


def slide_latex(s: dict, style_for, ctx: Context, flow: bool, tree: Path | None = None,
                deck_bg: str | None = None, pieces: list[str] | None = None, plan=None) -> str:
    """One deck slide as a frame. `flow` writes the readable version (`inverse.frame_latex`: a frame
    title and body text in the flow); otherwise every element keeps its own place.

    `pieces`: each element's `element_latex`, when the caller has them already. `plan`: what the
    recovered theme draws for this slide (`adopt_theme.FramePlan`): its layout's decoration, title,
    subtitle and number are left out of the frame, which names the layout instead."""
    if flow:
        return frame_latex(s, style_for, ctx)
    if pieces is None:
        pieces = [element_latex(el, ctx, tree) for el in s["elements"]]
    out = ["\\begin{frame}" + (plan.options() if plan else "[plain]")]
    if plan:
        out += plan.header()
    # The deck lists a page's elements in z-order, and a textblock written later is drawn on top:
    # in reading order a block's body panel, starting 2 pt under its title, was painted over it.
    for k, piece in enumerate(pieces):
        if piece and not (plan and k in plan.drawn):
            out.append(piece)
    if s.get("notes"):
        from .inverse import latex_escape
        out.append("  \\note{" + "\n\n".join(latex_escape(p) for p in s["notes"].split("\n") if p.strip()) + "}")
    out.append("\\end{frame}")
    text = "\n".join(x for x in out if x.strip()) + "\n"
    if plan:
        return text                    # the page's background is the frame's `layout`/`background`/`backdrop`
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
    opt, paper = page_setup(target["slides"][0].get("size") if target["slides"] else target.get("page_size"))
    from .scripts import script_preamble
    fonts = getattr(ctx, "font_lines", None) or font_preamble(target, tree, ctx)
    lines = [f"\\documentclass[{opt}]{{beamer}}" if opt else "\\documentclass{beamer}",
             *([paper] if paper else []),
             # A deck is mostly pictures, and LuaTeX re-encodes every PNG with transparency at zlib
             # level 9: devfest2020's pass took 14.1 s, 8.4 s at level 1 (PDF 9.0 -> 12.7 MB).
             "\\pdfvariable compresslevel=1",
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


def text_elements(node):
    """Every element of a deck (or slide, or group) that holds paragraphs, groups and tables' cells
    looked into."""
    if isinstance(node, dict):
        if isinstance(node.get("paragraphs"), list):
            yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from text_elements(v)
    elif isinstance(node, list):
        for v in node:
            yield from text_elements(v)


def deck_text_defaults(target: dict, ctx: Context) -> None:
    """What most of a deck's text is (its size, its colour, its boxes' inset), so the names of text
    styles are relative to it and the usual inset goes unsaid (`\\setslideinset`)."""
    sizes: dict = {}
    colours: dict = {}
    insets: dict = {}
    for el in text_elements(target.get("slides") or []):
        for p in el["paragraphs"]:
            if not p.get("runs"):
                continue
            n = sum(len(r.get("text") or "") for r in p["runs"])
            base = paragraph_base(p)
            k = float(f"{base['size']:.2f}")
            sizes[k] = sizes.get(k, 0) + n
            if base["color"]:
                colours[base["color"]] = colours.get(base["color"], 0) + n
        box = el.get("box") if isinstance(el.get("box"), dict) else None
        if el.get("bbox") and box is not None and (box.get("valign") or "top") != "middle":
            k = f"{box_insets(el)[1]:.2f}"
            insets[k] = insets.get(k, 0) + 1
    ctx.body_size = max(sizes, key=sizes.get) if sizes else None
    ctx.main_colour = max(colours, key=colours.get) if colours else None
    ctx.slide_inset = max(insets, key=insets.get) if insets else None


def rename_colours(text: str, colours: dict[str, str]) -> str:
    """The converter's `b2sRRGGBB` colours under names a person would give them (`Blue`, `DarkGrey`,
    `Blue2` for a second blue), the most used colour of a name taking it bare."""
    found = re.findall(r"(?<![A-Za-z0-9_])b2s([0-9A-F]{6})(?![A-Za-z0-9])", text)
    uses: dict = {}
    for h in found:
        uses[h] = uses.get(h, 0) + 1
    names: dict = {}
    taken: set = set()
    for h in sorted(uses, key=lambda h: (-uses[h], h)):
        word, k = colour_word(h), 2
        name = word
        while name.lower() in taken:
            name, k = f"{word}{k}", k + 1
        taken.add(name.lower())
        names[h] = name
    return re.sub(r"(?<![A-Za-z0-9_])b2s([0-9A-F]{6})(?![A-Za-z0-9])", lambda m: names[m.group(1)], text)


THEME_SPLIT = "\n%% b2s: theme file follows\n"

SLIDES_STY_HEAD = r"""%% slides.sty - written by beamer2slides adopt, with main.tex.
%% The vocabulary main.tex's frames are written in: Google Slides' text model (slidebox, \slidepar,
%% \slidestyle), its tab stops, and the helpers of its shapes and tables. Nothing here is specific to
%% this deck; the deck's styles and colours are named in main.tex's preamble.
\NeedsTeXFormat{LaTeX2e}
\ProvidesPackage{slides}
\RequirePackage{keyval}
"""


def split_packages(packages) -> tuple[list[str], list[str]]:
    """(the preamble's `\\usepackage` / `\\usetikzlibrary` lines, the macro blocks for slides.sty)."""
    uses, macros = [], []
    for p in sorted(packages):
        (uses if p.lstrip().startswith(("\\usepackage", "\\usetikzlibrary")) else macros).append(p)
    return uses, macros


def sty_block(block: str) -> str:
    """A macro block as a .sty holds it: no \\makeatletter / \\makeatother, @ being a letter there."""
    lines = [ln for ln in block.split("\n") if ln.strip() not in ("\\makeatletter", "\\makeatother")]
    return "\n".join(lines)


def bootstrap(target: dict, tex: Path, flow: bool = False) -> str:
    """Write `tex` (and return it): a compilable beamer source with a frame per deck slide, and the
    `slides.sty` beside it that its frames' vocabulary comes from."""
    ctx = Context()
    deck_text_defaults(target, ctx)
    style_for = level_style(target)
    tex.parent.mkdir(parents=True, exist_ok=True)
    deck_bg = background_colour(target)
    # the typefaces first: a text box whose letters are in the deck's second face switches to it
    ctx.font_lines = font_preamble(target, tex.parent, ctx)
    if not flow:
        # what most paragraphs and list items are, said once in the preamble
        deck_text_survey(target, ctx)
    # "% slide N" says which deck slide a frame is, for a person reading the source and for tools
    # that compile frames one at a time (devtools.adopt_bench finds the frames that break a build)
    from . import inverse
    inverse.GUARD_UNITS = True
    theme = None
    try:
        if flow:
            frames = [f"% slide {n}\n" + to_bp(slide_latex(s, style_for, ctx, flow, tex.parent, deck_bg))
                      for n, s in enumerate(target["slides"], 1)]
        else:
            pieces = [[to_bp(element_latex(el, ctx, tex.parent)) for el in s["elements"]] for s in target["slides"]]
            theme = recovered_theme(target, pieces, ctx, tex.parent, deck_bg)
            plans = theme[1] if theme else [None] * len(pieces)
            frames = [f"% slide {n}\n" + to_bp(slide_latex(s, style_for, ctx, flow, tex.parent, deck_bg, p, plan))
                      for n, (s, p, plan) in enumerate(zip(target["slides"], pieces, plans), 1)]
    finally:
        inverse.GUARD_UNITS = False
    head = preamble(target, ctx, flow, tex.parent)
    uses, macros = split_packages(ctx.packages)
    extra = list(uses)
    if macros:
        (tex.parent / "slides.sty").write_text(
            SLIDES_STY_HEAD + "\n".join(sty_block(m) for m in macros) + "\n\\endinput\n", encoding="utf-8")
        extra.append("\\usepackage{slides}")
    extra += [f"\\definecolor{{{n}}}{{HTML}}{{{v}}}" for n, v in sorted(ctx.colours.items())]
    styles = style_definitions(ctx)
    if SLIDES_TEXT in ctx.packages and ctx.slide_inset and num(float(ctx.slide_inset)) != "0":
        extra.append(f"\\setslideinset{{{num(float(ctx.slide_inset))}}}")
    if styles:
        extra += ["% the deck's text styles: size (bp), typeface, weight, colour, and the Slides line box of a",
                  "% paragraph in each (ascent above the first baseline, pitch, depth under the last line)"]
        extra += styles
    if SLIDES_TEXT in ctx.packages:
        levels = level_definitions(ctx)
        if levels:
            extra += ["% what a paragraph is unless it says otherwise, and each level of a list's items"]
            extra += levels
    theme_file = None
    if theme:
        # the deck's masters and layouts, said once (`adopt_theme`): after the colours and styles it draws in
        from .adopt_theme import theme_name
        name = theme_name(target)
        theme_file = tex.parent / f"beamertheme{name}.sty"
        extra.append(f"\\usetheme{{{name}}}")
    text = head + "\n" + "\n".join(extra) + "\n\n\\begin{document}\n\n" + "\n".join(frames) + "\n\\end{document}\n"
    if theme_file:
        # one naming for both files: the theme draws in the same colours as the frames
        text, sty = rename_colours(text + THEME_SPLIT + theme[0], ctx.colours).split(THEME_SPLIT)
        theme_file.write_text(sty, encoding="utf-8")
    else:
        text = rename_colours(text, ctx.colours)
    tex.parent.mkdir(parents=True, exist_ok=True)
    tex.write_text(text, encoding="utf-8")
    return text


def recovered_theme(target: dict, pieces: list[list[str]], ctx: Context, tree: Path, deck_bg: str | None):
    """(theme .sty, FramePlan per slide) from `adopt_theme.plan`, or None."""
    import copy
    from . import adopt_theme
    scratch = copy.deepcopy(ctx)          # writing a placeholder again must leave the real context alone

    def picture(file: str) -> str | None:
        pic = picture_of({"file": file, "alt": "background"}, tree)
        if pic is None:
            return None
        ctx.packages.add("\\usepackage{graphicx}")
        return pic.rel

    return adopt_theme.plan(target, pieces, lambda el: to_bp(element_latex(el, scratch, tree)),
                            lambda c: colour_name(c, ctx.colours), picture, deck_bg)


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
