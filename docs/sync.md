# Sync: merging a changed beamer source into an edited Slides deck

Context: an AI writes the talk as beamer, `convert` turns it into a Slides deck, people edit that
deck, the AI (or a LaTeX user) edits the beamer again. `sync` brings the source changes into the
live deck without destroying the deck edits; `pull` brings deck edits back towards the source.

**Never lose deck edits** (`guard.py`): `convert` on an output folder that already has a deck
replaces that deck's whole content, so it now checks first whether anyone edited it and refuses
if they did — see [Never lose deck edits](#never-lose-deck-edits) at the end.

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
  inserted or renamed frame doesn't shift keys. What that order leaves over is then paired by
  content alone (`identity.cross_pairs`), which is how a frame the source moved across another
  keeps its slide: the best explanation has to be good on its own (`CROSS_SURE`) and no other
  leftover may come within `CROSS_MARGIN` of it, on either side, so frames that say much the same
  pair nothing and stay with the order. What is still over is finally paired by its place
  (`identity.gap_pairs`): one slide and one frame alone between two neighbours that paired - the
  ends of the talk counting as neighbours - are the same frame if they still share some words
  (`GAP_SURE`), which is what saves an unlabelled frame whose title the source replaced, while a
  frame deleted and another written where it stood share the place and not the words, and stay
  apart. Unpaired ours slides get fresh keys.
  One label can name several slides - `--overlays all` gives a slide per step of a labelled frame,
  and a source may reuse a label - and then the n-th slide of that label pairs with the n-th in the
  base (`identity.align_slides`). Pairing them all with one base slide used to leave its siblings
  unpaired, i.e. read as slides the source had dropped: syncing an `--overlays all` deck against
  its own unchanged PDF planned to delete a step (and then crashed on the slide order). Found live
  on 2026-09-18, pinned in `tests/test_identity_labels.py`.
  "Two different labels never pair" holds only where both labels exist on both sides, which means
  two frames that both exist; a label neither side knows on the other is a label *renamed*, and
  then the words decide, or a rename would bring the deck's slide back beside itself
  (`align_slides.pairable`). See "When a label moved" below.
  `beamer2slides label` writes a label into every frame that has none and `convert --check-labels`
  says what a PDF's missing or duplicated labels will cost a later sync (docs/labels.md); AI
  authors are told to label every frame and never change one (docs/ai-authoring.md).
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
{version: 1, generation, presentationId, revisionId, source: {pdf, sha1}, overlays ("last"|"all"), scale,
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
Two crash-recovery keys live beside the slides: `pending` (a run has started writing) and `cleanup`
(objects a finished run has still to delete) - see "If a sync or a pull dies".

`overlays` is the overlay mode `convert` used. `sync` takes it from there unless `--overlays` says
otherwise (`sync.overlay_mode`): a deck converted with `--overlays all` has a slide per step, and
syncing the same source with `last` would leave the steps in between out of `ours`, where they read
as slides the source dropped - sync would delete the ones nobody had edited. Asking for the other
mode on purpose still works and is reported as a warning.

### Two checkouts, one deck
Because Drive holds the base, a second checkout (or a second person) syncing the same deck reads
the base the first one wrote, not the deck as it was before. A cache from another deck is refused
(`presentationId`), and with no base anywhere sync stops: "no sync base for presentation …".

When the deck names a base file in Drive that cannot be read - a Drive cleanup deleted it, or
`save_drive` failed and only warned - sync falls back to the folder's copy and says so
(`snapshot.stale_base_warning`, also in the report's warnings), because that copy can be older
than the deck. Measured on a live deck (2026-09-18): with the Drive base hidden and the local copy
rolled back one generation, syncing the same source again **wrote nothing and lost nothing** (same
10 slides, every line still there, the deck edit intact). The stale base only made sync see the
element the previous sync had recreated as deleted in Slides, and it kept it deleted: base object
ids that are no longer in the deck read as a deck deletion, and the deck wins. A stale base can
make sync do less than it should, never lose what the deck holds. Offline tests:
`tests/test_base_storage.py` (Drive over cache, refusals, the warning, save and repoint).

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
| position/size only | text or style, not geometry | `move`: shift the deck's objects by the source delta - only when every member of the unit moved by the same step (`merge.unit_shift`), since sync moves the unit's top object; a formula picture the source re-placed inside its line is recreated instead |
| changed | geometry | recreate, then re-apply the deck's transform change (`delta`: theirs · base⁻¹); if the source moved it too, the deck's position wins and it's a conflict. Only when the whole unit went with its top object (`merge.geometry_writable`): sync transforms the unit's top, so a member the person dragged on its own - a formula picture out of its line - would be put back where the converter had it, and such a unit is kept as the deck has it, with a conflict |
| changed | text style / shape style | recreate, re-apply the deck's change: uniform over all runs → over all the text; some words (or a table) → the deck's run attributes onto the same words of the new text (character alignment, `sync.style_range_requests`); non-uniform paragraph styles → keep the deck, conflict. A conflict is reported only when the source changed the same style attributes |
| text changed | text changed | word-level diff3; clean → recreate and write the merged text; overlapping → keep the deck, conflict. In a table the diff3 runs per cell (`merge.table_merge`, applied with `cellLocation`); a row or column added on either side, or a cell holding a line break, makes the whole table a conflict |
| text / position changed | the deck shows exactly that (same text; a move the source now reproduces within 2 pt) | `adopt`: nothing written, reported as converged; the base takes ours IR and the deck's version of those fields (e.g. after `pull`) |
| picture file changed, same picture | anything | no change: the base takes the new hash (`snapshot.refresh_pictures`, reported as converged) |
| added: a picture | the deck already shows that picture (after a pull) | `adopt_object`: the person's object becomes the element's, nothing written (`sync.picture_adopter`) |
| picture changed | image replaced with the picture the source now draws | `adopt`: the deck's picture and box stay, reported as converged instead of a conflict |
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
A picture that really is another one is still rewritten: a figure that is one `\includegraphics`
now keeps the image's own box and the author's own bytes, so on the first sync of a deck converted
before that it changes box and picture - one correct update, not a conflict.

A unit removed from the source is kept when its words went into a unit kept in conflict (classify
joined two paragraphs), so no text is lost. Slides: new frames are created at the aligned
position; frames removed from the source are deleted if the deck didn't touch them (no edits, no
user objects, same notes and background), else kept after their base predecessor and reported;
a frame the deck deleted stays deleted (a conflict if the source changed more than its frame counter);
the source's order is applied, except that a slide the deck itself picked up (out of its base order
there, `merge._out_of_place`) goes back beside what it follows in the deck, and when both sides
moved the same slide the deck's place wins and the report says so - one slide dragged in Slides is
no instruction to freeze the other forty; user-added slides stay after their live predecessor. Notes follow the text rules (diff3 against the notes read back);
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

## If a sync or a pull dies
The rule: **either the write completes, or it can be redone**; never a deck where a person's edit
is gone and no base knows about it. A sync can be killed at any point (`kill -9`, a lost network, an
expired token, a batch Google refuses) and the next sync of the same source reaches the deck an
uninterrupted one would have. What makes that true:

- **Nothing is deleted before its replacement exists.** The phases are, in order: *recovery* (only
  objects the deck holds twice, see below), *content* (creates and in-place updates), *order*
  (z-order), *overrides* (the deck's own edits back onto recreated objects), *store the new base*
  (locally, then Drive), and only then *cleanup*: `deleteObject` for the objects and slides this
  sync replaced. A run that dies before cleanup leaves the old objects in the deck, so the next
  sync plans exactly the same merge from the same base and writes it again; the duplicates it left
  are swept. The deletions are listed in the base as `cleanup` **before** they are sent, so a death
  between the base and the deletions is finished by the next sync. If the base could not be stored
  in Drive, the deletions are skipped altogether: a machine reading the stale Drive base must never
  find its objects gone.
- **Batches are cut at slide boundaries** (`sync.batches`, `BREAK` marks): a death between two
  batches leaves whole slides, never half an element. Only a single slide larger than 400 requests
  is split.
- **Pictures don't depend on the staging deck once they are in.** `createImage` copies the file into
  the live deck, so a picture keeps working when the staging deck is deleted after the content
  batch; a run that dies before that leaves only that Drive file, and an attempt that has to
  re-plan stages afresh, because the contentUrls die with the file. Its id is in the pending marker
  so a person can find it, but no sync deletes it: an id read from a file could name anything, and
  the only Drive file sync ever deletes is the one that same process just created
  (`tests/test_guard.py`).
- **A pending marker is stored before the first write** (`Sync.mark_pending`): the base keeps its
  generation (it still describes the deck) and gains `pending` = this run's generation and id token,
  the revision it planned against, the source's sha1, the object ids it is about to create, the
  slides it is about to add, the staging deck's file id, and the read-back of every placeholder it
  is about to rewrite **in place**. The last one matters: rewriting a title placeholder is the only
  destructive content write, and `pending.in_place` lets the next sync put the person's text back
  into what it compares against (`restore_in_place`), so their edit is merged again instead of
  quietly adopted. The marker is written with the base, so a run that dies leaves a valid base of
  the old generation plus a note of what it started; a run that finishes removes it.
- **Leftovers are swept, and only leftovers** (`plan_recovery`, run before anything is planned).
  An object is deleted only if the base's `cleanup`/`pending` names it, or its id was minted by a
  sync of a *later* generation than the base's (only our own code makes those ids) *and* the base's
  own objects for that element are all still alive - i.e. it is a second copy of something the deck
  already has. A person's object can never match: their ids are Slides' own or the converter's.
  Each run re-rolls its id token, so a second run never collides with the dead one's ids.
  The generation half of that rule needs a base that really is the deck's own. When the deck names a
  base in Drive that cannot be read (`stale_base_warning`, "Two checkouts" above), the folder's copy
  may simply be older than the deck, and a later generation then means nothing: those objects are as
  likely the finished work of the other checkout's sync. Sync is told so (`trust_generation`) and
  sweeps only what that base itself names. Healing is unaffected - taking an object over cannot be
  made wrong by a base that is behind.
- **An element whose objects an interrupted run already deleted is healed**, not reported as
  deleted: its replacement is still on the slide, tagged `b2s:<slide>/<element>`, and the base takes
  it over. If that run converted a different PDF, the element's hashes are set to `interrupted` so
  the source is written over it once more.
- **The base is validated before use** (`snapshot.base_problem`, `read_local`, `base_matches`):
  a truncated or unreadable file, a schema from a newer beamer2slides, or a base of another
  presentation is refused with a reason in the report's warnings instead of being merged against.
  Drive stays authoritative except when the local copy has the higher generation (the last sync's
  Drive upload failed). `save_local` writes `base.json.writing` and renames, so it is never half a
  file. If the base describes none of the deck's slides at all, sync stops and says so rather than
  reporting every element as deleted.
- **`pull --apply`** writes each file through a temporary and `os.replace`, and the `.bak` backup the
  same way, so every file is whole (the old one or the new one) whatever the moment of the kill. A
  source file whose sha1 changed since the pull read it (the person edited while the compile loop
  ran) is never overwritten: the pull's version lands beside it as `<name>.b2s-new` and is listed in
  `edits.json` as `not_applied`.
- What a killed sync **can** leave behind: scratch slides (`b2s_mNNN`, swept at the next start),
  its staging deck (harmless: it is only a source of picture URLs, and it costs nothing in the
  deck), and duplicate objects until the next sync. No sync-report is written.
  `python tools/drive_usage.py` lists the leftover staging decks among the app's Drive files (they
  carry the name sync gives them and, since 2026-09-18, an `appProperties.b2sStaging` marker naming
  the deck they were staging for), and `--delete-staging` deletes those over 12 h old - a person
  saying yes to what sync will not do from a file's word.
- Test runs also leave whole decks behind, of folders long since thrown away.
  `python tools/drive_usage.py --delete-orphans` lists the decks **no file under `--root`
  (default `out`) names any more** and, with `--yes`, moves them to the Drive trash (restorable for
  30 days). What counts as naming one is deliberately too generous - every id-shaped word in every
  .json/.md/.txt under the roots - because a reference it invents keeps a deck and one it misses
  loses one; a deck a Drive backup copy points at, a deck younger than `--older-than-hours`, and
  every deck at all if any file under the roots cannot be read, are spared.

**Why the next run re-plans instead of resuming.** The pending marker could hold the planned
requests and let a second run send the rest of them, but the deck is the truth and it may have moved
on: the person can edit between the two runs, and the requests were planned against a revision that
no longer exists. Re-planning from the deck as it is now is the only thing that keeps "deck edits
win" true, and it is cheap (a sync is seconds). So the marker records only what is needed to *undo*
a half-done run - which objects were its own, and which texts it overwrote. The cost of dying before
the new base is stored is therefore work, not data: everything that run created is swept as a
duplicate and written again from the old base, and the deck ends up where it would have been.

Fault injection for the tests (`faults.py`): `B2S_FAIL_AT=<point>[:<n>]`, comma-separated, `!point`
to leave the process at once with `os._exit` (no `finally`, no cleanup). Points: `plan`, `journal`,
`measure`, `content`, `order`, `overrides`, `base:save`, `base:drive`, `cleanup`, and `pull:apply`.
Unset, `fail_at` is one `os.environ.get` and a return; `B2S_BATCH_SIZE` (same rule) lowers the write
batch size so a phase really takes several batches. `tests/test_sync_crash.py` has the offline
tests (write order, base validation, recovery, pull's atomic apply) and, under the `sync` marker,
one case per point that really kills a sync of a real deck and checks with `tools/sync_check.py`
that the deck edits are all still there and the deck converges.

## When a label moved
A frame label is a promise: the frame carrying it is the frame the deck's slide was made from
(docs/labels.md). Rename one, or paste `[label=intro]` onto the next frame, and nothing in the PDF
says so - the destination `intro` is simply on another page. Following it writes one frame's text
onto another frame's slide, and the person's edits, which sync keeps, end up beside sentences they
were never about. The frame that lost the label comes back as a second copy of itself.

This is not a loss - nothing is deleted, no words disappear - so the loss oracle cannot see it.
`identity.label_moves` is the check that can, and it works the only way available: by asking
whether the two slides a label pairs say the same thing, and if not, whether some *other* slide
explains them better. It looks only among slides nothing else accounts for, so a frame settled by
its own label can never be stolen.

- **`moved`** - the source's labelled frame is recognisably some other base slide, *and* the base's
  labelled slide is recognisably some other source frame. The label is ignored and the content
  decides. That is also where the person's edits belong: they edited those words, not that label.
- **`unsure`** - only one of the two. Either a label moved, or the author moved a passage from one
  frame to another; from the PDF the two look the same. The label is followed and nothing is
  re-paired.
- **silence** - neither. A frame rewritten from scratch looks exactly like a label move from one
  side, and that is a plausible edit, not a broken invariant.

Both verdicts are **conflicts** in the report (`field: label`), because which frame is which is a
question with an answer and guessing it wrong is the one mistake here that quietly moves somebody's
work. A label renamed or dropped where the content still recognises the frame is a **warning**: the
slide kept its identity, but the source has one hook fewer for the next version.

The title counts by degree, not as yes or no (`identity._title_alike`). The stress deck moves a
label and retitles all 48 frames in the same version ("Moving labels" → "Moving labels v2"), and a
yes-or-no "same title?" says no to every pair at once - which leaves the frame the label left no
better an explanation than the one it landed on, and the check silent exactly where it is needed.

`tools/fuzz_labels.py` measures it against a truth the synthetic source knows (every frame is
tagged, so a pairing is right or wrong). 3000 rounds, half of them breaking the invariant on
purpose, plausible source edits either way (a quarter of them revising every title at once):

misidentified frames, by what is switched on (the campaign's three columns):

| | `order` | `before` | `now` |
|---|---|---|---|
| labels sound | 0.06% | **0.00%** | 0.00% |
| labels sound, a frame moved | 5.53% | **0.00%** | 0.00% |
| labels broken | 14.68% | 14.64% | **1.04%** |
| labels broken, a frame moved | 19.15% | 11.35% | **1.18%** |

`order` follows the labels and pairs the rest by the order-keeping alignment alone; `before` picks
that alignment's leftovers up by content and by place too (`identity.cross_pairs`,
`identity.gap_pairs`); `now` adds the moved-label check, which is what sync does. They cover
different halves: the leftover passes are what a moved or retitled frame needs and do nothing for a
moved label, and the reverse. **With the labels sound, not one frame of 1524 rounds - 6809 frames -
ends up on another frame's slide.** `GAP_SURE` earns its own place in that: pairing every lone
leftover regardless of its words puts 2 of them back (and 3 more in the broken rounds). The
campaign gives the same tally anywhere from 0.2 to 0.45 and degrades below 0.2, so the threshold
sits in the middle of that - the stress deck's own retitled-and-half-rewritten frame scores 0.45,
which is how close to the edge a real frame comes.

The check never spoke once in 1524 rounds whose labels nobody touched, and no round came out worse
than before it existed. Of the broken rounds still wrong without a reorder, 35 of 36 were reported
as a conflict and 1 passed in silence (a label pasted onto a frame added in the same version whose
own frame was deleted, a frame renamed and retitled and reworded at once - nothing left to
recognise them by; they become new slides, and the old ones are kept with their edits). Fixed seeds
run in the default suite
(`tests/test_sync_fuzz.py::test_a_broken_label_invariant_sends_far_fewer_slides_to_the_wrong_frame`,
which also asks the sound rounds for not one frame wrong, and
`::test_a_frame_the_source_moved_across_another_keeps_its_slide` for the rows with a move -
switching `cross_pairs` off puts 9 frames of its 19 reordered rounds back on the wrong slide).
The passes themselves: `tests/test_frame_moves.py`.

`tools/fuzz_sync.py` also moves, renames and drops labels as source edits now. That found two
things: a slide the deck deleted while the source keeps it (`gone`) kept a base entry frozen at the
moment of deletion, so renaming its label or title afterwards made it a new frame and it came back
(`sync.new_base` now lets a `gone` entry's label, title and words follow the source while its key
and its place stay); and a renamed label used to cost the slide its identity outright, which is
what `align_slides.pairable` now allows the content to fix.

## Reports
`sync-report.json` (top level: `applied`, `overrides`, `conflicts` with field, base, ours, theirs
and resolution, `converged`, `user_objects`, `slides_created`, `slides_deleted`, `slides_moved`,
`slides_kept`, `slides_user_added`, `warnings`, plus pdf, url, attempts, requests per phase and the
per-slide `actions`) and `sync-report.md`.

## Proving nothing is lost (fuzzing, `tools/loss_oracle.py` + `tools/fuzz_sync.py`)
The live scenarios check hand-written expectations; the fuzz checks the one property that has to
hold for every sync, including the combinations nobody thought of.

- **The oracle** takes the read-back *before* a sync, the one *after*, the base, the report and the
  new conversion (`ours`, so a word the source deliberately rewrote is not read as a word that
  vanished), and asks whether anything a person put in the deck disappeared without being accounted
  for. Its docstring defines "accounted for": user objects survive whole (text, picture, box, group
  - the only allowances are Slides' own, a one-child group disappearing and a child losing a
  converter group that is gone); a word the person typed is still readable somewhere on that slide
  or is reproduced verbatim in a `conflicts` entry; a word the person deleted doesn't come back into
  that element; an element the person moved stands afterwards where they put it, or - when the source
  moved it too - at the conversion's new box with the person's step on top of it (`deck_placement`
  works that box out, so the base's old box is never mistaken for it); its picture and its styling
  are the person's unless a conflict says otherwise; notes and backgrounds likewise (on a slide the
  person added, word for word); slides only vanish when reported *and* untouched,
  user-added slides never; taking the reported moves out of the order before and after must leave
  the same sequence; converter content the new conversion still has keeps an object; and the report
  is honest - every `applied` entry really changed something, every `converged` entry really changed
  nothing. It runs offline from two snapshots: `python tools/loss_oracle.py <folder>`. The fuzz
  harness writes them; a production sync writes `base.json` and the report but no read-backs yet, so
  auditing a real sync means `sync.py` keeping `theirs` (before) and a final read (after) beside them.
- **The fuzz** (`python tools/fuzz_sync.py offline --rounds N`, seeds replay: `--replay <seed>`)
  builds a synthetic deck, applies 2-8 random deck edits and 1-4 random source changes, plans the
  merge and applies the plan the way this document says sync does (`tools/fuzz_world.py`, the
  reference applier), then runs the oracle. About 50 rounds a second per core; today 10 000
  single-step rounds, 4000 three-step chains, 1500 six-step and 600 ten-step chains ran clean.
  `live` does the same against real decks (convert v1 → random `deck_edits` → `sync <variant>` →
  oracle + `sync_check.integrity`), 3 at a time in `out/sync-fuzz/<seed>`, decks of passing rounds
  deleted; `--chain N` edits and syncs N times in a row. Every failure writes base, before, after,
  report, edits and findings into the run folder, and a shrinker drops edits one at a time until
  the smallest failing combination is left.
- `tests/test_sync_fuzz.py` runs fixed offline seeds in the default suite (a second) and proves the
  oracle catches losses injected on purpose; the live campaign is marked `sync`
  (`B2S_FUZZ_LIVE_ROUNDS`, `B2S_FUZZ_ROUNDS`).
- Each round also checks that the sync **settles**: replanning against the base the round recorded,
  with the same source, must write nothing - a base that doesn't describe the deck it just wrote
  makes the next sync rewrite units, and a rewrite is where work gets lost.
- Found by it so far: the `move` shortcut took its step from the unit's anchor although the source
  may have re-placed only an anchored member (`merge.unit_shift`); a geometry override was promised
  for a unit whose parts the person had moved apart, which sync cannot write
  (`merge.geometry_writable`); sync sent an alt-text title for a diagram's main object, which is
  the group `emit.diagram_requests` builds - the API refuses that and rejected the whole batch, so a
  sync that rewrote a diagram slide died (fixed: the diagram goes untagged, like every element group
  in a converted deck); a slide the source dropped and the deck's edits kept alive went on claiming
  its label in the base, so the frame carrying that label now paired with the dead entry
  (`sync.new_base` clears it); and `sync.base_order` left out the slides the
  deck deleted although the source still has them, so their base entries landed last and the frames
  after them lost their keys on the next conversion, which created them again (fixed - and
  `fuzz_world.rebase` now orders the base with `sync.base_order` itself rather than with a correct
  copy of it, so the campaign catches it again if it stops doing that: putting the old rule back
  fails 54 of 1500 rounds).

## Not supported yet
- Crossing reorders of unlabelled frames whose words don't tell them apart: the alignment keeps the
  order, `cross_pairs` refuses to guess between two leftovers that explain each other equally well,
  and `gap_pairs` only speaks where one slide and one frame are alone between two paired
  neighbours, so both come back as new frames. Label them (`beamer2slides label`), and it is a
  non-issue.
- diff3 inside diagrams (a text edit there on both sides keeps the deck); tables merge per cell,
  but a row or column added in the deck keeps the deck's table.
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
target values, for an AI author to finish). `--apply` writes the changed files in place, keeping
what was there as `<file>.bak`, then `.bak2`, `.bak3` (`inverse.keep_backup`: a second `--apply`
must not write over the author's own version, and a picture it replaces is kept too; a file that
already holds what pull wants is left alone), and copies new picture files;
`--out DIR` writes the edited source tree there instead;
neither leaves the source untouched. After the rebuilt PDF is synced, the pulled fields are
converged overrides.

### Pictures (`deck_ir.picture_props`, `inverse.Planner.picture`, `compare.displayed_picture`)
What a picture in the deck can be recovered from, measured on a live deck by `tools/probe_images.py`:

| source | pixels | crop | rotation | transparency | brightness/contrast | recolour | outline |
|---|---|---|---|---|---|---|---|
| `contentUrl` (`=s2048`) | the stored file byte for byte | kept as `cropProperties` | kept in the transform | kept as a property | **baked** by Google on import | **baked** (duotone; greyscale is dropped) | kept as a property |
| `.pptx` export (`files.export`) | the same bytes | `a:srcRect` | `xfrm rot` | `a:alphaModFix` | baked | baked | `a:ln` |
| `sourceUrl` | the original, when the picture came in by URL and is still fetchable | — | — | — | — | — | — |

Google stores at most ~2046 px on the long side (a 3000 × 2000 PNG comes back 2046 × 1364, PNG
stays PNG, JPEG stays JPEG, an animated GIF keeps its frames, EMF/WMF become PNG). `contentUrl` is
therefore the recovery source; `sourceUrl` is used only when it holds the same picture with more
pixels. The bytes are saved as they are, never re-encoded or downscaled.

Pull writes each edit as a LaTeX option where LaTeX can express it, and bakes only what it can't:
- crop → `trim=l b r t,clip` in the file's natural bp (pixels at its dpi, a PDF's first page);
- rotation → `angle=` (Slides turns clockwise about the centre; the turned picture is placed by its
  bounding box, so no `origin` is needed), mirroring → `\reflectbox`;
- transparency → a `\tikz\node[inner sep=0pt,text opacity=…]`, a tikzpicture → a transparency group;
- outline → the same tikz node with `draw=` and `line width=`;
- brightness, contrast and recolour → baked into the saved file, and said so in `edits.md`;
- animated GIF, WEBP and other formats pdflatex can't read → PNG, first frame, with a note;
- pictures with edits or without a source of their own go into a `textblock*` at their bounding box.

Files are `figures/<slug of the alt text or `picture`>-<sha8>.<ext>`, but a picture already in the
source tree is reused instead: identical bytes first, else the best-resolution file that looks the
same (64 × 64 grey, correlation ≥ 0.98, aspect within 2%) — a PDF before any raster.

A picture the person put over a figure the source draws (tikzpicture, pgfplots) replaces it: the
environment is commented out under `% b2s pull: replaced by <file>`, with a `\phantom` keeping its
flow room when the picture goes into a textblock. Formula and icon pictures inside text lines are
never replaced silently: they are reported (`compare` matches them by thumbnail alone, tolerance
`inline_phash`). Replaced figures are found by thumbnail: mean difference plus, for opaque pictures
with contrast, correlation (`compare.picture_differs`) — a curve replaced by bars differs by only
0.11 on white, while transparent overlays must be judged by the mean alone.

Results: offline test bed (`python -m pytest -m inverse`, ~2 min) converges on 4 source pairs in one
round each and on 10 synthetic edits of 01_basic in 1-3 rounds; live, the sync talk edited in Slides
(reword, bold, 30 pt move, new red text box, new slide with bullets) converged in 3 rounds (~40 s)
and the deck converted from the pulled source compares to the edited deck with 0 open residuals
(text anchors within 2.1 pt), only the new slide's layout title differing (theme).
Pictures, live (`tools/pull_images_proof.py` on the same talk: a 3000 × 2000 photo inserted, cropped
and turned 12°, the TikZ plot replaced by a chart, a half-transparent picture added): converged in 4
rounds, and the deck converted from the pulled source compares to the edited deck with 0 open
residuals (largest picture box error 1.9 pt); the photo is on disk as Google's 2046 × 1364 PNG,
6.1 MB, unchanged.

### Note for sync and convert
- **After a pull, sync must not duplicate the pulled picture.** The source now has an
  `\includegraphics` where the person had inserted (or replaced with) a deck picture, so the new
  conversion carries a picture the deck already shows. Treat them as the same unit when the
  fingerprint matches: the file's sha1 (identical bytes are the normal case — pull saves the deck's
  own bytes), else a perceptual match (`compare.picture_hash` / `inverse.same_look`: 64 × 64 grey,
  correlation ≥ 0.98, aspect within 2%) **and** boxes overlapping by most of their area on the same
  slide key. On a match, keep the deck's object (its id, crop, rotation, transparency and outline are
  the person's edit, and the source now says the same) and record it in the base as converged instead
  of deleting the deck picture and creating a new one. A source figure that was commented out by pull
  (`% b2s pull: replaced by <file>`) disappears from the conversion: its base unit must be retired
  without deleting the deck object that replaced it.
  Done (`sync.picture_adopter`, live scenario `pull-picture`): a new picture unit whose file has the
  bytes or the look of an unmatched deck object overlapping its box is adopted (`adopt_object`), and a
  deck object whose own picture was replaced by the one the source now draws converges instead of
  conflicting. A figure only commented out is a unit the source removed, so its objects are deleted
  unless the deck edited or deleted them - the object that replaced it is a different one.
- **Convert should pass raster `\includegraphics` through losslessly.** Today a figure region is
  re-rendered from the page, so a 2046 px photo becomes a ~600 px crop and a pull afterwards can only
  recover that. PDFium gives the image object's own bitmap (`FPDFImageObj_GetImageDataDecoded` /
  `GetRenderedBitmap`): when a figure region is one image object with no vector ink on it, store its
  bytes and its transform instead of the rendered crop. The deck then keeps the author's full
  resolution, and a pull returns the same file.

Not translated yet (reported instead): tables, diagram labels, shape colours, paragraph alignment,
frame title position and theme styles, edits inside math, rotated text, overlays beyond the last
step (compile with `--handout` to pull a handout-style deck).

## Never lose deck edits

`convert` on an output folder that already has a deck does **not** create a new presentation: it
replaces that deck's whole content through `files.update`, so the URL stays and everything anyone
did in Slides is gone. That is the only write in the tool that can destroy a deck, and it used to
happen silently. `guard.py` now stands in front of it.

### What convert does now

Before any work (and again immediately before the write, in case someone typed in the deck while
the PDF was being converted), `guard.check_rebuild` reads the live deck with `presentations.get`
and compares it with the sync base (`<out>/sync/base.json`, or Drive `appProperties.b2sBase` -
whichever `snapshot.load_base` finds; Drive wins). It refuses for three reasons:

| reason | when | message says |
|---|---|---|
| `edited` | someone changed the deck since the converter wrote it | what was edited, with up to 3 examples |
| `no-base` | there is no base, so the question cannot be answered | the base is written by `convert`; older decks have none |
| `other-source` | the base says this folder's deck came from another PDF | rebuilding here would replace that deck with this PDF's slides |

A refusal exits non-zero (`RebuildRefused` → `SystemExit`) and nothing is written. It looks like
this (from `tools/rebuild_guard_proof.py`, a real run):

```
refusing to rebuild: this deck was edited in Google Slides after beamer2slides wrote it.
  https://docs.google.com/presentation/d/11yU.../edit
  1 slide edited: 1 text edit, 1 object added in Slides
    - slide 2 "Why decks and sources diverge": text edited (text/body/0: "The source is HANDWRITTEN in by an auth…")
    - slide 2 "Why decks and sources diverge": 1 object(s) added in Slides
  A rebuild replaces the whole deck. What to do instead:
    merge the PDF into the deck, keeping the edits:  python -m beamer2slides sync talk.pdf --deck out\talk
    leave that deck alone and make a new one:        python -m beamer2slides convert talk.pdf --out out\talk --new-deck
    rebuild anyway (the deck's content is replaced): python -m beamer2slides convert talk.pdf --out out\talk --force-rebuild
  The deck is at revision 2Ymo8YWHlbj6kg; a forced rebuild keeps a backup first (--backup, docs/sync.md).
```

### What counts as an edit

Exactly what sync would keep, through the same code: `merge.deck_edits` (geometry, text, text
style, shape style, image, group, deleted, part_deleted), `merge.user_objects` (objects the
converter never made), `merge.background_edited`, speaker notes, plus slides added, deleted or
reordered. There is one notion of "edited" in the tool, not two.

Deliberately **not** an edit:
- a new `revisionId` - Google bumps it on its own (opening the deck is enough);
- a new `contentUrl` for a picture nobody touched - Google reissues those; pixel signatures decide
  (`guard.sign_changed`, the same rule as `sync.Sync.sign_changed`), and only pictures whose URL
  hash changed are downloaded;
- exporting a thumbnail (`fidelity`, `measure_places`);
- `measure_places`' scratch slides `b2s_mNNN` left by an interrupted run - converter leftovers,
  filtered out before the comparison.

### Getting a deck back

Before every destructive write the deck's `revisionId`, Drive `modifiedTime` and what was found
are appended to `<out>/backups/backups.json` (and the rebuild's entry goes into `emit.json` as
`previous`), and the same lines are printed. `--backup` decides what else is kept:

| `--backup` | convert | sync |
|---|---|---|
| `auto` (default) | a `.pptx` export when the rebuild is forced, nothing when the deck was untouched | a `.pptx` export before the first write |
| `none` | nothing | nothing |
| `file` | `.pptx` in `<out>/backups` | the same |
| `drive` | a Drive copy of the presentation (its own URL) | the same |
| `both` | both | both |

`tools/deck_backup.py` lists (`list`), exports (`export`) and restores (`restore --from FILE`, or
from a revision). A restore creates a **new** presentation by default; `--in-place` writes the
backup back over the deck, so every link to it keeps working.

Every sync writes one, so a folder synced often grows without end (the live suites left 110 files,
53 MB in one night). `prune --deck <out> [--keep 10] [--older-than-days N]` says what it would
delete and deletes nothing without `--yes`; it only ever touches files `backups.json` says this
program wrote, and the log entry of a deleted file stays with `backup.deleted` - what the deck was,
and when, is evidence worth keeping even when the way back is not (`guard.prune_backups`).

**A backup that did not happen stops the rebuild** (`guard.demand_way_back`). Drive can refuse both
kinds: the `.pptx` export over 10 MB, the copy when the Drive is full or over quota; `backup_deck`
only collected warnings, and the forced rebuild then went ahead and replaced a deck nothing could
bring back. Now the attempt is recorded in `backups.json` and the rebuild is refused with the
warnings, `--new-deck`, `sync`, `--backup drive` and `--backup none` - which is how one says out
loud that this deck may go. `--backup none` and an unforced rebuild of an untouched deck are not
affected (there is nothing to lose).

> **Measured, not guessed** (`tools/probe_revision_history.py`): for a Google-native presentation
> Drive does keep a revision row per editing session, and `revisions.list` shows them - but every
> revision's export link returns the file's **current** content, even with `revision=N` in the URL,
> and `revisions.update(keepForever=True)` does not change that. So a `files.update` rebuild leaves
> **nothing the API can fetch back**: version history is evidence, not a recovery path. The live
> proof confirms it end to end - the export of the revision from before a forced rebuild does not
> contain the word that was typed into the deck (`drive_history.holds_the_edit == false` in
> `out/agent-guard/proof.json`), while the `.pptx` backup restored into a new deck still shows it.
> Version history in the Slides UI ("File → Version history") may still show the old state to a
> human; treat it as worth a try, never as a promise. **The `.pptx` backup is the way back.**

### The other in-place writes

- `--new-deck` creates a new presentation and prints `the previous deck is left as it is at <url>`:
  it never touches the old one (proved live: the old deck's revision is unchanged afterwards).
- A **trashed or deleted** previous deck is never resurrected or written to: `guard.previous_deck`
  reports `live` / `trashed` / `gone` / `other` (not a presentation), and anything but `live` makes
  convert create a new deck and say so.
- A **stale output folder** (its deck was converted from another PDF) is the `other-source` refusal
  above.
- **Sync's staging deck** can never be the user's deck: `Sync.stage` uses the id `files.create`
  just returned, and the only ids ever passed to `files().delete` in `sync.py` are that staging id
  and the picture file it made (checked at runtime and statically in `tests/test_guard.py`).
- **Sync's first write** is not a rebuild - it only rewrites what the source changed - but it is
  still a write, so `record_sync_point` records the revision and takes a `.pptx` backup
  (`--backup`), prints a `recovery:` block, and puts the entry into `<out>/sync/sync-report.json`
  as `recovery`.

### Tests

- `tests/test_guard.py` (28 tests, no Google calls): fake read-backs in the style of
  `tests/test_sync.py` cover every detection case above (including the reissued `contentUrl` and
  the scratch slides), the three refusal reasons, `--force-rebuild`, the backup modes and the
  export-refused → Drive-copy fallback, the rebuild refused when neither kind of backup worked
  (and allowed under `--backup none`), the recorded entry and its restore hint, `plan_rebuild`'s
  paths (untouched, forced, refused, `--new-deck`, trashed, gone) and the staging-deck proof.
- `python tools/rebuild_guard_proof.py` (live, ~2 min, fixed folders `out/agent-guard/<deck>`):
  convert → convert again (no false alarm) → edit like a person (`tools/deck_edits.py`) → convert
  refuses with exit code 1 and the deck's revision and edit are untouched → `--force-rebuild`
  rebuilds and records the backup → the backup restores into a deck that still shows the edit →
  Drive history recorded as evidence → **`restore --in-place`**, whose result is compared with the
  deck from before the rebuild word for word, speaker notes included (`lost_words`) → **a sync of
  the recovered deck**, because a recovery one cannot work with afterwards is only half a way back
  → `--new-deck` leaves the old deck alone → a trashed deck reads back as trashed. Evidence in
  `out/agent-guard/proof.json` and `proof.log`.

  Measured on 2026-09-18: the deck came back at its own URL with all 10 slides and **no word
  lost**, and the sync that followed wrote nothing at all (0 changes, 0 conflicts, integrity
  clean) - the object identity in the deck survives the `.pptx` round trip, so a recovered deck is
  an ordinary deck again.
