"""Probe: Google Sans / Google Sans Text / Google Sans Mono as rendered by Slides, against
local renderings of the open-source Google Sans Flex and Google Sans Code at several
optical sizes and widths. Finds the variable-font instance whose ink width, x-height and cap
height match each Slides family, so beamer themes can typeset in look-alike fonts.

Needs themes/google/fonts/src/*.ttf (see themes/google/fonts/README.md).
Usage: python tools/probe_google_sans.py [--refresh]
"""

import argparse
import json
from collections.abc import Mapping
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from beamer2slides.arrays import Gray
from beamer2slides.google_auth import slides_service
from beamer2slides.google_types import object_id, part, parts, presentation_id
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box
from beamer2slides.json_types import Json, JsonObject, JsonShapeError, as_array, as_object

Box = tuple[float, float, float, float]

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_google_sans"
SRC = ROOT / "themes" / "google" / "fonts" / "src"
SIZE = 36.0
K = 1600 / 720  # thumbnail px per pt
TEXTS = {"words": "Hamburgefonstiv quick 0123", "x": "xxxxxxxxxxxx", "H": "HHHHHHHHHHHH"}
ROWS = [  # (Slides family, weight)
    ("Google Sans", 400), ("Google Sans", 600), ("Google Sans", 700), ("Google Sans Text", 400),
    ("Google Sans Text", 700), ("Google Sans Mono", 400), ("Google Sans Flex", 400), ("Google Sans Code", 400),
]
ROW_H = 48


def ink(gray: Gray) -> Box | None:
    ys, xs = np.where(gray < 128)
    if not len(xs):
        return None
    return float(xs.min() / K), float(ys.min() / K), float((xs.max() + 1) / K), float((ys.max() + 1) / K)


def inked(b: Box | None, what: str) -> Box:
    """A measurement that must have found ink."""
    if b is None:
        raise ValueError(f"no ink: {what}")
    return b


def number(v: Json, where: str) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    raise JsonShapeError(f"{where}: a number was expected, found {type(v).__name__}")


def ink_of(ref: JsonObject, key: str) -> Box | None:
    """A Slides row's ink as slides_side recorded it (null: none)."""
    v = ref[key]
    if v is None:
        return None
    x0, y0, x1, y1 = (number(c, key) for c in as_array(v, key))
    return x0, y0, x1, y1


def slides_side(refresh: bool) -> JsonObject:
    cache = OUT / "slides.json"
    if cache.exists() and not refresh:
        return as_object(json.loads(cache.read_text()), str(cache))
    slides = slides_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe google sans"}))
    pid = presentation_id(pres)
    first = object_id(pres.get("slides", [])[0])
    reqs: list[Mapping[str, object]] = [{"deleteObject": {"objectId": first}}]
    for s, key in enumerate(TEXTS):
        page = f"page_{key}"
        reqs.append({"createSlide": {"objectId": page, "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
        for r, (family, weight) in enumerate(ROWS):
            oid = f"t_{key}_{r}"
            reqs += [
                text_box(oid, page, 10, 4 + r * ROW_H, 700, ROW_H),
                {"insertText": {"objectId": oid, "text": TEXTS[key]}},
                {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                     "fields": "weightedFontFamily,fontSize,foregroundColor",
                                     "style": {"weightedFontFamily": {"fontFamily": family, "weight": weight},
                                               "fontSize": pt(SIZE),
                                               "foregroundColor": {"opaqueColor": {"rgbColor": {}}}}}},
            ]
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    pres = execute(slides.presentations().get(presentationId=pid))
    result: JsonObject = {"presentation": pid}
    for key in TEXTS:
        path = OUT / f"slides_{key}.png"
        save_thumbnail(slides, pid, f"page_{key}", path, None)
        gray = np.asarray(Image.open(path).convert("L"))
        for r, (family, weight) in enumerate(ROWS):
            y0, y1 = int((4 + r * ROW_H) * K), int((4 + (r + 1) * ROW_H) * K)
            box = ink(gray[y0:y1])
            result[f"{family}|{weight}|{key}"] = None if box is None else list(box)
    # What Slides reports back for each family (unknown families fall back silently).
    for el in next(p for p in pres.get("slides", []) if object_id(p) == "page_words").get("pageElements", []):
        text = part(part(el.get("shape"), "shape").get("text"), "shape.text")
        run = next(as_object(te["textRun"], "textRun") for te in parts(text.get("textElements"), "textElements")
                   if "textRun" in te)
        result["style_" + object_id(el)] = part(run.get("style"), "textRun.style").get("weightedFontFamily")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, indent=1))
    return result


def axis_name(axis: ImageFont.Axis) -> str:
    name = axis["name"]
    if name is None:
        raise ValueError("a variation axis without its name")
    return name.decode() if isinstance(name, bytes) else name


def axis_default(axis: ImageFont.Axis) -> int:
    default = axis["default"]
    if default is None:
        raise ValueError(f"variation axis {axis_name(axis)} without its default")
    return default


def local_ink(path: Path, axes: Mapping[str, int], text: str) -> Box | None:
    font = ImageFont.truetype(str(path), size=round(SIZE * K))
    if axes:
        names = [axis_name(a) for a in font.get_variation_axes()]
        tags = {"Optical Size": "opsz", "Width": "wdth", "Weight": "wght", "Grade": "GRAD",
                "Roundness": "ROND", "Slant": "slnt"}
        defaults = {a_tag: axis_default(a) for a, a_tag in
                    zip(font.get_variation_axes(), [tags.get(n, n) for n in names])}
        values: list[float] = [axes.get(tags.get(n, n), defaults[tags.get(n, n)]) for n in names]
        font.set_variation_by_axes(values)
    img = Image.new("L", (int(760 * K), int(ROW_H * K)), 255)
    ImageDraw.Draw(img).text((10, 10), text, font=font, fill=0)
    return ink(np.asarray(img))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    ref = slides_side(args.refresh)
    print("Slides styles:", {k: v for k, v in ref.items() if k.startswith("style_")})
    print("\nSlides (pt at 36 pt): width of words, x-height, cap height")
    for family, weight in ROWS:
        w = ink_of(ref, f"{family}|{weight}|words")
        if w:
            x, h = (inked(ink_of(ref, f"{family}|{weight}|{k}"), f"{family} {weight} {k}") for k in ("x", "H"))
            print(f"  {family:18} {weight}: width {w[2] - w[0]:7.2f}  x {x[3] - x[1]:5.2f}  H {h[3] - h[1]:5.2f}")

    flex = SRC / "GoogleSansFlex-VF.ttf"
    print("\nGoogle Sans Flex instances: (opsz, wdth, wght) -> width, x, H; best match per Slides family")
    candidates: list[tuple[dict[str, int], float, float, float]] = []
    for opsz in (6, 9, 12, 14, 18, 24, 36, 48, 72, 144):
        for wdth in (96, 98, 100, 102, 104):
            for wght in (400, 600, 700):
                axes = {"opsz": opsz, "wdth": wdth, "wght": wght}
                m = {k: inked(local_ink(flex, axes, t), f"Flex {axes} {k}") for k, t in TEXTS.items()}
                candidates.append((axes, m["words"][2] - m["words"][0], m["x"][3] - m["x"][1], m["H"][3] - m["H"][1]))
    for family, weight in ROWS[:5]:
        w, x, h = (inked(ink_of(ref, f"{family}|{weight}|{k}"), f"{family} {weight} {k}") for k in TEXTS)
        tw, tx, th = w[2] - w[0], x[3] - x[1], h[3] - h[1]
        same = [c for c in candidates if c[0]["wght"] == weight]
        best = sorted(same, key=lambda c: abs(c[1] / tw - 1) * 3 + abs(c[2] / tx - 1) + abs(c[3] / th - 1))[:4]
        print(f"  {family} {weight}: target width {tw:.1f} x {tx:.2f} H {th:.2f}")
        for axes, cw, cx, ch in best:
            print(f"     {axes}  width {cw / tw:.4f}  x {cx / tx:.3f}  H {ch / th:.3f}")
    code = SRC / "GoogleSansCode-VF.ttf"
    for wght in (400, 500):
        m = {k: inked(local_ink(code, {"wght": wght}, t), f"Code {wght} {k}") for k, t in TEXTS.items()}
        w, x, h = (inked(ink_of(ref, f"Google Sans Mono|400|{k}"), f"Google Sans Mono {k}") for k in TEXTS)
        print(f"  Google Sans Code {wght} vs Google Sans Mono 400: width {(m['words'][2] - m['words'][0]) / (w[2] - w[0]):.4f}"
              f"  x {(m['x'][3] - m['x'][1]) / (x[3] - x[1]):.3f}  H {(m['H'][3] - m['H'][1]) / (h[3] - h[1]):.3f}")


if __name__ == "__main__":
    main()
