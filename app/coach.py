"""Orchestration : relie Telegram, la base, et la boucle Claude.

Un verrou par utilisateur sérialise les tours (un message entrant et une relance proactive ne
peuvent pas s'entrelacer sur le même utilisateur). La boucle Claude étant synchrone, elle tourne
dans un thread pour ne pas bloquer la boucle asyncio.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import defaultdict

from app import agent, repository, telegram, transcribe
from app.config import get_settings
from app.db import get_connection

logger = logging.getLogger("gym-coach")

_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# Message de repli quand le coach ne peut pas répondre (panne API, rate-limit, bug). Mieux
# vaut un mot honnête et une invitation à réessayer qu'un silence (l'utilisateur attend).
SERVER_ERROR_REPLY = (
    "Oups, j'ai un petit souci technique de mon côté 😅 "
    "Renvoie-moi ton message dans un instant, ça devrait repartir."
)

# Délimiteur (inséré par le modèle) qui sépare le texte en plusieurs bulles envoyées d'affilée.
# Token volontairement absent du markdown/français naturel pour éviter tout sur-découpage.
_BUBBLE_DELIMITER = "[[NEXT]]"
_MAX_BUBBLES = 4
# ReplyKeyboardRemove : retire un éventuel clavier restant quand un message n'a pas de boutons.
_REMOVE_KEYBOARD = {"remove_keyboard": True}


async def notify_failure(chat_id: int) -> None:
    """Prévient l'utilisateur d'un incident, sans jamais lever (best-effort)."""
    try:
        await telegram.send_message(chat_id, SERVER_ERROR_REPLY)
    except Exception:
        logger.exception("Impossible d'envoyer le message de repli à chat_id=%s", chat_id)


def _history(conn: sqlite3.Connection, user: sqlite3.Row) -> list[dict[str, str]]:
    """Messages 'vivants' (non encore résumés) à envoyer verbatim. Le reste est dans le résumé.

    Le cap protège contre une croissance non bornée si le résumé échoue ; en régime normal le
    nombre de messages vivants reste sous ce plafond.
    """
    settings = get_settings()
    through_id = user["summary_through_id"] if "summary_through_id" in user.keys() else 0
    cap = settings.summary_trigger + settings.summary_keep_recent
    return [
        {"role": m["role"], "content": m["content"]}
        for m in repository.live_messages(user["id"], through_id, cap, conn=conn)
    ]


def _maybe_summarize(conn: sqlite3.Connection, user_id: int) -> None:
    """Si trop de messages 'vivants', condense les PLUS ANCIENS dans le résumé glissant."""
    settings = get_settings()
    user = repository.get_user(user_id, conn=conn)
    through_id = user["summary_through_id"]
    n_live = repository.count_live_messages(user_id, through_id, conn=conn)
    if n_live <= settings.summary_trigger:
        return
    to_fold = repository.oldest_live_messages(
        user_id, through_id, n_live - settings.summary_keep_recent, conn=conn
    )
    if not to_fold:
        return
    new_through = to_fold[-1]["id"]
    folded = [{"role": m["role"], "content": m["content"]} for m in to_fold]
    try:
        new_summary = agent.summarize_conversation(user["summary"], folded)
    except Exception:
        return  # maintenance best-effort : ne jamais casser le tour pour ça
    if new_summary:
        repository.update_summary(user_id, new_summary, new_through, conn=conn)


def _split_bubbles(text: str) -> list[str]:
    """Découpe le texte en bulles sur les lignes contenant uniquement `[[NEXT]]`.

    Strip + drop des vides ; plafonné à `_MAX_BUBBLES` (le surplus est fusionné dans la dernière
    bulle pour ne rien perdre). Retourne `[]` si le texte est vide.
    """
    bubbles = [seg.strip() for seg in (text or "").split(_BUBBLE_DELIMITER) if seg.strip()]
    if len(bubbles) > _MAX_BUBBLES:
        head = bubbles[: _MAX_BUBBLES - 1]
        tail = "\n\n".join(bubbles[_MAX_BUBBLES - 1 :])
        bubbles = head + [tail]
    return bubbles


async def _deliver(
    chat_id: int, user_id: int, reply: agent.AgentReply, conn: sqlite3.Connection
) -> None:
    """Persiste la réponse (une fois) et l'envoie en une ou plusieurs bulles.

    Le clavier de boutons (ou un ReplyKeyboardRemove pour éviter un clavier fantôme) est attaché à la
    DERNIÈRE bulle uniquement. L'envoi est best-effort : on persiste le texte complet avant d'envoyer,
    pour que l'historique reste cohérent même si une bulle saute.
    """
    bubbles = _split_bubbles(reply.text) or ["C'est noté 👍"]
    repository.add_message(user_id, "assistant", "\n\n".join(bubbles), conn=conn)
    last = len(bubbles) - 1
    markup = telegram.reply_keyboard(reply.quick_replies) if reply.quick_replies else _REMOVE_KEYBOARD
    for i, bubble in enumerate(bubbles):
        await telegram.send_message(chat_id, bubble, reply_markup=markup if i == last else None)


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
        messages = _history(conn, user)

        try:
            reply = await asyncio.to_thread(agent.run_agent, conn, user, messages)
        except Exception:
            # Le message de l'utilisateur reste en base : il pourra simplement le renvoyer.
            logger.exception("run_agent a échoué pour chat_id=%s", chat_id)
            await notify_failure(chat_id)
            return SERVER_ERROR_REPLY
        await _deliver(chat_id, user["id"], reply, conn)
        await asyncio.to_thread(_maybe_summarize, conn, user["id"])

    return reply.text


async def handle_voice(chat_id: int, file_id: str) -> None:
    """Télécharge un vocal, le transcrit, puis le traite comme un message texte normal."""
    await telegram.send_chat_action(chat_id, "typing")
    audio = await telegram.download_file(file_id)
    if not audio:
        await telegram.send_message(chat_id, "Je n'ai pas réussi à récupérer ton vocal 😕")
        return
    text = await asyncio.to_thread(transcribe.transcribe, audio)
    if not text:
        await telegram.send_message(
            chat_id, "Je n'ai rien compris à ton vocal, tu peux réessayer ou m'écrire ?"
        )
        return
    await handle_incoming(chat_id, text)


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
            "pu faire sa séance. Propose-lui des réponses rapides (fait / pas encore / je reporte) "
            "via suggest_replies. N'enregistre rien maintenant (attends sa réponse)."
        ),
    }
    await telegram.send_chat_action(chat_id, "typing")
    async with _locks[chat_id]:
        fresh = repository.get_user(user["id"], conn=conn)
        messages = _history(conn, fresh) + [directive]
        reply = await asyncio.to_thread(agent.run_agent, conn, fresh, messages)
        if not reply.text:
            reply.text = f"Salut ! Tu as réussi à caser {activity} ?"
        await _deliver(chat_id, user["id"], reply, conn)
        repository.mark_checkin_asked(checkin["id"], conn=conn)


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
        messages = _history(conn, fresh) + [directive]
        reply = await asyncio.to_thread(agent.run_agent, conn, fresh, messages)
        if not reply.text:
            reply.text = "Hey ! On reprend quand tu veux pour finir de caler ton programme 💪"
        await _deliver(chat_id, user["id"], reply, conn)
        repository.increment_onboarding_nudge(user["id"], conn=conn)
