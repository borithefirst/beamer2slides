"""Plain decks, as a person makes them in Slides: File > New, a layout per slide, words typed in.

The adopt corpus (`adopt_bench`) was 29 finished public decks, and none of them was the simplest
deck there is: the default theme's title slide with one word on it. Its title is bottom-aligned,
which the loop had never measured, and the first three decks people brought from inside Google
(2026-09) failed to converge on that and on the weights of variable Google Fonts, which no deck in
the corpus happened to set. These decks are made through the API from the definitions below, so
anyone with the project's credentials can make them again, and captured into the corpus:

  plain-layouts   the default theme (Simple Light), one slide per predefined layout, Arial
  plain-fonts     the same layouts set in variable Google Fonts whose default instance is not their
                  Regular (Montserrat, Raleway: Thin), weights 300/500/700, italic, straight quotes,
                  Arabic and Hebrew lines in a Latin family, a table

Usage: python tools/plain_decks.py make [NAME...]    creates the decks, captures them, prints the ids
"""

import argparse
import json
import sys

from beamer2slides.devtools.adopt_bench import MANIFEST, capture

# a slide: (predefined layout, {(placeholder type, index): paragraphs}); a paragraph is a string or
# a list of (text, style) runs, style keys: bold, italic, weight, font
LAYOUTS = [
    ("TITLE", {("CENTERED_TITLE", 0): ["Hello"], ("SUBTITLE", 0): ["A simple deck"]}),
    ("SECTION_HEADER", {("TITLE", 0): ["Part one"]}),
    ("TITLE_AND_BODY", {("TITLE", 0): ["Agenda"],
                        ("BODY", 0): ["Why we measure what people see",
                                      "What a deck made in Slides looks like when nobody designed it, "
                                      "with a line long enough to wrap",
                                      "Questions"]}),
    ("TITLE_AND_TWO_COLUMNS", {("TITLE", 0): ["Before and after"],
                               ("BODY", 0): ["Left column", "Two items"],
                               ("BODY", 1): ["Right column", "Three", "Items"]}),
    ("TITLE_ONLY", {("TITLE", 0): ["Just a title"]}),
    ("ONE_COLUMN_TEXT", {("TITLE", 0): ["One column"],
                         ("BODY", 0): ["A paragraph of prose in the body of a one-column layout, long enough "
                                       "to wrap over two or three lines of the box it sits in."]}),
    ("MAIN_POINT", {("TITLE", 0): ["The main point"]}),
    ("SECTION_TITLE_AND_DESCRIPTION", {("TITLE", 0): ["A section"], ("SUBTITLE", 0): ["and its subtitle"],
                                       ("BODY", 0): ["A description of the section, in the body box."]}),
    ("CAPTION_ONLY", {("BODY", 0): ["A caption at the bottom of an empty slide"]}),
    ("BIG_NUMBER", {("TITLE", 0): ["42%"], ("BODY", 0): ["of the decks people make start here"]}),
]


def styled(font: str) -> list:
    """The layouts again, every run in `font`, with weights, italics, quotes and right-to-left lines."""
    body = ["Plain words at the family's regular weight",
            [("A ", {}), ("bold", {"bold": True}), (" word and an ", {}), ("italic", {"italic": True}), (" one", {})],
            [("Medium weight 500 all along", {"weight": 500})],
            [("Light weight 300 all along", {"weight": 300})],
            "It's the cats' \"quotes\", straight as typed"]
    return [
        ("TITLE", {("CENTERED_TITLE", 0): [[(font, {"bold": True})]], ("SUBTITLE", 0): ["in its own family"]}),
        ("TITLE_AND_BODY", {("TITLE", 0): [[("Weights of " + font, {"weight": 600})]], ("BODY", 0): body}),
        ("SECTION_HEADER", {("TITLE", 0): [[("A section in ", {}), (font, {"bold": True})]]}),
        ("TITLE_AND_BODY", {("TITLE", 0): ["Right to left"],
                            ("BODY", 0): ["Thanks: شكراً لكم", "Hebrew: תודה רבה", "Latin words again"]}),
        ("BIG_NUMBER", {("TITLE", 0): [[("7", {"bold": True})]], ("BODY", 0): ["slides in this family"]}),
    ]


def fonts_deck() -> list:
    return [s for font in ("Montserrat", "Raleway", "Inter", "Open Sans") for s in styled(font)]


DECKS = {"plain-layouts": ("b2s plain deck: layouts", lambda: LAYOUTS, None),
         "plain-fonts": ("b2s plain deck: Google Fonts", fonts_deck, "table")}


def paragraphs_requests(oid: str, paragraphs: list, font: str | None) -> list:
    text, runs = "", []
    for k, para in enumerate(paragraphs):
        for piece, style in ([(para, {})] if isinstance(para, str) else para):
            runs.append((len(text), len(text) + len(piece), style))
            text += piece
        if k < len(paragraphs) - 1:
            text += "\n"
    reqs = [{"insertText": {"objectId": oid, "text": text}}]
    if font:
        reqs.append({"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                         "style": {"fontFamily": font}, "fields": "fontFamily"}})
    for a, b, style in runs:
        if not style or a == b:
            continue
        s, fields = {}, []
        if "weight" in style:
            s["weightedFontFamily"] = {"fontFamily": font or "Arial", "weight": style["weight"]}
            fields.append("weightedFontFamily")
        for key in ("bold", "italic"):
            if key in style:
                s[key] = style[key]
                fields.append(key)
        # UTF-16 indices: every text here is in the BMP, so Python's are the same
        reqs.append({"updateTextStyle": {"objectId": oid, "textRange": {"type": "FIXED_RANGE", "startIndex": a,
                                                                        "endIndex": b},
                                         "style": s, "fields": ",".join(fields)}})
    return reqs


def make(name: str) -> str:
    from beamer2slides.google_auth import slides_service
    from beamer2slides.gslides import execute
    title, spec, extra = DECKS[name]
    slides = spec()
    s = slides_service()
    pres = execute(s.presentations().create(body={"title": title}))
    pid = pres["presentationId"]
    reqs = [{"deleteObject": {"objectId": pres["slides"][0]["objectId"]}}]
    for k, (layout, _) in enumerate(slides):
        reqs.append({"createSlide": {"objectId": f"b2s_plain_{k:02d}", "insertionIndex": k,
                                     "slideLayoutReference": {"predefinedLayout": layout}}})
    if extra == "table":
        reqs.append({"createSlide": {"objectId": "b2s_plain_table", "insertionIndex": len(slides),
                                     "slideLayoutReference": {"predefinedLayout": "TITLE_ONLY"}}})
        reqs.append({"createTable": {"objectId": "b2s_plain_tbl", "rows": 3, "columns": 3,
                                     "elementProperties": {"pageObjectId": "b2s_plain_table"}}})
    execute(s.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    pres = execute(s.presentations().get(presentationId=pid))
    reqs = []
    font_of = {}
    if name == "plain-fonts":
        order = [f for f in ("Montserrat", "Raleway", "Inter", "Open Sans") for _ in range(5)]
        font_of = {f"b2s_plain_{k:02d}": f for k, f in enumerate(order)}
    for k, (layout, fill) in enumerate(slides):
        page = next(p for p in pres["slides"] if p["objectId"] == f"b2s_plain_{k:02d}")
        held = {}
        for pe in page.get("pageElements", []):
            ph = pe.get("shape", {}).get("placeholder")
            if ph:
                held[(ph["type"], ph.get("index", 0))] = pe["objectId"]
        for key, paragraphs in fill.items():
            if key not in held:
                raise KeyError(f"{name} slide {k} ({layout}) has no {key} placeholder: {sorted(held)}")
            reqs += paragraphs_requests(held[key], paragraphs, font_of.get(page["objectId"]))
    if extra == "table":
        page = next(p for p in pres["slides"] if p["objectId"] == "b2s_plain_table")
        title = next(pe["objectId"] for pe in page["pageElements"]
                     if pe.get("shape", {}).get("placeholder", {}).get("type") == "TITLE")
        reqs += paragraphs_requests(title, ["A table"], "Montserrat")
        cells = [["Cat", "Region", "Weight"], ["Arabian Mau", "Riyadh", "4 kg"],
                 ["Street cat", "Jeddah, on the corniche", "3.5 kg"]]
        for r, row in enumerate(cells):
            for c, text in enumerate(row):
                reqs.append({"insertText": {"objectId": "b2s_plain_tbl", "cellLocation": {"rowIndex": r, "columnIndex": c},
                                            "text": text}})
                reqs.append({"updateTextStyle": {"objectId": "b2s_plain_tbl",
                                                 "cellLocation": {"rowIndex": r, "columnIndex": c},
                                                 "textRange": {"type": "ALL"},
                                                 "style": {"fontFamily": "Montserrat", "bold": r == 0},
                                                 "fields": "fontFamily,bold"}})
    execute(s.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    return pid


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("names", nargs="*")
    args = ap.parse_args(argv)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for name in args.names or list(DECKS):
        pid = make(name)
        capture(pid, name)
        manifest = [d for d in manifest if d["name"] != name] + [
            {"name": name, "id": pid, "title": DECKS[name][0], "features": ["16:9", "plain", "made"]}]
        print(f"{name}: https://docs.google.com/presentation/d/{pid}/edit", flush=True)
    MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
