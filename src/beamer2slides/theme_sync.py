"""The deck's theme in a sync: the master background, the theme decoration pictures on the layouts
and the look of the layouts' title and body placeholders (docs/sync.md "Layouts and the master").

`convert` puts the most common slide background on the master (or only its ground colour, when
the rest of it is decoration), the decoration shared by the slides onto every layout as a
full-page picture (`emit.plan_theme`), and the deck's title and body style into the layouts'
placeholders (`emit.style_layout_placeholders`), so a slide added in Slides looks like the others.
None of that is on a slide, so a sync that only merged slides left a retheme half done: the old
title bar, a layout picture, drawn over slides whose titles were in the new colours.

It is merged three ways like everything else. The base records what convert (or the last sync)
wrote there - per layout page, the decoration picture and each placeholder's style, and their
read-back - (`record`), the new PDF says what a fresh conversion would write (`ours_side`), and
the live deck says whether a person changed it since. What only the source changed is written;
what the person changed and the source did not stays; where both did, the deck's version stays
and a conflict says so (`plan`). What was not written stays in the base as it was, so the same
conflict comes back at the next sync rather than being forgotten.

A layout serves every slide on it, and the API cannot move a slide to another layout. Each layout
page takes the decoration a fresh conversion gives most of the slides on it; a slide that should
show another one is a warning. A layout nobody's slide uses keeps the decoration of its kind."""

from __future__ import annotations

import io
import json
from collections import Counter
from pathlib import Path

from . import identity, snapshot
from .merge import conflict_entry

DECORATION = "Theme decoration"   # the alt text emit gives the decoration picture (emit._add_decoration)
BOX_TOL = 0.5                     # pt: a layout object this far from where it was has been moved
THUMB = (32, 18)                  # colour thumbnail of a decoration picture, compared with a tolerance
THUMB_MAX, THUMB_MEAN = 12, 1.0   # levels: two thumbnails further apart than this are two pictures
PLACEHOLDERS = ("TITLE", "CENTERED_TITLE", "BODY")
STYLE_FIELD = {"TITLE": "title style", "CENTERED_TITLE": "title page title style", "BODY": "body style"}


# ---------------------------------------------------------------- small pieces

def key_text(key: tuple | None) -> str | None:
    """emit's background key ("color", "#fff") as the base spells it ("color:#fff")."""
    return f"{key[0]}:{key[1]}" if key else None


def group_of(layout_name: str | None) -> str:
    """The decoration group a b2s layout name belongs to (emit.plan_theme): "TITLE" for the title
    layout, "*" for every other, with the variant suffix of a copy ("TITLE_ONLY_V1" -> "*_V1")."""
    from .emit import VARIANT
    name = layout_name or ""
    kind, n = name, ""
    if VARIANT in name and name.rsplit(VARIANT, 1)[1].isdigit():
        kind, n = name.rsplit(VARIANT, 1)
    return ("TITLE" if kind == "TITLE" else "*") + (f"{VARIANT}{n}" if n else "")


def main_group(group: str) -> str:
    from .emit import VARIANT
    return group.split(VARIANT, 1)[0]


def picture_id(path: Path) -> dict:
    """What says which picture a decoration is: its bytes' digest, a colour thumbnail compared with
    a tolerance (a render of the same theme need not be byte for byte the same), and the pixel
    signature a live copy of it is compared with (Google re-encodes what it is given)."""
    from PIL import Image

    data = Path(path).read_bytes()
    with Image.open(io.BytesIO(data)) as img:
        rgba = img.convert("RGBA")
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    small = Image.alpha_composite(white, rgba).convert("RGB").resize(THUMB, Image.BOX)
    return {"sha1": identity.sha1(data), "thumb": small.tobytes().hex(), "signature": snapshot.signature(data)}


def same_picture_id(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if a.get("sha1") == b.get("sha1"):
        return True
    pa, pb = bytes.fromhex(a.get("thumb") or ""), bytes.fromhex(b.get("thumb") or "")
    if not pa or len(pa) != len(pb):
        return False
    d = [abs(x - y) for x, y in zip(pa, pb)]
    return max(d) <= THUMB_MAX and sum(d) / len(d) <= THUMB_MEAN


def same_spec(a: dict | None, b: dict | None) -> bool:
    """Two placeholder styles (emit.layout_style_spec entries) write the same thing."""
    def norm(v):
        if isinstance(v, float):
            return round(v, 2)
        if isinstance(v, dict):
            return {k: norm(x) for k, x in v.items()}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v
    return json.dumps(norm(a), sort_keys=True) == json.dumps(norm(b), sort_keys=True)


def style_hash(e: dict) -> str:
    """The styles of every run and paragraph of a page element's text. A layout placeholder holds
    nothing but a newline per list level, which the slides' `text_style_hash` leaves out (a run of
    newlines is no text), and that is exactly where a person's restyling of a layout lands."""
    parts = []
    for te in e.get("shape", {}).get("text", {}).get("textElements", []):
        if "textRun" in te:
            parts.append(["run", snapshot._text_style(te["textRun"].get("style", {}))])
        elif "paragraphMarker" in te:
            parts.append(["paragraph", snapshot._paragraph_style(te["paragraphMarker"])])
    return identity.sha1(json.dumps(parts, sort_keys=True))[:12]


def slim(rb: dict, e: dict) -> dict:
    """The part of a layout object's read-back a person's edit shows in (`e`: the element as read)."""
    return {"box": rb.get("box"), "style": style_hash(e), "contentHash": (rb.get("image") or {}).get("contentHash")}


def moved(a: list | None, b: list | None) -> bool:
    return a is None or b is None or max(abs(x - y) for x, y in zip(a, b)) > BOX_TOL


def texts_digest(texts: list[dict]) -> str:
    """What classify's `layout_texts` (the header and footer words every slide shares, which
    `emit.write_layout_texts` puts on every layout) say and look like."""
    return identity.sha1(json.dumps(identity.normalise_ir(texts), sort_keys=True))[:12]


def texts_says(texts: list[dict]) -> list[str]:
    return [identity.plain_text(t) for t in texts]


def live_texts(pres: dict) -> dict[str, dict]:
    """The layout texts on the deck's layouts: object id -> {page, text, box, style}."""
    from .emit import LAYOUT_TEXT_PREFIX
    out = {}
    for page in pres.get("layouts", []):
        objects = snapshot.read_slide(page)["objects"]
        for e in page.get("pageElements", []):
            if e["objectId"].startswith(LAYOUT_TEXT_PREFIX) and e["objectId"] in objects:
                rb = objects[e["objectId"]]
                out[e["objectId"]] = {"page": page["objectId"], "text": rb.get("text") or "", **slim(rb, e)}
    return out


def texts_entry(texts: list[dict], pres: dict) -> dict:
    """The base's record of the layout texts: what they said and what the deck held after writing them."""
    return {"digest": texts_digest(texts), "says": texts_says(texts), "objects": live_texts(pres)}


def texts_edited(was: dict, now: dict) -> list[str]:
    """How the deck's layout texts differ from what convert (or the last sync) left there."""
    out = []
    for oid in sorted(set(was) | set(now)):
        a, b = was.get(oid), now.get(oid)
        if a is None:
            out.append(f"{oid} added")
        elif b is None:
            out.append(f"{oid} deleted")
        elif a["text"].rstrip("\n") != b["text"].rstrip("\n"):
            out.append(f"{oid} retyped: {b['text'].strip()!r}")
        elif moved(a["box"], b["box"]):
            out.append(f"{oid} moved to {[round(v, 1) for v in b['box']]}")
        elif a["style"] != b["style"]:
            out.append(f"{oid} restyled")
    return out


def page_name(page: dict) -> str:
    props = page.get("layoutProperties") or {}
    return props.get("displayName") or props.get("name") or page["objectId"]


def spec_says(spec: dict | None, kind: str | None = None) -> dict | None:
    """A placeholder style as a report shows it."""
    if not spec:
        return None
    style = spec.get("style") or {}
    colour = ((style.get("foregroundColor") or {}).get("opaqueColor") or {}).get("rgbColor")
    out = {"font": (style.get("weightedFontFamily") or {}).get("fontFamily") or style.get("fontFamily"),
           "size": (style.get("fontSize") or {}).get("magnitude"),
           "color": "#" + "".join(f"{round(colour.get(c, 0) * 255):02x}" for c in ("red", "green", "blue")) if colour else None,
           "align": spec.get("align")}
    if spec.get("box"):
        out["box"] = [round(v, 1) for v in spec["box"]]
    return out


def picture_says(pid: dict | None, path=None) -> str:
    if pid is None:
        return "no theme picture"
    return f"theme picture {pid['sha1'][:10]}" + (f" ({Path(path).name})" if path else "")


# ---------------------------------------------------------------- what convert wrote

def layout_groups(pres: dict, slide_groups: dict[str, list[str]]) -> dict[str, str]:
    """layout page id -> the decoration group it carries: the one most slides on it have, else
    (a layout no slide uses) the one emit gives its kind."""
    out = {}
    for layout in pres.get("layouts", []):
        groups = slide_groups.get(layout["objectId"])
        if groups:
            out[layout["objectId"]] = Counter(groups).most_common(1)[0][0]
        else:
            out[layout["objectId"]] = "TITLE" if (layout.get("layoutProperties") or {}).get("name") == "TITLE" else "*"
    return out


def page_entry(page: dict, group: str, picture: dict | None, spec: dict) -> dict:
    """One layout or master page as the base records it."""
    objects = snapshot.read_slide(page)["objects"]
    entry = {"name": page_name(page), "group": group, "decoration": None, "placeholders": {}}
    for e in page.get("pageElements", []):
        if "image" in e and (e.get("description") or "") == DECORATION and entry["decoration"] is None:
            entry["decoration"] = {"oid": e["objectId"], "picture": picture, "readback": slim(objects[e["objectId"]], e)}
        kind = e.get("shape", {}).get("placeholder", {}).get("type")
        if kind in PLACEHOLDERS and spec.get(kind) is not None:
            entry["placeholders"][e["objectId"]] = {"kind": kind, "spec": spec[kind], "readback": slim(objects[e["objectId"]], e)}
    return entry


def record(deck: dict, out: Path, pres: dict, state: dict) -> dict | None:
    """The base's `theme` after `convert`: what emit wrote on the master and the layouts of `pres`
    (the deck read after it was made). `deck` / `state`: emit's plan.deck and emit.json state."""
    from .emit import PPTX_TITLE_DY, FontMapper, layout_style_spec, master_plan, slide_layout

    if not deck.get("slides") or not pres.get("masters"):
        return None
    st = state.get("theme")
    mp = master_plan(deck, out, theme=None)
    fill = ("color", st["master"]) if st and st.get("master") else mp["fill"]
    scale = state.get("scale") or 1.0
    spec = layout_style_spec(deck, scale, FontMapper(), PPTX_TITLE_DY, mp["ground"])
    pictures = {g: (picture_id(out / p) if p else None) for g, p in ((st or {}).get("decorations") or {}).items()}
    layout_of = {s["objectId"]: (s.get("slideProperties") or {}).get("layoutObjectId") for s in pres.get("slides", [])}
    slide_groups: dict[str, list[str]] = {}
    for s, emitted in zip(deck["slides"], state.get("slides", [])):
        name = ((st or {}).get("layouts") or {}).get(str(s["page"])) or slide_layout(s)[0]
        lid = layout_of.get(emitted.get("objectId"))
        if lid:
            slide_groups.setdefault(lid, []).append(group_of(name))
    groups = layout_groups(pres, slide_groups)
    master = pres["masters"][0]
    readback = snapshot.background(master)
    if "picture" in readback and fill[0] == "png" and fill in mp["bg_file"]:
        readback["signature"] = picture_id(mp["bg_file"][fill])["signature"]
    pages = {master["objectId"]: page_entry(master, "master", None, spec)}
    for layout in pres.get("layouts", []):
        g = groups[layout["objectId"]]
        pages[layout["objectId"]] = page_entry(layout, g, pictures.get(g, pictures.get(main_group(g))), spec)
    return {"fill": key_text(fill), "shared": key_text(mp["shared"]),
            "master": {"objectId": master["objectId"], "readback": readback}, "pages": pages,
            "texts": texts_entry(deck.get("layout_texts", []), pres)}


# ---------------------------------------------------------------- what the new PDF says

def ours_side(ours: dict) -> dict:
    """What a fresh conversion of the new PDF would write on the master and the layouts."""
    from .emit import PPTX_TITLE_DY, layout_style_spec, master_plan, slide_layout

    deck, out, plan = ours["deck"], Path(ours["out"]), ours["plan"]
    mp = master_plan(deck, out)
    theme = mp["theme"]
    spec = layout_style_spec(deck, plan.scale, plan.fonts, PPTX_TITLE_DY, mp["ground"])
    pictures = {g: ({**picture_id(p), "path": str(p)} if p else None) for g, p in ((theme or {}).get("decorations") or {}).items()}
    groups = {s["page"]: group_of(theme["layouts"][s["page"]] if theme else slide_layout(s)[0]) for s in deck["slides"]}
    return {"fill": key_text(mp["fill"]), "fill_file": str(mp["bg_file"][mp["fill"]]) if mp["fill"] in mp["bg_file"] else None,
            "shared": key_text(mp["shared"]), "spec": spec, "pictures": pictures, "groups": groups,
            "texts": deck.get("layout_texts", [])}


def ours_picture(side: dict, group: str) -> dict | None:
    pictures = side["pictures"]
    return pictures[group] if group in pictures else pictures.get(main_group(group))


# ---------------------------------------------------------------- the merge

def live_signature(url: str | None, oid: str | None = None, pictures=None) -> str | None:
    """The signature of a live picture of the master or a layout (`oid`: its image's id, or the
    page's for its background): read through `pictures` (`deck_pictures.LivePictures`, which falls
    back to a Drive export) when given, else downloaded from `url`."""
    if pictures is not None and oid:
        data = pictures.get([oid]).get(oid)
    else:
        data = snapshot._download(url) if url else None
    return snapshot.signature(data) if data else None


def plan(base: dict, side: dict, ours: dict, pres: dict, tok: str, picture_url, new_id, pictures=None) -> dict:
    """What to write on the master and the layouts, and what to say about it.

    `ours`: build_ours' answer (its slides are paired with the base's); `pres`: the live deck;
    `picture_url(path)`: the staging URL (or its marker) of a local picture; `new_id(page)`: an
    object id for a decoration picture this sync creates; `pictures`: how a live picture whose URL
    changed is read (`live_signature`).

    Returns {"requests" (sent before any slide's), "cleanup" (object ids deleted last), "stage"
    ({path: None | "background"}), "applied", "conflicts", "warnings", "page_group" (layout page id
    -> the group it serves now), "written" ({"master": fill, oid: value}: what went out, for
    `new_record`), "pending" (placeholder id -> the style written, for an interrupted run),
    "pinned" (slide objects given their inherited style explicitly, `inherited_pins`)}."""
    rec = base["theme"]
    out = {"requests": [], "cleanup": [], "stage": {}, "applied": [], "conflicts": [], "warnings": [],
           "page_group": {}, "written": {}, "pending": {}, "pinned": []}
    pending = ((base.get("pending") or {}).get("theme") or {})
    styling: dict[str, dict] = {}   # placeholder id -> the spec this run writes
    pages = {p["objectId"]: p for p in pres.get("masters", [])[:1] + pres.get("layouts", [])}

    # ---- which decoration each layout serves now
    live_layout = {s["objectId"]: (s.get("slideProperties") or {}).get("layoutObjectId") for s in pres.get("slides", [])}
    ours_pages = [s["page"] for s in ours["deck"]["slides"]]
    on_layout: dict[str, list[tuple[str, str]]] = {}   # layout -> [(slide key, ours group)]
    for j, i in ours.get("pairs", {}).items():
        b = base["slides"][i]
        lid = live_layout.get(b.get("objectId"))
        if lid:
            on_layout.setdefault(lid, []).append((ours["slides"][j]["key"], side["groups"][ours_pages[j]]))
    for pid, entry in rec["pages"].items():
        if entry["group"] == "master" or pid not in pages:
            continue
        here = on_layout.get(pid)
        g = Counter(x for _, x in here).most_common(1)[0][0] if here else entry["group"]
        out["page_group"][pid] = g
        for skey, sg in here or []:
            if sg != g and not same_picture_id(ours_picture(side, sg), ours_picture(side, g)):
                out["warnings"].append(
                    f"slide {skey}: the new version shows another theme decoration on this slide than on the "
                    f"others of its layout ({entry['name']}), which a fresh conversion puts on a copy of that layout; "
                    f"a sync cannot move a live slide to another layout, so it shows its layout's decoration")

    def conflict(where: str, element: str | None, field: str, b, o, t) -> None:
        entry, _ = conflict_entry(None, where, element, field, b, o, t)
        out["conflicts"].append(entry)

    # ---- the master's background
    live_master = pages.get(rec["master"]["objectId"])
    if live_master is not None and side["fill"] != rec["fill"]:
        now = snapshot.background(live_master)
        was = rec["master"]["readback"]
        edited = not snapshot.same_background(was, now)
        if edited and "picture" in now and "picture" in was and was.get("signature"):
            url = live_master.get("pageProperties", {}).get("pageBackgroundFill", {}).get("stretchedPictureFill", {}).get("contentUrl")
            edited = not snapshot.signatures_match(was["signature"], live_signature(url, live_master["objectId"], pictures))
        converged = side["fill"].startswith("color:") and now.get("color") == side["fill"][6:]
        if converged:
            out["written"]["master"] = side["fill"]
        elif edited:
            conflict("master", None, "master background", rec["fill"], side["fill"], now)
        else:
            mid = live_master["objectId"]
            if side["fill"].startswith("color:"):
                from .sync import api_colour
                out["requests"].append({"updatePageProperties": {
                    "objectId": mid, "fields": "pageBackgroundFill.solidFill.color",
                    "pageProperties": {"pageBackgroundFill": {"solidFill": {"color": api_colour(side["fill"][6:])}}}}})
            else:
                out["stage"][side["fill_file"]] = "background"
                out["requests"].append({"updatePageProperties": {
                    "objectId": mid, "fields": "pageBackgroundFill.stretchedPictureFill.contentUrl",
                    "pageProperties": {"pageBackgroundFill": {"stretchedPictureFill": {
                        "contentUrl": picture_url(side["fill_file"])}}}}})
            out["written"]["master"] = side["fill"]
            out["applied"].append({"slide": "master", "element": None, "fields": ["master background"], "page": mid})

    # ---- each page: its decoration picture and its placeholders
    for pid, entry in rec["pages"].items():
        page = pages.get(pid)
        if page is None:
            continue   # (a layout the person deleted: nothing of the theme to keep on it)
        where = "master" if entry["group"] == "master" else f"layout {entry['name']}"
        live = {e["objectId"]: e for e in page.get("pageElements", [])}
        objects = snapshot.read_slide(page)["objects"]
        if entry["group"] != "master":
            deco = entry.get("decoration")
            was = deco["picture"] if deco else None
            now = ours_picture(side, out["page_group"].get(pid, entry["group"]))
            if not same_picture_id(was, now):
                if deco is None:
                    # emit left this layout without a picture; the new theme has one for it
                    oid = new_id(pid)
                    out["stage"][now["path"]] = None
                    from .gslides import emu
                    w, h = snapshot.page_size(pres)
                    out["requests"] += [
                        {"createImage": {"objectId": oid, "url": picture_url(now["path"]), "elementProperties": {
                            "pageObjectId": pid, "size": {"width": emu(w), "height": emu(h)},
                            "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "EMU"}}}},
                        {"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "SEND_TO_BACK"}},
                        {"updatePageElementAltText": {"objectId": oid, "description": DECORATION}}]
                    out["written"][oid] = {"page": pid, "picture": now}
                    out["applied"].append({"slide": where, "element": oid, "fields": ["theme decoration"], "page": pid})
                else:
                    oid = deco["oid"]
                    rb = objects.get(oid)
                    theirs = None
                    if oid not in live:
                        theirs = "deleted"
                    elif moved(deco["readback"]["box"], rb["box"]):
                        theirs = f"moved to {[round(v, 1) for v in rb['box']]}"
                    elif (rb.get("image") or {}).get("contentHash") != deco["readback"].get("contentHash"):
                        # Google hands out new URLs for the same picture: only its pixels tell
                        sig = live_signature(live[oid]["image"].get("contentUrl"), oid, pictures)
                        if now is not None and snapshot.signatures_match(sig, now.get("signature")):
                            out["written"][oid] = {"page": pid, "picture": now}   # (an interrupted sync wrote it)
                        elif not snapshot.signatures_match(sig, (was or {}).get("signature")):
                            theirs = "another picture"
                    if oid in out["written"]:
                        pass
                    elif theirs is not None:
                        conflict(where, oid, "theme decoration", picture_says(was), picture_says(now, now and now["path"]), theirs)
                    elif now is None:
                        out["cleanup"].append(oid)
                        out["written"][oid] = {"page": pid, "picture": None}
                        out["applied"].append({"slide": where, "element": oid, "fields": ["theme decoration"], "how": "removed",
                                               "page": pid})
                    else:
                        out["stage"][now["path"]] = None
                        out["requests"] += [
                            {"replaceImage": {"imageObjectId": oid, "url": picture_url(now["path"]),
                                              "imageReplaceMethod": "CENTER_INSIDE"}},
                            {"updatePageElementAltText": {"objectId": oid, "description": DECORATION}}]
                        out["written"][oid] = {"page": pid, "picture": now}
                        out["applied"].append({"slide": where, "element": oid, "fields": ["theme decoration"], "page": pid})
        for oid, ph in entry["placeholders"].items():
            if oid not in live:
                continue
            now = side["spec"].get(ph["kind"])
            if now is None or same_spec(ph["spec"], now):
                continue
            rb = objects[oid]
            restyled = ph["readback"]["style"] != style_hash(live[oid])
            if moved(ph["readback"]["box"], rb["box"]) or restyled:
                if same_spec(pending.get(oid), now):
                    out["written"][oid] = {"page": pid, "spec": now}   # (an interrupted sync wrote it)
                    continue
                theirs = {}
                if moved(ph["readback"]["box"], rb["box"]):
                    theirs["box"] = [round(v, 1) for v in rb["box"]]
                if restyled:
                    theirs["style"] = "restyled in the deck"
                conflict(where, oid, STYLE_FIELD[ph["kind"]], spec_says(ph["spec"]), spec_says(now), theirs)
                continue
            from .emit import layout_placeholder_requests
            out["requests"] += layout_placeholder_requests(now, live[oid])
            styling[oid] = now
            out["written"][oid] = {"page": pid, "spec": now}
            out["pending"][oid] = now
            out["applied"].append({"slide": where, "element": oid, "fields": [STYLE_FIELD[ph["kind"]]]})
    plan_texts(rec, side, ours, pres, pending, out)
    # After the layouts' requests, in the same batch (an interrupted run that restyled did both):
    # Slides drops a run property equal to what the run inherits, so a pin written while the
    # layout still says the same value is gone before the layout changes.
    pins, out["pinned"] = inherited_pins(pres, styling, {s.get("objectId") for s in base["slides"]})
    out["requests"] += pins
    return out


TEXTS_FIELD = "header and footer"


def plan_texts(rec: dict, side: dict, ours: dict, pres: dict, pending: dict, out: dict) -> None:
    """The header and footer words every slide shares (`\\author`, `\\title`, `\\date` in a
    footline), which convert writes once per layout (`emit.write_layout_texts`), merged three ways
    into `out` (plan's answer). Nothing merged them before: a new `\\date`, or a colour theme that
    turned the footline's words white, reached the slides' own elements and the layouts' bars but
    left the words on every slide as they were - the old theme's red on the new theme's navy
    (edit hunt h5a, 2026-09-25). They are rewritten whole, like convert does, when the source
    changed them and the deck did not; a deck whose layout texts a person edited keeps them, with a
    conflict. A base from before they were recorded cannot tell a person's edit from convert's
    words: they are left alone, with a warning when the source's words differ from the deck's."""
    new = side.get("texts") or []
    now = live_texts(pres)
    says = texts_says(new)
    texts = rec.get("texts")
    if texts is None:
        shown = sorted({t["text"].strip() for t in now.values()})
        if shown != sorted({s.strip() for s in says}):
            out["warnings"].append(
                "the new version's header and footer words (" + ", ".join(repr(s) for s in says if s.strip()) +
                ") differ from the ones on the deck's layouts, but the deck's sync base is older than their "
                "sync and does not record what convert wrote there: they were left as they are (edit them in "
                "Slides with View > Theme builder)")
        return
    digest = texts_digest(new)
    if digest == texts["digest"]:
        return
    edits = texts_edited(texts["objects"], now)
    if edits and pending.get(TEXTS_FIELD) == digest:
        out["written"][TEXTS_FIELD] = {"digest": digest, "says": says}   # (an interrupted sync wrote them)
        return
    if edits:
        entry, _ = conflict_entry(None, "layouts", None, TEXTS_FIELD, texts["says"], says, edits)
        out["conflicts"].append(entry)
        return
    from .emit import LAYOUT_TEXT_PREFIX, text_box_requests
    reqs = [{"deleteObject": {"objectId": oid}} for oid in now]
    for li, layout in enumerate(pres.get("layouts", [])):
        for ti, el in enumerate(new):
            reqs += text_box_requests(el, layout["objectId"], f"{LAYOUT_TEXT_PREFIX}{li}_{ti}",
                                      ours["plan"].scale, ours["plan"].fonts)
    out["requests"] += reqs
    out["written"][TEXTS_FIELD] = {"digest": digest, "says": says}
    out["pending"][TEXTS_FIELD] = digest
    out["applied"].append({"slide": "layouts", "element": None, "fields": [TEXTS_FIELD]})


def _level_runs(e: dict) -> tuple[list[dict], list[dict]]:
    """A master or layout placeholder's style per list level: (run styles, paragraph styles), the
    "\\n" each level holds."""
    runs, paras = [], []
    for te in e.get("shape", {}).get("text", {}).get("textElements", []):
        if "textRun" in te:
            runs.append(te["textRun"].get("style") or {})
        elif "paragraphMarker" in te:
            paras.append(te["paragraphMarker"].get("style") or {})
    return runs, paras


def _inherited(chain: list[dict], level: int, field: str, paragraph: bool = False):
    for e in chain:
        runs, paras = _level_runs(e)
        styles = paras if paragraph else runs
        if styles:
            s = styles[min(level, len(styles) - 1)]
            if field in s:
                return s[field]
    return None


def inherited_pins(pres: dict, restyled: dict[str, dict], slide_ids: set) -> tuple[list[dict], list[str]]:
    """Requests that write onto the converter's slides, explicitly, the style their placeholder
    text now takes from a master or layout placeholder this sync restyles (`restyled`: object id
    -> the spec written), and the ids of the objects they touch.

    Google's .pptx import drops a run property equal to what the placeholder inherits: beamer
    sets the title page's title and the frame titles at one size, so the title page's title
    comes out with no size of its own and would take a retheme's new frame-title size - which a
    fresh conversion, whose .pptx says the old size, does not. The values are those rendered
    before the sync and go out after the layout requests (Slides drops a property equal to the
    inherited one, so written earlier they would vanish): the slide looks the same, and what its
    element says is left to the element's own merge. A frame title the merge refills with the new
    style gets it back from the layout, as a fresh conversion's does."""
    if not restyled:
        return [], []
    placeholders = {e["objectId"]: e for p in pres.get("masters", []) + pres.get("layouts", [])
                    for e in p.get("pageElements", []) if e.get("shape", {}).get("placeholder")}
    reqs, touched = [], []
    for slide in pres.get("slides", []):
        if slide.get("objectId") not in slide_ids:
            continue
        for e in slide.get("pageElements", []):
            chain, parent = [], e.get("shape", {}).get("placeholder", {}).get("parentObjectId")
            while parent in placeholders and parent not in chain:
                chain.append(placeholders[parent])
                parent = placeholders[parent]["shape"]["placeholder"].get("parentObjectId")
            written = [restyled[c["objectId"]] for c in chain if c["objectId"] in restyled]
            if not written:
                continue
            fields = list(dict.fromkeys(f for spec in written for f in spec["fields"].split(",")))
            elements = e["shape"].get("text", {}).get("textElements", [])
            if not elements:
                continue
            end_all = elements[-1].get("endIndex", 0) - 1       # (the last newline is Slides' own)
            level, before = 0, len(reqs)
            for te in elements:
                a, b = te.get("startIndex", 0), min(te.get("endIndex", 0), end_all)
                if "paragraphMarker" in te:
                    level = ((te["paragraphMarker"].get("bullet") or {}).get("nestingLevel") or 0)
                    style = te["paragraphMarker"].get("style") or {}
                    if "alignment" not in style and any(spec.get("align") for spec in written) and b > a:
                        value = _inherited(chain, level, "alignment", paragraph=True)
                        if value:
                            reqs.append({"updateParagraphStyle": {
                                "objectId": e["objectId"], "fields": "alignment", "style": {"alignment": value},
                                "textRange": {"type": "FIXED_RANGE", "startIndex": a, "endIndex": b}}})
                elif "textRun" in te and b > a:
                    style = te["textRun"].get("style") or {}
                    pin = {f: v for f in fields if f not in style
                           for v in [_inherited(chain, level, f)] if v is not None}
                    if pin:
                        reqs.append({"updateTextStyle": {
                            "objectId": e["objectId"], "fields": ",".join(pin), "style": pin,
                            "textRange": {"type": "FIXED_RANGE", "startIndex": a, "endIndex": b}}})
            if len(reqs) > before:
                touched.append(e["objectId"])
    return reqs, touched


def new_record(rec: dict, side: dict, done: dict, raw: dict | None) -> dict:
    """The base's `theme` after a sync: what was written (`done`, plan's "written") takes the
    source's value and the read-back after the write (`raw`: the deck read after it); everything
    else stays as the base had it - a conflict comes back next time, a deck edit stays one."""
    rec = json.loads(json.dumps(rec))
    pages = {p["objectId"]: p for p in (raw or {}).get("masters", [])[:1] + (raw or {}).get("layouts", [])}
    objects = {pid: {oid: slim(rb, e) for oid, rb in snapshot.read_slide(p)["objects"].items()
                     for e in p.get("pageElements", []) if e["objectId"] == oid} for pid, p in pages.items()}
    if "master" in done:
        rec["fill"] = done["master"]
        page = pages.get(rec["master"]["objectId"])
        if page is not None:
            readback = snapshot.background(page)
            if "picture" in readback and side.get("fill_file"):
                readback["signature"] = picture_id(Path(side["fill_file"]))["signature"]
            rec["master"]["readback"] = readback
    rec["shared"] = side["shared"]
    if TEXTS_FIELD in done and rec.get("texts") is not None and raw:
        rec["texts"] = {**done[TEXTS_FIELD], "objects": live_texts(raw or {})}
    for oid, what in done.items():
        if oid in ("master", TEXTS_FIELD):
            continue
        entry = rec["pages"].get(what["page"])
        if entry is None:
            continue
        rb = objects.get(what["page"], {}).get(oid)
        if "picture" in what:
            if what["picture"] is None:
                entry["decoration"] = None
            else:
                picture = {k: v for k, v in what["picture"].items() if k != "path"}
                entry["decoration"] = {"oid": oid, "picture": picture,
                                       "readback": rb or (entry.get("decoration") or {}).get("readback")}
        elif "spec" in what and oid in entry["placeholders"]:
            entry["placeholders"][oid]["spec"] = what["spec"]
            if rb:
                entry["placeholders"][oid]["readback"] = rb
    return rec


def old_base_warning(base: dict, side: dict) -> str | None:
    """A base from before theme sync records nothing of the layouts: they are left alone."""
    if side["shared"] == base.get("master_background"):
        return None
    return ("the new version's theme (the background most slides share) differs from the one this deck was "
            "converted with, but the deck's sync base is older than theme sync and does not record what convert "
            "wrote on the master and the layouts: they were left as they are, and a slide whose background "
            "changed got the new one as a picture of its own")
