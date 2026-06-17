"""Quand l'agent échoue, l'utilisateur reçoit un message de repli (jamais un silence)."""

from __future__ import annotations

import sqlite3

import pytest

from app import coach, repository
from app.agent import AgentReply


def _fake_settings():
    from app.config import Settings

    return Settings(
        oauth_creds_path="x", oauth_seed_json="", model="test", telegram_bot_token="x",
        webhook_secret="s", public_url="", db_path=":memory:", tick_minutes=15,
        default_tz="Europe/Paris", checkin_grace_minutes=45, summary_keep_recent=20,
        summary_trigger=40, onboarding_idle_hours=20, onboarding_max_nudges=3,
        whisper_model="base", whisper_cache_dir="/tmp/w", whisper_language="fr",
    )


@pytest.fixture
def sent(monkeypatch, conn: sqlite3.Connection):
    """Câble coach sur le conn de test et capture les envois Telegram."""
    monkeypatch.setattr(coach, "get_connection", lambda: conn)
    monkeypatch.setattr(coach, "get_settings", _fake_settings)

    messages: list[dict] = []

    async def fake_send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        messages.append({"text": text, "reply_markup": reply_markup})

    async def fake_send_chat_action(chat_id: int, action: str = "typing") -> None:
        pass

    monkeypatch.setattr(coach.telegram, "send_message", fake_send_message)
    monkeypatch.setattr(coach.telegram, "send_chat_action", fake_send_chat_action)
    return messages


@pytest.mark.asyncio
async def test_handle_incoming_envoie_un_repli_si_agent_plante(sent, conn, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("API down")

    monkeypatch.setattr(coach.agent, "run_agent", boom)

    reply = await coach.handle_incoming(chat_id=999, text="salut")

    assert reply == coach.SERVER_ERROR_REPLY
    assert [m["text"] for m in sent] == [coach.SERVER_ERROR_REPLY]
    # Le message de l'utilisateur reste persisté ; aucune réponse assistant n'est enregistrée.
    user = repository.get_or_create_user(999, default_tz="Europe/Paris", conn=conn)
    roles = [m["role"] for m in repository.live_messages(user["id"], 0, 50, conn=conn)]
    assert roles == ["user"]


@pytest.mark.asyncio
async def test_handle_incoming_repond_normalement_si_agent_ok(sent, monkeypatch):
    monkeypatch.setattr(
        coach.agent, "run_agent", lambda *a, **k: AgentReply("Bien reçu, on s'y met 💪", [])
    )

    reply = await coach.handle_incoming(chat_id=999, text="salut")

    assert reply == "Bien reçu, on s'y met 💪"
    assert [m["text"] for m in sent] == ["Bien reçu, on s'y met 💪"]
    # Sans choix proposés, on retire un éventuel clavier fantôme.
    assert sent[-1]["reply_markup"] == {"remove_keyboard": True}


@pytest.mark.asyncio
async def test_handle_incoming_attache_les_boutons_et_decoupe_en_bulles(sent, conn, monkeypatch):
    reply = AgentReply(
        "Combien de fois par semaine ?[[NEXT]]Et plutôt le matin ou le soir ?",
        ["2x", "3x", "4x"],
    )
    monkeypatch.setattr(coach.agent, "run_agent", lambda *a, **k: reply)

    await coach.handle_incoming(chat_id=999, text="je veux un programme")

    # Deux bulles envoyées d'affilée ; le clavier seulement sur la dernière.
    assert [m["text"] for m in sent] == [
        "Combien de fois par semaine ?",
        "Et plutôt le matin ou le soir ?",
    ]
    assert sent[0]["reply_markup"] is None
    assert sent[1]["reply_markup"] == coach.telegram.reply_keyboard(["2x", "3x", "4x"])
    # Persistance : une seule entrée assistant, recollée sans le délimiteur.
    user = repository.get_or_create_user(999, default_tz="Europe/Paris", conn=conn)
    msgs = repository.live_messages(user["id"], 0, 50, conn=conn)
    assistant = [m["content"] for m in msgs if m["role"] == "assistant"]
    assert assistant == ["Combien de fois par semaine ?\n\nEt plutôt le matin ou le soir ?"]
