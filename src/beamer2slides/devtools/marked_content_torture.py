"""Marked content against PDFium's text page: random BMC / BDC / EMC around text objects.

Each seed writes a page of Helvetica text (a ToUnicode map sends A-J to Hebrew and a-j to Arabic,
so an object may run right to left) inside random marked-content sequences: /ActualText written in
the stream, named in /Properties (directly or through a reference), empty, unprintable, a lone
U+FFFD, a surrogate pair, a name instead of a string; an /MCID with no /ActualText; BDC operands
that open nothing; EMC with nothing open; mirrored and turned text; a form with a sequence of its own.
The chars and object boxes the pure reader reports must be PDFium's.

    python tools/marked_content_torture.py FIRST COUNT
"""

from __future__ import annotations

import dataclasses
import random
import sys

from beamer2slides import pdf

TOUNICODE = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /X def "
             b"1 begincodespacerange <00> <FF> endcodespacerange "
             b"2 beginbfrange <41> <4A> <05D0> <61> <6A> <0627> endbfrange "
             b"1 beginbfchar <30> <FFFD> endbfchar endcmap CMapName currentdict /CMap defineresource pop end end")

STRINGS = [b"(Hi)", b"()", b"<FEFF062A0631>", b"<FEFF05D005D1>", b"(a\\001b)", b"<FEFFFFFD>", b"<FEFFD83DDE00>",
           b"(\\200)", b"<FEFF0020>", b"(x y)", b"<FEFFFFFE0041>", b"<EFBBBF41C3A9>", b"(\\n)", b"<FEFF00410301>",
           b"(fi)", b"<FEFF0644064A>", b"(-)", b"<FEFF00AD>"]
TEXTS = [b"(Hello)", b"(ABC)", b"(abc)", b"(0A0)", b"(Wo rld)", b"(A)", b"(-)", b"(a-)", b"(J)"]


def objects_pdf(objs: dict) -> bytes:
    out = [b"%PDF-1.7\n"]
    offsets = {}
    for num in sorted(objs):
        offsets[num] = sum(len(p) for p in out)
        out.append(b"%d 0 obj\n%s\nendobj\n" % (num, objs[num]))
    xref = sum(len(p) for p in out)
    n = max(objs) + 1
    out.append(b"xref\n0 %d\n0000000000 65535 f \n" % n)
    for i in range(1, n):
        out.append(b"%010d 00000 n \n" % offsets[i] if i in offsets else b"0000000000 65535 f \n")
    out.append(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (n, xref))
    return b"".join(out)


def case(seed: int) -> bytes:
    r = random.Random(seed)
    parts = []
    y = 180
    props = {b"/P1": b"<< /ActualText %s >>" % r.choice(STRINGS), b"/P2": b"8 0 R", b"/P3": b"<< /MCID 3 >>"}
    for _ in range(r.randint(2, 9)):
        k = r.random()
        if k < 0.25:
            what = r.choice([b"<< /ActualText %s >>" % r.choice(STRINGS),
                             b"<< /MCID 1 /ActualText %s >>" % r.choice(STRINGS),
                             b"<< /MCID 2 >>", b"/P1", b"/P2", b"/P3", b"/Nope", b"5", b"<< /ActualText /Name >>"])
            parts.append(b"/Span %s BDC" % what)
        elif k < 0.32:
            parts.append(b"/Tag BMC")
        elif k < 0.45:
            parts.append(b"EMC")
        else:
            if r.random() < 0.6:
                y -= r.choice([0, 0, 14, 25])
            x = r.randint(10, 200)
            size = r.choice([10, 12, 20])
            m = r.choice([b"1 0 0 1", b"-1 0 0 1", b"1 0 0 1", b"0 1 -1 0"])
            parts.append(b"BT /F1 %d Tf %s %d %d Tm %s Tj ET" % (size, m, x, y, r.choice(TEXTS)))
            if r.random() < 0.3:
                parts.append(b"BT /F1 %d Tf %s %d %d Tm %s Tj ET" % (size, m, x + 30, y, r.choice(TEXTS)))
    if r.random() < 0.3:
        body = b"/Span << /ActualText %s >> BDC BT /F1 12 Tf 30 30 Td (ABC) Tj ET EMC" % r.choice(STRINGS)
        parts.append(b"/X1 Do")
        form = b"<< /Subtype /Form /BBox [0 0 300 200] /Length %d >>\nstream\n%s\nendstream" % (len(body), body)
    else:
        form = b"<< >>"
    content = b"\n".join(parts)
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> "
               b"/XObject << /X1 6 0 R >> /Properties << %s >> >> /Contents 4 0 R >>"
               % b" ".join(k + b" " + v for k, v in props.items()),
            4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
            5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 7 0 R >>", 6: form,
            7: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(TOUNICODE), TOUNICODE),
            8: b"<< /ActualText %s >>" % r.choice(STRINGS)}
    return objects_pdf(objs)


def said(backend: str, data: bytes):
    doc = pdf.resolve(backend).open(data)
    try:
        page = doc[0]
        return [dataclasses.astuple(c) for c in page.chars()], page.object_bounds()
    finally:
        doc.close()


def first_diff(seed: int) -> str | None:
    data = case(seed)
    a, b = said("pure", data), said("pdfium", data)
    if a == b:
        return None
    return f"pure {''.join(c[0] for c in a[0])!r} pdfium {''.join(c[0] for c in b[0])!r}"


def main() -> None:
    first, count = int(sys.argv[1]), int(sys.argv[2])
    apart = []
    for seed in range(first, first + count):
        d = first_diff(seed)
        if d:
            apart.append(seed)
            print("APART", seed, d)
    print("done", count, "apart", len(apart), apart[:40])


if __name__ == "__main__":
    main()
