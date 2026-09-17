"""Capture a Google Slides template: full JSON, a per-layout summary and slide thumbnails.

Works on any deck the account can read, including public ones. The summary lists every
layout with its placeholders (type, box in pt, resolved font/size/colour) and decoration
shapes, and which sample slides use it. Used to rebuild Google templates as beamer themes.

Usage: python tools/theme_capture.py <presentationId> <name> [--no-thumbs]
Output: out/templates/<name>/{presentation.json, layouts.md, slides/NNN.png}
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from beamer2slides.google_auth import credentials, slides_service
from beamer2slides.gslides import EMU_PER_PT, execute, save_thumbnail

OUT = Path(__file__).resolve().parents[1] / "out" / "templates"


def hex_color(c: dict | None, scheme: dict[str, str]) -> str | None:
    if not c:
        return None
    if "themeColor" in c:
        return f"{c['themeColor']}={scheme.get(c['themeColor'], '?')}"
    rgb = c.get("rgbColor", {})
    return "#" + "".join(f"{round(rgb.get(k, 0) * 255):02x}" for k in ("red", "green", "blue"))


def fill_color(fill: dict | None, scheme: dict[str, str]) -> str | None:
    if not fill or fill.get("propertyState") == "NOT_RENDERED":
        return None
    if "solidFill" in fill:
        s = fill["solidFill"]
        alpha = s.get("alpha", 1)
        return hex_color(s.get("color"), scheme) + ("" if alpha == 1 else f" a={alpha:.2f}")
    if "stretchedPictureFill" in fill:
        return "picture"
    return None


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)  # scaleX, shearY, shearX, scaleY, translateX, translateY (pt)


def matrix(el: dict, parent: tuple = IDENTITY) -> tuple:
    """The element's affine transform in page pt; group children are relative to their group."""
    t = el.get("transform", {})
    k = EMU_PER_PT if t.get("unit", "EMU") == "EMU" else 1
    # Zero fields are omitted in the JSON: a rotated element has shears and no scales.
    a, b, c, d = t.get("scaleX", 0), t.get("shearY", 0), t.get("shearX", 0), t.get("scaleY", 0)
    e, f = t.get("translateX", 0) / k, t.get("translateY", 0) / k
    pa, pb, pc, pd, pe, pf = parent
    return (pa * a + pc * b, pb * a + pd * b, pa * c + pc * d, pb * c + pd * d,
            pa * e + pc * f + pe, pb * e + pd * f + pf)


def box(el: dict, m: tuple) -> tuple[float, float, float, float]:
    size = el.get("size", {})
    w = size.get("width", {}).get("magnitude", 0) / EMU_PER_PT
    h = size.get("height", {}).get("magnitude", 0) / EMU_PER_PT
    xs = [m[4] + u * m[0] + v * m[2] for u, v in ((0, 0), (w, 0), (0, h), (w, h))]
    ys = [m[5] + u * m[1] + v * m[3] for u, v in ((0, 0), (w, 0), (0, h), (w, h))]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)  # bounding box


def first_style(shape: dict) -> tuple[dict, dict]:
    run, para = {}, {}
    for te in shape.get("text", {}).get("textElements", []):
        if "paragraphMarker" in te and not para:
            para = te["paragraphMarker"].get("style", {})
        if "textRun" in te and te["textRun"].get("content", "").strip():
            run = te["textRun"].get("style", {})
            break
    return run, para


def text_of(shape: dict) -> str:
    return "".join(te.get("textRun", {}).get("content", "")
                   for te in shape.get("text", {}).get("textElements", [])).strip()


def describe(el: dict, scheme: dict[str, str], indent: str = "  ", parent: tuple = IDENTITY) -> list[str]:
    m = matrix(el, parent)
    x, y, w, h = box(el, m)
    geo = f"({x:.1f}, {y:.1f}) {w:.1f}x{h:.1f}"
    if "elementGroup" in el:
        lines = [f"{indent}group"]
        for child in el["elementGroup"].get("children", []):
            lines += describe(child, scheme, indent + "  ", m)
        return lines
    if "image" in el:
        return [f"{indent}image {geo}"]
    if "line" in el:
        lp = el["line"].get("lineProperties", {})
        c = hex_color(lp.get("lineFill", {}).get("solidFill", {}).get("color"), scheme)
        return [f"{indent}line {geo} {c} w={lp.get('weight', {}).get('magnitude')}"]
    if "table" in el:
        return [f"{indent}table {geo}"]
    shape = el.get("shape")
    if not shape:
        return [f"{indent}{next(iter(k for k in el if k not in ('objectId', 'size', 'transform')), '?')} {geo}"]
    props = shape.get("shapeProperties", {})
    ph = shape.get("placeholder")
    kind = f"PH {ph['type']}#{ph.get('index', 0)}" if ph else shape.get("shapeType", "?")
    run, para = first_style(shape)
    style = []
    if run:
        wff = run.get("weightedFontFamily", {})
        fam = wff.get("fontFamily") or run.get("fontFamily")
        style.append(f"{fam} {wff.get('weight', '')}".strip())
        if run.get("fontSize"):
            style.append(f"{run['fontSize']['magnitude']}pt")
        fg = hex_color(run.get("foregroundColor", {}).get("opaqueColor"), scheme)
        if fg:
            style.append(fg)
        for flag in ("bold", "italic"):
            if run.get(flag):
                style.append(flag)
    if para.get("alignment"):
        style.append(para["alignment"])
    if para.get("lineSpacing"):
        style.append(f"ls={para['lineSpacing']}")
    fill = fill_color(props.get("shapeBackgroundFill"), scheme)
    if fill:
        style.append(f"fill={fill}")
    outline = props.get("outline", {})
    if outline.get("propertyState") != "NOT_RENDERED" and outline.get("outlineFill"):
        oc = hex_color(outline["outlineFill"].get("solidFill", {}).get("color"), scheme)
        if oc:
            style.append(f"outline={oc}")
    if props.get("contentAlignment"):
        style.append(f"valign={props['contentAlignment']}")
    txt = text_of(shape).replace("\n", " / ")
    return [f"{indent}{kind} {geo} {' '.join(style)}" + (f"  \"{txt[:70]}\"" if txt else "")]


def summarize(p: dict) -> str:
    size = p["pageSize"]
    out = [f"# {p['title']}", "",
           f"Page {size['width']['magnitude'] / EMU_PER_PT:.0f} x {size['height']['magnitude'] / EMU_PER_PT:.0f} pt, "
           f"{len(p['slides'])} slides, {len(p['layouts'])} layouts", ""]
    schemes = {}
    for m in p["masters"]:
        scheme = {c["type"]: hex_color({"rgbColor": c["color"]}, {})
                  for c in m["pageProperties"].get("colorScheme", {}).get("colors", [])}
        schemes[m["objectId"]] = scheme
        out += [f"## Master {m['masterProperties'].get('displayName')} ({m['objectId']})",
                "colours: " + ", ".join(f"{k}={v}" for k, v in scheme.items()),
                f"background: {fill_color(m['pageProperties'].get('pageBackgroundFill'), scheme)}"]
        for el in m.get("pageElements", []):
            out += describe(el, scheme)
        out.append("")
    users: dict[str, list[int]] = {}
    for i, s in enumerate(p["slides"], 1):
        users.setdefault(s["slideProperties"]["layoutObjectId"], []).append(i)
    for lay in p["layouts"]:
        props = lay["layoutProperties"]
        scheme = schemes.get(props.get("masterObjectId"), {})
        out += [f"## Layout {props.get('displayName')} [{props.get('name')}] ({lay['objectId']})",
                f"master {props.get('masterObjectId')}; background "
                f"{fill_color(lay['pageProperties'].get('pageBackgroundFill'), scheme)}; "
                f"slides {users.get(lay['objectId'], [])}"]
        for el in lay.get("pageElements", []):
            out += describe(el, scheme)
        out.append("")
    out.append("## Slides")
    for i, s in enumerate(p["slides"], 1):
        lay = next((l for l in p["layouts"] if l["objectId"] == s["slideProperties"]["layoutObjectId"]), None)
        scheme = schemes.get(s["slideProperties"].get("masterObjectId"), {})
        out.append(f"### {i:03d} ({lay['layoutProperties'].get('displayName') if lay else '?'}) "
                   f"background {fill_color(s['pageProperties'].get('pageBackgroundFill'), scheme)}")
        for el in s.get("pageElements", []):
            out += describe(el, scheme)
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("presentation_id")
    ap.add_argument("name")
    ap.add_argument("--no-thumbs", action="store_true")
    args = ap.parse_args()
    folder = OUT / args.name
    folder.mkdir(parents=True, exist_ok=True)
    slides = slides_service()
    p = execute(slides.presentations().get(presentationId=args.presentation_id))
    (folder / "presentation.json").write_text(json.dumps(p, indent=1), encoding="utf-8")
    (folder / "layouts.md").write_text(summarize(p), encoding="utf-8")
    print(f"{p['title']}: {len(p['slides'])} slides, {len(p['layouts'])} layouts -> {folder}")
    if args.no_thumbs:
        return
    creds = credentials()

    def thumb(item):
        i, s = item
        path = folder / "slides" / f"{i:03d}.png"
        if not path.exists():
            save_thumbnail(slides_service(creds), args.presentation_id, s["objectId"], path)

    with ThreadPoolExecutor(3) as pool:
        list(pool.map(thumb, enumerate(p["slides"], 1)))
    print("thumbnails done")


if __name__ == "__main__":
    main()
