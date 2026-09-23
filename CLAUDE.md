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
  per-character advances in `calibration/advances.json` via `tools/probe_advances.py`).
- **Never destroy what a person did in the deck or the document.** A rebuild of an edited deck is
  refused (`guard.py`); sync is a three-way merge where the deck's edits win and conflicts are
  reported; a destructive write keeps a way back first. `--force-rebuild`, `--follow-labels`,
  `--force-adopted-deck`, `--take-source ID`, `--assume-base` are decisions a *person* makes in
  words - an agent never picks them itself.
- **A conversion or a sync is round trips, not work.** Speed comes from having independent calls
  in the air at once (threads only; each falls back to serial when a caller lends its own client).
  Measure A/B **interleaved** in one sitting: Google's latency drifts 2x with the day.
- Overlays: non-handout PDFs keep the last step of each frame; `--overlays all` keeps every page.

## What becomes native (deck.json element kinds)
Details, measurements and edge cases: docs/project-notes.md "What becomes native".
- `text`: paragraphs, bullet lists (glyph, number, ball and vector bullets with their PDF colour and
  size, `emit.BULLET_SHAPES`, `emit.bullet_level`), inline math as runs with scripts and Unicode,
  links, code. Frame titles use the layout's TITLE placeholder. Hanging labels are `label<TAB>text`.
  RTL (Hebrew/Arabic) is turned into logical order (`bidi.py`) and written `RIGHT_TO_LEFT` with
  mirrored alignment.
- `image`: figure regions (TikZ, plots, raster images with their labels) as pictures. A bare
  `\includegraphics` keeps the author's file byte for byte when it decodes identically
  (`classify.bare_image`, `render.image_file`). A chart's tick rows and centred titles belong to its
  picture (`tick_row`, `axis_titles`); a caption ("Figure:") stays text.
- `table`: text framed by rules (or rule-less `plain_tables`), with borders, merges, fills. Column
  widths come from measured Slides advances (`emit.slides_width`, `fit_columns`) so no cell wraps
  and the table does not grow over its caption; classify cuts a spanning chunk at word gaps when it
  lines up with the other rows.
- `diagram`: node/line/arrow clusters as grouped shapes, connectors and labels (`diagram_from`).
- `shape`: opaque panels such as beamer blocks (title bar + body built to survive resizing).
- Decorations on words: underline/strike/highlight runs; words on small graphics and complex inline
  formulas become **holes** (no-break Roboto Mono spaces) with the picture placed over them by
  measurement on scratch slides (`emit.measure_places`); graphics drawn at words (tikzmark arrows,
  braces) are **overlays** anchored to their text and stretched to the words Slides sets.
- Theme: the most common background goes on the master, shared decoration onto layout pictures,
  layouts' placeholders get the deck's title/body style, frame counters become per-slide text.
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
- `presentations.create` ignores `pageSize` (always 16:9), hence the .pptx route. `createImage`
  needs a fetchable URL and letterboxes. Object ids are 5-50 chars. `getThumbnail` LARGE = 1600 px.
- Emit robustness: a refused batch is retried per slide, then per element, then the deck is rebuilt
  once with pictures of refused regions (`fallback_pictures`).
- Re-running `convert` into the same folder rebuilds that deck in place (same URL); `--new-deck`
  makes another. **The rebuild guard** (`guard.py`, docs/sync.md "Never lose deck edits") refuses
  when the deck was edited, has no base, or came from another PDF, and a forced rebuild keeps a
  backup first (`--backup`, `tools/deck_backup.py`). A .pptx restore brings content back but not
  object ids, so sync refuses a recovered deck.

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
- Proving nothing is lost: `tools/loss_oracle.py` judges one sync; `tools/fuzz_sync.py offline`
  fuzzes the merge through a reference applier (`fuzz_world.py`), `live` against real decks;
  `tools/fuzz_labels.py` measures label pairing. Fixed seeds in `tests/test_sync_fuzz.py`.

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
- Slides table rows are at least 1.195 em x lineSpacing + 14.4 pt (7.2 pt cell padding, not
  settable); empty cells count with the default font unless given a styled space. A cell that
  wraps doubles its row and the table grows downwards over what is below.
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
