"""Font files a person supplies, made into families adopt sets the deck in.

A host with no internet cannot fetch a deck's fonts from google/fonts (`fontfetch`), and a deck in
a face google/fonts does not carry (a company's own) is not there to fetch at all. Whoever has the
files hands them over - `adopt --fonts`, `deck_adopt(fonts=[...])` - in any format fonts travel
in: .ttf, .otf, .ttc, .woff, .woff2. Web fonts are unwrapped (neither lualatex nor xelatex reads
them), and every file is named by what it says it is - its name table's family, weight and
italic - never by its file name, which for a web font is often a hash.

They are laid out the way `fontfetch`'s cache is, in a folder of their own marked
`fontfetch.MANAGED`:

    <root>/<dir>/<Stem>-Regular.ttf, -Bold, -Italic, -BoldItalic   (cut by `fontfetch._build`)
    <root>/<dir>/<Stem>-W<weight>[Italic].ttf                         (a static weight given as it is)
    <root>/<dir>/src/...                                              (the files, unwrapped)
    <root>/collections/<name>.ttc                                     (read by its faces' names)

so the same code cuts the four styles out of a variable font and `fontfetch.weight_file` the
weights between them. `adopt.use_fonts(root)` puts the folder first in the ones adopt looks in:
a family given is preferred to the machine's own and to a fetch.
"""

from __future__ import annotations

import io
import re
import shutil
from pathlib import Path

from . import fontfetch

SUFFIXES = (".ttf", ".otf", ".ttc", ".woff", ".woff2")
NOT_A_FONT = "not a font file (.ttf, .otf, .ttc, .woff or .woff2)"


def kind_of(data: bytes) -> str | None:
    """What a font file's first bytes say it is: ttf, otf, ttc, woff, woff2 or None."""
    head = data[:4]
    return {b"\x00\x01\x00\x00": "ttf", b"true": "ttf", b"OTTO": "otf", b"ttcf": "ttc",
            b"wOFF": "woff", b"wOF2": "woff2"}.get(head)


def expand(paths) -> list[Path]:
    """The files among `paths`, a folder standing for every font file under it."""
    out: list[Path] = []
    for p in paths or ():
        p = Path(p)
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in SUFFIXES)
        else:
            out.append(p)
    return out


def describe(font) -> dict:
    """Family, weight, italic and variation axes, as the font's own tables give them. The
    typographic family (name 16) first: name 1 of a medium cut is often "Montserrat Medium"."""
    name = font["name"]
    family = (name.getDebugName(16) or name.getDebugName(1) or "").strip()
    sub = (name.getDebugName(17) or name.getDebugName(2) or "").lower()
    os2 = font["OS/2"] if "OS/2" in font else None
    weight = int(os2.usWeightClass) if os2 else (700 if "bold" in sub else 400)
    italic = bool(os2.fsSelection & 1) if os2 else False
    axes = [a.axisTag for a in font["fvar"].axes] if "fvar" in font else []
    return {"family": family, "weight": max(1, min(weight, 1000)),
            "italic": italic or "italic" in sub or "oblique" in sub, "axes": axes}


class ForeignFolder(ValueError):
    """The folder the fonts were to be laid out in holds something this module did not put there."""


def _fresh(root: Path) -> None:
    """An empty folder at `root`. One this module laid out before goes (the fonts of an earlier
    run must not be taken for this one's); anything else there is refused, never deleted."""
    if root.exists() and any(root.iterdir()):
        if not (root / fontfetch.MANAGED).is_file():
            raise ForeignFolder(f"{root} holds files that are not supplied fonts laid out by adopt, and "
                                f"adopt empties that folder; move them, or give adopt another work folder")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "font"


def install(paths, root: Path) -> dict:
    """Lay the fonts among `paths` out under `root` (emptied first) as families adopt can use.

    Returns `{"families": {family: {"styles", "weights", "files", "variable"}}, "skipped":
    [{"file", "reason"}]}`: a family is listed with the fontspec styles made for it, the weights
    between them that were given as static files, and the files it came from; a file that could
    not be used says why. Nothing here raises for a bad file."""
    root = Path(root)
    report: dict = {"families": {}, "skipped": []}
    files = expand(paths)
    if not files:
        return report

    def skip(name: str, reason: str) -> None:
        report["skipped"].append({"file": name, "reason": reason})

    try:
        from fontTools.ttLib import TTCollection, TTFont
    except ImportError:
        for f in files:
            skip(f.name, "fontTools is not installed, so no font file can be read (pip install fonttools)")
        return report
    _fresh(root)
    (root / fontfetch.MANAGED).write_text("font files a person supplied, laid out by beamer2slides.fontfiles\n",
                                          encoding="utf-8")
    groups: dict[str, list[dict]] = {}
    for f in files:
        try:
            data = f.read_bytes()
        except OSError as e:
            skip(f.name, f"could not be read ({e})")
            continue
        kind = kind_of(data)
        if kind is None:
            skip(f.name, NOT_A_FONT)
            continue
        if kind == "ttc":
            try:
                faces = TTCollection(io.BytesIO(data)).fonts
                names = sorted({describe(face)["family"] for face in faces} - {""})
            except Exception as e:  # noqa: BLE001 - a broken file is no font, not a failed run
                skip(f.name, f"unreadable ({type(e).__name__}: {e})")
                continue
            dest = root / "collections" / (_safe(Path(f.name).stem) + ".ttc")
            fontfetch._write_atomic(dest, data)
            for n in names:
                report["families"].setdefault(n, {"styles": ["collection"], "weights": [], "files": [],
                                                  "variable": False})["files"].append(f.name)
            continue
        try:
            font = TTFont(io.BytesIO(data), recalcTimestamp=False)   # (the same file every run)
            if kind in ("woff", "woff2"):
                font.flavor = None                      # the sfnt inside, as lualatex reads it
                buf = io.BytesIO()
                font.save(buf)
                data = buf.getvalue()
                kind = "otf" if data[:4] == b"OTTO" else "ttf"
            info = describe(font)
        except Exception as e:  # noqa: BLE001 - woff2 without brotli, a truncated file...
            why = "a .woff2 needs the brotli package (pip install \"beamer2slides[woff2]\")" \
                if "brotli" in str(e).lower() \
                else f"unreadable ({type(e).__name__}: {e})"
            skip(f.name, why)
            continue
        if not fontfetch.folder_name(info["family"]):
            skip(f.name, "its name table gives no family name")
            continue
        groups.setdefault(info["family"], []).append({**info, "data": data, "suffix": "." + kind, "file": f.name})

    for family, entries in groups.items():
        folder, stem = fontfetch.folder_name(family), fontfetch.stem_name(family)
        src = root / folder / "src"
        meta: dict = {"name": family, "fonts": [], "axes": {}}
        used, statics = [], []
        for e in entries:
            if e["axes"]:
                name = f"{stem}{'-Italic' if e['italic'] else ''}[{','.join(e['axes'])}]{e['suffix']}"
            else:
                name = f"{stem}-{e['weight']}{'Italic' if e['italic'] else ''}{e['suffix']}"
            if (src / name).exists():
                skip(e["file"], f"{family} {e['weight']}{' italic' if e['italic'] else ''} was given twice; "
                                f"the first file is used")
                continue
            fontfetch._write_atomic(src / name, e["data"])
            used.append(e)
            # a variable font with an ital axis is its own italic
            for italic in ((False, True) if "ital" in e["axes"] else (e["italic"],)):
                meta["fonts"].append({"filename": name, "style": "italic" if italic else "normal",
                                      "weight": e["weight"]})
            if not e["axes"]:
                statics.append((e, name))
        try:
            styles = fontfetch._build(family, folder, None, meta, root=root)
        except Exception as e:  # noqa: BLE001
            for x in used:
                skip(x["file"], f"{family} could not be made into styles ({type(e).__name__}: {e})")
            continue
        if not styles:
            for x in used:
                skip(x["file"], f"{family} has no upright face among the files given, and a family needs one")
            continue
        # A static weight that is none of the four styles is kept as `weight_file` names what it
        # cuts, so a run set in it gets a face of its own (`adopt.weight_faces`)
        weights = []
        for e, name in statics:
            if e["weight"] not in (400, 700) and e["suffix"] == ".ttf":
                target = root / folder / f"{stem}-W{e['weight']}{'Italic' if e['italic'] else ''}.ttf"
                fontfetch._write_atomic(target, (src / name).read_bytes())
                weights.append(f"{e['weight']}{' italic' if e['italic'] else ''}")
        report["families"][family] = {"styles": sorted(styles), "weights": sorted(weights),
                                      "files": [x["file"] for x in used],
                                      "variable": any(x["axes"] for x in used)}
    return report


def summary(report: dict) -> list[str]:
    """What `install` made of the files, a line each, for a log."""
    lines = []
    for family, got in sorted(report.get("families", {}).items()):
        extra = f", weights {', '.join(got['weights'])}" if got.get("weights") else ""
        lines.append(f"  {family}: {', '.join(got['styles'])}{extra} (from {', '.join(got['files'])})")
    for s in report.get("skipped", []):
        lines.append(f"  {s['file']}: not used - {s['reason']}")
    return lines
