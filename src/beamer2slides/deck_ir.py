"""A live Google Slides deck as IR (deck.json-shaped), for comparison with classify's output.

`deck_ir(pres, ...)` is pure (the `presentations.get` JSON in, IR out); `read_deck` fetches the
presentation and downloads its pictures. Positions go back to PDF pt through emit's text box
model: a box's text starts PAD_X inside it and its first baseline sits BASELINE_A + ASCENT_EM x size
below its top (MIDDLE boxes: MIDDLE_BASELINE_EM below the middle), so `anchor` = (x by alignment,
first baseline) / scale is the same point classify's paragraphs give (compare.text_anchor).
Font sizes go back through FontMapper's width factors (`pdf_size`).

Slide keys: the `b2s:<slide key>/<element key>` alt-text title of our objects, else a base
snapshot's object map, else none (compare aligns slides by title and text).
"""

import hashlib
import json
import re
import urllib.request
from pathlib import Path

from .emit import (ASCENT_EM, BASELINE_A, FONT_FOR_FAMILY, MIDDLE_BASELINE_EM, PAD_X, PPTX_TITLE_DY, SLIDE_W,
                   FontMapper, extra_above)
from .gslides import EMU_PER_PT

FAMILY_FOR_FONT = {v: k for k, v in FONT_FOR_FAMILY.items()}
TAG_RE = re.compile(r"^b2s:(?P<slide>[^/]*)/(?P<element>.+)$")
BEAMER_SIZES = {(4, 3): (362.83, 272.13), (16, 9): (453.54, 255.12), (16, 10): (453.54, 283.46)}
DEFAULT_STYLE = {"fontFamily": "Arial", "fontSize": 18.0, "bold": False, "italic": False, "color": "#000000"}


def dim(d: dict | None) -> float:
    if not d or "magnitude" not in d:
        return 0.0
    return d["magnitude"] / (EMU_PER_PT if d.get("unit", "EMU") == "EMU" else 1)


def affine(t: dict | None) -> list[float]:
    """[a, b, tx, d, e, ty] in pt; fields the API leaves out are zero, a missing transform is identity."""
    if not t:
        return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    unit = EMU_PER_PT if t.get("unit", "EMU") == "EMU" else 1.0
    return [t.get("scaleX", 0.0), t.get("shearX", 0.0), t.get("translateX", 0.0) / unit,
            t.get("shearY", 0.0), t.get("scaleY", 0.0), t.get("translateY", 0.0) / unit]


def compose(p: list[float], c: list[float]) -> list[float]:
    a, b, tx, d, e, ty = p
    a2, b2, tx2, d2, e2, ty2 = c
    return [a * a2 + b * d2, a * b2 + b * e2, a * tx2 + b * ty2 + tx, d * a2 + e * d2, d * b2 + e * e2, d * tx2 + e * ty2 + ty]


def box(m: list[float], w: float, h: float) -> list[float]:
    xs = [m[0] * x + m[1] * y + m[2] for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
    ys = [m[3] * x + m[4] * y + m[5] for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
    return [min(xs), min(ys), max(xs), max(ys)]


def rgb_hex(color: dict | None, scheme: dict[str, str]) -> str | None:
    if not color:
        return None
    c = color.get("opaqueColor", color)
    if "themeColor" in c:
        return scheme.get(c["themeColor"])
    rgb = c.get("rgbColor")
    if rgb is None:
        return None
    return "#" + "".join(f"{round(rgb.get(k, 0.0) * 255):02x}" for k in ("red", "green", "blue"))


def design_for(size: float) -> int:
    """Computer Modern optical size LaTeX picks for a font size."""
    return 8 if size < 8.5 else 9 if size < 9.5 else 10 if size < 11.5 else 12 if size < 17 else 17


def pdf_size(fonts: FontMapper, family: str, slides_size: float, bold: bool, italic: bool, scale: float,
             font: str | None = None) -> tuple[float, str]:
    """Inverse of FontMapper: (PDF font size, a TeX font name that maps like it)."""
    tex_family = FAMILY_FOR_FONT.get(family)
    if tex_family is None:  # a Google font used as-is (or Arial for a box the user added)
        return round(slides_size / scale, 2), font or family.replace(" ", "")
    size = slides_size / scale
    name = font
    for _ in range(4):
        if font is None:
            prefix = {"sans": "CMSS", "serif": "CMR", "mono": "CMTT"}[tex_family]
            name = f"{prefix}{design_for(size)}"
        run = {"font": name, "family": tex_family, "size": size, "bold": bold, "italic": italic}
        z = fonts(run, scale)[1]
        if z <= 0:
            break
        size = size * slides_size / z if abs(z - slides_size) > 0.05 else size
    # FontMapper rounds to 0.1 pt: one more exact step without rounding
    return round(size, 2), name


class StyleResolver:
    """Text style fields a run leaves out come from its placeholder's parents (layout, master)."""

    def __init__(self, pres: dict):
        self.by_id: dict[str, dict] = {}
        for page in pres.get("layouts", []) + pres.get("masters", []):
            for pe in walk_elements(page.get("pageElements", [])):
                self.by_id[pe["objectId"]] = pe
        self.scheme = {}
        for master in pres.get("masters", []):
            for c in master.get("pageProperties", {}).get("colorScheme", {}).get("colors", []):
                self.scheme.setdefault(c["type"], rgb_hex({"rgbColor": c.get("color", {})}, {}))

    def parent_style(self, pe: dict) -> dict:
        chain = []
        cur = pe
        for _ in range(4):
            parent = cur.get("shape", {}).get("placeholder", {}).get("parentObjectId")
            if not parent or parent not in self.by_id:
                break
            cur = self.by_id[parent]
            chain.append(cur)
        style: dict = {}
        for el in reversed(chain):
            for te in el.get("shape", {}).get("text", {}).get("textElements", []):
                if "textRun" in te:
                    style.update(te["textRun"].get("style", {}))
                    break
        return style


def walk_elements(elements: list[dict]):
    for pe in elements:
        yield pe
        if "elementGroup" in pe:
            yield from walk_elements(pe["elementGroup"].get("children", []))


def flatten(elements: list[dict], parent: list[float] | None = None, group: str | None = None):
    """(page element, absolute matrix, top group id) for every leaf element."""
    parent = parent or [1, 0, 0, 0, 1, 0]
    for pe in elements:
        m = compose(parent, affine(pe.get("transform")))
        if "elementGroup" in pe:
            yield from flatten(pe["elementGroup"].get("children", []), m, group or pe["objectId"])
        else:
            yield pe, m, group


def text_paragraphs(pe: dict, text: dict, resolver: StyleResolver, fonts: FontMapper, scale: float) -> list[dict]:
    base = {**DEFAULT_STYLE}
    parent = resolver.parent_style(pe)
    base.update({k: v for k, v in parent.items() if k in ("fontFamily", "bold", "italic")})
    if parent.get("fontSize"):
        base["fontSize"] = dim(parent["fontSize"])
    if parent.get("weightedFontFamily"):
        base["fontFamily"] = parent["weightedFontFamily"]["fontFamily"]
    if parent.get("foregroundColor"):
        base["color"] = rgb_hex(parent["foregroundColor"], resolver.scheme) or base["color"]
    paragraphs: list[dict] = []
    cur = None
    for te in text.get("textElements", []):
        if "paragraphMarker" in te:
            pm = te["paragraphMarker"]
            st = pm.get("style", {})
            bullet = pm.get("bullet")
            cur = {"align": {"START": "left", "CENTER": "center", "END": "right", "JUSTIFIED": "left"}.get(
                       st.get("alignment", "START"), "left"),
                   "level": 0, "nesting": bullet.get("nestingLevel", 0) if bullet else 0,
                   "bullet": ({"kind": "number" if re.search(r"\d|[a-z]\.|[ivx]+\.", bullet.get("glyph", "")) else "glyph",
                               "text": bullet.get("glyph", "")} if bullet else None),
                   "indent_start": dim(st.get("indentStart")), "indent_first": dim(st.get("indentFirstLine")),
                   "line_spacing": (st.get("lineSpacing") or 100) / 100, "space_above": dim(st.get("spaceAbove")),
                   "runs": [], "tab_x0": None}
            paragraphs.append(cur)
        elif "textRun" in te or "autoText" in te:
            if cur is None:
                continue
            tr = te.get("textRun") or te.get("autoText")
            content = tr.get("content", "")
            st = tr.get("style", {})
            family = (st.get("weightedFontFamily") or {}).get("fontFamily") or st.get("fontFamily") or base["fontFamily"]
            size = dim(st.get("fontSize")) or base["fontSize"]
            bold = st.get("bold", base["bold"])
            if (st.get("weightedFontFamily") or {}).get("weight", 400) >= 600:
                bold = True
            italic = st.get("italic", base["italic"])
            color = rgb_hex(st.get("foregroundColor"), resolver.scheme) or base["color"]
            psize, font = pdf_size(fonts, family, size, bold, italic, scale)
            link = st.get("link") or {}
            text_part = content.rstrip("\n") if content.endswith("\n") else content
            if not text_part:
                continue
            run = {"text": text_part, "font": font, "family": FAMILY_FOR_FONT.get(family, "sans"),
                   "slides_font": family, "slides_size": size, "size": psize, "bold": bool(bold),
                   "italic": bool(italic), "smallcaps": bool(st.get("smallCaps")), "color": color,
                   "link": link.get("url") or (f"#slide={link['pageObjectId']}" if link.get("pageObjectId") else None),
                   "script": {"SUPERSCRIPT": "super", "SUBSCRIPT": "sub"}.get(st.get("baselineOffset")),
                   "underline": bool(st.get("underline")), "strike": bool(st.get("strikethrough")),
                   "highlight": rgb_hex(st.get("backgroundColor"), resolver.scheme)}
            if family == "Roboto Mono" and text_part.strip("\u00a0") == "" and "\u00a0" in text_part:
                run["hole"] = round(len(text_part) * 0.6 * size / scale, 2)
                run["text"] = " "
            cur["runs"].append(run)
    for p in paragraphs:
        p["runs"] = merge_runs(p["runs"])
        p["size"] = max((r["size"] for r in p["runs"]), default=0.0)
        text = "".join(r["text"] for r in p["runs"])
        if "\t" in text and not p["bullet"]:
            p["tab_x0"] = p["indent_start"]
    paragraphs = [p for p in paragraphs if p["runs"] or p is not paragraphs[-1]]
    # bullet levels: ranks of (indentStart, nesting level) among bulleted paragraphs
    keys = sorted({(round(p["indent_start"] / 3), p["nesting"]) for p in paragraphs if p["bullet"]})
    for p in paragraphs:
        if p["bullet"]:
            p["level"] = keys.index((round(p["indent_start"] / 3), p["nesting"]))
    return paragraphs


def merge_runs(runs: list[dict]) -> list[dict]:
    out = []
    keys = ("font", "size", "bold", "italic", "color", "link", "script", "underline", "strike", "highlight", "smallcaps")
    for r in runs:
        if out and not r.get("hole") and not out[-1].get("hole") and all(out[-1][k] == r[k] for k in keys):
            out[-1]["text"] += r["text"]
        else:
            out.append(dict(r))
    return out


def text_element(pe: dict, m: list[float], resolver: StyleResolver, fonts: FontMapper, scale: float,
                 page_w: float) -> dict | None:
    shape = pe.get("shape", {})
    paragraphs = text_paragraphs(pe, shape.get("text", {}), resolver, fonts, scale)
    if not any(p["runs"] for p in paragraphs):
        return None
    w, h = dim(pe["size"]["width"]), dim(pe["size"]["height"])
    x0, y0, x1, y1 = box(m, w, h)
    placeholder = shape.get("placeholder", {}).get("type")
    first = next(p for p in paragraphs if p["runs"])
    z = max(r["slides_size"] for r in first["runs"])
    aligns = {p["align"] for p in paragraphs if p["runs"]}
    align = aligns.pop() if len(aligns) == 1 else "left"
    content = shape.get("shapeProperties", {}).get("contentAlignment", "TOP")
    if content == "MIDDLE":
        baseline = (y0 + y1) / 2 + MIDDLE_BASELINE_EM * z
    else:
        baseline = y0 + BASELINE_A + ASCENT_EM * z + extra_above(first["line_spacing"], z) + first["space_above"]
        if placeholder in ("TITLE", "CENTERED_TITLE", "SUBTITLE"):
            baseline -= PPTX_TITLE_DY
    # emit puts the box PAD_X left of the text's (or bullet's) left edge; centred and right-aligned
    # boxes are widened symmetrically / to the left
    x = {"left": x0 + PAD_X, "center": (x0 + x1) / 2, "right": x1 - PAD_X}[align]
    lines_x1 = (x1 - PAD_X) / scale
    out_paras = []
    for p in paragraphs:
        if not p["runs"]:
            continue
        tx0 = (x0 + PAD_X + p["indent_start"]) / scale
        out_paras.append({
            "align": p["align"], "level": p["level"], "bullet": p["bullet"] and {**p["bullet"], "bbox": None},
            "size": p["size"], "text_x0": round(tx0, 2), "tab_x0": p["tab_x0"],
            "lines": [{"baseline": None, "x0": round(tx0, 2), "x1": round(lines_x1, 2)}],
            "runs": [{k: v for k, v in r.items() if k not in ("slides_font", "slides_size")} for r in p["runs"]],
            "slides": {"indent_start": p["indent_start"], "indent_first": p["indent_first"],
                       "line_spacing": p["line_spacing"], "space_above": p["space_above"],
                       "font": p["runs"][0]["slides_font"], "size": p["runs"][0]["slides_size"]},
        })
    out_paras[0]["lines"][0]["baseline"] = round(baseline / scale, 2)
    role = "title" if placeholder in ("TITLE", "CENTERED_TITLE") else "body"
    return {"kind": "text", "role": role, "bbox": [round(v / scale, 2) for v in (x0, y0, x1, y1)],
            "anchor": [round(x / scale, 2), round(baseline / scale, 2)], "wrap_width": round((w - 2 * PAD_X) / scale, 2),
            "placeholder": placeholder, "paragraphs": out_paras}


def page_background(page: dict, resolver_pages: dict[str, dict], scheme: dict) -> tuple[str | None, str | None]:
    """(solid colour, picture url) of a page's background, following INHERIT to layout and master."""
    cur = page
    for _ in range(3):
        fill = cur.get("pageProperties", {}).get("pageBackgroundFill", {})
        if fill and fill.get("propertyState", "RENDERED") != "INHERIT":
            if "solidFill" in fill:
                return rgb_hex(fill["solidFill"].get("color"), scheme), None
            if "stretchedPictureFill" in fill:
                return None, fill["stretchedPictureFill"].get("contentUrl")
            return None, None
        parent = cur.get("slideProperties", {}).get("layoutObjectId") or cur.get("layoutProperties", {}).get("masterObjectId")
        if not parent or parent not in resolver_pages:
            break
        cur = resolver_pages[parent]
    return None, None


def notes_text(slide: dict) -> str | None:
    props = slide.get("slideProperties", {})
    page = props.get("notesPage", {})
    sid = page.get("notesProperties", {}).get("speakerNotesObjectId")
    for pe in page.get("pageElements", []):
        if pe.get("objectId") == sid:
            text = "".join(te.get("textRun", {}).get("content", "")
                           for te in pe.get("shape", {}).get("text", {}).get("textElements", []))
            text = text.strip()
            return text or None
    return None


def page_size_for(pres: dict, pdf_size: list[float] | None) -> tuple[float, float, float]:
    w = dim(pres["pageSize"]["width"])
    h = dim(pres["pageSize"]["height"])
    if pdf_size:
        return pdf_size[0], pdf_size[1], w / pdf_size[0]
    ratio = w / h
    for (a, b), size in BEAMER_SIZES.items():
        if abs(ratio - a / b) < 0.01:
            return size[0], size[1], w / size[0]
    return w / 2, h / 2, 2.0


def deck_ir(pres: dict, pdf_size: list[float] | None = None, base: dict | None = None,
            fetch=None, images: Path | None = None) -> dict:
    """IR of a presentation. `pdf_size`: the PDF page size the deck came from (else beamer's
    default for the aspect). `base`: a sync snapshot (object ids -> keys). `fetch(url) -> bytes`
    downloads pictures into `images` (sha1-named) when both are given."""
    page_w, page_h, scale = page_size_for(pres, pdf_size)
    fonts = FontMapper()
    resolver = StyleResolver(pres)
    pages = {p["objectId"]: p for p in pres.get("layouts", []) + pres.get("masters", [])}
    object_keys, slide_keys = {}, {}
    for s in (base or {}).get("slides", []):
        if s.get("objectId"):
            slide_keys[s["objectId"]] = s.get("key")
        for e in s.get("elements", []):
            for oid in e.get("objects", []):
                object_keys[oid] = (s.get("key"), e.get("key"))
    slides = []
    for n, slide in enumerate(pres.get("slides", [])):
        elements = []
        tags = []
        for pe, m, group in flatten(slide.get("pageElements", [])):
            tag = TAG_RE.match(pe.get("title") or "")
            key = (tag.group("slide"), tag.group("element")) if tag else object_keys.get(pe["objectId"]) or \
                (object_keys.get(group) if group else None)
            if key and key[0]:
                tags.append(key[0])
            el = element_of(pe, m, resolver, fonts, scale, page_w, fetch, images)
            if el is None:
                continue
            el.update({"id": pe["objectId"], "object": pe["objectId"], "group": group,
                       "key": key[1] if key else None})
            if el["kind"] == "image" and key and key[1] and key[1].split("/")[1:2] in (["math"], ["icon"]):
                el["role"] = key[1].split("/")[1]
            elif el["kind"] == "image" and pe.get("title") in ("Formula", "Icon"):
                el["role"] = {"Formula": "math", "Icon": "icon"}[pe["title"]]
            if el["kind"] == "text" and el["key"] and "/footer/" in f"/{el['key']}/":
                el["role"] = "footer"
            elements.append(el)
        color, picture = page_background(slide, pages, resolver.scheme)
        key = slide_keys.get(slide["objectId"]) or (max(set(tags), key=tags.count) if tags else None)
        slides.append({"page": n, "frame": str(n + 1), "size": [page_w, page_h], "objectId": slide["objectId"],
                       "key": key, "notes": notes_text(slide), "background_color": color,
                       "background_picture": picture, "elements": elements})
    return {"version": 1, "source": {"presentationId": pres.get("presentationId"), "title": pres.get("title"),
                                     "revisionId": pres.get("revisionId")},
            "page_size": [page_w, page_h], "scale": scale, "slides": slides}


def element_of(pe: dict, m: list[float], resolver: StyleResolver, fonts: FontMapper, scale: float, page_w: float,
               fetch, images: Path | None) -> dict | None:
    if "size" not in pe and "line" not in pe:
        return None
    w, h = dim(pe.get("size", {}).get("width")), dim(pe.get("size", {}).get("height"))
    bbox = [round(v / scale, 2) for v in box(m, w, h)]
    if "shape" in pe:
        shape = pe["shape"]
        el = text_element(pe, m, resolver, fonts, scale, page_w) if shape.get("text") else None
        props = shape.get("shapeProperties", {})
        fill = props.get("shapeBackgroundFill", {})
        fill_hex = rgb_hex(fill.get("solidFill", {}).get("color"), resolver.scheme) \
            if fill.get("propertyState", "RENDERED") == "RENDERED" and "solidFill" in fill else None
        if el is not None:
            if fill_hex and shape.get("shapeType") != "TEXT_BOX":
                el["fill"] = fill_hex
            return el
        if shape.get("shapeType") == "TEXT_BOX" or shape.get("placeholder"):
            return None
        outline = props.get("outline", {})
        stroke = rgb_hex(outline.get("outlineFill", {}).get("solidFill", {}).get("color"), resolver.scheme) \
            if outline.get("propertyState", "RENDERED") == "RENDERED" else None
        if not fill_hex and not stroke:
            return None
        return {"kind": "shape", "role": "panel", "bbox": bbox, "shape": shape.get("shapeType", "RECTANGLE").lower(),
                "fill": fill_hex, "outline": stroke}
    if "image" in pe:
        el = {"kind": "image", "role": "figure", "bbox": bbox, "alt": pe.get("description")}
        url = pe["image"].get("contentUrl")
        if url and fetch and images is not None:
            try:
                data = fetch(url)
                sha = hashlib.sha1(data).hexdigest()
                ext = ".png" if data[:4] == b"\x89PNG" else ".jpg" if data[:2] == b"\xff\xd8" else ".gif" \
                    if data[:3] == b"GIF" else ".img"
                images.mkdir(parents=True, exist_ok=True)
                path = images / f"{sha[:16]}{ext}"
                if not path.exists():
                    path.write_bytes(data)
                el.update({"file": str(path), "sha1": sha})
            except OSError as e:
                el["error"] = str(e)[:120]
        return el
    if "table" in pe:
        rows = []
        for row in pe["table"].get("tableRows", []):
            cells = []
            for cell in row.get("tableCells", []):
                text = "".join(te.get("textRun", {}).get("content", "")
                               for te in cell.get("text", {}).get("textElements", []))
                cells.append(" ".join(text.split()))
            rows.append(cells)
        return {"kind": "table", "role": "table", "bbox": bbox, "rows": rows}
    if "line" in pe:
        return None
    return None


def fetch_url(url: str) -> bytes:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except OSError:
            if attempt == 3:
                raise
    return b""


def presentation_id(ref: str) -> str:
    """A deck URL, id or converted output folder (emit.json) -> presentation id."""
    p = Path(ref)
    if p.is_dir() and (p / "emit.json").exists():
        return json.loads((p / "emit.json").read_text(encoding="utf-8"))["presentationId"]
    m = re.search(r"/presentation/d/([A-Za-z0-9_-]+)", ref)
    return m.group(1) if m else ref


def read_deck(ref: str, images: Path | None = None, base: dict | None = None, pdf_size: list[float] | None = None,
              slides=None) -> dict:
    """Fetch a live deck and return its IR (pictures downloaded into `images`)."""
    from .google_auth import slides_service
    from .gslides import execute
    slides = slides or slides_service()
    pid = presentation_id(ref)
    pres = execute(slides.presentations().get(presentationId=pid))
    p = Path(ref)
    if pdf_size is None and p.is_dir() and (p / "deck.json").exists():
        pdf_size = json.loads((p / "deck.json").read_text(encoding="utf-8"))["slides"][0]["size"]
    if base is None and p.is_dir() and (p / "sync" / "base.json").exists():
        base = json.loads((p / "sync" / "base.json").read_text(encoding="utf-8"))
    return deck_ir(pres, pdf_size or (base or {}).get("page_size"), base, fetch_url if images else None, images)
