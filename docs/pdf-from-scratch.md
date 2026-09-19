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
  where PDFium *stores* a float (`syntax.float32`, `content.f32m`), and where it *computes* in
  floats it rounds after every product and sum as C does: `width * size / 1000` is two roundings,
  a CFX_Matrix product, inverse or transform one per term (`textpage.concat32`, `apply32`,
  `inverse32`, `transform_rect32`, `content.text_positions`, `Font.glyph_width`). One rounding in
  double was one ulp apart after a TJ kern and on a rotated axis label; now chars and object boxes
  are the same bits on every test deck (`test_chars_and_object_boxes_are_pdfiums_to_the_last_bit`).
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
- **Broken content syntax** is read as CPDF_StreamContentParser reads it, because what a parser makes
  of junk decides what gets drawn (`syntax._StreamParser`, `operations`, found by
  `render_torture --mutate`):
  - *Numbers* (FX_Number): any word of digits, signs and dots is one. With a dot it is StringToFloat
    (leading signs skipped, the longest valid prefix, straight to float32: `5..5` is 5); without,
    an optional sign and the digits up to the first non-digit (`--5` is 0), 0 past uint32, and 0
    past int32 with a sign.
  - *Operands* (CPDF_StreamParser::ReadNextObject): a `[` inside a top-level array is nothing and
    only its `[` is consumed, so its `]` closes the outer array; a keyword inside an array is
    skipped; a dictionary with a key that is no name is nothing, read up to there; a stray `]` or
    `>>` is an operand that is no object (it counts, and reads as 0).
  - *The 16-slot operand ring*: past 16 operands, each new one advances the start and is written
    into the new start slot - it overwrites the second oldest, and the oldest is read as the last.
  - *The path fast path* (ParsePathObject), entered after an `m` with exactly two operands: path
    operators take the first six numbers read, extras dropped, with no count check, so a missing
    operand is whatever the previous operator left (zeros at first); anything else hands back.
- **Stroke bounds in float32** (CFX_Path::GetBoundingBoxForStrokePath, which gives a path's and so
  a form's box): a join grows the rectangle on the side a point lies of a line, and a Bezier ending
  on its own control point lies *on* that line, where only float32 rounding decides the side (67 pt
  apart in one case). Every step rounds to float, a division by zero is ±inf or NaN as in C, and
  UpdateRect keeps a NaN out as std::min/max do. Found by comparing extract calls on mutated torture
  pages; `CFX_Matrix::TransformRect` is float32 too now.
- **Page boxes**: the MediaBox falls back to Letter, and the crop box is intersected with it.
- **Damaged file structure** is read as CPDF_Parser reads it (found by mutating whole files outside
  their streams and comparing, `tests/test_pure_pdf.py::test_a_broken_file_is_rebuilt_as_pdfium_rebuilds_it`):
  - *Objects* are read by a port of CPDF_SyntaxParser (`document._body`, `_Words`), not the content
    lexer: words split at `()<>[]{}/%`, a name that is only `/` is a key that is dropped, a key that
    is no name is skipped, `endobj` ends an unclosed dictionary, nesting stops at 64, and a stream's
    /Length counts only as a number reached through at most one reference (none while that object
    is itself being read) and only if a word beginning with `endstream` follows it (a length of 0
    is checked too: `/Length 0 R1` is a zero length with a stray word after it); else the nearer of
    `endstream`/`endobj` ends the data. An object whose header names another number is no object.
  - *Stream filters* (CPDF_StreamAcc over PDF_DataDecode, `filters.decode`): the bytes as stored
    come back when /Filter is not a name or an array of names, when a filter other than the last
    is not Flate/LZW/A85/AHx/RL, when a decoder fails, or when nothing was decoded. Any name PDFium
    doesn't decode itself counts as an image codec, so `/Filter /Foo` gives the stored bytes and a
    content stream that reads as plain text still draws. /DecodeParms pairs an array with an array
    of filters, a dictionary with a single name, and nothing else.
- **Fonts that aren't there** (bfuzz, `test_fonts_and_filters_resolve_as_pdfium_resolves_them`):
  - `Tf` naming no font, or a font that is no dictionary (a stream), is CPDF_Font::GetStockFont:
    a Helvetica with no file; the size is set either way. The lookup is FindResourceHolder: a
    form's own Font dictionary, and the page's only when the form has none of that category.
  - A non-embedded base-14 font is loaded as CPDF_Type1Font does (canonical name through
    kAltFontNames, Courier widths 600, symbolic flags without a descriptor, Symbol/Zapf/Standard
    base encoding) and drawn with the face CFX_Win32FontInfo maps it to: Arial, Times New Roman,
    Courier New from `%WINDIR%\Fonts`, read through the TrueType glyph-map branch of LoadGlyphMap
    ((3,0) cmap with the F0/F1/F2 prefixes, else Unicode from the glyph name) and with bbox
    left/right scaled by width / TrueType width in C integer division. Symbol, ZapfDingbats and
    unknown names get PDFium's built-in Foxit faces, which are third-party binaries this tree
    doesn't carry: those glyph boxes stay apart, as do the Foxit MM substitutes PDFium uses for an
    embedded program that doesn't parse. Outside Windows PDFium's font mapper differs anyway.
  - `char_width_` and `glyph_index_` are uint16: a /Widths entry of -1502 is 64034.
  - A descriptor without /FontBBox (CheckFontMetrics) takes the program's box, the right way up:
    the "deliberately flipped" in PDFium's comment is FX_RECT's y-down naming, so its `top` is yMin.
    Ascent and descent then come from the face, and FreeType makes a Type 1 or CFF face's ascender
    and descender its FontBBox yMax and yMin (floored and ceiled from 16.16), not 0 (bfuzz seed 452).
- **Cross references** are CPDF_Parser's loading ported, not a reader that accepts good files
  (`document.PdfFile`, whose table is CPDF_CrossRefTable; `test_cross_references_are_read_as_pdfium_reads_them`
  and `test_cross_references_are_loaded_as_pdfium_loads_them`, one case per rule). A regex reader
  that refused one damaged row made the fuzz's files unopenable where PDFium read them (seed 680).
  Two ports were written side by side and merged; the rules:
  - Positions count from the `%PDF` header, looked for in the first 1024 bytes. `startxref` is a
    whole word found backwards within the last 4096 bytes, and its offset must be at least 9.
  - Table entries are 20 bytes read blind: the offset is the digits before the first non-digit
    (`00000002f3 00000 n` is offset 2, an offset of 0 must be ten digits), byte 17 `f` means free,
    the generation is StringToInt from byte 11, and only the first entry with a position is
    verified, so a wrong offset elsewhere makes that object None rather than a rebuild.
  - /Prev chains: the oldest table is loaded first, a newer entry with a lower generation is
    ignored (AddNormal), the newest trailer's keys go over the older ones', `/Prev` loops stop.
  - XRef streams: type 3 is nothing, /Size, /Index and /W as PDFium checks them, and a type-2 entry
    only when its archive number is at most the last object number known so far.
    kMaxObjectNumber is 24·2^20, not 2^20.
  - Object streams need a direct `/Type /ObjStm` and integer `/N` and `/First` (`/N 2.0` makes
    every member None); a member numbered 0 is skipped but counts towards N.
  - *The table is believed* only if the trailer's /Root is a reference to a catalog with at least
    one page; otherwise RebuildCrossRef scans the file word by word (strings skipped, so `9 0 obj`
    inside a string is nothing), reading each object with the strict parser and stepping over its
    stream, merging every `trailer` and XRef stream dictionary, keeping the higher generation. The
    rebuild merges over the table it replaces, so entries the scan cannot see survive, and a later
    `3 0 obj` beats an object stream's member. It never looks for a catalog. A failed parse is
    never cached.
- **A shading that fails Validate is drawn from its second `sh`**: CPDF_ShadingPattern::Load sets
  the shading type before validating, a second Load answers true without validating again, and the
  document keeps the pattern. So the first `sh` makes no page object and every later one does
  (bfuzz seed 437, `test_a_shading_that_fails_validation_is_dropped_at_its_first_sh_only`).
- **The page tree** (CPDF_Document::CountPages, TraversePDFPages, GetPageIndex): /Count is believed
  when 0 < Count < 0xFFFFF, else the kids are counted (a visited set breaks cycles); a kid that is
  no dictionary uses up a page, a node that is its own kid is skipped, a node without /Kids is a
  page, and the traversal is stateful, so which dictionary page *i* is depends on the order pages
  were asked for (`test_page_trees_are_walked_as_pdfium_walks_them`, three orders each). A page
  loads when /Type is absent or resolves to /Page. PDFium opens a document with no pages;
  pypdfium2's `PdfDocument` refuses it, so the contract does too.
- **Navigation** (`pure/navigation.py`: FPDFLink_*, FPDFAction_*, FPDFDest_GetDestPageIndex,
  CPDF_NameTree, FPDF_GetNamedDest, CPDF_PageLabel, FPDF_GetMetaText), resolved one level at a time as
  PDFium does. Name-tree limits are put in order before they are compared, and names compare as
  UTF-16 code units (an astral name sorts below U+FF01). A URI action is joined to the catalog's
  `/URI /Base`; only GoTo actions give a destination; a link's /Rect with fewer than four numbers is
  no rect; letter labels wrap past 26 × n and label numbers wrap as int32; the Info dictionary must
  be a reference. A probe of 332 handmade files and 400 mutated ones differs from PDFium in none;
  the structure fuzzer (`--structure`, seeds 0-400) went from 93 differing files to 48, links 0.

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

- **Rendering, mostly.** Option 3 below is under way (next section); until images, Type 3 and
  TrueType text (and the shadings and colour spaces not ported yet) draw, `render` raises PdfError
  on any page holding one, and `api.renders(backend)` is
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
optionally inside nested forms with random /BBox and /Matrix (`--forms`) and on pages with media
boxes off the origin, crop boxes, /Rotate and render clips (`--page`), with junk tokens in every
content stream (`--mutate`: odd numbers, stray operators, unbalanced `[ << >>`), rendered by both at
random zooms on white and on clear bitmaps, each difference shrunk to the lines (and, mutated, the
tokens) that still cause it. When paths were done: 5,500 seeds of pages, 3,000 with forms, 300 of
page geometry and 3,500 mutated, not one pixel apart; `tests/test_pure_pdf.py` keeps 40 seeds of
each plus the shrunk pages that were once apart. `render_page` refuses (`unported`) what
it does not draw yet: text, images, tiling patterns, transfer functions.

**Transparency** (`pure/render_transparency.py`) is CPDF_RenderStatus::ProcessTransparency and
everything under it: soft masks (Luminosity and Alpha, /BC, /G drawn through its own Status with the
mask group's colour space), transparency-group forms (isolated or not, /K read and ignored as PDFium
does), constant alpha on groups, and every blend mode, separable or not (blend.cpp's formulas with
`kColorSqrt`, and SetLum's ClipColor testing the stale maximum). Rules found on the way:
- only `/Group << /S /Transparency >>` is a group; a /Group without /S is a plain form. A
  non-isolated group with no mask, alpha or blend is drawn straight onto the device.
- a sub-bitmap is the object's rect on the device, and a form's rect is the union of its
  children's `GetRect` - stroke boxes from `CFX_Path::GetBoundingBoxForStrokePath` included,
  computed in float32 (a `v` whose first control is the current point joins at a doubled point, and
  only float32 rounding decides which side the miter lands; AGG clips to the bitmap, so a bitmap one
  row taller rasterises the stroke differently).
- CompositeDIBitmap: a blend onto an opaque (BGRx) device is **GetBackdrop** - the page drawn again,
  up to the blended object, into a clear bitmap, blended there and laid on white (`render.Status`
  takes a `stop` object for that); onto a BGRA device or into a non-isolated group it is
  SetDIBitsWithBlend, the group's backdrop copied in with GetDIBits (which reads its backdrop piece at
  0,0 whatever the rect: a PDFium quirk, ported). A blend over pixels nothing was drawn on is a copy:
  the page's white is the caller's fill, not content.
- **CPDF_ContentParser::CheckClip**: after a page's (or a form's) content is parsed, an object whose
  only clip is a rectangle containing its box loses that clip. Only the render clip goes; the
  extraction's clip boxes are PDFium's `GetClipPath` answers and keep it. It moves edge pixels when the
  fill's edge lies on the /BBox's under a skewed `cm`, with or without groups.
The oracle is `devtools/render_torture_transparency.py` (`python tools/render_torture_transparency.py
SEED0 N [--page]`): render_torture's paths inside random forms and soft-mask groups nested in each
other, `/SMask /None`, alphas and a `/BM` on a third of the painted groups, over a ground rect. 6,000
seeds (3,000 with page geometry) are exact; the tests keep 40 of each and the shrunk cases.
Refused, each with its reason: transfer functions (/TR, /TR2, and a soft mask's /TR - they need the
function evaluator the shading port brings), a luminosity mask with a /BC whose group colour space is
not DeviceGray, DeviceRGB or DeviceCMYK, masks nested 8 deep, and anything unported inside a mask's
/G (the same `unported` check runs over it).

**Shadings** (`pure/render_shading.py`) are CPDF_RenderShading's axial (type 2) and radial (type 3)
loops, painted by `sh` (ProcessShading) and as shading patterns filling or stroking a path
(DrawShadingPattern: the path, or its stroke outline, becomes the clip), with the functions that
colour them (CPDF_Function types 0, 2, 3 and 4, the PostScript calculator whole) and the colour
spaces they feed (Device Gray/RGB/CMYK, CalGray, Separation, DeviceN). The content parser only
records which pattern each colour side holds and the shading's CTM (`PObj.fill_pattern`,
`stroke_pattern`, `shading_matrix`, `shading_record`); everything else happens at render time. Rules:
- the colours are 256 steps computed once per draw, step i at `t0 + (t1 - t0) * i / 256` in float32,
  each `ArgbEncode(alpha, roundf(r * 255), ...)`; a pixel's parameter becomes `static_cast<int>(s *
  255)`, so a NaN (an axial shading of zero length) is INT_MIN and takes the start extension;
- every pixel of a BGRA buffer the size of the clipped object is computed at (column, row), not at the
  pixel centre, through the inverted matrix in float, then laid on the page with SetDIBits
  (CompositeRow_Argb2Rgb/Argb2Argb under the clip mask), so alpha is `roundf(255 * ca)` where a path's
  is truncated;
- a radial shading is "decreasing" when its radius falls by more than the centres' distance
  *truncated to an int*; the root picked, the swap when `a <= 0` and the negative-radius skip follow
  the loop literally;
- /Background is drawn for patterns only (an `sh` ignores it), with its colour truncated, not rounded;
  /BBox is transformed by the shading's matrix and cuts the buffer;
- a pattern's matrix is its /Matrix times the *parent matrix* - the page's identity, or inside a form
  that form's /Matrix - never the CTM at `scn`: the object's own CTM comes in with mtObj2Device;
- the functions are PDFium's to the bit: float32 per operation, `powf`/`sinf`/`atan2f`/... from the
  C runtime (ucrtbase) through ctypes, a sampled function's bit reader returning 0 without moving past
  the end, a Separation whose tint function has too few outputs used without it, PostScript's stack of
  100 that ignores overflow and pops 0 when empty, `if` without a procedure ending the procedure.
  PostScript numbers are read the way fast_float reads them (correctly rounded to float32), and only
  plain decimals are accepted;
- DeviceCMYK goes through the same Adobe table as the extraction, each component `(int)(c * 255 +
  0.49999997)`.
The oracle is `devtools/render_torture_shading.py` (`python tools/render_torture_shading.py SEED0 N`):
random axial and radial shadings as `sh` and as patterns (fill and stroke, /Matrix, /Background,
/BBox, forms and transparency groups around them), random functions of every type (sampled at every
bit depth, stitched, PostScript programs) through every colour space above, clips, `cm`, alphas,
white and clear bitmaps, and one seed in seven "wild" (short /Coords, bad domains, wrong function
counts, CalRGB/Lab/Indexed). 12,000 seeds are exact and 8% refused; every test-deck page holding a
shading (41 pages of 14 decks), with only paths, forms and shadings on, is exact at two zooms. The
tests keep 60 seeds, the shrunk cases and one deck. Refused, each with its reason: function-based
and mesh shadings (types 1, 4-7), tiling patterns, CalRGB/Lab/ICCBased/Indexed colour spaces, a
PostScript word that is not a plain number, a pattern stroked through an all-zero matrix, a shading
that fails validation (PDFium's Load keeps the type it read, so the *second* Load of the same
object succeeds and draws: what is drawn depends on history), and a pattern object the page uses
under two parent matrices (PDFium's document cache keeps the first while it lives).

**Text** (`pure/render_text.py`, glyph outlines from `pure/ftoutline.py` - FreeType's port of Adobe's
CFF engine, Type 1 charstrings included, unhinted - and coverage from `pure/ftgrays.py`, FreeType's
smooth rasteriser) is ProcessText, DrawNormalText and DrawTextPath as a display bitmap without
FPDF_LCD_TEXT gets them: the glyph is rendered in FT_RENDER_MODE_LCD under FT_Set_Transform, and
DrawNormalTextHelper folds each pixel's three subpixels into one coverage (shifted by the origin's
third of a pixel, the first column of a shifted glyph normalised apart), gamma-adjusts it
(kTextGammaAdjust) and merges the colour into a copy of the pixels under the text (GetDIBits; zeros
on a BGRA device), which SetDIBits puts back through the clip. The origin is floor(x) and round(y) of
the transformed char position. Glyph bitmaps are cached under PDFium's key (the matrix × 10000,
truncated): the first rendering under a key is the one reused. Rules found on the way:
- **Positions are float32 op by op** in the content parser too: a char width is
  `F(F(w·size)/1000)`, a TJ kerning `F(F(F(k·size)/1000)·Tz)` (a TJ holding no string scales by Tz
  as well), TJ numbers accumulate in float, Tz is `F(n/100)`, and the object's origin is
  `ctm.Transform(tm.Transform(x, y))` in float. One ulp in a char's x changed a glyph's coverage by
  one level.
- **The path route**: |a| + |b| of the glyph-to-device matrix above 50 pixels, and every stroked
  mode (1, 2, 5, 6), draw the outlines (LoadGlyphPath) as paths: per glyph
  `Identity·(size, 0, 0, size, x, 0)·text2user`, filled non-zero, `text_mode` on (so its degenerate
  sub-paths skip DrawZeroAreaPath, the hairline pass a plain fill's get). A stroke under a `cm` whose a or d is not
  1 takes the CTM out of the text matrix and into the device matrix, so the pen is the user-space one.
- **Clip modes (4..7) draw like 0..3**, and mode 3 draws nothing: the AGG device has no soft clip, so
  ProcessClipPath skips text clips altogether.
Refused, each with its reason: Type 3 text (ProcessType3Text, not ported), fonts without an embedded
Type 1 / CFF program (the standard 14 and every substituted font - PDFium draws a system font - and
TrueType glyphs), a code whose glyph the font lacks (PDFium falls back to another font), vertical
writing, pattern colours, and text inside a soft mask (a mask device renders glyphs in
FT_RENDER_MODE_NORMAL). The oracle is `devtools/render_torture_text.py` (`python
tools/render_torture_text.py SEED0 N [--simple 0|1|2] [--kind type1|cid|...]`): the fonts are
harvested from the built test decks at run time (no font binaries in the tree), only codes whose
glyph has an outline, and a page is a few BT groups with random Tf sizes (0.5 to 120), Tm (upright,
scaled, mirrored, turned, skewed), `cm`, clips, Tz/Tc/Tw/Ts/TL, Tr 0..7, TJ kernings, constant
alpha and line widths, rendered at zooms 0.5 to 3.1 on white and clear bitmaps. When text was done:
1,200 seeds of everything, 400 Type 1 only, 300 CID-keyed CFF only, 200 without Tr/Tz/clips and 60
one-glyph pages, not one pixel apart (59 refused, all Type 3); across the test, sync, stress and
theme PDFs, 1,499 pages holding text render byte-identical and none differs (the rest are refused
for shadings, images, TrueType or Type 3). `tests/test_pure_pdf.py` keeps 80 seeds of two levels
and the shrunk cases.

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
