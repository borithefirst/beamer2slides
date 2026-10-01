"""A tiny Google Slides model: replays the requests emit plans (emit.plan_offline) into the JSON
`presentations.get` returns, as far as deck_ir reads it (text boxes and placeholders with their
runs, paragraph styles and bullets, pictures, groups, speaker notes). No Google involved."""

import copy
from dataclasses import dataclass

from beamer2slides.emit import SLIDE_W, plan_offline
from beamer2slides.emit_model import ObjectMap
from beamer2slides.google_types import SlidesRange, SlidesRequest, part_json, slides_request_kind
from beamer2slides.json_types import Json, JsonObject

from .json_reads import jnum, jnums, jobj, jobjs, jstr

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

    def ranges(self, rng: SlidesRange | None) -> tuple[int, int]:
        assert rng is not None, "a text request with no textRange"
        if rng.get("type") == "ALL":
            return 0, len(self.chars)
        return rng.get("startIndex", 0), rng.get("endIndex", len(self.chars))

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
        dup = req.get("duplicateObject")
        ids = None if dup is None else dup.get("objectIds")
        assert ids is not None, f"a copy that is no duplicateObject with objectIds: {req}"
        for src, new in ids.items():
            for kind in ("CENTERED_TITLE", "SUBTITLE", "TITLE"):
                if src.endswith("_" + kind):
                    placeholder_type[new] = kind
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


def text_of(oid: str, texts: dict[str, Text], notes: Text, slide_id: str) -> Text | None:
    """The text a text request names: the slide's speaker notes, a shape's, or None (not modelled)."""
    return notes if oid == f"{slide_id}_notes" else texts.get(oid)


def apply(r: SlidesRequest, elements: list[Json], objects: dict[str, JsonObject], texts: dict[str, Text], notes: Text,
          slide_id: str) -> None:
    slides_request_kind(r)   # (exactly one request in each: Google refuses anything else)
    if (made := r.get("createShape")) is not None:
        props = made.get("elementProperties")
        size = None if props is None else props.get("size")
        transform = None if props is None else props.get("transform")
        oid = made.get("objectId")
        assert oid is not None and size is not None and transform is not None, f"a shape made with no id or place: {r}"
        obj: JsonObject = {"objectId": oid, "size": part_json(copy.deepcopy(size), "createShape.size"),
                           "transform": part_json(copy.deepcopy(transform), "createShape.transform"),
                           "shape": {"shapeType": made["shapeType"], "shapeProperties": {}}}
        objects[oid] = obj
        texts[oid] = Text()
        elements.append(obj)
    elif (move := r.get("updatePageElementTransform")) is not None:
        moved = objects.get(move["objectId"])
        if moved is None:
            return
        t = move["transform"]
        if move["applyMode"] == "ABSOLUTE":
            moved["transform"] = part_json(copy.deepcopy(t), "updatePageElementTransform.transform")
        else:
            mine = jobj(moved, "transform")
            mine["translateX"] = jnum(mine.get("translateX", 0)) + t.get("translateX", 0)
            mine["translateY"] = jnum(mine.get("translateY", 0)) + t.get("translateY", 0)
    elif (shaped_by := r.get("updateShapeProperties")) is not None:
        shaped = objects.get(shaped_by["objectId"])
        if shaped is not None:
            none_yet: JsonObject = {}
            jobj(jobj(shaped, "shape").setdefault("shapeProperties", none_yet)).update(
                part_json(copy.deepcopy(shaped_by["shapeProperties"]), "updateShapeProperties.shapeProperties"))
    elif (insert := r.get("insertText")) is not None:
        text = text_of(insert["objectId"], texts, notes, slide_id)
        if text is None:
            return
        i = insert.get("insertionIndex", 0)
        if i > len(text.chars):
            raise ValueError(f"Invalid insertText: The insertion index ({i}) should not be greater "
                             f"than the existing text length ({len(text.chars)}).")
        text.insert(i, insert["text"])
    elif (delete := r.get("deleteText")) is not None:
        text = text_of(delete["objectId"], texts, notes, slide_id)
        if text is None:
            return
        a, b = text.ranges(delete.get("textRange"))
        if b > len(text.chars):
            # Slides counts the text without the newline it ends on and refuses to delete it
            # (`merge.text_edit_requests`); `chars` is exactly that length, so clipping the
            # slice here would hide a batch the API throws out whole.
            raise ValueError(f"Invalid deleteText: The end index ({b}) should not be greater "
                             f"than the existing text length ({len(text.chars)}).")
        text.remove(a, b)
    elif (styled := r.get("updateTextStyle")) is not None:
        text = text_of(styled["objectId"], texts, notes, slide_id)
        if text is None:
            return
        a, b = text.ranges(styled.get("textRange"))
        if b > len(text.chars):
            # A FIXED_RANGE is measured against the same length a delete is (see below), so
            # clipping here would let through a request the API throws the batch out for.
            # `sync.style_range_requests` keeps under it by construction - a run's trailing
            # newlines are taken off its range, and the text it styles ends on one - and this
            # is what says so if that ever stops being true.
            raise ValueError(f"Invalid updateTextStyle: The end index ({b}) should not be greater "
                             f"than the existing text length ({len(text.chars)}).")
        fields = styled["fields"].split(",")
        style = part_json(styled["style"], "updateTextStyle.style")
        for k in range(a, min(b, len(text.chars))):
            for f in fields:
                if f in style:
                    text.styles[k][f] = copy.deepcopy(style[f])
                if f == "weightedFontFamily" and "bold" not in fields:
                    # a weight reads back as bold from 700 up (tools/probe_font_weights.py)
                    family = styled["style"].get("weightedFontFamily")
                    assert family is not None, f"a weight asked for and not given: {r}"
                    weight = family.get("weight")
                    text.styles[k]["bold"] = (weight if weight else 400) >= 700
    elif (paragraph := r.get("updateParagraphStyle")) is not None:
        text = text_of(paragraph["objectId"], texts, notes, slide_id)
        if text is None:
            return
        a, b = text.ranges(paragraph.get("textRange"))
        style = part_json(paragraph["style"], "updateParagraphStyle.style")
        for pa, pb, marker in text.paragraphs():
            if pa <= max(a, b - 1) and pb >= a:
                for f in paragraph["fields"].split(","):
                    if f in style:
                        marker.style[f] = copy.deepcopy(style[f])
    elif (bullets := r.get("createParagraphBullets")) is not None:
        text = text_of(bullets["objectId"], texts, notes, slide_id)
        if text is None:
            return
        a, b = text.ranges(bullets.get("textRange"))
        preset = bullets.get("bulletPreset")
        assert preset is not None, f"bullets with no preset: {r}"
        paras = [(pa, pb, m) for pa, pb, m in text.paragraphs() if pa <= max(a, b - 1) and pb >= a]
        for pa, _pb, marker in reversed(paras):
            tabs = 0
            while pa + tabs < len(text.chars) and text.chars[pa + tabs] == "\t":
                tabs += 1
            marker.bullet = {"listId": "l", "nestingLevel": tabs, "glyph": GLYPHS.get(preset, "●")}
            if tabs:
                text.remove(pa, pa + tabs)
    elif (grouping := r.get("groupObjects")) is not None:
        children: list[Json] = []
        for cid in grouping["childrenObjectIds"]:
            got = find(elements, cid)
            if got:
                lst, i = got
                children.append(lst.pop(i))
        gid = grouping.get("groupObjectId")
        assert gid is not None, f"a group with no id: {r}"
        group: JsonObject = {"objectId": gid, "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU"},
                             "elementGroup": {"children": children}}
        objects[gid] = group
        elements.append(group)
    elif (gone := r.get("deleteObject")) is not None:
        got = find(elements, gone["objectId"])
        if got:
            lst, i = got
            lst.pop(i)
