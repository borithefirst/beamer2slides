"""End-to-end check of the Google side: auth, create, custom page size, text box, thumbnail.

Usage: python tools/slides_smoke.py
"""

import urllib.request
from pathlib import Path

from beamer2slides.google_auth import slides_service

EMU_PER_PT = 12700
OUT = Path(__file__).resolve().parents[1] / "out" / "smoke"


def pt(v: float) -> dict:
    return {"magnitude": v * EMU_PER_PT, "unit": "EMU"}


def main() -> None:
    slides = slides_service()

    # Beamer 4:3 (362.8 x 272.1 pt) scaled to the standard 720 x 540 pt.
    pres = slides.presentations().create(body={
        "title": "beamer2slides smoke test",
        "pageSize": {"width": pt(720), "height": pt(540)},
    }).execute()
    pid = pres["presentationId"]
    size = pres["pageSize"]
    print("created:", f"https://docs.google.com/presentation/d/{pid}/edit")
    print("page size (pt):", size["width"]["magnitude"] / EMU_PER_PT, "x",
          size["height"]["magnitude"] / EMU_PER_PT)

    page_id = pres["slides"][0]["objectId"]
    requests = [
        # Clear the default title-slide placeholders.
        *({"deleteObject": {"objectId": el["objectId"]}}
          for el in pres["slides"][0].get("pageElements", [])),
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
    urllib.request.urlretrieve(thumb["contentUrl"], png)
    print(f"thumbnail: {thumb['width']}x{thumb['height']} px -> {png}")


if __name__ == "__main__":
    main()
