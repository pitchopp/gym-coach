"""Tests des relances d'onboarding et de la sélection des utilisateurs actifs."""

from __future__ import annotations

from app import repository, scheduler
from tests.conftest import fixed_clock

MONDAY = "2026-06-15"


def _user(conn, chat_id=1, onboarding_done=False):
    u = repository.get_or_create_user(chat_id, conn=conn)
    repository.update_user(u["id"], onboarding_done=onboarding_done, conn=conn)
    return repository.get_user(u["id"], conn=conn)


def _set_last_message(conn, user_id, iso_utc):
    repository.add_message(user_id, "user", "coucou", conn=conn)
    conn.execute(
        "UPDATE messages SET created_at = ? WHERE user_id = ?",
        (iso_utc.replace("T", " "), user_id),
    )
    conn.commit()


def test_users_with_slots_ignores_onboarding_flag(conn):
    """Les relances de séance ciblent quiconque a un créneau, même onboarding non terminé."""
    u = _user(conn, 1, onboarding_done=False)
    repository.set_schedule(u["id"], [{"weekday": 0, "time": "18:00"}], conn=conn)
    _user(conn, 2, onboarding_done=True)  # sans créneau → exclu

    rows = repository.list_users_with_slots(conn=conn)
    assert [r["telegram_chat_id"] for r in rows] == [1]


def test_nudge_after_idle(conn):
    u = _user(conn, 1, onboarding_done=False)
    _set_last_message(conn, u["id"], f"{MONDAY}T08:00:00")  # 08:00 UTC
    u = repository.get_user(u["id"], conn=conn)
    now = scheduler.local_now("Europe/Paris", fixed_clock(f"{MONDAY}T15:00"))  # ~7h après

    # Seuil 20h → pas encore
    assert scheduler.should_nudge_onboarding(u, f"{MONDAY} 08:00:00", now, 20, 3) is False
    # Seuil 6h → on relance
    assert scheduler.should_nudge_onboarding(u, f"{MONDAY} 08:00:00", now, 6, 3) is True


def test_nudge_stops_after_max(conn):
    u = _user(conn, 1, onboarding_done=False)
    for _ in range(3):
        repository.increment_onboarding_nudge(u["id"], conn=conn)
    u = repository.get_user(u["id"], conn=conn)
    now = scheduler.local_now("Europe/Paris", fixed_clock(f"{MONDAY}T23:00"))
    assert scheduler.should_nudge_onboarding(u, f"{MONDAY} 00:00:00", now, 6, 3) is False


def test_reset_nudges_on_user_message(conn):
    u = _user(conn, 1, onboarding_done=False)
    repository.increment_onboarding_nudge(u["id"], conn=conn)
    repository.increment_onboarding_nudge(u["id"], conn=conn)
    assert repository.get_user(u["id"], conn=conn)["onboarding_nudges"] == 2

    repository.reset_onboarding_nudges(u["id"], conn=conn)
    assert repository.get_user(u["id"], conn=conn)["onboarding_nudges"] == 0


def test_no_nudge_in_quiet_hours(conn):
    u = _user(conn, 1, onboarding_done=False)
    repository.update_user(
        u["id"], comm_prefs={"quiet_hours": {"start": "22:00", "end": "08:00"}}, conn=conn
    )
    u = repository.get_user(u["id"], conn=conn)
    # 23:00 Paris = 21:00 UTC → heures calmes
    now = scheduler.local_now("Europe/Paris", fixed_clock(f"{MONDAY}T21:00"))
    assert scheduler.should_nudge_onboarding(u, f"{MONDAY} 00:00:00", now, 6, 3) is False
