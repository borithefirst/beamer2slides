"""Invariants over every built test PDF (tests/decks/out, last overlay step of each frame, and
tests/themes/out/*/talk.pdf), local only: extract, classify and render once per PDF, then the
checks of `beamer2slides.checks`:

- stray_ink: no graphic left in the background next to or under native words, bullets or the
  pictures anchored to text (they drift apart in Slides);
- stray_labels: no item label left in the background beside a paragraph without a bullet;
- lost_ink: everything that left the background is carried by text, a bullet, a picture, a
  table, a diagram, a decoration or a shape;
- structure: holes, number balls, overlays, decorations, shape bullets and references agree;
- junk_text: no private-use, replacement, picture-font or control characters, no lone
  combining marks.

Known problems are listed in tests/invariants_allow.json (deck, 1-based page, check, element
and/or bbox, reason); an entry that no longer matches anything fails too, so the list stays short.
"""

import json
from pathlib import Path

import pytest

from beamer2slides import checks

HERE = Path(__file__).resolve().parent
PDFS = sorted(p for p in (HERE / "decks" / "out").glob("*.pdf") if not p.stem.endswith("-handout")) + \
       sorted((HERE / "themes" / "out").glob("*/talk.pdf"))
ALLOW = json.loads((HERE / "invariants_allow.json").read_text(encoding="utf-8"))
CASES = [(pdf, check) for pdf in PDFS for check in checks.CHECKS]  # grouped by PDF: one conversion each


def name(pdf: Path) -> str:
    return pdf.parent.name if pdf.stem == "talk" else pdf.stem


_last: dict[Path, checks.Rendered] = {}  # the PDF being checked (all of them would not fit in memory)


def rendered(pdf: Path) -> checks.Rendered:
    if pdf not in _last:
        _last.clear()
        _last[pdf] = checks.convert_locally(pdf)
    return _last[pdf]


def test_allowlist_entries_have_reasons():
    for entry in ALLOW:
        assert {"deck", "page", "check", "reason"} <= set(entry) and entry["reason"].strip(), entry
        assert entry.get("element") or entry.get("bbox"), f"entry names no element or region: {entry}"


@pytest.mark.skipif(not PDFS, reason="no test PDFs built")
@pytest.mark.parametrize("pdf,check", CASES, ids=[f"{name(p)}-{c}" for p, c in CASES])
def test_invariant(pdf, check):
    r = rendered(pdf)
    deck = name(pdf)
    findings = [f for slide in r.deck["slides"] for f in checks.CHECKS[check](r, slide)]
    used = [checks.allowed(f, deck, ALLOW) for f in findings]
    new = [f for f, entry in zip(findings, used) if entry is None]
    stale = [e for e in ALLOW if e["deck"] == deck and e["check"] == check and not any(e is u for u in used)]
    assert not new, "\n".join(f"{deck} page {f['page'] + 1} {f['check']} {f['element']} {f['bbox']}: {f['detail']}" for f in new)
    assert not stale, f"allowlist entries that match nothing any more: {stale}"
