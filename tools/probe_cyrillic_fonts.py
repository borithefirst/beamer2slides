"""Probe: which Google font a run of Cyrillic should be set in, on Slides' own renderer.

Slides' Lato has no Cyrillic. A Russian deck converted in Lato (africa-remote-sens) came out with
every body list taller than the PDF's - a line pitch of 24.6 px where the PDF's is 22 (+12%), items
re-wrapping and running out of their panels - and its lines 2-3% wider. Slides draws the letters
from a fallback face of its own (Arial's widths: advances.json, `probe_advances.py cyrillic`),
and the widths say little: the calibration sentences predict a Cyrillic pangram only 0.5% wider
than a Latin one at CM's factor (cm-super's SFSS1000 against those advances). What a fallback does
to the *line box* nothing local can say, and neither which named face is nearest to cm-super's
Cyrillic once sized to its width. This measures, for Lato and for Google faces with Cyrillic:

- the width of a Russian and of an English pangram (em: the "|  text  |" bars of probe_advances);
- the line pitch of a paragraph of Cyrillic lines against one of Latin lines (soft line breaks,
  lineSpacing 100): a pitch ratio above 1 is a fallback's line box, which emit's line model
  (`emit_metrics.LINE_EM`, calibrated on Lato) does not know;
- the ink height of x-height letters and capitals, Latin and Cyrillic (40 pt rows).

and against cm-super's SFSS1000 (its AFM: Cyrillic advances, XHeight 0.444, CapHeight 0.694) it
prints, per face: the size factor that matches the Russian pangram's width (Slides em / PDF em),
the x-height and cap height that size gives (PDF em), and the pitch ratio. It decides:

- **keep Lato** when Lato's Cyrillic pitch ratio is within 1% of 1.0 and no face's width-matched
  x-height is nearer SFSS's by more than 2% of an em: the overflow is then not the font's (look at
  the optical sizes - the deck is set at 5-8 pt - and at the panel sizing instead);
- otherwise **name the best face for Cyrillic runs**: a pitch ratio within 1% (its Latin and
  Cyrillic share one line box) and the nearest width-matched x-height and cap height. emit would
  then write that face for a run whose letters are mostly Cyrillic (`FONT_FOR_FAMILY` per script),
  sized by its factor printed here, and the face's line box goes into emit's line model; its
  advances are measured with `tools/probe_advances.py cyrillic` after adding it to FONTS there.

One slide per face, deleted after. Results: out/probe_cyrillic_fonts/result.json, thumbnails beside it.

Usage: python tools/probe_cyrillic_fonts.py
"""

import gzip
import json
import os
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.google_types import SlidesRequest, object_id, presentation_id
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_cyrillic_fonts"
# Lato first (the substitute today), then Google faces with Cyrillic: humanist (PT Sans was drawn
# for Russian; Fira, Source, Open, Noto), grotesque (Roboto, Arimo - Arial's metrics, which the
# fallback's widths match), and two more.
FONTS = ["Lato", "PT Sans", "Roboto", "Open Sans", "Noto Sans", "Arimo", "Fira Sans", "Source Sans Pro",
         "IBM Plex Sans", "Ubuntu"]
RUSSIAN = "Съешь же ещё этих мягких французских булок, да выпей чаю"
ENGLISH = "The quick brown fox jumps over the lazy dog"
SIZE = 12          # pangram rows
PITCH_SIZE = 20    # pitch paragraphs
LINES = 6
INK_SIZE = 40      # x-height and cap height rows
SOFT_BREAK = chr(11)
# cm-super's glyph names for Russian's letters: afii10017-10049 А-Я and afii10065-10097 а-я, Ё/ё at
# 10023/10071 in between
AFII = {f"afii{10017 + i + (i >= 6)}": chr(0x410 + i) for i in range(32)}
AFII.update({f"afii{10065 + i + (i >= 6)}": chr(0x430 + i) for i in range(32)})
AFII.update({"afii10023": "Ё", "afii10071": "ё", "comma": ",", "period": "."})


@dataclass(frozen=True, kw_only=True)
class Sfss:
    """cm-super's SFSS1000 as TeX set the deck: advances (em), interword space, x and cap height."""
    advances: dict[str, float]
    space: float
    x_height: float
    cap_height: float


@dataclass(frozen=True, kw_only=True)
class Measured:
    """One face on Slides' renderer (em of the size set)."""
    font: str
    russian: float
    english: float
    pitch_latin: float
    pitch_cyrillic: float
    x_latin: float
    x_cyrillic: float
    cap_latin: float
    cap_cyrillic: float


def miktex_afm() -> Path:
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        for sub in ("Programs/MiKTeX", "MiKTeX"):
            p = Path(base) / sub / "fonts" / "afm" / "public" / "cm-super" / "sfss1000.afm.gz"
            if p.is_file():
                return p
    raise SystemExit("cm-super's sfss1000.afm.gz not found (MiKTeX)")


def read_sfss(path: Path) -> Sfss:
    advances: dict[str, float] = {}
    header: dict[str, float] = {}
    with gzip.open(path, "rt", encoding="latin-1") as f:
        for line in f:
            word = line.split(" ", 1)[0]
            if word in ("XHeight", "CapHeight"):
                header[word] = float(line.split()[1]) / 1000
            elif word == "C":
                fields = dict(x.strip().split(" ", 1) for x in line.split(";") if x.strip())
                name = fields["N"]
                ch = AFII.get(name) or (name if len(name) == 1 else None)
                if ch is not None:
                    advances[ch] = float(fields["WX"]) / 1000
    return Sfss(advances=advances, space=0.3333, x_height=header["XHeight"], cap_height=header["CapHeight"])


def pdf_em(text: str, face: Sfss) -> float:
    """The width TeX gives `text` in SFSS1000 (em; no kerning: Russian's pairs in it are rare)."""
    return sum(face.space if c == " " else face.advances[c] for c in text)


def ink_rows(img: np.ndarray, k: float, x: float, y: float, w: float, h: float) -> list[tuple[int, int]]:
    """(top, bottom) pixel rows of each run of inked rows inside a box (pt)."""
    crop = img[int(y * k):int((y + h) * k), int(x * k):int((x + w) * k)]
    inked = (crop < 128).any(axis=1)
    runs: list[tuple[int, int]] = []
    start = None
    for i, on in enumerate(inked.tolist()):
        if on and start is None:
            start = i
        elif not on and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(inked) - 1))
    return runs


def bars(img: np.ndarray, k: float, x: float, y: float, w: float) -> float:
    """Distance (pt) between the inner edges of a row's two bars (probe_advances)."""
    crop = img[int((y + 7) * k):int((y + 22) * k), int(x * k):int((x + w) * k)]
    cols = np.where((crop < 128).any(axis=0))[0].tolist()
    left, right = cols[0], cols[-1]
    while left + 1 in cols:
        left += 1
    while right - 1 in cols:
        right -= 1
    return (right - left) / k


def styled(oid: str, page: str, x: float, y: float, w: float, h: float, text: str, font: str,
           size: float) -> list[SlidesRequest]:
    return [text_box(oid, page, x, y, w, h),
            {"insertText": {"objectId": oid, "text": text}},
            {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                 "fields": "fontFamily,fontSize,bold,italic,foregroundColor",
                                 "style": {"fontFamily": font, "fontSize": pt(size), "bold": False, "italic": False,
                                           "foregroundColor": {"opaqueColor": {"rgbColor": {}}}}}},
            {"updateParagraphStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                      "style": {"lineSpacing": 100, "spaceAbove": pt(0), "spaceBelow": pt(0)},
                                      "fields": "lineSpacing,spaceAbove,spaceBelow"}}]


# Where each measurement sits on a face's slide (pt): pangram rows, pitch paragraphs, ink rows.
ROWS = {"english": (10.0, 10.0), "russian": (10.0, 40.0), "reference": (10.0, 70.0)}
PITCH_BOXES = {"latin": (10.0, 105.0), "cyrillic": (175.0, 105.0)}
INK_BOXES = {"x_latin": (350.0, 100.0, "xzxzxz"), "x_cyrillic": (350.0, 166.0, "нхнхнх"),
             "cap_latin": (350.0, 232.0, "HHHHHH"), "cap_cyrillic": (350.0, 298.0, "НННННН")}
ROW_W, PITCH_W, PITCH_H, INK_W, INK_H = 700.0, 150.0, 290.0, 340.0, 64.0


def requests_for(font: str, page: str, n: int) -> list[SlidesRequest]:
    reqs: list[SlidesRequest] = []
    for key, text in (("english", ENGLISH), ("russian", RUSSIAN), ("reference", "")):
        x, y = ROWS[key]
        reqs += styled(f"cyr_{n}_{key}", page, x, y, ROW_W, 26.0, "|  " + text + "  |", font, SIZE)
    for key, letter in (("latin", "HHH"), ("cyrillic", "ННН")):
        x, y = PITCH_BOXES[key]
        reqs += styled(f"cyr_{n}_p{key}", page, x, y, PITCH_W, PITCH_H, SOFT_BREAK.join([letter] * LINES), font,
                       PITCH_SIZE)
    for key, (x, y, text) in INK_BOXES.items():
        reqs += styled(f"cyr_{n}_{key}", page, x, y, INK_W, INK_H, text, font, INK_SIZE)
    return reqs


def measure(font: str, path: Path) -> Measured:
    img = np.asarray(Image.open(path).convert("RGB")).mean(axis=2)
    k = img.shape[1] / 720
    reference = bars(img, k, *ROWS["reference"], ROW_W)

    def row(key: str) -> float:
        return (bars(img, k, *ROWS[key], ROW_W) - reference) / SIZE

    def pitch(key: str) -> float:
        tops = [top for top, _ in ink_rows(img, k, *PITCH_BOXES[key], PITCH_W, PITCH_H)]
        if len(tops) != LINES:
            raise SystemExit(f"{font} {key}: {len(tops)} lines found, {LINES} written ({path})")
        return statistics.median(b - a for a, b in zip(tops, tops[1:])) / k / PITCH_SIZE

    def ink(key: str) -> float:
        x, y, _ = INK_BOXES[key]
        runs = ink_rows(img, k, x, y, INK_W, INK_H)
        return (max(b for _, b in runs) - min(a for a, _ in runs) + 1) / k / INK_SIZE

    return Measured(font=font, russian=row("russian"), english=row("english"), pitch_latin=pitch("latin"),
                    pitch_cyrillic=pitch("cyrillic"), x_latin=ink("x_latin"), x_cyrillic=ink("x_cyrillic"),
                    cap_latin=ink("cap_latin"), cap_cyrillic=ink("cap_cyrillic"))


@dataclass(frozen=True, kw_only=True)
class Row:
    """One face against SFSS1000: its size factor for Russian, pitch ratio, width-matched heights."""
    font: str
    factor: float          # Slides em per PDF em over the Russian pangram
    pitch_ratio: float     # Cyrillic line pitch over Latin line pitch
    pitch_latin_em: float
    x_height: float        # PDF em, at the width-matched size
    cap_height: float
    x_off: float           # against SFSS1000's, relative
    cap_off: float
    russian_em: float
    english_em: float


def row_of(m: Measured, face: Sfss) -> Row:
    factor = m.russian / pdf_em(RUSSIAN, face)
    return Row(font=m.font, factor=round(factor, 4), pitch_ratio=round(m.pitch_cyrillic / m.pitch_latin, 4),
               pitch_latin_em=round(m.pitch_latin, 4), x_height=round(m.x_cyrillic / factor, 4),
               cap_height=round(m.cap_cyrillic / factor, 4),
               x_off=round(m.x_cyrillic / factor / face.x_height - 1, 4),
               cap_off=round(m.cap_cyrillic / factor / face.cap_height - 1, 4),
               russian_em=round(m.russian, 4), english_em=round(m.english, 4))


def one_box(r: Row) -> bool:
    """Latin and Cyrillic lines of this face share one line pitch (within 1%)."""
    return abs(r.pitch_ratio - 1) <= 0.01


def verdict(rows: list[Row]) -> str:
    """The decision the module docstring states; rows[0] is Lato."""
    lato = rows[0]
    named = [r for r in rows[1:] if one_box(r)]
    best = min(named, key=lambda r: abs(r.x_off) + abs(r.cap_off)) if named else None
    if one_box(lato) and (best is None or abs(lato.x_off) - abs(best.x_off) <= 0.02):
        return ("keep Lato: its Cyrillic shares Latin's line box and no face is nearer SFSS's x-height by more "
                "than 2% - the overflow is not the font's")
    if best is None:
        return "no face keeps one line box for Latin and Cyrillic: keep Lato and model the fallback's pitch"
    return (f"set Cyrillic runs in {best.font} at factor {best.factor} (line pitch {best.pitch_latin_em} em; "
            f"Lato's Cyrillic pitch ratio {lato.pitch_ratio})")


def main() -> None:
    face = read_sfss(miktex_afm())
    OUT.mkdir(parents=True, exist_ok=True)
    slides = slides_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe cyrillic fonts"}))
    pid = presentation_id(pres)
    slide = pres.get("slides", [])[0]
    first = object_id(slide)
    reqs: list[SlidesRequest] = [{"deleteObject": {"objectId": object_id(e)}} for e in slide.get("pageElements", [])]
    pages = [first] + [f"page_{i}" for i in range(1, len(FONTS))]
    reqs += [{"createSlide": {"objectId": p}} for p in pages[1:]]
    for n, (font, page) in enumerate(zip(FONTS, pages)):
        reqs += requests_for(font, page, n)
    try:
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
        found: list[Measured] = []
        for font, page in zip(FONTS, pages):
            path = OUT / f"{font.replace(' ', '')}.png"
            save_thumbnail(slides, pid, page, path, None)
            found.append(measure(font, path))
            print(found[-1])
    finally:
        execute(drive_service(None).files().delete(fileId=pid))
    rows = [row_of(m, face) for m in found]
    decision = verdict(rows)
    for r in rows:
        print(r)
    print("decision:", decision)
    (OUT / "result.json").write_text(json.dumps({
        "source": "tools/probe_cyrillic_fonts.py", "sfss1000": {"x_height": face.x_height, "cap_height": face.cap_height,
                                                               "russian_em": round(pdf_em(RUSSIAN, face), 4)},
        "faces": [asdict(r) for r in rows], "decision": decision}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    print("wrote", OUT / "result.json")


if __name__ == "__main__":
    main()
