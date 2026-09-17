"""Synthetic deck edits made directly on a classified IR (targets for the inverse loop tests)."""

import copy

RUN_DEFAULTS = {"font": "CMSS10", "family": "sans", "bold": False, "italic": False, "smallcaps": False,
                "color": "#000000", "link": None, "script": None, "underline": False, "strike": False,
                "highlight": None}


def element(deck: dict, slide: int, eid: str | None = None, text: str | None = None) -> dict:
    els = deck["slides"][slide]["elements"]
    if eid:
        return next(e for e in els if e["id"] == eid)
    return next(e for e in els if e["kind"] == "text" and any(text in "".join(r["text"] for r in p["runs"])
                                                            for p in e["paragraphs"]))


def move(deck: dict, slide: int, el: dict, dx: float, dy: float) -> dict:
    out = copy.deepcopy(deck)
    e = next(x for x in out["slides"][slide]["elements"] if x["id"] == el["id"])
    e["bbox"] = [e["bbox"][0] + dx, e["bbox"][1] + dy, e["bbox"][2] + dx, e["bbox"][3] + dy]
    for p in e.get("paragraphs", []):
        p["text_x0"] += dx
        if p.get("tab_x0") is not None:
            p["tab_x0"] += dx
        for line in p["lines"]:
            line["baseline"] += dy
            line["x0"] += dx
            line["x1"] += dx
        if p.get("wrap_limit"):
            p["wrap_limit"] += dx
        if p.get("bullet") and p["bullet"].get("bbox"):
            b = p["bullet"]["bbox"]
            p["bullet"]["bbox"] = [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]
    return out


def split_style(deck: dict, slide: int, word: str, **style) -> dict:
    """The first occurrence of `word` on the slide gets `style`."""
    out = copy.deepcopy(deck)
    for e in out["slides"][slide]["elements"]:
        for p in e.get("paragraphs", []):
            for k, r in enumerate(p["runs"]):
                i = r["text"].find(word)
                if i < 0 or r.get("hole"):
                    continue
                parts = [(r["text"][:i], {}), (word, style), (r["text"][i + len(word):], {})]
                p["runs"][k:k + 1] = [{**r, "text": t, **st} for t, st in parts if t]
                return out
    raise ValueError(word)


def reword(deck: dict, slide: int, old: str, new: str) -> dict:
    out = copy.deepcopy(deck)
    for e in out["slides"][slide]["elements"]:
        for p in e.get("paragraphs", []):
            for r in p["runs"]:
                if old in r["text"]:
                    r["text"] = r["text"].replace(old, new, 1)
                    return out
    raise ValueError(old)


def add_text_box(deck: dict, slide: int, text: str, x: float, baseline: float, size: float = 10.91,
                 color: str = "#000000", width: float = 120.0) -> dict:
    out = copy.deepcopy(deck)
    s = out["slides"][slide]
    run = {**RUN_DEFAULTS, "text": text, "size": size, "color": color}
    s["elements"].append({"id": f"p{s['page']}new{len(s['elements'])}", "kind": "text", "role": "body",
                          "bbox": [x, baseline - 0.75 * size, x + width, baseline + 0.25 * size],
                          "paragraphs": [{"align": "left", "level": 0, "bullet": None, "size": size, "text_x0": x,
                                          "tab_x0": None, "lines": [{"baseline": baseline, "x0": x, "x1": x + width}],
                                          "runs": [run]}]})
    return out


def delete_paragraph(deck: dict, slide: int, text: str) -> dict:
    out = copy.deepcopy(deck)
    for e in out["slides"][slide]["elements"]:
        for i, p in enumerate(e.get("paragraphs", [])):
            if "".join(r["text"] for r in p["runs"]) == text:
                del e["paragraphs"][i]
                return out
    raise ValueError(text)


def add_slide(deck: dict, after: int, title: str, items: list[str], key: str | None = None) -> dict:
    out = copy.deepcopy(deck)
    ref = out["slides"][after]
    tref = next(e for e in ref["elements"] if e["role"] == "title")
    title_el = copy.deepcopy(tref)
    title_el["id"] = "new-title"
    title_el["paragraphs"] = [{**title_el["paragraphs"][0], "runs": [{**title_el["paragraphs"][0]["runs"][0], "text": title}]}]
    body = {"id": "new-body", "kind": "text", "role": "body", "bbox": [30, 90, 300, 150], "paragraphs": [
        {"align": "left", "level": 0, "bullet": {"kind": "glyph", "text": "▶", "color": "#3333b3"}, "size": 10.91,
         "text_x0": 44.7, "tab_x0": None, "lines": [{"baseline": 100 + 15 * k, "x0": 44.7, "x1": 200}],
         "runs": [{**RUN_DEFAULTS, "text": t, "size": 10.91}]} for k, t in enumerate(items)]}
    new = {**{k: v for k, v in ref.items() if k not in ("elements", "notes", "key")}, "page": -1, "key": key,
           "notes": None, "elements": [title_el, body]}
    out["slides"].insert(after + 1, new)
    return out


def swap_slides(deck: dict, i: int, j: int) -> dict:
    out = copy.deepcopy(deck)
    out["slides"][i], out["slides"][j] = out["slides"][j], out["slides"][i]
    return out


def set_notes(deck: dict, slide: int, text: str | None) -> dict:
    out = copy.deepcopy(deck)
    out["slides"][slide]["notes"] = text
    return out


def add_image(deck: dict, slide: int, file: str, bbox: list[float]) -> dict:
    out = copy.deepcopy(deck)
    s = out["slides"][slide]
    s["elements"].append({"id": f"p{s['page']}img{len(s['elements'])}", "kind": "image", "role": "figure",
                          "bbox": bbox, "file": file})
    return out
