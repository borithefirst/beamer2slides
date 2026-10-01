"""Probe: can a layout's (empty) TITLE placeholder be moved and given a text style that new
slides using the layout inherit?

Creates a deck, styles the TITLE_ONLY layout's title placeholder (red Lato 30 pt, moved),
adds a slide with that layout, types a title into it, and saves a thumbnail to
out/probe_layout_title.png. Prints the API errors, if any.

Usage: python tools/probe_layout_title.py
"""

from pathlib import Path

from beamer2slides.gapi import HttpError
from beamer2slides.google_auth import slides_service
from beamer2slides.google_types import PageElement, SlidesRequest, object_id, part, presentation_id
from beamer2slides.gslides import EMU_PER_PT, execute, pt, save_thumbnail
from beamer2slides.json_types import JsonObject, as_array

OUT = Path(__file__).resolve().parents[1] / "out"


def shape_of(e: PageElement) -> JsonObject:
    """A page element's shape part ({} for an element that is no shape)."""
    return part(e.get("shape"), f"{e.get('objectId')}.shape")


def main() -> None:
    slides = slides_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe layout title"}))
    pid = presentation_id(pres)
    layout = next(l for l in pres.get("layouts", []) if l.get("layoutProperties", {}).get("name") == "TITLE_ONLY")
    title = next(e for e in layout.get("pageElements", [])
                 if part(shape_of(e).get("placeholder"), "placeholder").get("type") == "TITLE")
    tid = object_id(title)
    print("layout title placeholder", tid, title.get("size"), title.get("transform"))
    attempts: dict[str, SlidesRequest] = {
        "transform": {"updatePageElementTransform": {"objectId": tid, "applyMode": "ABSOLUTE", "transform": {
            "scaleX": 1, "scaleY": 1, "unit": "EMU", "translateX": 30 * EMU_PER_PT, "translateY": 10 * EMU_PER_PT}}},
        "text style ALL": {"updateTextStyle": {"objectId": tid, "textRange": {"type": "ALL"},
                                               "style": {"fontFamily": "Lato", "fontSize": pt(30),
                                                         "foregroundColor": {"opaqueColor": {"rgbColor": {"red": 0.8}}}},
                                               "fields": "fontFamily,fontSize,foregroundColor"}},
        "paragraph style ALL": {"updateParagraphStyle": {"objectId": tid, "textRange": {"type": "ALL"},
                                                         "style": {"alignment": "START"}, "fields": "alignment"}},
        "shape props": {"updateShapeProperties": {"objectId": tid,
                                                  "shapeProperties": {"contentAlignment": "TOP"}, "fields": "contentAlignment"}},
    }
    for name, req in attempts.items():
        try:
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [req]}))
            print("ok  ", name)
        except HttpError as e:
            print("FAIL", name, str(e)[:300])
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [
        {"createSlide": {"objectId": "probe_slide", "slideLayoutReference": {"layoutId": object_id(layout)},
                         "placeholderIdMappings": [{"layoutPlaceholder": {"type": "TITLE", "index": 0},
                                                    "objectId": "probe_title"}]}},
        {"insertText": {"objectId": "probe_title", "text": "A title typed on a new slide"}},
    ]}))
    got = execute(slides.presentations().get(presentationId=pid, fields="slides(objectId,pageElements(objectId,transform,shape/text))"))
    for s in got.get("slides", []):
        if object_id(s) == "probe_slide":
            for e in s.get("pageElements", []):
                text = part(shape_of(e).get("text"), "shape.text")
                print(object_id(e), e.get("transform"), as_array(text.get("textElements", []), "textElements")[:2])
    print(save_thumbnail(slides, pid, "probe_slide", OUT / "probe_layout_title.png", None))
    print(f"https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()
