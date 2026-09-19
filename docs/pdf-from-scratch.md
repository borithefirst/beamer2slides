# A PDF reader from scratch

`beamer2slides.pdf.pure` is a PDF reader in pure Python (fontTools for font programs, zlib for
Flate, nothing else) that answers the backend contract (docs/pdf-backend.md) **the way PDFium
does**, rendering excepted:

```
B2S_PDF_BACKEND=pure python -m beamer2slides classify deck.pdf
pip install beamer2slides[pure]      # fontTools
```

It exists to answer "could we replace PDFium with our own code?" with a measurement instead of a
guess. The answer, for everything except drawing pixels: yes, and the pipeline can't tell the
difference.

## Why "the way PDFium does" and not "correctly"

classify was tuned on what PDFium reports: a word gap is a gap because PDFium's text page put a
generated space there, a char box is the font's ascent and descent as PDFium normalises them, and a
figure's bounds come from PDFium's stroke outlines. A reader that is *correct* but different moves
thresholds, and every decision downstream moves with them: element ids, holes, what goes to the
background. So the target is PDFium's output, quirks included, and PDFium is the test oracle:
every quirk below was found as a diff against it and is reproduced on purpose.

## Layers

Each module ports the PDFium code it names (`pure/__init__.py` has the list), about 4,600 lines:

| module | PDFium | what |
|---|---|---|
| `syntax` | CPDF_SyntaxParser, CPDF_StreamParser | objects, content operators, inline images |
| `filters` | fpdf_parser_decode | Flate (with PNG predictors), LZW, ASCII85/Hex, RunLength; image codecs are passed through |
| `document` | CPDF_Parser, CPDF_Document, CPDF_PageLabel, CPDF_Creator | xref tables and streams, object streams, rebuilding a broken xref from the objects, page tree, labels, named destinations, writing a page subset |
| `colors`, `cmyk_table` | CPDF_ColorSpace | every colour space to PDFium's RGB (its CMYK table, float32 rounding) |
| `encodings`, `fonts` | CPDF_Font, CPDF_SimpleFont, CPDF_Type3Font, CPDF_CIDFont, CPDF_ToUnicodeMap, CPDF_CMap | encodings, ToUnicode, CMaps, widths, glyph boxes, font programs (Type 1, CFF, TrueType through fontTools) |
| `content` | CPDF_StreamContentParser, CPDF_PageObject | page objects with PDFium's matrices, rects, clips and stroke bounds |
| `textpage` | CPDF_TextPage, CFX_BidiChar | text order, generated spaces and line breaks, hyphens, ligature pieces, bidi, char boxes |
| `backend` | fpdf_* (the public API) | `api.PdfBackend` over all of it |

The API's shared rules live in `api.py` so both backends use one copy: `font_metrics` (the
ascent/descent normalisation and the CFF math-font box), `char_box`, `trace`.

## PDFium's behaviour, reproduced

Each of these was a diff against PDFium until it was ported:

- **Numbers are C floats.** CPDF_Number stores a real as a float, and so do the CTM, `re` corners,
  text matrix and position, TJ adjustments, text-page matrices, origins and char boxes, loose
  bounds and glyph widths. With plain doubles, 672 values across the test decks were off in the last
  rounded digit, and one of them swapped two blocks' order in `18_blocks_resize`. The reader rounds
  where PDFium *stores* a float (`syntax.float32`, `content.f32m`), not after every operation.
  Two values in the test decks' raw.json are still 0.01 apart.
- **A line-end hyphen is U+0002** in FPDFText_GetUnicode. Both backends turn it back into "-".
- **Glyph names use FreeType's psnames table**: the full legacy AGL (`fi` → U+FB01) plus the Zapf
  Dingbats names, not AGLFN (which has no `fi`, so the ligature's width came back 0).
- **A code with no Unicode** (kInvalidCharCode) comes back as U+FFFD, except in Type 3 fonts, which
  return the glyph code (the TS1 `\textbullet` as U+0088, `classify.TYPE3_SYMBOLS`).
- **Bidi** (CFX_BidiString, CPDF_TextPage::CloseTempLine): the segment list starts with an empty
  neutral segment. A line with at least as many right-to-left segments as left-to-right ones (and at
  least one) is written in reverse segment order, and each right-to-left segment backwards with
  mirrored brackets. `GetUnicodeNormalization` never returns nothing, so every character of such a
  segment, a generated space included, becomes a kPiece. A mirrored text matrix with right-to-left
  text swaps the object's characters too. Measured on the stress deck's Hebrew line.
- **wchar_t is 16 bits on Windows**, so PDFium's glyph width for a character beyond U+FFFF looked up
  a truncated code and was garbage. Both backends now answer None, which is what the contract says
  for "no glyph".
- **A generated character has no text object**, and pdfium_backend reused its colour buffers, so
  such a character kept the previous character's colour and alpha. Both backends now do this on
  purpose, and a comment says so.
- **Image metadata** (FPDFImageObj_GetImageMetadata): bpp is 1 for 1-bit, 8 up to 8 bits, else 24.
  dpi is pixels × 72 over the image matrix's (|a|+|c|, |b|+|d|). "Transparent" means an SMask, a
  Mask, an ImageMask, a blend, a turn or a clip.
- **A Type 3 glyph with an empty `d1` box** takes the union of the rects its procedure draws, parsed
  as a form (CPDF_Type3Char::InitializeFromStreamData / CalcBoundingBox). This moves the font's
  ascent, and with it every char box. The FontBBox is scaled in floats too: metropolis's bullet font
  has a matrix of 0.011, and `6 × 0.011 × 1000` is 66 in float32 but 65.9999996 in doubles, which
  floors to 65.
- **Page boxes**: the MediaBox falls back to Letter, and the crop box is intersected with it.
- **A broken xref table** is rebuilt from the `n 0 obj` markers, as PDFium's repair does.

## Measured

- **Every call, value for value**: on the 48 test decks (231 pages, 32,209 characters) and 2,782
  pages of theme talks, stress and sync decks, pure and PDFium agree on objects, bounds, drawings,
  images, embedded-image metadata, links, labels, destinations, chars and glyph widths, up to
  float32 noise.
- **The pipeline**: extract and classify on the reader write the same deck.json as on PDFium for all
  48 decks. In raw.json, 2 numbers are 0.01 apart.
- **Speed**: extract takes 4.2 s for the 48 decks against 0.6 s on PDFium, mostly in the text page and
  fontTools. Classify is the same either way.
- `tests/test_pure_pdf.py` holds this in the default run: seven decks call for call, the pipeline on
  the same seven, plus one test per quirk. `tests/test_pdf_backend.py` runs the contract suite on
  `pure` too; its render and save checks know it cannot draw.

## What it does not do

- **Rendering, mostly.** Option 3 below is under way (next section); until text, images, shadings and
  soft masks draw, `render` raises PdfError on any page holding one, and `api.renders(backend)` is
  False. The pipeline needs renders for backgrounds, crops, ball colours, `_looks_like` and fidelity,
  so `classify` runs on the reader but `convert` does not. `embedded_image` gives no `pixels` or `rendered`, so
  `render.image_file` can't prove a raw JPEG looks right and keeps the page crop instead.
- Encrypted PDFs (LaTeX doesn't write them), vertical writing, ActualText, and JPX/JBIG2/CCITT
  decoding (those streams pass through as raw).

## Rendering: the options

What stands between the reader and a whole `convert`:

1. **Keep a rasteriser for pixels only.** The reader decides what is text, what is a figure and where
   everything is; PDFium or MuPDF draws, as a sandboxed worker. This is the smallest step, and it
   removes the library from the parsing path, where hostile input does its work.
2. **Draw with a 2D library** (skia-python, cairo, or Pillow's ImageDraw with supersampling): paths,
   clips, shadings, images and glyph outlines from fontTools, with soft masks and blend modes. Beamer
   decks need a small subset (no JPX, few blend modes, but soft masks everywhere: shadows, balls).
   The catch is anti-aliasing. Classify doesn't care, but backgrounds and the `_looks_like` checks
   compare pixels with thresholds tuned on PDFium's rasteriser, and fidelity is measured against a
   PDFium render.
3. **Port PDFium's rasteriser (AGG) itself.** The only way to get byte-identical backgrounds. That is
   a large port, and pure Python would be far too slow without numpy vectorisation of the scanline
   filler.

## Rendering: the port (option 3)

`pure/raster.py` is AGG's scanline rasteriser, stroker and dasher as PDFium configures them;
`pure/render.py` is CPDF_RenderStatus and CFX_AggDeviceDriver above it: clip masks, the gray8 clip
blend, AlphaMerge/AlphaUnion compositing into BGRx or BGRA, and DrawFillStrokePath (a translucent
stroke over its fill is a knockout on a sub-bitmap). Equality means equal bytes, and three things
decided it:

- **Float32 everywhere PDFium has a float**, rounded after every operation, not once at the end:
  `CFX_Matrix::operator*` computes `e*B + f*D + G` as float + float + float, and a matrix composed in
  double then rounded differs in the last bit - enough to move one pixel's coverage by one level.
  That goes for `cm` and the text matrix in the content parser too.
- **Parser details only a renderer sees**: `b*` always closes to the path start (a lone `m` then
  paints a round-capped dot, `b` and `s` do not); a form's /BBox clip keeps the corner order written,
  since the rasteriser walks edges in that order.
- **What is drawn at all**: a path whose stroke bounds overflow, or whose sub-bitmap would be empty,
  draws nothing.

The oracle is `devtools/render_torture.py` (`python tools/render_torture.py SEED0 N [--forms]`):
random pages of `cm`, clips, colours, line styles, dashes, constant alpha and paths painted every way,
optionally inside nested forms with random /BBox and /Matrix, rendered by both at random zooms on
white and on clear bitmaps, each difference shrunk to the lines that still cause it. When paths were
done: 5,500 seeds of pages and 500 with forms, not one pixel apart; `tests/test_pure_pdf.py` keeps
40 seeds of each plus the shrunk pages that were once apart. `render_page` refuses (`unported`) what
it does not draw yet: text, images, shadings, patterns, transparency groups, soft masks, blend modes,
transfer functions.

## Risks

- **PDFium changes.** The reader matches the pypdfium2 build it was measured against. A PDFium
  update that moves a rule moves the oracle, and `tests/test_pure_pdf.py` fails. That failure is the
  point: it says which of the two to follow.
- **Coverage is the test corpus.** Everything here comes from LaTeX PDFs (pdflatex, xelatex, lualatex,
  28 themes). A PDF from another producer will find unported paths: other font types, broken files
  of other kinds, ActualText.
- **Speed** is 7× PDFium's on extract. That is fine next to Google's round trips, but not for the
  invariants suite if pure became its default.

## A plan, if it goes further

1. Done: the reader, parity with PDFium on every non-rendering call, and pipeline parity on classify.
2. Run it under `sandbox:pure` and fuzz both readers with mutated test PDFs. Any crash or any
   diff from PDFium is a finding.
3. Rendering option 1 (a sandboxed rasteriser behind the reader), so that `convert` runs with the
   reader deciding everything.
4. Option 2 only if dropping the native library becomes a goal in itself. Measure it with
   `tools/leftovers.py`, the invariants suite and the fidelity scores, not byte equality.
