"""Random torture PDFs for the pure renderer (`pdf/pure/render.py`): each page is rendered by
PDFium and by the pure reader, and any pixel that differs is a failure, shrunk to the lines that
still make it differ. This is how the path renderer was made exact.

    python tools/render_torture.py [seed0] [n] [--forms] [--page] [--mutate] [--out DIR]

A page is a few `q ... Q` groups: a random `cm`, clip paths (`W n`, `W* n`), fill and stroke
colours, line width/cap/join/miter, dashes, constant alpha (/A0../A4: ca = CA), and one path of
`m l c v y re h` painted with any operator. `--forms` adds up to three form XObjects with random
/BBox and /Matrix, each calling the earlier ones. `--mutate` puts junk tokens into the content
(odd numbers, stray operators, unbalanced `[ << >>`), where what PDFium's content parser makes of
broken syntax decides the pixels. Failures are written to DIR as seedN.pdf (the
shrunk page) and seedN.png (PDFium | pure | difference)."""

from __future__ import annotations

import argparse
import random
from pathlib import Path

import numpy as np

EXTGS = b"/ExtGState << " + b" ".join(
    b"/A%d << /ca %s /CA %s >>" % (k, v, v) for k, v in enumerate([b"0.5", b"0.25", b"0.8", b"0.0", b"1"])) + \
    b" /D0 << /D [[3 2] 1] >> /L0 << /LW 3 /LJ 1 /LC 2 >> >>"


def pdf_bytes(pages: list[bytes], forms=(), media=(0, 0, 200, 150), extra_resources: bytes = b"",
              page_entries: bytes = b"") -> bytes:
    """A PDF with one page per content stream. `forms`: [(dict entries, content)] as /X0, /X1, ...;
    X<k> may call X<j> for j < k. `page_entries` go into every page dictionary (/Rotate, /CropBox)."""
    objs: list[bytes] = []

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)

    xids: list[int] = []
    for entries, content in forms:
        sub = b"<< " + EXTGS + extra_resources + b" /XObject << " + \
            b" ".join(b"/X%d %d 0 R" % (j, x) for j, x in enumerate(xids)) + b" >> >>"
        xids.append(add(b"<< /Type /XObject /Subtype /Form /Resources %s %s /Length %d >>\nstream\n"
                        % (sub, entries, len(content)) + content + b"\nendstream"))
    res = b"<< " + EXTGS + extra_resources + b" /XObject << " + \
        b" ".join(b"/X%d %d 0 R" % (j, x) for j, x in enumerate(xids)) + b" >> >>"
    content_ids = [add(b"<< /Length %d >>\nstream\n" % len(c) + c + b"\nendstream") for c in pages]
    pages_obj = len(objs) + 1 + len(pages)
    box = b"[%s]" % b" ".join(b"%g" % v for v in media)
    kids = [add(b"<< /Type /Page /Parent %d 0 R /MediaBox %s %s /Resources %s /Contents %d 0 R >>"
                % (pages_obj, box, page_entries, res, cid)) for cid in content_ids]
    assert add(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
               + b"] /Count %d >>" % len(kids)) == pages_obj
    cat = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj)
    out = bytearray(b"%PDF-1.7\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, cat, x)
    return bytes(out)


def num(r: random.Random, scale: float = 1.0) -> bytes:
    v = r.choice([r.uniform(-20, 220), r.uniform(0, 200), round(r.uniform(0, 200)), round(r.uniform(0, 200) * 2) / 2,
                  r.uniform(-5000, 5000) if r.random() < 0.05 else r.uniform(40, 160)])
    return b"%.4f" % (v * scale)


def random_path(r: random.Random) -> bytes:
    ops = []
    for _ in range(r.randint(1, 4)):
        x, y = num(r), num(r)
        if r.random() < 0.2:
            ops.append(b"%s %s %s %s re" % (x, y, num(r, 0.3), num(r, 0.3)))
            continue
        ops.append(b"%s %s m" % (x, y))
        for _ in range(r.randint(0, 5)):
            k = r.random()
            if k < 0.45:
                if r.random() < 0.3:  # axis-aligned or repeated points: zero area folds
                    ops.append(r.choice([b"%s %s l" % (x, num(r)), b"%s %s l" % (num(r), y), b"%s %s l" % (x, y)]))
                else:
                    ops.append(b"%s %s l" % (num(r), num(r)))
            elif k < 0.8:
                ops.append(b"%s %s %s %s %s %s c" % tuple(num(r) for _ in range(6)))
            elif k < 0.9:
                ops.append(b"%s %s %s %s v" % tuple(num(r) for _ in range(4)))
            else:
                ops.append(b"%s %s %s %s y" % tuple(num(r) for _ in range(4)))
        if r.random() < 0.4:
            ops.append(b"h")
    return b" ".join(ops)


def random_cm(r: random.Random) -> bytes:
    a, b, c, d = (r.uniform(-2, 2) for _ in range(4))
    return b"%.4f %.4f %.4f %.4f %.3f %.3f cm" % (a, b, c, d, r.uniform(-50, 150), r.uniform(-50, 150))


def random_forms(r: random.Random) -> list[tuple[bytes, bytes]]:
    forms = []
    for k in range(r.choice([0, 0, 1, 2, 3])):
        entries = b"/BBox [%s %s %s %s]" % (num(r), num(r), num(r), num(r))
        if r.random() < 0.7:
            m = [r.choice([1, 0, -1, r.uniform(-2, 2)]) for _ in range(4)] + [r.uniform(-60, 60), r.uniform(-60, 60)]
            entries += b" /Matrix [" + b" ".join(b"%.4f" % v for v in m) + b"]"
        forms.append((entries, random_page(r, k)))
    return forms


def random_page(r: random.Random, nforms: int = 0) -> bytes:
    out = []
    for _ in range(r.randint(1, 6)):
        g = [b"q"]
        if nforms and r.random() < 0.35:
            if r.random() < 0.4:
                g.append(random_cm(r))
            if r.random() < 0.3:
                g.append(random_path(r) + r.choice([b" W n", b" W* n"]))
            if r.random() < 0.3:
                g.append(b"%.3f %.3f %.3f rg %.3f %.3f %.3f RG %.3f w"
                         % (*(r.random() for _ in range(6)), r.choice([0, 1, 3])))
            g.append(b"/X%d Do" % r.randrange(nforms))
            g.append(b"Q")
            out.append(b"\n".join(g))
            continue
        if r.random() < 0.3:
            g.append(random_cm(r))
        if r.random() < 0.3:
            g.append(random_path(r) + r.choice([b" W n", b" W* n"]))
        if r.random() < 0.15:
            g.append(random_path(r) + b" W n")
        g.append(b"%.3f %.3f %.3f rg %.3f %.3f %.3f RG" % tuple(r.random() for _ in range(6)))
        g.append(b"%.3f w %d J %d j %.2f M" % (r.choice([0, 0.1, 0.4, 1, 2.5, 8, 20]), r.randint(0, 2),
                                             r.randint(0, 2), r.choice([1, 1.5, 4, 10])))
        if r.random() < 0.25:
            g.append(r.choice([b"[3 2] 0 d", b"[0 4] 0 d", b"[1] 0.5 d", b"[5 1 0.2 1] 3 d", b"[0.01 0.01] 0 d", b"[] 0 d"]))
        if r.random() < 0.3:
            g.append(b"/A%d gs" % r.randint(0, 4))
        if r.random() < 0.1:
            g.append(r.choice([b"/D0 gs", b"/L0 gs"]))
        g.append(random_path(r) + b" " + r.choice([b"f", b"f*", b"S", b"s", b"B", b"B*", b"b", b"b*", b"F", b"n"]))
        g.append(b"Q")
        out.append(b"\n".join(g))
    return b"\n".join(out)


def random_geometry(r: random.Random) -> dict:
    """Page geometry for `compare`: a media box off the origin, a crop box, /Rotate (also the odd
    values PDFium folds: -90, 450, 45), and a clip rectangle for `render`."""
    x0, y0 = r.choice([0, 0, r.uniform(-300, 300)]), r.choice([0, 0, r.uniform(-300, 300)])
    media = (x0, y0, x0 + r.uniform(20, 300), y0 + r.uniform(20, 300))
    entries = b""
    if r.random() < 0.6:
        entries += b"/Rotate %d " % r.choice([0, 90, 180, 270, -90, 450, 45, 360])
    if r.random() < 0.3:
        cx, cy = r.uniform(media[0] - 20, media[2]), r.uniform(media[1] - 20, media[3])
        entries += b"/CropBox [%.3f %.3f %.3f %.3f]" % (cx, cy, cx + r.uniform(5, 250), cy + r.uniform(5, 250))
    clip = None
    if r.random() < 0.4:
        cx, cy = r.uniform(-10, 150), r.uniform(-10, 150)
        clip = (cx, cy, cx + r.uniform(1, 120), cy + r.uniform(1, 120))
    return {"media": media, "page_entries": entries, "clip": clip}


JUNK = [b"1e30", b"-1e30", b"3.4e38", b"1e-40", b"nan", b"inf", b"--5", b"5..5", b".", b"-", b"1e",
        b"99999999999999999999", b"q", b"Q", b"Q Q Q", b"q q q q", b"h", b"W", b"W*", b"n", b"f", b"S", b"B*",
        b"cm", b"re", b"c", b"l", b"m", b"w", b"d", b"[", b"]", b"[1 2", b"<<", b">>", b"(", b")", b"/Name",
        b"%comment", b"\x00", b"-0", b"0 0 0 0 0 0 cm", b"1 0 0 0 0 0 cm", b"0 w", b"-3 w", b"[0 0] 0 d",
        b"[-1 2] 0 d", b"[1e-9] 0 d", b"1e9 w", b"0 M", b"-1 j", b"7 J", b"/A9 gs", b"/X7 Do"]


def mutate(r: random.Random, content: bytes) -> bytes:
    """One to six token edits: junk inserted, a token replaced by junk, a token deleted."""
    toks = content.split(b" ")
    for _ in range(r.randint(1, 6)):
        k, i = r.random(), r.randrange(len(toks) + 1)
        if k < 0.4:
            toks.insert(i, r.choice(JUNK))
        elif k < 0.7 and toks:
            toks[min(i, len(toks) - 1)] = r.choice(JUNK)
        elif toks:
            del toks[min(i, len(toks) - 1)]
    return b" ".join(toks)


def case(seed: int, forms: bool, page: bool = False, mutated: bool = False) -> tuple[bytes, list, float, bool, dict]:
    """The page (content, forms, zoom, transparent, geometry) seed `seed` stands for. `mutated`:
    the content streams get junk tokens (`mutate`), for the parser's handling of broken syntax."""
    r = random.Random(seed)
    fs = random_forms(r) if forms else []
    content = random_page(r, len(fs))
    zoom, transparent = r.choice([0.5, 1, 1.37, 2, 3.1]), r.random() < 0.3
    geometry = random_geometry(r) if page else {}
    if mutated:
        m = random.Random(seed * 7919 + 1)
        content = mutate(m, content)
        fs = [(e, mutate(m, c)) for e, c in fs]
    return content, fs, zoom, transparent, geometry


def compare(content: bytes, zoom: float, transparent: bool, forms=(), geometry=None):
    """(pixels that differ, PDFium's render, pure's render, per-pixel max difference).
    `geometry`: media, page_entries, clip (random_geometry)."""
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    g = dict(geometry or {})
    clip = g.pop("clip", None)
    data = pdf_bytes([content], forms=forms, **g)
    a = PdfiumBackend().open(data)[0].render(zoom, clip, transparent=transparent)
    b = PureBackend().open(data)[0].render(zoom, clip, transparent=transparent)
    if a.shape != b.shape:
        raise AssertionError(f"shapes {a.shape} != {b.shape}")
    d = np.abs(a.astype(int) - b.astype(int)).max(axis=2)
    return int((d > 0).sum()), a, b, d


def shrink(content: bytes, zoom: float, transparent: bool, forms=(), geometry=None, words: bool = False):
    """Drop lines (the page's, then each form's) while the difference remains; `words`: then
    single tokens too (for mutated pages, where the culprit is one token in a line)."""
    forms = list(forms)

    def fails(c, fs):
        try:
            return compare(c, zoom, transparent, fs, geometry)[0] > 0
        except Exception:
            return False

    def cut(text, test, sep=b"\n"):
        lines = text.split(sep) if sep != b" " else text.split()
        changed = True
        while changed:
            changed = False
            for i in range(len(lines)):
                if sep == b"\n" and lines[i] in (b"q", b"Q"):
                    continue
                trial = lines[:i] + lines[i + 1:]
                if test(sep.join(trial)):
                    lines, changed = trial, True
                    break
        return sep.join(lines)

    for sep in (b"\n", b" ") if words else (b"\n",):
        content = cut(content, lambda c: fails(c, forms), sep)
        for k in range(len(forms)):
            entries = forms[k][0]
            forms[k] = (entries, cut(forms[k][1], lambda c: fails(content, forms[:k] + [(entries, c)] + forms[k + 1:]), sep))
    return content, forms


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("seed0", type=int, nargs="?", default=0)
    ap.add_argument("n", type=int, nargs="?", default=200)
    ap.add_argument("--forms", action="store_true")
    ap.add_argument("--page", action="store_true", help="random media/crop boxes, /Rotate and render clips")
    ap.add_argument("--mutate", action="store_true", help="junk tokens in the content (broken syntax)")
    ap.add_argument("--out", default="out/render-torture")
    args = ap.parse_args(argv)
    out = Path(args.out)
    fails = 0
    for seed in range(args.seed0, args.seed0 + args.n):
        content, forms, zoom, transparent, geometry = case(seed, args.forms, args.page, args.mutate)
        try:
            npx = compare(content, zoom, transparent, forms, geometry)[0]
        except Exception as e:
            print("seed", seed, "EXC", type(e).__name__, e)
            fails += 1
            continue
        if not npx:
            continue
        fails += 1
        small, sforms = shrink(content, zoom, transparent, forms, geometry, words=args.mutate)
        npx, a, b, d = compare(small, zoom, transparent, sforms, geometry)
        print(f"seed {seed} zoom {zoom} transparent {transparent} {geometry}: {npx} px, max {d.max()}")
        for k, (e, c) in enumerate(sforms):
            print(f"-- X{k} {e.decode()}\n{c.decode(errors='replace')}")
        print("-- page\n" + small.decode(errors="replace"))
        out.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        vis = np.concatenate([a[..., :3], b[..., :3], np.stack([np.where(d > 0, 255, 0)] * 3, -1)], 1)
        Image.fromarray(vis.astype(np.uint8)).save(out / f"seed{seed}.png")
        g = {k: v for k, v in geometry.items() if k != "clip"}
        (out / f"seed{seed}.pdf").write_bytes(pdf_bytes([small], forms=sforms, **g))
    print(f"seeds {args.seed0}..{args.seed0 + args.n - 1}: {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
