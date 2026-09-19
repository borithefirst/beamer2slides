"""python tools/truetype_torture.py SEED0 N: simple fonts with an embedded sfnt made up per seed
(/TrueType, some declared /Type1) - raw cmap subtables in random order, formats 0/4/6/12/13,
overlapping, unsorted or broken ones, raw post tables of every format with duplicate names, cut
strings and junk tails, tables laid out in random order, random /Encoding, /Flags, /Widths and
ToUnicode - and every extract call compared between the pure reader and PDFium. The first
difference per seed is printed, shrunk, with the font's recipe. `tests/test_pure_pdf.py` replays
every seed that found a bug."""
import dataclasses
import io
import random
import struct
import sys
from collections import Counter

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from . import render_torture_text as T

NAMES = [".notdef", "space", "A", "a", "B", "b", "uni0041", "uni00E9", "uni00e9", "u1F600", "u00E9", "u0041",
         "a85", "a96", "a205", "a1", "Delta", "Omega", "fraction", "hyphen", "mu", "fi", "f_i", "A.sc", "e.alt",
         ".alt", "g", "one", "zero", "uniF041", "uniF020", "Euro", "dotlessi", "afii10017", "periodcentered",
         "nbspace", "sfthyphen", "", "space", "bullet", "quotesingle", "Tcommaaccent", "uni0394", "uni2206",
         "exclam", "numbersign", "eacute", "Eacute", "ellipsis", "uni20AC", "uniD800", "u10FFFF", "glyph7"]
CODES_UNI = [0x20, 0x41, 0x61, 0x42, 0xE9, 0xC9, 0x2022, 0x20AC, 0x394, 0x2206, 0x3A9, 0x2126, 0xA0, 0xAD,
             0x2D, 0x131, 0xF041, 0xF020, 0xF061, 0x1F600, 0x10FFFF, 0x21, 0x23, 0x2026, 0x27]


def glyph(i):
    pen = TTGlyphPen(None)
    x0, y0 = (i * 37) % 90, -30 + (i * 23) % 70
    w, h = 80 + 17 * (i % 13), 200 + 29 * (i % 11)
    pen.moveTo((x0, y0)); pen.lineTo((x0, y0 + h)); pen.lineTo((x0 + w, y0 + h)); pen.lineTo((x0 + w, y0))
    pen.closePath()
    return pen.glyph()


# ---------------------------------------------------------------- raw cmap subtables

def sub0(m):
    arr = bytes(m.get(c, 0) & 0xFF for c in range(256))
    return struct.pack(">HHH", 0, 262, 0) + arr


def sub6(m):
    codes = [c for c in m if c < 0x10000]
    if not codes:
        return struct.pack(">HHHHH", 6, 10, 0, 0, 0)
    first = min(codes)
    last = max(c for c in codes if c < first + 3000)
    ids = [m.get(c, 0) for c in range(first, last + 1)]
    return struct.pack(">HHHHH", 6, 10 + 2 * len(ids), 0, first, len(ids)) + struct.pack(">%dH" % len(ids), *ids)


def sub12(m, fmt=12):
    groups = []
    for c in sorted(m):
        g = m[c]
        if groups and c == groups[-1][1] + 1 and (fmt == 13 and g == groups[-1][2] or
                                                   fmt == 12 and g == groups[-1][2] + c - groups[-1][0]):
            groups[-1][1] = c
        else:
            groups.append([c, c, g])
    body = b"".join(struct.pack(">III", *g) for g in groups)
    return struct.pack(">HHIII", fmt, 0, 16 + len(body), 0, len(groups)) + body


def sub4(m, r, mode):
    """mode: plain | array | overlap | unsorted | nolast | badlast"""
    codes = sorted(c for c in m if c < 0xFFFF)
    segs = []   # [start, end, delta, glyphs or None]
    for c in codes:
        if segs and c == segs[-1][1] + 1 and r.random() < 0.8:
            segs[-1][1] = c
            segs[-1][3].append(m[c])
        else:
            segs.append([c, c, 0, [m[c]]])
    out = []
    for s, e, _, gl in segs:
        if mode != "plain" and r.random() < 0.5:
            out.append([s, e, 0, gl])                               # through glyphIdArray
        else:
            deltas = {(g - (s + k)) & 0xFFFF for k, g in enumerate(gl)}
            if len(deltas) == 1:
                out.append([s, e, deltas.pop(), None])
            else:
                out.append([s, e, 0, gl])
    if mode == "overlap" and out:
        k = r.randrange(len(out))
        s, e, d, gl = out[k]
        s2 = max(0, s - r.randint(0, 3))
        out.insert(k + 1, [s2, e + r.randint(0, 3), r.randint(0, 20), None])
        out.sort(key=lambda t: t[0])
    if mode == "unsorted" and len(out) > 1:
        i, j = r.sample(range(len(out)), 2)
        out[i], out[j] = out[j], out[i]
    if mode == "ffff" and out:
        out[r.randrange(len(out))][3] = "ffff"
    if mode != "nolast":
        out.append([0xFFFF, 0xFFFF, 1, None])
    if mode == "badlast":
        out[-1] = [0xFFFF, 0xFFFF, 0, [5]]
    n = len(out)
    ends = [t[1] for t in out]
    starts = [t[0] for t in out]
    deltas = [t[2] & 0xFFFF for t in out]
    offsets, array = [], []
    for i, t in enumerate(out):
        if t[3] == "ffff":
            offsets.append(0xFFFF)
        elif t[3] is None:
            offsets.append(0)
        else:
            offsets.append(2 * (n - i) + 2 * len(array))
            array += [(g - t[2]) & 0xFFFF if g else 0 for g in t[3]]
    body = struct.pack(">%dH" % n, *ends) + b"\0\0" + struct.pack(">%dH" % n, *starts) + \
        struct.pack(">%dH" % n, *deltas) + struct.pack(">%dH" % n, *offsets) + \
        struct.pack(">%dH" % len(array), *array)
    length = 14 + len(body)
    return struct.pack(">HHHHHHH", 4, length & 0xFFFF, 0, 2 * n, 0, 0, 0) + body


def make_cmap(r, n_glyphs, recipe):
    subs = []
    for _ in range(r.choice([0, 1, 1, 2, 2, 3, 4])):
        pid, eid = r.choice([(3, 0), (3, 1), (1, 0), (0, 3), (0, 4), (3, 10), (3, 2), (0, 5), (2, 1), (1, 1)])
        if (pid, eid) == (3, 0):
            base = r.choice([0, 0xF000, 0xF000, 0xF100, 0xF200])
            keys = [base + c for c in r.sample(range(0x20, 0x100), r.randint(3, 40))]
        elif pid == 1:
            keys = r.sample(range(0, 256), r.randint(3, 60))
        else:
            keys = r.sample(CODES_UNI, r.randint(2, len(CODES_UNI))) + r.sample(range(0x20, 0x7F), r.randint(0, 20))
        m = {k: r.randrange(n_glyphs + (2 if r.random() < 0.1 else 0)) for k in keys}
        if pid == 1 or r.random() < 0.15:
            fmt = r.choice([0, 6, 4])
        elif any(k > 0xFFFF for k in keys) or eid in (10, 4) or r.random() < 0.2:
            fmt = r.choice([12, 12, 13, 4])
        else:
            fmt = r.choice([4, 4, 6])
        if fmt == 0:
            m = {k: v for k, v in m.items() if k < 256}
            data = sub0(m)
            mode = ""
        elif fmt == 6:
            data = sub6(m)
            mode = ""
        elif fmt in (12, 13):
            data = sub12(m, fmt)
            mode = ""
        else:
            mode = r.choice(["plain", "array", "array", "overlap", "unsorted", "ffff", "nolast", "badlast"])
            data = sub4(m, r, mode)
        if r.random() < 0.06:          # a broken length or truncation
            data = data[:max(4, len(data) - r.randint(1, 8))] if r.random() < 0.5 else \
                data[:2] + struct.pack(">H", r.randint(0, 600)) + data[4:]
            mode += "+broken"
        subs.append((pid, eid, data))
        recipe.append(f"cmap({pid},{eid}) f{fmt} {mode} {sorted(m.items())[:12]}")
    if r.random() < 0.1 and subs:     # two records sharing one subtable
        subs.append((3, 1, subs[0][2]))
    head = struct.pack(">HH", 0, len(subs))
    off = 4 + 8 * len(subs)
    recs, blobs = b"", b""
    for pid, eid, data in subs:
        recs += struct.pack(">HHI", pid, eid, off + len(blobs))
        blobs += data
        if len(blobs) % 2:
            blobs += b"\0"
    return head + recs + blobs


def make_post(r, n, recipe):
    kind = r.choice(["none", "3", "2", "2", "2", "2.5", "1", "4", "short"])
    hdr = lambda fmt: struct.pack(">IihhIIIII", fmt, 0, -100, 50, 0, 0, 0, 0, 0)
    names = []
    if kind == "3":
        data = hdr(0x00030000)
    elif kind == "1":
        data = hdr(0x00010000)
    elif kind == "4":
        data = hdr(0x00040000)
    elif kind == "short":
        data = hdr(0x00020000)[:20]
    elif kind == "2.5":
        offs = [r.randint(-3, 5) for _ in range(n)]
        data = hdr(0x00025000) + struct.pack(">H", n) + bytes(o & 0xFF for o in offs)
    elif kind == "2":
        from ..pdf.pure.psnames_data import MAC_NAMES
        names = [r.choice(NAMES) for _ in range(n)]
        names[0] = ".notdef" if r.random() < 0.8 else names[0]
        extra, idx = [], []
        for nm in names:
            if nm in MAC_NAMES and r.random() < 0.7:
                idx.append(MAC_NAMES.index(nm))
            else:
                if nm not in extra or r.random() < 0.3:
                    extra.append(nm)
                idx.append(258 + len(extra) - 1 - (extra[::-1].index(nm) if nm in extra else 0))
        count = n if r.random() < 0.9 else n + r.choice([-1, 1])
        idx = (idx + [0])[:count]
        strings = [bytes([len(e)]) + e.encode("latin-1") for e in extra]
        tail = r.choice(["", "", "unused", "junk", "overrun", "nul", "cut"])
        if tail == "unused":
            strings.append(b"\x04a205")
        elif tail == "junk":
            strings.append(bytes(r.randrange(256) for _ in range(r.randint(1, 6))))
        elif tail == "overrun" and strings:
            k = r.randrange(len(strings))
            strings[k] = bytes([strings[k][0] + r.randint(1, 9)]) + strings[k][1:]
        elif tail == "nul" and strings:
            k = r.randrange(len(strings))
            strings[k] = bytes([strings[k][0] + 2]) + strings[k][1:] + b"\0x"
        elif tail == "cut" and strings:
            strings = strings[:r.randrange(len(strings))]
        names.append(tail)
        data = hdr(0x00020000) + struct.pack(">H", count) + struct.pack(">%dH" % len(idx), *idx) + b"".join(strings)
    else:
        data = None
    recipe.append(f"post {kind} {names}")
    return data


def make_os2(tables, r, recipe):
    """An OS/2 table of any version, maybe cut short, and hhea metrics maybe 0: sfnt_load_face's
    ascender and descender (USE_TYPO_METRICS, then hhea, then typo, then win) - they are the char
    boxes of a font whose FontBBox is 0 0 0 0 (--os2)."""
    version = r.choice([0, 1, 2, 3, 4, 5, 6, 0xFFFF])
    typo = r.choice([(0, 0), (0, -r.randint(1, 300)), (r.randint(1, 1200), -r.randint(0, 400))])
    win = (r.choice([0, r.randint(1, 1500), 0xFFF0]), r.choice([0, r.randint(1, 500), 0x8001]))
    selection = r.choice([0, 0x40, 0x80, 0xC0])
    body = bytearray(100)
    struct.pack_into(">H", body, 0, version)
    struct.pack_into(">H", body, 62, selection)
    struct.pack_into(">hhhHH", body, 68, typo[0], typo[1], r.randint(0, 200), *win)
    length = r.choice([78, 86, 96, 100, r.randint(0, 100)])
    tables[b"OS/2"] = bytes(body[:length])
    hhea = bytearray(tables[b"hhea"])
    zero = r.random() < 0.6
    if zero:
        struct.pack_into(">hh", hhea, 4, 0, 0)
    elif r.random() < 0.3:
        struct.pack_into(">hh", hhea, 4, 0, -r.randint(1, 300))
    tables[b"hhea"] = bytes(hhea)
    recipe.append(f"os2 v{version} len {length} sel {selection:#x} typo {typo} win {win} hhea0 {zero}")


def make_font(r, recipe, os2=None):
    n = r.randint(3, 30)
    order = [".notdef"] + ["g%d" % i for i in range(1, n)]
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({})
    fb.setupGlyf({nm: glyph(i) for i, nm in enumerate(order)})
    fb.setupHorizontalMetrics({nm: (400 + 25 * i, 0) for i, nm in enumerate(order)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupPost()
    tt = fb.font
    buf = io.BytesIO()
    tt.save(buf, reorderTables=False)
    raw = buf.getvalue()
    tables = {}
    for k in range(struct.unpack_from(">H", raw, 4)[0]):
        tag, _, off, length = struct.unpack_from(">4sIII", raw, 12 + 16 * k)
        tables[tag] = raw[off:off + length]
    for tag in (b"name", b"OS/2", b"cmap", b"post"):
        tables.pop(tag, None)
    cmap = make_cmap(r, n, recipe)
    if cmap[2:4] == b"\0\0" and r.random() < 0.5:
        recipe.append("no cmap table")
    else:
        tables[b"cmap"] = cmap
    post = make_post(r, n, recipe)
    if post is not None:
        tables[b"post"] = post
    if os2 is not None:
        make_os2(tables, os2, recipe)
    layout = None
    if r.random() < 0.5:
        layout = list(tables)
        r.shuffle(layout)
        recipe.append("layout " + " ".join(t.decode() for t in layout))
    return sfnt(tables, layout)


def sfnt(tables, layout=None):
    tags = sorted(tables)
    out = bytearray(struct.pack(">IHHHH", 0x00010000, len(tags), 0, 0, 0))
    off = 12 + 16 * len(tags)
    where, blobs = {}, b""
    for tag in layout or tags:
        where[tag] = off + len(blobs)
        blobs += tables[tag] + b"\0" * (-len(tables[tag]) % 4)
    for tag in tags:
        out += struct.pack(">4sIII", tag, 0, where[tag], len(tables[tag]))
    return bytes(out) + blobs


DIRECTORY = ["cut", "long", "maxp", "dup", "unsorted", "zero", "offset", "count", "head", "hhea", "loca"]


def mutate_directory(data, r, recipe):
    """Break the table directory or a table FreeType checks while opening the face (--directory)."""
    d = bytearray(data)
    n = struct.unpack_from(">H", d, 4)[0]
    entries = [list(struct.unpack_from(">4sIII", d, 12 + 16 * k)) for k in range(n)]
    at = {e[0]: k for k, e in enumerate(entries)}
    kind = r.choice(DIRECTORY)
    k = r.randrange(n)
    if kind == "cut":
        d = d[:r.randrange(12 + 16 * n, len(d))]
    elif kind == "long":
        entries[k][3] += r.choice([1, 3, 4, 100, len(d)])
    elif kind == "maxp":
        entries[at[b"maxp"]][3] = r.choice([0, 2, 4, 5, 6, 31])
    elif kind == "dup":
        j = r.randrange(n)
        entries[k][0] = entries[j][0]
    elif kind == "unsorted":
        r.shuffle(entries)
    elif kind == "zero":
        entries[k][3] = 0
    elif kind == "offset":
        entries[k][2] = r.choice([len(d), len(d) + 4, 0xFFFFFFF0, entries[k][2] + 2])
    elif kind == "count":
        struct.pack_into(">H", d, 4, max(0, n + r.choice([-2, -1, 1, 2])))
    elif kind in ("head", "hhea", "loca"):
        tag = {"head": b"head", "hhea": b"hhea", "loca": b"loca"}[kind]
        e = entries[at[tag]]
        e[3] = r.randrange(0, e[3])
    recipe.append(f"directory {kind} {entries[k][0]!r}")
    for i, e in enumerate(entries):
        struct.pack_into(">4sIII", d, 12 + 16 * i, *e)
    return bytes(d)


def pdf_font(r, recipe, directory=None, os2=None):
    data = make_font(r, recipe, os2)
    if directory is not None:
        data = mutate_directory(data, directory, recipe)
    flags = r.choice([4, 32, 36, 0, 4 | 65536, 32 | 65536, 6, 34])
    enc = r.choice(["none", "none", "/WinAnsiEncoding", "/MacRomanEncoding", "/MacExpertEncoding", "dict", "dict"])
    if enc == "dict":
        base = r.choice(["", "/BaseEncoding /WinAnsiEncoding", "/BaseEncoding /MacRomanEncoding",
                         "/BaseEncoding /StandardEncoding", "/BaseEncoding /Bogus"])
        diffs = []
        for _ in range(r.randint(1, 6)):
            diffs.append(str(r.randrange(256)))
            diffs += ["/" + (r.choice(NAMES + ["g%d" % r.randrange(8), "uni%04X" % r.choice(CODES_UNI[:16])])
                             or ".notdef") for _ in range(r.randint(1, 5))]
        enc = "<< %s /Differences [%s] >>" % (base, " ".join(diffs))
    recipe.append(f"flags {flags} enc {enc}")
    first = r.randrange(0, 200)
    widths = ""
    if r.random() < 0.7:
        last = min(255, first + r.randint(0, 80))
        widths = "/FirstChar %d /LastChar %d /Widths [%s]" % (first, last, " ".join(
            str(r.randint(0, 900)) for _ in range(last - first + 1)))
    elif r.random() < 0.5:
        widths = "/FirstChar %d" % first
    recipe.append(widths[:30])
    tu = r.random() < 0.3
    subtype = "TrueType" if r.random() < 0.8 else "Type1"
    recipe.append("subtype " + subtype)
    font = ("<< /Type /Font /Subtype /%s /BaseFont /ABCDEF+Torture %s %s /FontDescriptor @1@%s >>"
            % (subtype, "" if enc == "none" else "/Encoding " + enc, widths, " /ToUnicode @3@" if tu else "")).encode()
    desc = (b"<< /Type /FontDescriptor /FontName /ABCDEF+Torture /Flags %d /FontBBox [%s] "
            b"/ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 /FontFile2 @2@ >>"
            % (flags, b"0 0 0 0" if os2 is not None else b"-50 -250 1100 900"))
    ff = b"<< /Length %d /Length1 %d >>\nstream\n" % (len(data), len(data)) + data + b"\nendstream"
    objs = [font, desc, ff]
    if tu:
        pairs = " ".join("<%02X> <%04X>" % (r.randrange(256), r.choice(CODES_UNI[:16])) for _ in range(r.randint(1, 8)))
        cmap = ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /T def 1 begincodespacerange "
                "<00> <FF> endcodespacerange %d beginbfchar %s endbfchar endcmap CMapName currentdict /CMap "
                "defineresource pop end end" % (pairs.count("> <"), pairs)).encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream")
    return T.FontSpec("torture", "truetype", objs, list(range(256)))


def content(r):
    lines = []
    for k in range(r.randint(1, 8)):
        codes = [r.randrange(256) if r.random() < 0.5 else r.randrange(0x20, 0x80) for _ in range(r.randint(1, 16))]
        lines.append(b"BT /F0 %d Tf 5 %d Td <%s> Tj ET" % (r.choice([8, 10, 12]), 140 - 17 * k,
                                                           b"".join(b"%02x" % c for c in codes)))
    return b"\n".join(lines)


def said(page):
    chars = [{k: v for k, v in dataclasses.asdict(c).items() if k != "font_id"} for c in page.chars()]
    return {
        "objects": [dataclasses.astuple(o) for o in page.objects()],
        "object_bounds": page.object_bounds(),
        "chars": chars,
        "widths": page.glyph_widths([(c.font_id, c.c[:1], c.size) for c in page.chars() if c.font_id >= 0 and c.c]),
    }


def first_diff(cont, fonts):
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    data = T.pdf_bytes(cont, fonts)
    docs = PureBackend().open(data), PdfiumBackend().open(data)
    try:
        a, b = said(docs[0][0]), said(docs[1][0])
    finally:
        for doc in docs:
            doc.close()
    for k in a:
        if a[k] != b[k]:
            if isinstance(a[k], list) and len(a[k]) == len(b[k]):
                for i, (x, y) in enumerate(zip(a[k], b[k])):
                    if x != y:
                        if isinstance(x, dict):
                            x = {q: x[q] for q in x if x[q] != y[q]}
                            y = {q: y[q] for q in x}
                        return f"{k}[{i}] pure {x} pdfium {y}"
            return f"{k} lengths {len(a[k])} {len(b[k])}"
    return None


def shrink(cont, fonts):
    """Fewest lines, then fewest codes per line."""
    lines = cont.split(b"\n")
    i = 0
    while i < len(lines):
        trial = lines[:i] + lines[i + 1:]
        if trial and first_diff(b"\n".join(trial), fonts):
            lines = trial
        else:
            i += 1
    for li, line in enumerate(lines):
        pre, hexs = line.split(b"<")
        hexs, post = hexs.split(b">")
        codes = [hexs[k:k + 2] for k in range(0, len(hexs), 2)]
        k = 0
        while k < len(codes) and len(codes) > 1:
            trial = codes[:k] + codes[k + 1:]
            t = lines[:li] + [pre + b"<" + b"".join(trial) + b">" + post] + lines[li + 1:]
            if first_diff(b"\n".join(t), fonts):
                codes = trial
                lines = t
            else:
                k += 1
    return b"\n".join(lines)


def case(seed, directory=False, os2=False):
    """`directory` breaks the sfnt's table directory too, `os2` adds an OS/2 table and a zero
    FontBBox, each from a generator of its own, so the seeds without them keep making the fonts they
    always made."""
    r = random.Random(seed)
    recipe = []
    spec = pdf_font(r, recipe, random.Random(seed * 7919 + 1) if directory else None,
                    random.Random(seed * 7919 + 2) if os2 else None)
    return content(r), [spec], recipe


if __name__ == "__main__":
    seed0, n = int(sys.argv[1]), int(sys.argv[2])
    directory, os2 = "--directory" in sys.argv, "--os2" in sys.argv
    bad = Counter()
    for seed in range(seed0, seed0 + n):
        cont, fonts, recipe = case(seed, directory, os2)
        try:
            d = first_diff(cont, fonts)
        except Exception as e:  # noqa: BLE001
            print("crash", seed, repr(e)[:300], flush=True)
            bad["crash"] += 1
            continue
        if d:
            bad[d.split("[")[0].split(" ")[0]] += 1
            small = shrink(cont, fonts)
            print("seed", seed, d[:300], "\n   ", small[:300], "\n   ", "\n    ".join(x[:200] for x in recipe), flush=True)
    print("done", n, dict(bad))
