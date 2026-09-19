"""Random torture PDFs for the pure renderer's transparency (`pdf/pure/render_transparency.py`):
soft masks (luminosity and alpha, /BC), transparency-group forms (isolated or not), constant
alpha on forms and groups, nested in each other, each page rendered by PDFium and by the pure
reader, any pixel that differs a failure shrunk to the lines that still make it differ.

    python tools/render_torture_transparency.py [seed0] [n] [--page] [--out DIR]

A case is a list of items, each a form XObject (/X<k>, random /BBox, /Matrix and /Group) or a
soft-mask group (/G of the ExtGState /S<k>); an item's content may call the forms and set the
soft masks listed before it, so nothing refers to itself. Pages and items are `render_torture`'s
random paths plus `/S<k> gs` (and `/SN gs`, /SMask /None) and constant alpha before `Do`."""

from __future__ import annotations

import argparse
import random
from pathlib import Path

import numpy as np

from .render_torture import EXTGS, num, random_cm, random_geometry, random_path


BLENDS = (b"Normal", b"Multiply", b"Screen", b"Overlay", b"Darken", b"Lighten", b"ColorDodge",
          b"ColorBurn", b"HardLight", b"SoftLight", b"Difference", b"Exclusion", b"Hue",
          b"Saturation", b"Color", b"Luminosity", b"Compatible")


def pdf_bytes(page: bytes, items=(), media=(0, 0, 200, 150), page_entries: bytes = b"") -> bytes:
    """A one-page PDF. `items`: [(kind, entries, content, smask)] with kind "X" (a form, /X<k>)
    or "S" (a soft mask: its /G form; `smask` = b"/S /Luminosity /BC [...]" entries)."""
    objs: list[bytes] = []
    n_items = len(items)
    ids = list(range(1, n_items + 1))           # item k is object k + 1

    def resources(upto: int) -> bytes:
        xs = b" ".join(b"/X%d %d 0 R" % (k, ids[k]) for k in range(upto) if items[k][0] == "X")
        ss = b" ".join(b"/S%d << /SMask << /Type /Mask %s /G %d 0 R >> >>" % (k, items[k][3], ids[k])
                       for k in range(upto) if items[k][0] == "S")
        bm = b" ".join(b"/B%d << /BM /%s >>" % (k, m) for k, m in enumerate(BLENDS))
        gs = EXTGS[:-3] + b" /SN << /SMask /None >> " + bm + b" " + ss + b" >>"
        return b"<< " + gs + b" /XObject << " + xs + b" >> >>"

    for k, (kind, entries, content, _) in enumerate(items):
        objs.append(b"<< /Type /XObject /Subtype /Form /Resources %s %s /Length %d >>\nstream\n"
                    % (resources(k), entries, len(content)) + content + b"\nendstream")
    content_id = len(objs) + 1
    objs.append(b"<< /Length %d >>\nstream\n" % len(page) + page + b"\nendstream")
    page_id, pages_id, cat_id = content_id + 1, content_id + 2, content_id + 3
    box = b"[%s]" % b" ".join(b"%g" % v for v in media)
    objs.append(b"<< /Type /Page /Parent %d 0 R /MediaBox %s %s /Resources %s /Contents %d 0 R >>"
                % (pages_id, box, page_entries, resources(n_items), content_id))
    objs.append(b"<< /Type /Pages /Kids [%d 0 R] /Count 1 >>" % page_id)
    objs.append(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out = bytearray(b"%PDF-1.7\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, cat_id, x)
    return bytes(out)


def _colour(r: random.Random) -> bytes:
    k = r.random()
    if k < 0.3:
        return b"%.3f g %.3f G" % (r.random(), r.random())
    if k < 0.45:
        return b"%.3f %.3f %.3f %.3f k %.3f %.3f %.3f %.3f K" % tuple(r.random() for _ in range(8))
    return b"%.3f %.3f %.3f rg %.3f %.3f %.3f RG" % tuple(r.random() for _ in range(6))


def random_content(r: random.Random, items, upto: int) -> bytes:
    """A few `q ... Q` groups: paths as in render_torture, soft masks and alphas set before
    painting or calling a form."""
    forms = [k for k in range(upto) if items[k][0] == "X"]
    masks = [k for k in range(upto) if items[k][0] == "S"]
    out = []
    if r.random() < 0.5:
        # something under what follows: a blend over nothing drawn (the page's white is the
        # caller's fill, not content) is a plain copy
        out.append(b"q\n%s\n%s %s %s %s re f\nQ" % (_colour(r), num(r, 0.3), num(r, 0.3),
                                                   num(r), num(r)))
    for _ in range(r.randint(1, 5)):
        g = [b"q"]
        if r.random() < 0.3:
            g.append(random_cm(r))
        if r.random() < 0.25:
            g.append(random_path(r) + r.choice([b" W n", b" W* n"]))
        if masks and r.random() < 0.45:
            g.append(b"/S%d gs" % r.choice(masks))
            if r.random() < 0.15:
                g.append(b"/SN gs")
        if r.random() < 0.3:
            g.append(b"/A%d gs" % r.randint(0, 4))
        if r.random() < 0.35:
            g.append(b"/B%d gs" % r.randrange(len(BLENDS)))
        if r.random() < 0.3:
            g.append(random_cm(r))
        g.append(_colour(r))
        if forms and r.random() < 0.45:
            g.append(b"%.3f w" % r.choice([0, 1, 3]))
            g.append(b"/X%d Do" % r.choice(forms))
        else:
            g.append(b"%.3f w %d J %d j" % (r.choice([0, 0.4, 1, 2.5, 8]), r.randint(0, 2), r.randint(0, 2)))
            if r.random() < 0.1:
                g.append(b"[3 2] 0 d")
            if r.random() < 0.3 and r.random() < 0.5:
                g.append(b"%s %s %s %s re f" % (num(r), num(r), num(r, 0.5), num(r, 0.5)))
            else:
                g.append(random_path(r) + b" " + r.choice([b"f", b"f*", b"S", b"s", b"B", b"B*", b"b", b"F"]))
        g.append(b"Q")
        out.append(b"\n".join(g))
    return b"\n".join(out)


def _bbox(r: random.Random) -> bytes:
    if r.random() < 0.3:
        return b"/BBox [0 0 200 150]"
    return b"/BBox [%s %s %s %s]" % (num(r), num(r), num(r), num(r))


def _matrix(r: random.Random) -> bytes:
    if r.random() < 0.5:
        return b""
    m = [r.choice([1, 0, -1, r.uniform(-2, 2)]) for _ in range(4)] + [r.uniform(-60, 60), r.uniform(-60, 60)]
    return b" /Matrix [" + b" ".join(b"%.4f" % v for v in m) + b"]"


def random_items(r: random.Random) -> list:
    items: list = []
    for k in range(r.choice([1, 2, 2, 3, 4, 5])):
        if r.random() < 0.5:
            entries = _bbox(r) + _matrix(r)
            k2 = r.random()
            if k2 < 0.3:
                entries += b" /Group << /S /Transparency >>"
            elif k2 < 0.55:
                entries += b" /Group << /S /Transparency /I true%s >>" % r.choice([b"", b" /K true", b" /CS /DeviceRGB"])
            elif k2 < 0.6:
                entries += b" /Group << /I true >>"
            items.append(["X", entries, b"", b""])
        else:
            lum = r.random() < 0.6
            cs = r.choice([b"/DeviceGray", b"/DeviceRGB", b"/DeviceCMYK", b"/DeviceGray"])
            entries = _bbox(r) + _matrix(r)
            if r.random() < 0.85:
                entries += b" /Group << /S /Transparency /CS %s%s >>" % (cs, r.choice([b"", b" /I true"]))
            sm = b"/S /Luminosity" if lum else b"/S /Alpha"
            if lum and r.random() < 0.4:
                n = {b"/DeviceGray": 1, b"/DeviceRGB": 3, b"/DeviceCMYK": 4}[cs]
                sm += b" /BC [" + b" ".join(b"%.3f" % r.random() for _ in range(r.choice([n, n, 1, 5]))) + b"]"
            items.append(["S", entries, b"", sm])
        items[-1][2] = random_content(r, items, k)
    return [tuple(it) for it in items]


def case(seed: int, page: bool = False):
    r = random.Random(seed)
    items = random_items(r)
    content = random_content(r, items, len(items))
    zoom, transparent = r.choice([0.5, 1, 1.37, 2]), r.random() < 0.3
    geometry = random_geometry(r) if page else {}
    if r.random() < 0.3:
        geometry["page_entries"] = geometry.get("page_entries", b"") + b" /Group << /S /Transparency /CS /DeviceRGB >>"
    return content, items, zoom, transparent, geometry


def compare(content: bytes, zoom: float, transparent: bool, items=(), geometry=None):
    """(pixels that differ, PDFium's render, pure's render, per-pixel max difference)."""
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    g = dict(geometry or {})
    clip = g.pop("clip", None)
    data = pdf_bytes(content, items, **g)
    a = PdfiumBackend().open(data)[0].render(zoom, clip, transparent=transparent)
    b = PureBackend().open(data)[0].render(zoom, clip, transparent=transparent)
    if a.shape != b.shape:
        raise AssertionError(f"shapes {a.shape} != {b.shape}")
    d = np.abs(a.astype(int) - b.astype(int)).max(axis=2)
    return int((d > 0).sum()), a, b, d


def shrink(content: bytes, zoom: float, transparent: bool, items=(), geometry=None):
    """Drop lines (the page's, then each item's) while the difference remains."""
    items = [list(it) for it in items]

    def fails(c, its):
        try:
            return compare(c, zoom, transparent, [tuple(i) for i in its], geometry)[0] > 0
        except Exception:
            return False

    def cut(text, test):
        lines = text.split(b"\n")
        changed = True
        while changed:
            changed = False
            for i in range(len(lines)):
                if lines[i] in (b"q", b"Q"):
                    continue
                trial = lines[:i] + lines[i + 1:]
                if test(b"\n".join(trial)):
                    lines, changed = trial, True
                    break
        return b"\n".join(lines)

    content = cut(content, lambda c: fails(c, items))
    for k in range(len(items)):
        def test(c, k=k):
            trial = [list(i) for i in items]
            trial[k][2] = c
            return fails(content, trial)
        items[k][2] = cut(items[k][2], test)
    return content, [tuple(i) for i in items]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("seed0", type=int, nargs="?", default=0)
    ap.add_argument("n", type=int, nargs="?", default=200)
    ap.add_argument("--page", action="store_true", help="random media/crop boxes, /Rotate and render clips")
    ap.add_argument("--out", default="out/render-torture-transparency")
    ap.add_argument("--no-shrink", action="store_true")
    args = ap.parse_args(argv)
    from ..pdf.api import PdfError
    out = Path(args.out)
    fails = refused = 0
    for seed in range(args.seed0, args.seed0 + args.n):
        content, items, zoom, transparent, geometry = case(seed, args.page)
        try:
            npx = compare(content, zoom, transparent, items, geometry)[0]
        except PdfError as e:
            refused += 1
            print("seed", seed, "refused:", e)
            continue
        except Exception as e:
            print("seed", seed, "EXC", type(e).__name__, e)
            fails += 1
            continue
        if not npx:
            continue
        fails += 1
        if args.no_shrink:
            print(f"seed {seed}: {npx} px")
            continue
        small, sitems = shrink(content, zoom, transparent, items, geometry)
        npx, a, b, d = compare(small, zoom, transparent, sitems, geometry)
        print(f"seed {seed} zoom {zoom} transparent {transparent} {geometry}: {npx} px, max {d.max()}")
        for k, (kind, e, c, sm) in enumerate(sitems):
            print(f"-- {kind}{k} {e.decode()} {sm.decode()}\n{c.decode()}")
        print("-- page\n" + small.decode())
        out.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        vis = np.concatenate([a[..., :3], b[..., :3], np.stack([np.where(d > 0, 255, 0)] * 3, -1)], 1)
        Image.fromarray(vis.astype(np.uint8)).save(out / f"seed{seed}.png")
        g = {k: v for k, v in geometry.items() if k != "clip"}
        (out / f"seed{seed}.pdf").write_bytes(pdf_bytes(small, sitems, **g))
    print(f"seeds {args.seed0}..{args.seed0 + args.n - 1}: {fails} failed, {refused} refused")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
