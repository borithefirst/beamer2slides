"""Identity for sync: slide keys, element keys, fingerprints and IR hashes (docs/sync.md).

Slides are keyed by their beamer frame label, else `title:<normalised title>#<occurrence>`,
else `page:<n>`. Elements within a slide are keyed `kind/role/ordinal`. A new conversion (ours)
inherits the keys of the base it matches: labels first, then a sequence alignment of the
unlabelled slides; elements by key with a similar fingerprint, then by best fingerprint.
"""

import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

HOLE_MARK = "□"  # an inline formula picture's place in fingerprint text
SLIDE_MATCH = 0.6     # least similarity of two unlabelled slides to be the same frame
KEY_MATCH = 0.5       # least similarity for an element keeping the key it would get anyway
ELEMENT_MATCH = 0.35  # least similarity for an element inheriting another key
DROP_KEYS = {"id", "spans", "file", "px", "drawings", "drawing"}
STYLE_KEYS = {"font", "family", "size", "bold", "italic", "smallcaps", "color", "fill", "stroke", "shape", "align",
              "level", "script", "underline", "strike", "highlight", "link", "opacity", "shadow", "radius", "code",
              "flip", "weight", "arrow_from", "arrow_to", "rotation"}
ID_LIKE = re.compile(r"p\d+([a-z]+\d+.*)")
UNIQUE_ROLES = ("title", "footer")  # one per slide: matched by role and place whatever their words


def sha1(data: bytes | str) -> str:
    return hashlib.sha1(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def norm_title(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def run_text(runs: list[dict]) -> str:
    return "".join(HOLE_MARK if r.get("hole") else r["text"] for r in runs)


def plain_text(el: dict) -> str:
    """The words of an element, for fingerprints and text diffs."""
    kind = el["kind"]
    if kind == "text":
        return "\n".join(run_text(p["runs"]) for p in el["paragraphs"])
    if kind == "table":
        return "\n".join("\t".join(run_text(cell).strip() for cell in row) for row in el["cells"])
    if kind == "diagram":
        return "\n".join(" ".join(run_text(runs).strip() for runs in n["paragraphs"]) for n in el["nodes"])
    if kind == "image" and el.get("number"):
        return el["number"]["text"]
    return ""


def slide_title(slide: dict) -> str:
    return next((plain_text(e) for e in slide["elements"] if e["kind"] == "text" and e.get("role") == "title"), "")


def slide_text(slide: dict) -> str:
    return " ".join(plain_text(e) for e in slide["elements"])


def slide_info(slide: dict) -> dict:
    return {"label": slide.get("label"), "title": slide_title(slide), "text": slide_text(slide), "page": slide["page"]}


def fresh_slide_key(info: dict, taken: set[str]) -> str:
    if info.get("label"):
        key = info["label"]
    elif norm_title(info.get("title") or ""):
        stem = f"title:{norm_title(info['title'])}"
        key = next(f"{stem}#{k}" for k in range(1, 10 ** 6) if f"{stem}#{k}" not in taken)
    else:
        key = f"page:{info['page'] + 1}"
    base, k = key, 2
    while key in taken:
        key, k = f"{base}~{k}", k + 1
    return key


def slide_keys(infos: list[dict]) -> list[str]:
    """Keys of a first conversion (nothing to inherit)."""
    taken: set[str] = set()
    out = []
    for info in infos:
        out.append(fresh_slide_key(info, taken))
        taken.add(out[-1])
    return out


def slide_similarity(a: dict, b: dict) -> float:
    ratio = SequenceMatcher(None, a["text"].split(), b["text"].split(), autojunk=False).ratio()
    same_title = bool(norm_title(a["title"])) and norm_title(a["title"]) == norm_title(b["title"])
    return ratio + (0.5 if same_title else 0.0)


def align_slides(base: list[dict], ours: list[dict]) -> dict[int, int]:
    """ours index -> base index. Labelled frames pair by label wherever they moved; the others by
    an order-keeping alignment on (title, text) similarity, so an inserted frame shifts nothing."""
    pairs: dict[int, int] = {}
    base_labels = {b["label"]: i for i, b in enumerate(base) if b.get("label")}
    for j, o in enumerate(ours):
        if o.get("label") and o["label"] in base_labels:
            pairs[j] = base_labels[o["label"]]
    # Unpaired slides: a labelled one can still match an unlabelled one (a label added or removed).
    bs = [i for i in range(len(base)) if i not in pairs.values()]
    os_ = [j for j in range(len(ours)) if j not in pairs]
    m, n = len(bs), len(os_)
    sim = [[slide_similarity(base[bs[a]], ours[os_[b]]) for b in range(n)] for a in range(m)]
    score = [[0.0] * (n + 1) for _ in range(m + 1)]
    for a in range(m - 1, -1, -1):
        for b in range(n - 1, -1, -1):
            best = max(score[a + 1][b], score[a][b + 1])
            if sim[a][b] >= SLIDE_MATCH and not (base[bs[a]].get("label") and ours[os_[b]].get("label")):
                best = max(best, sim[a][b] + score[a + 1][b + 1])
            score[a][b] = best
    a = b = 0
    while a < m and b < n:
        s = sim[a][b]
        if s >= SLIDE_MATCH and not (base[bs[a]].get("label") and ours[os_[b]].get("label")) \
                and abs(score[a][b] - (s + score[a + 1][b + 1])) < 1e-9:
            pairs[os_[b]] = bs[a]
            a, b = a + 1, b + 1
        elif score[a + 1][b] >= score[a][b + 1]:
            a += 1
        else:
            b += 1
    return pairs


def inherit_slide_keys(base: list[dict], base_keys: list[str], ours: list[dict]) -> tuple[list[str], dict[int, int]]:
    """Keys for ours slides (matched ones inherit the base key) and the match (ours -> base index)."""
    pairs = align_slides(base, ours)
    taken = set(base_keys)
    keys = []
    for j, info in enumerate(ours):
        if j in pairs:
            keys.append(base_keys[pairs[j]])
        else:
            keys.append(fresh_slide_key(info, taken))
            taken.add(keys[-1])
    return keys, pairs


# ---------------------------------------------------------------- elements

def image_sha1(el: dict, out: Path | None) -> str | None:
    if el["kind"] != "image" or not el.get("file") or out is None:
        return None
    path = out / el["file"]
    return sha1(path.read_bytes()) if path.exists() else None


def fingerprint(el: dict, out: Path | None = None, anchor_key: str | None = None) -> dict:
    return {"text": plain_text(el), "bbox": [round(v, 2) for v in el["bbox"]], "image_sha1": image_sha1(el, out),
            "anchor": anchor_key}


def default_keys(elements: list[dict]) -> list[str]:
    counts: dict[str, int] = {}
    out = []
    for el in elements:
        stem = f"{el['kind']}/{el.get('role') or 'none'}"
        out.append(f"{stem}/{counts.get(stem, 0)}")
        counts[stem] = counts.get(stem, 0) + 1
    return out


def _geometry(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    iou = ix * iy / union if union > 0 else 0.0
    dist = ((a[0] + a[2] - b[0] - b[2]) ** 2 + (a[1] + a[3] - b[1] - b[3]) ** 2) ** 0.5 / 2
    return max(iou, max(0.0, 1 - dist / 60))


def element_similarity(a: dict, b: dict) -> float:
    """a, b: {"kind", "role", "fingerprint"}."""
    if a["kind"] != b["kind"]:
        return 0.0
    fa, fb = a["fingerprint"], b["fingerprint"]
    geom = _geometry(fa["bbox"], fb["bbox"])
    role = 1.0 if a.get("role") == b.get("role") else 0.0
    if fa["text"] or fb["text"]:
        ratio = SequenceMatcher(None, fa["text"], fb["text"], autojunk=False).ratio()
        score = 0.6 * ratio + 0.3 * geom + 0.1 * role
        if role and a.get("role") in UNIQUE_ROLES:  # a renamed title is still the slide's title
            score = max(score, 0.5 + 0.3 * geom + 0.2 * ratio)
        return score
    if a["kind"] == "image":
        same = 1.0 if fa["image_sha1"] and fa["image_sha1"] == fb["image_sha1"] else 0.0
        anchor = 1.0 if fa.get("anchor") == fb.get("anchor") else 0.0
        return 0.4 * same + 0.3 * geom + 0.2 * anchor + 0.1 * role
    return 0.7 * geom + 0.3 * role


def match_elements(base: list[dict], ours: list[dict], reserved: set[str] = frozenset()) -> list[str]:
    """Keys for ours elements ({"kind", "role", "fingerprint"}), given base elements ({"key",
    "kind", "role", "fingerprint"}): most similar pairs first (the key an element would get anyway
    counts slightly more and needs KEY_MATCH, others ELEMENT_MATCH), else a fresh ordinal.
    `reserved` keys are taken already."""
    by_key = {b["key"]: b for b in base if b["key"] not in reserved}
    keys: list[str | None] = [None] * len(ours)
    used: set[str] = set(reserved)
    defaults = default_keys(ours)
    candidates = []
    for j, o in enumerate(ours):
        for b in by_key.values():
            s = element_similarity(o, b)
            if s >= (KEY_MATCH if b["key"] == defaults[j] else ELEMENT_MATCH):
                candidates.append((s + (0.05 if b["key"] == defaults[j] else 0.0), j, b["key"]))
    for s, j, key in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
        if keys[j] is None and key not in used:
            keys[j] = key
            used.add(key)
    ordinals: dict[str, int] = {}
    for key in [b["key"] for b in base] + list(used):
        stem, _, n = key.rpartition("/")
        ordinals[stem] = max(ordinals.get(stem, 0), int(n) + 1 if n.isdigit() else 0)
    for j, o in enumerate(ours):
        if keys[j] is None:
            stem = f"{o['kind']}/{o.get('role') or 'none'}"
            keys[j] = f"{stem}/{ordinals.get(stem, 0)}"
            ordinals[stem] = ordinals.get(stem, 0) + 1
    return keys


def slide_element_keys(elements: list[dict], out: Path | None, base: list[dict] | None = None) -> tuple[list[str], list[dict]]:
    """Keys and fingerprints of a slide's IR elements; with `base` (the matched base slide's
    elements: {"key", "kind", "role", "fingerprint"}) keys are inherited. Elements anchored to
    text are matched after their anchors, with the anchor's key in their fingerprint."""
    ids = {e["id"]: i for i, e in enumerate(elements)}
    fps = [fingerprint(e, out) for e in elements]
    if base is None:
        keys = default_keys(elements)
        for e, fp in zip(elements, fps):
            if e.get("anchor") in ids:
                fp["anchor"] = keys[ids[e["anchor"]]]
        return keys, fps
    items = [{"kind": e["kind"], "role": e.get("role"), "fingerprint": fp} for e, fp in zip(elements, fps)]
    free = [i for i, e in enumerate(elements) if e.get("anchor") not in ids]
    anchored = [i for i, e in enumerate(elements) if e.get("anchor") in ids]
    keys = [""] * len(elements)
    for i, k in zip(free, match_elements([b for b in base if not b["fingerprint"].get("anchor")], [items[i] for i in free])):
        keys[i] = k
    for i in anchored:
        fps[i]["anchor"] = keys[ids[elements[i]["anchor"]]]
    got = match_elements([b for b in base if b["fingerprint"].get("anchor")], [items[i] for i in anchored],
                         {keys[i] for i in free} | {b["key"] for b in base if not b["fingerprint"].get("anchor")})
    for i, k in zip(anchored, got):
        keys[i] = k
    return keys, fps


# ---------------------------------------------------------------- IR hashes

def normalise_ir(value, anchor_key: str | None = None, page_key=None):
    """The element's IR without ids and page-specific numbering (links to pages become slide keys)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in DROP_KEYS:
                continue
            if k == "anchor":
                out[k] = anchor_key
            else:
                out[k] = normalise_ir(v, anchor_key, page_key)
        return out
    if isinstance(value, list):
        return [normalise_ir(v, anchor_key, page_key) for v in value]
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, str):
        if value.startswith("#page=") and value[6:].isdigit() and page_key:
            return f"#slide={page_key(int(value[6:]))}"
        m = ID_LIKE.fullmatch(value)
        return m.group(1) if m else value
    return value


def _styles(value, found: set) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k in STYLE_KEYS and not isinstance(v, (dict, list)):
                found.add((k, json.dumps(v)))
            elif k in STYLE_KEYS:
                found.add((k, json.dumps(v, sort_keys=True)))
            elif k not in ("bbox", "lines", "text"):
                _styles(v, found)
    elif isinstance(value, list):
        for v in value:
            _styles(v, found)


def ir_fields(el: dict, out: Path | None = None, anchor_key: str | None = None, page_key=None) -> tuple[str, dict]:
    """(ir_hash, {"text", "position", "size", "style", "image"} field hashes) of an element. A
    reworded line changes its size, not its position."""
    norm = normalise_ir(el, anchor_key, page_key)
    image = image_sha1(el, out)
    whole = sha1(json.dumps([norm, image], sort_keys=True, ensure_ascii=False))[:16]
    styles: set = set()
    _styles(norm, styles)
    x0, y0, x1, y1 = el["bbox"]
    fields = {
        "text": sha1(plain_text(el))[:12],
        "position": sha1(json.dumps([round(2 * x0) / 2, round(2 * y0) / 2]))[:12],
        "size": sha1(json.dumps([round(2 * (x1 - x0)) / 2, round(2 * (y1 - y0)) / 2]))[:12],
        "style": sha1(json.dumps(sorted(styles)))[:12],
        "image": (image or "")[:12],
    }
    return whole, fields


def source_changes(base_el: dict, ours_el: dict) -> set[str]:
    """Fields the source changed ({"text", "position", "size", "style", "image"}, or {"layout"} when only
    something else in the IR differs); empty when the IR hash is the same."""
    if base_el["ir_hash"] == ours_el["ir_hash"]:
        return set()
    changed = {k for k, v in ours_el["fields"].items() if base_el["fields"].get(k) != v}
    return changed or {"layout"}
