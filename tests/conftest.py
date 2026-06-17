"""Fixtures partagées : base SQLite en mémoire avec le schéma appliqué."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

# get_settings() exige TELEGRAM_BOT_TOKEN ; on le pose avant tout appel pour les tests qui touchent
# la config (ex. agent.run_agent → auth.using_api_key). Posé à l'import, avant le 1er get_settings.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON;")
    for sql_file in sorted(_MIGRATIONS.glob("*.sql")):
        c.executescript(sql_file.read_text(encoding="utf-8"))
    c.commit()
    yield c
    c.close()


def fixed_clock(iso_utc: str):
    """Retourne une horloge constante (datetime UTC aware) à partir d'un ISO 'YYYY-MM-DDTHH:MM'."""
    moment = datetime.fromisoformat(iso_utc).replace(tzinfo=UTC)
    return lambda: moment
