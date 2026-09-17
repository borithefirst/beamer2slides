# Sync: merging a changed beamer source into an edited Slides deck

Context: an AI writes the talk as beamer, `convert` turns it into a Slides deck, people edit that
deck, the AI (or a LaTeX user) edits the beamer again. `sync` brings the source changes into the
live deck without destroying the deck edits; `pull` brings deck edits back towards the source.

## Model: a three-way merge
- **base**: what the converter produced last time, recorded right after it was written
  (the deck's IR plus Google's read-back of each object it created).
- **ours**: the new conversion of the changed PDF (IR only; nothing is emitted to compare).
- **theirs**: the live deck now (`presentations.get`).

Source changes = base IR vs ours IR. Deck edits = base read-back vs live read-back.
**The base is always converter output, never the merged deck**: a deck edit keeps differing from
the base, so every later sync keeps it (a *deck override*) until the source says the same thing
(then ours == theirs for that field: *converged*, the override disappears from the reports).

Policy: **deck edits win**. A source change to a field the deck also edited is not applied; it is
reported as a conflict (with both versions) so the author can decide.

## Identity
- **Slide key**: the beamer frame label (`\begin{frame}[label=results]` → PDF named destination
  `results` on that page; `results<2>` for overlay steps is ignored), else `title:<normalised
  title>#<occurrence>`, else `page:<n>`. Unlabelled slides of base and ours are matched by a
  sequence alignment of (title, text fingerprint) so an inserted frame doesn't shift every key.
  AI authors are told to label every frame (themes/google README, docs).
- **Element key** within a slide: `kind/role/ordinal` (e.g. `text/title/0`, `text/body/2`,
  `image/figure/0`, `image/math/1`), plus a **fingerprint**: plain text, PDF bbox, image sha1 of its
  crop, anchor element key. Ours elements inherit the key of the base element they match
  (exact key with similar fingerprint first, then best fingerprint similarity: text ratio, kind,
  bbox overlap, image hash); unmatched ours elements get fresh ordinals.
- **Deck objects**: the base records, per element, every object id created for it (main object,
  group, number box, hole pictures, template copies). Objects created by sync get fresh unique ids
  (`b2s_<slidehash>_<elementhash>_<n>`). The element key is also written into the object's
  alt-text *title* (`b2s:<slide key>/<element key>`), so copies made in the Slides UI (new id, same
  tag) are recognised as user-added copies, not as the original.

## Base snapshot (`sync/base.json`, schema version 1)
```
{version: 1, presentationId, revisionId, source: {pdf, sha1}, scale, page_size,
 slides: [{key, label, title, page, objectId, layout, background_sha1, notes,
           elements: [{key, kind, role, ir_hash, fingerprint: {text, bbox, image_sha1, anchor},
                       objects: [objectId...], main: objectId,
                       readback: {objectId: {transform, size, text, text_style_hash, shape_style_hash,
                                             image: {sourceUrl?, contentHash?}, parent_group, z}}}]}]}
```
`ir_hash` hashes the element's IR without ids and page-specific numbering. Read-back values are
normalised (EMU→pt rounded to 0.01, colours to hex) so an untouched object compares equal.
Storage: locally in `<out>/sync/base.json`, and in Drive as a JSON file created by the app
(`drive.file` scope) whose id is kept in the presentation file's `appProperties.b2sBase`; Drive is
authoritative (anyone with the deck can sync), the local copy is a cache and offline fallback.

## Deck edits detected (per object, per field)
geometry (transform + size), text content, text style, shape fill/outline, image replaced, deleted,
regrouped/ungrouped, z-order; user-added objects (no base object, or a copied tag); slides added,
deleted, duplicated, reordered; speaker notes; slide background.

## Merge rules (per element and field)
| source (base→ours) | deck (base→theirs) | action |
|---|---|---|
| unchanged | anything | keep the deck |
| changed | unchanged | apply: recreate the element's objects from ours (same place in z-order and grouping) |
| changed field A | changed field B only | recreate from ours, then re-apply the deck's field B (e.g. keep the user's position, take the new text) |
| text changed | text changed | word-level diff3; clean → apply the merged text (ours styling, deck geometry); overlapping → conflict |
| changed | deleted | keep deleted, report |
| deleted | unchanged | delete its objects |
| deleted | edited | keep, report |
| added | — | create |
Slides: new frames are created at the aligned position; frames removed from the source are deleted
if the deck didn't touch them (no edits, no user objects), else kept and reported; reordering in
the source is applied only if the deck didn't reorder those slides; user-added slides stay after
their predecessor. Notes and backgrounds follow the same field rules.
Recreated elements with holes or overlays are re-measured (`measure_places`) on the live deck.

## Writing to the live deck
- Every batchUpdate carries `writeControl.requiredRevisionId` from the read it was planned on; on
  a mismatch sync re-reads and re-plans (at most 3 times), so concurrent edits are never overwritten.
- Pictures (new or changed figures, formula pictures, backgrounds) can't come from URLs: they go
  into a **staging deck** imported from a .pptx (the existing `build_pptx` route), whose images'
  `contentUrl`s are then used by `createImage` / `replaceImage` / `stretchedPictureFill` in the live
  deck (verified: works across private decks, no public links); the staging file is deleted after.
- Template shapes (native shadows, exact corner radii) can't be copied across decks: an existing
  object with the same template key in the live deck is duplicated if there is one, else a plain
  shape is created and the report says so.
- `--dry-run` plans and reports without writing. A second sync with no changes writes nothing.
- After writing, the new base = ours IR + read-back of objects as the converter created them
  (captured before deck overrides are re-applied); elements kept from the deck carry their old base.

## Reports
`sync-report.json` / `.md`: source changes applied, deck edits kept (overrides), conflicts (field,
base, ours, theirs), converged overrides, slides created/deleted/moved, warnings (template shapes,
unmeasured pictures).

## Pull: deck edits back to the source
```
beamer2slides pull --deck <url|id|out folder> --tex main.tex [--apply | --out DIR] [--max-iter N] [--work DIR]
beamer2slides converge --target deck.json --tex main.tex [same options]      # offline twin
```
Pull is an inverse problem solved by a loop, not a replay of recorded edits: it edits the source
until the source's own conversion matches the deck. It only reads the deck (`presentations.get`
and picture downloads), so the deck revision never changes; it needs no base snapshot.

1. **Target** (`deck_ir.py`): the live deck as deck.json-shaped IR. Text boxes give paragraphs,
   runs (bold, italic, colour, links, scripts; formula holes from Roboto Mono no-break spaces) and
   bullets; sizes go back to PDF pt through FontMapper, positions through emit's text box model
   (anchor = text start by alignment and first baseline, / scale), so a converted deck read back
   equals classify's IR (tests/slides_sim.py replays emit's requests to check it offline; on a live
   deck the unedited talk reads back with 0 residuals). Numbered balls (picture + number box) and
   diagram groups (node shapes joined by lines) fold back into one element; pictures are
   downloaded and hashed; notes, background colours and tables are read too. Slide keys come from
   `b2s:<slide key>/<element key>` alt-text titles or a base's object map when present.
2. **Loop** (`inverse.converge`): a working copy of the source tree (`<work>/loop/src`) is compiled
   with the document's engine (`% !TEX program`, fontspec → lualatex; `-synctex=1`, reruns on
   "Rerun"), then prepared, extracted and classified like `convert` (notes pages, last overlay step,
   panels verified like render). SyncTeX maps pages to frames (`texmap.page_frames`; inside a frame
   SyncTeX only names `\end{frame}`, so words are then located by printed text: `texmap.build_visible`
   maps every printed character to its source span, math is opaque, `\input`/`\include` followed).
3. **Compare** (`compare.py`): slides by key, then by title/text alignment (reorders detected);
   paragraphs across the slide; residuals with tolerances (`TOL`: 2 pt positions, 3 pt sizes, 6%
   font size, colour distance 24): slide missing/extra/order, notes, background, paragraph
   missing/extra/order, text (word diff), style ranges, bullet kind and relative level, alignment,
   element missing/extra, geometry (text anchors; picture, shape and table boxes), picture hash,
   shape fill, table cells, diagram labels. Whole frame-title size/colour and title positions are
   `theme` differences: reported, never written.
4. **Translate** (`inverse.Planner`), keeping the diff minimal and semantic: slide edits first and
   alone (new frame after its predecessor with title, lists, notes and `[label=key]`; frame deleted;
   frame block moved), then words (replace/insert/delete in plain source spans, word by word across
   commands), styles (`\textbf`, `\emph`, `\underline`, `\texttt`, `\textcolor` + `\definecolor`,
   size switches; wrap, unwrap or split the enclosing group), lists rebuilt from their items' own
   source (items added, deleted, reordered, relevelled), plain paragraphs inserted or deleted,
   `\note{}`, `\setbeamercolor{background canvas}`, new text boxes and pictures as `textblock*`
   (textpos, absolute page pt; picture files copied to `figures/b2s-<sha>`), and geometry last,
   top first, deferred on slides whose content changes that round: textblocks shift by the error,
   flow text moves vertically with `\par\vspace` (beamer centres frames: the gain is learned by
   secant updates), otherwise it is cut into a textblock at the target with a spacer keeping its
   flow room; pictures change width first.
5. **Stop**: within tolerance, `--max-iter` (10), or no edit left. Edits of words, styles and notes,
   new boxes and new slides are written once, added paragraphs twice, other residuals until they stop improving; repeated states stop the loop;
   a round that breaks the build is replayed one edit at a time and the breaking edits dropped.

Output in `--work` (default `<out folder>/pull`): `target.json`, `pull.patch`, `edits.json` and
`edits.md` (iterations with open residuals by kind and geometry error, the patch, theme
differences, and every unresolved residual with its reason, `file:line` range of its frame and the
target values, for an AI author to finish). `--apply` writes the changed files in place (`.bak`
backups) and copies new picture files; `--out DIR` writes the edited source tree there instead;
neither leaves the source untouched. After the rebuilt PDF is synced, the pulled fields are
converged overrides.

Results: offline test bed (`python -m pytest -m inverse`, ~2 min) converges on 4 source pairs in one
round each and on 10 synthetic edits of 01_basic in 1-3 rounds; live, the sync talk edited in Slides
(reword, bold, 30 pt move, new red text box, new slide with bullets) converged in 3 rounds (~40 s)
and the deck converted from the pulled source compares to the edited deck with 0 open residuals
(text anchors within 2.1 pt), only the new slide's layout title differing (theme).

Not translated yet (reported instead): tables, diagram labels, shape colours, paragraph alignment,
frame title position and theme styles, edits inside math, rotated text, overlays beyond the last
step (compile with `--handout` to pull a handout-style deck).
