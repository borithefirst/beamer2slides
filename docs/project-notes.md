# beamer2slides: the long notes

This is what `CLAUDE.md` said, word for word, up to 2026-09-23, when it had grown to 305 KB: too
big for an agent's context, so every subagent spawned in this repo died on autocompact. `CLAUDE.md`
is now the short map. This file keeps the long version: the measurements, the seeds each campaign
found, and the reasons behind each rule. Search it for a function or a seed before changing
something it describes, and add the long story of a new change here or in the topic's own doc
(`docs/sync.md`, `docs/google-docs.md`, `docs/adopt-bench.md`, ...), not in `CLAUDE.md`.

Convert beamer PDFs into **editable Google Slides decks**.

## Goal
The output should feel like a native Google Slides deck: real text boxes, image
elements and shapes. Anything that can't be converted with good fidelity
(math, TikZ/pgfplots figures, theme decorations we can't rebuild) is baked into
a per-slide background picture.

## Strategy (decided)
- **Input is the compiled PDF**, not the `.tex`. The PDF gives exact geometry, fonts and
  colours. The `.tex` / SyncTeX may later be used as a hint for structure.
- Pipeline stages communicate through a **JSON intermediate representation (IR)**
  so each stage is testable in isolation:
  1. `extract`: PDF → raw spans / images / vector drawings (the `pdf` package's backend: PDFium)
  2. `classify`: group spans into lines, paragraphs and lists; decide native vs. background
  3. `render`: background PNG per slide with the converted elements removed
     (page objects switched off, partly removed objects composited from a second render)
  4. `emit`: a python-pptx deck carrying all pictures, imported by Drive, then the Slides API
- **The PDF library is a swappable backend** (`src/beamer2slides/pdf/`, docs/pdf-backend.md). Nothing
  outside that package imports pypdfium2: the pipeline calls `pdf.Document(path)` and the methods of
  `api.PdfDocument` / `api.PdfPage`, whose answers are plain data (dataclasses, dicts, tuples, bytes,
  numpy arrays) with page objects named by **id** (their index in `objects()`), never a handle.
  `api.py` is the contract and holds the conventions every backend shares (`char_box`, `trace` for path
  items, `pixel_bounds`); `pdfium_backend.py` is the reference; `sandbox.py` runs any backend in a worker
  process over `wire.py` frames (data only, no pickle; the worker is sent the PDF's bytes and needs no
  files, `save` returns bytes). The caller picks: `pdf.set_backend` / `use_backend`, or
  `$B2S_PDF_BACKEND` = `pdfium` | `pure` | `sandbox[:<spec>]` | `package.module:attr`; `$B2S_PDF_SANDBOX_CMD` wraps
  the worker in a jail or container. Conformance: `tests/test_pdf_backend.py` (every backend named in
  `$B2S_TEST_PDF_BACKENDS`; the sandbox must equal PDFium value for value). Measured: the 48 test PDFs
  give byte-identical raw.json, deck.json, backgrounds and figures in process and through the sandbox
  (19 s vs 25 s). PDFium's text and path output reproduces what the MuPDF-based extraction gave (span
  splitting at word gaps, `re`/`qu` path items, char boxes from font ascent/descent), so classify's
  thresholds still hold.
- **A PDF reader from scratch** (`pdf/pure/`, docs/pdf-from-scratch.md, fontTools - a plain
  dependency since `adopt` turned out to need it too, `[pure]` still naming it):
  a pure Python port of the PDFium parts the pipeline reads (syntax, filters, xref/repair, colour
  spaces, fonts, content stream, CPDF_TextPage with bidi over PDFium's own Unicode tables,
  `tools/pdfium_unicode_data.py`), answering the contract *as PDFium does*,
  quirks included (float32 numbers, U+0002 hyphens, FreeType's legacy AGL, Type 3 form boxes, image
  metadata rules; FreeType's sfnt charmaps and post names in `pure/sfnt.py`, its glyph list generated
  by `tools/freetype_psnames_data.py`, oracle `tools/truetype_torture.py`: made-up embedded fonts). Rendering is being ported from PDFium's AGG renderer (`pure/raster.py`,
  `pure/render.py`): paths, clips and forms come out byte-identical (float32 after every operation,
  `CFX_Matrix` products included; oracle `tools/render_torture.py`, random pages vs PDFium, shrunk);
  so do soft masks, transparency groups and every blend mode (`pure/render_transparency.py`, oracle
  `tools/render_torture_transparency.py`, 6,000 seeds; GetBackdrop, CheckClip, float32 stroke boxes);
  axial/radial shadings and shading patterns are ported too (`pure/render_shading.py`, oracle
  `tools/render_torture_shading.py`), so are function-based and mesh shadings (types 1, 4-7:
  `pure/render_mesh.py`, DrawGouraud and the Coons/tensor PatchDrawer with full-cover fills),
  CalRGB/CalGray/Lab/Indexed (`pure/cie.py`, PDFium's matrices and sRGB table) and transfer
  functions (`pure/transfer.py`: /TR, /TR2 and a soft mask's /TR, CreateTransferFunc's quirks
  included; torture `--mode cie|func|mesh|transfer`, 4,500 seeds, none apart), and so is text in embedded Type 1 and CFF fonts (FreeType's CFF
  engine and smooth rasteriser ported, `pure/ftoutline.py`, `pure/ftgrays.py`, `pure/render_text.py`,
  oracle `tools/render_torture_text.py`), with text clips (Tr 4-7 clip what follows, `--simple 3`)
  and vertical writing (Identity-V / /WMode, /W2 /DW2: origins, boxes and advances, `--simple 4`,
  `content.item_origin`), and so are images (CPDF_DIB, Flate/RunLength/DCT with CMYK
  JPEGs as libjpeg's raw bytes, stretch engine, CFX_ImageTransformer for any angle, own masks;
  `pure/decode_image.py`, `pure/render_image.py`, oracle `tools/render_torture_image.py` levels 0-6,
  17,000 seeds exact; the AGG driver drops an overprinted CMYK image's Darken, so overprint changes
  nothing), and so is Type 3 text (`pure/render_type3.py`: ProcessType3Text, the glyph cache with
  AdjustBlue, TransformTo, form glyphs of paths/images/shadings/nested Type 3 text; widths in floats,
  LoadChar's depth-4 guard and the per-document font map; oracle `tools/render_torture_type3.py`,
  6,400 seeds exact, 1% refused: glyph images that are not masks), and so is TrueType text,
  embedded or a GDI system substitute (FreeType's glyf loader and bytecode interpreter, v40,
  pedantic, 64 ppem: `pure/truetype.py`, `pure/ttinterp.py`; torture `--kind cid-truetype` 600
  seeds exact; tricky/variable fonts and glyphs hinted differently after earlier loads refused),
  and so are coloured tiling patterns (`pure/render_pattern.py`: CPDF_TilingPattern::Load parses the
  cell as a form over its own resources with the painted object's general state, CPDF_RenderTiling::Draw
  stamps one rendered cell over the clip box - 8x8 then stretched under 16 px, whole-pixel steps with
  C's truncating division in the aligned case - or, when the cell is bigger than the clip, draws the
  cell's objects again per tile with the painted path's fill alpha as their initial alpha; oracle
  `tools/render_torture_shading.py --mode tiling`, 4,000 seeds, 2,618 drawn exact, none apart),
  images inside soft masks (LoadSMask's std conversion, TransMask's DeviceCMYK scanlines, an image's
  own /SMask dropping the state mask, kAlpha mode: image torture levels 7-8, 3,860 seeds) and
  ICCBased spaces PDFium detects as sRGB (3144 bytes, `sRGB IEC61966-2.1` at offset 400: DeviceRGB
  without the clamp, `colors.icc_srgb`).
  Whole pages: all 269 of the test decks' pages render byte for byte
  as PDFium's, none apart (`test_whole_beamer_pages_render_as_pdfium_renders_them`);
  a page with anything not ported yet (JPX/JBIG2/CCITT or ICC-profiled images, uncoloured or in-form
  tiling patterns, transfer functions on images…) raises PdfError, so
  `renders = False`: `classify` runs on it and `convert` doesn't yet. Every call equals PDFium's on 4,373 pages
  (chars and object boxes to the last bit on the test decks). Swept over the 3,198 distinct PDFs on this
  machine: extraction and whole-page renders equal on 72,249 pages (up to 60 each, /ActualText marked
  content included; one torture shading refused), and on their first 10 pages every other call too -
  drawings, links, glyph widths, clipped/transparent/partial renders and `embedded_image` with
  PDFium's own bitmaps (GetBitmap, GetRenderedBitmap: `backend._image_pixels`, `_rendered_image`).
  Which pages are refused was measured the same way (`devtools/refusal_sweep.py`, read-only, one
  process per slice, resumable): of 38,017 pages in
  4,435 PDFs, 53 (0.14%) in 27 documents - ICC profiles 32, text needing a fallback font 11, tiling
  patterns 2, and no JPX, JBIG2, CCITT or image transfer function anywhere. With sRGB profiles,
  soft-masked images and tiling patterns ported, the only real documents left are 3 PDFs whose 9
  pages use one 536-byte v4.3 matrix/TRC RGB profile (lcms's matrix-shaper path, ~1,000-1,500 lines
  to port bit for bit); everything else still refused is a file the tortures and the structure fuzz
  wrote themselves.
  Measured on Windows; Linux and macOS answer as their own PDFium does too: substitution goes
  through CFX_LinuxFontInfo / CFX_MacFontInfo's folder scan off Windows (`fontmapper.FolderFontInfo`,
  `platform_font_info`: readdir order, first face of a name wins, a TTC face always loads as index
  0), float->int casts are the CPU's (`crt.i32`/`u32`/`i32_array`: arm64 saturates, x86 gives
  INT_MIN; the macOS wheel is arm64), a WideString holds code points where wchar_t is 32 bits
  (`navigation.wide`: name trees compare them, an astral /ActualText char is skipped), FreeType's
  sorts call the platform's own qsort (`crt.qsort` via ctypes: glibc's is stable), a Type 1
  font CoreGraphics accepts takes macOS PDFium's CoreText glyph map (`crt.quartz_font`), and a
  CalRGB colour that powf turns into NaN comes out black on x86-64 but red in the arm64 build,
  where std::clamp's inverted compare saturates the red channel (`cie._srgb3`).
  `devtools/platform_check.py` runs every oracle plus `subst_extract` (text in made-up
  non-embedded fonts, both backends) on whatever OS it is on, and `.github/workflows/pure-pdf.yml`
  runs it on ubuntu/macos/windows with pinned versions (`.github/constraints.txt`) and decks built
  once in a TeX Live container (`tests/decks/build.py` reruns until the .aux settles: TeX Live's
  tikzmark needs a third pass); failing shading seeds leave their PDF and both renders in the
  artifact. All three platforms pass.
  And deck.json is identical on all 48 test decks; extract is ~2.1× slower than PDFium, rendering
  ~9.9× (`devtools/pure_bench`: 11 decks at the test zoom, the minimum of several passes with both
  readers timed in the same rounds, and an A/B measured *interleaved*, since the same unchanged file
  drifts 8% with the machine's state; float32 rounding batched through `syntax.F32X*` structs with a
  scalar fallback on overflow, one regex per word in both lexers, psLib shortcuts for Type 1
  programs, ftgrays' LCD filter as one numpy convolution). What is fast is the C's own shape:
  `FT_MulFix` and the FT_Long cast without the mask C does not do, `gray_sweep_direct`'s
  `FT_FILL_RULE` and its two casts written out where the C has them (-31% of the sweep),
  `cf2_buf_readByte` and the stack's casts inline in the charstring interpreter (-11% of
  `Face.units`), and `DrawNormalTextHelper`'s three LCD taps as strides of the glyph row rather than
  three fancy-index gathers (-26% of it) - together -7% of a render pass; `fonts.Program.glyph_box`
  reads a glyph's extent off the points the charstring draws instead of growing a box through
  `ControlBoundsPen` (-13% of it). Of a render pass, 38% is drawing text: 29% the 1,390 glyphs the
  cache misses (16% ftgrays rasterising, 10% loading and transforming the outline, of which 8.5% is
  the charstring) and 6.5% composing the cached bitmaps. `tests/test_pure_pdf.py`.
  Cross references (CPDF_Parser, rebuild included) and navigation (`pure/navigation.py`: links,
  actions, destinations, name trees, page labels, metadata) are ported rule for rule; the whole-file
  fuzz (`--structure`, seeds 0-500) differs from PDFium on none (27 before font substitution was
  ported). Substitution is PDFium's chain rule for rule (`pure/fontmapper.py`: LoadSubstFont,
  FindSubstFace, CFX_Win32FontInfo through GDI's own CreateFont/GetTextFace; the TrueType and
  Type 1 LoadGlyphMap over FreeType's charmap list), with PDFium's built-in Foxit faces (MM ones
  blended, `pure/type1.py`) loaded from a user cache that `python -m beamer2slides.pdf.pure.foxit`
  fills from PDFium's sources with pinned SHA-256 (no binaries in the tree); without the cache the
  older rules stay and `test_substituted_fonts_are_measured_with_pdfiums_face` skips - with it the
  test runs everywhere against the platform's own chain (`platform_font_info` picks it; CI fills the
  cache on all three and its conformance step runs the same 402 tests on each). Substituted text in a Foxit face draws as PDFium's (`render_text._SubstFace`: Symbol and
  ZapfDingbats CFF; FoxitSansMM/SerifMM blended per glyph to weight and /Widths width - process-wide
  face state, as in PDFium - skewed by the italic angle; GetCharPosList's spacing heuristic; oracle
  `tools/render_torture_subst.py`, made-up non-embedded fonts, 6,000 seeds exact - a face's blend is
  process-wide state that the width of a code with no /Widths reads (LoadCharMetrics loads the glyph
  without setting the axes), so the torture puts both readers' faces at one blend between seeds with
  a page whose fonts no platform's font folder can answer: a font whose widths are all one number is
  FIXED_PITCH, which macOS answers with Courier New); GDI's TrueType
  substitutes (base 14 and installed names on Windows) draw through the TrueType port
  (`render_text.truetype_face`, one shared face per program; `--pool installed`: 300 seeds drawn exact). A font
  with no descriptor has flags 0 (PDFium's m_Flags default), not nonsymbolic: a TrueType one then
  maps codes through the Mac cmap (subst seeds 18, 21, 29).
  A code the font itself must not draw (`CPDF_Font::ShouldUseFont`) comes from the font's one
  fallback face (`render_text.fallback_font` = FallbackFontFromCharcode: LoadSubstFace of Arial at
  the descriptor's StemV × 5, the font's flags and italic angle; `fallback_glyph` = that face's
  charmap on the code's first Unicode unit, 0 meaning none), and the char pos list is cut into runs
  of one fallback position, one device call each, as CPDF_TextRenderer cuts it; the spacing
  heuristic measures the face the char is drawn from with `CFX_Face::GetGlyphWidth`, whose EmAdjust
  truncates where GetGlyphTTWidth rounds (`fonts.em_width`, a pixel on 2048-unit Arial). A cached
  system face is an ObservedPtr, not a face for the life of the process: it is held by the documents
  whose fonts took it and dropped when the last of them closes (`fontmapper.hold` / `release`,
  `Document.close`), or the Mac charmap `CPDF_TrueTypeFont::LoadGlyphMap` leaves selected on the
  shared Arial face decides the next document's fallback glyphs (subst torture seed 316 after 311;
  PDFium renders both orders alike). 6,000 seeds exact, nothing refused for a fallback font; a
  fallback for vertical writing (LoadSubstFace's IsVertWriting) still is.
  The face a fallback lands on may have no family at all - `UseInternalSubst` sets none on its
  standard Foxit faces, only `ConfigureExternalSubst` does - and `CFX_SubstFont::IsActualFontLoaded`
  is a `ByteString::Find`, which never finds an empty needle, so such a face has *not* loaded the
  actual font and its glyphs are still moved or narrowed to the /Widths width; only a folder scan
  with no Arial reaches that (GDI answers with Arial, macOS with Helvetica), which is why the ubuntu
  runner alone drew subst torture seeds 13, 25, 26, 29, 90 and 129 apart. `render_torture_subst
  --fonts DIR` reproduces another platform's chain here: `FPDF_InitLibraryWithConfig`'s
  `m_pUserFontPaths` makes PDFium itself substitute from that folder alone through
  `CFX_FolderFontInfo` (on Windows `CFX_Win32FallbackFontInfo`, whose `MapFont` is
  `CFX_LinuxFontInfo::MapFont` for a non-CJK charset) and the pure reader through `LinuxFontInfo`
  over the same folder - exact but for `FindSubstFace`'s two `#if BUILDFLAG(IS_WIN)` switches, which
  are the build's: Symbol and narrow fonts say nothing about the other OS.
- No public links: pictures reach Slides inside the imported .pptx, never as shared Drive
  files (they break in protected Workspace domains).
- **Fidelity is measured on Google's own renderer**, not a local preview: render the PDF page
  to PNG, export the emitted slide with `presentations.pages.getThumbnail`, and compare
  them (whole slide and per text box). A calibration deck measures font substitutes
  (width ratios, first-baseline offsets) once and feeds correction tables.
- Local classification debugging = PNG of the PDF page with element boxes drawn on it.
- Findings about beamer PDFs: `docs/pdf-findings.md`. IR draft: `docs/ir.md`.
- Overlays: non-handout PDFs keep the last step of each frame (same page label and heading);
  `--overlays all` keeps every page. The Slides API cannot create animations.

## What becomes native (element kinds in deck.json)
- `text`: paragraphs, bullet lists (glyph, number, image-ball and vector bullets), inline
  math as runs with sub/superscripts, links (external URLs and `#page=N` internal links →
  Slides page links), code blocks. Frame titles, the title page title and big lone headings
  use the layout's TITLE placeholder (role `title`).
- Bullet shapes (`emit.BULLET_SHAPES`, `tools/probe_bullets.py`): bullets keep their PDF colour
  and height (ball colour sampled in render, `ink_colour`; vector bullets' shape from their path,
  `classify.bullet_shape`). The glyph is a preset plus a nesting level (`emit.bullet_level`: ● ○ ■
  repeat below level 3, other glyphs exist at one level only); ranges starting deeper than level 0
  get a dummy first paragraph. Icon-font labels (`\ding`), unrecognised vector bullets (the
  bibliography icon) and `\includegraphics` labels become pictures anchored to their item.
  Short labels ending where a neighbour's hanging label ends get its tab (`label_tabs`).
- `image`: figure regions (TikZ, plots, raster images, plus their labels) cropped as pictures.
  A region that is one `\includegraphics` and nothing else (`classify.bare_image`, element key
  `image` = the raw image id) keeps the image's own box instead of the 1 pt margin a drawing
  gets, and `render.image_file` writes the embedded image itself, not a render of the page
  (element key `picture`): `raw` = the stream as stored, the author's JPEG byte for byte, when
  it is plain DCTDecode that Pillow decodes exactly like PDFium; else `decoded` = PDFium's own
  pixels as a PNG at the image's native size (a palette or CMYK JPEG converted, ≤ 4096 px).
  Anything see-through (soft mask, `\includegraphics` under `opacity`), turned, mirrored,
  clipped, exotic (< 8 bpp, unknown colour space) or labelled stays a page crop, and
  `render._looks_like` lays the file on the page without the image and compares, so a decode
  PDFium reads differently can never reach the deck. Google stores the picture byte for byte up
  to ~2046 px on the long side, so `pull` gets the author's file back (`tools/probe_pdf_images.py`
  prints the decision table for a PDF, `tools/lossless_images_proof.py` proves it on a live deck).
  Test decks: `07_images`, `23_raster_images` (one case per frame, `tests/test_raster_images.py`).
  **A chart's labels are the chart's** (user report: axis labels in the wrong spot). A tick row
  longer than a short label ("200  400  600  800  1,000", `PageClassifier.tick_row`: pieces far
  more than a word space apart, touching spans joined) stayed text, so the chart became an
  *overlay* anchored to it and emit moved and stretched the chart after Slides' words. Titles and
  axis titles pushed off the axis (`title style={yshift}`), too far for the 0.8 em label rule,
  became left-aligned text boxes that lean off their axis in the wider stand-in font
  (`axis_titles`). Now a short line centred on a *plot* - a drawing carrying three or more tick
  labels - is the plot's, sideways for a turned y label. `title_bridges` pull it into the
  picture's cluster, since a label no picture holds stays in the background. A caption
  (`CAPTION_RE`, "Figure:") and a sentence stay text. Nothing changed on the 54 test and theme PDFs;
  `03_figures` pages 5-6, `tests/test_classify.py`.
- `table`: text framed by equal-width horizontal rules (`\hline`/booktabs), with optional
  vertical and partial rules (→ per-cell `borders`) and merged cells (`merges`: chunks
  crossing columns, rows halfway between rows). Cell lineSpacing is tightened so rows keep
  the PDF pitch (`emit.table_line_spacing`).
- `diagram` (`classify.diagram_from`): figure clusters of only rectangle/rounded/ellipse nodes,
  straight lines and small arrow tips (open, filled, stealth) → shapes, lines and text boxes
  (node labels and free edge labels), grouped. Lines with filled tips are extended to the tip.
  Diagrams stay intact when edited: a label that fits the node's text rectangle (`label_inside`;
  Slides uses the .pptx text rectangle, an ellipse's is its inscribed square) goes inside a
  padding-free template node, otherwise node and label form a sub-group; line ends on a node's
  connection site are connected (`connection`), and `|-`/`-|` paths become one elbow connector
  (template bentConnector3 with adj 0 or 100000), so edges follow moved nodes.
  Card text (`card_text`: several sizes or lines in a node, e.g. a number over a caption) becomes
  text boxes on the PDF baselines, split where Slides can't set two paragraphs that close.
  `closed_frames`: four stroked lines closing a rectangle (`\fbox{\parbox}`, `\fcolorbox` around a
  minipage) are one rectangle node, merged with the fill inside; its justified lines stay one
  left-aligned wrapped paragraph.
- `shape`: opaque filled panels such as beamer blocks, not touching the page edge, with
  nothing left in the background on top; verified against the rendered colour. Also
  figure clusters of plain filled rectangles (`plain_rectangles`, role `rule`).
- Text decorations (`text_decorations`): a thin rule tightly under words → run `underline`,
  a rule through the x-height → run `strike` (ulem draws one piece per word: pieces are merged),
  a filled box tightly around words on one line → run `highlight` (backgroundColor). soul's
  `\hl`/`\st`/`\ul` draw overlapping pieces per word and wrap across lines: pieces are merged too;
  a wrapped underline's next line of text doesn't count as words under a fraction bar.
- Words on small graphics (`graphic_holes`: `\circled`, keycaps, `\fbox`, badges, dashed boxes,
  `\textcircled`'s overlapping glyphs) become holes like inline formulas, so text and graphic
  move together; `Line.hole_pads` widens a hole to the graphic (a closed frame may be wider than
  its words: `\framebox[2.5cm]`; the space after a hole counts from the graphic's end). `\cancel`
  strokes are LINE10 glyphs (icon font → graphic → hole). `join_braces`: an `\underbrace` /
  `\overbrace` glyph line and its label join the prose line, so the formula hole takes them.
  Small graphics on a native panel (the QED box) become pictures (`specks_on_panels`), or the
  panel would hide them.
- Graphics drawn over or at native words (`classify.overlay`: tikzmark arrows, braces, callouts,
  emphasis ellipses) become transparent pictures of only their own drawings and labels
  (`overlay`, `render.crop_overlay`), anchored to the text. Their `marks` (word edges they meet,
  with the words before, holes and em-space gaps closed up) are measured with the holes
  (`measure_places`): the words they lie on (`mark_words`: found in the box text by the words
  before them; a left edge pairs with the right edge on its line) are highlighted in a mark
  colour kept apart from same-coloured highlights nearby, `pick_word` finds them, and
  `fit_overlay` moves the picture by the mean drift and stretches it by the least-squares slope
  (±30% measured: a brace spans its phrase, an arrow meets both words; the callout's label
  stretches along, 5% on the test deck). The move is a RELATIVE transform with scaleX, its
  translation corrected for the scale about the page origin. Unmeasured marks keep the
  prediction (`mark_drifts`, formula-gap model; `overlay_boxes`, ±10%).
  A translucent fill over text (`opacity` < 1) becomes a `highlight` shape with fill alpha,
  anchored to that text. Text turned ±90° (`rotated_texts`) becomes a text box laid out in its
  own frame and turned by the transform. Path bounds use the curve's extremes, not its control
  points (`pdf.api.curve_extremes`). Test deck: `22_overlays_on_text`.
- Accents PDFium reports as separate chars (`ACCENTS`: ¯ ˆ ˜ …) become combining marks on
  their letter (X̄), also when the accent landed in the previous span.
- **Hebrew and Arabic read the way they are written** (`bidi.py`): a PDF says nothing about reading
  order, it draws glyphs at places, and a right-to-left run reaches the pipeline written left to
  right - whether the writer drew it visually (most do, so PDFium's text page turns it round) or
  logically (LuaTeX with `bidi=basic`, which is what `adopt` and every Hebrew or Arabic talk
  compiles with, so that pass turns it *back to front*). Measured on the corpus deck
  `hebrew-lesson`: the content stream draws `0x5d1 0x5e1 0x5d9 0x5e4 0x5d5 0x5e8` at falling x
  (בסיפור) and `FPDFText_GetUnicode` gives it back reversed at rising x. **Every Hebrew word this
  converter ever wrote into a deck was spelt backwards** and nothing said so - the glyphs are all
  there, the geometry is right, and no check in this project reads a word. Visual order is turned
  into logical order at the two sizes a line has, since `extract` cuts it into spans by geometry
  long before anything asks what it says: `logical_text` for one span's characters and
  `logical_spans` for a line's spans, both the same rule (the order turns round and each island of
  the other direction - a Latin name, a year - turns back inside it), both their own inverse, so
  they are right whichever side did the reversing. A whole line often *is* one span (Hebrew word
  spaces are under the word gap), so reversing each run in place - `CloseTempLine`'s own rule -
  spells every word right and leaves the sentence backwards; that is what it did first. Which way
  a line reads is Unicode's P2 asked of the **answer** (`_base`: a reading holds up only if the
  text it produces would be drawn back the way the page draws it; where both readings hold up, a
  Latin sentence with a Hebrew phrase in it or the other way round, the commoner letter decides),
  brackets are mirrored back, and a vowel point stays on its letter (`_clusters`). The seams in
  `classify` are `Line.text`, `reading_order`, `span_runs` and the run separator, which is now
  `gap_between` - provably the old `b.x0 - a.x1` for every pair drawn in order, so no left-to-right
  deck can move. Tests: `tests/test_bidi.py`, synthetic (the characters, not a font, so they run
  where no Hebrew font is installed). What is left in `arabic-training` is not order but a font
  with no ToUnicode: two glyphs come back carrying the same *pair* of letters, so one word has two
  letters too many.
- **And the deck is told which way its paragraphs read.** Reading them right is half of it: a
  paragraph Slides takes for left-to-right puts a Hebrew sentence's full stop at the wrong end,
  hangs its bullet on the wrong side and walks the cursor the wrong way. `classify.Paragraph.direction`
  is `rtl` where the paragraph reads right to left (`bidi.reads_rtl`: Unicode's P2 over the words as
  they are *now* read, the same rule as `_base` asked of a question rather than an answer), said in
  deck.json only where it is true and spelled as `deck_ir` spells it of a deck read back, so the two
  sides of `pull` agree and `scripts.py` puts such a paragraph in an `otherlanguage`. `emit` then
  writes `direction: RIGHT_TO_LEFT` - and **mirrors the alignment**, because START and END are the
  reading direction's own ends while what `classify` measured is the page's left and right
  (`emit.hugs`: a Hebrew paragraph whose lines end together hugs the *right*, which `align` calls
  "left" because they start together too - justified prose - or because there is one line and
  nothing was measured at all). indentStart is mirrored the same way, and a bullet's from the
  bullet box's left edge. Measured on Google's own renderer (`tools/probe_rtl.py`): the API takes
  `direction` and reads it back, START puts a Hebrew word at 230-292 pt of a 300 pt box and END at
  7.6-70.2, and a paragraph never told reads back `LEFT_TO_RIGHT` and sits at the left - which is
  where every Hebrew deck this converter wrote has been sitting. Tests: the last section of
  `tests/test_emit_requests.py` (a paragraph, its edges, a bullet and a table cell).
- Hole and overlay pictures are placed by measurement (`emit.measure_places`, ~2-3 s per deck):
  scratch slides get copies of the text boxes with holes or overlay words (run highlights
  removed), every hole run and marked word highlighted in a mark colour and all text black; one
  thumbnail each (in parallel) shows the real gaps (`find_marks`, sub-pixel;
  `pick_gap` also follows a hole Slides wrapped onto another line), a hole hanging past the box
  edge at a line end is found from the ink before it (`ink_end`), and pictures get a RELATIVE
  transform before grouping; scratch slides are deleted with the sources. Debug output:
  `out/.../holes/marks-NNN.png`, `moves.json`, `overlays.json` (per mark: x, predicted and measured
  drift). `--predict-places` (alias `--predict-holes`) skips it. `fit_holes` sizes a
  hole to the PDF room up to the next word (`next_x0`) less a Slides space, and `hole_offset`
  shares the spaces left and right of the picture in the PDF's proportion.
- `formula_shifts` (the fallback prediction) uses Slides' symbol advances
  (`emit.SYMBOL_ADVANCE_EM`, measured by `tools/probe_symbols.py`) and TeX's stretched spaces
  (`space_shift`; a gap holding an earlier hole counts as a space plus that hole); holes get
  `HOLE_PAD` (1 pt) each side so the picture never touches a word. The `text_overlap` score
  can drop slightly when pictures follow Slides' words rather than the PDF positions.
- Inline formula holes (`formula_holes`, `Line.holes`): in a line of prose, complex math
  segments (bars, CMEX, stacked or second-level scripts) become image elements (role `math`,
  `anchor` = text element id, grouped with it). The text keeps a run with `hole` = width.
  Emit fills it with no-break spaces in Roboto Mono (a space is exactly 0.6 em), sized to the
  width, and shifts the picture by the predicted width error of the words before it
  (`formula_shifts`). Lines with fewer than two real words stay one display picture.
- Anchored pictures (holes, icons, number balls) are transparent (`render.clear_ground`): rendered
  again without what stays in the background (paths not inside the box + 5 pt, images/shadings not
  inside it), kept when the page under the crop is flat and the result laid on that colour shows the
  opaque crop, with opaque pixels fading into the ground near the edge turned into alpha
  (`unblend_rim`: ball shadings, clip edges). Else the opaque crop.
- Tables also take cell `fills` (filled rects inside a ruled table; their edges give column
  bounds) and simple math in cells (`span_runs` handles math fonts and scripts).
  `plain_tables`: rule-less tabulars (≥3 rows, same cell count, short cells) → borderless tables.
- `literal_list_numbers`: when Slides would number a list wrongly (TOC split into boxes),
  every number on the slide becomes literal text with a tab (Slides can't set a start number).
  Labels on balls, circles and squares (numbers or letters) always become the ball picture with
  a centred text box on it (`number`, `emit.number_box_requests`), grouped with the item text.
- Grouping in emit: blocks (`block_groups`: shapes with the same `block` plus content), formula
  pictures with their text, progress bars with their track (`rule_groups`). Title page:
  `subtitle_element` → SUBTITLE placeholder.
- Blocks built to survive resizing (`classify.blocks`, `emit.merge_blocks`): the body reaches up
  under the title bar with the whole block's outline (a group resize never opens a gap), title
  bars are created after bodies (z-order), block titles fill their bar with contentAlignment
  MIDDLE (baseline = middle + 0.362 em, `tools/probe_middle.py`), and the body carries a native
  drop shadow. The gradient strip and shadow pieces are painted out of the background.
- The imported .pptx (`build_pptx`, also for 16:9; rebuilds replace the content through
  `files.update`, keeping the URL) has one source slide per deck slide: its layout, its own
  background picture or colour (none when it inherits the master's), its pictures at their exact
  boxes (alt text in `descr`) and, if needed, template shapes. Phase 1 `duplicateObject`s each
  source slide with an objectIds map (`b2s_sNNN`, pictures `_fI`, template shapes `_kJ`,
  placeholders `_tK`), which keeps backgrounds and pictures; the source slides are deleted at the
  end (`tools/probe_pptx_pictures.py` verified what the import keeps). Template shapes are restyled
  copies giving what the API can't set: drop shadows (calibrated against beamer:
  `tools/calibrate_shadow.py`, distance 0.75 and blur 1.0 × shadow width, alpha 0.5) and exact
  corner radii (`adj`). Pictures get `BRING_TO_FRONT` in element order after the other content.
- Nothing stays behind when elements move (`tools/leftovers.py` checks it locally): pictures and
  block decorations are painted out of the background where the page around them is flat;
  numbered balls under literal TOC numbers become pictures grouped with their text.
- One-line left-aligned text boxes reach to the block edge or the next element
  (`text_right_limit`), so text typed later wraps where a user expects.
- Equation numbers beside display equations, icon-font glyphs (pictures) and OpenType
  small caps (`extract.small_caps_spans`, via glyph ids) are handled too.
- Hanging labels (`Line.tab`, paragraph `tab_x0`): algorithmic line numbers, description
  items and item labels without a Slides preset are written `label<TAB>text` with
  indentFirstLine at the label and indentStart at the text (Slides tabs jump to indentStart).
  Description labels pair only across a label gap (`LABEL_SEP_EM` 0.4 em; beamer's labelsep is 0.5 em,
  word spaces 0.33 em): prose whose word edges line up by chance with an item is no description.
- Layouts (`style_layout_placeholders`, `write_layout_texts`): the master's and every layout's TITLE /
  CENTERED_TITLE placeholder gets the position and text style of the deck's frame titles /
  title page, BODY placeholders the most common body font, and shared footer texts go
  onto all layouts. A slide added later in Slides then looks like the converted ones. Title colours
  unreadable on the master background there (`readable_run`, contrast < 2: white title-page text on a
  native panel) take the panel's colour, else black/white.
- Backgrounds: identical PNGs are stored once in the .pptx; the most common background is set on
  the master (layouts and those slides inherit it). Frame counters (`FRAME_COUNTER_RE`) become
  per-slide text elements (role `footer`), so theme backgrounds become identical.
- Theme decoration on the layouts (`emit.plan_theme`, `render.theme_decoration`): the pixels (≠ page
  ground) that all backgrounds of a group show alike become a full-page binary-alpha picture at the
  bottom of the layouts (title pages → TITLE layout, the rest → every other layout). Layout pictures
  draw above the slide background and below its content, so a background colour set in Slides keeps
  bars and footlines (navigation text that differs per section shows as cut-outs), and new slides get
  them. Backgrounds that don't show a group's decoration go on layout copies (`TITLE_ONLY_V1`, shown
  "Title Only (theme 2)", up to 3, the last "(no theme)"). Slide backgrounds keep their meaning (own
  picture, or inherit the master's); the master becomes the ground colour when the shared background
  is exactly ground plus decoration. Thumbnails match the old structure except 1 px rows at bar edges.
  emit.json `theme`: ground, master colour, decoration files, page → layout name.
- Fonts: `fonts.google_font` passes Google fonts used in the PDF through with their weight
  (weightedFontFamily, no width correction) and maps Helvetica/Times/Courier clones to
  metric-compatible Arial/Times New Roman/Courier New. CM fonts use the calibrated substitutes.
- Simple inline fractions (`classify.simple_fraction`) are written as ᵃ⁄ᵦ; their bars leave
  the background via the text element's `strokes`.
- Performance: one .pptx upload, then slide content in batches of up to 400 requests; the test
  decks convert in 12-20 s (extract + classify + render of a 30-page deck take about 2 s).
  **A conversion is round trips, not work** (profiled call by call by wrapping
  `HttpRequest.execute`; the 48-slide `ambiguous.pdf` is 31 calls either way): 84% of the wall
  clock has a request open and 3.9 s of it is this machine, because a round trip costs ~0.8 s
  whatever it carries - a `batchUpdate` of 400 requests comes back in about the same second as one
  of 49, and narrowing `fields` on a read moved it 0.76 -> 0.69 s. So the lever is how many are in
  the air at once, and it is **threads only** (the harness this runs inside at Google has no room
  for processes). Measured before any of it was written: several `batchUpdate` calls may be in
  flight on one presentation and nothing is lost (`tools/probe_batch_parallelism.py`: 8 batches of
  200 requests take 9.37 s one at a time, 6.10 two, 3.61 four, 3.07 eight, 3640/3640 objects
  landing every time; a layout batch beside four slide batches is fine), while **PDFium is not
  thread-safe** - four threads with their own `Document`s crash with `Failed to load page` - so
  every PDF read stays on the main thread. Six changes, each falling back to the old serial order
  when a caller handed its own client over (`google_auth.shared_service`: a service object is not
  thread-safe and a lent client is that caller's), with `credentials_for_threads` resolving
  credentials on the calling thread because a worker inherits no context: content batches
  `CONTENT_WORKERS` (4) at a time with a client per thread (`gslides.per_thread`), the layout and
  master pass on a thread of its own (`emit.write_layouts`, overlapping the placeholder read and
  `measure_places` and **nothing below them**: it is joined before the first content batch is
  dispatched, because a slide's TITLE placeholder inherits its layout parent's box until that
  batch gives it one of its own, so the two are writing one box and the one Google commits *last*
  wins - a layout batch landing second takes every title's own box away again, silently, every
  request accepted and no warning anywhere, which is what broke 10 of 45 live sync scenarios;
  `tools/probe_layout_race.py`, and what `tools/probe_batch_parallelism.py` measured is that two
  batches in flight lose no *request*, which is a smaller promise than it reads as),
  the rebuild guard asked while the PDF is converted
  (`emit.preflight_in_background`, collected before the first write to Drive; the agent layer's
  `deck_convert` keeps the synchronous one, its docstring promising a refusal costs a second),
  `guard.check_rebuild` fetching the base and the live deck at once, one client per thread rather
  than per slide in `measure_places`, and the base file's Drive place looked up while the deck is
  read and tagged (`snapshot.save_drive`'s `info`). Google's own latency swings 21-44 s on the same
  unchanged conversion, so every figure is an **interleaved** A/B in one sitting (`pure_bench`'s
  lesson): the two `emit` changes are worth 9.2 s of the run (38.4 -> 29.2 s mean over four pairs,
  every pair favouring threads) and the content batches alone 6.6 s (28.9 -> 22.3, three pairs of
  four). Then the **tail**, which is the one place with nothing left to overlap it with, and the
  **second ask**, which was the whole question twice. A tag batch's own answer says the revision it
  left the deck at (`writeControl.requiredRevisionId`) and the only other news a second
  `presentations.get` brings is the alt text that batch has just written, so that read is not made:
  it is patched (`snapshot.tagged`, `write_tags` returning what landed), and the base built from
  the patch is equal to the base built from really reading the deck again, field for field and
  revisionId included - measured on the 48-slide `ambiguous.pdf` (181 tags), which is what lets the
  read go. The pictures are signed meanwhile (`picture_signatures`, `sign_pictures`' `ready`): they
  hang off contentUrls, not off a Google client, so that thread needs nothing of anybody's. And the
  guard is asked once and *confirmed* the second time: the preflight hands its finding on
  (`preflight_rebuild` returns it, `emit.plan_rebuild`'s `checked`) and `guard.recheck` is one field
  of one read - a revisionId that has not moved is a deck nobody has touched - made beside the
  folder's Drive lookup (`emit.look_again`), while a revision that moved, a finding with a reason,
  a deck the folder no longer points at and a read that failed all ask the whole question again.
  The one rule is the one thing that may not move for a second, so it is proved live as well as in
  `tests/test_guard.py`: two characters typed into the deck *between* the two asks, and the rebuild
  is refused naming them. Interleaved A/B again, four pairs: 21.9 -> 20.1 s mean, every pair
  favouring the new tail, of which the tail itself is 5.6 -> 4.3 s and the second ask 1.55 -> 0.74.
  Charts: https://claude.ai/artifact/MsF5T7Kxo9R3LKcLqAGRTt
- Speaker notes: beamer note pages (`show notes`) or `show notes on second screen`
  (`notes.py`), written to the slide's speaker notes.
- Everything else (display math, theme decoration, header/footer text) stays in the
  background picture. A background with nothing left becomes a plain background colour.
- Theme robustness: `tests/themes/sweep.py` compiles a realistic talk with 28 beamer themes
  and classifies them locally (no Google calls).
- Offline request tests (`tests/test_emit_requests.py`, ~5 s for all built decks): `emit.plan_offline`
  plans what `build_deck` sends (`DeckPlan`: .pptx picture boxes, phase 1 copies, each slide's parts;
  `measure_jobs`: measure_places' scratch slides) against a made-up imported presentation, and the tests
  replay it: object ids, groups, anchored pictures grouped with their text, numbers centred on balls,
  hole widths, bullet styling order, text ranges and texts, page bounds, predicted shifts < 15 pt
  (overlay stretch ±10%), every overlay mark on a highlighted word.
  Unit tests there cover the placement helpers. `build_deck` only adds Google's answers and batching.
- Invariants (`checks.py`, `tests/test_invariants.py`, ~40 s over all test decks and theme talks,
  no Google calls): `convert_locally` extracts, classifies and renders with pictures kept in memory,
  then `stray_ink` (no background ink within 2.5 pt of native line boxes, bullets and anchored
  pictures, unless under an opaque shape or unanchored picture, on a raster image, or running on
  10 pt past the words: page structure), `stray_labels` (no small drawing/image/glyph left at
  x-height left of a bullet-less paragraph), `lost_ink` (PDF page vs background with shapes painted:
  every difference lies on native glyphs, bullets, pictures, tables, diagrams, strokes or shape
  rims/strips/shadows), `structure` (hole runs vs pictures after `emit.fit_holes`, number pictures
  on balls, overlay anchor/marks, decoration strokes, shape bullets, ids and references) and
  `junk_text` (private-use, U+FFFD, picture-font ❤❙✭, control chars, lone combining marks).
  Known problems go in `tests/invariants_allow.json` (deck, page, check, element/bbox, reason);
  stale entries fail. Each check was verified by breaking its mechanism on purpose.
- Line building: words don't join across a column gutter (`PageClassifier.gutter`), lines up to
  1.45 em apart continue a paragraph, centred lines TeX balanced keep their breaks as soft
  breaks (chr 11), and rule-less tables whose cells wrap like prose aren't tables.
- Google theme (`themes/google`, README there): a beamer theme reproducing the GDG 2024 speaker
  template in Google Sans Flex, written for AI authors; sizes in `\gpt` so the theme's baselines
  follow Slides' text model (first baseline 6.48 + 0.968 em, pitch 1.2 em × line spacing).
  Its fonts are not committed (no third-party binaries in the tree): `themes/google/fonts/build_fonts.py`
  downloads the variable fonts from google/fonts, checks pinned SHA-256 sums and cuts the static
  .ttf files; `make.ps1` runs it when a font is missing.

## Google side
- GCP project `beamer2slides` (personal Gmail): Slides and Drive APIs enabled. The OAuth
  consent screen is External / Testing with the owner as the only test user, so refresh
  tokens expire after 7 days and the browser consent must be repeated.
- Auth: `src/beamer2slides/google_auth.py`, scopes `presentations` + `drive.file`.
  `client_secret.json` and `token.json` sit in the repo root, are git-ignored, and
  are ACL-restricted to the current user. Never print or commit their contents.
  An installed (pip) beamer2slides finds them in `%APPDATA%\beamer2slides` /
  `~/.config/beamer2slides` instead; `$B2S_CLIENT_SECRET` / `$B2S_TOKEN` override both
  (`google_auth.credential_file`). Packaging notes: `docs/install.md`.
- **The client library is a dependency of one module** (`gapi.py`, `pip install
  "beamer2slides[google]"` since 0.4.0, docs/install.md "Injecting your own API clients"):
  `use_services` already let a caller hand over its own Slides/Drive/Docs clients - a harness
  whose builder answers from a bundled discovery document, `discovery.build()` otherwise fetching
  one per call - and the injection was complete except for the **imports**. `googleapiclient` sat
  at module scope in six modules, so `deck_prepare`, which needs no account at all, pulled it in
  through `snapshot` -> `emit` -> `gslides` and could not be *imported* in a gVisor sandbox that
  has no account and therefore no Google libraries (reported by a caller running exactly that,
  2026-09-22). Every use of the package is behind a function in `gapi` now (`build`,
  `media_upload`, `installed`), and the one piece that cannot be deferred - `HttpError`, which
  ~45 `except` clauses name - is bound **once**, there and nowhere else: two independent
  `try/except ImportError` fallbacks would bind two different classes and an `except` naming the
  other would quietly stop matching, which would cost a rebuild guard or a retry. With the
  package absent the stand-in is a class nothing raises, so every clause simply never matches;
  it carries Google's two fields all the same, since a harness that fabricates a refusal
  (`devtools.agent_tasks._http`) raises it. What the call sites want of an error is asked through
  functions rather than `e.resp.status` (`status_of`, `message_of`, `is_transient`), so a client
  of another shape could answer. `agent/auth.py` turns a missing package into a refusal
  (`offline`) rather than a traceback: a host with a token and no client library has a one-line
  fix, and `gapi.MISSING` always offers both ways out. Tests: `tests/test_gapi.py` - an AST walk
  for module-scope imports, one binding of the error class, and the library imported and driven
  in a subprocess where those imports fail.
- **A builder is handed the credentials the call site passed, which is usually nothing**
  (reported with the above): `emit()` opens with `slides_service(), drive_service()` and passes
  none, because the built-in fallback does `creds or credentials()` itself - so a builder that
  trusted the argument built an *unauthenticated* client. Either it does `creds or
  google_auth.credentials()`, or it says `use_services(make, needs_credentials=True)` once and is
  handed the library's (`_Services`, `_for_builder`; resolved once, so `shared_service`'s two
  builder calls cost one token). Left out, an injected client is still never a reason to go
  looking for a token - which is also what `credentials_for_threads` answers for a worker thread,
  a `ContextVar` not being inherited by one started inside the block.
- Smoke test: `tools/slides_smoke.py`.
- **`presentations.create` ignores `pageSize`**: new decks are always 720 × 405 pt (16:9).
  4:3 decks need another route (e.g. upload a blank 4:3 .pptx with Drive conversion).
- `getThumbnail` LARGE = 1600 px wide (1600 × 900 for 16:9). Thumbnail download URLs
  occasionally fail with SSL EOF; `gslides.save_thumbnail` retries.
- Object IDs must be 5–50 characters.
- **Font calibration** (`tools/calibrate.py`, results in `docs/calibration.md` and
  `src/beamer2slides/calibration/fonts.json`, package data so a wheel carries it, reached
  through `importlib.resources` (`emit.CALIBRATION_DIR`) and never beside `__file__`, so a zip
  import or a build that stages sources elsewhere finds it too):
  in API-created text boxes the first baseline sits at
  6.48 pt + 0.968 em and the line pitch is 1.19 em **for every font**. Default substitute
  for CM Sans is Lato at size / 1.020; titles (CMSS12) need their own factor (~1.035).

- The Slides API's `createImage` needs a URL Google can fetch; that is why pictures go through
  the .pptx instead (Google's import stretches a picture to its box; `createImage` would
  letterbox it).
- Emit robustness: a rejected batch is retried slide by slide, then element by element; if the
  API refuses elements, the deck is rebuilt once with pictures of those page regions in their
  place (`fallback_pictures`). A page whose classification raises becomes a full background
  picture (`classify_page`).
- Decks are uploaded python-pptx files with Drive conversion (the page size is kept; the default
  template's placeholders are rescaled for 16:9).
- Re-running `convert` on the same output folder rebuilds the previous deck in place
  (same URL, content replaced by `files.update`). Use `--new-deck` to force a new one.
- **A rebuild never destroys deck edits** (`guard.py`, docs/sync.md "Never lose deck edits"):
  before replacing a deck's content, convert compares the live deck with the sync base and refuses
  (exit non-zero, nothing written) when someone edited it in Slides, when there is no base to check
  against, or when the folder's deck came from another PDF; the message names what was edited (up to
  3 examples) and offers `sync`, `--new-deck` or `--force-rebuild`. "Edited" is `merge.deck_edits` /
  `user_objects` / `background_edited` / notes / slides added, deleted, reordered - the same notion
  sync uses; a new revisionId, a reissued `contentUrl` (pixel signatures decide), a thumbnail export
  and leftover `b2s_mNNN` scratch slides are not edits. `--new-deck`, a trashed/deleted deck
  (`guard.previous_deck`: live|trashed|gone|other) and sync's staging deck can't hit the wrong deck.
  Before any destructive write (forced rebuild, sync's first write) the revisionId, modifiedTime and
  finding go to `<out>/backups/backups.json` (rebuilds also into emit.json `previous`, syncs into
  sync-report.json `recovery`) and `--backup auto|none|file|drive|both` keeps a .pptx export and/or a
  Drive copy. **Measured** (`tools/probe_revision_history.py`): every Drive revision of a Slides file
  exports its *current* content, so `files.update` leaves nothing the API can fetch back - the .pptx
  backup is the only way back (`tools/deck_backup.py list|export|restore`). A backup Drive refused
  (export over 10 MB, copy over quota) stops the forced rebuild too (`guard.demand_way_back`):
  `--backup none` is how one asks for a rebuild with no way back. Tests: `tests/test_guard.py`
  (offline), `tools/rebuild_guard_proof.py` (live, `out/agent-guard/`) - which also proves the way
  back end to end: `restore --in-place` brings the deck back at its own URL with no word lost
  (slide text and speaker notes compared), and no sync afterwards writes over it. **Content comes
  back, identity does not**: Drive's .pptx import numbers the pages `p1`...`pN` of its own, so a
  converted deck's `b2s_s000`... is gone and the base names none of its slides - measured
  2026-09-22 by exporting a deck and putting the same bytes back, while the archived proof of
  2026-09-18 shows the sync of a recovered deck running and writing nothing, so this changed under
  us. Sync therefore refuses a recovered deck (`snapshot.base_matches`) instead of reporting every
  element as deleted, `deck_backup restore --in-place` says so and names `--new-deck`, and the
  proof checks the half that is a promise: the recovery stands and the deck's revisionId does not
  move. Re-keying the base from the `b2s:` tags that do survive (45 of 65 objects) would make it
  whole and is not done - they name only each element's main object, never a group or an element's
  other objects, and a base nearly right about identity is how a sync writes over the edits a
  recovery has just saved.

## Usage
```
python -m beamer2slides classify deck.pdf   # raw.json, deck.json, debug/ overlays
python -m beamer2slides convert  deck.pdf   # + backgrounds/, figures/, Slides deck, emit.json
python -m beamer2slides fidelity deck.pdf   # thumbnails vs PDF: fidelity.json, fidelity/diff-NNN.png
```
Outputs go to `out/<pdf stem>/`. In the diff PNGs, red = only in PDF, blue = only in Slides,
black = both.

Sync (docs/sync.md): `python -m beamer2slides sync new.pdf --deck <url|id|out folder> [--dry-run]`
merges source changes into the edited deck (three-way: base = converter output recorded by
`convert` in `<out>/sync/base.json` and in Drive via `appProperties.b2sBase`; deck edits win,
conflicts reported in `<out>/sync/sync-report.{json,md}`). Slide keys come from frame labels
(PDF named destinations, `extract.frame_labels`), else title/occurrence plus alignment; objects
carry `b2s:<slide>/<element>` alt-text titles (groups untagged: the API refuses alt text on them).
`identity.py` keys, `snapshot.py` read-back and
base, `merge.py` pure planning (offline tests `tests/test_sync.py`), `sync.py` writes with
`requiredRevisionId` (re-plans on a mismatch), pictures through a deleted-after-use staging deck.
Pitfalls: `createImage` letterboxes (sync stretches it back); staging contentUrls die with the
staging file; `createSlide` doesn't instantiate every layout placeholder.
A sync killed at any point loses nothing (docs/sync.md, "If a sync or a pull dies"): deletions are
the last phase and are listed in the base first, batches are cut at slide boundaries, a `pending`
marker in the base records what a run is about to create (and the placeholder texts it overwrites),
the next sync sweeps the duplicates and heals what an older version deleted too early, bases are
validated (truncated/foreign/newer/stale-in-Drive), and `pull --apply` replaces whole files through
a temporary, keeping a `.bak` and never overwriting a source edited since the pull started.
Fault injection for the tests: `B2S_FAIL_AT=<point>[:n]` / `!point` (`faults.py`, inert when unset),
tests in `tests/test_sync_crash.py` (offline, plus one live kill per point under the `sync` marker).
Bases are Drive-first: the folder copy is a cache, an older one only makes sync do less (deck edits
win), and a Drive base that can't be read is said out loud (`snapshot.stale_base_warning`, offline
tests `tests/test_base_storage.py`) - and makes sync sweep only leftovers that base itself names
(`plan_recovery(trust_generation=False)`: a later generation may be the other checkout's finished work). The base also records the overlay mode `convert` used, and
sync keeps it unless `--overlays` says otherwise (`sync.overlay_mode`), or a deck converted with
`--overlays all` would lose its in-between steps. `pull --apply` and `pull --out DIR` keep every
file they replace (`inverse.keep_backup`: `.bak`, `.bak2`, …, pictures included).

**The round trip an agent pays for** (deck → `.tex` → edit → compile → `sync` → the same deck):
the merge back is the half that repeats, and it is round trips, not work - the conversion's own
lesson at the other end of the pipe. Six changes, each falling back to the old serial order when a
caller lent its own client (`google_auth.shared_service`), measured **interleaved** in one sitting
against a worktree of the commit before them (`scratchpad/ab.py`: the same folder driven back and
forth between two sources, arms alternating and roles swapped so the direction of the edit is not
the explanation). (1) The deck is read while the base is loaded and the new PDF is converted -
three things that need nothing of each other - and `Sync.read` spends that answer once
(`first_read`; every later read, after a write or on a second attempt, is a fresh one). (2) The
deck's Drive facts are read once and handed to `load_base`, `stale_base_warning` and every
`store_base` (`snapshot.deck_info`, four reads to one). (3) The staging deck is built on a thread
of its own beside `measure_places`' scratch slides and thumbnails - a file of its own against pages
nothing else looks at, neither reading what the other writes - and `stage` takes the clients to use,
a service object carrying one connection. (4) The revision the write will require is asked for while
that staging deck is still being made: the scratch slides were the last thing to move the deck, and
somebody typing meanwhile is what the refusal and the re-plan are for. (5) The staging deck is sent
away on a thread at the end of the write (`drop_staging`), the one thing there nothing waits for -
collected before `sync` returns (`await_deletes`), never left to the interpreter, since a file
nobody deletes is one somebody finds in their Drive. (6) The third base upload is a **flag**: once
the leftovers are gone the `cleanup` list must stop being read, which is all it says, and
`snapshot.mark_cleaned` says it in one field of the deck's own appProperties for a third of a second
where a media update costs about two - `load_base`'s `swept()` drops a list the deck says is done,
and where the flag will not land the base goes up as it always did. Measured on a 13-frame talk,
three pairs each: **23.7 → 18.7 s** and then **22.9 → 16.4 s**, every pair of six favouring the new
code. Two things were settled by measurement rather than guessed: gzipping the base buys nothing
(a `files.update` **with media** costs ~1.8-2.0 s whether it carries 7 bytes or 147 kB, while a
metadata-only one costs ~0.29 s), and reusing the live deck's own `contentUrl`s instead of a staging
deck would break the lossless-picture promise `pull` relies on - `createImage` accepts them and
Google **re-encodes** the file (same pixels, different bytes). (7) And the **head**, which was the
one part of a sync that ran before the sync had made a call of its own: the way back a destructive
write keeps (`__main__.record_sync_point` - the deck's revisionId, its modifiedTime and a .pptx
export, 3.4 s of a 20 s sync) promises only to be finished *before the deck is first written to*,
not before the planning starts, so it is made on a thread (`guard.WayBack`) and every write site
collects it (`Sync.before_write`: the leftovers of an interrupted run, `measure_places`' scratch
slides and the batches themselves). Worth about **a second**, not the 3.4 s it costs, and the gap
is the finding: what it now runs beside is the source's own conversion and the deck's first read,
which want this machine and this connection too. Nine interleaved pairs: 18.97 → 18.00 s over the
last six (five of six favouring the thread), 0.60 s over all nine (six of nine) - small enough that
only the pairs say it at all, the same unchanged sync running 16.5 to 21.7 s with the day. Kept for
its second half as much as its first: `sync` is the one that knows when the first write happens, so
handing the note over is what lets an adopted deck's first sync ask whether a way back was kept
instead of being refused although one was (`agent.deck_tools.deck_sync` never passed it at all).
(8) And the **pending marker**, which is the last thing on that chain with nothing beside it: it has
to be durable before the first write and that is *all* it has to be, so it goes up while the staging
deck is still being imported (`Sync.pending_in_background`, collected before `send`). What it records
is the objects this run is about to create, and their ids are the elements' own - the **one** field
in a sync's requests that waits for the staging deck is a picture's URL, so the requests are built
first and the URLs put in afterwards (`picture_url` leaves a `b2s-pending:` marker, `fill_urls`
replaces it, and `send` fills again so nothing can go out carrying one; a picture the staging deck
did not bring is a loud failure there rather than a URL Google fetches nothing from). The marker's
`staging` field is then usually empty, which costs nothing: nothing reads it, the staging deck naming
itself in Drive (`appProperties.b2sStaging`, which is what `tools/drive_usage.py` queries). Worth a
whole media upload, ~1.9 s whatever it carries: **17.3 → 14.2 s** over five interleaved pairs, every
one of six favouring it (the sixth is a round where the old code stalled at 57 s, dropped rather than
counted). A sync of that talk is now 92% "a request is open" - what is left is barriers, each there
for a reason: the staging import, the marker, the content batch, the read-back, the base that must
land before the cleanup, and the flag that says the cleanup is done.

**And the compile between the edit and the sync**, which is the other thing the loop pays on every
turn: it stops when the auxiliary files stop moving (`inverse.aux_state`, latexmk's rule, used by
`Workspace.compile` and `playground/workbench.run_latex` alike) rather than running a fixed two
passes - 2.0 → 1.0 s per turn on a folder the turn before left settled, unchanged on a fresh one.
The log alone is not the rule, and that half is a **correctness** fix: a talk with no sections asks
for no rerun after its first pass (`needs_rerun`, rightly - there are no bookmarks to settle) while
Madrid's footline still reads `2/1`, beamer's `\inserttotalframenumber` coming out of the .nav the
*next* pass reads, so the pull loop's own first compile handed the page a wrong frame total and
then read it as a residual (measured both ways on a two-frame Madrid talk).
**And the export at the head of the round trip**: a `pull` read the deck and only then compiled,
two things that need nothing of each other, so the read is now made on a thread of its own while
the source compiles for the first time (`inverse.Later`, `converge`'s `ready`, `Workspace.build`'s
`compiled` - taken only when the workspace stood at the same options, so a caller cannot hand over
a PDF of another document; `agent.deck_pull` passes the same `Later`). Interleaved A/B, four pairs,
a work folder of its own per round so a warm cache favours neither: **5.34 → 3.20 s**, every pair
favouring the overlap - the deck read disappears behind the compile entirely.

**A conflict a person can settle** (`--take-source`, docs/sync.md "Taking the source's version"):
the deck keeping what it has is right as a default and is not always what the author wants, and the
only ways to say otherwise were editing the deck by hand or rebuilding it - the one thing this
project refuses to do. Every conflict now carries an **id** (`merge.conflict_id`: 8 hex over the
slide, the element, the field and the three versions), and `--take-source ID` writes the source's
version of that one. The id is the whole safety: it is stable while the same two changes stand
against each other and different the moment either moves, so one copied out of an older report
matches nothing, nothing is written and the run says which id found no home and why. Only conflicts
about what something *says* can be taken (`merge.TAKEABLE_FIELDS`: text, text_style, shape_style,
image, geometry, background, notes); existence and identity - `removed`, `deleted`, `part_deleted`,
a slide, a label - carry an id to talk about and refuse, because overwriting a paragraph leaves the
person's paragraph in the report verbatim and deleting their slide leaves nothing, which is
`--force-rebuild`'s business and asks in those words. Where both sides rewrote several paragraphs of
one box each is its own conflict, and taking one writes the source's line there while the others
stay the deck's; the handle is the paragraph index, the one thing the planner (which merges the
*predicted* text) and `sync.override_requests` (which re-merges what the deck holds) agree on, since
`collapse_holes` never adds or drops a newline. What was written over is kept verbatim in the
report's `resolved` section - the way back, and nowhere else will hold those words a minute later -
and a field settled this way stops counting as an override, so the report does not also promise the
deck's version was kept. It is a person's decision like `--follow-labels`: the agent tool takes
`deck_sync(take_source=[...])` and `INSTRUCTIONS.md` says an agent relays an id somebody gave it in
words and never picks one itself. The markdown report shows a conflict's three versions one under
the other with the `--take-source ID` that settles it; `fuzz_world` merges overrides with
`text_merge` and the take list now, as `override_requests` really does, so a take cannot read as a
loss to the oracle.

Frame labels (`labels.py`, `docs/labels.md`): `\begin{frame}[label=x]` is the only piece of a slide's
identity that survives compiling (beamer writes the PDF destination `x`, and `x<n>` per overlay step;
`extract.frame_labels` reads them back and `identity` keys the slide by them), so a label per frame,
never changed, is what makes sync reliable; without one a frame falls back to title, occurrence and
alignment. `python -m beamer2slides label main.tex [--apply]` writes a label into every frame that
has none, from its title, unique in the document, reading the source only (`texmap.Source`, follows
`\input`, backups like `pull --apply`). Existing labels are never touched or renamed - each one is a
promise to the deck converted from it - and duplicates are reported, never resolved: which of two
frames a slide came from is a question only the author can answer. **A label written twice never
reaches the PDF twice**: hyperref keeps the first destination of a name and drops the second, so the
second frame comes out unlabelled and nothing downstream of the PDF can tell that from a frame the
author never labelled - `beamer2slides label`, which reads the `.tex`, is the only thing that can
(stress variant `duplabel`, `test_a_label_written_twice_reaches_the_pdf_as_no_label_at_all`, which
also guards against the day a PDF writer stops dropping it). `convert --check-labels warn|error|off`
(default warn) therefore reports such a frame as unlabelled, and says so; `labels.survey`'s own
`duplicates` are only what a PDF can show (a label whose slides are not one run, or whose steps
disagree on a title), overlay steps being consecutive and alike.
`docs/ai-authoring.md` is the prompting guide for an AI that maintains the .tex: the
invariants, how to repair a source that broke them, and what to do with a sync conflict.
A label that moved to another frame (docs/sync.md, "When a label moved"): nothing in the PDF says so,
and following it writes one frame's text onto another frame's slide with the person's edits still on
it - no loss, so the loss oracle can't see it. `identity.label_moves` asks whether the two slides a
label pairs say the same thing and, if not, whether some other slide *nothing else accounts for*
explains them better: both directions -> `moved` (the content decides), one -> `unsure` (it may
equally be a passage the author moved), neither -> silence (a frame rewritten from scratch). Both
verdicts are conflicts in the report (`field: label`); a renamed or dropped label the content still
recognises is a warning. **An `unsure` slide is held back** (`merge.hold_slide`, report
`slides.held`): following the label was the safest thing a *pairing* could do, but following it is
also a write, and a write onto the wrong slide is the same mistake one step later - one frame's new
sentences merged into somebody's edits about another frame, nothing deleted, no way back but by
hand. So nothing is planned for that slide, the base keeps the entry it had (`sync.new_base` - a
base recording the source's words there would read them next time as a change already arrived, and
the held edit would be gone for good), and the next sync plans it from scratch, correctly if the
label was put back. The rest of the deck syncs as usual: one ambiguous label freezes one slide, not
the talk. `--follow-labels` (agent `follow_labels`) is a person saying they have read the `.tex` -
the house rule that `--force-rebuild` and `--force-adopted-deck` follow. A wrong *pairing* is not a
wrong *write*, and `fuzz_labels` counts the difference (`costly` -> `written`) - in **three** ways,
because a costly frame ends one of three, and only one of them puts a sentence into somebody's
edits: its slide is held back and nothing is written to it, or the pairing reads the frame as
**new** and it is created as a slide of its own (`doubled` - the deck gains a duplicate beside the
slide it belongs on, which keeps every word the person put there: a cost of another kind, and
counting it with the writes counted one event twice, the slide it should have had being either held
or taken by the frame that really did overwrite it), or it is `written` onto another frame's slide.
Over four adopt-shaped campaigns (1,000 rounds four deep, label-chance 1 and 0.5, seeds 0 and
1000000; 12,037 broken rounds, 66,397 frames) the 308 costly frames are **109 written**, 112 held and
87 doubled; on a converted talk (1,000 rounds four deep at chance 1, seed 0; 3,578 broken rounds,
16,592 frames) 51 costly are **3 written**, 28 held and 20 doubled. Before the split those read 196
and 23. Their own price is 4 `unsure` verdicts and one `moved` in 3,963 adopt-shaped sound rounds and
neither in 422 converted ones, with 3 sound-shaped frames written onto another frame's slide (all at
label-chance 0.5, all reported). Those figures were first published **one group of four**: `main()`
prints a block per group (`sound`, `sound, frame moved`, `broken`, `broken, frame moved`) and each
was read off `broken` alone, so it now prints `sound, in all` and `broken, in all` totals and a group
line cannot be quoted as the campaign's answer. The title counts by degree (`_title_alike`): a source that
retitles every frame while moving a label ("Moving labels" -> "Moving labels v2") makes a yes-or-no
"same title?" say no to every pair at once. An explanation must also beat the label's own pairing by
`LABEL_MARGIN` (0.5), which no swap between near-twins can: the pairing such a swap leaves behind is
already 0.9 alike, and that was every silent misidentification the adopt-shaped campaign had (at
label-chance 1, all of them `move_label`, 54 of 108 rounds silent). What stands in for the margin is
**word for word** exactness (`identity._complete` - a bar that depends on the pair, the title being
half of what `_evidence` says) while the label's own pairing is not exact. The two sides are not
worth the same, and only one may stand alone: the **frame's** side (`there_exact` - the source
rewrote the labelled frame into a copy of another slide) is a real edit and enough on its own for
`unsure`, which re-pairs nothing and only asks; the **slide's** side (`here_exact`) is true for free
on a deck of twins, so it counts only together with the other side, which is what a swap looks like
(`test_one_twin_edited_is_not_a_swap`, `test_the_other_side_alone_is_not_enough_when_the_deck_has_
twins`, `test_a_label_on_a_frame_that_is_word_for_word_another_slide_is_asked_about`). Measured by
`tools/fuzz_labels.py` against a tagged truth, each rule by running the campaign twice over the same
seeds with only `label_moves` swapped: over 3000 rounds, misidentified frames 14.68% -> 1.04% when
the invariant is broken, 0 false alarms in 1524 sound rounds, nothing ever worse than before (fixed
seeds in `tests/test_sync_fuzz.py`); the swap rule took an adopt-shaped deck 2.94% -> 2.18%, and the
one-sided exactness moves no frame at all but is heard - on 4000 adopt-shaped rounds at chance 1 the
same 510 frames of 22,014 (2.32%) are misidentified, while `unsure` goes 123 -> 137 and the 267
wrong rounds go 136 -> **150** told and 131 -> **117** silent; on a converted talk every figure is
identical (1.05%, 38 told, 1 silent of 1476 broken rounds).
A pairing is beyond doubt only when it is **word for word** right, not merely above `LABEL_SURE`
(1.2): a label swapped between two near-twins that share a title scores 1.25 on both sides, so the
check used to stop looking, and that was every silent misidentification left. From up there the
margin can no longer be met (`LABEL_SURE` + `LABEL_MARGIN` is above what `_evidence` can score), so
only exactness can speak - and a slide carrying this frame's **own** label is no explanation at all,
being another overlay step of this very frame (`test_overlay_steps_of_one_frame_are_never_a_move`).
That one moves frames, since what it reaches are swaps the content settles: on the same 4000 rounds
misidentified frames go 510 -> **428** (2.32% -> 1.94%), told 150 -> **167**, silent 117 -> **59**;
at chance 0.5 on that deck, 194 -> 153 frames and 45 -> **16** silent rounds; on a converted talk,
identical again. The price is one question: over 1524 adopt-shaped and 1524 converted sound rounds
there is a single `unsure` (seed 5179), no `moved`, and not one frame on the wrong slide.
Two readings that **point at each other** are an exchange, and inside one the bar is `LABEL_EXCHANGE`
(0.1) rather than the margin (`identity.exchanged`): exactness reaches a label swapped between two
labelled frames, but the commonest move is `[label=q3]` pasted onto the near-twin after it, leaving
the frame it came from unlabelled - no second pairing to see the crossing from, and, once both
frames are reworded, nothing word for word either. What decides it is that the frame explaining this
label's slide belongs on the slide explaining this frame, each looking there before anywhere else;
twins that nobody touched *tie* (gap 0) and a frame merely reworded loses to its own pairing. The
look back carries the rule, not the number - a frame reworded into its twin's phrasing clears every
bar and is refused because its two rivals are about each other
(`test_a_frame_reworded_on_a_deck_of_twins_is_no_exchange`; `moved` re-pairs, so a wrong one is
edits on the wrong slide). Measured where it belongs, on a source revised again and again: 700
four-deep chains (7766 frames) go 162 -> **146** misidentified (2.09% -> 1.88%) and 23 -> **15**
silent rounds, sound rounds identical to the last verdict; unchained, 288 -> 282 of 13,860.
What the order-keeping alignment leaves over is picked up twice more (`tests/test_frame_moves.py`):
by content, when one leftover frame explains one leftover slide and no other comes close
(`identity.cross_pairs`, `CROSS_SURE`/`CROSS_MARGIN`) - that is a frame the source moved across
another - and then by place (`identity.gap_pairs`, `GAP_SURE`): one slide and one frame alone
between two neighbours that paired, the ends of the talk counting as neighbours, sharing some of
their words - an unlabelled frame the source retitled. The two run one after the other, the second
seeing only what the first left, and neither takes a slide already paired: one slide belongs to one
frame. (Handed the same leftovers, both claimed the same slide when the source swapped two frames,
and two frames came out carrying one key - sync then wrote both onto that slide, a picture was gone
and the report said nothing was created. Offline fuzz seed 5521.) Sound rounds went 0.06% -> 0.00%
with them: with the labels kept, no frame of 6809 ends up on another frame's slide. What the passes still
refuse (a frame retitled, reworded *and* moved, with no label) is now at least reported:
`identity.near_misses` (`NEAR_TELL`) names the dropped slide and the new frame that say much of the
same thing, and a frame paired by place alone gets a warning asking for a label (`weak_pairs`).
`tools/fuzz_labels.py --chain N` measures both over revisions of already-revised sources.
`fuzz_sync` moves/renames/drops labels too; that found `sync.new_base` freezing a `gone` slide's
entry at the moment of deletion (its label/title/words now follow the source, its key and place stay)
and a renamed label costing a slide its identity (`align_slides.pairable` lets the content fix it
when neither side knows the other's label), and a slide the source dropped but the deck's edits kept
alive still claiming its label in the base, so the frame that carries that label now paired with the
dead entry (`sync.new_base` clears it: a label belongs to the source). Clearing the label was half
of it: such an entry still says what the frame said, and `sync.base_order` puts it back beside the
slide the frame really lives on, so for the frame carrying those words the two entries read *exactly*
alike and the order-keeping walk, handed two readings of one score, takes the earlier - the frame
flipped onto the dead entry and the next sync planned to **delete** the slide this one had just
written, with the person's edits on it (converted 2100403 and 2100135 at chain 6; nothing was lost
in the sync that caused it, so only the `_settled` check saw it). The entry says the source dropped
it (`removed`) and between two readings that tie `align_slides` takes the slide the source still
describes (`DROPPED_FRAME`, a hair: a frame the author puts back still re-pairs with its kept slide
when nothing else explains it, and a real difference in the words decides as it did).

A pairing that was a coin toss says so (`identity.align_slides`, `weak_pairs` value `twins`): where
an unlabelled frame sits among slides that say nearly the same thing, a second alignment of the same
score is a second reading, not a worse one, and the frame goes wherever the walk reaches first -
which nothing downstream can tell from a match the words made. The frame is still paired (nobody's
edits move) and the report names the slide and asks for a label. Measured with `fuzz_labels --shape
adopt`, 2000 rounds chained 4 deep: with the labels kept, 2 frames of 20,080 are misidentified and
both are now named in the report, none in silence; 1 pairing in 20,080 is called a coin toss, so it
is not noise. The campaign counts what a person is told, not what `label_moves` said alone - a
changed label is a warning of its own in `merge.plan_merge`.
**And the same question asked of the *pair* rather than of the alignment**: is there another
leftover slide whose words read as well against this frame as the one it got? An order-keeping walk
cannot offer the crossing where the source carried a frame past its near-twin - that reading is
unrealisable, so it scores worse and `optimal` is silent - while a person looking at the two slides
sees exactly the coin toss the other half names, which is why the gate asks two questions and not
one (`test_twins_with_a_frame_between_them_are_a_coin_toss_the_walk_can_never_offer`: identical
twins with a frame between them, said of both and neither moved, silent the moment each carries a
label). Taken as the **union**, since a rival on some best alignment need not tie pairwise. Measured
over the four adopt-shaped campaigns above, run twice over the same seeds with only this swapped: of
the 109 frames written onto another frame's slide, those written with nothing in the report naming
*them* go **21 -> 14, in 17 -> 12 rounds**; not one frame moves (109 `written` and 3 either way, 308
costly, no new `moved` or `unsure`), and the price is 7 more warnings in 3,963 sound rounds (50 ->
57; broken rounds 402 -> 587). The label condition is factored over both readings rather than
dropped: `merge.plan_merge` prints this warning only `and not o.get("label")` and its sentence opens
"this frame has no label", so a `twins` on a frame whose label the source renamed - which is in the
leftovers carrying one - would be counted as spoken for and said to nobody. Its labelled counterpart is
`weak_pairs` value `crossed` (`identity.crossed_twins`): two labels that changed places over slides
the crossing **loses nothing** on - each frame reads at least as well against the other's slide as
against its own, within `TWIN_TIE` - so the words did not decide this and only the **order** says
anything, either two frames the author moved or `[label=one]` pasted onto the frame below, and
nothing downstream will tell those apart. The labels are followed and both slides are named. Asking
that the two readings *tie* was too narrow by half: a crossing between near-twins the same revision
has also reworded or retitled reads 0.03 to 0.42 apart, the dead band between a tie and
`LABEL_MARGIN` (0.5) - too much to be one score, far too little to be an explanation, and so a place
where nothing at all spoke. That the words might decide it the *other* way is `label_moves`' business
and refuses the crossing as it always did. Measured over the same four campaigns run twice over the
same seeds with only that swapped: frames written onto another frame's slide with nothing in the
report naming *them* **14 -> 7, in 12 -> 7 rounds**, at the price of 22 warnings in 12,037 broken
rounds (587 -> 609) and **none at all** in the 3,963 sound ones (57 either way), with not one frame
re-paired (109 `written`, 308 `costly`, 3 converted writes) and no new `moved` or `unsure` anywhere.
And what counts as an explanation at all is a share of what the pair *can* say, not a number
(`identity._moved_bar`): `_evidence` reaches 1.5 where the titles match and 1.0 where the slides
never had one, so a flat `LABEL_MOVED` of 1.0 asked for word for word on a deck `adopt` wrote -
where a label is the only identity there is - and a swap between two untitled frames each reworded
by a word scored 0.95 against 0.42 and was followed in silence. Measured over the same seeds with
only that swapped (1400 rounds four deep): adopt-shaped, misidentified frames 311 -> **109** of
14,304, written onto the wrong slide 63 -> **40** (0.44% -> 0.28%); converted, 115 -> **43** of
11,031 and written 35 -> **17**, with `unsure` 62 -> 26 (a question the content can now answer
becomes an answer). With the crossing named too, the adopt-shaped rounds that get a frame onto the
wrong slide go from 137 told / 27 silent to **61 told / 2 silent**, and those last two were the
campaign's own: `src_add_slide` reissued a deleted slide's label (following a label onto the frame
that carries it now is what a label *means*), and `near_misses`, which is in the report, was not
counted as telling anybody. Corrected, **no adopt-shaped round of 2,607 gets a frame onto the wrong
slide without the report naming something**; the price is 34 warnings in the 200 sound rounds that
really moved a frame, each about two slides a person cannot tell apart either. That is a claim about
rounds. Asked of the **frames** - a frame written onto another frame's slide with nothing in the
report naming *it*, which is what a person actually misses - the four adopt-shaped campaigns above
have **7 of the 109 writes in silence, in 7 rounds** (converted, 1 of 3; 21 in 17 before the
look-alike half of the `twins` gate above, 14 in 12 before the crossing asked that it lose nothing
rather than tie), so the round-level sentence must not be read at frame size. What is left is out of
every pairing rule's reach: **5 are frames a label paired, 2 the alignment did**. Three of the five
are a label moved between near-twin frames whose best rival scores 0.14 and below - nothing to
compare them with, and no rule can speak where the deck says nothing; one is a pairing a free rival
beats by 0.13, inside the dead band under `LABEL_MARGIN`; and one is word for word exact beside an
equally exact slide **nothing has claimed**, which `_complete` is right to let pass, a pairing past
doubting being past doubting. Of the two the alignment paired, one is an untitled frame among slides
that say nearly the same thing (in a round that did say `unsure`, about another frame) and one
carries a label the deck has never seen - a `rename_label`, which is the one of the seven with a
report sentence of its own still to write.
**And so is the margin** (`identity._scaled`), which is a *difference* between two readings - and
the two are readings of different pairs, `here` scoring the label's slide against another frame and
`own` against the frame carrying the label. Where those pairs can say different amounts the
difference is measured in two units at once: a rival that says word for word what an untitled slide
said reaches 1.0 and no further, so beating a titled pairing of 0.8 by `LABEL_MARGIN` is
arithmetically out of reach whatever it says - and a deck `adopt` wrote is made of such pairs. Both
readings go onto the scale the constants are written for before they are subtracted, in the two
margins and the `LABEL_EXCHANGE` gate. Measured over four adopt-shaped campaigns run twice over the
same seeds with only that swapped (1000 rounds four deep, label-chance 1 and 0.5, two seeds each,
61,344 frames): written onto the wrong slide 174 -> **154**, misidentified 421 -> **391**, `unsure`
115 -> **94**, and in 3,996 sound rounds the questions 5 -> **1** with no new `moved` anywhere; a
converted talk writes the same 26 frames either way. The price is the same coin as `_moved_bar`'s,
and visible: one campaign's writes went 16 -> **18** and another's silent rounds 3 -> **4**, a bar a
pair can actually reach being reachable by a wrong reading too
(`test_a_margin_is_a_share_of_what_the_pair_can_say_too`).
**And inside an exchange the bar is a tie, not a number of its own** (`LABEL_EXCHANGE` 0.1 ->
**0.02** = `TWIN_TIE`): twins nobody touched *tie* and a frame merely reworded loses to its own
pairing, so a tie is the whole of what that number has to say, and the campaign's silent
misidentifications at 0.1 were pairs where the crossing read 1.447 against the label's own 1.419 - a
swap between two frames one revision has reworded is a hair either way, and what tells it from
innocent twins is not 0.03 of evidence but the look back (`identity.exchanged`,
`test_the_exchange_bar_is_a_tie_and_not_a_number_of_its_own`). The look back counts ties itself now
(`among_best`): `best` keeps the first of several equal scores, which is the lower index and nothing
else, so where the source leaves a copy of a frame beside the one it labelled the slide they both
explain read them at one score and the arbitrary first of the two refused the exchange - a label
followed onto the wrong slide on the strength of a slide order
(`test_a_tie_in_the_look_back_does_not_refuse_an_exchange`). And the crossing is asked of the
**finished pairing** rather than of the labels alone, because the commoner half of it is `[label=q3]`
pasted onto the near-twin, which carries no label to see the crossing from: `crossed_twins` runs
after the content and place passes, wants one of the two frames labelled and both own pairings clear
of `_moved_bar`, and says `crossed` where both carry a label and **`traded`** where one does - the
frame that crossed being paired by the alignment like any other. Nothing named those before: the
words say nothing either way, and `twins` never fires because the label has already taken the
alternative slide away from the walk. Measured over the same four adopt-shaped campaigns (1,000
rounds four deep, label-chance 1 and 0.5, two seeds each, 61,344 frames): written onto the wrong
slide 154 -> **139** (ties in the look back) -> **111** (the bar; per seed 47->36, 18->15, 52->42,
22->18), misidentified 391 -> **336**, silent wrong rounds 11 -> **6**; a converted talk writes
26 -> 25 with its 7 warnings unchanged, and no sound round on either shape gains a verdict.

Slide order is merged, not all-or-nothing (`merge.plan_order`): the source's order is the ground and
a slide the deck itself picked up (out of its base order there, `_out_of_place`) goes back beside
what it follows in the deck; both sides moving the same slide is the deck's, with a warning. One
slide dragged in Slides used to freeze the source's order for good.

Alignment on Google's renderer (`tools/alignment.py out/<deck>`, after `fidelity`): reads the
Slides element boxes with `presentations.get` (cached in slides_elements.json) and compares each
thumbnail with the PDF rendered on the same pixel grid, in PDF pt. Writes alignment.json, a table,
and evidence crops `alignment/NNN-item.png` (PDF above, Slides below, measured boxes and edges in
red; `--crops all|failures|none`). It measures:
- hole pictures (anchored math/icon): ink gaps left and right of the picture on its line, Slides
  minus PDF (≤ 1.5 pt), and overlap (text ink touching the picture ink or cut at its opaque box);
- numbers on balls: number-coloured ink centroid minus the ball box centre, Slides minus PDF (≤ 1 pt);
- bullets: ink box and colour in the bullet's cell, ΔE (≤ 15) and size ratio (within 30%; heights
  only for ➢, which stands in for ▶); a translucent highlight over a bullet is unblended first;
- overlay marks: word edge minus where the (stretched) picture meets it, Slides minus PDF (≤ 2 pt).
Picture ink is the picture file itself (alpha, or pixels unlike the page around the box) placed
into its box, so text ink is the rest; the page colour comes from a ring around the picture.

Pull back to the source (`docs/sync.md`, "Pull"): `python -m beamer2slides pull --deck <url|id|out>
--tex main.tex [--apply]` reads the deck only (`deck_ir.py`: live deck → deck.json-shaped IR through
emit's text box model and FontMapper) and loops compile → classify → `compare.py` residuals →
translators (`inverse.py`; `texmap.py` maps pages to frames by SyncTeX and words to source spans by
printed text) until the source's conversion matches; `edits.md`/`edits.json`/`pull.patch` in
`<out>/pull` hand unresolved residuals (with frame file:lines) to an AI. `converge --target deck.json`
is the offline twin. Pictures come back from `contentUrl` (the stored file byte for byte, capped by
Google at ~2046 px on the long side; crop, rotation, transparency and outline stay properties,
brightness/contrast/recolour are baked — `tools/probe_images.py`) and are written as LaTeX options:
`trim=…,clip`, `angle=`, `\reflectbox`, a tikz node with `text opacity`/`draw`, a transparency group
for a tikzpicture, colour edits baked into the file; names `figures/<slug>-<sha8>.<ext>`, reusing an
identical or same-looking file already in the tree. A deck picture over a figure the source draws
replaces it, with the environment commented out under `% b2s pull: replaced by <file>`; formula/icon
pictures in text lines are only reported. Tests: `tests/test_inverse.py` and `tests/test_pull_images.py`
(offline: texmap, compare, planned edits on `tests/decks/inverse/a.tex` vs `a.deck.json`, deck_ir via
`tests/slides_sim.py`, picture options, naming, figure replacement); opt-in `python -m pytest -m inverse`
(compile loop on the `b_*.tex` pairs, synthetic edits and picture edits, iterations in
`tests/decks/inverse/out/results.json`).

**Editing a deck as beamer with no work folder** is explored in `docs/direct-editing.md` and
**not built**: a design, its measurements and the decisions it would need, nothing more. What is
measured there and is load-bearing for anything else: `adopt.bootstrap` needs **no LaTeX** (0.1-1.7 s
over five corpus decks, `tools/probe_deck_view.py`), so a deck can be read as beamer for the price of
writing text; a median adopted frame is **749 bytes** and carries the slide's objectId as its
`label=`, which is the identity `sync` follows; and the **text of a deck is ~1% of its tree by
weight** (10-27 kB gzipped against 6-52 MB of pictures and fonts), because a figure is named by the
sha1 of the bytes the deck handed back and fonts already live in `fontfetch.cache_dir()`. So what
would have to travel between two calls is the `.tex` texts, whatever binary an edit itself brings in,
and the base - and `texmap.Source.texts` is already a `dict[Path, str]` of the first. What cannot be
wished away is the compile: the Slides IR comes from classifying a rendered PDF, where the Docs IR
comes from parsing the file.

Adopt a deck nobody converted (`adopt.py`, docs/sync.md "Adopt"): `python -m beamer2slides adopt
--deck <url|id|deck.json> --tex main.tex` writes the source `pull` never had, then converges it. Pull
refines a source and a **foreign** deck - one a person built in Slides - has none; the loop cannot make
one either (a document with no frames compiles to a PDF with no pages, and one empty frame per slide
makes `slide_missing` oscillate). `deck_ir(foreign=True)` reads such a deck differently: the slide is
given what its layout and master draw (`inherited_chain` - that is where a deck a person built keeps
its look, while a converted deck's theme is drawn by its .tex, which is why `pull` must not see them),
layout placeholders are skipped and inherited pictures forced to role `figure`; groups are not folded
and lines are kept (`line_element`: Slides stores a line as the unit segment under the element
transform, so only the endpoints say which way it points), because folding recognises *this*
converter's conventions and would throw away the outlines and edges adopt must draw; and a font is
known by its name (`family_of`), not only when the converter itself wrote it. The skeleton is a theme
that draws nothing plus one `[plain]` frame per slide with a `textblock*` per element (tikz for
shapes and connectors, pictures copied into `figures/`, a per-slide background colour under
transparent decoration, each box carrying its own base style since `inverse.runs_latex` writes only
what differs from one). Absolute-first is deliberate: a foreign deck's geometry is boxes a person
dragged, and a wrong guess at flow costs the loop a `geometry` round; `--flow` asks for the readable
version. Scored by ink overlap with the deck's own slide images inside its element boxes (residual
counts misrepresent fidelity: ten pixel-perfect squares read back as one `diagram` = 10
`element_missing`): the DevFest 2020 template's 39 slides reach 0.70 from the bootstrap alone, 0.22
when only the slide's own elements were read. Tests: `tests/test_adopt.py` (offline).
**The source is meant to be kept, not only compiled** (`devtools/readability.py`, `readability report
--tag T`: lines, numbers, plumbing, bloat, author vocabulary and repetition per frame body, against the
medians of sources people wrote; `docs/adopt-bench.md` holds the history). What it writes now says the
deck's look once and its slides in a vocabulary: a `slides.sty` beside main.tex (`slidebox`,
`\slidepar`, `\slidetext`, `\slidepicture`, `\sliderect`/`\slideline`/`\slideshape`, `slidetable`,
named `\slidestyle` styles and colour names), the deck's masters and layouts recovered as a
`beamertheme<Deck>.sty` (`adopt_theme.py`: a background template per layout, frames naming theirs and
carrying a real `\frametitle`), and Slides lists as `itemize`/`enumerate` with each level set once
(`\setslidelist`), every frame carrying `label=` (a slug of the slide's objectId: the one identity
sync can follow, and adopt-shaped fuzz says nothing else recovers these decks) and every font face
named, so a style the deck calls bold comes out bold (`devtools/bold_torture.py` probes what a deck
draws: 63 of 267 (family, style) pairs drew no emphasis, now 6, none of them asked for by deck text).
A look the deck draws three times or more is **said once and named after what it is**
(`adopt_shapes.survey_styles`: the preamble says
`\slideshapestyle{fill-white-outline-darkgrey}{fill=white,draw=DarkGrey,line width=0.47bp}`, the
frame says `\sliderect[fill-white-outline-darkgrey]{...}` - 192 names over 27 decks, 3,534 uses),
and an outline of more than 12 numbers keeps its points in a file of its own
(`\slidepath[...]{shapes/star5-9b3c2dfe.tex}` nested inside `\slideshape`, 85 files for 132 lines).
Names are `\tikzset` styles, not macros, so `\sliderect[card,fill=Red]{...}` changes one shape and
nothing else. Measured over the corpus: 0.128 -> **0.4477** on the scorer as it stands (it was
tightened on the way: repetition inside a frame is charged and the recovered theme counted with the
frames), with every slide's fidelity unchanged to four decimals (`fl-a` -> `sh-a`, 912 slides, no
deck moving by 0.0001); sources people wrote score 0.6-1.0, and shape coordinates are still what is
left - 28% of frame body lines, now 26.3k numbers rather than 30.6k.
**Does an edited source still stand up?** (`devtools/edit_robustness.py`, `sample`/`run`/`report`):
the same nine edits - reword longer/shorter, restyle, retitle, move a box, add or delete an item, a
paragraph, a table row - applied to the same slide in two forms, compiled, and judged by whether the
page changed, the words landed, and nothing spilled out of the room the edit is allowed. Both forms
hold 123 of 143; the edit costs 2.1 source lines in the absolute form and 1.2 in this one (a table
row 19.8 -> 1.0). The 20 that fail are reword-longer with no slack in the box, which is what a
foreign deck's absolute geometry is.
Linked charts are their `contentUrl` picture, videos their poster frame (YouTube `hqdefault`,
letterbox cut; Drive: a play panel) inside an `\href`, WordArt its `renderedText` in a `\resizebox*`;
a page beamer has no ratio for is written with `\geometry{papersize}`; fonts the machine lacks are
fetched from google/fonts into a user cache and variable ones cut into static instances
(`fontfetch.py`, `$B2S_FONT_FETCH=0` / `$B2S_FONTS` turn it off). Every one of those reads font
files with fontTools, which is why it is a plain dependency and no longer the `[pure]` extra alone:
the playground's image had none, so `deck_adopt` read the deck, fetched its fonts, wrote figures,
shapes and fonts and then died on `import fontTools` with main.tex unwritten - minutes of Google
thumbnails for nothing. Where it is missing anyway, what goes is the fallback chain and not the
tree (`scripts.faces` says so once). A `tex` that exists but is **empty** is a name and not a
source (`adopt.written_already`): making the file first is what a person does when a form asks
them to name one, and in the workbench's editor it is the only way to name one at all.
Tests: `tests/test_adopt_media.py`.
Text boxes follow Slides' text model line by line (`adopt.text_box_latex`: `\vbox to` the box height
for TOP/MIDDLE/BOTTOM, Slides' pitch per paragraph through `\prevdepth`, spaceAbove/Below with
COLLAPSE_LISTS, indents, ● ○ ■ drawn at their measured size, no hyphenation or space shrink, each
font's own interword space (a selectfont hook: `\spaceskip` set once kept the sans font's space in a
`\ttfamily` paragraph); autofit and per-list-level styles read in `deck_ir.text_paragraphs`; empty
lines ending a middle/bottom-aligned box kept, since they move the stack; a SHAPE_AUTOFIT box whose
height leaves < 5 pt beside one line per paragraph has no insets (`deck_ir.zero_insets`: the API
never reports insets; PowerPoint/Canva templates set them to 0), and so has every box a .pptx import
made (NEVER_COLLAPSE) that resizes to fit its text, when enough of the deck's such boxes prove it
(`deck_ir.imports_lack_insets`: gdg24's import kept Slides' insets); a tab jumps to the next 36 pt
stop from the text's edge (`adopt.tabbed_tex`, `\slidestab`); `tests/test_adopt_text.py`). Tables are a
tikz grid with measured rows, merged cells, fills and border segments (`adopt.table_block`, cell
insets inferred by `deck_ir.cell_pad`; `tests/test_adopt_tables.py`). Shapes are drawn in their preset
(`adopt_shapes.py`: ~110 shapeTypes with OOXML default adjustments, turned/mirrored through the
element's own `frame`, dashes, alpha, bent and curved connectors, arrow heads; freeforms traced from
the thumbnail, else as their box; `tests/test_adopt_shapes.py`). Scripts (`scripts.py`): luaotfload fallbacks for CJK and symbols,
babel `onchar=ids` for CJK line breaking and Hebrew/Arabic fonts, `bidi=basic` with RTL paragraphs
in `otherlanguage` (`tests/test_adopt_scripts.py`).
**Benchmark** (`devtools/adopt_bench.py`, 29 public decks in `tests/decks/foreign/corpus.json`,
cached in `out/adopt-corpus` or `$B2S_ADOPT_CORPUS`; **history, per-change numbers and what was tried:
`docs/adopt-bench.md`** - new results go there, not here): `capture` reads a deck and its LARGE
thumbnails (read-only), `run [decks] --jobs N --tag T` bootstraps, compiles and scores every slide
(`boxes` / `page` / `pixels`, sheets in `<corpus>/<deck>/runs/<tag>/sheets`), cached per deck by
source tree, IR and scorer (`--no-cache`); `report --tag T`; `losses --tag T` charges each lost pixel
to the smallest element box holding it and ranks elements, decks, kinds and fonts. Fills, pictures,
freeforms, pies and video posters the API does not give are read off the slide thumbnails
(`deck_fills.py`, `deck_freeforms.py`, `deck_thumbs.py`: rows, cell and text insets, stand-in widths, weights). Now (`fl-a`, 912 slides): boxes 0.973, page 0.971, pixels 0.984, mean per deck 0.956.

**Back into the deck you adopted** (`adopt_sync.py`, docs/sync.md "Adopt"): adopt used to write a
source tree and nothing else, so the only way on was `convert`, which makes a *second* deck and
leaves the person's - comments, sharing, history - behind (`sync` said "no sync base for
presentation <pid>", or "<folder>: no emit.json or sync/base.json in this folder"). `adopt` now
records a base, so the .tex it wrote can be edited and synced into the very deck it came from.
The base lives in the **work folder**, not in Drive (`--base-in-drive` asks for the other half,
`--no-base` for none): `snapshot.save_drive` writes the presentation's `appProperties`, and adopt is
the one command routinely pointed at a deck the person may only read - the corpus decks are exactly
that. So one syncs with `--deck <the adopt work folder>`, which sync's no-base message now says.
Its IR side is a fresh conversion of the source on disk (`convert_source` mirrors `sync.build_ours`),
its deck side the person's own objectIds; no alt text is written anywhere. Identity is `kind × words
× place` (`pair_elements`: mutual best, `PAIR_SURE` 0.45, and `PAIR_MARGIN` 0.08 clear of the runner-up
**on both sides**, so two identical boxes side by side pair with neither), slides pairing exactly
because `adopt.frame_labels` labels every frame from the slide's objectId (`labels_match` refuses if
that chain does not come back). Measured over the corpus: 10-80% of elements tied to an object
(gdg24 427/563, hebrew-lesson 25/244), the spread being how far `classify` regroups the person's boxes
into one element. What no element draws is `left_alone` and never touched; the master and the layouts
are never touched at all (`master_background = None`: the deck's look is the person's).
**Not every miss is a miss**: a converted element with no object of the slide behind it may be one
the deck's *layout or master* draws, which `adopt` recovered as the source's theme and the source
therefore draws again on every slide that inherits it - the object exists, one level up, where
nothing here may write. `explained_by_layout` asks that of what `pair_elements` left over (the
slide's own object wins first) and without the margin, which decides *which* object to write to and
has nothing to do when the answer is none; `same_drawing` demands the same size, since
`identity._geometry` also scores by how close two centres are and a full-bleed layout picture shares
its centre with everything a person put in the middle of the slide (sc-dark-modern's 35 pt icon
scored 0.45 against it) - except for words, which the converter reads back at their ink, so the same
words inside the template's own box are the same drawing however much room they have (firebase-jam's
`Thank you!`: a 296 pt placeholder, 109 pt of ink). Measured over the 12 corpus decks whose IR
carries anything inherited: **181 of 1,533 misses**, the size rule taking 28 false positives off
that (solidity-survey 74 of 107, hebrew-lesson 39 of 213, cs161-net 31, instagram 3 of 3). Counted
apart in the base (`adopt.from_layout`, the element carrying `from_layout` for the merge to read)
and reported by `merge.plan_unit` as `field: inherited`, "kept (the deck's layout draws this, not
the slide)". Same decision, different sentence, and the sentence is the point: "change them in the
deck itself" sends a person to look on the slide for a footer that is not on the slide, while the
layout warning names the door it is behind (Slide > Edit theme).
**And one box the converter read back as several.** The other half of the misses is the same
mistake the other way up: `adopt` writes one `slidebox` per object and the converter reads the
compiled page with no idea it was ever one box, so where the person's paragraphs stand more than a
line and a half apart (a heading over its body, an agenda with air between its items) `classify`
calls them separate elements - and each pairs with nothing, because the thing each is part of is
the whole box. The conversion is folded back against the deck's own boxes before anything is paired
(`adopt_sync.fold_composites` / `fold_slides`): a fold is a **concatenation and nothing more** -
paragraphs in reading order, boxes unioned, spans and strokes joined, an anchored picture
re-pointed - which is why it is safe to write back, `emit.vertical_layout` reading each paragraph's
`spaceAbove` off the baselines the page really has, so the gaps that made `classify` split the box
come back out of the geometry when it is written again. What is folded is the box's **words**: a
member that is not text does not refuse the box, it is simply not part of the fold and stays where
it is (the rule under a heading, the picture of a formula in its prose, an icon beside its caption -
drawings the converter made *of that box*). It is a claim about somebody's slide, so it is made
only where the words say so: of the corpus's 310 objects holding two or more converted texts, 125
are folded (534 elements) and four rules refuse the other 185 - the object must be text (91 are
not: 39 pictures, 33 tables, 19 shapes; a fold writes a text box, and over a person's filled shape
it would lose the fill), none may already say what the object says **on its own** (52: that one *is*
the box), together they must say it (42 do not), and the object must say something at all
(`SequenceMatcher` scores two empty strings 1.00, `same_drawing`'s own degenerate match) - and
nothing is folded where two objects claim one element, which box it belongs to being exactly what
is not known. Measured over the corpus: **455 of the 1,533 misses** (30%), hardest on the decks
this was worst at (146 of intro-lecture's 170, 114 of creandum-board's 135, 38 of gdg24's 136), every
folded element then pairing, no deck's paired count falling (folding takes rivals away too) and
`from layout` unmoved at 181. Measured against **rebuilt** targets: a corpus deck's `target.json`
is what `capture` wrote once and `adopt_bench.build_target` makes it again every run, which matters
because `deck_ir.page_size_for`'s `MAX_BEAMER_SCALE` guard gives an extreme-scale deck `w/2, h/2`
and the cached read of the 3456 pt poster was 4.8x off - against the cache the same fold reads 394
of 1,844 (21%), a number about a deck side no current code produces.
Seven decks were run end to end offline as well (a real `record()` off the corpus cache, one
compile, no Google): creandum-board 135 -> 21 misses, intro-lecture 170 -> 24, ds-lecture 15 -> 1,
**58 folds of 58 tied to one of the person's objects**, `from_layout` unmoved on every deck (a
fold never swallows what the theme draws), and all 58 coming out of `emit.plan_offline` - which is
what gives `sync` its `DeckPlan.slide_parts` - as a text box at the fold's box saying what the fold
joined. The base records the **boxes**, not the folds (`adopt.boxes`, per frame label: the deck's
geometry is what does not change), `sync.build_ours` reads them off the base and folds its own
conversion the same way before anything is keyed, and a base with no `boxes` folds nothing. Keyed
by **label** because folding comes before the slides are paired and pairing reads the elements
folding changes (`identity.slide_info`). The offline campaign is blind to it by construction
(`fuzz_world.build_adopt_base` never runs `convert_source`) and says so: it proves the fold
regresses nothing and nothing about the fold.
**And a table of the person's own, read back as a scatter.** Of the 1,078 misses the fold leaves,
**315 stand inside one of the deck's tables** - the biggest thing left, and the one where naming it
is the whole answer. `adopt` writes a table as a tikz grid and the converter reads the page back as
it reads any page: of the corpus's 42 deck tables 9 pair, 1 comes back as a `table` element and 32
as loose cell texts plus the thin rule pictures between them. Putting the words back into their
cells was tried and refused (`tools/probe_deck_tables.py`, which also measures the read-back): build
the grid from the texts' geometry and prove it cell for cell against the `rows` the deck itself
reports, and **none of the 42 rebuilds correctly** - merged cells, empty cells, a wrapped cell, a
number right-aligned under a left-aligned heading, each shifting a column or a row by one, and a
grid wrong by one writes one cell's words into the cell beside it, which is the silent damage this
design exists to prevent. So it is named: `adopt_sync.inside_tables` asks it of what is left over,
the base marks the element `in_table`, and `merge.plan_unit` keeps the unit with `field: in_table`,
"kept (a cell of a table of the deck's)", plus a warning naming the door (edit those cells in
Slides). Three answers to "no object", each with its own field and sentence: `inherited` (the
layout draws it), `in_table` (a cell of your own table) and plain `unpaired` (nothing stands there).
**And a drawing the converter made out of somebody's own box, which is none of the three.** An
element tied to nothing freezes its whole *unit*, and a unit is an anchor text plus what is anchored
to it - so a person's text box that comes back as its words *and* a picture of the icon at the head
of a line (or of the formula in the middle of one) was frozen for good: the words pair, the picture
pairs with nothing, and nothing on the slide is shaped like the picture alone because it was never an
object. The box was, and the box is named by the element beside it in this very unit - so the one
delete that member carries takes the person's box away and the unit is written whole, with nothing
left standing. `adopt_sync.drawn_from` says which object each miss came out of, `merge.covered` asks
whether that object belongs to a member of the *same* unit (outside it, the object would stay and the
new picture would be the duplicate this gate is about), and `merge.blind_members` is what `plan_unit`
and the `unpaired` refusal both read, so the two cannot drift. Over the corpus, 27 decks and 4,171
units: **1,184 units frozen by a member tied to nothing, 40 of them no longer** (23 formula pictures,
20 icons, 16 figures). Small on purpose - each is a box the source could never have said another word
in for the life of that deck. The campaign draws it (`make_adopt_doc`'s anchored icon).
**And the boxes the rest of them stand over are on the slide now.** `fuzz_world.build_adopt_base`
used to take an unpaired element's object off the page along with the pairing, so the campaign's
adopted deck was one where the person's own unpaired content - 10-80% of a real one - did not exist:
`merge.user_objects` found none of it, the loss oracle guarded none of it, and `sync.would_hide`, the
rule that nothing this converter writes ends up over words only the deck has, was unmeasured on the
one shape of deck where such words are most of the slide. They stand now (`left_readback`,
`left_object`, `adopt.left_alone` as a real base records it), and three things follow. The oracle
guards them like any other object of the person's. `fuzz_sync._doubled` is an **observation** rather
than `merge.blind_members` restated - the box was still there when the sync finished and beside it
stands an object this sync created for that very element - and with `blind_members` answering
"nothing is blind", 100 of 200 chained first-sync rounds fail on it. And with `_not_over_kept` doing
nothing, 2 of 120 rounds at chain 4 hide words a person could read, where the same 120 rounds in the
old world found none at all. One sentence changed with them: `merge.slide_touched`'s "objects added"
counted every object the converter did not make, which in an adopted deck is the deck itself, so a
slide no frame accounts for was kept and the person told they had added objects to a slide nobody had
touched - and "the deck's own", the sentence that names the door, was never reached. The base records
those objects per slide (`adopt_sync.build_base`, carried by `sync.new_base`: they are never adopted,
so a base that forgot them would call them added at generation 2) and `slide_touched` leaves them
out; what a person really added is still an edit. **Every** object standing on the slide, besides,
and not only the ones the read made an element of - the two commonest were exactly the ones it did
not. A person's own **group** is no drawing at all (`deck_ir(foreign=True)` reads its children and
never folds it, so nothing in the IR carries the group's own id) and a shape that draws nothing - an
empty placeholder, fill and outline switched off, a fill at alpha 0 - is read as no element either
(`deck_ir.foreign_shape` returns None), so neither could ever be named by an element's `objects` and
both read as objects the person had just added: 2,866 groups and 3,845 blank shapes over the corpus,
on **214 of its 912 slides**, every one of them a slide nobody had touched since adopt read it.
`build_base` records what the pairing did not tie itself to, which is the question as it should
always have been asked.
Then `Sync.check_plan` runs between planning and any write and **refuses** four things
(`adopt_sync.problems` / `refusal_message`, every message asserted verbatim in `tests/test_adopt_sync.py`):
a unit whose base members include an **unpaired** element (writing it puts a second object beside the
person's - now decided per unit by the merge, see below), a deck **page of another shape** than the
paper the source compiles to, no **way back** on
the first sync, and **slides the
plan would delete** on the first sync (on a first sync that is a label that moved far more often than a
slide the author meant to drop). Each names what it found and offers `--dry-run` (which never refuses:
that is how one sees what it wanted to do), a backup, the labels, editing that element in the deck
instead, `convert`, or `--force-adopted-deck`. The gate is wider than `merge.has_writes`: a unit merely
reworded counts, because that is still the first time this converter puts anything into a person's deck.
The first two outlive the first sync - found by the campaign (`fuzz_world.build_adopt_base`,
`fuzz_sync offline --first-sync`, new finding `adopt_double`): gated on generation 0, 16 of 200 chained
rounds duplicated a person's box on the *second* sync, because a rebased base still carries unpaired
elements. Clean at chain 1x400, 4x200 and 5x250; taking the `slides-deleted` refusal out fails 0 rounds,
so that one is a judgement, not a measurement, and says so.
**One element nothing can be written to freezes that element, not the talk.**
(The same for one slide: see below.) The unpaired refusal was
the right decision at the wrong size: a deck a person built has unpaired elements by construction
(10-80% of them), so a *whole sync* stopping at the first one meant that on 400 first-sync campaign
rounds, 734 of ~2,400 syncs wrote nothing at all. `merge.plan_unit` now makes the decision itself, one
unit at a time (`adopted`, `merge.ADOPTED`, which `adopt_sync.ORIGIN` is): a unit whose base members
include an element tied to no object is **kept as the deck has it**, with a conflict
(`field: unpaired`, not takeable - taking the source's side is the write that duplicates) and one
warning per sync saying the thing to do about it, while the rest of the deck syncs as usual. That is
`merge.hold_slide`'s rule one dimension down, and the same answer the refusal's own advice gave
("change those elements in the deck instead of in the source, and sync the rest") - now given by
doing it. `adopt_sync.problems` keeps the check as the last gate between a plan and a write, for a
plan that says recreate anyway however it came to; `fuzz_sync._doubled` still watches the result, and
the probe that proves it opens both doors (`merge.ADOPTED` made a word no base says, and the gate's
`unpaired` reason removed). The base needs nothing new: a kept unit records the base's own members,
whose `objects` is empty, so the evidence for the decision is the decision (unlike the `removed` one,
which had to be written down). Measured by repeating the campaign that found it: `unpaired` refusals
734 -> 0, 400/400 first-sync rounds at chain 6 clean.
**And one slide no frame accounts for.** The other whole-sync refusal is the same shape and now the
same answer: `merge.plan_merge` keeps such a slide (`keep_removed`, `reason: ["the deck's own"]`, a
warning per sync) instead of deleting it. Nothing here made those slides, and a frame gone out of the
source is as likely to be a label that did not survive the round trip as a slide the author meant to
drop - on that evidence this tool may not take somebody's own slide, with its pictures and the
comments hanging on it, when the way to really drop one is one click in Slides. At **every**
generation, not the first only: the base records the decision by not accounting for the slide, so
there is nothing to reverse itself (the lesson above, applied before it could be learnt twice). It
was 109 refusals in 600 campaign rounds at chain 8; the same 400 rounds at chain 6 that had 734
`unpaired` and 30 `slides-deleted` refusals now have **none at all** - every one of 2,400 first syncs
into a person's deck does what it can - and are clean, as are 600 at chain 8 and 500 at chain 10.
**A campaign that stops writing stops measuring**, so both decisions are counted where the refusals
are (`fuzz_sync` prints `held: element N, slide M` beside `refused:`), and the deck they are measured
on is drawn rather than fixed: `fuzz_world.build_adopt_base` takes its unpaired share from
`rng.uniform(0.0, 0.9)` per deck, since a flat 15% was gentler than every deck in the corpus (24% of
gdg24's elements are tied to no object, 90% of hebrew-lesson's) and the hard deck is the campaign's
whole job here. 200 rounds at chain 6 hold 1,338 elements and 76 slides, 500 at chain 8 hold 5,055
and 175, 400 at chain 12 hold 7,640 and 166; all clean, and not one refusal among them.
**And a key belongs to the source, the way a label does.** The refusals came back at chain 8 all the
same - three syncs of adopt-shaped seed 86066 wrote nothing at all, over a unit the merge itself had
never called blind. A unit the source dropped that the deck's edits keep alive stays in the base
under the key it had, and the next conversion's `identity.slide_element_keys` hands that very key to
whatever it finds in its place: an icon at the head of *another* line is `image/icon/0` as readily as
the one that went. The slide then answers to one key twice, and every `{e["key"]: e}` map over its
elements silently reads the second of them - so the gate looked the source's new unit up that way,
found the person's own unpaired icon standing under its key, and refused the whole sync. The kept one
gives way (`merge.keys_the_source_took`, called by `sync.new_base` and by `fuzz_world.rebase`, which
is its hand-written copy): from here on it is bookkeeping for the deck's own version, which the
source will never name again, while the new element keeps the key the next conversion has to inherit -
its anchored members follow it (a picture names its anchor by key) and nothing the source still draws
is renamed, so no pairing moves. The gate asks `merge.units` rather than a map besides, the answer to
"is this unit blind" not being a thing that may depend on which of two elements a dict kept. Measured
where it happened: of 1,997 rebases over adopt-shaped seeds 86000-86249 at chain 8, five slides
carried a duplicate key and none of 2,000 do now (converted seeds 9200000-9200249: 0 of 2,000 either
way, a converted deck's base having no kept-though-removed units to collide with), and the 250 rounds
that printed `refused: unpaired 3` are 250/250 clean with no refusal at all, holding 2,471 elements
and 64 slides.
**The plan is made for the deck it is going into**: everything sync writes is PDF pt times one number,
and that number was `SLIDE_W / page`, so a deck of any other width got nothing created in it at all -
ten of the 29 corpus decks (1440, 1920, 960, 800, 3456, 481.5, 595 pt). `DeckPlan(deck, page_width)`
and `measure_places(..., page_width)` (a thumbnail is 1600 px over the deck's page, not over 720) take
it from `snapshot.page_size`, `sync.build_ours` passes the base's `deck_page_size`, and what is refused
instead is the one thing a single scale cannot carry: a source recompiled onto paper of another
**shape** (`aspect_mismatch`, 0.5%), where every created object lands at the right place across the
slide and the wrong one down it.

Opt-in suite (real Google Slides, ~95 s): `python -m pytest -m slides` (the default run deselects
the `slides` marker, pyproject.toml). It converts the stress decks (19–22, 25, 13, demo) 3 at a
time into `out/slides-tests/<deck>` of the main checkout (fixed folders: the same decks are
rebuilt), runs `fidelity --refresh` and the measurement, and fails on items over the thresholds
unless `tests/slides_baseline.json` lists them under `known_failures` (with a verdict), or when
an error grows more than 0.75 over its baseline. `B2S_UPDATE_BASELINE=1` rewrites the baseline
(new failures come in marked UNVERIFIED), `B2S_SLIDES_REUSE=1` measures the existing folders
again without converting. Skipped with a message when the token needs a browser consent.
Measurements repeat to 0.01 across rebuilds (Slides renders deterministically).

Sync test harness (opt-in, marker `sync`, deselected by default): `python -m pytest -m sync tests/test_sync_live.py`
(`-k variants`: offline; `-k edit_catalogue`, `-k checker_controls`, `-k "scenario and <name>"`).
- Source versions: `tests/decks/sync/talk.tex` (Madrid 16:9 talk with every element kind: title page,
  nested bullets, number balls, formula hole + circled number, TikZ figure, block, table, diagram,
  tikzmark arrow, notes, an unlabelled frame) with docstrip-like guards (`%<flag>`, `%<!flag>`,
  `%<*flag>`…`%</flag>`). `tests/decks/sync/build.py [variant]` writes a plain .tex per variant into
  `tests/decks/sync/out/` and compiles it with SyncTeX; `VARIANTS` (flag sets), `CHECKS` (what a synced
  deck must show per flag), `titles`, `INTENDED` (the classification diff vs v1, tested offline).
- `tools/deck_edits.py`: human-like Slides API edits found by content (text, style, geometry, objects,
  groups, slides, notes, background); each returns an expectation `{edit, args, slides, checks}`.
  `verified` applies one and reads it back (checks fail before, hold after).
- `tools/sync_check.py`: evaluates checks on `presentations.get`, plus `integrity` (duplicates,
  orphans against base.json ids, formula pictures out of their group, groups taken apart),
  `check_report` (sync-report sections, `warnings` among them), `compare_fresh` / `thumbnail_diff` /
  `alignment_compare` (untouched slides vs a fresh conversion of the same source). An inline formula
  and a display equation are both tagged `image/math/N`; only the first belongs to a text, and
  `on_a_text_line` tells them apart by where the picture stands (inside a text's box top to bottom =
  between its words). Measured over 139 math pictures: all 44 display equations out, 90 of 95 inline
  ones in, the five it lets go being math beside a graphic rather than in prose.
- Scenarios (`convert v1 → edits → sync vN → check → second sync writes nothing`): untouched, disjoint,
  same-element, diff3, last-paragraph (the deck deletes a box's *last* paragraph and the source
  rewrites the same box: the merged text ends a paragraph early, so the diff's last hunk reaches the
  newline Slides will not let go of - that batch used to be refused and the sync died - and, found
  by writing it, a deleted bullet took its own line spacing out of the read-back's distinct paragraph
  styles, which `merge.uniform_changes` read as a restyle: the box was then kept as the deck had it
  and every later source change to it was dropped),
  conflict, deletions, slides, reorder-both, chain, concurrent (sync runs the
  command in `B2S_SYNC_BEFORE_WRITE` after planning), pull-wording (`pull --apply` → rebuild → sync
  leaves the revision alone, overrides converged), converged (the source says what the deck says:
  nothing written), many-edits (one slide edited every way while the source rewrites it), groups (a
  user group around a redrawn figure, a converter group taken apart, a deleted user group),
  nested-group (a block inside a user group), repainted-pictures (the converter writes the picture
  files differently - the transparent ground anchored pictures got - and nothing is rewritten),
  pull-picture (a picture added in the deck and pulled into the .tex is adopted, not duplicated),
  table-words (cells of one table edited on both sides merge per cell, `merge.table_merge`);
  `XFAIL` pins what sync can't do. Folders `out/sync-tests/<scenario>` of
  the main checkout (decks rebuilt in place), fresh conversions `out/sync-tests/_fresh/<variant>`,
  3 at a time; the whole suite takes about 17 min. Skipped while `beamer2slides sync`/`pull` don't exist.
  Delete `out/sync-tests/_fresh` after a converter change, or the scenarios compare their decks with
  conversions made by the previous one.
- Found by it: Google issues new `contentUrl`s for unchanged pictures (compare pixel signatures, not
  URLs); a base must keep the source's slide order (else the deck's reorder is undone next time);
  a slide sync creates must inherit the master background, not copy it (the theme sits there now).

The ambiguous deck (`tests/test_stress_live.py`, marker `sync`, ~5.5 min): the same machinery on a
48-frame talk built so that nothing can be identified by its title - three frames called Results,
untitled frames, twins one word apart, one paragraph on three slides, the same picture twice, a
table whose every row says the same, identical notes, astral characters and soft hyphens. Its
variants change the source drastically and ambiguously (labels moved and dropped, every title
renamed at once, ten frames reversed, twins swapped, a frame inserted between two near-identical
ones, the unlabelled frame retitled and half rewritten - `recast`, which is `identity.gap_pairs`
live: without it that sync creates a second slide and the old one keeps the person's edits;
`recastmoved` does the same to it *and* carries it across nine frames, which nothing can follow, so
that one is the live proof of `identity.near_misses` naming what it could not pair, while
`strangers` is the proof of its silence - a frame added and two dropped with nothing in common -
`kitchen` combining most of it) against decks edited every way at once; the order is checked
against the frames' labels, not their titles, and every slide nobody edited must still equal a
fresh conversion, thumbnail included. It is where the moved-label check and the order merge were
made to work; `-k "variants or pairs or selectors or budget"` is its offline part, which includes
the pairing itself against the truth `stress.frames` knows (every variant's frames paired with v1's
frame for frame by `identity.label_moves` + `align_slides`, a frame the variant adds paired with
nothing) - 19 variants in under a second, no Google.

Sync fuzzing (docs/sync.md, "Proving nothing is lost"): `tools/loss_oracle.py` judges one sync from
the read-backs before and after, the base, the report and the new conversion - did anything a person
put in the deck disappear without being accounted for? (Its docstring defines that; no Google call.)
`tools/fuzz_sync.py offline --rounds N` fuzzes `merge.plan_merge` against synthetic decks through a
reference applier (`tools/fuzz_world.py`), ~70 rounds/s with a shrinker; `live` runs the same against
real decks (`out/sync-fuzz/<seed>`, 3 at a time, passing rounds' decks deleted, `--chain N` for
edit→sync→edit→sync). `tests/test_sync_fuzz.py`: fixed offline seeds in the default run plus tests
that the oracle catches losses injected on purpose; the live campaign is marked `sync`.
Found by it: `merge.plan_unit`'s `move` shortcut took the delta from the unit's anchor while the
source may have moved only an anchored member, so a re-placed inline formula became a move of
[0, 0] that the report still called applied (`merge.unit_shift` now requires one step for the whole
unit, else the unit is recreated). Also: a geometry override was promised for a unit whose parts the
person had moved apart, although sync re-applies it by transforming the unit's top object, so the
dragged picture went back to the converter's box (`merge.geometry_writable` keeps such a unit and
reports a conflict); and `sync.tag_requests` alt-texted a diagram's main object, which is the group
emit builds under that id - the API refuses that and rejected the whole batch, so a sync that
rewrote a diagram slide died (fixed: the diagram goes untagged, as it does in a converted deck,
`tests/test_sync.py::test_sync_does_not_alt_text_a_diagram_group`);
and `sync.base_order` left out the slides the deck deleted although the source still has them, so
their base entries landed at the end and the frames after them lost their keys next time and were
created again (fixed: a `gone` slide keeps its place in the source's order; `fuzz_world.rebase`
orders the base with `sync.base_order` itself, so the campaign fails again if it stops).
Live chained rounds found two more: a word bolded in the deck whose sentence the source rewrote two
versions later lost its styling while the report promised an override - nothing can save styling
whose words are gone, so `merge.styling_lost` makes it a conflict (`snapshot.read_text` now records
run spans, so the read-back says *which* words a style is on; the offline campaign reaches it
through the `bold_word` deck edit and `fuzz_world._styling_ends`, and taking the conflict out fails
2 of 400 rounds). **Which** words those are is the alignment's business and was asked of the wrong
one (adopt-shaped seed 86044 at chain 8): the styling goes back through the matching blocks of the
live text and the text being written, and asking the tokens instead answered with the other copy of
a sentence the box says twice - so a bold on a word the source replaced had a token to land on, no
request wrote it, and the report promised an override. Asked of those blocks now, and of whether
they carry a **word** of the run across whole rather than anything at all, since two sentences
sharing no words still match the odd letter and a bold put back on the `i` and the `r` of another
word is styling gone (seed 404 again); a run the person's own rewording clipped to a few letters
inside a word is its own word here, so styling sync really does re-apply is not called lost
(`_styling_ends`' lesson from the other side). 1 of 250 rounds at chain 8 before, 250 of 250 after.
And the oracle itself accused a slide that two others - a moved frame and the
copy riding behind it - had merely passed (`loss_oracle.order_findings`). A third was the campaign's
own: chained steps could drop the same picture on the same slide at the same box twice, which is a
real duplicate made by the fuzzer, so `random_spec` now places an added picture or blank shape where
no other one stands (`_free_box`). The campaign's own again, at converted seed 9200614: a slide the
person adds or duplicates was given a six-digit id drawn afresh, which the birthday rule collides
well before a campaign is over, and two slides of one objectId is a deck Slides could never hand
back - the two read as one, so the oracle saw a picture the person had added disappear and the
report list one created slide where two appeared, three findings and every one of them the
fuzzer's hand. `fuzz_sync._fresh` gives a drawn id a *tail* when the deck already answers to it
(one op still takes one number, so every other round is unchanged), a duplicated slide gets its
own notes page as it does in Slides, and `_sync_step` now refuses a deck carrying an id twice out
loud rather than letting the oracle blame the merge for it. **A decision the base does not record
reverses itself** (converted seed 7700464 at chain 10): a unit the source removed and the person
edited is kept with a conflict, but the base then said only what it had always said, so the next
sync - finding the deck no longer different from the base - deleted the box the sync before had
promised to keep, and silently, `removed` being an applied change rather than a conflict. The
evidence for keeping is the deck differing from the base and a sync can take it away with its own
hands: deleting another element the source dropped left the person's group around this one with a
single child, which Slides dissolves. A kept unit now says `removed` in the base, as a kept *slide*
does (`merge.plan_unit`, `sync.new_base`), and only the person taking it out of the deck ends it.
And the one that killed a sync outright (seeds 608, 616): the
newline a Slides text ends on cannot be deleted - the API reads it back but leaves it out of the
length it will accept - so a deck edit that deleted a box's *last* paragraph made the merged text end
early, the diff's last hunk ran to the end, and the overrides batch was refused whole
(`merge.text_edit_requests` now keeps that newline out of the diff). Nothing offline could see it,
because the reference applier merges text without ever building a request: `fuzz_sync._writable` now
applies the requests sync would send under Slides' index rules and checks they write the merged text
(the fix removed fails offline seed 399), and `tests/slides_sim.py` refuses such a delete like Google.
`sync_check.integrity`'s `allow_ungrouped` / `allow_groups_changed` take a slide objectId as well as
a title, because a sync may retitle the very slide the person ungrouped (seed 607).
Both sides editing the same text is what every merge rule is about, and drawing each side's target at
random made it rare (5 text overrides in 200 rounds, never a table): the source op `collide` changes
exactly what the person just changed - reword, append, drop a paragraph, rewrite a cell - which is
what a real deck looks like, and it took that to 56 overrides and 10 tables in the same 200 rounds.
It found the pairing bug above within 600 chained rounds. `_writable` checks a table's cells the way
sync writes them, one at a time (`_writable_cells`), and `fuzz_sync._retext` keeps `run_spans` on the
words a deck edit leaves behind, as Slides does - stale spans had the campaign accusing
`merge.styling_lost` of losing styling that was never where the spans said (seed 2194).
A geometry override is one RELATIVE transform on the unit's *top* object, and the top is the group
that carries its children - but a person who took that group apart leaves a recreation with no group
to write on, and the anchored formula picture stayed at the converter's box while the report called
the move applied (live seed 903 at chain depth 8). Each member of a group-less unit now takes the
step itself (`sync.Sync._unit_oids`); the offline campaign is blind to this by construction, since
`fuzz_world._place_unit` models the outcome (the whole unit moves) and not the mechanism, so the
mechanism is pinned by `test_a_moved_unit_with_no_group_is_moved_member_by_member` and its
counterpart with the group. The harness's own (seed 900): a slide the person duplicated after
ungrouping something on it inherits the ungrouping, and the `integrity` excuse now follows the copy.
The other write path - a `move`, the source's place written onto the deck's own objects - is checked
offline now (`fuzz_sync._movable`: every object of the unit takes the step exactly once; the old
top-only move fails 2 of 400 rounds at chain 4). It took two draws to reach it at all, since a move
needs both sides on one unit: a source that moves a text moves the pictures its lines place, and
`collide` can move the box the person just edited (2 `move` units per 800 chained steps -> 57).
Counting what the campaign reaches (`unit/*`, `override/*`, geometry modes) is how the blind spot
showed; `fuzz_world` also kept `children` on a group whose unit had been recreated, which only
`parent_group` readers were saving it from.
The applier's own opinion has to be the mechanism's: a recreated element gets the person's run
styling back through `sync.style_range_requests`, which maps each styled span through the matching
blocks of the old text and the new one, so a span still sitting on characters the source kept goes
back on. `fuzz_world._styling_ends` asked instead whether the styled *words* turn up in the new
text, so a bold the person's own earlier rewording had clipped to two letters inside another word
counted as gone - the applier dropped it and the campaign accused the merge of losing it in silence
(adopt-shaped seed 23599, chained 6 deep, the only failure in 4,000 rounds).

Occlusion (`loss_oracle.occlusion_findings`, `text_hidden`): a text no opaque shape covered before a
sync (paint order: page elements, a group's children inside it; opaque: solid fill at alpha 1) must not
end up under a shape the sync created, unless the new conversion stacks that shape above it. A block
rebuilt with its panels over the kept body text (dc8523a) deleted nothing, so nothing else saw it. The
fuzz world has blocks and keeps z-order; `fuzz_sync._stacked` replays `Sync.regroups` /
`Sync.regroup_requests` through Slides' rule that a group keeps its children's page order (`_zorder`):
without the restack, 14 of 400 offline rounds fail. The page's own element order is replayed the same
way (`_restacked`, `Sync.restack`'s requests over the slide as the content batch leaves it), because
the applier's `_page_order` / `_restack` / `_not_over_kept` are a hand-written *copy* of that method -
two files that must be fixed together, with nothing to say when they drift apart (seed 8300231 was
caught only because both halves were wrong alike). It differs from the copy on ~10% of rewritten
slides and reaches what the group rule cannot, an element standing on the page itself: with
`Sync.restack` writing nothing, 6 of the first 120 converted rounds at chain 6 hide words a person
could read (`test_the_offline_fuzz_orders_the_page_as_sync_does`).
Four fresh seeds over three shapes then said the rest of it, in one sentence with two halves: **the
deck's order between two converter elements is nobody's edit, and about what it does not draw the
source has no opinion at all.** (1) *The order the last conversion drew* (79045, 610106): a rebuilt
group put its children back in the deck's order and `restack` gave two recreated page elements the
deck's places, but that order is only what the previous conversion happened to draw, and this one
draws them the other way round - the source moved a text into a panel; a moved label brought another
frame's elements onto the slide - so the panel came back over the text. A rebuilt group's children
that are source elements now take the source's order among themselves, rewritten or kept
(`Sync.zrank`, `regroup_requests`), unconditionally, since Slides will not let a person restack
inside a group; on the page the source's elements do too (`Sync._by_the_source`), rewritten or kept
(1500512 at chain 10: ordering only the rewritten ones leaves a slide with one of them nothing to be
ordered against), but only where the deck still has them in the base's order, because where it does
not somebody restacked and that survives - asked of the whole set at once, a z-order change being
the one deck edit `merge.deck_edits` cannot see. What the page rule orders are the page *elements*,
a converter group - one the base itself draws on the page - standing for the elements it carries and
a group the **person** made for nobody (1300381 at chain 6: asked of the objects alone it left out
everything inside a group, so a block's panel could not be ordered against a table beside it). And
an object the deck's order has no slot for - one this sync created, or one a group this rewrite
dissolves frees onto the page - is placed by the source too, or it lands on top of everything past
both guards; the applier models the outcome there (`_drop_lonely_groups` gives the freed child the
group's slot), so that half is pinned by a test alone. (2) *Words only the deck has* (78036, adopt 680477): the ceiling
above consults source elements alone, so a created panel landed on a text the source had dropped and
the deck's edits had kept alive, and a recreated panel that grew landed on another; nothing a sync
writes now ends up above a page element the source does not draw whose words it would cover
(`sync.would_hide`, the oracle's own question - opaque fill at alpha 1 over more than 20% of a text's
box - asked before the write). Hiding those words loses work nobody can get back and the price of
going under them is z-order, so that is the way round to be wrong. What moves is the page *element*
the new shape is drawn inside - the converter group this rewrite rebuilt, most often the block the
panel belongs to - since that is what the page order holds: asked of the object alone the rule
reached nothing at all when the panel was in a block, its id being in no page order (2300025 at chain
12, adopt 3200538 at chain 10). Which words are the deck's own is asked of each *text*, not of the
page element holding it, a group the person made being able to hold one of their text boxes beside
one of the converter's (5200496 at chain 12). And the two halves have a seam: a page element stands
for *every* converter element inside it while `_by_the_source` ranks it by the **first** of them, so
a group holding two of the converter's texts with a panel drawn between them is a place where the
source's order cannot be honoured at all - the group goes below the panel for the sake of the text
below it and takes the text above it down with it, and those words being the source's own is exactly
what this guard had been told to keep quiet about (8300231 at chain 11: one rule silent because the
other had spoken, and it had not). A text the source draws above the shape is spoken for only while
its page element is not itself standing below the shape on another text's account; where the page
order does carry the source's word the text is already above and the pass never sees it, so nothing
else moves (`test_a_created_panel_goes_under_the_text_a_group_carries_above_it`). All four are mirrored in the
applier (`_restack`, `_not_over_kept`, `_page_order`, `_regroup_order`) and pinned by tests that fail
without them (`test_a_created_panel_stays_under_words_only_the_deck_has`,
`test_the_children_of_a_rebuilt_group_take_the_sources_order`,
`test_the_rank_a_rebuilt_group_is_ordered_by_covers_kept_objects_too`,
`test_the_elements_a_rewrite_replaces_take_the_sources_order`). (3) *The one shape of it no ordering
reaches, so the person is told* (670146 at chain 6, 2 of 2,600 rounds at chains 4 to 10): z-order is
written in those two places only, and neither reaches a converter element the person folded into
**their own** group - its page element *is* that group, so restacking it moves everything else they
put in there, and children of a group nobody rebuilds cannot be reordered at all. With the text in
another page element the source's order across the two is unrealisable, and honouring the grouping is
not what hid the words, so the oracle calls it a **note**
(`loss_oracle._folded_into_a_group_of_their_own`, the `uncertain_slides` precedent: not a loss *in
silence*) and `Sync.finish` names it in the report, which nothing else would - nothing was deleted
and every write went through (`sync.folded_hiders`: ungroup it, or move the shape; `finish` reads
the order before the cleanup, so words the cleanup deletes - the old version of a block the source
redrew inside the person's group - are left out, `w["doomed"]`: the live nested-group scenario had
warned about v1's block title since the warning came in). The excuse is
exactly that shape and no wider - the same panel standing on the page itself is a loss again, since
`restack` could have ordered it. It lives in the oracle and not in `merge.plan_merge` because the
offline campaign never runs `Sync.finish`, and predicting the final z-order at plan time would mean
reimplementing the applier inside `merge`.

## Agent tools (`src/beamer2slides/agent/`, docs/agent-tools.md)
Every journey in this file is a thing an AI should be able to do, and the CLI is the wrong door for
one: it says what happened in prose, says no by raising `SystemExit`, and asks for Google by opening
a browser - which hangs a harness forever. The agent layer fixes exactly those three and calls the
same functions underneath; nothing here reimplements a journey.
- **Eleven tools, one per journey** (not a gateway: a model picks better from eleven named tools with
  their own arguments than from one with a `command` string, and a gateway's schema cannot say that
  `deck_sync` writes to somebody's deck while `deck_inspect` does not). `b2s_status` (what this
  workspace holds and whether Google is reachable, no Google call), `deck_inspect`, `deck_convert`,
  `deck_sync`, `deck_pull`, `deck_adopt`, `tex_label`, `tex_converge`, `doc_push`, `doc_sync`,
  `doc_adopt`. `tools.TOOLS` is the registry, iterating in `tools.ORDER` - the order of operations
  INSTRUCTIONS.md gives, since a model's tool list, the MCP catalogue and the workbench's dropdown
  all read top to bottom and what they read there is advice (collecting them from the modules put
  `deck_convert` above `deck_inspect` and `doc_adopt` above `doc_push`); the collection still says
  which tools exist, so one left out of `ORDER` raises at import.
- **One of them also comes in halves**, because a caller may not have the local work and the Google
  write in one place (a sandbox that compiles, a service that holds the account): `deck_prepare`
  (`needs=LOCAL_ONLY`) classifies, renders and writes the folder, `deck_upload`
  (`needs=(READS, WRITES, WRITES_GOOGLE)`) builds the deck from that folder, and `deck_convert` is
  the two in sequence over the same `_prepare` / `_upload`, so there is no third code path and the
  refusal codes, the artifacts and the rebuild guard are the same ones instead of a caller reaching
  past the layer into `classify`, `render` and `emit`. What crosses is **the folder and nothing
  else**: deck.json, backgrounds/, figures/ and `prepared.json`, which carries what `_upload` would
  otherwise have re-read from the PDF - the title, the overlay mode the base records, and the
  source's **name and digest**, which is all anything ever wanted of the file (`guard.check_rebuild`
  compares the name to catch a folder whose deck came from another PDF, `snapshot.source_info`
  stores a sha1 and now takes those two facts already measured). The PDF's *bytes* are read by
  exactly one code path, `emit.fallback_pictures` - the retry that crops a refused element's region
  out of the page - so `deck_upload` takes an optional `pdf`, says `can_crop_refused_elements`
  either way, writes the new location into deck.json when it is handed one, and `emit` names the
  file it wanted rather than failing inside the retry. A folder written before the split has no
  prepared.json and is not refused: deck.json carries classify's source block, so only the digest is
  missing (one conservative branch in a later interrupted sync), and that is said out loud.
  `deck_convert` stays first in `ORDER`: a model reading top to bottom should meet the whole journey
  before its parts.
- **Three seams**, which is all a harness plugs in (`context.AgentContext`): `Workspace` (refs in,
  absolute paths out; a ref that climbs out is `outside_workspace`, a folder may be `readable` but
  never written, `stage` brings an outside file in, `out_dir` is the workspace's own - `paths.out_root()`
  answers differently for a checkout and a pip install and a workspace must not); `GoogleAccess`
  (`NoGoogle`, `TokenFile`, `InjectedToken`; **never interactive** - a dead token is `needs_consent`
  naming the command a person runs, and `InstalledAppFlow` is never reached, pinned by a test);
  and `allow`, four action classes (`READS`, `WRITES`, `READS_GOOGLE`, `WRITES_GOOGLE`) checked
  **before the body runs**, so a forbidden journey does no work rather than stopping halfway through
  a rebuild. `google_auth.use_provider` is the one hook added to the library: credentials injected
  for the length of a call instead of found in the filesystem; `use_services` is the same hook one
  step later, a ready Slides/Drive/Docs client (or a `(api, version, creds) -> client | None`
  builder) handed over instead of one built, which is what a server wants when `build()` would
  fetch a discovery document per call (and, with `[google]` an extra, what lets such a caller do
  without the package altogether - `gapi`, "Google side" above, where `needs_credentials` is too).
  **Both are per context, not per process** (`_Hook`, a
  `ContextVar`): they were module-level globals, fine for one conversion at a time and wrong for a
  server with two visitors' tokens in the air, neither of which may reach the other's deck. The one
  edge is that a thread started inside the block runs in a *fresh* context and inherits nothing -
  which the library's own pools do not care about, every one of them resolving `credentials()` on
  the calling thread and handing the answer down (`emit.measure_places`,
  `deck_ir.slide_thumbnails`, `snapshot.sign_pictures`) - but a thread that asks anyway must never
  fall through to `InstalledAppFlow` and open a browser on a server, so while exactly one block is
  open it is given that one, and where two differ it gets a `RuntimeError` naming
  `contextvars.copy_context().run(...)`: guessing there would hand one visitor another's account.
- **A harness with no filesystem to name** (`content.py`, docs/agent-tools.md): the disk the library
  needs does not go away, it leaves the interface. Every file argument also takes the file - a `data:`
  URI or `{"name", "base64"|"text"}` - which `take_in` writes into the workspace's `inbox/` and
  replaces with the ref, so the journey underneath sees the file it always saw (in `@tool`, and in
  `mcp.dispatch` *before* the schema check, since a content dict is not a publishable parameter type;
  idempotent, because what comes out is a plain ref). `deliver="inline"` fills each `Artifact` with
  its own `text`/`base64` beside `bytes` and `sha256`, under a per-artifact cap and a per-call budget
  so a 30-slide conversion cannot hand a model its own weight in PNG; what did not fit says
  `truncated` and is fetched with `workspace.read_bytes`. `AgentContext.detached()` is both over a
  `MemoryWorkspace` - a private temp dir removed on `close()` whose name nothing outside the context
  learns. **A plain string is never content**: `"talk.pdf"` is a ref and a `docs.google.com` URL is a
  deck the journey resolves itself, or `deck_sync(deck=<url>)` would start syncing against the deck's
  own HTML; `data:` is the one string form. A `{"url": …}` is fetched by the *context's* fetcher and
  refused by name when there is none - a built-in `urlopen` would be an egress path from a model's
  argument to any host inside a call the harness thought was local. `tests/test_agent_content.py`.
- `@tool(name, needs)` is the wrapper every journey is written inside (a contextmanager cannot skip
  its body when the gate refuses). It serialises - **one journey per process**, because the library
  underneath is full of process-wide state two journeys would share (`redirect_stdout`, `pdf._backend`,
  `checks.convert_locally` monkey-patching `render.save_png`, the pure backend's multiple-master
  blend); catches, so prints become `data["log"]` and a progress callback, `SystemExit` becomes a
  code and an unforeseen exception becomes `failed` rather than taking the harness down; gates; and
  fetches credentials up front. `needs` declares the **least** a journey does, so a read-only context
  can still run `deck_sync(dry_run=True)`; a body about to write for real calls `j.require(WRITES_GOOGLE)`
  and gets the same refusal one step earlier. A context with no account hears `offline`, not
  `forbidden`: it is not withholding permission, it has nothing to give.
- **One result shape** (`types.Result`): `ok`, `code`, `summary` (prose for the model), `data`
  (numbers for a benchmark), `artifacts` (workspace refs with a kind), `diagnostics`
  (`warning`/`conflict` with a `where`), `next_steps`, `seconds`. The refusal vocabulary is closed
  (`types.CODES`) and every code in it is explained in the guide, pinned by a test.
- `INSTRUCTIONS.md` travels in the wheel (package data, read through `importlib.resources`): the
  bargain, the one rule (**never rebuild a deck somebody has edited** - `force_rebuild` is a thing a
  person asks for in words), the order of operations, and the truths an agent cannot infer - a label
  written twice reaches the PDF as no label at all, an open comment is invisible to the merge, a
  second `doc_sync` writes zero requests.
- `schema.py` / `mcp.py`: JSON Schema straight off `typing.Annotated` parameter descriptions, plus
  `needs` and an `effects` block saying whether a call wants approval; `anthropic_tools` /
  `openai_tools` for the two wire formats, `dispatch` (validates, never raises), and an MCP server
  (`beamer2slides-mcp`, extra `[mcp]`; everything else works without the SDK). `serve` wires itself
  to **either generation** of that SDK: 1.x takes its handlers through decorators, 2.x took the
  decorators away and takes them in the constructor, so `pip install beamer2slides[mcp]` - which
  resolves to 2.x - fetched a server this one died on with an `AttributeError` before the first
  message. The protocol is the same either way, so it is a binding difference and nothing else
  (a refusal says `is_error` itself there rather than being raised, which is what the `Result`
  meant in the first place), and both shapes are pinned by fakes
  (`test_the_server_speaks_to_either_generation_of_the_sdk`), the real SDK being an optional extra
  the suite cannot count on.
- Tests are offline and fast: `tests/test_agent_core.py` (each promise broken on purpose),
  `test_agent_deck_tools.py`, `test_agent_source_tools.py`, `test_agent_doc_tools.py` (a whole
  `doc_sync` end to end against `devtools/doc_world.py` - plan, write, settle, regenerate, and the
  next sync writes nothing), `test_agent_schema.py`.
- Live suite (opt-in, `tests/test_agent_live.py`, markers `slides` ~50 s and `docs` ~20 s): three
  journeys driven **in process**, as a harness drives them, since a refusal arriving as a `code`
  rather than an exit status is the whole point and no fake can show it. A deck converted and
  inspected; **the one rule against a deck a person really edited** - `deck_convert` refuses with
  `deck_edited`, names `deck_sync` in `next_steps`, and the dry-run merge then plans to keep the
  typed word; and a document pushed, reworded in the source, synced, with the second sync writing
  0 requests. Folders are fixed (`out/agent-live/`) and the deck is rebuilt, not remade, so the
  edit the middle test makes is taken back at the start of the next run (`_untype`) - without that
  it passes once and is refused for ever after. The document is deleted at the end.
- **Agent benchmark** (`devtools/agent_bench.py`, tasks in `agent_tasks.py`, history and results in
  docs/agent-bench.md): can an agent drive these journeys without destroying someone's work? Not
  whether the library is correct - the suites do that - but whether the agent looks before it leaps,
  dry-runs, reads a conflict, refuses a forced rebuild nobody asked for, and puts a choice to the
  person when it is the person's. 13 **replay** tasks run against a scripted fake registry (real
  `Result` shapes, nothing read or written) and grade the decision sequence; 7 **live** tasks really
  run the Google-free tools and are graded on their artifacts by the project's own graders - five of
  them the Google Docs journeys against `devtools/doc_world.py`, a real `doc_sync` with no Google
  and no quota (plan, write, settle, regenerate, and the document read back), whose fixture lives in
  the process that builds it (`Task.process_bound`).
  **Harm** - a task failed in a way that would have destroyed work (a forced rebuild, a guessed
  `assume_base`, a document rewritten under an open comment) - is counted and reported apart from the
  pass rate. Every task ships a correct policy that passes and at least one wrong one that fails,
  both asserted by `tests/test_agent_bench.py` (offline, ~1 s; the latex-tier task behind the
  `inverse` marker). No model is called from this repo: `Scripted` proves the graders and `Recorded`
  scores a transcript made in any harness (`agent_bench bundle` prints what one needs).
  `run --tier all --tag T`, `report --tag T`, `tasks`. Baseline: 20/20 correct, HARM 0; 37 wrong
  policies, all failing, 12 of them harmful.
  **And what a run cost sits in the same table**, so choosing a smaller model is a measurement and
  not a hope: a `Recorded` transcript may carry `model` and `usage` (top-level or per step, several
  harnesses' spellings; `bundle` asks for both), which `summarise` totals per task and for the
  suite, and the answer to "is the cheap model good enough here" is read off one table - the pass
  rate held **and** HARM stayed 0, at this many tokens; a model that costs half as much and harms
  once is not cheaper. Nothing here calls a model, so a cost is only ever what the transcript
  reports: a task nobody priced reads `-` and never 0 (an unmeasured suite must not look free), and
  the total carries how many tasks it covers, so a figure measured on 3 of 20 cannot be quoted as
  the bill. The column is the instrument; no A/B has been run yet (docs/agent-bench.md).
- **The round trip, live** (`devtools/agent_tasks_google.py`, tier `live_google`, 2 tasks): the one
  question the other tiers cannot answer - can an agent edit a **real** Slides deck and a **real**
  Google Doc through the text representation, the beamer `.tex` and the canonical `.html`, which is
  the only form of either a model can read? The fixture is built in Drive and carries an edit a
  person made in the browser (a colleague's note on the Risks slide, a reader's sentence in the
  handbook), and the grade is what Drive holds when the run ends: the source's change arrived and
  the person's edit is still there. No tool edits the source - the harness's own file tools do that,
  which is the real arrangement: the eleven journeys are the bridge to Google and the `.tex`/`.html`
  are ordinary files a model reads, changes and hands back (the Slides half recompiles, and the
  prompt names the command). Gated before the fixture is built, since the fixture is itself a write:
  `agent_bench run --tier live_google --allow-google`, `agent_play start <task> --allow-google`.
  Both fixtures are reused under fixed names, so a hundred runs leave two files behind. Such a run
  is also the one transcript that cannot be scored by replaying its calls into the tools - they went
  to a real deck - so `agent_bench.Replayed` hands the grader the answers the run really got and
  `run_task(facts=...)` the fixture it was played on.
- **Playing one task as a model runs** (`devtools/agent_play.py`, `tools/agent_play.py`): `Scripted`
  and `Recorded` both want the whole run to exist before grading, and a model decides its next move
  after reading the last result - so between the two there was no door. `agent_play start <task>
  --run-dir D` prints the request, the tools, their schemas and the one rule; `call <tool> k=v` takes
  one turn (`k=v`, not `--args` JSON: PowerShell 5.1 mangles quotes inside a native command's
  arguments); `answer "<text>"` ends it; `score` hands the transcript to `agent_bench.run_task` with
  the task's own grader, so a played run and `--policy recorded:D` of the same calls come back with
  the same status, the same failure sentences and the same harm count (asserted over 36 comparisons,
  not hoped). The run dir *is* a `recorded:DIR` folder. A model cannot reach past its task: a replay
  registry answers an unoffered tool with the benchmark's own `bad_request`, a live one runs under
  `AgentContext.offline`, and tier `live_google` needs `--allow-google` at `start`; a
  `process_bound` task is refused at `start` rather than played, since one process per turn is
  exactly what its fixture does not survive. Still no model
  called from this repo (pinned by a test walking the module's imports). Measured with Opus 5
  agents playing **six** tasks blind, one agent per task, given the folder and the three commands
  and nothing to read (docs/agent-bench.md): 9 calls, **none redundant**, 5 passed with HARM 0, and
  each of the five stops where the next decision is the person's - declining to force, declining to
  pick a side when both sides lose work, declining to resolve a duplicate label, doing the half of
  a request that was possible offline and saying why the other half was not. The sixth failed
  because the **agent's own permission classifier** refused the `agent_play call` that would have
  written - the model had chosen exactly the right call (`force_rebuild=True, backup=both`, forcing
  only because the person asked in words). A harness that classifies commands has to pre-authorise
  that one, or the score measures the sandbox; nothing here can see a command that was never run,
  and telling the model a replay task writes nothing would destroy what is measured.

## Playground (docs/playground.md)
`python -m beamer2slides playground` (`src/beamer2slides/playground/`: stdlib `http.server` + a static
page): a talk typed, picked or uploaded runs through compile → extract + classify → render on one
worker thread (the stages print; `redirect_stdout` is process-wide, so request logs go to stderr), and
the page draws each slide from the job's deck.json (editable preview over the background, native
boxes, background, debug, IR). Jobs in `$B2S_PLAYGROUND_JOBS/<port>` (swept at start: one
folder per port, or a second server deletes the first one's jobs).
Whose Drive a deck goes into decides whether the button exists at all (`server.google_mode`):
`local` = `B2S_PLAYGROUND_GOOGLE=1` and a token, the owner's Drive, for one's own machine;
`signin` = `B2S_PLAYGROUND_GOOGLE_CLIENT_ID` (an OAuth **web** client, not a secret), the visitor's
own - their browser gets an access token from Google's sign-in script and the server holds it for
one `emit` call through `google_auth.use_provider` (nothing stored, no refresh token; that provider
is per context, so two visitors' tokens can be in the air at once, and `to_slides` still takes a
lock for the pipeline's own process-wide state); neither = the recorded runs from `docs/media`.
The browser asks for `drive.file` alone (`server.WEB_SCOPES`): measured, a token carrying only that
builds a deck end to end - and unlike `presentations`, which the command line asks for, it is not a
*sensitive* scope, so a published playground needs no Google review.
`Dockerfile` = the deployment (TeX Live, uid 1000, port 7860 or `$PORT`, `openin_any=p`); not built
on this machine (no Docker). Hosted on Cloud Run (`docs/playground.md`, "On Cloud Run":
`gcloud run deploy --source .` builds it, `.gcloudignore` says what goes up; `--max-instances 1` is
correctness as much as thrift, since a job lives in one container's memory, and the service URL must
be an authorized JavaScript origin of the web client). A Hugging Face Docker Space now needs PRO.
**Live**: https://beamer2slides-playground-702466108736.europe-west1.run.app (project `beamer2slides`,
region europe-west1, one instance, scale to zero, CHF 50/month with the budget's *spend cap*
enforcing on Cloud Run). A run there moves only while somebody is asking about it: Cloud Run gives
CPU during a request and throttles in between, so `b2s_status` measured 2.7 s polled four times a
second, 21 s polled twice and 68 s left alone (the page polls; a script should too). The consent screen is **In production**, which it could only become once
the Branding page had a home page and a privacy policy on an authorized domain: hence `/privacy`
(`static/privacy.html`), served by the playground itself and saying what the code does.
**The workbench** (`playground/workbench.py`, `runner.py`, `static/workbench.js`) is the second
tab and the rest of the library: *Try it* is one road, and sync, pull, adopt and the Docs side are
roads it does not have. A folder per visitor, a file editor, and the **eleven journeys of the agent
layer** run in it - nothing reimplemented: the tools are `agent.tools.TOOLS`, each form is built in
the browser from `agent.schema.all_schemas()` (so the form and the signature are the same text),
what comes back is the `Result` every tool answers in (summary, diagnostics, the files it wrote -
clickable - and `next_steps`), and `INSTRUCTIONS.md` is on the page. `tex_compile` is the twelfth
entry and the only non-journey: how a source in the workspace becomes the PDF the deck journeys
start from. **There is no shell**; the only two things executed are a TeX engine and a journey, and
a journey runs in a **subprocess** (`@tool` serialises one per process, `inverse.Compiler`'s
compiles carry no time limit of their own and a process can be killed where a thread cannot), with
the job - and the visitor's access token - going in on **stdin**, never a command line, and
progress and the result coming back as JSON lines. A journey that needs no account is handed no
token, so it must not report on Google as though it had looked: `b2s_status` on a `signin` host
said "not reachable (offline)" on a page with a sign-in button, and now says the arrangement
(`runner.SIGN_IN`; `auth.NoGoogle` carries a `reason` and a one-sentence `fix`), which is the part
the process can see. **The editor never reverts a file a run rewrote, it merges with it**: a journey
rewrites what it is pointed at (`doc_sync` regenerates the canonical HTML from the document it has
just written), so a buffer opened before a run is older than the file and saving it back is a
revert - which the next sync reads as the source dropping what the rewrite brought in, and deletes
those blocks from a live document. `GET .../file` answers with `X-B2S-Version` (a digest of the
content: a byte-for-byte rewrite has taken nothing away), the page holds it while it types, and
hands it back on `PUT ...&version=<stamp>` (the empty stamp is "nothing of that name when I read
it", so *New file* cannot land on one a run just made, while an upload names no stamp and replaces
what is there). Where the file no longer says that, the two are **merged**
(`workbench.reconcile`) - and here, and only here, a textual merge is the right one: the Docs side
cannot have one, a paragraph having to be recognised again after the reader reworded and dragged it
(hence the named ranges, which `files.update` would destroy), while between the editor and the
journey the two sides are the *same file in the same format* and there is a real base, the bytes
this server handed the editor, which the stamp names (`Session.remember`, the last
`KEEP_VERSIONS`=16 under `MERGE_BYTES`=512 kB). It is `merge.diff3`, the sync's own word-level one,
so an edit at the start of a line and a rewrite at the end of it both land. What both sides changed
is **refused**: 409, nothing written, both versions quoted - no conflict marker is ever left in the
file, since in a canonical HTML file a marker is not a marker but words outside every block, the one
thing `doc_ir` stops a sync over; a stamp the server has forgotten and a file that is not text are
refused the same way, the base being memory and what is gone not being guessed at. And the page
does not sit on a stale buffer until it saves: the open file is read again when a run finishes, an
untouched buffer taking what the run wrote and **a buffer somebody has typed in never being thrown
away**. Measured on the live playground, on a real document: an adopt, an edit in the browser, a
sync that merged it, then a save from a buffer older than that sync - which deleted the very
sentence the merge had kept. Stopped after 420 s, killed by process group;
every path goes through `LocalWorkspace.resolve`; `shell_escape=f` beside `openin_any=p` so the
library's own compiles are fenced as the playground's are; 80 MB and 3000 files per workspace, the
last 12 kept. A deck **this app did not make** is unreachable under `drive.file`, which is what
keeps that scope non-sensitive, so `deck`/`doc` arguments grow a Google **Picker** button where
`B2S_PLAYGROUND_GOOGLE_API_KEY` names a browser key: the visitor picks the file in Google's own
window and that grants this app `drive.file` on that one file. A `signin` thing only (`canPick`):
the Picker wants a token from the *browser*, and in `local` mode the host's own token carries
`presentations` and already reaches whatever its owner can open.
Tests: `tests/test_playground.py` and `tests/test_workbench.py` (offline: a workspace through the
HTTP API, a journey in its own process, the boundary, the time limit, and where the token goes).

## Google Docs (docs/google-docs.md)
The same bargain as the Slides sync, one dimension smaller: a **canonical HTML file** in git is
what the source says, a Google Doc is what the reader says, and where both moved the document
wins. `python -m beamer2slides docs push doc.html` imports the file through Drive, plants one
**named range** `b2s:<key>` per block and rewrites the file with those keys and a
`<meta name="b2s-document">`; `docs sync doc.html [--dry-run]` merges three ways, writes with
`requiredRevisionId` (re-plans up to 3 times on a mismatch; `B2S_DOCS_BEFORE_WRITE` is the test
hook) and then **regenerates the file from the document it just wrote**, so file, document and
base agree and the next sync writes 0 requests. Modules: `doc_ir.py` (IR ↔ canonical HTML ↔ `documents.get`,
keys, named ranges), `doc_merge.py` (pure planning), `doc_sync.py` (the commands).
- **And here too it is round trips, not work** (docs/google-docs.md, "What a sync waits for"):
  profiled call by call (`devtools/docs_bench.py`, wrapping `HttpRequest.execute` as the Slides
  side does), a steady sync of a 50-block document was **7.0-7.5 s over 10 Google calls, 95-97%
  of it with a request open** - 0.4 s of that clock is this machine, so the lever is how many
  calls are in the air. Five changes, each falling back to the old serial order when a caller
  lent its own client (`doc_sync.lent_clients` asks `shared_service` *and* whether this module's
  `docs_service`/`drive_service` are still the library's, since a harness that rebinds them is
  lending too; `credentials_for_threads` resolved on the calling thread): the head's three reads
  need nothing of each other, so the document and the comments go on threads of their own
  (`in_background`) while the base stays here, it being the one that reads a file and appends to
  `troubles`; the **comments** are waited for at report time, so that read overlaps the write
  rather than the planning; the base file is written to the id the read already learned
  (`save_drive`'s `known_fid`) instead of asking the document again; the **cache remembers that
  id** (`BASE_FID`), so `load_base` fetches the base without Drive's own lookup, falling back
  where the hint names another document's base or none; and the planted anchors are patched from
  the batch's own answer (`adopt_replies`: a `createNamedRange` reply carries the id and the
  request carries the key and the span), the read after the plant asking for what is in hand -
  `adopt` hands `settle` the read it made a moment earlier for the same reason, nothing having
  been written in between. **10 calls -> 7**, which are the five serial steps there is no getting
  under: the head (base ∥ document ∥ comments), the words `batchUpdate`, the settle read, the
  plant `batchUpdate`, the base upload. Interleaved A/B against a worktree of the commit before
  them, two documents, alternating order, a subprocess per arm: over ten pairs **old 6.2-16.6 s
  (means 9.86 and 8.39) against new 4.5-5.4 s (means 5.02 and 4.93), ten of ten favouring the new
  code**, ~6.5 -> 4.9 s against the old arm's clean rounds alone. The floor is ~95% "a request is
  open" and its biggest item is the base's `files.update` **with media** (~1.6-1.9 s whatever it
  carries, the Slides side's own measurement), which has nothing left to overlap it with; sending
  it before the plant was refused, the base recording the planted range ids and "nobody reads
  that field today" being no thing to make a durable artifact depend on. One defect came out of
  the measurement rather than the suite: `store_base` writes the cache before Drive and put the
  id back only where Drive answered with a *different* one, which in the steady state it does
  not, so the cache lost the hint on every run that used it and the call count alternated 7, 8,
  7, 8 with neither arm wrong (`known`;
  `test_the_cache_remembers_where_the_base_is_and_keeps_remembering`).
- **The base is Drive-first**: it is a JSON file in Drive beside the document, its id in the
  document's own `appProperties.b2sBase`, and `.b2s/<stem>.base.json` is a cache (the Slides
  arrangement, `snapshot.save_drive`; `.b2s/` is scratch state a fresh clone, a colleague or a
  second checkout does not have, and without a base `sync` fell into the `--assume-base` dialog,
  where both answers throw work away). Reading prefers Drive, falls back to the cache and says
  which it used - a cache another checkout has overtaken (`generation` counts the syncs), a cache
  newer than Drive's (a sync whose upload failed), no base in Drive yet, or one Drive names and
  cannot serve; a base that is truncated or belongs to another document is refused out loud.
  Writing goes to the cache atomically, then to Drive; a Drive write that fails never fails the
  sync. `.b2s/<stem>.sync-report.{json,md}` is the report. With no base anywhere, sync stops and
  asks for `--assume-base document-wins` (nothing is written to the document, the file is
  rewritten from it: source edits since the last sync go) or `source-wins` (the file is written
  over the live document: the reader's edits go). `file` / `document` are the old names and read
  backwards - they named the side the base is taken *from* - and still work with a warning.
  Before `source-wins` the document is exported to `.b2s/backups/<stem>-<when>.html` and the path
  is reported; an export Drive refuses stops that sync (`guard.demand_way_back`'s principle),
  `--no-backup` is how one asks for a write with no way back.
- **`docs adopt --doc <url|id> [doc.html] [--force]`**: the canonical file a document nobody
  pushed never had - `push` only goes file → document and refuses a file that already names one.
  It reads every tab, keys the blocks, plants one named range each, writes the file and stores
  the base; then it is an ordinary synced pair. The path defaults to a slug of the title;
  idempotent (a second run plants no second set of anchors and writes the same file); refuses a
  target that names another document, or none, unless `--force`.
- Batching: `send` cuts a plan over 500 requests into consecutive batches in the same order,
  chaining each answer's `writeControl` into the next `requiredRevisionId` so a reader typing
  mid-run is still refused. Order is preserved and nothing crosses a boundary, so the document
  written is the same - but a chunked write is **not atomic**: one that fails part-way leaves
  the earlier batches in. The report says so whenever chunking happened. `_write_structure`
  keeps its own batch.
- A `files.update` rebuild **destroys every named range**, so after the first import only
  incremental `batchUpdate` edits - there is no Docs equivalent of `convert`'s rebuild.
- Indices are UTF-16 code units, and a chip is **one** unit however long its words look.
- **Frozen runs** (chips, equations, dropdowns, TOC) are content no HTML import can create:
  never rewritten, reported instead. The mirror image: a list's ordered-ness is content an
  *imported* document cannot report (Drive's importer leaves every nesting level
  `GLYPH_TYPE_UNSPECIFIED` for `<ul>` and `<ol>` alike), so the **file** fills it in
  (`doc_merge.restore_unreadable`) and `settle` then gives the list bullets of the
  document's own (`bullet_requests`: measured, they read back from then on, so a reader's
  switch to numbers is seen); block identity ignores ordered-ness entirely.
- **What the dialect says** (`doc_ir`): runs carry `font`, `fontsize`, `smallcaps` and
  `script`
  beside the marks — one unquoted family name (a fallback list is mangled on import:
  `Georgia, serif` → `Geo`), the size as the document reports it (the importer rounds a
  fraction away and the settle rewrites the file, so nothing oscillates), small caps as
  `data-smallcaps`. `<code>` made four families one face; it is still written and still
  means Courier New, but a face is now carried as itself. `script` is `baselineOffset`,
  the last `TextStyle` field the dialect could not say: a superscript is content, not
  decoration (the 2 of x², a footnote marker), and a block written again from nothing
  came back flat on the baseline with nothing saying so. Not a mark, since it is one of
  three values, so "none" is a third value and not the absence of the other two — a
  theme can raise a whole named style and a reader must be able to refuse it (the
  `data-off` reasoning, one value wider). The import carries `<sup>`/`<sub>`; the
  refusal has no tag, so the file says `data-script="none"` and only a `batchUpdate`
  writes it. Paragraphs carry `indent`,
  `indent_first` and `line_spacing` as `margin-left` / `text-indent` / `line-height`
  (kept by the importer) and `shading`, `space_above`, `space_below`, the four
  `border_<side>`s, `page_break` and `keep_with_next` as `data-` attributes:
  `background-color` on a `<p>` is a character highlight on its runs, not
  paragraph shading, and no margin survives, so those go in by `batchUpdate`
  only. A rule is `"<width>pt <solid|dotted|dashed> #rrggbb [pad <n>pt]"`
  (`doc_ir.BORDER_RE`) and a rule of no width is no rule; the padding is always named,
  since inside a `paragraphBorder` an unset field is not the API's "back to what you
  inherit" — the border itself is the field being written (`doc_merge._border_value`).
  `borderBetween` stays unmodelled on purpose: it is a rule *between* consecutive
  paragraphs sharing a style, and a block model that plans one paragraph on its own has
  nowhere honest to put half of it. A run or paragraph that only repeats its named style says nothing
  (`_named_defaults`), or an imported document reads back as a wall of spans; a
  bullet's own indents are the list preset's. `adopt_keys` compares the plan with the
  read-back (`carry_unimported`) and `tidy_requests` writes what no import could carry
  — additions only, since in a read "absent" is also a reader who took the styling off.
- **`MANAGED` / `MANAGED_PARAGRAPH`**: the fields the merge owns, named on a restyle
  whether or not the block asks for them, so a property the source dropped goes away. A
  field belongs there only when the file can say it *and* a read can see it — naming
  one the file cannot carry would clear, on every source restyle, something a reader set
  in the browser; that is why `weightedFontFamily` stayed out while `<code>` was all the
  file could say, and why it, `fontSize`, `smallCaps` and `baselineOffset` are in now —
  with that last one every field of a `TextStyle` the merge could mean to write is one
  the file can say and a read can see, and a field the *world* does not know
  (`doc_world.API_TO_IR`) is one the campaign draws on both sides and never applies, so
  a test pins that set against `MANAGED`. `_take_shape`
  replaces a dict update that could only add, so a source that took a block's centring
  or shading away now takes it away. Both fire only where the source changed the styling
  and the document did not, so a reader's own face or shading is never written over.
- **The document's theme is the reader's, and an edit through the file leaves it alone.**
  A document's look lives in its named styles, which no request can write, so the only
  question is whether a source edit can *pin* a paragraph against its theme - and
  `documents.get` reports what is set on a paragraph and its runs, never what they
  inherit. A heading a theme makes blue, bold and centred says none of it, the file says
  none of it, and a managed field with no value is the API's "back to what you inherit",
  so all three come back after a rewrite and follow the theme if the reader changes it.
  The alignment was the one exception and a real loss: `paragraph_style` wrote `START`
  whenever the file said nothing, which is also what such a heading says, so one source
  restyle took it to the margin for good. It is subtracted now like the rest
  (`_named_defaults` reads the named style's alignment) and given a value only when
  somebody chose one, `left` among them. Neither the loss oracle nor the convergence
  check could see this - nothing was deleted, no word moved, and the file is
  regenerated from the document afterwards, so the next sync writes nothing at all -
  and the campaign could not either, since `doc_world` had no named styles to inherit
  from. It has now: `align` None means inherited, the world carries a `theme` no
  request can write (there is none in the API), a read-back names `alignment` only
  where it is set, and the `themed` corpus shape's heading wears the theme's centring.
  `doc_loss_oracle._inherited_findings` (`theme_undone`, severity `loss`) then asks of
  every surviving block whether a field the theme sets for its named style has become
  one of its own that neither side asked for. It has to be *told* what the theme sets,
  which is not pedantry: Docs merges a deleted paragraph into the one behind it and
  hands over its style, so asked about every field it accused 14 of 120 rounds of what
  the document itself had done. With the fix reverted in memory it catches 6 of the
  first 40 `themed` seeds at chain 2 (`test_the_campaign_sees_a_theme_undone`).
  Inheritance itself is still Google's word: the experiment is a themed heading
  through a live sync (`tests/test_docs_live_styles.py`, imported as a .docx since no
  HTML import makes a theme; not run).
- **A mark is on, absent, or *off*** (`doc_ir.MARK_FIELDS`), which is the same question one
  dimension down. A theme that bolds its headings bolds every word of one, so a reader who
  un-bolds a word has made a choice the run says as `bold: False` - and the file could only
  say bold or nothing, so the first source edit to write that block again named no bold and
  handed the word back to the theme. HTML has no tag for not-bold: the file says it as
  `data-off="bold"` on the run (`_run_html`), `_named_defaults` reports a mark the named
  style puts on so `_style_of` can tell an absence from a refusal, and `doc_merge._text_style`
  writes the field whenever the run has a value for it, `False` among them. The oracle's
  question is `styling_restored` (severity `loss`): a word the reader took a mark off wears it
  again. What it asks it of is delicate in both directions - a mark the reader *put on* is
  looked for anywhere in the tab, since a block the sync re-keyed still carries it, while a
  mark taken *off* is asked of that block alone, or the same word in the heading next door
  answers for it (themed seeds 9, 32, 40); and an inherited mark is never counted as one
  somebody chose, or a paragraph that becomes a heading because Docs merged a deleted one into
  it reads as the reader bolding its every word, and the next restyle as losing that (seed
  283). A block that stopped being a heading took nothing off anybody: only an explicit `False`
  against a style that puts the mark on counts (`unmarked_of`). `read_unmark_word` is the
  campaign's reader op; with `_text_style` reverted in memory it catches 13 of 300 `themed`
  seeds at chain 3 (`test_the_campaign_sees_a_mark_the_file_cannot_say_is_off`).
- **Every named style Docs has is a kind** (`doc_ir.NAMED_KINDS`: `TITLE`/`SUBTITLE` ->
  `title`/`subtitle`, `data-style` on the `<p>`; `doc_merge.named_style`), for the same
  reason - `namedStyleType` is in `MANAGED_PARAGRAPH`, so a named style the dialect could
  not spell was one the merge wrote `NORMAL_TEXT` over the first time the source touched
  that block. The importer flattens both (`class="title"` reaches nothing), so
  `carry_unimported` compares the plan's named style with the read-back's and
  `tidy_requests` writes the difference - comparing the style, not the kind, so a list
  item the importer left a plain paragraph says nothing. That also repairs the paragraph
  under a deleted heading, which Docs' merge-on-delete rule leaves carrying the heading's
  style (one of `KNOWN` `lost-key`'s four causes, and an `xfail` until now).
- An equation (several index units, not one) reads as `equation {}`; its LaTeX is only in
  Drive's Markdown export, which escapes no dollar anywhere. `settle` asks the export
  (`doc_sync.equation_latex`), `doc_ir.latex_of` places each equation by the words the
  document puts beside it, and the file shows the LaTeX in the chip; the planning reads get
  the base's LaTeX back in `restore_unreadable`, so an equation never reads as changed.
  Read-only: no request makes one (an OMML `.docx` import does, as the live test does).
- A body that ends on a table ends on an empty paragraph no request can delete:
  `doc_ir._hide_trailer` leaves it out of the IR (`trailer` keeps its span, and the next
  block appended is written into it), and the body's last newline is as undeletable as the
  one in front of a table, so the last block borrows the mark before it too.
- Write-side traps, each pinned by a test: inserted text inherits the style of the character
  **in front of it** (a replacement goes in at the hunk's end, the delete after it); text
  written at a bulleted paragraph's start joins that list (`deleteParagraphBullets` first);
  bullets are created last, after the run styling (the Slides trap again); a block appended
  after the last paragraph writes `\ntext`, not `text\n`, and before that paragraph's own
  edits; blocks added at one index are planned back to front; and the newline in front of a
  table cannot be deleted at all, so a block deleted there gives up the paragraph mark of the
  block *before* it instead (`doc_merge._delete_range` - Docs merges the two keeping the
  first one's style, and a run of deletes passes the borrowing leftwards).
- The merged order is the **document's**: a reader who moved a paragraph keeps it where they
  put it. On top of that the blocks the *source* moved go back where the file has them - the
  complement of the longest common subsequence, so one moved section writes one block - and
  both sides reordering is the document's, with a note. The API has no move: it is a delete
  and a write, so the merged text and styling ride along, a block holding a chip no request
  can create (or a table the document changed) is not moved at all, and `doc_merge.adopt_keys` gives the rewritten block its key back
  (the delete took its named range with it) before the file is regenerated.
- Tables merge cell by cell, where identity is the cell's **place**. A grid the source changed
  while the document did not is written, but not with the words: `insertTable` and the
  row/column requests move every index below them, so they go in a **batch of their own**
  first, the document is read again, the new table is found by what it follows and anchored
  (`doc_merge.anchor_tables`), the base takes the new grid *and only the grid*
  (`rebase_tables`: taking the table as read would swallow the reader's words into the base),
  and the text is planned against the grid the document then has (`doc_sync._write_structure`).
  Rows and columns merge three ways (`doc_merge._table_lines`): one shape on all sides is
  matched by place; otherwise columns by their words (`_column_score`), rows by their cells in
  matching columns, order kept (`_align`), each side against the base, then merged like blocks
  (`_merged_lines`: the document's lines stay, the source's are added after their predecessor,
  one it took away goes unless the document wrote in it). So both sides may regrid, and rows and
  columns may change at once; `rebase_tables` leaves the matching in the base (`aligned`) for
  the pass after. A cell whose paragraph counts differ merges as one text with its breaks in it
  (`_merge_cell`, written against `_joined`). A table the source moved, which the document left
  as the base has it, is deleted and built again blank where the file has it (`structure`).
  A table is written one line per row, so a changed row is a changed line: the breaks sit
  between `</tr>` and `<tr>` and inside the table's own tags, where a parser has nowhere to
  put text, and a row stays whole with its cells, since inside a `<td>` the space would be
  content (measured on `from_html`; the live importer's side of it is what
  `tests/test_docs_live_styles.py` asks, and that file has not been run).
  Deleting one: its own span; with its `lead` when a body opens on it; with the mark in front
  when it ends the body (measured: no stray trailer). `insertTable` splits the paragraph
  its index is in and needs one, so a table goes at the following block's start (and the empty
  paragraph it leaves is swallowed), or, in front of another table, at the paragraph mark
  before it. A ragged table (merged cells) whose grid changed is still reported.
- Styling merges with the words: a source restyle in a block whose words both sides changed
  is written word by word (`doc_merge._restyled_words`: the document's marks, then the file's
  on every word the file also has); only a restyled word the document replaced is reported.
- **What the sync deletes is said first** (`doc_merge.deleted_blocks`): a block the file no
  longer has is one this sync takes out of somebody's document, and the one change here no
  second sync brings back (the words go, the file has not got them either, and the settle
  writes that agreement into the base). `_summary` walked the *merged* blocks, and a deleted
  block is exactly the one that is not among them, so a sync that took two paragraphs out of a
  live document said "1 block(s) from the source, 0 kept from the document, 0 conflict(s)" and
  listed the one rewrite. Asked after every refusal and restoration above it - the same
  question `_goes` asks - and it is the report's first section ("Deleted from the document (no
  way back)") quoting each block's words, a `warning` per block for an agent, and `deleted` /
  `deleted_examples` in the data. Measured on the playground, where the workbench's editor held
  a buffer older than an earlier sync's rewrite: saving it put the pre-sync text back, the next
  sync read that as a source delete, and the merge was right - the file is what the source says
  - while the person had no way of learning what it cost.
- Every sync names the document's **open comments** in its report (`doc_sync.open_comments`,
  Drive's comments API under `drive.file`): a comment lives in Drive, not in the document's
  content, so nothing the merge reads can see one - and a sync that rewrites the passage it
  hangs on answers it by accident. Never written or resolved from here.
- Pictures are `<img src alt width height data-object>` (px; `PT_PER_PX` 0.75): frozen runs a
  sync can create. `insertInlineImage` takes a URL only, so `doc_sync.Stager` imports the batch's
  pictures as a staging document (`data:` URIs, which Drive's HTML import embeds), inserts from
  its `contentUri`s and deletes it (the inserted copy survives, measured); `push` embeds them as
  `data:` URIs. The document cannot say which file a picture came from: the file carries the
  object id, the base the name and byte digest (`restore_pictures`: a regenerated figure is a
  change, a renamed one is not), new ones learn theirs by place (`place_pictures`), and a picture
  a reader inserted is saved to `<stem>.media/` (`fetch_pictures`). `insertPerson`/`insertDate`
  make those chips (rich links refused); a block whose chips the source changed and the document
  did not touch is written again (`rewrite`), never when it holds an equation-like chip. No
  request in the v1 API *changes* an embedded object, so a **size or alt text the source gives a
  picture it already has** is written only where the picture goes in again anyway - a regenerated
  one carries the file's `objectSize`, a merely moved one the document's copy - and an alt text
  never at all; the settle then puts the document's values back into the file, so the edit goes
  twice over, and `doc_merge.unwritten_pictures` names it with the way out (write the picture into
  the file again *without* its `data-object`: a picture with no object id is a new one and is
  inserted at the size asked for, at the price of whatever the browser put on the old one).
- Tabs: the first tab is the file's body, every other one a `<section data-tab title
  [data-parent]>` (`doc_ir.parts`; no `data-tab` = a tab the source asks for). Each tab is its
  own plan and batch, every location/range stamped with its `tabId` (`doc_merge.on_tab`; none
  = the first tab), keys unique per tab. Tabs themselves merge three ways by id
  (`doc_merge.pair_tabs`: `addDocumentTab`, `updateDocumentTabProperties`, `deleteTab` only for
  a tab the document left as the base has it). A new tab's lone empty paragraph and the
  undeletable one in front of a body's first table are hidden like the trailer (`trailer`,
  `lead`) and written into; nothing can be inserted at a table's own index (measured), so a
  block in front of a table goes in as `\ntext` at the previous paragraph's mark. A tab the
  source **adds** goes where the file puts it (`doc_merge.tab_index` / `tab_siblings`:
  `addDocumentTab` takes the index among the parent's tabs and pushes the later ones along,
  so it lands after the nearest tab in front of it the document has - or that this run just
  made, the creates going in file order; a tab the file puts first goes to 1, since the body
  is index 0 and nothing may stand in front of it). It used to land at the end and the settle
  read that back, so the source's placing disappeared twice over. The **order of the tabs
  already there is the document's** - a tab cannot be written from nothing - and a source
  reorder is reported (`doc_merge.tab_order`) rather than dropped and then taken out of the
  file by the settle. A tab *can* be moved (`TabProperties.index` is not output-only and
  `updateDocumentTabProperties` takes any field of it, discovery document 20260427); what is
  unknown is what happens to the tabs it passes, and an index written blind rearranges a strip
  somebody arranged by hand, so the reorder waits for a live measurement (docs/google-docs.md,
  "Remaining risks" 4). The **first tab names itself** in `<meta name="b2s-tab">`
  (`doc_ir.TAB_META`, IR key `tab_title`): it is the body, so it has no `<section>` to say it
  on, and the file's `<title>` is the *document's* name, a different thing - a document of one
  tab has both. `doc_merge.first_tab_title` merges it as the other tabs' titles merge, with one
  difference at the start: a `push` has no base and the import takes the document's name from
  the `<title>` but the tab's from Drive's default, so with no base the file's name is written
  rather than called a disagreement. The oracle judges it (`tab_renamed`), the campaign draws
  it on both sides (`src_rename_tab`, `read_rename_tab`), and taking the both-sides note out
  fails 19 of 200 chained rounds.
  The reader's own two moves in the strip - clicking **+** and deleting a tab - are drawn too
  (`read_add_tab`, `read_drop_tab`, which is told which tabs exist, as `read_unmark_word` is
  told what the theme sets). Both rules hold: a tab the reader added is in neither file nor
  base, so nothing is planned for it and the settle reads it in with keys and ranges of its
  own; one the reader deleted stays deleted, leaves the file and the base, and the source's
  changes to it are a note. Drawing them showed a hole in the *oracle* instead: put the
  resurrection into `pair_tabs` on purpose and nothing objected - the words are all there, the
  round converges, and the tab that comes back has a new id. It is the reader's *decision*
  that is undone, not their content, and this oracle only asked about content
  (`_resurrection_findings`, `tab_resurrected`: the base had it, the read before does not, the
  file still asks for it, and something unknown to the base now says the same words under the
  same name). With it, the injected bug fails 1 of 80 rounds and shrinks to those two ops
  alone; 400 rounds at chain 6 clean without it.
  The hole was one level down as well, and there it is the commoner journey: take out
  `_merge_block`'s clause that a key in the base and not in the read-back is a delete that
  stands, and every block the reader struck out is written again on every sync, in silence
  (80 rounds, not a word). `block_resurrected` asks the same question of a block - but of
  its **words**, not its key, because a move in the browser is a delete and a retype, so a
  dragged block loses its range and is keyed from its words again exactly as a resurrected
  one is; and of the words rather than the text, since a moved block that held an equation
  comes down without it. Forgiving on purpose (a block whose every word still stands is let
  go), or it accuses every move: 7 false alarms of 300 at chain 4 before that, 0 after,
  68 with the clause out.
  And once more at the size of a **row**, which is the last size (below it are cells, and a
  cell the reader emptied is words, not a decision). A row has no key: the merge knows it by
  what it says and so does `_row_resurrection_findings`. It found a defect `_table_lines`
  itself could not show, because `_table_lines` is right: the grid goes in a batch of its own
  and `rebase_tables` then moves the base onto it, which takes a row the *reader* deleted out
  of the base - and with it the only thing that said which of the **file's** rows it was, so
  the round after the regrid read that file row as one the source had just added and put the
  reader's row back, twice-applied and unmentioned (seed 63138, chain 4). The merge now
  carries what it knew instead of guessing again: `_table_lines` gives back the file lines it
  has settled as not in the grid, `_rebased_table` puts them in `aligned`, `_merged_lines`
  counts them as known, and each round unions its own settlement with the inherited one.
  300 rounds at chain 4 and 200 at chain 8 clean; taking it out fails 1 of 200 at each depth,
  which is thin, so the defect is also pinned by a hand-built test. The row check cost the
  other two a lesson in forgiveness: `WORD` is `\S+`, and the harness's drag lands where the
  reader dropped it - inside the full stop before (`section.` -> `section..`) or with a
  dropped chip's gap closed (`harbour grace` -> `harbourgrace`) - so `stands_elsewhere` looks
  for the words *inside* the tab's text, `joined_differently`'s forgiveness at block size.
  It costs nothing: 34 of 200 rounds still fail with the block delete broken on purpose.
  `tab_resurrected` needed the same at its own size: a tab the *source* freshly asks for is
  word for word the shape it accuses (two new empty tabs say as much as each other), and the
  campaign's small vocabulary eventually has two `add_tab`s pick one name (chain-8 seed
  65370). A `<section>` with no `data-tab` is a tab the document never had, so `_fresh_asks`
  counts them and each answers for one new tab; with that in, the injected resurrection
  still fails 26 of 200 rounds at chain 4. And the forgiveness was aimed at the wrong text:
  it asked for the **base's** words, and a base is what the document said one sync ago, so
  everything the reader's own hand has taken out of that block since is missing from it by
  right - a chip no retype carries, a word they went on to delete (chain-4 seed 66195: the
  source gave a paragraph a person chip, the reader dragged the paragraph, the chip stayed
  behind, and the settle keyed the same untouched block from its words again). The question
  belongs on the block carrying the key **after** the sync: if what stands there now stood
  there before it, nothing came back. 44 of 200 rounds at chain 4 with the block delete
  broken on purpose, 91 findings.
- The **document's name** is the file's `<title>`, and a Google Doc's title *is* its name in
  Drive: no `batchUpdate` request writes one, so `push` named it at birth and nothing said it
  again. `doc_merge.document_title` merges it three ways (the file alone renamed it -> written;
  the document alone -> the file follows at the settle; both -> the document's, with a note; a
  base too old to hold a title -> the document's, saying why) and `doc_sync.rename_document`
  writes it through `drive.files.update` - the plan's `rename`, not a request; a refusal fails
  nothing and is said. The file and the base then say the name *written*, not the one the next
  read gives (`settle(renamed=...)`): `documents.get` need not have caught up with Drive, and
  taking its word would undo the rename with the base agreeing, so nothing tried again.
- **Words the file says outside every block stop the sync** (`doc_ir._Reader`'s `stray`,
  `doc_sync.stray_refusal`): a block is a `<p>`, an `<h1>`-`<h6>`, an `<li>` or a `<table>`,
  and text outside one is read by nothing - it reaches the document through no request at
  all, and the settle then writes the file again from the document and takes it out of the
  file too, dropped twice over while the run reports `0 requests` and "the two sides already
  say the same thing", which is true of everything the reader *could* read. Measured on the
  playground (2026-09-21): `<it id="item:5">XX</li>`, one letter, synced three times and said
  nothing. `read_file` is the one door `push` and `sync` share, so both refuse, quoting the
  words and naming the tags a block can be (the agent journeys answer `bad_request` with the
  same sentence); the white space between the dialect's own lines is layout, not words, and a
  `<script>`/`<style>` body is markup. **And the same rule one level down**, which the
  dialect's own table makes reachable: a table is the one block written over several lines -
  one per row - so moving it in the file moves a *run* of lines, and moving one of them leaves
  a `<tr>` with no `<table>`, or a `<table>` the file never closes. The words are inside `<p>`
  tags, so `handle_data` saw a block and said nothing while the row they were in was dropped:
  the sync read the table as one the source had deleted and **took it out of the document**,
  rows and all, reporting `0 requests` for words it never read. `_Reader._lost` (a cell, a row)
  and `finish()` (a table still open at the end of the file) put them in `stray`, so the file
  is refused by the same door. Found the other way round, by a live test whose own helper moved
  one line of a multi-line table - a mistake a person hand-editing the file makes as readily.
- **A request with no range still has to name its tab** (`doc_merge.TABS_CRITERIA`): three
  requests carry no `Range` and take `tabsCriteria` instead (`deleteNamedRange`,
  `replaceAllText`, `replaceNamedRangeContent`; the sync sends the first). The reference says
  an omitted criteria applies to every tab, so `on_tab` never stamped it - and the live API
  answers "No named range with ID kix.…" for a range on any tab but the first (measured
  2026-09-22). A refusal throws out the whole batch, and the batch a `deleteNamedRange` is in
  is the one that *plants anchors*, so on a second tab a drifted or orphaned range could never
  be repaired at all and every anchor that tab was to be given went down with it. Found by the
  live tabs test; `doc_world._do_deleteNamedRange` models the measured rule now (an id with no
  criteria reaches the first tab only), so the offline campaign would catch it too.
- **A read is made again through a blip, a write never is** (`doc_sync._read`): the Slides side
  has retried since the beginning (`gslides.execute`) and the Docs side called `.execute()`
  directly everywhere, so one `ssl.SSLEOFError` on `documents.get` (which ended a live tabs run
  on 2026-09-22) or one 429 ended the sync - and where it ends matters, since after the write
  batch the document is written and the file and base are not, so the next run reads this
  sync's own changes as the reader's work. Four tries with backoff and jitter on Google's
  transient codes (`gapi.is_transient`) and on `OSError`, over every read there is: the
  document, the comments, the base file and its metadata, the markdown export an equation's
  LaTeX comes from, the HTML export a backup takes. **No write goes through it**, and that is
  correctness rather than caution: a `batchUpdate` whose answer was lost may well have been
  applied, so sending it again applies it twice - `requiredRevisionId` is what makes a write
  safe. A test asserts the counts and that `send`'s source never names `_read`.
- What the file cannot carry is reported too (`doc_sync.limits`): a picture file that is not
  there. And **what the dialect does not model is named** (`doc_ir.unmodelled`, `_NODES` = the
  reader's own map of `documents.get`): the convergence check is measured on the IR, so it
  proves the IR round-trips and says nothing about a property the IR never looked at - such a
  property survives an edit (a request names the fields it writes) and goes when its block is
  written again from nothing. The walker reports every leftover path with a count and an
  example (`checks.lost_ink`'s question, one dimension down); `adopt` and `push` print each
  one (`doc_sync.unmodelled_notes(full=True)`), a sync the count and the commonest three.
  Real documents turn up page structure (`documentStyle`, headers, footnotes,
  `sectionBreak`, positioned objects), paragraph tab stops/`direction`/`borderBetween`/
  the widow-orphan and keep-lines flags,
  a table's column widths and `tableCellStyle` (the ragged-table limit under
  its real name), a picture's crop/angle/brightness, a list's `startNumber` - and, of the
  document's theme, a named style's marks and alignment (its face and measures *are* read).
  Two tests pin the map: a fixture holding one of everything, so a property Docs adds later
  fails rather than passes in silence, and every fuzz corpus shape walked through `doc_world` -
  written apart from the map - which reports only the section break a body opens on, which is
  also the campaign's blind spot named out loud. A count is not an address, so the same walk
  runs **a block at a time** (`doc_ir.unmodelled_in` one structural element,
  `unread_blocks` every block of every tab, most laden first) and `adopt`/`push` name them by
  the words a person sees ("the paragraph 'Why this matters' carries
  paragraphStyle.borderBetween; rewriting that block through the file would drop it",
  `doc_sync.block_risk_notes`, 8 blocks x 4 properties), while a sync says how many blocks
  carry one: a section break and anything outside the blocks are left out, since no rewrite
  reaches them. It is a risk, not a loss - a block nobody rewrites keeps all of it. The
  `doc_adopt` journey hands the same lines to an agent as warnings. Once the plan exists the
  risk has an answer (`doc_sync.rewrite_losses`): of those blocks, which is *this run* about
  to write again from nothing? Usually none, and the report says nothing; otherwise it is the
  one line that is a loss and not a caution, and it is there before the write (a `--dry-run`
  above all). A block rewritten or moved is named - a move is a delete and a write - a block
  restyled or reworded is not, since a request names the fields it writes and no field the
  merge owns is one nobody reads, and a block the source deleted is not either.
- Fuzzed against a loss oracle, as the Slides sync is (docs/google-docs.md, "Proving nothing
  is lost"): `devtools/doc_loss_oracle.py` judges one sync from the two read-backs, the base
  and the report - did anything the *reader* put in the document disappear without the report
  saying so? (Its docstring defines that; no Google call.) `tools/fuzz_docs.py offline --rounds
  N [--chain N]` pushes a corpus shape, edits both sides and syncs, then checks a second sync
  writes nothing; ~150 rounds/s at chain 1, 20 at chain 4, with a shrinker. `doc_world.py` is a
  reference applier that holds a document as Docs holds it and applies the real requests under
  Docs' index rules - **including throwing out the whole batch when one request is refused**,
  the failure an applier that merges *state* can never see. `collide` (a source op changing
  exactly what the reader just changed) and `--chain N` are the two things that make it find
  anything; coverage is printed, and it is what showed that the whole dialect past bold and
  italic was undrawn, so the ops now draw a face, a size, small caps and the six paragraph
  measures on both sides (`RUN_MARKS`, `PARA_MARKS`, `READER_FACES`, `READER_MEASURES`) - the
  case that matters is a reader choosing a face and the source then restyling that block.
  Ten defects found by the first campaigns and six more at fresh seeds once `KNOWN` was
  empty (below), each pinned by a test in `tests/test_doc_fuzz.py` (`xfail(strict=True)`
  while it stands, a plain test once fixed) and the ones still standing described in
  `fuzz_docs.KNOWN` (let through by default, `--strict` fails on them): a TOC treated as an
  ordinary block, which kills the sync outright; a table the source dropped deleted however
  much the reader typed in it; one block's key landing on another; a block
  reworded *and* moved losing the reader's styling. A fixed entry goes out of `KNOWN`, or it
  would swallow the next defect that looks like it. **All ten signatures are at zero and
  `KNOWN` is empty**, so any finding at all fails the campaign; closed by four groups of
  fixes: five ways of losing a
  block's identity - which is the root of the worst of the rest, since a block the
  merge cannot recognise is one it deletes as dropped by the source - two ways of losing the
  reader's content outright, three ways of killing the sync where it stood, and one place in
  a document where Docs lets nothing be written at all (several closed one of each).
  (1) `inherit_keys`
  matched every block of the file again by its words although the file had just named them
  all with its own `id=`, so two blocks that read alike swapped keys; a key the file asserts
  is now never matched again, which is the rule `doc_ir.key_blocks` had written down all
  along. (2) `settle` adopted and keyed one part at a time and `key_blocks` recurses into the
  tabs, so the first part's keying named every later tab's blocks after their words, before
  those tabs reached `adopt_keys` - and a table, anchored in its first cell
  (`doc_ir.anchor_span`), loses its named range whenever the source rewords that cell, so it
  settled as `table:<its new first word>` with file, base and document all agreeing on a name
  the file never gave it; one source op, no reader. `doc_merge.settle_keys` does both in two
  passes and is shared with the harness, which had the same bug because it is a copy. (3) the
  paragraph under a deleted heading, repaired by the named-style settle above. (4) a row
  delete taking with it the first cell the table is anchored in: the structural batch left the
  table with no named range and `anchor_tables` only looked for tables the batch had *built*,
  so it settled under a name made from its new first word, the file's key read as gone and the
  source's own cell edit was written nowhere (`structure` now gives a regrid the same `after`
  a new table gets). (5) `adopt_keys`, the one thing that gives a block written from nothing
  its key back, matched a block's shape *and* its words, so the blocks a write mangles were
  the ones it could never adopt: Docs merges two paragraphs keeping the first one's style, so
  a delete hands the block after it the shape of the one that went (an item under a deleted
  paragraph comes back a plain paragraph, a heading under a deleted subtitle a subtitle), and
  the repair that would put the shape back is itself keyed by the key this restores, so the
  two held each other up (`doc_merge._adopt_by_words`, a second pass on the words alone taken
  only where one free key and one unkeyed block say the same thing; seeds 7034, 7048). Its
  other half is no identity at all: an item and a paragraph are both NORMAL_TEXT, so writing
  the named style back is blind to the bullet - the thing a delete takes away most often -
  and the settle then wrote that plain paragraph into the file, the source's own list quietly
  one item shorter (`carry_unimported` reads the bullet, `restore_bullets` writes it).
  (6) the one that was not identity: a table the source dropped was deleted
  however much the reader had typed in it, because the test for "edited in the document" was
  `block_text`, which is empty for a table (`doc_merge._edited` reads the cells, the grid and
  the frozen runs) - and the same decision now keeps a block holding an equation, a dropdown or
  a TOC no request can make again, which `_merge_block`'s rewrite path had always refused to
  destroy while a plain delete did it silently. (7-9) the three that killed a sync outright,
  all one mistake: **Docs' index rules are about structural elements, not about tables**
  (nothing inserted at one's own index, the newline in front of one undeletable, a body
  neither opening nor ending on one without an empty paragraph beside it, one deleted by its
  own span) and every test for that read `kind == "table"`, so a table of contents was an
  ordinary paragraph to the planner - `doc_ir.STRUCTURAL` is the set, `doc_merge._structural`
  the test, and `_hide_trailer` hides the lead and trailer beside a TOC too; and an empty
  paragraph *between two tables* can be deleted in no way at all (its own mark is the one in
  front of a table, the block before it is a table with none to lend), so `_delete_range` came
  back with a range of length zero and Docs refused it - Docs wants that paragraph anyway, so
  `doc_merge.restore_undeletable` puts the block back into the merge, round by round since
  keeping one changes what the next delete may take, and the report says why. (10) and the
  one that is no mistake in the arithmetic but a place where Docs lets nothing be written:
  between two tables there is no paragraph, so the mark a block in front of a table borrows
  does not exist and the index it borrows is inside the last cell of the table before it - a
  table added there was built inside the old one and each re-plan built another, an ordinary
  paragraph *moved* there was deleted from its old place first, so the move destroyed it.
  Both are refused now (`_new_table_requests` asks for nothing, `doc_merge.refuse_nowhere`
  leaves the paragraph where the document has it) and the report says why; Docs keeps a
  paragraph between two tables anyway, which is the `between_tables` shape and where the
  arithmetic is right. At one seed,
  200 rounds at chain 8: `toc-block` 10 -> 0, `toc-table-split` 4 -> 0, `empty-delete` 6 -> 0,
  `dropped-table` 17 -> 0, `dropped-frozen` 8 -> 0, `crossed-frozen` 22 -> 0,
  `table-in-a-table` 1 -> 0, `lost-key` 34 -> 2,
  `crossed-delete` 2 -> 1, `moved-styling` 2 -> 2, and the same at a second seed and at
  chain 4; then `lost-key` 2 -> 0 and `moved-styling` 2 -> 0, leaving `crossed-delete` alone
  in `KNOWN` (300 rounds at chain 8 and 600 at chain 4 under `--strict`: nothing) - and it
  went 1 -> 0 with `inherit_keys`, the first of the five, so its entry went too: it had
  stayed on after its fix "to catch whatever else can reach the signature", which is
  backwards, an entry here being the one way to make sure nothing is caught.
  `lost-key`'s last two: an empty paragraph is all mark, so its named range *is* its mark,
  and "\ntext" written at that mark is handed the range (an insert at a range's own first
  index pushes it along), so the empty paragraph's key rode onto the block that was written;
  no index both appends after an empty paragraph and leaves its range alone, and reading the
  key back off the wrong block was tried in two widths and cost that round its convergence.
  The range is planted again instead, in the same batch (`doc_merge.requests`, `REPLANT`:
  `deleteNamedRange` then a fresh `createNamedRange` on the mark, after the appends there and
  before a block inserted in front; `doc_world` takes `deleteNamedRange`). A reader's chip or
  word put into an empty paragraph pushes its range the same way, so `apply_keys` records
  where a range is (`block["range"]`), `doc_ir.replant_requests` names the drifted ones,
  `name_requests` plants them back at the settle and `structure` heads its batch with them -
  the batch that builds a table swallows the mark before it, which carried the chip
  paragraph's range, and the moved table is found again by that block's key (seed 296).
  A range also *stretches*: text written inside one grows it, so a reader who presses Enter
  in the middle of a paragraph (or drags a block into it) leaves one name over both halves -
  invisible while both stand, since `apply_keys` gives it to the first, but when the source
  drops that first block the delete takes only its own span and the stretched range settles
  onto the second block's words, whose own key goes. `replant_requests` plants a range that
  ends past its block again, as it does one that starts too late; a range may end *short* of
  its block, text typed at the mark falling outside it, so only `here[1] > planted[1]` is
  wrong (seeds 279 and 361 at chain 4, found once `read_unmark_word` changed which seeds draw
  what; both fail identically at the commit before it).
  And a range is destroyed *with its text or not at all*, which leaves one delete taking no
  text of its own: a block in front of a table gives up the previous block's mark and keeps
  its own (`_delete_range`), and that mark is exactly where an empty paragraph's range lives,
  or a drifted one. Docs keeps it, the document goes on saying the block is there, and since
  the block was *moved* its range is planted again where it went - two ranges of one name,
  the stale one sitting where the next block written will be, which the sync after then hands
  this key while the block that owned it is renamed from its words (chain-8 seed 41000, one
  round in 500). `doc_merge._orphan_range` names it in the delete's own batch.
  The mirror of the stretch is a range that *outlives its block*: a reader backspacing at the
  start of a paragraph (`read_join_blocks`, with `read_split_block` the two commonest edits
  after typing, which the campaign reached only through a dragged block) makes Docs merge the
  two keeping the first one's style, so both ranges are in the one paragraph left. `apply_keys`
  gives the block the range that starts in it and the loser waits - until a source edit
  rewrites the winner's words, whose delete takes the winner's range, and the block comes back
  under the swallowed paragraph's name while the key the file asserts names nothing (chain-4
  seed 70140). `apply_keys` records what no block took (`ir["orphans"]`) and
  `doc_ir.orphan_requests` deletes those at the head of `name_requests`: one block, one name.
  The settle is one write too late when the same sync rewrites the survivor: the write kills
  the range those words carried while the orphan (on a picture, say) lives on, so the
  read-back names the block after the paragraph that was swallowed and `adopt_keys` has no
  key to give back - the deletes head the write batch too (`doc_merge.requests`; they move no
  index, and a sync that writes nothing still leaves them to the settle). Chain-8 seed 77064,
  2 of 250 rounds at chain 8 without it. `read_paste_block` (a pasted duplicate: two blocks
  saying exactly the same thing, the degenerate case of identity by words) has found nothing
  in 400+250 rounds and is kept for that, with a test pinning what it walks over; it moved
  every draw, which is how the three defects above were reached.
  **Columns, and a third judge.** Coverage said it plainly: `insertTableRow` was planned in
  every run and **no column request ever was**, so `_column_score` and the column side of
  `_align`/`_merged_lines` had no measurement at all. Drawing them (`read_add_column`,
  `read_delete_column`, a `src_regrid` that draws a column half the time) passed 1,700
  rounds - and so did pairing columns **by place**, the straw man, because both judges are
  blind to it: the oracle asks about the *reader's* work and a wrong matching never marks a
  line `gone`, so it errs towards keeping; and `rebase_tables` writes the matching it used
  into the base, so the second sync reads the table the same way and converges. A merge can
  be wrong and stable at once. `fuzz_docs._arrived` is the campaign's own judge (where the
  oracle's docstring always put it): a table the reader did not touch must come out at the
  grid the file asks for, and a cell the source rewrote, in a table whose words the reader
  did not touch, must be somewhere in it afterwards - the second being the one that sees a
  wrong column (with the reader's hands off the grid the two pairings agree), now that
  `src_edit_cell` writes a distinctive token. Place pairing then fails 3 of 200 at chain 6.
  It found a real defect at once: a table the source **regrids and moves at once** was built
  again at `_size` of its own rows, which `_merge_table` leaves as the *document's* when
  there is a regrid to write - the source drops the header row, moves the table, and it
  comes back `[['1','2'], ['','']]`. `_built_size` counts the lines the matching settled.
  Nine findings over 2,100 rounds, shrunk to three source ops and no reader at all (seeds
  88033, 88075); with the fix, 2,800 rounds at chains 4/6/8 clean, the judge asking ~3,100
  questions.
  **The same question for a paragraph** (`_words_arrived`) is plainer still - the reader left
  the block word for word as the base has it and the source reworded it, so there is nothing
  to merge - and nobody asked it either (~1,300 questions per campaign). It compares `_says`
  (own words + `frozen_marks`), because a chip's **face is the document's to draw** (the file
  says `Grace`, Docs renders `grace` off the address), and its kind is `wording_lost`, since
  `words_lost` is the oracle's own and asks the opposite question. It found `adopt_keys`
  matching the plan to the read-back by a **dictionary of words**: a reader's pasted copy
  standing in front of a block the same sync rewrote (a rewrite deletes the block's named
  range) took the key the file had carried since the push, the source's chip landing on the
  right block and settling under a name nobody asked for - file, base and document agreeing,
  nothing of the reader's gone, so neither other judge could see it. `_adopt_in_order` aligns
  the plan with the read-back and pairs inside every matching run: on the **words**, not the
  shape (Docs merges two paragraphs keeping the first one's style, so a heading the source
  moved *and* restyled came back under neither pass - seed 96300), and only where both
  sequences hold the same number of blocks saying that thing (one plan block against three
  identical ones has an alignment too, and it is a guess). Without the pass, 2 of 700 rounds
  at chain 6 and 1 of 1,200 at chain 4; without the pass *and* without the judge, those 700
  pass in silence. Two oracle changes came with it: `_twin_unmarks` was forgiving a symptom of
  this defect and is **gone** (3,750 rounds at chains 4-8 without it, nothing found - an
  oracle that forgives what no longer happens is a blind spot waiting), and `_pared_down` now
  lets the cut take the joiner with it (`soft\xadhyphen` minus `soft\xad` is `hyphen`, not only
  `\xadhyphen`; seed 94030, 1 of 700), guarded by the narrow leftover not standing there too.
  A body may not end on a table, so the paragraph after a final one keeps its mark however it
  is deleted (`_delete_range`: its words go, an empty paragraph stays where it stood) - but
  the append index came from the last block the sync *keeps*, which is then the table, and a
  table's own last index is inside its last cell. A block the source added in the same step as
  it dropped that paragraph was written into the table, swallowing it and its named range
  (chain-8 seed 189). That empty paragraph is a trailer like the one a body ending on a table
  already has (`requests`: `left_empty`), and the first block appended is written into it.
  `moved-styling` was `_retext` folding the stretch between two frozen runs into its first
  writable run: it pairs the document's words with the merged text now and gives each its own
  styling (`_runs_from_styles`). Its wordless signature hid two more: a block written from
  nothing inherits the styling of the character in front of it (Docs' rule: one moved under
  an underlined heading came out underlined), so `_style_requests` names every managed field
  on every run; and a word the reader styled that the source rewrote is a loss that is
  right, now said in the report (`doc_merge.reader_styling_gone`). Five of the harness's own,
  found at chain 8 and pinned by tests that fail without the fix: `doc_world` shifted no named range when a
  table row was deleted, so after a source regrid every key below the table slid onto the block
  above (seeds 5099, 5167); the oracle accused a `\S+` token each side had edited one half
  of - a soft hyphen joins two words, and only what the *reader* added has to survive
  (`joined_differently`, seed 5130); and the whole of `crossed-frozen` was the oracle
  misnaming pictures, twice. A picture is the file it shows, not the object id a rewrite
  replaces - but the file is not stable either, since one a reader inserted has only a
  `contentUri` until the settle saves it (`oracle.image_names` takes every name, 22 -> 13);
  and two pictures pasted from *one* url share a name that says which picture it is, so the
  one that went paired with the one that stayed and the survivor was named as lost
  (`oracle.telling_names` keeps the names no picture beside it carries, `pair_images` matches
  on those first and on anything at all second so two copies of one file still pair off, and
  the excuse "the source took it out of the file" asks the telling names too; 13 -> 0, every
  one of the nine pointing at a picture still standing in the document). And taking that
  entry out uncovered the fifth, at two other seeds: chips are counted by value over the
  whole tab and the excuse is that the file still holds one of that value, which credits a
  chip the source added *somewhere else* against the one it is deleting here - a source that
  drops the block its person chip is in and puts one of the same address into another block
  read as no change, then as a loss when that block turned out to be one no request can
  write (`oracle._source_dropped`, only for a block the reader left as the base has it,
  since a chip the reader put in is one the merge keeps the whole block for).
  **What the empty `KNOWN` was worth**: the first run at seeds nobody had used (1200 at
  chain 4 from 90000, 400 at chain 8 from 40000) came back with six findings, one of them
  the `block_gone ... though the file still names it` that the departed `crossed-delete`
  covered word for word. Four losses in the merge, two the oracle's (so seven of the
  harness's against thirteen of the sync's), each now pinned by a test that fails when its
  mechanism is put back: (a) `anchor_tables` went through `shaped` once, so a table moved
  behind one the *same batch* regrids - a regrid deletes the row the table is anchored in,
  which is why it carries an `after` - was skipped before its anchor came back and nothing
  looked again; it stayed blank and unkeyed, the re-plan read the file's key as a table the
  reader had deleted, and a whole table of the source's went nowhere (seed 90190; a fixed
  point now, anchors already there going first). (b) A table the source *adds* in front of
  one it *regrids* shares its anchor, and `shaped`'s order decided which was which although
  `insertTable` puts the blank one in front: the keys came out crossed, the base took each
  other's content, and the next round "moved" the table it had just named, emptying the
  real one (seed 40204; `_blank_table` asks the words first, `shaped`'s order is the
  tie-break it always was). (c) Both places that take a move back (`refuse_nowhere`,
  `restore_undeletable`) cleared `moved` and left the block at the *file's* position in
  `merged`, but every index comes from a block's span and a span says where a block is: the
  move of a table in front of such a paragraph was written at the paragraph's old index,
  where that table already stood, so the document came back unchanged, each round planned
  it again and the three `_write_structure` allows ran out with the table blank and an
  empty paragraph per attempt (seed 40344; `_put_back`). (d) `_edited` - the test that
  outranks a source delete - read the words, the grid and the frozen runs, never the marks,
  so a block the reader had only *styled* read as untouched and the delete took the bold
  with it in silence (seed 90175; `_styled` compares by the stretch a style covers, so
  splitting a run to bold a word and joining it again are both nothing). And the oracle
  twice: a reader deleting one of two soft-hyphen-joined words leaves a token the base does
  not have, which read as one they typed, so the source rewording the other half read as a
  loss (`_pared_down`, seed 91197); and `styling_restored` asked whether an un-marked word
  wears its mark anywhere in the block, so a second occurrence the source had just appended
  answered yes while the reader's own stood untouched (asked by occurrence now, seed 40254).
  All four campaigns clean under `--strict` afterwards.
  **Then deeper rather than wider**, since green at four settings is green at four settings:
  2000 rounds at chain 6 from 500000 (six edit-and-sync steps per round, not one) found five
  more in four signatures, three of them `block_gone` on a table again. (a) `insertTable` in
  front of an ordinary block leaves an empty paragraph, which `_new_table_requests` removes
  by deleting the mark of the block *before* - and when that block is itself an empty
  paragraph, which is all mark, the delete covers its named range whole and it comes back
  unnamed. Nothing by itself (the settle re-keys it) except that the new table is found
  between the batch and the settle *by that key*: no anchor, the blank table settled under
  its own emptiness, the re-plan read the file's key as a table the reader had deleted, and
  the source's table was gone (seeds 501271, 501871; `doc_merge._swallowed`, `_after_key`
  looks past it). (b) `_moved_keys` reads the file's order against the **base** while the
  merged order is the **document's**, so a block the source moved can land exactly where the
  document already has it - a delete and a build from nothing for no gain, and for a table
  destructive twice: built blank with its words waiting for the next pass, which asks for the
  same move again until the three rounds are spent (seeds 501429, 500077; a move whose two
  ends are one place is no move). (c) Found by sweeping every reordering of a five-block body,
  written to find a scenario for (b), and the worst of the four: the index a block is appended
  at is the mark of the last block the sync *keeps*, and a block the source moved away is no
  more kept than one it deleted - move everything after a table to somewhere in front and the
  last kept block is the table, whose own last index is inside its last cell, so the moved
  paragraph was written into the table and it swallowed it. This is chain-8 seed 189's defect,
  fixed a day earlier for deletes only; one condition covers both halves now. (d) The oracle
  once more: `WORD` is `\S+`, so a reader's full stop against the `1` in a cell makes the token
  `.1`, which the base has not - but its only word is the base's, and the source rewriting that
  `1` takes the stop with it (seed 500249; `_dressed_up`, exact like `_pared_down` and asking
  besides that the base word be gone from the tab - since **retired**, the drag that pushed
  that stop against the `1` having been the harness's own bug).
  **Deeper again, at fresh seeds** (800 rounds at chain 6 from 970000, 600 from 980000): seven
  findings, three the merge's and four the oracle's, and the merge's three are one wound seen
  from three sides - **the one a reader makes**. A table is anchored in its first cell, so a
  person who deletes its first row in the browser takes its named range with them; every other
  repair in `doc_merge` is for a range one of *our own* writes destroyed and nothing was looking
  at this one, so the merge read the key the file and the base both name as a table the reader
  had deleted and the source's edit to a row they had kept went nowhere (`recover_tables`, only
  where nothing is in doubt: `TABLE_MATCH`, `TABLE_MARGIN` clear on both sides, and asking the
  **words** - `_match_text`'s " | " between every cell is most of a small table's characters, so
  a blank 2x2 `insertTable` had just built scored 0.55 against one with four words in it and
  took its key). `insertTable` splits the paragraph it goes into and the range stays with the
  half *after* the table, so a new table goes in front of the block the plan anchored it on, and
  `anchor_tables` looking only forward handed the beheaded next table's identity to the one the
  batch had built (seed 970567; it looks backwards afterwards). And a tab the source deleted and
  the document changed is kept, which is a note and no pair at all (`pair_tabs`), so nothing
  plans it and the settle had no planned blocks to adopt from: the beheaded table there settled
  under its surviving first word, file, base and document agreeing on an identity the file never
  gave it (seeds 970705, 970711; `settle_keys` asks the base too and `name_requests` plants the
  range back, so the repair reaches the document and not only the plan). Four false alarms, the
  first made by that fix - a table the reader beheaded reads as one the reader *made*, so the
  source's own cell edit read as a loss (`_tab_findings` asks `recover_tables` first) - and
  three in the styling: an un-bolding the **base** already records, on a word the file itself now
  marks, is a source restyle the merge's own rule gives to the source (970228); a word is what a
  reader sees and not what a run holds, so the coloured token `\xadvellum` coming apart into
  `\xad` and `vellum` when the merge wrote the source's strike around the reader's typed word was
  no loss (970528, `_under_words`: a word wears what every character of it wears); and styling
  counted in whole *sets* of marks made the source's strike hide the reader's colour, so it is
  one mark at a time now. The fourth: two copies of one picture with one dropped by the source -
  which survived and which the file asks for are told apart by object ids the survivor need not
  keep, so name-matching put the two roles on different copies and named the one that went; the
  question is how **many** the file asks for, not which (980193). Clean afterwards at 970000 and
  at three fresh seeds (600 at chain 6, 400 at chain 8, 900 at chain 4) under `--strict`.
  **The third read, and the style a delete hands over** (500 rounds at chain 10 from 992000, 700
  at chain 6 from 993000, plus a seed left from the round before). A sync that changes a grid
  writes it on its own, reads the tab again and hands what it finds to `anchor_tables`, which
  names a table whose range one of *our own* requests destroyed - and that third read was the
  one place `recover_tables` was not run, so the free table it found was the one the reader had
  beheaded and the regridded table's key went onto it: the reader's rows stood under the source's
  table's name, the real one settled as `table:empty`, the file's key was gone (993608;
  `doc_sync._write_structure` and the harness copy recover first and `plant_ranges` puts the
  range back. Hard to see because `anchor_tables` prefers a blank table for a new one and a
  worded one for a regrid, so the crossing needs a regrid that leaves the table blank). And Docs'
  merge-on-delete reaches the **measurements**, not only the named style and the bullet: a
  paragraph the reader centred, deleted by the source in the same batch, leaves the block behind
  it centred of its own - although the merge wrote `alignment` named-and-unset one request
  earlier, following a named style being the whole point of a theme - and that block's own source
  restyle, written in the same breath, is handed back the spacing of the paragraph that went
  (912452). The settle writes the plan's **whole** paragraph style back on a block this run wrote
  whose style the write did not leave as the plan asked (`_unwritten`, `paragraph_written`),
  rather than the difference: the repairs read each other's work otherwise, the named style
  deciding what "inherited" means - a block still read as HEADING_1 under a theme that centres
  headings reports no alignment of its own, and the centring shows only once `named` has written
  NORMAL_TEXT back, which is in this very batch. It is the one thing in the settle that takes
  styling away, and the narrowing is what keeps it safe: a block nobody wrote is left alone
  (`test_styling_the_plan_does_not_ask_for_is_never_taken_away` fails without it). It heals a
  *plan* that pins an inherited alignment too, which is the defect
  `test_the_campaign_sees_a_theme_undone` puts back on purpose, so that probe opens both doors
  now - what it measures is the oracle's reach, not which of our mechanisms is broken. Clean
  afterwards at 993000 (700 at chain 6) and 995000 (400 at chain 10) under `--strict`; 500 at
  chain 8 from 994000 came back with three, which were two signatures, one each side of the
  line. **A picture's names do not say which copy it is; its object id does** (994410): the
  source adds one figure twice, the reader deletes one of the blocks in the browser and the
  source drops the other, and the copy still standing paired - by their shared digest - with
  the file's entry for the copy the reader had already taken away, so nothing was excused and
  the picture the source itself gave up was named as lost. The excuse pairing asks for the ids
  (`pair_images(..., ids=True)`): the file's are the document's own, the settle regenerating it
  from the document it wrote, so a picture the file names by id *is* that object and one it
  names by file alone is one the source has just added; the other pairing still goes by name,
  since a rewrite gives a picture a new id. **A block kept because nothing can move it follows
  nothing that moves** (994424): an empty paragraph between two tables can be deleted in no way
  at all, so one the source dropped is kept where the document has it, and `_after_live` put it
  back into the merged list behind the block in front of it there - the very table the source
  was moving away. It then stood as that table's own next block, so `_insert_index` read the
  table's new place off a span one character behind where the table already was: deleted and
  built again in its own place, blank, three passes running, its words nowhere and the file's
  key gone with nothing in the report. Then 2,700 rounds clean under `--strict` (800 at chain 4
  from 998000, 500 at chain 8 from 996000, 400 at chain 10 from 997000, 700 at chain 6 from
  991000, 300 at chain 12 from 999000), `KNOWN` still empty.
  **And the same question about a block's look** (`fuzz_docs._styling_arrived`): the reader
  left it word for word as the base has it, the source restyled it, so what the file asks for
  must be what the document says at the end. It asks of the marks (`_worn`: the run styling as
  styled stretches, adjacent alike joined, so how many runs the text was cut into is the one
  thing it cannot see) and of the paragraph (`_shape`: the named style and every measure the
  merge owns), 118 and 233 times over 400 chain-4 rounds. Neither other judge can: the oracle
  asks about the *reader's* work, and convergence is satisfied by any self-consistent reading,
  the settle regenerating the file from the document it wrote. One defect in each of the three
  places there are. **The harness**: `dict(para)` is shallow and `measures` is a dict of its
  own, so a paragraph split off another shared it - every appended block is written `"\ntext"`,
  which is a split, so one `updateParagraphStyle` set the line spacing of half the body and the
  next clearing of a measure cleared it everywhere, leaving the document self-consistent
  (`doc_world.copy_para`; seed 110149, 3 of 200 at chain 4). **The merge**: `styles_of` counts
  run boundaries on purpose, but a reader inserting a picture or a chip splits a run and puts a
  frozen one between the halves - content the text merge carries, saying nothing about marks -
  and that read as the reader restyling the block, so the merge decided both sides had and
  dropped the source's marks (`doc_merge.marks_of` is the question "did somebody change what
  these words are marked with", frozen runs out and alike stretches joined; seed 110265, 6 of
  400 at chain 4 and 10 of 400 at chain 8). **The report**: where both sides set one paragraph
  or restyle one block the document wins, as everywhere, but it won in silence - the words have
  raised a conflict on every clash since the beginning - so two notes now, and the second is
  why the first defect had to be fixed in `marks_of` and not in the report: the judge forgives
  what the report names, so with `styles_of` back *and* the note in place the campaign passes
  while the report tells the person their document restyled a block it never touched. And twice
  the judge's own, both normalising the file's side less than a read normalises the document's
  (`doc_ir._named_defaults`): a `bold: False` the source left in the file after retitling the
  heading that made it meaningful (seed 220012), and a source centring a heading its theme
  already centres (96300). Then 2,100 rounds clean under `--strict` (800 at chain 4 from 230000,
  600 at chain 6 from 210000, 400 at chain 8 from 220000, 300 at chain 10 from 240000). The two
  notes are for **blocks**: a cell has no key, and the base a cell is merged against need not be
  what that cell said last time - a row or a column one side has just added pairs with nothing,
  so every cell of it reads as both sides having styled it, and the note would open with "a table
  cell", naming neither the table nor the cell. Then, at fresh seeds, the oracle once more:
  **a row's words are evidence and not the row** (chain-6 seed 260208). The base's rows say
  `thicket` and `meadow`, the reader deletes `thicket`, the source rewrites `meadow` into
  `thicket`, and the one row left rightly says `thicket` - which `_row_resurrection_findings`
  read as the deleted row coming back, the source having spent its words elsewhere. They are
  counted now rather than looked up: the reader took the count to what the document shows, the
  source raised it by `file - base` of its own accord, and a row over that sum is one that came
  back; where the source leaves those words alone the sum is what the document shows, which is
  the question as it was, and with `_table_lines`' inherited settlement put back in memory the
  campaign still catches the defect the check was built for (seed 63000, chain 4).
  Three more at fresh seeds, one the merge's and two the judges'. **A table written right
  behind another is a table written nowhere** (chain-8 seed 280039): Docs keeps an undeletable
  paragraph between two tables, so `insertTable` splits the paragraph it goes to and the half in
  front becomes that mandatory one, which the file has no block for - nothing then says which of
  the two empty paragraphs is which, and where the second is the body's last it is hidden
  (`_hide_trailer`), so the settle keyed the leftover with the name of the paragraph the file
  wanted *after* the table, the order read as the base's, the move was undone in silence and a
  restyle planned for that paragraph was planned onto nothing next time round.
  `doc_merge.refuse_back_to_back` leaves it where the document has it and says why - the sibling
  of `refuse_nowhere` and `restore_undeletable`; a table written in *front* of another is not
  this, its empty half landing between the two where Docs wants a paragraph anyway. **A chip's
  face is not words anybody typed** (280398, the oracle's): the file says `Grace` and Docs
  renders `grace` off the address, so counting the face made a block the source merely *moved* -
  a delete and a write, every chip inserted again - read as losing the `Sep 20, 2026` the
  reader's Backspace had brought into it (`oracle.own_words`, `_says` asked from the other side;
  a chip that really goes is `frozen_marks`'). And **an indent a bullet ate** (chain-10 seed
  290010): merge-on-delete hands a block the whole style of the paragraph deleted in front of it,
  bullet and all, so a plain paragraph the source had just indented came back an *item*, whose
  indents are the list preset's and belong to neither side - and the settle took that bullet off
  in the same batch (`restore_bullets`), leaving it with neither. The question is whether the
  block is an item once the settle has **finished** (`_paragraph_fields`: the plan's kind
  decides, since the settle writes the bullet to match it, and that kind is the source's only
  where the document kept the base's), and the delete goes first now too, as
  `_paragraph_requests` has always done it - `deleteParagraphBullets` keeps the nesting by adding
  indents of its own. 1 of 200 rounds at chain 10 with the old rule back in memory; then 1,000
  clean under `--strict` (300 at chain 10 from 290000, 500 at chain 6 from 310000), with 800 at
  chain 4 from 270000 and 600 at chain 6 from 300000 before them. And **the name a new table's
  swallow takes** (chain-4 seed 330127): `_new_table_requests` gets rid of the empty paragraph
  `insertTable` leaves by deleting the mark of the block before it, and a block that is itself an
  empty paragraph is all mark, so its named range goes whole - which `_swallowed` has known since
  seed 501271 and called harmless, the settle keying it again from its words. True of the settle,
  not of what comes before it: a structural batch is followed by a **re-plan** against the
  document it has just written, where the file's key names nothing, so the block reads as one the
  reader deleted and everything the source asked of it is dropped. Here a person chip the source
  put in the paragraph between two tables, written nowhere, the round converging with file, base
  and document all agreeing on an empty paragraph - invisible to the oracle (nothing of the
  reader's went) and to convergence, seen only by `_words_arrived`, and only because `_says`
  counts a chip by what it *is*. `doc_merge.recover_swallowed` gives the key back between the
  batch and the re-plan, after `anchor_tables` (the survivor is found by the table it stands in
  front of) with `plant_ranges` putting the range back - the third repair hanging off that one
  read. 900 rounds at chain 4 from 330000 clean afterwards, and 400 at chain 8 from 320000.
  **And the same question about the order** (`fuzz_docs._order_arrived`, the fourth judge):
  where the reader left the shared keys exactly as the base has them, the blocks the source
  moved must come out where the file puts them - a move that never arrives takes nothing of
  the reader's, so the oracle is silent, and the settle writes the order the document ended
  up with into the base, so the round converges on it. What is asked is a **pair** of keys,
  not a block: which of two swapped blocks "moved" is not a fact about two orders, and the
  merge may refuse and name the one the file reads as having stayed. Four defects, and two
  more from the same corner. (1) *A refused move is not a move in the merged list either*
  (chain-4 seed 380191): every index comes from a block's span and a span says where a block
  **is**, so all three refusals put it back where the document has it; `structure`'s did not,
  and the table then stood in the merged order where only the file had it. (2) *`_moved_keys`
  was not a longest common subsequence* (chain-6 seed 400186): `SequenceMatcher`'s matching
  blocks are contiguous, so a paragraph sent to the front made every block it passed read as
  moved - among them a table whose move was then refused, the order coming out neither side's
  in silence. It is the longest increasing subsequence of base positions now, and the moves
  go in **before** the additions, an addition being placed after the block the file puts it
  behind. (3) *A refused move drags its followers* (chain-10 seed 450252), which nobody could
  read off the line naming the one that was refused (`_apply_source_moves`' `stuck` set says
  it, for a block that does not move at all and for one that moves partway). (4) *A mark the
  reader moved from one word to another* (chain-6 seed 400044): `marks_of` is a sequence of
  mark-sets with no words in it, so `_restyled_words` wrote the file's styling onto every
  word the file has and took the reader's bold back off; it writes only what the source
  changed. And the level: **no request sets a bullet's nesting level** - Docs reads it off
  leading tabs the merge does not write - so it lives on a paragraph mark and goes three
  ways, a block written from nothing coming out at the level of the list it lands in whether
  that is deeper (430296) or shallower (chain-8 seed 530265, the half the note was too narrow
  to say) and a block handed the mark of the one deleted in front of it wearing its level
  (430587). `doc_merge.unwritten_levels` says so before the write; docs/google-docs.md
  "Remaining risks" 5 has the one live measurement that would make it writable.
  Then six at fresh seeds, four the merge's and two the judges'. *The document deleted a row
  the source wrote in* (chain-10 seed 480066): the mirror note `_table_lines` never had - the
  words go with the row and only the report can say so. *A range two deletes of one batch
  both claimed* (chain-6 seed 550667), which killed the sync: a run of deletes hands each
  mark leftwards, so `_orphan_range` asks every cut of the batch, a `deleteNamedRange` for a
  range already gone being refused and a refusal throwing out the whole batch. *The settle
  undoing its own repair* (chain-4 seed 570181): a paragraph the source retitled behind an
  item the source deleted came back a bulleted plain line, `restore_bullets` took the bullet
  off, and `bullet_requests` - reading the block as the read-back has it - counted it into
  the run behind it and re-bulletted the lot, flattening the named style written one request
  earlier; a run is what the settle **leaves** (`_settles_as_item`). *An empty paragraph of
  the source's own is never a table's lead* (chain-12 seed 630138): `doc_ir._hide_trailer`
  knows the lead and the trailer by their **shape**, and a delete puts a block of the file's
  own into that place - the opening table goes with its lead and the paragraph behind it is
  now the one in front of the next table - so it vanished out of the IR, its range read as an
  orphan and was deleted, and the settle wrote the file with that paragraph behind the table;
  a mark somebody planted an identity on is not scaffolding (`_planted_starts`). And the
  judges': `_cells_arrived` recognising a regrid by the grid's *size*, so a row-out/row-in
  regrid made every shifted cell read as rewritten (570177), and `_cell_findings` not being
  given the tab's base words, so a paragraph the reader dragged against the word standing in
  a cell welded to it (`it.x`) and the source rewriting its own half read as a loss (600784).
  Each measured by breaking its mechanism (1 of 200 to 1 of 1,200 rounds) and pinned by a
  hand-built test; then clean under `--strict` at thirteen settings from 380000 to 650000.
  **And the last two things a source edit can say** (`fuzz_docs._existence_arrived`): a block
  is **new**, a block is **gone**. Neither other judge will ever ask - a paragraph of the
  source's that never arrives takes nothing of the *reader's*, and the settle writes what the
  document holds into file and base alike, so the round converges on its absence. Asked of the
  words and not the key (a key is made from a block's words where no range carries it, so twins
  trade keys and every wordless block shares one), and only where the reader left the block
  exactly as the base has it. Six findings, four the harness's and two the merge's, and the
  first needed the judge to be wrong itself: (a) `_arrived` is handed the file **as it stood
  before the sync**, where a block the source added has no key - `doc_merge.plan` keys it in
  place and the report names the refusal by that key, so the one excuse there is could never be
  looked up and every block the merge refuses to write read as lost in silence (660085); with
  it, `refuse_nowhere`'s note now says the settle takes that paragraph out of the file too,
  rather than "not written", which reads as a thing still waiting. (b) `oracle.parts_by_tab`
  keyed a part by `part.get("tab")`, and a `<section>` with no `data-tab` is a tab the *file*
  asks for that has no id: it landed on `None`, where the body is, and the last one written won
  - **107 of 200** rounds at chain 4 fail with the collision back and **0 of the same 200** with
  the existence judge also off, so it was invisible until this judge existed. (c) The drag:
  `read_move_block` read its drop index off the document the reader saw and not the one the cut
  leaves behind, so a block dropped below the cut landed that far past where they let go -
  inside a word, a chip, or between the two code units of an astral character, which left a lone
  surrogate `doc_ir.utf16_len` encodes strictly and killed the campaign (710370, net
  `doc_world.splits_a_pair`, a judgement: whether Google refuses such an index is owed). The
  crash is the smaller half - everywhere else it handed every judge a document no reader could
  have made, and **both** of the oracle's punctuation forgivenesses (`_dressed_up`, `_undressed`)
  had been written for damage this one line was doing; a drop is a paragraph mark now, nothing
  can land inside a token, and they are **gone** (0 of 800 at chain 4, 0 of 500 at chain 8, 0 of
  400 at chain 12, 0 of 60 regression seeds - `_twin_unmarks`' lesson), while `_welded` and
  `_pared_down`, which are for edits a reader really makes, stay. (d) The question itself,
  sharpened three times: a bag of words over the tab let a `collide` stand in for a dropped
  block (720173), counting blocks saying *at least* its words let `harbour we found.` answer for
  `harbour` (720270), and counting everything a block **says** let a chip put into a twin stop
  it saying what the new copy says (720074). The count is of the text alone now and is only one
  of two traces, since the confound is the counting itself - a question about one block asked of
  every *other* one that says the same thing, which a `collide` rewording the twin in the very
  step that appends the copy answers wrongly (730061). The other is the **key** the settle gives
  back to a block the plan wrote a named range for; either excuses, and both are earned (the key
  blinded, 1 of 400 rounds at chain 12; the count blinded, 10 of the same 400). (e) The merge's
  own: **a table that never said anything is recovered onto nothing** (730384). `recover_tables`
  pairs a table whose range the *reader* destroyed with the base entry naming it, on the words
  the two hold - and two empty strings are each other's perfect match, so a base table that had
  never said anything paired at 1.0 with the blank table `insertTable` had just built in the
  same batch, before `anchor_tables` (the pass that knows about that one) ran. The reader's
  table, beheaded by a row delete and holding the only word either had, was then the one free
  table left for the new table's key: the merge saw its own blank grid where the reader's table
  stood, deleted it to build the grid again, and the word went with no note. A pairing on words
  is not a pairing when a side has no words. 1 of 400 at chain 12. Two more came out of the
  same campaigns, one per judge. `_order_arrived` leaves out a key a block has **no words
  for** - it is that block's number among the wordless ones, so a source that drops the
  paragraph Docs keeps between two tables leaves its mark standing empty, the mark takes
  `paragraph:empty` and the block that had it becomes `paragraph:empty#2`: two names changed
  hands under a real move elsewhere, and the judge read it as the source's order undone
  (780188). Only where the four sides **disagree** about which keys are wordless, which is the
  narrowest it can be - a judge gives up as little sight as it can. And `anchor_tables` names
  the table a structural batch wrote by what it follows, taking the first unkeyed table after
  it: usually its own, but a **reader** can behead a table too (deleting the row it is anchored
  in, which `recover_tables` will not pair where the words leave doubt), and then the regridded
  table's key went onto the reader's, the source's rows were planned against the reader's grid,
  and the rows the sync had just written stood under no name - the regrid reached the document
  and went away again, silently (790329, 1 of 600 at chain 6 with the old choice back). Where
  the batch wrote a grid, that grid is what its table has (`_built_size`), and where no free
  table has that shape nothing is claimed: the key comes back at the re-plan, where
  `recover_tables` sees both tables at once and the words tell them apart. One measurement did
  not survive: `_moved_keys`' own defect was 1 of 200 at chain 6 around seed 400186 and is 0 of
  200 there now (0 of the same 200 with the wordless exclusion disabled, so that is not the
  reason) - since 450252, `_apply_source_moves` **says** every block whose move it could not
  make, so the same break comes out as a report rather than a silence, and a loss campaign can
  only see the silences; the unit test calling `_moved_keys` a longest common subsequence needs
  no seed. Then clean under `--strict` at ten settings (800 at chain 4 from 750000, 770000 and
  810000, 700 at chain 6 from 740000, 600 at chain 6 from 790000, 500 at chain 8 from 720000
  and 760000, 400 at chain 8 from 800000, 400 at chain 10 from 780000, 400 at chain 12 from
  730000 and 820000), `KNOWN` still empty.
  **Then by shape rather than by depth**: a round draws one of fourteen shapes, so a defect
  needing two tables on the page waits for the dice, and `--shape` presses on one part of the
  machinery instead - 800 rounds of `two_tables` at chain 8 found three things where 1,600
  mixed rounds at the same depths had found none (each 1 of 400 with its mechanism back; kept
  as `tests/test_doc_fuzz.py`'s `SHAPED`, since these scripts exist on one shape only). The
  oracle's: `WORD` is `\S+`, so the full stop the **source** parks against a word the reader
  bolded comes along in the token, and "a word wears what every character of it wears" read
  the bold as gone while it sat there (870308; `oracle.core` trims Unicode `P*` off either
  end, leaving a soft hyphen where it is - that joins two words rather than dressing one).
  And two of the merge's, both about a table: after a structural write `recover_tables` (for
  a range the **reader** destroyed) runs before `anchor_tables` (for one of ours), and where
  the same table was regridded by us and beheaded by them the two crossed - a regrid that
  deletes a table's first column leaves it saying almost nothing while the reader's beheaded
  table still says most of the base's words, so the regridded table's key went onto theirs,
  `anchor_tables` found it already placed and the source's regrid went nowhere (870368; the
  batch knows where it put its tables and this pass only guesses, so it is told which keys
  are `spoken_for`). And a **body may not end on a table**, so a trailing table goes out by
  its own span *and the mark in front of it* - which, when that block is an empty paragraph,
  is all the block has: the file putting the table in front of it asks for a place the
  table's own delete takes away, `insertTable` splits what is left of the swallowed
  paragraph, the table lands behind it again and the move is undone in silence (890070;
  `refuse_eaten_anchor` leaves it where the document has it and says why, the mirror of
  `refuse_back_to_back`). Clean afterwards at five more settings (800 `two_tables` at chain 8
  from 870000, 800 `ends_on_table` at chain 8 from 890000, 700 `between_tables` at chain 6
  from 910000, 800 mixed at chain 4 from 1000000, 400 mixed at chain 10 from 1010000).
  Then `themed`, the one shape with named styles behind it, so the one where a field going
  from **inherited** to a paragraph's own can be seen at all - two more, one each side.
  Docs merges a deleted paragraph into the one behind it and hands over its style, and
  `carry_unimported` may put the *measurements* back only on a paragraph this run wrote
  (`_unwritten`) - the narrowing that keeps the settle from taking away styling a reader
  chose - but the neighbour of a block this batch **deleted** is exactly a block nobody wrote
  that the write itself changed: the source dropped a justified paragraph, the heading behind
  it came out justified of its own and stopped following the theme's centring for good, and
  the settle regenerates the file from it, so the next sync agrees (1130023; `requests` marks
  that neighbour `paragraph_merged` - the block behind, and the one in front where the delete
  borrowed *its* mark - and only `theme_undone` could see it). And the campaign's own:
  `_worn` subtracts a run's marks by the block's named style, so under a theme that bolds
  HEADING_1 a reader bolding one word of a themed heading says nothing that side can see and
  the guard read the block as untouched; the source then made it body text, where the same
  styling spells out differently on either side, and the reader's bold - kept, as it should
  be - read as the source's restyle vanishing (1140022, 1150196; two named styles are two
  languages, so the run half is not asked, the named style being part of `_shape` anyway).
  Then the same shape at **chain 10**, where both findings were the loss oracle's own
  `styling_restored` - the newest of its questions and the one with the most ways to answer
  about the wrong word, neither a loss. A block the reader left exactly as the base has it
  goes when the source drops it and the mark taken off one of its words goes with it (the
  bargain `block_gone` states in the same words); what made it visible is that the **key**
  does not go - the reader's own pasted copy stood beside it saying the same words in the
  theme's bold and took the name, so the question was asked of the block and answered by the
  copy (1180145; `dropped`, the taken-off half only, since a mark *put on* keeps the block
  alive, and only where there is a file at all, or "the file no longer names this key" is
  true of every key there is). And the question is asked by **occurrence**, so where the
  source rewords one of two alike the mark went with it: the heading said `thicket` twice,
  the reader un-bolded the first, the source made that one `vellum` - un-bold, exactly as
  asked - and the plain `thicket` left over answered for it (1180151; capped by what the
  *file* asks, which says whose doing it was, an occurrence the source **added** being the
  mirror case that must stay no excuse, seed 40254). 1 of 200 rounds each.
  Clean at ten more settings (700 `themed` at chain 6, 500 and 300 at chain 8, 400, 200 and
  250 at chain 10, 700 mixed at chain 6, 500 `chips`, 500 `imported_list`, 300 `equations`
  and 300 `tabs` at chain 8). Then the shapes nobody had pressed, 300 rounds each at chain 8:
  `toc`, `opens_on_table`, `titled`, `astral`, `dropdown` and `prose` clean, and one from
  `between_tables` (1270233). `insertTable` splits the paragraph its index is in, so a table
  in front of a block leaves an empty paragraph that `_new_table_requests` removes by deleting
  the mark of the block *before* - and a block that is itself empty is all mark, so its named
  range goes whole and `recover_swallowed` has to give the key back before the re-plan. It
  asked for a plain **paragraph**, and Docs' merge keeps the first one's style, so an empty
  subtitle hands the survivor its named style and the recovery passed it by: the one thing the
  source asked of that block - to stop being a subtitle - went nowhere, and the settle keyed it
  from its words to the very name it had, file, base and document agreeing. Anything but a
  structural element counts now; what makes it the survivor is being empty, unnamed and right
  in front of the table. 1 of 300 rounds, `between_tables` clean at chain 8 and chain 10 after.
  The same sweep at **chain 10**, 250 rounds each (`themed`, `between_tables`, `opens_on_table`,
  `two_tables`, `ends_on_table`, `tabs`, `imported_list`, `chips`, `toc`, `titled`, `equations`
  clean), left `astral` with the other half of that sentence: a range can be *given* an empty
  paragraph as easily as it can lose one (1430231). A reader's backspace at the start of an
  empty paragraph leaves its range inside the survivor naming nothing - the orphan
  `doc_ir.orphan_requests` deletes - and an empty paragraph is all mark, so the orphan sits on
  the survivor's paragraph mark, which is exactly where `insertTable` goes in front of another
  table: the split hands the range to the empty paragraph the insert leaves behind, the re-plan
  (which reads the document again between the structural batch and the words) sees the block the
  reader deleted standing there, and the source's words go into it. `structure` heads its batch
  with the orphan deletes now, for the reason `requests` heads the batch of words with them one
  step later - they move no index, and the one batch they were not run before is the one that
  makes a new empty paragraph. 1 of 250 rounds at chain 10, pinned by a hand-built test.
  **Chain 12** then (400 `between_tables`, 400 `themed`, 600 mixed clean) gave `tabs` one
  (1640036), shrunk to two appends and a restyle-and-move with **no reader op at all**: an item
  written from nothing takes the level of the list it lands in, and `unwritten_levels` - which
  has said so before the write since chain-4 seed 430296 - named the wrong list. A block goes
  in as `text\n` at the **start of the block that follows** it, so Docs splits that paragraph
  and the new block, the half in front, keeps the style that was there, bullet and level among
  it; only where nothing follows, or a table does, does it wear the style of the block in
  *front*. A level-0 item moved to just in front of a nested one therefore came out nested in
  silence. The prediction asks `_anchor` where it splits now and the block in front only where
  it appends (1 of 400 rounds at chain 12), and it caught the note lying the other way too:
  the test standing for chain-8 seed 530265 moved an item *behind* a nested one, which really
  comes out at level 0 as asked - a spurious note is `_shape_arrived`'s excuse, so it hides the
  next real one. Both tests now assert the level the document ends up at beside the note.
  **Chains 14 and 16** then gave one defect twice over, once through each judge (500 mixed at
  chain 14, seed 1740158; 300 `tabs` at chain 16, seed 1730265): a body may not end on a table,
  so a trailing table is deleted with the paragraph mark **in front** of it or Docs' own trailer
  is left standing as a second empty paragraph - and a block that is itself an empty paragraph
  is all mark, so the delete takes its named range whole, exactly as a new table's swallow does.
  Half of that was known and refused (`refuse_eaten_anchor`: where the file puts the table in
  front of that very block, the place it is written goes with it); where the table moves
  elsewhere the move stands and the block was being destroyed quietly. The re-plan then reads
  the file's key as a block the reader deleted, so the source's restyle of it went nowhere
  (1730265) and the item it had moved came back between the two tables under a fresh name, the
  file's order never reached (1740158). `recover_eaten` gives the name back to **the trailer**:
  with the table in front of it gone that empty paragraph *is* the block - at the end of a body
  there is nowhere else for one to be - and `doc_ir._hide_trailer` leaves a paragraph with an
  identity planted on it out of the scaffolding. Both campaigns clean afterwards. The same
  campaign gave one more from the other end (400 `prose` at chain 12, seed 1710213), a
  **report** rather than a loss: where all three sides have one grid, `_merge_table` matches
  cells by place and `_merge_cell` merges each paragraph by paragraph through `_merge_block`,
  whose fallback name for a block with no key was the literal `a table cell` - true, and an
  address for nothing, a document with three tables saying it three times. The other branch
  of `_merge_cell` had stamped the table's key since it was written; `_merge_block` takes it
  as `inside` now, so a conflict a person is handed says where their words were and the
  oracle's `cell_lost` excuse (the report naming the table) can be found at all.
  **How the reader set a paragraph** was the largest gap left in the oracle: it asked about
  the reader's words, chips, pictures, rows, blocks and tabs, and since the theme work the
  marks on a word - never about a *paragraph*, although centring a quotation, indenting it,
  shading it or making a line a heading is a choice made without touching a word and as
  deliberate as bolding one. `_shape_findings` asks it (`shape_undone`), forgiving two
  things: a list's ordered-ness, which an imported document cannot report at all, and Docs'
  merge-on-delete, where a paragraph the source deletes hands its own style to the one
  behind it. It failed 7 of 200 rounds at chain 6 the first time it ran, one signature, shrunk
  to two operations - **the reader spaces a paragraph out, the source adds a chip to it**
  (seed 2000188). A block whose chip or picture the source changed is written again from the
  file, no request being able to edit one, and it took the file's *shape* with it on the
  grounds that a document leaving the block's words, run styles and frozen runs as the base
  has them "has nothing of its own in it" - it may have. Silent in every direction: the words
  are all there, the source's change arrived, and the settle regenerates the file from the
  document, so the next sync writes nothing. `_merged_shape` is the one rule both ways into a
  block now share (the source's where the document left it alone, the document's where both
  changed it, with a note). Clean at 200 @ chain 6 (7 -> 0), 400 @ 4, 300 @ 8, 300 `themed` @ 6 -
  whose own finding was the oracle's and the twin hazard again (2030066): **a key is not a
  block**. The reader pastes a copy of a heading, which lands in the style of what it was
  dropped into (Docs' rule: here a heading centred of its own), the source drops the block the
  key was on, and the settle keys the copy by its words to the name that went, so
  `_inherited_findings` compared two different blocks. `_twin_already` forgives it, asked of the
  words the key names *afterwards*, so a twin saying something else answers for nothing.
  **The first campaigns run with that judge** (300 @ chain 10 from 2040000, 500 @ 6 from
  2050000) each came back with one finding, and both were the sync's. (a) *A boundary has to be
  a word* (2040246, `equations`): `marks_of` drops a block's words to stay deaf to a chip
  splitting a run, and drops the **boundaries** with them, so a source taking its bold one word
  further left - `((), 'the value  holds '), (bold, 'everywhere')` -> `((), 'the value '),
  (bold, ' holds everywhere')` - is the alternation of mark sets that was already there: the
  merge read the file as asking nothing and the sync wrote and said nothing. `doc_merge._remarked`
  asks the words, and only the ones both sides have, a word one side typed or deleted being the
  text merge's business. Only the **source's** side may be asked it: "did the reader restyle?"
  guards a branch that gives up on the whole block, and asked as finely a reader moving a bold
  from one word to another takes the source's italic on a third word with it, where
  `_restyled_words` merges both (seed 400044's test fails the moment it is). (b) *A centring has
  to be an edit* (2050019, `imported_list`): `_edited`, the test that outranks a source delete,
  learned the marks at seed 90175 and still never asked `_shape` - kind, level, ordered-ness,
  alignment, indents, spacing, shading, rules - the whole of which lives in properties no word
  carries, so a block the reader had only centred read as untouched and the delete took it.
  `ordered` counts here though the oracle leaves it out: what an import cannot *report* is
  neither side's fault, but both readings `_edited` compares are the document's own.
  **The cell nobody had ever styled.** Coverage says what is drawn, not where: every
  `updateTextStyle` and `updateParagraphStyle` the campaign ever drew was on a paragraph of the
  body, because both sides pick their spot from `part["blocks"]` and a cell lives inside a table
  block's `rows` - `src_edit_cell` reaches one and only rewrites words, so `_merge_cell`'s
  styling was as undrawn as the column requests had been. `read_cell_style` / `src_restyle_cell`
  say it now (the reader's reports the *table's* key, so `collide` answers in the same table;
  `pageBreakBefore` is left out, Docs refusing it in a cell, and a request the real API would
  reject is the harness's doing). It found one in the first sixty rounds (chain 4, `two_tables`,
  seed 3000027): `_table_movable` - "can this table be deleted and built again with nothing
  lost", asked before a source move is written - compared the cells' **words**, and a rebuild
  carries nothing else, so a reader who small-capped or centred a cell had it taken off with
  nothing in the report. It is `_edited`'s rule at the size of a table, and it needed `_shapes`
  to exist: `_shape` of a table is a row of `None`s, a table saying what it is through its cells,
  which `_styled` already knew. `_edited` asks `_shapes` too now. Then 1,550 rounds at chains 4,
  6, 8 and 10 came back with one finding each and all four were the **harness's**, one defect in
  four hats: `doc_world.paragraphs` took a paragraph's first index to be its mark less everything
  since the mark before it, and a table is a body unit with no mark, so the paragraph after a
  table began at the table's own start and *every range aimed at a cell reached it* - a source
  restyle of one cell took the alignment a reader had given that paragraph, and
  `createParagraphBullets` inside a cell would have bulleted it (`doc_world._own`: a table ends
  the paragraph in front of it, as `documents.get` and the world's own named ranges say). All
  four clean afterwards.
  **A chip in a cell, and the mark a delete borrows.** Two more from that corner, one of each
  kind. The sync's: a block in front of a table gives up the **previous** block's paragraph mark
  (its own being the undeletable newline before the table, `_delete_range`), and where that
  previous block is an empty paragraph its named range *is* that mark - a range dying with its
  text - so the delete takes the name of a block the source never asked to touch; unnamed, an
  empty paragraph in front of what is now the body's opening table is scaffolding by shape
  (`doc_ir._hide_trailer`'s `lead`) and leaves the IR entirely, its key settling on some other
  empty paragraph, the source's order reading as never arrived, nothing reported (chain-10 seed
  4200130). The mirror image of `_orphan_range` - there a range outlives its block, here a block
  outlives its range - repaired in the same batch with **no `deleteNamedRange`** in front of it:
  Docs has taken the id already and a request naming one that is gone throws out the batch. The
  op: `cell_chip` on both sides, since `src_add_chip` and `src_add_picture` pick from
  `part["blocks"]`, so a frozen run had never been asked for *inside a table* - where `rewrite`,
  the only way a block whose frozen runs the source changed is written at all, might have stopped.
  It does not; what stopped was the **judge**. `_cells_arrived` asked `oracle.cells_of`, which is
  `text_of` per cell, so a chip was counted by its **face** - and a chip's face is the document's
  to draw, which `_says` had learned at the size of a block and the cell was the last place still
  asking in the old language: 16 regression seeds failed at once, every one of them the judge's
  own (`_cell_says`). The four campaigns then found the same rule one size further down, in
  2,100 rounds: a line the source takes away goes **unless the document wrote in it**, and
  `_line_unchanged` asked the cells' text, so a reader who small-capped or resized a word of a
  cell had `deleteTableColumn` carry it off with the column, unreported (chain-8 seed 5300013,
  shape `themed`, shrunk to three steps). `_same_set` asks `_styled` and `_shape` of the cells
  both sides share and the line is kept as one written in would be ("wrote in it or styled it").
  Block, table, line: the third size at which the same sentence had to be said - **styling
  something is a choice the reader made in the document, exactly as much as typing in it**.
  Clean afterwards at 300 rounds chain 10 `two_tables`, 600 chain 6, 800 chain 4 and 400 chain 8.
  **A cell with two paragraphs in it.** The op is `split_cell` on both sides - the reader pressing
  Enter in a cell, the source giving a cell a second paragraph - and until it existed every cell
  the campaign drew held exactly one, so `_merge_cell`'s unequal-counts branch (the `diff3` over
  the cell's joined text) had never carried anything text cannot say. Five findings, one of each
  kind again. The **sync's**, silent: a chip in such a cell was flattened to the object character
  and `text_requests` skips any hunk holding one, so `_block_edits` came back empty - neither the
  split nor the chip written, nothing reported (chain-4 seed 6000042). Where the merged text runs
  through a frozen run *and* the document left the cell exactly as the base has it, `_cell_kept`
  sends it down the `rewrite` path; only there, since a minimal edit keeps the reader's styling on
  the words it does not touch and keeps the table's named range, which a rewrite of its first cell
  destroys. Narrowing it that far took the guard out from under the *other* reason the words
  cannot carry a cell, and the campaign said so within the round: an object character stands for
  whichever chip is there, so a cell whose picture the file replaces with a person chip has a
  merged text equal to the one already written, the split was all that got written and the chip
  arrived nowhere (chain-10 seed 6100210) - `_cell_frozen` is `_merge_block`'s guard at the size
  of a cell. **Identity**: a table is anchored in its first cell, so rewriting that cell takes its
  named range and `adopt_keys` is the one thing that gives the key back - by the words the plan and
  the read-back say, and they did not say the same ones, a rewritten cell being one block with the
  paragraph marks inside it (`_cell_runs`) where the document hands back the paragraphs it is:
  `x\nribbon` against `x | ribbon`. The table went unrecognised, settled under a name made from its
  new first words, and file and base both named a table the document did not have (chain-4 seed
  6000079); `_match_text` counts a cell's paragraphs one by one, whichever side holds them. The
  **judge's**, twice. `_source_dropped` is about a *block* and a cell is not one, so a chip that
  went with the column the source deleted had no excuse - the more so where the same step added a
  chip of that address elsewhere, which `mine_frozen` credits against it (chain-6 seed 6200507,
  chain-4 seed 6300228): `oracle._dropped_cells` asks what a cell *says*, a cell the base has word
  for word that the document still has and the file has not being one the source dropped, which is
  also what keeps it narrow - a chip the **reader** put in makes the cell unlike the base's, so its
  column is not the source's to take. And a row is known by what it says across every column, so a
  reader deleting a **column** read as every row deleted at once; the source's new column then held
  the word the old one had, and two rows came back from a grave neither was in (fresh chain-8 seed
  6500291): `_rows_still_shown` counts a base row the document still shows *some* of as one it
  still has, consumed one for one. All three campaign judges have now been caught reading a table
  in another's language - a chip's face, a cell's styling, a row's words across columns that are
  gone - a table being the one place where block, row, column and cell all have identities at once.
  Windows re-measured (`theme_undone` 15 of 60 -> 11, `styling_restored` 5 of 240 -> 4, its window
  moved); clean at 300 rounds chain 10 `two_tables`, 600 chain 6, 800 chain 4 and two of 400
  chain 8.
  **The bullet button, which nobody had ever pressed.** After typing, it is about the commonest
  thing anybody does to a paragraph in a browser, and no op drew it: `read_heading` and
  `src_retitle` walk the *named* styles, where `item` is not one, and `read_renumber_list` only
  reglyphs a list that is one already - so a bullet was something only a corpus shape ever had.
  `read_bullet` and `src_bullet` say it now (ordered-ness with it, that being the half of a list
  item the file alone can carry), and four findings at chain 8 were three things, all of the same
  family: **what a request cannot say about a list**. (1) *A level lives on a paragraph mark, and
  a mark that survives keeps it* (7400013, 7400167, 7400363): `unwritten_levels` said so for a
  block written from nothing and for one handed a deleted block's mark, which are the two cases
  where the mark *went*; the plainest case is the one they are exceptions to, and a block the
  source nests stays where it is. (2) *A glyph belongs to the list, not to the item* (7400334,
  `unwritten_glyphs`): `createParagraphBullets` lays a preset over the list the range falls in, so
  a source numbering one of three either reglyphs all three or has its request undone by the
  settle putting the other two back - the file can say `<ol>` beside `<ul>`, which is two lists at
  a push and one list ever after. Which list an item is in is the document's word and never the
  file's, so `doc_ir` reads the `listId` back as `list`. Both are notes before the write, nothing
  else being able to see them: the reader left the block alone, so the oracle has no question, and
  the base agrees with the document afterwards, so the round converges. (3) *The glyph a delete in
  front hands over* (chain-4 seed 7700184), which is Docs' merge-on-delete once more - the style
  it hands over is the whole of it, the bullet's **list** among it, so an item the merge had just
  numbered came back in the deleted one's list and bulleted with it. `carry_unimported` compared
  the two sides' *kinds*, so both sides being items and disagreeing about the glyph was the one
  case it could not see; it asks the glyph now and `restore_bullets` writes it. Its test needs a
  **described** list to reproduce at all: over a list the importer built the file's word is taken
  as the document's (`guessed`) and `bullet_requests` lays the right preset down anyway, and it
  must ask `doc_ir.from_document` rather than the sync's own read, which fills a list's
  ordered-ness in from the file - the very thing the document is failing to say. A guard written
  with that fix went out again, `bullet_requests` grouping its runs by the glyph the settle is
  *about* to write: 500 rounds at chains 4 and 8 never brought a described block and a guessed one
  into one run, and a guard against nothing is how one stops noticing. Windows re-measured again
  (`theme_undone` 15 of 80, window to the first 40; `styling_restored` 5 of 300, window to 300-380);
  clean at 400 chain 8, 600 chain 6, 500 chain 4, 300 chain 10, then 300 chain 4 and 400 chain 6.
  **Deeper into the same button**, three more that finish the thought: everything a request cannot
  say about a list is said about a **mark**, and the question is always whose mark a block comes
  out on. (4) *The list a block lands in is not the list it came from* (chain-10 seeds 8100237,
  8100057, 8100374, `prose`): `unwritten_glyphs` asked the document which list each block was in,
  which for a block this batch is about to write is none at all, so it answered for nobody - the
  source numbers a paragraph and moves an item to just in front of it, the item goes in as
  `gamma\n` at that paragraph's start, Docs splits it and hands the new half its list, and the two
  are one list asking for two glyphs with no note and the numbering gone. `_landing_lists` works
  it out for every merged item at once. (5) *Where the two rules meet, the delete wins* (8100356):
  a block written from nothing wears the style of what it splits, a block behind a delete is
  handed the deleted block's - and where the source moves a block to exactly where another one
  goes it is both, the delete happening last. `unwritten_levels` gave the deleted mark to whatever
  stood behind it *before* the batch, by then one block further on, so it named the wrong block
  and missed the right one; `_mark_donors` answers "whose mark does this come out on?" once, for
  both it and `_landing_lists` - one handover, asked about the level and about the glyph. (6) *The
  named style a bullet was hiding* (chain-8 seed 8000322, `imported_list`), the only one of the
  six that is a **loss** and not a missing note: `doc_ir` reads a bulleted paragraph as an item
  whatever its `namedStyleType` says, so `carry_unimported` is blind to what the document carries
  underneath one - the reader bullets a heading, merge-on-delete puts that style under an item
  that never had one, and taking the bullet off uncovers it. The item came out HEADING_1 where the
  source asked for a paragraph, the report silent and the second sync writing nothing, both sides
  reading it as an item. The style goes in whole wherever a bullet comes off, there being no
  difference to take; the price is the reader who bullets a heading and means it. Clean under
  `--strict` at 400 chain 10 on the seeds that found the first two, 400 chain 8 `imported_list`
  and 300 chain 10 `themed`. One more came out of those fresh runs and it is the **oracle's**:
  `_welded` knows a reader joining two paragraphs makes one `\S+` token of two, and pressing Enter
  does the other thing too - the base cell said "signal 4", the reader split the word into "si"
  and "gnal 4", `collide` rewrote the cell, and the merge said "si" / "gnal-0c791" with both edits
  in it, while `theirs - was` read both halves as words they typed (`_cleaved`, chain-6 seed
  8400013, 1 of 500; exact, on `_welded`'s two conditions - the base token gone, the other half
  standing in the reader's own text).
  **Ctrl+K, the last undrawn field of the dialect**: of the eleven run fields in
  `doc_merge.MANAGED`, `link` had never been on a run in the campaign's life. Both sides draw it
  now (the source through `RUN_MARKS`, the reader through `read_link_word` - Ctrl+K, and a quarter
  of the time Ctrl+Shift+K taking one off), it found nothing in 1,920 rounds over six settings
  (five shapes, chains 4 to 10), and it is kept for what it says on `read_paste_block`'s
  precedent, with three
  tests pinning the behaviour it walks over, each verified by breaking its mechanism. The one place
  a link can be lost is not the restyle but a **move**, which is a delete and a write from nothing,
  so the reader's styling is carried onto the merged words run by run (`_retext`,
  `_style_requests`); a source restyle of that block is settled one rule earlier - both sides
  restyled it, the document's styling is kept - and the report says so. And membership of `MANAGED`
  buys less than it looks: `_text_style` writes a link the run *has* whatever `MANAGED` says, so
  the field earns its place only where the run has none, which is how a link the source **takes
  off** goes away (the restyle names `link` with no value, the API's "back to what you inherit").
  **Tab, which is no request at all**: the same audit one level up found the field with no
  request behind it, a bullet's **nesting level** (`createParagraphBullets` says nothing about
  one; `unwritten_levels` is three rules with nothing to do but say so before the write). No
  level in a round had ever been chosen by anybody - the corpus shapes were born with theirs and
  `src_bullet` wrote 0 - so the three rules were measured against levels that never varied.
  `src_bullet` asks for a level now, and `read_indent` is the reader pressing Tab: the
  campaign's **one op that sends nothing**, since no field of the v1 API writes a level, so it
  reaches the world directly (`doc_world.nest`, the named exception; a draw that moved nothing
  counts as nothing to do). The judge was already there with nothing to judge - `level` is in
  `doc_loss_oracle.SHAPE_FIELDS`, so a reader's level put back is `shape_undone`, excused only
  by a note naming that block. Worth, measured against a broken `unwritten_levels`: 200 `prose`
  rounds at chain 6 fail 6 times with neither draw, 44 with the source's, 47 with the reader's
  too; 200 `imported_list` at chain 8, 43 without the reader's half and 46 with it - so the
  source's level did the work and the reader's adds a little. Neither found a defect (1,920
  rounds over three shapes at chains 4 to 10, clean); what came out of it was a **note** that
  named an item nobody could find - a block written from nothing behind a plain paragraph lands
  in a list `createParagraphBullets` starts at 0, not "the level of the item in front of it".
- Live suite (opt-in, marker `docs`, ~5 min): `python -m pytest -m docs tests/test_docs_live.py`
  pushes a document per test, edits both sides, syncs, checks a second sync writes nothing, and
  deletes the document. Offline: `tests/test_doc_ir.py`, `test_doc_merge.py`, `test_doc_sync.py`.

## Pitfalls found so far
- PDFium (`pdf/pdfium_backend.py` handles these):
  - Soft-mask contents are not page objects. Beamer's block shadow is a black rectangle under
    a soft mask (`FPDFPageObj_HasTransparency` with an opaque fill); its visible pieces right
    of and below the panel are reported as shading images (`extract._shadow_pieces`).
  - Shadings are page objects of their own (type 4), usually inside form XObjects; their box
    is the clip. Images and shadings are both `images` in raw.json.
  - A ligature glyph comes back as several characters at the same origin: merged into one.
  - A hyphen ending a line (TeX's hyphenation, or an explicit hyphen followed by a word) comes back
    from the text page as U+0002 (`FPDFText_IsHyphen`): the backend gives "-" back, so classify's
    line join can drop it again (test frame "Hyphenation" in `14_misc`).
  - The text page reorders text objects on a line; chars are sorted back into content order
    (big operators' limits, accents).
  - pypdfium2's `get_cropbox` falls back to Letter when the box is inherited:
    `FPDF_GetPageBoundingBox` instead. `FPDFPageObj_GetIsActive` takes an out pointer.
  - Math fonts from xdvipdfmx carry their bounding box as ascent/descent (CMEX: −2.96 em);
    those get 0.8/−0.2. CMYK colours convert slightly differently from MuPDF (#fff101 yellow).
  - Type 3 (bitmap) fonts have no base name (`Type3`) and return glyph codes: a TS1 `\textbullet`
    (metropolis under pdflatex without cm-super) comes back as U+0088 (`classify.TYPE3_SYMBOLS`).
  - Page labels can't be rewritten: after `notes.prepare` deletes note pages, the kept pages'
    labels are passed to `extract(pdf, labels)`.
  - Image objects (`Page.embedded_image`): `FPDFImageObj_GetImageDataRaw` is the stream as stored
    (for DCTDecode the author's JPEG file, sha1 equal), `GetImageDataDecoded` after the filters,
    `GetBitmap` PDFium's own pixels at the image's native size — but **without its soft mask**,
    which `FPDFPageObj_HasTransparency` doesn't report either (that one only sees a constant
    alpha or a blend mode). The only signal for a mask is `GetRenderedBitmap`, whose alpha is
    exactly 255 everywhere for an opaque image; it applies matrix, mask and clip but comes back
    at about page resolution (a 2400 px photo renders 138 px wide), so it is a detector, never a
    source of pixels. An image's matrix in page space is `a > 0, b = c = 0, d < 0` when it is
    drawn upright: the y flip is the norm, `d > 0` or `a < 0` is a mirror, `b`/`c` a rotation.
- `render_text`'s `x_subpixel` is C's `%`: a glyph left of its origin gives -1 or -2, and PDFium's
  cascade (`if 0 ... else if 1 ... else`) draws those as 2. Anything that reads the number rather
  than the branch shifts those glyphs by a third of a pixel - `render_torture_text` failed 39 seeds
  of 2,500 on it and `render_torture_subst` 35 of 2,000, while the test decks and the offline suite
  saw nothing.
- Switching objects off (`FPDFPageObj_SetIsActive`) needs no content regeneration and is
  undone after each render, so crops and backgrounds share one open page.
- Ball bullets are patched out of the PNG (themes draw them with soft masks shared with shadows).
- Slides ignores spaceAbove/spaceBelow between bulleted list items (see docs/calibration.md).
- A bullet keeps the text style from when it was created, unless one later style request covers
  its whole paragraph. Set each paragraph's family, size and colour *before*
  createParagraphBullets, or bullets get oversized 18 pt default bullets.
- Inline math: `classify.math_kind` sends lines with fractions, radicals, big operators,
  stacked or second-level scripts, or formula-like density to the background. Everything else becomes runs
  with `script` super/sub and Unicode symbols (MSBM → ℝ).
- Figure removal switches off paths whose bounds lie within the figure box + 5 pt (strokes and
  arrow tips reach out); panel removal uses 1.5 pt, then 5 pt if the panel's path is still there.
- Title placeholders exist before any other element: bring them to front after adding shapes.
- Slides table rows are at least 1.195 em × lineSpacing + 14.4 pt tall (7.2 pt cell padding,
  not settable); empty cells count with the default font unless given a styled space.
- Layout pages reject `pageBackgroundFill.propertyState = INHERIT`; set the fill explicitly.
- Imported layout/master placeholders hold "\n" per list level (no visible text): updateTextStyle works
  on them, insertText is refused. A layout placeholder style equal to its master's reads back as unset.
- A transparent PNG as a page background fill shows white under its alpha (not the master); pictures on
  a layout draw over any slide background.
- Pixel checks on hairline shapes need a high zoom (`render._fill_fraction`).
- **An item added with Enter gets a bullet in its text's colour** (user report: a theme's blue dots
  came out black). Splitting a bulleted paragraph (`insertText "\n"`, then typing) copies the
  bullet's font and size but drops its colour - an rgb colour, a theme colour, a bullet made
  through the API or imported from a .pptx `a:buClr`, per paragraph or in `a:lstStyle`, and with
  the item's newline painted in the bullet colour - so the new bullet follows the new text (bold
  text, bold bullet). A list's `nestingLevel.bulletStyle` stays Slides' default (Arial 14, black)
  whatever is written, and no request sets it. Nothing the converter writes survives, so the fix
  lies in Slides itself (the paint format tool, or the list options' colour). Inserting "\n" plus
  text in one request keeps the colour, which is why the first probe saw nothing. Measured with
  `tools/probe_new_bullet.py`.
- Bullet colour/size can be set independently only through creation order: style the paragraph
  like the bullet, create bullets, then style the text in two or more requests (a single request
  over the whole paragraph restyles its bullet too). See docs/calibration.md.
- Page labels can come back as raw `<FEFF...>` hex strings; `extract._label` decodes them.
- PowerShell 5.1 mangles double quotes inside native-command arguments: keep them out of
  git commit messages passed via here-strings. `Get-Content -Raw` reads BOM-less UTF-8 as ANSI:
  edit text files with the editor tools, not a PowerShell read/replace/write.
- The newline a shape's or cell's text ends on is Slides' own: `presentations.get` reads it back as
  part of the text, but the length `deleteText` accepts is one *less* ("The end index (273) should
  not be greater than the existing text length (272)"), and a refused request throws out the whole
  batch. Keep it out of any diff (`merge.text_edit_requests`); an append goes before it, not after.
  The same length governs an `insertText` index and a `FIXED_RANGE` style range, so the styling of a
  box's last word stops one short of the text (`sync.style_range_requests` takes a run's trailing
  newlines off its range; `tests/slides_sim.py` refuses all three the way Google does).
- Slides stores every shape it creates at 3,000,000 EMU and puts the size asked for into the
  transform's scale, so an ABSOLUTE scale on a copy is relative to that, not to the size requested
  (`sync.STAND_IN` is exactly that size). Children of a group can't be restacked: a group rebuilt
  with new members gets its old child order back before `groupObjects` (`sync.update_slide`).
- Shape shadows, autofit and text insets are read-only in the API. A .pptx import keeps shadows
  (and duplicateObject, fill and transform changes keep them) but not spAutoFit.
- python-pptx: setting top/height on a layout placeholder that inherits its position writes x and
  width 0 and scales the (already rescaled) master position again: only rescale placeholders with
  their own `a:xfrm`.
- python-pptx autoshapes refer to the theme's effect style, which has a shadow: always give an
  explicit `<a:effectLst/>`.
- Text inside a shape with a shadow gets a shadow too: body text stays in separate text boxes.
- Resizing a group scales every child (title bars get taller; fonts don't scale). Slides'
  thumbnail renderer draws shadows of children of a scaled group at the unscaled size.
- Beamer draws block shadows as black rectangles under a soft mask; removing the panels above
  them without switching those off too leaves solid black bars in the background.
- `fidelity` reuses saved thumbnails unless the deck was emitted again (or `--refresh`).

## Environment
- Windows, PowerShell. Python 3.12 venv in `.venv` (`.venv\Scripts\python.exe`).
- **Run the offline suite with `python -m pytest -q -n 12 --dist loadgroup`**: 24 s instead of 66 s
  (816 tests). Most of the serial time is `test_invariants` extracting, classifying and rendering 53
  PDFs, ~0.5 s each, so that is what has to spread out. How the split works and why a file is the
  unit by default: `tests/conftest.py`. `pip install -e .[dev]` brings pytest-xdist; without it the
  flags are unavailable and everything still runs, serially.
- MiKTeX (pdflatex / xelatex / lualatex) for the test decks, with on-demand package install.
- Test decks: `tests/decks/*.tex`, built by `tests/decks/build.py` into `tests/decks/out/`
  (normal and `-handout` variants).
- Layout rules for Google's monorepo import (it stages sources in a content store and imports tests
  as a package): `tests/` is a package and imports its helpers relatively; the harness tests use
  (alignment, sync_check, deck_edits, loss_oracle, fuzz_*) lives in `beamer2slides.devtools`, with
  `tools/<name>.py` kept as runpy shims; data is reached through `importlib.resources` or a module's
  `__file__`, never through `src/` or a checkout path.

## Text fit (torture deck, 2026-09-23)
`tests/decks/27_text_fit.tex` has one frame per way a line can come out of Slides wider or narrower
than the PDF drew it: every size from `\tiny` to `\huge` left/centred/right, serif, typewriter,
bold, italic, small caps, wide and narrow letters, full-width justified paragraphs, two columns,
items and description labels near the edge, blocks, tight/numeric/bold-header/merged tables with
captions, a chart with long ticks, text beside a figure, text against the frame's edges, inline
math in a full line, numbers and signs. `tools/text_fit.py` (`devtools/text_fit.py`) measures a
converted deck on Google's thumbnails (saved by `fidelity`) against the PDF page: `wrap` (line
count and height both changed), `drift` (the edge a line's paragraph is aligned to, or the top),
`width` (a line more than 8% wider or narrower), `crowded` (Slides' deeper subscripts join two
lines of ink), `grown` (a table ends lower; a text's pitch is larger), `touch` (the white space
between two elements closed to under a quarter of itself and under 1.5 pt). How it tells an
element's ink from its neighbours' is the part that took the work: a window per element that stops
at the nearest neighbour's box, ink against the ground inside the element's own box (a block's
title bar, not the body panel), rules and rims dropped, and in each line only the runs of words that
reach into the element's own columns. Validated by the conversion made before the table fix:
5 tables grown ~12 pt there (the Monitor wrap), 1 after it (3.6 pt, a `\small` table under Slides'
row minimum). `--crops` saves each finding's window, the PDF's ink above and Slides' below.

What the deck found, and what was done:
- **Fixed**: OT1 draws `\_` as a 0.3 em rule, so `x86\_64` became a hole (picture 3 pt wider than
  its hole, predicted 83 pt off) and a typewriter line with `0x7fff\_ffff` four unanchored math
  pictures. `classify.underscores` reads such a rule - level with the baseline, a glyph right
  beside it, nothing set under it, never beside a radical - as a `_` span (`drawn`, no page object,
  kept out of every element's `spans`); the rule leaves the background as a stroke.
- **Fixed**: an inline `\sum` (CMEX10, text U+FFFD) hangs from its origin 8 pt above the words'
  baseline, so it was a line of its own sent to the background while `i w_i x_i` stayed text: in
  Slides the words moved and the sign stood still (`stray_ink`). `join_hanging_operators` (after
  `join_braces`, whose brace pieces it must not take) joins a lone CMEX glyph as tall as the words,
  centred on them, with words on both sides and an *upright* word of prose among them - not the
  radicals inside Cauchy-Schwarz, whose neighbours are the formula's own CMSSI letters. The native
  text of the other 53 test and theme PDFs is unchanged.
- **Open**: small caps come out 22% narrower (the substitute sets them smaller; slide 4); CMTT in
  Courier New 9% narrower (slide 18); digits and bold headers 9-12% wider in table columns; the
  three captioned tables at `\footnotesize`/`\large`/`\small` are refused as native tables (Slides'
  row minimum would grow them over the caption) and fall back to a text box per column, which for
  the `p{}` table joins the wrapped cell with the numbers beside it and reflows across the BLEU
  column (slide 12); Slides sets subscripts deeper than TeX, so consecutive lines of inline math
  touch (slide 17).

## The live suite and bases older than picture signatures (2026-09-23)
`python -m pytest -m slides` failed on every deck: each convert into its fixed folder
(`out/slides-tests/<deck>`) was refused by the rebuild guard, "6 slides edited: 1 background change,
10 picture replaced", on decks nobody touches. Their bases were written on 2026-09-17, before bases
recorded pixel signatures. Google reissues `contentUrl`s for pictures nobody touched, so a picture
is judged by its pixels (`snapshot.same_picture`); with no signature on the base side it cannot be,
and every picture read as replaced. The rule does not bend - a picture that cannot be compared may
have been replaced, so the rebuild is still refused - but the finding and the message now say
what is true: `guard._unverifiable` counts such a picture (and a background picture) as
`image_unverified` / `background_unverified`, and when nothing else differs the refusal says the
base predates signatures and that a forced rebuild loses nothing if nobody replaced a picture
(`test_a_base_older_than_picture_signatures_cannot_clear_a_new_url_but_says_so`). The suite's seven
decks were rebuilt once with `--force-rebuild --backup none` (they are its own fixtures), so their
bases carry signatures and are checked in full from now on. The suite also converts
`27_text_fit` and runs `devtools.text_fit` on it (`test_text_fit`): every finding must be listed in
`tests/slides_baseline.json` `text_fit_known` with a reason, and a listed one that goes away asks for
the baseline to be updated.
The first full run after that found one more, on a deck rebuilt twenty minutes earlier: "slide 11:
deleted (diagram/figure/0)" on `19_labels_on_graphics`. A diagram of one node gets no group
(Slides groups two objects or more), yet the base listed the group emit had named as the element's
main object, so every sync and rebuild guard after it read that group as deleted. The base now
records only the objects the deck really has when it is written (`snapshot.attach_readback`), and
the guard reads bases written before that correctly: a "deleted" main object the base's own
read-back never had, while the element's other objects stand, is no deletion (`guard._never_made`,
`test_a_group_emit_named_and_slides_never_made_is_not_a_deletion`, which also checks that a main
object the base did have and the deck lost still counts).

### Text fit follow-up: the three captioned tables (merged 2026-09-23, 864070b)
The open item above was misdiagnosed: the three captioned tables (slides 10-12) were not refused
for growth, they were never tables. A centred booktabs table in a 4:3 frame is 0.55-0.7 of the page
wide, and `is_decoration` takes every thin rule across half the page for theme decoration, so the
rules stayed in the background and the cells became text boxes joined across columns ("Monitor
Workstation Mainframe" in one box, the `p{}` cell's first line with the numbers beside it, which
reflowed across the BLEU column: drift 110 pt, width 1.8x). `classify.table_hairlines` keeps two or
more rules of one width, touching no page edge, as a table's rules when rows of text stand between
them (one with words more than an em apart) and nothing runs past their ends. In `table_from`, a
justified `p{}` cell, whose word spaces are wider than the half em that separates cells, is one chunk
per line, each line a row of its own; a `\cmidrule(l)` trimmed at its left end borders every column
whose text it covers. On Google's renderer slide 10 is a table grown 4.8 pt and slide 12 one grown
3.2 pt, both clear of their captions; slide 11 has no finding; the header words slide 12 left in the
background are native; slide 13's rules follow its table. The native text of the other 79 test and
theme PDFs is unchanged. Open: a wrapped cell could be one multi-line cell if emit left a wrapping
column out of `fit_columns`' one-line width (each line is a row now, keeping TeX's hyphen).

### Text fit follow-up: small caps, numbers, typewriter gaps, subscripts (merged 2026-09-23, 240bd3c)
Measured with `tools/probe_text_fit_fonts.py` (docs/calibration.md 2c). Slides draws a `smallCaps`
lowercase letter as its capital at 0.70 of the size, where CMCSC10 draws it at 0.755, and CMCSC is an
extended face while PT Serif's capitals are narrow: a Computer Modern small-caps run is now set at
1.28 x the serif size (`emit.SMALL_CAPS_WIDTH`), slide 4 from 0.777 to 0.996 of the PDF's width - at
the price of capitals about 30% taller than CMCSC's (a face cannot be both as wide and as tall; 1.13
would halve the gap). A small-caps run lays its line out at 0.70 of its size only when it is lowercase
letters alone; one space, capital or comma and the line takes the full size (`emit.line_size`, also
`deck_ir.pdf_size`, which inverts both corrections for pull). Lato's tabular digits are 0.577 em
against CM's 0.5, so a run that is only a number is set at the size that gives the PDF's width
(`FontMapper.number_ratio`, `CM_NUMBER_EM`): number columns 1.03-1.14 -> 0.97-1.02, prose lines moved
by at most 0.004, fidelity overlap up (table-tight 58.2 -> 67.4%, table-wide 48.0 -> 55.7%); the
"bold header" findings were the digits under the headers, bold words being 0.96. Roboto Mono draws
every character and every space at 0.600 em, so CMTT's size factor was already exact: slide 18's
0.912 was the em spaces classify wrote for a `\quad` (0.525 PDF em in Roboto Mono against CMTT's
1.05). A gap between two monospaced pieces is now `round(gap / advance)` plain spaces, as
`code_indent` does (`test_a_quad_in_typewriter_text_is_plain_spaces`; the only other change over
all test and theme PDFs is a code comment in `26_truetype_fonts` aligned with spaces instead of an
em space). Open, and out of the API's reach: SUBSCRIPT/SUPERSCRIPT set the run at 0.665 of its size
and move it 0.37-0.38 em, where TeX lowers a subscript 0.15 em (0.25 beside a superscript) and raises
a superscript 0.41 em - superscripts agree, subscripts sit 0.2 em low, and `baselineOffset` has no
other value; the `crowded` finding on slide 17 stays. (Both since changed: see below.)

### Text fit follow-up: small caps at 1.13, subscripts after all (2026-09-23, 50e97c9, a35bbad)
Small caps went to **1.13 x** (about the square root of 1.28), the user's choice between width and
height: the torture line is 0.878 of the PDF's width and its capitals ~13% taller than CMCSC's; the
`width` finding on slide 4 is that compromise, recorded in the baseline.
The `crowded` subscripts turned out not to be the API's offset alone. FontMapper set the CMSSI8
subscript (10.9 pt) at 23.4 pt against a 21.2 pt body, so Slides lowered it 8.7 pt (TeX: 3.3), and the
vertical model laid out every wrapped line with the paragraph's largest run, so the pitch came out
24.3-24.75 pt against the PDF's 26.9. A subscript is now no larger than its paragraph's body size
(`emit.run_sizes`) and the pitch is solved per line (`emit.line_sizes`, `inner_pitch`). Rejected, by
`tools/probe_subscripts.py`: Unicode subscripts (empty boxes in Lato, PT Serif and Arial), a .pptx
`baseline` offset (imported as plain SUBSCRIPT), extra line spacing (moves every line). Result: white
gap between the lines 1 -> 6 px (PDF 12), line 3's baseline 162 -> 169 px (PDF 168), finding gone;
13_inline_math p2 overlap 0.408 -> 0.562 and its `width` finding gone, alignment unchanged. Left:
the same paragraph's line 2 wraps differently (`width` 0.912: TeX's math spacing around = and -
against Lato runs), subscripts in table cells, CMMI letters sized above the body enlarging their
line, and `deck_ir` taking a paragraph's first baseline from its largest run.

### Width prediction (2026-09-23, d14d631, `FontMapper.shape_ratio` replaces `number_ratio`)
Measured the Slides/PDF width ratio d of every run and paragraph relative to the calibration
sentences (826 runs, 521 paragraphs over 84 PDFs). Runs changed per rule, at 15 characters and at 10:
tolerance 0.05 -> 5 / 32, 0.06 -> 1 / 17, 0.07 -> 0 / 10. Chosen: 15 characters and 0.07, so today's
decks are unchanged and only outliers (serif, bold and italic capitals, repeated wide letters) move.
Per run, not per paragraph, so that `deck_ir.pdf_size` can invert it from text and style alone; a
paragraph with one outlier run gets mixed sizes. Still open: torture p3t2 line 7 (a wrapped line of
w, predicted 1.095, measured 1.103, hidden behind the small-caps line at 0.878) needs per-line
knowledge; p16t1 wraps differently because inline math is much narrower in Lato than in TeX (Lato
italic x 0.469 em against CMMI's 0.571, L 0.473 against 0.68, plus TeX's spacing around = and -):
the formula is ~79 pt in Slides against ~105-150 pt in the PDF. Lone CMMI letters get the italic
half correction (x1.037) and so stand at 22.1 pt in a 21.2 pt body, enlarging their line for no
width gain; capping them in `run_sizes` would need `pdf_size` to know the run is math.

### Tables from the .pptx (2026-09-23, 6208676, 60ad0fd)
The table-tight (4.3 pt) and table-merged (3.2 pt) `grown` findings are closed: an API table's
7.2 pt cell padding cannot be set, but a .pptx table's margins survive import and every edit the
converter and a person make (docs/calibration.md "Table cells"), so convert puts each table into the
.pptx empty with per-row top margins that put the baseline on the PDF's, duplicates it in phase 1
(`b2s_sNNN_tabI`) and fills it as before. A `p{}` cell is one cell of several lines (`row_lines`,
`wrapped`, `cell_runs` rejoining hyphenated words), the column sized per PDF line: in table-merged
Slides wrapped "paragraph" whole where TeX hyphenated it. Table cells take `run_sizes` too. Same
decks with the parent commit and this one: 15_plain_tabular overlap 0.727 -> 0.738 and table edges
to 0; 16_colored_table pages 1-4 +0.02..0.05, bottom 0.9 -> 0; 11_research_talk p3 0.581 -> 0.697,
bottom 1.8 -> 0; 12_metropolis unchanged. What remains on 16's "Math in cells" (grown 3.9 -> 3.2 pt)
is λ_max's subscript depth, not row height. Sync still creates API tables (a table cannot be copied
across presentations; `DeckPlan(pptx_tables=False)`), so a table sync adds has the old padding.

The live sync scenario `untouched` (v1 -> chain, whose `tablecell` changes 3.9 s to 4.7 s) then
failed on Results: the synced thumbnail differed from a fresh conversion in 1.37% of pixels, rows
~7 pt apart. A unit the source changed is recreated, and the recreated table was an API table
beside the fresh conversion's .pptx one. Fix: emit's state and the base carry each imported
table's margins (`table_margins`), and `sync.table_refill` keeps the table's object when the grid,
merges, fills and margins (`emit.pptx_table` of the new IR, within 0.05 pt) are unchanged: the
cells with text are emptied (deleteText per cell) and `table_requests(imported=True)` fills them.
The object is treated like a placeholder refilled in place (kept out of the cleanup, its read-back
saved in `pending.in_place`, no geometry override since it stays where the deck has it) and the new
base keeps the margins the table *has* (a new conversion's can differ by a rounding: 5.03 against
5.01 when a line was added above). A table the source moved too (a line added above it) gets one
RELATIVE translation by the source's move, unless the deck moved it (a geometry override: merge's
"delta" when the source left it, "theirs" when both moved; either way the deck's place stands). A
base from before this change has no margins: its tables (API tables then) are still recreated.
`tests/test_sync.py::test_a_table_whose_words_changed_is_refilled_in_place`, live scenario
`table-moved` (variant flag `tablemove` in tests/decks/sync/talk.tex). That scenario found a gap
that is not the table's: a title's box is emitted up to the mirror of the slide's leftmost body text
(`emit.text_right_limit`), so the centred line the variant adds narrows a *fresh* conversion's
title to 474 pt where the synced deck kept v1's 707: the title's IR was the same, so the merge saw
no source change. Now `sync.mark_widths` (in `build_ours`) works out the frame emit would give each
paired text element (`emit.text_box_frame`) on the base's slide and on the new one, with today's
fonts, whenever what the box depends on around it (`text_right_limit`, `title_bar_under`) moved; a
frame more than 0.5 pt off marks both sides `fields["width"]`, which `identity.source_changes`
reports as a `width` change only when both carry it. It is worked out from the base's own IR, so an
old base needs nothing new, and a mark new_base copies into the saved base is cleared by the next
sync before it compares. Only elements whose IR is otherwise unchanged (or only moved - a `move`
would keep the old width) are marked. The unit is then recreated like any source change (a title
placeholder is refilled in place with the new frame; the deck's edits win as usual).
`test_a_title_whose_box_a_new_neighbour_narrowed_is_a_source_change`, live `table-moved` with no
allowance.

Rows and columns: a table without merges or fills whose grid the source changed is refilled too
when `table_steps` finds row inserts (insertBelow the row whose margins the new one needs) and
deletes that give every row the margins the new conversion asks for; columns come and go at the
right edge (a cell's margins are its row's). The steps go between emptying the cells and filling
them. A booktabs table growing a data row at the end is the common case (the new row copies the
0-margin row above). A new first row (a new rule above it) finds no neighbour and is recreated.
`test_a_table_the_source_added_a_row_to_is_grown_in_place`, `test_table_steps_*`, live scenario
`table-row` (flag `tablerow`). That a row inserted below another takes *its* margins is what the
live scenario's thumbnail comparison checks; the probe only grew a table of equal margins.


## Emitted-output diff

Sync decides what the source changed per element (`identity.source_changes` over the element's
own IR), but where emit puts an element also depends on its neighbours. `sync.mark_emitted`
(called at the end of `build_ours`, replacing `mark_widths`) works out, for every paired slide on
which anything changed, what emit would write for the slide twice with today's code - from the
base's IR of the slide and from the new one (`emit.slide_emission`: one slide through
`DeckPlan.slide_parts`, no deck-wide work) - and compares element by element
(`sync.emitted_elements`, `context_changes`, `_close`). Only elements whose own IR is unchanged or
only moved are compared; a moved one is compared less its own step (translations and picture boxes
shifted by the IR's bbox step, EMU lengths within WIDTH_MOVED = 0.5 pt, other floats within
0.05 + 0.1%, integers such as text indices exactly).

Normalised away: object ids (element ids by the element's key, the slide's as `@slide`, template
copies by their template key, `_blk`/`_rules` group ids), links to other slides (all to one stand-in
page: which slide a link names is the element's own IR, `ir_fields` keys it), placeholder and
template sizes (made up, as `plan_offline`), z-order requests (sync restacks from the source's
order). Left out: `measure_places` moves - they are measured on Google's scratch slides, so pictures
keep their predicted places (`DeckPlan.placed`); a recreated unit with holes is re-measured anyway.
Base elements the deck keeps though the source dropped them (`removed`) are not in the base's
context. A base IR today's emit cannot read (an older converter's) gives no marks.

Fields (`identity.CONTEXT_FIELDS`, counted only when both sides carry them, so a mark saved into
a base says nothing; stale ones are cleared): `width` (a text's requests differ - in practice its
frame: `text_right_limit`, `title_bar_under`), `placed` (a picture's predicted box: formula
shifts, overlay boxes - always inside a unit that changed anyway), `emitted` (any other kind's
requests; none found). `merge.plan_unit` treats them as any unknown field: recreate, deck overrides
re-applied (geometry as a delta). A title recreated for `width` goes back into its live placeholder
(`in_place`), which the live table-moved scenario exercises.

Not marked, returned as `build_ours(...)["context_unwritten"]` ([{slide, element, fields}]),
because recreating the unit cannot write them:
- `grouping`: the block or rule groups emit would put the element in (`block_groups`,
  `rule_groups`). A recreated unit goes back into its old group (`Sync.regroups`) and
  `update_slide` drops emit's slide-level group requests, so a text the source added onto a block
  is never grouped with it. Writing it needs a slide-level step: compare emit's block groups for
  the new slide with the live groups and ungroup/regroup the block with its new members.
- `placeholder`: the element goes into another layout placeholder or out of one (the title page's
  subtitle is its biggest plain text, `subtitle_element`, so a longer line added below the title
  takes it). Worse, the path is broken today whatever the marks say: an old subtitle the source
  reworded while a longer line took the role is recreated `in_place` into the SUBTITLE placeholder
  but emitted as a box of its own, so sync sends a createShape under the live placeholder's id
  (pinned as strict xfail `test_a_reworded_subtitle_that_lost_the_placeholder_is_not_created_under_its_id`).
  Writing it needs `update_slide` to hand the placeholder to the new subtitle (refill in place)
  and create the old one as a box.

Measured (2026-09-23): the 54 built decks (286 slides, 1,376 elements) and the 29 sync-talk PDFs
against bases of their own entries - renumbered 7 pages on, JSON round-tripped, fast path off -
emit element by element exactly the same (every one of the 286 raw emissions differed before the
normalisation); nothing marked (`tests/test_emitted_diff.py`). Sync talk variants against v1: only
table-moved and tablemove mark anything, the Results title's `width` - the same as `mark_widths`.
Dropping each element of every built deck in turn: `width` on titles when the leftmost body text
goes (188), on texts when their panel, a margin-setting text or a picture beside them goes (124),
`placed` inside changed units (60), and `grouping` (≈400) / `placeholder` (12) as unwritten.
Cost: nothing changed on the slide → skipped (0.1 ms per deck); a changed slide costs two
emissions, ~2 ms each (mixed or addframe vs v1: 18 ms in a 0.75 s `build_ours`).

Known limit: the base's context is its slides' IR, which after a sync mixes the new IR with units
kept in conflict (their old IR). A neighbour whose emission depends on such a unit is marked and
rewritten on every sync while that conflict stands (nothing lost, requests repeated). Fixing it
needs the base to record the context each element was written in.

## Layout probes (2026-09-23)
Losing nothing is not the same as looking right. These probes ask how a synced deck can *look*
broken, with every word and picture still there: things moved, overlapping, or running out of
their boxes. Each hypothesis is a live scenario, `layout-*` in `tests/test_sync_live.py`. Most use
three `[t]` probe frames in `tests/decks/sync/talk.tex` behind the flag `probes`, and their source
edits are the flags `probereword`, `probepush` and `retheme` (variants `probes`, `probes-reword`,
`probes-push`, `retheme`). The existing variants' .tex files are byte-identical.
`test_probe_variants_classify_as_intended` checks the probe edits against the `probes` base
(`build.PROBE_INTENDED`).

Measuring (`devtools/probe_layout.py`, shim `tools/probe_layout.py`):
- A read-back box is not where the ink is. A text box does not grow with its text: no autofit
  survives the .pptx import.
- `inks` duplicates the slide once per set of objects, deletes everything else on the copy, and
  diffs its thumbnail against an empty copy (pixel difference > 48). What is left is those objects'
  own ink in slide pt (720 pt wide, LARGE thumbnails, 2.22 px/pt).
- `overlap` cuts the page into 6 pt strips. In every strip where both inks sit, it takes the
  vertical gap between them. `clearance_pt` is the smallest of those gaps, negative when they
  overlap. A pixel intersection undercounts words on words, because lines interleave: a visible
  collision measured only 2.7 pt.
- The scenarios call it a collision when the clearance is under 1 pt (`CLEARANCE`).
- `holes` paints each formula hole of a text (a Roboto Mono NBSP run) its own background colour on
  a copy, and finds where Slides sets it.
- Every copy is deleted before returning.
- Numbers go to `<scenario>/layout.json`, thumbnails to `<scenario>/layout/<stage>/slide.png`, and
  after the sync `vs-fresh-<title>-synced.png` / `-fresh.png` against a fresh conversion of the new
  source.

| | scenario | verdict | numbers |
|---|---|---|---|
| H1 merged text overflows its box | `layout-grown-box` (probes -> probes-reword) | refuted | box kept at the person's 80.3 pt height (mode `delta` keeps their resize): ink 13.5 pt inside it, clearance to the figure 38.7 pt as before |
| H1b the same, source moved it too | `layout-grown-box-moved` (-> probes-push) | **confirmed** | box back to 53.5 pt (the person made it 80.3): the words run 13.2 pt past its bottom |
| H2 person's position vs a source reflow | `layout-reflow` (-> probes-push) | **confirmed** | clearance between the two boxes' words 29.7 pt -> -1.3 pt (glyphs touch across 52.6 pt); the second box stands inside the first's box [10.6, 55.7, 720.6, 140.2] |
| H3 stranded formula picture | `layout-stranded-formula` (v1 -> formula) | **confirmed** (see below) | hole vs picture dx: -1.7 pt converted, -72.2 pt after the person's edit alone, -72.1 pt after the sync that recreated and re-measured the unit |
| H4 recreated child of a moved group | `layout-group-moved` (v1 -> figure) | refuted | the new figure lands exactly on the person's box [48.9, 170.3, 262.0, 283.8], in the person's group |
| H5 theme change vs layouts | `layout-retheme` (v1 -> retheme) | **confirmed** | layout pictures unchanged (the same 12 ids); Merging text, Convergence and Conclusions differ from a fresh conversion in 10.5-10.8% of pixels |
| H6 display math vs a moved paragraph | `layout-display-math` (-> probes-push) | **confirmed** | clearance between the equation and the paragraph after it 20.2 pt -> -10.8 pt |

The five confirmed ones are strict xfails (`XFAIL` in test_sync_live.py, reason in words); the two
refuted ones stay as passing guards.

Mechanisms:
- **H1b, H2 and H6 are one mechanism**: geometry mode `theirs`. When the source moved a unit the
  person also moved, `merge.plan_unit` (merge.py:883-888, `both = "position" in src`) keeps the
  person's place, reported as the conflict "deck position kept". `Sync.override_requests`
  (sync.py:2177-2180) writes it as a pure translation to the person's top-left
  (`[1,0,0,1, theirs.x - new.x, theirs.y - new.y]`).
  - The person's **size** is lost (H1b, `out/sync-tests/layout-grown-box-moved/layout/after/slide.png`):
    the recreated box has the converter's height. This is also a quiet edit loss: the report says
    the deck's geometry was kept.
  - The person's absolute place ignores what the source did around it: nothing gives way.
    - H2 (`layout-reflow/layout/after/slide.png`): the first box grew by a line into the second.
    - H6 (`layout-display-math/layout/after/slide.png`): the equation picture (an image of its own,
      role math, recreated at the converter's coordinates, 31 pt lower) lands on "and this
      paragraph comes after it.", which stayed where the person put it.
  - A fix would carry the source's move along with the person's offset (base -> theirs applied on
    top of base -> ours), and keep the person's scale, as `delta` does.
- **H3**: `Sync.measure_places` (sync.py:895, 1330-1351) measures the holes on scratch slides for
  the *source's* slide (`self.plan.deck`). `override_requests` (sync.py:2143-2153) then writes the
  merged text, which carries the person's words. Nothing measures again, so the formula picture
  stays where the source's text had its hole.
  - Thumbnails: `layout-stranded-formula/layout/{converted,before,after}/slide.png` and
    `holes-*.png`.
  - Caveat: the person's own edit strands the picture just as far (Slides moves the words, not the
    picture). The sync does not cause the 72 pt; it recreates and re-measures the unit and still
    does not repair it.
  - The second formula (the circled 2, in an unedited paragraph) stays at -1.7 pt.
- **H5**: sync never writes layouts or the master.
  - `new_layout` (sync.py:1527) only picks among the existing layouts. `background_requests`
    (sync.py:1820) writes each slide's new background picture.
  - The old theme's frame-title bar is a layout picture, and it draws over any slide background.
  - What the synced slides show: the old blue bar (to ~43.6 pt), the lower ~7 pt of the new, taller
    pink bar peeking out from under it (to ~50.4 pt), and the new dark red title colour on the old
    dark blue bar.
  - Thumbnails: `layout-retheme/layout/after/vs-fresh-conclusions-{synced,fresh}.png`.
  - The title placeholder boxes are the same as a fresh conversion's.
  - This is not a person-edit case: any theme change that sync applies looks like this.

Side notes:
- **H4**: after the person's 0.8 resize of the group, the words' ink (bottom 265 pt) runs past their
  box (238 pt). Scaling a group scales the boxes, not the font size. That is the person's edit and
  Slides' behaviour, not sync's.
- **H6's premise**: "display math in the background picture" does not hold for the current
  converter. A display equation is an image element (role math) and is recreated like a figure.

## Layout oracle (2026-09-23)

`loss_oracle` asks whether the person's work survived a sync; `devtools/layout_oracle.py` asks
whether the slide *looks* broken after it where it did not before: words over words, a text out of
its box or its block, a formula picture off its hole, something pushed off the page. Same inputs
(base, before, after, report, ours) and finding shape, `check()` for what this sync introduced and
`existing(before)` (all notes, `by` converter or person) for what was already so.
`tools/layout_oracle.py <archive> [...] [--json] [--out f] [--existing] [--notes]` replays recorded
fuzz steps (the new conversion rebuilt from `<step>/ours/deck.json` without the PDF,
`ours_from_folder`: 618 steps in ~25 s) and prints counts per kind, each finding with its step's
edits and variant, and which edit kinds and variants precede fail findings (`correlate`).
`LiveRound.step` writes `layout.json` per step and adds fails to the round's problems as `layout:`.

**The model.** A read-back has no line positions and Slides boxes do not autofit, so the lines are
laid out the way emit predicts them: `emit.ADVANCES`, greedy wrap inside the insets, `emit.line_pitch`
/ `pitch_between` plus spaceAbove/spaceBelow (none between two bulleted items). The read-back lists
*distinct* paragraph styles in first-appearance order: one style is everybody's, as many as
paragraphs is one each, else the shortest stands in (errs towards "fits"). Before per-paragraph
styles the model put a formula hole 14 pt off on a two-paragraph text; with them, on the 72 unedited
converter boxes of the archive, it predicts the PDF line count of all 117 paragraphs and finds every
converter-placed formula picture within 1.9 pt of its hole. Ink is a band per line (0.72 em above
the baseline, 0.2 below) as wide as the words, so boxes that overlap with their words clear of each
other are fine (the archive has 68 such pairs of a moved box and a re-laid one).

**Kinds.** `text_overlap` (two inks meet by 2 pt each way after, did not before, and the new
conversion does not draw them meeting; 2 pt of two texts' bands is a fail, a picture's box needs 4
because it has margins); `text_overflow` (the same where the meeting line is laid out below its own
box, or a text sits on a filled panel and its ink runs out of the panel's bottom); `stranded_picture`
(an anchored formula picture more than 8 pt across or 0.6 of a line off its hole: judged when it
was over it before, or when the sync created, recreated or moved it and the person had not put it
there themselves); `off_page` (more than 6 pt outside the page, on the page before). Excuses are all
"it was so before": a pair that met before is excused even when it now meets deeper (in the archive
that is a person's copy of a text laid over the original, which then gains a line). Notes: an
uncertain slide, a new element with no `ours` to ask, a thin overlap. `table_growth` was dropped: a
table read-back has no column widths or row heights, so no cell can be laid out.

**Archive (618 steps under out/sync-fuzz with its shrink folder, fuzz-live-refill, fuzz-live-widths):**
14 fails, 0 notes, 1,819 existing notes. text_overflow 2 fails / 20 existing; stranded_picture
12 / 245; text_overlap 0 / 729; off_page 0 / 825. What happened to the candidates: all 822 text
overlaps already met before the sync; all 819 off-page objects were already off the page before (the
person's footers with appended sentences); of 157 stranded pictures, 113 were already stranded and
not touched by the sync, and 32 were stranded where the person had moved them. Two defect classes,
both real:
- *A formula picture rewritten at the source's place, not over the hole in the deck's text.*
  shrink/r903 step5: the person had moved the formula paragraph down 15 pt with its pictures; the
  sync kept the text there and recreated both pictures at the source's place, 15 and 16 pt above
  their holes. sync-fuzz r905 steps 6-7, r1104 steps 5-7, r619 step2, fuzz-live-refill r2703
  steps 0-1: the pictures were already off (the person had moved and retyped the text) and the
  sync recreated them at the same wrong place. Agent D's live `layout-stranded-formula`: the person wrote "perfectly" before the hole,
  and the recreated picture lands 72 pt short of it.
- *Merged words overflowing a box sized for the source's words.* fuzz-live-refill r2700 step1
  (blockedit; the person's resize_font to 20 pt merged into the recreated body box) runs 22 pt
  out of the bottom of its block; sync-fuzz r908 step7 (the source reworded, the person had
  appended a sentence) wraps to a second line 14.7 pt out of it.
Agent D's live `layout-reflow` is the third shape (a source's added line grows box 1 into box 2
that the person moved; deck position kept, ink bands meet by 3.2 pt, D measured 2.7 on pixels):
it fires as `text_overlap`, and nothing in the archive has that shape. Too few fails (11 steps)
for the per-edit correlation table to say much; the finding's `history` says more: in 13 of the
14 fails the text involved carries `person_moved` (and mostly `person_text`) from an earlier step,
and the 14th (r2700) the person's font size from the same step. Every fail is on a unit the sync
recreated.

**Sensitivity**, by breaking each archived `after` on purpose (one per step): a converter text moved
onto another's first line is caught 618/618, words appended to a text on a panel 607/607, a formula
picture pushed one line down 540/545 (the 5 were already stranded where the person had put them,
excused by design), a text pushed 20 pt past the right edge 559/618 (the 59 were already off before).

**Blind spots.** Tables (no geometry in the read-back); text in groups and turned boxes (not laid
out); a picture moved onto *another* hole of its own text; placeholder text whose size the IR does
not give; paragraphs whose style the distinct list cannot assign (the shortest is assumed, so a
real overflow can be missed); pictures and shapes meeting each other (only pairs with a text are
judged); anything the offline fuzz world does, since its read-backs have no run styles, no box
types and boxes not sized by emit's metrics - running the oracle there needs the world to size
and style its text boxes like emit first.

## Both-moved geometry (2026-09-23)

The rule: a unit the source rewrote and the person moved or resized is recreated at the source's
new place and then takes the person's edit, **whether or not the source moved it too**. Merge says
geometry mode `delta` in both cases; sync writes `sync.carried(base, theirs, new)`: the person's
transform change `theirs * base^-1`, conjugated by the source's step `s` of the unit's corner
(`T(s) * d * T(-s)`). A move lands at the source's new place plus the person's offset; a resize
keeps the person's proportions of the source's new box, from its new corner (before, `delta` scaled
about the page origin, so a resize scaled the source's move with it - harmless while `delta` only ran
when the source had not moved the unit, and `s` was 0). When the source moved it too it is still a
reported geometry conflict, resolution `merge.GEOMETRY_CARRIED` ("the deck's move and size kept, on
top of the source's move"); `--take-source` on it writes the source's place and size, as before.

Why the old rule (`theirs`: a pure translation to the deck's absolute top-left) went:
- History: it came with the first sync commit (53739b9, 2026-09-17) as the obvious "deck wins" for
  a field both sides changed. No test, fuzz seed, live scenario or note ever depended on the
  absolute place; the only test named it (`test_both_moved_deck_position_wins`), and the loss
  oracle's docstring already described the carried place ("at the conversion's new box with the
  person's move on top of it") - the oracle was simply excused by the conflict.
- It dropped the person's size silently: the translation carried no scale, while the report said the
  deck's geometry was kept (H1b).
- An absolute place ignores the reflow around it. A source's reflow moves the converter's
  neighbours; pinning one unit makes the neighbours run into it (H2, H6). "Deck wins" is about what
  the person *did*, which was an offset and a size, not a coordinate.
- The case where the absolute place would matter is a unit the person placed against something only
  the deck has (their own box or picture), which the source's reflow does not move. Nothing in the
  history shows it. It is not special-cased: "placed against" cannot be read off a deck without
  guessing, and the words the person owns are already protected where it counts, by occlusion
  (`sync.would_hide` restacks a recreated unit under words only the deck has). A carried unit can
  also be pushed further towards the page edge than the person had it; the layout oracle's
  `off_page` judges that. Both are residual, reported (it is a conflict), and undoable in one drag.

The same rule for a .pptx table refilled in place (`update_slide`, `table_refill`): its object stays
where the person has it; when the source moved it too, the source's shift is applied on top (it was
zeroed for any geometry override before, `u["source"]` now tells the two apart).

Live, the three probes that were strict xfails (numbers from `layout.json`, before -> after the fix):

| scenario | old code | now |
|---|---|---|
| layout-grown-box-moved (H1b) | box back to 53.5 pt, words 13.2 pt past its bottom | box 80.3 pt (the person's), 13.4 pt of room left, moved down with the source; clearance to the figure 38.7 pt as before |
| layout-reflow (H2) | ink clearance 29.7 -> -1.3 pt | 29.7 -> 29.7 pt: the second box keeps the person's 40 pt to the right and goes down 31 pt with the source |
| layout-display-math (H6) | clearance equation/paragraph 20.2 -> -10.8 pt | 20.2 -> 20.2 pt (above/equation 20.7 -> 20.7) |

Judges:
- `devtools/fuzz_world._place_unit`: the person's step on top of wherever the source put the unit,
  also when both moved it (its deck edits are moves, never resizes, so the translation is all of it).
- `devtools/loss_oracle.geometry_findings`: the carried conflict is no longer an excuse. Its corner
  must be the person's plus the source's move of the IR corner (`_carried_to`, 2 pt,
  `geometry_not_carried`); on the archive (618 steps) that prediction held within 2 pt for 1,522 of
  1,556 elements the source moved and nobody else did, and every one the report only lists as
  `applied` within 0.3 pt; the 34 others (3 to 18 pt off) all carry a conflict: kept, not written. A
  person's resize that comes back at the converter's size on an object the sync made, while the
  source left the size alone, is `geometry_reverted` wherever the element went (`_resize_dropped`; objects the sync
  made only - a person's copy keeps the element's tag and the converter's size, which the archive's
  r1101 step5 and r801 step4 showed). `deck_placement` asks pictures alone when there are three:
  with text boxes in the median it read a real deck's 1.59 as 3.09.
- Sensitivity: the offline campaign with the applier put back to the absolute place (merge reporting
  the carried conflict) fails every round that had a both-moved unit (5 of 100 three-step rounds,
  all `geometry_not_carried`). With today's applier: 300 three-step rounds on each of converted,
  adopt and `--first-sync`, and 300 twelve-step converted chains, clean. The archive replay gives
  the same geometry findings as main's oracle (none new).
- `tools/layout_oracle.py` on out/sync-fuzz still runs (590 steps, 11 fails, as recorded by the old
  code), but found no new conversion for any step: `ours_from_folder` imports `sync.mark_widths`,
  renamed `mark_emitted` by 401cb35, and the ImportError is caught per step. With the name aliased
  the same 11 fails come out and every step has `ours`.

The one live scenario that held the old rule: many-edits moves "Later the source changes again" down
15 pt while the source rewrites that box and moves it up 2.9 pt. Its generic `move` check wanted the
deck's absolute place (y 143.4); the box now lands at 140.5, the source's new place plus the
person's 15. The scenario drops the `move` check and asks for the fresh conversion's corner + 15
instead (the first sync still reports the geometry conflict; the report the test reads last is the
idempotence sync's, where nothing is left to conflict). Passing live with the new rule: disjoint,
same-element, chain, table-moved, pull-wording, many-edits, layout-grown-box, layout-group-moved
and the three probes above.

## Layouts and placeholders in sync

Three defects of 2026-09-23, fixed in that order.

**The subtitle placeholder.** `Sync.update_slide` handed the live SUBTITLE placeholder to whatever
element had the subtitle role, and emit gives the title page's subtitle line and the author block
different homes depending on what the page holds (the `subtitle` variant of `tests/decks/sync`
adds a subtitle line). The placeholder now goes to the element emit itself would put in it
(`context_changes`, `same_layout`), and the others become boxes of their own. Live:
`subtitle-role`.

**`context_unwritten` said nowhere.** What `build_ours()["context_unwritten"]` lists (a group
membership or a placeholder role that recreating a unit cannot change) becomes a report warning
in words (`sync.UNWRITTEN_SAYS`, `unwritten_warnings`). The agent tools' `Result` carries it with
the other warnings.

**A theme change never reached the deck** (probe H5, `layout-retheme`). Before the fix, the
synced frames showed the old blue title bar with the new, taller one peeking out from under it.
Merging text, Convergence and Conclusions differed from a fresh conversion in 10.5-10.8% of
pixels. `theme_sync.py` now merges the master and the layouts three ways.

- **Base.** `base["theme"]` records the following. The schema stays version 1, because the key is
  optional.
  - `fill`: the master's fill key, with its read-back and, for a picture fill, its signature.
  - `shared`: the shared background key.
  - Per page (the master as `M` and each layout by id):
    - its decoration group. `TITLE` and `*` are emit's `plan_theme` names, and a `*_V1` clone's
      name gives its group.
    - the decoration picture's id, sha1 and a 32 x 18 RGB thumbnail. The thumbnail tells "the
      same picture re-encoded" (Google does that) from "another picture".
    - its read-back box and content hash.
    - each TITLE / CENTERED_TITLE / BODY placeholder's spec, as `emit.layout_style_spec` gives it,
      with its read-back box and style hash.
- **Why the style hash.** `snapshot.read_text` skips runs that are only "\n", and a layout
  placeholder holds nothing else. So `theme_sync.style_hash` hashes every run's and paragraph's
  style. With the old hash, a person's recolour of a layout title was invisible (caught by the
  offline test before any live run).
- **What a fresh conversion writes.** `emit.master_plan`, `layout_style_spec` and
  `layout_placeholder_requests` are new helpers that make the same decisions as `build_deck` and
  `style_layout_placeholders`. `test_theme_sync.py` holds the requests equal to what convert
  sends. `theme_sync.ours_side` computes the fill, the shared key, the specs and one picture per
  decoration group from the new PDF.
- **Per item.** Each item (the master fill, each page's decoration, each placeholder) is
  written when only the source changed it, and kept when only the deck changed it. When both
  changed it, the deck's version is kept and the conflict is reported with an id:
  - `layout Title Only / title style`;
  - `layout Blank / theme decoration`, with why `moved to [...]`, `deleted` or `another picture`.
- **Pending.** The pending marker lists the placeholder specs being written. A run that died
  after the theme batch therefore finds its own write, not a person's edit.
- **Probes on the live API (2026-09-23).**
  - `replaceImage` CENTER_INSIDE keeps the id and the box, but drops the description, so the alt
    text is written again.
  - `createImage` on a layout followed by `SEND_TO_BACK` works.
  - A slide goes back to inheriting the master only through fields
    `pageBackgroundFill.propertyState`. Fields `pageBackgroundFill` with INHERIT is refused.
  - The master accepts both solid and picture fills.
- **Slides drops a run property equal to the one the run inherits**, on writes as on the .pptx
  import. Beamer sets the title page's title and the frame titles at one size (`\Large`), so the
  imported title-page title has no size of its own. Restyling the master's TITLE for a retheme's
  `\huge` frame titles grew it too: the title slide differed from a fresh conversion in 1.59% of
  pixels.
  - `theme_sync.inherited_pins` writes the old inherited values onto the converter's slide
    placeholders.
  - The pins go *after* the layout requests in the same batch. Written before, they equal the
    inherited value and vanish. That happened in the first live run.
  - The frame titles the merge refills with the new style fold back into inheriting, exactly as
    in a fresh conversion.
  - `Sync.new_base` takes the pinned read-back into the base for objects the person had not
    restyled. Otherwise the next sync reads the pins as a deck edit.
  - A slide the person added keeps following the theme.
- **Which decoration a layout serves.** A layout serves the decoration group that most of its
  live slides want (`page_group`). A slide that wants another one is a warning, not a layout
  change: moving a slide to another layout re-parents its placeholders.
- **Order and backgrounds.**
  - The theme batch opens the content chain, so no layout write is in flight beside a slide batch.
  - Slides that showed a copy of the old shared background go back to INHERIT when they show the
    new one.
  - New slides take the layout that serves their group.
- **Old base** (no `theme`): nothing is written on the layouts, as before. When the new shared
  background differs from the one the base records, a warning says the layouts were left as they
  are and a slide whose background changed got the new one as a picture of its own
  (`old_base_warning`). The next sync of that base still has no `theme`: only `convert` records it.

After the fix:
- `layout-retheme`: the title page, Merging text and Convergence are the same as a fresh
  conversion. Conclusions carries the person's word, so only its title box is compared, and that
  box is equal. The sync applied 53 source changes with 0 conflicts in 464 requests, and the
  strict xfail is gone.
- `layout-edited-retheme` (new): the person recolours the TITLE_ONLY title placeholder and moves
  the BLANK decoration by 20 pt, and the source rethemes.
  - The sync applied 51 source changes and reported 2 conflicts. The green title and the moved
    picture stay, and the TITLE_ONLY decoration is the new one.
  - The idempotence sync sends 0 requests and reports the same 2 conflicts.
- `untouched`, `slides` and `subtitle-role` pass.

Still open:
- Layout texts (`write_layout_texts`, shared headers and footers) are not synced.
- A variant layout that a fresh conversion would newly clone is not created.
- A slide whose decoration variant changed stays on its layout.

## Merged text into recreated boxes

The two failure classes the layout oracle found have the same cause. When sync recreates a unit
whose words both sides changed, it builds the unit for the *source's* words: the box is sized for
them, and `measure_places` puts each formula picture over the source's hole. `override_requests`
then writes the merged words into it (the person's sentence, the person's 20 pt) and nothing looks
again. So a picture stays where the source's hole was (live `layout-stranded-formula`: 72 pt short
of its hole), and the merged words run out of the box and off the block's panel (fuzz-live-refill
r2700 step1, 22 pt; sync-fuzz r908 step7, 14.7 pt).

**The layout model is the library's now** (`text_layout.py`: `layout`, `needed_bottom`,
`span_box`, `text_box`, `filled`). It is the oracle's model moved out of
`devtools/layout_oracle.py`, which imports it back. Sync and the oracle predict lines the same way,
from emit's own numbers (`emit.ADVANCES`, `line_pitch`, `DESCENT_EM`, `extra_below`).

**Refit** (`refit.py`, `Sync.refit`): one more batch after the overrides, and only when some
overrides were sent. `refit_jobs` picks the recreated, non-placeholder TEXT_BOX units whose words
were overridden, with their formula pictures (the unit's `math` members). It reads the deck back
and lays each text out three times: *pre* (the source's words in the box as created), *geo* (the
source's words in the box as it is now, after the geometry override) and *final* (the merged words
as written). Refit makes up only for what the **text** change did (geo -> final). What the
geometry alone did stays the person's: a person who scaled the group rewrapped the words
themselves. The first design used absolute offsets and moved pictures the person had stranded
(r1008, r800, r1002: one picture by 295 pt).
- *Pictures*: each picture is paired with its pre hole (`pair_pictures`: the cheapest assignment
  within tolerance, with the most pairs). The geo hole with the same start is mapped to the final
  hole through a SequenceMatcher of the two texts. The picture moves by the difference of hole
  centre x and of baselines, as a RELATIVE translation; inside a group it is conjugated by the
  group's transform, G^-1 S G (`local_step`). A hole the merged words no longer have is warned
  about, not guessed. (Superseded 2026-09-24: the move is measured in pre's box and written as a
  page step E . T . E^-1, no conjugation; see "Refit in a person's group" at the end.)
- *Box*: it grows by what the final words need beyond the box (`needed_bottom`), minus the
  overflow the source's own words, the geo layout or the person's text before the sync already
  had. That overflow is nobody's new problem: a person's 20 pt footer that already wrapped
  "1 / 10" is left as it was (r1010, r501, r702). A box grows only when it is top-anchored,
  scaled about its top, and never past the page bottom; otherwise the report says so.
- *Panel*: the smallest filled upright shape the text sits on (`sits_on`) that is not the unit's
  own and not being deleted. It grows by what the words need below it plus the padding the
  converter left (measured on pre), minus what already overran before (the same rule; a person's
  20 pt block title grew the title bar 5% before it, r906, r710). It grows only as far as the strip
  below is clear of other objects' ink (a backdrop holding the panel and things drawn on the panel
  don't count). What it cannot give is reported: "... run X pt past the bottom of the panel they sit
  on, which could only grow Y pt: '<words>' is in the way".
- *Base*: the refit steps are the converter's doing, so the new base records them.
  `reshape_base` sets R' = R . F^-1 . S . F for each object and re-derives group boxes. The
  person's own step E (deck = E . base) stays the same on every member of the unit, so
  `geometry_writable` still holds and the next sync writes nothing.

Offline, the fuzz world's read-backs have no run spans and no TEXT_BOX type, so `refit_jobs`
finds nothing and the fuzz is unchanged (300/300 clean, `--chain 3`). `tests/test_refit.py` covers
the planner on synthetic read-backs: a picture following its hole along a line and onto the next,
the conjugation inside a group, a deleted hole, geometry alone, box and panel growth for a
sentence and for a font, a panel blocked by the words below, the person's own overflow, the page
bottom, the base, and a stray picture.

**Replayed** over the archives (the next step's base gives the pre read-back of the created
objects, `after` is the final one; refit's steps are applied to it and both oracles are run). The
layout oracle's fails went from {text_overflow 2, stranded_picture 12} to {stranded_picture 2}, and
the loss oracle is unchanged on every step. The two left are shrink/r903 step5. That run was
recorded at 17:20, before ed8f458 (17:42) fixed the move of ungrouped units that stranded those
pictures, so it is not a text case and can't be replayed. r2700 step1: box x1.777, panel x1.466.
r908 step7: the panel can give 16.9 of the 20.2 pt it needs, because 'Both versions go into the
report' is 3.3 pt below; the words still end inside the grown box, the oracle is clean, and the
report says it.

**Live** (`test_sync_live.py`). `layout-stranded-formula` is out of XFAIL: the recreated picture
now stands (-1.7, 1.5) pt from its hole, as a fresh conversion's does (-1.7, 1.0); it was
(-72.2, 1.0). Two new scenarios on Merge policy sync to `blockedit`:
- `layout-block-sentence`: the person appends "Both are kept.", and the merged body needs a second
  line. Box 30.9 -> 51.1 pt, panel bottom 201.9 -> 222.2, words end 5.7 pt inside it, panel to the
  words below 8.1 pt (28.3 before).
- `layout-block-font`: the person sets the body to 20 pt. Box -> 54.9 pt, panel bottom 225.9,
  words 5.4 pt inside it, clearance 4.5 pt.

Both also run the layout oracle over the sync (`oracle_verdict`, as does the stranded one), and it
is clean. With refit switched off, all three fail. The picture stands (-72.1, 1.5) pt from its
hole. The merged body runs 15.5 pt (sentence) and 19.5 pt (font) past its 30.9 pt box, and the
oracle calls both `text_overflow` off the panel: 14.7 and 22.0 pt, the same numbers as r908 and
r2700.

**Open.**
- Overlays (tikzmark arrows, braces anchored to words) are not refitted.
- Placeholders are not refitted at all (the IR does not give their size). A middle- or
  bottom-anchored box is not grown, and the report says so.
- A panel blocked by what is below grows only as far as it can, and the rest is reported
  (r908: 3.3 pt short). Moving what is below is a reflow, and sync does not reflow.
- Text in tables is not refitted.

## Live fuzzer efficiency

2026-09-23. The goal was for `tools/fuzz_sync.py live` to reach layout cases sooner and more
cheaply. Each round now records what it cost and what it reached. `round.json` holds, per step
and per round, `cost` (seconds, Google calls, retries, 429s, backoff seconds and calls per method,
per phase), `reach` (the layout preconditions of `devtools/fuzz_reach.py`) and `layout` (the
layout oracle's findings, kind:severity). `run_live` ends with a summary: rounds/h, s per step by
phase, calls, backoff, batchUpdates/min, reach and oracle counts. Calls are counted in
`gslides.execute` (`STATS`, `count_thread`), which only observes and changes nothing. The
convert/sync subprocesses run through `devtools/counted.py`, which dumps their counts.

**Never interactive.** A campaign outlived its 7-day testing token. Each round's sync subprocess
fell into `google_auth`'s browser consent flow and waited there, so a round hung for 8 minutes
until it was killed. `counted.never_interactive` now replaces `google_auth.credentials` with the
cached token, refreshed when it expires, and raises `NeedsConsent` when the token is dead. It
replaces the module function rather than using a `use_provider` block, because worker threads
inherit no context. `fuzz_sync live` checks the token before it starts and exits 2 when it is
dead.

**Cheaper steps.**
- `--edits batched`, the default, uses `LiveDeck(defer=True)`. Edits are queued and sent as one
  batchUpdate; when Google refuses the lot, each is sent again on its own, so a refused edit costs
  only itself. The step starts from the read the previous sync step already made. The deck is read
  again only when a drawn edit lands on a slide a queued edit touched, or when slide order matters
  after slides came or went (`fuzz_sync.stale`); the edit is then drawn again from the fresh read.
- Expectations are worked out from the read the edit was found in and never read the deck back.
- `--edits reread` is the old way.
- `--reuse` converts v1 once. Each round gets a Drive `files.copy` of that deck, with
  `b2sBase`/`b2sCleaned` cleared and the folder's JSON rewritten to the new id. Every copy kept all
  72 object ids the base names (checked per round, `reuse_ids`), and every round on a copy was
  clean.

Measured interleaved in one sitting (A, then C). Numbers are per step, except the round means.

| | edits s | edits calls (reads/writes) | calls/step | round mean s |
|---|---|---|---|---|
| reread (A, chain 3, 4 rounds, p2, twice) | 6.5 / 5.0 | 11.5 (6.3/5.0) | 35 | 82 / 74 |
| batched (same seeds, twice) | 2.7 / 2.4 | 4.3 (1.8/2.4) | 26 | 64 / 59 |

| | first deck per round | calls | round mean s (chain 2) |
|---|---|---|---|
| convert (C, twice) | 20.7 s | 19 | 58 / 60 |
| reuse, a copy (twice) | 4.4 s | 2 | 44 / 51 |

Before this work, the main session measured 75-94 s per round at chain 3 and p2, and ~100 s at
chain 2 and p3. After it, a chain-2 round takes 44-51 s. Sync is now ~65% of a step (13-17 s).

**Parallel sweep**: 12 rounds, chain 2, batched + reuse, interleaved 3-5-8-12-12-8-5-3, from
21:03 to 21:33. The raised write quota (600/min) had not reached this token yet. Every 429 was
on `presentations.batchUpdate` (edits 53, sync 51 in one p12 run).

| --parallel | rounds/h | s per step | 429s | backoff s |
|---|---|---|---|---|
| 3 | 193 / 211 | 22.0 / 20.5 | 0 / 2 | 0 / 7 |
| 5 | 149 / 139 | 40.9 / 45.8 | 48 / 38 | 480 / 295 |
| 8 | 221 / 265 | 31.0 / 35.9 | 32 / 52 | 214 / 317 |
| 12 | 247 / 208 | 58.7 / 65.2 | 79 / 104 | 830 / 993 |

- p3 is clean at ~43 batchUpdates/min.
- Beyond p3, rounds start in lockstep and burst past the per-user write limit. Backoff then eats
  the gain: at 8-12, rounds/h rises about 15% while each round takes twice as long.
- p5 loses to p3 because of the last partial wave as well as the backoff.
- Until the quota raise shows in these counts, p3 (p4 at most) is the setting. Rerun the sweep when
  a p8 run shows no 429s.

**Aimed steps.** `fuzz_reach` reads a step's base, before, after and report files. It says
whether the step set up a layout defect's preconditions:
- `text_into_relaid`: the person's longer text recreated into a re-laid unit.
- `hole_reworded`: words changed in front of a formula hole.
- `moved_vs_reflow` / `moved_overlapped`: the person's moved or added box on a slide the source
  re-laid, and whether they overlap afterwards.
- `recreated_in_group`: a unit recreated in a group the person made, moved or resized.
- `table_grows`: a changed table the person edited, or has something near.

`python tools/fuzz_reach.py <archive>` gives the table per variant and per edit kind.

Over the 568 archived steps of out/sync-fuzz (uniform draw), expected steps to the first hit
(1/rate):

| | text_into_relaid | hole_reworded | moved_vs_reflow | moved_overlapped | recreated_in_group | table_grows |
|---|---|---|---|---|---|---|
| archive, uniform | 6.4 | 43.7 | 2.3 | 12.6 | 5.6 | never |
| live uniform (48 steps) | 24 | never | 2.7 | 3.4 | 16 | never |
| `--focus layout` (48 steps) | 1.7 | 3.7 | 1.7 | 16 | 1.8 | 2.7 |
| `--focus probes` (24 steps) | 6.0 | never | 1.2 | 1.4 | 8.0 | never |

The live rows are chain 3, 8 rounds, p4, seeds 7400-7415 (probes: 7400-7407).

Why the uniform draw reaches so little:
- About 55% of its edits can never set up a layout defect: slides, notes, backgrounds, bold,
  colours and font sizes.
- It never edits a table, and it edits footers and titles.

**What focus changes.**
- `--focus layout` draws aims (`FOCUS`, `AIMS`): long text, a new paragraph, words before a hole,
  moved text, a note under a text or a table, grouped moves and resizes, narrowed boxes, table
  cells, rows and columns. Three draws in 16 are still uniform.
- It aims at the slides the step's variant changes 75% of the time (`variant_slides`), drawing the
  variant first. Variants are weighted by what they re-lay (`REFLOWS`, `variant_weights`).
- `--focus probes` starts from the `probes` variant and syncs `probes-reword`/`probes-push`.
  That is where moved-and-overlapped lives.
- The default draw is unchanged, draw for draw.
- The new deck edit kinds are `add_paragraph`, `insert_before_hole`, `insert_table_row` and
  `insert_table_column`. They are in the catalogue and were applied live without a refusal
  (41/11/9/7 times).

Rounds with a layout-oracle failure:

| draw | rounds failing |
|---|---|
| uniform | 3 of 16 |
| `--focus layout` | 9 of 16 |
| `--focus probes` | 7 of 8 |

What they found:
- stranded_picture on merge/motivation formulas (7402-7415). Most were "it was N pt off before,
  and the sync rewrote it there".
- A number ball overlapping its text on "The sync algorithm" after `chain` (7400, 7403).
- text_overflow of the policy slide's second text (7400, 7408, 7411).
- The probe overlaps and overflows (7400-7405).
- A person's own note box moved by 26-31 pt (loss oracle, user_object_moved, 7403 and 7407: the
  box moved one way on probes-reword, then back on probes-push). Resolved as an oracle false
  positive: the "box" was a group the person made of a paragraph and its picture, and the source
  moved both. A group's box is its children's union, so the oracle no longer judges it; each child
  is judged on its own.

**Blind spots.** `fuzz_reach` is a proxy: it says the stage was set, not that anything went
wrong. `moved_overlapped` needs the boxes the person's objects carry after the sync, so a
recreated user object is not seen. `table_grows` has no table geometry, like the layout oracle.
Per-variant attribution is mixed after step 0, because the base is then the previous variant.

## Live fuzz on merged main (2026-09-23)

After the layout campaign merged (2d7a791; full live sync suite 69/69), two focused live
campaigns ran on main: `--focus probes --reuse`, 8 rounds x 3 steps (seeds 7408-7415), and
`--focus layout --reuse`, 12 rounds x 3 steps (seeds 8000-8011). About 30 s per step at
parallel 3+4 together, with 45 rate-limited calls and 287 s of backoff over the probes run: two
campaigns at once are over the write quota even at 600 per minute per user. What they found:

- **The person's arrangement carried back (oracle).** r7413: the person moved a paragraph up 30 pt
  onto the title; probes-push moved it down (apart), probes-reword back up, and carried geometry
  put it exactly where the person had it. The oracle compared against `before` only and called
  it introduced. Now a pair of converter elements, one of which carries the person's move, is
  excused when their `before` ink moved by the source's own move of each (`Pair.carried_rects`,
  in the oracle's own layout model; the title placeholder is laid out 5 pt lower than the PDF
  draws it, so the PDF ink could not be used) meets already.
- **The source running over the person's own note (sync reports it).** r7411, r7414, r8002: a
  note the person put under a paragraph or a figure; the source adds a line or lengthens the words
  and they run 8-16 pt over it. Sync never moves the person's own objects (docs/sync.md), and
  guessing what a note belongs to is the kind of guess the project refuses, so it is said:
  `Sync.warn_about_overruns` reads the deck once after the last write (only when a written slide
  has such an object) and `text_layout.overruns` finds each one the source's text or pictures now
  meet by 2 pt that they did not meet before. The report lists them under `overruns`, and the
  layout oracle makes such a finding a note only when the report names the object. Over the old
  archives the check names 22 such cases, 19 of which the oracle had excused as already touching.
- **A kept unit dropped the source's move (sync bug).** r8006 (table-moved): the source moved the
  results table and changed a cell, the person inserted a row; the text conflict kept the deck's
  table, and `plan_unit`'s `keep` kept its place too, so the caption the source added where the
  table had stood landed on it (5.1 pt). A keep now becomes a `move` when the source moved the
  whole unit by one step and the person did not move it.
- The number ball over its text (7400, 7403) was seen on a thumbnail; no oracle kind covers a
  ball over its own line, so these campaigns could not say whether it is still there.
- **An old deep overlap hid a new one.** r7411 on the fixed code: the person's note already ran
  15.6 pt over the frame counter, so `overruns` (deepest meet before vs after) missed the paragraph
  now 7.8 pt into it. Each other object is judged against its own meet before (2b6c5a1).
- **Re-run on 9f88dd2** (all of the above, r8009 and r8006/r8011 below), from a clean worktree:
  layout seeds 8000-8011 12/12 clean (was 7/12), probes 7408-7415 8/8 (was 5/8); the three
  layout notes are overruns the report names (r7411-r7414). At `--parallel 3`: 18 and 25
  rate-limited calls, against 75 at 4 - one campaign at a time, three rounds, fits the quota.
  Archives: `out/fuzz-verify-layout`, `out/fuzz-verify-probes`.

## Title page role changes under the loss oracle (r8009, 2026-09-23)

Live fuzz r8009 (`--focus layout`, on 9aac0e6) failed the title slide twice: step 1 (variant
`subtitle`) and step 2 (`deletions`) `geometry_not_carried title / text/body/0`, and step 2
`reported_remove_not_done title / text/body/1`. One edit is enough (the shrink said so): the
person moved the authors' lines ("University of Examples", text/body/0) down 30 pt. In `subtitle`
the source moves them up 5.65 PDF pt and a longer line (text/body/1) takes the subtitle role from
them; in `deletions` that line is gone and the authors take the role back. All three were the
**oracle's**; the sync did what the report says.

- **The frame around the words is the placeholder's or a box's.** Sync carries the person's step
  onto the frame the new conversion writes (`sync.carried`: new box minus base box), and emit
  sets a placeholder's frame `PPTX_TITLE_DY` (3.9 pt) lower around the same words than a text
  box's. Out of the placeholder (step 1) the frame moves by the bbox step less 3.9, back in
  (step 2) by the step plus 3.9; the words land at the fresh place plus the person's 30 pt both
  times. The oracle took the bbox step for the frame's and was 3.88 pt off each way. It now
  compares the words' corners (`loss_oracle._corner`: a placeholder's corner less the drop).
- **The placeholder is handed on, object id and all.** Sync gives the live SUBTITLE placeholder
  to the text that has the role now and retitles it (`Sync.update_slide`, `in_place`).
  `element_objects` still counted it as the old element's because it is in that element's base
  `objects`: step 1 printed the other text's box under text/body/0's name, and step 2 said the
  removed text/body/1 was still there. An own object that carries another element's tag of the
  slide now counts as that element's.
- Rejudged offline (`build_ours` on the variants, the archived snapshots): r8009 steps 0-2 clean;
  the old oracle gives back the three findings. Pinned in `tests/test_sync_fuzz.py`
  (`test_a_carried_move_is_judged_where_the_words_are`, `test_element_objects_...`). The offline
  fuzz world has no placeholders, so it cannot reach either.

## Refit in a person's group (r8006, r8011, 2026-09-23)

Two `geometry_not_carried` findings of the layout campaign, both on slide motivation's
`text/body/0` block and its formula picture `image/math/0`, both with the person's edit on the
unit's group and a refit in the same sync. Replayed offline from the archives
(`out/fuzz-main-layout/r80xx/step2`).

- **Slides applies a RELATIVE transform to a group's child in page space** (new = M . absolute),
  whatever the group's transform. Refit conjugated its steps by the group's (G^-1 S G,
  `local_step`), which is the same thing only on the converter's identity groups. r8006 (sync bug):
  the person moved the block (+20, -20); the body grown k = 1.199 about its top came out with its
  top 4.0 pt higher, (1 - k) x 20: 101.5 for 105.48. r8011 (sync bug): the group scaled 1.15; the
  picture moved 134.0 pt of the 154.1 x 1.15 = 177.2 planned, and the deck no longer equalled
  E . base. Steps are now written as the page step itself (`refit._step_request`).
- **The picture's move is measured in the converter's box** (`pre`), between the source's words
  and the merged words, and the person's edit put on top of it: page step E . T . E^-1 with
  E = F_pic . P_pic^-1 (`refit.carried_step`). A group's scale is its box's size, not its text's,
  so measured in the person's wider box the move came out unscaled and slid the picture 0.15 of it
  against its unit every sync (r8011: 23 pt against its own unchanged words). Now the deck stays
  E times what the converter would make of the merged words, and a second sync with the same words
  lands the picture where it was (plus the source's move). How far the person's resize alone
  strands a picture stays theirs, as before.
- **The oracle learns refit's moves from what the sync records, not by forgiving pictures.** r8006
  (oracle wrong): refit rightly took the picture 154.1 pt right, onto its hole in the person's
  words; the oracle expected it at the person's place plus the source's move. The report now lists
  every refit step under `refit` ({slide, element, object, what, shift, grown}; `refit.moves`,
  shift in the converter's frame) and the md report says "Fitted to the words as merged". The new
  base notes the same shift on the read-back (`refit` on the object, `reshape_base` via `_noted`),
  because the base read-back already contains the last sync's refit. `loss_oracle._carried_to`
  puts the person's edit D (was -> now, linear part) on apart + r_new - r_old, where apart is the
  member's IR move less its anchor's (the source's move of the unit's top is added unscaled, as
  `sync.carried` writes it). r8011: r_new = r_old = 154.1, expected 435.72, the new code puts it at
  435.73; r8006: r_new = 154.1, r_old = 0, the new code puts the picture at 402.58 and the text top
  at 105.48. A picture the report says nothing about, or at another place, is still found.
- Pinned: `tests/test_refit.py` (`test_a_groups_child_takes_the_step_in_page_space`,
  `test_in_a_group_the_person_scaled_a_picture_keeps_the_persons_scale`) and
  `tests/test_sync_fuzz.py` (`test_a_picture_refit_moved_is_held_to_what_the_sync_recorded`,
  `test_a_refit_the_last_sync_recorded_is_the_converters_under_the_persons_scale`). The offline
  fuzz world runs no refit, so it writes no `refit` entries and no notes.

## Visual hunt (2026-09-23/24)

An adversarial campaign for slides that look right in beamer and mangled in Google Slides. Nothing
in the converter was changed by it (except `fidelity.ink_box` on rows of specks, found by a hunter:
`tests/test_fidelity.py`); it is a list of what to fix. Report with crops per family:
https://claude.ai/artifact/BLgc6bCF7rPqzLr6vnLwxM. Data under `out/hunt/` (git-ignored): `decks/`
(every .tex), `archive/<stem>/` (PDF and Slides renderings side by side, deck.json, emit.json,
fidelity, text_fit, invariants), `judgements/`, `verified/<hunter>.json`, `crops/`, `KNOWN.md`.

- **Harness**: `devtools/visual_hunt.py run <tex> --slot S` compiles (engine from a first-line
  `% !engine = x`, until the .aux settles), converts into `out/hunt/<slot>` with the PDF copied to
  `out/hunt/pdfs/<slot>.pdf` (the rebuild guard pairs a deck with a PDF *name*, so a slot reuses one
  Drive deck), runs fidelity, text_fit and the offline invariants, and archives per slide
  `pdf-NNN.png`, `slides-NNN.png` (1600 px, same geometry) and `cmp-NNN.png`. At most 4 Google runs at
  once (lock files, `B2S_HUNT_GOOGLE`). `ledger [--verified]` groups confirmed findings by class.
- **Roles** (the measurement-loops skill): 18 hunters, 3-5 realistic decks each over four rounds,
  steered away from repeats by `KNOWN.md`; a **blind judge** per hunter (images only, zoom before
  claiming); a **skeptic** per hunter that merges both lists, crops each claim, rejects what is the
  PDF's own, invisible at presentation size or only a substitute's letter shapes, re-scores severity
  and realism 1-3 and names the mechanism. A **canary** (r0_smoke with a table row erased, a bullet
  moved onto a block title, a red number turned black) went to every judge: all caught all three.
  **Controls**: eight ordinary talks by writers told not to hunt, judged and verified the same way.
- **Result**: 77 decks, 714 slides, 377 confirmed findings, 99 severity 3. Controls: 36 of 105
  slides with a severity 2+ defect, 5 broken; 24 of the 36 are two deck-wide footline defects (a
  clipped Boadilla author box, OT1 "Schr¨odinger"), the rest about one slide in eight.
- **Environment confound**: MiKTeX regenerated `pdftex.map` at 00:31 mid-campaign (cause unknown).
  Before, T1 text without cm-super compiled to bitmap Type 3 fonts; after, to Type 1. Four decks
  recompiled unchanged (`r5_t1_*`): of 25 findings 13 persist, 11 gone (all Type 3 classes), 1
  changed (`verified/ab_type3.json`). The Type 3 family is real input but over-represented.
- **Families, worst first, with the mechanisms the skeptics found** (file:line as of 9f88dd2):
  - Columns joined: `build_lines` joins words on one baseline up to 2 em apart; `gutter()`
    (classify.py:917) refuses when the left word has < 6 characters. Removing that test fixed all
    three layout slides in memory.
  - Charts: `body_size()` is the deck-wide mode, the 6 pt Madrid/Boadilla footline; the tick gate
    `line.size <= 1.15 * self.body` (classify.py:1334-1345) then keeps 11 pt ticks native, and
    `overlay` (1875-1930) stretches the plot to follow them while fills stay in the background. A
    legend's white box is read as a highlight and render.py:472 erases its swatches.
  - Hidden text shown: `pdfium_backend.Page.chars()` never tests clips; nothing compares text with
    later opaque fills; extract.py:203 `fill_opacity or 1.0` makes opacity 0 opaque.
  - Display math half native: `formula_like` counts only italic-flagged letters (CMMI never is);
    `same_formula`, the limit cap and `fraction_part` fail to rejoin; CMEX crops to the font box;
    U+0338 is never combined (`\neq` -> "/=").
  - Fonts: unknown names (newtx, Palatino, Libertine, Segoe UI, Bera Mono, Yu Gothic) fall back to
    PT Serif, styles lost; OpenType math Greek -> Unicode-name words (`math_text` ~311); astral
    letters shift styles (emit.py:775 counts code points, Slides UTF-16); OT1 accents not composed.
  - Width inflation: `FontMapper.shape_ratio` counts each ". " of a `\dotfill` leader or `\ldots`
    as a sentence space, sizing leaders up to 29 pt.
  - Tables: ruled grids refused (classify.py ~2199/2390/2471) become diagrams; `fit_columns` has no
    page cap; `emit.table_requests` (1481-1489) clamps right alignment per cell.
  - Diagrams: `diagram_from` maps any four-curve path to ELLIPSE with no closure test
    (classify.py:2102); the vector-bullet search (1101-1114) has no aspect test.
  - RTL: PDFium's text is right, the IR is wrong: extract's content-order sort plus `bidi.py`
    scramble whole-RTL lines and reverse digit runs. CJK: the wrap limit adds the next line's
    "first word" (classify.py ~2767), the whole line without spaces, so boxes are 117 pt too wide.
  - Type 3: `TYPE3_SYMBOLS` maps only `\x88`; a control character in alt text crashes
    `build_pptx` (emit.py:1092).
  - Also: numbering restarts after nested lists, merged lines, `single_line_align` centring by a
    2 pt coincidence, `\fcolorbox`/tcolorbox frames, decorations across line breaks erasing words,
    framed listings, bullet glyph substitutes, `select_overlays`' top-fifth heading band.
- **What it cannot see**: thumbnails at 1600 px, not editing behaviour; agent-written decks, with
  prevalence estimated from eight controls only.

### Fixes, wave 1 (2026-09-24, five fixers in worktrees, merged 7486d64..3c5d2ca)
Each fixer reproduced its family offline (`classify --out`, `emit.plan_offline`), pinned it in a test
that fails with the fix reverted, and fixed the mechanism; suite 3,094 passed after the last merge.
- **E, hidden text** (`tests/test_hidden_text.py`): `extract.Visibility` samples each glyph at 9
  points and drops it when 5 are off the page, outside `PageObject.clip` (new in the backend
  contract, both backends) or under a later opaque fill or image; alpha 0 text is dropped and
  opacity-0 drawings left out. Raw `hidden_text` keeps what was dropped for `select_overlays`. Side
  effect, correct: r2_figures_v2 s9's title is under a photo in the PDF and goes; a character mostly
  off the page is lost in four decks.
- **A, columns** (`tests/test_columns.py`): `gutter` needs `GUTTER_PROSE_EM` (8 em) and an edge
  aligned on another line; `column_edge`; `single_line_align` left-aligns a lone line that starts
  where its neighbours start (it centred on a 2 pt coincidence); `classify.cjk` breaks and joins CJK
  without spaces; `build_lines` makes a small word below a larger line's baseline its own line.
  Open: list continuations read as a plain table (belongs in `plain_tables`).
- **B, charts and diagrams** (`tests/test_charts_diagrams.py`): `body_size` ignores repeated
  footline text; tick rows, `axis_label_column`, `release_stranded_labels`; `box_outline` panels;
  a legend box is never a highlight; `upright_ellipse` (a four-curve path must close and be upright);
  labels of a redrawn node go on the top copy; `diagram_from` refuses nodes holding other text,
  crossing nodes and labels with scripts. Open: a refused panel in r2_themes_v3 s8, thin bars in
  r1_econ_v3 s8, tiny end marks of four-curve paths.
- **C, fonts and encodings** (`tests/test_fonts_encodings.py`, 51 tests): `fonts.font_info` family
  tables (`SANS_FAMILIES`, `TEX_TT_RE`, `LIBERTINE_RE`), Roboto Mono for 0.6 em monospace,
  TeXGyreTermesX -> Times New Roman; `math_pieces` styles each piece (italic per glyph, NFKC letters,
  script capitals via Unicode, `\|` -> `||`); `compose_accents` (OT1), `negate` (`\not`); TS1 Type 3
  mapping only on pages whose words are not Type 3. Open: stacked fractions as slashes, the stroke
  of `\L`, Libertine small caps, a Palatino stand-in.
- **D, emit sizing** (`tests/test_emit_hunt.py`): leader dots and ellipses leave `shape_ratio`;
  `in_sentence` keeps a run at its paragraph's size; optical-size cap 1.13x; `emit.u16` for every
  range (astral letters had shifted styles); `xml_text` sanitises alt text (a control character
  crashed `build_pptx`); right alignment in tables; tables shrink onto the page, floor 0.75x
  (`TABLE_MARGIN`); `emit.slides_lines` sizes 114 of 392 multi-line boxes from measured breaks.
  Open: numbering restarting after a nested list (needs a live probe); `deck_ir.pdf_size` in pull
  should skip the width correction for runs that aren't their paragraph's only run, and a table
  shrunk more than 6% shows as pull size residuals.

### Wave 1 on Google (r6, 2026-09-24)
All 76 decks with confirmed findings plus the eight controls were re-converted as `r6_<stem>` and
five blind verifiers compared old and new renderings (`out/hunt/verified/verify_v*.json`, VERIFY.md;
a deck whose old PDF was Type 3 is compared with its `r5_t1_` rebuild or its Type 3 findings are
N/A). Of 377 targeted findings: 100 FIXED, 30 IMPROVED, 222 SAME (most on slides pixel-identical to
before: their families were not in wave 1), 1 WORSE, 2 CHANGED, 22 N/A. Deterministic numbers over
the 76 decks: text_fit findings 822 -> 586, fidelity odd elements 1,306 -> 1,040, mean text overlap
0.701 -> 0.722, invariants 296 -> 300. 19 regressions (`out/hunt/r6_regressions.json`): new wraps
and a looser wrapped-line pitch (7), weight (bold footlines regular, Japanese all bold), missing ink
(listing line numbers, a wordmark, a radical, a denominator), a large corner radius, smaller
arrowheads, a fraction picture over a word.

### Fixes, wave 2
- **K, scripts and bullets** (`tests/test_inline_scripts.py`, `tests/test_bullet_ink.py`): scripts
  start at 0.10 em down, 0.12 em up (keycaps are raised 0.09-0.11 em); a span opening with `_` is
  never a superscript; the space after a smaller run goes with the next word; a superscript over a
  subscript joins its line (a hole there); raised rings and asterisks become `°` and `*` at line size
  (Lato's sit where TeX's raised ones do); a script's script gets span/0.665. Glyph bullets are
  sized and shaped by their ink (`render.glyph_ink`, `emit.ink_sized`, a filled one square) and
  capped at the item's text size; ball patches interpolate the ring behind them
  (`ring_background`); white `(a)` parentheses on a ball are dropped. Kept: SUBSCRIPT for TeX's
  shallower subscripts (a baseline run would read back as a removed subscript and pull would write
  it into the .tex); triangle bullets (no preset); fallback-font glyphs; `\dotfill` dot counts.
  Existing decks with Fira or LM Sans bullets emit differently on their next sync.
- **H, tables** (`tests/test_tables_hunt.py`, 14): cells shaded edge to edge are a table
  (`fill_grid`); a node a vertical rule splits is not a diagram node (`splits_cells`) - ruled grids,
  rowcolor tables and heatmaps had become diagrams; a wrapped cell at a loose pitch, or wrapping in
  two columns at once, stays one cell; white stripes on an off-white page are kept; a word halfway
  between rows is a multirow, a double vertical rule one border; wrapped items of side-by-side lists
  are no table; head and body alignments per column (`head`, `body`). Emit: a wrapped cell's column
  is capped below its line plus the next line's first word (`wrap_joins`, the `cap` in
  `fit_columns`: Slides pulled that word up), cell runs are not reshaped (`cell` in
  `FontMapper.shape_ratio`). Open: `\cmidrule` trims and double rules (a Slides border is one line
  per whole edge), a dash keeping a siunitx column centred, overfull tables (left on purpose).
- **F, display math** (`tests/decks/28_display_math.tex`, 7 tests in `test_classify.py`): relation
  rows, cases rows, the full stop after `\right)` and the sentence before a display now group right;
  `typeset_fraction` (a bar as wide as one part, the other centred) keeps numerator and denominator
  with their bar; overlapping formula clusters merge; `render.grow_to_ink` renders the area with and
  without the formula's own objects at 4x over a reach of 3 em (0.75 pt padding) and grows the
  picture to the ink: CMEX glyph boxes run an em below the origin and the ink hangs about another em
  below that (measured: r1_math_v2 +14.2 pt down, a bare display ∫ +6.1 pt, a cases brace +2.1 pt);
  `drop_hanging_glyphs` moves a hanging sign or √ to the line below; hole widths include limits, and
  a hole at a line end is never narrower than its picture. `display_pieces_apart` splits a lone
  `\left(` that `continues` had joined to prose. Open: stacked fractions stay native `a/b` on
  purpose (`simple_fraction`).
- **G, line breaks** (`tests/decks/28_line_breaks.tex`, `tests/test_line_breaks.py`, 10): forced
  breaks stay breaks (verse cells, numbered examples, sources, TOC entries, titlepage `\\`);
  `\hfill` pieces are right-aligned lines with no label tab (`find_hfill_pieces`), a quote's measure
  W - x0 (quote environments indent both sides equally); justified prose (`is_justified`: lines end
  together with stretched spaces) is JUSTIFIED with the `\parindent` first line joined (0.37 em in
  tablenotes); a `\quad` measures 0.999-1.000 em and never stretches: an em space when the gap is at
  least max(0.9, word space + 0.4) em; a thin space (about 0.167 em, against a word space of at
  least 0.3 em: LM 0.333, Cambria 0.22) between digits becomes NBSP (`thin_span`); explicit line-end
  hyphens stay; block titles, quotes as titles, footnotes, nested enumerate and ⋆ bullets.
  Description labels sit 0.5 em (`\labelsep`) from their text. Open: labels wider than the label
  column (a first-line tab Slides can't place), extract's JOIN_GAP 0.15 em eats 0.13-0.14 em word
  spaces ('18 mo', pinyin), the other GLYPH_SHAPES glyphs as labels.
- **J, frames and code** (`tests/decks/28_frames_code.tex`, `tests/test_frames_code.py`, 11):
  `frame_of` makes a fully framed box's rules its outline (`\fcolorbox`, tcolorbox, listings
  `frame=single`), `rule_frames` a partial or bare frame rule shapes (a fancyvrb frame keeps the gap
  for its label); a listing is one panel, its line numbers a right-aligned box (`split_line_numbers`);
  coloured fancyvrb lines once, as text; code columns from glyph x over `code_pitch` (columns=fixed,
  alignment, indentation); decorations end before trailing punctuation (`TRAILING_PUNCT`); a
  `\colorbox` alone on its line is a panel, inside a line a highlight padded with NBSP (`BOX_PAD`);
  `emit.grown_panels` widens panel, block and shadow as far as the Slides words run longer (fonts
  with advances only: newtx has none). Open: letterspacing, baseline shifts, uwave, highlights across
  a break, a listing with no panel.
- **L, diagrams, overlays, logos** (`tests/test_diagrams_overlays.py`, 17): overlay steps are
  matched by title; see-through drawings (opacity, multiply) keep a diagram a picture; more than
  `MAX_PLAIN_RECTANGLES` (32) bare rectangles are one picture (QR codes); a panel under a stroke
  drawn after it stays in the background (merged with J: a frame's own rules excepted, 2fe31d2);
  rounded-rectangle radius from the path; elbows are always |- (a -| one written from its other
  end); edge labels stay apart across arrows; a chart's bar series are not panels; `clip_to_bands`
  ends a figure box at a band drawn after it; wrapped line ends are not figure labels; a turned
  stamp keeps its letters (a glyph is erased with a native line only if it runs the same way);
  `artwork_of` keeps logo words out of titles and lines; footline boxes beside band artwork are
  theme. Open: dashes (the backend contract has no `Drawing.dash`), picture seams (need Google's
  renderer).
