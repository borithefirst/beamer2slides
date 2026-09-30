"""The presentation and its theme: the import, the master's ground, layout pictures, placeholder
styles and texts, and the same as data (theme_sync).

The decisions are records (`ThemePlan`, `MasterPlan`, `PlaceholderStyle`) that `build_deck` reads.
theme_sync still reads them as the dicts it stored in a base: `plan_theme`, `master_plan`,
`layout_style_spec` and `layout_placeholder_requests` are its entries, each over its typed twin.
"""

import copy
import hashlib
import io
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TypedDict

import numpy as np

from .arrays import RGB, RGBA
from .emit_metrics import ASCENT_EM, BASELINE_A, LINE_EM, PAD_X, PPTX_TITLE_DY, SLIDE_W, FontMapper, rgb
from .emit_model import (
    JsonMap, ObjectMap, SetRun, align_of, box_of, json_number, maps_of, objects_of, point_of, run_of,
)
from .emit_pptx import THEME_VARIANTS, VARIANT, api_error
from .emit_text import text_box_requests
from .gapi import HttpError, media_upload
from .google_types import DriveService, SlidesService, as_json, file_id
from .gslides import EMU_PER_PT, execute
from .ir_types import Box, Color
from .json_types import Json, JsonObject, as_int, as_object, as_objects, as_optional_str, as_str
from .typing_compat import assert_never

PPTX_MIME ="application/vnd.openxmlformats-officedocument.presentationml.presentation"

LayoutName = Literal["TITLE", "TITLE_ONLY", "BLANK"]
TitleKind = Literal["CENTERED_TITLE", "TITLE"]
PlaceholderKind = Literal["TITLE", "CENTERED_TITLE", "BODY"]
PLACEHOLDER_KINDS: tuple[PlaceholderKind, ...] = ("TITLE", "CENTERED_TITLE", "BODY")
Alignment = Literal["START", "CENTER", "END"]
ALIGNMENTS: tuple[Alignment, ...] = ("START", "CENTER", "END")

BgKey = tuple[Literal["color", "png"], str]
"""A slide's background as the master compares them: its colour, or its picture's SHA-1."""

Ground = Callable[[Sequence[float]], str]
"""A PDF box -> the master background's colour there (`master_ground`)."""


# (a slide is read as `ObjectMap`: sync hands over its own copies, whose values are no `Json` to the checker)

def title_element(slide: ObjectMap) -> int | None:
    """Index of the element that becomes the slide's title placeholder."""
    for i, el in enumerate(maps_of(slide["elements"], "elements")):
        if el["kind"] == "text" and el["role"] == "title":
            return i
    return None


def subtitle_element(slide: ObjectMap, title_idx: int) -> int | None:
    """On the title page, the biggest plain text box below the title (authors, institute,
    date) goes into the TITLE layout's subtitle placeholder."""
    if not slide.get("title_page"):
        return None
    els = maps_of(slide["elements"], "elements")
    title_bottom = box_of(els[title_idx]["bbox"], "bbox")[3]
    below: list[tuple[int, int]] = []
    for i, e in enumerate(els):
        if e["kind"] != "text" or e["role"] != "body" or box_of(e["bbox"], "bbox")[1] <= title_bottom:
            continue
        paras = objects_of(e["paragraphs"], "paragraphs")
        if not any(p["bullet"] for p in paras):
            below.append((sum(len(as_str(r["text"], "text")) for p in paras for r in objects_of(p["runs"], "runs")), i))
    return max(below)[1] if below else None


def slide_layout(slide: ObjectMap) -> tuple[LayoutName, TitleKind | None]:
    """The title page uses the TITLE layout (centered title), frames with a title TITLE_ONLY."""
    if title_element(slide) is None:
        return "BLANK", None
    return ("TITLE", "CENTERED_TITLE") if slide.get("title_page") else ("TITLE_ONLY", "TITLE")


LAYOUT_PLACEHOLDERS: dict[LayoutName, list[str]] = {"TITLE": ["CENTERED_TITLE", "SUBTITLE"], "TITLE_ONLY": ["TITLE"],
                                                    "BLANK": []}


def _magnitude(size: Json, side: str) -> float:
    """A width or height of a size the API answered (a page element's in EMU, the page's)."""
    return json_number(as_object(as_object(size, "size")[side], side)["magnitude"], "magnitude")


def import_presentation(slides: SlidesService, drive: DriveService, title: str, page_w: float, page_h: float,
                        pptx: io.BytesIO, existing: str | None) -> JsonObject:
    """A new deck from the .pptx, or an existing one (same URL) with its content replaced by it."""
    media = media_upload(pptx, PPTX_MIME)
    if existing:
        execute(drive.files().update(fileId=existing, media_body=media, fields="id"))
        pid = existing
    else:
        from .drive_folder import place
        pid = file_id(execute(drive.files().create(
            body=place({"name": title, "mimeType": "application/vnd.google-apps.presentation"}, drive, beside=None),
            media_body=media, fields="id")), f"the deck {title!r}")
    pres = as_json(execute(slides.presentations().get(presentationId=pid)), "the imported deck")
    got = _magnitude(pres["pageSize"], "height") / _magnitude(pres["pageSize"], "width")
    if abs(got - page_h / page_w) > 0.003:
        raise RuntimeError(f"page aspect {got:.4f} != PDF aspect {page_h / page_w:.4f}")
    return pres


def background_key(slide: JsonMap, out: Path) -> BgKey:
    colour = slide.get("background_color")
    if colour:
        return ("color", as_str(colour, "background_color").lower())
    return ("png", hashlib.sha1((out / as_str(slide["background"], "background")).read_bytes()).hexdigest())


# ------------------------------------------------------------------------------ the master and the layouts


@dataclass(frozen=True, kw_only=True)
class ThemeSlide:
    """What the master and the layouts read of a slide."""
    page: int
    layout: LayoutName
    background: str | None
    """Render's picture of the page (under the out folder); None where the slide has none."""


def theme_slide(slide: JsonMap) -> ThemeSlide:
    return ThemeSlide(page=as_int(slide["page"], "page"), layout=slide_layout(slide)[0],
                      background=as_optional_str(slide.get("background"), "background"))


@dataclass(frozen=True, kw_only=True)
class ThemePlan:
    """The theme decoration on the layouts (`plan_theme_of`)."""
    ground: str
    """The page ground, #rrggbb."""
    decorations: Mapping[str, Path | None]
    """"TITLE" (the title layout), "*" (every other layout), "TITLE_V1", "*_V1", ... (copies) -> its
    picture, or None."""
    layouts: Mapping[int, str]
    """Page -> the layout it takes, e.g. "TITLE_ONLY_V1"."""
    exact: Mapping[str, BgKey | None]
    """"TITLE", "*" -> the background that is exactly the ground plus the decoration, or None."""


class ThemeDict(TypedDict):
    """A `ThemePlan` as theme_sync and emit.json read it."""
    ground: str
    decorations: dict[str, Path | None]
    layouts: dict[int, str]
    exact: dict[str, BgKey | None]


def theme_dict(theme: ThemePlan) -> ThemeDict:
    return {"ground": theme.ground, "decorations": dict(theme.decorations), "layouts": dict(theme.layouts),
            "exact": dict(theme.exact)}


def theme_of(d: ThemeDict) -> ThemePlan:
    return ThemePlan(ground=d["ground"], decorations=d["decorations"], layouts=d["layouts"], exact=d["exact"])


def _group(layout: LayoutName) -> str:
    """The decoration group of a slide's layout: the title layout's, or every other one's."""
    return "TITLE" if layout == "TITLE" else "*"


def plan_theme(deck: JsonMap, out: Path, bg_key: Mapping[int, BgKey]) -> ThemeDict | None:
    """`plan_theme_of` a deck dict (theme_sync, the tests)."""
    theme = plan_theme_of([theme_slide(s) for s in objects_of(deck["slides"], "slides")], out, bg_key)
    return None if theme is None else theme_dict(theme)


def plan_theme_of(slides: Sequence[ThemeSlide], out: Path, bg_key: Mapping[int, BgKey]) -> ThemePlan | None:
    """Theme decoration on the layouts (render.theme_decoration), so a background colour set in
    Slides changes only the page ground and new slides get the decoration too. Title pages (TITLE
    layout) and the other slides get one each. Backgrounds that don't show it (a standout frame, a
    closing page without the headline) get the decoration they share among themselves on a copy of
    their layout (VARIANT suffix; up to THEME_VARIANTS copies, the last without decoration). Slide
    backgrounds stay as they are: the decoration drawn over a background showing it changes nothing.
    None without decoration."""
    from PIL import Image

    from .render import page_ground, save_png, theme_decoration

    counts = Counter(bg_key.values())
    groups: dict[str, list[ThemeSlide]] = {"TITLE": [s for s in slides if s.layout == "TITLE"]}
    groups["*"] = [s for s in slides if s.layout != "TITLE"]

    def load(s: ThemeSlide) -> RGB:
        if s.background is None:
            raise KeyError("background")  # (as the slide's dict was read)
        return np.asarray(Image.open(out / s.background).convert("RGB"))

    main = groups["*"] or groups["TITLE"]
    if not main:
        return None
    for old in (out / "backgrounds").glob("theme-*.png"):
        old.unlink()
    ground = page_ground(load(max(main, key=lambda s: counts[bg_key[s.page]])))
    decorations: dict[str, Path | None] = {}
    layouts: dict[int, str] = {s.page: s.layout for s in slides}
    exact: dict[str, BgKey | None] = {}
    for name, members in groups.items():
        for variant in range(THEME_VARIANTS + 1):
            if not members:
                break
            key = name if variant == 0 else f"{name}{VARIANT}{variant}"
            keys = sorted(dict.fromkeys(bg_key[s.page] for s in members), key=lambda k: -counts[k])
            first = {k: next(s for s in members if bg_key[s.page] == k) for k in keys}
            picture: RGBA | None = None
            inside = [True] * len(keys)
            if variant < THEME_VARIANTS:
                picture, inside, whole = theme_decoration((load(first[k]) for k in keys), ground)
                inside = inside if picture is not None else [True] * len(keys)
                if variant == 0:
                    exact[name] = keys[0] if picture is not None and whole else None
            path: Path | None = None
            if picture is not None:
                path = out / "backgrounds" / f"theme-{'title' if name == 'TITLE' else 'main'}-{variant}.png"
                save_png(picture, path)
            decorations[key] = path
            if variant:
                for s in members:
                    if inside[keys.index(bg_key[s.page])]:
                        layouts[s.page] += f"{VARIANT}{variant}"
            members = [s for s in members if not inside[keys.index(bg_key[s.page])]]
    if not any(decorations.values()):
        return None
    if not groups["TITLE"]:
        decorations["TITLE"] = decorations.get("*")  # (for title slides added in Slides)
    return ThemePlan(ground="#" + "".join(f"{int(v):02x}" for v in ground), decorations=decorations,
                     layouts=layouts, exact=exact)


def master_ground(shared: BgKey | None, files: Mapping[BgKey, Path], page_w: float) -> Ground:
    """bbox (PDF pt) -> the master background's median colour there (see background_key)."""
    if shared is None or shared[0] == "color":
        colour = shared[1] if shared else "#ffffff"

        def flat(bbox: Sequence[float]) -> str:
            return colour
        return flat
    from PIL import Image

    img: RGB = np.asarray(Image.open(files[shared]).convert("RGB"))
    k =img.shape[1] / page_w

    def ground(bbox: Sequence[float]) -> str:
        x0, y0, x1, y1 = (max(0, round(v * k)) for v in bbox)
        area = img[y0:max(y1, y0 + 1), x0:max(x1, x0 + 1)].reshape(-1, 3)
        return "#" + "".join(f"{int(v):02x}" for v in np.median(area, axis=0)) if len(area) else "#ffffff"
    return ground


@dataclass(frozen=True, kw_only=True)
class MasterPlan:
    """`build_deck`'s decisions about the master and the layouts (`master_plan_of`)."""
    bg_key: Mapping[int, BgKey]
    """Page -> its background."""
    bg_file: Mapping[BgKey, Path]
    """A picture background -> its file."""
    shared: BgKey | None
    """The background most slides share; None when no two do."""
    theme: ThemePlan | None
    fill: BgKey
    """What goes on the master."""
    ground: Ground


class MasterPlanDict(TypedDict):
    """A `MasterPlan` as theme_sync reads it."""
    bg_key: dict[int, BgKey]
    bg_file: dict[BgKey, Path]
    shared: BgKey | None
    theme: ThemeDict | None
    fill: BgKey
    ground: Ground


def master_plan(deck: JsonMap, out: Path, theme: ThemeDict | None | Literal["plan"]) -> MasterPlanDict:
    """`master_plan_of` a deck dict, as the dict theme_sync reads (`theme` as a dict too)."""
    given: ThemePlan | None | Literal["plan"]
    if theme is None or isinstance(theme, str):
        given = theme
    else:
        given = theme_of(theme)
    mp = master_plan_of(deck, out, given)
    return {"bg_key": dict(mp.bg_key), "bg_file": dict(mp.bg_file), "shared": mp.shared,
            "theme": None if mp.theme is None else theme_dict(mp.theme), "fill": mp.fill, "ground": mp.ground}


def master_plan_of(deck: JsonMap, out: Path, theme: ThemePlan | None | Literal["plan"]) -> MasterPlan:
    """`build_deck`'s decisions about the master and the layouts, for it and for a sync that has to
    compare them with what convert wrote (`theme_sync`). `theme="plan"` plans the theme (and so
    rewrites out/backgrounds/theme-*.png, as plan_theme_of does); a theme passed in is taken as it
    is, None as no theme."""
    dicts = objects_of(deck["slides"], "slides")
    bg_key = {as_int(s["page"], "page"): background_key(s, out) for s in dicts}
    bg_file = {bg_key[as_int(s["page"], "page")]: out / as_str(s["background"], "background")
               for s in dicts if not s.get("background_color")}
    counts = Counter(bg_key.values())
    common = counts.most_common(1)
    shared = common[0][0] if counts and common[0][1] >= 2 else None
    slides = [theme_slide(s) for s in dicts]
    planned = plan_theme_of(slides, out, bg_key) if isinstance(theme, str) else theme
    fill: BgKey = shared or ("color", "#ffffff")
    if planned is not None:
        # The master's ground colour where the shared background is nothing but ground and decoration.
        if shared is None or all(planned.exact.get(_group(s.layout)) == shared for s in slides
                                 if bg_key[s.page] == shared):
            fill = ("color", planned.ground)
    page_w = point_of(dicts[0]["size"], "size")[0] if dicts else SLIDE_W
    return MasterPlan(bg_key=bg_key, bg_file=bg_file, shared=shared, theme=planned, fill=fill,
                      ground=master_ground(shared, bg_file, page_w))


# ------------------------------------------------------------------------------ layout texts and placeholders


LAYOUT_TEXT_PREFIX = "b2s_L"


def write_layout_texts(slides: SlidesService, pid: str, texts: Sequence[JsonMap], scale: float,
                       fonts: FontMapper) -> None:
    """Header/footer text shared by every slide goes onto the layouts our slides use, so
    it is edited once for the whole deck. Previous runs' layout texts are replaced."""
    pres = as_json(execute(slides.presentations().get(
        presentationId=pid, fields="layouts(objectId,layoutProperties,pageElements(objectId))")), "the layouts")
    layouts = as_objects(pres.get("layouts", []), "layouts")  # all of them: slides added later in Slides get the footer too
    # All old ones first: object IDs are unique across the whole presentation.
    reqs: list[JsonObject] = []
    for layout in layouts:
        for e in as_objects(layout.get("pageElements", []), "pageElements"):
            oid = as_str(e["objectId"], "objectId")
            if oid.startswith(LAYOUT_TEXT_PREFIX):
                reqs.append({"deleteObject": {"objectId": oid}})
    for li, layout in enumerate(layouts):
        for ti, el in enumerate(texts):
            reqs += text_box_requests(el, as_str(layout["objectId"], "objectId"), f"{LAYOUT_TEXT_PREFIX}{li}_{ti}",
                                      scale, fonts)
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


def readable_run(run: SetRun, slide: JsonMap, bbox: Sequence[float], ground: Ground) -> SetRun:
    """The run in a colour readable on a new slide: that is on the master background under `bbox`
    (`ground(bbox)`: its colour there), without the shapes of the converted slide. White title
    text on a native title panel becomes the panel's colour (else black or white)."""
    under = ground(bbox)
    if contrast(run.color, under) >= MIN_CONTRAST:
        return run
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    panels: list[str] = []
    for e in reversed(objects_of(slide["elements"], "elements")):
        if e["kind"] == "shape" and e.get("fill"):
            x0, y0, x1, y1 = box_of(e["bbox"], "bbox")
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                panels.append(as_str(e["fill"], "fill"))
    colour = next((c for c in panels if contrast(c, under) >= MIN_CONTRAST), None) or \
        max(("#000000", "#ffffff"), key=lambda c: contrast(c, under))
    return replace(run, color=Color(colour))


@dataclass(frozen=True, kw_only=True)
class PlaceholderStyle:
    """What `style_layout_placeholders` writes into one kind of placeholder."""
    box: Box | None
    """(x, y, w, h), slide pt; None for the body, whose box is left alone."""
    style: JsonObject
    """The TextStyle, written over the whole placeholder."""
    fields: str
    align: Alignment


PlaceholderSpec = dict[PlaceholderKind, PlaceholderStyle | None]
"""Each kind of placeholder -> what is written into it, or None where nothing is."""


def _alignment(value: Json) -> Alignment:
    for a in ALIGNMENTS:
        if value == a:
            return a
    raise ValueError(f"align: {value!r} is not one of {ALIGNMENTS}")


def placeholder_style_json(entry: PlaceholderStyle) -> JsonObject:
    box: Json = None
    if entry.box is not None:
        x, y, w, h = entry.box
        box = [x, y, w, h]
    return {"box": box, "style": copy.deepcopy(entry.style), "fields": entry.fields, "align": entry.align}


def placeholder_style_of(d: JsonMap) -> PlaceholderStyle:
    """A `layout_style_spec` entry as a base holds it."""
    box = d.get("box")
    return PlaceholderStyle(box=None if box is None else box_of(box, "box"), style=as_object(d["style"], "style"),
                            fields=as_str(d["fields"], "fields"), align=_alignment(d["align"]))


def _styled(run: SetRun, scale: float, fonts: FontMapper) -> tuple[JsonObject, str]:
    style, fields = fonts.style_of(run, scale)
    style["foregroundColor"] = rgb(run.color)
    if "bold" not in fields:  # a weighted family: the layout's own bold (section header) would add to it
        style["bold"], fields = False, fields + ["bold"]
    return style, ",".join(fields + ["foregroundColor"])


def _title_style(el: JsonMap, run: SetRun, scale: float, fonts: FontMapper, dy: float) -> PlaceholderStyle:
    p = objects_of(el["paragraphs"], "paragraphs")[0]
    z = fonts.size_of(run, scale)[1]
    x = json_number(p["text_x0"], "text_x0") * scale - PAD_X
    align = align_of(p["align"])
    if align == "center":
        x = min(x, 0.1 * SLIDE_W)
    w = SLIDE_W - 2 * max(x, 10)
    h = 2 * LINE_EM * z + 2 * BASELINE_A
    y = json_number(objects_of(p["lines"], "lines")[0]["baseline"], "baseline") * scale - (BASELINE_A + ASCENT_EM * z) + dy
    style, fields = _styled(run, scale, fonts)
    alignment: Alignment
    if align == "left":
        alignment = "START"
    elif align == "center":
        alignment = "CENTER"
    elif align == "right":
        alignment = "END"
    else:
        assert_never(align)
    # (max(x, 10) stays the int 10 where it is: the base has always recorded it so)
    return PlaceholderStyle(box=(max(x, 10), max(0.0, y), w, h), style=style, fields=fields, align=alignment)


def layout_style_spec(deck: JsonMap, scale: float, fonts: FontMapper, dy: float, ground: Ground) -> JsonObject:
    """`layout_style_spec_of` as data a base can hold: {"TITLE" / "CENTERED_TITLE" / "BODY": {"box":
    [x, y, w, h] or None, "style", "fields", "align"}, or None}. `layout_placeholder_requests` turns
    one entry into the requests for one placeholder; tests/test_theme_sync.py holds the two to the
    requests `style_layout_placeholders` sends."""
    out: JsonObject = {}
    for kind, entry in layout_style_spec_of(deck, scale, fonts, dy, ground).items():
        out[kind] = None if entry is None else placeholder_style_json(entry)
    return out


def layout_style_spec_of(deck: JsonMap, scale: float, fonts: FontMapper, dy: float, ground: Ground) -> PlaceholderSpec:
    """What `style_layout_placeholders` writes into each kind of placeholder: the frame title's look
    into TITLE, the title page's (else the frame title's) into CENTERED_TITLE, the deck's most used
    body run into BODY, each in a colour readable on the master (`ground(bbox)`)."""
    texts: list[tuple[JsonObject, JsonObject]] = []
    for s in objects_of(deck["slides"], "slides"):
        for e in objects_of(s["elements"], "elements"):
            if e["kind"] == "text" and objects_of(e["paragraphs"], "paragraphs")[0]["runs"]:
                texts.append((s, e))
    frame = next(((s, e) for s, e in texts if e["role"] == "title" and not s.get("title_page")), None)
    page = next(((s, e) for s, e in texts if e["role"] == "title" and s.get("title_page")), None) or frame

    def title_run(s: JsonMap, e: JsonMap) -> SetRun:
        first = objects_of(objects_of(e["paragraphs"], "paragraphs")[0]["runs"], "runs")[0]
        return readable_run(run_of(first), s, box_of(e["bbox"], "bbox"), ground)

    body_runs = Counter((r.font, r.size, r.color, r.family) for s, e in texts if e["role"] == "body"
                        for p in objects_of(e["paragraphs"], "paragraphs")
                        for r in map(run_of, objects_of(p["runs"], "runs")) for _ in range(len(r.text)))
    body: SetRun | None = None
    if body_runs:
        font, size, color, family = body_runs.most_common(1)[0][0]
        body = SetRun(text="", font=font, family=family, size=size, bold=False, italic=False, smallcaps=False,
                      script=None, color=color, underline=False, strike=False, highlight=None, link=None, hole=None,
                      hole_size=None, cell=False, in_sentence=False)
        w, h = point_of(objects_of(deck["slides"], "slides")[0]["size"], "size")
        body = readable_run(body, {"elements": []}, [0.1 * w, 0.3 * h, 0.9 * w, 0.8 * h], ground)
    spec: PlaceholderSpec = {}
    for kind, pair in (("TITLE", frame), ("CENTERED_TITLE", page)):
        spec[kind] = None if pair is None else _title_style(pair[1], title_run(*pair), scale, fonts, dy)
    if body is not None:
        style, fields = _styled(body, scale, fonts)
        spec["BODY"] = PlaceholderStyle(box=None, style=style, fields=fields, align="START")
    else:
        spec["BODY"] = None
    return spec


def _has_text(pe: JsonMap) -> bool:
    """Whether a placeholder holds any text (an imported one holds a "\\n" per list level)."""
    shape = as_object(pe.get("shape", {}), "shape")
    return bool(as_object(shape.get("text", {}), "text").get("textElements"))


def _placeholder_kind(pe: JsonMap) -> PlaceholderKind | None:
    shape = as_object(pe.get("shape", {}), "shape")
    kind = as_object(shape.get("placeholder", {}), "placeholder").get("type")
    for k in PLACEHOLDER_KINDS:
        if kind == k:
            return k
    return None


def layout_placeholder_requests(entry: JsonMap, pe: JsonMap) -> list[JsonObject]:
    """`placeholder_requests` from a `layout_style_spec` entry as a base holds it (theme_sync)."""
    return placeholder_requests(placeholder_style_of(entry), pe)


def placeholder_requests(entry: PlaceholderStyle, pe: JsonMap) -> list[JsonObject]:
    """The requests `style_layout_placeholders` sends for one placeholder `pe` (a layout or master
    page element as presentations.get gives it)."""
    reqs: list[JsonObject] = []
    oid = as_str(pe["objectId"], "objectId")
    if entry.box is not None:
        x, y, w, h = entry.box
        reqs.append({"updatePageElementTransform": {"objectId": oid, "applyMode": "ABSOLUTE", "transform": {
            "scaleX": w / (_magnitude(pe["size"], "width") / EMU_PER_PT),
            "scaleY": h / (_magnitude(pe["size"], "height") / EMU_PER_PT), "unit": "EMU",
            "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}}})
        reqs.append({"updateShapeProperties": {"objectId": oid, "fields": "contentAlignment",
                                               "shapeProperties": {"contentAlignment": "TOP"}}})
    if _has_text(pe):
        # (without any text, not even the imported "\n" per list level, a placeholder can't be
        # styled, and the API refuses to put text into layout placeholders)
        reqs.append({"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                         "style": copy.deepcopy(entry.style), "fields": entry.fields}})
        reqs.append({"updateParagraphStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                              "style": {"alignment": entry.align}, "fields": "alignment"}})
    return reqs


def style_layout_placeholders(slides: SlidesService, pid: str, deck: JsonMap, scale: float, fonts: FontMapper,
                              dy: float, ground: Ground) -> None:
    """Title and body placeholders of every layout take the deck's own look (font, size,
    colour, title position), so slides added later in Slides match the converted ones.
    `ground(bbox)`: the master background's colour under a PDF box, to keep text readable there."""
    spec = layout_style_spec_of(deck, scale, fonts, dy, ground)
    page_fields = "objectId,pageElements(objectId,size,transform,shape(placeholder/type,text/textElements))"
    pres = as_json(execute(slides.presentations().get(presentationId=pid, fields=f"masters({page_fields}),layouts({page_fields})")),
                   "the masters and layouts")
    reqs: list[JsonObject] = []
    # The master too: layout placeholders inherit whatever style they don't set themselves.
    for page in as_objects(pres.get("masters", []), "masters") + as_objects(pres.get("layouts", []), "layouts"):
        for pe in as_objects(page.get("pageElements", []), "pageElements"):
            kind = _placeholder_kind(pe)
            entry = None if kind is None else spec.get(kind)
            if entry is not None:
                reqs += placeholder_requests(entry, pe)
    for i in range(0, len(reqs), 200):
        try:
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs[i:i + 200]}))
        except HttpError as e:  # the deck's look for new slides is a nicety, never a reason to fail
            print(f"warning: could not style the layouts ({api_error(e)})")


def write_layouts(client: Callable[[], SlidesService], pid: str, deck: JsonMap, scale: float, fonts: FontMapper,
                  ground: Ground) -> None:
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
    write_layout_texts(slides, pid, objects_of(deck.get("layout_texts", []), "layout_texts"), scale, fonts)
    style_layout_placeholders(slides, pid, deck, scale, fonts, PPTX_TITLE_DY, ground)
