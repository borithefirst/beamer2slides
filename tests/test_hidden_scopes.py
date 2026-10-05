"""Hidden storage's one more scope (`drive_folder.APPDATA_SCOPE`): asked for only while the mode is
on (`google_auth.wanted_scopes`), and a token is loaded with what it was granted, never with more
(`google_auth.token_scopes`): a refresh asking for a scope never granted is refused by Google."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from beamer2slides import drive_folder, google_auth


def token(path: Path, scopes: list[str]) -> Path:
    """A token file of made-up values, valid until long after this test."""
    path.write_text(json.dumps({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                                "scopes": scopes, "expiry": "2999-01-01T00:00:00Z"}), encoding="utf-8")
    return path


def test_the_hidden_space_is_asked_for_only_while_the_mode_is_on() -> None:
    with drive_folder.use_hidden("off"):
        assert google_auth.wanted_scopes() == google_auth.SCOPES
    for mode in ("deck", "pptx"):
        with drive_folder.use_hidden(drive_folder.hidden_mode(mode)):
            assert google_auth.wanted_scopes() == [*google_auth.SCOPES, drive_folder.APPDATA_SCOPE]


def test_a_tokens_scopes_are_what_it_says(tmp_path: Path) -> None:
    assert google_auth.token_scopes(token(tmp_path / "t.json", ["a", "b"])) == ["a", "b"]
    assert google_auth.token_scopes(tmp_path / "missing.json") == []
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert google_auth.token_scopes(tmp_path / "bad.json") == []


def test_a_token_without_the_hidden_scope_is_asked_again_only_in_hidden_mode(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The consent flow is where a missing client secret is said: reaching it is asking again."""
    pytest.importorskip("google.oauth2.credentials")
    monkeypatch.setattr(google_auth, "TOKEN", token(tmp_path / "token.json", list(google_auth.SCOPES)))
    monkeypatch.setattr(google_auth, "CLIENT_SECRET", tmp_path / "no_client_secret.json")
    with drive_folder.use_hidden("off"):
        assert google_auth.credentials().valid
    with drive_folder.use_hidden("deck"), pytest.raises(FileNotFoundError, match="client secret"):
        google_auth.credentials()


def test_a_token_granted_the_hidden_scope_keeps_it_with_the_mode_off(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pytest.importorskip("google.oauth2.credentials")
    granted = [*google_auth.SCOPES, drive_folder.APPDATA_SCOPE]
    monkeypatch.setattr(google_auth, "TOKEN", token(tmp_path / "token.json", granted))
    monkeypatch.setattr(google_auth, "CLIENT_SECRET", tmp_path / "no_client_secret.json")
    with drive_folder.use_hidden("off"):
        creds = google_auth.credentials()
    assert creds.valid
    assert google_auth.token_scopes(tmp_path / "token.json") == granted   # (left as granted)
    with drive_folder.use_hidden("pptx"):
        assert google_auth.credentials().valid
