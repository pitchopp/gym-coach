"""Moteur de proactivité.

Logique pure et testable (horloge injectable) qui décide *quoi* relancer et *quand*, séparée de
l'envoi effectif (Telegram) et de la rédaction (Claude), injectés via un callback.

Règles :
- Matérialisation idempotente : chaque jour, pour chaque créneau récurrent, un check-in 'pending'.
- Un check-in n'est relancé qu'une seule fois (transition pending -> asked).
- On ne relance qu'après le créneau (+ délai de grâce) et hors heures calmes.
- Un report ("j'irai demain") crée un nouveau check-in à la date cible (géré côté tools).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app import repository

# Renvoie l'instant courant en UTC (aware). Injectable pour les tests.
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def local_now(tz_name: str, clock: Clock = _utcnow) -> datetime:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Paris")
    return clock().astimezone(tz)


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def in_quiet_hours(comm_prefs: str, moment: time) -> bool:
    """Vrai si `moment` tombe dans la plage d'heures calmes définie dans comm_prefs (JSON)."""
    try:
        prefs = json.loads(comm_prefs or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    quiet = prefs.get("quiet_hours")
    if not quiet or "start" not in quiet or "end" not in quiet:
        return False
    start, end = parse_hhmm(quiet["start"]), parse_hhmm(quiet["end"])
    if start <= end:
        return start <= moment < end
    # Plage qui traverse minuit (ex: 22:00 -> 08:00).
    return moment >= start or moment < end


def materialize_day(
    conn: sqlite3.Connection, user_id: int, local_date: date
) -> None:
    """Crée les check-ins 'pending' du jour à partir des créneaux récurrents (idempotent)."""
    weekday = local_date.weekday()  # 0 = lundi
    iso = local_date.isoformat()
    for slot in repository.list_slots(user_id, conn=conn):
        if slot["weekday"] != weekday:
            continue
        repository.create_checkin(
            user_id,
            due_date=iso,
            due_time=slot["time"],
            slot_id=slot["id"],
            activity=slot["activity"],
            conn=conn,
        )


def due_checkins(
    conn: sqlite3.Connection,
    user: sqlite3.Row,
    now_local: datetime,
    grace_minutes: int,
) -> list[sqlite3.Row]:
    """Check-ins à relancer maintenant : échus, après créneau + grâce, hors heures calmes."""
    if in_quiet_hours(user["comm_prefs"], now_local.time()):
        return []

    today = now_local.date()
    materialize_day(conn, user["id"], today)

    ready: list[sqlite3.Row] = []
    for checkin in repository.list_due_checkins(user["id"], today.isoformat(), conn=conn):
        due_dt = datetime.combine(
            date.fromisoformat(checkin["due_date"]),
            parse_hhmm(checkin["due_time"]),
            tzinfo=now_local.tzinfo,
        )
        if now_local >= due_dt + timedelta(minutes=grace_minutes):
            ready.append(checkin)
    return ready


async def run_tick(
    conn: sqlite3.Connection,
    grace_minutes: int,
    send_proactive: Callable[[sqlite3.Row, sqlite3.Row], Awaitable[None]],
    clock: Clock = _utcnow,
) -> int:
    """Un passage du scheduler : pour chaque utilisateur actif, déclenche les relances dues.

    `send_proactive(user, checkin)` doit rédiger + envoyer le message puis marquer le check-in
    'asked'. Renvoie le nombre de relances déclenchées.
    """
    triggered = 0
    for user in repository.list_active_users(conn=conn):
        now_local = local_now(user["timezone"], clock)
        for checkin in due_checkins(conn, user, now_local, grace_minutes):
            await send_proactive(user, checkin)
            triggered += 1
    return triggered
