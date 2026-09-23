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
from pathlib import Path

from beamer2slides import gslides
from beamer2slides.google_auth import slides_service


def _colour(style: dict) -> str | None:
    rgb = (((style.get("foregroundColor") or {}).get("opaqueColor") or {}).get("rgbColor"))
    if rgb is None:
        return None
    return "#%02x%02x%02x" % tuple(round(255 * rgb.get(c, 0.0)) for c in ("red", "green", "blue"))


def _style(style: dict) -> str:
    size = (style.get("fontSize") or {}).get("magnitude")
    return f"{style.get('fontFamily')} {size} {_colour(style)}"


def paragraphs(shape: dict):
    """(start, end, text, paragraphMarker, first run style) per paragraph of a shape."""
    elements = shape.get("text", {}).get("textElements", [])
    out = []
    for i, te in enumerate(elements):
        if "paragraphMarker" not in te:
            continue
        start, end = te.get("startIndex", 0), te["endIndex"]
        text, first = "", None
        for run in elements[i + 1:]:
            if "paragraphMarker" in run:
                break
            if "textRun" in run:
                text += run["textRun"]["content"]
                if first is None:
                    first = run["textRun"].get("style", {})
        out.append((start, end, text, te["paragraphMarker"], first or {}))
    return out


def report(shape: dict) -> None:
    lists = shape.get("text", {}).get("lists", {})
    for start, end, text, marker, first in paragraphs(shape):
        bullet = marker.get("bullet")
        ps = marker.get("style", {})
        indent = [(ps.get(k) or {}).get("magnitude") for k in ("indentStart", "indentFirstLine")]
        line = f"  [{start:3}-{end:3}] {text.strip()[:24]!r:28}"
        if bullet:
            level = bullet.get("nestingLevel", 0)
            lvl = lists.get(bullet["listId"], {}).get("nestingLevel", {}).get(str(level), {})
            line += (f" L{level} glyph={bullet.get('glyph')!r} own=({_style(bullet.get('bulletStyle', {}))})"
                     f" list=({_style(lvl.get('bulletStyle', {}))})")
        line += f" indent={indent} text=({_style(first)})"
        print(line)


def bulleted(slide: dict):
    for el in slide.get("pageElements", []):
        shape = el.get("shape")
        if shape and any(p[3].get("bullet") for p in paragraphs(shape)):
            yield el["objectId"], shape


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path, help="a convert output folder (emit.json)")
    ap.add_argument("--slide", type=int, default=2, help="1-based slide number")
    ap.add_argument("--add", action="store_true", help="insert new bullets (writes to the deck)")
    ap.add_argument("--text", default="New point")
    args = ap.parse_args()
    pid = json.loads((args.out / "emit.json").read_text(encoding="utf-8"))["presentationId"]
    slides = slides_service()

    def read():
        pres = gslides.execute(slides.presentations().get(presentationId=pid))
        return pres["slides"][args.slide - 1]

    slide = read()
    boxes = list(bulleted(slide))
    for oid, shape in boxes:
        print(f"{oid}:")
        report(shape)
    if not args.add or not boxes:
        return
    oid, shape = boxes[0]
    items = [p for p in paragraphs(shape) if p[3].get("bullet")]
    # One after the last item (before the shape's final newline) and one after the first.
    spots = sorted({items[-1][1] - 1, items[0][1] - 1}, reverse=True)
    reqs = [{"insertText": {"objectId": oid, "insertionIndex": at,
                            "text": f"\n{args.text} {n}"}} for n, at in enumerate(spots)]
    gslides.execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    print(f"after inserting at {spots}:")
    for o, s in bulleted(read()):
        if o == oid:
            report(s)
    path = args.out / "probe_new_bullet" / f"slide-{args.slide:03}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    gslides.save_thumbnail(slides, pid, slide["objectId"], path)
    print(f"thumbnail: {path}")


if __name__ == "__main__":
    main()
