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

## Command
```
python -m beamer2slides sync deck.pdf --deck <url|id|out folder> [--out DIR] [--dry-run]
                                      [--overlays last|all] [--predict-places]
```
`--out` defaults to the folder given as `--deck` (else `out/<pdf stem>`). The new conversion goes to
`<out>/sync/ours/`, reports to `<out>/sync/sync-report.{json,md}` (`sync-report-dry-run.*` for a dry
run). `convert` records the first base itself (`snapshot.snapshot_after_convert`). Modules:
`identity.py` (keys, fingerprints, IR field hashes), `snapshot.py` (read-back, base.json, tags,
storage), `merge.py` (pure planning and diff3), `sync.py` (requests and the write loop).

## Identity
- **Slide key**: the beamer frame label (`\begin{frame}[label=results]` → PDF named destinations
  `results` and `results<n>` per overlay step; hyperref's own destinations have no `<n>` form,
  `extract.frame_labels`), else `title:<normalised title>#<occurrence>`, else `page:<n>`; a clash
  gets `~k`. Base and ours slides pair by label first; the rest by an order-keeping alignment of
  word similarity (+0.5 for the same title, at least 0.6; two different labels never pair), so an
  inserted or renamed frame doesn't shift keys. Unpaired ours slides get fresh keys.
  AI authors are told to label every frame (themes/google README, docs).
- **Element key** within a slide: `kind/role/ordinal` (e.g. `text/title/0`, `text/body/2`,
  `image/figure/0`, `image/math/1`), plus a **fingerprint**: plain text, PDF bbox, image sha1 of its
  crop, anchor element key. Ours elements inherit the key of the base element they match: most
  similar pairs first (0.6 text ratio + 0.3 geometry + 0.1 role; the key it would get anyway needs
  0.5 and counts 0.05 more, another key 0.35); unmatched elements get fresh ordinals. Titles and
  footers (one per slide) score at least 0.5 + 0.3 geometry + 0.2 text ratio with each other, so a
  renamed title keeps its key and goes back into its placeholder. Anchored
  elements (formula pictures, overlays, number balls) are matched after their anchors.
- **Units**: an element and the elements anchored to it are planned together (they share a group).
- **Deck objects**: the base records, per element, every object id created for it (main object
  first, then its `_g` group, number boxes, anchored pictures; `emit.element_objects` from the
  planned parts), plus the slide's other groups (blocks, rules). Objects created by sync get fresh
  unique ids (`b2s_<h6 slide key>_<h6 element key>_<generation+1><2 letters>`, new slides
  `b2s_<h6 slide key>_<tok>`). The element key is also written into the object's alt-text *title*
  (`b2s:<slide key>/<element key>`, groups untagged), so copies made in the Slides UI (new id, same
  tag) are recognised as user-added copies, not as the original.

## Base snapshot (`sync/base.json`, schema version 1)
```
{version: 1, generation, presentationId, revisionId, source: {pdf, sha1}, scale,
 page_size (PDF pt), deck_page_size, master_background (key), master_readback,
 slides: [{key, label, title, page, text, layout, background ("color:#rrggbb" | "png:<sha1>"), notes,
           objectId, layoutObjectId, background_readback, notes_readback, groups: [objectId...],
           order: [top-level objectIds, back to front],
           elements: [{key, id, kind, role, ir_hash, anchor (element key), ir (normalised IR),
                       fields: {text, position, size, style, image},
                       fingerprint: {text, bbox, image_sha1, anchor},
                       objects: [objectId...], main: objectId,
                       readback: {objectId: {kind, transform (absolute), size, box, parent_group, z,
                                             title, description, placeholder?, text, text_styles,
                                             paragraph_styles, text_style_hash, shape_style,
                                             shape_style_hash, image: {contentHash, sourceUrl, signature}?,
                                             children?, table?}}}]}]}
```
`ir_hash` hashes the element's IR without ids, spans and files, with `#page=N` links as slide keys
and floats to 0.01; `fields` hash its parts (position = top-left to 0.5 pt, size, the set of style
values, plain text, image), so a reworded line changes `text` and `size`, not `position`. Read-back
values are normalised (EMU→pt rounded to 0.01, scale to 1e-4, colours to hex or `theme:NAME`, group
children composed to absolute transforms, image hash = contentUrl path) so an untouched object
compares equal. Google issues new contentUrls for unchanged pictures now and then, so pictures and
picture backgrounds also carry a pixel `signature` (`<w>x<h>:` + 32 × 32 grey levels, downloaded when
the base is recorded and, at sync, only for live pictures whose URL hash differs): a picture counts
as replaced when its signature differs (ink-normalised difference ≥ 0.3 or another aspect ratio).
The base lists slides in the source's order, not the deck's, so a deck reorder stays a deck edit. Storage: locally in `<out>/sync/base.json`, and in Drive as a JSON file created by
the app (`drive.file` scope) whose id is kept in the presentation file's `appProperties.b2sBase`;
Drive is authoritative (anyone with the deck can sync), the local copy is a cache and fallback.
The base is replaced only when a sync wrote something or adopted converged deck fields (`generation` + 1).

## Deck edits detected (per object, per field)
geometry (box within 0.05 pt, scale within 1e-3), text content, text style, shape fill/outline,
image replaced, deleted (all or part of a unit), regrouped/ungrouped; user-added objects (no base
object, or a copied tag); slides added, deleted, reordered; speaker notes; slide background.
Z-order changes are not detected.

## Merge rules (per unit and field; `merge.plan_unit`)
| source (base→ours) | deck (base→theirs) | action |
|---|---|---|
| unchanged | anything | keep the deck (edited fields reported as overrides) |
| changed | unchanged | recreate the unit's objects from ours (same place in z-order and grouping) |
| position/size only | text or style, not geometry | `move`: shift the deck's objects by the source delta |
| changed | geometry | recreate, then re-apply the deck's transform change (`delta`: theirs · base⁻¹); if the source moved it too, the deck's position wins and it's a conflict |
| changed | text style / shape style | recreate, re-apply the deck's change: uniform over all runs → over all the text; some words (or a table) → the deck's run attributes onto the same words of the new text (character alignment, `sync.style_range_requests`); non-uniform paragraph styles → keep the deck, conflict. A conflict is reported only when the source changed the same style attributes |
| text changed | text changed | word-level diff3; clean → recreate and write the merged text; overlapping → keep the deck, conflict |
| text / position changed | the deck shows exactly that (same text; a move the source now reproduces within 2 pt) | `adopt`: nothing written, reported as converged; the base takes ours IR and the deck's version of those fields (e.g. after `pull`) |
| picture file changed, same picture | anything | no change: the base takes the new hash (`snapshot.refresh_pictures`, reported as converged) |
| changed | group taken apart (the unit's `_g` group gone) | recreate without that group |
| changed | image replaced | keep the deck, conflict |
| changed | deleted (all or part) | keep deleted, conflict |
| deleted | unchanged | delete its objects |
| deleted | deleted | nothing |
| deleted | edited | keep, conflict |
| added | — | create |

A picture whose file the converter now writes differently but that puts the same thing on the page
(`snapshot.same_picture_file`: the same pixels where the new file is opaque, one flat colour under
it where it is transparent - the ground the older picture painted in) is not a source change: the
base takes the new hash and is saved, and the deck keeps its object, instead of every formula,
icon and ball being rewritten the first time a deck converted before that change is synced.

A unit removed from the source is kept when its words went into a unit kept in conflict (classify
joined two paragraphs), so no text is lost. Slides: new frames are created at the aligned
position; frames removed from the source are deleted if the deck didn't touch them (no edits, no
user objects, same notes and background), else kept after their base predecessor and reported;
a frame the deck deleted stays deleted (a conflict if the source changed more than its frame counter);
the source order is applied only if the deck didn't reorder slides itself; user-added slides stay
after their live predecessor. Notes follow the text rules (diff3 against the notes read back);
a background the source changed is rewritten unless the deck changed it too (conflict).
Recreated units with holes or overlays are re-measured (`measure_places` scratch slides in the live
deck; leftovers `b2s_mNNN` of an interrupted run are deleted at the next start).

## Writing to the live deck
- Every batchUpdate carries `writeControl.requiredRevisionId`, chained through the responses. A
  mismatch before the first write re-reads and re-plans (at most 3 times), so concurrent edits are
  never overwritten; later batches (order, overrides) are planned on the deck sync itself wrote.
  Test hook: `B2S_SYNC_BEFORE_WRITE` (a shell command) runs once after planning, before any write.
- Pictures (new or changed figures, formula pictures, backgrounds) can't come from URLs: they go
  into a **staging deck** imported from a .pptx (`build_pptx`, pictures tagged `b2s-stage:N`), whose
  images' `contentUrl`s are used by `createImage` (plus a transform undoing its letterboxing) and
  `stretchedPictureFill` in the live deck (no public links). The staging file is deleted after the
  content batch: its contentUrls stop working once it's gone.
- New slides: `createSlide` with the layout (title/subtitle placeholders mapped, the others deleted
  after a read), then emit's `slide_parts` under renamed ids. The theme decoration sits on the
  layouts (`emit.plan_theme`), and backgrounds that don't show it got a copy of their layout without
  it, so a new slide with a background picture of its own takes the layout (by object id) of the
  converted slides with that background; one that inherits the master background takes the plain one. Updated slides: recreated units are
  built the same way; their old objects are deleted, the groups they were in are ungrouped
  (outermost first, since a group inside a group can't be ungrouped) and regrouped under the same
  ids (innermost first), and the new objects are brought into the old z position (`restack`).
  A group that would be left with one child disappears, its child taking its place.
  Placeholders are refilled in place.
- Template shapes (native shadows, exact corner radii, diagram nodes and elbow connectors) can't
  be copied across decks: a live object with the same template key is duplicated if there is one,
  else a plain 100 pt shape stands in and the report warns.
- `--dry-run` plans and reports without writing. A second sync with no changes sends no request
  (same revisionId) and leaves the base alone.
- After writing, the new base = ours IR + read-back of the objects as sync created them (before
  deck overrides are re-applied); units kept from the deck carry their old base.

## Reports
`sync-report.json` (top level: `applied`, `overrides`, `conflicts` with field, base, ours, theirs
and resolution, `converged`, `user_objects`, `slides_created`, `slides_deleted`, `slides_moved`,
`slides_kept`, `slides_user_added`, `warnings`, plus pdf, url, attempts, requests per phase and the
per-slide `actions`) and `sync-report.md`.

## Not supported yet
- Crossing reorders of unlabelled frames (the alignment keeps order; label frames).
- diff3 inside tables and diagrams (a text edit there on both sides keeps the deck).
- Layout texts and placeholder styles (`write_layout_texts`) aren't synced; nor are
  `fallback_pictures` rebuilds.
- Z-order edits in the deck aren't detected; a recreated unit goes back to its old z position.

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
