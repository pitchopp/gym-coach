"""Connexion SQLite (mode WAL) et exécution des migrations.

Une connexion unique partagée par le process (un seul worker uvicorn). SQLite en WAL gère
correctement lecture concurrente + une écriture ; les écritures concurrentes sur le même
utilisateur sont par ailleurs sérialisées par un verrou applicatif (voir coach.py).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_connection: sqlite3.Connection | None = None


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")


def init_db(db_path: str) -> sqlite3.Connection:
    """Ouvre (ou crée) la base, applique les migrations, mémorise la connexion globale."""
    global _connection
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    _configure(conn)
    _run_migrations(conn)
    _connection = conn
    return conn


def get_connection() -> sqlite3.Connection:
    if _connection is None:
        raise RuntimeError("Base non initialisée : appeler init_db() au démarrage.")
    return _connection


def _run_migrations(conn: sqlite3.Connection) -> None:
    for sql_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        conn.executescript(sql_file.read_text(encoding="utf-8"))
    conn.commit()
