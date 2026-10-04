"""Probe: how does a one-character list item keep its bullet's colour?

emit styles each paragraph in its bullet's size and colour, creates the bullets, then styles the
runs in parts: one request over a whole paragraph restyles its bullet. A one-character item ('R',
real_presentation-biore s81; 'A' and 'd' on real_presentazione-rxjs s14) has no second part, so
its black run took the teal bullet black. This probe writes three teal-bulleted items "R", "ab",
"R" in black Lato 24 pt per variant of how the middle-less run is styled:

    whole        the run's style in one request over its paragraph   (what emit wrote until 2026-10-04)
    newline      the same request taking the paragraph's newline too
    fields       two requests over the paragraph, colour apart from the rest
    joiner       the item written "R" + WORD JOINER, the two units styled apart

and reads back each paragraph's bullet colour (bulletStyle.foregroundColor) and the run's colour,
saving the thumbnail to out/probe_short_bullet.png. Everything it creates is deleted.

Usage: python tools/probe_short_bullet.py
"""

from dataclasses import dataclass
from pathlib import Path

from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.google_types import (Presentation, SlidesRequest, SlidesService, SlidesTextStyle, object_id,
                                        presentation_id)
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box
from beamer2slides.json_types import as_object, as_objects, as_str

OUT = Path(__file__).resolve().parents[1] / "out"
SIZE = 24.0
TEAL: SlidesTextStyle = {"fontFamily": "Lato", "fontSize": pt(SIZE),
                         "foregroundColor": {"opaqueColor": {"rgbColor": {"red": 0.11, "green": 0.55, "blue": 0.69}}}}
BLACK: SlidesTextStyle = {"fontFamily": "Lato", "fontSize": pt(SIZE), "bold": False,
                          "foregroundColor": {"opaqueColor": {"rgbColor": {}}}}
JOINER = "⁠"


@dataclass(frozen=True, kw_only=True)
class Variant:
    name: str
    joiner: bool


VARIANTS = [Variant(name="whole", joiner=False), Variant(name="newline", joiner=False),
            Variant(name="fields", joiner=False), Variant(name="joiner", joiner=True)]


def style(oid: str, s: SlidesTextStyle, fields: str, a: int, b: int) -> SlidesRequest:
    return {"updateTextStyle": {"objectId": oid, "style": s, "fields": fields,
                                "textRange": {"type": "FIXED_RANGE", "startIndex": a, "endIndex": b}}}


def requests(oid: str, page: str, x: float, v: Variant) -> list[SlidesRequest]:
    short = "R" + (JOINER if v.joiner else "")
    items = [short, "ab", short]
    text = "\n".join(items)
    reqs: list[SlidesRequest] = [text_box(oid, page, x, 40, 150, 160), {"insertText": {"objectId": oid, "text": text}},
                                 style(oid, TEAL, "fontFamily,fontSize,foregroundColor", 0, len(text)),
                                 {"createParagraphBullets": {"objectId": oid, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                                                             "textRange": {"type": "ALL"}}}]
    every = "fontFamily,fontSize,bold,foregroundColor"
    at = 0
    for item in items:
        n = len(item)
        if item == "ab":
            reqs += [style(oid, BLACK, every, at, at + 1), style(oid, BLACK, every, at + 1, at + 2)]
        elif v.name == "whole":
            reqs.append(style(oid, BLACK, every, at, at + n))
        elif v.name == "newline":
            reqs.append(style(oid, BLACK, every, at, min(at + n + 1, len(text))))
        elif v.name == "fields":
            reqs += [style(oid, BLACK, "fontFamily,fontSize,bold", at, at + n),
                     style(oid, BLACK, "foregroundColor", at, at + n)]
        else:
            reqs += [style(oid, BLACK, every, at, at + 1), style(oid, BLACK, every, at + 1, at + n)]
        at += n + 1
    return reqs


def main() -> None:
    slides = slides_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe short bullet"}))
    pid = presentation_id(pres)
    try:
        measure(slides, pres, pid)
    finally:
        execute(drive_service(None).files().delete(fileId=pid))


def measure(slides: SlidesService, pres: Presentation, pid: str) -> None:
    first = pres.get("slides", [])[0]
    page = object_id(first)
    reqs: list[SlidesRequest] = [{"deleteObject": {"objectId": object_id(e)}} for e in first.get("pageElements", [])]
    for i, v in enumerate(VARIANTS):
        reqs += requests(f"probe_sb{i}", page, 20 + 170 * i, v)
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    png = OUT / "probe_short_bullet.png"
    save_thumbnail(slides, pid, page, png, None)
    read = execute(slides.presentations().get(presentationId=pid))
    for e in read.get("slides", [])[0].get("pageElements", []):
        name = VARIANTS[int(object_id(e)[len("probe_sb"):])].name
        cells: list[str] = []
        text = as_object(e.get("shape", {}).get("text", {}), "a box's text")
        for t in as_objects(text.get("textElements", []), "its text elements"):
            marker = t.get("paragraphMarker")
            if marker is not None:
                bullet = as_object(marker, "a paragraph marker").get("bullet")
                style = None if bullet is None else as_object(bullet, "a bullet").get("bulletStyle", {})
                colour = None if style is None else as_object(style, "a bullet style").get("foregroundColor")
                cells.append(f"bullet {colour}")
            run = t.get("textRun")
            if run is not None:
                read_run = as_object(run, "a text run")
                content = as_str(read_run.get("content", ""), "a run's content")
                if content.strip("\n" + JOINER):
                    colour = as_object(read_run.get("style", {}), "a run's style").get("foregroundColor")
                    cells.append(f"run {content!r} {colour}")
        print(name, "|", " ; ".join(cells))
    print(f"thumbnail: {png}")


if __name__ == "__main__":
    main()
