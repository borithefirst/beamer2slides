# Frame labels: the identity of a slide

A converted deck and its beamer source drift apart the moment somebody edits either one. `sync`
puts them back together by working out which slide in the deck is which frame in the source. That
question has exactly one reliable answer, and it has to survive compiling to a PDF.

## What survives

```latex
\begin{frame}[label=intro]{Why decks diverge}
```

beamer turns `label=intro` into a **PDF named destination** `intro` on the frame's first page, and
`intro<n>` on its n-th overlay step. That is how `\hyperlink{intro}` works, and it is the only
thing about a frame that comes through the compiler intact - the title can be rewritten, the text
replaced, the frame moved to another file.

The chain:

| where | what |
|---|---|
| `main.tex` | `\begin{frame}[label=intro]` |
| `intro.pdf` | named destination `intro`, and `intro<2>`, `intro<3>`, ... per step |
| `extract.frame_labels` | page index -> `intro` |
| `identity.slide_keys` | the slide's key **is** `intro` |
| `<out>/sync/base.json`, Drive `appProperties.b2sBase` | the key, recorded |
| the deck | every object's alt-text title `b2s:intro/<element>` |

The label never appears in Slides as text. Nobody editing the deck can see it or break it.
**The only place it can break is the `.tex`.**

## What happens without one

A frame with no label falls back to its title, its occurrence among frames with that title, and an
order-keeping alignment against the base. That works for a deck whose titles are distinct and whose
frames stay put. It gets less reliable exactly where decks get interesting: repeated titles
("Results" three times), frames reordered, a frame inserted between two that look alike, a title
rewritten in the same commit that moves the frame.

What the order leaves over is picked up by the content (`identity.cross_pairs`: a frame that moved
still says what it says) and by the place (`identity.gap_pairs`: the only slide and the only frame
between two neighbours that paired), which covers a good deal of that - measurably, `docs/sync.md`
has the numbers. Neither will guess: two frames that changed places and say much the same thing
come back as new slides, because the alternative is writing one frame's words onto the other's
slide, under somebody's edits. And when the place is all that was left to go on, the report says so
in a warning naming the slide: read it as "this one is down to its last hook, give it a label".
So does a match that was a coin toss: where an unlabelled frame sits between slides that say nearly
the same thing, another alignment of the same score would have put it on one of the others, and
nothing downstream can tell that from a match the words really made. The frame is paired - your
edits are safe either way - and the report names the slide it went to and asks for a label, because
which slide that frame writes to next time is otherwise decided by a walk, not by the source. A
deck a person built in Slides is full of such near-twins, which is why `adopt` labels every frame
it writes (a slug of the slide's own objectId).
When even that is gone - a frame retitled, reworded and moved in one version - the report names the
two halves it could not join: the slide the deck still has, and the frame that came back as new.

So: **give every frame a label of its own, and never change one.**

## Keeping that true

`python -m beamer2slides label main.tex --apply` writes a label into every frame that has none,
from its title (`Why decks diverge` -> `label=why-decks-diverge`), unique within the document. It
reads the source only - no compilation - and follows `\input`. Existing labels are **never**
touched, renamed or moved: each one is a promise to a deck that was converted from it. What was
there is kept as `main.tex.bak` (then `.bak2`, ...), the same way `pull --apply` does it.

Duplicates are reported, never resolved. If two frames carry `label=results`, no program can know
which of them the deck's slide came from; only the author can.

**A label written twice is worse than a label never written, and `label` is the only thing that can
see it.** hyperref keeps the first destination of a name and drops the second, so a PDF with
`label=results` on two frames carries exactly one `results`, on the first of them - the second frame
comes out of `extract.frame_labels` with no label at all and falls back to its title and position.
Nothing downstream of the PDF can tell that apart from a frame the author never labelled, and
nothing pretends to: `identity` never sees two slides sharing a label except the overlay steps of
one frame, and `--check-labels` reports the second frame as *unlabelled*, with a line saying that a
label another frame already uses is one of the ways a frame ends up there. Measured on the stress
deck's `duplabel` variant (`tests/test_stress_live.py::test_a_label_written_twice_reaches_the_pdf_
as_no_label_at_all`), which exists to catch the day a PDF writer stops dropping the duplicate.

`convert --check-labels warn|error|off` (default `warn`) says what the PDF it just converted will
cost a later sync: how many frames have no label, and which labels its slides do not line up with.
`error` refuses the conversion before anything is written to Drive.

## If a label does change

Sooner or later one will: a rename, a copy-paste, a frame split in two. Then the deck's slide
`intro` and the source's frame `intro` are different frames, and following the label would carry
the person's edits onto a slide they never touched.

Sync notices (`identity.label_moves`). When the words on both sides agree that the label moved, the
content decides instead of the label - the person's edits stay with the words they edited. When
only half the story fits, the label is followed and nothing is re-paired. Either way it is a
**conflict** in `sync-report.md`, addressed to whoever maintains the source, because which frame is
which is not something a program should guess. A label renamed or dropped where the content still
recognises the frame is a warning instead: nothing was at risk this time.

`docs/sync.md`, "When a label moved", has the rules and what they measure out at.

## For an AI writing the source

If the `.tex` is maintained by an AI, the rules above are invariants it has to keep. They are
written out for that purpose in [ai-authoring.md](ai-authoring.md), in a form that can be pasted
into a prompt.
