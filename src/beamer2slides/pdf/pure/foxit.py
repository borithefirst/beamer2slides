"""PDFium's built-in font faces (core/fxge/fontdata/chromefontdata), fetched, never committed.

PDFium draws a font the PDF does not embed with a face its font mapper finds: a system font (GDI
on Windows) or, when none fits, one of sixteen faces compiled into the library - the fourteen
standard faces (bare CFF programs, `CFX_StandardFont::GetFontData`) and the two generic multiple
master faces FoxitSansMM / FoxitSerifMM (Type 1 PFB programs, `GetGenericSansFontData` /
`GetGenericSerifFontData`). Their glyphs decide the boxes PDFium reports for such text, so the
pure reader needs the very same bytes. They are third-party binaries and stay out of the tree:
`fetch()` reads the `std::array` initialisers from PDFium's sources at the pinned branch
(`pdfium.googlesource.com`, or a local checkout), checks each face against its pinned SHA-256
and stores it in a user cache; `face_data()` reads it back, and answers None - the pure reader
then keeps its old behaviour (no face) - when a face is absent or does not match.

Cache: `$B2S_FOXIT_FONTS`, else %LOCALAPPDATA%\\beamer2slides\\foxit on Windows and
~/.cache/beamer2slides/foxit elsewhere. `python -m beamer2slides.pdf.pure.foxit [--from DIR]`
fills it (DIR: a folder holding the Foxit*.cpp files, flat or in a PDFium checkout).
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = "https://pdfium.googlesource.com/pdfium/"
REF = "refs/heads/chromium/7999"        # the PDFium pypdfium2 5.13.0 ships (153.0.7999.0)
SOURCE_DIR = "core/fxge/fontdata/chromefontdata"

# face -> (size, SHA-256) at REF
FACES = {
    "FoxitDingbats": (29513, "845c752392b6c914fb989c75a08b7792b88f542d2499042ef2889f8c814a16ed"),
    "FoxitFixed": (17597, "b6c8fe53f134b8b6d4578cd2d544df4cee9624c4efa8d51a560fb40ea296101b"),
    "FoxitFixedBold": (18055, "f1b7159702973f54fc86254ea38bcf3712b2a736eb3a0995751e9f4bb45ad603"),
    "FoxitFixedBoldItalic": (19151, "8a000945843bd31add06aee63bf9fd41b7578b4f3242a4b7cd46349ad9d24d4d"),
    "FoxitFixedItalic": (18746, "5007faf8320fa1fcc08b8894b356ad976f60b992ba29ee5272578bbdebaf3876"),
    "FoxitSans": (15025, "a02339e95d20db14cbdfc1c6baf359cd36a091d5434925c3a4040f60e1a1af9e"),
    "FoxitSansBold": (16344, "7bdfa36de1d5757dcfaf8b8a4be95c6e41c40fa70dbed9d78bf979a7b9959054"),
    "FoxitSansBoldItalic": (16418, "555ce45924338e2e4b32821910221ce85da691fd59a9723c4c971a7c5627fc8b"),
    "FoxitSansItalic": (16339, "7751cbefeda351708dae8c4a88db043411bc39ab2ae2097685da845d4d16f307"),
    "FoxitSansMM": (66919, "ced6f6742bf591dacae1cc540792b163b6bedba4b0beafdcb4f3d7b9ad226b72"),
    "FoxitSerif": (19469, "4f57d2b9d884af8f907bf22df6019b52d86cbf6214fdbd02b1ae05472a543f35"),
    "FoxitSerifBold": (19395, "0bdf4b04e964139818d51eda03d566ba999fc3ba2421b1c6c51f9dc969022e80"),
    "FoxitSerifBoldItalic": (20733, "a406cac82583bf98175cb62c87ed5e95c45fbb34eece63a10b0a13e793cb2e10"),
    "FoxitSerifItalic": (21227, "610ae0687198045c4db5d1a6650fd5e92536631706382eb8b72e38df578d0ae9"),
    "FoxitSerifMM": (113417, "24b32eaeaf795e341e3818665062d3fdc2e06787686618a877944b6515746a46"),
    "FoxitSymbol": (16729, "47967d055530e7357088a08403115425643ec2cdfd6201ba8af0fbd7116c1539"),
}

# CFX_StandardFont::GetFontData: the standard faces in the base 14 index order
STANDARD = ("FoxitFixed", "FoxitFixedBold", "FoxitFixedBoldItalic", "FoxitFixedItalic",
            "FoxitSans", "FoxitSansBold", "FoxitSansBoldItalic", "FoxitSansItalic",
            "FoxitSerif", "FoxitSerifBold", "FoxitSerifBoldItalic", "FoxitSerifItalic",
            "FoxitSymbol", "FoxitDingbats")
GENERIC_SANS, GENERIC_SERIF = "FoxitSansMM", "FoxitSerifMM"

_ARRAY = re.compile(r"std::array<uint8_t,\s*(\d+)>\s*k(\w+)FontData\s*=\s*\{\{(.*?)\}\};", re.S)
_loaded: dict[str, bytes | None] = {}


def cache_dir() -> Path:
    if os.environ.get("B2S_FOXIT_FONTS"):
        return Path(os.environ["B2S_FOXIT_FONTS"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "beamer2slides" / "foxit"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "beamer2slides" / "foxit"


def _valid(name: str, data: bytes) -> bool:
    size, digest = FACES[name]
    return len(data) == size and hashlib.sha256(data).hexdigest() == digest


def face_data(name: str) -> bytes | None:
    """The face's bytes as PDFium compiles them in, or None when the cache lacks it (or holds
    anything else)."""
    if name not in _loaded:
        data = None
        try:
            raw = (cache_dir() / f"{name}.bin").read_bytes()
            data = raw if _valid(name, raw) else None
        except OSError:
            data = None
        _loaded[name] = data
    return _loaded[name]


def missing() -> list[str]:
    """The faces the cache does not (correctly) hold."""
    return [n for n in FACES if face_data(n) is None]


def available() -> bool:
    return not missing()


def extract(cpp_text: str) -> tuple[str, bytes]:
    """The face name and bytes of one chromefontdata source file."""
    m = _ARRAY.search(cpp_text)
    if not m:
        raise ValueError("no font data array")
    data = bytes(int(x, 16) for x in re.findall(r"0x[0-9a-fA-F]+", m.group(3)))
    if len(data) != int(m.group(1)):
        raise ValueError(f"k{m.group(2)}FontData: {len(data)} bytes, declared {m.group(1)}")
    return m.group(2), data


def _download(url: str) -> bytes:
    for attempt in range(8):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):   # rate limited, or the server out of sorts
                raise
            time.sleep(10 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError):
            if attempt == 7:
                raise
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"refused eight times: {url}")


def _source(name: str, local: Path | None) -> str:
    if local is not None:
        for p in (local / f"{name}.cpp", local / SOURCE_DIR / f"{name}.cpp",
                  local / f"{SOURCE_DIR.replace('/', '_')}_{name}.cpp"):
            if p.is_file():
                return p.read_text(encoding="latin-1")
        raise FileNotFoundError(f"{name}.cpp not under {local}")
    raw = _download(f"{REPO}+/{REF}/{SOURCE_DIR}/{name}.cpp?format=TEXT")
    return base64.b64decode(raw).decode("latin-1")


def fetch(local: Path | None = None, log=print) -> Path:
    """Fill the cache: every face not there yet, from `local` or from PDFium's repository.
    A face whose bytes do not match the pinned digest is refused (nothing is written)."""
    dest = cache_dir()
    dest.mkdir(parents=True, exist_ok=True)
    for name in FACES:
        if face_data(name) is not None:
            continue
        array_name, data = extract(_source(name, local))
        if array_name != name or not _valid(name, data):
            raise ValueError(f"{name}: the source does not hold the pinned face (sha256 "
                             f"{hashlib.sha256(data).hexdigest()})")
        tmp = dest / f"{name}.bin.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, dest / f"{name}.bin")
        _loaded.pop(name, None)
        log(f"{name}: {len(data)} bytes")
    return dest


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m beamer2slides.pdf.pure.foxit", description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="local", type=Path, help="a folder with PDFium's Foxit*.cpp sources")
    ap.add_argument("--check", action="store_true", help="only report what the cache holds")
    args = ap.parse_args(argv)
    if args.check:
        gone = missing()
        print(f"{cache_dir()}: {len(FACES) - len(gone)} of {len(FACES)} faces" + (f", missing {gone}" if gone else ""))
        return 1 if gone else 0
    print(f"faces in {fetch(args.local)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
