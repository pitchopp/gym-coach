"""Tests du parsing des updates Telegram (texte vs vocal)."""

from __future__ import annotations

from app.telegram import parse_update


def test_parse_text_message():
    msg = parse_update({"message": {"chat": {"id": 42}, "text": "salut"}})
    assert msg is not None and msg.chat_id == 42 and msg.text == "salut"
    assert msg.voice_file_id is None


def test_parse_voice_message():
    msg = parse_update({"message": {"chat": {"id": 7}, "voice": {"file_id": "AbC123"}}})
    assert msg is not None and msg.chat_id == 7
    assert msg.voice_file_id == "AbC123" and msg.text is None


def test_parse_audio_file():
    msg = parse_update({"message": {"chat": {"id": 7}, "audio": {"file_id": "Zzz"}}})
    assert msg is not None and msg.voice_file_id == "Zzz"


def test_parse_ignores_other_updates():
    assert parse_update({"update_id": 1}) is None
    assert parse_update({"message": {"chat": {"id": 1}, "sticker": {}}}) is None
    assert parse_update({"message": {"text": "x"}}) is None  # pas de chat id
