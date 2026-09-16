"""Probe: how does paragraph lineSpacing change a Slides table's minimum row height and
the text position inside the cell?

Creates a deck with five 3x1 tables (Lato 21 pt, lineSpacing 100/85/70/55/40), prints the
row heights the API reports and saves a thumbnail to out/probe_table_rows.png.

Usage: python tools/probe_table_rows.py
"""

from pathlib import Path

from beamer2slides.google_auth import slides_service
from beamer2slides.gslides import EMU_PER_PT, emu, execute, pt, save_thumbnail

OUT = Path(__file__).resolve().parents[1] / "out"
SPACINGS = [100, 85, 70, 55, 40]


def main() -> None:
    slides = slides_service()
    pres = execute(slides.presentations().create(body={"title": "b2s probe table rows"}))
    pid = pres["presentationId"]
    page = pres["slides"][0]["objectId"]
    reqs = [{"deleteObject": {"objectId": e["objectId"]}} for e in pres["slides"][0].get("pageElements", [])]
    for i, ls in enumerate(SPACINGS):
        oid = f"probe_tab{i}"
        reqs += [
            {"createTable": {"objectId": oid, "rows": 3, "columns": 1, "elementProperties": {
                "pageObjectId": page, "size": {"width": emu(110), "height": emu(30)},
                "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                              "translateX": round((20 + i * 135) * EMU_PER_PT), "translateY": round(60 * EMU_PER_PT)}}}},
            {"updateTableRowProperties": {"objectId": oid, "rowIndices": [0, 1, 2],
                                          "tableRowProperties": {"minRowHeight": emu(1)}, "fields": "minRowHeight"}},
        ]
        for r in range(3):
            loc = {"rowIndex": r, "columnIndex": 0}
            reqs += [
                {"insertText": {"objectId": oid, "cellLocation": loc, "text": "Hxg"}},
                {"updateTextStyle": {"objectId": oid, "cellLocation": loc, "textRange": {"type": "ALL"},
                                     "style": {"fontFamily": "Lato", "fontSize": pt(21)}, "fields": "fontFamily,fontSize"}},
                {"updateParagraphStyle": {"objectId": oid, "cellLocation": loc, "textRange": {"type": "ALL"},
                                          "style": {"lineSpacing": ls, "spaceAbove": pt(0), "spaceBelow": pt(0)},
                                          "fields": "lineSpacing,spaceAbove,spaceBelow"}},
            ]
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    got = execute(slides.presentations().get(presentationId=pid))
    for el in got["slides"][0]["pageElements"]:
        rows = el["table"]["tableRows"]
        print(el["objectId"], [round(r["rowHeight"].get("magnitude", 0) / EMU_PER_PT, 2) for r in rows])
    print(save_thumbnail(slides, pid, page, OUT / "probe_table_rows.png"))
    print(f"https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()
