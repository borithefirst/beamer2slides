# beamer2slides

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
- **A PDF reader from scratch** (`pdf/pure/`, docs/pdf-from-scratch.md, extra `[pure]` = fontTools):
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
  `tools/render_torture_shading.py`), and so is text in embedded Type 1 and CFF fonts (FreeType's CFF
  engine and smooth rasteriser ported, `pure/ftoutline.py`, `pure/ftgrays.py`, `pure/render_text.py`,
  oracle `tools/render_torture_text.py`), and so is embedded TrueType text (FreeType's glyf loader and
  bytecode interpreter, v40, pedantic, 64 ppem: `pure/truetype.py`, `pure/ttinterp.py`; torture
  `--kind cid-truetype` 600 seeds exact; tricky/variable fonts and glyphs hinted differently after
  earlier loads refused). Whole pages: 233 of the test decks' 269 render byte for byte
  as PDFium's, none apart (`test_whole_beamer_pages_render_as_pdfium_renders_them`);
  a page with anything not ported yet (images, Type 3 or non-embedded text, CalRGB/Lab/Indexed
  shadings, transfer functions…) raises PdfError, so
  `renders = False`: `classify` runs on it and `convert` doesn't yet. Every call equals PDFium's on 4,373 pages
  (chars and object boxes to the last bit on the test decks),
  and deck.json is identical on all 48 test decks; extract is 7× slower. `tests/test_pure_pdf.py`.
  Cross references (CPDF_Parser, rebuild included) and navigation (`pure/navigation.py`: links,
  actions, destinations, name trees, page labels, metadata) are ported rule for rule; the whole-file
  fuzz (`--structure`, seeds 0-500) differs from PDFium on none (27 before font substitution was
  ported). Substitution is PDFium's chain rule for rule (`pure/fontmapper.py`: LoadSubstFont,
  FindSubstFace, CFX_Win32FontInfo through GDI's own CreateFont/GetTextFace; the TrueType and
  Type 1 LoadGlyphMap over FreeType's charmap list), with PDFium's built-in Foxit faces (MM ones
  blended, `pure/type1.py`) loaded from a user cache that `python -m beamer2slides.pdf.pure.foxit`
  fills from PDFium's sources with pinned SHA-256 (no binaries in the tree); without the cache, or
  outside Windows, the older rules stay and `test_substituted_fonts_are_measured_with_pdfiums_face`
  skips. Substituted text measures as PDFium's but is not drawn yet.
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
  (slide text and speaker notes compared), and a sync of the recovered deck writes nothing.

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
explains them better: both directions -> `moved` (the content decides), one -> `unsure` (the label is
followed; it may equally be a passage the author moved), neither -> silence (a frame rewritten from
scratch). Both verdicts are conflicts in the report (`field: label`); a renamed or dropped label the
content still recognises is a warning. The title counts by degree (`_title_alike`): a source that
retitles every frame while moving a label ("Moving labels" -> "Moving labels v2") makes a yes-or-no
"same title?" say no to every pair at once. Measured by `tools/fuzz_labels.py` against a tagged
truth: over 3000 rounds, misidentified frames 14.68% -> 1.04% when the invariant is broken, 0 false
alarms in 1524 sound rounds, nothing ever worse than before (fixed seeds in `tests/test_sync_fuzz.py`).
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
dead entry (`sync.new_base` clears it: a label belongs to the source).

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
Linked charts are their `contentUrl` picture, videos their poster frame (YouTube `hqdefault`,
letterbox cut; Drive: a play panel) inside an `\href`, WordArt its `renderedText` in a `\resizebox*`;
a page beamer has no ratio for is written with `\geometry{papersize}`; fonts the machine lacks are
fetched from google/fonts into a user cache and variable ones cut into static instances
(`fontfetch.py`, `$B2S_FONT_FETCH=0` / `$B2S_FONTS` turn it off). Tests: `tests/test_adopt_media.py`.
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
element's own `frame`, dashes, alpha, bent and curved connectors, arrow heads; freeforms as their box;
`tests/test_adopt_shapes.py`). Scripts (`scripts.py`): luaotfload fallbacks for CJK and symbols,
babel `onchar=ids` for CJK line breaking and Hebrew/Arabic fonts, `bidi=basic` with RTL paragraphs
in `otherlanguage` (`tests/test_adopt_scripts.py`).
**Benchmark** (`tools/adopt_bench.py`, corpus of 29 public decks in `tests/decks/foreign/corpus.json`,
cached in `out/adopt-corpus` or `$B2S_ADOPT_CORPUS`): `capture` reads a deck and its LARGE thumbnails
(read-only, 429 back-off), `run [decks] --jobs N --tag T` bootstraps, compiles (a failing deck is
split frame by frame to name the broken ones), scores every slide (`boxes` / `page` / `pixels`) and
writes deck|source|diff sheets to `<corpus>/<deck>/runs/<tag>/sheets`; `report --tag T`.
Measured (bootstrap only, 912 slides): boxes 0.593 -> 0.754 (mean per deck 0.565 -> 0.725, every
deck up; pixels 0.913 -> 0.939), 11 frames that did not compile -> 0 (tags `abs` -> `merged`).
Lengths in bp (`adopt.to_bp`: the IR is PDF points, TeX's pt is 72.27 to the inch) and see-through page
backgrounds blended over white: 0.754 -> 0.818 (`units`). Text (tags `units` -> `text-b` -> `text-ins2`):
boxes 0.818 -> 0.843 -> 0.852, page 0.814 -> 0.839 -> 0.848. With the fills below (`combined`): boxes
0.885, page 0.881, pixels 0.970, no deck down.
Known gaps, by what they cost: text insets the API does not report where no autofit height gives
them away, freeform shapes (5,791 in the corpus, drawn as their box; Google's .pptx
export has their geometry), and dragged shape adjustments.
Fills the API cannot say (`deck_fills.py`, `tests/test_adopt_fills.py`): a gradient, picture or texture
fill reads `shapeBackgroundFill: {}`, a .pptx table style's cell colour NOT_RENDERED, and every
placeholder INHERIT chain in the corpus ends NOT_RENDERED too - so `deck_ir(foreign=True,
thumbnails=n -> image)` reads them from the slide's own thumbnail (the bench passes the cached LARGE
ones; live `adopt` does not fetch them yet, and without thumbnails nothing changes). Conservative, since a false fill paints over what lies
under it: the box less a rim and less opaque elements above must be one flat colour (ink allowed only
in boxes of texts above), not the page's or the colour all around it (`edges_show`, table cells vs
the page outside the table), settled top down; a rectangle may read as a three-stop axis gradient
(TikZ `left/middle/right color`: cs161's header bar). Measured (`units` -> `fills-d`): cs161-net
page 0.734 -> 0.917, cs161-tls 0.647 -> 0.864, hebrew-lesson 0.310 -> 0.668 (paper backdrops and
style-coloured cells). SlidesCarnival's 2,068 `{}` freeforms are squiggles whose box is not flat
and stay unfilled: that is the freeform gap, not a fill one.

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
2 of 400 rounds); and the oracle itself accused a slide that two others - a moved frame and the
copy riding behind it - had merely passed (`loss_oracle.order_findings`). A third was the campaign's
own: chained steps could drop the same picture on the same slide at the same box twice, which is a
real duplicate made by the fuzzer, so `random_spec` now places an added picture or blank shape where
no other one stands (`_free_box`). And the one that killed a sync outright (seeds 608, 616): the
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

## Playground (docs/playground.md)
`python -m beamer2slides playground` (`src/beamer2slides/playground/`: stdlib `http.server` + a static
page): a talk typed, picked or uploaded runs through compile → extract + classify → render on one
worker thread (the stages print; `redirect_stdout` is process-wide, so request logs go to stderr), and
the page draws each slide from the job's deck.json (editable preview over the background, native
boxes, background, debug, IR). Google only with `B2S_PLAYGROUND_GOOGLE=1` and a token; public hosts
get the recorded runs from `docs/media`. Jobs in `$B2S_PLAYGROUND_JOBS/<port>` (swept at start: one
folder per port, or a second server deletes the first one's jobs). `Dockerfile` = the deployment
(TeX Live, uid 1000, port 7860, `openin_any=p`); not built on this machine (no Docker).
Tests: `tests/test_playground.py` (offline, an uploaded test PDF through the HTTP API).

## Google Docs (docs/google-docs.md)
The same bargain as the Slides sync, one dimension smaller: a **canonical HTML file** in git is
what the source says, a Google Doc is what the reader says, and where both moved the document
wins. `python -m beamer2slides docs push doc.html` imports the file through Drive, plants one
**named range** `b2s:<key>` per block and rewrites the file with those keys and a
`<meta name="b2s-document">`; `docs sync doc.html [--dry-run]` merges three ways, writes with
`requiredRevisionId` (re-plans up to 3 times on a mismatch; `B2S_DOCS_BEFORE_WRITE` is the test
hook) and then **regenerates the file from the document it just wrote**, so file, document and
base agree and the next sync writes 0 requests. State beside the file: `.b2s/<stem>.base.json`
and `.b2s/<stem>.sync-report.{json,md}`; without a base, sync stops and asks for
`--assume-base file|document`. Modules: `doc_ir.py` (IR ↔ canonical HTML ↔ `documents.get`,
keys, named ranges), `doc_merge.py` (pure planning), `doc_sync.py` (the commands).
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
  Deleting one: its own span; with its `lead` when a body opens on it; with the mark in front
  when it ends the body (measured: no stray trailer). `insertTable` splits the paragraph
  its index is in and needs one, so a table goes at the following block's start (and the empty
  paragraph it leaves is swallowed), or, in front of another table, at the paragraph mark
  before it. A ragged table (merged cells) whose grid changed is still reported.
- Styling merges with the words: a source restyle in a block whose words both sides changed
  is written word by word (`doc_merge._restyled_words`: the document's marks, then the file's
  on every word the file also has); only a restyled word the document replaced is reported.
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
  did not touch is written again (`rewrite`), never when it holds an equation-like chip.
- Tabs: the first tab is the file's body, every other one a `<section data-tab title
  [data-parent]>` (`doc_ir.parts`; no `data-tab` = a tab the source asks for). Each tab is its
  own plan and batch, every location/range stamped with its `tabId` (`doc_merge.on_tab`; none
  = the first tab), keys unique per tab. Tabs themselves merge three ways by id
  (`doc_merge.pair_tabs`: `addDocumentTab`, `updateDocumentTabProperties`, `deleteTab` only for
  a tab the document left as the base has it). A new tab's lone empty paragraph and the
  undeletable one in front of a body's first table are hidden like the trailer (`trailer`,
  `lead`) and written into; nothing can be inserted at a table's own index (measured), so a
  block in front of a table goes in as `\ntext` at the previous paragraph's mark.
- What the file cannot carry is reported too (`doc_sync.limits`): a picture file that is not
  there.
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
