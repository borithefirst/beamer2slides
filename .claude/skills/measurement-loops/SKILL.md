---
name: measurement-loops
description: How to run, judge and speed up a measure-and-improve loop in this repo - fuzz campaigns (fuzz_docs, fuzz_sync, fuzz_labels), tortures (render_torture*, truetype_torture), benchmarks (adopt_bench, agent_bench, pure_bench, readability, edit_robustness), sweeps (refusal_sweep, themes/sweep.py), the offline suite, and any perf work. Load this before starting or extending such a loop, before optimising anything for speed, before adding or changing a check, oracle, judge or invariant, and whenever asking "did that number really move?" or "is this measurement measuring what I think it is?". Covers: what to know before the first run, when it pays to make the loop faster instead of running it, proving a measurement can see the thing it is for, and keeping the loop cheap in wall clock and in tokens - including when to spend a smaller model, a larger one, or none at all, and how to check the cheaper choice held the quality bar.
---

# Measurement loops

Most of the work in this repo is a loop: run something over many inputs, ask a judge
whether the answer is right, change the code, run it again. The loop is not overhead
around the work - **it is the instrument**, and a blunt instrument costs more than the
time it takes, because it also reports success.

Four numbers come before the first run. Say them out loud; guessing them is how a
campaign runs all night and proves nothing.

1. **Cost per iteration** - seconds, and whether it is CPU, a compile, or a Google call.
2. **Iterations to a signal** - at what rate does this loop find anything? `fuzz_docs`
   is ~150 rounds/s at chain 1 and ~20 at chain 4; defects arrive at 1 in 200 to 1 in
   1,200 rounds. That arithmetic says a run must be hundreds of rounds or it is theatre.
3. **What decides** - which judge fails the run, and what a failure will hand back
   (a seed? a shrunk reproduction? a number?).
4. **What it cannot see** - every loop is blind to something. Name it before it bites.

## Spending time on the loop instead of on the work

If the loop will run `N` more times at `T` each, an optimisation costing `C` pays back
when `C < N x saved`. `N` is almost always larger than it feels, because the loop runs
again on every fix and every doubt. In this repo the payoffs were:

- `-n 12 --dist loadgroup` on the offline suite: 66 s -> 24 s, and that suite runs
  dozens of times a day. Worth hours of setup; it took less.
- `adopt_bench`'s per-deck cache, keyed on the source tree, the IR and the scoring code
  (`cache_key`): a change that leaves a deck's source alone costs that deck no compile.
  Without it, every bench run recompiles 29 decks to re-measure two.
- `fuzz_docs --shape`: a round draws one of fourteen shapes, so a defect needing two
  tables waits for the dice. 800 `two_tables` rounds at chain 8 found three things that
  1,600 mixed rounds at the same depths had found none of.
- `collide` - a source op that changes exactly what the reader just changed. Drawing
  both sides at random took text overrides from 5 in 200 rounds to 56, and tables from
  0 to 10. The cheapest speed-up is usually not a faster round, it is a round that
  aims.

**When a run will take more than a few minutes, stop and cost the loop first.** A slow
loop is also a loop nobody reruns after a fix, which is worse than its clock.

## Can the measurement see the thing it is for?

A judge nobody has seen fail is not a judge. Every check in this repo was verified by
**breaking its mechanism on purpose** and watching the check catch it - `checks.py`
says so for each invariant, and `tests/test_doc_fuzz.py` pins each defect with the
minimal reproduction that failed before the fix. Do the same, always:

- **Break it in memory and rerun.** "With the fix reverted, 6 of the first 40 `themed`
  seeds fail" is a measurement. "The check looks right" is not.
- **To measure a rule's worth, run the campaign twice over the same seeds with only
  that rule swapped.** Everything else held: the same draws, the same depth. That is
  how `_moved_bar` was shown to take misidentified frames 311 -> 109, and how a rule
  that moved nothing (`identity` one-sided exactness) was still kept, because the
  *reporting* improved.
- **Count what the run actually reached.** `fuzz_docs.coverage()` prints which shapes,
  ops, request kinds and outcomes a campaign touched. A campaign that never plans an
  `insertTable` proves nothing about tables, and only counting says so. This is how the
  whole styling dialect, every column request, and the bullet button were each found to
  be undrawn after months of green runs.
- **One judge is one question.** The loss oracle asks whether the *reader's* work
  survived; convergence asks only that the round settles, and is satisfied by any
  self-consistent reading. A merge can be wrong and stable at once - that is why
  `_arrived` judges exist (did the source's change actually arrive?). When a loop goes
  green for a long time, suspect the question, not the code.
- **A forgiveness must expire.** An oracle that forgives what no longer happens is a
  blind spot waiting. `_twin_unmarks` was retired once 3,750 rounds found nothing it
  excused; `_dressed_up` went when the harness bug it was written for was fixed. Before
  retiring one, run enough rounds to show it forgives nothing, and check it against the
  regression seeds.
- **A spurious note hides the next real one.** A warning that fires wrongly is not
  harmless noise: judges forgive what the report names, so a false note is a hole.

### The KNOWN list rule

A defect that is found and not yet fixed is listed (`fuzz_docs.KNOWN`), so the hunt can
go past it. **A fixed entry comes straight out.** An entry is let through, so leaving a
fixed one in means it silently swallows the next defect with the same symptom - and a
signature says which *symptom* was seen, never which defect caused it. `--strict` fails
on the known ones again, which is how a fix is checked. The tuple being empty is the
goal, not an accident.

## Measuring speed without lying

`pure_bench`'s docstring is the reference. The traps, all of them paid for here:

- **Use `time.process_time`, not the wall clock.** A second heavy job on this machine
  moved a wall-clock total 35%, more than any optimisation was worth.
- **Report the minimum of several repeats**, not the mean - under unrelated load the
  minimum is the only robust statistic.
- **A/B interleaved**, alternating the two versions back to back in the same rounds.
  Never a run today against a run an hour ago: the same unchanged code measured 2484 ms
  and 1922 ms within the hour, ~8% drift from frequency scaling and cache state alone.
- **Know the clock granularity.** PDFium's total is ~230 ms against a 15.6 ms clock:
  read that ratio to one significant digit, or not at all.
- Benches in this repo take `--baseline f.json` for exactly this - the same decks, the
  same repeats, one number per deck plus a total.

## Making a failure cheap to understand

- **Shrink.** `fuzz_docs.shrink` drops steps and ops while the failure stands; each op
  carries its own salt so the rest draw what they drew. A three-op reproduction is worth
  a hundred rounds of staring. When shrinking, pass `still=` so a reduction cannot trade
  the unknown finding for a `KNOWN` one - otherwise it shrinks the new defect away and
  prints a reproduction of an old one.
- **Replay by seed.** Every campaign prints `--replay <seed> --chain <n>`. Use it
  before reading any code.
- **Pin it.** A found seed goes into `tests/test_doc_fuzz.py` / `test_sync_fuzz.py` as a
  fixed seed or a hand-built test, so the fix cannot rot. Hand-built when the seed is
  thin (1 of 200 rounds) or shape-specific.

## Cost in wall clock

- **Run independent things at once** - benches with `--jobs`, agents in their own
  worktrees with disjoint file ownership, several campaign settings in parallel. The
  offline suite flakes under a saturated CPU: re-run a named test alone before
  believing its failure.
- **Long runs go in the background** and are collected, not watched.
- **Read the tool's docstring, not its source.** Every loop here opens with its
  invocations and what it measures; that is usually the whole answer.

## Cost in tokens

Where a model is in the loop, **tokens are a per-iteration cost like seconds and are
measured the same way**: tokens per iteration x iterations, before the run, not after
the bill. An agent loop run a thousand times is a budget decision, and one that is
never measured is one nobody is allowed to optimise.

Three levers, in the order they are worth trying.

### 1. Take the model out of the loop

The strongest saving is not a cheaper model, it is no model. This repo's campaigns run
at 20-150 rounds/s and its benchmark grades twenty policies in a second **because
nothing calls a model**: `agent_bench` says so as a property (`Scripted` proves the
graders, `Recorded` scores a transcript made elsewhere), the fuzz world applies real
requests deterministically, and the oracles are code. A deterministic judge is cheaper,
reproducible, shrinkable and replayable by seed - an LLM judge is none of those.

Spend a model only where the judgement is irreducible, and split the loop so that part
is small: the deterministic harness does the setup, the replay and the grading, and the
model does the one decision. `agent_bench`'s replay tier is exactly this shape.

### 2. Measure whether a smaller model holds the quality bar

Model size is a parameter, and it gets the same treatment as any other: **swap only it,
hold the seeds, compare against a bar you can actually check.** Do not assume the
downgrade costs quality, and do not assume it is free.

The apparatus is here and the loop is closed. `agent_bench` grades a run on pass rate,
HARM (a task failed in a way that would have destroyed work) **and cost**: a `Recorded`
transcript carries `model` and `usage`, which `summarise` totals per task and for the
suite, so the table has one row per task with calls, harm and tokens on it. Run the
same tasks on a cheaper model in any harness, score the transcripts here with
`--policy recorded:DIR`, and read the trade off one table.

    agent_bench bundle <id>                     # what the outside harness must produce
    agent_bench run --tier all --tag small --policy recorded:runs/small
    agent_bench report --tag small

Take the saving when the pass rate holds **and HARM stays 0**. A cheaper model that
passes as often but harms once is not cheaper - that is why the two are never averaged
together. Two properties of the column worth keeping when you build the next one: a
task nobody priced reads `-` and never 0, because an unmeasured suite must not look
free; and the total says how many tasks it covers, so a figure measured on 3 of 20
cannot be quoted as the bill.

For subagents, `Agent(model: haiku|sonnet|opus)` is per-spawn, so the split can be by
job: a broad read-only sweep on a small model, the judgement that decides on a large
one. Measure the cheap one against the expensive one on the same task before fanning
out a hundred of them.

### 3. Trim what is re-sent

If quality will not hold, the cost is in the inputs, and most of it is re-sent every
iteration:

- **Point at paths, do not paste files.** A brief naming `devtools/fuzz_docs.py` and the
  three functions that matter beats the file's contents, and the agent reads only what
  it needs.
- **Carry a summary, not a transcript.** Round N does not need rounds 1..N-1 verbatim -
  it needs what they concluded. This is the same discipline as printing `coverage()`
  instead of every round.
- **Never let raw output in.** A `Get-ChildItem -Recurse` in this repo dumped 7.1 MB
  because of `.claude/worktrees/`. Write results to a file and read the summary:
  campaigns print coverage and a count, benches have `report --tag T` and
  `losses --tag T`, sweeps are resumable one process per slice (`refusal_sweep`).
- **Keep the stable part stable.** Prompt caching pays only while the prefix does not
  move, so put what changes at the end and stop editing what does not.
- **Count the calls.** `agent_bench` reports `calls` and `redundant` per task for this
  reason - a redundant call is tokens spent on something already known, and it is the
  first thing to cut.

## When it is done

Write the numbers down where the next person meets them: `docs/adopt-bench.md` and
`docs/agent-bench.md` for bench history, CLAUDE.md for what a campaign proved and at
what settings. A result without its settings - rounds, chain depth, shape, seed - is
not a result. Say what was run, what it found, and what it still cannot see.
