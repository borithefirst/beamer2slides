# Compile latency

Where the time goes when the project compiles a beamer source, measured 2026-10-27 to decide
what to optimise. The compile measured is the one `inverse.Workspace.compile` runs (the adopt /
pull loop's): the engine with `-interaction=nonstopmode -halt-on-error -synctex=1
-file-line-error`, passes until the aux files settle and nothing asks for a rerun, at most 3.

**Settings.** Linux cloud VM (4 vCPU, Firecracker), TeX Live 2023 (Ubuntu 24.04 packages:
pdfTeX 1.40.25, LuaHBTeX 1.17.0, XeTeX 0.999995), luaotfload 2023-08-19. Not measured: MiKTeX
on Windows, where process start-up and file lookup differ - expect other absolute numbers,
probably the same ratios.

**Decks.** Hand-written: `tests/decks` 11 (research talk, Madrid, 7 pages), 12 (metropolis, 9),
23 (raster images), 27 (text fit, 18), 29 (TikZ, 39), and two scaled to talk length by repeating
the body with labels renamed: research x6 (42 pages), metropolis x5 (45). Adopted: the six
showcase targets bootstrapped as `tests/test_adopt_compiles.py` does (no fonts: TeX Gyre and the
scripts' stand-ins), and water / talk / hashing x8 (40-48 slides). Every adopted source asks for
**lualatex** (fontspec, babel's `\babelprovide`), so the loop's hot compile is lualatex.

**Method.** A probe read before the deck (`\input{b2stime}\input{main}`) writes `\sys_timer:`
(1/65536 s since the job began; pdfTeX, XeTeX and LuaTeX all have it) at every file
open/close (`file/before`, `file/after` hooks), `\begin{document}`, every `frame`, every
shipout and `\end{document}`; the wall clock after the last mark is font embedding, PDF close
and exit. For lualatex a second probe re-adds every callback function under its own description
with a timer (`luatexbase.remove_from_callback` / `add_to_callback`), and a third marks where
each `slides.sty` command starts (a command is charged the time to the next mark). CPU time is
the TeX process's user+sys (`os.wait4`). Runs are interleaved (every configuration once per
round), minimum and median of 5-9 rounds kept.

**Noise floor.** The same configuration spreads 11-20% across 9 runs on this VM, even on CPU
time, and the host steals CPU (wall-clock minimum picked up one 17%-fast outlier). Effects
under ~8% are not resolvable here. The probe's own cost is inside that noise (median -1% to
+6%).

## One pass, by engine (steady state, settled aux, min wall of 5, seconds)

| deck (pages)          | pdflatex | xelatex | lualatex |
|-----------------------|---------:|--------:|---------:|
| research talk (7)     |     0.78 |    1.05 |     1.60 |
| metropolis talk (9)   |     1.10 |    1.62 |     2.17 |
| text fit (18)         |     1.43 |    1.62 |     2.36 |
| TikZ diagrams (39)    |     2.92 |    3.12 |     3.59 |
| research x6 (42)      |     1.56 |    1.60 |     2.32 |
| metropolis x5 (45)    |     1.86 |    2.17 |     2.93 |
| adopted water x8 (48) |        - |       - |     5.85 |
| adopted talk x8 (40)  |        - |       - |     3.00 |
| adopted hashing x8 (48) |      - |       - |     3.85 |

pdflatex is fastest everywhere; lualatex is 1.25-2x slower (start-up +0.4 s, preamble +0.1 to
+0.5 s, body 1.1-1.5x). Wall time equals CPU time: compiling is single-threaded CPU work,
disk I/O plays no part. Adopted sources cannot use pdflatex.

## Inside a pass

| phase                     | pdflatex       | lualatex       | what it is |
|---------------------------|----------------|----------------|------------|
| start-up (before line 1)  | 0.08-0.11 s    | 0.40-0.52 s    | binary, kpathsea's ls-R, format undump; lualatex also initialises luaotfload |
| preamble                  | 0.33-0.80 s    | 0.60-1.10 s    | class, theme, packages, the deck's own preamble lines (fontspec font loading) |
| `\begin{document}`        | 0.02-0.06 s    | 0.03-0.12 s    | AtBeginDocument hooks, .nav/.toc read |
| body (typesetting)        | 0.02-0.05 s/page | 0.03-0.09 s/page | macros of every frame |
| shipout                   | 2-3 ms/page    | 4-7 ms/page    | writing pages |
| after `\end{document}`    | 0.08-0.11 s    | 0.05-0.18 s    | font embedding, PDF close, exit |

Small decks are fixed cost (pdflatex research talk: 0.6 of 0.78 s); talk-length decks
are body (adopted water x8: 4.6 of 6.4 s).

**Start-up, lualatex** (`lualatex '\csname @@end\endcsname'` = 0.447 s; plain `luahbtex` 0.116 s,
`pdflatex` 0.171 s, an engine with no format 0.08 s): about 0.24 s is Lua run at every job -
luaotfload's name database load 0.061, its features 0.058, Unicode normalisation tables parsed
from text 0.056, the merged font loader 0.021, multiscript 0.020 - and about 0.2 s the 12 MB
format and kpathsea. None of it depends on the deck.

**Preamble.** Biggest single files: metropolis' font theme 0.21-0.24 s on lualatex (Fira via
fontspec; 0.05 s on pdflatex), tcolorbox 0.10-0.14 s, fontspec 0.11-0.13 s, circuitikz 0.15-0.18
s, the adopted source's own lines 0.18-0.23 s (`\setsansfont`, babel `\babelprovide`), beamer's
base files 0.14-0.21 s together, PGF/TikZ ~0.1 s, hyperref ~0.08 s.

**Body, lualatex: Lua callbacks are not the cost.** Font shaping (luaotfload's node processor),
babel's hyphenation and `slides.justify` total 0.08-0.30 s, 3-7% of body time. The body is TeX
macro expansion, which LuaTeX runs slower than pdfTeX.

**Body, adopted sources: `slides.sty` (ours, written by adopt).** Per call:

| command          | cost       | share of body |
|------------------|------------|---------------|
| `\slidetable`    | 80-160 ms  | 26-30% (one table per original deck; water's: 25 cells in five scripts) |
| `\slideshape` (`\sliderect`, freeforms) | 3-8 ms | 17-33% |
| `\slideline`     | 4.0-4.5 ms | 2-17% |
| `\slideellipse`  | 2.5-2.8 ms | 2-14% |
| `\slidepar` + a `slidebox`'s end | 0.6-0.8 + 0.7-1.1 ms | 12-25% |
| frame start (options, background) | 2.3-3.5 ms | 3-7% |

Each shape is a `tikzpicture`: 46-65% of adopted body time is inside TikZ. A metropolis frame's
start costs 20 ms (its frame title and progress bar).

## Passes

Cold (empty build folder), the rule runs 2 passes on every deck, 3 on two (overlays and labels
settling). In 5 of 8 decks the second pass changed **no pixel** of any page (rendered at 0.7x):
it ran because pass 1 created the aux files from nothing and hyperref asked to get the outline
(bookmarks, which the converter never reads) right. It did change pages for metropolis (frame
total, 2 pages), 22_overlays_on_text (8) and 19_labels_on_graphics (12 + 1 in pass 3).

Warm - the loop's case, the same build folder, one frame edited - every deck took **1 pass**.
The build folder lives as long as the workspace, so the double pass is paid once per
adopt / pull / convert of a fresh source.

## Knobs (A/B interleaved, CPU time, median of 9)

| change                                   | effect           |
|------------------------------------------|------------------|
| no `-synctex=1`                          | -4% to +6%: noise |
| `-draftmode` (no PDF written)            | -6% to +2%: noise |
| no compression (`\pdfcompresslevel=0`)   | -2% to +4%: noise |
| precompiled preamble (`mylatexformat`), pdflatex | **-25% to -31%** |
| precompiled preamble, lualatex + beamer  | **fails**: PGF's core (which beamer loads) reserves PDF objects at load; the format keeps their numbers, not the objects ("referenced object has wrong type") |
| `\includeonlyframes{one}` (lualatex, 40-48 slides) | **-45% to -70%** (water x8: 6.3 -> 1.9 s) |
| `\includeonlyframes{a quarter}`          | -34% to -57% (water x8: 6.3 -> 2.7 s) |

A frame left out costs ~4 ms; a one-frame compile is the fixed cost and little else.

## One-off costs

The first lualatex run on a machine with an empty font cache takes **10.5 s instead of 1.4 s**:
luaotfload builds its name database and caches each font it parses. Every fresh container pays
it - CI, each new Cloud Run instance of the playground, a sandbox. See "A fresh machine" below
for what it costs in parts and how an image avoids it.

## Compile against the rest of a round

On the same PDFs, `read_with_notes` + `classify` take 0.80 s (water x8) and 0.59 s (talk x8)
against 6.3 s and 3.0 s of compile: compile is 83-89% of a loop round's local CPU.

## Where to spend (ranked by measured ceiling)

1. **Compile only the frames that changed** (`\includeonlyframes`; adopted sources have a label
   per frame). Round cost -> fixed cost + changed frames: -45..-70% on talk-length decks. The
   same mechanism splits a deck across cores (a quarter per process: 6.3 -> 2.7 s wall on
   4 cores, plus a page merge). Needs: splicing the new pages into the last PDF, SyncTeX per
   piece, frame totals.
2. **`slides.sty`'s hot commands**: tables (80-160 ms each) and TikZ shapes (3-8 ms each) are
   60-75% of an adopted body. Cheaper drawing for plain rectangles, lines and ellipses (PGF's
   basic layer or direct PDF literals) and a cheaper table are ours to write.
3. **The cold second pass**: 5 of 8 decks gain nothing from it. A rule that knows which aux
   changes reach the pages (not bookmarks) halves a cold compile there; pixel equality of the
   two passes is the oracle to check such a rule with.
4. **Fixed cost per compile** (lualatex 1.4-1.9 s: 0.45 s start-up, 0.8-1.0 s preamble): only a
   process that outlives one compile removes it; a format would need PGF's object reservations
   redone after undump, and fontspec's fonts reloaded. On pdflatex a format works today (-25 to
   -31%).
5. **Warm the font cache in images** (playground, CI): 9 s once per fresh container.

Not worth it: SyncTeX, compression, draft mode, the PDF writer, Lua shaping callbacks, disk I/O.

## What this cannot see

- The scaled decks repeat their own slides: the content mix of a real 50-slide talk differs.
  The corpus decks (devfest2020's 28 s for two passes) need Google to fetch and were not run.
- Adopted sources ran with stand-in fonts (TeX Gyre): `define_font` took 1.4-3 ms a call,
  0.015-0.063 s a deck. Real Google Fonts were measured later (water with its fetched fonts,
  "Done: fonts with a STAT table" below).
- Windows / MiKTeX was not measured.

## Deeper: is the CPU justified? (2026-10-28)

Same machine and method; this VM ran ~20% faster that day (water x8: 4.7 s a compile), so
compare within this section only.

### Compiles nobody needed (Python)

- **Every edit round of `converge` compiles the same source twice.** After writing a round's
  edits it compiles to check they build (`inverse.py:3098`) and keeps only the success flag;
  the next round's `ws.build(..., compiled=ready)` has `ready = None` (3039) and compiles again
  (756) - same text, options and build folder, unless a revert (3129-3148) wrote in between.
  `build` already takes a `compiled=` handoff (pull's first round uses it). One compile of two
  per round; a round of a 48-slide deck is ~2 x 4.7 s compile + 0.8 s extract/classify.
  After a bisect whose last edit was kept, the next build is a third compile of the same text.
- **adopt compiles its finished source again from an empty folder** to record the sync base
  (`record_base` -> `adopt_sync.convert_source_of`, `Workspace(..., work / "compile", fresh=True)`,
  `adopt_sync.py:658`): 2 passes, of the text the loop's last compile already built (with
  `--apply`).

### Idle cores

A compile is one thread; the machine has four.

| water x8 (48 slides), one compile 4.6-4.7 s | wall    | pages identical to one compile |
|-------------------------------------------|---------|-------------------------------|
| 2 / 3 / 4 `\includeonlyframes` pieces at once, merged | 2.98 / 2.55 / 2.29 s | 48/48 |
| one pre-warmed engine (body sent after 3 s)            | 3.67 s  | 48/48 |
| 2 / 3 / 4 pre-warmed pieces, merged                    | 1.98 / 1.43 / **1.14 s** | 48/48 |

Pre-warmed: the engine is started on the preamble followed by the primitive `\input` of a FIFO
(LaTeX's `\input` would open it twice to test it), and does start-up and preamble while the loop
does something else; the body is written into the FIFO when it is ready. talk x8: 2.28 ->
1.30 s; hashing (6 slides): 1.47 -> 0.47 s. A piece pays the fixed cost again, which is why
pieces alone stop near 2x; pre-warmed they do not. A merge (pypdfium2 `import_pages`) takes
4 ms. Not settled: SyncTeX per piece (inverse maps pages to frames by it; the labels would do),
the preamble changing between rounds (`ensure_preamble`: start a new engine), outlines and
links across pieces.

### TikZ: ~150x the floor

One filled rectangle, 400 of them in the water preamble, lualatex, ms each:

| drawn with                                          | ms     |
|-----------------------------------------------------|--------|
| a PDF literal (`re f`)                              | 0.008  |
| PGF's basic layer                                   | 0.76   |
| `\tikz\fill`                                        | 1.2-1.3 |
| textpos + `tikzpicture` + `\useasboundingbox`, as `slides.sty` | 2.18 |
| `\sliderect`                                        | 2.24   |
| `\slideline` (six `\fpeval`) / a TikZ arrow line    | 3.74 / 2.46 |
| `\slideellipse` / `\tikz\fill ... ellipse`          | 2.26 / 1.53 |

Trimming around TikZ does not pay: `baseline=0pt` for `(current bounding box.north)` and
`\dimexpr` for `\slideline`'s `\fpeval` moved the whole water x8 deck by -1.4% to +0.5% (median of
9, noise), and the `\dimexpr` one changed pixels on 16 of 48 pages. The cost is TikZ itself.

`\slidetable` (water's: 25 cells, 137 ms): borders 15 ms, fills 26 ms, language switches 4 ms;
with all three off, 89 ms, of which **TikZ `\node`s placing the finished cell boxes are 56 ms
(2.25 ms a cell)** and the first-baseline `\vsplit` loop 2 ms. About 100 of the 137 ms is TikZ
doing what a box placement (`\kern`/`\raise`) or a rule does in microseconds.

Shapes in the six adopted showcase decks: rectangles 31 plain, 21 rounded, 3 rounded+turned;
ellipses 24 plain, 7 turned; freeforms 10 (point lists); `\slideshape` 34; lines 30, every one
with an arrow head. A turn is one `cm`; rounded corners are four curves; an arrow head is a
triangle. Opacity (an ExtGState) and dash patterns are the parts needing more than geometry.

### Verdict

| cost                                   | justified? |
|----------------------------------------|------------|
| the second compile of every loop round | no: drop it (hand the check's PDF over) |
| adopt's base compile from an empty folder | no: reuse the loop's PDF or its aux files |
| the cold second pass for bookmarks     | no, for the converter (5 of 8 decks, no pixel moves) |
| ~1.0-1.5 s fixed per lualatex process  | per process yes; hidden by pre-warming |
| one thread out of four                 | no: pieces, pre-warmed: 4.2x on water x8 |
| TikZ for every shape and table cell    | convenience, not need: 60-75% of an adopted body; table cell nodes and borders first |
| Lua shaping, SyncTeX, compression, PDF writing | small or noise (part one) |

## Done: compiles nobody needed (2026-10-28)

Three changes in `inverse.Workspace.compile`, all invisible to the pages:

- **A tree compiled before is not compiled again.** `tree_digest` (names and bytes of every file
  in the tree plus the command line, ~45 ms on water's 33 MB tree) is remembered after a
  successful compile; the same digest with its PDF still there returns that PDF. `converge`'s
  check of a round's edits is now the PDF the next round reads, and a kept bisect step costs no
  third compile. A compile that wrote into its own tree (minted, externalised pictures) is not
  remembered.
- **babel's language switches no longer force a pass.** A round that wraps a paragraph in
  `otherlanguage` adds `\babel@aux{hebrew}{}` / `\babel@toc {english}{}\relax` pairs that
  leave the language as it was; `aux_state` hashes the files with those cut
  (`language_switches`). They asked for a second pass on almost every water round.
- **A failed compile puts the aux files back.** It left `main.aux` cut off where it stopped,
  so the next compile needed two passes to settle it again; now the files the last good compile
  left are restored (`aux_files`).

Checked by compiling one more pass after every compile of a loop (same options) and comparing
each rendered page: no page moved on talk or water, and every compile after the first is one
pass. The residuals each round leaves are the same as before.

| loop, 3 rounds (A/B interleaved) | compiles' wall before | after | |
|----------------------------------|-------------|-------|------|
| talk (2 runs each)               | 30.6 s      | 22.8 s | -25% |
| water                            | 105.3 s     | 57.4 s | -45% |

Seen on the way, outside speed: on both decks the loop's open residuals *rise* after round 0
(water 14 -> 66 -> 47 -> 43, talk 4 -> 16 -> 11 -> 17); `frame_guard` puts frames back at their
best round, but each of those rounds still costs a compile.

## Done: an engine waiting on the preamble (2026-10-28)

A lualatex pass spends its first second or more starting up and reading the preamble, which is
the same every round. `converge` now has the next pass's engine started early
(`Workspace.warm_up`, `start_warm`), on another core, while the current pass runs:

- The engine is started on main as the tree stands, with a Lua hook (`WARM_LUA`) on the first file
  it opens (main): lines 1..k (k: the line before `\begin{document}`) come from a copy of those
  lines taken at start, and line k waits for a "go" on stdin. Every other file is read by LuaTeX
  itself (serving them all through Lua cost 28 s a pass instead of 6). After the go, the rest of
  main is read from main as it is then. CR before LF is dropped and trailing spaces are left to
  LuaTeX, as its own reader does.
- A pass takes the waiting engine only when it is the same command, still alive, main still
  begins with the copied lines, and every other file in the tree has the size and mtime it had
  when the engine started (`file_stamps`: a preamble's `\input`, a style file, a picture).
  Otherwise the engine is stopped and the pass is compiled cold. The aux files are copied to the
  engine's own folder at the go (it already holds a `main.log` from its start), and everything it
  writes is moved into the build folder afterwards; two such folders alternate.
- A waiting engine that fails is compiled cold again from the same aux files; if the cold pass
  succeeds the waiting engines are switched off for the workspace; if it fails too,
  failures are believed from then on.
- An engine nobody takes exits when its stdin closes (exit 75), and `atexit` stops the rest.

`tests/test_inverse.py::test_an_engine_waiting_on_the_preamble_compiles_what_a_cold_one_compiles`
compiles a body edit, a preamble edit in main, and one in a file the preamble reads, warm and
cold, and compares the pixels; breaking the head check, the file stamps or the CR drop each fails
it. On the six decks of the table below, every warm pass of a 3-round loop was compiled again
cold from the same tree and aux files: 28 of 28 passes gave the same rendered pages and the same
SyncTeX (output paths and byte offsets aside).

**Fonts were re-cached every run.** luaotfload keeps one cache entry per font *name*
(`luatex-cache/generic/fonts/otl/inter-bold.luc`), checked against the font file's size and
mtime. adopt copied the fonts into each tree with `shutil.copyfile`, so every tree had new
mtimes and the first pass of every run rebuilt the cache of every font: ~6.5-7 s on talk.
adopt and `scripts` now copy with `copy2`, which keeps the font store's mtime.

| loop, 3 rounds, whole wall (A/B interleaved, 2 runs each)     | cold | warm | |
|-------------------------------------------------|------|------|------|
| talk                                            | 17.1 s | 10.7 s | -37% |
| water                                           | 39.7 s | 33.5 s | -16% |
| bees                                            | 9.8 s  | 6.5 s  | -34% |
| hashing                                         | 25.5 s | 16.1 s | -37% |
| review                                          | 16.4 s | 12.0 s | -27% |
| portfolio (converged at round 0: one compile)   | 3.7 s  | 2.6 s  | -29% |

Both arms have the `copy2` fonts (talk's loop was 22.8 s in the table above, cold 17.1 s here:
the font cache, though not measured in one sitting). Residuals per round are identical in every
run. Per deck, one pass in each run missed its engine, rightly: `ensure_preamble` changed the
preamble. On talk a warm pass takes 0.3-0.9 s. Water gains least: the engine saves the same start-up
and preamble, but its passes are mostly body.

## A fresh machine (2026-10-28)

What the first lualatex run on a new machine pays, in parts (same VM, TeX Live 2023):

| run | wall |
|---|---|
| article, no font cache at all (names database built) | 7-8 s on a warm disk, 53 s cold |
| the same article again | 0.5 s |
| talk (adopted, 5 faces), no cache at all | 9.5 s |
| talk, names database present, no per-font caches | 2.2 s |
| talk, everything cached | 1.4 s |
| pdflatex, no cache at all | 0.58 s (no such cost) |

**The cache must be built by the user who compiles.** luaotfload looks for its names database
only under the first cache folder it may *write* (`luaotfload-configuration.lua`,
`build_cache_paths`: the names path is `getwritablepath(...)`), never in a read-only one. A
database built as root in `/var/lib/texmf/luatex-cache` is not read by user 1000: run as the
`ubuntu` user, lualatex said "Font names database not found, generating new one" and took 8.2 s,
with root's database sitting right there. So:

- **The playground image** (`Dockerfile`) runs `luaotfload-tool -u` and one beamer + fontspec
  compile *after* `USER player`. The database and the TeX Gyre / Latin Modern caches go into
  the image under `/home/player`, and a new Cloud Run instance's first compile pays 1-2 s, not
  9-55 s. (Cloud Run's writable layer is in memory: a font cache written at run time is lost
  with the instance, the image's is not.)
- **Any other sandbox** (`adopt_bench.sandbox` runs as the bench's own user; a gVisor or
  container sandbox an agent runs in): either warm it the same way as the user it runs as, or
  set `TEXMFCACHE` to one writable folder that is kept between runs and was filled once. A
  read-only prefilled cache is no use for the names database (above); per-font caches in a
  read-only folder are read.
- **Fonts copied into a tree keep their mtime** (`copy2`, "Fonts were re-cached every run"
  above): luaotfload's per-font cache is keyed by name, size and mtime, so a fresh mtime is a
  fresh parse. The same holds when timing by hand: `cp -r` gives new mtimes and two trees
  alternating thrash each other's cache (one A/B arm read 15 s instead of 6).

## Done: fonts with a STAT table (2026-10-28)

water with its real fonts (fetched from google/fonts, Latin + Arabic + Hebrew + CJK fallback)
spent a quarter of each pass on something no page needs. luaotfload (`luaotfload-features.lua`)
wraps `fonts.constructors.trytosharefont` so that any font with variation data gets a *glyph
stream provider*: at PDF write the `glyph_stream_provider` callback loads that font's whole
"streams" cache (5-18 MB for a CJK face, ~0.2 s each) to write the glyphs. That is right for a
variable font used at some axis value. But the font reader (`readers.stat`) makes variation
data out of a STAT table alone, and every static instance `fontfetch` cuts from a google/fonts
variable font keeps its STAT table: a font with no axes at all was being written the slow way.
The same code is in luaotfload's current main.

The fix is one line of Lua before any font loads (`inverse.NO_GLYPH_STREAMS`): after the
original, a font whose variation data has no `axis` and that is no named instance gets its
stream provider taken away, and is written from its own file as any static font is. It is given
on the command line of every lualatex compile `Workspace` runs, and written into the preamble
`adopt` writes, so the person's own compiles of an adopted source get it too.

| water (6 slides, fetched fonts), interleaved | before | after | |
|---|---|---|---|
| one pass (5 runs) | 5.8-6.0 s | 4.25-4.5 s | -26% |
| `Workspace.compile` (passes until settled, 3 runs) | 13.25 s | 9.4 s | -29% |
| + no CJK fallback lines (not done, see below) | | 3.7-3.9 s a pass | |

Pages: 6/6 the same pixels, the same text and character boxes. The six showcase trees (TeX
Gyre stand-ins, no STAT) render the same and take the same time. Stripping STAT from the
font files instead gives the same speed and the same pages; the TeX-side line also covers fonts
nobody fetched. `tests/test_inverse.py::test_a_static_font_with_a_stat_table_is_written_from_its_own_file`
builds a STAT-only font, has the page report its font's stream provider (2 without the line, nil
with it) and compares the pixels; with the line taken out of `compile` it fails.

**Still open on fonts** (water, after the fix: start-up 0.4 s, preamble 1.25 s, frames 2.3 s,
end 0.6 s; `define_font` 140 calls, 1.17 s, 60 of them in the body):

- ~~The CJK fallback is attached to every face at every size~~: done, next section.
- Each size of each face is a `define_font` of its own (1-40 ms). Fewer distinct sizes in what
  adopt writes would help a little; not measured.

## Done: the fallback chain, on demand (2026-10-28)

`scripts.script_preamble` gives every font of an adopted deck the scripts' fallback chain
(`\defaultfontfeatures`), so a glyph the face lacks comes from the first chain font that has it.
luaotfload's own `fallback=` builds that chain **when the face is defined**: every chain font, at
the face's size, for every face and every size the document uses. On water that was NotoSansSC
(31k glyphs) defined 30 times a pass, for the dozen Chinese words of one slide.

`scripts.LAZY_FALLBACK` is the same chain built **when a glyph first needs it**. The feature
(`b2sfallback=b2sscripts`) only marks the face. A `pre_shaping_filter` callback, which luaotfload
calls just before its own fallback step, looks at each glyph: if the face lacks it, it asks the
chain at the face's size for the first font that has it, defining the chain fonts the first time
that size is asked. The chain fonts are requested with the same `;-fallback` suffix luaotfload uses,
and the first font in the chain wins as in luaotfload, so they are the same fonts and glyphs.

Measured with `Workspace.compile` on today's adoption of the showcase decks, old chain and new
interleaved, 4 rounds each (seconds per compile; pages compared pixel for pixel):

| deck | chain | luaotfload `fallback=` | on demand | pages |
|---|---|---|---|---|
| water | NotoSansSC (CJK) | 12.8-13.9 | 9.1-10.3 (-29%) | 6/6 same |
| review | Montserrat + Noto Sans Symbols 2 | 3.5-3.7 | 3.2-3.3 (-12%) | 5/5 same |
| hashing | Lato | 3.6-3.7 | 3.2-3.7 (same) | 6/6 same |

Water's PDF embeds NotoSansSC Regular and Bold either way, so the chain draws glyphs in both
versions; the pages are the same, not just blank alike. On water the chain is now built at one
size, not thirty.

`tests/test_inverse.py::test_the_fallback_chain_defined_on_demand_draws_what_luaotfloads_own_draws`
compiles three tiny fonts: a face with A, a first chain font with a square B, a second with a
pointed B and a C. It sets them at three sizes and compares the pixels with luaotfload's own
`fallback=` and with no chain at all. If the later chain font is allowed to win, or every size
gets one size's chain, the test fails (both tried).

A trap found on the way: a feature value of `t` is read as the boolean *true*, so a chain can't
be named `t`.

## Done: drawn without TikZ (2026-10-28)

"TikZ: ~150x the floor" above: a plain rectangle cost 2.2 ms and a table cell's `\node` 2.25 ms, for
what pgf's system layer does in microseconds. `slides.sty` now draws the plain cases with the
**system layer's own commands, the ones TikZ itself ends in, in TikZ's order**
(`adopt_shapes.PLAIN_MACRO`, the table's `\slides@t@q*` in `adopt.TABLE_MACROS`). The source does
not change: `\sliderect[card,fill=Red]{...}`, `\cell[fill=...]`, `\hborder{1}{...}` are written as
before and take any TikZ option.

- **Exact by construction, not by tuning.** A TikZ coordinate goes through `\pgfpoint`, i.e.
  `\pgfmathsetlength` on its text; the fast path calls `\pgfmathsetlength` on the same tokens, so
  the lengths are the same scaled points. The picture is pgfpicture's real skeleton
  (`pgfcorescopes.code.tex`: `\pgfsys@beginpicture`, the scope, `\pgfsetcolor{.}`,
  `\pgfsetlinewidth{0.4pt}`, the inner hbox, `\pgfsys@discardpath`, `\pgfsys@typesetpicturebox`
  with the bounding box TikZ would record), so the PDF stream is TikZ's
  (`q 0 G 0 g 0.3985 w q q <colour, width> path f|S|B Q Q n Q`). A cell is placed the way
  `\pgfnode` places a rectangle node: south west / north east from the box, the inner seps and
  minimum sizes, the anchor's height, `\pgfsys@hboxsynced`.
- **Only while nothing could make TikZ draw it otherwise.**
  - The `every picture/scope/path/node` styles must still be empty (`\slides@tikzplain`, compared
    with an empty style's `.@cmd`), and no `every text node part`/`every rectangle node` may exist.
  - A cell also needs TikZ's node globals at their defaults: shape rectangle, no
    `node halign header`, no text colour, opacity, font, width, height or depth, and no
    `execute at begin/end node` or node hooks.
  - A rectangle's options must expand, through `\slideshapestyle` styles, to `fill=`, `draw=` and
    `line width=` only. A style counts only while its `.@cmd` is still the one
    `\slideshapestyle` made, so a later `\tikzset` of the same name sends it back to TikZ.
  - Each colour must be a defined colour that no TikZ or pgf key shares a name with
    (`\slides@ifcolour`).
  - Anything else goes to TikZ: rounded or turned rectangles, opacity, dashes and every other
    shape.
- `bootstrap` writes the block into `slides.sty` whenever shapes or tables are used. It sorts
  before both ("% --- Drawn" < "% --- Shapes" < "% --- Tables").

Measured on today's adoption of the six showcase decks (TeX Gyre stand-ins, no fetched fonts)
and an 8x water deck (48 frames). Each figure is `Workspace(main, work, handout=False,
engine="lualatex", fresh=True).compile()` from an empty folder: passes until the aux settles.
Old and new trees were interleaved, 5 rounds in one sitting, and the table gives the minimum of
the 5 in seconds. Pages were rendered with `render(2.0, clip=None, transparent=False)` and
compared byte for byte.

| deck | old | new | | pages identical |
|---|---|---|---|---|
| bees | 2.88 | 2.90 | (noise) | 4/4 |
| hashing | 3.41 | 3.36 | -1.5% | 6/6 |
| portfolio | 2.97 | 3.00 | (noise) | 5/5 |
| review | 3.21 | 3.11 | -3% | 5/5 |
| talk | 3.03 | 2.98 | -2% | 5/5 |
| water | 7.78 | 7.07 | -9% | 6/6 |
| water x8 (48 frames) | 25.27 | 20.81 | **-18%** | 48/48 |

A small deck is mostly start-up, preamble and the second pass, so it gains a few percent at most.
The more a body draws, the more is saved: water x8 saves 4.5 s of its 25 s.

What still goes to TikZ (counted with a `\typeout` probe on each fast path):

| deck | `\sliderect` fast / all | table fills / rules / cells fast | still TikZ |
|---|---|---|---|
| bees | 5/14 | - | 9 rounded or turned rectangles, 18 ellipses |
| hashing | 6/12 | 4 / 10 / 15 (all) | 6 rounded or turned rectangles, 6 lines |
| portfolio | 2/2 | - | 4 ellipses, 3 lines |
| review | 2/7 | 35 / 14 / 35 (all) | 5 rounded or turned rectangles |
| talk | 14/18 | - | 4 rounded or turned rectangles, 10 ellipses, 1 line |
| water | 13/13 | 25 / 12 / 25 (all) | 4 ellipses, 20 lines |

Not done: ellipses (four curves, as TikZ computes them), rounded corners and lines. Every adopted
line has an arrow head, which is TikZ's arrow code.

**The oracle sees a hair.**
- Old against new over all six decks: every page identical.
- One fast path broken on purpose by 0.1 pt in a copied `slides.sty`:
  - a table cell 0.1 pt lower on water: 1 page of 6 differs, 8,260 bytes, max delta 198;
  - a plain rectangle 0.1 pt wider on talk: 5 pages of 5 differ, about 60 bytes each, max delta 211.
- `tests/test_adopt_compiles.py::test_what_slides_sty_draws_without_tikz_is_what_tikz_draws`
  adopts a slide holding:
  - three rectangles of one named look;
  - a fill with an outline, and an outline alone;
  - a turned rectangle;
  - a table with merged, filled, middle- and bottom-aligned cells and solid and dashed rules.

  It compiles that slide twice: once with probes saying each fast path ran, and once with
  `every path` changed, which sends everything to TikZ. It then compares the pixels.
- Broken in the source (`adopt_shapes.py` / `adopt.py`), the test fails for:
  - a rectangle 0.1 pt wider;
  - a cell 0.1 pt to the right;
  - a cell 0.25 pt lower.

  A cell only 0.1 pt lower passes: PDFium sets glyphs on whole pixels vertically at scale 2, and
  the test's table did not cross a pixel boundary where water's did.

## Going further: frames in parallel (analysis, 2026-10-28)

A pass is one thread, and TeX has no way to run one document on several cores: every page
depends on the counters, marks, floats and aux state left by the pages before it. The only
parallelism there is is *several engines on pieces of the deck*:

- **How**: beamer's `\includeonlyframes{a,b}` keeps only the named frames (adopted sources have
  one label per frame). N engines, each pre-warmed on the preamble (as `start_warm` does), each
  given a quarter of the labels, and their PDFs joined. Measured on water x8: one compile 4.6 s,
  four pre-warmed pieces 1.14 s, 48/48 pages identical ("Idle cores" above).
- **What it needs before it is right**: an excluded frame still steps `framenumber` (the step is
  before the restriction test) but makes no page, so `page` differs between pieces: anything
  printing the page number, `\pageref`, the navigation's page links and the aux written by each
  piece must be stitched (the aux of piece k is only correct when pieces 1..k-1 ran first, i.e.
  a second round, which the loop's passes already are). SyncTeX is per piece and must be
  re-based on the joined PDF (inverse maps pages to frames by it; the labels would do instead).
  Outlines and links across pieces need a merge.
- **What it buys**: on the six showcase decks (5-6 frames) a converge round changes ~4 frames,
  so compiling only the changed frames saves little; four pieces on four cores still cut a
  6-slide pass to about a third (hashing 1.47 -> 0.47 s pre-warmed). On a 40-50 slide talk the
  gain is 3-4x a pass.
- **Not a fork**: LuaTeX has no `os.fork`; reaching `fork()` through `ffi` needs
  `--shell-escape` or a `--lua` init script, which the playground forbids (`shell_escape=f`) and
  which would let a source run anything. Pre-warmed separate engines need neither.
- **pdflatex** has no fixed font cost and starts in 0.4 s; a format (`-ini`, `\dump`) cuts it
  another 25-31%. Adopted sources need lualatex (fontspec, babel's providers), so this helps
  only hand-written beamer sources.

Order of work, by measured ceiling: (1) the CJK fallback on demand (done above: -29% on water);
(2) N pre-warmed pieces with stitched aux and SyncTeX (3-4x on talk-length
decks, a real project); (3) cheaper `slides.sty` drawing for plain shapes and tables (60-75%
of an adopted body is these commands).

## Done: a compile in pieces (2026-10-28)

Item (2) above. `converge` compiles a lualatex source on several cores at once
(`Workspace.compile_in_pieces`, `pieces.py`, `pdf/pure/join.py`): N engines, each waiting on the
preamble, each typesetting its own share of the frames, the PDFs
joined, the .aux files merged. N is `pieces.piece_count()`: one per core, at most 4;
`$B2S_COMPILE_PIECES` sets it, `0` or `1` compiles whole.

**How a piece skips what is not its own.** Every compile (whole or piece) loads `pieces.LOAD`
before the source, which writes at the end of each labelled frame the count registers that differ
from the document's start, `\btwos@ckpt{label}{0=12;5=3;...}`, and the start's own nonzero ones,
`\btwos@base{...}`. A piece reads the last pass's .aux: frames before its cut are skipped like
`\includeonlyframes` skips them (beamer's `\beamer@framerestriction`), and at each skipped labelled
frame's end the registers are set to its checkpoint, so the piece's first own frame begins with
the page, frame, section and subsection numbers a whole compile had there. LaTeX's allocation
pointers (`\count10`-`22`, `256`-`263`) are left alone. What a skipped frame writes to the .aux goes
to a void file (`\jobname.btwos`); a piece marks where its own part starts and ends
(`\btwos@own{1}` / `{0}`). Cuts fall after labels recorded once, at even shares of the pages.

**Merge.** `pieces.merged` puts the pieces' own parts end to end and rebuilds the contents files
(.toc, .nav, .snm, ...) from their `\@writefile` lines, as `\end{document}` would, babel's
`\babel@toc` line included. A line it does not know (`KNOWN`), a missing mark, or a piece whose
bookmarks (.out) are not a prefix of the last piece's sends the compile back whole. The PDFs are
joined by our own PDF writer (`pdf.join_files`): objects renumbered, one page tree, page labels per
file, named destinations as one tree (a real destination beats LuaTeX's `[last page /Fit]`
stand-in for a link its piece does not hold), and the outline taken from the first file when every
file's (depth, title, destination name) list is the same. Each piece's SyncTeX is kept
(`piece_syncs`); `sync_pages` reads them in order.

**A fixpoint, like any LaTeX pass.** A piece pass is settled when the .aux files, checkpoints
included, come out as they went in (`piece_state`). When a frame came or went, the pieces after
it began at stale numbers; before the next pass, `pieces.carried` moves each later piece's
checkpoints by how far its start moved (a register stops moving where the piece reset it, as a
section resets its subsections). At most 3 piece passes; a page count that disagrees with the
.aux once settled refuses (`split_refused` says why) and the compile finishes whole. `aux_state`,
which decides when whole passes settle, ignores the `\btwos@` lines, so a first compile costs the
passes it always did; a whole compile's .aux read by a source without `pieces.LOAD` has them
dropped first (`drop_marks`). Fragile frames' `.vrb` scratch files are allowed in a piece's folder.

**Checked.** `tests/test_inverse.py::test_a_compile_in_pieces_is_the_pages_of_a_whole_one`
compiles a deck with sections, an outline, a fragile frame and a babel language, edits a frame's
body, then inserts a frame and a section before the others, in 3 pieces and whole: same pixels on
every page, same outline, a SyncTeX page per page, no refusal. Each of three deliberate breaks of the mechanism
fails it. `tests/test_pieces.py` covers cuts, merge,
refusals and `carried` without TeX.

| converge-like edit, 4 cores, both arms pre-warmed (wall)  | whole | pieces |
|---|---|---|
| water x8 (50 pages), a frame's body edited            | 3.9 s  | 1.5 s |
| hashing x8 (50 pages)                                 | 2.1-2.2 s | 0.9 s |
| talk x8 (42 pages)                                    | 1.4 s  | 0.7 s |
| a 6-9 page test deck                                  | 0.4 s  | 0.4 s |
| tikz_diagrams (41 pages, mostly pictures)             | 2.7-2.9 s | 2.4-2.7 s |
| water x8, a frame and a section inserted              | 7.9 s  | 5.3 s |
| talk x8, a frame and a section inserted               | 2.8 s  | 3.0 s |
| first compile (no .aux to cut by)                     | equal  | equal |

Pixels, outline and page labels were identical whole vs pieces on all seven decks, for body edits
and for inserts. A frame inserted costs three piece passes where a whole compile needs two: the
pieces after it began at stale numbers, and only a third pass can confirm the second. So **two
engines wait per piece** between compiles: the second set takes pass 2. Pass 3 is cold. Starting
engines while pieces run was tried and lost: on 4 cores they took the pieces' cores, and pass 2 went
from 0.7 to 1.4 s on talk. A pass's spare engines are kept, never stopped, and each engine has a
folder of its own (`warm-N`). Before this, an insert cost talk 4.1 s and water 6.3 s.

**On a sandbox VM.** Each waiting engine is a lualatex holding its fonts and the preamble's state: 210 MB resident
measured on a metropolis talk. N pieces want N cores and 2N engines' memory (8 engines, ~1.7 GB, on
4 cores); with fewer cores than 2, `piece_count` gives 1 and nothing
changes. Set `B2S_COMPILE_PIECES=1` where memory is tight. The warm-up advice above holds for each
engine: a font cache built as the user who compiles, fonts copied with their mtime.

**What it cannot see.** Anything that is not a count register carried across a skipped frame: a
`\def`'d total, a token list or dimension a frame changes for the frames after it, a package
writing its own .aux lines (refused by `KNOWN`, then whole). `\pageref` and links to a label in
another piece read the merged .aux, so they are right once settled; a link's target inside the
joined PDF is right when its name is. Overlay pages of one frame never split. pdflatex and xelatex
always compile whole.

## Done: a first compile on every core (2026-10-28)

A compile in pieces needs the .aux of a compile before it, so a source's first compile (adopt's
first round, adopt's base, every `convert_source` of a sync, `ir_from_tex`) was whole: two passes
on one core. Now its first pass is whole, it writes the .aux with the checkpoints, and **the
passes after it go in pieces** (`Workspace.compile_whole(cut=...)`). The engines for those pieces
start while pass 1 runs, on the cores it leaves, when the source labels enough frames to cut at
(`labelled_frames`, a guess from the text; `pieces.cuts` decides). Without `warm_up` they are
stopped once the compile is done. Three changes made it pay:

- **The settle test read a line no pass reads.** The pieces' first pass did not settle, so a
  second ran cold (water x8: 8.5 s, the same as whole). The line was `\btwos@base` (the registers
  where the document began), which counts what pass 1's empty .aux gave (two registers 0 -> 85 on
  talk). TeX defines the command to do nothing, and every pass counts the start afresh, so
  `piece_state` leaves the line out (`PIECE_BASE`). The test fails with it put back (`[3, 3]` piece passes).
- **Waiting engines stand back** (`WARM_NICE` = 10, `behind`): four engines reading the preamble
  beside pass 1 on four cores slowed it from 4.2 to 5.0 s; at nice 10 they took 0.5 s less
  (A/B twice). An engine keeps that priority for the pass it then compiles, because a process may
  not raise its own priority. Nothing else then runs on the machine during a compile.
- `adopt_sync.convert_source_of` and `ir_from_tex` set `pieces` (one-shot: no engines kept after).

| first compile, fresh folder (wall, 4 rounds interleaved, 4 cores) | whole | later passes in pieces |
|---|---|---|
| water x8 (48 slides)                    | 8.2-9.1 s | 6.0-6.6 s |
| hashing x8 (48)                         | 5.8-6.0 s | 3.9-4.2 s |
| talk x8 (40)                            | 4.4-4.7 s | 3.0-3.2 s |
| tikz_diagrams (39 pages)                | 6.3-6.5 s | 4.0-4.2 s |
| metropolis talk (9)                     | 3.3-3.5 s | 2.6-3.1 s |
| hashing (6 slides)                      | 3.1-3.2 s | 2.0-2.1 s |

The adopted decks were bootstrapped again with today's `slides.sty`. Pixels and the outline were
the same on every deck. Handout mode (a labelled notes deck x3, water x8; first compile, a body
edit, a frame and a section inserted) gave the same pixels in pieces as whole.

**Notes shown are not cut.** A `\note` written after a frame, outside it, needs that frame's
`\beamer@shortframetitle`. A piece starting right after that frame never defined it, so the piece
fails. The compile goes on whole, with the same pixels (checked), and `pieces` is 0 for that workspace
from then on: one wasted piece pass. Carrying macros across a skipped frame (not only count
registers) would fix it. Not done.

What is left of a first compile is pass 1 itself, one core (water x8: 4.2 s of 6.0). Cutting it too
would mean pieces with no checkpoints, each starting from guessed numbers (a piece's sections are
right, its pages are not), settled by `carried` over more piece passes. That is about 3 passes
of ~1.2 s against one of 4.2, if `carried` learns to start from nothing. Not done.

## Done: only the frames that changed (2026-10-28)

A converge round mostly edits words inside a few frames. A compile in pieces still typesets every
frame. Now **a compile typesets again only the frames whose text changed**, and keeps the
others' pages from the last compile's PDF (`Workspace.compile_patched`, `pieces.patches`).

**Which pages are whose.** At each labelled frame's end, a piece and a whole compile also write
the pages shipped so far, `\btwos@shipped{label}{n}` (`merged` counts them document-wide,
`pieces.shifted`). So the last compile's .aux says which pages each frame left.

**Which frames changed.** `patches` cuts the main file at each frame's `label=`: a frame is the
text from its label to its `\end{frame}`, and what follows, up to the next label, goes with the
next frame (it is typeset after the checkpoint). Changed frames are grouped into runs of
neighbours, at most one run per piece, each no longer than a piece of a compile in pieces. Each
run is a piece on its own waiting engine, starting from the checkpoint before it, as in a compile
in pieces. Its pages replace the run's old pages (`pdf.join_file_pages`, `pure.join.join_pages`).

**When it is right.** It is right when every run wrote the .aux lines the last compile wrote for
those frames: then every frame began and ended as before, and read what it read before
(`patch_checked`; it also checks the bookmarks, the page count, the files written). Otherwise
nothing of it is kept (`patch_refused` says why) and the compile goes on in pieces.

**Refused before it costs anything.** A refused patch used up the waiting engines that the
compile in pieces then needed: about 1.3 s lost per section edit. So `patches` refuses without
compiling when the edit adds, removes or changes what writes the document's own lines or adds a
frame (`pieces.STRUCTURE`: `\section` and its kin, `\label`, `\cite`, `\pause`, `\frame`, ...), or
the preamble changed, or the labels are not one per frame in order. With that, a section or frame
insert costs the same with patching on as off (A/B: 34.8 vs 34.7 s, 25.0 vs 25.1 s over three
edits each).

| water x8 (50 pages), 4 cores, warm engines (wall)  | whole | pieces | patched |
|---|---|---|---|
| a few words changed in one frame                   | ~3.1 s | ~1.2 s | 0.41-0.46 s |

The patched PDF is pixel for pixel the whole compile's on every edit tried.

**A frame's colours leaked into the next.** Patching first showed differing pixels: a frame
typeset alone started with black words where the whole compile had DarkBlue. slides.sty's table
cell (`\slides@t@setcell`) set its words in an ungrouped `\setbox...\vbox{}`. A `\color` pushes a
colour and pops it after the enclosing group, which there was outside the box. So a glossary
table's DarkBlue stayed on the colour stack and coloured every later frame's plain words. This was
a real bug in adopted decks, older than pieces. The cell now groups its colour
(`\color@begingroup`...`\color@endgroup`). `tests/test_adopt_compiles.py` puts a frame of bare
words after every frame of each showcase deck: all must have the first one's colour. With the fix
reverted, several decks fail.

**The outline after a patch.** A piece puts the anchors of the frames it skipped on its first
page. When the join keeps a piece's pages and the last compile's, a name could be on both, and the
outline then went to the piece's first page. The join now gives a name to the earliest page that
holds it.

**A register set to a page.** When a frame comes in, `carried` moves each later piece's
checkpoints by how far its start moved. Beamer's `\beamer@sectionstartpage` (and the part's and
subsection's) is set to the page a section starts on: it moves with the pages, not by what it had
moved where the piece began. Moving it by the wrong amount meant some edits did not settle, and a
cold whole pass followed (water x8, a frame inserted: up to 8.5 s instead of 4.4 s). Now a
register set at a checkpoint to a page of the frame just typeset is moved with the pages, unless
it was already there at the checkpoint before (then it counts along with the pages, like
a frame number). Tests: `tests/test_pieces.py`.
Scored against the settled checkpoints of 10 page and 3 section edits on water x8, this rule
and the first one we tried (shift every register set at a checkpoint to a moved page) both guess
every register exactly. We kept the one tested for registers that were not moving at the cut.

**Bookmarks apart in a pass that has not settled.** Each piece writes its section bookmarks under
anchors `Outline0.N`, which follow the section counter. In a pass whose checkpoints are still
stale (a section just came in), the pieces' bookmarks do not follow on, and the join refused:
"the pieces' bookmarks do not follow on". That refusal also switched pieces off for the rest of
the session (`split_refused`), so every later compile was whole. Now such a pass merges its .aux
anyway and joins no PDF (`Workspace.marks_apart`), so the next pass runs from settled
checkpoints. Only a pass that has settled and still disagrees is refused. Water x8, six sections
inserted one after another: the third went from 10.5 s to 5.7 s, and the ones after it from 8-10 s
(whole) to 2.9-5.9 s (pieces). Every edit gave the whole compile's pixels: 6 section, 5 frame
and 4 word edits, each compared with a whole compile of the same source
(scratch `patch_each.py`). The refusal of a structure edit now names what came in
(`\section`), not the first line that differs.
`test_a_compile_in_pieces_is_the_pages_of_a_whole_one` inserts a section before a frame,
which gives this pass. With the fix reverted, the test fails.

**Tried, saved nothing: settling each piece apart.** A frame insert still takes three piece
passes (1.1 + 1.1 + 2.2 s, the third cold). Reading each piece's .aux only up to where it starts
should let the pieces before an edit settle sooner. It did not save a pass: the .nav's page
numbers change everywhere after the edit too. Reverted.

**What it cannot see.** An edit outside the main file (an `\input` file, a picture) is not
patched: the last compile's other files must be the same (`PatchFrom.others`). Anything a frame
changes that is not written to the .aux, a `\gdef` read by a later frame for example, would pass
the check and be wrong.

## Done: an engine for the third pass (2026-10-28)

A frame or a section coming in takes three passes in pieces. The first starts from stale
checkpoints. The second reads the .nav and .toc the first wrote: the first wrote them with stale
section and slide numbers (`\slideentry {0}{0}{37}` where the whole says `{1}{0}{8}`), and a
whole compile would need that pass too. The third only finds that nothing moved. Engines are
warmed between compiles only (one started while pieces run takes a core from them), and two
waited per piece: the third pass ran cold.

Now three wait per piece (`WARM_PASSES`), as many as fit in half the memory available
(`warm_room`, `WARM_ENGINE_BYTES`: an engine waiting on water's preamble holds 140-155 MB). The
2 GiB playground keeps about as many as before.

| water x8, 4 cores, a frame inserted (A/B interleaved, 8 edits) | 2 a piece | 3 a piece |
|---|---|---|
| edits taking three passes                       | 7.3-8.0 s | 5.5-6.8 s |
| edits taking two                                 | 4.0-4.6 s | 4.2-4.7 s |
| the third pass alone                             | 3.4-3.6 s | 1.7-1.9 s |

The machine was slower that day than for the tables above (a warm pass 1.8-2.2 s, not 1.3 s):
compare within a table, not across tables. Cost: four more idle engines (~600 MB), and four more
preamble loads at `WARM_NICE` after an insert.

**Not taken: skipping the third pass.** The second pass read .nav lines the first wrote stale.
Whether its pages depend on them depends on the theme, and only the third pass can tell.

## Done: adopt's base compile starts from the loop's .aux (2026-10-29)

`adopt` compiles the source twice: the pull loop does (`work/loop`), and then the sync base does
again (`work/sync-base/compile`, `adopt_sync.convert_source_of`), cold, from an empty build
folder. A cold first compile runs its first pass whole, because there is no .aux to cut by.
Now the base compile starts from the loop's .aux (`Workspace.seed`), so even its first pass runs
in pieces. A wrong .aux only makes it slower, never wrong: a pass in pieces is kept once it
settles, and the pages came out the same to the pixel on every deck below.

| first compile, 4 cores (A/B interleaved) | cold | seeded |
|---|---|---|
| water x8 (48 pages), no notes      | 8.2-8.9 s | 3.2-3.9 s |
| showcase hashing, no notes (whole `convert_source_of`) | 3.7-3.8 s | 2.6-2.9 s |
| showcase talk, notes shown in the loop | 3.1-3.7 s | **10.5-11.8 s** |

**The seed is taken only when the loop compiled without notes shown**
(`inverse.shows_notes`: the deck has speaker notes or the source writes `\note`). With notes
shown, every page number in the .aux is off. The passes in pieces never settle, then the
compile goes whole with no cut. That is three times slower than cold (last row).

**What it cannot see.** Whether the seed is close. The caller decides by options. A source the
loop rewrote heavily before stopping still settles, but in more passes.

**Not taken: remembering which glyphs are hidden.** `spans` reads every page twice: the glyphs
shown, then the hidden ones. Each reading asks `Visibility.hidden` of every glyph. A cache per
glyph took read_with_notes on water x8 from 0.578 to 0.565 s of CPU (min of 8, A/B interleaved,
raw identical). That is 2%, inside the noise, and not worth the code.
