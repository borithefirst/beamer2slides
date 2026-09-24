# beamer2slides

Convert beamer PDFs into **editable Google Slides decks**, keep the deck and its source in sync
both ways, and do the same for canonical HTML files and Google Docs.

**This file is the map, not the history.** It was 305 KB on 2026-09-23 and every subagent died
on it. The long version - measurements, the seeds each campaign found, why each rule is the way it
is - is `docs/project-notes.md` (the old CLAUDE.md, word for word) and the topic docs named
below. Before changing a mechanism named here, grep those for it. New long stories go there. Here
goes one line per mechanism, and only what a session needs to act correctly. Keep this file under
~50 KB.

## Goal
The output should feel like a native Google Slides deck: real text boxes, image elements and
shapes. What can't be converted with good fidelity (display math, TikZ/pgfplots figures, theme
decorations) is a picture, or baked into a per-slide background picture.

## Rules that are decided
- **Input is the compiled PDF**, not the `.tex` (exact geometry, fonts, colours). The `.tex` /
  SyncTeX are hints (pull, labels).
- **Stages talk through a JSON IR** so each is testable alone: `extract` (PDF -> raw.json spans,
  images, drawings) -> `classify` (lines, paragraphs, lists; native vs background -> deck.json) ->
  `render` (background PNG per slide with converted objects switched off; figure crops) -> `emit`
  (one python-pptx upload carrying all pictures, imported by Drive, then the Slides API).
- **The PDF library is a swappable backend** (`src/beamer2slides/pdf/`, docs/pdf-backend.md).
  Nothing outside that package imports pypdfium2; answers are plain data, page objects are named
  by id. `api.py` is the contract, `pdfium_backend.py` the reference, `sandbox.py` runs any backend
  in a worker process, `pdf/pure/` is a from-scratch Python port of the PDFium parts we use,
  byte-identical on every test page (docs/pdf-from-scratch.md). Pick with `pdf.set_backend` or
  `$B2S_PDF_BACKEND`. **PDFium is not thread-safe**: every PDF read stays on the main thread.
- **No public links**: pictures reach Slides inside the imported .pptx, never as shared Drive files.
- **Fidelity is measured on Google's own renderer** (`getThumbnail` vs the PDF page), never a local
  preview. Font substitutes are calibrated (`tools/calibrate.py`, `calibration/fonts.json`,
  per-character advances in `calibration/advances.json` via `tools/probe_advances.py`). A run of
  15+ characters whose letters are unlike a sentence's (serif capitals, a line of w) is sized to
  its PDF width when more than 7% off: Slides advances against Computer Modern's
  (`calibration/cm_advances.json` from `tools/cm_advances.py`, `FontMapper.shape_ratio`; numbers
  too). Small caps are a deliberate compromise at 1.13x (`SMALL_CAPS_WIDTH`).
- **Never destroy what a person did in the deck or the document.** A rebuild of an edited deck is
  refused (`guard.py`); sync is a three-way merge where the deck's edits win and conflicts are
  reported; a destructive write keeps a way back first. `--force-rebuild`, `--follow-labels`,
  `--force-adopted-deck`, `--take-source ID`, `--assume-base` are decisions a *person* makes in
  words - an agent never picks them itself.
- **A conversion or a sync is round trips, not work.** Speed comes from having independent calls
  in the air at once (threads only; each falls back to serial when a caller lends its own client).
  Measure A/B **interleaved** in one sitting: Google's latency drifts 2x with the day.
- Overlays: non-handout PDFs keep the last step of each frame (steps matched by title);
  `--overlays all` keeps every page.

## What becomes native (deck.json element kinds)
Details, measurements and edge cases: docs/project-notes.md "What becomes native".
- `text`: paragraphs, bullet lists (glyph, number, ball and vector bullets with their PDF colour and
  size, `emit.BULLET_SHAPES`, `emit.bullet_level`; glyph bullets sized and shaped by their PDF ink,
  `render.glyph_ink` -> bullet `ink`/`fill`, `emit.ink_sized`, capped at the item's body size; a
  ball keeps its label's parentheses unless they reach its rim, `BALL_RIM`; numbers after nested
  glyph items are literal, emit making one list per preset; a mark outlined in another colour is
  a picture),
  inline math as runs with scripts (down from 0.10 em, up from 0.12 em; raised rings and asterisks
  become `°`/`*` at line size) and Unicode,
  links, code. Frame titles use the layout's TITLE placeholder. Hanging labels are `label<TAB>text`.
  RTL (Hebrew/Arabic) is turned into logical order (`bidi.py`) and written `RIGHT_TO_LEFT` with
  mirrored alignment: a line is read whole (`classify.read_lines`, `bidi.logical_line`: the logical
  text whose UAX#9 display is the page, LRM/RLM where needed, the page's direction for mixed
  lines, `bidi.page_direction`), after PDFium's reversal and mirroring are undone
  (`bidi.visual_chars`); an RTL item's bullet hangs right of its words (`detect_rtl_bullet`; lists
  join, nest and wrap by the right edge); bidi marks take no room (`bidi.MARKS`). Type 3 EC/LH/TC bitmap fonts are named by their TFM widths (`type3.py`,
  `calibration/tex_fonts.json` from `tools/tex_fonts.py`): ligatures, dashes, weights, TS1, T2A. OT1's `\_` is a rule, read back as `_` (`classify.underscores`, span
  `drawn`: no page object); a CMEX glyph hangs from its origin, so an inline `\sum` between words
  is joined to their line (`join_hanging_operators`) and becomes part of its formula hole.
  Text the PDF hides (off the page, outside its clip, under a later opaque fill or image, alpha 0:
  5 of 9 samples) is dropped at extract (`extract.Visibility`, `PageObject.clip`, raw `hidden_text`).
  Side-by-side text is split only with evidence (`gutter`: `GUTTER_PROSE_EM` plus an edge aligned
  on another line; `column_edge`); a lone line starting where its neighbours start is left-aligned
  (`single_line_align`), a lone line flush with a right-aligned paragraph beside it right-aligned;
  lines broken by hand never join (`hand_broken`); classify records where wrapped lines start
  (`line_starts`) so emit measures a paragraph without TeX widths; CJK breaks anywhere and joins with no space (`classify.cjk`). Font names
  map through family tables (`fonts.font_info`: `SANS_FAMILIES`, `TEX_TT_RE`, `LIBERTINE_RE`;
  0.6 em monos -> Roboto Mono; CJK faces -> Noto Sans/Serif JP/SC/TC/KR, `fonts.cjk_font`; a CM sans
  cut of 6 pt or less is set at weight 800 in bold widths, `FontMapper.optical_weight` (Slides
  draws 500/600 as Regular, 700/800 as Bold), which deck_ir reads back regular (600 too); CM math
  letters stay serif among sans words, `serif_math_letters`);
  extract finds narrow spaces, letterspacing (`tracked_gaps`; tracked by `TRACK_SPACED` 0.14 em or
  more, a no-break space between letters) and accent overhang; a Type 3 font of only codes above
  0x7F tries TS1/T2A before T1; math letters are styled per piece (`classify.math_pieces`: italic
  per glyph, NFKC, script capitals as Unicode); OT1 accents compose (`compose_accents`), `\not`
  negates (`negate`). Every text range emit writes is UTF-16 (`emit.u16`: astral math letters). A
  run inside a sentence keeps its paragraph's size (`emit.in_sentence`); leader dots and ellipses
  never set `shape_ratio`. A multi-line box is sized from where Slides breaks its lines
  (`emit.slides_lines`, `box_lines` per paragraph), keeping `emit.LINE_MARGIN` (2.5 pt) past its
  widest line; an unmeasured paragraph never makes it narrower than the measured ones. A glyph the
  page edge cuts stays text (only samples on the page are judged). Justified prose (`is_justified`,
  `stretched`: word spaces compared font by font, the last line never the longest, no hanging
  label) is written JUSTIFIED with a `\parindent` first line, and never ends past its PDF lines
  (`justified_right`, else START); a paragraph needing a nearer edge than its box's gets
  `indentEnd` (`paragraph_ends`, which `text_layout` honours); an unmeasured paragraph may grow its
  box up to 1 em to keep the PDF's line count (`unhyphenated_room`); `\hfill` pieces are their own right-aligned lines (`find_hfill_pieces`);
  a `\quad` is an em space at max(0.9, word space + 0.4) em; thin-spaced digits keep NBSP
  (`thin_span`). Code keeps its columns: spaces from glyph x over the column pitch (`code_pitch`),
  line numbers a right-aligned box of their own (`split_line_numbers`, Line `code_number`); a
  line's baseline is the one most of its letters stand on (`Line.main`); code lines are never
  formulas.
- `image`: figure regions (TikZ, plots, raster images with their labels) as pictures. A bare
  `\includegraphics` keeps the author's file byte for byte when it decodes identically
  (`classify.bare_image`, `render.image_file`). A chart's tick rows and centred titles belong to its
  picture (`tick_row`, `axis_titles`, `axis_label_column`, `release_stranded_labels`); a caption
  ("Figure:") stays text. A legend box is never a highlight; a drawn frame is a panel
  (`box_outline`); a chart's bar series are not panels. A figure box ends at a band drawn after it
  (`clip_to_bands`). A display formula is one picture grown to its glyph ink (`render.grow_to_ink`,
  CMEX ink hangs an em below its box); a bar as wide as one part with the other centred is a
  fraction bar (`typeset_fraction`); hanging CMEX/√ signs go to the line below
  (`drop_hanging_glyphs`); `extension_font` names every math-extension font. A glyph a picture
  owns stays for its crop (`render.owned_by`); a figure takes the glyphs its box holds and ignores
  words it only grazes; a stroked arrow head reaches its mitred point (`miter_reach`); ulem chains
  are underlines, and a lone hairline under words stays in the background; overlapped pieces are
  one symbol (`compose_symbols`); an accent over a Greek letter is a hole (`accent_beside`);
  adjacent holes are one; icons are never formulas; an item's formula wrapped alone is the item's
  (`wrapped_formula`) but its own paragraph (`wrapped_formulas_apart`), one hole when it has no
  prose and holds symbols Slides was never measured on (`unmeasured_symbols`); a long arrow
  (⟶ ⟹ ⟺, mhchem) is a hole at its PDF length, its labels in its picture (`long_arrow_groups`);
  a figure label beside a column is no line (`figure_label_apart`), nor one of a row of like
  labels a plot title (`in_label_row`). A list's balls and a photo under a hole stay whole
  whatever box reaches them; a hole's picture on a photo is its glyphs on a clear ground
  (`render.on_picture_ground`); icons grow to their ink; a line crossing a picture's edge keeps its
  crossing rows in the background (`picture_crossings`).
- `table`: text framed by rules (or rule-less `plain_tables`), with borders, merges, fills. Column
  widths come from measured Slides advances (`emit.slides_width`, `fit_columns`) so no cell wraps
  and the table does not grow over its caption; classify cuts a spanning chunk at word gaps when it
  lines up with the other rows. Rules wider than half the page with rows of cells between them are
  a table's, not theme decoration (`table_hairlines`). A wrapped `p{}` cell is one cell of several
  lines (`row_lines`, `wrapped`), its column wide enough for each PDF line (`emit.wrapped_width`).
  A table wider than the page shrinks to fit, never below 0.75x (`emit.TABLE_MARGIN`). Cells shaded
  edge to edge are a table (`fill_grid`), and a node a rule splits is no diagram node
  (`splits_cells`); a column's head and body keep their own alignments (`head`, `body`); a wrapped
  cell's column stays below its line plus the next line's first word (`wrap_joins`); cell runs are
  never reshaped (`cell`). Wrapped items of side-by-side lists are not a plain table. Wrapped
  cells take the least width keeping the PDF's line count (`emit.wrap_window`); justified X cells
  are JUSTIFIED (`justified`); a centred table grows both ways (`table_shift`); rows are laid out
  at their own size (`row_sizes`); a flush merged cell keeps its indent (`merge_x`); only a
  `\multirow` cell loses its top margin (`middle`). Rows shaded by bands of their own sit on
  them (`bands`); a measured column gets its words plus `WRAP_MARGIN` (8% only unmeasured); an
  overfull table's rules run off the right edge and it shrinks to end where the PDF's does; a
  siunitx dash is a `centred` cell; cell accents compose (`span_runs`).
  Convert brings tables empty in the .pptx with their own cell margins (`emit.pptx_table`) and
  fills them through the API, so rows keep the PDF pitch. The base records those margins
  (`table_margins`); sync refills such a table in place when its words or place changed, or rows and columns that
  inserts beside a neighbour can give (`sync.table_refill`, `table_steps`), and otherwise creates an API table (`DeckPlan(pptx_tables=False)`).
- `diagram`: node/line/arrow clusters as grouped shapes, connectors and labels (`diagram_from`).
  It refuses (the drawing stays a picture) when a node holds other text, nodes cross, or a label
  has scripts; an ellipse must be upright (`upright_ellipse`), a redrawn node keeps its label on the
  top copy. See-through (opacity, blend) drawings and clusters of more than `MAX_PLAIN_RECTANGLES`
  rectangles stay pictures; elbows are written |- (a -| one from its other end); a panel under a
  stroke drawn after it stays in the background (a frame's own rules excepted).
- `shape`: opaque panels such as beamer blocks (title bar + body built to survive resizing).
  Frames (`frame_of`): a fully framed box (`\fcolorbox`, tcolorbox, `frame=single`) is a panel
  with an outline, a partial or bare frame rule shapes (`rule_frames`); `emit.grown_panels`
  widens a panel as far as its Slides words run past min(left pad, PDF right margin, 0.5 em), its
  frame panel with it. A `\colorbox` alone on
  its line is a panel, inside a line a highlight padded by no-break spaces.
- Decorations on words: underline/strike/highlight runs; words on small graphics and complex inline
  formulas become **holes** (no-break Roboto Mono spaces) with the picture placed over them by
  measurement on scratch slides (`emit.measure_places`); graphics drawn at words (tikzmark arrows,
  braces) are **overlays** anchored to their text and stretched to the words Slides sets.
- Theme: the most common background goes on the master, shared decoration onto layout pictures,
  layouts' placeholders get the deck's title/body style, frame counters become per-slide text.
  Words on different theme artwork or logo boxes are different lines (`artwork_of`); footline
  boxes beside band artwork are theme.
- Everything else (display math, theme decoration) stays in the background picture.

## Google side
- GCP project `beamer2slides` (personal Gmail), Slides + Drive + Docs APIs. OAuth consent is
  **External / Testing**: refresh tokens expire after 7 days and need a browser consent again.
- Auth: `google_auth.py`, scopes `presentations` + `drive.file`. `client_secret.json` and
  `token.json` sit in the repo root, git-ignored. **Never print or commit their contents.** An
  installed package looks in `%APPDATA%\beamer2slides` / `~/.config/beamer2slides`;
  `$B2S_CLIENT_SECRET` / `$B2S_TOKEN` override.
- **`googleapiclient` is imported by `gapi.py` only** (extra `[google]`); `HttpError` is bound there
  once. Never import the client library at module scope anywhere else (`tests/test_gapi.py`).
- `google_auth.use_provider` / `use_services` inject credentials or ready clients **per context**
  (ContextVar); a worker thread inherits nothing, so resolve credentials on the calling thread
  (`credentials_for_threads`).
- **Every content download goes through `net.download`** (picture signatures, thumbnails, read-deck
  pictures, `source_url`, Doc pictures); `google_auth.use_fetcher` / `AgentContext.fetch_google_content`
  let a caller own it (resolve with `fetcher_for_threads` on the calling thread). No new `urlopen`
  (`fontfetch`, fixed GitHub host, `$B2S_FONT_FETCH=0`, is the one other); tests install a
  fetcher (`fetcher` fixture), not monkeypatches.
- `presentations.create` ignores `pageSize` (always 16:9), hence the .pptx route. `createImage`
  needs a fetchable URL and letterboxes. Object ids are 5-50 chars. `getThumbnail` LARGE = 1600 px.
- Emit robustness: a refused batch is retried per slide, then per element, then the deck is rebuilt
  once with pictures of refused regions (`fallback_pictures`).
- Re-running `convert` into the same folder rebuilds that deck in place (same URL); `--new-deck`
  makes another. **The rebuild guard** (`guard.py`, docs/sync.md "Never lose deck edits") refuses
  when the deck was edited, has no base, or came from another PDF, and a forced rebuild keeps a
  backup first (`--backup`, `tools/deck_backup.py`). A .pptx restore brings content back but not
  object ids, so sync refuses a recovered deck. A base older than picture signatures (2026-09-17)
  cannot clear a reissued picture URL: still refused, but said as such (`guard._unverifiable`).

## Usage
```
python -m beamer2slides classify deck.pdf   # raw.json, deck.json, debug/ overlays
python -m beamer2slides convert  deck.pdf   # + backgrounds/, figures/, Slides deck, emit.json
python -m beamer2slides fidelity deck.pdf   # thumbnails vs PDF: fidelity.json, fidelity/diff-NNN.png
python -m beamer2slides sync new.pdf --deck <url|id|out folder> [--dry-run] [--take-source ID]
python -m beamer2slides pull --deck <...> --tex main.tex [--apply]
python -m beamer2slides adopt --deck <...> --tex main.tex
python -m beamer2slides label main.tex [--apply]
python -m beamer2slides docs push|sync|adopt ...
python -m beamer2slides playground
```
Outputs go to `out/<pdf stem>/`. Diff PNGs: red = only in PDF, blue = only in Slides, black = both.
Debugging classification locally: `debug/slide-NNN.png` (element boxes over the page).

## Sync, pull, labels, adopt (docs/sync.md, docs/labels.md; history in docs/project-notes.md "Usage")
- **Sync** merges a new PDF into an edited deck, three ways: base = what convert wrote
  (`<out>/sync/base.json`, Drive-first via `appProperties.b2sBase`, the folder copy a cache).
  Modules: `identity.py` (slide/element keys), `snapshot.py` (read-back, base), `merge.py` (pure
  planning), `sync.py` (writes with `requiredRevisionId`, re-plans on a mismatch; pictures via a
  staging deck deleted after use). Objects carry `b2s:<slide>/<element>` alt-text titles (never
  on groups).
- **A sync killed anywhere loses nothing**: deletions last, a `pending` marker records what a run
  creates, the next sync sweeps duplicates. Fault injection: `B2S_FAIL_AT` (`faults.py`).
- **A decision the base does not record reverses itself**: whatever a sync decided to keep (a unit
  or slide the source removed, the deck's own slide) must be written into the base.
- **A box can change with its neighbours**: sync diffs what emit would write, not only the IR.
  `sync.mark_emitted` emits each changed slide from the base's IR and the new one
  (`emit.slide_emission`; ids, links, z-order normalised, measured places left out) and marks an
  unchanged or moved element `width`/`placed`/`emitted` (`identity.CONTEXT_FIELDS`). Group or
  placeholder-role changes can't be written by recreating a unit: `build_ours()["context_unwritten"]`.
- **Both moved it**: a unit the source rewrote and the person moved or resized takes the person's
  move and size on top of the source's new place (`sync.carried`, geometry mode `delta` always); a
  reported conflict (`merge.GEOMETRY_CARRIED`), held by the loss oracle (`geometry_not_carried`).
  An absolute "deck position kept" let the source's reflow run into the person's box.
- **Merged text into a recreated box** (`refit.py`, `text_layout.py`): after the overrides, formula
  pictures follow their holes into the words as written, and the box and a block panel under it
  grow as emit would size them (a panel only as far as the strip below is clear, else reported);
  only the text change is corrected, never the person's geometry. The base records these steps as
  the converter's (`reshape_base`, noted `refit` on the read-back; the report lists them under
  `refit`). Slides applies a RELATIVE transform to a group's child **in page space**, so a step is
  written as it is; a picture's move is measured in the converter's box and the person's edit put on
  top (`refit.carried_step`). `text_layout` is also the layout oracle's line model.
- **Theme sync** (`theme_sync.py`): `base["theme"]` records what convert wrote on the master and
  layouts (fill, decoration pictures, placeholder styles); sync merges them three ways, the theme
  batch first. Slides drops a run property equal to the inherited one, so old placeholder styles
  are pinned onto converted slides *after* the layout write (`inherited_pins`). An old base leaves
  layouts alone with a warning. Group or placeholder-role changes sync can't write are warnings.
- **Frame labels** (`\begin{frame}[label=x]`) are the only slide identity that survives compiling.
  A label written twice reaches the PDF as *no* label (hyperref keeps the first). `label` writes
  missing labels; it never renames or resolves duplicates. When a label moved between frames,
  `identity.label_moves` says `moved`/`unsure`; an `unsure` slide is held back, not written.
- Conflicts carry ids (`merge.conflict_id`); `--take-source ID` writes the source's version of one
  (only for what something *says*, never existence or identity).
- **Pull** (deck -> `.tex`): `deck_ir.py` reads the deck, the loop compiles, classifies, compares
  (`compare.py`) and translates residuals (`inverse.py`, `texmap.py`) until the source's conversion
  matches; unresolved residuals go to `<out>/pull/edits.md`. Compiles stop when the aux files
  stop moving (`inverse.aux_state`).
- **Adopt** (a deck a person built in Slides -> a beamer source, `adopt*.py`,
  docs/adopt-bench.md): absolute-first `slidebox` frames in a `slides.sty` vocabulary, a recovered
  theme, a label per frame from the slide's objectId, and a base so the source can be synced back
  into that same deck (`adopt_sync.py`). An element tied to no object of the person's is kept, not
  duplicated (`merge.ADOPTED`, `field: unpaired`); layout-drawn and in-table elements are named as
  such. Benchmark: `devtools/adopt_bench.py` (29 public decks).
- **Occlusion**: nothing a sync creates may end up over words only the deck has
  (`sync.would_hide`, `Sync.restack`); the fuzz applier mirrors it and must be fixed together.
  The person's own objects are never moved: when the source's words or pictures now run over one,
  the report says so (`Sync.warn_about_overruns`, `text_layout.overruns`, report `overruns`).
- A unit kept for a conflict (the deck's words win) still goes where the source moved it
  (`merge.plan_unit` -> `move`); only a person's own move of it outranks the source's.
- Proving nothing is lost: `tools/loss_oracle.py` judges one sync; `tools/fuzz_sync.py offline`
  fuzzes the merge through a reference applier (`fuzz_world.py`), `live` against real decks;
  `tools/fuzz_labels.py` measures label pairing. Fixed seeds in `tests/test_sync_fuzz.py`.
- Proving nothing *looks* broken: `devtools/layout_oracle.py` lays text out like emit (exact line
  counts on converter boxes) and fails `text_overlap` / `text_overflow` / `stranded_picture` /
  `off_page` a sync introduced; `tools/layout_oracle.py <archive> [--json]` replays recorded live
  fuzz steps offline; `LiveRound.step` writes `layout.json`. Live ground truth: the `layout-*`
  scenarios in `test_sync_live.py`, measured by `devtools/probe_layout.py` (docs/project-notes.md
  "Layout probes", "Layout oracle").
- Live fuzz: `tools/fuzz_sync.py live --reuse [--focus layout|probes]` (a Drive copy of one
  converted deck per round; edits batched, `--edits reread` the old way). `round.json` has cost,
  reach and layout per step; keep `--parallel` at 3-4 under the write quota. Subprocesses never go
  interactive (`devtools/counted.py`). `tools/fuzz_reach.py <archive>`: how often steps set up each
  layout precondition (docs/project-notes.md "Live fuzzer efficiency").

## Agent tools (`src/beamer2slides/agent/`, docs/agent-tools.md)
- Eleven tools, one per journey (`agent.tools.TOOLS`, ordered by `tools.ORDER`): `b2s_status`,
  `deck_inspect`, `deck_convert` (also in halves `deck_prepare` / `deck_upload`), `deck_sync`,
  `deck_pull`, `deck_adopt`, `tex_label`, `tex_converge`, `doc_push`, `doc_sync`, `doc_adopt`.
  They call the same functions as the CLI; nothing reimplemented.
- Every tool is written inside `@tool(name, needs)`: one journey per process, prints captured,
  `SystemExit` -> a code, permission (`READS`/`WRITES`/`READS_GOOGLE`/`WRITES_GOOGLE`) checked
  **before** the body runs, and Google **never interactive** (a dead token is `needs_consent`).
- One result shape (`types.Result`), a closed refusal vocabulary (`types.CODES`), `INSTRUCTIONS.md`
  in the wheel. **The layer runs at Google in a harness with no filesystem**: every file argument
  also takes content (`content.py`), results can be inline; keep every input and output
  serialisable and never add a built-in URL fetcher.
- MCP server `beamer2slides-mcp` (extra `[mcp]`) works with both SDK generations.
- Agent benchmark: `devtools/agent_bench.py` (replay/live tasks, HARM counted apart),
  `devtools/agent_play.py` to play one task turn by turn. No model is called from this repo.

## Playground (docs/playground.md)
`python -m beamer2slides playground`: compile -> classify -> render in the browser, plus the
**workbench** (a folder per visitor and the eleven journeys in subprocesses, token on stdin, no
shell). Live on Cloud Run (project `beamer2slides`, europe-west1, one instance):
https://beamer2slides-playground-702466108736.europe-west1.run.app. Google modes: `local` (owner's
token) or `signin` (the visitor's own `drive.file` token, never stored). The editor merges with a
file a run rewrote (`workbench.reconcile`) and refuses a both-sides conflict rather than writing
markers.

## Google Docs (docs/google-docs.md; history in docs/project-notes.md "Google Docs")
- A **canonical HTML file** is what the source says, a Google Doc what the reader says; where both
  moved the document wins. `docs push` imports the file and plants one **named range**
  `b2s:<key>` per block; `docs sync` merges three ways, writes with `requiredRevisionId` and then
  regenerates the file from the document, so the next sync writes 0 requests. `docs adopt` writes
  the file for a document nobody pushed. Modules: `doc_ir.py`, `doc_merge.py` (pure planning),
  `doc_sync.py`.
- Base is Drive-first (a JSON file beside the document), `.b2s/<stem>.base.json` a cache. With no
  base, sync stops and asks for `--assume-base document-wins|source-wins`.
- A `files.update` rebuild destroys every named range: after the first import only incremental
  `batchUpdate`s. Indices are UTF-16 code units; a chip is one unit.
- **Docs' index rules are about structural elements** (tables, TOC): nothing inserted at one's own
  index, the newline in front of one undeletable, a body never opening or ending on one. Many
  repairs in `doc_merge` exist for these (`_delete_range`, `refuse_nowhere`,
  `restore_undeletable`, `recover_*`). Read their docstrings before touching them.
- Docs' **merge-on-delete** hands the deleted paragraph's whole style (named style, bullet, list,
  level, measures) to the block behind it; the settle repairs what the plan asked for.
- What a request cannot say is **reported, not guessed**: bullet nesting levels and glyphs
  (`unwritten_levels`, `unwritten_glyphs`), picture sizes and alt text, unmodelled properties
  (`doc_ir.unmodelled`). What a sync deletes is listed first in the report.
- Reads retry through blips (`doc_sync._read`); **writes never retry** (a lost answer may have
  been applied).
- Fuzzed against `devtools/doc_loss_oracle.py` and four campaign judges in `tools/fuzz_docs.py`
  (`offline --rounds N --chain N --shape S --strict`), through `devtools/doc_world.py`, a reference
  applier that applies the real requests under Docs' rules. `fuzz_docs.KNOWN` is empty and must stay
  so (a fixed entry goes out, or it hides the next defect). Every defect found is pinned in
  `tests/test_doc_fuzz.py`. **An oracle's forgiveness that no longer matches a real edit is a blind
  spot: retire it.**

## Tests
- **Offline suite: `python -m pytest -q -n 12 --dist loadgroup`** (~2.5 min, ~2,800 tests, no
  Google). `tests/conftest.py` explains the split. Re-run a named failure alone before believing it
  while something else loads the CPU.
- Offline highlights: `test_emit_requests.py` replays what emit would send (`emit.plan_offline`);
  `test_invariants.py` (`checks.py`: stray_ink, stray_labels, lost_ink, structure, junk_text over all
  decks and theme talks; allowed exceptions in `tests/invariants_allow.json`, stale ones fail);
  `test_classify.py` against the built decks.
- Opt-in live suites (real Google, fixed folders under `out/` that are rebuilt): `-m slides`
  (alignment/fidelity of the stress decks vs `tests/slides_baseline.json`), `-m sync`
  (`test_sync_live.py` scenarios, ~17 min; `test_stress_live.py`, the 48-frame ambiguous deck),
  `-m docs`, `-m inverse`. Delete `out/sync-tests/_fresh` after a converter change.
- Tools that answer questions on the live renderer: `tools/alignment.py` (holes, number balls,
  bullets, overlay marks in pt), `tools/probe_*.py` (one question each, e.g. `probe_new_bullet.py`).
- **Text fit torture**: `tests/decks/27_text_fit.tex` (one frame per way text can fit differently)
  and `tools/text_fit.py <pdf> --out <folder> [--crops]` after `convert` + `fidelity`: wrap, drift,
  width, crowded, grown, touch, line by line on Google's renderer (`devtools/text_fit.py`,
  `tests/test_text_fit.py` on synthetic pages). Open findings: docs/project-notes.md "Text fit".
- **Visual hunt** (`tools/visual_hunt.py run <tex> --slot S`, `ledger`): compile, convert, fidelity,
  text_fit and invariants, archived as PDF | Slides side by side for hunter / blind judge / skeptic
  campaigns under `out/hunt/`. 377 open findings by family with mechanisms: docs/project-notes.md
  "Visual hunt".
- Theme robustness: `tests/themes/sweep.py` (28 beamer themes, classify only).

## Pitfalls found so far
- PDFium (`pdf/pdfium_backend.py` handles these): soft-mask contents are not page objects (beamer's
  block shadow is a black rectangle under a soft mask; its visible pieces come back as shading
  images, `extract._shadow_pieces`); shadings are page objects of their own; a ligature comes back
  as several characters at one origin; a line-end hyphen comes back as U+0002; the text page
  reorders objects on a line (chars are sorted back into content order); `get_cropbox` falls back
  to Letter when the box is inherited; math fonts from xdvipdfmx report CMEX-sized ascent/descent;
  Type 3 fonts return glyph codes (`classify.TYPE3_SYMBOLS`); page labels can come back as raw
  `<FEFF...>` hex (`extract._label`); `GetBitmap` gives an image **without its soft mask** and only
  `GetRenderedBitmap`'s alpha detects one.
- Switching objects off (`FPDFPageObj_SetIsActive`) needs no content regeneration and is undone
  after each render. Ball bullets are patched out of the PNG. Removing block panels without their
  soft-masked shadows leaves black bars.
- Figure removal switches off paths within the figure box + 5 pt; panel removal 1.5 pt, then 5 pt.
- Slides ignores spaceAbove/spaceBelow between bulleted items (docs/calibration.md).
- **Bullet styling is creation order**: style the paragraph like the bullet, create the bullets,
  then style the text in two or more requests (one request over a whole paragraph restyles its
  bullet), or bullets come out 18 pt default.
- **An item added with Enter gets a bullet in its text's colour**: splitting a bulleted paragraph
  keeps the bullet's font and size and drops its colour (rgb or theme, API-made or .pptx-imported),
  and a list's `nestingLevel.bulletStyle` stays Arial 14 black whatever is written. No converter-side
  fix exists (`tools/probe_new_bullet.py`); the person uses paint format or the list options.
- An API-made table row is at least 1.195 em x lineSpacing + 14.4 pt (7.2 pt padding the API
  cannot set); a .pptx table's `a:tcPr` margins survive import, duplication, text/style edits and
  row/column inserts, its row being top + bottom margin + 1.195 em x lineSpacing
  (`tools/probe_pptx_table_margins.py`). Empty cells count with the default font unless given a styled space. A cell that
  wraps doubles its row and the table grows downwards over what is below.
- Slides lowers a SUBSCRIPT 0.371 em of its *own* size (no other offset; a .pptx `baseline` is
  rounded to SUBSCRIPT on import), so a subscript is set no larger than its paragraph's body text
  (`emit.run_sizes`); a wrapped paragraph's pitch comes from each line's own largest run
  (`emit.line_sizes`, `inner_pitch`). Slides rounds each paragraph step to whole pixels with its
  spaceAbove included (`emit.pitch_between(..., gap)`, 0.07 pt rms on 18 boxes). Slides keeps a
  space and the no-break spaces after it together, so the word before a hole wraps with it
  (`text_layout.wrap` models it; a ZWSP would break there, unwritten until checked live).
- Title placeholders exist before any other element: bring them to front after adding shapes.
- Layout pages reject `pageBackgroundFill.propertyState = INHERIT`. Imported layout/master
  placeholders hold "\n" per list level: updateTextStyle works on them, insertText is refused. A
  transparent PNG as a page background shows white under its alpha; layout pictures draw over any
  slide background.
- **The newline a shape's or cell's text ends on is Slides' own**: it reads back but the length
  `deleteText`, `insertText` and a `FIXED_RANGE` accept is one less. A refused request throws out
  the whole batch. Keep it out of every diff and range (`merge.text_edit_requests`,
  `tests/slides_sim.py` refuses like Google).
- Slides stores every created shape at 3,000,000 EMU with the size in the transform's scale.
  Children of a group can't be restacked. Shape shadows, autofit and text insets are read-only; a
  .pptx import keeps shadows but not spAutoFit. Text inside a shape with a shadow gets a shadow.
  Resizing a group scales every child.
- python-pptx: rescale only placeholders with their own `a:xfrm`; give autoshapes an explicit
  `<a:effectLst/>` (the theme's effect style has a shadow).
- Google issues new `contentUrl`s for unchanged pictures (compare pixel signatures, not URLs), and
  re-encodes a picture given back through `createImage`.
- A layout batch and a slide batch in flight together can undo each other's placeholder boxes
  (last commit wins, silently): the layout pass is joined before content batches.
- `render_text`'s `x_subpixel` is C's `%` (negative for glyphs left of their origin).
- `fidelity` reuses saved thumbnails unless the deck was emitted again (or `--refresh`).
- PowerShell 5.1 mangles double quotes inside native-command arguments: keep them out of git
  commit messages. `Get-Content -Raw` reads BOM-less UTF-8 as ANSI: edit text files with the
  editor tools.

## Environment
- Windows, PowerShell 5.1. Python 3.12 venv in `.venv` (`.venv\Scripts\python.exe`);
  `pip install -e .[dev]` brings pytest-xdist.
- MiKTeX (pdflatex / xelatex / lualatex, on-demand packages) for the test decks: `tests/decks/*.tex`,
  built by `tests/decks/build.py [name]` into `tests/decks/out/` (normal and `-handout` variants).
- Layout rules for Google's monorepo import: `tests/` is a package importing its helpers
  relatively; harness code the tests use lives in `beamer2slides.devtools` with `tools/<name>.py`
  kept as runpy shims; data is reached through `importlib.resources` or a module's `__file__`,
  never through `src/` or a checkout path.
- `themes/google`: a beamer theme reproducing the GDG 2024 template; its fonts are downloaded and
  checked by `themes/google/fonts/build_fonts.py` (no third-party binaries in the tree).
