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
SLIDES_TEXT = (
    "\\newif\\ifslidesspace\n"
    "\\AddToHook{selectfont}{\\ifslidesspace\\spaceskip=\\fontdimen2\\font plus\\fontdimen3\\font\\relax\\fi}\n"
    "\\newcommand{\\slidesize}[1]{\\fontsize{#1bp}{#1bp}\\selectfont"
    "\\spaceskip=\\fontdimen2\\font plus\\fontdimen3\\font\\relax}\n"
    "\\newcommand{\\slidesbox}{\\slidesspacetrue\\parindent=0pt\\parskip=0pt\\lineskip=0pt\\lineskiplimit=-\\maxdimen"
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
        outer = f"\\vrule width0pt height{a:.2f}pt depth{b:.2f}pt\\relax "
    elif struts is not None:
        # a paragraph of several sizes: every word carries its own line box (text_box_latex)
        a, b = line_box(r.get("size") or base.get("size") or 10.0, struts)
        strut = f"\\vrule width0pt height{a:.2f}pt depth{b:.2f}pt\\relax "
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
        core = f"{{\\slidesize{{{r['size']:.2f}}}{core}}}"
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
    out.append(runs_tex(segments[-1], base, ctx, brk) if segments[-1] else "")
    return "".join(out)


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
    size, pitch, spacing and indents, bullets drawn where Slides draws them. See the notes above."""
    from .emit import BASELINE_A, PAD_X
    box = el.get("box") or {}
    scale = box.get("scale") or 720 / 453.54
    x0, y0, x1, y1 = el["bbox"]
    # a box with no insets (deck_ir.zero_insets) sets its text against its edges
    pad, inset = (0.0, 0.0) if box.get("insets") == 0 else (PAD_X / scale, BASELINE_A / scale)
    if box.get("inset_y") is not None and box.get("insets") != 0:
        # PowerPoint's own top and bottom insets, which a deck's thumbnails showed (deck_ir.pptx_insets)
        inset = (BASELINE_A - (SLIDES_INSET_Y - box["inset_y"])) / scale
    if box.get("inset_x") is not None and box.get("insets") != 0:
        pad = box["inset_x"] / scale                    # the same deck's side insets
    width, height = max(x1 - x0 - 2 * pad, 1.0), max(y1 - y0, 0.1)
    valign = box.get("valign", "top")
    paras = [p for p in el["paragraphs"] if p["runs"]]
    ctx.packages.add(TEXTPOS)
    ctx.packages.add(SLIDES_TEXT)
    out = [f"{ind}\\begin{{textblock*}}{{{measure(width, paras, scale):.2f}pt}}({x0 + pad:.1f}pt,{y0:.1f}pt)",
           f"{ind}  \\vbox to {height:.1f}pt{{\\slidesbox",
           f"{ind}  " + ("\\vss" if valign in ("middle", "bottom") else f"\\vskip{inset:.2f}pt")]
    prev = None
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
        head = []
        if prev is None:
            # a box that grows to fit its text (SHAPE_AUTOFIT) draws its first line without the first
            # paragraph's spaceAbove: gdg24's body copy says 22 pt and starts 22 pt higher than
            # that, while ds-lecture's bodies (no autofit type) keep their master's 6 pt
            if sl.get("space_above") and not box.get("grows"):
                head.append(f"\\vskip{sl['space_above'] / scale:.2f}pt")
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
                head.append(f"\\prevdepth={pitch - gap - above:.2f}pt\\relax")
            else:
                k = pitch - snapped_line_box(pz, pr, scale, bool(box.get("snap")))[1] - gap - above
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
        lead += font_switch(base.get("font"), ctx)
        weight = series(base, ctx)
        lead += "" if weight == "m" else series_switch(weight)
        lead += "\\itshape" if base["italic"] else ""
        lead += f"\\color{{{colour_name(base['color'], ctx.colours)}}}" if base["color"] else ""
        brk = "\\unskip\\hfil\\break " if justified else "\\unskip\\break "
        blank = not any(x["text"].strip() for x in p["runs"])
        ctx.line_struts = r if mixed else None
        body = "" if blank else runs_tex(p["runs"], base, ctx, brk)
        start = f"\\vrule width0pt height{above:.2f}pt depth0pt\\relax"
        if shift:
            start += f"\\hskip{shift:.2f}pt\\relax"
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
        if "\t" in "".join(x["text"] for x in p["runs"]) and not rtl and align == "left" and not blank \
                and (glyph or not p.get("bullet") and first >= left):
            # a bulleted line's text starts at indentStart (or where the bullet pushed it), and its tabs
            # count from the text edge like any other: creandum-board's "DD/MM/YY XX am<TAB><TAB>Other
            # important date" items stand their second column at 180 pt, not one space after "am"
            pen = max(left, first) if glyph else first
            body = tabbed_tex(p["runs"], base, ctx, brk, pen, TAB_STOP / scale) or body
        ctx.line_struts = None
        # a right-to-left paragraph is set in its language (scripts.py: babel's bidi, shaping)
        lang_in, lang_out = "", ""
        if rtl:
            from .scripts import rtl_language
            lang_in = f"\\begin{{otherlanguage}}{{{rtl_language(p)}}}"
            lang_out = "\\end{otherlanguage}"
        out.append(f"{ind}  " + "".join(head) + lang_in +
                   f"{{\\leftskip={lskip}\\relax\\rightskip={rskip}\\relax\\parfillskip={fill}\\relax"
                   + ("\\lineskiplimit=0pt\\relax" if mixed else ""))
        # the `%`: a bullet's \llap{} ends in a brace, and the line end after it was a word space -
        # every bulleted line of the corpus began one space (5 pt at 18 pt Arial) right of Slides'
        out.append(f"{ind}    \\noindent{lead}{start}" + ("% blank line" if blank else "%"))
        if body:
            out.append(f"{ind}    {body}")
        # the pitch last: \selectfont (in \slidesize) resets \baselineskip, and TeX reads it at \par
        out.append(f"{ind}  \\baselineskip={pitch:.2f}pt\\par}}{lang_out}")
        prev = p
    if prev is not None:
        # The space a wide lineSpacing adds under a line is not under the stack's last one: a middle-
        # aligned box centres the lines without it (sc-dark-modern's quotes at 170% sat 7 pt high,
        # sc-aesthetic-school's 150% numbers 14 pt). At 115% it is there all the same (firebase-jam,
        # apps-edu-zh, ap-bio-stats: measured to the pixel both ways), hence the threshold.
        pr = (prev.get("slides") or {}).get("line_spacing") or 1.0
        last = line_box(para_size(prev), 1.0 if pr >= WIDE_SPACING else pr)[1]
        if not mixed_sizes(prev):                      # else its words' struts already end it
            out.append(f"{ind}  \\vskip\\dimexpr{last:.2f}pt-\\prevdepth\\relax")
        tail = trailing_space(prev, valign, el.get("shape_type")) / scale
        if tail:
            out.append(f"{ind}  \\vskip{tail:.2f}pt")
    out.append(f"{ind}  " + ("\\vss" if valign in ("middle", "top") else f"\\vskip{inset:.2f}pt") + "}")
    out.append(f"{ind}\\end{{textblock*}}")
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
# A line stands in its cell as in a Slides line box, 0.968 em of its 1.2 above the baseline (emit's
# ASCENT_EM), not as in LaTeX's 70/30 strut: a middle-aligned cell's baseline is 0.368 em under the
# middle, where the strut put creandum-board's P&L 1 pt high in every row. So the text is set with the
# strut, which decides how rows grow (a Slides-shaped strut of its own grew comps-analysis' tight rows
# under their underlined headings), and a middle-aligned cell's is drawn 10.67% of the strut lower
# (`\adoptdrop`), where a Slides line box puts it. Top and bottom ones stay: their place depends on the
# vertical inset, which is inferred (`deck_ir.cell_pad`), and comps-analysis' top-aligned cells sat
# within 0.3 pt with the strut and 0.8 pt low with the drop.
#
# Slides never hyphenates, so neither does a cell. (It does break a word between two letters when
# the word is wider than the cell; TeX lets it stick out instead, because a penalty between every
# two letters, tried, also split the numbers of creandum-board's narrow columns where Slides keeps
# them whole: the inset is a guess, and a word that sticks out costs less than a row that grows.)
TABLE_MACROS = r"""\makeatletter
\newcommand\adoptrow[2]{\expandafter\edef\csname adopt@row@#1\endcsname{\the\dimexpr#2\relax}%
  \expandafter\let\csname adopt@fix@#1\endcsname\relax}
\newcommand\adoptfix[1]{\expandafter\def\csname adopt@fix@#1\endcsname{1}}% a row the thumbnail measured
\newcommand\adoptsetcell[2]{\vbox{\hsize=#1\relax\linewidth\hsize\parindent\z@
    \hyphenpenalty\@M\exhyphenpenalty\@M\everypar{\strut}#2\ifhmode\strut\fi
    \xdef\adopt@lastdrop{\the\dimexpr(\ht\strutbox+\dp\strutbox)*1067/10000\relax}}}
\newcommand\adoptcell[8]{% box, first row, last row, text width, vertical inset (both),
  % width with no insets, its shift, content
  \expandafter\ifx\csname adopt@box@#1\endcsname\relax\expandafter\newbox\csname adopt@box@#1\endcsname\fi
  \global\setbox\csname adopt@box@#1\endcsname\adoptsetcell{#4}{#8}%
  \expandafter\adopt@first\csname adopt@box@#1\endcsname
  \expandafter\xdef\csname adopt@drop@#1\endcsname{\adopt@lastdrop}%
  \dimen@\z@\@tempcnta#2\relax
  \loop\advance\dimen@\csname adopt@row@\the\@tempcnta\endcsname\relax
  \ifnum\@tempcnta<#3\relax\advance\@tempcnta\@ne\repeat
  \dimen@ii\dimexpr\ht\csname adopt@box@#1\endcsname+\dp\csname adopt@box@#1\endcsname+#5*2\relax
  % a measured row is what Slides laid out, to the pixel: text that fills it exactly is no sign of
  % text set without insets
  \expandafter\ifx\csname adopt@fix@#3\endcsname\relax\skip@\z@\else\skip@\p@\fi
  \ifdim\dimen@ii>\dimexpr\dimen@+\skip@\relax
    \setbox\@tempboxa\adoptsetcell{#6}{#8}%
    \ifdim\dimexpr\ht\@tempboxa+\dp\@tempboxa\relax<\dimexpr\dimen@ii-#5*2\relax
      \adopt@first\@tempboxa
      \global\setbox\csname adopt@box@#1\endcsname\hbox to #4{\kern#7\box\@tempboxa\hss}%
      \dimen@ii\dimexpr\ht\csname adopt@box@#1\endcsname+\dp\csname adopt@box@#1\endcsname+#5*2\relax
    \fi
  \fi
  \expandafter\ifx\csname adopt@fix@#3\endcsname\relax
    \ifdim\dimen@ii>\dimen@
      \expandafter\edef\csname adopt@row@#3\endcsname{\the\dimexpr\csname adopt@row@#3\endcsname+\dimen@ii-\dimen@\relax}%
    \fi
  \fi
  \expandafter\let\csname adopt@ht@#1\endcsname\adopt@fht
  \expandafter\xdef\csname adopt@dp@#1\endcsname{\the\dp\csname adopt@box@#1\endcsname}}
\newbox\adopt@split
\newcommand\adopt@first[1]{% from a vbox's top to its first baseline (its height reaches its last,
  % and a colour's whatsit on either end keeps \lastbox from counting lines): pieces split off the
  % top until one holds a line
  {\setbox\adopt@split\copy#1\vbadness\@M\vfuzz\maxdimen\splittopskip\z@\splitmaxdepth\maxdimen
   \dimen@\z@\@tempswatrue
   \loop\setbox\z@\vsplit\adopt@split to\z@\setbox\z@\vbox{\unvbox\z@}%
     \ifdim\ht\z@>\z@\@tempswafalse\advance\dimen@\ht\z@\else\advance\dimen@\dp\z@\fi
     \ifvoid\adopt@split\@tempswafalse\fi
   \if@tempswa\repeat
   \xdef\adopt@fht{\the\dimen@}}}
\newcommand\adopttops[1]{% \adopty{k}: the top of row k, and \adopty{#1} the table's foot
  \dimen@\z@\@tempcnta\z@
  \loop\expandafter\edef\csname adopt@y@\the\@tempcnta\endcsname{\the\dimen@}%
  \ifnum\@tempcnta<#1\relax
    \advance\dimen@\csname adopt@row@\the\@tempcnta\endcsname\relax\advance\@tempcnta\@ne\repeat}
\newcommand\adopty[1]{\csname adopt@y@#1\endcsname}
\newcommand\adoptbox[1]{\copy\csname adopt@box@#1\endcsname}
\newcommand\adoptdrop[1]{\csname adopt@drop@#1\endcsname}
\newcommand\adoptht[1]{\csname adopt@ht@#1\endcsname}% the height of a cell's first line
\newcommand\adoptdp[1]{\csname adopt@dp@#1\endcsname}% the depth of its last
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
    text_y = el.get("cell_text_y")
    xs = [0.0]
    for w in widths:
        xs.append(xs[-1] + w)
    n_rows = len(heights)
    x0, y0 = el["bbox"][0], el["bbox"][1]
    lines = [f"{ind}\\begin{{textblock*}}{{{xs[-1]:.1f}pt}}({x0:.1f}pt,{y0:.1f}pt)"]
    lines += [f"{ind}  \\adoptrow{{{r}}}{{{h:.2f}pt}}" for r, h in enumerate(heights)]
    # rows the thumbnail showed (`deck_ir.thumbnail_rows`) keep their height whatever TeX makes of
    # their text
    lines += [f"{ind}  \\adoptfix{{{r}}}" for r in el.get("rows_fixed", []) if r < n_rows]
    cells = [c for c in el.get("table_cells", []) if c["row"] < n_rows and c["col"] < len(widths)]
    boxes: dict[int, int] = {}
    # rows that hold one cell grow first, so a merged cell only adds what they left it short of
    for k, c in sorted(enumerate(cells), key=lambda kc: (kc[1]["rowspan"], kc[0])):
        last_row = min(c["row"] + c["rowspan"], n_rows) - 1
        last_col = min(c["col"] + c["colspan"], len(widths))
        span = xs[last_col] - xs[c["col"]]
        width = max(span - 2 * padx, 1.0)
        if not c["paragraphs"]:
            continue
        # the insets let go of when the text would otherwise need more room than the stored row
        # height gives it (see TABLE_MACROS); the wider box keeps the paragraph's alignment
        shift = {"center": -padx, "right": -2 * padx}.get(c["paragraphs"][0].get("align"), 0.0)
        base = element_style(c)
        body = paragraphs_latex(c["paragraphs"], lambda _p, b=base: b, ctx, ind + "    ")
        one = c["paragraphs"][0]
        words = "".join(r["text"] for r in one["runs"]).strip()
        if len(c["paragraphs"]) == 1 and not one.get("bullet") and not one.get("direction") \
                and words and not any(ch.isspace() for ch in words):
            # One word wider than the room the insets leave is set with no insets at all, in its
            # alignment: creandum-board's P&L puts 25 pt numbers into 33 pt columns (18.6 pt inside
            # the insets); the thumbnail shows the centred ones centred on the cell and a left-aligned
            # one starting 1 pt from the cell's edge, where a paragraph left them all sticking out to
            # the right from the left inset, 4 pt off in every one of its 200 cells.
            from .inverse import runs_latex
            lf, rf = {"center": ("\\hss", "\\hss"), "right": ("\\hss", "")}.get(one.get("align"), ("", "\\hss"))
            word = runs_latex(one["runs"], base, ctx).strip()
            body = (f"{ind}    \\noindent\\hbox to\\linewidth{{\\setbox0\\hbox{{{word}}}"
                    f"\\ifdim\\wd0>\\linewidth\\kern-{padx:.2f}pt\\hbox to\\dimexpr\\linewidth+{2 * padx:.2f}pt"
                    f"{{{lf}\\box0{rf}}}\\kern-{padx:.2f}pt\\else{lf}\\box0{rf}\\fi}}")
        boxes[k] = len(boxes) + 1
        lines.append(f"{ind}  \\adoptcell{{{boxes[k]}}}{{{c['row']}}}{{{last_row}}}{{{width:.2f}pt}}{{{pady:.2f}pt}}"
                     f"{{{span:.2f}pt}}{{{shift:.2f}pt}}{{%")
        lines.append(f"{ind}    \\raggedright{cell_lead(base, c, ctx)}%")
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
        line = cell_line_place(c, text_y) if text_y is not None else None
        if c.get("valign") == "middle":
            where, anchor = f"({x:.1f}pt,{{-(\\adopty{{{c['row']}}}+\\adopty{{{r1}}})/2}})", "west"
        elif line is not None and c.get("valign") == "bottom":
            # the last baseline where the thumbnail shows it (`deck_thumbs.thumbnail_cell_text`)
            where, anchor = f"({x:.1f}pt,{{-\\adopty{{{r1}}}+{line:.2f}pt-\\adoptdp{{{boxes[k]}}}}})", "south west"
        elif line is not None:
            # the first baseline, likewise, whatever the strut made of the box's first line
            where, anchor = f"({x:.1f}pt,{{-\\adopty{{{c['row']}}}-{line:.2f}pt+\\adoptht{{{boxes[k]}}}}})", "north west"
        elif c.get("valign") == "bottom":
            where, anchor = f"({x:.1f}pt,{{-\\adopty{{{r1}}}+{pady:.2f}pt}})", "south west"
        else:
            where, anchor = f"({x:.1f}pt,{{-\\adopty{{{c['row']}}}-{pady:.2f}pt}})", "north west"
        drop = f",yshift=-\\adoptdrop{{{boxes[k]}}}" if c.get("valign") == "middle" else ""
        lines.append(f"{ind}    \\node[anchor={anchor}{drop}] at {where} {{\\adoptbox{{{boxes[k]}}}}};")
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
        elif el["kind"] == "image" and el.get("video"):
            ctx.packages.add(TEXTPOS)
            out.append(video_block(el, ctx, "  ", tree).rstrip("\n"))
        elif el["kind"] == "text" and el.get("wordart"):
            out.append(wordart_block(el, ctx, "  ").rstrip("\n"))
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


def bootstrap(target: dict, tex: Path, flow: bool = False) -> str:
    """Write `tex` (and return it): a compilable beamer source with a frame per deck slide."""
    ctx = Context()
    style_for = level_style(target)
    tex.parent.mkdir(parents=True, exist_ok=True)
    deck_bg = background_colour(target)
    # the typefaces first: a text box whose letters are in the deck's second face switches to it
    ctx.font_lines = font_preamble(target, tex.parent, ctx)
    # "% slide N" says which deck slide a frame is, for a person reading the source and for tools
    # that compile frames one at a time (devtools.adopt_bench finds the frames that break a build)
    from . import inverse
    inverse.GUARD_UNITS = True
    try:
        frames = [f"% slide {n}\n" + to_bp(slide_latex(s, style_for, ctx, flow, tex.parent, deck_bg))
                  for n, s in enumerate(target["slides"], 1)]
    finally:
        inverse.GUARD_UNITS = False
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
