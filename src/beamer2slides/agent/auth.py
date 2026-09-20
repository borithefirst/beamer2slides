"""Google access for something that cannot answer a browser prompt.

`google_auth.credentials()` opens a browser when the cached token has run out, and blocks the
calling thread until someone clicks. That is right at a terminal and fatal anywhere else: an
agent harness would simply hang, with nothing in the transcript to say why. Every source here
therefore **fails fast instead of asking** - a missing or dead token comes back as
`needs_consent`, with the one command a human has to run to fix it - and a harness that holds
its own token hands it over (`InjectedToken`) rather than putting a file where the library
happens to look.

Nothing here prints, returns or stores a token's contents. `describe()` answers what an agent
legitimately needs - is there access, whose, until when - and nothing more.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .types import Refused

CONSENT_COMMAND = "python -m beamer2slides.agent.auth"


@runtime_checkable
class GoogleAccess(Protocol):
    """Where a journey's Google credentials come from. Never interactive."""

    def credentials(self) -> Any: ...
    def describe(self) -> dict: ...


class NoGoogle:
    """No Google at all: every tool that needs it refuses with `offline`.

    The right source for a benchmark's offline tier, for a harness that has not been given an
    account, and for a first run where only the local journeys should be reachable.

    A host that *could* have an account says so: `reason` is what `b2s_status` reports and `fix`
    the one sentence telling a person what to do about it. They matter where the absence is not
    a property of the machine but of this moment - the playground's visitor has not signed in
    yet, and "offline" there reads as a server that cannot reach Google at all.
    """

    def __init__(self, reason: str = "offline", fix: str | None = None) -> None:
        self.reason, self.fix = reason, fix

    def credentials(self) -> Any:
        raise Refused("offline",
                      "This workspace has no Google access. Local journeys (deck_inspect, "
                      "tex_label, and converge) work; anything touching Slides, Docs or Drive "
                      "does not." + (f" {self.fix}" if self.fix else ""))

    def describe(self) -> dict:
        out = {"available": False, "reason": self.reason, "scopes": []}
        if self.fix:
            out["fix"] = self.fix
        return out


class TokenFile:
    """The cached OAuth token on this machine, refreshed if it can be, never re-consented.

    Where the file is follows the library's own rules (`google_auth.credential_file`), except
    that they are read now rather than at import, so setting `$B2S_TOKEN` in a harness works.
    """

    def __init__(self, token: Path | str | None = None, client_secret: Path | str | None = None) -> None:
        from .. import google_auth

        self._token = Path(token) if token else None
        self._secret = Path(client_secret) if client_secret else None
        self._auth = google_auth

    def _paths(self) -> tuple[Path, Path]:
        secret = self._secret
        if secret is None:
            secret = self._auth.credential_file("B2S_CLIENT_SECRET", "client_secret.json",
                                                self._auth.CLIENT_SECRET)
        token = self._token
        if token is None:
            token = self._auth.credential_file("B2S_TOKEN", "token.json",
                                               secret.with_name("token.json"))
        return secret, token

    def credentials(self) -> Any:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        secret, token = self._paths()
        if not token.exists():
            if not secret.exists():
                raise Refused("no_credentials",
                              f"No OAuth client is installed (looked for {secret}). Follow "
                              f"docs/install.md to create a desktop-app client, then run "
                              f"`{CONSENT_COMMAND}` once.", client_secret=str(secret))
            raise Refused("needs_consent",
                          f"No Google token yet. A human has to run `{CONSENT_COMMAND}` once at "
                          f"a terminal and approve the access; it cannot be done from here.",
                          command=CONSENT_COMMAND)
        creds = Credentials.from_authorized_user_file(str(token), self._auth.SCOPES)
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise Refused("needs_consent",
                              f"The Google token has expired and could not be refreshed ({exc}). "
                              f"This project's consent screen is in testing mode, where refresh "
                              f"tokens die after 7 days: a human has to run `{CONSENT_COMMAND}` "
                              f"again.", command=CONSENT_COMMAND) from None
            token.write_text(creds.to_json(), encoding="utf-8")
            self._auth.restrict_to_current_user(token)
            return creds
        raise Refused("needs_consent",
                      f"The Google token is not usable and has no refresh token. Run "
                      f"`{CONSENT_COMMAND}` at a terminal.", command=CONSENT_COMMAND)

    def describe(self) -> dict:
        secret, token = self._paths()
        out: dict[str, Any] = {"available": False, "source": "token file",
                               "token_installed": token.exists(),
                               "client_installed": secret.exists(),
                               "scopes": list(self._auth.SCOPES)}
        if not token.exists():
            out["reason"] = "needs_consent" if secret.exists() else "no_credentials"
            out["command"] = CONSENT_COMMAND
            return out
        try:
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_file(str(token), self._auth.SCOPES)
        except Exception as exc:                                   # a truncated or foreign file
            out["reason"] = f"the token file could not be read ({type(exc).__name__})"
            return out
        out["available"] = bool(creds.valid or creds.refresh_token)
        out["expired"] = bool(creds.expired)
        out["refreshable"] = bool(creds.refresh_token)
        if creds.expiry:
            out["expires"] = creds.expiry.replace(tzinfo=_dt.timezone.utc).isoformat()
        if not out["available"]:
            out["reason"] = "needs_consent"
            out["command"] = CONSENT_COMMAND
        return out


class InjectedToken:
    """Credentials the harness supplies, as the authorized-user JSON Google's own libraries use.

    For a harness that keeps secrets in its own store and never puts them on disk. The mapping
    is one call, so a harness holding a bare access token can pass
    `{"token": ..., "refresh_token": ..., "client_id": ..., "client_secret": ...}`.
    """

    def __init__(self, info: dict) -> None:
        self._info = dict(info)

    def credentials(self) -> Any:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from .. import google_auth

        try:
            creds = Credentials.from_authorized_user_info(self._info, google_auth.SCOPES)
        except ValueError as exc:
            raise Refused("no_credentials", f"The injected credentials are not usable: {exc}") from None
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            return creds
        raise Refused("needs_consent",
                      "The injected credentials are expired and cannot be refreshed. The harness "
                      "has to supply fresh ones.")

    def describe(self) -> dict:
        from .. import google_auth
        return {"available": True, "source": "injected", "scopes": list(google_auth.SCOPES)}


def default_access() -> GoogleAccess:
    """`NoGoogle` when `$B2S_AGENT_OFFLINE` says so, else this machine's token file."""
    if os.environ.get("B2S_AGENT_OFFLINE", "").lower() in ("1", "yes", "true", "on"):
        return NoGoogle()
    return TokenFile()


def _consent() -> int:
    """`python -m beamer2slides.agent.auth`: the browser round an agent cannot do for itself."""
    from .. import google_auth

    creds = google_auth.credentials()
    _, token = TokenFile()._paths()
    print(f"Google access granted; the token is in {token}.")
    print("Scopes: " + ", ".join(google_auth.SCOPES))
    if creds.expiry:
        print(f"It expires {creds.expiry:%Y-%m-%d %H:%M} UTC. This project's consent screen is in "
              f"testing mode, so plan on doing this again in a week.")
    return 0


if __name__ == "__main__":                                        # pragma: no cover - interactive
    raise SystemExit(_consent())
