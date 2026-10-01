"""Probe: how does paragraph lineSpacing change a Slides table's minimum row height and
the text position inside the cell?

Creates a deck with five 3x1 tables (Lato 21 pt, lineSpacing 100/85/70/55/40), prints the
row heights the API reports and saves a thumbnail to out/probe_table_rows.png.

Usage: python tools/probe_table_rows.py
"""

from pathlib import Path

from beamer2slides.google_auth import slides_service
from beamer2slides.google_types import (Dimension, SlidesRequest, SlidesTableCellLocation, object_id, part,
                                        presentation_id)
from beamer2slides.gslides import EMU_PER_PT, execute, save_thumbnail
from beamer2slides.json_types import Json, as_objects


OUT = Path(__file__).resolve().parents[1] / "out"
SPACINGS = [100, 85, 70, 55, 40]


def pt(v: float) -> Dimension:
    """`gslides.pt` as a request's dimension."""
    return {"magnitude": v, "unit": "PT"}


def emu(v_pt: float) -> Dimension:
    """`gslides.emu` as a request's dimension."""
    return {"magnitude": round(v_pt * EMU_PER_PT), "unit": "EMU"}


def as_number(v: Json) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"a number was expected, found {v!r}")
    return v


def main() -> None:
    slides = slides_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe table rows"}))
    pid = presentation_id(pres)
    first = pres.get("slides", [])[0]
    page = object_id(first)
    reqs: list[SlidesRequest] = [{"deleteObject": {"objectId": object_id(e)}} for e in first.get("pageElements", [])]
    for i, ls in enumerate(SPACINGS):
        oid = f"probe_tab{i}"
        reqs.extend([
            {"createTable": {"objectId": oid, "rows": 3, "columns": 1, "elementProperties": {
                "pageObjectId": page, "size": {"width": emu(110), "height": emu(30)},
                "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                              "translateX": round((20 + i * 135) * EMU_PER_PT), "translateY": round(60 * EMU_PER_PT)}}}},
            {"updateTableRowProperties": {"objectId": oid, "rowIndices": [0, 1, 2],
                                          "tableRowProperties": {"minRowHeight": emu(1)}, "fields": "minRowHeight"}},
        ])
        for r in range(3):
            loc: SlidesTableCellLocation = {"rowIndex": r, "columnIndex": 0}
            reqs.extend([
                {"insertText": {"objectId": oid, "cellLocation": loc, "text": "Hxg"}},
                {"updateTextStyle": {"objectId": oid, "cellLocation": loc, "textRange": {"type": "ALL"},
                                     "style": {"fontFamily": "Lato", "fontSize": pt(21)}, "fields": "fontFamily,fontSize"}},
                {"updateParagraphStyle": {"objectId": oid, "cellLocation": loc, "textRange": {"type": "ALL"},
                                          "style": {"lineSpacing": ls, "spaceAbove": pt(0), "spaceBelow": pt(0)},
                                          "fields": "lineSpacing,spaceAbove,spaceBelow"}},
            ])
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    got = execute(slides.presentations().get(presentationId=pid))
    for el in got.get("slides", [])[0].get("pageElements", []):
        rows = as_objects(part(el.get("table"), "table").get("tableRows"), "table.tableRows")
        print(object_id(el), [round(as_number(part(r.get("rowHeight"), "rowHeight").get("magnitude", 0)) / EMU_PER_PT, 2)
                              for r in rows])
    print(save_thumbnail(slides, pid, page, OUT / "probe_table_rows.png", None))
    print(f"https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()
