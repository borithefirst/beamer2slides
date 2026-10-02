# Speaker notes

A frame's `\note{...}` becomes the speaker notes of its slide in Slides, as one plain string
(`deck.json` slide `notes`, emit's single `insertText` into the notes shape). Code: `notes.py`;
measured by `tools/notes_score.py`; pinned in `tests/test_speaker_notes.py`.

## Where the notes come from

1. **The PDF's own note pages** (`notes.prepare`). Beamer writes them when the deck is compiled with
   `\setbeameroption{show notes}` (one note page after each slide that has a note) or
   `show notes on second screen=right` (pages twice as wide, the note on the right half).
   - Note page templates recognised:
     - `default`: a grey header band, about a quarter of the page, with a quarter-scale frame
       thumbnail.
     - `compressed`: a band of an eighth of the page, with a white canvas at 0.125 scale.
     - `plain`: nothing drawn. The page is known by its words alone. A note page carries the
       absolute page number as its label, so it is a "bare" page (no drawings, no images) whose
       label is not the running frame count (`plain_notes`). This needs at least two such pages
       that are not next to each other.
   - On a second screen, the left halves are the slides (written as `slides.pdf`). Their labels
     are rebuilt as a frame counter, because pgfpages' labels are useless there.
   - Every note page goes to the slide page in front of it. Steps of one frame share their notes:
     a `\note<2>` that shows on step 2 only is joined to the note of the step overlays keep
     (`carried`, `union`; steps are matched as `select_overlays` matches them).

2. **The source, when the PDF has none and the person names it**:
   `convert deck.pdf --tex main.tex` (also on `classify`).
   - The source is compiled once more with `\PassOptionsToClass{notes=show}{beamer}`, through
     `inverse.Workspace` (a copy of its folder in `<out>/notes-source`). That is the same compile
     the pull loop uses.
   - The note pages are read as in (1).
   - Each page of the given PDF is paired to its page in that compile (`pair_pages`):
     - equal page counts pair in order;
     - otherwise frame by frame (a run of one label), the given PDF's steps being the compile's
       last ones. That is a handout's one page per frame.
     - Every pair must also share at least `PAIRED_WORDS` (half) of its words.
   - Outcomes (`SourceNotes.outcome`):
     - `found`
     - `no-engine`: no TeX here. The deck converts without notes, saying so.
     - `no-notes`: the source has no `\note`, or hides them.
     - `failed`: a compile error. Refused, with the log.
     - `mismatch`: the PDF is not this source's. Refused, naming both page counts and frame ranges.
   - When the PDF has note pages, `--tex` is ignored, with a line saying so.

### Why `--tex`, and never a .tex found beside the PDF

Compiling runs the person's TeX: their packages, `\write18` if enabled, minutes of time. A `.tex`
of the same name beside the PDF may be an older draft, or another deck's. So convert never
compiles a source nobody named. When it sees one (`source_beside`: same stem, an uncommented
`\note`), it only prints a hint: `--tex X.tex brings them`.

The PDF stays the input; the compile is only read for its notes. That keeps the rule that
geometry, fonts and colours come from the PDF the person gave. The pairing check is what makes
borrowing notes from another compile safe.

## How a note page is read (`read_note`)

- Glyphs below the header (`_note_header`), horizontal, inside the note's area. Glyphs that run
  below the page bottom are kept: an overflowing note is never cut.
- Lines: a new line when the pen drops at least `NEW_LINE_DROP` em or moves back at least
  `NEW_LINE_BACK` em.
- Words: a gap of at least `extract.JOIN_GAP` em is a space, measured in ems of the larger of the
  two glyphs (a script's own size would split `y²`). So words of a style change join right, and
  ligatures come back from `readable`.
- Text, by font family:
  - OT1 accents compose (`compose_accents`).
  - Math fonts go through `classify_text.math_text`.
  - Scripts become Unicode super- and subscripts where every character has one, else `^x`,
    `^(..)`, `_x`, `_(..)`. Raised rings and asterisks stay as they are.
  - U+0002 (PDFium's line-end hyphen) becomes `-`, and is joined to a lowercase continuation.
- Links: a `\href` keeps its words with ` (uri)` after them, unless the words are the address.
  On a second screen, pgfpages leaves the annotations at left-half coordinates, so they are
  shifted (`web_links`).
- Paragraphs (`paragraphs`): a line starts one when it is a list item, when it steps out of a
  list, when the line above had room for its first word (beamer's notes are justified, with no
  `\parindent`), or when the vertical gap is over 1.5 line pitches.
  - List items are written `• words`, or `1. words`, with their nesting indented two spaces.
  - Paragraphs are separated by a newline.
  - Several `\note{}` on one frame run on with no separator in the PDF, so `step.Only` gets its
    space back (`RUN_ON`).

## Scoreboard

`tools/notes_score.py <pdf> [--tex main.tex] [--save F] [--against F]` prints, frame by frame,
the note that the slide kept after overlays. On `tests/decks/30_speaker_notes.tex`, 14 frames,
the score is frames whose note equals the text the source says:

| build | before | after |
|---|---|---|
| notes (pdflatex) | 5/14 | 14/14 |
| notes-xelatex | 5/14 | 14/14 |
| notes-right (second screen) | 5/14 (15 frames: the overlay split) | 14/14 |
| notes-plain | 1/14 (31 frames: no note found) | 14/14 |
| notes-compressed | 1/14 (31 frames: no note found) | 14/14 |
| plain build | 1/14 (no notes) | 14/14 with `--tex` |

Before the change, notes were lost in these ways:
- ligatures were dropped;
- paragraphs were joined into one;
- `\note[item]` items ran on one line;
- math was broken, and pdflatex accents were not composed;
- an `\href`'s address was lost;
- a long note was cut at the page bottom;
- a `\note<2>` was lost to the overlay that was kept.

## Known limits

- An explicit hyphen at a line end, followed by a lowercase word, is joined as if TeX had
  hyphenated there.
- A deck with one plain-template note page only is not recognised (one bare page proves nothing).
  A plain-template second screen is not recognised at all.
- `inverse.original_pages` still finds note pages by their header, so it misses plain-template
  ones. `Prepared.kept` lists the kept pages, and inverse could read that instead.
- **Sync** has no `--tex`. A PDF without note pages says nothing about notes, so
  `sync.build_ours` gives each paired slide its base's notes (it once read that as the source
  deleting them, and cleared every note `convert --tex` brought that nobody edited). Changed
  notes reach a synced deck only from a PDF compiled with notes shown. A `--tex` on sync would
  call `notes_of`'s source step from `build_ours` the same way.

## Rich notes (not done)

The notes are one plain string end to end: raw page `notes`, the IR's slide `notes`, the base and
merge's notes field, `deck_ir` reading them back, and emit's one `insertText`. Bold, italic, links
and real bullets would take the following:
- `read_line` keeping runs: it already splits at font and script changes; their style would be
  kept rather than flattened.
- The IR's notes becoming paragraphs of runs (ir.py, ir_types.py).
- Emit styling them with `updateTextStyle` and `createParagraphBullets` on the notes shape's
  object id.
- Sync merging them paragraph by paragraph, as it does text boxes (merge, snapshot, deck_ir).
- Pull and adopt writing `\textbf`, `\href` and `itemize` back into `\note{}`.

## Agent tools

`deck_convert` and `deck_prepare` are unchanged. To bring notes from a source they would need:
- a `tex: File | None` parameter, as `deck_pull` takes its source: a workspace ref of the main
  .tex, its folder holding what it inputs;
- a TeX engine in the harness. Without one the outcome is `no-engine`: the deck converts, and a
  `data["notes_source"]` would say why there are no notes.

`notes.notes_from_source(tex, pdf, work)` is the call. It needs only a working folder.
