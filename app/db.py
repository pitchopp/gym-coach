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


def _statements(sql: str) -> list[str]:
    """Découpe un fichier SQL en instructions (retire les commentaires de ligne)."""
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return [stmt.strip() for stmt in body.split(";") if stmt.strip()]


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Applique les migrations *.sql une seule fois, dans l'ordre, en mémorisant celles appliquées.

    Tolérant aux colonnes déjà présentes (ex. ALTER ADD COLUMN rejoué sur une base où la colonne
    existe sans avoir été enregistrée) : on ignore l'erreur 'duplicate column' au lieu de planter.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    applied = {row["filename"] for row in conn.execute("SELECT filename FROM schema_migrations")}
    for sql_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        if sql_file.name in applied:
            continue
        for stmt in _statements(sql_file.read_text(encoding="utf-8")):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (sql_file.name,))
    conn.commit()
