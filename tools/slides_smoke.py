"""End-to-end check of the Google side: auth, create, custom page size, text box, thumbnail.

Usage: python tools/slides_smoke.py
"""

import urllib.request
from pathlib import Path

from beamer2slides.google_auth import slides_service
from beamer2slides.google_types import Dimension, SlidesRequest, object_id, presentation_id

EMU_PER_PT = 12700
OUT = Path(__file__).resolve().parents[1] / "out" / "smoke"


def pt(v: float) -> Dimension:
    return {"magnitude": v * EMU_PER_PT, "unit": "EMU"}


def main() -> None:
    slides = slides_service(None)

    # Beamer 4:3 (362.8 x 272.1 pt) scaled to the standard 720 x 540 pt (Google takes the
    # pageSize and ignores it: a new deck is always 16:9).
    pres = slides.presentations().create(body={
        "title": "beamer2slides smoke test",
        "pageSize": {"width": pt(720), "height": pt(540)},
    }).execute()
    pid = presentation_id(pres)
    size = pres.get("pageSize", {})
    print("created:", f"https://docs.google.com/presentation/d/{pid}/edit")
    print("page size (pt):", size.get("width", {}).get("magnitude", 0) / EMU_PER_PT, "x",
          size.get("height", {}).get("magnitude", 0) / EMU_PER_PT)

    first = pres.get("slides", [])[0]
    page_id = object_id(first)
    requests: list[SlidesRequest] = [
        # Clear the default title-slide placeholders.
        *({"deleteObject": {"objectId": object_id(el)}}
          for el in first.get("pageElements", [])),
        {"createShape": {
            "objectId": "smoke_text",
            "shapeType": "TEXT_BOX",
            "elementProperties": {
                "pageObjectId": page_id,
                "size": {"width": pt(500), "height": pt(60)},
                "transform": {"scaleX": 1, "scaleY": 1, "translateX": 56, "translateY": 40, "unit": "PT"},
            },
        }},
        {"insertText": {"objectId": "smoke_text", "text": "Itemize, nested"}},
        {"updateTextStyle": {
            "objectId": "smoke_text",
            "style": {"fontFamily": "Open Sans", "fontSize": {"magnitude": 28.5, "unit": "PT"},
                      "foregroundColor": {"opaqueColor": {"rgbColor": {"red": 0.2, "green": 0.2, "blue": 0.7}}}},
            "fields": "fontFamily,fontSize,foregroundColor",
        }},
    ]
    slides.presentations().batchUpdate(presentationId=pid, body={"requests": requests}).execute()

    thumb = slides.presentations().pages().getThumbnail(
        presentationId=pid, pageObjectId=page_id,
        thumbnailProperties_mimeType="PNG", thumbnailProperties_thumbnailSize="LARGE",
    ).execute()
    OUT.mkdir(parents=True, exist_ok=True)
    png = OUT / "slide1.png"
    url = thumb.get("contentUrl")
    if url is None:
        raise ValueError(f"getThumbnail answered no picture: {sorted(thumb)}")
    urllib.request.urlretrieve(url, png)
    print(f"thumbnail: {thumb.get('width')}x{thumb.get('height')} px -> {png}")


if __name__ == "__main__":
    main()
