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
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from beamer2slides.devtools.adopt_bench import MANIFEST, capture
from beamer2slides.google_types import as_json
from beamer2slides.json_types import Json, JsonObject, as_array, as_int, as_object, as_objects, as_str


@dataclass(frozen=True, kw_only=True)
class Style:
    """A run's style: bold, italic, a weight of the paragraph's font (None: as the text has it)."""

    bold: bool | None
    italic: bool | None
    weight: int | None

    def said(self) -> bool:
        return self.bold is not None or self.italic is not None or self.weight is not None


PLAIN = Style(bold=None, italic=None, weight=None)
BOLD = Style(bold=True, italic=None, weight=None)
ITALIC = Style(bold=None, italic=True, weight=None)


def weight(w: int) -> Style:
    return Style(bold=None, italic=None, weight=w)


# a paragraph is a string or a list of (text, style) runs
Paragraph = str | list[tuple[str, Style]]
Placeholder = tuple[str, int]    # (placeholder type, index)


@dataclass(frozen=True, kw_only=True)
class PlainSlide:
    """A slide: its predefined layout, and the paragraphs typed into its placeholders."""

    layout: str
    fill: dict[Placeholder, list[Paragraph]]


LAYOUTS = [
    PlainSlide(layout="TITLE", fill={("CENTERED_TITLE", 0): ["Hello"], ("SUBTITLE", 0): ["A simple deck"]}),
    PlainSlide(layout="SECTION_HEADER", fill={("TITLE", 0): ["Part one"]}),
    PlainSlide(layout="TITLE_AND_BODY", fill={("TITLE", 0): ["Agenda"],
                                              ("BODY", 0): ["Why we measure what people see",
                                                            "What a deck made in Slides looks like when nobody "
                                                            "designed it, with a line long enough to wrap",
                                                            "Questions"]}),
    PlainSlide(layout="TITLE_AND_TWO_COLUMNS", fill={("TITLE", 0): ["Before and after"],
                                                     ("BODY", 0): ["Left column", "Two items"],
                                                     ("BODY", 1): ["Right column", "Three", "Items"]}),
    PlainSlide(layout="TITLE_ONLY", fill={("TITLE", 0): ["Just a title"]}),
    PlainSlide(layout="ONE_COLUMN_TEXT",
               fill={("TITLE", 0): ["One column"],
                     ("BODY", 0): ["A paragraph of prose in the body of a one-column layout, long enough "
                                   "to wrap over two or three lines of the box it sits in."]}),
    PlainSlide(layout="MAIN_POINT", fill={("TITLE", 0): ["The main point"]}),
    PlainSlide(layout="SECTION_TITLE_AND_DESCRIPTION",
               fill={("TITLE", 0): ["A section"], ("SUBTITLE", 0): ["and its subtitle"],
                     ("BODY", 0): ["A description of the section, in the body box."]}),
    PlainSlide(layout="CAPTION_ONLY", fill={("BODY", 0): ["A caption at the bottom of an empty slide"]}),
    PlainSlide(layout="BIG_NUMBER", fill={("TITLE", 0): ["42%"], ("BODY", 0): ["of the decks people make start here"]}),
]


def styled(font: str) -> list[PlainSlide]:
    """The layouts again, every run in `font`, with weights, italics, quotes and right-to-left lines."""
    body: list[Paragraph] = [
        "Plain words at the family's regular weight",
        [("A ", PLAIN), ("bold", BOLD), (" word and an ", PLAIN), ("italic", ITALIC), (" one", PLAIN)],
        [("Medium weight 500 all along", weight(500))],
        [("Light weight 300 all along", weight(300))],
        "It's the cats' \"quotes\", straight as typed"]
    return [
        PlainSlide(layout="TITLE", fill={("CENTERED_TITLE", 0): [[(font, BOLD)]], ("SUBTITLE", 0): ["in its own family"]}),
        PlainSlide(layout="TITLE_AND_BODY", fill={("TITLE", 0): [[("Weights of " + font, weight(600))]],
                                                  ("BODY", 0): body}),
        PlainSlide(layout="SECTION_HEADER", fill={("TITLE", 0): [[("A section in ", PLAIN), (font, BOLD)]]}),
        PlainSlide(layout="TITLE_AND_BODY", fill={("TITLE", 0): ["Right to left"],
                                                  ("BODY", 0): ["Thanks: شكراً لكم", "Hebrew: תודה רבה",
                                                                "Latin words again"]}),
        PlainSlide(layout="BIG_NUMBER", fill={("TITLE", 0): [[("7", BOLD)]], ("BODY", 0): ["slides in this family"]}),
    ]


FONTS = ("Montserrat", "Raleway", "Inter", "Open Sans")


def fonts_deck() -> list[PlainSlide]:
    return [s for font in FONTS for s in styled(font)]


def layouts_deck() -> list[PlainSlide]:
    return LAYOUTS


@dataclass(frozen=True, kw_only=True)
class DeckSpec:
    title: str
    slides: Callable[[], list[PlainSlide]]
    table: bool      # a last TITLE_ONLY slide holding a 3 by 3 table


DECKS = {"plain-layouts": DeckSpec(title="b2s plain deck: layouts", slides=layouts_deck, table=False),
         "plain-fonts": DeckSpec(title="b2s plain deck: Google Fonts", slides=fonts_deck, table=True)}


def paragraphs_requests(oid: str, paragraphs: Sequence[Paragraph], font: str | None) -> list[JsonObject]:
    text = ""
    runs: list[tuple[int, int, Style]] = []
    for k, para in enumerate(paragraphs):
        for piece, style in ([(para, PLAIN)] if isinstance(para, str) else para):
            runs.append((len(text), len(text) + len(piece), style))
            text += piece
        if k < len(paragraphs) - 1:
            text += "\n"
    reqs: list[JsonObject] = [{"insertText": {"objectId": oid, "text": text}}]
    if font:
        reqs.append({"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                         "style": {"fontFamily": font}, "fields": "fontFamily"}})
    for a, b, style in runs:
        if not style.said() or a == b:
            continue
        s: JsonObject = {}
        fields: list[str] = []
        if style.weight is not None:
            s["weightedFontFamily"] = {"fontFamily": font or "Arial", "weight": style.weight}
            fields.append("weightedFontFamily")
        if style.bold is not None:
            s["bold"] = style.bold
            fields.append("bold")
        if style.italic is not None:
            s["italic"] = style.italic
            fields.append("italic")
        # UTF-16 indices: every text here is in the BMP, so Python's are the same
        reqs.append({"updateTextStyle": {"objectId": oid, "textRange": {"type": "FIXED_RANGE", "startIndex": a,
                                                                        "endIndex": b},
                                         "style": s, "fields": ",".join(fields)}})
    return reqs


def _placeholder(element: JsonObject) -> JsonObject:
    """A page element's placeholder, as `presentations.get` answers it ({} for none)."""
    return as_object(as_object(element.get("shape", {}), "a shape").get("placeholder", {}), "a placeholder")


def make(name: str) -> str:
    from beamer2slides.google_auth import slides_service
    from beamer2slides.gslides import execute
    spec = DECKS[name]
    slides = spec.slides()
    s = slides_service()
    made = execute(s.presentations().create(body={"title": spec.title}))
    first = made.get("slides", [])
    pid, blank = made.get("presentationId"), first[0].get("objectId") if first else None
    if pid is None or blank is None:
        raise ValueError(f"presentations.create answered no deck id or no first slide: {sorted(made)}")
    reqs: list[JsonObject] = [{"deleteObject": {"objectId": blank}}]
    for k, slide in enumerate(slides):
        reqs.append({"createSlide": {"objectId": f"b2s_plain_{k:02d}", "insertionIndex": k,
                                     "slideLayoutReference": {"predefinedLayout": slide.layout}}})
    if spec.table:
        reqs.append({"createSlide": {"objectId": "b2s_plain_table", "insertionIndex": len(slides),
                                     "slideLayoutReference": {"predefinedLayout": "TITLE_ONLY"}}})
        reqs.append({"createTable": {"objectId": "b2s_plain_tbl", "rows": 3, "columns": 3,
                                     "elementProperties": {"pageObjectId": "b2s_plain_table"}}})
    execute(s.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    pres = as_json(execute(s.presentations().get(presentationId=pid)), pid)
    reqs = []
    font_of: dict[str, str] = {}
    if name == "plain-fonts":
        order = [f for f in FONTS for _ in range(5)]
        font_of = {f"b2s_plain_{k:02d}": f for k, f in enumerate(order)}
    elements = {as_str(p.get("objectId"), "a slide's id"):
                as_objects(p.get("pageElements", []), "a slide's elements")
                for p in as_objects(pres.get("slides", []), f"{name}'s slides")}
    for k, slide in enumerate(slides):
        page = f"b2s_plain_{k:02d}"
        held: dict[tuple[str, int], str] = {}
        for pe in elements[page]:
            ph = _placeholder(pe)
            if ph:
                held[(as_str(ph.get("type"), "a placeholder's type"),
                      as_int(ph.get("index", 0), "a placeholder's index"))] = as_str(pe.get("objectId"), "an id")
        for key, paragraphs in slide.fill.items():
            if key not in held:
                raise KeyError(f"{name} slide {k} ({slide.layout}) has no {key} placeholder: {sorted(held)}")
            reqs += paragraphs_requests(held[key], paragraphs, font_of.get(page))
    if spec.table:
        title = next(as_str(pe.get("objectId"), "an id") for pe in elements["b2s_plain_table"]
                     if _placeholder(pe).get("type") == "TITLE")
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


def main(argv: list[str] | None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("names", nargs="*")
    args = ap.parse_args(argv)
    names: list[str] = args.names
    manifest = as_array(json.loads(MANIFEST.read_text(encoding="utf-8")), str(MANIFEST))
    for name in names or list(DECKS):
        pid = make(name)
        capture(pid, name)
        manifest = [d for d in manifest if as_object(d, str(MANIFEST))["name"] != name] + [
            {"name": name, "id": pid, "title": DECKS[name].title, "features": list[Json](["16:9", "plain", "made"])}]
        print(f"{name}: https://docs.google.com/presentation/d/{pid}/edit", flush=True)
    MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main(None))
