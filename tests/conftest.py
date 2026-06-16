"""Fixtures partagées : base SQLite en mémoire avec le schéma appliqué."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
