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

The first lualatex run on a machine with an empty font cache (`/var/lib/texmf/luatex-cache` here)
takes **10.5 s instead of 1.4 s**: luaotfload builds its name database and caches each font it
parses. Every fresh container pays it - CI, and each new Cloud Run instance of the playground.

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
  0.015-0.063 s a deck. Real Google Fonts through fontspec, and decks with many faces, were not
  measured.
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
it.

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
