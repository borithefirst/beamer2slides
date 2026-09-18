"""The pictures on the front page, made from a conversion that already happened.

    python tools/readme_images.py [--out docs/media] [--deck out/demo] [--pdf examples/demo/demo.pdf]

Everything here is drawn from files a `convert` + `fidelity` run leaves behind - the PDF pages, the
deck.json the classifier wrote, and the thumbnails Google's own renderer returned - so the front
page shows the real thing and can be remade after any change, with no Google call and no TeX.

`out/demo` is the conversion of `examples/demo/demo.tex`, a talk written to hold one of everything:
a TikZ pipeline, beamer blocks, a table, nested lists, inline and display math.

The sync and adopt pictures are drawn from live runs `tools/readme_demos.py` makes (out/demo-sync,
out/demo-adopt); without them those two are skipped.
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


def titled(im: Image.Image, stage: str, note: str, width: int) -> Image.Image:
    """One frame of the loop: a stage's name, what it did, and what it left."""
    body = framed(im.convert("RGB").resize((width, round(width * im.height / im.width))))
    out = Image.new("RGB", (width, body.height + 74), PAPER)
    d = ImageDraw.Draw(out)
    d.text((4, 8), stage, font=font(30, bold=True), fill=INK)
    d.text((4 + d.textlength(stage, font=font(30, bold=True)) + 16, 14), note, font=font(22), fill=MUTED)
    out.paste(body, (0, 62))
    return out


def loop_gif(pdf: Path, deck: Path, out: Path, page: int, slide: int, width: int = 900) -> Path:
    """The four stages on one slide, as four frames: what each one sees and what it hands on."""
    frames = [
        titled(page_png(pdf, page, width), "extract", "the PDF, as PDFium reads it", width),
        # debug/ and backgrounds/ count kept slides, like the deck; the PDF counts overlay steps
        titled(Image.open(deck / "debug" / f"slide-{slide + 1:03}.png"), "classify",
               "what is text, what is a shape, what is a figure", width),
        titled(Image.open(deck / "backgrounds" / f"bg-{slide + 1:03}.png"), "render",
               "what is left for a picture — here, only the theme", width),
        titled(Image.open(deck / "fidelity" / f"slides-{slide + 1:03}.png"), "emit",
               "the deck, in Google Slides, editable", width)]
    h = max(f.height for f in frames)
    even = []
    for f in frames:
        pad = Image.new("RGB", (width, h), PAPER)
        pad.paste(f, (0, 0))
        even.append(pad.convert("P", palette=Image.ADAPTIVE, colors=96))
    path = out / "stages.gif"
    even[0].save(path, save_all=True, append_images=even[1:], duration=[1700, 2200, 2000, 2600],
                 loop=0, optimize=True)
    return path


def code_panel(lines: list[str], width: int, size: int = 21) -> Image.Image:
    """Source lines on a light ground, in a code face: what adopt wrote, verbatim."""
    face = FONTS / "GoogleSansCode-Regular.ttf"
    mono = ImageFont.truetype(str(face), size) if face.exists() else ImageFont.load_default(size)
    pitch, pad = round(size * 1.45), 20
    out = Image.new("RGB", (width, 2 * pad + pitch * len(lines)), (248, 249, 250))
    d = ImageDraw.Draw(out)
    for i, line in enumerate(lines):
        d.text((pad, pad + i * pitch), line, font=mono, fill=(60, 64, 67))
    return framed(out)


def adopt_pair(deck: Path, adopted: Path, out: Path, slide: int) -> Path | None:
    """A deck read back from Google Slides, and the beamer source `adopt` wrote for it, compiled.

    `adopted` is an adopt tree (`main.tex`, `main.pdf`) made from the live deck read as one nobody
    converted: nothing in it came from the PDF the deck was converted from."""
    tex, pdf = adopted / "main.tex", adopted / "main.pdf"
    if not (tex.exists() and pdf.exists()):
        print(f"no adopt tree in {adopted}: adopt.png not made")
        return None
    w = 760
    a = caption_under(framed(Image.open(deck / "fidelity" / f"slides-{slide + 1:03}.png").resize(
        (w, round(w * 9 / 16))).convert("RGB")), "a deck in Google Slides", "shapes, text boxes, a backdrop")
    b = caption_under(framed(page_png(pdf, 0, w)), "adopt: a beamer source for it",
                      "compiled by pdflatex, every box where the deck has it")
    src = tex.read_text(encoding="utf-8").splitlines()
    # the first node of the flow chart: its rounded box, then its label on top
    at = next(i for i, l in enumerate(src) if "rounded corners" in l and "draw=" in l) - 2
    code = code_panel(src[at:at + 9], a.width + 34 + b.width)
    both = side_by_side(a, b)
    page = Image.new("RGB", (both.width, both.height + 18 + code.height), PAPER)
    page.paste(both, (0, 0))
    page.paste(code, (0, both.height + 18))
    path = out / "adopt.png"
    page.save(path)
    return path


DECK_EDIT, SOURCE_EDIT = (24, 128, 56), (26, 115, 232)


def sync_story(folder: Path, out: Path, page: int) -> Path | None:
    """A live sync, in three pictures: the deck someone edited, the talk rewritten meanwhile, and the
    deck after `sync` - both sets of changes, with a box around each.

    `folder` is a converted out folder with `shots/2-edited.{png,json}` and `shots/3-synced.{png,json}`
    (thumbnail and presentations.get of the slide, before and after the sync) and the rewritten
    source's PDF in `src/`. The boxes come from the read-back, found by the words each edit touched."""
    from beamer2slides.devtools.sync_check import Model
    shots = folder / "shots"
    if not all((shots / f"{n}.{x}").exists() for n in ("2-edited", "3-synced") for x in ("png", "json")):
        print(f"no sync shots in {shots}: sync.png not made")
        return None
    deck_edits = ["Slides elements", "Love this slide"]
    source_edits = ["Pictures where it is not (yet)", "still move and resize"]

    def boxes(name: str, phrases: list[str]) -> list[list[float]]:
        m = Model(json.loads((shots / f"{name}.json").read_text(encoding="utf-8")))
        s = m.one({"title": "Pipeline"})
        return [e.box for e in s.elements if e.kind == "shape" and any(p in e.text for p in phrases)]

    w = 500

    def panel(im: Image.Image, marks: list[tuple[list[list[float]], tuple]]) -> Image.Image:
        im = im.convert("RGB").resize((w, round(w * 9 / 16)))
        d = ImageDraw.Draw(im)
        px = w / 720                                    # read-back boxes are in slide pt, 720 wide
        for bs, colour in marks:
            for x0, y0, x1, y1 in bs:
                d.rounded_rectangle([x0 * px - 4, y0 * px - 4, x1 * px + 4, y1 * px + 4], 6, outline=colour, width=3)
        return framed(im)

    edited = boxes("2-edited", deck_edits)
    synced_deck, synced_source = boxes("3-synced", deck_edits), boxes("3-synced", source_edits)
    # the rewrite touched one block, title and body: one box around it reads better than two
    synced_source = [[f(b[k] for b in synced_source) for k, f in enumerate((min, min, max, max))]]
    a = caption_under(panel(Image.open(shots / "2-edited.png"), [(edited, DECK_EDIT)]),
                      "1. Someone edits the deck", "green words, a comment of their own")
    b = caption_under(panel(page_png(folder / "src" / "demo.pdf", page, w * 2), [(synced_source, SOURCE_EDIT)]),
                      "2. You rewrite the talk", "a new title, a longer sentence")
    c = caption_under(panel(Image.open(shots / "3-synced.png"),
                            [(synced_deck, DECK_EDIT), (synced_source, SOURCE_EDIT)]),
                      "3. sync", "both - and nothing anyone typed is lost")
    path = out / "sync.png"
    side_by_side(side_by_side(a, b, 26), c, 26).save(path)
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--deck", type=Path, default=ROOT / "out" / "demo")
    ap.add_argument("--pdf", type=Path, default=ROOT / "examples" / "demo" / "demo.pdf")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "media")
    ap.add_argument("--page", type=int, default=4, help="PDF page (0-based) for the hero")
    ap.add_argument("--slide", type=int, default=3, help="the deck slide that page became")
    ap.add_argument("--adopted", type=Path, default=ROOT / "out" / "demo-adopt" / "tree",
                    help="an adopt tree of the --deck's slide, for adopt.png")
    ap.add_argument("--synced", type=Path, default=ROOT / "out" / "demo-sync",
                    help="a converted folder with shots/ of a live sync, for sync.png")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for path in (hero(args.pdf, args.deck, args.out, args.page, args.slide),
                 elements(args.deck, args.out, args.slide),
                 fidelity(args.deck, args.out, args.slide),
                 loop_gif(args.pdf, args.deck, args.out, args.page, args.slide),
                 adopt_pair(args.deck, args.adopted, args.out, args.slide),
                 sync_story(args.synced, args.out, args.page)):
        if path is not None:
            print(path.relative_to(ROOT), Image.open(path).size)


if __name__ == "__main__":
    main()
