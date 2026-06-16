"""Client minimal de l'API Bot Telegram (envoi de messages, webhook) via httpx."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings

_API_BASE = "https://api.telegram.org/bot{token}/{method}"


def _url(method: str) -> str:
    return _API_BASE.format(token=get_settings().telegram_bot_token, method=method)


async def send_message(chat_id: int, text: str) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            _url("sendMessage"),
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        )
        if resp.status_code >= 400:
            # Markdown invalide ? On retente en texte brut pour ne pas perdre le message.
            await client.post(_url("sendMessage"), json={"chat_id": chat_id, "text": text})


async def set_webhook() -> dict[str, Any]:
    settings = get_settings()
    if not settings.public_url:
        return {"ok": False, "reason": "PUBLIC_URL non défini"}
    webhook_url = f"{settings.public_url}{settings.webhook_path}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(_url("setWebhook"), json={"url": webhook_url})
        return resp.json()


def parse_update(update: dict[str, Any]) -> tuple[int, str] | None:
    """Extrait (chat_id, texte) d'un update Telegram. None si pas un message texte exploitable."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return None
    text = message.get("text")
    chat = message.get("chat", {})
    if not text or "id" not in chat:
        return None
    return int(chat["id"]), text
