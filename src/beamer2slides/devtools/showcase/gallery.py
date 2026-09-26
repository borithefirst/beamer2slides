"""The gallery site: for each picked slide, Google's thumbnail, the adopted PDF's page, the frame that
draws it and the preamble lines it names; per deck, a view-only link and the adopted source.

  write(out, tag)   out/index.html, out/img/*.jpg, out/src/<deck>.zip (no fonts: FONTS.txt names them)
"""

import json
import re
import zipfile
from importlib import resources
from pathlib import Path

from beamer2slides.devtools.showcase.decks import CORPUS, MANIFEST

W = 1200

# deck -> one line on how it was made
MADE = {
    "hashing": "Built with the Slides API alone: Google's layouts, placeholders and bullets.",
    "talk": "A conference talk with a master and layouts of its own, imported from .pptx.",
    "review": "A quarterly review: KPI cards, a heatmap table, charts as pictures.",
    "bees": "A lesson for children: preset shapes, freeforms, a callout.",
    "water": "One lesson in Hebrew, Arabic, Japanese and Chinese.",
    "portfolio": "Full-bleed and cropped pictures, a script font.",
}

# (deck, slide, note, tags): what adopt wrote, and where it falls short
PICKS = [
    ("hashing", 2, "Nested bullets from Google's <em>Title and body</em> layout become nested <code>itemize</code>. "
                   "Inline code is <code>\\texttt</code>, the italic <em>O(1)</em> is <code>\\textit</code>, and the "
                   "title is the layout's <code>\\frametitle</code>.", ["lists", "text", "code", "theme"]),
    ("hashing", 3, "A diagram drawn with the Slides API. The buckets and keys are <code>\\sliderect</code>s in "
                   "named shape styles, and the pointers are <code>\\slideline</code>s with arrow tips.", ["shapes"]),
    ("hashing", 4, "Code typed into a shape. Each line is a <code>\\slidepar</code> with its indentation kept as "
                   "spaces, and the keywords are coloured <code>\\textbf</code> runs.", ["code", "text", "lists"]),
    ("hashing", 5, "A native table with a filled header row, right-aligned numbers and one highlighted "
                   "<code>\\cell</code>.", ["tables"]),
    ("talk", 1, "The title slide takes everything from its layout. The navy ground, the rings picture and the "
                "red rule are the recovered theme's <code>title-slide</code> template. The frame keeps only the "
                "words and the speaker note.", ["theme", "pictures", "fonts"]),
    ("talk", 3, "Stat cards in Poppins. The API does not report a rounded rectangle's corner radius, so adopt "
                "uses the preset's default, and the cards come out rounder than in the deck.",
     ["shapes", "fonts", "theme"]),
    ("talk", 4, "A timeline made of an arrow, <code>\\slideellipse</code> day markers with centred labels, and "
                "boxes anchored to their top or bottom edge (<code>slidebox[bottom,center]</code>).",
     ["shapes", "theme"]),
    ("review", 2, "KPI cards. Text styles are declared once in the preamble, and the ring charts are "
                  "<code>\\slidepicture</code>s.", ["pictures", "shapes", "text"]),
    ("review", 3, "A heatmap table. The fifteen cell fills are recovered as named colours, with white text on "
                  "the dark cells and white rules between cells.", ["tables"]),
    ("bees", 1, "Preset hexagons become <code>\\slideshape</code>s, with their outlines in "
                "<code>shapes/*.tex</code>. The API gives no path for the star and the cloud, so both are traced "
                "from the thumbnail into <code>\\slidefreeform</code>s. The bee is ellipses and rectangles.",
     ["shapes", "fonts"]),
    ("bees", 2, "Cards and a callout. The API reports no adjustment handles, so the callout's tail goes where "
                "the preset puts it by default (below the box, not at the bee), and the card corners are "
                "rounder.", ["shapes", "fonts"]),
    ("water", 2, "Hebrew, right to left. The numbered list is an <code>enumerate</code> in a "
                 "<code>lang=hebrew</code> box, set in the deck's Noto Sans Hebrew. The diagram shows where "
                 "tracing fails: the outlined cloud became a rectangle, and the wavy sea edge a flat strip.",
     ["rtl", "lists", "shapes"]),
    ("water", 4, "Japanese. Babel follows the characters (<code>onchar=ids</code>), so the list is set in Noto "
                 "Sans JP with Slides' proportional punctuation.", ["cjk", "lists", "shapes"]),
    ("water", 6, "One table in five languages. Each cell names its font, and the Hebrew and Arabic cells are "
                 "<code>lang=hebrew</code> and <code>lang=arabic</code>.", ["tables", "rtl", "cjk"]),
    ("portfolio", 1, "A full-bleed picture under a dark band, with the title in Dancing Script. The deck's "
                     "Google Fonts are fetched and loaded with <code>fontspec</code>.", ["pictures", "fonts"]),
    ("portfolio", 2, "Four pictures cropped square with <code>trim=</code>, captioned in Dancing Script and "
                     "Playfair Display italic.", ["pictures", "fonts"]),
    ("portfolio", 4, "Outlined circles with centred numerals, joined by stealth-tipped "
                     "<code>\\slideline</code>s.", ["shapes", "fonts"]),
]


def slide_url(deck: dict, n: int) -> str:
    return f"https://docs.google.com/presentation/d/{deck['id']}/edit?usp=sharing#slide=id.{deck['pages'][n - 1]}"


def frame_parts(run: Path, n: int) -> dict:
    """The frame, the preamble lines it names, and the theme layout it draws on."""
    from beamer2slides.devtools.adopt_bench import split_frames
    pre, frames, _ = split_frames((run / "tree" / "main.tex").read_text(encoding="utf-8"))
    frame = dict(frames)[n].rstrip()
    if "\\end{frame}" in frame:
        frame = re.sub(r"\n\s*\\end\{frame\}.*", "\n\\\\end{frame}", frame, flags=re.S)
    used = []
    for line in pre.split("\n"):
        m = re.match(r"\\(slidestyle|slideshapestyle|definecolor|colorlet)\{([^}]+)\}", line)
        if m and re.search(r"(?<![\w-])" + re.escape(m.group(2)) + r"(?![\w-])", frame):
            used.append(line)
        m = re.match(r"\\setslidelist\{(\w+)\}", line)
        if m and "\\begin{" + m.group(1) + "}" in frame:
            used.append(line)
    themes = [t for t in re.findall(r"\\usetheme\{([^}]+)\}", pre) if t != "default"]
    theme = themes[-1] if themes else None
    layout = None
    lay = re.search(r"layout=([^,\]]+)", frame)
    sty = run / "tree" / f"beamertheme{theme}.sty" if theme else None
    if lay and sty and sty.exists():
        st = sty.read_text(encoding="utf-8").replace("\r\n", "\n")
        m = re.search(r"(% layout [^\n]*\n)?\\defbeamertemplate\{background\}\{" + re.escape(lay.group(1))
                      + r"\}.*?\n\}\}\n", st, re.S)
        if m:
            layout = m.group(0).rstrip()
            dm = re.search(r"\\drawmaster\{([^}]+)\}", layout)
            mm = dm and re.search(r"\\defmaster\{" + re.escape(dm.group(1)) + r"\}.*?\n\}\n", st, re.S)
            if mm:
                layout = mm.group(0).rstrip() + "\n\n" + layout
    return {"frame": frame, "preamble": used, "theme": theme, "layout": layout}


def family_of(stem: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)


def source_zip(tree: Path, dest: Path, deck: dict) -> None:
    """The adopted tree without its fonts: those are the deck's Google Fonts (static instances adopt
    made) and, where a deck needs one, a face from the machine that ran adopt - not ours to hand out."""
    fonts = sorted(p.name for p in (tree / "fonts").glob("*") if not p.name.endswith("-LICENSE.txt")) \
        if (tree / "fonts").is_dir() else []
    families = sorted({f.split("-")[0] for f in fonts})
    lines = [f"Adopted from the Google Slides deck \"{deck['title']}\"",
             f"  https://docs.google.com/presentation/d/{deck['id']}/view", "",
             "Compile with:  lualatex main.tex", "",
             "main.tex loads these files from fonts/, which this archive leaves out. `beamer2slides adopt`",
             "writes them (static instances of the deck's Google Fonts, SIL Open Font License):", ""]
    lines += [f"  {family_of(f)}: https://fonts.google.com/specimen/{family_of(f).replace(' ', '+')}"
              for f in families]
    lines += ["", "Files expected in fonts/:"] + [f"  {f}" for f in fonts]
    dest.parent.mkdir(parents=True, exist_ok=True)
    # one fixed date, so a rebuild changes gh-pages only where a source changed
    def put(z, name, data):
        z.writestr(zipfile.ZipInfo(f"{dest.stem}/{name}", (2026, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(tree.rglob("*")):
            rel = p.relative_to(tree)
            if p.is_file() and rel.parts[0] != "fonts":
                put(z, rel.as_posix(), p.read_bytes())
        put(z, "FONTS.txt", "\n".join(lines) + "\n")


def write(out: Path, tag: str = "showcase") -> Path:
    import pypdfium2 as pdfium
    from PIL import Image
    decks = json.loads(MANIFEST.read_text(encoding="utf-8"))
    (out / "img").mkdir(parents=True, exist_ok=True)
    items, deck_rows, pdfs, results = [], [], {}, {}
    for name, deck in decks.items():
        run = CORPUS / name / "runs" / tag
        pdfs[name] = pdfium.PdfDocument(str(run / "work" / "build" / "main.pdf"))
        results[name] = json.loads((run / "result.json").read_text(encoding="utf-8"))
        source_zip(run / "tree", out / "src" / f"{name}.zip", deck)
        cover = Image.open(CORPUS / name / "slides" / "001.png").convert("RGB")
        cover.resize((640, round(640 * cover.height / cover.width)), Image.LANCZOS) \
            .save(out / "img" / f"{name}-cover.jpg", quality=84)
        deck_rows.append({"deck": name, "title": deck["title"], "made": MADE[name], "slides": results[name]["n"],
                          "boxes": results[name]["bootstrap_mean"]["boxes"],
                          "url": f"https://docs.google.com/presentation/d/{deck['id']}/edit?usp=sharing",
                          "zip": f"src/{name}.zip"})
    for name, n, note, tags in PICKS:
        run = CORPUS / name / "runs" / tag
        page = pdfs[name][n - 1]
        wpt, hpt = page.get_size()
        page.render(scale=W / wpt).to_pil().convert("RGB").save(out / "img" / f"{name}-{n}-tex.jpg", quality=86)
        im = Image.open(CORPUS / name / "slides" / f"{n:03d}.png").convert("RGB")
        im.resize((W, round(W * im.height / im.width)), Image.LANCZOS).save(out / "img" / f"{name}-{n}-deck.jpg",
                                                                             quality=86)
        score = next(b for b in results[name]["bootstrap"] if b["slide"] == n)
        items.append({"deck": name, "slide": n, "title": decks[name]["title"], "url": slide_url(decks[name], n),
                      "note": note, "tags": tags, "boxes": score["boxes"], "pixels": score["pixels"],
                      "size": [round(wpt, 1), round(hpt, 1)], **frame_parts(run, n)})
    every = [b["boxes"] for r in results.values() for b in r["bootstrap"]]
    data = {"items": items, "decks": deck_rows, "slides": len(every), "mean": sum(every) / len(every)}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = resources.files("beamer2slides.devtools.showcase").joinpath("template.html").read_text(encoding="utf-8")
    (out / "index.html").write_text(page.replace("__DATA__", blob), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"{len(items)} examples from {len(deck_rows)} decks -> {out / 'index.html'}")
    return out / "index.html"


# the README's animation: the divider sweeping across slides where the two renders are hardest to tell
# apart, flat colours so the GIF stays small
SWIPES = [("talk", 1), ("review", 3), ("hashing", 3), ("water", 6)]


def swipe_gif(site: Path, dest: Path, width: int = 800) -> Path:
    """Google's render left of a moving divider, the adopted PDF right of it, from a built site's img/."""
    import math

    from PIL import Image, ImageDraw, ImageFont
    blue, frames, durations = (51, 51, 178), [], []
    try:
        font = ImageFont.truetype("arialbd.ttf", 15)
    except OSError:
        font = ImageFont.load_default()

    def tag(d, xy, words, anchor):
        box = d.textbbox(xy, words, font=font, anchor=anchor)
        d.rounded_rectangle((box[0] - 8, box[1] - 5, box[2] + 8, box[3] + 5), 6, fill=(20, 22, 30))
        d.text(xy, words, font=font, fill=(255, 255, 255), anchor=anchor)

    for name, n in SWIPES:
        a, b = (Image.open(site / "img" / f"{name}-{n}-{k}.jpg").convert("RGB") for k in ("deck", "tex"))
        size = (width, round(width * a.height / a.width))
        a, b = a.resize(size, Image.LANCZOS), b.resize(size, Image.LANCZOS)
        steps, first = 30, len(frames)
        for i in range(steps + 1):
            u = i / steps
            cut = round(size[0] * (0.5 + 0.42 * math.sin(u * 2 * math.pi)))
            im = a.copy()
            im.paste(b.crop((cut, 0, size[0], size[1])), (cut, 0))
            d = ImageDraw.Draw(im)
            d.rectangle((cut - 1, 0, cut + 1, size[1]), fill=blue)
            y = size[1] // 2
            d.ellipse((cut - 16, y - 16, cut + 16, y + 16), fill=blue)
            d.polygon([(cut - 10, y), (cut - 3, y - 6), (cut - 3, y + 6)], fill="white")
            d.polygon([(cut + 10, y), (cut + 3, y - 6), (cut + 3, y + 6)], fill="white")
            tag(d, (14, 14), "Google Slides", "lt")
            tag(d, (size[0] - 14, 14), "adopted beamer PDF", "rt")
            frames.append(im)
            durations.append(700 if i in (0, steps) else 50)
        # one palette per slide, so a frame differs from the last only around the divider and the GIF
        # stores just that strip
        both = Image.new("RGB", (size[0], size[1] * 2))
        both.paste(a, (0, 0)); both.paste(b, (0, size[1]))
        ImageDraw.Draw(both).rectangle((0, 0, 40, size[1]), fill=blue)  # the divider's colour survives
        tag(ImageDraw.Draw(both), (14, 14), "Google Slides", "lt")
        palette = both.quantize(colors=96, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        frames[first:] = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames[first:]]
    pal = frames
    dest.parent.mkdir(parents=True, exist_ok=True)
    pal[0].save(dest, save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True)
    print(f"{len(frames)} frames -> {dest} ({dest.stat().st_size // 1024} KB)")
    return dest
