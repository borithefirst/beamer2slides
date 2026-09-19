"""The typefaces a foreign deck names that this machine does not have, from github.com/google/fonts.

Most decks people build in Slides are written in Google Fonts (Open Sans, Montserrat, Inter, Work
Sans, Delius, ...), and a family set in TeX Gyre instead breaks every line in another place. Every
one of them is in the google/fonts repository under an open licence (OFL, Apache, UFL), so adopt
fetches the family once into a user cache and uses it like a font the machine has:

    <cache>/<dir>/<Stem>-Regular.ttf, -Bold, -Italic, -BoldItalic   (what `adopt.font_candidates` reads)
    <cache>/<dir>/<Stem>-LICENSE.txt                                  (travels with the files)
    <cache>/<dir>/src/...                                             (the downloads, not read as fonts)

`<dir>` is the family's folder name in google/fonts ("Open Sans" -> opensans) and `<Stem>` its name
without spaces, which is the name the IR carries (`deck_ir` runs' `font`). Where the family only
exists as a variable font (most of them today) the four static instances are cut with fontTools'
instancer at weights 400 and 700 (clamped to the font's range; every other axis at its default).

Nothing here is required: no network, no fontTools, a family google/fonts does not have - each of
these returns None and adopt falls back to what it did before (the nearest family it has, or TeX
Gyre). A family google/fonts answered "not found" for is remembered (`missing.json`), so a deck in
Calibri asks GitHub once, not on every build. `$B2S_FONT_FETCH=0` turns fetching off; `adopt`
also never fetches while `$B2S_FONTS` names the only folders to use (the tests).

Cache: `$B2S_FONT_CACHE`, else %LOCALAPPDATA%\\beamer2slides\\fonts on Windows and
~/.cache/beamer2slides/fonts elsewhere.
"""

import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RAW = "https://raw.githubusercontent.com/google/fonts/main/"
LICENCE_DIRS = ("ofl", "apache", "ufl")
LICENCE_FILES = {"ofl": "OFL.txt", "apache": "LICENSE.txt", "ufl": "UFL.txt"}
STYLES = {"Regular": (400, False), "Bold": (700, False), "Italic": (400, True), "BoldItalic": (700, True)}
TIMEOUT = 30


def cache_dir() -> Path:
    if os.environ.get("B2S_FONT_CACHE"):
        return Path(os.environ["B2S_FONT_CACHE"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "beamer2slides" / "fonts"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "beamer2slides" / "fonts"


def enabled() -> bool:
    return os.environ.get("B2S_FONT_FETCH", "1").strip().lower() not in ("0", "no", "off", "false")


def folder_name(family: str) -> str:
    """google/fonts' folder for a family: "Open Sans" / "OpenSans" -> opensans, "Exo 2" -> exo2."""
    return "".join(c for c in family.lower() if c.isalnum())


def stem_name(family: str) -> str:
    return "".join(c for c in family if c.isalnum())


def get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return r.read()


def parse_metadata(text: str) -> dict:
    """The parts of a METADATA.pb (protobuf text format) this needs: name, the font files with their
    style and weight, and the axes of a variable font."""
    name = re.search(r'^name:\s*"([^"]*)"', text, re.M)
    fonts = []
    for block in re.findall(r"^fonts\s*\{(.*?)^\}", text, re.M | re.S):
        f = dict(re.findall(r'^\s*(\w+):\s*"?([^"\n]*)"?\s*$', block, re.M))
        if f.get("filename"):
            fonts.append({"filename": f["filename"], "style": f.get("style", "normal"),
                          "weight": int(f.get("weight", "400") or 400)})
    axes = {}
    for block in re.findall(r"^axes\s*\{(.*?)^\}", text, re.M | re.S):
        a = dict(re.findall(r'^\s*(\w+):\s*"?([^"\n]*)"?\s*$', block, re.M))
        if a.get("tag"):
            axes[a["tag"]] = (float(a.get("min_value", 0)), float(a.get("max_value", 0)))
    return {"name": name.group(1) if name else None, "fonts": fonts, "axes": axes}


def _missing_path() -> Path:
    return cache_dir() / "missing.json"


def _missing() -> set[str]:
    try:
        return set(json.loads(_missing_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _remember_missing(folder: str) -> None:
    try:
        known = _missing() | {folder}
        _write_atomic(_missing_path(), json.dumps(sorted(known)).encode())
    except OSError:
        pass


def _write_atomic(path: Path, data: bytes) -> None:
    """Several adopt runs may fetch the same family at once: each writes a temporary and renames it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cached(family: str) -> dict[str, Path]:
    """The static files of a family already in the cache, by style name (Regular, Bold, ...)."""
    folder = cache_dir() / folder_name(family)
    stem = stem_name(family)
    return {s: folder / f"{stem}-{s}.ttf" for s in STYLES if (folder / f"{stem}-{s}.ttf").exists()}


# Families Slides offers under a name of their own that google/fonts carries as an optical size of
# another family's variable font: "Google Sans Text" is Google Sans at opsz 17 (the text cut, looser
# and wider: gdg24's bold 50 pt "Statistics" sets 3.2% wider there, as the deck's thumbnail shows it,
# than in the opsz 18 default that stood in for it). Folder name -> (family, axis location).
OPTICAL = {"googlesanstext": ("Google Sans", {"opsz": 17.0})}


def fetch_family(family: str, log=print) -> dict[str, Path] | None:
    """The family's static files by style name, fetched into the cache if they are not there yet;
    None when google/fonts has no such family or it cannot be fetched (no network, no fontTools)."""
    if not family or not enabled():
        return None
    have = cached(family)
    if "Regular" in have:
        return have
    folder = folder_name(family)
    base, axes = OPTICAL.get(folder, (None, {}))
    if base is not None:
        folder = folder_name(base)
    if not folder or folder in _missing():
        return None
    meta, lic = None, None
    try:
        for licence in LICENCE_DIRS:
            try:
                meta = parse_metadata(get(f"{RAW}{licence}/{folder}/METADATA.pb").decode("utf-8", "replace"))
                lic = licence
                break
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
        if meta is None or not meta["fonts"]:
            _remember_missing(folder)
            return None
        if base is not None and not any("[" in f["filename"] for f in meta["fonts"]):
            return None                                 # no variable font to cut the optical size from
        out = _build(family, folder, lic, meta, axes)
    except (OSError, ValueError, ImportError, KeyError) as e:     # offline, GitHub down, a broken font
        log(f"  {family}: could not fetch it from google/fonts ({type(e).__name__}: {str(e)[:80]})")
        return None
    if out:
        log(f"  {family}: fetched from google/fonts ({lic}) into {cache_dir() / folder_name(family)}")
    return out or None


def _build(family: str, folder: str, lic: str, meta: dict, axes: dict | None = None) -> dict[str, Path]:
    """The four static instances of `family` from google/fonts' `folder` (its own, or the family an
    OPTICAL name is cut from, at the axis location `axes`), into the family's own cache folder."""
    dest = cache_dir() / folder_name(family)
    src = cache_dir() / folder / "src"
    stem = stem_name(family)
    downloaded: dict[str, Path] = {}

    def download(name: str) -> Path:
        if name not in downloaded:
            path = src / name
            if not path.exists():
                _write_atomic(path, get(f"{RAW}{lic}/{folder}/{urllib.parse.quote(name)}"))
            downloaded[name] = path
        return downloaded[name]

    try:
        _write_atomic(dest / f"{stem}-LICENSE.txt", get(f"{RAW}{lic}/{folder}/{LICENCE_FILES[lic]}"))
    except urllib.error.HTTPError:
        pass
    out: dict[str, Path] = {}
    for style, (weight, italic) in STYLES.items():
        files = [f for f in meta["fonts"] if (f["style"] == "italic") == italic]
        if not files:
            continue
        variable = [f for f in files if "[" in f["filename"]]
        target = dest / f"{stem}-{style}.ttf"
        if variable:
            from fontTools.ttLib import TTFont
            from fontTools.varLib import instancer
            vf = TTFont(download(variable[0]["filename"]))
            have = {a.axisTag: a for a in vf["fvar"].axes}
            loc = {tag: a.defaultValue for tag, a in have.items()}
            for tag, value in (axes or {}).items():
                if tag in have:
                    loc[tag] = min(max(value, have[tag].minValue), have[tag].maxValue)
            if "wght" in have:
                w = have["wght"]
                loc["wght"] = min(max(weight, w.minValue), w.maxValue)
                if weight == 700 and w.maxValue < 600:
                    continue                                    # no bold in this family: fontspec fakes none
            if italic and "ital" in have:
                loc["ital"] = have["ital"].maxValue
            font = instancer.instantiateVariableFont(vf, loc)
            font["OS/2"].usWeightClass = int(loc.get("wght", weight))
            if axes:
                rename(font, family, style)
            dest.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=dest) as tmp:
                part = Path(tmp) / target.name
                font.save(part)
                os.replace(part, target)
        else:
            best = min(files, key=lambda f: abs(f["weight"] - weight))
            if weight == 700 and best["weight"] < 600:
                continue
            _write_atomic(target, download(best["filename"]).read_bytes())
        out[style] = target
    return out if "Regular" in out else {}


def weight_file(upright: Path, weight: int, italic: bool = False) -> Path | None:
    """A static instance at `weight` of a family this cache holds (`upright` is its Regular file),
    cut from the variable font it was made from - Slides sets a weight per run (gdg24's headings are
    Google Sans 600 and 500, journey-maps' text Montserrat 300 and 500) where fontspec's four styles
    have only 400 and 700. None when the family is not a variable one fetched here, has no such
    weight on its axis, or has no italic when one is asked for. Written beside the four styles as
    `<Stem>-W<weight>[Italic].ttf`, which `adopt.font_candidates` does not read as a style."""
    upright = Path(upright)
    dest = upright.parent
    try:
        if dest.parent.resolve() != cache_dir().resolve():
            return None
    except OSError:
        return None
    stem = upright.stem.partition("-")[0]
    target = dest / f"{stem}-W{weight}{'Italic' if italic else ''}.ttf"
    if target.exists():
        return target
    base, axes = OPTICAL.get(dest.name, (None, {}))
    src = cache_dir() / (folder_name(base) if base else dest.name) / "src"
    variable = [f for f in sorted(src.glob("*[[]*].ttf")) if ("Italic" in f.name) == italic] if src.is_dir() else []
    if not variable:
        return None
    try:
        from fontTools.ttLib import TTFont
        from fontTools.varLib import instancer
        vf = TTFont(variable[0])
        have = {a.axisTag: a for a in vf["fvar"].axes}
        w = have.get("wght")
        if w is None or not w.minValue <= weight <= w.maxValue:
            return None
        loc = {tag: a.defaultValue for tag, a in have.items()}
        for tag, value in axes.items():
            if tag in have:
                loc[tag] = min(max(value, have[tag].minValue), have[tag].maxValue)
        loc["wght"] = float(weight)
        if italic and "ital" in have:
            loc["ital"] = have["ital"].maxValue
        font = instancer.instantiateVariableFont(vf, loc)
        font["OS/2"].usWeightClass = int(weight)
        with tempfile.TemporaryDirectory(dir=dest) as tmp:
            part = Path(tmp) / target.name
            font.save(part)
            os.replace(part, target)
    except (OSError, ValueError, ImportError, KeyError, AssertionError):
        return None
    return target


def rename(font, family: str, style: str) -> None:
    """Give an instance cut for an OPTICAL name that name in its own name table, which is what
    `adopt.font_candidates` also reads: left as it was, Google Sans Text's files would call themselves
    Google Sans and could be taken for the display cut."""
    name = font["name"]
    words = {"Regular": "Regular", "Bold": "Bold", "Italic": "Italic", "BoldItalic": "Bold Italic"}[style]
    for rec in list(name.names):
        if rec.nameID in (16, 17, 21, 22, 25):              # typographic names would still say the base
            name.removeNames(nameID=rec.nameID)
    for nid, value in ((1, family), (2, words), (4, f"{family} {words}"),
                       (6, f"{stem_name(family)}-{style}")):
        name.setName(value, nid, 3, 1, 0x409)
        name.setName(value, nid, 1, 0, 0)
