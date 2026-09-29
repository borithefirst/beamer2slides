"""The presentation and its theme: the import, the master's ground, layout pictures, placeholder
styles and texts, and the same as data (theme_sync).
"""

import hashlib
import io
import json
from collections import Counter
from pathlib import Path

import numpy as np

from .arrays import RGB
from .emit_metrics import ASCENT_EM, BASELINE_A, LINE_EM, PAD_X, PPTX_TITLE_DY, SLIDE_W, FontMapper, rgb
from .emit_pptx import THEME_VARIANTS, VARIANT, api_error
from .emit_text import text_box_requests
from .gapi import HttpError, media_upload
from .gslides import EMU_PER_PT, execute


PPTX_MIME ="application/vnd.openxmlformats-officedocument.presentationml.presentation"


def subtitle_element(slide: dict, title_idx: int) -> int | None:
    """On the title page, the biggest plain text box below the title (authors, institute,
    date) goes into the TITLE layout's subtitle placeholder."""
    if not slide.get("title_page"):
        return None
    title_bottom = slide["elements"][title_idx]["bbox"][3]
    below = [(sum(len(r["text"]) for p in e["paragraphs"] for r in p["runs"]), i)
             for i, e in enumerate(slide["elements"])
             if e["kind"] == "text" and e["role"] == "body" and e["bbox"][1] > title_bottom
             and not any(p["bullet"] for p in e["paragraphs"])]
    return max(below)[1] if below else None


def title_element(slide: dict) -> int | None:
    """Index of the element that becomes the slide's title placeholder."""
    for i, el in enumerate(slide["elements"]):
        if el["kind"] == "text" and el["role"] == "title":
            return i
    return None


def import_presentation(slides, drive, title: str, page_w: float, page_h: float, pptx: io.BytesIO,
                        existing: str | None) -> dict:
    """A new deck from the .pptx, or an existing one (same URL) with its content replaced by it."""
    media = media_upload(pptx, PPTX_MIME)
    if existing:
        execute(drive.files().update(fileId=existing, media_body=media, fields="id"))
        pid = existing
    else:
        from .drive_folder import place
        pid = execute(drive.files().create(
            body=place({"name": title, "mimeType": "application/vnd.google-apps.presentation"}, drive),
            media_body=media, fields="id"))["id"]
    pres = execute(slides.presentations().get(presentationId=pid))
    got = pres["pageSize"]["height"]["magnitude"] / pres["pageSize"]["width"]["magnitude"]
    if abs(got - page_h / page_w) > 0.003:
        raise RuntimeError(f"page aspect {got:.4f} != PDF aspect {page_h / page_w:.4f}")
    return pres


def background_key(slide: dict, out: Path) -> tuple:
    if slide.get("background_color"):
        return ("color", slide["background_color"].lower())
    return ("png", hashlib.sha1((out / slide["background"]).read_bytes()).hexdigest())


def plan_theme(deck: dict, out: Path, bg_key: dict) -> dict | None:
    """Theme decoration on the layouts (render.theme_decoration), so a background colour set in
    Slides changes only the page ground and new slides get the decoration too. Title pages (TITLE
    layout) and the other slides get one each. Backgrounds that don't show it (a standout frame, a
    closing page without the headline) get the decoration they share among themselves on a copy of
    their layout (VARIANT suffix; up to THEME_VARIANTS copies, the last without decoration). Slide
    backgrounds stay as they are: the decoration drawn over a background showing it changes nothing.

    Returns None without decoration, else {"ground": "#rrggbb", "decorations": {"TITLE" (title layout),
    "*" (every other layout), "TITLE_V1", "*_V1", ... (copies): Path or None}, "layouts": {page:
    layout name, e.g. "TITLE_ONLY_V1"}, "exact": {"TITLE", "*": background key that is exactly the
    ground plus the decoration, or None}}."""
    from PIL import Image

    from .render import page_ground, save_png, theme_decoration

    counts = Counter(bg_key.values())
    groups = {"TITLE": [s for s in deck["slides"] if slide_layout(s)[0] == "TITLE"]}
    groups["*"] = [s for s in deck["slides"] if slide_layout(s)[0] != "TITLE"]
    load = lambda s: np.asarray(Image.open(out / s["background"]).convert("RGB"))
    main = groups["*"] or groups["TITLE"]
    if not main:
        return None
    for old in (out / "backgrounds").glob("theme-*.png"):
        old.unlink()
    ground = page_ground(load(max(main, key=lambda s: counts[bg_key[s["page"]]])))
    theme = {"ground": "#" + "".join(f"{int(v):02x}" for v in ground), "decorations": {},
             "layouts": {s["page"]: slide_layout(s)[0] for s in deck["slides"]}, "exact": {}}
    for name, members in groups.items():
        for variant in range(THEME_VARIANTS + 1):
            if not members:
                break
            key = name if variant == 0 else f"{name}{VARIANT}{variant}"
            keys = sorted(dict.fromkeys(bg_key[s["page"]] for s in members), key=lambda k: -counts[k])
            first = {k: next(s for s in members if bg_key[s["page"]] == k) for k in keys}
            picture, inside = None, [True] * len(keys)
            if variant < THEME_VARIANTS:
                picture, inside, exact = theme_decoration((load(first[k]) for k in keys), ground)
                inside = inside if picture is not None else [True] * len(keys)
                if variant == 0:
                    theme["exact"][name] = keys[0] if picture is not None and exact else None
            if picture is not None:
                path = out / "backgrounds" / f"theme-{'title' if name == 'TITLE' else 'main'}-{variant}.png"
                save_png(picture, path)
            theme["decorations"][key] = path if picture is not None else None
            if variant:
                for s in members:
                    if inside[keys.index(bg_key[s["page"]])]:
                        theme["layouts"][s["page"]] += f"{VARIANT}{variant}"
            members = [s for s in members if not inside[keys.index(bg_key[s["page"]])]]
    if not any(theme["decorations"].values()):
        return None
    if not groups["TITLE"]:
        theme["decorations"]["TITLE"] = theme["decorations"].get("*")  # (for title slides added in Slides)
    return theme


def master_ground(shared: tuple | None, files: dict, page_w: float):
    """bbox (PDF pt) -> the master background's median colour there (see background_key)."""
    if shared is None or shared[0] == "color":
        colour = shared[1] if shared else "#ffffff"
        return lambda bbox: colour
    from PIL import Image

    img: RGB = np.asarray(Image.open(files[shared]).convert("RGB"))
    k =img.shape[1] / page_w

    def ground(bbox: list[float]) -> str:
        x0, y0, x1, y1 = (max(0, round(v * k)) for v in bbox)
        area = img[y0:max(y1, y0 + 1), x0:max(x1, x0 + 1)].reshape(-1, 3)
        return "#" + "".join(f"{int(v):02x}" for v in np.median(area, axis=0)) if len(area) else "#ffffff"
    return ground


LAYOUT_TEXT_PREFIX = "b2s_L"


def write_layout_texts(slides, pid: str, texts: list[dict], scale: float, fonts: FontMapper) -> None:
    """Header/footer text shared by every slide goes onto the layouts our slides use, so
    it is edited once for the whole deck. Previous runs' layout texts are replaced."""
    pres = execute(slides.presentations().get(
        presentationId=pid, fields="layouts(objectId,layoutProperties,pageElements(objectId))"))
    layouts = pres.get("layouts", [])  # all of them: slides added later in Slides get the footer too
    # All old ones first: object IDs are unique across the whole presentation.
    reqs = [{"deleteObject": {"objectId": e["objectId"]}} for l in pres.get("layouts", [])
            for e in l.get("pageElements", []) if e["objectId"].startswith(LAYOUT_TEXT_PREFIX)]
    for li, layout in enumerate(layouts):
        for ti, el in enumerate(texts):
            reqs += text_box_requests(el, layout["objectId"], f"{LAYOUT_TEXT_PREFIX}{li}_{ti}", scale, fonts)
    if reqs:
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two #rrggbb colours."""
    def luminance(h: str) -> float:
        c = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


MIN_CONTRAST = 2.0


def readable_run(run: dict, slide: dict, bbox: list[float], ground) -> dict:
    """The run in a colour readable on a new slide: that is on the master background under `bbox`
    (`ground(bbox)`: its colour there), without the shapes of the converted slide. White title
    text on a native title panel becomes the panel's colour (else black or white)."""
    under = ground(bbox) if ground else None
    if under is None or contrast(run["color"], under) >= MIN_CONTRAST:
        return run
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    panels = [e["fill"] for e in reversed(slide["elements"]) if e["kind"] == "shape" and e.get("fill")
              and e["bbox"][0] <= cx <= e["bbox"][2] and e["bbox"][1] <= cy <= e["bbox"][3]]
    colour = next((c for c in panels if contrast(c, under) >= MIN_CONTRAST), None) or \
        max(("#000000", "#ffffff"), key=lambda c: contrast(c, under))
    return {**run, "color": colour}


def style_layout_placeholders(slides, pid: str, deck: dict, scale: float, fonts: FontMapper, dy: float,
                              ground=None) -> None:
    """Title and body placeholders of every layout take the deck's own look (font, size,
    colour, title position), so slides added later in Slides match the converted ones.
    `ground(bbox)`: the master background's colour under a PDF box, to keep text readable there."""
    texts = [(s, e) for s in deck["slides"] for e in s["elements"] if e["kind"] == "text" and e["paragraphs"][0]["runs"]]
    frame = next(((s, e) for s, e in texts if e["role"] == "title" and not s.get("title_page")), None)
    page = next(((s, e) for s, e in texts if e["role"] == "title" and s.get("title_page")), None) or frame
    frame_title, page_title = (frame or (None, None))[1], (page or (None, None))[1]
    title_runs = {id(e): readable_run(e["paragraphs"][0]["runs"][0], s, e["bbox"], ground)
                  for s, e in (pair for pair in (frame, page) if pair)}
    body_runs = Counter((r["font"], r["size"], r["color"], r["family"]) for s, e in texts if e["role"] == "body"
                        for p in e["paragraphs"] for r in p["runs"] for _ in range(len(r["text"])))
    body = None
    if body_runs:
        font, size, color, family = body_runs.most_common(1)[0][0]
        body = {"font": font, "size": size, "color": color, "family": family, "bold": False, "italic": False}
        w, h = deck["slides"][0]["size"]
        body = readable_run(body, {"elements": []}, [0.1 * w, 0.3 * h, 0.9 * w, 0.8 * h], ground)
    page_fields = "objectId,pageElements(objectId,size,transform,shape(placeholder/type,text/textElements))"
    pres = execute(slides.presentations().get(presentationId=pid, fields=f"masters({page_fields}),layouts({page_fields})"))
    reqs = []
    # The master too: layout placeholders inherit whatever style they don't set themselves.
    for layout in pres.get("masters", []) + pres.get("layouts", []):
        for pe in layout.get("pageElements", []):
            kind = pe.get("shape", {}).get("placeholder", {}).get("type")
            if kind in ("TITLE", "CENTERED_TITLE"):
                el = page_title if kind == "CENTERED_TITLE" else frame_title
                if el is None:
                    continue
                p = el["paragraphs"][0]
                run = title_runs[id(el)]
                z = fonts(run, scale)[1]
                x = p["text_x0"] * scale - PAD_X
                if p["align"] == "center":
                    x = min(x, 0.1 * SLIDE_W)
                w = SLIDE_W - 2 * max(x, 10)
                h = 2 * LINE_EM * z + 2 * BASELINE_A
                y = p["lines"][0]["baseline"] * scale - (BASELINE_A + ASCENT_EM * z) + dy
                reqs.append({"updatePageElementTransform": {"objectId": pe["objectId"], "applyMode": "ABSOLUTE", "transform": {
                    "scaleX": w / (pe["size"]["width"]["magnitude"] / EMU_PER_PT),
                    "scaleY": h / (pe["size"]["height"]["magnitude"] / EMU_PER_PT), "unit": "EMU",
                    "translateX": round(max(x, 10) * EMU_PER_PT), "translateY": round(max(0.0, y) * EMU_PER_PT)}}})
                reqs.append({"updateShapeProperties": {"objectId": pe["objectId"], "fields": "contentAlignment",
                                                       "shapeProperties": {"contentAlignment": "TOP"}}})
                align = {"left": "START", "center": "CENTER", "right": "END"}[p["align"]]
            elif kind == "BODY" and body:
                run, align = body, "START"
            else:
                continue
            if not pe["shape"].get("text", {}).get("textElements"):
                # Without any text (not even the imported "\n" per list level) a placeholder can't
                # be styled, and the API refuses to put text into layout placeholders.
                continue
            style, fields = fonts.text_style(run, scale)
            style["foregroundColor"] = rgb(run["color"])
            if "bold" not in fields:  # a weighted family: the layout's own bold (section header) would add to it
                style["bold"], fields = False, fields + ["bold"]
            reqs += [
                {"updateTextStyle": {"objectId": pe["objectId"], "textRange": {"type": "ALL"}, "style": style,
                                     "fields": ",".join(fields + ["foregroundColor"])}},
                {"updateParagraphStyle": {"objectId": pe["objectId"], "textRange": {"type": "ALL"},
                                          "style": {"alignment": align}, "fields": "alignment"}},
            ]
    for i in range(0, len(reqs), 200):
        try:
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs[i:i + 200]}))
        except HttpError as e:  # the deck's look for new slides is a nicety, never a reason to fail
            print(f"warning: could not style the layouts ({api_error(e)})")


def write_layouts(client, pid: str, deck: dict, scale: float, fonts: FontMapper, ground) -> None:
    """The whole of the layout and master work, as one job: what a deck's own look gives a slide
    somebody adds later in Slides. It reads and writes layout and master pages only, which is
    what lets it run on a thread of its own (`client()` gives that thread its own Slides client,
    `gslides.per_thread`) - but writing a layout is not the same as leaving the slides alone: a
    slide's TITLE placeholder inherits its layout parent's box until a content batch gives it one
    of its own, so this pass and that batch write one value and the later commit wins. It is
    joined before the first content batch goes out (`build_deck`, `tools/probe_layout_race.py`).

    Inside itself it stays serial, which is the one place in `build_deck` where that was measured
    rather than assumed: a layout write reaches every slide inheriting from it and Google charges
    for that, so more of this pass in the air is *slower*. Interleaved A/B on a 10-slide deck, the
    placeholder requests cut into four batches at once against one batch: 10.10 s against 4.95 s;
    the texts beside the placeholders rather than after them: 5.24 s against 3.96 s (18.6 s of
    conversion against 16.4 s). `tools/probe_batch_parallelism.py`'s finding - that several
    `batchUpdate`s may be in flight on one presentation and nothing is lost - is about slide
    content and does not carry here. Nor is there anything to win by reading less: merging the
    two passes' reads into one saves a round trip inside the pass and nothing at all outside it
    (17.17 s against 16.93 s over four interleaved pairs, which the arms split two each), because
    what is left of the pass hides behind phase 1, the placeholder read and `measure_places`."""
    slides = client()
    write_layout_texts(slides, pid, deck.get("layout_texts", []), scale, fonts)
    style_layout_placeholders(slides, pid, deck, scale, fonts, PPTX_TITLE_DY, ground)


def master_plan(deck: dict, out: Path, theme="plan") -> dict:
    """`build_deck`'s decisions about the master and the layouts, as data, for a sync that has to
    compare them with what convert wrote (`theme_sync`): {"bg_key": page -> background key,
    "bg_file": key -> file, "shared": the key most slides share (None: none shared), "theme":
    plan_theme's answer, "fill": the key on the master, "ground": master_ground}. `theme="plan"`
    plans the theme (and so rewrites out/backgrounds/theme-*.png, as plan_theme does); a theme
    passed in is taken as it is, None as no theme."""
    bg_key = {s["page"]: background_key(s, out) for s in deck["slides"]}
    bg_file = {bg_key[s["page"]]: out / s["background"] for s in deck["slides"] if not s.get("background_color")}
    counts = Counter(bg_key.values())
    shared = counts.most_common(1)[0][0] if counts and counts.most_common(1)[0][1] >= 2 else None
    if theme == "plan":
        theme = plan_theme(deck, out, bg_key)
    fill = shared or ("color", "#ffffff")
    if theme and theme.get("exact") is not None:
        group = lambda s: "TITLE" if slide_layout(s)[0] == "TITLE" else "*"
        if shared is None or all(theme["exact"].get(group(s)) == shared for s in deck["slides"] if bg_key[s["page"]] == shared):
            fill = ("color", theme["ground"])
    page_w = deck["slides"][0]["size"][0] if deck["slides"] else SLIDE_W
    return {"bg_key": bg_key, "bg_file": bg_file, "shared": shared, "theme": theme, "fill": fill,
            "ground": master_ground(shared, bg_file, page_w)}


def layout_style_spec(deck: dict, scale: float, fonts: FontMapper, dy: float, ground=None) -> dict:
    """What `style_layout_placeholders` writes into each kind of placeholder, as data a base can
    hold: {"TITLE" / "CENTERED_TITLE" / "BODY": {"box": [x, y, w, h] (slide pt; None for the
    body, whose box is left alone), "style", "fields", "align"}, or None where nothing is written}.
    `layout_placeholder_requests` turns one entry into the requests for one placeholder;
    tests/test_theme_sync.py holds the two to the requests `style_layout_placeholders` sends."""
    texts = [(s, e) for s in deck["slides"] for e in s["elements"] if e["kind"] == "text" and e["paragraphs"][0]["runs"]]
    frame = next(((s, e) for s, e in texts if e["role"] == "title" and not s.get("title_page")), None)
    page = next(((s, e) for s, e in texts if e["role"] == "title" and s.get("title_page")), None) or frame
    frame_title, page_title = (frame or (None, None))[1], (page or (None, None))[1]
    title_runs = {id(e): readable_run(e["paragraphs"][0]["runs"][0], s, e["bbox"], ground)
                  for s, e in (pair for pair in (frame, page) if pair)}
    body_runs = Counter((r["font"], r["size"], r["color"], r["family"]) for s, e in texts if e["role"] == "body"
                        for p in e["paragraphs"] for r in p["runs"] for _ in range(len(r["text"])))
    body = None
    if body_runs:
        font, size, color, family = body_runs.most_common(1)[0][0]
        body = {"font": font, "size": size, "color": color, "family": family, "bold": False, "italic": False}
        w, h = deck["slides"][0]["size"]
        body = readable_run(body, {"elements": []}, [0.1 * w, 0.3 * h, 0.9 * w, 0.8 * h], ground)

    def styled(run: dict) -> tuple[dict, str]:
        style, fields = fonts.text_style(run, scale)
        style["foregroundColor"] = rgb(run["color"])
        if "bold" not in fields:
            style["bold"], fields = False, fields + ["bold"]
        return style, ",".join(fields + ["foregroundColor"])

    spec: dict = {}
    for kind, el in (("TITLE", frame_title), ("CENTERED_TITLE", page_title)):
        if el is None:
            spec[kind] = None
            continue
        p = el["paragraphs"][0]
        run = title_runs[id(el)]
        z = fonts(run, scale)[1]
        x = p["text_x0"] * scale - PAD_X
        if p["align"] == "center":
            x = min(x, 0.1 * SLIDE_W)
        w = SLIDE_W - 2 * max(x, 10)
        h = 2 * LINE_EM * z + 2 * BASELINE_A
        y = p["lines"][0]["baseline"] * scale - (BASELINE_A + ASCENT_EM * z) + dy
        style, fields = styled(run)
        spec[kind] = {"box": [max(x, 10), max(0.0, y), w, h], "style": style, "fields": fields,
                      "align": {"left": "START", "center": "CENTER", "right": "END"}[p["align"]]}
    if body:
        style, fields = styled(body)
        spec["BODY"] = {"box": None, "style": style, "fields": fields, "align": "START"}
    else:
        spec["BODY"] = None
    return json.loads(json.dumps(spec))  # (plain data: nothing shared with the caller's runs)


def layout_placeholder_requests(entry: dict, pe: dict) -> list[dict]:
    """The requests `style_layout_placeholders` sends for one placeholder `pe` (a layout or master
    page element as presentations.get gives it) from its `layout_style_spec` entry."""
    reqs = []
    if entry.get("box") is not None:
        x, y, w, h = entry["box"]
        reqs.append({"updatePageElementTransform": {"objectId": pe["objectId"], "applyMode": "ABSOLUTE", "transform": {
            "scaleX": w / (pe["size"]["width"]["magnitude"] / EMU_PER_PT),
            "scaleY": h / (pe["size"]["height"]["magnitude"] / EMU_PER_PT), "unit": "EMU",
            "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}}})
        reqs.append({"updateShapeProperties": {"objectId": pe["objectId"], "fields": "contentAlignment",
                                               "shapeProperties": {"contentAlignment": "TOP"}}})
    if pe.get("shape", {}).get("text", {}).get("textElements"):
        reqs += [
            {"updateTextStyle": {"objectId": pe["objectId"], "textRange": {"type": "ALL"},
                                 "style": json.loads(json.dumps(entry["style"])), "fields": entry["fields"]}},
            {"updateParagraphStyle": {"objectId": pe["objectId"], "textRange": {"type": "ALL"},
                                      "style": {"alignment": entry["align"]}, "fields": "alignment"}},
        ]
    return reqs


def slide_layout(slide: dict) -> tuple[str, str | None]:
    """The title page uses the TITLE layout (centered title), frames with a title TITLE_ONLY."""
    if title_element(slide) is None:
        return "BLANK", None
    return ("TITLE", "CENTERED_TITLE") if slide.get("title_page") else ("TITLE_ONLY", "TITLE")


LAYOUT_PLACEHOLDERS = {"TITLE": ["CENTERED_TITLE", "SUBTITLE"], "TITLE_ONLY": ["TITLE"], "BLANK": []}
