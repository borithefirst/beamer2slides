"""What PDFium gives for every raster image in a PDF, and which route can carry it losslessly.

Evidence for render.image_file's decision table: for each image XObject drawn on a page it
prints the geometry (matrix, drawn box, clip), the stream (filters, colour space, bits, the
raw bytes' magic and sha1), PDFium's own decode (FPDFImageObj_GetBitmap) and the rasterisation
that applies matrix, mask and colour space (FPDFImageObj_GetRenderedBitmap), plus two checks
that decide whether the stored bytes may be handed to Slides as they are:

- `raw==pdfium`: the raw stream decoded by Pillow equals PDFium's decode (no decode array, no
  palette or colour space Pillow reads differently);
- `sha1 of` : the raw stream is byte for byte a file in --files (the author's include).

Usage:
    python tools/probe_pdf_images.py tests/decks/out/07_images.pdf [more.pdf ...]
        [--files tests/decks/img] [--dump out/probe-images]
"""

import argparse
import hashlib
import io
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from beamer2slides.pdf import JPEG_MAGIC, OBJ_IMAGE, PNG_MAGIC, Document  # noqa: E402
from beamer2slides.render import image_file  # noqa: E402


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def magic(data: bytes) -> str:
    if data[:3] == JPEG_MAGIC:
        return "JPEG"
    if data[:8] == PNG_MAGIC:
        return "PNG"
    return data[:4].hex() if data else "-"


def pillow_pixels(data: bytes) -> np.ndarray | None:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        return np.array(img.convert("RGB"))
    except Exception:
        return None


def compare(a: np.ndarray | None, b: np.ndarray | None) -> str:
    if a is None or b is None:
        return "n/a"
    if a.shape[:2] != b.shape[:2]:
        return f"size {a.shape[1]}x{a.shape[0]} vs {b.shape[1]}x{b.shape[0]}"
    d = np.abs(a[..., :3].astype(int) - b[..., :3].astype(int))
    return f"max {d.max()} mean {d.mean():.2f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--files", type=Path, default=None, help="folder of the author's image files")
    ap.add_argument("--dump", type=Path, default=None, help="write every route's file there")
    args = ap.parse_args(argv)

    originals = {}
    if args.files:
        for p in sorted(args.files.iterdir()):
            if p.is_file():
                originals[sha1(p.read_bytes())] = p.name

    for pdf in args.pdfs:
        print(f"\n=== {pdf} ===")
        doc = Document(pdf)
        try:
            for page in doc:
                for k, po in enumerate(page.objects()):
                    if po.type != OBJ_IMAGE:
                        continue
                    im = page.embedded_image(po)
                    if im is None:
                        continue
                    a, b, c, d, e, f = im.matrix
                    box = ", ".join(f"{v:.2f}" for v in im.box)
                    print(f"\npage {page.index + 1} object {k}")
                    print(f"  box      [{box}]  {im.box[2] - im.box[0]:.1f} x {im.box[3] - im.box[1]:.1f} pt")
                    print(f"  matrix   a={a:.3f} b={b:.3f} c={c:.3f} d={d:.3f} e={e:.2f} f={f:.2f}"
                          f"  upright={im.upright} clipped={im.clipped}")
                    print(f"  image    {im.px[0]} x {im.px[1]} px, {im.bpp} bpp, {im.colorspace}, "
                          f"dpi {im.dpi[0]:.0f}x{im.dpi[1]:.0f}, filters {im.filters or ['-']}")
                    print(f"  raw      {len(im.raw)} bytes {magic(im.raw)} sha1 {sha1(im.raw)[:12]}"
                          f"  decoded {im.decoded_size} bytes")
                    if sha1(im.raw) in originals:
                        print(f"  sha1 of  {originals[sha1(im.raw)]}  (the author's file, byte for byte)")
                    px = im.pixels
                    print(f"  bitmap   {'-' if px is None else f'{px.shape[1]} x {px.shape[0]} x {px.shape[2]}'}"
                          f"  blended={im.blended} transparent={im.transparent}")
                    rendered = page.rendered_image(po)
                    print(f"  rendered {'-' if rendered is None else f'{rendered.shape[1]} x {rendered.shape[0]} x {rendered.shape[2]}'}")
                    print(f"  raw==pdfium  {compare(pillow_pixels(im.raw), px)}")
                    print(f"  rendered==pdfium  {compare(rendered, px)}")
                    chosen = image_file(page, po)
                    if chosen is None:
                        print("  verdict  page crop (nothing here may stand in for it)")
                    else:
                        data, ext, size, route = chosen
                        print(f"  verdict  {route}: {len(data)} bytes .{ext}, {size[0]} x {size[1]} px"
                              f"{'  (author bytes)' if sha1(data) in originals else ''}")
                        if args.dump:
                            args.dump.mkdir(parents=True, exist_ok=True)
                            out = args.dump / f"{pdf.stem}-p{page.index + 1}-{k}.{ext}"
                            out.write_bytes(data)
                            print(f"  wrote    {out}")
        finally:
            doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
