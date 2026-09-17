"""Opt-in: native text and its graphics stay aligned on real Google Slides (tools/alignment.py).

  python -m pytest -m slides                          # convert, export thumbnails, measure, assert
  B2S_UPDATE_BASELINE=1 python -m pytest -m slides    # rewrite tests/slides_baseline.json
  B2S_SLIDES_REUSE=1 python -m pytest -m slides       # measure the existing folders again only

Each stress deck is converted into out/slides-tests/<deck> of the main checkout (a fixed folder
rebuilds the same Slides deck), 3 at a time, then `fidelity --refresh` and the measurement run.
Items over the thresholds fail unless the baseline lists them under `known_failures` (with the
reason); every error more than GROWTH pt over its baseline value fails too.
"""

import json
import os
import subprocess
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def main_checkout() -> Path:
    """The main checkout (a worktree's .git file points into its .git/worktrees)."""
    git = ROOT / ".git"
    if git.is_file():
        return Path(git.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip()).parents[2]
    return ROOT


MAIN = main_checkout()
for var, name in (("B2S_TOKEN", "token.json"), ("B2S_CLIENT_SECRET", "client_secret.json")):
    if (MAIN / name).exists():
        os.environ.setdefault(var, str(MAIN / name))
sys.path.insert(0, str(ROOT / "tools"))

import alignment  # noqa: E402

pytestmark = pytest.mark.slides

DECKS = ["19_labels_on_graphics", "20_marks_edge_cases", "21_bullet_shapes", "22_overlays_on_text",
         "25_hole_placement", "13_inline_math", "demo"]
OUT = Path(os.environ.get("B2S_SLIDES_TESTS_OUT", MAIN / "out" / "slides-tests"))
BASELINE = ROOT / "tests" / "slides_baseline.json"
GROWTH = 0.75  # pt (ΔE and size ratios likewise) an error may grow over its baseline
PARALLEL = 3
REUSE = bool(os.environ.get("B2S_SLIDES_REUSE"))


def pdf_for(deck: str) -> Path | None:
    rel = Path("examples/demo/demo.pdf") if deck == "demo" else Path("tests/decks/out") / f"{deck}.pdf"
    return next((root / rel for root in (ROOT, MAIN) if (root / rel).exists()), None)


def google_unavailable() -> str | None:
    """Why Google can't be used without a browser consent, or None. The token is refreshed here
    if it runs out soon, so parallel conversions never write the token file at the same time."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from beamer2slides import google_auth
    if not google_auth.TOKEN.exists():
        return f"no Google token at {google_auth.TOKEN} (run tools/slides_smoke.py once to consent)"
    creds = Credentials.from_authorized_user_file(str(google_auth.TOKEN), google_auth.SCOPES)
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # (google-auth keeps expiry naive UTC)
    if creds.valid and creds.expiry and creds.expiry - now > timedelta(minutes=30):
        return None
    if not creds.refresh_token:
        return "the Google token can't be refreshed: a browser consent is needed"
    try:
        creds.refresh(Request())
    except RefreshError as e:
        return f"the Google token expired ({e}): a browser consent is needed"
    google_auth.TOKEN.write_text(creds.to_json(), encoding="utf-8")
    google_auth.restrict_to_current_user(google_auth.TOKEN)
    return None


def convert(deck: str) -> float:
    """Convert a deck and export its thumbnails (log: out/slides-tests/<deck>.log); seconds taken."""
    started = time.monotonic()
    out, pdf = OUT / deck, pdf_for(deck)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    with open(OUT / f"{deck}.log", "w", encoding="utf-8") as log:
        for args in (["convert", str(pdf), "--out", str(out)], ["fidelity", str(pdf), "--out", str(out), "--refresh"]):
            done = subprocess.run([sys.executable, "-m", "beamer2slides", *args], env=env, cwd=ROOT,
                                  stdout=log, stderr=subprocess.STDOUT)
            if done.returncode:
                raise RuntimeError(f"{deck}: {args[0]} failed, see {OUT / f'{deck}.log'}")
    return time.monotonic() - started


@pytest.fixture(scope="module")
def reports(request, pytestconfig):
    """deck -> alignment report (or the exception), for the decks this session selected."""
    decks = [d for d in DECKS if any(getattr(item, "callspec", None) and item.callspec.params.get("deck") == d
                                     for item in request.session.items)]
    missing = [d for d in decks if pdf_for(d) is None]
    if missing:
        pytest.skip(f"PDFs not built: {missing} (tests/decks/build.py, examples/demo)")
    reason = None if REUSE else google_unavailable()
    if reason:
        pytest.skip(reason)
    OUT.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    results, seconds = {}, {}
    if not REUSE:
        def run(deck):
            try:
                return convert(deck)
            except Exception as e:  # reported by that deck's test
                return e
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            for deck, done in zip(decks, pool.map(run, decks)):
                (results if isinstance(done, Exception) else seconds)[deck] = done
    for deck in decks:  # (PDFium is not thread-safe: measured one after another)
        if deck not in results:
            try:
                results[deck] = alignment.measure(OUT / deck, refresh=not REUSE)
            except Exception as e:
                results[deck] = e
    writer = pytestconfig.pluginmanager.get_plugin("terminalreporter")
    if writer:
        writer.write_line(f"\nslides alignment: {len(decks)} decks in {time.monotonic() - started:.0f} s"
                          + (f" (conversions: {', '.join(f'{d} {s:.0f} s' for d, s in seconds.items())})" if seconds else ""))
    return results


def failure_keys(report: dict) -> list[tuple[str, str, dict]]:
    """(baseline key `page:item metric`, message, row) of every failure."""
    return [(f"{key} {msg.split(' ')[0].rstrip(':')}", f"{kind} {key}: {msg}", row)
            for kind, key, row in alignment.items(report) for msg in alignment.row_failures(kind, row)]


@pytest.mark.parametrize("deck", DECKS)
def test_alignment(deck, reports):
    report = reports[deck]
    if isinstance(report, Exception):
        raise report
    base = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {"known_failures": {}, "decks": {}}
    known = base["known_failures"].get(deck, {})
    values = {key: m for kind, key, row in alignment.items(report) if (m := alignment.metrics(kind, row))}
    current = failure_keys(report)
    if os.environ.get("B2S_UPDATE_BASELINE"):
        base["decks"][deck] = values
        base["known_failures"][deck] = {k: known.get(k, f"UNVERIFIED: {msg}") for k, msg, _ in current}
        BASELINE.write_text(json.dumps(base, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        return
    problems = [msg + (f" (evidence: {OUT / deck / row['evidence']})" if row.get("evidence") else "")
                for k, msg, row in current if k not in known]
    old = base["decks"].get(deck, {})
    problems += [f"{key} {name} grew from {old[key][name]} to {value} (baseline + {GROWTH})"
                 for key, metrics in values.items() for name, value in metrics.items()
                 if name in old.get(key, {}) and value > old[key][name] + GROWTH]
    fixed = sorted(set(known) - {k for k, _, _ in current})
    if fixed:
        warnings.warn(f"{deck}: known failures no longer fail, update the baseline: {fixed}")
    if problems:
        pytest.fail(f"{deck} ({report['url']}):\n  " + "\n  ".join(problems), pytrace=False)
