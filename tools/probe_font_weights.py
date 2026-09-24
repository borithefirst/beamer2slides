"""Probe: which face Slides draws for a family at a weight, and whether it serves a family by name.

The visual hunt (r8) found the 6 pt footlines written at Lato 600 (`emit.OPTICAL_WEIGHT`) drawn as
light as Lato 400, and Japanese in Noto Sans JP still heavier than the PDF. This sets one line per
(family, weight) and reads back, from Google's thumbnail, the ink width and the ink per pt of line
(darkness), and what the API says the run's font is. A family Slides does not serve falls back
silently: its row then measures like the fallback's.

Usage: python tools/probe_font_weights.py [--refresh]   (writes out/probe_font_weights/)
"""

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.google_auth import slides_service
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_font_weights"
SIZE = 24.0
K = 1600 / 720  # thumbnail px per pt
ROW_H = 34
LATIN = "Hamburgefonstiv M. Keller June 2026"
CJK = "日本語の文章（医療）？分類"
TRIANGLES = "▶ ▸ ► ‣ ■ □"   # beamer's triangle bullets, and squares (K2's bullet findings)
ROWS = [  # (family, weight, text)
    ("Lato", 300, LATIN), ("Lato", 400, LATIN), ("Lato", 500, LATIN), ("Lato", 600, LATIN),
    ("Lato", 700, LATIN), ("Lato", 800, LATIN), ("Lato", 900, LATIN),
    ("Noto Sans JP", 400, CJK), ("Noto Sans JP", 700, CJK), ("Noto Serif JP", 400, CJK),
    ("Lato", 400, CJK), ("M PLUS 1p", 400, CJK), ("Noto Sans SC", 400, CJK), ("Noto Sans KR", 400, CJK),
    ("Noto Sans JP", 100, CJK), ("Noto Sans JP", 300, CJK), ("Noto Sans JP", 500, CJK), ("Noto Serif JP", 300, CJK),
    ("Lato", 400, TRIANGLES), ("Noto Sans Symbols 2", 400, TRIANGLES), ("Noto Sans Symbols", 400, TRIANGLES),
    # families Google Fonts serves with a medium between Regular and Bold (Lato has none: 500/600 draw 400)
    ("Fira Sans", 400, LATIN), ("Fira Sans", 500, LATIN), ("Roboto", 400, LATIN), ("Roboto", 500, LATIN),
    ("Source Sans 3", 400, LATIN), ("Source Sans 3", 600, LATIN), ("Open Sans", 400, LATIN), ("Open Sans", 600, LATIN),
    ("Noto Sans", 400, LATIN), ("Noto Sans", 500, LATIN), ("Noto Sans", 600, LATIN), ("Carlito", 400, LATIN),
    ("PT Sans", 400, LATIN), ("Inter", 500, LATIN),
    # r9: a 6 pt CM sans footline's stroke is 1.06 of Lato Regular's (1.15 against the body text),
    # Source Sans 3 600 width-matched 1.31, Lato Bold 1.34: is a served 500 in between?
    ("Source Sans 3", 500, LATIN), ("Source Sans 3", 700, LATIN),
]
PER_PAGE = 7


def ink(gray: np.ndarray) -> dict | None:
    dark = gray < 128
    ys, xs = np.where(dark)
    if not len(xs):
        return None
    return {"x0": round(xs.min() / K, 2), "x1": round((xs.max() + 1) / K, 2),
            "height": round((ys.max() + 1 - ys.min()) / K, 2),
            "darkness": round(float((255 - gray).sum()) / 255 / (K * K) / SIZE, 3)}


def slides_side(refresh: bool) -> dict:
    cache = OUT / "slides.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    slides = slides_service()
    pres = execute(slides.presentations().create(body={"title": "b2s probe font weights"}))
    pid = pres["presentationId"]
    reqs = [{"deleteObject": {"objectId": pres["slides"][0]["objectId"]}}]
    pages = []
    for r, (family, weight, text) in enumerate(ROWS):
        if r % PER_PAGE == 0:
            pages.append(f"page_{r // PER_PAGE}")
            reqs.append({"createSlide": {"objectId": pages[-1], "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
        oid = f"text_{r:02d}"
        reqs += [text_box(oid, pages[-1], 10, 6 + (r % PER_PAGE) * ROW_H * 1.5, 700, ROW_H),
                 {"insertText": {"objectId": oid, "text": text}},
                 {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                      "fields": "weightedFontFamily,fontSize,foregroundColor",
                                      "style": {"weightedFontFamily": {"fontFamily": family, "weight": weight},
                                                "fontSize": pt(SIZE),
                                                "foregroundColor": {"opaqueColor": {"rgbColor": {}}}}}}]
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    back = execute(slides.presentations().get(presentationId=pid))
    styles = {el["objectId"]: next(te["textRun"]["style"] for te in el["shape"]["text"]["textElements"] if "textRun" in te)
              for page in back["slides"] for el in page["pageElements"]}
    result = {"presentation": pid, "rows": []}
    OUT.mkdir(parents=True, exist_ok=True)
    grays = {}
    for page in pages:
        path = OUT / f"{page}.png"
        save_thumbnail(slides, pid, page, path)
        grays[page] = np.asarray(Image.open(path).convert("L"))
    for r, (family, weight, text) in enumerate(ROWS):
        top = 6 + (r % PER_PAGE) * ROW_H * 1.5
        gray = grays[pages[r // PER_PAGE]][int(top * K):int((top + ROW_H) * K)]
        st = styles[f"text_{r:02d}"]
        result["rows"].append({"family": family, "weight": weight, "text": text, "ink": ink(gray),
                               "read_back": {"weightedFontFamily": st.get("weightedFontFamily"), "bold": st.get("bold")}})
    cache.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    res = slides_side(ap.parse_args().refresh)
    print(f"presentation {res['presentation']}; {SIZE:g} pt; width and height in pt, darkness = ink per pt of line")
    for row in res["rows"]:
        i = row["ink"] or {}
        print(f"  {row['family']:14} {row['weight']}  {'cjk' if row['text'] == CJK else 'tri' if row['text'] == TRIANGLES else 'latin':5}"
              f"  width {i.get('x1', 0) - i.get('x0', 0):7.2f}  height {i.get('height', 0):5.2f}"
              f"  darkness {i.get('darkness', 0):6.3f}  read back {row['read_back']}")


if __name__ == "__main__":
    main()
