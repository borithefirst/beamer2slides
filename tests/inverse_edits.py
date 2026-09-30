"""Synthetic deck edits made directly on a classified IR (targets for the inverse loop tests).

A deck here is deck.json as the loop reads it (with the fixture's `key` and `frame_index`), so a
`JsonObject` read through `json_reads`; each edit returns an edited copy."""

import copy

from beamer2slides.json_types import Json, JsonObject

from .json_reads import jarr, jnum, jnums, jobj, jobjs, jstr

RUN_DEFAULTS: JsonObject = {"font": "CMSS10", "family": "sans", "bold": False, "italic": False, "smallcaps": False,
                            "color": "#000000", "link": None, "script": None, "underline": False, "strike": False,
                            "highlight": None}


def _paragraphs(e: JsonObject) -> list[JsonObject]:
    """A text element's paragraphs; none for a picture or a shape."""
    return jobjs(e, "paragraphs") if "paragraphs" in e else []


def _text(p: JsonObject) -> str:
    return "".join(jstr(r, "text") for r in jobjs(p, "runs"))


def element(deck: JsonObject, slide: int, eid: str | None, text: str | None) -> JsonObject:
    """The element `eid` of the slide, else its first text element saying `text`."""
    els = jobjs(deck, "slides", slide, "elements")
    if eid:
        return next(e for e in els if e["id"] == eid)
    assert text is not None, "an element is found by its id or by its text"
    return next(e for e in els if e["kind"] == "text" and any(text in _text(p) for p in _paragraphs(e)))


def move(deck: JsonObject, slide: int, el: JsonObject, dx: float, dy: float) -> JsonObject:
    out = copy.deepcopy(deck)
    e = next(x for x in jobjs(out, "slides", slide, "elements") if x["id"] == el["id"])
    x0, y0, x1, y1 = jnums(e, "bbox")
    e["bbox"] = [x0 + dx, y0 + dy, x1 + dx, y1 + dy]
    for p in _paragraphs(e):
        p["text_x0"] = jnum(p, "text_x0") + dx
        if p.get("tab_x0") is not None:
            p["tab_x0"] = jnum(p, "tab_x0") + dx
        for line in jobjs(p, "lines"):
            line["baseline"] = jnum(line, "baseline") + dy
            line["x0"] = jnum(line, "x0") + dx
            line["x1"] = jnum(line, "x1") + dx
        if p.get("wrap_limit"):
            p["wrap_limit"] = jnum(p, "wrap_limit") + dx
        bullet = p.get("bullet")
        if isinstance(bullet, dict) and bullet.get("bbox"):
            b0, b1, b2, b3 = jnums(bullet, "bbox")
            bullet["bbox"] = [b0 + dx, b1 + dy, b2 + dx, b3 + dy]
    return out


def split_style(deck: JsonObject, slide: int, word: str, **style: Json) -> JsonObject:
    """The first occurrence of `word` on the slide gets `style`."""
    out = copy.deepcopy(deck)
    for e in jobjs(out, "slides", slide, "elements"):
        for p in _paragraphs(e):
            runs = jarr(p, "runs")
            for k, r in enumerate(jobjs(p, "runs")):
                said = jstr(r, "text")
                i = said.find(word)
                if i < 0 or r.get("hole"):
                    continue
                parts: list[tuple[str, JsonObject]] = [(said[:i], {}), (word, style), (said[i + len(word):], {})]
                pieces: list[Json] = [{**r, "text": t, **st} for t, st in parts if t]
                runs[k:k + 1] = pieces
                return out
    raise ValueError(word)


def reword(deck: JsonObject, slide: int, old: str, new: str) -> JsonObject:
    out = copy.deepcopy(deck)
    for e in jobjs(out, "slides", slide, "elements"):
        for p in _paragraphs(e):
            for r in jobjs(p, "runs"):
                said = jstr(r, "text")
                if old in said:
                    r["text"] = said.replace(old, new, 1)
                    return out
    raise ValueError(old)


def add_text_box(deck: JsonObject, slide: int, text: str, x: float, baseline: float, size: float,
                 color: str, width: float) -> JsonObject:
    out = copy.deepcopy(deck)
    s = jobj(out, "slides", slide)
    elements = jarr(s, "elements")
    run: JsonObject = {**RUN_DEFAULTS, "text": text, "size": size, "color": color}
    paragraph: JsonObject = {"align": "left", "level": 0, "bullet": None, "size": size, "text_x0": x, "tab_x0": None,
                             "lines": [{"baseline": baseline, "x0": x, "x1": x + width}], "runs": [run]}
    box: JsonObject = {"id": f"p{s['page']}new{len(elements)}", "kind": "text", "role": "body",
                       "bbox": [x, baseline - 0.75 * size, x + width, baseline + 0.25 * size],
                       "paragraphs": [paragraph]}
    elements.append(box)
    return out


def delete_paragraph(deck: JsonObject, slide: int, text: str) -> JsonObject:
    out = copy.deepcopy(deck)
    for e in jobjs(out, "slides", slide, "elements"):
        for i, p in enumerate(_paragraphs(e)):
            if _text(p) == text:
                del jarr(e, "paragraphs")[i]
                return out
    raise ValueError(text)


def add_slide(deck: JsonObject, after: int, title: str, items: list[str], key: str | None) -> JsonObject:
    out = copy.deepcopy(deck)
    ref = jobj(out, "slides", after)
    tref = next(e for e in jobjs(ref, "elements") if e["role"] == "title")
    title_el = copy.deepcopy(tref)
    title_el["id"] = "new-title"
    first = jobj(title_el, "paragraphs", 0)
    title_el["paragraphs"] = [{**first, "runs": [{**jobj(first, "runs", 0), "text": title}]}]
    paragraphs: list[Json] = [
        {"align": "left", "level": 0, "bullet": {"kind": "glyph", "text": "▶", "color": "#3333b3"}, "size": 10.91,
         "text_x0": 44.7, "tab_x0": None, "lines": [{"baseline": 100 + 15 * k, "x0": 44.7, "x1": 200}],
         "runs": [{**RUN_DEFAULTS, "text": t, "size": 10.91}]} for k, t in enumerate(items)]
    body: JsonObject = {"id": "new-body", "kind": "text", "role": "body", "bbox": [30, 90, 300, 150],
                        "paragraphs": paragraphs}
    new: JsonObject = {**{k: v for k, v in ref.items() if k not in ("elements", "notes", "key")}, "page": -1,
                       "key": key, "notes": None, "elements": [title_el, body]}
    jarr(out, "slides").insert(after + 1, new)
    return out


def swap_slides(deck: JsonObject, i: int, j: int) -> JsonObject:
    out = copy.deepcopy(deck)
    slides = jarr(out, "slides")
    slides[i], slides[j] = slides[j], slides[i]
    return out


def set_notes(deck: JsonObject, slide: int, text: str | None) -> JsonObject:
    out = copy.deepcopy(deck)
    jobj(out, "slides", slide)["notes"] = text
    return out


def add_image(deck: JsonObject, slide: int, file: str, bbox: list[float]) -> JsonObject:
    out = copy.deepcopy(deck)
    s = jobj(out, "slides", slide)
    elements = jarr(s, "elements")
    image: JsonObject = {"id": f"p{s['page']}img{len(elements)}", "kind": "image", "role": "figure",
                         "bbox": [float(v) for v in bbox], "file": file}
    elements.append(image)
    return out
