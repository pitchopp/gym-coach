"""Refresh OAuth : on ne doit jamais matraquer le endpoint quand un refresh échoue."""

from __future__ import annotations

import time

import httpx
import pytest

from app import auth


@pytest.fixture(autouse=True)
def _reset_cooldown():
    auth._refresh_blocked_until = 0.0
    auth._refresh_last_error = None
    yield
    auth._refresh_blocked_until = 0.0
    auth._refresh_last_error = None


def _expired_creds() -> dict:
    return {"accessToken": "old", "refreshToken": "r", "expiresAt": int((time.time() - 60) * 1000)}


def _http_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", auth.OAUTH_TOKEN_URL)
    resp = httpx.Response(status, headers=headers or {}, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


def test_refresh_failure_pose_un_cooldown_et_ne_re_tente_pas(monkeypatch):
    calls = {"n": 0}

    def fake_refresh(creds):
        calls["n"] += 1
        raise _http_error(429)

    monkeypatch.setattr(auth, "load_creds", _expired_creds)
    monkeypatch.setattr(auth, "_refresh", fake_refresh)

    # 1er appel : tente le refresh, échoue, propage l'erreur (token expiré).
    with pytest.raises(httpx.HTTPStatusError):
        auth.get_access_token()
    # 2e appel : bloqué par le cooldown, AUCUN nouvel appel réseau.
    with pytest.raises(RuntimeError, match="cooldown"):
        auth.get_access_token()

    assert calls["n"] == 1


def test_retry_after_allonge_le_cooldown(monkeypatch):
    monkeypatch.setattr(auth, "load_creds", _expired_creds)

    def _raise_429(_creds):
        raise _http_error(429, {"retry-after": "600"})

    monkeypatch.setattr(auth, "_refresh", _raise_429)

    with pytest.raises(httpx.HTTPStatusError):
        auth.get_access_token()

    assert auth._refresh_blocked_until - time.time() > 500


def test_token_encore_valide_survit_a_un_echec_de_refresh(monkeypatch):
    # Token dans la marge de refresh mais pas encore expiré : un échec ne doit pas planter.
    creds = {"accessToken": "good", "refreshToken": "r", "expiresAt": int((time.time() + 120) * 1000)}
    monkeypatch.setattr(auth, "load_creds", lambda: creds)
    monkeypatch.setattr(auth, "_refresh", lambda c: (_ for _ in ()).throw(_http_error(429)))

    assert auth.get_access_token() == "good"
