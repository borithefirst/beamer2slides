"""What does a bullet added in Slides look like in a converted list?

A person who clicks at the end of a list item and presses Enter gets a new paragraph whose
bullet and text style Slides takes from the paragraph they split. This probe does the API's
nearest equivalent - insertText of "\\n<text>" before an item's own newline - on the bulleted
text boxes of one slide of a converted deck, and prints, per paragraph, what the bullet and the
text carry: the glyph, the list level's bulletStyle, the paragraph's own bullet style, indents
and the first run's style. With --add it inserts, reads back and saves a thumbnail.

    python tools/probe_new_bullet.py out/agent-bullets --slide 2            # read only
    python tools/probe_new_bullet.py out/agent-bullets --slide 2 --add      # insert, then read

The insert writes into the deck: point it at a scratch conversion, never at somebody's deck.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from beamer2slides import gslides
from beamer2slides.google_auth import slides_service
from beamer2slides.google_types import Page, SlidesRequest, object_id, part
from beamer2slides.json_types import Json, JsonObject, JsonShapeError, as_int, as_object, as_objects, as_str


@dataclass(frozen=True, kw_only=True)
class Paragraph:
    """One paragraph of a shape's text: its range, words, paragraphMarker and first run's style."""
    start: int
    end: int
    text: str
    marker: JsonObject
    first: JsonObject


def num(v: Json) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise JsonShapeError(f"a number was expected, found {v!r}")
    return v


def _colour(style: JsonObject) -> str | None:
    rgb = part(part(style.get("foregroundColor"), "foregroundColor").get("opaqueColor"), "opaqueColor").get("rgbColor")
    if rgb is None:
        return None
    colour = as_object(rgb, "rgbColor")
    return "#%02x%02x%02x" % tuple(round(255 * num(colour.get(c, 0.0))) for c in ("red", "green", "blue"))


def _style(style: JsonObject) -> str:
    size = part(style.get("fontSize"), "fontSize").get("magnitude")
    return f"{style.get('fontFamily')} {size} {_colour(style)}"


def paragraphs(shape: JsonObject) -> list[Paragraph]:
    """(start, end, text, paragraphMarker, first run style) per paragraph of a shape."""
    elements = as_objects(part(shape.get("text"), "text").get("textElements", []), "textElements")
    out: list[Paragraph] = []
    for i, te in enumerate(elements):
        if "paragraphMarker" not in te:
            continue
        start = as_int(te.get("startIndex", 0), "startIndex")
        end = as_int(te.get("endIndex"), "endIndex")
        text, first = "", None
        for run in elements[i + 1:]:
            if "paragraphMarker" in run:
                break
            if "textRun" in run:
                text_run = as_object(run["textRun"], "textRun")
                text += as_str(text_run.get("content"), "textRun.content")
                if first is None:
                    first = part(text_run.get("style"), "textRun.style")
        out.append(Paragraph(start=start, end=end, text=text,
                             marker=as_object(te["paragraphMarker"], "paragraphMarker"),
                             first=first if first is not None else {}))
    return out


def report(shape: JsonObject) -> None:
    lists = part(part(shape.get("text"), "text").get("lists"), "lists")
    for p in paragraphs(shape):
        bullet = part(p.marker.get("bullet"), "bullet")
        ps = part(p.marker.get("style"), "paragraph style")
        indent = [part(ps.get(k), k).get("magnitude") for k in ("indentStart", "indentFirstLine")]
        line = f"  [{p.start:3}-{p.end:3}] {p.text.strip()[:24]!r:28}"
        if bullet:
            level = as_int(bullet.get("nestingLevel", 0), "nestingLevel")
            listed = part(lists.get(as_str(bullet.get("listId"), "listId")), "list")
            lvl = part(part(listed.get("nestingLevel"), "nestingLevel").get(str(level)), "nesting level")
            line += (f" L{level} glyph={bullet.get('glyph')!r}"
                     f" own=({_style(part(bullet.get('bulletStyle'), 'bulletStyle'))})"
                     f" list=({_style(part(lvl.get('bulletStyle'), 'bulletStyle'))})")
        line += f" indent={indent} text=({_style(p.first)})"
        print(line)


def bulleted(slide: Page) -> Iterator[tuple[str, JsonObject]]:
    for el in slide.get("pageElements", []):
        shape = el.get("shape")
        if shape and any(p.marker.get("bullet") for p in paragraphs(shape)):
            yield object_id(el), shape


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path, help="a convert output folder (emit.json)")
    ap.add_argument("--slide", type=int, default=2, help="1-based slide number")
    ap.add_argument("--add", action="store_true", help="insert new bullets (writes to the deck)")
    ap.add_argument("--text", default="New point")
    args = ap.parse_args()
    out: Path = args.out
    number: int = args.slide
    add: bool = args.add
    new_text: str = args.text
    emitted = as_object(json.loads((out / "emit.json").read_text(encoding="utf-8")), "emit.json")
    pid = as_str(emitted.get("presentationId"), "emit.json's presentationId")
    slides = slides_service(None)

    def read() -> Page:
        pres = gslides.execute(slides.presentations().get(presentationId=pid))
        return pres.get("slides", [])[number - 1]

    slide = read()
    boxes = list(bulleted(slide))
    for oid, shape in boxes:
        print(f"{oid}:")
        report(shape)
    if not add or not boxes:
        return
    oid, shape = boxes[0]
    items = [p for p in paragraphs(shape) if p.marker.get("bullet")]
    # One after the last item (before the shape's final newline) and one after the first.
    spots = sorted({items[-1].end - 1, items[0].end - 1}, reverse=True)
    reqs: list[SlidesRequest] = [{"insertText": {"objectId": oid, "insertionIndex": at,
                                                 "text": f"\n{new_text} {n}"}} for n, at in enumerate(spots)]
    gslides.execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    print(f"after inserting at {spots}:")
    for o, s in bulleted(read()):
        if o == oid:
            report(s)
    path = out / "probe_new_bullet" / f"slide-{number:03}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    gslides.save_thumbnail(slides, pid, object_id(slide), path, None)
    print(f"thumbnail: {path}")


if __name__ == "__main__":
    main()
