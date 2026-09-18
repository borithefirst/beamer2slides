"""Human-like edits of a converted deck through the Slides API, for the sync tests.

Every edit finds its target by content, the way a person would (the slide titled "…", the
text box containing "…", the largest picture), never by our object ids, and returns an
expectation: what must still hold after a later sync, as tools/sync_check.py checks.
  {"edit": name, "args": {...}, "slides": [SEL, ...], "checks": [CHECK, ...]}
`slides` are the slides the edit touched (their selectors hold after the edit).

  python tools/deck_edits.py <deck> catalogue [--out expectations.json]   every edit kind, verified
  python tools/deck_edits.py <deck> apply '{"edit": "replace_word", "args": {...}}' [--out exp.json]
  python tools/deck_edits.py <deck> apply @edits.json [--out exp.json]            a list of edits from a file
<deck>: presentation id, URL or converted out folder.
"""

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

from .sync_check import (EMU_PER_PT, CheckError, Model, check_all, norm, phrase_span,
                         presentation_id, raw_text, text_elements, utf16)


class LiveDeck:
    """A presentation being edited: the API and the latest read-back."""

    def __init__(self, pid: str, api=None):
        from beamer2slides.google_auth import slides_service
        self.pid, self.api = pid, api or slides_service()
        self.model = self.read()

    def read(self) -> Model:
        from beamer2slides.gslides import execute
        self.model = Model(execute(self.api.presentations().get(presentationId=self.pid)))
        return self.model

    def batch(self, requests: list[dict]) -> dict:
        from beamer2slides.gslides import execute
        done = execute(self.api.presentations().batchUpdate(presentationId=self.pid, body={"requests": requests}))
        self.read()
        return done


def new_id() -> str:
    return "u" + uuid.uuid4().hex[:16]


def rgb(color: str) -> dict:
    return {"rgbColor": {k: int(color[i:i + 2], 16) / 255 for k, i in (("red", 1), ("green", 3), ("blue", 5))}}


def expectation(edit: str, args: dict, slides: list, checks: list[dict]) -> dict:
    return {"edit": edit, "args": args, "slides": slides, "checks": checks}


def locate(deck: LiveDeck, slide, phrase: str):
    """(slide, element, raw text, cellLocation, (start, end)) of the one text holding `phrase`."""
    s = deck.model.one(slide)
    el = deck.model.element(s, {"text": phrase})
    for raw, cell in el.texts:
        span = phrase_span(raw, phrase)
        if span:
            return s, el, raw, cell, span
    raise CheckError(f"{phrase!r} not found in {el.id}")


def _where(el, cell) -> dict:
    return {"objectId": el.id, **({"cellLocation": cell} if cell else {})}


def _range(raw: str, a: int, b: int) -> dict:
    return {"type": "FIXED_RANGE", "startIndex": utf16(raw, a), "endIndex": utf16(raw, b)}


def _word(raw: str, span: tuple[int, int], word: str) -> tuple[int, int]:
    m = re.search(rf"(?<!\w){re.escape(word)}(?!\w)", raw[span[0]:span[1]])
    if not m:
        raise CheckError(f"{word!r} not in {raw[span[0]:span[1]]!r}")
    return span[0] + m.start(), span[0] + m.end()


def _target_check(el, target: dict) -> dict:
    """A target that finds the element again after sync: its text, or the picture's centre."""
    if "text" in target:
        return {"text": target["text"]}
    return {"image_near": [round(v, 1) for v in el.center]}


# ---------------------------------------------------------------- text

def replace_word(deck: LiveDeck, slide, text: str, old: str, new: str) -> dict:
    """Replace one word inside the phrase `text` (select it, type over it)."""
    s, el, raw, cell, span = locate(deck, slide, text)
    a, b = _word(raw, span, old)
    after = norm(raw[span[0]:a] + new + raw[b:span[1]])
    deck.batch([{"deleteText": {**_where(el, cell), "textRange": _range(raw, a, b)}},
                {"insertText": {**_where(el, cell), "text": new, "insertionIndex": utf16(raw, a)}}])
    return expectation("replace_word", {"slide": slide, "text": text, "old": old, "new": new}, [slide],
                       [{"check": "text", "slide": slide, "text": after, "count": 1},
                        {"check": "text", "slide": slide, "text": text, "count": 0}])


def append_sentence(deck: LiveDeck, slide, text: str, sentence: str) -> dict:
    """Type a sentence at the end of the paragraph holding `text`."""
    s, el, raw, cell, span = locate(deck, slide, text)
    end = raw.find("\n", span[1])
    end = len(raw) if end < 0 else end
    deck.batch([{"insertText": {**_where(el, cell), "text": " " + sentence, "insertionIndex": utf16(raw, end)}}])
    return expectation("append_sentence", {"slide": slide, "text": text, "sentence": sentence}, [slide],
                       [{"check": "text", "slide": slide, "text": f"{text} {sentence}", "count": 1}])


def delete_paragraph(deck: LiveDeck, slide, text: str) -> dict:
    """Delete the paragraph (bullet) holding `text`; its neighbours stay."""
    s, el, raw, cell, span = locate(deck, slide, text)
    start = raw.rfind("\n", 0, span[0]) + 1
    end = raw.find("\n", span[1])
    end = len(raw) - 1 if end < 0 else end
    paragraphs = [p for p in raw.split("\n") if norm(p) and norm(text) not in norm(p)]
    a, b = (start, end + 1) if end < len(raw) - 1 else (max(0, start - 1), end)
    deck.batch([{"deleteText": {**_where(el, cell), "textRange": _range(raw, a, b)}}])
    checks = [{"check": "text", "slide": slide, "text": text, "count": 0}]
    if paragraphs:
        checks.append({"check": "text", "slide": slide, "text": norm(paragraphs[0]), "count": 1})
    return expectation("delete_paragraph", {"slide": slide, "text": text}, [slide], checks)


# ---------------------------------------------------------------- style

def _style(deck, name, slide, word, context, style, fields, check) -> dict:
    s, el, raw, cell, span = locate(deck, slide, context or word)
    a, b = _word(raw, span, word) if context else span
    deck.batch([{"updateTextStyle": {**_where(el, cell), "textRange": _range(raw, a, b), "style": style, "fields": fields}}])
    args = {"slide": slide, "word": word, "context": context}
    return expectation(name, args, [slide], [{"check": "style", "slide": slide, "text": word,
                                              **({"context": context} if context else {}), **check}])


def bold(deck: LiveDeck, slide, word: str, context: str | None = None) -> dict:
    return _style(deck, "bold", slide, word, context, {"bold": True}, "bold", {"bold": True})


def recolour(deck: LiveDeck, slide, word: str, color: str, context: str | None = None) -> dict:
    exp = _style(deck, "recolour", slide, word, context, {"foregroundColor": {"opaqueColor": rgb(color)}},
                 "foregroundColor", {"color": color.lower()})
    exp["args"]["color"] = color
    return exp


def resize_font(deck: LiveDeck, slide, text: str, size: float) -> dict:
    """Set the font size of the whole paragraph holding `text`."""
    s, el, raw, cell, span = locate(deck, slide, text)
    start, end = raw.rfind("\n", 0, span[0]) + 1, raw.find("\n", span[1])
    end = len(raw) if end < 0 else end
    deck.batch([{"updateTextStyle": {**_where(el, cell), "textRange": _range(raw, start, end),
                                     "style": {"fontSize": {"magnitude": size, "unit": "PT"}}, "fields": "fontSize"}}])
    return expectation("resize_font", {"slide": slide, "text": text, "size": size}, [slide],
                       [{"check": "style", "slide": slide, "text": text, "size": size}])


# ---------------------------------------------------------------- geometry

def _relative(object_id: str, m: list[float]) -> dict:
    return {"updatePageElementTransform": {"objectId": object_id, "applyMode": "RELATIVE", "transform": {
        "scaleX": m[0], "shearX": 0, "translateX": m[2] * EMU_PER_PT,
        "shearY": 0, "scaleY": m[1], "translateY": m[3] * EMU_PER_PT, "unit": "EMU"}}}


def move(deck: LiveDeck, slide, target: dict, dx: float, dy: float) -> dict:
    """Drag an element (a grouped one moves with its group, as a click selects the group)."""
    s = deck.model.one(slide)
    el = deck.model.element(s, target)
    x0, y0 = el.box[:2]
    deck.batch([_relative(el.top, [1, 1, dx, dy])])
    moved = deck.model.element(deck.model.one(slide), {"id": el.id})
    return expectation("move", {"slide": slide, "target": target, "dx": dx, "dy": dy}, [slide],
                       [{"check": "box", "slide": slide, "target": _target_check(moved, target),
                         "origin": [round(x0 + dx, 2), round(y0 + dy, 2)]}])


def resize(deck: LiveDeck, slide, target: dict, sx: float, sy: float | None = None) -> dict:
    """Resize an element (or its group) about its top-left corner."""
    sy = sx if sy is None else sy
    s = deck.model.one(slide)
    el = deck.model.element(s, target)
    top = next(e for e in s.elements if e.id == el.top)
    x0, y0 = top.box[:2]
    w, h = el.box[2] - el.box[0], el.box[3] - el.box[1]
    ox, oy = el.box[0] - x0, el.box[1] - y0
    deck.batch([_relative(el.top, [sx, sy, x0 * (1 - sx), y0 * (1 - sy)])])
    moved = deck.model.element(deck.model.one(slide), {"id": el.id})
    return expectation("resize", {"slide": slide, "target": target, "sx": sx, "sy": sy}, [slide],
                       [{"check": "box", "slide": slide, "target": _target_check(moved, target),
                         "origin": [round(x0 + ox * sx, 2), round(y0 + oy * sy, 2)],
                         "size": [round(w * sx, 2), round(h * sy, 2)]}])


# ---------------------------------------------------------------- objects

def delete_element(deck: LiveDeck, slide, target: dict) -> dict:
    """Select an element (inside its group if need be) and delete it."""
    s = deck.model.one(slide)
    el = deck.model.element(s, target)
    deck.batch([{"deleteObject": {"objectId": el.id}}])
    check = {"check": "text", "slide": slide, "text": target["text"], "count": 0} if "text" in target else \
        {"check": "image", "slide": slide, "near": [round(v, 1) for v in el.center], "count": 0}
    return expectation("delete_element", {"slide": slide, "target": target}, [slide], [check])


def delete_group(deck: LiveDeck, slide, target: dict) -> dict:
    """Click an element (which selects its outermost group) and delete: the whole group goes."""
    s = deck.model.one(slide)
    el = deck.model.element(s, target)
    if el.parent is None:
        raise CheckError(f"{target} is not in a group")
    top = next(e for e in s.elements if e.id == el.top)
    gone = [c for c in s.elements if c.groups[:1] == (top.id,) and c.kind != "group"]
    deck.batch([{"deleteObject": {"objectId": top.id}}])
    checks = [{"check": "text", "slide": slide, "text": c.text[:60], "count": 0} for c in gone if c.kind in ("shape", "table") and c.text]
    checks += [{"check": "image", "slide": slide, "near": [round(v, 1) for v in c.center], "count": 0} for c in gone if c.kind == "image"]
    return expectation("delete_group", {"slide": slide, "target": target}, [slide], checks)


def _props(page_id: str, box: list[float]) -> dict:
    x, y, w, h = box
    return {"pageObjectId": page_id, "size": {"width": {"magnitude": w * EMU_PER_PT, "unit": "EMU"},
                                              "height": {"magnitude": h * EMU_PER_PT, "unit": "EMU"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x * EMU_PER_PT, "translateY": y * EMU_PER_PT,
                          "unit": "EMU"}}


def add_text_box(deck: LiveDeck, slide, text: str, box: list[float]) -> dict:
    """Draw a text box ([x, y, w, h] pt) and type into it."""
    s, oid = deck.model.one(slide), new_id()
    deck.batch([{"createShape": {"objectId": oid, "shapeType": "TEXT_BOX", "elementProperties": _props(s.id, box)}},
                {"insertText": {"objectId": oid, "text": text}}])
    el = deck.model.element(deck.model.one(slide), {"id": oid})
    return expectation("add_text_box", {"slide": slide, "text": text, "box": box}, [slide],
                       [{"check": "text", "slide": slide, "text": text, "count": 1},
                        {"check": "box", "slide": slide, "target": {"text": text}, "origin": [round(v, 2) for v in el.box[:2]]}])


def add_shape(deck: LiveDeck, slide, shape_type: str, box: list[float], color: str) -> dict:
    s, oid = deck.model.one(slide), new_id()
    deck.batch([{"createShape": {"objectId": oid, "shapeType": shape_type, "elementProperties": _props(s.id, box)}},
                {"updateShapeProperties": {"objectId": oid, "fields": "shapeBackgroundFill.solidFill.color",
                                           "shapeProperties": {"shapeBackgroundFill": {"solidFill": {"color": rgb(color)}}}}}])
    el = deck.model.element(deck.model.one(slide), {"id": oid})
    return expectation("add_shape", {"slide": slide, "shape_type": shape_type, "box": box, "color": color}, [slide],
                       [{"check": "shape", "slide": slide, "shape_type": shape_type, "color": color.lower(),
                         "near": [round(v, 1) for v in el.center], "count": 1}])


def donor_image_url(api, pid: str) -> str:
    """contentUrl of a picture in another deck (createImage accepts it; it expires after a while)."""
    from beamer2slides.gslides import execute
    pres = execute(api.presentations().get(presentationId=pid, fields="slides(pageElements)"))
    for s in pres.get("slides", []):
        stack = list(s.get("pageElements", []))
        while stack:
            e = stack.pop(0)
            if "image" in e and e["image"].get("contentUrl"):
                return e["image"]["contentUrl"]
            stack += e.get("elementGroup", {}).get("children", [])
    raise CheckError(f"no picture in {pid}")


def add_image(deck: LiveDeck, slide, url: str, box: list[float]) -> dict:
    s, oid = deck.model.one(slide), new_id()
    deck.batch([{"createImage": {"objectId": oid, "url": url, "elementProperties": _props(s.id, box)}}])
    el = deck.model.element(deck.model.one(slide), {"id": oid})
    return expectation("add_image", {"slide": slide, "box": box}, [slide],
                       [{"check": "image", "slide": slide, "near": [round(v, 1) for v in el.center], "count": 1}])


def duplicate(deck: LiveDeck, slide, target: dict, dx: float = 12, dy: float = 12) -> dict:
    """Ctrl+D on the element (or its group) and drag the copy away."""
    s = deck.model.one(slide)
    el = deck.model.element(s, target)
    before = [dict(c) for c in _copies_checks(deck.model, slide, el, target)]
    oid = new_id()
    deck.batch([{"duplicateObject": {"objectId": el.top, "objectIds": {el.top: oid}}}, _relative(oid, [1, 1, dx, dy])])
    checks = []
    for c in before:
        if c["check"] == "text":
            checks.append({**c, "count": c["count"] * 2})
        else:
            checks += [c, {**c, "near": [round(c["near"][0] + dx, 1), round(c["near"][1] + dy, 1)]}]
    return expectation("duplicate", {"slide": slide, "target": target, "dx": dx, "dy": dy}, [slide], checks)


def _copies_checks(model: Model, slide, el, target: dict) -> list[dict]:
    if "text" in target:
        n = sum(norm(t).count(norm(target["text"])) for e in model.one(slide).elements for t, _ in e.texts)
        return [{"check": "text", "slide": slide, "text": target["text"], "count": n}]
    return [{"check": "image", "slide": slide, "near": [round(v, 1) for v in el.center], "count": 1}]


def _member(el) -> dict:
    t = {"text": el.text[:60], "near": [round(v, 1) for v in el.center]} if el.texts and el.text else \
        {"image_near": [round(v, 1) for v in el.center]}
    return t


def group(deck: LiveDeck, slide, targets: list[dict]) -> dict:
    """Select several top-level elements and group them."""
    s = deck.model.one(slide)
    els = [deck.model.element(s, t) for t in targets]
    tops = list(dict.fromkeys(e.top for e in els))
    deck.batch([{"groupObjects": {"groupObjectId": new_id(), "childrenObjectIds": tops}}])
    s = deck.model.one(slide)
    members = [_member(deck.model.element(s, {"id": e.id})) for e in els]
    return expectation("group", {"slide": slide, "targets": targets}, [slide],
                       [{"check": "grouped", "slide": slide, "members": members, "grouped": True}])


def ungroup(deck: LiveDeck, slide, target: dict) -> dict:
    """Ungroup the group holding the target."""
    s = deck.model.one(slide)
    el = deck.model.element(s, target)
    if el.parent is None:
        raise CheckError(f"{target} is not in a group")
    children = [c for c in s.elements if c.parent == el.parent and c.kind != "group"]
    deck.batch([{"ungroupObjects": {"objectIds": [el.parent]}}])
    members = [_member(c) for c in children]
    return expectation("ungroup", {"slide": slide, "target": target}, [slide],
                       [{"check": "grouped", "slide": slide, "members": members, "grouped": False}])


# ---------------------------------------------------------------- slides

def _title_placeholder(s) -> tuple[str, int] | None:
    """The slide's title placeholder as (type, index), or None. `createSlide` can only map a
    placeholder the layout really has - it answers *"The placeholder (15_0_0) is not on the page"*
    and refuses the whole batch otherwise - and a converted deck has layouts without a plain TITLE:
    the title page's carries CENTERED_TITLE, and a "(no theme)" copy may carry neither."""
    for e in s.elements:
        ph = e.obj.get("shape", {}).get("placeholder", {}) if e.kind == "shape" else {}
        if ph.get("type") in ("TITLE", "CENTERED_TITLE"):
            return ph["type"], ph.get("index", 0)
    return None


def add_slide(deck: LiveDeck, after, title: str, body: str | None = None) -> dict:
    """A new slide after `after`, with a title (and a text box). It takes `after`'s layout when that
    one offers a title placeholder to write in, else the layout of a slide that does - which is what
    a person does too, and what keeps the slide findable by its title afterwards. Without it, every
    such edit after the title page was refused by the API and silently dropped from the round."""
    s = deck.model.one(after)
    host = s if _title_placeholder(s) else next((x for x in deck.model.slides if _title_placeholder(x)), s)
    kind, index = _title_placeholder(host) or ("TITLE", 0)
    sid, tid = new_id(), new_id()
    reqs = [{"createSlide": {"objectId": sid, "insertionIndex": s.index + 1,
                             "slideLayoutReference": {"layoutId": host.obj["slideProperties"]["layoutObjectId"]},
                             "placeholderIdMappings": [{"layoutPlaceholder": {"type": kind, "index": index}, "objectId": tid}]}},
            {"insertText": {"objectId": tid, "text": title}}]
    if body:
        bid = new_id()
        reqs += [{"createShape": {"objectId": bid, "shapeType": "TEXT_BOX", "elementProperties": _props(sid, [40, 120, 600, 60])}},
                 {"insertText": {"objectId": bid, "text": body}}]
    deck.batch(reqs)
    sel = {"title": title}
    checks = [{"check": "slides", "order": [after, sel]}, {"check": "slide_count", "slide": sel, "count": 1}]
    if body:
        checks.append({"check": "text", "slide": sel, "text": body, "count": 1})
    return expectation("add_slide", {"after": after, "title": title, "body": body}, [sel], checks)


def duplicate_slide(deck: LiveDeck, slide, new_title: str | None = None) -> dict:
    """Duplicate a slide (the copy comes right after it), optionally retitling the copy."""
    s = deck.model.one(slide)
    sid = new_id()
    deck.batch([{"duplicateObject": {"objectId": s.id, "objectIds": {s.id: sid}}}])
    if not new_title:
        return expectation("duplicate_slide", {"slide": slide}, [],
                           [{"check": "slide_count", "slide": slide, "count": 2}])
    copy = next(x for x in deck.model.slides if x.id == sid)
    title = next(e for e in copy.elements if e.kind == "shape" and e.obj["shape"].get("placeholder", {}).get("type") in ("TITLE", "CENTERED_TITLE"))
    raw = title.texts[0][0]
    first = raw.split("\n")[0]
    deck.batch([{"deleteText": {"objectId": title.id, "textRange": _range(raw, 0, len(first))}},
                {"insertText": {"objectId": title.id, "text": new_title, "insertionIndex": 0}}])
    sel = {"title": new_title}
    return expectation("duplicate_slide", {"slide": slide, "new_title": new_title}, [slide, sel],
                       [{"check": "slides", "order": [slide, sel], "adjacent": True},
                        {"check": "slide_count", "slide": sel, "count": 1}])


def delete_slide(deck: LiveDeck, slide) -> dict:
    s = deck.model.one(slide)
    deck.batch([{"deleteObject": {"objectId": s.id}}])
    return expectation("delete_slide", {"slide": slide}, [], [{"check": "slide_count", "slide": slide, "count": 0}])


def move_slide(deck: LiveDeck, slide, after) -> dict:
    """Drag a slide in the filmstrip to right after `after`."""
    s, a = deck.model.one(slide), deck.model.one(after)
    deck.batch([{"updateSlidesPosition": {"slideObjectIds": [s.id], "insertionIndex": a.index + 1}}])
    return expectation("move_slide", {"slide": slide, "after": after}, [slide],
                       [{"check": "slides", "order": [after, slide], "adjacent": True}])


def set_notes(deck: LiveDeck, slide, text: str) -> dict:
    """Replace the speaker notes."""
    s = deck.model.one(slide)
    shape = s.notes_shape()
    reqs = [{"deleteText": {"objectId": shape["objectId"], "textRange": {"type": "ALL"}}}] \
        if norm(raw_text(text_elements(shape.get("shape", {})))) else []
    deck.batch(reqs + [{"insertText": {"objectId": shape["objectId"], "text": text, "insertionIndex": 0}}])
    return expectation("set_notes", {"slide": slide, "text": text}, [slide],
                       [{"check": "notes", "slide": slide, "text": text}])


def set_background(deck: LiveDeck, slide, color: str) -> dict:
    s = deck.model.one(slide)
    deck.batch([{"updatePageProperties": {"objectId": s.id, "fields": "pageBackgroundFill.solidFill.color",
                                          "pageProperties": {"pageBackgroundFill": {"solidFill": {"color": rgb(color)}}}}}])
    return expectation("set_background", {"slide": slide, "color": color}, [slide],
                       [{"check": "background", "slide": slide, "color": color.lower()}])


EDITS = {f.__name__: f for f in (replace_word, append_sentence, delete_paragraph, bold, recolour, resize_font, move,
                                 resize, delete_element, delete_group, add_text_box, add_shape, add_image, duplicate, group, ungroup,
                                 add_slide, duplicate_slide, delete_slide, move_slide, set_notes, set_background)}


def apply(deck: LiveDeck, spec: dict) -> dict:
    """Run one edit given as {"edit": name, "args": {...}}."""
    return EDITS[spec["edit"]](deck, **spec["args"])


def verified(deck: LiveDeck, spec: dict) -> tuple[dict, list[str]]:
    """Apply an edit and read it back: its checks must fail before (the edit changes something)
    and hold after. (expectation, problems)"""
    before = deck.model
    exp = apply(deck, spec)
    problems = check_all(deck.model, exp["checks"])
    if not check_all(before, exp["checks"]):
        problems.append(f"{spec['edit']}: every check already held before the edit")
    return exp, [f"{spec['edit']}: {p}" for p in problems]


def catalogue(donor_url: str | None) -> list[dict]:
    """Every edit kind once, on a deck converted from tests/decks/sync v1, not interfering."""
    why, algo, merging, conv, policy, results = ("Why decks and sources diverge", "The sync algorithm", "Merging text",
                                                  "Convergence", "Merge policy", "Results")
    versions, identity, concl = "Three versions", "Finding the same slide", "Conclusions"
    edits = [
        ("replace_word", dict(slide=why, text="People polish the converted deck by hand", old="polish", new="refine")),
        ("delete_paragraph", dict(slide=why, text="adding their own slides")),
        ("append_sentence", dict(slide=merging, text="writes the merged paragraph back into the deck.",
                                 sentence="Nothing is lost.")),
        ("bold", dict(slide=concl, word="survive", context="Deck edits survive every sync")),
        ("recolour", dict(slide=concl, word="both versions", context="Conflicts are reported with both versions",
                          color="#c00000")),
        ("resize_font", dict(slide=concl, text="Conflicts are reported with both versions", size=20)),
        ("move", dict(slide=conv, target={"text": "Conflicts disappear once"}, dx=0, dy=40)),
        ("resize", dict(slide=conv, target={"image": "largest"}, sx=0.8)),
        ("delete_element", dict(slide=conv, target={"text": "open conflicts"})),
        ("add_text_box", dict(slide=results, text="Measured on the test decks", box=[460, 60, 220, 30])),
        ("add_shape", dict(slide=results, shape_type="STAR_5", box=[640, 100, 50, 50], color="#ffc000")),
        ("duplicate", dict(slide=results, target={"text": "Same element"}, dx=0, dy=110)),
        ("group", dict(slide=policy, targets=[{"text": "Both versions go into the report."}, {"text": "Deck edits win"}])),
        ("ungroup", dict(slide=algo, target={"text": "Read the base snapshot"})),
        ("delete_group", dict(slide=versions, target={"text": "Merged"})),
        ("add_slide", dict(after=versions, title="Reviewer questions", body="What happens to comments?")),
        # ...and once after the title page, whose layout has no plain TITLE placeholder: the API
        # refuses a mapping for a placeholder the layout hasn't got, and the campaign lost every
        # such edit to that (`_title_placeholder`).
        ("add_slide", dict(after="Keeping Slides and Source in Sync", title="Agenda for today",
                           body="Written on the title page's own layout")),
        ("duplicate_slide", dict(slide=concl, new_title="Conclusions (short)")),
        ("move_slide", dict(slide=policy, after=results)),
        ("delete_slide", dict(slide=identity)),
        ("set_notes", dict(slide=merging, text="Mention diff3 here.")),
        ("set_background", dict(slide=merging, color="#fff2cc")),
    ]
    if donor_url:
        edits.insert(12, ("add_image", dict(slide=versions, url=donor_url, box=[540, 250, 150, 100])))
    return [{"edit": name, "args": args} for name, args in edits]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck", help="presentation id, URL or converted out folder")
    ap.add_argument("command", choices=["catalogue", "apply"])
    ap.add_argument("spec", nargs="?", help="apply: the edit as JSON")
    ap.add_argument("--donor", help="presentation id of another deck to take a picture URL from")
    ap.add_argument("--out", type=Path, help="write the expectations here (JSON list)")
    args = ap.parse_args()
    deck = LiveDeck(presentation_id(args.deck))
    if args.command == "apply":
        spec = json.loads(Path(args.spec[1:]).read_text(encoding="utf-8-sig") if args.spec.startswith("@") else args.spec)
        specs = spec if isinstance(spec, list) else [spec]
    else:
        specs = catalogue(donor_image_url(deck.api, args.donor) if args.donor else None)
    expectations, problems = [], []
    for spec in specs:
        exp, bad = verified(deck, spec)
        expectations.append(exp)
        problems += bad
        print(f"{'FAIL' if bad else 'ok  '} {spec['edit']}" + "".join(f"\n     {p}" for p in bad))
    final = check_all(deck.model, [c for e in expectations for c in e["checks"]])
    problems += [f"at the end: {p}" for p in final]
    print("".join(f"at the end: {p}\n" for p in final), end="")
    if args.out:
        args.out.write_text(json.dumps(expectations, indent=1, ensure_ascii=False), encoding="utf-8")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
