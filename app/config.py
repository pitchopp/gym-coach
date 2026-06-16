"""Configuration centralisée, lue depuis l'environnement (.env en local)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # Auth Claude par OAuth (abonnement) : pas de clé API. Voir app/auth.py.
    oauth_creds_path: str
    oauth_seed_json: str
    model: str
    telegram_bot_token: str
    webhook_secret: str
    public_url: str
    db_path: str
    tick_minutes: int
    default_tz: str
    # Délai après le créneau avant de relancer (minutes) et nb de messages d'historique chargés.
    checkin_grace_minutes: int
    history_limit: int
    # Relances d'onboarding : silence minimal avant relance, et nb max de relances sans réponse.
    onboarding_idle_hours: float
    onboarding_max_nudges: int

    @property
    def webhook_path(self) -> str:
        return f"/webhook/{self.webhook_secret}"


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Variable d'environnement requise manquante : {name}")
    return value or ""


@lru_cache
def get_settings() -> Settings:
    return Settings(
        oauth_creds_path=_get("OAUTH_CREDS_PATH", "./data/claude_oauth.json"),
        oauth_seed_json=_get("CLAUDE_OAUTH_JSON", ""),
        model=_get("MODEL", "claude-sonnet-4-6"),
        telegram_bot_token=_get("TELEGRAM_BOT_TOKEN", required=True),
        webhook_secret=_get("WEBHOOK_SECRET", "dev-secret"),
        public_url=_get("PUBLIC_URL", "").rstrip("/"),
        db_path=_get("DB_PATH", "./data/coach.db"),
        tick_minutes=int(_get("TICK_MINUTES", "15")),
        default_tz=_get("DEFAULT_TZ", "Europe/Paris"),
        checkin_grace_minutes=int(_get("CHECKIN_GRACE_MINUTES", "45")),
        history_limit=int(_get("HISTORY_LIMIT", "30")),
        onboarding_idle_hours=float(_get("ONBOARDING_IDLE_HOURS", "20")),
        onboarding_max_nudges=int(_get("ONBOARDING_MAX_NUDGES", "3")),
    )
