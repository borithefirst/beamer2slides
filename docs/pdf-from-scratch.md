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
    base encoding).
  - **Substituted fonts** (`pure/fontmapper.py`, `pure/foxit.py`, `pure/type1.py`;
    `test_substituted_fonts_are_measured_with_pdfiums_face`, 66 handmade cases): any font without a
    program - or whose program FreeType can't open, which LoadFontDescriptor purges, so it is
    substituted as if never embedded - goes through PDFium's chain rule for rule:
    CPDF_Font::LoadSubstFont (weight = /FontWeight, else from /StemV (×5 below 140, else ×4 + 140),
    both counting only when ItalicAngle, Ascent, CapHeight and Descent are all present - the
    ExternAttr flag - clamped to 100..800), CFX_Font::LoadSubstFace, CFX_FontMapper::FindSubstFace
    (GetSubstName, ParseStyles, GetStyleType, TT_NormalizeName, the base-14 and "Arial,Bold"
    style suffixes, symbolic/serif/fixed/script flags, MatchInstalledFonts, UseInternalSubst,
    UseExternalSubst) and on Windows CFX_Win32FontInfo: EnumFontFamiliesEx for the installed list,
    GDI's own `CreateFont` + `GetTextFace` for MapFont (so the face picked is Windows' choice, not
    a guess), GetFontData for the bytes and GetTTCIndex for a collection. Where PDFium falls back
    to its built-in faces (Symbol, Dingbats, FoxitSans/Serif and the multiple-master
    FoxitSerifMM/FoxitSansMM for an unknown name), the port loads the same bytes from a user cache:
    `python -m beamer2slides.pdf.pure.foxit` fetches PDFium's `core/fxge/fontdata/chromefontdata`
    sources and keeps a face only if its SHA-256 matches the pinned sum - no third-party binary is
    in the tree. The MM faces are Type 1 programs parsed by `type1.py` and blended by FreeType's
    rules (WeightVector, the /BlendDesignPositions normalisation, blended charstrings).
    The face is then read by the program's own LoadGlyphMap: CPDF_Type1Font's (Adobe/custom
    charmaps, glyph names) or CPDF_TrueTypeFont's (DetermineEncoding, DetermineCharmapType,
    SetGlyphIndicesFromFirstChar, GetGlyphIndexForMSSymbol, the (3,0) F0/F1/F2 prefixes, MacRoman
    through PDF_FindCode), over FreeType's charmap list as FreeType builds it (sfnt_find_encoding,
    the Unicode charmap synthesised from post names, format 14 unselectable, find_unicode_charmap's
    preference order). Selecting a charmap changes the shared cached face, as in PDFium. Bbox
    left/right are scaled by width / TrueType width in C integer division. Without the Foxit cache
    (or outside Windows, where PDFium asks fontconfig and that is not ported) the older rules stay:
    system Arial/Times New Roman/Courier New for base-14, and boxes apart elsewhere.
  - **Embedded sfnt fonts** (`pure/sfnt.py`, `pure/psnames_data.py`; oracle
    `devtools/truetype_torture.py`, `python tools/truetype_torture.py SEED0 N`, 8,000 seeds clean,
    `test_made_up_truetype_fonts_extract_as_pdfium_does`): the charmap list, glyph names and the
    post table are FreeType's own (sfobjs.c, ttcmap.c, ttpost.c, cffobjs.c, psnames), not fontTools'
    reading of them. FreeType's glyph list and its Mac names are generated from its sources by
    `tools/freetype_psnames_data.py`. The quirks that separated the two:
    the Unicode charmap synthesised from glyph names is sorted by the Windows UCRT's qsort, which
    is not stable, so of two glyphs sharing a name the one it leaves first wins (`msvc_qsort`, a
    port of qsort.cpp); the post header is read from the *stream* at the table's offset, so a post
    table shorter than 32 bytes still loads when bytes follow it; format 2 turns its Pascal strings
    into C strings in place, so the last name loaded runs on into the bytes after it up to a 0;
    any read at the end of the file, even of no bytes, fails the whole name table; a count of 0 or
    over maxp's gives no names. A CFF-in-OpenType face takes its names from the CFF charset (not
    CID-keyed), adds its own Unicode charmap unless (3,1) or a platform-0 one exists, and an Adobe
    encoding charmap when the encoding is not empty. A name's Unicode value is looked up up to its
    first non-initial dot, and PDFium keeps it as a Windows `wchar_t` (`& 0xFFFF`). A Type 1 font's
    `.notdef` is swapped with glyph 0, not moved. A post or cmap table fontTools can't read no
    longer costs the whole program: its glyphs are read under a synthetic glyph order.
    Opening the face is FreeType's too (`--directory`, which breaks the table directory, maxp, head,
    hhea or loca, took 126 of 300 seeds apart): `sfnt.font_dir` is check_table_dir (an entry past
    the file dropped, hmtx/vmtx cut to it, the first of two tags winning, a zero length missing),
    `Face._open` is sfnt_load_face plus tt_face_init (head, maxp and hhea read from the stream past a
    short table; a face FreeType refuses raises `FaceError`, and PDFium then substitutes the font), and
    glyphs come from `Face.location` / `Face.metrics` (tt_face_get_location, tt_face_get_metrics),
    not fontTools' glyf. Measured with `FPDFFont_GetIsEmbedded`: a glyf font without loca is
    refused even when glyf is missing too (sfnt_load_face makes a face with no outlines and no
    bitmaps scalable, so loca is loaded), and a short loca is read on to the next table or cuts the
    glyph count, which then bounds `FT_Get_Name_Index` and `FT_Get_Glyph_Name` (the post names still
    run to maxp's count). `--os2` adds an OS/2 table and a zero FontBBox, so the char boxes show the
    face's ascender: USE_TYPO_METRICS, else hhea, else (both 0) typo, else win; an OS/2 that fails
    any of tt_face_load_os2's frames or says version 0xFFFF counts as missing. 6,000 directory
    seeds and 4,000 OS/2 seeds clean.
  - `char_width_` and `glyph_index_` are uint16: a /Widths entry of -1502 is 64034.
  - A descriptor without /FontBBox (CheckFontMetrics) takes the program's box, the right way up:
    the "deliberately flipped" in PDFium's comment is FX_RECT's y-down naming, so its `top` is yMin.
    Ascent and descent then come from the face, and FreeType makes a Type 1 or CFF face's ascender
    and descender its FontBBox yMax and yMin (floored and ceiled from 16.16), not 0 (bfuzz seed 452).
    A Type 3 font without /FontBBox has no face either: its box is the union of its char boxes.
  - A non-symbolic Type 1 glyph missing by name is looked up through FreeType's Unicode charmap -
    which FreeType only makes when some glyph name maps to Unicode. MSAM10's (`trianglerightsld`)
    don't, so `SelectCharMap` fails, the builtin encoding stays selected and the *code* is looked up
    in it (bfuzz seed 246: the ▶ bullet found, not .notdef).
  - CID fonts: `CharCodeFromUnicode` (FPDFFont_GetGlyphWidth) answers 0 for an embedded CMap
    (kUNKNOWN) and for Identity-H/V (kCID, no CID-to-Unicode map); a Type0 font with no /Encoding, or
    without exactly one descendant dictionary, fails to load and its text is stock Helvetica.
    Found by editing every font dictionary of a deck the same way
    (`test_font_dictionaries_edited_deck_wide_read_as_pdfium_reads_them`).
  - A stroked text object (Tr 1, 2, 5, 6) has its box inflated by half the line width, in floats
    (CFX_FloatRect::Inflate). A Type 3 text object keeps the real Tr for that: only its glyphs are
    forced to fill. A Tr outside 0..7 is ignored (`SetTextRenderingModeFromInt`). Found by reading
    the render torture's text pages instead of drawing them
    (`test_text_torture_pages_extract_as_pdfium_to_the_last_bit`).
- **Right-to-left text** follows PDFium's own Unicode tables (`pure/unicode_data.py`, written by
  `tools/pdfium_unicode_data.py` from fx_ucddata.inc and unicodenormalizationdata.cpp at the PDFium
  build pypdfium2 ships): an old snapshot with Foxit's choices, 3,982 bidi directions and 3,996
  decompositions apart from Python's `unicodedata` (U+00A8 decomposes to U+0308 alone, an unassigned
  Hebrew point is right to left, U+FB05 is "ſt"). CFX_BidiChar makes CS/ES/ET/NSM/BN weak left, not
  neutral. CloseTempLine builds its CFX_BidiString without auto order, so a line is reversed only in
  a document whose catalog says `/ViewerPreferences << /Direction /R2L >>` (FPDFText_LoadPage), never
  by its letters; IsRightToLeft (a mirrored object's chars put back) does auto order, which wants
  strictly more right segments than left ones, and sees a TJ kern as U+FFFF. A generated char is
  placed in floats. Found by the first TrueType deck (`26_truetype_fonts`, xelatex + DejaVu from the
  TeX distribution): its subsets have glyph ids with no Unicode, so the text page reads the codes
  themselves - Hebrew, Arabic, Syriac - and 78 of 100 torture pages were apart
  (`test_right_to_left_text_is_ordered_as_pdfium_orders_it`, with and without /R2L).
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

- **Rendering, mostly.** Option 3 below is under way (next section); until Type 3 and
  TrueType text (and the shadings, image codecs and colour spaces not ported yet) draw, `render` raises PdfError
  on any page holding one, and `api.renders(backend)` is
  False. The pipeline needs renders for backgrounds, crops, ball colours, `_looks_like` and fidelity,
  so `classify` runs on the reader but `convert` does not. `embedded_image` gives no `pixels` or `rendered`, so
  `render.image_file` can't prove a raw JPEG looks right and keeps the page crop instead.
  Text in a substituted font is drawn when PDFium draws it with one of its Foxit faces (see
  "Substituted text" below); a system TrueType substitute (GDI's Arial for Helvetica, Symbol,Bold,
  Verdana...) raises PdfError until TrueType glyphs are ported.
- Font substitution outside Windows (PDFium's fontconfig/`CFX_LinuxFontInfo` scan is not ported),
  and without the Foxit cache (older rules, see above); CID fonts' own substitution
  (CPDF_CIDFont's CJK charset and ordering rules) keeps the older behaviour too.
- Encrypted PDFs (LaTeX doesn't write them), ActualText, and JPX/JBIG2/CCITT
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
it does not draw yet: images, tiling patterns, some text.

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
Refused, each with its reason: a luminosity mask with a /BC whose group colour space is
not DeviceGray, DeviceRGB or DeviceCMYK, masks nested 8 deep, and anything unported inside a mask's
/G (the same `unported` check runs over it).

**Transfer functions** (`pure/transfer.py`) are drawn now, with the shading port's function
evaluator. The ExtGState's /TR2 wins over /TR and a name clears it (CPDF_AllStates);
CPDF_DocRenderData::CreateTransferFunc samples the function at `float(v) / 255` into three 256-byte
tables and GetFillArgb/GetStrokeArgb run the colour through them (TranslateColor) - paths and text,
never shadings or shading patterns. Its quirks are ported:
- the three functions of an array land in the tables in reverse, so the first one maps blue;
- one `output[16]` serves every call, so a call that fails keeps the last one's value;
- a function with more than 16 outputs gives the identity in an array, and the stale output alone;
- the rounded sample is stored as its low byte (a negative one wraps).

A soft mask's /TR (LoadSMask) is one function, and only when it is a dictionary or a stream. It is
sampled at `i / 255` into a table that maps the luminosity or the alpha. Still refused: a
non-identity transfer on an image (TranslateImage) or on Type 3 text, a mask function with no
outputs, and one that would write past its output array. The oracle is the shading torture's
`--mode transfer`:
- /TR, /TR2 and both at once, as single functions or arrays of three (with /Identity, short and long
  arrays among them);
- soft masks carrying a /TR, and groups that set one inside;
- over plain gray, RGB and CMYK fills and strokes, and over shadings.

1,200 seeds are exact.

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
- the CIE spaces (`pure/cie.py`) are PDFium's arithmetic, not colorimetry. CalRGB applies gamma and
  matrix, then goes XYZ to sRGB under its white point through PDFium's own 3x3 inverses (all zeros
  when |det| < FLT_EPSILON). Lab uses its piecewise curve and the fixed D65 matrix. Both end in
  RGB_Conversion's 1024-step table, not the sRGB formula. Indexed looks its base colour up per step.
- **function-based shadings** (type 1, `pure/render_mesh.py` DrawFuncShading) evaluate the function
  at every pixel through the inverse of /Matrix times the device matrix. Pixels outside /Domain are
  skipped. The results array is shared across pixels, as in C++.
- **mesh shadings** (types 4-7, CPDF_MeshStream) read coordinates, colours and flags with the bit
  widths PDFium accepts. 32-bit coordinates are scaled in double, the rest in float32. Vertices are
  byte-aligned in types 4 and 5, patches are not. Types 4 and 5 are DrawGouraud: per row, the two
  edge crossings, then the colour stepped across the span by float32 accumulation. Types 6 and 7 are
  CPDF_PatchDrawer: Coons/tensor subdivision with its FX_SAFE_INT32 bi-interpolation, down to cells
  drawn as 13-point bezier paths with *full cover* (every pixel the rasteriser touches is painted
  opaque, `Device.draw_path(full_cover=True)`). The shading object's box is GetShadingBBox (every
  mesh point, colours skipped) cut by the clip, so a mesh draws only where its points are.
The oracle is `devtools/render_torture_shading.py` (`python tools/render_torture_shading.py SEED0 N
[--mode classic|cie|func|mesh|transfer]`; classic is the original sequence of seeds):
random axial and radial shadings as `sh` and as patterns (fill and stroke, /Matrix, /Background,
/BBox, forms and transparency groups around them), random functions of every type (sampled at every
bit depth, stitched, PostScript programs) through every colour space above, clips, `cm`, alphas,
white and clear bitmaps, and one seed in seven "wild" (short /Coords, bad domains, wrong function
counts, CalRGB/Lab/Indexed). 12,000 seeds are exact and 8% refused; every test-deck page holding a
shading (41 pages of 14 decks), with only paths, forms and shadings on, is exact at two zooms. The
other modes each stress one of the gaps closed later:
- `cie`: CalRGB with Gamma, Matrix and BlackPoint; CalGray; Lab with Range; Indexed, Separation and
  DeviceN over them.
- `func`: type 1 shadings with 2-in sampled and PostScript functions, including ones that fail
  validation.
- `mesh`: types 4-7 at every bit width, with flags carrying edges over, invalid bit widths, a short
  /Decode, cut or padded streams, bad /VerticesPerRow and a dictionary instead of a stream.
- `transfer`: described above.

Not one pixel apart in 900 seeds of `cie`, 900 of `func`, 1,500 of `mesh` and 1,200 of `transfer`.
The tests keep 60 classic seeds and 25 per mode, eight fixed seeds (one per feature), the shrunk
cases and one deck. Refused, each with its reason: tiling patterns, ICCBased colour spaces, a
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
- **Clip modes (4..7) draw like 0..3, then clip**, and mode 3 draws nothing. The parser keeps a
  clone of every text shown in a clip mode (Type 3 text counts as mode 0) and, at ET, appends them
  to the clip path if the mode *at ET* is still a clip mode (CPDF_ClipPath::AppendTexts: a group
  ends with a null entry, and a list that would pass 1,024 texts takes none); CheckClip leaves a
  clip holding texts alone. The AGG device *has* soft clips (RenderCapSoftClip), so ProcessClipPath
  follows them: per group, DrawTextPath appends each glyph outline through
  `Identity·(size, 0, 0, size, x, 0)·textmatrix` and the object-to-device matrix (the CTM is never
  taken out, whatever the mode) to one device-space path, and SetClip_PathFill clips to it,
  non-zero. Clones, so switching the text object off leaves its clip. The text page, the object
  list and the clip boxes (FPDFClipPath counts paths only) do not see text clips. The torture's
  `--simple 3` pages (text clips followed by paths, inline images, more text, path clips, q/Q):
  317 of the first 400 seeds draw and all are exact, the inline images inside the clip included
  (the rest wait for TrueType or Type 3 text); 26 of the first 60 are up to 243,066 pixels apart when the text clip is ignored, which
  the port used to do (no earlier torture page could show it: every BT group sat inside q..Q).
  200 seeds extract equal (objects, bounds, chars).
- **Vertical writing** (CID fonts only): a CMap is vertical when its predefined name ends in `V`
  or an embedded one's /WMode reads non-zero through the CMap parser's GetCode (so `2`, `-1`,
  `<01>` and `1.5` are vertical too). Then /W2 (LoadMetricsArray with n = 3, each value an int16,
  a group cut short padded with 0) and /DW2 (default `[880 -1000]`, each element read alone)
  give GetVertWidth (w1, the advance, down the page) and GetVertOrigin (vx, vy; without a W2 entry
  `(int16(W width / 2), vy)`). A W2 whose flat size is not a multiple of 5 is a CHECK failure in
  PDFium (reinterpret_span), so it is a PdfError here. What changes: TJ kernings and the advance
  move the text position's y by `-(k·size/1000)` (no Tz; a TJ with no strings still moves x);
  each item's origin is `(-size·vx/1000, pos - size·vy/1000)` in float32 (`content.item_origin`)
  and every consumer takes it from there: CalcPositionData's box (the char rect offset by the
  vertical origin in integers, then scaled), the text page's char origins and boxes (a zero-width
  char falls back to the vertical width), GetLooseBounds (left, left + size, float top and bottom,
  unrotated, then the matrix), the writing-mode guess, and FPDFFont_GetGlyphWidth (the vertical
  advance). Drawing an *embedded* font changes nothing but the origins: the CID transform is only
  for fonts PDFium substitutes, so glyph outlines and bitmaps are the horizontal ones. Found on
  the way (not vertical at all): a char of an embedded CMap with no ToUnicode entry takes
  PDFium's Windows answer, MultiByteToWideChar in the ANSI code page of the code's bytes
  (`fonts._ansi_char`; `Þ`, not U+FFFD). The torture's `--simple 4` pages mix vertical and
  horizontal CID fonts (Identity-V or an embedded CMap with a random /WMode, random /W2 and
  /DW2, clip pages among them): 300 seeds extract equal (objects, bounds, chars), and with
  `--kind cid-cff` all of the first 400 seeds draw and are exact;
  17 of the first 30 are apart when the vertical origins are ignored.
Refused, each with its reason: Type 3 text (ProcessType3Text, not ported), TrueType glyphs (embedded
or a system substitute GDI picked), a code whose glyph the font lacks (PDFium falls back to another
font), a /W2 PDFium would crash on, pattern colours, and text inside a soft mask (a mask device
renders glyphs in FT_RENDER_MODE_NORMAL). The oracle is `devtools/render_torture_text.py` (`python
tools/render_torture_text.py SEED0 N [--simple 0|1|2|3|4] [--kind type1|cid|...]`): the fonts are
harvested from the built test decks at run time (no font binaries in the tree), only codes whose
glyph has an outline, and a page is a few BT groups with random Tf sizes (0.5 to 120), Tm (upright,
scaled, mirrored, turned, skewed), `cm`, clips, Tz/Tc/Tw/Ts/TL, Tr 0..7, TJ kernings, constant
alpha and line widths, rendered at zooms 0.5 to 3.1 on white and clear bitmaps. When text was done:
1,200 seeds of everything, 400 Type 1 only, 300 CID-keyed CFF only, 200 without Tr/Tz/clips and 60
one-glyph pages, not one pixel apart (59 refused, all Type 3); across the test, sync, stress and
theme PDFs, 1,499 pages holding text render byte-identical and none differs (the rest are refused
for shadings, images, TrueType or Type 3). `tests/test_pure_pdf.py` keeps 80 seeds of two levels
and the shrunk cases. Since `26_truetype_fonts` the harvest also holds CID TrueType (DejaVu) and
simple CFF fonts (xelatex's Computer Modern): CFF renders exact (60 of 60 seeds), TrueType is refused.

**Substituted text** (a font with no program in the PDF) is drawn from the face `fontmapper.py`
picked, through what CFX_Font does differently for a CFX_SubstFont. On Windows the base 14 and any
installed name go to GDI's TrueType faces (refused, `render_text.truetype_face` is the one line that
will hand them to the TrueType port), so what draws is PDFium's own Foxit faces: Symbol and
ZapfDingbats (CFF, `ftoutline.Face.from_cff` over the cached bytes; "Chrome Symbol"/"Chrome
Dingbats", weight and angle 0), and for a name nothing matches the multiple masters FoxitSansMM or,
with the serif flag, FoxitSerifMM (weight × 4/5). Ported:
- **The multiple master blend** (`ftoutline.Face.adjust_variation`, CFX_Font::AdjustVariationParams
  over FreeType's T1_Get_MM_Var / T1_Set_MM_Design / t1_set_mm_blend, `/BlendDesignMap` read by
  `type1.py`): before each glyph is loaded, axis 0 is set to the font's weight and axis 1 is solved
  so the glyph's advance equals its /Widths width (`dest_width`, CPDF_CharPosList's
  font_char_width_): the advance at the axis' minimum and maximum, then a C `long` interpolation;
  with no width, axis 1's default. FT_MulDiv, the design map's piecewise interpolation *without* the
  lower blend point added back, clamping at the ends and the product of (coord | 1 - coord) per design
  are FreeType's to the bit.
- **The blend is process-wide state.** PDFium's font mapper keeps each MM face for the life of the
  process and FreeType keeps the blend on the face, so every glyph drawn moves it and every width
  read without /Widths (LoadCharMetrics at parse time, GetCharBBox) is read *at the blend the last
  glyph left* - in any document. The pure mapper also keeps one face per process, its metrics caches
  are keyed by the blend (`Face.blend_key`), and it performs the same sequence of set-design calls
  as long as it draws what PDFium draws. A page it refuses after PDFium drew it leaves the two
  apart; `render_torture_subst.resync` puts both back (one glyph of each face at a /Widths width sets
  both axes), and the tests that compare measurements call it first
  (`test_a_generic_face_keeps_its_blend_between_documents`). The torture's first failures (seeds
  38, 40, 41 before 43) were exactly this.
- **The skew** (CFX_SubstFont's italic angle, only on the MM faces: a matched face gets angle 0):
  `xy -= xx · skew / 100` in FT_Fixed with C truncation for bitmaps (the effective skew), the same
  on the identity for paths (GetSkew), from PDFium's angle table.
- **Synthetic bold** (FT_Outline_EmboldenXY, `ftoutline.embolden`, at GetEmboldenLevel's strength)
  is ported but unreached on Windows: the MM faces carry weight in the blend and the CFF faces have
  weight 0, so only GDI's TrueType faces would take it. Unverified.
- **GetCharPosList's spacing heuristic** for a non-MM substitute under a name that is neither
  standard nor the loaded family's (IsActualFontLoaded): a glyph whose /Widths width exceeds the
  face's advance + 1 moves right by half the excess (`F((pdf - face) · size) / 2000`), a narrower
  one is squeezed by the adjust matrix (pdf/face, 0, 0, 1) that GetEffectiveMatrix puts before the
  char matrix; reached by "ZapfDingbats,Bold" with the symbolic flag.
- **The glyph cache** (CFX_GlyphCache) belongs to the face and lives while a document's fonts hold
  it (CFX_FontMgr keeps an ObservedPtr per face): one per face per document here, bitmaps keyed by
  the matrix × 10000, dest width, weight and angle, paths by glyph, dest width, weight and angle. A
  cache hit skips AdjustVariationParams, so the cache's lifetime is part of the blend's history.
- `kFontWeightExtraBold` is 900 (a /FontWeight up to 900 is kept; the mapper had 800 and turned
  900 into 400: torture seed 67).
The oracle is `devtools/render_torture_subst.py` (`python tools/render_torture_subst.py SEED0 N
[--pool unknown|symbol] [--simple 0|1|2]`): pages of `render_torture_text`'s groups over made-up
simple fonts - random /BaseFont (unknown names, Symbol and ZapfDingbats variants, base 14 and
installed names), /Subtype, /Flags, /FontWeight, /StemV, /ItalicAngle, /Widths that agree with
nothing or are absent, /Encoding by name or /Differences - rendered by both with `resync` first.
6,000 seeds with nothing apart (about 35% refused: GDI TrueType substitutes and codes needing a
fallback font); `tests/test_pure_pdf.py` keeps 40 seeds and four shrunk cases. Needs the Foxit
cache (`python -m beamer2slides.pdf.pure.foxit`); without it every such page is refused.

**Images** (`pure/decode_image.py` loads, `pure/render_image.py` draws) are CPDF_DIB and the decoders
it creates, then CPDF_ImageRenderer down to the AGG driver. Loading is LoadColorInfo, the /Decode and
colour-key arrays, LoadPalette and GetScanline/TranslateScanline24bpp over DeviceGray, DeviceRGB,
DeviceCMYK, Indexed and ICCBased-through-its-alternate at 1 to 16 bits, from raw, Flate (PNG and
TIFF predictors), RunLength, ASCIIHex/85 and DCT data, into PDFium's own formats (k1bppMask,
k1bppRgb, k8bppRgb, kBgr, kBgra) with /SMask (and /Matte) or a /Mask stream beside them. Drawing is
DrawMaskedImage/CalculateDrawImage for an image's own mask, CFX_ImageStretcher and CStretchEngine
for upright and quarter-turned images (the weight tables and both passes, uint32 sums wrapping as
C's do), CFX_ImageTransformer for any other angle or skew, and CFX_AggBitmapComposer's scanline
compositor rows. Rules found on the way:
- **A JPEG is libjpeg's bytes.** Pillow's libjpeg decodes with the same ISLOW IDCT and fancy
  upsampling, so its pixels are PDFium's; a 4-component JPEG is kept as libjpeg's raw CMYK with no
  Adobe inversion (Pillow's `CMYK;I` inverts every byte, so it is XORed back) and then goes through
  the same Adobe CMYK table as every other CMYK colour.
- **The transformer** takes GetClosestRect of the unit square (MatchFloatRange in float32) cut by the
  device's clip box, stretches the image to the unit vectors' lengths (`ceil(hypotf)`) first, and
  samples that through the inverted matrix in 8.8 fixed point (`roundf(x * 256)`, `+ 128` in float32,
  the whole part saturated, the fraction taken with C's `%`), interpolating rows then columns with
  `>> 8`. Its "normal" branch (|b|, |c| < 0.05) is reachable only with a or d zero, and draws nothing.
- **Overprint changes nothing.** CPDF_ImageRenderer picks Darken for a CMYK image under fill
  overprint with /OPM 0, but the AGG driver's StartDIBits drops the blend mode: the pixels are those
  of a Normal draw (31 cases measured).
- A constant alpha multiplies an image mask's colour as FXARGB_MUL_ALPHA with `roundf(alpha * 255)`,
  a bitmap's alpha as `a * int(alpha * 255) / 255`; both then meet only the clip mask.
The oracle is `devtools/render_torture_image.py` (`python tools/render_torture_image.py SEED0 N
[--level 0..6]`): image XObjects and inline images of every format above, stencils, colour-key and
stream masks, soft masks with mattes, /Interpolate and truncated data, drawn upright, flipped,
scaled, quarter-turned, turned by any angle and skewed, under clips and constant alpha, at zooms 0.5
to 3.1 on white and clear bitmaps; levels grow the generator a class at a time and keep their seeds.
When images were done: level 4 (filters, masks, inline images) 7,000 seeds, level 5 (CMYK) 4,000 and
level 6 (everything, the transformer and CMYK JPEGs) 6,000, not one pixel apart; about 0.25% refused,
all "filters that decode to nothing". Every page of the test decks `07_images` and
`23_raster_images` is exact, and across the 50 test PDFs 229 of 241 pages render byte for byte (the
other 12 are refused for TrueType and Type 3 text). `tests/test_pure_pdf.py` keeps 60 seeds of level
6, the level-4 seeds that were once apart and one of each transformer format. Refused, each with its
reason: JPX, JBIG2 and CCITT data, ICC profiles lcms would open (only data that cannot be a profile
falls back to the alternate), Default colour spaces, CalGray/CalRGB/Lab/Separation/DeviceN images,
JPEG /ColorTransform 0 and JPEGs PDFium patches or scales, LZW and mid-chain predictors, a filter
that fails or decodes to nothing, images inside soft masks, image blend modes, pattern-filled
stencils, a soft mask under a soft mask, an 8-bit mask device, and inline images whose DCT or CCITT
end the parser cannot find as PDFium does.

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
