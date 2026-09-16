"""Probe: can a layout's (empty) TITLE placeholder be moved and given a text style that new
slides using the layout inherit?

Creates a deck, styles the TITLE_ONLY layout's title placeholder (red Lato 30 pt, moved),
adds a slide with that layout, types a title into it, and saves a thumbnail to
out/probe_layout_title.png. Prints the API errors, if any.

Usage: python tools/probe_layout_title.py
"""

from pathlib import Path

from googleapiclient.errors import HttpError

from beamer2slides.google_auth import slides_service
from beamer2slides.gslides import EMU_PER_PT, emu, execute, pt, save_thumbnail

OUT = Path(__file__).resolve().parents[1] / "out"


def main() -> None:
    slides = slides_service()
    pres = execute(slides.presentations().create(body={"title": "b2s probe layout title"}))
    pid = pres["presentationId"]
    layout = next(l for l in pres["layouts"] if l["layoutProperties"]["name"] == "TITLE_ONLY")
    title = next(e for e in layout["pageElements"] if e.get("shape", {}).get("placeholder", {}).get("type") == "TITLE")
    print("layout title placeholder", title["objectId"], title["size"], title["transform"])
    attempts = {
        "transform": {"updatePageElementTransform": {"objectId": title["objectId"], "applyMode": "ABSOLUTE", "transform": {
            "scaleX": 1, "scaleY": 1, "unit": "EMU", "translateX": 30 * EMU_PER_PT, "translateY": 10 * EMU_PER_PT}}},
        "text style ALL": {"updateTextStyle": {"objectId": title["objectId"], "textRange": {"type": "ALL"},
                                               "style": {"fontFamily": "Lato", "fontSize": pt(30),
                                                         "foregroundColor": {"opaqueColor": {"rgbColor": {"red": 0.8}}}},
                                               "fields": "fontFamily,fontSize,foregroundColor"}},
        "paragraph style ALL": {"updateParagraphStyle": {"objectId": title["objectId"], "textRange": {"type": "ALL"},
                                                         "style": {"alignment": "START"}, "fields": "alignment"}},
        "shape props": {"updateShapeProperties": {"objectId": title["objectId"],
                                                  "shapeProperties": {"contentAlignment": "TOP"}, "fields": "contentAlignment"}},
    }
    for name, req in attempts.items():
        try:
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [req]}))
            print("ok  ", name)
        except HttpError as e:
            print("FAIL", name, str(e)[:300])
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [
        {"createSlide": {"objectId": "probe_slide", "slideLayoutReference": {"layoutId": layout["objectId"]},
                         "placeholderIdMappings": [{"layoutPlaceholder": {"type": "TITLE", "index": 0},
                                                    "objectId": "probe_title"}]}},
        {"insertText": {"objectId": "probe_title", "text": "A title typed on a new slide"}},
    ]}))
    got = execute(slides.presentations().get(presentationId=pid, fields="slides(objectId,pageElements(objectId,transform,shape/text))"))
    for s in got["slides"]:
        if s["objectId"] == "probe_slide":
            for e in s["pageElements"]:
                print(e["objectId"], e.get("transform"), e.get("shape", {}).get("text", {}).get("textElements", [])[:2])
    print(save_thumbnail(slides, pid, "probe_slide", OUT / "probe_layout_title.png"))
    print(f"https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()
