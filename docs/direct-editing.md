# Editing a deck as beamer, directly

*An exploration, not a built thing. Everything under "Measured" was run on this machine;
everything under "Proposed" is a design that has not been written yet.*

## The question

The Docs side has an arrangement that works: a canonical HTML file is what the source says,
a Google Doc is what the reader says, `doc_sync` merges them, and the file a model edits is
**one file it can read**. The Slides side has the same two ends - a `.tex` in git, a deck in
Drive, `sync` merging them - and a middle that a harness with no durable filesystem cannot
hold: a work folder with a source tree, a build directory, `out/` and a base.

So: can a caller read a deck **as beamer**, edit the text, add a chart, and put it back,
without ever holding a directory?

The answer splits cleanly in two, and only one half is hard.

- **Reading a deck as beamer is free.** No LaTeX, no work folder, measured below.
- **Putting it back needs a compile**, and a compile needs a real directory with the whole
  tree in it. That cannot be wished away; it can be *moved*.

The rest of this file is what "moved" would look like.

## What a round trip needs today, and what of it is really state

`inverse.Workspace` (`inverse.py:289`) makes `work/src` (a copy of the tree), `work/build`
(the PDF, the log, the aux family and `synctex.gz`) and `work/classify`. `converge` loops
compile → extract → classify → residuals → translators until the source's conversion matches
the target. Three kinds of thing live in there and they are not equally precious:

| | what it is | must it survive between calls? |
|---|---|---|
| `work/src/**.tex` | **authored** - the artefact, the thing an edit edits | yes |
| new `figures/*` the loop writes | **authored** - bytes nothing else has | yes |
| `work/build/*` (aux, log, synctex), `work/classify/*` | derived | no - `aux_state` is an optimisation, `inverse.py:447` |
| `figures/` and `fonts/` from the bootstrap | derived - see below | no |
| `sync/base.json` | the agreement between source and deck | yes |

The in-memory form of the first row already exists: `texmap.Source.texts` is a
`dict[Path, str]` built by following `\input` (`texmap.py:172`). It is text only and never
owns a binary, which is exactly the right shape.

**Measured.** Bootstrapping five adopt-corpus decks from their cached targets
(`tools/probe_deck_view.py`, no network, no LaTeX):

| deck | slides | bootstrap | files | text | text gzipped | binaries | median frame |
|---|---|---|---|---|---|---|---|
| gdg24 | 99 | 1.7 s | 144 | 182 kB | **27 kB** | 51.9 MB | 749 B |
| creandum-board | 36 | 0.1 s | 23 | 79 kB | 17 kB | 6.6 MB | 899 B |
| intro-lecture | 34 | 0.7 s | 97 | 88 kB | 20 kB | 9.2 MB | 1169 B |
| hebrew-lesson | 24 | 0.1 s | 13 | 46 kB | 12 kB | 6.9 MB | 493 B |
| ds-lecture | 16 | 0.1 s | 13 | 38 kB | 10 kB | 6.2 MB | 682 B |

**The text of a deck is about 1% of its tree by weight** - 10 to 27 kB gzipped against 6 to
52 MB of pictures and fonts. That one number decides most of the design: what has to travel
is small, and what is heavy does not have to travel at all.

## The view: a deck as beamer costs no compile

`adopt.bootstrap(target, tex, flow)` (`adopt.py:3388`) writes a compilable beamer source with
one frame per slide, `slides.sty`, a recovered `beamertheme<Deck>.sty`, the figures and the
fonts - **purely from the IR, with no subprocess anywhere in it**. `cmd_adopt` follows it with
`run_pull`, which compiles; nothing forces that. `adopt_bench` and `tools/readme_demos.py`
already call `bootstrap` on its own.

So the view is one `deck_ir` read (Google) plus 0.1-1.7 s of writing. And it is readable:
a median adopted frame is **749 bytes** of the `slides.sty` vocabulary -

```latex
\begin{frame}[plain,label=g30d9ee10555-1-1047,layout=blank-2,background=OffWhite]
  \slidepicture{194.6,70,208.8,137}{figures/picture-f06edb2b.png}
  \sliderect[fill-magenta,rounded=3.62]{11.4,73.8,67.34,21.73}
  \slidetext[middle,center]{15.6,73.8,58.92,21.7}{small-googlesans-medium}{Right click and "replace image"}
  \slidetext{10.9,7.4,72.3,14.5}{tiny-mono}{DEVICE FRAME - TABLET}
\end{frame}
```

Two things in that snippet matter more than they look.

**The frame carries the deck's own identity.** `label=g30d9ee10555-1-1047` is a slug of the
slide's objectId, which `adopt` writes into every frame because it is the one identity `sync`
can follow (docs/labels.md). So a *per-frame* read/write API is keyed by exactly the thing the
merge is keyed by - the granularity the machinery was already built for, not a new one.
`texmap.Frame` (`texmap.py:152`) already carries `file`, `start`, `end`, `body`, `label` and
`title`, so label → span is a scan over `source.frames` and replacing one is a splice plus
`set_text`.

**The picture is named by its content.** `figures/picture-f06edb2b.png` takes its 8 hex from
the sha1 of the bytes the deck handed back (`adopt.py:768`, `deck_ir.py:626`), so the same deck
re-read gives byte-identical names. A picture is therefore a *reference into the deck*, not a
file that has to be carried.

## What can be left behind

- **Pictures**: content-addressed, above. Re-fetchable from the deck's own `contentUrl`s, which
  Google serves byte for byte up to ~2046 px. The deck id plus the name is enough to get it back.
- **Fonts**: already a user cache - `fontfetch.cache_dir()` is `%LOCALAPPDATA%\beamer2slides\fonts`
  (`fontfetch.py:43`) - and `adopt.font_files_latex` with `tree=None` (`adopt.py:420`) emits a
  `Path=` pointing straight at that cache instead of copying `.ttf` files into the tree. Today
  `bootstrap` hardcodes the tree form; making it a parameter is the whole change.
- **The aux family**: derived. `Workspace.compile` reruns until `aux_state` settles
  (`inverse.py:345`), which is latexmk's rule; a warm build directory saves a pass and proves
  nothing. A stateless caller pays one extra LaTeX pass per turn and is correct.

**What must travel, then, is the `.tex` texts, any binary the edit itself brings in, and the
base.** For a 99-slide deck that is 27 kB gzipped.

## Where that state lives between calls

This is the real design decision, and it has three answers.

**(a) In the base, in Drive.** The base is already a JSON file beside the deck with its id in
the deck's `appProperties.b2sBase` (`snapshot.py:669`), and `load_base` already treats Drive as
authoritative with the folder as a cache; `read_local(out=None, ...)` already short-circuits
(`snapshot.py:652`). Putting the source *inside* the base is not only convenient, it is a
**correctness** argument: a base and a source that can drift apart is what `snapshot.base_matches`
exists to refuse, and a base that carries the source it was built from cannot drift. The cost is
nothing: a Drive `files.update` **with media** costs ~1.8-2.0 s whether it carries 7 bytes or
147 kB (measured on the Docs side, docs/google-docs.md), and the source is 27 kB.

The blockers are three lines: `store_base` and `adopt_sync.store` write the local copy
unconditionally before Drive, and `snapshot.refresh_pictures` reopens the previous out folder
(a miss there costs an unnecessary picture rewrite, not correctness).

**(b) In the message.** The caller holds the bundle and hands it back. Zero server state, and
the agent layer already carries files both ways: `content.take_in` writes a `data:` URI or
`{"name","base64"}` into the workspace before the journey sees it, and `deliver="inline"` fills
each artifact with its own bytes under a per-call budget (`content.py:69`). 27 kB is fine for a
frame and heavy for a whole deck every turn, so this is the escape hatch, not the default.

**(c) A server-side session.** The playground's workbench already *is* this service, with a
plain HTTP API: `POST /api/ws` opens a folder, `GET`/`PUT /api/ws/<id>/file?path=` reads and
writes one file (with the version stamp and the three-way merge that stops a stale buffer
reverting a run's rewrite), `POST /api/ws/<id>/runs` runs any of the eleven journeys plus
`tex_compile`, `GET .../runs/<rid>?since=` follows it (`playground/server.py:385`). It has TeX
Live in the image. What it is not is durable: sessions are pruned to the last 12, and on Cloud
Run a run moves only while somebody is polling (measured: 2.7 s polled four times a second,
68 s left alone).

**Recommendation: (a) as the carrier, (c) as the compiler, (b) as the escape hatch.**

One rule constrains (a) and must survive: `adopt.record_base` deliberately keeps its base in the
folder rather than Drive, because *"writing to a deck we do not own must never happen because a
command was run"* (`adopt.py:3496`); `--base-in-drive` is how one asks. A source-carrying base is
the same write and needs the same consent.

## The compile, which is the one thing that cannot be wished away

`tex → pdf → IR` is where the Slides side differs from the Docs side in kind. `doc_ir` parses
HTML into the IR directly - cheap, pure, no external tool. The Slides IR comes from **classifying
a rendered PDF**, so the mapping from source to IR runs through a TeX engine and PDFium, and
`inverse.converge` needs several rounds of it because the mapping is not exact.

The seam is narrow: one method, 24 lines (`inverse.compile`, `inverse.py:332`), with call sites
in `converge`, `Workspace.build`, `adopt_sync.convert_source` and `ir_from_tex`. Its signature
is too narrow as written - a remote compiler has to hand back the whole output directory, since
`build` reads `synctex.gz` out of `build_dir` separately (`inverse.py:391`), not out of
`compile`'s return value. So the honest shape of a compile service is

```
{files: {path: bytes}, engine, options} → {pdf, log, synctex.gz, aux}
```

and the multi-pass loop can live on either side.

**A compile-free fast path, offered with its risk named.** Some edits do not need re-measuring:
a word changed inside a `\slidetext` whose element has no holes, no overlays and no anchored
pictures changes the IR in exactly one place, and Slides reflows a text box itself. Such an edit
could be applied to the *base's* IR without compiling and written straight to the deck. It would
be an approximation - line breaking moves baselines, and `emit.vertical_layout` reads
`spaceAbove` off them - but a self-correcting one: the source is written too, so the next real
sync compiles, diffs against the base and fixes whatever the prediction got wrong. That is the
same bargain `formula_shifts` makes against `measure_places`. It is the difference between a
two-second edit and a twenty-second one, and it should not be built before the slow path works.

## Adding a chart

Three roads, and two of them already exist.

1. **A pgfplots or TikZ chart is text.** It travels in the frame body for free, costs a compile,
   and comes out as a figure picture the way every TikZ figure already does. For "add a chart"
   this is the best answer by a distance - it is also the only one where the *next* edit of the
   chart is an edit of data rather than of pixels.
2. **A picture file** already has a road: `content.take_in` accepts a `data:` URI or
   `{"name","base64"}`, writes it into the workspace and substitutes the ref, so a chart rendered
   elsewhere arrives as an argument. It must then be carried like any authored binary.
3. **A Slides-native chart** cannot be created - a linked chart reads back as its `contentUrl`
   picture (docs/adopt-bench.md) and no API request makes one.

## A shape for the journeys

Proposed, in the vocabulary of `agent/tools.py`:

- **`deck_source(deck, frame=None)`** — `needs=READS_GOOGLE`. The beamer view. With no `frame`,
  a table of contents (label, title, one line of what is on it) plus the preamble; with one, that
  frame's body. Never writes anywhere. Built on `adopt.bootstrap` for a foreign deck and on the
  carried source for a deck that has one. This is the piece that is unambiguously right, needs no
  policy decision and makes everything below testable.
- **`deck_edit(deck, frame, tex=...)`** — `needs=(READS, WRITES_GOOGLE)`. Replaces one frame's
  body in the carried source, compiles, and syncs. Keyed by the label, which is what the merge is
  keyed by.
- **`tex_compile`** already exists in the workbench as the twelfth entry and is the piece a
  no-TeX caller needs published.

Everything under them is `sync`, unchanged: the guard, the conflict report, `--take-source`, the
loss oracle. Nothing here is a second way to write to a deck.

## What would have to change in the library

Small, in this order:

1. `adopt.bootstrap` grows a `fonts="cache"|"tree"` choice (one parameter, `font_files_latex`
   already has the branch).
2. A `Bundle` = `dict[str, str]` of the text files plus a manifest of content-addressed binaries,
   with `pack`/`unpack` against a real directory. `Source.texts` is most of it already.
3. `store_base` / `adopt_sync.store` take `out=None`.
4. The base grows an optional `source` block, and `sync.build_ours` prefers it over a folder.
5. `inverse.compile` widens to hand back the build directory, so a remote compiler can stand in.
6. `converge`'s final diff reads the *original* tree (`inverse.py:2397`) and `Result.files` is
   keyed by absolute real-tree path - both need a bundle-shaped variant.

Steps 1-3 are independently useful. Nothing before step 5 needs a decision from anyone.

## What this must never do

- **Never rebuild a deck somebody has edited.** Direct editing is a *sync*, never a `convert`;
  the guard and the conflict report stand exactly as they are.
- **Never write to a deck because a command was run.** A source-carrying base is a write to the
  owner's Drive and needs the same consent `--base-in-drive` asks for today.
- **Never let the source and the base drift.** They are written together or not at all; that is
  the argument for carrying one inside the other rather than beside it.
- **Never guess at a frame.** An edit names a label; a label that names nothing is a refusal, not
  a best match.
