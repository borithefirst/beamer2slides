"""Probe: how far apart Google's renderer sets the dots of a leader (". . . ."), per font and face
(emit_metrics.LEADER_PITCH_EM; real_defense-defense 41, a PT Serif \\dotfill row 12% short).

ADVANCES' "." plus " " add up to 0.525 em in PT Serif, but a hunt thumbnail measured 0.442 em from
one dot to the next; Lato's 0.403 held (0.405). This sets "H" and DOTS spaced dots, and DOTS
unspaced ones, at SIZE pt in Lato and PT Serif in every face, one box each, and measures the dot
pitch on the LARGE thumbnail: em per ". " and per ".", against what ADVANCES predicts.

Everything it creates is deleted. Usage: python tools/probe_leaders.py
(prints a JSON summary, also written with the thumbnail to out/probe_leaders)
"""

import json
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.arrays import Gray, Mask
from beamer2slides.emit_metrics import ADVANCES
from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.google_types import SlidesRequest, SlidesTextStyle, object_id, presentation_id
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_leaders"
SIZE = 20.0
DOTS = 25  # (one line in a 350 pt box in every face)
FONTS = ("Lato", "PT Serif")
FACES: tuple[tuple[str, bool, bool], ...] = (("regular", False, False), ("bold", True, False),
                                             ("italic", False, True), ("bold_italic", True, True))
SEPARATORS = ((" ", "spaced"), ("", "unspaced"))
ROW = 40.0  # pt per box row


def columns(mask: Mask) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for c in (int(c) for c in np.flatnonzero(mask.any(axis=0))):
        if out and c - out[-1][1] <= 1:
            out[-1] = (out[-1][0], c + 1)
        else:
            out.append((c, c + 1))
    return out


def pitch_em(mask: Mask, k: float) -> float | None:
    """Mean distance from one dot's centre to the next (em of SIZE) after the leading H."""
    dots = columns(mask)[1:]
    if len(dots) < 2:
        return None
    centres = [(a + b) / 2 for a, b in dots]
    return round((centres[-1] - centres[0]) / (len(centres) - 1) / k / SIZE, 4)


def main() -> None:
    slides, drive = slides_service(None), drive_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe leaders"}))
    pid = presentation_id(pres)
    try:
        page = object_id(pres.get("slides", [])[0])
        reqs: list[SlidesRequest] = [{"deleteObject": {"objectId": object_id(e)}}
                                     for e in pres.get("slides", [])[0].get("pageElements", [])]
        boxes: dict[str, tuple[float, float, float, float]] = {}
        for i, (font, (face, bold, italic), (sep, how)) in enumerate(
                (f, fc, s) for f in FONTS for fc in FACES for s in SEPARATORS):
            oid = f"leader_{i:02}"
            x, y, w = 10 + (i % 2) * 355, 10 + (i // 2) * ROW, 350
            text = "H" + sep.join("." * DOTS)
            st: SlidesTextStyle = {"fontFamily": font, "fontSize": pt(SIZE), "bold": bold, "italic": italic}
            insert: SlidesRequest = {"insertText": {"objectId": oid, "text": text}}
            styled: SlidesRequest = {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                                         "fields": "fontFamily,fontSize,bold,italic", "style": st}}
            reqs += [text_box(oid, page, x, y, w, ROW - 4), insert, styled]
            boxes[f"{font}|{face}|{how}"] = (x, y, w, ROW - 4)
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / "leaders.png"
        save_thumbnail(slides, pid, page, path, None)
    finally:
        execute(drive.files().delete(fileId=pid))
    img: Gray = np.asarray(Image.open(path).convert("L"))
    k = img.shape[1] / 720
    result: dict[str, dict[str, float | None]] = {}
    for label, (x, y, w, h) in boxes.items():
        font, face, how = label.split("|")
        crop = img[int(y * k):int((y + h) * k), int(x * k):int((x + w) * k)] < 128
        adv = ADVANCES[font][face]
        predicted = adv.get(".", 0.0) + (adv.get(" ", 0.0) if how == "spaced" else 0.0)
        result[label] = {"measured_em": pitch_em(crop, k), "advances_em": round(predicted, 4)}
    text = json.dumps(result, indent=1)
    print(text)
    (OUT / "result.json").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
