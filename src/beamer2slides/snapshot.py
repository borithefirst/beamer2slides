"""The sync base (docs/sync.md): the converter's IR plus Google's read-back of every object it
created, recorded right after the deck was written, in `<out>/sync/base.json` and in Drive."""

import io
import json
import os
import re
from pathlib import Path

from googleapiclient.errors import HttpError

from . import identity
from .emit import background_key as emit_background_key, slide_layout
from .gslides import EMU_PER_PT, execute

VERSION = 1
TAG_PREFIX = "b2s:"
TEXT_STYLE_KEYS = ("fontFamily", "bold", "italic", "underline", "strikethrough", "smallCaps", "baselineOffset")
PARAGRAPH_KEYS = ("alignment", "lineSpacing", "direction")
GEOMETRY_TOLERANCE = 0.05  # pt


# ---------------------------------------------------------------- read-back

def _unit(v: dict | None) -> float:
    if not v:
        return 0.0
    return v.get("magnitude", 0.0) / (EMU_PER_PT if v.get("unit", "EMU") == "EMU" else 1.0)


def colour(c: dict | None) -> str | None:
    """An OpaqueColor / OptionalColor as '#rrggbb' or 'theme:NAME'."""
    if not c:
        return None
    c = c.get("opaqueColor", c)
    if "themeColor" in c:
        return f"theme:{c['themeColor']}"
    if "rgbColor" in c:
        rgb = c["rgbColor"]
        return "#" + "".join(f"{round(rgb.get(k, 0.0) * 255):02x}" for k in ("red", "green", "blue"))
    return None


def matrix(t: dict | None) -> list[float]:
    """[scaleX, shearX, shearY, scaleY, translateX pt, translateY pt]."""
    if not t:
        return [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    div = EMU_PER_PT if t.get("unit", "EMU") == "EMU" else 1.0  # (omitted fields are 0, as in the API)
    return [t.get("scaleX", 0.0), t.get("shearX", 0.0), t.get("shearY", 0.0), t.get("scaleY", 0.0),
            t.get("translateX", 0.0) / div, t.get("translateY", 0.0) / div]


def compose(p: list[float], c: list[float]) -> list[float]:
    """p after c."""
    pa, pb, pc, pd, px, py = p
    ca, cb, cc, cd, cx, cy = c
    return [pa * ca + pb * cc, pa * cb + pb * cd, pc * ca + pd * cc, pc * cb + pd * cd,
            pa * cx + pb * cy + px, pc * cx + pd * cy + py]


def invert(m: list[float]) -> list[float]:
    a, b, c, d, x, y = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return [1.0, 0.0, 0.0, 1.0, -x, -y]
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return [ia, ib, ic, id_, -(ia * x + ib * y), -(ic * x + id_ * y)]


def box(m: list[float], w: float, h: float) -> list[float]:
    pts = [(m[0] * x + m[1] * y + m[4], m[2] * x + m[3] * y + m[5]) for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
    return [round(min(p[0] for p in pts), 2), round(min(p[1] for p in pts), 2),
            round(max(p[0] for p in pts), 2), round(max(p[1] for p in pts), 2)]


def _text_style(style: dict) -> dict:
    out = {k: style[k] for k in TEXT_STYLE_KEYS if k in style}
    if "weightedFontFamily" in style:
        out["fontFamily"] = style["weightedFontFamily"].get("fontFamily")
        out["weight"] = style["weightedFontFamily"].get("weight")
    if "fontSize" in style:
        out["fontSize"] = round(_unit(style["fontSize"]), 2)
    for k in ("foregroundColor", "backgroundColor"):
        if style.get(k):
            out[k] = colour(style[k])
    link = style.get("link")
    if link:
        out["link"] = link.get("url") or link.get("pageObjectId") or link.get("relativeLink") or str(link.get("slideIndex"))
    return out


def _paragraph_style(marker: dict) -> dict:
    style = marker.get("style", {})
    out = {k: style[k] for k in PARAGRAPH_KEYS if k in style}
    for k in ("indentStart", "indentFirstLine", "spaceAbove", "spaceBelow"):
        if k in style:
            out[k] = round(_unit(style[k]), 2)
    if "bullet" in marker:
        out["bullet"] = [marker["bullet"].get("glyph"), marker["bullet"].get("nestingLevel", 0)]
    return out


def read_text(text: dict | None) -> tuple[str, list[dict], list[dict]]:
    """(content, distinct run styles, distinct paragraph styles) of a shape's or cell's text."""
    content, runs, paras = [], [], []
    for te in (text or {}).get("textElements", []):
        if "textRun" in te:
            content.append(te["textRun"].get("content", ""))
            s = _text_style(te["textRun"].get("style", {}))
            if te["textRun"].get("content", "").strip("\n") and s not in runs:
                runs.append(s)
        elif "autoText" in te:
            content.append(te["autoText"].get("content", ""))
        elif "paragraphMarker" in te:
            s = _paragraph_style(te["paragraphMarker"])
            if s not in paras:
                paras.append(s)
    return "".join(content), runs, paras


def _fill(fill: dict | None) -> dict | None:
    if not fill:
        return None
    if "solidFill" in fill:
        return {"color": colour(fill["solidFill"].get("color")), "alpha": round(fill["solidFill"].get("alpha", 1.0), 3)}
    return {"state": fill.get("propertyState", "RENDERED")}


def _outline(o: dict | None) -> dict | None:
    if not o:
        return None
    return {"fill": _fill(o.get("outlineFill")), "weight": round(_unit(o.get("weight")), 2),
            "dash": o.get("dashStyle"), "state": o.get("propertyState", "RENDERED")}


def shape_style(e: dict) -> dict:
    if "shape" in e:
        props = e["shape"].get("shapeProperties", {})
        return {"type": e["shape"].get("shapeType"), "fill": _fill(props.get("shapeBackgroundFill")),
                "outline": _outline(props.get("outline")), "align": props.get("contentAlignment")}
    if "line" in e:
        props = e["line"].get("lineProperties", {})
        return {"line": e["line"].get("lineType"), "fill": _fill(props.get("lineFill")), "weight": round(_unit(props.get("weight")), 2),
                "dash": props.get("dashStyle"), "arrows": [props.get("startArrow"), props.get("endArrow")]}
    if "image" in e:
        return {"outline": _outline(e["image"].get("imageProperties", {}).get("outline"))}
    return {}


def image_hash(url: str | None) -> str | None:
    """A hash of a contentUrl. Google hands out new contentUrls for the same picture now and then,
    so equal hashes mean the same picture but different ones don't mean a replaced picture: see
    `signature` / `same_picture`."""
    return identity.sha1(re.split(r"[=?]", url, maxsplit=1)[0])[:16] if url else None


SIGNATURE_SIZE = 32
SIGNATURE_DIFFERENCE = 0.3  # ink-normalised difference below which two signatures are the same picture


def signature(data: bytes) -> str | None:
    """A small fingerprint of a picture's pixels ("<w>x<h>:<32x32 grey levels hex>"), stable across
    Google's re-encodings and new contentUrls."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as img:
            rgba = img.convert("RGBA")
    except Exception:  # noqa: BLE001 (not a picture)
        return None
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    grey = Image.alpha_composite(white, rgba).convert("L").resize((SIGNATURE_SIZE, SIGNATURE_SIZE), Image.BOX)
    return f"{rgba.size[0]}x{rgba.size[1]}:{grey.tobytes().hex()}"


def signatures_match(a: str | None, b: str | None) -> bool | None:
    """Whether two signatures show the same picture (None: one is missing)."""
    if not a or not b:
        return None
    (size_a, pixels_a), (size_b, pixels_b) = a.split(":", 1), b.split(":", 1)
    ra, rb = (int(w) / max(1, int(h)) for w, _, h in (size_a.partition("x"), size_b.partition("x")))
    if abs(ra - rb) > 0.05 * max(ra, rb):
        return False
    pa, pb = bytes.fromhex(pixels_a), bytes.fromhex(pixels_b)
    diff = sum(abs(x - y) for x, y in zip(pa, pb))
    ink = max(sum(255 - x for x in pa), sum(255 - x for x in pb), 255 * 2)
    return diff / ink < SIGNATURE_DIFFERENCE


def same_picture(a: dict | None, b: dict | None) -> bool:
    """Image read-backs ({"contentHash", "signature"?}) or picture backgrounds ({"picture", "signature"?}):
    the same picture if the URL hash is the same, or else the pixel signatures match."""
    a, b = a or {}, b or {}
    ha, hb = a.get("contentHash", a.get("picture")), b.get("contentHash", b.get("picture"))
    if ha == hb:
        return True
    return bool(signatures_match(a.get("signature"), b.get("signature")))


def same_background(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a == b
    if "picture" in a and "picture" in b:
        return same_picture(a, b)
    return a == b


def _download(url: str) -> bytes | None:
    import time
    import urllib.request
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except OSError:
            if attempt == 2:
                return None
            time.sleep(1 + attempt)
    return None


def picture_urls(pres: dict) -> tuple[dict[str, str], dict[str, str]]:
    """(image objectId -> contentUrl, slide objectId -> background picture contentUrl) of a presentations.get."""
    images, backgrounds = {}, {}
    for s in pres.get("slides", []):
        fill = s.get("pageProperties", {}).get("pageBackgroundFill", {})
        if fill.get("stretchedPictureFill", {}).get("contentUrl"):
            backgrounds[s["objectId"]] = fill["stretchedPictureFill"]["contentUrl"]
        stack = list(s.get("pageElements", []))
        while stack:
            e = stack.pop()
            if e.get("image", {}).get("contentUrl"):
                images[e["objectId"]] = e["image"]["contentUrl"]
            stack += e.get("elementGroup", {}).get("children", [])
    return images, backgrounds


def sign_pictures(read: dict, pres: dict, objects=None, slides=None, workers: int = 8) -> int:
    """Adds pixel signatures to the image read-backs and picture backgrounds of `read`
    (read_presentation of `pres`); `objects` / `slides`: only these ids (None: all). Returns how
    many pictures were downloaded."""
    from concurrent.futures import ThreadPoolExecutor
    images, backgrounds = picture_urls(pres)
    jobs = []
    for s in read["slides"]:
        bg = s.get("background") or {}
        if "picture" in bg and s["objectId"] in backgrounds and (slides is None or s["objectId"] in slides):
            jobs.append((bg, backgrounds[s["objectId"]]))
        for oid, rb in s["objects"].items():
            if "image" in rb and oid in images and (objects is None or oid in objects):
                jobs.append((rb["image"], images[oid]))
    if not jobs:
        return 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (target, _), data in zip(jobs, pool.map(lambda j: _download(j[1]), jobs)):
            if data:
                target["signature"] = signature(data)
    return len(jobs)


def readback(e: dict, parent: list[float], parent_group: str | None, z: int) -> dict:
    """Normalised read-back of one page element (pt, hex colours, absolute transform)."""
    m = compose(parent, matrix(e.get("transform")))
    w, h = _unit(e.get("size", {}).get("width")), _unit(e.get("size", {}).get("height"))
    out = {"kind": next((k for k in ("shape", "image", "line", "table", "elementGroup", "sheetsChart", "video", "wordArt")
                         if k in e), "other"),
           "transform": [round(v, 4) for v in m[:4]] + [round(v, 2) for v in m[4:]], "size": [round(w, 2), round(h, 2)],
           "box": box(m, w, h), "parent_group": parent_group, "z": z, "title": e.get("title"),
           "description": e.get("description")}
    text, runs, paras = None, [], []
    if "shape" in e:
        text, runs, paras = read_text(e["shape"].get("text"))
        if "placeholder" in e["shape"]:
            out["placeholder"] = e["shape"]["placeholder"].get("type")
    elif "table" in e:
        rows = []
        for row in e["table"].get("tableRows", []):
            cells = []
            for cell in row.get("tableCells", []):
                t, r, p = read_text(cell.get("text"))
                cells.append(t.rstrip("\n"))
                runs += [s for s in r if s not in runs]
                paras += [s for s in p if s not in paras]
            rows.append("\t".join(cells))
        text = "\n".join(rows)
        out["table"] = [e["table"].get("rows"), e["table"].get("columns")]
    out["text"] = text
    out["text_styles"] = runs
    out["paragraph_styles"] = paras
    out["text_style_hash"] = identity.sha1(json.dumps([sorted(json.dumps(s, sort_keys=True) for s in runs),
                                                       sorted(json.dumps(s, sort_keys=True) for s in paras)]))[:12]
    style = shape_style(e)
    out["shape_style"] = style
    out["shape_style_hash"] = identity.sha1(json.dumps(style, sort_keys=True))[:12]
    if "image" in e:
        out["image"] = {"contentHash": image_hash(e["image"].get("contentUrl")),
                        "sourceUrl": e["image"].get("sourceUrl")}
    return out


def background(page: dict) -> dict:
    fill = page.get("pageProperties", {}).get("pageBackgroundFill", {})
    if "stretchedPictureFill" in fill:
        return {"picture": image_hash(fill["stretchedPictureFill"].get("contentUrl"))}
    if "solidFill" in fill:
        return {"color": colour(fill["solidFill"].get("color"))}
    return {"state": fill.get("propertyState", "INHERIT")}


def read_slide(slide: dict) -> dict:
    """A slide as sync compares it: {objectId, layoutObjectId, background, notes, notes_id,
    order (top-level ids), objects {id: readback}}."""
    objects: dict[str, dict] = {}
    counter = [0]

    def walk(elements, parent, group):
        for e in elements:
            rb = readback(e, parent, group, counter[0])
            counter[0] += 1
            objects[e["objectId"]] = rb
            if "elementGroup" in e:
                m = compose(parent, matrix(e.get("transform")))
                walk(e["elementGroup"].get("children", []), m, e["objectId"])
                kids = [objects[c["objectId"]]["box"] for c in e["elementGroup"].get("children", [])]
                if kids:
                    rb["box"] = [min(k[0] for k in kids), min(k[1] for k in kids), max(k[2] for k in kids), max(k[3] for k in kids)]
                rb["children"] = [c["objectId"] for c in e["elementGroup"].get("children", [])]

    walk(slide.get("pageElements", []), [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], None)
    notes_page = slide.get("slideProperties", {}).get("notesPage", {})
    notes_id = notes_page.get("notesProperties", {}).get("speakerNotesObjectId")
    notes = ""
    for e in notes_page.get("pageElements", []):
        if e["objectId"] == notes_id:
            notes = read_text(e.get("shape", {}).get("text"))[0]
    return {"objectId": slide["objectId"], "layoutObjectId": slide.get("slideProperties", {}).get("layoutObjectId"),
            "background": background(slide), "notes": notes.rstrip("\n"), "notes_id": notes_id,
            "order": [e["objectId"] for e in slide.get("pageElements", [])], "objects": objects}


def read_presentation(pres: dict) -> dict:
    layouts = {l["objectId"]: l.get("layoutProperties", {}).get("name") for l in pres.get("layouts", [])}
    return {"presentationId": pres["presentationId"], "revisionId": pres.get("revisionId"),
            "page_size": [_unit(pres["pageSize"]["width"]), _unit(pres["pageSize"]["height"])],
            "layouts": layouts, "master_background": background(pres["masters"][0]) if pres.get("masters") else None,
            "slides": [read_slide(s) for s in pres.get("slides", [])]}


# ---------------------------------------------------------------- base

def source_info(pdf: Path) -> dict:
    return {"pdf": str(pdf), "sha1": identity.sha1(pdf.read_bytes()) if pdf.exists() else None}


def background_key(slide: dict, out: Path) -> str:
    kind, value = emit_background_key(slide, out)
    return f"{kind}:{value}"


PICTURE_FLAT = 6     # levels: the ground the older picture painted in counts as one colour
PICTURE_MATCH = 16   # levels: the new picture laid on that ground shows the older one (render.clear_ground)


def same_picture_file(a: Path, b: Path) -> bool:
    """`a` (the file the base recorded) and `b` (the new one) put the same thing on the page: the
    same pixels where `b` is opaque, over one flat colour where it is transparent - the ground the
    older picture had painted in (render.clear_ground). Anchored pictures became RGBA on a
    transparent ground, so without this every one of them would be rewritten on the first sync
    after that change although the slides look the same."""
    import numpy as np
    from PIL import Image

    try:
        with Image.open(a) as ia, Image.open(b) as ib:
            if ia.size != ib.size:
                return False
            old = np.asarray(ia.convert("RGB")).astype(float)
            new = np.asarray(ib.convert("RGBA")).astype(float)
    except (OSError, ValueError):
        return False
    ground = np.array([255.0, 255.0, 255.0])
    clear = new[..., 3] == 0
    if clear.any():
        under = old[clear]
        ground = np.median(under, axis=0)
        if (np.abs(under - ground).max(axis=1) > PICTURE_FLAT).mean() > 0.002:
            return False  # (the older picture had something else under the new one's clear ground)
    alpha = new[..., 3:] / 255
    laid = new[..., :3] * alpha + ground * (1 - alpha)
    return bool((np.abs(laid - old).max(axis=2) > PICTURE_MATCH).mean() <= 0.002)


def refresh_pictures(base: dict, ours: dict, deck_out: Path) -> list[dict]:
    """Pictures whose file changed but that look the same are not a source change: the base takes
    the new hash and the deck keeps its object, instead of every anchored picture being rewritten
    when the converter changes how it writes them. `base` is updated in place; returns
    [{"slide", "element"}] for the report."""
    refreshed = []
    for j, i in ((int(k), v) for k, v in ours["pairs"].items()):
        b, o = base["slides"][i], ours["slides"][j]
        ours_by = {e["key"]: e for e in o["elements"]}
        for el in b["elements"]:
            oe = ours_by.get(el["key"])
            old, new = el["ir"].get("file"), (oe or {}).get("ir", {}).get("file")
            if oe is None or el["kind"] != "image" or not old or not new:
                continue
            if identity.source_changes(el, oe) != {"image"}:
                continue
            if identity.normalise_ir(el["ir"], el.get("anchor")) != identity.normalise_ir(oe["ir"], oe.get("anchor")):
                continue  # (the IR changed in a way the field hashes don't see)
            if not same_picture_file(deck_out / old, ours["out"] / new):
                continue
            el["fields"] = {**el["fields"], "image": oe["fields"]["image"]}
            el["ir_hash"] = oe["ir_hash"]
            refreshed.append({"slide": b["key"], "element": el["key"]})
    return refreshed


def slide_entries(deck: dict, out: Path, keys: list[str], element_keys: list[list[str]], fingerprints: list[list[dict]]) -> list[dict]:
    """The IR part of base slides (keys, hashes, fingerprints, IR), without objects and read-back."""
    page_key = page_keys(deck, keys)
    entries = []
    for slide, key, ekeys, fps in zip(deck["slides"], keys, element_keys, fingerprints):
        ids = {e["id"]: k for e, k in zip(slide["elements"], ekeys)}
        elements = []
        for el, ek, fp in zip(slide["elements"], ekeys, fps):
            h, fields = identity.ir_fields(el, out, ids.get(el.get("anchor")), page_key)
            elements.append({"key": ek, "id": el["id"], "kind": el["kind"], "role": el.get("role"), "ir_hash": h,
                             "fields": fields, "fingerprint": fp, "anchor": ids.get(el.get("anchor")), "ir": el})
        entries.append({"key": key, "label": slide.get("label"), "title": identity.slide_title(slide), "page": slide["page"],
                        "text": identity.slide_text(slide), "layout": slide_layout(slide)[0],
                        "background": background_key(slide, out), "notes": slide.get("notes") or "",
                        "elements": elements})
    return entries


def page_keys(deck: dict, keys: list[str]):
    """PDF page -> slide key, for internal links (a skipped overlay step links to its kept step)."""
    kept = sorted((s["page"], k) for s, k in zip(deck["slides"], keys))

    def key(page: int) -> str:
        return next((k for p, k in kept if p >= page), kept[-1][1] if kept else "")
    return key


def attach_readback(entry: dict, slide_read: dict | None, objects: list[list[str]], groups: list[str]) -> None:
    """Objects and read-back of one base slide."""
    entry["objectId"] = slide_read["objectId"] if slide_read else None
    entry["layoutObjectId"] = slide_read["layoutObjectId"] if slide_read else None
    entry["background_readback"] = slide_read["background"] if slide_read else None
    entry["notes_readback"] = slide_read["notes"] if slide_read else ""
    entry["groups"] = groups
    entry["order"] = slide_read["order"] if slide_read else []
    found = slide_read["objects"] if slide_read else {}
    for el, oids in zip(entry["elements"], objects):
        el["objects"] = oids
        el["main"] = oids[0] if oids else None
        el["readback"] = {oid: found[oid] for oid in oids if oid in found}


def build_base(deck: dict, out: Path, pres: dict, state: dict, pdf: Path, generation: int = 0, sign: bool = False) -> dict:
    """The base after `convert`: `state` is emit's (slides with element object ids); `sign`:
    download the pictures for their signatures."""
    infos = [identity.slide_info(s) for s in deck["slides"]]
    keys = identity.slide_keys(infos)
    ekeys, fps = zip(*[identity.slide_element_keys(s["elements"], out) for s in deck["slides"]]) if deck["slides"] else ((), ())
    entries = slide_entries(deck, out, keys, list(ekeys), list(fps))
    read = read_presentation(pres)
    if sign:
        sign_pictures(read, pres)
    by_id = {s["objectId"]: s for s in read["slides"]}
    for entry, s in zip(entries, state["slides"]):
        attach_readback(entry, by_id.get(s["objectId"]), s.get("objects") or [[o] for o in s["elements"]], s.get("groups", []))
    return {"version": VERSION, "generation": generation, "presentationId": read["presentationId"],
            "revisionId": read["revisionId"], "source": source_info(pdf), "scale": state.get("scale"),
            "page_size": deck["slides"][0]["size"] if deck["slides"] else None, "deck_page_size": read["page_size"],
            "master_background": master_key(deck, out), "master_readback": read["master_background"],
            "slides": entries}


def master_key(deck: dict, out: Path) -> str | None:
    """The background emit put on the master (the most common one, if shared)."""
    from collections import Counter
    counts = Counter(background_key(s, out) for s in deck["slides"])
    if not counts:
        return None
    key, n = counts.most_common(1)[0]
    return key if n >= 2 else None


def tag(slide_key: str, element_key: str) -> str:
    return f"{TAG_PREFIX}{slide_key}/{element_key}"


def tag_requests(base: dict) -> list[dict]:
    """Alt-text titles naming each element's main object, so copies made in Slides are recognised."""
    reqs = []
    for s in base["slides"]:
        for el in s["elements"]:
            main = el.get("main")
            rb = el.get("readback", {}).get(main)
            if main and rb and rb.get("title") != tag(s["key"], el["key"]) and rb["kind"] != "elementGroup":
                reqs.append({"updatePageElementAltText": {"objectId": main, "title": tag(s["key"], el["key"])}})
    return reqs


def write_tags(slides, pid: str, reqs: list[dict]) -> int:
    """Sends tag requests; the ones the API refuses (some placeholders) are skipped."""
    if not reqs:
        return 0
    try:
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
        return len(reqs)
    except HttpError:
        sent = 0
        for r in reqs:
            try:
                execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [r]}))
                sent += 1
            except HttpError:
                pass
        return sent


# ---------------------------------------------------------------- storage

BASE_PROPERTY = "b2sBase"


def local_path(out: Path) -> Path:
    return out / "sync" / "base.json"


def save_local(base: dict, out: Path) -> Path:
    """Written to a temporary file and moved into place: a process killed while the base is being
    written leaves the previous one, never half of a file (a truncated base is no base at all)."""
    path = local_path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".writing")
    tmp.write_text(json.dumps(base, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def base_problem(data, pid: str | None) -> str | None:
    """Why `data` cannot be used as the base of presentation `pid` (None: it can)."""
    if not isinstance(data, dict):
        return "not a JSON object"
    version = data.get("version")
    if not isinstance(version, int):
        return "no schema version"
    if version > VERSION:
        return f"schema version {version} is newer than this beamer2slides (up to {VERSION})"
    if not isinstance(data.get("slides"), list):
        return "no slides"
    if pid and data.get("presentationId") != pid:
        return f"it belongs to presentation {data.get('presentationId')}"
    return None


def read_local(out: Path | None, pid: str | None) -> tuple[dict | None, str | None]:
    """(base, why not) of the local cache: a missing, truncated or foreign file is no base."""
    if out is None or not local_path(out).exists():
        return None, None
    try:
        data = json.loads(local_path(out).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"{local_path(out)} could not be read ({type(e).__name__}: {e})"
    problem = base_problem(data, pid)
    return (None, f"{local_path(out)}: {problem}") if problem else (data, None)


def save_drive(drive, base: dict, title: str | None = None) -> str:
    """The base as a JSON file next to the presentation (drive.file scope), its id in the
    presentation's appProperties.b2sBase. Returns the file id."""
    from googleapiclient.http import MediaIoBaseUpload

    pid = base["presentationId"]
    info = execute(drive.files().get(fileId=pid, fields="name,parents,appProperties"))
    data = json.dumps(base, ensure_ascii=False).encode("utf-8")
    fid = (info.get("appProperties") or {}).get(BASE_PROPERTY)
    if fid:
        try:
            execute(drive.files().update(fileId=fid, media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype="application/json"),
                                         fields="id"))
        except HttpError:
            fid = None
    if not fid:
        body = {"name": f"{title or info.get('name', pid)} - beamer2slides sync base.json", "mimeType": "application/json",
                "appProperties": {"b2sBaseOf": pid}}
        if info.get("parents"):
            body["parents"] = info["parents"]
        fid = execute(drive.files().create(body=body, fields="id", media_body=MediaIoBaseUpload(
            io.BytesIO(data), mimetype="application/json")))["id"]
        execute(drive.files().update(fileId=pid, body={"appProperties": {BASE_PROPERTY: fid}}, fields="id"))
    return fid


def load_drive(drive, pid: str) -> dict | None:
    try:
        info = execute(drive.files().get(fileId=pid, fields="appProperties"))
        fid = (info.get("appProperties") or {}).get(BASE_PROPERTY)
        if not fid:
            return None
        data = execute(drive.files().get_media(fileId=fid))
        return json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except (HttpError, ValueError):
        return None


def load_base(pid: str, out: Path | None, drive=None, problems: list[str] | None = None) -> tuple[dict | None, str]:
    """(base, where it came from): Drive is authoritative, the local copy a cache - except when the
    local one is newer, which is what a sync whose Drive upload failed leaves behind. A base that is
    truncated, from another deck or from a newer schema is not used at all; `problems` collects why
    (the caller reports them: a silently ignored base would sync against nothing and rewrite the
    whole deck)."""
    problems = problems if problems is not None else []
    remote = load_drive(drive, pid) if drive is not None else None
    if remote is not None:
        problem = base_problem(remote, pid)
        if problem:
            problems.append(f"the base stored in Drive was ignored: {problem}")
            remote = None
    local, why = read_local(out, pid)
    if why:
        problems.append(f"the local base was ignored: {why}")
    if remote is not None and local is not None and local.get("generation", 0) > remote.get("generation", 0):
        problems.append(f"the base in Drive is older than the local one (generation {remote.get('generation', 0)} vs "
                        f"{local.get('generation', 0)}): syncing from the local one and storing it in Drive again")
        return local, "local"
    if remote is not None:
        return remote, "drive"
    if local is not None:
        return local, "local"
    return None, "none"


def store_base(base: dict, out: Path, drive=None, label: str = "base") -> str | None:
    """Store the base where the next sync will look for it: locally first (atomically), then in
    Drive. Returns why Drive could not take it (None: it did). The caller decides what to do about
    a base that only reached the local folder - sync keeps the objects it would have deleted."""
    from .faults import fail_at

    fail_at(f"{label}:save")
    save_local(base, out)
    if drive is None:
        return "no Drive service"
    fail_at(f"{label}:drive")
    try:
        save_drive(drive, base)
    except (HttpError, OSError) as e:
        return f"{type(e).__name__}: {e}"
    return None


def base_matches(base: dict, theirs: dict) -> bool:
    """Whether the base describes this live deck at all. A base whose slides are all gone means the
    deck was copied or rebuilt behind our back: syncing would report every element as deleted in the
    deck and keep nothing."""
    known = [b.get("objectId") for b in base.get("slides", []) if b.get("objectId")]
    if not known:
        return True
    live = {s["objectId"] for s in theirs.get("slides", [])}
    return any(sid in live for sid in known)


def snapshot_after_convert(deck: dict, out: Path, state: dict, pdf: Path) -> dict:
    """Tag the new deck's objects and record the base (convert's last step)."""
    from .google_auth import drive_service, slides_service

    slides, drive = slides_service(), drive_service()
    pid = state["presentationId"]
    pres = execute(slides.presentations().get(presentationId=pid))
    base = build_base(deck, out, pres, state, pdf)
    if write_tags(slides, pid, tag_requests(base)):
        pres = execute(slides.presentations().get(presentationId=pid))
    base = build_base(deck, out, pres, state, pdf, sign=True)
    save_local(base, out)
    try:
        save_drive(drive, base)
    except HttpError as e:
        print(f"warning: could not store the sync base in Drive ({e}); kept locally")
    return base
