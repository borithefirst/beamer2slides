"""Probe: does a tab after a short label jump to indentStart (hanging indent), like in Docs?

Creates a text box with paragraphs "2:<TAB>classify" using indentFirstLine 0 and
indentStart 40/80/120 pt, plus one where the label is wider than indentStart, and saves a
thumbnail to out/probe_tabs.png.

Usage: python tools/probe_tabs.py
"""

from pathlib import Path

from beamer2slides.google_auth import slides_service
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

OUT = Path(__file__).resolve().parents[1] / "out"
CASES = [(0, 40), (0, 80), (20, 120), (0, 10)]


def main() -> None:
    slides = slides_service()
    pres = execute(slides.presentations().create(body={"title": "b2s probe tabs"}))
    pid = pres["presentationId"]
    page = pres["slides"][0]["objectId"]
    reqs = [{"deleteObject": {"objectId": e["objectId"]}} for e in pres["slides"][0].get("pageElements", [])]
    text = "\n".join("22:\tclassify p" for _ in CASES)
    reqs += [
        text_box("probe_tabs", page, 50, 50, 500, 200),
        {"insertText": {"objectId": "probe_tabs", "text": text}},
        {"updateTextStyle": {"objectId": "probe_tabs", "textRange": {"type": "ALL"},
                             "style": {"fontFamily": "Lato", "fontSize": pt(20)}, "fields": "fontFamily,fontSize"}},
    ]
    start = 0
    for first, indent in CASES:
        end = start + len("22:\tclassify p") + 1
        reqs.append({"updateParagraphStyle": {
            "objectId": "probe_tabs", "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": end - 1},
            "style": {"indentFirstLine": pt(first), "indentStart": pt(indent)}, "fields": "indentFirstLine,indentStart"}})
        start = end
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    print(save_thumbnail(slides, pid, page, OUT / "probe_tabs.png"))
    print(f"https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()
