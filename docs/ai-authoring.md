# Writing and repairing beamer source that stays in sync with a deck

A guide for an AI that maintains the `.tex` of a talk which is also a live Google Slides deck.
Paste the section below into a prompt, or hand it to the model as a file.

The situation: the `.tex` is compiled to a PDF and converted to a Slides deck. People then edit the
deck in Slides *and* the source keeps changing. `beamer2slides sync` merges the two, and **deck
edits win**. For that merge to know which slide is which frame, the source has to keep a few
invariants. Breaking them does not produce an error at compile time - it produces a deck where
somebody's work has quietly moved to the wrong slide, or come back as a duplicate.

---

## The rules

**1. Every frame carries a label of its own.**

```latex
\begin{frame}[label=why-decks-diverge]{Why decks diverge}
```

Not optional for any frame that should keep its identity across versions. A frame with no label is
matched by its title and position, which fails as soon as titles repeat or frames move.

**2. Never change, move or reuse an existing label.**

A label is a promise to a deck that was already converted from it. Changing `label=intro` to
`label=introduction`, or moving `label=intro` onto the next frame, tells sync that a frame it knows
is a different frame. Rewrite the title, the bullets, the whole body - the label stays.

**3. A new frame gets a new label.** Never one that any frame has ever had in this document,
including labels of frames that were deleted. Derive it from the new frame's title.

A label you use twice is the one mistake in this list that nothing downstream will catch for you.
hyperref writes the first frame's destination and silently drops the second, so the PDF carries the
name once: the second frame arrives with **no label at all**, and sync treats it as a frame you
never labelled. `python -m beamer2slides label main.tex` reads the `.tex` and is the only thing that
can see it - run it before you hand the source back (docs/labels.md).

**4. Splitting a frame: the original keeps its label**, the new part gets a new one. Pick whichever
half keeps most of the original's content to keep the old label - that is the frame the deck's
slide corresponds to.

**5. Merging two frames: keep one label, drop the other.** Say which in your reply; the slide whose
label disappeared will be reported by sync as removed from the source, and its deck edits are kept.

**6. Deleting a frame: delete its label with it.** Do not hand it on to a neighbour.

**7. Labels are `[a-z0-9-]`.** They go into an option list split on commas and brackets, and into a
PDF destination name. No spaces, no braces, no accents, no `=` or `,`.

**8. Do not rename `\label{}`, `\ref{}` or citation keys as a way of doing any of this.** Frame
labels are `[label=...]` in the frame's option list; that is the only thing this system reads.

**9. On an unlabelled frame, don't replace the title and rewrite the body in the same version.**

That combination is the one edit that can cost a frame its identity: with no label, a new title and
new words, nothing on either side says it is the same frame. Sync still gets it right when the
frame stayed where it was and kept some of its sentences (it is then the only slide and the only
frame between two neighbours it recognises), but a frame that also moved, or that kept nothing, is
read as a new one - the old slide stays with whatever the person wrote on it, next to the new one.
Give the frame a label (rule 1) and none of this applies; if you cannot, make the two changes in
two versions, so the first one is always recognisable by what the second one has not touched yet.

**10. Reorder frames freely - with rule 1 kept.** A labelled frame is recognised wherever it lands.
Frames without labels are matched by an order-keeping alignment, so two of them changing places is
the one case the content has to settle on its own; it does when they say clearly different things,
and when they don't (three frames called "Results" over the same table) both come back as new
slides and their deck edits stay on the old ones. Move the frame and its label together, and never
swap two frames by swapping their labels - that is rule 2, and it reads as two frames rewritten.

---

## Repairing a source that broke them

Run `python -m beamer2slides label main.tex` (without `--apply`) to see the state. It lists frames
with no label and labels on more than one frame; `--apply` writes labels into the unlabelled ones
and touches nothing else.

- **Frames without labels.** Let the tool write them, or write them yourself from the titles. Do
  this *before* the next sync, not after.
- **A duplicate label.** Only you can fix this: decide which frame keeps it, and give the other a
  new one. If a deck was already converted, the frame that keeps the label should be the one whose
  content matches the deck's slide, or a person's edits will follow the label to the wrong place.
- **A label you already changed.** Change it back if the old value is known (git history, the deck's
  `<out>/sync/base.json`). If it is not known, treat it as a new frame and say so - sync will report
  the old slide as removed from the source, and the deck keeps it.

---

## When sync reports a conflict

`sync` writes `<out>/sync/sync-report.md`. Conflicts there are the places where the source and the
deck both changed the same thing, and the deck won. They are addressed to you.

A conflict looks like:

```
- `backup` / `text/body/0`: **text**, deck kept
  - base:   "The numbers are rounded to whole seconds"
  - ours:   "Seconds are rounded, milliseconds are dropped"
  - theirs: "The numbers are rounded to full seconds"
```

`base` is what the converter last wrote, `ours` is what the source now says, `theirs` is what the
deck shows. The deck's version is what people see. Your job is to make the source say what the deck
says, so the two stop disagreeing - the same thing you would do resolving a three-way merge, except
that one side has already won.

Usually this means rewriting that bullet in the `.tex` to the deck's wording, keeping whatever your
version was actually trying to add. Do not simply revert your change if it carried real
information; fold it into the deck's sentence. If you genuinely disagree with the deck's edit, say
so in your reply rather than overwriting it - a person made that edit on purpose.

`beamer2slides pull --tex main.tex --apply` does the mechanical part of this for you: it edits the
source until its conversion matches the deck, and writes what it could not resolve into
`<out>/pull/edits.md`, each item with the frame's `file:line`. Those leftovers are the ones worth
your attention.

A conflict about **identity** is different. It looks like this:

```
- `mobile` / **label**, the label moved: identity taken from the content instead
  - base:   the slide `mobile` says "Moving labels", which the source now has under "Arriving labels"
  - ours:   the frame `mobile` now says "Arriving labels", which is what the slide `arriving` says
```

Sync is telling you that `[label=mobile]` is not on the frame this deck's slide was made from. It
has already done the safe thing - either it went by the content (`the label moved`) or, where only
half the story fit, it wrote **nothing at all** to that slide (`nothing written to this slide`) and
synced the rest of the deck - and it is asking you to put the source right so the next sync has
nothing to work out. A slide held back is not a slide that failed: it is waiting, it keeps the base
it had, and the next sync writes it once the labels say which frame it is.

**Do not guess, and do not resolve it by moving the label again.** Open the `.tex`, find the two
frames named in the conflict, and either put the label back on the frame whose words the deck's
slide shows, or say in your reply that the two frames genuinely changed places and let a person
confirm. Everything else in this system is recoverable; a wrong identity silently moves somebody's
work onto another slide.

**Never reach for `--follow-labels`** to make a held slide go through. It says "I have read the
source and the labels are right", which is a claim about the author's intent, and only the person
can make it. Your job is to fix the `.tex` or to report what you found.

A **warning** about a label (renamed, or gone) means the content recognised the frame anyway, so
nothing was at risk - but put the label back to what it was, or the next edit has nothing to fall
back on. Two more warnings say the same thing about a frame with no label at all:

- *"matched by where it stands, between the frames around it"* - the frame kept its slide, but only
  its neighbours identified it. Give it a label (rule 1) before you touch it again.
- *"the source has no frame this slide could be matched to, and the frame X is new - but the two say
  much of the same thing"* - sync could not follow that frame at all and made a new slide for it,
  and the old one is still in the deck. Read the two and decide: if they are one frame, say so in
  your reply (a person may want to move the edits over) and give it a label now; if they are not,
  there is nothing to do.

---

## Checklist before handing back a changed source

- [ ] every `\begin{frame}` has a `label=` in its options (and if one still has none, it did not
      get a new title and a new body in this same version - rule 9)
- [ ] no label appears twice (`python -m beamer2slides label main.tex` says so)
- [ ] no label that existed before has a different value now
- [ ] new frames have labels that have never been used in this document
- [ ] it compiles
