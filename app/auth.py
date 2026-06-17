"""Authentification Claude par OAuth (abonnement Max), avec refresh automatique.

On utilise les credentials OAuth de l'abonnement plutôt qu'une clé API. Le token d'accès expire
(~8 h) ; on le rafraîchit via le refresh token et on persiste le résultat sur le volume `/data`
(le refresh token tourne à chaque rafraîchissement, il faut donc le réécrire aussitôt).

Contraintes vérifiées :
- Appels Messages API : en-tête `anthropic-beta: oauth-2025-04-20`, et le 1er bloc system DOIT être
  l'identité « Claude Code » sinon la requête est rejetée.
- Refresh : POST console.anthropic.com/v1/oauth/token avec `User-Agent: anthropic`.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from anthropic import Anthropic

from app.config import get_settings

OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA = "oauth-2025-04-20"
# Le 1er bloc system doit être exactement cette identité pour que l'auth OAuth soit acceptée.
CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
# Marge avant expiry pour déclencher un refresh proactif.
_REFRESH_MARGIN_S = 300
# Cooldown minimal après un refresh échoué : empêche de matraquer le endpoint OAuth
# (un seul échec transitoire suffisait sinon à déclencher une rafale de 429 en boucle).
_REFRESH_FAIL_COOLDOWN_S = 60

_lock = threading.Lock()
# Instant (epoch s) avant lequel on s'interdit toute nouvelle tentative de refresh, et
# dernier message d'erreur — protégés par `_lock`.
_refresh_blocked_until = 0.0
_refresh_last_error: str | None = None


def _creds_path() -> Path:
    return Path(get_settings().oauth_creds_path)


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Accepte soit le JSON complet du Keychain ({claudeAiOauth: {...}}), soit le bloc interne."""
    return raw.get("claudeAiOauth", raw)


def load_creds() -> dict[str, Any]:
    path = _creds_path()
    if not path.exists():
        raise RuntimeError(
            f"Credentials OAuth absents ({path}). Définir CLAUDE_OAUTH_JSON pour le seed initial."
        )
    return _normalize(json.loads(path.read_text(encoding="utf-8")))


def save_creds(creds: dict[str, Any]) -> None:
    path = _creds_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(creds), encoding="utf-8")
    tmp.replace(path)  # écriture atomique


def seed_from_env() -> None:
    """Au premier démarrage, crée le fichier de creds depuis l'env si absent."""
    path = _creds_path()
    if path.exists():
        return
    seed = get_settings().oauth_seed_json
    if not seed:
        return
    save_creds(_normalize(json.loads(seed)))


def _refresh(creds: dict[str, Any]) -> dict[str, Any]:
    resp = httpx.post(
        OAUTH_TOKEN_URL,
        headers={"content-type": "application/json", "user-agent": "anthropic"},
        json={
            "grant_type": "refresh_token",
            "refresh_token": creds["refreshToken"],
            "client_id": OAUTH_CLIENT_ID,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    updated = dict(creds)
    updated["accessToken"] = data["access_token"]
    if data.get("refresh_token"):
        updated["refreshToken"] = data["refresh_token"]
    updated["expiresAt"] = int(time.time() * 1000) + int(data["expires_in"]) * 1000
    save_creds(updated)
    return updated


def _cooldown_for(exc: Exception) -> float:
    """Durée de cooldown après un refresh échoué ; respecte `Retry-After` sur un 429."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        retry_after = exc.response.headers.get("retry-after", "")
        if retry_after.isdigit():
            return max(_REFRESH_FAIL_COOLDOWN_S, float(retry_after))
    return _REFRESH_FAIL_COOLDOWN_S


def get_access_token() -> str:
    """Renvoie un token d'accès valide, en rafraîchissant si nécessaire (thread-safe).

    En cas d'échec de refresh, on pose un cooldown avant toute nouvelle tentative : sans ça,
    chaque requête entrante re-déclenchait un refresh immédiat et l'avalanche d'appels finissait
    par entretenir un 429 permanent sur le endpoint OAuth.
    """
    global _refresh_blocked_until, _refresh_last_error
    with _lock:
        creds = load_creds()
        now = time.time()
        expires_at_s = creds.get("expiresAt", 0) / 1000
        if expires_at_s - now > _REFRESH_MARGIN_S:
            return creds["accessToken"]

        # Refresh requis. Si une tentative récente a échoué, on ne re-matraque pas.
        if now < _refresh_blocked_until:
            if now < expires_at_s:  # token techniquement encore valide : on s'en sert
                return creds["accessToken"]
            raise RuntimeError(
                f"Refresh OAuth en cooldown ({_refresh_blocked_until - now:.0f}s restantes) "
                f"après échec : {_refresh_last_error}"
            )

        try:
            creds = _refresh(creds)
        except Exception as exc:
            _refresh_blocked_until = time.time() + _cooldown_for(exc)
            _refresh_last_error = str(exc)
            if now < expires_at_s:  # le token courant n'est pas encore expiré : on continue
                return creds["accessToken"]
            raise
        _refresh_blocked_until = 0.0
        _refresh_last_error = None
        return creds["accessToken"]


def using_api_key() -> bool:
    """True si une clé API dédiée est configurée (prioritaire sur l'OAuth d'abonnement)."""
    return bool(get_settings().anthropic_api_key)


def build_client() -> Anthropic:
    """Client Anthropic. Clé API si fournie (pas de refresh OAuth, limites isolées) ; sinon OAuth.

    En mode clé API, on n'envoie NI l'en-tête beta OAuth NI le bloc d'identité « Claude Code »
    (tous deux spécifiques à l'auth par abonnement — cf. run_agent qui omet l'identité dans ce cas).
    """
    api_key = get_settings().anthropic_api_key
    if api_key:
        return Anthropic(api_key=api_key)
    return Anthropic(
        auth_token=get_access_token(),
        default_headers={"anthropic-beta": OAUTH_BETA},
    )
