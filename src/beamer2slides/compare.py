"""IR vs IR: what differs between a current deck (classify of a candidate source) and a target
(classify of another PDF, a synthetic edit, or `deck_ir` of a live Slides deck).

Both sides are deck.json-shaped. Text is compared by paragraph across the whole slide (the
classifier may group paragraphs into boxes differently than the deck does), elements by the
paragraphs they hold; pictures and shapes by kind, box and picture hash.

Residual kinds: slide_missing, slide_extra, slide_order, notes, background, paragraph_missing,
paragraph_extra, paragraph_order, text, style, bullet, align, element_missing, element_extra,
geometry, image, shape, table. Every residual carries `within` (inside the tolerance).
"""

import difflib
import math
import re
import unicodedata
from dataclasses import dataclass, field

from .classify import FRAME_COUNTER_RE
from .emit import merge_blocks
from .fonts import google_font

TOL = {
    "pos": 2.0,          # PDF pt: text anchors, picture edges
    "size": 3.0,         # PDF pt: picture width and height
    "font": 0.06,        # relative font size
    "color": 24,         # summed RGB channel difference (0..765)
    "phash": 0.15,       # mean abs difference of 16x16 grey thumbnails (0..1)
    "inline_phash": 0.3,  # the same for formula/icon pictures (transparent, on coloured panels)
}
IGNORED_ROLES = {"footer", "math", "icon", "overlay", "highlight"}
NORMALISE = str.maketrans({"\u00a0": " ", "\u2009": " ", "\u202f": " ", "\t": " ", "\x0b": " ", "“": '"', "”": '"',
                           "‘": "'", "’": "'", "…": "...", "−": "-", "​": ""})
HOLE ="\ue000"


def norm_text(t: str) -> str:
    return " ".join(unicodedata.normalize("NFC", t.translate(NORMALISE)).split())


def run_text(r: dict) -> str:
    return HOLE if r.get("hole") else r["text"]


def para_text(p: dict) -> str:
    return "".join(run_text(r) for r in p["runs"])


def element_text(el: dict) -> str:
    return "\n".join(para_text(p) for p in el.get("paragraphs", []))


def similarity(a: str, b: str) -> float:
    wa, wb = norm_text(a).split(), norm_text(b).split()
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def colour_distance(a: str | None, b: str | None) -> int:
    if not a or not b:
        return 0 if a == b else 765
    a, b = a.lstrip("#"), b.lstrip("#")
    return sum(abs(int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) for i in (0, 2, 4))


# ---------------------------------------------------------------- geometry

def text_left(el: dict) -> float:
    """Left edge of an element's text as emit places it (bullets included)."""
    ps = el["paragraphs"]
    return min(p["bullet"]["bbox"][0] if p.get("bullet") and p["bullet"].get("bbox") else
               min([p["text_x0"]] + ([l["x0"] for l in p["lines"]] if p["align"] != "left" else [])) for p in ps)


def text_anchor(el: dict) -> tuple[float, float]:
    """(x, first baseline) in PDF pt; x is the left edge, the centre or the right edge by the
    paragraphs' alignment. `deck_ir` sets `anchor` from the Slides box instead."""
    if el.get("anchor"):
        return tuple(el["anchor"])
    ps = el["paragraphs"]
    aligns = {p["align"] for p in ps}
    left = text_left(el)
    right = max(l["x1"] for p in ps for l in p["lines"])
    x = (left + right) / 2 if aligns == {"center"} else right if aligns == {"right"} else left
    return x, ps[0]["lines"][0]["baseline"]


def anchor_align(el: dict) -> str:
    aligns = {p["align"] for p in el["paragraphs"]}
    return aligns.pop() if len(aligns) == 1 else "left"


# ---------------------------------------------------------------- alignment helpers

def align_sequences(a: list, b: list, sim, threshold: float = 0.5) -> list[tuple[int | None, int | None]]:
    """Order-preserving alignment (Needleman-Wunsch) maximising summed similarity over pairs
    at or above `threshold`."""
    n, m = len(a), len(b)
    s = [[sim(x, y) for y in b] for x in a]
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best = max(score[i + 1][j], score[i][j + 1])
            if s[i][j] >= threshold:
                best = max(best, s[i][j] + score[i + 1][j + 1])
            score[i][j] = best
    out, i, j = [], 0, 0
    while i < n and j < m:
        if s[i][j] >= threshold and abs(score[i][j] - (s[i][j] + score[i + 1][j + 1])) < 1e-9:
            out.append((i, j))
            i, j = i + 1, j + 1
        elif abs(score[i][j] - score[i + 1][j]) < 1e-9:
            out.append((i, None))
            i += 1
        else:
            out.append((None, j))
            j += 1
    out += [(k, None) for k in range(i, n)] + [(None, k) for k in range(j, m)]
    return out


def longest_increasing(values: list[int]) -> set[int]:
    """Indices (into values) of one longest strictly increasing subsequence."""
    if not values:
        return set()
    tails, prev, idx = [], [-1] * len(values), []
    import bisect
    for i, v in enumerate(values):
        k = bisect.bisect_left([values[t] for t in tails], v)
        if k == len(tails):
            tails.append(i)
        else:
            tails[k] = i
        prev[i] = tails[k - 1] if k else -1
    out, i = set(), tails[-1]
    while i >= 0:
        out.add(i)
        i = prev[i]
    return out


# ---------------------------------------------------------------- slides

def slide_title(slide: dict) -> str:
    for e in slide["elements"]:
        if e["kind"] == "text" and e.get("role") == "title":
            return norm_text(element_text(e))
    return ""


def slide_fingerprint(slide: dict) -> str:
    return " ".join(norm_text(element_text(e)) for e in slide["elements"]
                    if e["kind"] == "text" and e.get("role") not in IGNORED_ROLES)


def slide_similarity(a: dict, b: dict) -> float:
    ta, tb = slide_title(a), slide_title(b)
    title = similarity(ta, tb) if (ta or tb) else 0.5
    body = similarity(slide_fingerprint(a), slide_fingerprint(b))
    kinds = lambda s: sorted(e["kind"] for e in s["elements"] if e.get("role") not in IGNORED_ROLES)
    shape = difflib.SequenceMatcher(None, kinds(a), kinds(b)).ratio()
    return 0.45 * title + 0.4 * body + 0.15 * shape


def match_slides(cur: list[dict], tgt: list[dict]) -> list[tuple[int | None, int | None]]:
    """Pairs (current index, target index): equal keys first, the rest by an order-preserving
    alignment of title and text similarity."""
    pairs: dict[int, int] = {}
    ckeys = {s.get("key"): i for i, s in enumerate(cur) if s.get("key")}
    for j, s in enumerate(tgt):
        if s.get("key") and s["key"] in ckeys and ckeys[s["key"]] not in pairs:
            pairs[ckeys[s["key"]]] = j
    free_c = [i for i in range(len(cur)) if i not in pairs]
    free_t = [j for j in range(len(tgt)) if j not in pairs.values()]
    for a, b in align_sequences([cur[i] for i in free_c], [tgt[j] for j in free_t], slide_similarity, 0.45):
        if a is not None and b is not None:
            pairs[free_c[a]] = free_t[b]
    # slides moved past others: clearly similar leftovers pair up out of order (slide_order)
    free_c = [i for i in range(len(cur)) if i not in pairs]
    free_t = [j for j in range(len(tgt)) if j not in pairs.values()]
    cands = sorted(((slide_similarity(cur[i], tgt[j]), i, j) for i in free_c for j in free_t), reverse=True)
    for score, i, j in cands:
        if score >= 0.6 and i not in pairs and j not in pairs.values():
            pairs[i] = j
    out = [(i, j) for i, j in pairs.items()]
    out += [(i, None) for i in range(len(cur)) if i not in pairs]
    out += [(None, j) for j in range(len(tgt)) if j not in pairs.values()]
    return out


# ---------------------------------------------------------------- paragraphs

@dataclass
class Para:
    el: dict
    ei: int        # element index in the slide
    pi: int        # paragraph index in the element
    order: int     # reading order on the slide
    text: str

    @property
    def p(self) -> dict:
        return self.el["paragraphs"][self.pi]


def counts_as_text(el: dict) -> bool:
    """Text elements that are compared: not footers (frame counters "3 / 9" included, which a live
    deck without keys can't tell apart), formulas or overlays."""
    if el["kind"] != "text" or el.get("role") in IGNORED_ROLES:
        return False
    return not FRAME_COUNTER_RE.match(norm_text(element_text(el)))


def reading_order(slide: dict) -> list[int]:
    """Text element indices, title first, then top to bottom and left to right (a live deck lists
    its elements in z-order)."""
    idx = [i for i, e in enumerate(slide["elements"]) if counts_as_text(e) and e.get("paragraphs")]

    def key(i: int):
        e = slide["elements"][i]
        try:
            x, y = text_anchor(e)
        except (KeyError, IndexError, TypeError, ValueError):
            x, y = e["bbox"][0], e["bbox"][1]
        return (e.get("role") != "title", round((y or 0) / 4), x or 0)
    return sorted(idx, key=key)


def slide_paragraphs(slide: dict) -> list[Para]:
    out = []
    for ei in reading_order(slide):
        el = slide["elements"][ei]
        for pi, p in enumerate(el["paragraphs"]):
            text = norm_text(para_text(p))
            if text.replace(HOLE, "").strip():
                out.append(Para(el, ei, pi, len(out), text))
    return out


def match_paragraphs(cur: list[Para], tgt: list[Para]) -> list[tuple[int, int, float]]:
    """Greedy assignment by word similarity (ties: same title role, reading order, position)."""
    cands = []
    for i, a in enumerate(cur):
        for j, b in enumerate(tgt):
            r = similarity(a.text, b.text)
            if r < 0.34 and not (a.text and b.text and len(a.text.split()) <= 3 and
                                 difflib.SequenceMatcher(None, a.text, b.text).ratio() >= 0.6):
                continue
            role = 0.1 if (a.el.get("role") == "title") == (b.el.get("role") == "title") else -0.3
            order = 0.05 * (1 - min(1.0, abs(a.order / max(1, len(cur)) - b.order / max(1, len(tgt))) * 2))
            cands.append((r + role + order, i, j, r))
    cands.sort(reverse=True)
    used_c, used_t, out = set(), set(), []
    for score, i, j, r in cands:
        if i in used_c or j in used_t:
            continue
        used_c.add(i)
        used_t.add(j)
        out.append((i, j, r))
    return sorted(out, key=lambda t: t[1])


STYLE_FIELDS = ("bold", "italic", "underline", "color", "size", "mono")


def char_styles(p: dict) -> tuple[str, list[dict]]:
    text, styles = "", []
    for r in p["runs"]:
        t = run_text(r)
        google = google_font(r.get("font") or "")  # emit writes a Google font's own weight and slant
        st = {"bold": bool(r.get("bold")) or bool(google and google[1] >= 600),
              "italic": bool(r.get("italic")) or bool(google and google[2]), "underline": bool(r.get("underline")),
              "color": (r.get("color") or "#000000").lower(), "size": r.get("size") or p.get("size"),
              "mono": r.get("family") == "mono", "link": r.get("link"), "script": bool(r.get("script"))}
        text += t
        styles += [st] * len(t)
    return text, styles


def style_diffs(cp: dict, tp: dict, tol: dict) -> list[dict]:
    """Ranges of equal text whose style differs: [{field, cur, tgt, t0, t1, c0, c1, text}]
    (character offsets in the target and the current paragraph text)."""
    ct, cs = char_styles(cp)
    tt, ts = char_styles(tp)
    sm = difflib.SequenceMatcher(None, ct, tt, autojunk=False)
    out = []

    def off(k: int, fld: str, a: int, b: int) -> bool:
        c, t = cs[a + k], ts[b + k]
        if tt[b + k] == HOLE or (fld == "size" and (c["script"] or t["script"])):
            return False
        return differs(fld, c[fld], t[fld], tol)

    for a, b, n in sm.get_matching_blocks():
        for fld in STYLE_FIELDS:
            k = 0
            while k < n:
                c, t = cs[a + k], ts[b + k]
                if not off(k, fld, a, b) or tt[b + k].isspace():
                    k += 1
                    continue
                start = k
                while k < n and (off(k, fld, a, b) or tt[b + k].isspace()):
                    k += 1
                end = k
                while end > start and tt[b + end - 1].isspace():
                    end -= 1
                out.append({"field": fld, "cur": c[fld], "tgt": t[fld], "t0": b + start, "t1": b + end,
                            "c0": a + start, "c1": a + end, "text": tt[b + start:b + end]})
    return out


def differs(fld: str, c, t, tol: dict) -> bool:
    if fld == "color":
        return colour_distance(c, t) > tol["color"]
    if fld == "size":
        return bool(c and t) and abs(c - t) > tol["font"] * max(c, t)
    return c != t


def word_diff(a: str, b: str) -> list[dict]:
    wa, wb = a.split(), b.split()
    ops = []
    for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if tag != "equal":
            ops.append({"op": tag, "cur": " ".join(wa[a0:a1]), "tgt": " ".join(wb[b0:b1]), "c": [a0, a1], "t": [b0, b1]})
    return ops


def bullet_sig(p: dict, el: dict | None = None) -> tuple:
    """(bullet or number or None, level). Levels count from the element's shallowest bullet: Slides
    has no absolute level (classify gives a lone picture-bullet list level 1)."""
    b = p.get("bullet")
    if not b:
        return (None, 0)
    base = min((q.get("level", 0) for q in (el or {}).get("paragraphs", []) if q.get("bullet")), default=0)
    return ("number" if b.get("kind") == "number" or str(b.get("text", "")).rstrip(".)").isdigit() else "bullet",
            p.get("level", 0) - base)


# ---------------------------------------------------------------- pictures and shapes

def match_boxes(cur: list[dict], tgt: list[dict], hashes: dict | None = None) -> list[tuple[int, int]]:
    cands = []
    for i, a in enumerate(cur):
        for j, b in enumerate(tgt):
            d = sum(abs(x - y) for x, y in zip(a["bbox"], b["bbox"])) / 4
            aw, ah = a["bbox"][2] - a["bbox"][0], a["bbox"][3] - a["bbox"][1]
            bw, bh = b["bbox"][2] - b["bbox"][0], b["bbox"][3] - b["bbox"][1]
            aspect = abs(math.log(max(aw, 1) / max(ah, 1)) - math.log(max(bw, 1) / max(bh, 1)))
            score = math.exp(-d / 60) + 0.5 * math.exp(-aspect * 4)
            if hashes:
                h = hash_distance(hashes.get(id(a)), hashes.get(id(b)))
                if h is not None:
                    score += 1.0 - min(1.0, h * 4)
            if a.get("fill") and b.get("fill"):
                score += 0.3 * (colour_distance(a["fill"], b["fill"]) <= TOL["color"])
            cands.append((score, i, j))
    cands.sort(reverse=True)
    used_c, used_t, out = set(), set(), []
    for score, i, j in cands:
        if score < 0.45 or i in used_c or j in used_t:
            continue
        used_c.add(i)
        used_t.add(j)
        out.append((i, j))
    return out


def hash_grey(h) -> list[int] | None:
    return h.grey if isinstance(h, PicHash) else h


def hash_coverage(h) -> float:
    return h.coverage if isinstance(h, PicHash) else 1.0


def hash_distance(a, b) -> float | None:
    a, b = hash_grey(a), hash_grey(b)
    if not a or not b or len(a) != len(b):
        return None
    return sum(abs(x - y) for x, y in zip(a, b)) / (255 * len(a))


def picture_differs(a, b, tol: float) -> bool:
    """Two pictures that don't show the same thing. The mean difference of the thumbnails misses a
    replaced figure on white (a curve for bars: 0.11), so opaque pictures with some contrast must
    also correlate. Transparent ones (formula and overlay pictures: the page shows through) are
    judged by the mean alone."""
    d = hash_distance(a, b)
    if d is None:
        return False
    if d > tol:
        return True
    x, y = hash_grey(a), hash_grey(b)
    if d <= 0.03 or min(hash_coverage(a), hash_coverage(b)) < 0.5:
        return False
    mx, my = sum(x) / len(x), sum(y) / len(y)
    vx = sum((v - mx) ** 2 for v in x) / len(x)
    vy = sum((v - my) ** 2 for v in y) / len(y)
    if min(vx, vy) < 36:
        return False
    cov = sum((u - mx) * (v - my) for u, v in zip(x, y)) / len(x)
    return cov / math.sqrt(vx * vy) < 0.75


@dataclass
class PicHash:
    """A 16x16 grey thumbnail (on white) and how much of the picture is opaque."""
    grey: list[int]
    coverage: float = 1.0


def picture_hash(path):
    """Thumbnail of a picture file: survives Google's re-encoding."""
    try:
        from PIL import Image
        img = Image.open(path)
        img.seek(0)
        return pic_hash(img)
    except (OSError, ValueError, EOFError):
        return None


def pic_hash(img) -> PicHash:
    coverage = 1.0
    if img.mode in ("RGBA", "LA", "P"):
        from PIL import Image
        img = img.convert("RGBA")
        small = img.getchannel("A").resize((16, 16), Image.BILINEAR)
        coverage = sum(1 for v in small.getdata() if v > 128) / 256
        ground = Image.new("RGBA", img.size, (255, 255, 255, 255))
        ground.alpha_composite(img)
        img = ground
    return PicHash(grey16(img), coverage)


def grey16(img) -> list[int]:
    from PIL import Image
    small = img.convert("L").resize((16, 16), Image.BILINEAR)
    return [small.getpixel((x, y)) for y in range(16) for x in range(16)]


PICTURE_EDITS = ("crop", "rotation", "flip", "opacity", "brightness", "contrast", "recolor")


def adjusted_picture(img, el: dict):
    """Brightness, contrast and recolour of a Slides picture applied to its pixels (RGBA). Slides'
    own formulas aren't documented. Recolour maps luminance onto the gradient of its stops; then
    contrast scales around mid grey (1 + c, or 1 / (1 - c) above 0) and brightness scales the
    colour: by 1 + b below 0, 1 / (1 - b) above (measured on the adopt corpus' thumbnails: -0.32,
    -0.5 and -0.7 give 0.68, 0.49 and 0.30 x the file's values, 0.34 about 1.48 x, and ap-bio-stats'
    CUSTOM ramp under 0.6 comes out 2.5 x the ramp's colours - an offset of b would black out a photo
    at -0.5 that the deck shows dimmed)."""
    import numpy as np
    from PIL import Image
    arr = np.asarray(img.convert("RGBA"), np.float32) / 255
    rgb, alpha = arr[..., :3], arr[..., 3:]
    stops = sorted((el.get("recolor") or {}).get("stops") or [], key=lambda s: s["position"])
    if stops:
        lum = np.clip(rgb @ np.array([0.299, 0.587, 0.114], np.float32), 0, 1)
        pos = np.array([s["position"] for s in stops], np.float32)
        cols = np.array([[int(s["color"][i:i + 2], 16) / 255 for i in (1, 3, 5)] for s in stops], np.float32)
        rgb = np.stack([np.interp(lum, pos, cols[:, ch]) for ch in range(3)], -1)
    elif (el.get("recolor") or {}).get("name") == "GRAYSCALE":
        lum = rgb @ np.array([0.299, 0.587, 0.114], np.float32)
        rgb = np.repeat(lum[..., None], 3, -1)
    c, b = el.get("contrast") or 0.0, el.get("brightness") or 0.0
    if c:
        k = 1 / max(1e-3, 1 - c) if c > 0 else 1 + c
        rgb = (rgb - 0.5) * k + 0.5
    if b:
        rgb = rgb * (1 + b if b < 0 else 1 / max(1e-3, 1 - b))
    out = np.concatenate([np.clip(rgb, 0, 1), alpha], -1)
    return Image.fromarray((out * 255 + 0.5).astype(np.uint8), "RGBA")


def cropped_picture(img, crop: dict | None):
    if not crop:
        return img
    w, h = img.size
    box = (round(crop["l"] * w), round(crop["t"] * h), round((1 - crop["r"]) * w), round((1 - crop["b"]) * h))
    if box[2] - box[0] < 1 or box[3] - box[1] < 1:
        return img
    return img.crop(box)


def displayed_picture(el: dict):
    """A deck picture as Slides shows it inside its axis-aligned box, on white: crop, adjustments,
    transparency, mirroring and rotation applied to its file. None without a readable file."""
    from PIL import Image, ImageOps
    try:
        img = Image.open(el["file"])
        img.seek(0)
        img = img.convert("RGBA")
    except (OSError, ValueError, KeyError, EOFError):
        return None
    img = cropped_picture(img, el.get("crop"))
    if any(el.get(k) for k in ("brightness", "contrast", "recolor")):
        img = adjusted_picture(img, el)
    box = el.get("box") or el["bbox"]
    w, h = max(1, box[2] - box[0]), max(1, box[3] - box[1])
    size = (max(8, round(4 * w)), max(8, round(4 * h)))  # 4 px per pt, like the candidate's crop
    img = img.resize(size, Image.BILINEAR)
    if el.get("opacity") is not None and el["opacity"] < 1:
        a = img.getchannel("A").point(lambda v: round(v * el["opacity"]))
        img.putalpha(a)
    if el.get("flip"):
        img = ImageOps.mirror(img)
    if el.get("rotation"):
        img = img.rotate(-el["rotation"], resample=Image.BILINEAR, expand=True)
    ground = Image.new("RGBA", img.size, (255, 255, 255, 255))
    ground.alpha_composite(img)
    return ground.convert("RGB")


# ---------------------------------------------------------------- compare

@dataclass
class Comparison:
    slides: list[tuple[int | None, int | None]]
    residuals: list[dict] = field(default_factory=list)
    elements: dict = field(default_factory=dict)     # (ci, element id) -> (ti, target element id)

    def open(self) -> list[dict]:
        return [r for r in self.residuals if not r["within"]]

    def summary(self) -> dict:
        out: dict[str, int] = {}
        for r in self.open():
            out[r["kind"]] = out.get(r["kind"], 0) + 1
        return out


def compare(cur: dict, tgt: dict, tol: dict | None = None, hashes: dict | None = None) -> Comparison:
    """`hashes`: id(element dict) -> picture_hash, for pictures on either side."""
    tol = {**TOL, **(tol or {})}
    cs, ts = cur["slides"], tgt["slides"]
    pairs = match_slides(cs, ts)
    comp = Comparison(pairs)
    res = comp.residuals

    def add(kind: str, within: bool = False, **kw) -> None:
        res.append({"kind": kind, "within": within, **kw})

    matched = sorted((j, i) for i, j in pairs if i is not None and j is not None)
    lis = longest_increasing([i for _, i in matched])
    for k, (j, i) in enumerate(matched):
        if k not in lis:
            add("slide_order", slide=i, target_slide=j)
    for i, j in pairs:
        if i is None:
            add("slide_missing", target_slide=j, title=slide_title(ts[j]))
        elif j is None:
            add("slide_extra", slide=i, title=slide_title(cs[i]))
        else:
            compare_slide(cs[i], ts[j], i, j, tol, add, comp, hashes)
    return comp


def compare_slide(c: dict, t: dict, ci: int, ti: int, tol: dict, add, comp: Comparison, hashes) -> None:
    where = {"slide": ci, "target_slide": ti}
    cn, tn = norm_text(c.get("notes") or ""), norm_text(t.get("notes") or "")
    if cn != tn:
        add("notes", **where, cur=c.get("notes"), tgt=t.get("notes"))
    if t.get("background_color") and c.get("background_color") and \
            colour_distance(c["background_color"], t["background_color"]) > tol["color"]:
        add("background", **where, cur=c["background_color"], tgt=t["background_color"])

    cps, tps = slide_paragraphs(c), slide_paragraphs(t)
    pmatch = match_paragraphs(cps, tps)
    c_of_t = {j: i for i, j, _ in pmatch}
    t_of_c = {i: j for i, j, _ in pmatch}
    for j, tp in enumerate(tps):
        if j not in c_of_t:
            add("paragraph_missing", **where, target_element=tp.el["id"], target_para=tp.pi, text=tp.text,
                after=_previous_match(j, c_of_t, cps))
    for i, cp in enumerate(cps):
        if i not in t_of_c:
            add("paragraph_extra", **where, element=cp.el["id"], para=cp.pi, text=cp.text)

    # paragraph order within the slide (reading order of the matched pairs)
    order = sorted((j, i) for i, j, _ in pmatch)
    lis = longest_increasing([i for _, i in order])
    kept = {order[k][0]: order[k][1] for k in lis}
    for k, (j, i) in enumerate(order):
        if k not in lis:
            add("paragraph_order", **where, element=cps[i].el["id"], para=cps[i].pi,
                target_element=tps[j].el["id"], target_para=tps[j].pi, text=tps[j].text,
                after=_previous_match(j, kept, cps))

    for i, j, r in pmatch:
        cp, tp = cps[i], tps[j]
        pw = {**where, "element": cp.el["id"], "para": cp.pi, "target_element": tp.el["id"], "target_para": tp.pi}
        if cp.text != tp.text:
            add("text", **pw, cur=cp.text, tgt=tp.text, ops=word_diff(cp.text, tp.text))
        titles = cp.el.get("role") == "title" and tp.el.get("role") == "title"
        for d in style_diffs(cp.p, tp.p, tol):
            # a whole title's size or colour is the theme's (a slide added in Slides takes its layout's)
            theme = titles and d["field"] in ("size", "color") and d["t1"] - d["t0"] >= 0.9 * len(tp.text)
            add("style", **pw, **{**d, **({"within": True, "theme": True} if theme else {})})
        if bullet_sig(cp.p, cp.el) != bullet_sig(tp.p, tp.el):
            add("bullet", **pw, cur=bullet_sig(cp.p, cp.el), tgt=bullet_sig(tp.p, tp.el))
        if cp.p["align"] != tp.p["align"] and len(tp.text) > 0:
            add("align", **pw, cur=cp.p["align"], tgt=tp.p["align"],
                within=len(tp.p.get("lines", [])) <= 1 and "center" not in (cp.p["align"], tp.p["align"]))

    # elements: a target text element corresponds to the current element holding most of its paragraphs
    t_elements = [e for e in t["elements"] if counts_as_text(e)]
    c_elements = [e for e in c["elements"] if counts_as_text(e)]
    for te in t_elements:
        mine = [j for j, tp in enumerate(tps) if tp.el is te]
        if not mine:
            continue
        hits = [cps[c_of_t[j]] for j in mine if j in c_of_t]
        if not hits:
            add("element_missing", **where, target_element=te["id"], el_kind="text", text=element_text(te),
                role=te.get("role"))
            continue
        first = cps[c_of_t[mine[0]]] if mine[0] in c_of_t else hits[0]
        ce = first.el
        comp.elements[(ci, ce["id"])] = (ti, te["id"])
        if mine[0] not in c_of_t:
            continue  # its first paragraph is missing: placed once that is there
        (cx, cy), (tx, ty) = text_anchor_for(ce, first.pi), text_anchor(te)
        # a middle- or bottom-aligned box of a foreign deck: where its lines' middle or bottom stand,
        # which the box fixes however they wrap (when the current element is this box's alone)
        own = [k for k, p in enumerate(cps) if p.el is ce]
        stacked = stacked_y(ce, te) if first.pi == 0 and all(t_of_c.get(k) in mine for k in own) else None
        if stacked:
            cy, ty = stacked
        dx, dy = tx - cx, ty - cy
        # frame titles sit where the theme puts them: a moved title is noted, not written back
        theme = te.get("role") == "title" and ce.get("role") == "title"
        add("geometry", **where, element=ce["id"], target_element=te["id"], para=first.pi, dx=round(dx, 2),
            dy=round(dy, 2), cur=[round(cx, 2), round(cy, 2)], tgt=[round(tx, 2), round(ty, 2)],
            align=anchor_align(te), within=theme or (abs(dx) <= tol["pos"] and abs(dy) <= tol["pos"]),
            wrap_width=te.get("wrap_width"), mixed=len({id(h.el) for h in hits}) > 1,
            **({"theme": True} if theme and (abs(dx) > tol["pos"] or abs(dy) > tol["pos"]) else {}))
    for ce in c_elements:
        if not any(p.el is ce and k in t_of_c for k, p in enumerate(cps)) and any(p.el is ce for p in cps):
            add("element_extra", **where, element=ce["id"], el_kind="text", text=element_text(ce))

    for kind in ("image", "shape", "table", "diagram"):
        c_els = merge_blocks(c["elements"]) if kind == "shape" else c["elements"]
        cc = [e for e in c_els if e["kind"] == kind and e.get("role") not in IGNORED_ROLES]
        tt = [e for e in t["elements"] if e["kind"] == kind and e.get("role") not in IGNORED_ROLES]
        got = match_boxes(cc, tt, hashes)
        for a, b in got:
            ce, te = cc[a], tt[b]
            comp.elements[(ci, ce["id"])] = (ti, te["id"])
            cb, tb = ce["bbox"], te["bbox"]
            d = [round(y - x, 2) for x, y in zip(cb, tb)]
            dw, dh = d[2] - d[0], d[3] - d[1]
            if kind == "table":  # Slides sets row heights (cell padding) and column widths: the corner counts
                within = abs(d[0]) <= 1.5 * tol["pos"] and abs(d[1]) <= 1.5 * tol["pos"]
            else:
                within = abs(d[0]) <= tol["pos"] and abs(d[1]) <= tol["pos"] and abs(dw) <= tol["size"] and abs(dh) <= tol["size"]
            add("geometry", **where, element=ce["id"], target_element=te["id"], el_kind=kind, dx=d[0], dy=d[1],
                dw=round(dw, 2), dh=round(dh, 2), cur=cb, tgt=tb, within=within)
            if kind == "diagram" and diagram_text(ce) != diagram_text(te):
                add("diagram", **where, element=ce["id"], target_element=te["id"], cur=diagram_text(ce),
                    tgt=diagram_text(te))
            if kind == "image" and hashes and picture_differs(hashes.get(id(ce)), hashes.get(id(te)), tol["phash"]):
                add("image", **where, element=ce["id"], target_element=te["id"],
                    distance=round(hash_distance(hashes.get(id(ce)), hashes.get(id(te))), 3), file=te.get("file"))
            if kind == "shape" and colour_distance(ce.get("fill"), te.get("fill")) > tol["color"]:
                add("shape", **where, element=ce["id"], target_element=te["id"], cur=ce.get("fill"), tgt=te.get("fill"))
            if kind == "table":
                ct, tt_ = table_text(ce), table_text(te)
                if ct != tt_:
                    add("table", **where, element=ce["id"], target_element=te["id"], cur=ct, tgt=tt_)
        for b, te in enumerate(tt):
            if b not in {y for _, y in got}:
                add("element_missing", **where, target_element=te["id"], el_kind=kind, bbox=te["bbox"], file=te.get("file"))
        for a, ce in enumerate(cc):
            if a not in {x for x, _ in got}:
                add("element_extra", **where, element=ce["id"], el_kind=kind, bbox=ce["bbox"])
    if hashes:  # formula and icon pictures in text lines: a replaced one is reported (never written)
        cm = [e for e in c["elements"] if e["kind"] == "image" and e.get("role") in ("math", "icon")]
        tm = [e for e in t["elements"] if e["kind"] == "image" and e.get("role") in ("math", "icon")]
        for a, b in match_boxes(cm, tm, hashes):
            h = hash_distance(hashes.get(id(cm[a])), hashes.get(id(tm[b])))
            if h is not None and h > tol["inline_phash"]:  # the mean alone: they are transparent
                add("image", **where, element=cm[a]["id"], target_element=tm[b]["id"], distance=round(h, 3),
                    role=cm[a]["role"], file=tm[b].get("file"))


def stacked_y(cur: dict, tgt: dict) -> tuple[float, float] | None:
    """(current, target) y to compare for a target box aligned to its middle or bottom that says the
    span of its lines (`deck_ir.stacked_baseline`): the last baseline of a bottom-aligned box, the
    one halfway between the first and the last of a middle-aligned one. Its first baseline moves
    with every line Slides wraps that TeX does not, or the other way round; these do not. None for
    anything else."""
    box = tgt.get("box") if isinstance(tgt.get("box"), dict) else {}
    span, valign = box.get("span"), box.get("valign")
    if span is None or valign not in ("middle", "bottom") or not tgt.get("anchor"):
        return None
    lines = [ln["baseline"] for p in cur["paragraphs"] for ln in p.get("lines", []) if ln.get("baseline") is not None]
    if not lines:
        return None
    if valign == "bottom":
        return lines[-1], tgt["anchor"][1] + span
    return (lines[0] + lines[-1]) / 2, tgt["anchor"][1] + span / 2


def text_anchor_for(el: dict, pi: int) -> tuple[float, float]:
    if pi == 0 or el.get("anchor"):
        return text_anchor(el)
    sub = {**el, "paragraphs": el["paragraphs"][pi:]}
    return text_anchor(sub)


def table_text(el: dict) -> list[list[str]]:
    """Cell texts: classify's cells are lists of runs, deck_ir's rows plain strings."""
    rows = el.get("rows") or el.get("cells") or []
    return [[norm_text(c if isinstance(c, str) else "".join(run_text(r) for r in c)) for c in row] for row in rows]


def diagram_text(el: dict) -> list[str]:
    """Node and label texts of a diagram, sorted (Slides keeps no order between them)."""
    texts = [norm_text(" ".join("".join(run_text(r) for r in runs) for runs in n.get("paragraphs") or []))
             for n in el.get("nodes", [])]
    return sorted(t for t in texts if t)


def _previous_match(j: int, c_of_t: dict, cps: list[Para]) -> dict | None:
    """The current paragraph matched to the nearest earlier target paragraph."""
    for k in range(j - 1, -1, -1):
        if k in c_of_t:
            cp = cps[c_of_t[k]]
            return {"element": cp.el["id"], "para": cp.pi}
    return None


def residual_line(r: dict) -> str:
    """One line per residual for reports."""
    where = f"slide {r.get('target_slide', r.get('slide', '?'))}"
    k = r["kind"]
    if k == "text":
        ops = "; ".join(f"{o['op']} '{o['cur']}' -> '{o['tgt']}'" for o in r["ops"])
        return f"{where} {r['target_element']} ¶{r['target_para']}: text {ops}"
    if k == "style":
        return f"{where} {r['target_element']} ¶{r['target_para']}: {r['field']} '{r['text']}' {r['cur']} -> {r['tgt']}"
    if k == "geometry":
        return f"{where} {r['target_element']}: moved dx={r['dx']:+.1f} dy={r['dy']:+.1f}" + \
            (f" dw={r['dw']:+.1f} dh={r['dh']:+.1f}" if "dw" in r else "")
    if k in ("paragraph_missing", "paragraph_extra"):
        return f"{where}: {k} '{r['text'][:60]}'"
    if k in ("slide_missing", "slide_extra", "slide_order"):
        return f"{where}: {k} '{r.get('title', '')}'"
    if k in ("element_missing", "element_extra"):
        return f"{where}: {k} {r.get('el_kind')} '{str(r.get('text', r.get('bbox', '')))[:60]}'"
    if k == "image":
        return f"{where} {r['target_element']}: {r.get('role') or 'picture'} replaced (distance {r.get('distance')})"
    return f"{where}: {k} {r.get('cur')!r} -> {r.get('tgt')!r}"


def plain(r: dict) -> dict:
    return {k: v for k, v in r.items()}


def slide_key_of(label: str | None, title: str, n: int) -> str:
    if label:
        return label
    t = re.sub(r"\W+", "-", title.casefold()).strip("-")
    return f"title:{t}#{n}" if t else f"page:{n}"
