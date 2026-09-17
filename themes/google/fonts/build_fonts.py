"""Fetch the open-source Google Sans Flex / Google Sans Code and cut static instances for LaTeX.

The variable fonts come from github.com/google/fonts (SIL OFL 1.1, no reserved font names).
Static cuts keep the original family names, so PDFs name them GoogleSansFlex-SemiBold etc.,
which beamer2slides maps to the same families in Google Slides.

Instances: Google Sans Flex at opsz 18, wdth 100 (within 0.5% of Slides' Google Sans, see
tools/probe_google_sans.py), weights 400/500/600/700 upright and italic (slnt -10);
Google Sans Code 400/500/700 and italic 400 (same advance widths as Google Sans Mono).

Usage: python themes/google/fonts/build_fonts.py
"""

import urllib.request
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
RAW = "https://raw.githubusercontent.com/google/fonts/main/ofl/"
DOWNLOADS = {
    "GoogleSansFlex-VF.ttf": "googlesansflex/GoogleSansFlex%5BGRAD%2CROND%2Copsz%2Cslnt%2Cwdth%2Cwght%5D.ttf",
    "OFL-GoogleSansFlex.txt": "googlesansflex/OFL.txt",
    "TRADEMARKS-GoogleSansFlex.md": "googlesansflex/TRADEMARKS.md",
    "GoogleSansCode-VF.ttf": "googlesanscode/GoogleSansCode%5Bwght%5D.ttf",
    "GoogleSansCode-Italic-VF.ttf": "googlesanscode/GoogleSansCode-Italic%5Bwght%5D.ttf",
    "OFL-GoogleSansCode.txt": "googlesanscode/OFL.txt",
}
WEIGHT_NAMES = {400: "Regular", 500: "Medium", 600: "SemiBold", 700: "Bold"}


def fetch() -> None:
    SRC.mkdir(exist_ok=True)
    for name, path in DOWNLOADS.items():
        if not (SRC / name).exists():
            print("downloading", name)
            urllib.request.urlretrieve(RAW + path, SRC / name)


def rename(font: TTFont, family: str, weight: int, italic: bool) -> str:
    style = WEIGHT_NAMES[weight]
    if italic:
        style = "Italic" if weight == 400 else style + " Italic"
    ps = f"{family.replace(' ', '')}-{style.replace(' ', '')}"
    name = font["name"]
    # RIBBI naming: family + style for 400/700, typographic names (16/17) for everything.
    ribbi = weight in (400, 700)
    legacy_family = family if ribbi else f"{family} {WEIGHT_NAMES[weight]}"
    legacy_style = ("Bold " if weight == 700 else "") + ("Italic" if italic else "")
    legacy_style = legacy_style.strip() or "Regular"
    for rec_id, value in ((1, legacy_family), (2, legacy_style), (4, f"{family} {style}"), (6, ps),
                          (16, family), (17, style)):
        name.setName(value, rec_id, 3, 1, 0x409)
    for rec_id in (25,):  # variations PostScript name prefix no longer applies
        name.removeNames(nameID=rec_id)
    font["OS/2"].usWeightClass = weight
    fs = 0
    if italic:
        fs |= 0x01
    if weight == 700:
        fs |= 0x20
    if not fs:
        fs = 0x40
    font["OS/2"].fsSelection = (font["OS/2"].fsSelection & ~0x61) | fs
    font["head"].macStyle = (0x01 if weight == 700 else 0) | (0x02 if italic else 0)
    return ps


def cut(vf_name: str, family: str, axes: dict, weight: int, italic: bool) -> None:
    vf = TTFont(SRC / vf_name)
    loc = dict(axes, wght=weight)
    loc = {k: v for k, v in loc.items() if k in {a.axisTag for a in vf["fvar"].axes}}
    font = instancer.instantiateVariableFont(vf, loc)
    ps = rename(font, family, weight, italic)
    font.save(HERE / f"{ps}.ttf")
    print("wrote", f"{ps}.ttf")


def main() -> None:
    fetch()
    flex_axes = {"opsz": 18, "wdth": 100, "GRAD": 0, "ROND": 0}
    for weight in WEIGHT_NAMES:
        cut("GoogleSansFlex-VF.ttf", "Google Sans Flex", dict(flex_axes, slnt=0), weight, False)
        cut("GoogleSansFlex-VF.ttf", "Google Sans Flex", dict(flex_axes, slnt=-10), weight, True)
    for weight in (400, 500, 700):
        cut("GoogleSansCode-VF.ttf", "Google Sans Code", {}, weight, False)
    cut("GoogleSansCode-Italic-VF.ttf", "Google Sans Code", {}, 400, True)
    for name in ("OFL-GoogleSansFlex.txt", "OFL-GoogleSansCode.txt", "TRADEMARKS-GoogleSansFlex.md"):
        (HERE / name).write_bytes((SRC / name).read_bytes())


if __name__ == "__main__":
    main()
