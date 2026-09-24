"""Type 3 (bitmap) TeX text fonts: which font each one is, and what its characters say.

pdflatex without cm-super embeds the Metafont bitmaps of T1 (EC), T2A (LH) and TS1 (TC) fonts as
Type 3 fonts: no /BaseFont, no /ToUnicode, glyph names /a<code>. PDFium then gives each character
code back as if it were Latin-1 - T1's fi ligature U+001C, its en dash U+0015, its ś U+00B1 (±),
T2A's Cyrillic as Latin-1 letters (Ж as Æ), TS1's euro U+00BF (¿) - and calls every such font
"Type3", so bold, italic, sans and typewriter are all one serif.

What still tells the fonts apart is how far each glyph moves the pen: TeX sets every glyph at its
TFM width, and `calibration/tex_fonts.json` (tools/tex_fonts.py) holds those widths for every EC,
TC and LH font of a TeX installation, with the three encodings' code -> Unicode maps from LaTeX's
own `<enc>enc.def` / `.dfu` files. `identify` matches a font's measured advances against them;
the match names the font (ecbx1095 -> bold serif, lass1095 -> T2A sans) and its encoding.

Slanted faces (ecsl, ecsi, ecso, ecbl, ecst) have exactly their upright twin's widths: the widths
say "roman or slanted roman", and the glyphs' ink (`slanted`) says which.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import statistics
import struct
import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .fonts import font_info

TABLE_PATH = Path(__file__).resolve().parent / "calibration" / "tex_fonts.json"
# Measured advances are the TFM widths to 0.05 % (through PK pixels and PDFium floats, on the
# archive decks); neighbouring design sizes of one face are 0.6-2 % apart, faces 4-11 %.
NO_GLYPH = 0.05       # the error of a code the candidate font has no glyph for
GOOD = 0.01           # a font is identified when its trimmed mean error is below this
TRIM = 0.25           # the worst quarter of a font's codes (always kerned, a stray measurement) left out
SIZE_PRIOR = 0.03     # a design size this close (log ratio) to the size drawn is preferred
# Upright face -> its slanted twin, which has the very same widths (the second two letters of the
# TFM name; the typewriter's italic, ecit, is also ectt's widths).
SLANTED = {"rm": "sl", "ss": "si", "sx": "so", "bx": "bl", "tt": "st", "cc": "sc", "xc": "oc"}
TWINS = set(SLANTED.values()) | {"it"}
SLANTED_OF = {v: k for k, v in SLANTED.items()} | {"it": "tt"}
# Of faces with the same widths, the everyday one: typewriter before its small caps (ectc).
PLAIN_FIRST = ("rm", "ss", "tt", "bx", "sx", "ti", "bi", "cc", "xc")
SANS, MONO = {"ss", "si", "sx", "so"}, {"tt", "st", "it", "tc"}


def family(shape: str) -> str:
    """serif | sans | mono of a TFM name's shape letters (as `fonts.font_info` names them)."""
    return "sans" if shape in SANS else "mono" if shape in MONO else "serif"


@dataclass(frozen=True)
class TexFont:
    name: str           # TFM name: ecbx1095, tcss1095, lass1000
    encoding: str       # T1 | TS1 | T2A
    size: float         # design size, pt
    widths: tuple       # 256 advances, em (0: no glyph)


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, TexFont], dict[str, dict[int, str]]]:
    data = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    raw = zlib.decompress(base64.b64decode(data["tables"]))
    unit = data["unit"]
    tables = [tuple(v / unit for v in struct.unpack_from("<256H", raw, 512 * i)) for i in range(len(raw) // 512)]
    fonts = {name: TexFont(name, f["enc"], f["size"], tables[f["table"]]) for name, f in data["fonts"].items()}
    encodings = {enc: {int(k): v for k, v in m.items()} for enc, m in data["encodings"].items()}
    return fonts, encodings


def fonts() -> dict[str, TexFont]:
    return _load()[0]


def encoding(name: str) -> dict[int, str]:
    return _load()[1][name]


MAX_SAMPLES = 6       # measurements kept per code


OVER = 0.2            # weight of a reported advance's excess over a width (ink reaching past it)


def advances(chars: list) -> dict[int, dict[int, tuple[list[float], float]]]:
    """What each Type 3 font's glyphs measure (em): font_id -> code -> (moves, reported), from the
    page's characters in content order (`pdf.api.Char`; the code is the character PDFium gave
    back). PDFium's advance for a Type 3 glyph (`reported`, the smallest seen) is its loose box,
    which reaches as far as the ink where that is further (every italic letter): never less than
    the advance. The pen's `moves` to the next glyph of the same font on its line are the advance
    plus a kern where the pair has one. A code matches a width when one of them does. Text drawn
    at a slant (a chart's rotated labels) has a box that is no advance (`reported` None): only
    its moves say anything, and a code without one is left out."""
    out: dict[int, dict[int, tuple[list[float], float | None]]] = {}
    moves: dict[tuple[int, int], list[float]] = {}
    reported: dict[tuple[int, int], float | None] = {}
    for a, b in zip(chars, chars[1:] + [None]):
        if a.font != "Type3" or len(a.c) != 1 or ord(a.c) > 0xFF or a.size <= 0 or a.synthetic:
            continue
        key = (a.font_id, ord(a.c))
        ux, uy = a.dir
        adv = a.advance / a.size
        if min(abs(ux), abs(uy)) > 0.01:
            reported[key] = None
            low = 0.1
        else:
            reported[key] = min(adv, reported.get(key) or adv)
            low = 0.5 * adv
        samples = moves.setdefault(key, [])
        if b is not None and b.font_id == a.font_id and abs(b.size - a.size) < 1e-3 and b.dir == a.dir:
            dx, dy = b.origin[0] - a.origin[0], b.origin[1] - a.origin[1]
            move, across = (dx * ux + dy * uy) / a.size, abs(dx * uy - dy * ux) / a.size
            if across < 0.05 and low <= move <= adv + 0.1 and len(samples) < MAX_SAMPLES:
                samples.append(move)
    for (font_id, code), r in reported.items():
        if r is not None or moves[font_id, code]:
            out.setdefault(font_id, {})[code] = (moves[font_id, code], r)
    return out


def _code_error(moves: list[float], reported: float | None, width: float) -> float:
    if not width:
        return NO_GLYPH
    e = NO_GLYPH if reported is None else \
        (width - reported) / width if reported < width else OVER * (reported - width) / width
    for m in moves:
        e = min(e, abs(m - width) / width)
    return min(e, NO_GLYPH)


def _error(obs: dict[int, tuple[list[float], float | None]], widths: tuple) -> float:
    errs = sorted(_code_error(moves, reported, widths[c]) for c, (moves, reported) in obs.items())
    keep = len(errs) - int(TRIM * len(errs)) if len(errs) >= 4 else len(errs)
    return sum(errs[:keep]) / keep


def candidates(obs: dict, size: float) -> list[TexFont]:
    """The TeX fonts the measured glyphs (`advances`) fit best, drawn at `size` pt: all those
    within one step of the best fit, of the design sizes near `size` when one of those fits (LaTeX
    loads ecrm1095 for 10.95 pt text; a font drawn scaled - \\scalebox, an odd \\fontsize - still
    matches at another size). Several when the glyphs do not tell them apart: twins with the very
    same widths, or a few digits that are as wide in every face."""
    scored = []
    for f in fonts().values():
        e = _error(obs, f.widths) if obs else NO_GLYPH
        if e < GOOD:
            near = size > 0 and abs(size / f.size - 1) <= SIZE_PRIOR
            scored.append(((round(e / 0.0015), not near), f))
    if not scored:
        return []
    best = min(k for k, _ in scored)
    return [f for k, f in scored if k == best]


def _rank(f: TexFont) -> int:
    shape = f.name[2:4]
    return PLAIN_FIRST.index(shape) if shape in PLAIN_FIRST else len(PLAIN_FIRST) + (shape in TWINS)


def identify(obs: dict, size: float, prior: dict[str, float] | None = None) -> TexFont | None:
    """The TeX font of the measured glyphs, or None. Of the `candidates`, the face (the TFM name's
    two shape letters) the page uses most elsewhere, then the family (`prior`: shape or
    "family:<serif|sans|mono>" -> characters; "text:<family>" for the page's other fonts), then
    T1 (T2A's Latin letters are T1's; without a Cyrillic code the two are one font), then the
    everyday face: upright before its slanted twin (`slanted` tells those apart).

    A font of nothing but codes above 0x7F is no T1 text font: T1's accented letters come in the
    font of the words they are in, with its ASCII letters. Such a font is a symbol (TS1) or a
    Cyrillic (T2A) one before T1: a lone \\textmu at 0xB5, as wide as ectt's ţ, came out 'ţs'."""
    found = candidates(obs, size)
    if not found:
        return None
    prior = prior or {}
    symbols = bool(obs) and all(c >= 0x80 for c in obs)

    def key(f: TexFont):
        shape = f.name[2:4]
        return (-prior.get(SLANTED_OF.get(shape, shape), 0), -prior.get("family:" + family(shape), 0),
                -prior.get("text:" + family(shape), 0), (f.encoding == "T1") == symbols, _rank(f), f.name)
    return min(found, key=key)


def slanted(obs: dict, font: TexFont) -> bool:
    """The glyphs are the slanted twin of `font` (ecsi of ecss, ecsl of ecrm), which has the same
    widths. A slanted letter's ink leans out past its advance, so PDFium's loose box for it is
    wider than the width; upright CM letters stay inside theirs but for f and j (and a
    typewriter's m). Most of the letters leaning out: slanted."""
    letters = [c for c in obs if chr(c).isalpha() and chr(c) not in "fjm" and font.widths[c]
               and obs[c][1] is not None]
    out = [c for c in letters if obs[c][1] > font.widths[c] * 1.01 + 0.005]
    return len(out) >= min(3, len(letters)) and len(letters) >= 2 and len(out) >= 0.5 * len(letters)


def twin(font: TexFont) -> str:
    """The TFM name of `font`'s slanted twin (ecsi1095 for ecss1095), or its own."""
    shape = font.name[2:4]
    return font.name[:2] + SLANTED[shape] + font.name[4:] if shape in SLANTED else font.name


@dataclass(frozen=True)
class PageFont:
    name: str           # TFM name, the slanted twin's where the glyphs lean: ecsi1095
    encoding: str


def page_fonts(chars: list) -> dict[int, PageFont]:
    """font_id -> the TeX font of each Type 3 font the page's characters (content order) are
    drawn in, where one fits. Fonts whose glyphs fit several faces (a few digits are as wide in
    every face) take the face, else the family, the page's other Type 3 text is in, else the
    family of its other text (a line number in the body's sans, a euro in a sans deck)."""
    measured = advances(chars)
    if not measured:
        return {}
    size: dict[int, list[float]] = {}
    count: dict[int, int] = {}
    prior: dict[str, float] = {}
    for ch in chars:
        if ch.font == "Type3":
            size.setdefault(ch.font_id, []).append(ch.size)
            count[ch.font_id] = count.get(ch.font_id, 0) + ch.c.isalpha()
        elif ch.c.isalpha():
            fam = font_info(ch.font).family
            if fam in ("serif", "sans", "mono"):
                prior["text:" + fam] = prior.get("text:" + fam, 0) + 1
    found = {fid: candidates(obs, statistics.median(size[fid])) for fid, obs in measured.items()}
    for fid, fs in found.items():
        shapes = {SLANTED_OF.get(f.name[2:4], f.name[2:4]) for f in fs if f.encoding != "TS1"}
        families = {family(s) for s in shapes}
        if len(shapes) == 1:
            shape = next(iter(shapes))
            prior[shape] = prior.get(shape, 0) + count[fid]
        if len(families) == 1:
            key = "family:" + families.pop()
            prior[key] = prior.get(key, 0) + count[fid]
    out: dict[int, PageFont] = {}
    unknown = []
    for fid, obs in measured.items():
        font = identify(obs, statistics.median(size[fid]), prior) if found[fid] else None
        high = {c: v for c, v in obs.items() if c >= 0x80}
        if font is None or font.encoding == "T1" and high and _error(high, font.widths) >= GOOD:
            # (a Cyrillic word among Latin ones: EC's letters fit, its accented ones do not)
            unknown.append(fid)
            continue
        out[fid] = PageFont(twin(font) if slanted(obs, font) else font.name, font.encoding)
    for fid in unknown:
        if (font := _cyrillic(measured[fid], statistics.median(size[fid]), prior, out)) is not None:
            out[fid] = font
    return out


def _cyrillic(obs: dict, size: float, prior: dict, known: dict[int, PageFont]) -> PageFont | None:
    """A T2A (LH) font the table has no widths for (LH fonts are generated on demand: an
    installation has the few it was asked for). Its Latin letters are EC's, so an EC font that
    fits them but not the codes above 0x7F is that face's Cyrillic. Cyrillic words with no Latin
    letter: a Cyrillic font where the page has one already, or where the codes are T2A's letters
    (0xC0-0xFF, А-я) and none is Latin."""
    ascii_obs = {c: v for c, v in obs.items() if c < 0x80}
    high = [c for c in obs if c >= 0x80]
    if not high:
        return None
    font = identify(ascii_obs, size, prior) if ascii_obs else None
    if font is not None and font.encoding == "T1":
        name = "la" + (twin(font) if slanted(ascii_obs, font) else font.name)[2:]
        return PageFont(name, "T2A")
    letters_high = len({c for c in high if c >= 0xC0})
    t2a = [f for f in known.values() if f.encoding == "T2A"]
    if not any(chr(c).isalpha() for c in ascii_obs) and (t2a or letters_high >= 3):
        shape = statistics.mode([f.name[2:4] for f in t2a]) if t2a else "rm"
        return PageFont(f"la{shape}{round(size * 100):04d}", "T2A")
    return None


def decode(chars: list, found: dict[int, PageFont]) -> list:
    """The page's characters with those of the identified Type 3 fonts as their text (T1's 0x1C
    as ﬁ, T2A's 0xC6 as Ж) and their font named for the TeX font (ECBX1095; `fonts.font_info`
    reads its family and weight). A mark with no text (T1's compound word mark) is left out.
    PDFium gives code 0 (T1's grave accent) as U+FFFD."""
    out = []
    for ch in chars:
        font = found.get(ch.font_id) if ch.font == "Type3" else None
        if font is None or ch.synthetic:
            out.append(ch)
            continue
        code = 0 if ch.c == "�" else ord(ch.c) if len(ch.c) == 1 else None
        text = encoding(font.encoding).get(code, ch.c) if code is not None and code <= 0xFF else ch.c
        if text:
            out.append(dataclasses.replace(ch, c=text, font=font.name.upper()))
    return out
