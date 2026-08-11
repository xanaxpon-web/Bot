"""PythonAnywhere WSGI entry point for the Telegram bot."""

import asyncio
import logging
import os
import threading
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
_loop: asyncio.AbstractEventLoop | None = None
_loop_process_id: int | None = None
_loop_lock = threading.Lock()
_started = False


def _run_async(coroutine: Coroutine[object, object, _T]) -> _T:
    """Run PTB work serially on a loop owned by the current WSGI process."""
    global _loop, _loop_process_id, _started

    with _loop_lock:
        process_id = os.getpid()
        if _loop is None or _loop_process_id != process_id:
            _loop = asyncio.new_event_loop()
            _loop_process_id = process_id
            _started = False

        asyncio.set_event_loop(_loop)
        return _loop.run_until_complete(coroutine)


async def _ensure_application_started() -> None:
    """Initialize PTB once on the same loop used for all incoming updates."""
    global _started

    if _started:
        return

    await telegram_application.initialize()
    if telegram_application.post_init is not None:
        await telegram_application.post_init(telegram_application)
    await telegram_application.start()
    _started = True
    logger.info("Telegram application initialized for webhook processing")


async def _process_update(update_data: dict) -> None:
    await _ensure_application_started()
    update = Update.de_json(update_data, telegram_application.bot)
    await telegram_application.process_update(update)


def _process_update_after_response(update_data: dict) -> None:
    """Process an accepted update after the WSGI response has been emitted."""
    try:
        _run_async(_process_update(update_data))
    except Exception:
        logger.exception("Failed to process Telegram webhook update")


@app.get("/")
def healthcheck() -> Response:
    return Response("Bot is running", status=200, mimetype="text/plain")


@app.post("/telegram")
def telegram_webhook() -> Response:
    update_data = request.get_json(silent=True)
    if not isinstance(update_data, dict):
        return Response("Invalid Telegram update", status=400, mimetype="text/plain")

    response = Response("OK", status=200, mimetype="text/plain")
    response.call_on_close(lambda: _process_update_after_response(update_data))
    return response
