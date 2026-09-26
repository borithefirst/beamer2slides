"""Text in scripts other than Latin, for sources written from a deck (`adopt`).

A foreign deck is written in whatever the person typed: Japanese in Arial, Hebrew in Book Antiqua,
Arabic in Calibri. Slides draws every letter a font lacks from a fallback of its own; TeX draws
nothing ("Missing character: There is no 成 in font arial.ttf"), lays Hebrew and Arabic out left to
right with the letters unjoined, and never breaks a line of Japanese that has no spaces. So:

- **Glyphs** (`script_preamble`): the characters the deck uses are sorted into scripts by their
  Unicode block; each script gets a font that has all of them - the deck's own font for those
  letters when this machine has it and it covers them, else a known fallback - and those fonts go
  into a luaotfload fallback chain that every font of the document carries (`\\defaultfontfeatures`,
  so the deck's own font lines stay what `adopt.font_preamble` writes). A glyph the main font has is
  never taken from the chain. The files are copied into `fonts/` beside the source, like the deck's
  own fonts, so the tree compiles elsewhere.
- **Shaping**: a deck with Hebrew, Arabic or another complex script is set with `Renderer=HarfBuzz`,
  which joins Arabic letters and places marks.
- **Direction**: `babel` with `bidi=basic` orders every mixed run (Latin words and numbers inside
  Arabic, Arabic inside English) by the Unicode algorithm; a right-to-left paragraph is set in an
  `otherlanguage` environment of an RTL language (`paragraphs_direction`), and `layout=lists` puts
  its bullets on the right, as Slides does.
- **Line breaking**: babel's CJK locales (`onchar=ids`, chosen by the characters themselves) break
  between any two ideographs or kana, with the kinsoku rules (no 。 opening a line), as Slides does.

Measured choices and what did not work are in the commit that added this module.
"""

from __future__ import annotations

import os
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# ------------------------------------------------------------------------------------------ scripts

RANGES = [
    ("hebrew", 0x0590, 0x05FF), ("hebrew", 0xFB1D, 0xFB4F),
    ("arabic", 0x0600, 0x06FF), ("arabic", 0x0750, 0x077F), ("arabic", 0x0870, 0x08FF),
    ("arabic", 0xFB50, 0xFDFF), ("arabic", 0xFE70, 0xFEFF),
    ("kana", 0x3040, 0x30FF), ("kana", 0x31F0, 0x31FF), ("kana", 0xFF66, 0xFF9F),
    ("hangul", 0x1100, 0x11FF), ("hangul", 0x3130, 0x318F), ("hangul", 0xAC00, 0xD7AF),
    ("han", 0x2E80, 0x2FDF), ("han", 0x3000, 0x303F), ("han", 0x3190, 0x31EF), ("han", 0x3200, 0x33FF),
    ("han", 0x3400, 0x4DBF), ("han", 0x4E00, 0x9FFF), ("han", 0xF900, 0xFAFF), ("han", 0xFE30, 0xFE4F),
    ("han", 0xFF00, 0xFF65), ("han", 0xFFE0, 0xFFEF), ("han", 0x20000, 0x3FFFF),
]
RTL_SCRIPTS = ("hebrew", "arabic")
# Scripts whose letters change shape with their neighbours or carry marks placed by the font:
# node mode leaves Arabic letters unjoined, so these are set with HarfBuzz.
COMPLEX = ("hebrew", "arabic", "indic")


def script_of(ch: str) -> str | None:
    """The script a character asks a font for, or None for what every Latin font has."""
    o = ord(ch)
    if o < 0x250 or ch.isspace() or 0xE000 <= o <= 0xF8FF or 0xFE00 <= o <= 0xFE0F or o in (0xFEFF, 0xFFFC, 0xFFFD):
        return None
    for name, lo, hi in RANGES:
        if lo <= o <= hi:
            return name
    if 0x0900 <= o <= 0x0DFF:
        return "indic"
    if 0x2000 <= o <= 0x206F:                     # general punctuation: dashes, quotes, bullets
        return None if o <= 0x2027 or 0x2030 <= o <= 0x203A else "other"
    return "other"


def group_of(script: str) -> str:
    """Scripts that must come from one font: kana and ideographs are one Japanese text."""
    return "cjk" if script in ("han", "kana", "hangul") else script


# The ideographs Simplified and Traditional Chinese write differently, among the most frequent ones:
# which of the two a deck is decides the fallback font and babel's locale.
TRADITIONAL = set("們個這說國來時會對學發開關書長門問間見車東體與後過還樣經點無現實電話語資訊網頁練習師課")
SIMPLIFIED = set("们个这说国来时会对学发开关书长门问间见车东体与后过还样经点无现实电话语资讯网页练习师课")


def cjk_language(chars: Counter) -> str:
    """babel's locale for the deck's CJK text, from its characters."""
    kana = sum(n for c, n in chars.items() if script_of(c) == "kana")
    hangul = sum(n for c, n in chars.items() if script_of(c) == "hangul")
    if kana:
        return "japanese"
    if hangul:
        return "korean"
    trad = sum(n for c, n in chars.items() if c in TRADITIONAL)
    simp = sum(n for c, n in chars.items() if c in SIMPLIFIED)
    return "chinese-traditional" if trad > simp else "chinese-simplified"


# Fonts that have a script, most like what Slides draws it with first; the deck's own font for the
# letters comes before these. Windows names, then what Linux and macOS machines carry.
FALLBACKS = {
    "japanese": ["Yu Gothic", "Meiryo", "MS Gothic", "Noto Sans CJK JP", "Noto Sans JP", "Hiragino Sans",
                 "Source Han Sans JP", "IPAexGothic"],
    "chinese-traditional": ["Microsoft JhengHei", "Noto Sans CJK TC", "Noto Sans TC", "PingFang TC",
                            "Microsoft YaHei", "MingLiU", "Yu Gothic"],
    "chinese-simplified": ["Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans SC", "PingFang SC",
                           "SimSun", "Microsoft JhengHei", "Yu Gothic"],
    "korean": ["Malgun Gothic", "Noto Sans CJK KR", "Noto Sans KR", "Apple SD Gothic Neo"],
    "hebrew": ["Arial", "Segoe UI", "Tahoma", "Times New Roman", "Noto Sans Hebrew", "DejaVu Sans",
               "Arial Hebrew"],
    "arabic": ["Arial", "Segoe UI", "Tahoma", "Times New Roman", "Noto Naskh Arabic", "Noto Sans Arabic",
               "DejaVu Sans", "Geeza Pro"],
    "indic": ["Nirmala UI", "Noto Sans Devanagari", "Mangal"],
    "other": ["Segoe UI Symbol", "Cambria Math", "Segoe UI", "Arial", "Noto Sans Symbols 2",
              "Noto Sans Symbols", "Noto Sans Math", "DejaVu Sans", "Symbola", "Apple Symbols"],
}


# -------------------------------------------------------------------------------------------- faces

@dataclass(frozen=True)
class Face:
    path: Path
    index: int              # the face's number in a .ttc collection
    families: tuple         # every family name the font gives itself, flattened
    weight: int
    italic: bool


_FACES: dict[tuple, list[Face]] = {}
_CMAPS: dict[tuple, frozenset] = {}
_SAID = False


def _warn_no_fonttools() -> None:
    """Said once per process, where the library says everything else: into adopt's own log."""
    global _SAID
    if not _SAID:
        _SAID = True
        print("  fontTools is not installed, so no font on this machine can be read: the deck's "
              "symbols and non-Latin letters get no fallback font (pip install fonttools)")


def flatten(name: str) -> str:
    return "".join(c for c in (name or "").lower() if c.isalnum())


def faces() -> list[Face]:
    """Every face in the font folders adopt looks in (`adopt.font_dirs`), by the names it gives
    itself (a family name is what a deck says: "MS PGothic" is face 2 of msgothic.ttc). Read once.

    Nothing on a machine with no fontTools: the fallback chain is then empty and the deck's
    symbols and non-Latin letters are set in whatever the main font has. It is a plain dependency
    (pyproject.toml), so this is the stripped install nobody meant to make - and losing a fallback
    chain is a source of lesser fidelity, while raising here loses the source tree altogether,
    after the minutes of thumbnails adopt has already spent."""
    from .adopt import font_dirs
    dirs = tuple(font_dirs())
    if dirs in _FACES:
        return _FACES[dirs]
    try:
        from fontTools.ttLib import TTFont, TTCollection
    except ImportError:
        _warn_no_fonttools()
        _FACES[dirs] = []
        return []
    out: list[Face] = []
    for folder in dirs:
        files = [f for pat in ("*.tt[fc]", "*.otf", "*.TT[FC]", "*.OTF", "*/*.tt[fc]", "*/*.otf")
                 for f in folder.glob(pat)]
        for f in sorted(set(files)):
            try:
                fonts = TTCollection(f, lazy=True).fonts if f.suffix.lower() == ".ttc" else [TTFont(f, lazy=True)]
            except Exception:                                   # noqa: BLE001 - a broken file is no candidate
                continue
            for i, font in enumerate(fonts):
                try:
                    names = {flatten(str(r)) for r in font["name"].names if r.nameID in (1, 16)}
                    os2 = font["OS/2"] if "OS/2" in font else None
                    out.append(Face(f, i, tuple(sorted(n for n in names if n)),
                                    os2.usWeightClass if os2 else 400,
                                    bool(os2.fsSelection & 1) if os2 else False))
                except Exception:                               # noqa: BLE001
                    continue
            # once, after every face: a collection's faces share one file, and closing the first
            # left the others unreadable - MS PGothic (face 2 of msgothic.ttc) was on no list
            fonts[0].close() if fonts else None
    _FACES[dirs] = out
    return out


def coverage(face: Face) -> frozenset:
    key = (face.path, face.index)
    if key not in _CMAPS:
        try:
            from fontTools.ttLib import TTFont
            font = TTFont(face.path, fontNumber=face.index, lazy=True)
            _CMAPS[key] = frozenset(font.getBestCmap() or {})
            font.close()
        except Exception:                                       # noqa: BLE001
            _CMAPS[key] = frozenset()
    return _CMAPS[key]


def find_face(name: str, bold: bool = False) -> Face | None:
    """The upright face of a family, regular or bold, or None when the machine does not have it."""
    want = flatten(name)
    found = [f for f in faces() if want in f.families and not f.italic]
    if bold:
        found = [f for f in found if f.weight >= 600]
    if not found:
        return None
    return min(found, key=lambda f: (abs(f.weight - (700 if bold else 400)), str(f.path), f.index))


# --------------------------------------------------------------------------------------------- deck

def deck_text(target: dict):
    """(text, font, family) of every run of the deck, tables and nested groups included."""
    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("runs"), list):
                for r in o["runs"]:
                    if isinstance(r, dict) and r.get("text"):
                        yield r["text"], r.get("font") or "", r.get("family") or "sans"
                b = o.get("bullet")
                if isinstance(b, dict) and b.get("text") and b["text"] not in "●○■" and o["runs"]:
                    # a glyph bullet is set in its paragraph's face: supercharge-slides' ➔, which
                    # Alegreya lacks, came out as its .notdef cross with no fallback to draw it
                    r = o["runs"][0]
                    yield b["text"], r.get("font") or "", r.get("family") or "sans"
            for k, v in o.items():
                if k != "runs":
                    yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
    for s in target.get("slides", []):
        yield from walk(s.get("elements", []))


@dataclass
class Plan:
    """What the deck's scripts need."""
    chain: list                 # [(regular Face, bold Face | None)] luaotfload tries for a missing glyph
    cjk: str | None             # babel locale for CJK line breaking
    languages: dict             # RTL babel language -> {"rm"|"sf"|"tt": (Face, bold Face | None)}
    bidi: bool                  # any right-to-left letters or paragraphs at all
    complex: bool               # HarfBuzz for the whole document (a shaped script in the chain)


# babel's language for a script whose letters join or reorder: its runs are set in a font of their
# own (`onchar=ids fonts`), whole, because a glyph-by-glyph fallback shapes each letter on its own -
# measured, Arabic from the fallback chain came out half joined.
SCRIPT_LANGUAGES = {"hebrew": "hebrew", "arabic": "arabic"}
FAMILY_KEYS = {"sans": "sf", "serif": "rm", "mono": "tt"}
FAMILY_FALLBACKS = {"rm": ["Times New Roman"], "tt": ["Courier New"], "sf": []}


def _with_bold(face: Face) -> tuple:
    return face, next((b for fam in face.families for b in [find_face(fam, bold=True)] if b), None)


def _pick(names: list[str], need: set[int]) -> Face | None:
    """The first font of `names` this machine has that covers every character, else the one that
    covers most of them; None when none covers any."""
    candidates = []
    for n in names:
        f = find_face(n)
        if f and f not in candidates:
            candidates.append(f)
    full = next((f for f in candidates if need <= coverage(f)), None)
    best = full or max(candidates, key=lambda f: len(need & coverage(f)), default=None)
    return best if best is not None and need & coverage(best) else None


RENDERER_CJK = {"japanese": "Noto Sans JP", "korean": "Noto Sans KR", "chinese-traditional": "Noto Sans TC",
                "chinese-simplified": "Noto Sans SC"}


def _fetch(name: str) -> bool:
    """Whether google/fonts gave a family not in the font folders before (`adopt.fetching` rules)."""
    from .adopt import fetching
    if not fetching() or find_face(name) is not None:
        return False
    from .fontfetch import fetch_family
    return bool(fetch_family(name))


def plan(target: dict) -> Plan:
    chars: dict[str, Counter] = {}
    fonts: dict[tuple, Counter] = {}                     # (group, family) -> deck font names
    for text, font, family in deck_text(target):
        for ch in text:
            sc = script_of(ch)
            if sc is None:
                continue
            g = group_of(sc)
            chars.setdefault(g, Counter())[ch] += 1
            fonts.setdefault((g, family), Counter())[font] += 1
    rtl_paras = [p for p in paragraphs(target) if p.get("direction") == "rtl"]
    cjk = cjk_language(chars["cjk"]) if "cjk" in chars else None
    chain: list = []
    languages: dict = {lang: {} for lang in sorted({rtl_language(p) for p in rtl_paras})}
    for g in sorted(chars, key=lambda g: -sum(chars[g].values())):
        need = {ord(c) for c in chars[g]}
        deck = Counter()
        for (gg, _fam), c in fonts.items():
            if gg == g:
                deck.update(c)
        if g in SCRIPT_LANGUAGES:
            # a font per family the letters are written in, the deck's own first
            lang = SCRIPT_LANGUAGES[g]
            fams = languages.setdefault(lang, {})
            for family, key in FAMILY_KEYS.items():
                own = [n for n, _ in fonts.get((g, family), Counter()).most_common() if n]
                if key != "sf" and not own:
                    continue
                # the deck's own face may still be on google/fonts only, as CJK's is below: a first
                # run set the showcase's Noto Sans Hebrew and Arabic words in Arial
                if [n for n in own if _fetch(n)]:
                    _FACES.clear()
                face = _pick(own + FAMILY_FALLBACKS[key] + FALLBACKS[g], need)
                if face is not None:
                    fams[key] = _with_bold(face)
            continue
        if g == "cjk" and _fetch(RENDERER_CJK[cjk]):
            _FACES.clear()
        own = [n for n, _ in deck.most_common() if n]
        if g == "cjk":
            # a CJK face Slides does not have draws nothing: its letters come from the renderer's
            # Noto (apps-edu-zh's Microsoft JhengHei, `adopt.slides_lacks_cjk`)
            from .adopt import slides_lacks_cjk
            own = [n for n in own if not slides_lacks_cjk(n)]
        names = own + FALLBACKS.get(cjk if g == "cjk" else g, FALLBACKS["other"])
        if g == "cjk":
            # the face Slides' renderer draws a CJK letter in when the deck's font has none (its
            # thumbnails show Noto's shapes, not Yu Gothic's or Microsoft YaHei's), ahead of the
            # machine's own fallbacks
            names.insert(len(names) - len(FALLBACKS[cjk]), RENDERER_CJK[cjk])
        if g == "other":
            # symbols come one by one from whichever font has each
            for n in names:
                f = find_face(n)
                got = need & coverage(f) if f else set()
                if got and all(f != c for c, _ in chain):
                    chain.append(_with_bold(f))
                    need -= got
                if not need:
                    break
            continue
        face = _pick(names, need)
        if face is not None and all(face != c for c, _ in chain):
            chain.append(_with_bold(face))
    return Plan(chain, cjk, languages, bool(rtl_paras) or any(g in RTL_SCRIPTS for g in chars),
                any(g in COMPLEX and g not in SCRIPT_LANGUAGES for g in chars))


def paragraphs(target: dict):
    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("runs"), list) and "align" in o:
                yield o
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
    for s in target.get("slides", []):
        yield from walk(s.get("elements", []))


def font_file(face: Face, tree: Path | None) -> tuple[str, str]:
    """(folder, file name) of a face as the source names it: copied beside the source when there is
    a tree, like the deck's own fonts, so the tree compiles on another machine."""
    if tree is not None:
        dest = tree / "fonts" / face.path.name
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(face.path, dest)
        return "fonts/", face.path.name
    return face.path.parent.as_posix().rstrip("/") + "/", face.path.name


def font_spec(face: Face, tree: Path | None, mode: str, extra: str = "") -> str:
    """luaotfload's name for a face (`[path](index)`, measured: `[path(index)]` is refused)."""
    folder, name = font_file(face, tree)
    return f"[{folder}{name}]" + (f"({face.index})" if face.index else "") + f":mode={mode};{extra}"


def babelfont_line(lang: str, key: str, regular: Face, bold: Face | None, tree: Path | None) -> str:
    """The font babel sets one language's letters in, with every style named.

    `onchar=ids fonts` sends each Hebrew or Arabic letter to this font whatever the text around it is
    set in, so it is this line, not `adopt.font_preamble`'s, that decides what `\\textbf` and
    `\\textit` draw for those letters - and naming only the upright leaves fontspec looking for no
    other file, so the emphasis is silently gone (`devtools/bold_torture`: hebrew-lesson's Hebrew
    serif drew `\\textit` upright). A style with no file of its own is synthesised from the nearest
    one, as `adopt.fake_faces` does it for the deck's own families. A bold that lives in another
    folder is faked off the upright rather than dropped: one `Path` holds for the whole family."""
    from .adopt import fake_faces
    folder, name = font_file(regular, tree)
    opts = [f"Path={folder}", "Renderer=HarfBuzz"]
    if regular.index:
        opts.append(f"FontIndex={regular.index}")
    files, index = {"UprightFont": name}, {"UprightFont": regular.index}
    if bold is not None:
        bfolder, bname = font_file(bold, tree)
        if bfolder == folder:
            files["BoldFont"], index["BoldFont"] = bname, bold.index
            opts.append(f"BoldFont={bname}")
            if bold.index != regular.index:
                opts.append(f"BoldFeatures={{FontIndex={bold.index}}}")
    # a face of a collection keeps its own index wherever it stands in for a style it is not
    opts += fake_faces(files, files.__getitem__,
                       lambda k: [f"FontIndex={index[k]}"] if index[k] != regular.index else [])
    return f"\\babelfont[{lang}]{{{key}}}[{','.join(opts)}]{{{name}}}"


def language_letters(target: dict, font: str) -> dict[str, dict[str, int]]:
    """The letters of each babel-font language (Hebrew, Arabic) the deck types in `font`."""
    out: dict[str, dict[str, int]] = {}
    for text, f, _fam in deck_text(target):
        if f != font:
            continue
        for ch in text:
            sc = script_of(ch)
            lang = SCRIPT_LANGUAGES.get(group_of(sc)) if sc else None
            if lang:
                seen = out.setdefault(lang, {})
                seen[ch] = seen.get(ch, 0) + 1
    return out


def switch_font_lines(target: dict, tree: Path | None, command: str, fam: str, options: str,
                      name: str, lacking: set[str], plan_: Plan | None = None) -> list[str] | None:
    """The deck's second typeface (`adopt.font_preamble`'s `command`) as a babel family, when the deck
    types letters of a `lacking` language in it that it has no glyphs for: `onchar=ids fonts` only
    swaps the families babel was told about, so Arabic in a `\\newfontfamily` font stayed in it and
    drew as empty boxes (saudi-cats' Montserrat "Shukran | شكراً"). Only the lacking languages get a
    face of their own: arabic-training's Tahoma draws its own Arabic, and sending that to the sans
    family's face cost its slides up to 0.05 ink. None when the plain `\\newfontfamily` does."""
    if os.environ.get("B2S_NO_SCRIPTS") or not lacking:
        return None
    p = plan_ or plan(target)
    key = command.lstrip("\\")
    langs = [babelfont_line(lang, key, *faces, tree) for lang, fams in p.languages.items() if lang in lacking
             for faces in [fams.get(FAMILY_KEYS.get(fam, "sf")) or fams.get("sf")] if faces]
    if not langs:
        return None
    return [f"\\babelfont{{{key}}}[{options}]{{{name}}}", *langs, f"\\newcommand{command}{{\\{key}family}}"]


def script_preamble(target: dict, tree: Path | None) -> list[str]:
    """Preamble lines for the deck's scripts, to go before `adopt.font_preamble`'s font lines
    (`\\defaultfontfeatures` applies to the fonts declared after it). Empty for a Latin deck."""
    if os.environ.get("B2S_NO_SCRIPTS"):
        return []
    p = plan(target)
    lines: list[str] = []
    if p.bidi or p.cjk or p.languages:
        lines.append("\\usepackage[bidi=basic,layout=lists]{babel}" if p.bidi else "\\usepackage{babel}")
        lines.append("\\babelprovide[import,main]{english}")
        for lang, fams in p.languages.items():
            # onchar=ids fonts: the language and its fonts follow the letters, so a Hebrew word in
            # an English line is set in a Hebrew font without the source saying so
            lines.append(f"\\babelprovide[import{',onchar=ids fonts' if fams else ''}]{{{lang}}}")
            lines += [babelfont_line(lang, key, *faces_, tree) for key, faces_ in sorted(fams.items())]
        if p.cjk:
            # onchar=ids: the locale follows the characters, so its line breaking applies to every CJK
            # run without the source having to say which runs are Chinese or Japanese
            lines.append(f"\\babelprovide[import,onchar=ids]{{{p.cjk}}}")
    if not p.chain and not p.complex:
        return lines
    mode = "harf" if p.complex else "node"
    lines.append("\\usepackage{fontspec}")
    feats = ["Renderer=HarfBuzz"] if p.complex else []
    if p.chain:
        # Slides sets Japanese kana and brackets proportionally: `palt` (measured on jruby-ja, 0.469
        # -> 0.478 ink overlap, lines ending where the deck's do), and Chinese too (apps-edu-zh's
        # Traditional Chinese: its full-width ：and 、 are drawn half wide)
        extra = "+palt;" if p.cjk in ("japanese", "chinese-traditional", "chinese-simplified") else ""
        regular = ", ".join(f'"{font_spec(f, tree, mode, extra)}"' for f, _ in p.chain)
        bold = ", ".join(f'"{font_spec(b or f, tree, mode, extra)}"' for f, b in p.chain)
        lines.append(f"\\directlua{{luaotfload.add_fallback(\"b2sscripts\", {{{regular}}})}}")
        lines.append(f"\\directlua{{luaotfload.add_fallback(\"b2sscriptsbold\", {{{bold}}})}}")
        feats += ["RawFeature={fallback=b2sscripts}", "BoldFeatures={RawFeature={fallback=b2sscriptsbold}}"]
    lines.append("\\defaultfontfeatures{" + ",".join(feats) + "}")
    return lines




# ---------------------------------------------------------------------------------------- direction

def is_rtl(p: dict) -> bool:
    return p.get("direction") == "rtl"


def rtl_language(p: dict) -> str:
    """The babel language a right-to-left paragraph is set in: its direction is what matters, the
    letters decide which (a paragraph of Latin only, marked RTL, is still set right to left)."""
    text = "".join(r.get("text", "") for r in p.get("runs", []))
    return "arabic" if any(script_of(c) == "arabic" for c in text) else "hebrew"


def align_switch(p: dict) -> str:
    """The switch that puts a paragraph's lines where the deck has them. `align` is where they sit
    on the page (left/center/right, `deck_ir.text_paragraphs`); LuaTeX's skips are logical, so in a
    right-to-left paragraph `\\raggedright` is flush right - the paragraph's start."""
    a = p.get("align")
    if is_rtl(p):
        return {"center": "\\centering ", "left": "\\raggedleft "}.get(a, "\\raggedright ")
    return {"center": "\\centering ", "right": "\\raggedleft "}.get(a, "")


def paragraph_direction(p: dict, latex: str, block_rtl: bool) -> str:
    """One paragraph's LaTeX in its direction. Inside a block that is right to left as a whole
    (`block_direction`) there is nothing to add; a right-to-left paragraph among left-to-right ones
    gets a language group of its own, ended by `\\par` inside it so its alignment still holds when
    TeX breaks the lines."""
    if not is_rtl(p) or block_rtl:
        return latex
    lang = rtl_language(p)
    return f"\\begin{{otherlanguage}}{{{lang}}}{latex}\\par\\end{{otherlanguage}}"


def block_rtl(paragraphs: list[dict]) -> bool:
    ps = [p for p in paragraphs if p.get("runs")]
    return bool(ps) and all(is_rtl(p) for p in ps)


def block_direction(paragraphs: list[dict], latex: str, ind: str) -> str:
    """A text's LaTeX (paragraphs, lists) in an RTL language when every paragraph is right to left,
    which is how a Hebrew or Arabic deck is written: lists then open on the right too."""
    if not block_rtl(paragraphs):
        return latex
    lang = rtl_language({"runs": [r for p in paragraphs for r in p.get("runs", [])]})
    return f"{ind}\\begin{{otherlanguage}}{{{lang}}}\n{latex}\\par\n{ind}\\end{{otherlanguage}}"
