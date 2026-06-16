"""Tests du moteur de proactivité — la logique la plus risquée."""

from __future__ import annotations

import pytest

from app import repository, scheduler
from tests.conftest import fixed_clock

# 2026-06-15 est un LUNDI (weekday 0).
MONDAY = "2026-06-15"
TUESDAY = "2026-06-16"


def _user_with_monday_slot(conn, tz="Europe/Paris", quiet=None):
    user = repository.get_or_create_user(12345, conn=conn)
    prefs = {"quiet_hours": quiet} if quiet else {}
    repository.update_user(user["id"], onboarding_done=True, comm_prefs=prefs, conn=conn)
    repository.set_schedule(
        user["id"], [{"weekday": 0, "time": "18:00", "activity": "jambes"}], conn=conn
    )
    return repository.get_user(user["id"], conn=conn)


def test_materialize_creates_one_checkin_for_matching_weekday(conn):
    user = _user_with_monday_slot(conn)
    from datetime import date

    scheduler.materialize_day(conn, user["id"], date.fromisoformat(MONDAY))
    scheduler.materialize_day(conn, user["id"], date.fromisoformat(MONDAY))  # idempotent

    rows = conn.execute("SELECT * FROM checkins WHERE user_id = ?", (user["id"],)).fetchall()
    assert len(rows) == 1
    assert rows[0]["due_date"] == MONDAY
    assert rows[0]["status"] == "pending"


def test_no_checkin_on_non_matching_weekday(conn):
    user = _user_with_monday_slot(conn)
    from datetime import date

    scheduler.materialize_day(conn, user["id"], date.fromisoformat(TUESDAY))
    assert conn.execute("SELECT COUNT(*) c FROM checkins").fetchone()["c"] == 0


def test_due_only_after_grace(conn):
    user = _user_with_monday_slot(conn)
    # Créneau 18:00 Paris = 16:00 UTC. Grâce 45 min -> dû à partir de 16:45 UTC.
    not_yet = scheduler.due_checkins(
        conn, user, scheduler.local_now("Europe/Paris", fixed_clock(f"{MONDAY}T16:30")), 45
    )
    assert not_yet == []

    ready = scheduler.due_checkins(
        conn, user, scheduler.local_now("Europe/Paris", fixed_clock(f"{MONDAY}T17:00")), 45
    )
    assert len(ready) == 1


def test_no_second_ask_once_asked(conn):
    """Anti-spam : une fois 'asked', le check-in n'est plus dû."""
    user = _user_with_monday_slot(conn)
    now = scheduler.local_now("Europe/Paris", fixed_clock(f"{MONDAY}T17:00"))
    due = scheduler.due_checkins(conn, user, now, 45)
    assert len(due) == 1

    repository.mark_checkin_asked(due[0]["id"], conn=conn)
    assert scheduler.due_checkins(conn, user, now, 45) == []


def test_quiet_hours_suppress(conn):
    user = _user_with_monday_slot(conn, quiet={"start": "22:00", "end": "08:00"})
    # 23:00 Paris = 21:00 UTC -> dans les heures calmes.
    due = scheduler.due_checkins(
        conn, user, scheduler.local_now("Europe/Paris", fixed_clock(f"{MONDAY}T21:00")), 45
    )
    assert due == []


def test_reschedule_creates_next_day_checkin(conn):
    """Report : nouveau check-in à J+1, relancé le lendemain."""
    user = _user_with_monday_slot(conn)
    now_mon = scheduler.local_now("Europe/Paris", fixed_clock(f"{MONDAY}T17:00"))
    due = scheduler.due_checkins(conn, user, now_mon, 45)[0]
    repository.mark_checkin_asked(due["id"], conn=conn)
    repository.resolve_checkin(due["id"], "rescheduled", reschedule_to=TUESDAY, conn=conn)
    repository.create_checkin(
        user["id"], due_date=TUESDAY, due_time="18:00", activity="jambes", conn=conn
    )

    # Mardi soir : le report doit ressortir comme dû.
    now_tue = scheduler.local_now("Europe/Paris", fixed_clock(f"{TUESDAY}T17:00"))
    due_tue = scheduler.due_checkins(conn, user, now_tue, 45)
    assert len(due_tue) == 1
    assert due_tue[0]["due_date"] == TUESDAY


@pytest.mark.parametrize(
    "moment,expected",
    [("07:00", True), ("09:00", False), ("23:00", True), ("21:59", False)],
)
def test_in_quiet_hours_crossing_midnight(moment, expected):
    prefs = '{"quiet_hours": {"start": "22:00", "end": "08:00"}}'
    assert scheduler.in_quiet_hours(prefs, scheduler.parse_hhmm(moment)) is expected
