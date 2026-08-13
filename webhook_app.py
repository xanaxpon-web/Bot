"""PythonAnywhere WSGI entry point for the Telegram bot."""

import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
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


def _run_one_time_referral_cleanup() -> None:
    """Run the explicitly requested production-data cleanup exactly once."""
    target_user_id = 887845876
    request_id = "remove-referrals-887845876-20260813"
    database_path = Path("/home/ctttuu/users.db")
    result_path = Path(f"/home/ctttuu/{request_id}.json")
    lock_path = Path(f"/home/ctttuu/{request_id}.lock")

    if result_path.exists() or not database_path.exists():
        return

    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return

    os.close(lock_fd)
    result = {
        "request_id": request_id,
        "target_user_id": target_user_id,
        "ok": False,
    }
    connection = None
    try:
        backups_dir = Path("/home/ctttuu/backups")
        backups_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backups_dir / (
            f"users_before_referral_cleanup_{target_user_id}_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        )

        source = sqlite3.connect(database_path, timeout=30)
        backup = sqlite3.connect(backup_path)
        try:
            source.backup(backup)
        finally:
            backup.close()
            source.close()

        connection = sqlite3.connect(database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")

        target = connection.execute(
            "SELECT attempts FROM users WHERE user_id=?",
            (target_user_id,),
        ).fetchone()
        if target is None:
            raise RuntimeError(f"User {target_user_id} was not found")

        referral_ids = [
            int(row["referred_id"])
            for row in connection.execute(
                "SELECT referred_id FROM referrals WHERE referrer_id=? ORDER BY referred_id",
                (target_user_id,),
            ).fetchall()
        ]

        deleted_uploads = deleted_payments = 0
        if referral_ids:
            placeholders = ",".join("?" for _ in referral_ids)
            deleted_uploads = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM uploads WHERE user_id IN ({placeholders})",
                    referral_ids,
                ).fetchone()[0]
            )
            deleted_payments = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM payments WHERE user_id IN ({placeholders})",
                    referral_ids,
                ).fetchone()[0]
            )
            connection.execute(
                f"UPDATE users SET referrer=NULL WHERE referrer IN ({placeholders})",
                referral_ids,
            )
            connection.executemany(
                "DELETE FROM users WHERE user_id=?",
                [(user_id,) for user_id in referral_ids],
            )

        connection.execute(
            "DELETE FROM referrals WHERE referrer_id=?",
            (target_user_id,),
        )
        connection.execute(
            "UPDATE users SET attempts=0 WHERE user_id=?",
            (target_user_id,),
        )

        attempts_after = int(
            connection.execute(
                "SELECT attempts FROM users WHERE user_id=?",
                (target_user_id,),
            ).fetchone()[0]
        )
        remaining_referrals = int(
            connection.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id=?",
                (target_user_id,),
            ).fetchone()[0]
        )
        remaining_deleted_users = 0
        if referral_ids:
            placeholders = ",".join("?" for _ in referral_ids)
            remaining_deleted_users = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM users WHERE user_id IN ({placeholders})",
                    referral_ids,
                ).fetchone()[0]
            )

        if attempts_after != 0 or remaining_referrals != 0 or remaining_deleted_users != 0:
            raise RuntimeError("Cleanup verification failed")

        connection.commit()
        result.update(
            {
                "ok": True,
                "attempts_before": int(target["attempts"]),
                "attempts_after": attempts_after,
                "deleted_user_ids": referral_ids,
                "deleted_user_count": len(referral_ids),
                "deleted_upload_records": deleted_uploads,
                "deleted_payment_records": deleted_payments,
                "remaining_direct_referrals": remaining_referrals,
                "remaining_deleted_users": remaining_deleted_users,
                "backup_path": str(backup_path),
                "completed_at": datetime.now().isoformat(),
            }
        )
    except Exception as exc:
        if connection is not None:
            connection.rollback()
        result["error"] = f"{type(exc).__name__}: {exc}"
        logger.exception("One-time referral cleanup failed")
    finally:
        if connection is not None:
            connection.close()
        temporary_result = result_path.with_suffix(".tmp")
        temporary_result.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        temporary_result.replace(result_path)


_run_one_time_referral_cleanup()
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
    return Response("Bot is running v2", status=200, mimetype="text/plain")


@app.post("/telegram")
def telegram_webhook() -> Response:
    update_data = request.get_json(silent=True)
    if not isinstance(update_data, dict):
        return Response("Invalid Telegram update", status=400, mimetype="text/plain")

    response = Response("OK", status=200, mimetype="text/plain")
    response.call_on_close(lambda: _process_update_after_response(update_data))
    return response
