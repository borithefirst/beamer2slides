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
`beamer2slides pull --deck <url|id|out> [--tex main.tex] [--apply]` lists deck edits relative to
the base (same detection) as `edits.json` / `edits.md`: slide key and title, element key, field,
before → after (text as a word diff), and for text the source location found through SyncTeX
(`-synctex=1` build; the element's PDF bbox → file:line range). `--apply` patches plain wording edits
into the .tex when the old words are found uniquely in that range (LaTeX-aware matching: commands,
braces, `~`, `--`, math left alone); everything else is left for the author, typically the AI, with
enough context to do it. After the source is updated and reconverted, sync sees those fields as
converged. Geometry-only edits usually stay deck overrides.
