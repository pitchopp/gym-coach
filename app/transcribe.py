"""Transcription audio locale (faster-whisper), sans clé API externe.

Le modèle est chargé paresseusement au premier usage et mis en cache sur le volume `/data`
(téléchargé une seule fois). L'inférence CPU est sérialisée par un verrou.
"""

from __future__ import annotations

import os
import tempfile
import threading

from app.config import get_settings

_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # import lourd différé au 1er vocal

        settings = get_settings()
        _model = WhisperModel(
            settings.whisper_model,
            device="cpu",
            compute_type="int8",
            download_root=settings.whisper_cache_dir,
        )
    return _model


def transcribe(audio_bytes: bytes) -> str:
    """Transcrit un audio (ex. .oga/opus Telegram) en texte. Renvoie '' si rien d'exploitable."""
    settings = get_settings()
    with _lock:
        model = _get_model()
        with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
            tmp.write(audio_bytes)
            path = tmp.name
        try:
            language = settings.whisper_language or None
            segments, _ = model.transcribe(path, language=language)
            return " ".join(seg.text.strip() for seg in segments).strip()
        finally:
            os.unlink(path)
