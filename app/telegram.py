"""Client minimal de l'API Bot Telegram (envoi de messages, webhook) via httpx."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings


@dataclass
class IncomingMessage:
    """Message entrant normalisé : soit du texte, soit un vocal (file_id à transcrire)."""

    chat_id: int
    text: str | None = None
    voice_file_id: str | None = None

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


async def send_chat_action(chat_id: int, action: str = "typing") -> None:
    """Affiche l'indicateur « en train d'écrire… » côté Telegram (dure ~5 s)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(_url("sendChatAction"), json={"chat_id": chat_id, "action": action})
    except httpx.HTTPError:
        pass  # purement cosmétique : ne jamais bloquer le traitement pour ça


async def set_webhook() -> dict[str, Any]:
    settings = get_settings()
    if not settings.public_url:
        return {"ok": False, "reason": "PUBLIC_URL non défini"}
    webhook_url = f"{settings.public_url}{settings.webhook_path}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(_url("setWebhook"), json={"url": webhook_url})
        return resp.json()


def parse_update(update: dict[str, Any]) -> IncomingMessage | None:
    """Extrait un message texte OU vocal d'un update Telegram. None si rien d'exploitable."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return None
    chat = message.get("chat", {})
    if "id" not in chat:
        return None
    chat_id = int(chat["id"])

    if message.get("text"):
        return IncomingMessage(chat_id=chat_id, text=message["text"])
    # Note vocale (voice) ou fichier audio (audio).
    media = message.get("voice") or message.get("audio")
    if media and media.get("file_id"):
        return IncomingMessage(chat_id=chat_id, voice_file_id=media["file_id"])
    return None


async def download_file(file_id: str) -> bytes | None:
    """Récupère le contenu binaire d'un fichier Telegram à partir de son file_id."""
    token = get_settings().telegram_bot_token
    async with httpx.AsyncClient(timeout=30) as client:
        info = await client.post(_url("getFile"), json={"file_id": file_id})
        if info.status_code >= 400:
            return None
        file_path = info.json().get("result", {}).get("file_path")
        if not file_path:
            return None
        resp = await client.get(f"https://api.telegram.org/file/bot{token}/{file_path}")
        return resp.content if resp.status_code < 400 else None
