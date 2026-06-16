"""Tests des handlers d'outils, en particulier les transitions de log_session."""

from __future__ import annotations

from app import repository
from app.tools import handle_tool


def _user(conn):
    u = repository.get_or_create_user(999, conn=conn)
    return repository.get_user(u["id"], conn=conn)


def test_update_profile_and_onboarding(conn):
    user = _user(conn)
    handle_tool(
        user["id"],
        "update_profile",
        {"name": "Amine", "training_frequency": "4x/semaine", "onboarding_done": True},
        conn,
    )
    refreshed = repository.get_user(user["id"], conn=conn)
    assert refreshed["name"] == "Amine"
    assert refreshed["onboarding_done"] == 1


def test_set_schedule_replaces(conn):
    user = _user(conn)
    handle_tool(user["id"], "set_schedule", {"slots": [{"weekday": 0, "time": "18:00"}]}, conn)
    handle_tool(
        user["id"],
        "set_schedule",
        {"slots": [{"weekday": 2, "time": "07:00", "activity": "cardio"}]},
        conn,
    )
    slots = repository.list_slots(user["id"], conn=conn)
    assert len(slots) == 1
    assert slots[0]["weekday"] == 2


def test_log_session_done_resolves_open_checkin(conn):
    user = _user(conn)
    cid = repository.create_checkin(user["id"], due_date="2026-06-15", due_time="18:00", conn=conn)
    repository.mark_checkin_asked(cid, conn=conn)

    msg = handle_tool(user["id"], "log_session", {"status": "done"}, conn)
    assert "faite" in msg
    row = conn.execute("SELECT status FROM checkins WHERE id = ?", (cid,)).fetchone()
    assert row["status"] == "done"


def test_log_session_reschedule_creates_followup(conn):
    user = _user(conn)
    cid = repository.create_checkin(
        user["id"], due_date="2026-06-15", due_time="18:00", activity="jambes", conn=conn
    )
    repository.mark_checkin_asked(cid, conn=conn)

    handle_tool(
        user["id"],
        "log_session",
        {"status": "rescheduled", "reschedule_to": "2026-06-16"},
        conn,
    )
    rows = conn.execute(
        "SELECT * FROM checkins WHERE user_id = ? ORDER BY id", (user["id"],)
    ).fetchall()
    assert rows[0]["status"] == "rescheduled"
    assert rows[0]["reschedule_to"] == "2026-06-16"
    # Un nouveau check-in 'pending' a été créé pour le lendemain.
    assert rows[1]["due_date"] == "2026-06-16"
    assert rows[1]["status"] == "pending"
    assert rows[1]["activity"] == "jambes"


def test_remember_and_recall_facts(conn):
    user = _user(conn)
    handle_tool(user["id"], "remember_fact", {"key": "blessure", "value": "genou droit"}, conn)
    handle_tool(user["id"], "remember_fact", {"key": "objectif", "value": "prise de masse"}, conn)
    # Mise à jour d'un fait existant (upsert).
    handle_tool(user["id"], "remember_fact", {"key": "blessure", "value": "épaule"}, conn)

    out = handle_tool(user["id"], "recall_facts", {}, conn)
    assert "épaule" in out and "prise de masse" in out
    assert len(repository.list_facts(user["id"], conn=conn)) == 2


def test_save_and_get_program(conn):
    user = _user(conn)
    handle_tool(user["id"], "save_program", {"content": "# Semaine 1\n- Squat 5x5"}, conn)
    handle_tool(user["id"], "save_program", {"content": "# Semaine 2\n- Deadlift 3x5"}, conn)
    out = handle_tool(user["id"], "get_program", {}, conn)
    assert "Semaine 2" in out
    # Une seule version active.
    active = conn.execute(
        "SELECT COUNT(*) c FROM programs WHERE user_id = ? AND active = 1", (user["id"],)
    ).fetchone()
    assert active["c"] == 1
