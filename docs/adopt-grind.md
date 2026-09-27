# The adopt fidelity grind

A loop that keeps finding what `adopt` gets wrong on decks other people made, and measures what
each fix bought and what each round cost. Driver: `devtools/grind.py`. Everything it writes is
under `out/grind/`, which is git-ignored because the corpora are other people's decks.

## The path under test is the offline one

Most decks will reach adopt as files (`deck-files`, docs/agent-tools.md "Everything, preloaded"):
a sandbox with no network and no fonts installed. The bench used to adopt from its own capture
format, with this machine's fonts. So every round now goes through the sandbox's path:

- `grind files` turns each bench capture into a `deck-files/` folder beside it:
  - the presentation and thumbnails;
  - the capture's pictures, recorded by URL;
  - the google/fonts files adopt fetches when it may look at no installed font
    (`deck_files.record`, the same code `deck-files` runs).
- `adopt_bench run --offline` reads that folder inside `adopt_bench.sandbox`:
  - the recordings answer every download, and anything else is refused;
  - `adopt.no_machine_fonts()` is on;
  - the google/fonts cache is filled only from the recordings.

A slide that scores lower offline than with the machine's fonts is a sandbox defect.

The first such defect was plain-fonts' Arabic, which came out as boxes. The script fallback
chain (`scripts.FALLBACKS`) named only fonts a machine has. `scripts.FETCHABLE` now gives each
script a google/fonts face, fetched only when nothing installed covers the letters.

## One round

```
grind round TAG [--gpu --python out\metrics-venv\Scripts\python.exe]
```

Each stage is timed into `out/grind/ledger.jsonl`:

1. **bench**, per corpus (`out/adopt-corpus`, `out/adopt-hunt`): `adopt_bench run --offline
   --tag TAG`. Results are cached by source tree and IR hash, so a fix that leaves a deck's
   source alone costs that deck no compile.
2. **metrics**: `slide_metrics run TAG` measures every slide against Google's thumbnail. The
   numpy metrics always run; the torch ones (OT, LPIPS, DINOv2) run with `--gpu`.
3. **show**: the worst-slides page (`slide_metrics.gallery`).
   - Severity is the sum of the `SEVERITY` metrics over their judged-identical 95th
     percentiles, each capped at 10.
   - The list shows at most 2 slides per deck and marks slides new since the last page.

## Judge, trace, fix

After a round:

1. **Judge.** Cheap blind judges (Haiku) see the worst new slides' sheets and write verdicts:
   - format: `out/grind/judging/<round>/verdict-*.json`, in hunt0's shape;
   - categories: background, shape, line_breaks, text_size, text_colour, font_face, font_style,
     text_position, text_wrong, text_missing, picture_missing, picture_wrong, overlap, bullets,
     other.

   Only sheets whose metrics moved are judged again.
2. **Trace.** A stronger model (Sonnet) traces the families that recur across decks to a
   mechanism: file, function and the IR field. A skeptic reruns the claim on the slide
   (`slide_metrics show DECK:N TAG`) before anyone fixes it.
3. **Fix.** The main session fixes it, with a test pinned on a synthetic page where possible. It
   then runs `grind round NEWTAG DECK...` on the decks concerned, `slide_metrics compare OLD NEW`,
   the offline suite, and commits.

Ideas for what to test next come from:
- past families (docs/adopt-bench.md, `hunt0/findings.json`);
- offline-only risks, i.e. anything that reads a machine font;
- new public decks found online (`out/grind/candidates.json`, captured with `deck-files` then
  `adopt_bench capture`), used once the known families run dry.

## Costs are measured

`grind log STAGE --tokens N --seconds S --agents K --model M` records work done outside the
driver: judge batches, analysts. `grind ledger` sums time and tokens by stage and by round.

Levers already pulled:
- per-deck result caches;
- judging only sheets that moved;
- Haiku for looking, Sonnet for tracing;
- torch metrics only with `--gpu`.

## Showing the worst slides

`grind show` regenerates the page for the newest round against the last page. It prints the
flagged count and what came onto and left the list. The page is a self-contained HTML file for
looking at locally, never published: the decks are not ours to republish.
