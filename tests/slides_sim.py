"""A tiny Google Slides model: replays the requests emit plans (emit.plan_offline) into the JSON
`presentations.get` returns, as far as deck_ir reads it (text boxes and placeholders with their
runs, paragraph styles and bullets, pictures, groups, speaker notes). No Google involved."""

import copy

from beamer2slides.emit import SLIDE_W, plan_offline

GLYPHS = {"BULLET_DISC_CIRCLE_SQUARE": "●", "BULLET_ARROW3D_CIRCLE_SQUARE": "➢", "BULLET_CHECKBOX": "❏",
          "BULLET_STAR_CIRCLE_SQUARE": "★", "BULLET_DIAMOND_CIRCLE_SQUARE": "◆",
          "BULLET_DIAMONDX_HOLLOWDIAMOND_SQUARE": "❖", "NUMBERED_DIGIT_ALPHA_ROMAN": "1.",
          "NUMBERED_DIGIT_ALPHA_ROMAN_PARENS": "1)"}


class Text:
    """Characters with their styles; a paragraph's marker sits on its closing newline (the last
    paragraph's in `end`)."""

    def __init__(self):
        self.chars: list[str] = []
        self.styles: list[dict] = []
        self.marks: list[dict | None] = []
        self.end = {"style": {}, "bullet": None}

    def paragraphs(self) -> list[tuple[int, int, dict]]:
        out, start = [], 0
        for i, c in enumerate(self.chars):
            if c == "\n":
                out.append((start, i, self.marks[i]))
                start = i + 1
        out.append((start, len(self.chars), self.end))
        return out

    def insert(self, at: int, text: str) -> None:
        style = dict(self.styles[at - 1]) if at > 0 and self.styles else {}
        self.chars[at:at] = list(text)
        self.styles[at:at] = [dict(style) for _ in text]
        self.marks[at:at] = [{"style": {}, "bullet": None} if c == "\n" else None for c in text]

    def remove(self, a: int, b: int) -> None:
        self.chars[a:b] = []
        self.styles[a:b] = []
        self.marks[a:b] = []

    def ranges(self, rng: dict) -> tuple[int, int]:
        if rng.get("type") == "ALL":
            return 0, len(self.chars)
        return rng.get("startIndex", 0), rng.get("endIndex", len(self.chars))

    def json(self) -> dict:
        out = []
        for a, b, marker in self.paragraphs():
            para = {"paragraphMarker": {"style": copy.deepcopy(marker["style"])}}
            if marker["bullet"]:
                para["paragraphMarker"]["bullet"] = copy.deepcopy(marker["bullet"])
            out.append(para)
            k = a
            while k < b:
                e = k
                while e < b and self.styles[e] == self.styles[k]:
                    e += 1
                out.append({"textRun": {"content": "".join(self.chars[k:e]), "style": copy.deepcopy(self.styles[k])}})
                k = e
            out.append({"textRun": {"content": "\n", "style": copy.deepcopy(self.styles[b - 1]) if b > a else {}}})
        return {"textElements": out}


def simulate(deck: dict) -> dict:
    """presentations.get-shaped JSON of the deck emit would build (offline plan, no measured moves)."""
    plan = plan_offline(deck)
    scale = plan["plan"].scale
    page_w, page_h = deck["slides"][0]["size"]
    slides, objects, placeholder_type = [], {}, {}
    for req in plan["copies"]:
        ids = req["duplicateObject"]["objectIds"]
        for src, new in ids.items():
            for kind in ("CENTERED_TITLE", "SUBTITLE", "TITLE"):
                if src.endswith("_" + kind):
                    placeholder_type[new] = kind
    for slide_id, page, parts, element_ids in plan["slides"]:
        elements: list[dict] = []
        for pe in plan["page_elements"].get(slide_id, []):
            oid = pe["objectId"]
            if oid in placeholder_type:
                obj = {"objectId": oid, "size": copy.deepcopy(pe["size"]),
                       "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU"},
                       "shape": {"shapeType": "TEXT_BOX", "placeholder": {"type": placeholder_type[oid]}},
                       "_text": Text()}
                objects[oid] = obj
                elements.append(obj)
        slide_deck = next(s for s in plan["plan"].deck["slides"] if s["page"] == page)
        pictures = plan["pictures"][page]
        for i, (el, box) in zip([i for i, e in enumerate(slide_deck["elements"]) if e["kind"] == "image"], pictures):
            oid = f"{slide_id}_f{i}"
            obj = {"objectId": oid, "size": {"width": {"magnitude": (box[2] - box[0]) * 12700, "unit": "EMU"},
                                             "height": {"magnitude": (box[3] - box[1]) * 12700, "unit": "EMU"}},
                   "transform": {"scaleX": 1, "scaleY": 1, "translateX": box[0] * 12700, "translateY": box[1] * 12700,
                                 "unit": "EMU"},
                   "title": "Figure", "image": {"contentUrl": None}}
            objects[oid] = obj
            elements.append(obj)
        notes = Text()
        for el, reqs in parts:
            for r in reqs:
                apply(r, elements, objects, notes, slide_id)
        for obj in objects.values():
            if "_text" in obj:
                obj["shape"]["text"] = obj["_text"].json()
        slides.append({"objectId": slide_id, "pageElements": [strip(e) for e in elements],
                       "slideProperties": {"notesPage": {"notesProperties": {"speakerNotesObjectId": f"{slide_id}_notes"},
                                                         "pageElements": [{"objectId": f"{slide_id}_notes", "shape": {
                                                             "text": notes.json()}}]}}})
    return {"presentationId": "simulated", "pageSize": {"width": {"magnitude": SLIDE_W * 12700, "unit": "EMU"},
                                                        "height": {"magnitude": page_h * scale * 12700, "unit": "EMU"}},
            "slides": slides, "layouts": [], "masters": []}


def strip(e: dict) -> dict:
    out = {k: v for k, v in e.items() if not k.startswith("_")}
    if "elementGroup" in out:
        out["elementGroup"] = {"children": [strip(c) for c in out["elementGroup"]["children"]]}
    return out


def find(elements: list[dict], oid: str) -> tuple[list[dict], int] | None:
    for i, e in enumerate(elements):
        if e["objectId"] == oid:
            return elements, i
        if "elementGroup" in e:
            got = find(e["elementGroup"]["children"], oid)
            if got:
                return got
    return None


def apply(r: dict, elements: list[dict], objects: dict, notes: Text, slide_id: str) -> None:
    (name, body), = r.items()
    if name == "createShape":
        props = body["elementProperties"]
        obj = {"objectId": body["objectId"], "size": copy.deepcopy(props["size"]),
               "transform": copy.deepcopy(props["transform"]),
               "shape": {"shapeType": body["shapeType"], "shapeProperties": {}}, "_text": Text()}
        objects[body["objectId"]] = obj
        elements.append(obj)
    elif name == "updatePageElementTransform":
        obj = objects.get(body["objectId"])
        if obj is None:
            return
        t = body["transform"]
        if body["applyMode"] == "ABSOLUTE":
            obj["transform"] = copy.deepcopy(t)
        else:
            obj["transform"]["translateX"] = obj["transform"].get("translateX", 0) + t.get("translateX", 0)
            obj["transform"]["translateY"] = obj["transform"].get("translateY", 0) + t.get("translateY", 0)
    elif name == "updateShapeProperties":
        obj = objects.get(body["objectId"])
        if obj is not None:
            obj["shape"].setdefault("shapeProperties", {}).update(copy.deepcopy(body["shapeProperties"]))
    elif name in ("insertText", "deleteText", "updateTextStyle", "createParagraphBullets", "updateParagraphStyle"):
        oid = body["objectId"]
        text = notes if oid == f"{slide_id}_notes" else objects.get(oid, {}).get("_text")
        if text is None:
            return
        if name == "insertText":
            text.insert(body.get("insertionIndex", 0), body["text"])
        elif name == "deleteText":
            a, b = text.ranges(body["textRange"])
            text.remove(a, b)
        elif name == "updateTextStyle":
            a, b = text.ranges(body["textRange"])
            fields = body["fields"].split(",")
            for k in range(a, min(b, len(text.chars))):
                for f in fields:
                    if f in body["style"]:
                        text.styles[k][f] = copy.deepcopy(body["style"][f])
        elif name == "updateParagraphStyle":
            a, b = text.ranges(body["textRange"])
            for pa, pb, marker in text.paragraphs():
                if pa <= max(a, b - 1) and pb >= a:
                    for f in body["fields"].split(","):
                        if f in body["style"]:
                            marker["style"][f] = copy.deepcopy(body["style"][f])
        elif name == "createParagraphBullets":
            a, b = text.ranges(body["textRange"])
            paras = [(pa, pb, m) for pa, pb, m in text.paragraphs() if pa <= max(a, b - 1) and pb >= a]
            for pa, pb, marker in reversed(paras):
                tabs = 0
                while pa + tabs < len(text.chars) and text.chars[pa + tabs] == "\t":
                    tabs += 1
                marker["bullet"] = {"listId": "l", "nestingLevel": tabs, "glyph": GLYPHS.get(body["bulletPreset"], "●")}
                if tabs:
                    text.remove(pa, pa + tabs)
    elif name == "groupObjects":
        children = []
        for cid in body["childrenObjectIds"]:
            got = find(elements, cid)
            if got:
                lst, i = got
                children.append(lst.pop(i))
        group = {"objectId": body["groupObjectId"], "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU"},
                 "elementGroup": {"children": children}}
        objects[body["groupObjectId"]] = group
        elements.append(group)
    elif name == "deleteObject":
        got = find(elements, body["objectId"])
        if got:
            lst, i = got
            lst.pop(i)
