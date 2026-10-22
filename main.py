"""Optional local health endpoint for the SQLite queue.

The Telegram ingestion process is now telegram_listener.py and is managed by
systemd as darvas-telegram-listener. This file does not start Telethon.
"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from config import CHANNEL_MAPPINGS, LIVE_TRADING_ENABLED
from signal_queue import initialize_queue, queue_stats, default_database_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("darvas_queue_health")

@asynccontextmanager
async def lifespan(app: FastAPI):
    path = initialize_queue()
    logger.info("SQLite queue health app using %s; Telegram ingestion runs separately", path)
    yield

app = FastAPI(title="Darvas Queue Health", version="4.0.0", lifespan=lifespan)

@app.get("/health")
async def health_check():
    try:
        stats = queue_stats()
        return {"status": "healthy", "telegram_listener": "separate systemd service",
                "signal_database": default_database_path(), "queue": stats,
                "channels_configured": len(CHANNEL_MAPPINGS), "live_trading_enabled": LIVE_TRADING_ENABLED}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Signal queue unavailable: {exc}")

@app.get("/ready")
async def readiness_check():
    try:
        stats = queue_stats()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Signal queue unavailable: {exc}")
    if not CHANNEL_MAPPINGS:
        raise HTTPException(status_code=503, detail="No Telegram channel mappings configured")
    return {"ready": True, "signal_queue_ready": True, "queue": stats,
            "channels_configured": len(CHANNEL_MAPPINGS), "live_trading_enabled": LIVE_TRADING_ENABLED}
