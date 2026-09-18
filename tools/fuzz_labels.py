"""How often a moved label sends a slide's identity to the wrong frame, and how often the check
that catches it cries wolf (`identity.label_moves`, docs/sync.md "When a label moved").

The loss oracle cannot judge this: following a label onto the wrong frame deletes nothing and
loses no words, it writes one frame's content onto another frame's slide - and the person's edits,
which sync keeps, end up beside sentences they were never about. So this campaign asks a different
question, and it can ask it because it knows the answer: every frame in the synthetic source is
tagged, so for any conversion there is a right pairing and a wrong one.

    python tools/fuzz_labels.py --rounds 2000

Per round it makes a deck, applies a few plausible source edits, sometimes breaks the label
invariant (a label moved onto another frame, renamed, or dropped) and compares two pairings
against the truth:

    order       neither check: follow the label, and pair the rest in order alone
    before      the leftovers of that order picked up too (identity.cross_pairs, identity.gap_pairs)
    now         the same with the moved-label check as well (what sync does)

and counts what the check said about each round: `moved` (the content decided), `unsure` (the
label was followed and the report asks), or nothing. A false alarm is an `unsure` or `moved` in a
round whose labels nobody touched; a miss is a frame the `now` pairing still gets wrong.

Rounds that moved a frame are counted apart, because a frame that crossed another has a second way
of losing its identity: the alignment keeps the order, so one of the two falls out of it.
`identity.cross_pairs` picks such leftovers up when the content is unmistakable (this campaign is
what measured that: 5.53% -> 0.00% of the frames in sound rounds with a move), and what is left in
these rows is the honest remainder - frames that moved and say too little to be told apart, which
come back as new slides.

The other leftover is a frame nothing moved whose title the source replaced: no label, half its
words new, and both passes above too careful to claim it. `identity.gap_pairs` pairs it when it is
the only slide and the only frame between two neighbours that paired - which took the rounds with
sound labels from 0.06% to 0.00%, i.e. every frame of 1524 rounds on its own slide.
"""

import argparse
import contextlib
import copy
import random
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tools")]

import fuzz_world as W                                       # noqa: E402
from beamer2slides import identity                           # noqa: E402
from fuzz_sync import SOURCE_OPS                             # noqa: E402

LABEL_OPS = ["move_label", "rename_label", "drop_label"]
PLAIN_OPS = sorted(set(SOURCE_OPS) - set(LABEL_OPS))


def _infos(base):
    return [{"label": b.get("label"), "title": b.get("title") or "", "text": b.get("text") or "", "page": b["page"]}
            for b in base["slides"]]


@contextlib.contextmanager
def _order_only():
    """The alignment without the leftover passes (`identity.cross_pairs`, `identity.gap_pairs`):
    what the labels and the order-keeping alignment pair between them."""
    sure, gap = identity.CROSS_SURE, identity.GAP_SURE
    identity.CROSS_SURE = identity.GAP_SURE = float("inf")
    try:
        yield
    finally:
        identity.CROSS_SURE, identity.GAP_SURE = sure, gap


def _wrong(pairs, ours_truth, base_truth) -> int:
    """Frames the pairing reads as another frame, or as new when the base knows them."""
    wrong = 0
    for j, truth in enumerate(ours_truth):
        want = base_truth.index(truth) if truth in base_truth else None
        if pairs.get(j) != want:
            wrong += 1
    return wrong


def round_once(seed: int, label_chance: float, tmp: Path, chain: int = 1) -> list[dict]:
    """One deck, `chain` revisions of it in a row. Each revision is measured against the one before
    it - which is what sync does, and what a talk revised over a term looks like: the second version
    is not edited from the pristine deck but from a source already reworded, reordered and retitled,
    with titles that carry two rounds of amendments and frames whose text has drifted twice."""
    rng = random.Random(seed)
    doc = W.make_doc(rng, tmp)
    for i, s in enumerate(doc["slides"]):
        s["truth"] = f"t{i}"                      # what frame this really is, whatever its label
    fresh = len(doc["slides"])
    steps = []
    for step in range(chain):
        base = W.build_base(doc, tmp)
        base_truth = [s["truth"] for s in doc["slides"]]

        doc2 = copy.deepcopy(doc)
        revision = rng.random() < 0.25   # the whole talk revised at once, every title amended
        ops = [rng.choice(PLAIN_OPS) for _ in range(rng.randint(0, 3))]
        broke = rng.random() < label_chance
        if broke:
            ops.insert(rng.randint(0, len(ops)), rng.choice(LABEL_OPS))
        done = []
        for k, name in enumerate(ops):
            fn = SOURCE_OPS[name]
            r = random.Random(seed * 7919 + 101 * step + k)
            got = fn(r, doc2, tmp) if name == "repaint" else fn(r, doc2)
            done.append(f"{name}: {got}")
            if got is None and name in LABEL_OPS:
                broke = False                      # nothing to move: the invariant still holds
        if revision:
            for s in doc2["slides"]:
                s["title"] += " v2"
                s["elements"][0]["paragraphs"] = [{**s["elements"][0]["paragraphs"][0], "runs": [W.run(s["title"])]}]
            done.append("revision: every title amended")
        ours_truth = [s.get("truth") for s in doc2["slides"]]

        base_infos, infos = _infos(base), [W.slide_info(s) for s in doc2["slides"]]
        moves = identity.label_moves(base_infos, infos)
        with _order_only():
            order = identity.align_slides(base_infos, infos, moves=[])
        pairings = {"order": order,
                    "before": identity.align_slides(base_infos, infos, moves=[]),
                    "now": identity.align_slides(base_infos, infos, moves)}
        said = "moved" if any(m["verdict"] == "moved" for m in moves) else ("unsure" if moves else "quiet")
        steps.append({"seed": seed, "step": step, "broke": broke, "said": said, "ops": done,
                      "reordered": any(line.startswith("move_slide") and not line.endswith("None") for line in done),
                      "wrong": {k: _wrong(p, ours_truth, base_truth) for k, p in pairings.items()},
                      "frames": len(ours_truth)})
        # A frame the source wrote in this revision is a frame in its own right for the next one.
        for s in doc2["slides"]:
            if "truth" not in s:
                s["truth"] = f"t{fresh}"
                fresh += 1
        doc = doc2
    return steps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--label-chance", type=float, default=0.5, help="rounds that break the invariant")
    ap.add_argument("--show", type=int, default=5, help="worst rounds to print")
    ap.add_argument("--chain", type=int, default=1, help="revisions per round, each measured against the last")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="b2s-labels-"))
    tally: Counter = Counter()
    frames = Counter()
    worst = []
    try:
        for n in range(args.rounds):
            for r in round_once(args.seed + n, args.label_chance, tmp, args.chain):
                group = ("broken" if r["broke"] else "sound") + (", frame moved" if r["reordered"] else "")
                tally[f"{group}/rounds"] += 1
                tally[f"{group}/said:{r['said']}"] += 1
                for how, w in r["wrong"].items():
                    frames[f"{group}/{how}"] += w
                frames[f"{group}/frames"] += r["frames"]
                if r["wrong"]["now"]:
                    tally[f"{group}/{'silent' if r['said'] == 'quiet' else 'said so'}"] += 1
                if r["wrong"]["now"] > r["wrong"]["before"]:
                    tally[f"{group}/worse"] += 1
                if r["wrong"]["now"] or (not r["broke"] and r["said"] != "quiet"):
                    worst.append(r)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for group in ("sound", "sound, frame moved", "broken", "broken, frame moved"):
        rounds = tally[f"{group}/rounds"]
        if not rounds:
            continue
        print(f"{group}: {rounds} rounds, {frames[f'{group}/frames']} frames")
        for how in ("order", "before", "now"):
            w = frames[f"{group}/{how}"]
            print(f"  {how:6} misidentified {w:5} frames ({100 * w / max(1, frames[f'{group}/frames']):.2f}%)")
        print("  said " + ", ".join(f"{s} in {tally[f'{group}/said:{s}']}" for s in ("moved", "unsure", "quiet")))
        print(f"  of the rounds still wrong, {tally[f'{group}/said so']} were reported and "
              f"{tally[f'{group}/silent']} passed in silence; {tally[f'{group}/worse']} came out worse than before")
    if worst:
        print(f"\n{len(worst)} round(s) left wrong or noisy:")
        for r in worst[:args.show]:
            print(f"  seed {r['seed']} step {r['step']} broke={r['broke']} said={r['said']} wrong={r['wrong']}")
            for line in r["ops"]:
                print(f"      {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
