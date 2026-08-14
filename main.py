import asyncio
import json
import logging
import sqlite3
import tempfile
import time
import shutil
import uuid
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path
import exifread
import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# ============================================================
# НАСТРОЙКИ
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ВПИШИ СВОИ ДАННЫЕ НИЖЕ.
# Старые токены лучше не использовать — перевыпусти их, если они уже где-то публиковались.
BOT_TOKEN = "8609768853:AAG0Lc2uP3rZ9V3p_YCcHOZcg0GbmoJTbkA"
CRYPTO_TOKEN = "620273:AA2OobGrPhpIyRzlcts86t8fiqn5eBJ8FeN"  # Если Crypto Bot не нужен, оставь пустую строку: ""
ADMIN_ID = 1594601701
SUPPORT_USERNAME = "exifsupport"
PAYMENT_CARD = "4441 1144 6936 4374"

DB_FILE = Path("/home/ctttuu/users.db")
LEGACY_USER_FILE = Path("users.json")
UPLOADS_DIR = Path("saved_photos")
CRYPTO_API_URL = "https://pay.crypt.bot/api"
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 МБ
FREE_ATTEMPTS = 2
PHOTO_RETENTION_DAYS = 7
BACKUP_INTERVAL_HOURS = 24
ANTI_SPAM_SECONDS = 1.2
DEFAULT_CARD_PRICE_PER_ATTEMPT = 15
DEFAULT_STARS_PRICE_PER_ATTEMPT = 15
REFERRAL_MILESTONES = {1: 1, 5: 7, 10: 15}  # количество друзей -> суммарный бонус
BACKUPS_DIR = Path("backups")
LOG_FILE = Path("bot.log")

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logger.addHandler(_file_handler)

ALLOWED_ATTEMPT_PACKS = {1, 5, 10, 20, 30}
CRYPTO_PRICES = {
    1: 0.40,
    5: 1.70,
    10: 3.40,
    20: 6.80,
    30: 10.20,
}

# ============================================================
# БАЗА ДАННЫХ SQLITE
# ============================================================


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                reg TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                used INTEGER NOT NULL DEFAULT 0 CHECK(used >= 0),
                referrer INTEGER,
                bonus INTEGER NOT NULL DEFAULT 0 CHECK(bonus >= 0)
            );

            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (referrer_id, referred_id),
                FOREIGN KEY (referrer_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (referred_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                original_name TEXT,
                stored_name TEXT NOT NULL UNIQUE,
                file_path TEXT NOT NULL,
                mime_type TEXT,
                file_size INTEGER,
                telegram_file_id TEXT,
                uploaded_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                method TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                status TEXT NOT NULL,
                external_id TEXT UNIQUE,
                payload TEXT,
                created_at TEXT NOT NULL,
                paid_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS referral_milestones (
                user_id INTEGER NOT NULL,
                milestone INTEGER NOT NULL,
                awarded_at TEXT NOT NULL,
                PRIMARY KEY (user_id, milestone),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_uploads_user ON uploads(user_id);
            CREATE INDEX IF NOT EXISTS idx_uploads_time ON uploads(uploaded_at);
            CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
            CREATE INDEX IF NOT EXISTS idx_payments_time ON payments(created_at);
            """
        )
        # Мягкие миграции для старой базы.
        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "banned" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
        upload_cols = {r["name"] for r in conn.execute("PRAGMA table_info(uploads)").fetchall()}
        if "meta_json" not in upload_cols:
            conn.execute("ALTER TABLE uploads ADD COLUMN meta_json TEXT")
        conn.commit()


def save_upload_record(
    user_id: int,
    original_name: str | None,
    stored_name: str,
    file_path: str,
    mime_type: str | None,
    file_size: int,
    telegram_file_id: str | None,
    meta_json: str | None = None,
) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO uploads
            (user_id, original_name, stored_name, file_path, mime_type, file_size, telegram_file_id, uploaded_at, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                original_name,
                stored_name,
                file_path,
                mime_type,
                int(file_size),
                telegram_file_id,
                datetime.now().isoformat(),
                meta_json,
            ),
        )
        conn.commit()


def persist_uploaded_photo(user_id: int, source_path: Path, doc, meta_json: str | None = None) -> Path:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = source_path.suffix[:10] or ".img"
    stored_name = f"{int(user_id)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:10]}{suffix}"
    dest = UPLOADS_DIR / stored_name
    shutil.copy2(source_path, dest)
    save_upload_record(
        user_id=user_id,
        original_name=getattr(doc, "file_name", None),
        stored_name=stored_name,
        file_path=str(dest),
        mime_type=getattr(doc, "mime_type", None),
        file_size=dest.stat().st_size,
        telegram_file_id=getattr(doc, "file_id", None),
        meta_json=meta_json,
    )
    return dest


def migrate_legacy_users() -> None:
    """Одноразово переносит старый users.json в SQLite, не удаляя JSON."""
    if not LEGACY_USER_FILE.exists():
        return

    try:
        with db_connect() as conn:
            existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if existing > 0:
                return

        with LEGACY_USER_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("users.json должен содержать объект")

        with db_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            for uid, user in data.items():
                try:
                    user_id = int(uid)
                except (TypeError, ValueError):
                    logger.warning("Пропущен некорректный user_id из JSON: %r", uid)
                    continue

                reg = user.get("reg") or datetime.now().isoformat()
                attempts = max(0, int(user.get("attempts", FREE_ATTEMPTS)))
                used = max(0, int(user.get("used", 0)))
                bonus = max(0, int(user.get("bonus", 0)))
                referrer = user.get("referrer")
                try:
                    referrer = int(referrer) if referrer is not None else None
                except (TypeError, ValueError):
                    referrer = None

                conn.execute(
                    """
                    INSERT OR IGNORE INTO users
                    (user_id, reg, attempts, used, referrer, bonus)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, reg, attempts, used, referrer, bonus),
                )

            for uid, user in data.items():
                try:
                    referrer_id = int(uid)
                except (TypeError, ValueError):
                    continue

                for referred in user.get("referrals", []) or []:
                    try:
                        referred_id = int(referred)
                    except (TypeError, ValueError):
                        continue
                    if referrer_id == referred_id:
                        continue
                    exists = conn.execute(
                        "SELECT 1 FROM users WHERE user_id = ?", (referred_id,)
                    ).fetchone()
                    if exists:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO referrals
                            (referrer_id, referred_id, created_at)
                            VALUES (?, ?, ?)
                            """,
                            (referrer_id, referred_id, datetime.now().isoformat()),
                        )

            conn.commit()
        logger.info("Старый users.json успешно перенесён в %s", DB_FILE)
    except (OSError, json.JSONDecodeError, ValueError, sqlite3.Error) as exc:
        logger.exception("Не удалось перенести users.json: %s", exc)


def row_to_user(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    refs = conn.execute(
        "SELECT referred_id FROM referrals WHERE referrer_id = ? ORDER BY created_at",
        (row["user_id"],),
    ).fetchall()
    return {
        "reg": row["reg"],
        "attempts": row["attempts"],
        "used": row["used"],
        "referrer": str(row["referrer"]) if row["referrer"] is not None else None,
        "referrals": [str(r["referred_id"]) for r in refs],
        "bonus": row["bonus"],
        "banned": bool(row["banned"]) if "banned" in row.keys() else False,
    }


def get_or_create_user(user_id: int, referrer=None) -> dict:
    uid = int(user_id)
    ref_id = None
    try:
        if referrer is not None:
            candidate = int(referrer)
            if candidate != uid:
                ref_id = candidate
    except (TypeError, ValueError):
        ref_id = None

    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (uid,)
        ).fetchone()

        if existing is None:
            # Реферер учитывается только если он уже существует.
            valid_referrer = None
            if ref_id is not None:
                ref_exists = conn.execute(
                    "SELECT 1 FROM users WHERE user_id = ?", (ref_id,)
                ).fetchone()
                if ref_exists:
                    valid_referrer = ref_id

            conn.execute(
                """
                INSERT INTO users (user_id, reg, attempts, used, referrer, bonus)
                VALUES (?, ?, ?, 0, ?, 0)
                """,
                (uid, datetime.now().isoformat(), FREE_ATTEMPTS, valid_referrer),
            )

            if valid_referrer is not None:
                inserted = conn.execute(
                    """
                    INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (valid_referrer, uid, datetime.now().isoformat()),
                ).rowcount
                if inserted:
                    conn.execute(
                        """
                        UPDATE users
                        SET attempts = attempts + 1, bonus = bonus + 1
                        WHERE user_id = ?
                        """,
                        (valid_referrer,),
                    )

        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone()
        conn.commit()
        return row_to_user(conn, row)


def get_user(user_id: int) -> dict | None:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        return row_to_user(conn, row) if row else None


def reserve_attempt(user_id: int) -> bool:
    """Атомарно резервирует одну попытку до обработки файла."""
    if int(user_id) == ADMIN_ID:
        return True

    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            UPDATE users
            SET attempts = attempts - 1
            WHERE user_id = ? AND attempts > 0
            """,
            (int(user_id),),
        )
        conn.commit()
        return cur.rowcount == 1


def finalize_attempt(user_id: int) -> None:
    if int(user_id) == ADMIN_ID:
        return
    with db_connect() as conn:
        conn.execute(
            "UPDATE users SET used = used + 1 WHERE user_id = ?",
            (int(user_id),),
        )
        conn.commit()


def refund_attempt(user_id: int) -> None:
    if int(user_id) == ADMIN_ID:
        return
    with db_connect() as conn:
        conn.execute(
            "UPDATE users SET attempts = attempts + 1 WHERE user_id = ?",
            (int(user_id),),
        )
        conn.commit()


def add_attempts(user_id, count: int) -> bool:
    try:
        uid = int(user_id)
        count = int(count)
    except (TypeError, ValueError):
        return False
    if count <= 0:
        return False

    with db_connect() as conn:
        cur = conn.execute(
            "UPDATE users SET attempts = attempts + ? WHERE user_id = ?",
            (count, uid),
        )
        conn.commit()
        return cur.rowcount == 1


def get_stats() -> tuple[int, int, int, int, int]:
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(attempts), 0) AS attempts,
                   COALESCE(SUM(used), 0) AS used,
                   COALESCE(SUM(bonus), 0) AS bonus
            FROM users
            """
        ).fetchone()
        referrals = conn.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
        return row["total"], row["attempts"], row["used"], referrals, row["bonus"]


def get_all_users(limit: int | None = None, offset: int = 0) -> list[tuple[str, dict]]:
    with db_connect() as conn:
        # Новые пользователи сверху — так админ сразу видит свежие регистрации.
        sql = "SELECT * FROM users ORDER BY reg DESC"
        params = ()
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = (int(limit), max(0, int(offset)))
        rows = conn.execute(sql, params).fetchall()
        return [(str(row["user_id"]), row_to_user(conn, row)) for row in rows]


def get_all_user_ids() -> list[int]:
    with db_connect() as conn:
        return [r[0] for r in conn.execute("SELECT user_id FROM users").fetchall()]



def is_banned(user_id: int) -> bool:
    if int(user_id) == ADMIN_ID:
        return False
    with db_connect() as conn:
        row = conn.execute("SELECT banned FROM users WHERE user_id = ?", (int(user_id),)).fetchone()
        return bool(row and row["banned"])


def set_banned(user_id: int, banned: bool) -> bool:
    if int(user_id) == ADMIN_ID:
        return False
    with db_connect() as conn:
        cur = conn.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if banned else 0, int(user_id)))
        conn.commit()
        return cur.rowcount == 1


def remove_attempts(user_id: int, count: int) -> bool:
    try:
        uid, count = int(user_id), int(count)
    except (TypeError, ValueError):
        return False
    if count <= 0:
        return False
    with db_connect() as conn:
        cur = conn.execute(
            "UPDATE users SET attempts = MAX(0, attempts - ?) WHERE user_id = ?",
            (count, uid),
        )
        conn.commit()
        return cur.rowcount == 1


def get_user_uploads(user_id: int, limit: int = 10, offset: int = 0):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM uploads WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (int(user_id), int(limit), max(0, int(offset))),
        ).fetchall()


def get_upload(upload_id: int):
    with db_connect() as conn:
        return conn.execute("SELECT * FROM uploads WHERE id=?", (int(upload_id),)).fetchone()


def get_admin_user_details(user_id: int) -> dict:
    uid = int(user_id)
    with db_connect() as conn:
        uploads = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(file_size), 0) AS total_size,
                   MAX(uploaded_at) AS last_upload
            FROM uploads WHERE user_id=?
            """,
            (uid,),
        ).fetchone()
        payments = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END), 0) AS paid,
                   COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0) AS pending,
                   COALESCE(SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END), 0) AS rejected
            FROM payments WHERE user_id=?
            """,
            (uid,),
        ).fetchone()
        paid_totals = conn.execute(
            """
            SELECT currency, COALESCE(SUM(amount), 0) AS amount
            FROM payments
            WHERE user_id=? AND status='paid'
            GROUP BY currency
            ORDER BY currency
            """,
            (uid,),
        ).fetchall()
        return {
            "upload_count": int(uploads["total"]),
            "upload_size": int(uploads["total_size"]),
            "last_upload": uploads["last_upload"],
            "payment_count": int(payments["total"]),
            "paid_count": int(payments["paid"]),
            "pending_count": int(payments["pending"]),
            "rejected_count": int(payments["rejected"]),
            "paid_totals": [(row["currency"], row["amount"]) for row in paid_totals],
        }


def get_admin_user_activity(user_id: int, limit: int = 8, offset: int = 0):
    uid = int(user_id)
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT event_type, event_id, event_time, label, status, amount, currency, attempts
            FROM (
                SELECT 'registration' AS event_type,
                       user_id AS event_id,
                       reg AS event_time,
                       NULL AS label,
                       NULL AS status,
                       NULL AS amount,
                       NULL AS currency,
                       NULL AS attempts
                FROM users WHERE user_id=?

                UNION ALL

                SELECT 'upload', id, uploaded_at,
                       COALESCE(original_name, stored_name),
                       NULL, file_size, NULL, NULL
                FROM uploads WHERE user_id=?

                UNION ALL

                SELECT 'payment', id, created_at, method,
                       status, amount, currency, attempts
                FROM payments WHERE user_id=?

                UNION ALL

                SELECT 'referral', referred_id, created_at,
                       CAST(referred_id AS TEXT),
                       NULL, NULL, NULL, NULL
                FROM referrals WHERE referrer_id=?
            )
            ORDER BY event_time DESC
            LIMIT ? OFFSET ?
            """,
            (uid, uid, uid, uid, int(limit), max(0, int(offset))),
        ).fetchall()


def get_admin_user_activity_count(user_id: int) -> int:
    uid = int(user_id)
    with db_connect() as conn:
        return int(
            conn.execute(
                """
                SELECT 1
                     + (SELECT COUNT(*) FROM uploads WHERE user_id=?)
                     + (SELECT COUNT(*) FROM payments WHERE user_id=?)
                     + (SELECT COUNT(*) FROM referrals WHERE referrer_id=?)
                """,
                (uid, uid, uid),
            ).fetchone()[0]
        )


def card_price_per_attempt() -> int:
    try:
        return max(1, int(get_setting("card_price_per_attempt", str(DEFAULT_CARD_PRICE_PER_ATTEMPT))))
    except ValueError:
        return DEFAULT_CARD_PRICE_PER_ATTEMPT


def stars_price_per_attempt() -> int:
    try:
        return max(1, int(get_setting("stars_price_per_attempt", str(DEFAULT_STARS_PRICE_PER_ATTEMPT))))
    except ValueError:
        return DEFAULT_STARS_PRICE_PER_ATTEMPT


def retention_days() -> int:
    try:
        return max(1, min(365, int(get_setting("photo_retention_days", str(PHOTO_RETENTION_DAYS)))))
    except ValueError:
        return PHOTO_RETENTION_DAYS


def setting_enabled(key: str, default: bool = True) -> bool:
    return get_setting(key, "1" if default else "0") == "1"


def payment_method_enabled(method: str) -> bool:
    return setting_enabled(f"payment_{method}", True)


def notification_enabled(key: str) -> bool:
    return setting_enabled(f"notify_{key}", True)


def get_finance_stats() -> dict:
    now = datetime.now()
    cutoffs = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
        "week": (now - timedelta(days=7)).isoformat(),
        "month": (now - timedelta(days=30)).isoformat(),
    }
    result = {}
    with db_connect() as conn:
        for label, cutoff in cutoffs.items():
            rows = conn.execute(
                """SELECT currency, COALESCE(SUM(amount),0) total, COUNT(*) cnt
                   FROM payments WHERE status='paid' AND paid_at>=?
                   GROUP BY currency""",
                (cutoff,),
            ).fetchall()
            result[label] = {r["currency"]: (float(r["total"]), int(r["cnt"])) for r in rows}
        popular = conn.execute(
            """SELECT attempts, COUNT(*) cnt FROM payments
               WHERE status='paid' GROUP BY attempts ORDER BY cnt DESC, attempts DESC LIMIT 5"""
        ).fetchall()
        methods = conn.execute(
            """SELECT method, COUNT(*) cnt FROM payments
               WHERE status='paid' GROUP BY method ORDER BY cnt DESC"""
        ).fetchall()
    result["popular"] = [(int(r["attempts"]), int(r["cnt"])) for r in popular]
    result["methods"] = [(r["method"], int(r["cnt"])) for r in methods]
    return result


def referral_progress(user_id: int) -> tuple[int, int, int]:
    with db_connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (int(user_id),)).fetchone()[0]
        user = conn.execute("SELECT bonus FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        bonus = int(user["bonus"]) if user else 0
    next_target = next((m for m in sorted(REFERRAL_MILESTONES) if count < m), max(REFERRAL_MILESTONES))
    return count, bonus, next_target


def award_referral_milestones(referrer_id: int) -> int:
    """Доводит суммарный реферальный бонус до уровня milestone. Возвращает сколько добавлено сейчас."""
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        count = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (int(referrer_id),)).fetchone()[0]
        row = conn.execute("SELECT bonus FROM users WHERE user_id=?", (int(referrer_id),)).fetchone()
        if not row:
            conn.commit()
            return 0
        current_bonus = int(row["bonus"])
        target_bonus = 0
        reached = []
        for milestone, total_bonus in sorted(REFERRAL_MILESTONES.items()):
            if count >= milestone:
                target_bonus = max(target_bonus, total_bonus)
                reached.append(milestone)
        delta = max(0, target_bonus - current_bonus)
        if delta:
            conn.execute(
                "UPDATE users SET attempts=attempts+?, bonus=bonus+? WHERE user_id=?",
                (delta, delta, int(referrer_id)),
            )
        for m in reached:
            conn.execute(
                "INSERT OR IGNORE INTO referral_milestones(user_id,milestone,awarded_at) VALUES(?,?,?)",
                (int(referrer_id), int(m), datetime.now().isoformat()),
            )
        conn.commit()
        return delta


async def access_guard(update: Update) -> bool:
    if not await maintenance_guard(update):
        return False
    uid = update.effective_user.id if update.effective_user else 0
    if uid and is_banned(uid):
        msg = f"⛔ Доступ к боту ограничен.\n\nЕсли считаешь, что это ошибка — напиши @{SUPPORT_USERNAME}."
        if update.callback_query:
            await update.callback_query.answer("Доступ ограничен", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(msg)
        return False
    return True


def progress_bar(current: int, target: int, width: int = 10) -> str:
    if target <= 0:
        return "█" * width
    filled = min(width, int(width * min(current, target) / target))
    return "█" * filled + "░" * (width - filled)



def format_full_exif(meta_json: str | None) -> str:
    if not meta_json:
        return "🔬 Полные EXIF-данные для этого анализа не сохранены."
    try:
        data = json.loads(meta_json)
    except (TypeError, json.JSONDecodeError):
        return "⚠️ Не удалось прочитать сохранённые EXIF-данные."
    tags = data.get("tags", {})
    if not tags:
        return "🔬 Дополнительные EXIF-теги не найдены."
    lines = ["🔬 ПОЛНЫЕ МЕТАДАННЫЕ", ""]
    for key in sorted(tags):
        value = str(tags[key]).replace("\x00", "").strip()
        if len(value) > 180:
            value = value[:177] + "..."
        lines.append(f"• {key}: {value}")
        if len("\n".join(lines)) > 3500:
            lines.append("\n…список сокращён из-за лимита Telegram.")
            break
    return "\n".join(lines)



def get_setting(key: str, default: str = "") -> str:
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()


def maintenance_enabled() -> bool:
    return get_setting("maintenance", "0") == "1"


def save_payment(user_id: int, method: str, amount: float, currency: str, attempts: int,
                 status: str, external_id: str | None = None, payload: str | None = None) -> int:
    now = datetime.now().isoformat()
    with db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO payments(user_id, method, amount, currency, attempts, status, external_id, payload, created_at, paid_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(user_id), method, float(amount), currency, int(attempts), status, external_id, payload, now,
             now if status == "paid" else None),
        )
        conn.commit()
        return cur.lastrowid


def mark_payment_paid(external_id: str) -> bool:
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM payments WHERE external_id = ?", (str(external_id),)).fetchone()
        if not row or row["status"] == "paid":
            conn.commit()
            return False
        cur = conn.execute(
            "UPDATE payments SET status='paid', paid_at=? WHERE external_id=? AND status!='paid'",
            (datetime.now().isoformat(), str(external_id)),
        )
        conn.commit()
        return cur.rowcount == 1


def get_payment_by_external(external_id: str):
    with db_connect() as conn:
        return conn.execute("SELECT * FROM payments WHERE external_id = ?", (str(external_id),)).fetchone()


def get_payment_by_id(payment_id: int):
    with db_connect() as conn:
        return conn.execute("SELECT * FROM payments WHERE id = ?", (int(payment_id),)).fetchone()


def set_payment_status(payment_id: int, status: str) -> bool:
    if status not in {"paid", "rejected", "pending"}:
        return False
    with db_connect() as conn:
        cur = conn.execute(
            "UPDATE payments SET status=?, paid_at=? WHERE id=?",
            (status, datetime.now().isoformat() if status == "paid" else None, int(payment_id)),
        )
        conn.commit()
        return cur.rowcount == 1


def get_recent_payments(limit: int = 15):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM payments ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()


def get_recent_uploads(limit: int = 10):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM uploads ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()


def extended_stats() -> dict:
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    week = (now - timedelta(days=7)).isoformat()
    with db_connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        today_users = conn.execute("SELECT COUNT(*) FROM users WHERE reg >= ?", (today,)).fetchone()[0]
        week_users = conn.execute("SELECT COUNT(*) FROM users WHERE reg >= ?", (week,)).fetchone()[0]
        uploads = conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
        today_uploads = conn.execute("SELECT COUNT(*) FROM uploads WHERE uploaded_at >= ?", (today,)).fetchone()[0]
        paid = conn.execute("SELECT COUNT(*) FROM payments WHERE status='paid'").fetchone()[0]
        stars = conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid' AND currency='XTR'").fetchone()[0]
        usdt = conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid' AND currency='USDT'").fetchone()[0]
        used = conn.execute("SELECT COALESCE(SUM(used),0) FROM users").fetchone()[0]
        referrals = conn.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
    return {"total":total,"today":today_users,"week":week_users,"uploads":uploads,"today_uploads":today_uploads,
            "paid":paid,"stars":stars,"usdt":usdt,"used":used,"referrals":referrals}


def cleanup_old_photos() -> int:
    cutoff = (datetime.now() - timedelta(days=retention_days())).isoformat()
    removed = 0
    with db_connect() as conn:
        rows = conn.execute("SELECT id, file_path FROM uploads WHERE uploaded_at < ?", (cutoff,)).fetchall()
        for row in rows:
            try:
                path = Path(row["file_path"])
                if path.exists():
                    path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("Не удалось удалить старое фото %s: %s", row["file_path"], exc)
            conn.execute("DELETE FROM uploads WHERE id = ?", (row["id"],))
        conn.commit()
    return removed


def create_backup() -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUPS_DIR / f"users_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    with db_connect() as source:
        dest = sqlite3.connect(backup)
        try:
            source.backup(dest)
        finally:
            dest.close()
    backups = sorted(BACKUPS_DIR.glob("users_*.db"), key=lambda x: x.stat().st_mtime, reverse=True)
    for old in backups[10:]:
        try:
            old.unlink()
        except OSError:
            pass
    return backup


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None) -> None:
    try:
        await context.bot.send_message(ADMIN_ID, text, reply_markup=reply_markup)
    except Exception as exc:
        logger.warning("Не удалось отправить уведомление админу: %s", exc)


_last_action: dict[int, float] = {}

def anti_spam(user_id: int) -> bool:
    if int(user_id) == ADMIN_ID:
        return True
    now = time.monotonic()
    last = _last_action.get(int(user_id), 0.0)
    if now - last < ANTI_SPAM_SECONDS:
        return False
    _last_action[int(user_id)] = now
    return True


async def maintenance_guard(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    if uid != ADMIN_ID and maintenance_enabled():
        msg = "🛠 Бот временно закрыт на технические работы.\n\nПопробуй немного позже — мы сообщим о возобновлении работы."
        if update.callback_query:
            await update.callback_query.answer("Технические работы", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(msg)
        return False
    return True


async def background_maintenance(app: Application):
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_HOURS * 3600)
        try:
            removed = await asyncio.to_thread(cleanup_old_photos)
            backup = await asyncio.to_thread(create_backup)
            if removed:
                logger.info("Автоочистка: удалено %s фото", removed)
            logger.info("Создан бэкап: %s", backup)
        except Exception as exc:
            logger.exception("Фоновое обслуживание: %s", exc)


async def post_init(app: Application):
    await asyncio.to_thread(cleanup_old_photos)
    await asyncio.to_thread(create_backup)
    app.create_task(background_maintenance(app))

# ============================================================
# ИЗВЛЕЧЕНИЕ МЕТАДАННЫХ
# ============================================================


def frac_to_float(value) -> float:
    if isinstance(value, Fraction):
        return float(value.numerator) / float(value.denominator)
    if hasattr(value, "num") and hasattr(value, "den"):
        return float(value.num) / float(value.den)
    return float(value)


def gps_to_dec(values, ref):
    try:
        if not values or len(values) < 3:
            return None
        d = frac_to_float(values[0])
        m = frac_to_float(values[1])
        s = frac_to_float(values[2])
        dec = d + m / 60 + s / 3600
        if str(ref).upper() in {"S", "W"}:
            dec = -dec
        return dec
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def extract_meta(file_path: str) -> dict:
    result = {
        "make": None,
        "model": None,
        "date": None,
        "software": None,
        "lat": None,
        "lon": None,
        "alt": None,
        "found": False,
        "error": None,
        "tags": {},
    }

    try:
        with open(file_path, "rb") as f:
            tags = exifread.process_file(f, details=False)

        if not tags:
            result["error"] = "EXIF не найдены"
            return result

        result["tags"] = {str(k): str(v) for k, v in tags.items()}

        result["make"] = str(tags["Image Make"]) if "Image Make" in tags else None
        result["model"] = str(tags["Image Model"]) if "Image Model" in tags else None
        result["date"] = str(tags["Image DateTime"]) if "Image DateTime" in tags else None
        result["software"] = str(tags["Image Software"]) if "Image Software" in tags else None

        lat_vals = lon_vals = None
        lat_ref = lon_ref = None
        alt_val = None

        for tag, val in tags.items():
            if "GPSLatitude" in tag and "Ref" not in tag:
                lat_vals = val.values if hasattr(val, "values") else val
            elif "GPSLatitudeRef" in tag:
                lat_ref = str(val).strip()
            elif "GPSLongitude" in tag and "Ref" not in tag:
                lon_vals = val.values if hasattr(val, "values") else val
            elif "GPSLongitudeRef" in tag:
                lon_ref = str(val).strip()
            elif "GPSAltitude" in tag and "Ref" not in tag:
                alt_val = val.values[0] if hasattr(val, "values") and val.values else val

        if lat_vals and lat_ref and lon_vals and lon_ref:
            lat = gps_to_dec(lat_vals, lat_ref)
            lon = gps_to_dec(lon_vals, lon_ref)
            if lat is not None and lon is not None:
                result["found"] = True
                result["lat"] = lat
                result["lon"] = lon

            if alt_val is not None:
                try:
                    result["alt"] = frac_to_float(alt_val)
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

        return result
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:
        logger.exception("Ошибка EXIF: %s", exc)
        result["error"] = "Не удалось обработать метаданные файла"
        return result



def format_exif_datetime_kyiv(value: str) -> str:
    """Формат MM.DD.YYYY HH:MM:SS. EXIF без timezone считаем локальным временем Киева."""
    if not value:
        return value
    raw = str(value).strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            # Обычный EXIF DateTime не содержит часовой пояс.
            # Поэтому считаем записанное камерой время киевским локальным временем.
            dt = dt.replace(tzinfo=ZoneInfo("Europe/Kyiv"))
            return dt.strftime("%m.%d.%Y %H:%M:%S")
        except ValueError:
            continue
    return raw

def format_meta(data: dict) -> str:
    if data["error"]:
        return f"⚠️ {data['error']}"

    lines = ["🔎 РЕЗУЛЬТАТ АНАЛИЗА", ""]
    if any([data["make"], data["model"], data["software"]]):
        lines.append("📱 УСТРОЙСТВО")
        if data["make"]:
            lines.append(f"• Производитель: {data['make']}")
        if data["model"]:
            lines.append(f"• Модель: {data['model']}")
        if data["software"]:
            lines.append(f"• ПО: {data['software']}")
        lines.append("")
    if data["date"]:
        lines.extend(["📅 ДАТА СЪЁМКИ", f"• {format_exif_datetime_kyiv(data['date'])}", ""])
    if data.get("tags"):
        lines.extend(["🧾 МЕТАДАННЫЕ", f"• Найдено EXIF-тегов: {len(data['tags'])}", "• Результат сохранён в истории ✅", ""])
    lines.append("📍 ГЕОЛОКАЦИЯ")
    if data["found"] and data["lat"] is not None and data["lon"] is not None:
        lines.append(f"• Широта: {data['lat']:.6f}")
        lines.append(f"• Долгота: {data['lon']:.6f}")
        if data["alt"] is not None:
            lines.append(f"• Высота: {data['alt']:.2f} м")

    else:
        lines.append("• GPS-координаты не найдены")
    return "\n".join(lines)


# ============================================================
# КЛАВИАТУРЫ
# ============================================================


def get_main_keyboard(user_id: int):
    buttons = [
        ["📸 Как правильно отправить?"],
        ["💰 Купить попытки"],
        ["🔗 Реферальная система"],
        ["👤 Мой профиль", "🕘 История анализов"],
        ["✉️ Написать в поддержку"],
    ]
    if int(user_id) == ADMIN_ID:
        buttons.append(["🛡️ Админ-панель"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def get_admin_keyboard():
    maint = "🟢 Выключить техработы" if maintenance_enabled() else "🛠 Включить техработы"
    return ReplyKeyboardMarkup(
        [
            ["📊 Статистика", "💰 Финансы"],
            ["💳 Платежи", "🖼 Последние загрузки"],
            ["🔎 Пользователь", "👥 Все пользователи"],
            ["➕ Добавить попытки", "📤 Рассылка"],
            ["⚙️ Настройки", maint],
            ["💾 Сделать бэкап", "🔙 Назад"],
        ],
        resize_keyboard=True,
    )


# ============================================================
# CRYPTO BOT API
# ============================================================


async def create_crypto_invoice(user_id: int, amount: float, description: str):
    if not CRYPTO_TOKEN or CRYPTO_TOKEN == "ВСТАВЬ_НОВЫЙ_CRYPTO_TOKEN":
        logger.error("CRYPTO_TOKEN не задан")
        return None

    url = f"{CRYPTO_API_URL}/createInvoice"
    headers = {
        "Crypto-Pay-API-Token": CRYPTO_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {
        "asset": "USDT",
        "amount": f"{amount:.2f}",
        "description": description,
        "payload": f"user_{int(user_id)}_{int(time.time())}",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            result = resp.json()

        if result.get("ok"):
            return result["result"]

        logger.error("Crypto Bot API error: %s", result)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.error("Crypto Bot request failed: %s", exc)
    return None


async def get_crypto_invoice(invoice_id: str):
    if not CRYPTO_TOKEN:
        return None
    headers = {"Crypto-Pay-API-Token": CRYPTO_TOKEN}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{CRYPTO_API_URL}/getInvoices", headers=headers, params={"invoice_ids": str(invoice_id)})
            resp.raise_for_status()
            result = resp.json()
        if result.get("ok"):
            items = result.get("result", {}).get("items", [])
            return items[0] if items else None
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.error("Crypto Bot status error: %s", exc)
    return None


# ============================================================
# TELEGRAM STARS
# ============================================================


async def stars_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, stars_amount: int, attempts_count: int):
    user_id = update.effective_user.id
    try:
        return await context.bot.create_invoice_link(
            title=f"⭐ {attempts_count} попыток",
            description=f"Добавление {attempts_count} попыток для бота",
            payload=f"stars_{user_id}_{attempts_count}_{int(time.time())}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"{attempts_count} попыток", amount=stars_amount)],
        )
    except Exception as exc:
        logger.exception("Stars invoice error: %s", exc)
        return None


async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    try:
        parts = query.invoice_payload.split("_")
        valid = (
            len(parts) >= 4
            and parts[0] == "stars"
            and int(parts[1]) == query.from_user.id
            and int(parts[2]) in ALLOWED_ATTEMPT_PACKS
        )
    except (ValueError, IndexError):
        valid = False

    if not valid:
        await query.answer(ok=False, error_message="Некорректный счёт. Создай его заново в боте.")
        return

    await query.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    user_id = update.effective_user.id

    try:
        parts = payment.invoice_payload.split("_")
        if len(parts) < 4 or parts[0] != "stars":
            raise ValueError("Неверный payload")

        payload_user_id = int(parts[1])
        attempts_count = int(parts[2])
        expected_stars = attempts_count * stars_price_per_attempt()

        if payload_user_id != user_id:
            raise ValueError("Платёж принадлежит другому пользователю")
        if attempts_count not in ALLOWED_ATTEMPT_PACKS:
            raise ValueError("Недопустимый пакет")
        if payment.currency != "XTR" or payment.total_amount != expected_stars:
            raise ValueError("Сумма платежа не совпадает")

        get_or_create_user(user_id)
        external_id = payment.telegram_payment_charge_id or f"stars_{payment.invoice_payload}"
        if get_payment_by_external(external_id):
            raise RuntimeError("Этот платёж уже обработан")
        if not add_attempts(user_id, attempts_count):
            raise RuntimeError("Не удалось начислить попытки")
        save_payment(user_id, "stars", payment.total_amount, "XTR", attempts_count, "paid", external_id, payment.invoice_payload)
        if notification_enabled("payment"):
            await notify_admin(context,
                f"💰 НОВАЯ ОПЛАТА\n\n👤 ID: {user_id}\n💎 Способ: Telegram Stars\n⭐ Сумма: {payment.total_amount} Stars\n🔑 Начислено: {attempts_count} попыток")

        user_data = get_user(user_id)
        await update.message.reply_text(
            f"✅ Оплата прошла успешно!\n\n"
            f"⭐ Добавлено: {attempts_count} попыток\n"
            f"🔑 Теперь у тебя: {user_data['attempts']} попыток"
        )
    except Exception as exc:
        logger.exception("Stars payment error: %s", exc)
        await update.message.reply_text(
            f"❌ Не удалось автоматически обработать платёж. Напиши @{SUPPORT_USERNAME}."
        )


# ============================================================
# ОПЛАТА
# ============================================================


async def buy_attempts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = []
    if payment_method_enabled("card"):
        rows.append([InlineKeyboardButton("💳 На карту 🇺🇦", callback_data="pay_card")])
    if payment_method_enabled("stars"):
        rows.append([InlineKeyboardButton("💎 Telegram Stars", callback_data="pay_stars")])
    if payment_method_enabled("crypto"):
        rows.append([InlineKeyboardButton("🪙 Crypto Bot", callback_data="pay_crypto")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_pay")])
    await update.message.reply_text(
        "💰 ВЫБЕРИ СПОСОБ ОПЛАТЫ:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_guard(update):
        return
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "cancel_pay":
        await query.edit_message_text("❌ Отменено.")
        return

    if query.data == "pay_card":
        if not payment_method_enabled("card"):
            await query.edit_message_text("❌ Оплата картой временно отключена.")
            return
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"💳 {n} попыток — {n * card_price_per_attempt()} грн", callback_data=f"card_{n}")]
             for n in sorted(ALLOWED_ATTEMPT_PACKS)]
            + [[InlineKeyboardButton("❌ Отмена", callback_data="cancel_pay")]]
        )
        await query.edit_message_text("💳 ОПЛАТА НА КАРТУ 🇺🇦\n\nВыберите тариф:", reply_markup=keyboard)
        return

    if query.data.startswith("card_"):
        try:
            attempts_count = int(query.data.split("_", 1)[1])
            if attempts_count not in ALLOWED_ATTEMPT_PACKS:
                raise ValueError
        except ValueError:
            await query.edit_message_text("❌ Некорректный тариф.")
            return

        amount_uah = attempts_count * card_price_per_attempt()
        await query.edit_message_text(
            f"💳 ОПЛАТА НА КАРТУ 🇺🇦\n\n"
            f"💰 Сумма: {amount_uah} грн\n"
            f"💳 Номер карты: {PAYMENT_CARD}\n"
            f"🔑 Количество попыток: {attempts_count}\n"
            f"🆔 Ваш ID аккаунта: {uid}\n\n"
            "После перевода отправьте скриншот квитанции и ваш ID аккаунта. "
            "Вы можете посмотреть его у себя в профиле в боте.\n\n"
            f"📩 Поддержка: @{SUPPORT_USERNAME}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📩 Написать в поддержку", url=f"https://t.me/{SUPPORT_USERNAME}")],
                [InlineKeyboardButton("❌ Закрыть", callback_data="cancel_pay")],
            ]),
        )
        return

    if query.data == "pay_stars":
        if not payment_method_enabled("stars"):
            await query.edit_message_text("❌ Оплата Stars временно отключена.")
            return
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"⭐ {n} попыток — {n * stars_price_per_attempt()} Stars", callback_data=f"stars_{n}")]
                for n in sorted(ALLOWED_ATTEMPT_PACKS)
            ]
            + [[InlineKeyboardButton("❌ Отмена", callback_data="cancel_pay")]]
        )
        await query.edit_message_text(
            "💎 ОПЛАТА TELEGRAM STARS\n\nВыберите тариф ниже:",
            reply_markup=keyboard,
        )
        return

    if query.data.startswith("stars_"):
        try:
            attempts_count = int(query.data.split("_", 1)[1])
            if attempts_count not in ALLOWED_ATTEMPT_PACKS:
                raise ValueError("Недопустимый пакет")
            stars_amount = attempts_count * stars_price_per_attempt()
            invoice_link = await stars_invoice(update, context, stars_amount, attempts_count)
            if not invoice_link:
                raise RuntimeError("Invoice link not created")

            await query.edit_message_text(
                f"💎 Оплата {attempts_count} попыток\n\nСумма: {stars_amount} Stars",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("💎 Оплатить Stars", url=invoice_link)],
                        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_pay")],
                    ]
                ),
            )
        except Exception as exc:
            logger.error("Stars selection error: %s", exc)
            await query.edit_message_text("❌ Ошибка создания счёта. Попробуй позже.")
        return

    if query.data == "pay_crypto":
        if not payment_method_enabled("crypto"):
            await query.edit_message_text("❌ Оплата Crypto временно отключена.")
            return
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"🪙 {n} попыток — {CRYPTO_PRICES[n]:.2f} USDT",
                        callback_data=f"crypto_{n}",
                    )
                ]
                for n in sorted(CRYPTO_PRICES)
            ]
            + [[InlineKeyboardButton("❌ Отмена", callback_data="cancel_pay")]]
        )
        await query.edit_message_text(
            "🪙 ОПЛАТА ЧЕРЕЗ CRYPTO BOT\n\nВыберите тариф ниже:",
            reply_markup=keyboard,
        )
        return

    if query.data.startswith("crypto_"):
        try:
            attempts_count = int(query.data.split("_", 1)[1])
            if attempts_count not in CRYPTO_PRICES:
                raise ValueError("Недопустимый пакет")

            amount_usdt = CRYPTO_PRICES[attempts_count]
            invoice = await create_crypto_invoice(uid, amount_usdt, f"{attempts_count} попыток для бота")
            if not invoice:
                raise RuntimeError("Invoice not created")
            invoice_id = str(invoice.get("invoice_id"))
            invoice_url = invoice.get("pay_url") or invoice.get("bot_invoice_url") or invoice.get("mini_app_invoice_url")
            if not invoice_id or not invoice_url:
                raise RuntimeError("Invoice data incomplete")
            try:
                save_payment(uid, "crypto", amount_usdt, "USDT", attempts_count, "pending", invoice_id, invoice.get("payload"))
            except sqlite3.IntegrityError:
                pass

            await query.edit_message_text(
                f"🪙 Оплата {attempts_count} попыток\n\n"
                f"Сумма: {amount_usdt:.2f} USDT\n\n"
                "После оплаты нажми «Я оплатил» — бот проверит платёж автоматически.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🪙 Оплатить USDT", url=invoice_url)],
                        [InlineKeyboardButton("✅ Я оплатил", callback_data=f"confirm_crypto_{invoice_id}")],
                        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_pay")],
                    ]
                ),
            )
        except Exception as exc:
            logger.error("Crypto selection error: %s", exc)
            await query.edit_message_text("❌ Ошибка создания счёта. Попробуй позже.")
        return

    if query.data.startswith("confirm_crypto_"):
        invoice_id = query.data.rsplit("_", 1)[-1]
        payment_row = get_payment_by_external(invoice_id)
        if not payment_row or int(payment_row["user_id"]) != uid:
            await query.edit_message_text("❌ Счёт не найден. Создай новый.")
            return
        if payment_row["status"] == "paid":
            await query.edit_message_text("✅ Этот платёж уже был зачислен.")
            return
        invoice = await get_crypto_invoice(invoice_id)
        if not invoice or invoice.get("status") != "paid":
            await query.answer("Платёж пока не найден", show_alert=True)
            return
        if not mark_payment_paid(invoice_id):
            await query.edit_message_text("✅ Этот платёж уже был обработан.")
            return
        if not add_attempts(uid, int(payment_row["attempts"])):
            await query.edit_message_text(f"❌ Ошибка начисления. Напиши @{SUPPORT_USERNAME}.")
            return
        user_data = get_user(uid)
        if notification_enabled("payment"):
            await notify_admin(context,
                f"💰 НОВАЯ ОПЛАТА\n\n👤 ID: {uid}\n🪙 Способ: Crypto Bot\n💵 Сумма: {float(payment_row['amount']):.2f} USDT\n🔑 Начислено: {payment_row['attempts']} попыток")
        await query.edit_message_text(
            f"✅ Оплата подтверждена!\n\n🔑 Добавлено: {payment_row['attempts']} попыток\n🎟 Теперь у тебя: {user_data['attempts']}"
        )


# ============================================================
# ОБРАБОТЧИКИ КОМАНД
# ============================================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_guard(update):
        return
    uid = update.effective_user.id
    referrer = None

    if context.args and context.args[0].startswith("ref_"):
        referrer = context.args[0][4:]

    existed_before = get_user(uid) is not None
    user = get_or_create_user(uid, referrer)
    days = max(0, (datetime.now() - datetime.fromisoformat(user["reg"])).days)
    attempts = "♾️" if uid == ADMIN_ID else str(user["attempts"])

    if not existed_before:
        username = f"@{update.effective_user.username}" if update.effective_user.username else "без username"
        if notification_enabled("new_user"):
            await notify_admin(context, f"🆕 НОВЫЙ ПОЛЬЗОВАТЕЛЬ\n\n👤 ID: {uid}\n🔗 {username}\n🎁 Стартовых попыток: {FREE_ATTEMPTS}")
    if not existed_before and user.get("referrer"):
        added = award_referral_milestones(int(user["referrer"]))
        await update.message.reply_text("🎉 Ты пришёл по реферальной ссылке!")
        if notification_enabled("referral"):
            try:
                ref_count, ref_bonus, next_target = referral_progress(int(user["referrer"]))
                if added:
                    bonus_line = f"\n🎁 Начислено: +{added} попыток"
                else:
                    bonus_line = ""
                await context.bot.send_message(
                    int(user["referrer"]),
                    "👤 По вашей ссылке зарегистрировался новый пользователь.\n\n"
                    f"👥 Всего приглашено: {ref_count}"
                    f"{bonus_line}\n"
                    f"🎯 До следующего уровня: {max(0, next_target - ref_count)}"
                )
            except Exception:
                pass

    await update.message.reply_text(
        "📸 Привет! Я бот для извлечения метаданных из фото.\n"
        "Отправь фото как Файл (📎 → Файл).\n\n"
        f"🔐 Загруженные файлы хранятся до {retention_days()} дней для работы сервиса и затем удаляются автоматически.\n\n"
        f"🎁 {FREE_ATTEMPTS} бесплатные попытки.\n"
        "🔗 Реферальные уровни: 1 / 5 / 10 друзей.\n"
        f"📅 Дней в боте: {days}\n"
        f"🔑 Попыток: {attempts}",
        reply_markup=get_main_keyboard(uid),
    )



async def send_admin_users_page(message, page: int = 0, edit: bool = False):
    per_page = 10
    total = get_stats()[0]
    if total <= 0:
        text = "👥 Пользователей пока нет."
        if edit:
            await message.edit_text(text)
        else:
            await message.reply_text(text)
        return

    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(int(page), pages - 1))
    users = get_all_users(limit=per_page, offset=page * per_page)

    lines = [
        "👥 ВСЕ ПОЛЬЗОВАТЕЛИ",
        f"📊 Всего: {total}  •  Страница {page + 1}/{pages}",
        "",
    ]
    buttons = []
    start = page * per_page + 1
    for i, (uid2, data) in enumerate(users, start=start):
        reg = datetime.fromisoformat(data["reg"]).strftime("%d.%m.%Y")
        status = "🚫" if data.get("banned") else "🟢"
        lines.append(
            f"{i}. {status} {uid2}\n"
            f"   📅 {reg}  •  🔑 {data['attempts']}  •  👥 {len(data['referrals'])}"
        )
        buttons.append([InlineKeyboardButton(
            f"👤 {i}. {uid2}", callback_data=f"admin_users_open_{uid2}_{page}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_users_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="admin_users_noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"admin_users_page_{page + 1}"))
    buttons.append(nav)

    text = "\n".join(lines)
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


async def send_admin_user_card(message, user_id: int):
    user = get_user(user_id)
    if not user:
        await message.reply_text("❌ Пользователь не найден.")
        return

    details = get_admin_user_details(user_id)
    telegram_name = "недоступно"
    telegram_username = "не указан"
    try:
        chat = await message.get_bot().get_chat(int(user_id))
        telegram_name = chat.full_name or "не указано"
        if chat.username:
            telegram_username = f"@{chat.username}"
    except Exception as exc:
        logger.info("Не удалось получить Telegram-профиль %s: %s", user_id, exc)

    paid_totals = ", ".join(
        f"{amount:g} {currency}" for currency, amount in details["paid_totals"]
    ) or "0"
    last_upload = "нет"
    if details["last_upload"]:
        last_upload = datetime.fromisoformat(details["last_upload"]).strftime("%d.%m.%Y %H:%M")
    referrer = user.get("referrer") or "нет"
    status = "🚫 Заблокирован" if user.get("banned") else "✅ Активен"
    kb = [
        [
            InlineKeyboardButton(
                f"📋 Активность ({get_admin_user_activity_count(user_id)})",
                callback_data=f"admin_user_activity_{user_id}_0",
            ),
            InlineKeyboardButton(
                f"🖼 Фото ({details['upload_count']})",
                callback_data=f"admin_user_photos_{user_id}_0",
            ),
        ],
        [
            InlineKeyboardButton("➕ +1", callback_data=f"admin_user_add_{user_id}_1"),
            InlineKeyboardButton("➖ -1", callback_data=f"admin_user_sub_{user_id}_1"),
        ],
        [
            InlineKeyboardButton("➕ +10", callback_data=f"admin_user_add_{user_id}_10"),
            InlineKeyboardButton("➖ -10", callback_data=f"admin_user_sub_{user_id}_10"),
        ],
        [InlineKeyboardButton("✅ Разблокировать" if user.get("banned") else "🚫 Заблокировать",
                              callback_data=f"admin_user_ban_{user_id}_{0 if user.get('banned') else 1}")],
    ]
    await message.reply_text(
        "👤 КАРТОЧКА ПОЛЬЗОВАТЕЛЯ\n\n"
        f"🆔 ID: {user_id}\n"
        f"👤 Имя: {telegram_name}\n"
        f"🔗 Username: {telegram_username}\n"
        f"📅 Регистрация: {datetime.fromisoformat(user['reg']).strftime('%d.%m.%Y %H:%M')}\n"
        f"🔑 Попыток: {user['attempts']}\n"
        f"📊 Использовано: {user['used']}\n"
        f"↩️ Пришёл от: {referrer}\n"
        f"🔗 Рефералов: {len(user['referrals'])}\n"
        f"🎁 Бонусов: {user['bonus']}\n"
        f"🖼 Загрузок: {details['upload_count']} ({details['upload_size'] / 1024 / 1024:.1f} МБ)\n"
        f"🕘 Последняя загрузка: {last_upload}\n"
        f"💳 Платежей: {details['payment_count']}\n"
        f"✅ Успешных: {details['paid_count']} на {paid_totals}\n"
        f"⏳ Ожидают: {details['pending_count']}  •  ❌ Отклонено: {details['rejected_count']}\n"
        f"Статус: {status}",
        reply_markup=InlineKeyboardMarkup(kb),
    )


def format_admin_activity_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return str(value or "неизвестно")


async def send_admin_user_activity(message, user_id: int, page: int = 0, edit: bool = False):
    user = get_user(user_id)
    if not user:
        await message.reply_text("❌ Пользователь не найден.")
        return

    per_page = 8
    total = get_admin_user_activity_count(user_id)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(int(page), pages - 1))
    events = get_admin_user_activity(user_id, per_page, page * per_page)
    lines = [
        f"📋 АКТИВНОСТЬ ПОЛЬЗОВАТЕЛЯ {user_id}",
        f"Записей: {total}  •  Страница {page + 1}/{pages}",
        "",
    ]
    buttons = []
    payment_statuses = {"paid": "✅ оплачен", "pending": "⏳ ожидает", "rejected": "❌ отклонён"}

    for event in events:
        event_time = format_admin_activity_time(event["event_time"])
        event_type = event["event_type"]
        if event_type == "registration":
            lines.append(f"🆕 {event_time}\nРегистрация в боте")
        elif event_type == "upload":
            filename = " ".join(str(event["label"] or "изображение").split())[:70]
            size = int(event["amount"] or 0) / 1024 / 1024
            lines.append(f"🖼 {event_time}\nЗагрузка #{event['event_id']}: {filename} ({size:.1f} МБ)")
            buttons.append([
                InlineKeyboardButton(
                    f"🖼 Открыть фото #{event['event_id']}",
                    callback_data=f"admin_upload_{event['event_id']}",
                )
            ])
        elif event_type == "payment":
            payment_status = payment_statuses.get(event["status"], event["status"] or "неизвестно")
            lines.append(
                f"💳 {event_time}\nПлатёж #{event['event_id']}: "
                f"{event['amount']:g} {event['currency']} • {event['label']} • "
                f"{event['attempts']} попыток • {payment_status}"
            )
        elif event_type == "referral":
            lines.append(f"🎁 {event_time}\nПриглашён пользователь {event['event_id']}")
        lines.append("")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admin_user_activity_{user_id}_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="admin_users_noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admin_user_activity_{user_id}_{page + 1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("👤 Карточка пользователя", callback_data=f"admin_users_open_{user_id}_0")])

    markup = InlineKeyboardMarkup(buttons)
    text = "\n".join(lines).rstrip()
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


async def send_admin_user_photos(message, user_id: int, page: int = 0, edit: bool = False):
    if not get_user(user_id):
        await message.reply_text("❌ Пользователь не найден.")
        return

    per_page = 6
    total = get_admin_user_details(user_id)["upload_count"]
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(int(page), pages - 1))
    uploads = get_user_uploads(user_id, per_page, page * per_page)
    lines = [
        f"🖼 ФОТОГРАФИИ ПОЛЬЗОВАТЕЛЯ {user_id}",
        f"Всего: {total}  •  Страница {page + 1}/{pages}",
        "",
    ]
    buttons = []

    if not uploads:
        lines.append("Пользователь ещё не отправлял фотографии.")
    for upload in uploads:
        filename = " ".join(str(upload["original_name"] or upload["stored_name"]).split())[:70]
        uploaded_at = format_admin_activity_time(upload["uploaded_at"])
        size = int(upload["file_size"] or 0) / 1024 / 1024
        file_available = Path(upload["file_path"]).exists()
        file_status = "✅ файл хранится" if file_available else "🗑 файл уже удалён"
        lines.append(
            f"#{upload['id']} • {uploaded_at}\n"
            f"{filename} • {size:.1f} МБ • {file_status}\n"
        )
        if file_available:
            buttons.append([
                InlineKeyboardButton(
                    f"📥 Скачать #{upload['id']}",
                    callback_data=f"admin_upload_{upload['id']}",
                )
            ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admin_user_photos_{user_id}_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="admin_users_noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admin_user_photos_{user_id}_{page + 1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("👤 Карточка пользователя", callback_data=f"admin_users_open_{user_id}_0")])

    markup = InlineKeyboardMarkup(buttons)
    text = "\n".join(lines).rstrip()
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


def settings_text() -> str:
    return (
        "⚙️ НАСТРОЙКИ БОТА\n\n"
        f"💳 Карта: {card_price_per_attempt()} грн / попытка — {'ВКЛ' if payment_method_enabled('card') else 'ВЫКЛ'}\n"
        f"⭐ Stars: {stars_price_per_attempt()} Stars / попытка — {'ВКЛ' if payment_method_enabled('stars') else 'ВЫКЛ'}\n"
        f"🪙 Crypto: {'ВКЛ' if payment_method_enabled('crypto') else 'ВЫКЛ'}\n"
        f"🗑 Хранение фото: {retention_days()} дней\n\n"
        f"🔔 Новые пользователи: {'ВКЛ' if notification_enabled('new_user') else 'ВЫКЛ'}\n"
        f"💰 Оплаты: {'ВКЛ' if notification_enabled('payment') else 'ВЫКЛ'}\n"
        f"🎁 Рефералы: {'ВКЛ' if notification_enabled('referral') else 'ВЫКЛ'}\n"
        f"🚨 Ошибки: {'ВКЛ' if notification_enabled('errors') else 'ВЫКЛ'}"
    )


def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Цена карты", callback_data="admin_set_card_price"),
         InlineKeyboardButton("⭐ Цена Stars", callback_data="admin_set_stars_price")],
        [InlineKeyboardButton("🗑 Срок хранения", callback_data="admin_set_retention")],
        [InlineKeyboardButton("💳 Карта ВКЛ/ВЫКЛ", callback_data="admin_toggle_pay_card"),
         InlineKeyboardButton("⭐ Stars ВКЛ/ВЫКЛ", callback_data="admin_toggle_pay_stars")],
        [InlineKeyboardButton("🪙 Crypto ВКЛ/ВЫКЛ", callback_data="admin_toggle_pay_crypto")],
        [InlineKeyboardButton("🔔 Новые юзеры", callback_data="admin_toggle_notify_new_user"),
         InlineKeyboardButton("💰 Оплаты", callback_data="admin_toggle_notify_payment")],
        [InlineKeyboardButton("🎁 Рефералы", callback_data="admin_toggle_notify_referral"),
         InlineKeyboardButton("🚨 Ошибки", callback_data="admin_toggle_notify_errors")],
    ])



async def handle_why_no_gps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_guard(update):
        return
    query = update.callback_query
    await query.answer()

    await query.message.reply_text(
        "📍 *Почему нет GPS?*\n\n"
        "GPS определяется только тогда, когда в фотографии сохранены данные о месте съёмки.\n\n"
        "❌ *GPS, скорее всего, не будет, если:*\n"
        "— отправлен скриншот\n"
        "— фото скачано из Instagram, Telegram или другого приложения\n"
        "— фото было обработано или пересохранено\n"
        "— при съёмке сохранение геолокации было отключено\n\n"
        "✅ *Что нужно делать:*\n"
        "Отправлять *оригинальную фотографию именно ФАЙЛОМ/ДОКУМЕНТОМ*, без сжатия и обработки.\n\n"
        "Если фотография другого человека — необходимо получить от него оригинальный файл с его согласия.\n\n"
        "⚠️ Даже в оригинальном файле GPS может отсутствовать, если камера не сохраняла геолокацию.",
        parse_mode="Markdown",
    )


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_guard(update):
        return
    uid = update.effective_user.id
    text = update.message.text or ""
    if not anti_spam(uid):
        return
    user = get_or_create_user(uid)
    reg_date = datetime.fromisoformat(user["reg"])
    days = max(0, (datetime.now() - reg_date).days)

    if text == "🛡️ Админ-панель":
        if uid != ADMIN_ID:
            await update.message.reply_text("⛔ Доступ запрещён.")
            return
        await update.message.reply_text("🛡️ Админ-панель", reply_markup=get_admin_keyboard())
        return

    if text == "🔙 Назад":
        context.user_data.pop("admin_action", None)
        await update.message.reply_text("🔙 Главное меню", reply_markup=get_main_keyboard(uid))
        return

    if uid == ADMIN_ID:
        action = context.user_data.get("admin_action")

        if action == "add_attempts":
            try:
                target, count_raw = text.split(maxsplit=1)
                count = int(count_raw)
                if count <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("❌ Формат: ID КОЛИЧЕСТВО. Количество должно быть > 0.")
                return

            if add_attempts(target, count):
                await update.message.reply_text(f"✅ Добавлено {count} попыток пользователю {target}")
            else:
                await update.message.reply_text("❌ Пользователь не найден")
            context.user_data.pop("admin_action", None)
            return

        if action == "broadcast":
            context.user_data.pop("admin_action", None)
            context.user_data["broadcast_text"] = text
            await update.message.reply_text(
                f"👀 ПРЕДПРОСМОТР РАССЫЛКИ:\n\n📢 {text}\n\nОтправить всем?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Отправить", callback_data="admin_broadcast_send"),
                    InlineKeyboardButton("❌ Отмена", callback_data="admin_broadcast_cancel"),
                ]])
            )
            return

        if action == "find_user":
            try:
                target_id = int(text.strip())
            except ValueError:
                await update.message.reply_text("❌ Введи числовой ID пользователя.")
                return
            context.user_data.pop("admin_action", None)
            await send_admin_user_card(update.message, target_id)
            return

        if action == "settings_value":
            key = context.user_data.get("settings_key")
            try:
                value = int(text.strip())
                if value <= 0:
                    raise ValueError
                if key == "photo_retention_days":
                    value = min(value, 365)
            except ValueError:
                await update.message.reply_text("❌ Введи целое число больше 0.")
                return
            set_setting(key, str(value))
            context.user_data.pop("admin_action", None)
            context.user_data.pop("settings_key", None)
            await update.message.reply_text("✅ Настройка сохранена.", reply_markup=get_admin_keyboard())
            return

        if text == "💰 Финансы":
            fs = get_finance_stats()
            def money_line(label):
                d = fs[label]
                parts = []
                for cur in ("UAH", "USDT", "XTR"):
                    if cur in d:
                        total, cnt = d[cur]
                        parts.append(f"{total:g} {cur} ({cnt})")
                return ", ".join(parts) if parts else "0"
            popular = ", ".join(f"{pack} попыток × {cnt}" for pack, cnt in fs["popular"]) or "нет данных"
            methods = ", ".join(f"{m}: {c}" for m, c in fs["methods"]) or "нет данных"
            await update.message.reply_text(
                "💰 ФИНАНСОВАЯ СТАТИСТИКА\n\n"
                f"Сегодня: {money_line('today')}\n"
                f"7 дней: {money_line('week')}\n"
                f"30 дней: {money_line('month')}\n\n"
                f"🔥 Популярные пакеты: {popular}\n"
                f"💳 По способам: {methods}"
            )
            return

        if text == "🔎 Пользователь":
            context.user_data["admin_action"] = "find_user"
            await update.message.reply_text("Введи ID пользователя:")
            return

        if text == "⚙️ Настройки":
            await update.message.reply_text(
                settings_text(),
                reply_markup=settings_keyboard()
            )
            return

        if text == "📊 Статистика":
            st = extended_stats()
            await update.message.reply_text(
                "📊 СТАТИСТИКА\n\n"
                f"👥 Пользователей: {st['total']}\n"
                f"🆕 Сегодня: {st['today']} | за 7 дней: {st['week']}\n"
                f"📸 Проверок всего: {st['used']}\n"
                f"🖼 Сохранено загрузок: {st['uploads']} | сегодня: {st['today_uploads']}\n"
                f"🔗 Рефералов: {st['referrals']}\n\n"
                f"💳 Успешных платежей: {st['paid']}\n"
                f"⭐ Stars получено: {st['stars']:.0f}\n"
                f"🪙 USDT получено: {st['usdt']:.2f}\n"
                f"🛠 Техработы: {'ВКЛ' if maintenance_enabled() else 'ВЫКЛ'}"
            )
            return

        if text in {"🛠 Включить техработы", "🟢 Выключить техработы"}:
            enabled = not maintenance_enabled()
            set_setting("maintenance", "1" if enabled else "0")
            await update.message.reply_text(
                "🛠 Технические работы включены." if enabled else "🟢 Бот снова открыт для пользователей.",
                reply_markup=get_admin_keyboard(),
            )
            return

        if text == "💳 Платежи":
            rows = get_recent_payments(15)
            if not rows:
                await update.message.reply_text("Платежей пока нет.")
                return
            lines = ["💳 ПОСЛЕДНИЕ ПЛАТЕЖИ:"]
            for r in rows:
                icon = "✅" if r["status"] == "paid" else "⏳"
                dt = datetime.fromisoformat(r["created_at"]).strftime("%d.%m %H:%M")
                lines.append(f"{icon} {dt} | {r['user_id']} | {r['method']} | {r['amount']:g} {r['currency']} | +{r['attempts']}")
            await update.message.reply_text("\n".join(lines))
            return

        if text == "🖼 Последние загрузки":
            rows = get_recent_uploads(10)
            if not rows:
                await update.message.reply_text("Загрузок пока нет.")
                return
            lines = ["🖼 ПОСЛЕДНИЕ ЗАГРУЗКИ:"]
            buttons = []
            for r in rows:
                dt = datetime.fromisoformat(r["uploaded_at"]).strftime("%d.%m %H:%M")
                lines.append(f"#{r['id']} | {r['user_id']} | {dt} | {r['original_name'] or r['stored_name']}")
                buttons.append([InlineKeyboardButton(f"Открыть #{r['id']}", callback_data=f"admin_upload_{r['id']}")])
            await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
            return

        if text == "💾 Сделать бэкап":
            backup = await asyncio.to_thread(create_backup)
            await update.message.reply_document(document=backup.open("rb"), filename=backup.name, caption="💾 Резервная копия базы")
            return

        if text == "👥 Все пользователи":
            await send_admin_users_page(update.message, 0)
            return

        if text == "➕ Добавить попытки":
            context.user_data["admin_action"] = "add_attempts"
            await update.message.reply_text("Введи: ID КОЛИЧЕСТВО")
            return

        if text == "📤 Рассылка":
            context.user_data["admin_action"] = "broadcast"
            await update.message.reply_text("Введи текст для рассылки:")
            return

    if text == "📸 Как правильно отправить?":
        await update.message.reply_text(
            "📎 Как правильно отправить фото:\n\n"
            "1️⃣ Выберите фото.\n"
            "2️⃣ Отправьте его без сжатия / как файл.\n"
            "3️⃣ Дождитесь результата.\n\n"
            "✅ Только исходный файл может сохранить EXIF/GPS.\n"
            "⚠️ При отправке как обычного фото Telegram обычно удаляет метаданные."
        )
        return

    if text == "💰 Купить попытки":
        await buy_attempts(update, context)
        return

    if text == "🔗 Реферальная система":
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=ref_{uid}"
        count, bonus, next_target = referral_progress(uid)
        target_bonus = REFERRAL_MILESTONES.get(next_target, max(REFERRAL_MILESTONES.values()))
        await update.message.reply_text(
            "🔗 РЕФЕРАЛЬНАЯ СИСТЕМА:\n\n"
            f"👥 Приглашено: {count}\n"
            f"🎁 Получено бонусов: {bonus}\n"
            f"📈 Прогресс: {progress_bar(count, next_target)} {count}/{next_target}\n\n"
            "🏆 Уровни:\n"
            "• 1 друг → суммарно 1 бонусная попытка\n"
            "• 5 друзей → суммарно 7 бонусных попыток\n"
            "• 10 друзей → суммарно 15 бонусных попыток\n\n"
            f"🎯 Следующий уровень: {next_target} друзей / {target_bonus} бонусов\n\n"
            f"📋 Твоя ссылка:\n{link}"
        )
        return

    if text == "👤 Мой профиль":
        attempts = "♾️" if uid == ADMIN_ID else str(user["attempts"])
        await update.message.reply_text(
            "👤 МОЙ ПРОФИЛЬ:\n\n"
            f"🆔 ID аккаунта: {uid}\n"
            f"📅 Регистрация: {reg_date.strftime('%d.%m.%Y %H:%M')}\n"
            f"📆 Дней в боте: {days}\n"
            f"🔑 Попыток: {attempts}\n"
            f"📊 Использовано: {user['used']}\n"
            f"🔗 Приглашено: {len(user['referrals'])}\n"
            f"🎁 Бонусов: {user['bonus']}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🕘 Последние анализы", callback_data="user_history")]
            ])
        )
        return

    if text == "🕘 История анализов":
        rows = get_user_uploads(uid, 10)
        if not rows:
            await update.message.reply_text("🕘 История анализов пока пустая.")
            return
        buttons = []
        lines = ["🕘 ПОСЛЕДНИЕ АНАЛИЗЫ", ""]
        for r in rows:
            dt = datetime.fromisoformat(r["uploaded_at"]).strftime("%m.%d.%Y %H:%M")
            lines.append(f"• #{r['id']} — {dt} — {r['original_name'] or 'изображение'}")
            buttons.append([InlineKeyboardButton(f"🔎 Анализ #{r['id']}", callback_data=f"user_analysis_{r['id']}")])
        await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
        return

    if text == "✉️ Написать в поддержку":
        await update.message.reply_text(
            "✉️ Связь с поддержкой",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📩 Написать в поддержку", url=f"https://t.me/{SUPPORT_USERNAME}")]]
            ),
        )
        return

    await update.message.reply_text("🤔 Нажми на кнопку снизу.")


# ============================================================
# ОБРАБОТКА ФАЙЛОВ
# ============================================================


async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_guard(update):
        return
    uid = update.effective_user.id
    if not anti_spam(uid):
        await update.message.reply_text("⏳ Слишком быстро. Подожди секунду и попробуй снова.")
        return
    get_or_create_user(uid)
    doc = update.message.document

    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text("❌ Файл слишком большой. Максимум — 20 МБ.")
        return

    if doc.mime_type and not doc.mime_type.startswith("image/"):
        await update.message.reply_text("❌ Отправь изображение как файл.")
        return

    if not reserve_attempt(uid):
        await update.message.reply_text("❌ Попытки закончились! Купи новые или пригласи друга.")
        return

    reserved = uid != ADMIN_ID
    finalized = False

    try:
        await update.message.chat.send_action("typing")
        tg_file = await doc.get_file()

        suffix = Path(doc.file_name or ".img").suffix[:10] or ".img"
        with tempfile.TemporaryDirectory(prefix="exifbot_") as tmpdir:
            path = Path(tmpdir) / f"upload{suffix}"
            await tg_file.download_to_drive(custom_path=str(path))

            # Повторная проверка фактического размера после загрузки.
            if path.stat().st_size > MAX_FILE_SIZE:
                raise ValueError("Файл превышает лимит 20 МБ")

            data = await asyncio.to_thread(extract_meta, str(path))
            meta_payload = json.dumps(data, ensure_ascii=False)
            saved_path = await asyncio.to_thread(
                persist_uploaded_photo, uid, path, doc, meta_payload
            )
            with db_connect() as conn:
                upload_row = conn.execute(
                    "SELECT id FROM uploads WHERE stored_name=?",
                    (saved_path.name,),
                ).fetchone()
            upload_id = int(upload_row["id"]) if upload_row else 0
            response = format_meta(data)

        finalize_attempt(uid)
        finalized = True
        current = get_user(uid)
        attempts = "♾️" if uid == ADMIN_ID else str(current["attempts"])
        buttons = []
        if data.get("found") and data.get("lat") is not None and data.get("lon") is not None:
            buttons.append([InlineKeyboardButton(
                "📍 Открыть на карте",
                url=f"https://www.google.com/maps?q={data['lat']},{data['lon']}"
            )])
        else:
            buttons.append([
                InlineKeyboardButton("❓ Почему нет GPS?", callback_data="why_no_gps")
            ])
        if upload_id:
            buttons.append([
                InlineKeyboardButton("🔬 Все метаданные", callback_data=f"user_fullmeta_{upload_id}")
            ])
        if uid != ADMIN_ID and current["attempts"] <= 1:
            buttons.append([InlineKeyboardButton("💰 Купить попытки", callback_data="pay_menu")])
        await update.message.reply_text(
            f"{response}\n\n🔑 Осталось: {attempts}",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None
        )
        if uid != ADMIN_ID and current["attempts"] == 1:
            await update.message.reply_text("⚠️ У тебя осталась последняя попытка.")

    except ValueError as exc:
        logger.warning("Файл пользователя %s отклонён: %s", uid, exc)
        await update.message.reply_text(f"❌ {exc}")
    except Exception as exc:
        logger.exception("Ошибка обработки файла пользователя %s: %s", uid, exc)
        await notify_admin(context, f"🚨 ОШИБКА ОБРАБОТКИ\n\n👤 ID: {uid}\n📄 Файл: {doc.file_name or 'без имени'}\n⚠️ {type(exc).__name__}: {str(exc)[:500]}")
        await update.message.reply_text("❌ Не удалось обработать файл. Попытка не списана.")
    finally:
        if reserved and not finalized:
            refund_attempt(uid)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_guard(update):
        return
    await update.message.reply_text(
        "⚠️ Telegram удаляет метаданные из сжатых фото!\n"
        "Отправляй изображение как Файл (📎 → Файл)."
    )



async def user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_guard(update):
        return
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "pay_menu":
        rows = []
        if payment_method_enabled("card"):
            rows.append([InlineKeyboardButton("💳 На карту 🇺🇦", callback_data="pay_card")])
        if payment_method_enabled("stars"):
            rows.append([InlineKeyboardButton("💎 Telegram Stars", callback_data="pay_stars")])
        if payment_method_enabled("crypto"):
            rows.append([InlineKeyboardButton("🪙 Crypto Bot", callback_data="pay_crypto")])
        rows.append([InlineKeyboardButton("❌ Закрыть", callback_data="cancel_pay")])
        await query.edit_message_text("💰 ВЫБЕРИ СПОСОБ ОПЛАТЫ:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if query.data == "user_history":
        rows = get_user_uploads(uid, 10)
        if not rows:
            await query.edit_message_text("🕘 История анализов пока пустая.")
            return
        buttons = []
        lines = ["🕘 ПОСЛЕДНИЕ АНАЛИЗЫ", ""]
        for r in rows:
            dt = datetime.fromisoformat(r["uploaded_at"]).strftime("%m.%d.%Y %H:%M")
            lines.append(f"• #{r['id']} — {dt} — {r['original_name'] or 'изображение'}")
            buttons.append([InlineKeyboardButton(f"🔎 Анализ #{r['id']}", callback_data=f"user_analysis_{r['id']}")])
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
        return

    if query.data.startswith("user_analysis_") or query.data.startswith("user_fullmeta_"):
        try:
            upload_id = int(query.data.rsplit("_", 1)[-1])
        except ValueError:
            return
        row = get_upload(upload_id)
        if not row or int(row["user_id"]) != uid:
            await query.answer("Анализ не найден", show_alert=True)
            return
        if query.data.startswith("user_fullmeta_"):
            await query.message.reply_text(format_full_exif(row["meta_json"]))
            return
        try:
            data = json.loads(row["meta_json"]) if row["meta_json"] else {}
        except json.JSONDecodeError:
            data = {}
        summary = format_meta(data) if data else "⚠️ Данные этого анализа недоступны."
        buttons = [[
            InlineKeyboardButton("🔬 Все метаданные", callback_data=f"user_fullmeta_{upload_id}")
        ]]
        if data.get("found") and data.get("lat") is not None and data.get("lon") is not None:
            buttons.insert(0, [InlineKeyboardButton("📍 Открыть на карте", url=f"https://www.google.com/maps?q={data['lat']},{data['lon']}")])
        else:
            buttons.insert(0, [
                InlineKeyboardButton("❓ Почему нет GPS?", callback_data="why_no_gps")
            ])
        await query.message.reply_text(summary, reply_markup=InlineKeyboardMarkup(buttons))
        return



async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "admin_users_noop":
        return

    if query.data.startswith("admin_users_page_"):
        try:
            page = int(query.data.rsplit("_", 1)[-1])
        except ValueError:
            return
        await send_admin_users_page(query.message, page, edit=True)
        return

    if query.data.startswith("admin_users_open_"):
        parts = query.data.split("_")
        try:
            target_id = int(parts[-2])
        except (ValueError, IndexError):
            return
        await send_admin_user_card(query.message, target_id)
        return

    if query.data.startswith("admin_user_activity_"):
        try:
            target_raw, page_raw = query.data.removeprefix("admin_user_activity_").rsplit("_", 1)
            target_id = int(target_raw)
            page = int(page_raw)
        except (ValueError, IndexError):
            return
        await send_admin_user_activity(query.message, target_id, page, edit=True)
        return

    if query.data.startswith("admin_user_photos_"):
        try:
            target_raw, page_raw = query.data.removeprefix("admin_user_photos_").rsplit("_", 1)
            target_id = int(target_raw)
            page = int(page_raw)
        except (ValueError, IndexError):
            return
        await send_admin_user_photos(query.message, target_id, page, edit=True)
        return

    if query.data.startswith("admin_user_add_") or query.data.startswith("admin_user_sub_"):
        parts = query.data.split("_")
        try:
            target_id = int(parts[-2])
            count = int(parts[-1])
        except (ValueError, IndexError):
            return
        ok = add_attempts(target_id, count) if "admin_user_add_" in query.data else remove_attempts(target_id, count)
        await query.answer("Готово" if ok else "Ошибка", show_alert=True)
        await send_admin_user_card(query.message, target_id)
        return

    if query.data.startswith("admin_user_ban_"):
        parts = query.data.split("_")
        try:
            target_id = int(parts[-2])
            flag = bool(int(parts[-1]))
        except (ValueError, IndexError):
            return
        if set_banned(target_id, flag):
            await query.answer("Статус изменён", show_alert=True)
            try:
                if flag:
                    await context.bot.send_message(target_id, f"⛔ Доступ к боту ограничен. Поддержка: @{SUPPORT_USERNAME}")
            except Exception:
                pass
            await send_admin_user_card(query.message, target_id)
        return

    if query.data in {"admin_set_card_price", "admin_set_stars_price", "admin_set_retention"}:
        mapping = {
            "admin_set_card_price": ("card_price_per_attempt", "Введи цену одной попытки в гривнах:"),
            "admin_set_stars_price": ("stars_price_per_attempt", "Введи цену одной попытки в Stars:"),
            "admin_set_retention": ("photo_retention_days", "Введи количество дней хранения фото (1–365):"),
        }
        key, prompt = mapping[query.data]
        context.user_data["admin_action"] = "settings_value"
        context.user_data["settings_key"] = key
        await query.message.reply_text(prompt)
        return

    if query.data.startswith("admin_toggle_pay_"):
        method = query.data.removeprefix("admin_toggle_pay_")
        set_setting(f"payment_{method}", "0" if payment_method_enabled(method) else "1")
        await query.edit_message_text(settings_text(), reply_markup=settings_keyboard())
        return

    if query.data.startswith("admin_toggle_notify_"):
        key = query.data.removeprefix("admin_toggle_notify_")
        set_setting(f"notify_{key}", "0" if notification_enabled(key) else "1")
        await query.edit_message_text(settings_text(), reply_markup=settings_keyboard())
        return

    if query.data == "admin_broadcast_cancel":
        context.user_data.pop("broadcast_text", None)
        await query.edit_message_text("❌ Рассылка отменена.")
        return

    if query.data == "admin_broadcast_send":
        text = context.user_data.pop("broadcast_text", None)
        if not text:
            await query.edit_message_text("❌ Текст рассылки не найден.")
            return
        sent = failed = 0
        for target_id in get_all_user_ids():
            try:
                await context.bot.send_message(target_id, f"📢 {text}")
                sent += 1
            except Exception as exc:
                failed += 1
                logger.warning("Рассылка не доставлена %s: %s", target_id, exc)
            await asyncio.sleep(0.04)
        await query.edit_message_text(f"✅ Рассылка завершена.\n\n📨 Доставлено: {sent}\n❌ Ошибок/блокировок: {failed}")
        return

    if query.data.startswith("admin_cardapprove_"):
        try:
            payment_id = int(query.data.rsplit("_", 1)[-1])
        except ValueError:
            return
        row = get_payment_by_id(payment_id)
        if not row or row["method"] != "card":
            await query.edit_message_text("❌ Платёж не найден.")
            return
        if row["status"] == "paid":
            await query.edit_message_text("✅ Этот платёж уже подтверждён.")
            return
        if row["status"] == "rejected":
            await query.edit_message_text("❌ Эта заявка уже отклонена.")
            return
        if not add_attempts(row["user_id"], row["attempts"]):
            await query.message.reply_text("❌ Не удалось начислить попытки пользователю.")
            return
        set_payment_status(payment_id, "paid")
        await query.edit_message_text(
            f"✅ Оплата подтверждена.\n👤 ID: {row['user_id']}\n💵 {row['amount']:g} UAH\n🔑 +{row['attempts']} попыток"
        )
        try:
            await context.bot.send_message(
                row["user_id"],
                f"✅ Оплата подтверждена!\n\n🔑 Начислено: {row['attempts']} попыток."
            )
        except Exception as exc:
            logger.warning("Не удалось уведомить пользователя об оплате: %s", exc)
        return

    if query.data.startswith("admin_cardreject_"):
        try:
            payment_id = int(query.data.rsplit("_", 1)[-1])
        except ValueError:
            return
        row = get_payment_by_id(payment_id)
        if not row or row["method"] != "card":
            await query.edit_message_text("❌ Платёж не найден.")
            return
        if row["status"] != "pending":
            await query.edit_message_text("Заявка уже обработана.")
            return
        set_payment_status(payment_id, "rejected")
        await query.edit_message_text(f"❌ Заявка #{payment_id} отклонена.")
        try:
            await context.bot.send_message(row["user_id"], f"❌ Заявка об оплате не подтверждена. Если это ошибка, напиши @{SUPPORT_USERNAME}.")
        except Exception:
            pass
        return

    if query.data.startswith("admin_upload_"):
        try:
            upload_id = int(query.data.rsplit("_", 1)[-1])
        except ValueError:
            return
        with db_connect() as conn:
            row = conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        if not row:
            await query.message.reply_text("❌ Запись не найдена.")
            return
        path = Path(row["file_path"])
        if not path.exists():
            await query.message.reply_text("❌ Файл уже удалён по сроку хранения.")
            return
        with path.open("rb") as f:
            await query.message.reply_document(
                document=f,
                filename=row["original_name"] or row["stored_name"],
                caption=f"🖼 Загрузка #{row['id']}\n👤 User ID: {row['user_id']}\n📅 {row['uploaded_at']}",
            )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Необработанная ошибка Telegram handler", exc_info=context.error)
    try:
        if notification_enabled("errors"):
            await notify_admin(context, f"🚨 НЕОБРАБОТАННАЯ ОШИБКА\n\n{type(context.error).__name__}: {str(context.error)[:700]}")
    except Exception:
        pass



async def admin_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Использование: /user ID")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return
    await send_admin_user_card(update.message, target_id)


# ============================================================
# ЗАПУСК
# ============================================================


def prepare_bot_storage() -> None:
    """Initialize persistent bot data before polling or webhook startup."""
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_НОВЫЙ_BOT_TOKEN":
        raise RuntimeError(
            "Впиши BOT_TOKEN в блок НАСТРОЙКИ в начале файла."
        )

    init_db()
    migrate_legacy_users()
    defaults = {
        "maintenance": "0",
        "card_price_per_attempt": str(DEFAULT_CARD_PRICE_PER_ATTEMPT),
        "stars_price_per_attempt": str(DEFAULT_STARS_PRICE_PER_ATTEMPT),
        "photo_retention_days": str(PHOTO_RETENTION_DAYS),
        "payment_card": "1",
        "payment_stars": "1",
        "payment_crypto": "1",
        "notify_new_user": "1",
        "notify_payment": "1",
        "notify_referral": "1",
        "notify_errors": "1",
    }
    for key, value in defaults.items():
        if get_setting(key, "") == "":
            set_setting(key, value)

    logger.info("Бот запускается. Админ: %s", ADMIN_ID)
    if not CRYPTO_TOKEN or CRYPTO_TOKEN == "ВСТАВЬ_НОВЫЙ_CRYPTO_TOKEN":
        logger.warning("CRYPTO_TOKEN не задан — Crypto Bot платежи работать не будут")


def build_application() -> Application:
    """Build the Telegram application with all existing handlers."""
    prepare_bot_storage()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("user", admin_user_command))
    app.add_handler(CallbackQueryHandler(handle_why_no_gps, pattern=r"^why_no_gps$"))
    app.add_handler(CallbackQueryHandler(user_callback, pattern=r"^(?:user_|pay_menu$)"))
    app.add_handler(
        CallbackQueryHandler(
            pay_callback,
            pattern=r"^(?:pay_|cancel_pay$|confirm_crypto_|card_|stars_|crypto_)",
        )
    )
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin_"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_doc))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_error_handler(error_handler)
    return app


def run_bot():
    app = build_application()

    # run_polling сам управляет жизненным циклом; внешний while True здесь не нужен.
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    run_bot()
