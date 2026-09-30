"""Capture a Google Slides template: full JSON, a per-layout summary and slide thumbnails.

Works on any deck the account can read, including public ones. The summary lists every
layout with its placeholders (type, box in pt, resolved font/size/colour) and decoration
shapes, and which sample slides use it. Used to rebuild Google templates as beamer themes.

Usage: python tools/theme_capture.py <presentationId> <name> [--no-thumbs]
Output: out/templates/<name>/{presentation.json, layouts.md, slides/NNN.png}
"""

import argparse
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from beamer2slides.google_auth import credentials, slides_service
from beamer2slides.google_types import (
    AffineTransform, Dimension, LayoutProperties, Page, PageElement, Presentation, SlideProperties, Size,
    background_fill, children, object_id, part, parts,
)
from beamer2slides.gslides import EMU_PER_PT, execute, save_thumbnail
from beamer2slides.json_types import Json, JsonObject, JsonShapeError, as_object, as_str

OUT = Path(__file__).resolve().parents[1] / "out" / "templates"

Scheme = Mapping[str, str]
"""A master's colour scheme: theme colour name -> "#rrggbb"."""


def number(v: Json, where: str) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    raise JsonShapeError(f"{where}: a number was expected, found {type(v).__name__}")


def rgb_hex(rgb: JsonObject) -> str:
    return "#" + "".join(f"{round(number(rgb.get(k, 0), 'rgbColor.' + k) * 255):02x}" for k in ("red", "green", "blue"))


def hex_color(c: Json, scheme: Scheme) -> str | None:
    if not c:
        return None
    c = as_object(c, "a colour")
    if "themeColor" in c:
        theme = as_str(c["themeColor"], "themeColor")
        return f"{theme}={scheme.get(theme, '?')}"
    return rgb_hex(part(c.get("rgbColor"), "rgbColor"))


def fill_color(fill: JsonObject | None, scheme: Scheme) -> str | None:
    if not fill or fill.get("propertyState") == "NOT_RENDERED":
        return None
    if "solidFill" in fill:
        s = as_object(fill["solidFill"], "solidFill")
        alpha = number(s.get("alpha", 1), "solidFill.alpha")
        colour = hex_color(s.get("color"), scheme)
        if colour is None:
            raise JsonShapeError("a solidFill without its colour")
        return colour + ("" if alpha == 1 else f" a={alpha:.2f}")
    if "stretchedPictureFill" in fill:
        return "picture"
    return None


Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)  # scaleX, shearY, shearX, scaleY, translateX, translateY (pt)


def matrix(el: PageElement, parent: Matrix) -> Matrix:
    """The element's affine transform in page pt; group children are relative to their group."""
    t = el.get("transform", AffineTransform())
    k = EMU_PER_PT if t.get("unit", "EMU") == "EMU" else 1
    # Zero fields are omitted in the JSON: a rotated element has shears and no scales.
    a, b, c, d = t.get("scaleX", 0), t.get("shearY", 0), t.get("shearX", 0), t.get("scaleY", 0)
    e, f = t.get("translateX", 0) / k, t.get("translateY", 0) / k
    pa, pb, pc, pd, pe, pf = parent
    return (pa * a + pc * b, pb * a + pd * b, pa * c + pc * d, pb * c + pd * d,
            pa * e + pc * f + pe, pb * e + pd * f + pf)


def box(el: PageElement, m: Matrix) -> tuple[float, float, float, float]:
    size = el.get("size", Size())
    w = size.get("width", Dimension()).get("magnitude", 0) / EMU_PER_PT
    h = size.get("height", Dimension()).get("magnitude", 0) / EMU_PER_PT
    xs = [m[4] + u * m[0] + v * m[2] for u, v in ((0, 0), (w, 0), (0, h), (w, h))]
    ys = [m[5] + u * m[1] + v * m[3] for u, v in ((0, 0), (w, 0), (0, h), (w, h))]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)  # bounding box


def text_elements(shape: JsonObject) -> list[JsonObject]:
    return parts(part(shape.get("text"), "shape.text").get("textElements"), "text.textElements")


def first_style(shape: JsonObject) -> tuple[JsonObject, JsonObject]:
    run: JsonObject = {}
    para: JsonObject = {}
    for te in text_elements(shape):
        if "paragraphMarker" in te and not para:
            para = part(as_object(te["paragraphMarker"], "paragraphMarker").get("style"), "paragraphMarker.style")
        text_run = part(te.get("textRun"), "textRun")
        if "textRun" in te and as_str(text_run.get("content", ""), "textRun.content").strip():
            run = part(text_run.get("style"), "textRun.style")
            break
    return run, para


def text_of(shape: JsonObject) -> str:
    return "".join(as_str(part(te.get("textRun"), "textRun").get("content", ""), "textRun.content")
                   for te in text_elements(shape)).strip()


def describe(el: PageElement, scheme: Scheme, indent: str, parent: Matrix) -> list[str]:
    m = matrix(el, parent)
    x, y, w, h = box(el, m)
    geo = f"({x:.1f}, {y:.1f}) {w:.1f}x{h:.1f}"
    if "elementGroup" in el:
        lines = [f"{indent}group"]
        for child in children(el, object_id(el)):
            lines += describe(child, scheme, indent + "  ", m)
        return lines
    if "image" in el:
        return [f"{indent}image {geo}"]
    line = el.get("line")
    if line is not None:
        lp = part(line.get("lineProperties"), "lineProperties")
        solid = part(part(lp.get("lineFill"), "lineFill").get("solidFill"), "lineFill.solidFill")
        c = hex_color(solid.get("color"), scheme)
        return [f"{indent}line {geo} {c} w={part(lp.get('weight'), 'weight').get('magnitude')}"]
    if "table" in el:
        return [f"{indent}table {geo}"]
    shape = el.get("shape")
    if not shape:
        return [f"{indent}{next(iter(k for k in el if k not in ('objectId', 'size', 'transform')), '?')} {geo}"]
    props = part(shape.get("shapeProperties"), "shapeProperties")
    ph = part(shape.get("placeholder"), "placeholder")
    kind = f"PH {ph['type']}#{ph.get('index', 0)}" if ph else shape.get("shapeType", "?")
    run, para = first_style(shape)
    style: list[str] = []
    if run:
        wff = part(run.get("weightedFontFamily"), "weightedFontFamily")
        fam = wff.get("fontFamily") or run.get("fontFamily")
        style.append(f"{fam} {wff.get('weight', '')}".strip())
        if run.get("fontSize"):
            style.append(f"{as_object(run['fontSize'], 'fontSize')['magnitude']}pt")
        fg = hex_color(part(run.get("foregroundColor"), "foregroundColor").get("opaqueColor"), scheme)
        if fg:
            style.append(fg)
        for flag in ("bold", "italic"):
            if run.get(flag):
                style.append(flag)
    if para.get("alignment"):
        style.append(as_str(para["alignment"], "alignment"))
    if para.get("lineSpacing"):
        style.append(f"ls={para['lineSpacing']}")
    fill = fill_color(part(props.get("shapeBackgroundFill"), "shapeBackgroundFill"), scheme)
    if fill:
        style.append(f"fill={fill}")
    outline = part(props.get("outline"), "outline")
    if outline.get("propertyState") != "NOT_RENDERED" and outline.get("outlineFill"):
        solid = part(as_object(outline["outlineFill"], "outlineFill").get("solidFill"), "outlineFill.solidFill")
        oc = hex_color(solid.get("color"), scheme)
        if oc:
            style.append(f"outline={oc}")
    if props.get("contentAlignment"):
        style.append(f"valign={props['contentAlignment']}")
    txt = text_of(shape).replace("\n", " / ")
    return [f"{indent}{kind} {geo} {' '.join(style)}" + (f"  \"{txt[:70]}\"" if txt else "")]


def layout_of(slide: Page) -> str:
    """The id of the layout a slide is made from."""
    layout = slide.get("slideProperties", SlideProperties()).get("layoutObjectId")
    if layout is None:
        raise JsonShapeError(f"slide {object_id(slide)} without its layoutObjectId")
    return layout


def scheme_of(schemes: Mapping[str, Scheme], master: str | None) -> Scheme:
    return schemes.get(master, {}) if master is not None else {}


def summarize(p: Presentation) -> str:
    size = p.get("pageSize", Size())
    width = size.get("width", Dimension()).get("magnitude", 0)
    height = size.get("height", Dimension()).get("magnitude", 0)
    slides, layouts = p.get("slides", []), p.get("layouts", [])
    out = [f"# {p.get('title')}", "",
           f"Page {width / EMU_PER_PT:.0f} x {height / EMU_PER_PT:.0f} pt, "
           f"{len(slides)} slides, {len(layouts)} layouts", ""]
    schemes: dict[str, Scheme] = {}
    for m in p.get("masters", []):
        colours = parts(part(part(m.get("pageProperties"), "pageProperties").get("colorScheme"), "colorScheme")
                        .get("colors"), "colorScheme.colors")
        scheme = {as_str(c["type"], "colorScheme type"): rgb_hex(as_object(c["color"], "colorScheme color"))
                  for c in colours}
        schemes[object_id(m)] = scheme
        out += [f"## Master {part(m.get('masterProperties'), 'masterProperties').get('displayName')} ({object_id(m)})",
                "colours: " + ", ".join(f"{k}={v}" for k, v in scheme.items()),
                f"background: {fill_color(background_fill(m), scheme)}"]
        for el in m.get("pageElements", []):
            out += describe(el, scheme, "  ", IDENTITY)
        out.append("")
    users: dict[str, list[int]] = {}
    for i, s in enumerate(slides, 1):
        users.setdefault(layout_of(s), []).append(i)
    for lay in layouts:
        props = lay.get("layoutProperties", LayoutProperties())
        scheme = scheme_of(schemes, props.get("masterObjectId"))
        out += [f"## Layout {props.get('displayName')} [{props.get('name')}] ({object_id(lay)})",
                f"master {props.get('masterObjectId')}; background "
                f"{fill_color(background_fill(lay), scheme)}; "
                f"slides {users.get(object_id(lay), [])}"]
        for el in lay.get("pageElements", []):
            out += describe(el, scheme, "  ", IDENTITY)
        out.append("")
    out.append("## Slides")
    for i, s in enumerate(slides, 1):
        lay = next((l for l in layouts if object_id(l) == layout_of(s)), None)
        scheme = scheme_of(schemes, s.get("slideProperties", SlideProperties()).get("masterObjectId"))
        shown = lay.get("layoutProperties", LayoutProperties()).get("displayName") if lay else "?"
        out.append(f"### {i:03d} ({shown}) background {fill_color(background_fill(s), scheme)}")
        for el in s.get("pageElements", []):
            out += describe(el, scheme, "  ", IDENTITY)
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("presentation_id")
    ap.add_argument("name")
    ap.add_argument("--no-thumbs", action="store_true")
    args = ap.parse_args()
    pid: str = args.presentation_id
    folder = OUT / args.name
    folder.mkdir(parents=True, exist_ok=True)
    slides = slides_service(None)
    p = execute(slides.presentations().get(presentationId=pid))
    (folder / "presentation.json").write_text(json.dumps(p, indent=1), encoding="utf-8")
    (folder / "layouts.md").write_text(summarize(p), encoding="utf-8")
    print(f"{p.get('title')}: {len(p.get('slides', []))} slides, {len(p.get('layouts', []))} layouts -> {folder}")
    if args.no_thumbs:
        return
    creds = credentials()

    def thumb(item: tuple[int, Page]) -> None:
        i, s = item
        path = folder / "slides" / f"{i:03d}.png"
        if not path.exists():
            save_thumbnail(slides_service(creds), pid, object_id(s), path, None)

    with ThreadPoolExecutor(3) as pool:
        list(pool.map(thumb, enumerate(p.get("slides", []), 1)))
    print("thumbnails done")


if __name__ == "__main__":
    main()
