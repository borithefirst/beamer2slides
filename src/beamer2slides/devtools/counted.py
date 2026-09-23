"""`python -m beamer2slides ...`, never interactive, and what its Google calls cost written to a JSON file.

    B2S_API_STATS=stats.json python -m beamer2slides.devtools.counted sync new.pdf --deck out/x

The file gets `gslides.STATS` when the command ends (whether it succeeded or not): calls
(attempts), retries, the seconds slept backing off, rate limits hit, and `call <methodId>` per
method. It is how devtools/fuzz_sync.py counts the calls of the convert and sync it runs in a
subprocess of their own.

The command runs under `quiet_credentials`: the cached token, refreshed if it can be, and an error
when it cannot - never the browser consent flow `google_auth.credentials` falls back to. An
unattended campaign that outlives its token (testing-mode tokens last 7 days) must fail its round,
not open a browser tab per subprocess and wait there for a person (it did, 2026-09-23).
"""

import json
import os
import runpy
import threading
import time


class NeedsConsent(RuntimeError):
    """The cached Google token is gone or expired for good: a person has to sign in again."""


def quiet_credentials():
    """The cached token's credentials, refreshed when expired; `NeedsConsent` instead of a browser."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    from beamer2slides import google_auth
    if not google_auth.TOKEN.exists():
        raise NeedsConsent(f"no Google token at {google_auth.TOKEN}: sign in once with a browser")
    creds = Credentials.from_authorized_user_file(str(google_auth.TOKEN), google_auth.SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise NeedsConsent(f"the Google token can no longer be refreshed ({type(e).__name__}): "
                               f"sign in again with a browser") from None
    return creds


def never_interactive() -> None:
    """Make this process's `google_auth.credentials` `quiet_credentials` (cached, refreshed when
    expired, shared by every thread). The module's own function, not a `use_provider` block: that
    is per context, and worker threads inherit none - they would fall back to the browser flow."""
    from beamer2slides import google_auth
    creds, lock = None, threading.Lock()

    def provider():
        nonlocal creds
        with lock:
            if creds is None or not creds.valid:
                creds = quiet_credentials()
            return creds

    google_auth.credentials = provider


def main() -> None:
    from beamer2slides import gslides

    path = os.environ.get("B2S_API_STATS")
    started = time.monotonic()
    never_interactive()
    try:
        runpy.run_module("beamer2slides", run_name="__main__", alter_sys=True)
    finally:
        if path:
            stats = dict(gslides.STATS)
            stats["seconds"] = round(time.monotonic() - started, 2)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
