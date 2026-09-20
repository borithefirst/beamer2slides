"""Every oracle the pure reader is held to, on whatever OS this runs: PDFium (pypdfium2) is the
truth on each platform, and what PDFium does with fonts a PDF doesn't embed is the platform's own
(GDI on Windows, CFX_LinuxFontInfo's folder scan on Linux, CFX_MacFontInfo on macOS). CI runs this
on all three (.github/workflows/pure-pdf.yml); locally: `python -m beamer2slides.devtools.platform_check`.

Each torture oracle runs in its own process with fixed seeds (PDFium's font mapper and ours are
process-wide), and one check runs here: `subst_extract`, chars and glyph widths of text in made-up
fonts that are not embedded, which no render oracle sees (a render the pure reader refuses is only
`refused`, while the text page measures with the substituted face all the same). Writes
<out>/summary.json (platform, versions, the fonts PDFium's folder scan would see, each check's exit
code and last lines) and exits 1 when any check found PDFium and the pure reader apart."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

DECKS = Path(__file__).resolve().parents[3] / "tests" / "decks" / "out"

# (name, module, arguments); {out} is the check's own output folder
ORACLES = [
    ("paths", "render_torture", ["0", "150", "--out", "{out}"]),
    ("paths-forms", "render_torture", ["0", "100", "--forms", "--out", "{out}"]),
    ("paths-page", "render_torture", ["0", "100", "--page", "--out", "{out}"]),
    ("paths-mutate", "render_torture", ["0", "100", "--mutate", "--out", "{out}"]),
    ("transparency", "render_torture_transparency", ["0", "100", "--no-shrink", "--out", "{out}"]),
    *[(f"shading-{m}", "render_torture_shading", ["0", "80", "--mode", m, "--quiet", "--out", "{out}"])
      for m in ("classic", "cie", "func", "mesh", "transfer", "tiling")],
    *[(f"image-{lv}", "render_torture_image", ["0", "100", "--level", str(lv), "-q", "--out", "{out}"])
      for lv in (0, 3, 6, 7, 8)],
    ("type3", "render_torture_type3", ["0", "100", "-q", "--out", "{out}"]),
    *[(f"subst-{p}", "render_torture_subst", ["0", "150", "--pool", p, "--no-shrink", "--out", "{out}"])
      for p in ("any", "installed")],
    ("truetype", "truetype_torture", ["0", "100"]),
    ("marked-content", "marked_content_torture", ["0", "100"]),
]
# these draw their fonts from the built test decks (tests/decks/build.py)
WITH_DECKS = [
    *[(f"text-{k}", "render_torture_text", ["0", "80", "--kind", k, "--no-shrink", "--out", "{out}"])
      for k in ("any", "cid-truetype")],
    ("text-clip", "render_torture_text", ["0", "60", "--simple", "3", "--no-shrink", "--out", "{out}"]),
    ("text-vertical", "render_torture_text", ["0", "60", "--simple", "4", "--no-shrink", "--out", "{out}"]),
]


def font_folders() -> dict:
    """What CFX_LinuxFontInfo / CFX_MacFontInfo would scan (their default folders), for the
    summary: which faces this machine has decides what substitution PDFium does."""
    home = Path.home()
    folders = {"linux": ["/usr/share/fonts", "/usr/share/X11/fonts/Type1", "/usr/share/X11/fonts/TTF",
                         "/usr/local/share/fonts"],
               "darwin": [str(home / "Library/Fonts"), "/Library/Fonts", "/System/Library/Fonts"],
               "win32": [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")]}
    out = {}
    for folder in folders.get("linux" if sys.platform.startswith("linux") else sys.platform, []):
        p = Path(folder)
        files = sorted(str(f.relative_to(p)) for f in p.rglob("*") if f.is_file()
                       and f.suffix.lower() in (".ttf", ".ttc", ".otf", ".pfb", ".pfa", ".otc")) if p.is_dir() else []
        out[folder] = files
    return out


def folder_faces() -> list[str]:
    """The face names the pure reader's port of PDFium's folder font info found (none on Windows,
    where GDI answers)."""
    from ..pdf.pure import fontmapper
    info = fontmapper.platform_font_info()
    if not isinstance(info, fontmapper.FolderFontInfo):
        return []
    info.enum_font_list(fontmapper.FontMapper(None))
    return list(info.font_list)


def subst_extract(seed0: int, n: int, pool: str) -> dict:
    """Chars (font ids aside) and glyph widths of render_torture_subst's pages, both backends."""
    from .. import pdf
    from .render_torture_subst import case
    from .render_torture_text import pdf_bytes
    apart, crashed = [], []
    for seed in range(seed0, seed0 + n):
        content, fonts, _, _ = case(seed, 2, pool)
        data = pdf_bytes(content, fonts)
        try:
            said = []
            for name in ("pdfium", "pure"):
                doc = pdf.resolve(name).open(data)
                try:
                    page = doc[0]
                    chars = page.chars()
                    said.append(([{k: v for k, v in dataclasses.asdict(c).items() if k != "font_id"} for c in chars],
                                 page.glyph_widths([(c.font_id, c.c, c.size) for c in chars])))
                finally:
                    doc.close()
        except Exception as e:  # noqa: BLE001
            crashed.append((seed, repr(e)[:200]))
            continue
        if said[0] != said[1]:
            a, b = said
            k = next((i for i, (x, y) in enumerate(zip(a[0], b[0])) if x != y), None)
            why = (f"char {k}: pdfium {a[0][k]} pure {b[0][k]}" if k is not None
                   else f"{len(a[0])} chars vs {len(b[0])}" if len(a[0]) != len(b[0]) else "glyph widths")
            apart.append((seed, [f.name for f in fonts], why[:400]))
    return {"n": n, "apart": apart, "crashed": crashed}


def run(name: str, module: str, args: list[str], out: Path, timeout: float) -> dict:
    folder = out / name
    folder.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", f"beamer2slides.devtools.{module}", *[a.replace("{out}", str(folder)) for a in args]]
    t = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
        code, text = p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired as e:
        code, text = -1, f"timeout after {timeout} s\n" + (e.stdout or b"").decode("utf-8", "replace")[-4000:]
    (folder / "log.txt").write_text(text, encoding="utf-8")
    # truetype/marked-content print their counts and exit 0: an APART line or a non-empty tally fails
    lines = text.strip().splitlines()
    if code == 0 and module in ("truetype_torture", "marked_content_torture"):
        last = next((ln for ln in reversed(lines) if ln.startswith("done")), "")
        if not last or "APART" in text or "crash" in text or (last.endswith("}") and not last.endswith("{}")):
            code = 1
    return {"check": name, "exit": code, "seconds": round(time.time() - t, 1), "tail": lines[-12:]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="out/platform-check")
    ap.add_argument("--only", nargs="*", help="names of the checks to run")
    ap.add_argument("--timeout", type=float, default=1800)
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    import pypdfium2.version as pv
    checks = list(ORACLES)
    decks = DECKS.is_dir() and any(DECKS.glob("*.pdf"))
    if decks:
        checks += WITH_DECKS
    if args.only:
        checks = [c for c in checks if c[0] in args.only]
    summary = {"platform": platform.platform(), "python": sys.version, "pdfium": str(pv.PDFIUM_INFO),
               "pypdfium2": str(pv.PYPDFIUM_INFO), "decks": decks, "fonts": font_folders(), "faces": folder_faces(), "checks": []}
    failed = []
    for name, module, cargs in checks:
        r = run(name, module, cargs, out, args.timeout)
        summary["checks"].append(r)
        print(f"{name:22s} exit {r['exit']:3d}  {r['seconds']:7.1f} s  {r['tail'][-1] if r['tail'] else ''}", flush=True)
        if r["exit"] != 0:
            failed.append(name)
    for pool in ("any", "installed"):
        name = f"subst-extract-{pool}"
        if args.only and name not in args.only:
            continue
        t = time.time()
        r = subst_extract(0, 150, pool)
        r.update(check=name, seconds=round(time.time() - t, 1), exit=1 if r["apart"] or r["crashed"] else 0)
        summary["checks"].append(r)
        print(f"{name:22s} exit {r['exit']:3d}  {r['seconds']:7.1f} s  apart {len(r['apart'])} crashed {len(r['crashed'])}",
              flush=True)
        if r["exit"]:
            failed.append(name)
    summary["failed"] = failed
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print("failed:", failed or "none")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
