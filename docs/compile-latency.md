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
