"""PythonAnywhere WSGI entry point for the Telegram bot."""

import asyncio
import logging
import os
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Coroutine, TypeVar

from flask import Flask, Response, request


# PythonAnywhere starts WSGI independently of the shell's current directory.
# Change it before importing main so all relative bot paths resolve correctly.
PYTHONANYWHERE_PROJECT_DIR = Path("/home/ctttuu/Bot")
PROJECT_DIR = (
    PYTHONANYWHERE_PROJECT_DIR
    if PYTHONANYWHERE_PROJECT_DIR.is_dir()
    else Path(__file__).resolve().parent
)
os.chdir(PROJECT_DIR)

from main import build_application  # noqa: E402
from telegram import Update  # noqa: E402


logger = logging.getLogger(__name__)
app = Flask(__name__)
telegram_application = build_application()

_T = TypeVar("_T")
_loop = asyncio.new_event_loop()
_loop_ready = threading.Event()
_startup_lock: asyncio.Lock | None = None
_started = False


def _run_event_loop() -> None:
    asyncio.set_event_loop(_loop)
    _loop_ready.set()
    _loop.run_forever()


_loop_thread = threading.Thread(
    target=_run_event_loop,
    name="telegram-asyncio-loop",
    daemon=True,
)
_loop_thread.start()
_loop_ready.wait()


def _submit(coroutine: Coroutine[object, object, _T]) -> Future[_T]:
    """Run a coroutine on the bot's persistent asyncio loop."""
    return asyncio.run_coroutine_threadsafe(coroutine, _loop)


async def _ensure_application_started() -> None:
    """Initialize PTB once on the same loop used for all incoming updates."""
    global _startup_lock, _started

    if _started:
        return
    if _startup_lock is None:
        _startup_lock = asyncio.Lock()

    async with _startup_lock:
        if _started:
            return

        await telegram_application.initialize()
        if telegram_application.post_init is not None:
            await telegram_application.post_init(telegram_application)
        await telegram_application.start()
        _started = True
        logger.info("Telegram application initialized for webhook processing")


async def _enqueue_update(update_data: dict) -> None:
    await _ensure_application_started()
    update = Update.de_json(update_data, telegram_application.bot)
    await telegram_application.update_queue.put(update)


@app.get("/")
def healthcheck() -> Response:
    return Response("Bot is running", status=200, mimetype="text/plain")


@app.post("/telegram")
def telegram_webhook() -> Response:
    update_data = request.get_json(silent=True)
    if not isinstance(update_data, dict):
        return Response("Invalid Telegram update", status=400, mimetype="text/plain")

    try:
        _submit(_enqueue_update(update_data)).result(timeout=30)
    except Exception:
        logger.exception("Failed to accept Telegram webhook update")
        return Response("Webhook processing failed", status=500, mimetype="text/plain")

    return Response("OK", status=200, mimetype="text/plain")
