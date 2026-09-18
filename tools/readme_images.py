"""The pictures on the front page, made from a conversion that already happened.

    python tools/readme_images.py [--out docs/media] [--deck out/demo] [--pdf examples/demo/demo.pdf]

Everything here is drawn from files a `convert` + `fidelity` run leaves behind - the PDF pages, the
deck.json the classifier wrote, and the thumbnails Google's own renderer returned - so the front
page shows the real thing and can be remade after any change, with no Google call and no TeX.

`out/demo` is the conversion of `examples/demo/demo.tex`, a talk written to hold one of everything:
a TikZ pipeline, beamer blocks, a table, nested lists, inline and display math.
"""
import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FONTS = ROOT / "themes" / "google" / "fonts"
INK, MUTED, PAPER = (32, 33, 36), (95, 99, 104), (255, 255, 255)
# what a reader is being shown, by the element kind the classifier wrote
KINDS = {"text": ((16, 137, 62), "text box"), "shape": ((232, 113, 10), "shape"),
         "diagram": ((124, 58, 183), "diagram"), "table": ((26, 115, 232), "table"),
         "image": ((26, 115, 232), "picture")}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "GoogleSansFlex-Bold.ttf" if bold else "GoogleSansFlex-Regular.ttf"
    if (FONTS / name).exists():
        return ImageFont.truetype(str(FONTS / name), size)
    return ImageFont.load_default(size)


def page_png(pdf: Path, page: int, width: int) -> Image.Image:
    from beamer2slides.pdf import Document
    doc = Document(pdf)
    try:
        return Image.fromarray(doc[page].render(width / doc[page].width)).convert("RGB")
    finally:
        doc.close()


def framed(im: Image.Image, colour=(218, 220, 224)) -> Image.Image:
    ImageDraw.Draw(im).rectangle([0, 0, im.width - 1, im.height - 1], outline=colour, width=2)
    return im


def caption_under(im: Image.Image, text: str, note: str = "") -> Image.Image:
    """The picture with a line of its own under it."""
    pad, line = 14, 34 + (24 if note else 0)
    out = Image.new("RGB", (im.width, im.height + pad + line), PAPER)
    out.paste(im, (0, 0))
    d = ImageDraw.Draw(out)
    d.text((2, im.height + pad), text, font=font(26, bold=True), fill=INK)
    if note:
        d.text((2, im.height + pad + 30), note, font=font(21), fill=MUTED)
    return out


def side_by_side(left: Image.Image, right: Image.Image, gap: int = 34) -> Image.Image:
    out = Image.new("RGB", (left.width + gap + right.width, max(left.height, right.height)), PAPER)
    out.paste(left, (0, 0))
    out.paste(right, (left.width + gap, 0))
    return out


def hero(pdf: Path, deck: Path, out: Path, page: int, slide: int) -> Path:
    """The same slide as TeX printed it and as Google now renders it."""
    w = 760
    a = caption_under(framed(page_png(pdf, page, w)), "beamer PDF", "what pdflatex printed")
    b = caption_under(framed(Image.open(deck / "fidelity" / f"slides-{slide + 1:03}.png").resize(
        (w, round(w * 9 / 16))).convert("RGB")),
        "Google Slides", "every word, shape and cell is editable")
    path = out / "hero.png"
    side_by_side(a, b).save(path)
    return path


def boxes_of(slide: dict) -> list[tuple[str, list[float]]]:
    """Every element a reader can click on in the deck, the parts of a diagram among them: the
    whole point is that a converted figure is shapes and words, not a picture of them."""
    out = []
    for el in slide["elements"]:
        if el.get("role") == "footer":
            continue                                    # the frame counter: true, but not the story
        out.append((el["kind"], el["bbox"]))
        for node in el.get("nodes", []):
            out.append(("shape", node["bbox"]))
    return out


def elements(deck: Path, out: Path, slide: int) -> Path:
    """Google's own rendering with a box around each thing the deck is made of."""
    im = Image.open(deck / "fidelity" / f"slides-{slide + 1:03}.png").convert("RGB")
    data = json.loads((deck / "deck.json").read_text(encoding="utf-8"))
    page = data["slides"][slide]
    px = im.width / page["size"][0]
    d = ImageDraw.Draw(im, "RGBA")
    shown = []
    for kind, bbox in boxes_of(page):
        colour, label = KINDS.get(kind, (MUTED, kind))
        x0, y0, x1, y1 = (v * px for v in bbox)
        d.rectangle([x0 - 3, y0 - 3, x1 + 3, y1 + 3], outline=colour + (255,), width=3)
        if label not in shown:
            shown.append(label)
    legend = Image.new("RGB", (im.width, 62), PAPER)
    d = ImageDraw.Draw(legend)
    x = 4
    for label in shown:
        colour = next(c for c, n in KINDS.values() if n == label)
        d.rectangle([x, 20, x + 26, 42], outline=colour, width=3)
        d.text((x + 36, 18), label, font=font(24), fill=INK)
        x += 36 + round(d.textlength(label, font=font(24))) + 34
    both = Image.new("RGB", (im.width, im.height + legend.height), PAPER)
    both.paste(framed(im), (0, 0))
    both.paste(legend, (0, im.height))
    path = out / "elements.png"
    caption_under(both, "Google's rendering of the converted slide",
                  "boxes drawn from the converter's own IR — each one is a separate Slides object").save(path)
    return path


def fidelity(deck: Path, out: Path, slide: int) -> Path:
    """The measurement the project is steered by, as it looks."""
    im = Image.open(deck / "fidelity" / f"diff-{slide + 1:03}.png").convert("RGB")
    scores = json.loads((deck / "fidelity.json").read_text(encoding="utf-8"))["slides"]
    page = next((p for p in scores if p.get("page") == slide), {})
    note = "black: ink both sides agree on   red: only the PDF   blue: only Slides"
    if page.get("text_overlap"):
        note += f"   —   {page['text_overlap']:.0%} of the ink on this slide lands on itself"
    path = out / "fidelity.png"
    caption_under(framed(im), "Measured on Google's renderer, not on a local preview", note).save(path)
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--deck", type=Path, default=ROOT / "out" / "demo")
    ap.add_argument("--pdf", type=Path, default=ROOT / "examples" / "demo" / "demo.pdf")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "media")
    ap.add_argument("--page", type=int, default=4, help="PDF page (0-based) for the hero")
    ap.add_argument("--slide", type=int, default=3, help="the deck slide that page became")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for path in (hero(args.pdf, args.deck, args.out, args.page, args.slide),
                 elements(args.deck, args.out, args.slide),
                 fidelity(args.deck, args.out, args.slide)):
        print(path.relative_to(ROOT), Image.open(path).size)


if __name__ == "__main__":
    main()
