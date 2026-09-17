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
  1. `extract`: PDF → raw spans / images / vector drawings (PDFium through pypdfium2, `pdf.py`)
  2. `classify`: group spans into lines, paragraphs and lists; decide native vs. background
  3. `render`: background PNG per slide with the converted elements removed
     (page objects switched off, partly removed objects composited from a second render)
  4. `emit`: a python-pptx deck carrying all pictures, imported by Drive, then the Slides API
- No PyMuPDF: PDF access is PDFium only. `pdf.py` wraps it (`notes.py` also uses pypdfium2 to
  write slides.pdf); its text and path output reproduces what the MuPDF-based extraction gave (span splitting at word gaps, `re`/`qu`
  path items, char boxes from font ascent/descent), so classify's thresholds still hold.
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
  points (`pdf._curve_extremes`). Test deck: `22_overlays_on_text`.
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
  `src/beamer2slides/calibration/fonts.json`, package data so a wheel carries it):
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
  backup is the only way back (`tools/deck_backup.py list|export|restore`). Tests: `tests/test_guard.py`
  (offline), `tools/rebuild_guard_proof.py` (live, `out/agent-guard/`).

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
carry `b2s:<slide>/<element>` alt-text titles. `identity.py` keys, `snapshot.py` read-back and
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
tests `tests/test_base_storage.py`). The base also records the overlay mode `convert` used, and
sync keeps it unless `--overlays` says otherwise (`sync.overlay_mode`), or a deck converted with
`--overlays all` would lose its in-between steps. `pull --apply` and `pull --out DIR` keep every
file they replace (`inverse.keep_backup`: `.bak`, `.bak2`, …, pictures included).

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
  `check_report` (sync-report sections), `compare_fresh` / `thumbnail_diff` / `alignment_compare`
  (untouched slides vs a fresh conversion of the same source).
- Scenarios (`convert v1 → edits → sync vN → check → second sync writes nothing`): untouched, disjoint,
  same-element, diff3, conflict, deletions, slides, reorder-both, chain, concurrent (sync runs the
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

## Pitfalls found so far
- PDFium (`pdf.py` handles these):
  - Soft-mask contents are not page objects. Beamer's block shadow is a black rectangle under
    a soft mask (`FPDFPageObj_HasTransparency` with an opaque fill); its visible pieces right
    of and below the panel are reported as shading images (`extract._shadow_pieces`).
  - Shadings are page objects of their own (type 4), usually inside form XObjects; their box
    is the clip. Images and shadings are both `images` in raw.json.
  - A ligature glyph comes back as several characters at the same origin: merged into one.
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
- MiKTeX (pdflatex / xelatex / lualatex) for the test decks, with on-demand package install.
- Test decks: `tests/decks/*.tex`, built by `tests/decks/build.py` into `tests/decks/out/`
  (normal and `-handout` variants).
