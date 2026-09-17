"""Calibrate Google Slides fonts against beamer's Computer Modern (Sans or Roman).

  build    create the calibration deck in Google Slides
  measure  export its slides as PNGs, compile the LaTeX reference, measure both,
           and write src/beamer2slides/calibration/fonts.json (sans) or fonts_serif.json
  all      build, then measure

Usage: python tools/calibrate.py {build,measure,all} [--family sans|serif] [--refresh]
"""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import time
from pathlib import Path

from beamer2slides import ink
from beamer2slides.extract import spans as page_spans
from beamer2slides.pdf import Document
from beamer2slides.google_auth import slides_service
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "out" / "calibration"
RESULT = ROOT / "src" / "beamer2slides" / "calibration" / "fonts.json"  # ships with the package

FONT_SETS = {
    "sans": [
        "Arial",  # control: also what Slides falls back to for unknown families
        "Open Sans", "Lato", "Source Sans 3", "Source Sans Pro", "Fira Sans", "PT Sans",
        "Noto Sans", "Roboto", "IBM Plex Sans", "Nunito Sans", "Inter", "Carlito",
    ],
    "serif": [
        "Arial",  # control
        "Times New Roman", "Georgia", "Noto Serif", "Source Serif 4", "PT Serif", "Crimson Text",
        "Crimson Pro", "Libre Baskerville", "Lora", "Merriweather", "EB Garamond", "Spectral", "Tinos",
    ],
}
FONTS = FONT_SETS["sans"]
LATEX_PREAMBLE = ""  # beamer font theme for the reference document


def configure(family: str) -> None:
    global FONTS, WORK, RESULT, LATEX_PREAMBLE
    FONTS = FONT_SETS[family]
    if family == "serif":
        WORK = ROOT / "out" / "calibration_serif"
        RESULT = RESULT.with_name("fonts_serif.json")
        LATEX_PREAMBLE = r"\usefonttheme{serif}"

# (key, text, style). style picks both the LaTeX markup and the Slides text style.
WIDTH_ROWS = [
    ("pangram", "The quick brown fox jumps over the lazy dog", "regular"),
    ("item", "Another first level item that is long enough to wrap", "regular"),
    ("lorem", "Lorem ipsum dolor sit amet, consectetur adipiscing elit", "regular"),
    ("digits", "0123456789 (x, y) = [a; b] 3.14 + 2.71", "regular"),
    ("caps", "SPEAKER NOTES AND CAPITALS: WHY NOT?", "regular"),
    ("bold", "The quick brown fox jumps over the lazy dog", "bold"),
    ("italic", "The quick brown fox jumps over the lazy dog", "italic"),
    ("title", "Itemize, nested and frame titles", "title"),
]
TEXT_ROWS = ["pangram", "item", "lorem"]
LATEX_STYLE = {"regular": "{}", "bold": r"\textbf{{{}}}", "italic": r"\textit{{{}}}", "title": r"{{\Large {}}}"}

WIDTH_SIZE = 18
VERTICAL_SIZES = [14, 24, 40]
CAPS = "HIMENZ"  # flat tops and bottoms: ink top = cap height, ink bottom = baseline
VERTICAL_BOXES = {  # key: (x, text)
    "single": (20, CAPS),
    "soft": (260, "\v".join([CAPS] * 3)),        # line breaks inside one paragraph
    "paragraphs": (500, "\n".join([CAPS] * 3)),      # one paragraph per line
}
SLIDE_W = 720
V_TOP, V_W, V_H = 30, 200, 360
W_LEFT, W_TOP, W_PITCH, W_W, W_H = 10, 10, 48, 700, 40


# ---------------------------------------------------------------- build

def styled_box(object_id, page_id, x, y, w, h, text, font, size, bold=False, italic=False):
    return [
        text_box(object_id, page_id, x, y, w, h),
        {"insertText": {"objectId": object_id, "text": text}},
        {"updateTextStyle": {
            "objectId": object_id, "textRange": {"type": "ALL"},
            "style": {"fontFamily": font, "fontSize": pt(size), "bold": bold, "italic": italic,
                      "foregroundColor": {"opaqueColor": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}}},
            "fields": "fontFamily,fontSize,bold,italic,foregroundColor",
        }},
        {"updateParagraphStyle": {
            "objectId": object_id, "textRange": {"type": "ALL"},
            "style": {"lineSpacing": 100, "spaceAbove": pt(0), "spaceBelow": pt(0)},
            "fields": "lineSpacing,spaceAbove,spaceBelow",
        }},
    ]


def build() -> None:
    slides = slides_service()
    pres = execute(slides.presentations().create(body={"title": f"beamer2slides font calibration ({WORK.name})"}))
    pid = pres["presentationId"]
    width = pres["pageSize"]["width"]["magnitude"] / 12700
    assert abs(width - SLIDE_W) < 0.5, f"unexpected page width {width}"

    slide_ids = []
    for fi, font in enumerate(FONTS):
        reqs = []
        sid = f"cal_f{fi}_w"
        slide_ids.append(sid)
        reqs.append({"createSlide": {"objectId": sid, "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
        for ri, (_, text, style) in enumerate(WIDTH_ROWS):
            reqs += styled_box(f"{sid}_{ri}", sid, W_LEFT, W_TOP + ri * W_PITCH, W_W, W_H, text, font,
                               WIDTH_SIZE, bold=style == "bold", italic=style == "italic")
        for size in VERTICAL_SIZES:
            sid = f"cal_f{fi}_v{size}"
            slide_ids.append(sid)
            reqs.append({"createSlide": {"objectId": sid, "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
            for key, (x, text) in VERTICAL_BOXES.items():
                reqs += styled_box(f"{sid}_{key}", sid, x, V_TOP, V_W, V_H, text, font, size)
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
        print(f"built slides for {font}")

    first = pres["slides"][0]["objectId"]
    execute(slides.presentations().batchUpdate(
        presentationId=pid, body={"requests": [{"deleteObject": {"objectId": first}}]}))

    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "state.json").write_text(json.dumps(
        {"presentationId": pid, "fonts": FONTS, "slides": slide_ids}, indent=2), encoding="utf-8")
    shutil.rmtree(WORK / "thumbs", ignore_errors=True)
    print("deck:", f"https://docs.google.com/presentation/d/{pid}/edit")


# ---------------------------------------------------------------- reference (LaTeX)

def find_engine(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    miktex = Path(os.environ["LOCALAPPDATA"]) / "Programs/MiKTeX/miktex/bin/x64" / f"{name}.exe"
    if miktex.exists():
        return str(miktex)
    raise FileNotFoundError(name)


def reference() -> dict:
    """Compile the strings with beamer + pdflatex and measure their ink."""
    ref_dir = WORK / "reference"
    ref_dir.mkdir(parents=True, exist_ok=True)
    frames = [rf"\begin{{frame}}\mbox{{{LATEX_STYLE[style].format(text)}}}\end{{frame}}"
              for _, text, style in WIDTH_ROWS]
    frames.append(rf"\begin{{frame}}\mbox{{{CAPS}}}\end{{frame}}")
    tex = "\n".join([
        r"\documentclass[aspectratio=169]{beamer}",
        r"\setbeamertemplate{navigation symbols}{}", LATEX_PREAMBLE,
        r"\begin{document}", *frames, r"\end{document}", "",
    ])
    (ref_dir / "reference.tex").write_text(tex, encoding="utf-8")
    subprocess.run([find_engine("pdflatex"), "-interaction=nonstopmode", "-halt-on-error", "reference.tex"],
                   cwd=ref_dir, check=True, capture_output=True)

    doc = Document(ref_dir / "reference.pdf")
    zoom = 12.0
    rows = {}
    for (key, text, style), page in zip(WIDTH_ROWS, doc):
        spans = [s for s in page_spans(page) if s["text"].strip()]
        box = ink.ink_box(ink.render_gray(page, zoom), zoom)
        rows[key] = {"font": spans[0]["font"], "size": round(spans[0]["size"], 3), "ink_width": box.width}
    caps_page = doc[len(WIDTH_ROWS)]
    caps_span = page_spans(caps_page)[0]
    caps_box = ink.ink_box(ink.render_gray(caps_page, zoom), zoom)
    return {
        "engine": "pdflatex",
        "rows": rows,
        "cap_height_em": caps_box.height / caps_span["size"],
        "cap_font": caps_span["font"],
    }


# ---------------------------------------------------------------- measure (Slides)

def fetch_thumbnails(state: dict, refresh: bool) -> Path:
    slides = slides_service()
    thumbs = WORK / "thumbs"
    for sid in state["slides"]:
        path = thumbs / f"{sid}.png"
        if path.exists() and not refresh:
            continue
        save_thumbnail(slides, state["presentationId"], sid, path)
        print("thumbnail", sid)
        time.sleep(1.5)  # getThumbnail is an expensive read with a low per-minute quota
    return thumbs


def linear_fit(xs: list[float], ys: list[float]) -> dict:
    """ys = a + b * xs, least squares. `a` in pt, `b` in em."""
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    a = my - b * mx
    resid = max(abs(y - (a + b * x)) for x, y in zip(xs, ys))
    return {"a_pt": round(a, 3), "b_em": round(b, 4), "max_residual_pt": round(resid, 3)}


def measure_font(fi: int, thumbs: Path, ref: dict) -> dict:
    gray = ink.load_gray(thumbs / f"cal_f{fi}_w.png")
    scale = gray.shape[1] / SLIDE_W
    ratios, widths = {}, {}
    for ri, (key, _, _) in enumerate(WIDTH_ROWS):
        top = W_TOP + ri * W_PITCH
        box = ink.ink_box(gray, scale, ink.Box(W_LEFT - 3, top - 3, W_LEFT + W_W + 3, top + W_H + 3))
        widths[key] = round(box.width, 2)
        r = ref["rows"][key]
        ratios[key] = round((box.width / WIDTH_SIZE) / (r["ink_width"] / r["size"]), 4)

    sizes, baseline, inset, cap_em, soft_em, para_em = [], [], [], [], [], []
    for size in VERTICAL_SIZES:
        gray = ink.load_gray(thumbs / f"cal_f{fi}_v{size}.png")
        scale = gray.shape[1] / SLIDE_W
        bands = {}
        for key, (x, _) in VERTICAL_BOXES.items():
            bands[key] = ink.ink_bands(gray, scale, ink.Box(x - 3, V_TOP - 3, x + V_W + 3, V_TOP + V_H + 3))
        if any(len(bands[k]) != n for k, n in (("single", 1), ("soft", 3), ("paragraphs", 3))):
            print(f"  skipping f{fi} at {size} pt: test string wraps in this font")
            continue
        single = bands["single"][0]
        sizes.append(size)
        baseline.append(single.y1 - V_TOP)
        inset.append(single.x0 - VERTICAL_BOXES["single"][0])
        cap_em.append(single.height / size)
        for key, out in (("soft", soft_em), ("paragraphs", para_em)):
            lines = bands[key]
            out += [(lines[i + 1].y1 - lines[i].y1) / size for i in range(2)]

    # Running text sets the size correction; capitals, digits and other styles are judged
    # relative to it, since one font size has to serve all of them.
    text = [ratios[k] for k in TEXT_ROWS]
    mean_ratio = statistics.fmean(text)
    relative = {k: round(ratios[k] / mean_ratio, 4) for k in ("caps", "digits", "bold", "italic", "title")}
    cap = statistics.fmean(cap_em)
    return {
        "width_ratio": {
            "text_mean": round(mean_ratio, 4),
            "text_spread": round(max(text) - min(text), 4),
            "relative_to_text": relative,
            # Worst mismatch left after the size correction (title excluded: CMSS12 is a
            # different optical size, and titles get their own size anyway).
            "max_relative_deviation": round(max(abs(v - 1) for k, v in relative.items() if k != "title"), 4),
            "by_row": ratios,
        },
        "ink_widths_pt_at_18": widths,
        "cap_height_em": round(cap, 4),
        # If the size is scaled by 1/regular_mean so widths match CM, how tall do caps look?
        "cap_height_vs_cm_after_width_match": round(cap / mean_ratio / ref["cap_height_em"], 4),
        "first_baseline_offset": linear_fit(sizes, baseline),
        "left_ink_offset": linear_fit(sizes, inset),
        "line_pitch_em": {"soft_break": round(statistics.fmean(soft_em), 4),
                          "paragraph": round(statistics.fmean(para_em), 4)},
    }


def measure(refresh: bool) -> None:
    state = json.loads((WORK / "state.json").read_text(encoding="utf-8"))
    thumbs = fetch_thumbnails(state, refresh)
    ref = reference()
    fonts = {font: measure_font(fi, thumbs, ref) for fi, font in enumerate(state["fonts"])}

    arial = fonts["Arial"]["ink_widths_pt_at_18"]
    for font, m in fonts.items():
        same = all(abs(m["ink_widths_pt_at_18"][k] - arial[k]) < 0.6 for k in arial)
        m["same_widths_as_arial"] = same and font != "Arial"

    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps({
        "slide_width_pt": SLIDE_W, "width_size_pt": WIDTH_SIZE, "vertical_sizes_pt": VERTICAL_SIZES,
        "presentationId": state["presentationId"], "reference": ref, "fonts": fonts,
    }, indent=2), encoding="utf-8")

    print(f"\nreference: {ref['rows']['pangram']['font']} {ref['rows']['pangram']['size']} pt, "
          f"cap height {ref['cap_height_em']:.3f} em\n")
    print(f"{'font':<16}{'text':>6}{'sprd':>6} |{'CAPS':>6}{'digit':>6}{'bold':>6}{'ital':>6}{'title':>6}"
          f"{'maxdev':>7} |{'capH*':>6} |{'baseline a+b (resid)':>22}{'inset a+b':>13}{'pitch':>6}")
    for font, m in sorted(fonts.items(), key=lambda kv: kv[1]["width_ratio"]["max_relative_deviation"]):
        w, bl, li = m["width_ratio"], m["first_baseline_offset"], m["left_ink_offset"]
        rel = w["relative_to_text"]
        print(f"{font:<16}{w['text_mean']:>6.3f}{w['text_spread']:>6.3f} |"
              + "".join(f"{rel[k]:>6.3f}" for k in ("caps", "digits", "bold", "italic", "title"))
              + f"{w['max_relative_deviation']:>7.3f} |{m['cap_height_vs_cm_after_width_match']:>6.3f} |"
              f"{bl['a_pt']:>8.2f}+{bl['b_em']:.4f} ({bl['max_residual_pt']:.2f})"
              f"{li['a_pt']:>6.1f}+{li['b_em']:.3f}{m['line_pitch_em']['paragraph']:>6.3f}"
              f"{'  ARIAL METRICS?' if m['same_widths_as_arial'] else ''}")
    print("\ntext  = width vs CM Sans at the same nominal size (running text); sprd = spread over text rows")
    print("CAPS..title = width relative to the font's own text ratio; maxdev = worst of CAPS/digit/bold/ital")
    print("capH* = cap height vs CM after scaling the size by 1/text")
    print(f"written: {RESULT}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["build", "measure", "all"])
    ap.add_argument("--refresh", action="store_true", help="re-download thumbnails")
    ap.add_argument("--family", choices=["sans", "serif"], default="sans")
    args = ap.parse_args()
    configure(args.family)
    if args.command in ("build", "all"):
        build()
    if args.command in ("measure", "all"):
        measure(args.refresh)


if __name__ == "__main__":
    main()
