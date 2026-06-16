"""Accès aux données (CRUD). Fonctions simples au-dessus de la connexion SQLite globale.

Toutes les fonctions acceptent une connexion explicite (pratique pour les tests) ou retombent
sur la connexion globale via get_connection().
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.db import get_connection


def _conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_connection()


# --------------------------------------------------------------------------- users


def get_or_create_user(
    chat_id: int, *, default_tz: str = "Europe/Paris", conn: sqlite3.Connection | None = None
) -> sqlite3.Row:
    c = _conn(conn)
    row = c.execute("SELECT * FROM users WHERE telegram_chat_id = ?", (chat_id,)).fetchone()
    if row is not None:
        return row
    c.execute(
        "INSERT INTO users (telegram_chat_id, timezone) VALUES (?, ?)",
        (chat_id, default_tz),
    )
    c.commit()
    return c.execute("SELECT * FROM users WHERE telegram_chat_id = ?", (chat_id,)).fetchone()


def get_user(user_id: int, *, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    return _conn(conn).execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users_with_slots(*, conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    """Utilisateurs ayant au moins un créneau actif → candidats aux relances de séance."""
    return (
        _conn(conn)
        .execute(
            "SELECT DISTINCT u.* FROM users u "
            "JOIN schedule_slots s ON s.user_id = u.id AND s.active = 1"
        )
        .fetchall()
    )


def list_onboarding_users(*, conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    """Utilisateurs n'ayant pas terminé l'onboarding → candidats aux relances d'onboarding."""
    return _conn(conn).execute("SELECT * FROM users WHERE onboarding_done = 0").fetchall()


def last_message_at(user_id: int, *, conn: sqlite3.Connection | None = None) -> str | None:
    """Horodatage UTC ('YYYY-MM-DD HH:MM:SS') du dernier message, ou None si aucun."""
    row = (
        _conn(conn)
        .execute("SELECT MAX(created_at) AS ts FROM messages WHERE user_id = ?", (user_id,))
        .fetchone()
    )
    return row["ts"] if row else None


def increment_onboarding_nudge(user_id: int, *, conn: sqlite3.Connection | None = None) -> None:
    c = _conn(conn)
    c.execute("UPDATE users SET onboarding_nudges = onboarding_nudges + 1 WHERE id = ?", (user_id,))
    c.commit()


def reset_onboarding_nudges(user_id: int, *, conn: sqlite3.Connection | None = None) -> None:
    c = _conn(conn)
    c.execute(
        "UPDATE users SET onboarding_nudges = 0 WHERE id = ? AND onboarding_nudges > 0", (user_id,)
    )
    c.commit()


_USER_FIELDS = {"name", "timezone", "training_frequency", "onboarding_done"}


def update_user(user_id: int, *, conn: sqlite3.Connection | None = None, **fields: Any) -> None:
    c = _conn(conn)
    sets, values = [], []
    for key, value in fields.items():
        if key == "comm_prefs":
            sets.append("comm_prefs = ?")
            values.append(json.dumps(value) if not isinstance(value, str) else value)
        elif key in _USER_FIELDS:
            sets.append(f"{key} = ?")
            values.append(int(value) if key == "onboarding_done" else value)
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    values.append(user_id)
    c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", values)
    c.commit()


# ------------------------------------------------------------------- schedule_slots


def set_schedule(
    user_id: int, slots: list[dict[str, Any]], *, conn: sqlite3.Connection | None = None
) -> None:
    """Remplace l'intégralité des créneaux récurrents de l'utilisateur."""
    c = _conn(conn)
    c.execute("DELETE FROM schedule_slots WHERE user_id = ?", (user_id,))
    for slot in slots:
        c.execute(
            "INSERT INTO schedule_slots (user_id, weekday, time, activity, active) "
            "VALUES (?, ?, ?, ?, 1)",
            (user_id, int(slot["weekday"]), slot["time"], slot.get("activity")),
        )
    c.commit()


def list_slots(
    user_id: int, *, active_only: bool = True, conn: sqlite3.Connection | None = None
) -> list[sqlite3.Row]:
    q = "SELECT * FROM schedule_slots WHERE user_id = ?"
    if active_only:
        q += " AND active = 1"
    return _conn(conn).execute(q + " ORDER BY weekday, time", (user_id,)).fetchall()


# ------------------------------------------------------------------------- checkins


def get_checkin(
    user_id: int, due_date: str, slot_id: int | None, *, conn: sqlite3.Connection | None = None
) -> sqlite3.Row | None:
    c = _conn(conn)
    if slot_id is None:
        return c.execute(
            "SELECT * FROM checkins WHERE user_id = ? AND due_date = ? AND slot_id IS NULL",
            (user_id, due_date),
        ).fetchone()
    return c.execute(
        "SELECT * FROM checkins WHERE user_id = ? AND due_date = ? AND slot_id = ?",
        (user_id, due_date, slot_id),
    ).fetchone()


def create_checkin(
    user_id: int,
    *,
    due_date: str,
    due_time: str,
    slot_id: int | None = None,
    activity: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Crée un check-in 'pending'. Idempotent via l'index unique (user, date, slot)."""
    c = _conn(conn)
    cur = c.execute(
        "INSERT OR IGNORE INTO checkins (user_id, slot_id, due_date, due_time, activity) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, slot_id, due_date, due_time, activity),
    )
    c.commit()
    if cur.lastrowid:
        return cur.lastrowid
    existing = get_checkin(user_id, due_date, slot_id, conn=c)
    return existing["id"] if existing else 0


def list_due_checkins(
    user_id: int, today: str, *, conn: sqlite3.Connection | None = None
) -> list[sqlite3.Row]:
    """Check-ins 'pending' échus (date <= aujourd'hui), ordonnés du plus ancien au plus récent."""
    return (
        _conn(conn)
        .execute(
            "SELECT * FROM checkins WHERE user_id = ? AND status = 'pending' AND due_date <= ? "
            "ORDER BY due_date, due_time",
            (user_id, today),
        )
        .fetchall()
    )


def list_open_checkins(
    user_id: int, *, conn: sqlite3.Connection | None = None
) -> list[sqlite3.Row]:
    """Check-ins déjà demandés mais sans réponse (statut 'asked'). Injectés dans le contexte agent."""
    return (
        _conn(conn)
        .execute(
            "SELECT * FROM checkins WHERE user_id = ? AND status = 'asked' ORDER BY due_date, due_time",
            (user_id,),
        )
        .fetchall()
    )


def mark_checkin_asked(checkin_id: int, *, conn: sqlite3.Connection | None = None) -> None:
    c = _conn(conn)
    c.execute(
        "UPDATE checkins SET status = 'asked', asked_at = datetime('now') WHERE id = ?",
        (checkin_id,),
    )
    c.commit()


def resolve_checkin(
    checkin_id: int,
    status: str,
    *,
    note: str | None = None,
    reschedule_to: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    c = _conn(conn)
    c.execute(
        "UPDATE checkins SET status = ?, note = COALESCE(?, note), reschedule_to = ?, "
        "responded_at = datetime('now') WHERE id = ?",
        (status, note, reschedule_to, checkin_id),
    )
    c.commit()


def latest_open_checkin(
    user_id: int, *, conn: sqlite3.Connection | None = None
) -> sqlite3.Row | None:
    """Dernier check-in 'asked' — cible par défaut quand l'utilisateur répond sans préciser."""
    return (
        _conn(conn)
        .execute(
            "SELECT * FROM checkins WHERE user_id = ? AND status = 'asked' "
            "ORDER BY asked_at DESC LIMIT 1",
            (user_id,),
        )
        .fetchone()
    )


# ------------------------------------------------------------------------- programs


def save_program(user_id: int, content: str, *, conn: sqlite3.Connection | None = None) -> None:
    c = _conn(conn)
    c.execute("UPDATE programs SET active = 0 WHERE user_id = ?", (user_id,))
    version = (
        c.execute("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM programs WHERE user_id = ?", (user_id,))
        .fetchone()["v"]
    )
    c.execute(
        "INSERT INTO programs (user_id, content, version, active) VALUES (?, ?, ?, 1)",
        (user_id, content, version),
    )
    c.commit()


def get_active_program(
    user_id: int, *, conn: sqlite3.Connection | None = None
) -> sqlite3.Row | None:
    return (
        _conn(conn)
        .execute("SELECT * FROM programs WHERE user_id = ? AND active = 1", (user_id,))
        .fetchone()
    )


# ------------------------------------------------------------------------- messages


def add_message(
    user_id: int, role: str, content: str, *, conn: sqlite3.Connection | None = None
) -> None:
    c = _conn(conn)
    c.execute(
        "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content),
    )
    c.commit()


def live_messages(
    user_id: int, after_id: int, limit: int, *, conn: sqlite3.Connection | None = None
) -> list[sqlite3.Row]:
    """Messages non encore résumés (id > after_id), des plus récents (cap `limit`), ordre chronologique."""
    rows = (
        _conn(conn)
        .execute(
            "SELECT id, role, content FROM messages WHERE user_id = ? AND id > ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, after_id, limit),
        )
        .fetchall()
    )
    return list(reversed(rows))


def count_live_messages(
    user_id: int, after_id: int, *, conn: sqlite3.Connection | None = None
) -> int:
    return (
        _conn(conn)
        .execute(
            "SELECT COUNT(*) AS n FROM messages WHERE user_id = ? AND id > ?", (user_id, after_id)
        )
        .fetchone()["n"]
    )


def oldest_live_messages(
    user_id: int, after_id: int, limit: int, *, conn: sqlite3.Connection | None = None
) -> list[sqlite3.Row]:
    """Les plus ANCIENS messages non résumés (id > after_id), ordre chronologique — pour le résumé."""
    return (
        _conn(conn)
        .execute(
            "SELECT id, role, content FROM messages WHERE user_id = ? AND id > ? "
            "ORDER BY id ASC LIMIT ?",
            (user_id, after_id, limit),
        )
        .fetchall()
    )


def update_summary(
    user_id: int, summary: str, through_id: int, *, conn: sqlite3.Connection | None = None
) -> None:
    c = _conn(conn)
    c.execute(
        "UPDATE users SET summary = ?, summary_through_id = ? WHERE id = ?",
        (summary, through_id, user_id),
    )
    c.commit()


# ---------------------------------------------------------------------------- facts


def remember_fact(
    user_id: int, key: str, value: str, *, conn: sqlite3.Connection | None = None
) -> None:
    c = _conn(conn)
    c.execute(
        "INSERT INTO facts (user_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, "
        "updated_at = datetime('now')",
        (user_id, key, value),
    )
    c.commit()


def list_facts(user_id: int, *, conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    return (
        _conn(conn)
        .execute("SELECT key, value FROM facts WHERE user_id = ? ORDER BY key", (user_id,))
        .fetchall()
    )
