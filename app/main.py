"""Point d'entrée FastAPI : webhook Telegram + démarrage du moteur de proactivité."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request

from app import auth, coach, scheduler, telegram
from app.config import get_settings
from app.db import get_connection, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gym-coach")

_scheduler = AsyncIOScheduler()


async def _tick() -> None:
    settings = get_settings()
    try:
        n = await scheduler.run_tick(
            get_connection(),
            settings.checkin_grace_minutes,
            coach.handle_proactive,
            coach.handle_onboarding_nudge,
            onboarding_idle_hours=settings.onboarding_idle_hours,
            onboarding_max_nudges=settings.onboarding_max_nudges,
        )
        if n:
            logger.info("Tick : %d relance(s) déclenchée(s).", n)
    except Exception:  # le scheduler ne doit jamais mourir sur une erreur ponctuelle
        logger.exception("Erreur pendant le tick de proactivité")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db(settings.db_path)
    auth.seed_from_env()  # crée le fichier de creds OAuth au 1er démarrage si besoin
    result = await telegram.set_webhook()
    logger.info("setWebhook: %s", result)
    _scheduler.add_job(_tick, "interval", minutes=settings.tick_minutes, id="proactivity")
    _scheduler.start()
    logger.info("Coach démarré (tick toutes les %d min).", settings.tick_minutes)
    yield
    _scheduler.shutdown(wait=False)


app = FastAPI(title="gym-coach", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request) -> dict[str, bool]:
    if secret != get_settings().webhook_secret:
        raise HTTPException(status_code=403, detail="secret invalide")
    update = await request.json()
    parsed = telegram.parse_update(update)
    if parsed is None:
        return {"ok": True}
    if parsed.text:
        await coach.handle_incoming(parsed.chat_id, parsed.text)
    elif parsed.voice_file_id:
        await coach.handle_voice(parsed.chat_id, parsed.voice_file_id)
    return {"ok": True}


@app.post("/internal/tick")
async def manual_tick(secret: str) -> dict[str, str]:
    """Déclenche un tick à la demande (tests E2E). Protégé par le webhook_secret."""
    if secret != get_settings().webhook_secret:
        raise HTTPException(status_code=403, detail="secret invalide")
    await _tick()
    return {"status": "done"}
