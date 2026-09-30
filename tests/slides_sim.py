"""A tiny Google Slides model: replays the requests emit plans (emit.plan_offline) into the JSON
`presentations.get` returns, as far as deck_ir reads it (text boxes and placeholders with their
runs, paragraph styles and bullets, pictures, groups, speaker notes). No Google involved."""

import copy
from dataclasses import dataclass

from beamer2slides.emit import SLIDE_W, plan_offline
from beamer2slides.emit_model import ObjectMap
from beamer2slides.json_types import Json, JsonObject

from .json_reads import jint, jnum, jnums, jobj, jobjs, jstr, jstrs

GLYPHS = {"BULLET_DISC_CIRCLE_SQUARE": "●", "BULLET_ARROW3D_CIRCLE_SQUARE": "➢", "BULLET_CHECKBOX": "❏",
          "BULLET_STAR_CIRCLE_SQUARE": "★", "BULLET_DIAMOND_CIRCLE_SQUARE": "◆",
          "BULLET_DIAMONDX_HOLLOWDIAMOND_SQUARE": "❖", "NUMBERED_DIGIT_ALPHA_ROMAN": "1.",
          "NUMBERED_DIGIT_ALPHA_ROMAN_PARENS": "1)"}


def units(text: str) -> list[str]:
    """A text's UTF-16 code units, one string each, as Slides indexes text (an astral 𝔼 is two)."""
    data = text.encode("utf-16-le", "surrogatepass")
    return [chr(int.from_bytes(data[i:i + 2], "little")) for i in range(0, len(data), 2)]


def joined(chars: list[str]) -> str:
    """Code units back into text; a surrogate pair split by a style range stays two halves."""
    return "".join(chars).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


@dataclass(kw_only=True)
class Marker:
    """A paragraph's marker: its paragraph style and its bullet (None: not a list item). Requests
    edit both in place."""
    style: JsonObject
    bullet: JsonObject | None


def no_marker() -> Marker:
    return Marker(style={}, bullet=None)


class Text:
    """Characters (UTF-16 code units: `units`) with their styles; a paragraph's marker sits on its
    closing newline (the last paragraph's in `end`)."""

    def __init__(self) -> None:
        self.chars: list[str] = []
        self.styles: list[JsonObject] = []
        self.marks: list[Marker | None] = []
        self.end = no_marker()

    def paragraphs(self) -> list[tuple[int, int, Marker]]:
        out: list[tuple[int, int, Marker]] = []
        start = 0
        for i, c in enumerate(self.chars):
            if c == "\n":
                mark = self.marks[i]
                assert mark is not None, "a newline without its paragraph's marker"
                out.append((start, i, mark))
                start = i + 1
        out.append((start, len(self.chars), self.end))
        return out

    def insert(self, at: int, text: str) -> None:
        style: JsonObject = dict(self.styles[at - 1]) if at > 0 and self.styles else {}
        chars = units(text)
        self.chars[at:at] = chars
        self.styles[at:at] = [dict(style) for _ in chars]
        self.marks[at:at] = [no_marker() if c == "\n" else None for c in chars]

    def remove(self, a: int, b: int) -> None:
        self.chars[a:b] = []
        self.styles[a:b] = []
        self.marks[a:b] = []

    def ranges(self, rng: JsonObject) -> tuple[int, int]:
        if rng.get("type") == "ALL":
            return 0, len(self.chars)
        return (jint(rng, "startIndex") if "startIndex" in rng else 0,
                jint(rng, "endIndex") if "endIndex" in rng else len(self.chars))

    def json(self) -> JsonObject:
        out: list[Json] = []
        for a, b, marker in self.paragraphs():
            paragraph_marker: JsonObject = {"style": copy.deepcopy(marker.style)}
            if marker.bullet:
                paragraph_marker["bullet"] = copy.deepcopy(marker.bullet)
            out.append({"paragraphMarker": paragraph_marker})
            k = a
            while k < b:
                e = k
                while e < b and self.styles[e] == self.styles[k]:
                    e += 1
                out.append({"textRun": {"content": joined(self.chars[k:e]), "style": copy.deepcopy(self.styles[k])}})
                k = e
            last: JsonObject = copy.deepcopy(self.styles[b - 1]) if b > a else {}
            out.append({"textRun": {"content": "\n", "style": last}})
        return {"textElements": out}


def presentation_of(deck: ObjectMap) -> JsonObject:
    """presentations.get-shaped JSON of the deck emit would build (offline plan, no measured moves)."""
    plan = plan_offline(deck)
    deck_plan = plan["plan"]
    scale = deck_plan.scale
    page_h = jnums(deck_plan.slides()[0], "size")[1]
    slides: list[Json] = []
    objects: dict[str, JsonObject] = {}
    texts: dict[str, Text] = {}  # the text of each object that holds one, by objectId
    placeholder_type: dict[str, str] = {}
    for req in plan["copies"]:
        for src, new in jobj(req, "duplicateObject", "objectIds").items():
            for kind in ("CENTERED_TITLE", "SUBTITLE", "TITLE"):
                if src.endswith("_" + kind):
                    placeholder_type[jstr(new)] = kind
    for slide_id, page, parts, _element_ids in plan["slides"]:
        elements: list[Json] = []
        for pe in plan["page_elements"].get(slide_id, []):
            oid = jstr(pe, "objectId")
            if oid in placeholder_type:
                obj: JsonObject = {"objectId": oid, "size": copy.deepcopy(pe["size"]),
                                   "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU"},
                                   "shape": {"shapeType": "TEXT_BOX", "placeholder": {"type": placeholder_type[oid]}}}
                objects[oid] = obj
                texts[oid] = Text()
                elements.append(obj)
        slide_deck = next(s for s in deck_plan.slides() if s["page"] == page)
        pictures = plan["pictures"][page]
        images = [i for i, e in enumerate(jobjs(slide_deck, "elements")) if e["kind"] == "image"]
        for i, (_el, box) in zip(images, pictures):
            oid = f"{slide_id}_f{i}"
            picture: JsonObject = {
                "objectId": oid, "size": {"width": {"magnitude": (box[2] - box[0]) * 12700, "unit": "EMU"},
                                          "height": {"magnitude": (box[3] - box[1]) * 12700, "unit": "EMU"}},
                "transform": {"scaleX": 1, "scaleY": 1, "translateX": box[0] * 12700, "translateY": box[1] * 12700,
                              "unit": "EMU"},
                "title": "Figure", "image": {"contentUrl": None}}
            objects[oid] = picture
            elements.append(picture)
        notes = Text()
        for _el, reqs in parts:
            for r in reqs:
                apply(r, elements, objects, texts, notes, slide_id)
        for oid, text in texts.items():
            jobj(objects[oid], "shape")["text"] = text.json()
        page_elements: list[Json] = [strip(jobj(e)) for e in elements]
        slides.append({"objectId": slide_id, "pageElements": page_elements,
                       "slideProperties": {"notesPage": {"notesProperties": {"speakerNotesObjectId": f"{slide_id}_notes"},
                                                         "pageElements": [{"objectId": f"{slide_id}_notes", "shape": {
                                                             "text": notes.json()}}]}}})
    return {"presentationId": "simulated", "pageSize": {"width": {"magnitude": SLIDE_W * 12700, "unit": "EMU"},
                                                        "height": {"magnitude": page_h * scale * 12700, "unit": "EMU"}},
            "slides": slides, "layouts": [], "masters": []}


def simulate(deck: ObjectMap) -> JsonObject:
    """`presentation_of`, untyped, for the modules that read it (ir_sources, test_inverse,
    test_fonts_weights): they index the presentation and hand it on as a `Presentation`, which a
    JSON object would make errors of there."""
    return presentation_of(deck)


def strip(e: JsonObject) -> JsonObject:
    """A page element as presentations.get gives it: a copy, its group's children copied too."""
    out: JsonObject = {k: v for k, v in e.items() if not k.startswith("_")}
    if "elementGroup" in out:
        children: list[Json] = [strip(c) for c in jobjs(out, "elementGroup", "children")]
        out["elementGroup"] = {"children": children}
    return out


def find(elements: list[Json], oid: str) -> tuple[list[Json], int] | None:
    for i, item in enumerate(elements):
        e = jobj(item)
        if e["objectId"] == oid:
            return elements, i
        if "elementGroup" in e:
            children = jobj(e, "elementGroup")["children"]
            assert isinstance(children, list)
            got = find(children, oid)
            if got:
                return got
    return None


def apply(r: JsonObject, elements: list[Json], objects: dict[str, JsonObject], texts: dict[str, Text], notes: Text,
          slide_id: str) -> None:
    (name, request), = r.items()
    body = jobj(request)
    if name == "createShape":
        props = jobj(body, "elementProperties")
        oid = jstr(body, "objectId")
        obj: JsonObject = {"objectId": oid, "size": copy.deepcopy(props["size"]),
                           "transform": copy.deepcopy(props["transform"]),
                           "shape": {"shapeType": body["shapeType"], "shapeProperties": {}}}
        objects[oid] = obj
        texts[oid] = Text()
        elements.append(obj)
    elif name == "updatePageElementTransform":
        moved = objects.get(jstr(body, "objectId"))
        if moved is None:
            return
        t = jobj(body, "transform")
        if body["applyMode"] == "ABSOLUTE":
            moved["transform"] = copy.deepcopy(t)
        else:
            mine = jobj(moved, "transform")
            mine["translateX"] = jnum(mine.get("translateX", 0)) + jnum(t.get("translateX", 0))
            mine["translateY"] = jnum(mine.get("translateY", 0)) + jnum(t.get("translateY", 0))
    elif name == "updateShapeProperties":
        shaped = objects.get(jstr(body, "objectId"))
        if shaped is not None:
            none_yet: JsonObject = {}
            jobj(jobj(shaped, "shape").setdefault("shapeProperties", none_yet)).update(
                copy.deepcopy(jobj(body, "shapeProperties")))
    elif name in ("insertText", "deleteText", "updateTextStyle", "createParagraphBullets", "updateParagraphStyle"):
        oid = jstr(body, "objectId")
        text = notes if oid == f"{slide_id}_notes" else texts.get(oid)
        if text is None:
            return
        if name == "insertText":
            i = jint(body, "insertionIndex") if "insertionIndex" in body else 0
            if i > len(text.chars):
                raise ValueError(f"Invalid insertText: The insertion index ({i}) should not be greater "
                                 f"than the existing text length ({len(text.chars)}).")
            text.insert(i, jstr(body, "text"))
        elif name == "deleteText":
            a, b = text.ranges(jobj(body, "textRange"))
            if b > len(text.chars):
                # Slides counts the text without the newline it ends on and refuses to delete it
                # (`merge.text_edit_requests`); `chars` is exactly that length, so clipping the
                # slice here would hide a batch the API throws out whole.
                raise ValueError(f"Invalid deleteText: The end index ({b}) should not be greater "
                                 f"than the existing text length ({len(text.chars)}).")
            text.remove(a, b)
        elif name == "updateTextStyle":
            a, b = text.ranges(jobj(body, "textRange"))
            if b > len(text.chars):
                # A FIXED_RANGE is measured against the same length a delete is (see below), so
                # clipping here would let through a request the API throws the batch out for.
                # `sync.style_range_requests` keeps under it by construction - a run's trailing
                # newlines are taken off its range, and the text it styles ends on one - and this
                # is what says so if that ever stops being true.
                raise ValueError(f"Invalid updateTextStyle: The end index ({b}) should not be greater "
                                 f"than the existing text length ({len(text.chars)}).")
            fields = jstr(body, "fields").split(",")
            style = jobj(body, "style")
            for k in range(a, min(b, len(text.chars))):
                for f in fields:
                    if f in style:
                        text.styles[k][f] = copy.deepcopy(style[f])
                    if f == "weightedFontFamily" and "bold" not in fields:
                        # a weight reads back as bold from 700 up (tools/probe_font_weights.py)
                        weight = jobj(style, f).get("weight")
                        text.styles[k]["bold"] = (jnum(weight) if weight else 400) >= 700
        elif name == "updateParagraphStyle":
            a, b = text.ranges(jobj(body, "textRange"))
            style = jobj(body, "style")
            for pa, pb, marker in text.paragraphs():
                if pa <= max(a, b - 1) and pb >= a:
                    for f in jstr(body, "fields").split(","):
                        if f in style:
                            marker.style[f] = copy.deepcopy(style[f])
        elif name == "createParagraphBullets":
            a, b = text.ranges(jobj(body, "textRange"))
            paras = [(pa, pb, m) for pa, pb, m in text.paragraphs() if pa <= max(a, b - 1) and pb >= a]
            for pa, _pb, marker in reversed(paras):
                tabs = 0
                while pa + tabs < len(text.chars) and text.chars[pa + tabs] == "\t":
                    tabs += 1
                marker.bullet = {"listId": "l", "nestingLevel": tabs,
                                 "glyph": GLYPHS.get(jstr(body, "bulletPreset"), "●")}
                if tabs:
                    text.remove(pa, pa + tabs)
    elif name == "groupObjects":
        children: list[Json] = []
        for cid in jstrs(body, "childrenObjectIds"):
            got = find(elements, cid)
            if got:
                lst, i = got
                children.append(lst.pop(i))
        gid = jstr(body, "groupObjectId")
        group: JsonObject = {"objectId": gid, "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU"},
                             "elementGroup": {"children": children}}
        objects[gid] = group
        elements.append(group)
    elif name == "deleteObject":
        got = find(elements, jstr(body, "objectId"))
        if got:
            lst, i = got
            lst.pop(i)
