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

FONT_FOR_FAMILY = {"sans": "Lato", "serif": "Noto Serif", "mono": "Roboto Mono"}
CMTT_ADVANCE_EM, ROBOTO_MONO_ADVANCE_EM = 0.525, 0.6

BULLET_PRESETS = {
    "arrow": "BULLET_ARROW_DIAMOND_DISC",
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


def text_box_requests(el: dict, slide_id: str, object_id: str, scale: float, fonts: FontMapper) -> list[dict]:
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
                     "foregroundColor": rgb(run["color"]), "underline": False}
            fields = "fontFamily,fontSize,bold,italic,smallCaps,foregroundColor,underline"
            if run["link"]:
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
    try:
        for slide in deck["slides"]:
            uploaded.append(upload_public_png(drive, out / slide["background"], folder))
        time.sleep(5)  # a fresh "anyone with the link" permission takes a moment to apply

        old = [s["objectId"] for s in pres.get("slides", [])]
        if any(oid.startswith("b2s_s") for oid in old):
            # Rebuilding in place: remove our slides first so their object IDs can be reused.
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [
                {"deleteObject": {"objectId": oid}} for oid in old if oid.startswith("b2s_s")]}))
            old = [oid for oid in old if not oid.startswith("b2s_s")]
        cleanup = [{"deleteObject": {"objectId": oid}} for oid in old]
        for slide, (file_id, _) in zip(deck["slides"], uploaded):
            n = slide["page"]
            slide_id = f"b2s_s{n:03}"
            reqs = [{"createSlide": {"objectId": slide_id, "insertionIndex": n,
                                     "slideLayoutReference": {"predefinedLayout": "BLANK"}}},
                    {"updatePageProperties": {
                        "objectId": slide_id,
                        "pageProperties": {"pageBackgroundFill": {"stretchedPictureFill": {
                            "contentUrl": f"https://drive.google.com/uc?export=view&id={file_id}"}}},
                        "fields": "pageBackgroundFill"}}]
            element_ids = []
            for i, el in enumerate(slide["elements"]):
                oid = f"{slide_id}_t{i}"
                element_ids.append(oid)
                reqs += text_box_requests(el, slide_id, oid, scale, fonts)
            if n == 0:
                reqs = reqs + cleanup
            batch_with_image_retry(slides, pid, reqs)
            state["slides"].append({"page": n, "objectId": slide_id, "elements": element_ids})
            print(f"  slide {n + 1}: {len(element_ids)} text boxes")
    finally:
        for file_id, perm_id in uploaded:
            try:
                execute(drive.permissions().delete(fileId=file_id, permissionId=perm_id))
            except Exception as e:  # keep revoking the others
                print(f"warning: could not revoke public link on {file_id}: {e}")
    (out / "emit.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    return state
