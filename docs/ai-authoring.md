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

A conflict about **identity** - a label that moved, a frame that cannot be matched - is different:
do not guess. Report what you see and let a person say which frame is which. Everything else in
this system is recoverable; a wrong identity silently moves somebody's work onto another slide.

---

## Checklist before handing back a changed source

- [ ] every `\begin{frame}` has a `label=` in its options
- [ ] no label appears twice (`python -m beamer2slides label main.tex` says so)
- [ ] no label that existed before has a different value now
- [ ] new frames have labels that have never been used in this document
- [ ] it compiles
