"""Orchestration : relie Telegram, la base, et la boucle Claude.

Un verrou par utilisateur sérialise les tours (un message entrant et une relance proactive ne
peuvent pas s'entrelacer sur le même utilisateur). La boucle Claude étant synchrone, elle tourne
dans un thread pour ne pas bloquer la boucle asyncio.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import defaultdict

from app import agent, repository, telegram
from app.config import get_settings
from app.db import get_connection

_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def _history(conn: sqlite3.Connection, user_id: int) -> list[dict[str, str]]:
    limit = get_settings().history_limit
    return [
        {"role": m["role"], "content": m["content"]}
        for m in repository.recent_messages(user_id, limit, conn=conn)
    ]


async def handle_incoming(chat_id: int, text: str) -> str:
    """Traite un message entrant : persiste, fait répondre le coach, envoie la réponse."""
    settings = get_settings()
    conn = get_connection()
    await telegram.send_chat_action(chat_id, "typing")
    async with _locks[chat_id]:
        user = repository.get_or_create_user(chat_id, default_tz=settings.default_tz, conn=conn)
        # L'utilisateur répond → on remet à zéro le compteur de relances d'onboarding.
        repository.reset_onboarding_nudges(user["id"], conn=conn)
        repository.add_message(user["id"], "user", text, conn=conn)
        messages = _history(conn, user["id"])

        reply = await asyncio.to_thread(agent.run_agent, conn, user, messages)
        if not reply:
            reply = "C'est noté 👍"
        repository.add_message(user["id"], "assistant", reply, conn=conn)

    await telegram.send_message(chat_id, reply)
    return reply


async def handle_proactive(user: sqlite3.Row, checkin: sqlite3.Row) -> None:
    """Déclenche une relance : Claude rédige le message, on l'envoie, on marque le check-in 'asked'."""
    conn = get_connection()
    chat_id = user["telegram_chat_id"]
    activity = checkin["activity"] or "ta séance"
    directive = {
        "role": "user",
        "content": (
            "[CONSIGNE INTERNE — ne pas mentionner ce message] "
            f"C'est l'heure de prendre des nouvelles : l'utilisateur avait prévu « {activity} » "
            f"le {checkin['due_date']}. Écris-lui un message court et naturel pour savoir s'il a "
            "pu faire sa séance. N'enregistre rien maintenant (attends sa réponse)."
        ),
    }
    await telegram.send_chat_action(chat_id, "typing")
    async with _locks[chat_id]:
        fresh = repository.get_user(user["id"], conn=conn)
        messages = _history(conn, user["id"]) + [directive]
        text = await asyncio.to_thread(agent.run_agent, conn, fresh, messages)
        if not text:
            text = f"Salut ! Tu as réussi à caser {activity} ?"
        repository.add_message(user["id"], "assistant", text, conn=conn)
        repository.mark_checkin_asked(checkin["id"], conn=conn)

    await telegram.send_message(chat_id, text)


async def handle_onboarding_nudge(user: sqlite3.Row) -> None:
    """Relance l'utilisateur pour finir son onboarding (Claude rédige), puis incrémente le compteur."""
    conn = get_connection()
    chat_id = user["telegram_chat_id"]
    directive = {
        "role": "user",
        "content": (
            "[CONSIGNE INTERNE — ne pas mentionner ce message] L'utilisateur a commencé à discuter "
            "mais n'a pas terminé de configurer son suivi. Relance-le gentiment et brièvement pour "
            "compléter ce qui manque encore (objectif, jours/horaires d'entraînement, préférences). "
            "Ne redemande pas ce qu'il a déjà donné, varie la formulation, reste léger et non "
            "insistant. Une seule courte question à la fois."
        ),
    }
    await telegram.send_chat_action(chat_id, "typing")
    async with _locks[chat_id]:
        fresh = repository.get_user(user["id"], conn=conn)
        messages = _history(conn, user["id"]) + [directive]
        text = await asyncio.to_thread(agent.run_agent, conn, fresh, messages)
        if not text:
            text = "Hey ! On reprend quand tu veux pour finir de caler ton programme 💪"
        repository.add_message(user["id"], "assistant", text, conn=conn)
        repository.increment_onboarding_nudge(user["id"], conn=conn)

    await telegram.send_message(chat_id, text)
