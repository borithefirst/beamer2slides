"""Which unported page content makes the pure reader refuse a page, measured over the PDFs on this
machine - the measurement that says what to port next, and what not to.

`render.unported` stops at the first thing a page holds that the pure reader cannot draw exactly, so
a page has one reason and a document has a few; this walks the given roots, opens each PDF with the
pure backend, parses its first pages and asks `unported` what it would need - mirroring its loop, so
a page's *other* reasons show up behind the first one. Nothing is rendered: parsing and the refusal
check are all it costs (a few hundred pages a second, one worker per slice).

**Read-only.** It opens files, never writes near them, and never copies a document's contents
anywhere: what a slice records per document is its path, its page count and the reasons, and the
tally prints counts and reasons alone. The parts are ndjson so a killed slice resumes (`slice`
skips the paths already in its part file, and a document that outlives `--timeout` kills the worker,
which the parent restarts past it).

    python -m beamer2slides.devtools.refusal_sweep sweep C:\\ --jobs 4 --out sweep
    python -m beamer2slides.devtools.refusal_sweep tally sweep/part*.ndjson
    python -m beamer2slides.devtools.refusal_sweep list C:\\ --out files.json   # the two halves
    python -m beamer2slides.devtools.refusal_sweep slice --files files.json --n 4 --i 0 --out p0.ndjson

Measured 2026-09-20 over 4,435 distinct PDFs of this machine (38,017 pages, 10 per document, no
timeouts): 53 pages refused, 0.14%, in 27 documents - 32 ICC profiles (12 documents), 11 text
needing a fallback font, 9 soft mask backdrop colour spaces (always behind an ICC refusal on the
same page), 6 a fuzzed file with no page dictionary, 2 a shading that fails validation, 2 tiling
patterns. No page needed JPX, JBIG2, CCITTFaxDecode or a transfer function on an image, which is
why none of those is ported. sRGB ICCBased profiles, images inside soft masks and coloured tiling
patterns were ported after it; what a real document still refuses is 9 pages of 3 PDFs carrying one
536-byte v4.3 matrix/TRC RGB profile, everything else being a file the tortures wrote themselves.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

SKIP = {"$recycle.bin", "winsxs", "system volume information", "windows.old", "driverstore"}


def walk(roots):
    for root in roots:
        for dirpath, dirnames, names in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP]
            for n in names:
                if n.lower().endswith(".pdf"):
                    yield os.path.join(dirpath, n)


def digest(path: str) -> str | None:
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def cmd_list(args):
    """The distinct PDFs under the roots: same size and sha1 = one file, whatever it is called."""
    seen: dict[str, str] = {}
    n = 0
    for p in walk(args.roots):
        n += 1
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        if size < 32 or size > args.max_size:
            continue
        d = digest(p)
        if d is None:
            continue
        seen.setdefault(f"{size}:{d}", p)
        if n % 500 == 0:
            print(f"  {n} seen, {len(seen)} distinct", file=sys.stderr)
    files = sorted(seen.values())
    Path(args.out).write_text(json.dumps(files, indent=0), encoding="utf-8")
    print(f"{n} pdf files, {len(files)} distinct -> {args.out}")


# ---------------------------------------------------------------------- the refusal check


def page_reasons(page, limit: int = 4) -> list[str]:
    """Every reason `render.unported` would have to give for this page, in its own order (it stops
    at the first; this mirrors its loop to show what stands behind it). Empty = the page renders."""
    from ..pdf.pure import render_shading
    from ..pdf.pure.render import OBJ_FORM, OBJ_IMAGE, OBJ_PATH, OBJ_SHADING, OBJ_TEXT, unported
    from ..pdf.pure.render_image import refusal as image_refusal
    from ..pdf.pure.render_text import clip_unsupported
    from ..pdf.pure.render_text import unsupported as text_unsupported
    from ..pdf.pure.render_transparency import Context
    from ..pdf.pure.render_transparency import unsupported as tr_unsupported

    pdf = page.doc.pdf
    group = pdf.resolve(page.dict.get("Group"))
    page_group = isinstance(group, dict) and str(pdf.resolve(group.get("S"))) == "Transparency"
    ctx = Context(pdf, pdf.resolve(page.dict.get("Resources")), page.doc._font_cache, page_group)
    ctx.images = {}
    out: list[str] = []

    def add(why):
        if why is not None and why not in out:
            out.append(why)

    for o in page._parse():
        if len(out) >= limit:
            break
        p = o
        while p is not None and p.active:
            p = p.parent
        if p is not None:                       # switched off: never drawn, never refused
            continue
        if o.clip_texts:
            add(clip_unsupported(o.clip_texts))
        if o.type == OBJ_TEXT:
            add(text_unsupported(o))
        elif o.type == OBJ_IMAGE:
            add(image_refusal(o, ctx))
        elif o.type not in (OBJ_PATH, OBJ_FORM, OBJ_SHADING):
            add("objects")
        if o.smask is not None or o.blend != "Normal" or o.transfer is not None:
            add(tr_unsupported(o, ctx, unported))
        if o.type == OBJ_FORM and o.group and ctx is None:
            add("transparency groups")
        if o.type in (OBJ_PATH, OBJ_SHADING):
            add(render_shading.refusal(o, ctx))
    return out


def sweep_one(path: str, pages: int) -> dict:
    """{path, pages, reasons: [[reason, ...] per page]} - or {path, open_error}."""
    from ..pdf.api import PdfError
    from ..pdf.pure.backend import Document

    rec: dict = {"path": path}
    t0 = time.time()
    try:
        doc = Document(path)
    except Exception as e:  # noqa: BLE001
        rec["open_error"] = f"{type(e).__name__}: {e}"[:120]
        return rec
    try:
        rec["pages"] = len(doc)
        got = []
        for i in range(min(len(doc), pages)):
            try:
                got.append(page_reasons(doc[i]))
            except PdfError as e:
                got.append([f"!PdfError {e}"[:160]])
            except Exception as e:  # noqa: BLE001
                got.append([f"!{type(e).__name__}: {e}"[:160]])
        rec["reasons"] = got
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
    rec["secs"] = round(time.time() - t0, 2)
    return rec


class Watchdog:
    """A document that takes longer than `limit` kills this worker; the line it wrote first says so,
    and the parent's next run of the same slice resumes past it."""

    def __init__(self, limit, out):
        self.limit, self.out = limit, out
        self.path, self.start = None, None
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(2)
            if self.path is not None and time.time() - self.start > self.limit:
                self.out.write(json.dumps({"path": self.path, "timeout": self.limit}) + "\n")
                self.out.flush()
                os._exit(3)


def cmd_slice(args):
    """Every `n`th file of the list, appended to `out` as one line per document."""
    files = json.loads(Path(args.files).read_text(encoding="utf-8"))
    mine = [p for k, p in enumerate(files) if k % args.n == args.i]
    done = set()
    outp = Path(args.out)
    if outp.exists():
        for line in outp.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                done.add(json.loads(line)["path"])
            except Exception:  # noqa: BLE001
                pass
    with outp.open("a", encoding="utf-8") as f:
        dog = Watchdog(args.timeout, f)
        deadline = time.time() + args.budget
        for p in mine:
            if p in done:
                continue
            if time.time() > deadline:
                print(f"slice {args.i}: budget spent", file=sys.stderr)
                break
            dog.path, dog.start = p, time.time()
            rec = sweep_one(p, args.pages)
            dog.path = None
            f.write(json.dumps(rec) + "\n")
            f.flush()


def cmd_sweep(args):
    """list + `jobs` slices in their own processes (a wedged one only loses its own slice) + tally."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    files = out / "files.json"
    if not files.exists() or args.relist:
        cmd_list(argparse.Namespace(roots=args.roots, out=str(files), max_size=args.max_size))
    if args.max_files:
        every = json.loads(files.read_text(encoding="utf-8"))
        files = out / "files-some.json"
        files.write_text(json.dumps(every[:args.max_files], indent=0), encoding="utf-8")
    parts = [out / f"part{i}.ndjson" for i in range(args.jobs)]
    running = []
    for i, part in enumerate(parts):
        cmd = [sys.executable, "-m", "beamer2slides.devtools.refusal_sweep",
               "slice", "--files", str(files), "--n",
               str(args.jobs), "--i", str(i), "--out", str(part), "--pages", str(args.pages),
               "--timeout", str(args.timeout), "--budget", str(args.budget)]
        running.append(subprocess.Popen(cmd))
    for p in running:
        p.wait()
    cmd_tally(argparse.Namespace(parts=[str(p) for p in parts], top=args.top, docs=args.docs))


# ---------------------------------------------------------------------- the histogram


def normalise(reason: str) -> str:
    """A page's reason as the table names it: the codecs and ICC by name, a crash by its class."""
    r = reason
    if r.startswith("!"):
        return "(crash) " + r[1:].split(":")[0]
    for name in ("JPXDecode", "JBIG2Decode", "CCITTFaxDecode"):
        if name in r:
            return name
    if "ICC" in r:
        return "ICC"
    return r


def cmd_tally(args):
    """The table: how many pages each reason refused, how often it was the *first* one, and how
    many documents hold it. Counts and reasons only - no document's contents, none of its text."""
    docs = pages = refused_pages = refused_docs = timeouts = 0
    doc_reason: Counter = Counter()
    page_reason: Counter = Counter()
    first_reason: Counter = Counter()
    open_errors: Counter = Counter()
    worst: list[tuple[int, int, str, str]] = []
    for part in args.parts:
        for line in Path(part).read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if "timeout" in rec:
                timeouts += 1
                continue
            docs += 1
            if "open_error" in rec:
                open_errors[rec["open_error"].split(":")[0]] += 1
                continue
            here = set()
            for reasons in rec.get("reasons", []):
                pages += 1
                if reasons:
                    refused_pages += 1
                    first_reason[normalise(reasons[0])] += 1
                for r in reasons:
                    page_reason[normalise(r)] += 1
                    here.add(normalise(r))
            if here:
                refused_docs += 1
                bad = [x for x in rec["reasons"] if x]
                worst.append((len(bad), len(rec["reasons"]), Path(rec["path"]).name, bad[0][0]))
            for r in here:
                doc_reason[r] += 1
    print(f"documents {docs} ({refused_docs} with a refused page), pages {pages} "
          f"({refused_pages} refused, {100.0 * refused_pages / max(pages, 1):.2f}%), "
          f"timeouts {timeouts}")
    if open_errors:
        print("open errors:", dict(open_errors.most_common(8)))
    print(f"\n{'pages':>7} {'first':>7} {'docs':>6}  reason")
    for r, n in page_reason.most_common(args.top):
        print(f"{n:>7} {first_reason.get(r, 0):>7} {doc_reason.get(r, 0):>6}  {r}")
    if args.docs:
        print("\ndocuments with a refused page")
        for bad, all_pages, name, why in sorted(worst, reverse=True):
            print(f"  {name[:56]:56} {bad}/{all_pages}  {why[:60]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="refusal_sweep", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.splitlines()[-9:]))
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("list", help="the distinct PDFs under the roots -> files.json")
    a.add_argument("roots", nargs="*", default=[str(Path.home())])
    a.add_argument("--out", default="files.json")
    a.add_argument("--max-size", type=int, default=200 << 20)
    a.set_defaults(fn=cmd_list)

    b = sub.add_parser("slice", help="check every nth file of a list, appending to a part file")
    b.add_argument("--files", required=True)
    b.add_argument("--n", type=int, default=1, help="how many slices the list is cut into")
    b.add_argument("--i", type=int, default=0, help="which slice this is")
    b.add_argument("--out", required=True)
    b.add_argument("--pages", type=int, default=10, help="pages per document (default 10)")
    b.add_argument("--timeout", type=float, default=90.0, help="seconds one document may take")
    b.add_argument("--budget", type=float, default=3600.0, help="seconds this slice may take")
    b.set_defaults(fn=cmd_slice)

    c = sub.add_parser("sweep", help="list, then the slices in their own processes, then the table")
    c.add_argument("roots", nargs="*", default=[str(Path.home())])
    c.add_argument("--out", default="out/refusal-sweep", help="folder for files.json and the parts")
    c.add_argument("--jobs", type=int, default=4)
    c.add_argument("--pages", type=int, default=10)
    c.add_argument("--max-files", type=int, default=0, help="only the first N of the list")
    c.add_argument("--max-size", type=int, default=200 << 20)
    c.add_argument("--timeout", type=float, default=90.0)
    c.add_argument("--budget", type=float, default=3600.0)
    c.add_argument("--relist", action="store_true", help="walk the roots again")
    c.add_argument("--top", type=int, default=40)
    c.add_argument("--docs", action="store_true", help="also name every document with a refusal")
    c.set_defaults(fn=cmd_sweep)

    d = sub.add_parser("tally", help="the table, from the part files")
    d.add_argument("parts", nargs="+")
    d.add_argument("--top", type=int, default=40)
    d.add_argument("--docs", action="store_true", help="also name every document with a refusal")
    d.set_defaults(fn=cmd_tally)

    args = ap.parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
