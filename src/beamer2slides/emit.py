"""Stage 4: build the Google Slides deck from deck.json and the background images."""

import io
import json
import time
from pathlib import Path

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

from .fonts import font_info
from .google_auth import drive_service, slides_service
from .gslides import EMU_PER_PT, emu, execute, pt

ROOT = Path(__file__).resolve().parents[2]
CALIBRATION = ROOT / "calibration" / "fonts.json"
SLIDE_W = 720.0
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Slides text box model, measured by tools/calibrate.py and the spacing probes
# (docs/calibration.md).
BASELINE_A = 6.48    # box top -> first baseline = A + ASCENT_EM * size
ASCENT_EM = 0.968
LINE_EM = 1.195      # baseline pitch at lineSpacing 100
DESCENT_EM = LINE_EM - ASCENT_EM
PAD_X = 6.7          # box edge -> text start
BULLET_GAP = 1.9     # bullet glyph's right edge sits this far before indentFirstLine
PPTX_TITLE_DY = 3.9  # title placeholders of pptx-imported decks have a smaller top inset

FONT_FOR_FAMILY = {"sans": "Lato", "serif": "Noto Serif", "mono": "Roboto Mono"}
CMTT_ADVANCE_EM, ROBOTO_MONO_ADVANCE_EM = 0.525, 0.6

BULLET_PRESETS = {
    "arrow": "BULLET_ARROW3D_CIRCLE_SQUARE",  # ➢ is the closest preset glyph to beamer's ▶
    "disc": "BULLET_DISC_CIRCLE_SQUARE",
    "number": "NUMBERED_DIGIT_ALPHA_ROMAN",
    "number_parens": "NUMBERED_DIGIT_ALPHA_ROMAN_PARENS",
}


class FontMapper:
    """TeX font + size -> Slides font family + size with calibrated width correction."""

    def __init__(self):
        cal = json.loads(CALIBRATION.read_text(encoding="utf-8"))["fonts"]
        lato = cal["Lato"]["width_ratio"]
        self.sans_text = lato["text_mean"]
        self.sans_title = lato["by_row"]["title"]

    def __call__(self, run: dict, scale: float) -> tuple[str, float]:
        info = font_info(run["font"])
        family = FONT_FOR_FAMILY.get(run["family"], "Lato")
        if run["family"] == "mono":
            factor = ROBOTO_MONO_ADVANCE_EM / CMTT_ADVANCE_EM
        elif run["family"] == "sans":
            factor = self.sans_title if (info.design_size or 10) >= 11.5 else self.sans_text
        else:
            factor = 1.0  # serif not calibrated yet
        return family, round(run["size"] * scale / factor, 1)


def bullet_preset(bullet: dict) -> str:
    text = bullet.get("text", "")
    if bullet["kind"] == "number" or (bullet["kind"] == "image" and text.isdigit()):
        return BULLET_PRESETS["number_parens" if ")" in text else "number"]
    if bullet["kind"] == "glyph" and text in "▶►▸‣":
        return BULLET_PRESETS["arrow"]
    return BULLET_PRESETS["disc"]


def rgb(hex_color: str) -> dict:
    h = hex_color.lstrip("#")
    return {"opaqueColor": {"rgbColor": {k: int(h[i:i + 2], 16) / 255 for k, i in
                                         (("red", 0), ("green", 2), ("blue", 4))}}}


# ---------------------------------------------------------------- text boxes

def extra_below(r: float, z: float) -> float:
    """Extra space lineSpacing r adds under a line of size z (negative when r < 1)."""
    return (r - 1) * LINE_EM * z if r >= 1 else -(1 - r) * 0.25 * LINE_EM * z


def extra_above(r: float, z: float) -> float:
    """lineSpacing >= 100% never moves a line down; below 100% it pulls the baseline up."""
    return 0.0 if r >= 1 else -(1 - r) * 0.75 * LINE_EM * z


def pitch_between(z1: float, r1: float, z2: float, r2: float) -> float:
    """Baseline distance from the last line of one paragraph to the first line of the next."""
    return DESCENT_EM * z1 + ASCENT_EM * z2 + extra_below(r1, z1) + extra_above(r2, z2)


def solve_increasing(f, target: float, lo: float = 0.5, hi: float = 3.0) -> float:
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) < target else (lo, mid)
    return (lo + hi) / 2


def vertical_layout(paras: list[dict], baselines: list[list[float]], sizes: list[float]):
    """lineSpacing ratio and spaceAbove per paragraph so Slides baselines land on the PDF's.

    Slides ignores spaceAbove/spaceBelow between items of a bulleted list, so there the gap
    to the next item has to come from the item's own lineSpacing; for a wrapped item one
    ratio covers its inner lines plus that gap, spreading the difference evenly."""
    ratios = []
    for i, (p, bl, z) in enumerate(zip(paras, baselines, sizes)):
        n = len(bl)
        if i + 1 < len(paras) and p["bullet"] and paras[i + 1]["bullet"]:
            target = baselines[i + 1][0] - bl[0]
            zn = sizes[i + 1]
            r = solve_increasing(lambda r: (n - 1) * LINE_EM * z * r + pitch_between(z, r, zn, 1.0), target)
        elif n > 1:
            r = (bl[-1] - bl[0]) / (n - 1) / (LINE_EM * z)
        else:
            r = 1.0
        ratios.append(min(3.0, max(0.5, r)))
    space_above = [0.0]
    for i in range(1, len(paras)):
        if paras[i - 1]["bullet"] and paras[i]["bullet"]:
            space_above.append(0.0)
            continue
        gap = baselines[i][0] - baselines[i - 1][-1]
        space_above.append(max(0.0, gap - pitch_between(sizes[i - 1], ratios[i - 1], sizes[i], ratios[i])))
    return ratios, space_above


def text_box_requests(el: dict, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                      placeholder: dict | None = None, page_slide: dict[int, str] | None = None) -> list[dict]:
    paras = el["paragraphs"]
    # A line is as tall as its largest run.
    sizes = [max(fonts(r, scale)[1] for r in p["runs"]) if p["runs"] else p["size"] * scale for p in paras]

    left_pdf = min(p["bullet"]["bbox"][0] if p["bullet"] else p["text_x0"] for p in paras)
    right_pdf = max(line["x1"] for p in paras for line in p["lines"])
    first_baseline = paras[0]["lines"][0]["baseline"] * scale
    last_baseline = paras[-1]["lines"][-1]["baseline"] * scale

    baselines = [[line["baseline"] * scale for line in p["lines"]] for p in paras]
    ratios, space_above = vertical_layout(paras, baselines, sizes)

    inner_w = (right_pdf - left_pdf) * scale
    multiline = any(len(p["lines"]) > 1 for p in paras)
    # Wrapped paragraphs need a tight width to break where TeX did; single lines get room
    # so that a slightly wider font never wraps them.
    slack = 2 + 0.01 * inner_w if multiline else max(0.15 * inner_w, 2 * max(sizes))
    x = left_pdf * scale - PAD_X
    aligns = {p["align"] for p in paras}
    if aligns == {"center"}:
        x -= slack / 2
    elif aligns == {"right"}:
        x -= slack
    y = first_baseline - (BASELINE_A + ASCENT_EM * sizes[0] + extra_above(ratios[0], sizes[0]))
    w = inner_w + 2 * PAD_X + slack
    h = last_baseline - y + DESCENT_EM * sizes[-1] + extra_below(ratios[-1], sizes[-1]) + 4

    if placeholder:
        # An existing layout placeholder (the slide title): its size is fixed at creation, so
        # it is resized through the transform's scale. Text is not scaled by that.
        y += placeholder["dy"]
        reqs = [
            {"updatePageElementTransform": {"objectId": object_id, "applyMode": "ABSOLUTE", "transform": {
                "scaleX": w / placeholder["base_w"], "scaleY": h / placeholder["base_h"], "unit": "EMU",
                "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}}},
            {"updateShapeProperties": {"objectId": object_id, "fields": "contentAlignment,autofit.autofitType",
                                       "shapeProperties": {"contentAlignment": "TOP",
                                                           "autofit": {"autofitType": "NONE"}}}},
        ]
    else:
        reqs = [{"createShape": {
            "objectId": object_id, "shapeType": "TEXT_BOX",
            "elementProperties": {
                "pageObjectId": slide_id,
                "size": {"width": emu(w), "height": emu(h)},
                "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                              "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)},
            },
        }}]

    texts = ["".join(r["text"] for r in p["runs"]) for p in paras]
    tabbed = "\n".join(("\t" * p["level"] if p["bullet"] else "") + t for p, t in zip(paras, texts))
    reqs.append({"insertText": {"objectId": object_id, "text": tabbed, "insertionIndex": 0}})

    # Bullets: contiguous ranges with the same preset. createParagraphBullets consumes the
    # leading tabs (they set the nesting level), so ranges are applied last-to-first.
    starts_tabbed, pos = [], 0
    for p, t in zip(paras, texts):
        starts_tabbed.append(pos)
        pos += (p["level"] if p["bullet"] else 0) + len(t) + 1
    ranges = []
    for i, p in enumerate(paras):
        preset = bullet_preset(p["bullet"]) if p["bullet"] else None
        if preset and ranges and ranges[-1][2] == preset and ranges[-1][1] == i - 1:
            ranges[-1][1] = i
        elif preset:
            ranges.append([i, i, preset])
    # A bullet keeps the text style it was created with unless its whole paragraph later
    # shares one style (mixed paragraphs, e.g. with inline math, never update it). So give
    # every paragraph its base family and size before the bullets exist.
    for p, start, size in zip(paras, starts_tabbed, sizes):
        family = fonts(p["runs"][0], scale)[0] if p["runs"] else "Lato"
        length = (p["level"] if p["bullet"] else 0) + len("".join(r["text"] for r in p["runs"]))
        if length:
            reqs.append({"updateTextStyle": {
                "objectId": object_id, "fields": "fontFamily,fontSize",
                "style": {"fontFamily": family, "fontSize": pt(size)},
                "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": start + length},
            }})
    for first, last, preset in reversed(ranges):
        end = starts_tabbed[last] + (paras[last]["level"]) + len(texts[last])
        reqs.append({"createParagraphBullets": {
            "objectId": object_id, "bulletPreset": preset,
            "textRange": {"type": "FIXED_RANGE", "startIndex": starts_tabbed[first], "endIndex": end},
        }})

    # From here on indices refer to the final text, without tabs.
    pos = 0
    for p, t, ratio, above in zip(paras, texts, ratios, space_above):
        p_start, p_end = pos, pos + len(t)
        pos = p_end + 1
        start = p_start
        for run in p["runs"]:
            if not run["text"]:
                continue
            family, run_size = fonts(run, scale)
            style = {"fontFamily": family, "fontSize": pt(run_size), "bold": run["bold"],
                     "italic": run["italic"], "smallCaps": run["smallcaps"],
                     "foregroundColor": rgb(run["color"]), "underline": False,
                     "baselineOffset": {"super": "SUPERSCRIPT", "sub": "SUBSCRIPT"}.get(run.get("script"), "NONE")}
            fields = "fontFamily,fontSize,bold,italic,smallCaps,foregroundColor,underline,baselineOffset"
            if run["link"] and run["link"].startswith("#page="):
                target = page_slide.get(int(run["link"][6:])) if page_slide else None
                if target:
                    style["link"] = {"pageObjectId": target}  # TOC entries jump to their slide
                    fields += ",link"
            elif run["link"]:
                style["link"] = {"url": run["link"]}
                fields += ",link"
            reqs.append({"updateTextStyle": {
                "objectId": object_id, "style": style, "fields": fields,
                "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": start + len(run["text"])},
            }})
            start += len(run["text"])

        # Code lines carry their indentation as leading spaces already.
        text_indent = 0.0 if el.get("code") else (p["text_x0"] - left_pdf) * scale
        if p["bullet"]:
            # Slides ends the bullet glyph BULLET_GAP before indentFirstLine, whatever the glyph.
            first_indent = (p["bullet"]["bbox"][2] - left_pdf) * scale + BULLET_GAP
        else:
            first_indent = text_indent
        reqs.append({"updateParagraphStyle": {
            "objectId": object_id,
            "textRange": {"type": "FIXED_RANGE", "startIndex": p_start, "endIndex": max(p_end, p_start + 1)},
            "style": {
                "alignment": {"left": "START", "center": "CENTER", "right": "END"}[p["align"]],
                "lineSpacing": round(100 * ratio, 1),
                "spaceAbove": pt(round(above, 2)), "spaceBelow": pt(0),
                "indentStart": pt(round(text_indent, 2)), "indentFirstLine": pt(round(first_indent, 2)),
            },
            "fields": "alignment,lineSpacing,spaceAbove,spaceBelow,indentStart,indentFirstLine",
        }})
    return reqs


def shape_requests(el: dict, slide_id: str, object_id: str, scale: float) -> list[dict]:
    x0, y0, x1, y1 = (v * scale for v in el["bbox"])
    # ROUND_2_SAME_RECTANGLE rounds the top corners; for bottom corners flip both axes
    # (a 180° rotation), which moves the origin to the opposite corner.
    flip = -1 if el["flip"] else 1
    tx, ty = (x1, y1) if el["flip"] else (x0, y0)
    return [
        {"createShape": {
            "objectId": object_id, "shapeType": el["shape"],
            "elementProperties": {
                "pageObjectId": slide_id,
                "size": {"width": emu(x1 - x0), "height": emu(y1 - y0)},
                "transform": {"scaleX": flip, "scaleY": flip, "unit": "EMU",
                              "translateX": round(tx * EMU_PER_PT), "translateY": round(ty * EMU_PER_PT)},
            },
        }},
        {"updateShapeProperties": {
            "objectId": object_id,
            "shapeProperties": {"shapeBackgroundFill": {"solidFill": {"color": rgb(el["fill"])["opaqueColor"]}},
                                "outline": {"propertyState": "NOT_RENDERED"}},
            "fields": "shapeBackgroundFill.solidFill.color,outline.propertyState",
        }},
    ]


TABLE_MIN_COLUMN_PT = 32.0  # the API refuses narrower columns


def table_requests(el: dict, slide_id: str, object_id: str, scale: float, fonts: FontMapper) -> list[dict]:
    cols = el["columns"]
    fx0, _, fx1, _ = el["frame"]
    bounds = [fx0] + [(a["x1"] + b["x0"]) / 2 for a, b in zip(cols, cols[1:])] + [fx1]
    widths = [max(TABLE_MIN_COLUMN_PT, (b - a) * scale) for a, b in zip(bounds, bounds[1:])]
    first_run = next((r for row in el["cells"] for cell in row for r in cell), None)
    z = fonts(first_run, scale)[1] if first_run else el["size"] * scale
    x = fx0 * scale
    y = el["row_baselines"][0] * scale - (BASELINE_A + ASCENT_EM * z)
    n_rows, n_cols = len(el["cells"]), len(cols)
    heights = [h * scale for h in el["row_heights"]]

    reqs: list[dict] = [
        {"createTable": {"objectId": object_id, "rows": n_rows, "columns": n_cols, "elementProperties": {
            "pageObjectId": slide_id,
            "size": {"width": emu(sum(widths)), "height": emu(sum(heights))},
            "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                          "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}}}},
        # No grid: only the rules of the original are drawn.
        {"updateTableBorderProperties": {
            "objectId": object_id, "borderPosition": "ALL",
            "tableRange": {"location": {"rowIndex": 0, "columnIndex": 0}, "rowSpan": n_rows, "columnSpan": n_cols},
            "tableBorderProperties": {"tableBorderFill": {"solidFill": {"color": {"rgbColor": {}}, "alpha": 0}}},
            "fields": "tableBorderFill.solidFill.alpha"}},
    ]
    for i, w in enumerate(widths):
        reqs.append({"updateTableColumnProperties": {"objectId": object_id, "columnIndices": [i],
                                                     "tableColumnProperties": {"columnWidth": emu(w)},
                                                     "fields": "columnWidth"}})
    for i, h in enumerate(heights):
        reqs.append({"updateTableRowProperties": {"objectId": object_id, "rowIndices": [i],
                                                  "tableRowProperties": {"minRowHeight": emu(h)},
                                                  "fields": "minRowHeight"}})
    for rule in el["rules"]:
        reqs.append({"updateTableBorderProperties": {
            "objectId": object_id, "borderPosition": rule["position"],
            "tableRange": {"location": {"rowIndex": rule["row"], "columnIndex": 0}, "rowSpan": 1, "columnSpan": n_cols},
            "tableBorderProperties": {
                "tableBorderFill": {"solidFill": {"color": rgb(rule["color"])["opaqueColor"], "alpha": 1}},
                "weight": pt(round(max(0.5, rule["weight"] * scale), 2))},
            "fields": "tableBorderFill.solidFill.color,tableBorderFill.solidFill.alpha,weight"}})

    for r, row in enumerate(el["cells"]):
        for c, runs in enumerate(row):
            text = "".join(run["text"] for run in runs).strip()
            if not text:
                continue
            loc = {"rowIndex": r, "columnIndex": c}
            reqs.append({"insertText": {"objectId": object_id, "cellLocation": loc, "text": text}})
            start = 0
            for run in runs:
                piece = run["text"].strip() if len(runs) == 1 else run["text"]
                if start == 0:
                    piece = piece.lstrip()
                if not piece:
                    continue
                family, run_size = fonts(run, scale)
                reqs.append({"updateTextStyle": {
                    "objectId": object_id, "cellLocation": loc,
                    "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": min(len(text), start + len(piece))},
                    "style": {"fontFamily": family, "fontSize": pt(run_size), "bold": run["bold"], "italic": run["italic"],
                              "smallCaps": run["smallcaps"], "foregroundColor": rgb(run["color"])},
                    "fields": "fontFamily,fontSize,bold,italic,smallCaps,foregroundColor"}})
                start += len(piece)
            col = cols[c]
            # Line the text up with the original inside the (contiguous) Slides columns.
            indent_start = max(0.0, (col["x0"] - bounds[c]) * scale - PAD_X) if col["align"] == "left" else 0.0
            indent_end = max(0.0, (bounds[c + 1] - col["x1"]) * scale - PAD_X) if col["align"] == "right" else 0.0
            reqs.append({"updateParagraphStyle": {
                "objectId": object_id, "cellLocation": loc, "textRange": {"type": "ALL"},
                "style": {"alignment": {"left": "START", "center": "CENTER", "right": "END"}[col["align"]],
                          "lineSpacing": 100, "spaceAbove": pt(0), "spaceBelow": pt(0),
                          "indentStart": pt(round(indent_start, 2)), "indentFirstLine": pt(round(indent_start, 2)),
                          "indentEnd": pt(round(indent_end, 2))},
                "fields": "alignment,lineSpacing,spaceAbove,spaceBelow,indentStart,indentFirstLine,indentEnd"}})
    return reqs


def title_element(slide: dict) -> int | None:
    """Index of the element that becomes the slide's title placeholder."""
    for i, el in enumerate(slide["elements"]):
        if el["kind"] == "text" and el["role"] == "title":
            return i
    return None


def image_request(el: dict, slide_id: str, object_id: str, scale: float, url: str) -> dict:
    x0, y0, x1, y1 = el["bbox"]
    return {"createImage": {
        "objectId": object_id, "url": url,
        "elementProperties": {
            "pageObjectId": slide_id,
            "size": {"width": emu((x1 - x0) * scale), "height": emu((y1 - y0) * scale)},
            "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                          "translateX": round(x0 * scale * EMU_PER_PT), "translateY": round(y0 * scale * EMU_PER_PT)},
        },
    }}


# ---------------------------------------------------------------- presentation + assets

def create_presentation(slides, drive, title: str, page_w: float, page_h: float) -> dict:
    ratio = page_h / page_w
    if abs(ratio - 9 / 16) < 0.003:
        pres = execute(slides.presentations().create(body={"title": title}))
    else:
        # presentations.create ignores pageSize, but Drive keeps the size of an imported .pptx.
        from pptx import Presentation
        from pptx.util import Emu
        prs = Presentation()
        prs.slide_width = Emu(round(SLIDE_W * EMU_PER_PT))
        prs.slide_height = Emu(round(SLIDE_W * ratio * EMU_PER_PT))
        buf = io.BytesIO()
        prs.save(buf)
        buf.seek(0)
        f = execute(drive.files().create(
            body={"name": title, "mimeType": "application/vnd.google-apps.presentation"},
            media_body=MediaIoBaseUpload(buf, mimetype=PPTX_MIME), fields="id"))
        pres = execute(slides.presentations().get(presentationId=f["id"]))
    got = pres["pageSize"]["height"]["magnitude"] / pres["pageSize"]["width"]["magnitude"]
    if abs(got - ratio) > 0.003:
        raise RuntimeError(f"page aspect {got:.4f} != PDF aspect {ratio:.4f}")
    return pres


def asset_folder(drive, name: str) -> str:
    q = (f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    found = execute(drive.files().list(q=q, fields="files(id)")).get("files", [])
    if found:
        return found[0]["id"]
    return execute(drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"}, fields="id"))["id"]


def upload_public_png(drive, path: Path, folder: str) -> tuple[str, str]:
    f = execute(drive.files().create(
        body={"name": path.name, "parents": [folder]},
        media_body=MediaFileUpload(str(path), mimetype="image/png"), fields="id"))
    perm = execute(drive.permissions().create(
        fileId=f["id"], body={"type": "anyone", "role": "reader"}, fields="id"))
    return f["id"], perm["id"]


def batch_with_image_retry(slides, pid: str, reqs: list[dict], attempts: int = 4) -> None:
    for attempt in range(attempts):
        try:
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
            return
        except HttpError as e:
            if "problem retrieving the image" not in str(e) or attempt == attempts - 1:
                raise
            time.sleep(5 * (attempt + 1))


# ---------------------------------------------------------------- main entry

def existing_presentation(slides, out: Path, page_w: float, page_h: float) -> dict | None:
    """The deck from a previous run of this output folder, if it still exists with the right shape."""
    state_file = out / "emit.json"
    if not state_file.exists():
        return None
    pid = json.loads(state_file.read_text(encoding="utf-8"))["presentationId"]
    try:
        pres = execute(slides.presentations().get(presentationId=pid))
    except HttpError:
        return None
    got = pres["pageSize"]["height"]["magnitude"] / pres["pageSize"]["width"]["magnitude"]
    return pres if abs(got - page_h / page_w) < 0.003 else None


def emit(deck: dict, out: Path, title: str, new_deck: bool = False) -> dict:
    slides, drive = slides_service(), drive_service()
    page_w, page_h = deck["slides"][0]["size"]
    scale = SLIDE_W / page_w
    fonts = FontMapper()

    pres = None if new_deck else existing_presentation(slides, out, page_w, page_h)
    if pres:
        print(f"updating existing deck {pres['presentationId']}")
    else:
        pres = create_presentation(slides, drive, title, page_w, page_h)
    pid = pres["presentationId"]
    folder = asset_folder(drive, "beamer2slides assets")

    state = {"presentationId": pid, "url": f"https://docs.google.com/presentation/d/{pid}/edit",
             "scale": scale, "slides": []}
    uploaded: list[tuple[str, str]] = []
    urls: dict[str, str] = {}  # local file -> public URL
    try:
        files = [s["background"] for s in deck["slides"] if not s.get("background_color")] + \
                [e["file"] for s in deck["slides"] for e in s["elements"] if e["kind"] == "image"]
        for f in files:
            file_id, perm_id = upload_public_png(drive, out / f, folder)
            uploaded.append((file_id, perm_id))
            urls[f] = f"https://drive.google.com/uc?export=view&id={file_id}"
        time.sleep(5)  # a fresh "anyone with the link" permission takes a moment to apply

        old = [s["objectId"] for s in pres.get("slides", [])]
        if any(oid.startswith("b2s_s") for oid in old):
            # Rebuilding in place: remove our slides first so their object IDs can be reused.
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [
                {"deleteObject": {"objectId": oid}} for oid in old if oid.startswith("b2s_s")]}))
            old = [oid for oid in old if not oid.startswith("b2s_s")]
        # Phase 1: slides with backgrounds. Slides with a frame title use the TITLE_ONLY layout
        # and get their title placeholder mapped to our object ID.
        reqs = []
        for position, slide in enumerate(deck["slides"]):
            n = slide["page"]  # PDF page index; slides may skip pages (overlays)
            slide_id = f"b2s_s{n:03}"
            title_idx = title_element(slide)
            create = {"objectId": slide_id, "insertionIndex": position,
                      "slideLayoutReference": {"predefinedLayout": "TITLE_ONLY" if title_idx is not None else "BLANK"}}
            if title_idx is not None:
                create["placeholderIdMappings"] = [{"layoutPlaceholder": {"type": "TITLE", "index": 0},
                                                    "objectId": f"{slide_id}_t{title_idx}"}]
            reqs += [{"createSlide": create},
                     {"updatePageProperties": {
                         "objectId": slide_id,
                         "pageProperties": {"pageBackgroundFill": (
                             {"solidFill": {"color": rgb(slide["background_color"])["opaqueColor"]}}
                             if slide.get("background_color") else
                             {"stretchedPictureFill": {"contentUrl": urls[slide["background"]]}})},
                         "fields": "pageBackgroundFill"}}]
        reqs += [{"deleteObject": {"objectId": oid}} for oid in old]
        batch_with_image_retry(slides, pid, reqs)

        # Placeholder sizes (needed to resize them) and any extra layout placeholders.
        created = execute(slides.presentations().get(
            presentationId=pid,
            fields="slides(objectId,pageElements(objectId,size),slideProperties/notesPage/notesProperties)"))
        page_elements = {s["objectId"]: s.get("pageElements", []) for s in created["slides"]}
        speaker_notes = {s["objectId"]: s.get("slideProperties", {}).get("notesPage", {})
                         .get("notesProperties", {}).get("speakerNotesObjectId") for s in created["slides"]}
        placeholder_dy = 0.0 if abs(page_h / page_w - 9 / 16) < 0.003 else PPTX_TITLE_DY
        # Internal link targets: PDF page -> slide. A skipped overlay step maps to the kept
        # (last) step of its frame, which comes right after it.
        kept = sorted(s["page"] for s in deck["slides"])
        page_slide = {}
        for page in range(kept[-1] + 1):
            target = next(k for k in kept if k >= page)
            page_slide[page] = f"b2s_s{target:03}"

        # Phase 2: content, one batch per slide.
        for slide in deck["slides"]:
            n = slide["page"]
            slide_id = f"b2s_s{n:03}"
            title_idx = title_element(slide)
            title_oid = f"{slide_id}_t{title_idx}" if title_idx is not None else None
            reqs = [{"deleteObject": {"objectId": e["objectId"]}}
                    for e in page_elements.get(slide_id, []) if e["objectId"] != title_oid]
            element_ids = []
            for i, el in enumerate(slide["elements"]):  # shapes, then pictures, then text on top
                if el["kind"] == "shape":
                    oid = f"{slide_id}_s{i}"
                    reqs += shape_requests(el, slide_id, oid, scale)
                elif el["kind"] == "table":
                    oid = f"{slide_id}_tab{i}"
                    reqs += table_requests(el, slide_id, oid, scale, fonts)
                elif el["kind"] == "image":
                    oid = f"{slide_id}_f{i}"
                    reqs.append(image_request(el, slide_id, oid, scale, urls[el["file"]]))
                else:
                    oid = f"{slide_id}_t{i}"
                    placeholder = None
                    if oid == title_oid:
                        size = next(e["size"] for e in page_elements[slide_id] if e["objectId"] == oid)
                        placeholder = {"base_w": size["width"]["magnitude"] / EMU_PER_PT,
                                       "base_h": size["height"]["magnitude"] / EMU_PER_PT, "dy": placeholder_dy}
                    reqs += text_box_requests(el, slide_id, oid, scale, fonts, placeholder, page_slide)
                element_ids.append(oid)
            if slide.get("notes") and speaker_notes.get(slide_id):
                reqs.append({"insertText": {"objectId": speaker_notes[slide_id], "text": slide["notes"]}})
            if title_oid and len(slide["elements"]) > 1:
                # The placeholder was created with the slide, below everything added since.
                reqs.append({"updatePageElementsZOrder": {"pageElementObjectIds": [title_oid],
                                                          "operation": "BRING_TO_FRONT"}})
            batch_with_image_retry(slides, pid, reqs)
            state["slides"].append({"page": n, "objectId": slide_id, "elements": element_ids})
            kinds = [el["kind"] for el in slide["elements"]]
            print(f"  slide {n + 1}: {kinds.count('text')} text boxes, {kinds.count('image')} pictures, "
                  f"{kinds.count('shape')} shapes, {kinds.count('table')} tables")
    finally:
        for file_id, perm_id in uploaded:
            try:
                execute(drive.permissions().delete(fileId=file_id, permissionId=perm_id))
            except Exception as e:  # keep revoking the others
                print(f"warning: could not revoke public link on {file_id}: {e}")
    (out / "emit.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    return state
