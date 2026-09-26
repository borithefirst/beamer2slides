# beamer2slides, for an agent

These tools convert LaTeX beamer talks into editable Google Slides decks, keep a source and a
deck in step after both have been edited, and do the same one dimension smaller for a canonical
HTML file and a Google Doc. A harness that publishes the tools without these rules will sooner
or later destroy something a person made, so read this before the schemas.

## The bargain

Two people are writing the same document and neither of them is you.

* **The source is the author's.** The `.tex` file, or the canonical `.html`, is what the
  document *says*. It lives in git. Changing it is cheap and reversible.
* **The deck is the reader's.** The Google Slides deck, or the Google Doc, is what people have
  since done to it: a box dragged, a word bolded, a slide added, a comment left. None of that is
  in git, none of it can be recompiled, and most of it cannot be recovered once overwritten.

Every journey here exists to move changes between those two without losing either. When they
disagree, **the deck wins on content the reader touched** and the source wins on everything
else. You are not asked to adjudicate; the merge does that, and it tells you what it could not
decide. Your job is to make sure the merge is the thing that runs, rather than a rebuild.

## The one rule

**Never rebuild a deck somebody has edited.** `deck_convert` (or `deck_upload`) on an existing
deck replaces its whole content. The library checks first and refuses with the code
`deck_edited`, naming what was edited. When you see that refusal:

* run `deck_sync` instead — that is what it is for; or
* `deck_convert(new_deck=True)` to leave the old deck alone and make a second one; or
* stop and put the choice to the person.

Do **not** pass `force_rebuild=True` on your own initiative. Not once, not "to unblock", not
because the refusal looks like a technicality. Drive's revision history cannot undo it: every
revision of a Slides file exports the file's *current* content, so after a rebuild there is
nothing to fetch back except the `.pptx` backup the tool made on its way past. `force_rebuild`
is a thing a person asks for, in words, having been told what it costs.

The same applies to `doc_push` on a file that already names a document, and to `assume_base` on
`doc_sync`: both are ways of saying "ignore what the other side did". Ask.

## The order of operations

1. **`b2s_status`** — what is already in this workspace, what state it is in, whether Google is
   reachable. Cheap. Start here when you do not already know.
2. **`deck_inspect`** — classify the PDF and look at it before converting anything. It reports
   the structure, the local invariant checks, and the frame labels. No Google, a second or two.
3. **`tex_label`** — if frames have no labels, write them *before* the first conversion. A
   frame's `[label=...]` is the only part of its identity that survives compiling: without it a
   later sync falls back to matching on the title, and a talk whose titles move will pair the
   wrong slides. This is the cheapest thing you can do for someone and it cannot be done
   retroactively without guessing. A duplicate label is reported and never resolved — which of
   two frames a slide came from is a question only the author can answer.
4. **`deck_convert`** — the first conversion, or a rebuild of a deck nobody has touched.
   (`deck_prepare` and `deck_upload` are this same journey in halves, for a caller whose local
   work and Google write happen in different places — a sandbox that compiles, a service that
   holds the account. `deck_prepare` needs no account at all and writes the folder;
   `deck_upload` builds the deck from that folder and needs nothing else, not even the PDF.
   Use `deck_convert` unless you know you are that caller.)
5. **`deck_sync`** — every time after that. **Always `dry_run=True` first**, read the conflicts,
   then run it for real. A dry run writes a report and touches nothing.
6. **`deck_pull`** / **`deck_adopt`** — the other direction: fold the deck's edits back into the
   source, or write a source for a deck nobody converted. Both compile LaTeX in a loop and take
   minutes.

For Google Docs: `doc_push` once, then `doc_sync` forever. There is no rebuild — a `files.update`
would destroy every named range the merge depends on — so `doc_push` on an already-pushed file is
refused rather than repeated.

## Reading a result

Every tool returns the same shape and never raises:

```json
{"tool": "deck_sync", "ok": true, "summary": "…", "data": {…},
 "artifacts": [{"ref": "out/talk/sync/sync-report.md", "kind": "report"}],
 "diagnostics": [{"level": "conflict", "message": "…", "where": "slide 7"}],
 "next_steps": ["…"], "seconds": 12.4}
```

* `summary` is written for you to read; `data` is written for you to act on.
* `diagnostics` at level **conflict** mean the merge could not decide something. Do not report
  success while conflicts are open. Name them to the person, with the slide.
* `artifacts` are files that were produced, named relative to the workspace. A sync report and
  a pull's `edits.md` are meant to be read, not just mentioned. Where the harness asked for it,
  an artifact also carries its own content (`text` or `base64`), its size and its `sha256`; one
  marked `"truncated": true` was over the cap and has to be asked for by `ref`.
* `ok: false` always carries a `code`. Branch on the code, not on the words.
* A `deck_sync` that wrote says how to undo it: the `.pptx` of the deck as it was is an artifact
  of kind `pptx`, a Drive copy is `data["backup_copy"]`, and a way back that could not be made is
  a diagnostic at `where: "backup"`. Tell the person when there is none. Where the workspace goes
  away after the call, `backup="auto"` makes a Drive copy, since a file would go with it.

## Files: a name, or the file itself

Anywhere a tool takes a workspace ref for a file, it also takes the file. Three forms:

```json
{"pdf": "talks/2026/talk.pdf"}
{"pdf": "data:application/pdf;base64,JVBERi0xLjcK…"}
{"pdf": {"name": "talk.pdf", "base64": "JVBERi0xLjcK…"}}
```

Text content goes under `text` instead of `base64`. Give a `name` when you have one: it is what
you will see again in every artifact and every refusal, so `talk.pdf` reads better than `pdf.pdf`.

A **plain string is never content**. `"talk.pdf"` is a ref and
`"https://docs.google.com/presentation/d/…"` is a deck the journey resolves itself; neither is
fetched. A `{"url": …}` is fetched only if the operator gave this context a fetcher, and refused
by name if not - nothing here opens a socket to a host you chose.

## The refusals, and what each one wants

| code | what it means | what to do |
| --- | --- | --- |
| `deck_edited` | Someone edited the deck; a rebuild would destroy that. | `deck_sync`, or `new_deck=True`, or ask. Never force. |
| `no_base` | No record of what was last converted, so no three-way merge. | `deck_convert` creates one. Do not force a rebuild instead. |
| `base_mismatch` | There is a base, but it describes none of this deck's slides: another copy's, or a deck rebuilt outside sync. | Check that `deck` names the deck you meant; otherwise ask the person how the deck came to be. Converting would make a second deck, not fix this one. |
| `base_choice_needed` | A document with no base; one side must be assumed. | Ask the person which side. Guessing loses the other. |
| `already_pushed` | The file already names a document. | `doc_sync`. |
| `source_exists` | `deck_adopt` will not write over a source. | `deck_pull` refines an existing one. |
| `needs_consent` | Google sign-in expired or was never given. | Tell the person the command in `data`, once. It needs a browser; you cannot do it. Do not retry in a loop. |
| `no_credentials` | No OAuth client installed at all. | Point at `docs/install.md`. |
| `offline` | This workspace has no Google account at all. | Say so plainly. `tex_converge`, `tex_label` and `deck_inspect` still work. Nobody here can grant it; do not retry. |
| `forbidden` | There is an account, but this context does not allow what you asked. | The permission is the operator's to change, not the deck's. A `dry_run` of the same journey is usually still allowed. |
| `compile_failed` | LaTeX did not build the source. | Read the error in `data`; fix the source; retry. |
| `not_converged` | The loop ran out of iterations. | The residuals are in `data` and `edits.md`. Act on them or hand them over. A frame the loop made worse was already put back to its best text (`data["restored"]`, one note each): do not write that edit again by hand. |
| `outside_workspace` | A path climbed out of the workspace. | Use a path inside it. |
| `rate_limited` | Google said no for quota reasons. | Wait. Do not hammer. |

## What things cost

`deck_inspect` and `tex_label` are seconds and free. `deck_convert` is 12-20 seconds for a
normal talk and makes a deck. `deck_sync` is comparable and makes incremental edits.
`deck_pull`, `tex_converge` and `deck_adopt` compile LaTeX in a loop — minutes, and
`deck_adopt` additionally reads one thumbnail per slide with a back-off that can sleep a minute
per attempt. Say so before you start one; do not start two.

## Things that are true and not obvious

* A label written twice reaches the PDF as **no label at all** — hyperref keeps the first
  destination of a name and drops the second — so the second frame comes out looking unlabelled
  and nothing downstream of the PDF can tell the difference. Only `tex_label`, which reads the
  `.tex`, can see it.
* An **open comment** on a Google Doc lives in Drive, not in the document's content. Nothing the
  merge reads can see one, so a sync that rewrites the passage a comment hangs on answers it by
  accident. `doc_sync` names the open comments; pass them on before writing.
* `deck_sync` may **hold a slide back** (`data["held"]`): a frame label looks as if it moved onto
  another frame, so which frame that slide belongs to is an open question and nothing was written
  to it. The rest of the deck was synced; the slide keeps its base and catches up next time. This
  is a question for the person — ask them to check the `.tex`. Do **not** pass
  `follow_labels=True` to make it go through: that is a claim about what the author meant, and
  only the author can make it.
* A conflict is settled **for the deck** and reported; `take_source` on `deck_sync` is the one way
  to settle one the other way, and it is a decision the person makes, not you. Each conflict in the
  report carries an id; a person who reads the three versions and says "the source is right about
  that paragraph" gives you the id, and you pass it on. Never pick one yourself, never pass every
  id you were shown, and never pass one to make a report come back clean: the id names a place
  somebody wrote something, and what it writes over survives only in the report's `resolved`
  section. An id stops matching as soon as either side of its conflict moves, so it is read out of
  *this* run's report, not a stored one — a stale id settles nothing and says so in a warning.
* A deck **`deck_adopt` wrote a source for** can be synced back into, but not all of it. Nothing on
  a person's slide says which part of a source it came from, so the base is a *pairing* by place and
  words, and an element it could not tie to an object is kept as the deck has it — at every sync,
  for ever, since no sync ever gives such an element an object. `data["sync_base"]` says how many
  were tied, how many were not, and how many are drawn by the deck's **layouts or master** rather
  than by a slide (those are changed in Slides under Slide > Edit theme, and no amount of editing
  the source will move them). Tell the person the number before they start editing: a source edit
  to an untied element is not lost, but it does not arrive either — it comes back as a conflict.
* `deck_adopt` sets the deck in the deck's own fonts only when it has their files. A font it had
  to replace is in `data["fonts_missing"]` (the font, how many letters use it, what it was set in
  instead); its lines break in other places than the deck's. That is not a failure, but the fix is
  a file only the person has: ask for the fonts named there (.ttf, .otf, .ttc, .woff or .woff2,
  any file names) and adopt again with `fonts=[...]` into a new `tex` path. Do not look for the
  fonts on the web yourself. A file that could not be used is a warning at `where: "fonts"`.
* A picture the deck would not give (downloads refused, or a format LaTeX cannot include) is left
  out of the source, its frame marked with a `% picture left out` comment where it went. They are
  in `data["pictures_missing"]` (slide, alt text, why) and warnings at `where: "pictures"`: tell
  the person which slides lack a picture; never report such a source as complete. The fix is the
  deck as a .pptx the person downloads (File > Download > Microsoft PowerPoint): pass it as
  `pptx=` (a ref or the file as content) and adopt again into a new `tex` path. Its pictures need
  no download; `data["pptx_pictures"]` says how many of the deck's it held (0: another deck, or a
  deck changed since - ask for a fresh download).
* `deck_adopt` needs no Google and no network when the deck comes as files. Best: `deck=` the
  folder or `.zip` that `python -m beamer2slides deck-files --deck <id> --out <dir> --zip` saved
  where Google can be reached (a ref, or the .zip as content). It holds everything a live adopt
  reads, so the source is the one a live adopt writes. Each part can also come alone, with
  `deck=` a saved `presentations.get` `.json`:
  - `thumbnails=` for the fills the API does not report (gradients, table colours), the measured
    insets, and the page each frame is scored against;
  - `pictures=` and `google_fonts=` (the saved recordings) for the pictures' bytes and the
    deck's own typefaces;
  - `pptx=` for the pictures when there is no recording.

  `data["offline"]` lists each part: whether it was given, what it adds, and what its absence
  cost. Tell the person which parts were missing and what that cost. The sync base is recorded as
  usual.
* Slide order is merged, not taken wholesale: a slide a person dragged stays where they put it.
* A sync killed halfway loses nothing — the next one sweeps up — so a timeout is not a reason to
  force anything.
* After a successful `doc_sync` the file, the document and the base agree, and a second sync
  writes **zero** requests. That is the project's own definition of settled, and it is worth
  checking.
