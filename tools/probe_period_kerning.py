"""Probe: which side of a period Slides' PT Serif kerns a space against (emit_metrics.LEADER_PITCH_EM).

tools/probe_leaders.py measured PT Serif's dots of ". . ." about 0.1 em closer than ADVANCES' "."
plus " " in every face, and dots with no space 3-6% further apart than ADVANCES' "." (whose rows of
eight periods between spaces fold the space kerns into it). Lato kerns none. Whether the pair is
". " (every sentence end in a serif deck) or " ." (leaders and spaced ellipses only) decides
whether ADVANCES needs a pair model. Each box repeats a unit 12 times between H's, at SIZE pt:

  HH     "H" * 13         H + kern(HH)
  H.H    "H" + ".H" * 12  H + . + kern(H.) + kern(.H)
  H H    "H" + " H" * 12  H + space + kern(H ) + kern( H)
  H. H   "H" + ". H" * 12 H + . + space + kern(H.) + kern(. ) + kern( H)
  H .H   "H" + " .H" * 12 H + space + . + kern(H ) + kern( .) + kern(.H)

measured as the distance from the first H's left stem to the last's, over 12. With H's kerns
about nil, kern(". ") = "H. H" + "HH" - "H.H" - "H H", and kern(" .") = "H .H" + "HH" - "H.H" - "H H".

Everything it creates is deleted. Usage: python tools/probe_period_kerning.py
(prints a JSON summary, also written with the thumbnail to out/probe_period_kerning)
"""

import json
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.arrays import Gray, Mask
from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.google_types import SlidesRequest, SlidesTextStyle, object_id, presentation_id
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_period_kerning"
SIZE = 18.0
REPEAT = 12
FONTS = ("PT Serif", "Lato")
FACES: tuple[tuple[str, bool, bool], ...] = (("regular", False, False), ("bold", True, False),
                                             ("italic", False, True), ("bold_italic", True, True))
UNITS = (("HH", "H"), ("H.H", ".H"), ("H H", " H"), ("H. H", ". H"), ("H .H", " .H"))
ROW = 29.0  # pt per box row (25 boxes, two a row: 13 rows on the slide)
COLUMNS = 2
BOX_W = 355.0  # (the widest row, PT Serif bold "H. H", about 310 pt)


def runs(flags: np.ndarray) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for c in (int(c) for c in np.flatnonzero(flags)):
        if out and c - out[-1][1] <= 1:
            out[-1] = (out[-1][0], c + 1)
        else:
            out.append((c, c + 1))
    return out


def unit_em(mask: Mask, k: float) -> float | None:
    """The distance from the first H's left stem to the last's over REPEAT (em of SIZE): the stems
    are what the row a third down the H's cap height crosses (a period stays at the baseline)."""
    ys = np.flatnonzero(mask.any(axis=1))
    if not len(ys):
        return None
    top, bottom = int(ys[0]), int(ys[-1])
    stems = runs(mask[top + (bottom - top) // 3])
    if len(stems) != 2 * (REPEAT + 1):
        return None
    return round((stems[-2][0] - stems[0][0]) / REPEAT / k / SIZE, 4)


def main() -> None:
    slides, drive = slides_service(None), drive_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe period kerning"}))
    pid = presentation_id(pres)
    try:
        page = object_id(pres.get("slides", [])[0])
        reqs: list[SlidesRequest] = [{"deleteObject": {"objectId": object_id(e)}}
                                     for e in pres.get("slides", [])[0].get("pageElements", [])]
        boxes: dict[str, tuple[float, float, float, float]] = {}
        jobs = [(f, fc, u) for f in FONTS for fc in FACES for u in UNITS if f == "PT Serif" or fc[0] == "regular"]
        for i, (font, (face, bold, italic), (label, unit)) in enumerate(jobs):
            oid = f"kern_{i:02}"
            x, y = 4 + (i % COLUMNS) * (BOX_W + 2), 4 + (i // COLUMNS) * ROW
            st: SlidesTextStyle = {"fontFamily": font, "fontSize": pt(SIZE), "bold": bold, "italic": italic}
            insert: SlidesRequest = {"insertText": {"objectId": oid, "text": "H" + unit * REPEAT}}
            styled: SlidesRequest = {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                                         "fields": "fontFamily,fontSize,bold,italic", "style": st}}
            reqs += [text_box(oid, page, x, y, BOX_W, ROW - 2), insert, styled]
            boxes[f"{font}|{face}|{label}"] = (x, y, BOX_W, ROW - 2)
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / "kerning.png"
        save_thumbnail(slides, pid, page, path, None)
    finally:
        execute(drive.files().delete(fileId=pid))
    img: Gray = np.asarray(Image.open(path).convert("L"))
    k = img.shape[1] / 720
    units: dict[str, dict[str, float | None]] = {}
    for key, (x, y, w, h) in boxes.items():
        font, face, label = key.split("|")
        crop = img[int(y * k):int((y + h) * k), int(x * k):int((x + w) * k)] < 128
        units.setdefault(f"{font}|{face}", {})[label] = unit_em(crop, k)
    result: dict[str, dict[str, float | None]] = {}
    for key, u in units.items():
        hh, hdh, hsh = u.get("HH"), u.get("H.H"), u.get("H H")
        out = dict(u)
        for pair, label in (('kern(". ")', "H. H"), ('kern(" .")', "H .H")):
            v = u.get(label)
            out[pair] = None if v is None or hh is None or hdh is None or hsh is None else round(v + hh - hdh - hsh, 4)
        out["period"] = None if hdh is None or hh is None else round(hdh - hh, 4)
        out["space"] = None if hsh is None or hh is None else round(hsh - hh, 4)
        result[key] = out
    text = json.dumps(result, indent=1)
    print(text)
    (OUT / "result.json").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
