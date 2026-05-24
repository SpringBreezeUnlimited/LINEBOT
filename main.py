import csv
import io
import math
import os
import re
import secrets
import time
import uuid
from datetime import timedelta, datetime, timezone
from threading import Lock
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import psycopg2 # type: ignore
from flask import Flask, request, abort, render_template, redirect, url_for, session, jsonify, Response, g, has_request_context, stream_with_context # type: ignore
from linebot.v3 import WebhookHandler # type: ignore
from linebot.v3.exceptions import InvalidSignatureError # type: ignore
from linebot.v3.messaging import ApiClient, Configuration, MessagingApi, PushMessageRequest, ReplyMessageRequest, TextMessage # type: ignore
from linebot.v3.webhooks import MessageEvent, TextMessageContent # type: ignore
from werkzeug.middleware.proxy_fix import ProxyFix # type: ignore
from werkzeug.security import check_password_hash, generate_password_hash # type: ignore

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def normalize_db_url(raw_url: str) -> str:
    url = raw_url.replace("postgres://", "postgresql://", 1)
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError("DATABASE_URL is invalid")
    # 本番ではTLS必須。ローカル開発時はlocalhostのみ緩和する。
    local_hosts = {"localhost", "127.0.0.1"}
    if parsed.hostname not in local_hosts:
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("sslmode", "require")
        url = urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment)
        )
    return url

# --- セキュリティ設定 ---
SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is required")
app.secret_key = SECRET_KEY

ADMIN_PASSWORD_HASH = (os.getenv('ADMIN_PASSWORD_HASH') or "").strip()
if not ADMIN_PASSWORD_HASH:
    raise RuntimeError("ADMIN_PASSWORD_HASH is required")
if os.getenv("ADMIN_PASSWORD"):
    app.logger.warning("ADMIN_PASSWORD is deprecated and ignored. Use ADMIN_PASSWORD_HASH only.")
AUDIT_ADMIN_PASSWORD_HASH = (os.getenv("AUDIT_ADMIN_PASSWORD_HASH") or "").strip()

CHANNEL_ACCESS_TOKEN = (os.getenv('CHANNEL_ACCESS_TOKEN') or "").strip()
CHANNEL_SECRET = (os.getenv('CHANNEL_SECRET') or "").strip()
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("CHANNEL_ACCESS_TOKEN and CHANNEL_SECRET are required")

raw_db_url = (os.getenv('DATABASE_URL') or "").strip()
if not raw_db_url:
    raise RuntimeError("DATABASE_URL is required")
DATABASE_URL = normalize_db_url(raw_db_url)
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))

OWNER_LINE_ID = os.getenv('OWNER_LINE_ID', '').strip()

APP_VERSION = "1.0.102"
APP_RELEASED_AT = "2026-05-09 00:00 JST"

FORCE_HTTPS = parse_bool_env("FORCE_HTTPS", True)
ALLOWED_HOSTS = {
    host.strip().lower() for host in os.getenv("ALLOWED_HOSTS", "").split(",") if host.strip()
}

# 本番環境での安全性チェック
IS_PRODUCTION = bool(os.getenv("RENDER"))
if IS_PRODUCTION and not ALLOWED_HOSTS:
    raise RuntimeError("ALLOWED_HOSTS is required in production environment. Set it to your Render app domain(s)")

SESSION_IDLE_TIMEOUT_SECONDS = int(os.getenv("SESSION_IDLE_TIMEOUT_SECONDS", "1800"))
MAX_TYPE_NAME_LENGTH = int(os.getenv("MAX_TYPE_NAME_LENGTH", "40"))
MAX_USER_MESSAGE_CHARS = int(os.getenv("MAX_USER_MESSAGE_CHARS", "100"))
TYPE_NAME_PATTERN = re.compile(
    rf"^[A-Za-z0-9ぁ-んァ-ヶー一-龠々・ 　_-]{{1,{MAX_TYPE_NAME_LENGTH}}}$"
)
LOGIN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}$")

WEBHOOK_RATE_LIMIT_COUNT = int(os.getenv("WEBHOOK_RATE_LIMIT_COUNT", "120"))
WEBHOOK_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("WEBHOOK_RATE_LIMIT_WINDOW_SECONDS", "60"))
CALL_TIMEOUT_MINUTES = int(os.getenv("CALL_TIMEOUT_MINUTES", "15"))
ADMIN_REFRESH_INTERVAL_MS = parse_int_env("ADMIN_REFRESH_INTERVAL_MS", 15000, 1000, 300000)
BATCH_CALL_RUNNER_TOKEN = (os.getenv("BATCH_CALL_RUNNER_TOKEN") or "").strip()
LINE_PUSH_MAX_RETRIES = parse_int_env("LINE_PUSH_MAX_RETRIES", 3, 1, 10)
LINE_PUSH_RETRY_BASE_SECONDS = parse_int_env("LINE_PUSH_RETRY_BASE_SECONDS", 1, 1, 30)
LINE_PUSH_RETRY_MAX_SECONDS = parse_int_env("LINE_PUSH_RETRY_MAX_SECONDS", 8, 1, 300)

MESSAGING_CONFIGURATION = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
JST = ZoneInfo("Asia/Tokyo")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=parse_bool_env("SESSION_COOKIE_SECURE", True),
    SESSION_COOKIE_NAME="__Host-session" if parse_bool_env("SESSION_COOKIE_SECURE", True) else "session",
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_IDLE_TIMEOUT_SECONDS),
)
app.jinja_env.autoescape = True


@app.context_processor
def inject_template_globals():
    return {
        "app_version": APP_VERSION,
        "app_released_at": APP_RELEASED_AT,
        "format_duration": format_duration_from_seconds,
    }

handler = WebhookHandler(CHANNEL_SECRET)

STATUS_WAITING = "waiting"
STATUS_CALLED = "called"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

AUTO_CALL_SETTING_KEYS = (
    "last_auto_call_run_at",
    "last_auto_call_sent_count",
    "last_auto_call_failed_count",
    "last_auto_call_selected_count",
    "previous_auto_call_run_at",
    "previous_auto_call_sent_count",
    "previous_auto_call_failed_count",
    "previous_auto_call_selected_count",
)
WAIT_TIME_SETTING_KEYS = (
    "last_wait_time_run_at",
    "last_wait_time_estimated_seconds",
    "last_wait_time_waiting_count",
    "last_wait_time_avg_service_seconds",
)
ROLE_ADMIN = "admin"
ROLE_AUDIT_ADMIN = "audit_admin"
SCHEMA_LOCK = Lock()
SCHEMA_READY = False
RUNTIME_SETTING_KEYS = ("accepting_new", "auto_call_count") + AUTO_CALL_SETTING_KEYS + WAIT_TIME_SETTING_KEYS

class ManagedConnection:
    def __init__(self, connection, close_on_exit: bool):
        self._connection = connection
        self._close_on_exit = close_on_exit

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __enter__(self):
        self._connection.__enter__()
        return self._connection

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._connection.__exit__(exc_type, exc, tb)
        finally:
            if self._close_on_exit and not self._connection.closed:
                self._connection.close()


def create_connection():
    return psycopg2.connect(DATABASE_URL, connect_timeout=DB_CONNECT_TIMEOUT)


def get_connection():
    if has_request_context():
        connection = getattr(g, "_db_connection", None)
        if connection is None or connection.closed:
            connection = create_connection()
            g._db_connection = connection
        return ManagedConnection(connection, close_on_exit=False)
    return ManagedConnection(create_connection(), close_on_exit=True)


def extract_http_status(error: Exception):
    for attr in ("status", "status_code"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value
    return None


def is_retryable_push_error(error: Exception) -> bool:
    status = extract_http_status(error)
    if status is None:
        # タイムアウトやネットワーク断のようにHTTPステータスが取れない失敗は再試行対象
        return True
    if status >= 500 or status == 429:
        return True
    return False


def push_message_with_retry_key(messaging_api: MessagingApi, request_payload: PushMessageRequest, retry_key: str):
    try:
        return messaging_api.push_message(request_payload, x_line_retry_key=retry_key)
    except TypeError as error:
        message = str(error)
        if "x_line_retry_key" not in message:
            raise
        app.logger.warning("line-bot-sdk does not support x_line_retry_key argument; fallback without retry key")
        return messaging_api.push_message(request_payload)


def send_push_message(user_id: str, text: str, retry_key: str | None = None):
    stable_retry_key = retry_key or str(uuid.uuid4())
    payload = PushMessageRequest(
        to=user_id,
        messages=[TextMessage(text=text)],
    )
    for attempt in range(1, LINE_PUSH_MAX_RETRIES + 1):
        try:
            with ApiClient(MESSAGING_CONFIGURATION) as api_client:
                messaging_api = MessagingApi(api_client)
                push_message_with_retry_key(messaging_api, payload, stable_retry_key)
            return
        except Exception as error:
            status = extract_http_status(error)
            if status == 409:
                # 同じリトライキーで受理済み。重複送信は行われていないので成功扱いにする。
                app.logger.info("Push already accepted (409) retry_key=%s user_id=%s", stable_retry_key, user_id)
                return
            if attempt >= LINE_PUSH_MAX_RETRIES or not is_retryable_push_error(error):
                raise
            delay_seconds = min(LINE_PUSH_RETRY_MAX_SECONDS, LINE_PUSH_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
            app.logger.warning(
                "Push failed (attempt %s/%s, status=%s). Retry after %ss retry_key=%s",
                attempt,
                LINE_PUSH_MAX_RETRIES,
                status,
                delay_seconds,
                stable_retry_key,
            )
            time.sleep(delay_seconds)


def send_reply_message(reply_token: str, text: str):
    with ApiClient(MESSAGING_CONFIGURATION) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def format_dt(value):
    if not value:
        return ""
    # UTCまたはnaive datetimeの場合、JSTに変換
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(JST)
    return value.strftime("%m-%d %H:%M")


def format_duration_from_seconds(total_seconds):
    if total_seconds is None:
        return ""
    seconds = max(0, int(total_seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}時間{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def parse_hhmm_to_minute_of_day(value: str):
    text = (value or "").strip()
    if not text:
        return None
    if not re.fullmatch(r"\d{2}:\d{2}", text):
        return None
    hour = int(text[:2])
    minute = int(text[3:5])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour * 60 + minute


def format_minute_of_day(minute_of_day):
    if minute_of_day is None:
        return ""
    minute = int(minute_of_day) % (24 * 60)
    hour, minute = divmod(minute, 60)
    return f"{hour:02d}:{minute:02d}"


def is_minute_in_window(minute_of_day: int, start_minute: int, end_minute: int) -> bool:
    if start_minute == end_minute:
        return True
    if start_minute < end_minute:
        return start_minute <= minute_of_day < end_minute
    return minute_of_day >= start_minute or minute_of_day < end_minute


def get_admin_reservation_window(admin_account_id: int, cur=None):
    if cur is not None:
        cur.execute(
            "SELECT reservation_start_minute, reservation_end_minute FROM admin_accounts WHERE id = %s",
            (admin_account_id,),
        )
        row = cur.fetchone()
        if not row:
            return None, None
        return row[0], row[1]
    with get_connection() as conn:
        with conn.cursor() as local_cur:
            local_cur.execute(
                "SELECT reservation_start_minute, reservation_end_minute FROM admin_accounts WHERE id = %s",
                (admin_account_id,),
            )
            row = local_cur.fetchone()
            if not row:
                return None, None
            return row[0], row[1]


def is_within_admin_reservation_window(admin_account_id: int, now=None) -> bool:
    start_minute, end_minute = get_admin_reservation_window(admin_account_id)
    if start_minute is None or end_minute is None:
        return True
    current_dt = datetime.now(JST) if now is None else now.astimezone(JST)
    current_minute = current_dt.hour * 60 + current_dt.minute
    return is_minute_in_window(current_minute, int(start_minute), int(end_minute))


def calculate_wait_time_minutes(people_ahead: int) -> int:
    ahead = max(0, int(people_ahead))
    return max(0, math.ceil(ahead * 0.5 + 2))


def count_waiting_people_ahead_by_owner(cur, reservation_id: int, owner_admin_id: int) -> int:
    cur.execute(
        """
            SELECT COUNT(*)
            FROM reservations r
            JOIN reservation_types t ON r.type_id = t.id
            WHERE r.status = %s
              AND r.id < %s
              AND t.owner_admin_id = %s
        """,
        (STATUS_WAITING, reservation_id, owner_admin_id),
    )
    return int(cur.fetchone()[0] or 0)


def should_run_call_batch(now=None) -> bool:
    current = now or time.localtime()
    return current.tm_min % 5 == 0


def build_call_message(reservation_id: int, called_at=None) -> str:
    called_dt = datetime.now(JST) if called_at is None else called_at.astimezone(JST)
    timeout_at = called_dt + timedelta(minutes=CALL_TIMEOUT_MINUTES)
    timeout_label = timeout_at.strftime("%H:%M")
    return (
        f"【順番が来ました】番号 {reservation_id} 番の方、会場へお越しください！"
        f"\n{CALL_TIMEOUT_MINUTES}分以内（{timeout_label}まで）にお越しください。"
        "\n時間を過ぎると自動でキャンセルされます。"
    )


def expire_called_reservations() -> int:
    if CALL_TIMEOUT_MINUTES <= 0:
        return 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                        UPDATE reservations
                        SET status = %s
                        WHERE status = %s
                          AND called_at IS NOT NULL
                          AND called_at <= (CURRENT_TIMESTAMP - (%s * INTERVAL '1 minute'))
                        RETURNING id, user_id
                    """,
                    (STATUS_CANCELLED, STATUS_CALLED, CALL_TIMEOUT_MINUTES),
                )
                timed_out_rows = cur.fetchall()
                conn.commit()
        for reservation_id, user_id in timed_out_rows:
            try:
                send_push_message(
                    user_id,
                    (
                        f"【自動キャンセル】番号 {reservation_id} は呼出から{CALL_TIMEOUT_MINUTES}分経過したため"
                        "タイムアウトでキャンセルされました。"
                    ),
                )
            except Exception:
                app.logger.exception("Failed to send timeout message for reservation %s", reservation_id)
        return len(timed_out_rows)
    except Exception:
        app.logger.exception("Failed to expire called reservations")
        return 0


def process_queued_calls(now=None):
    # 日本時間で現在時刻を取得
    current_dt = datetime.now(JST) if now is None else now
    current = current_dt.timetuple()
    minute_label = current_dt.strftime("%m-%d %H:%M")
    timed_out_count = expire_called_reservations()
    latest_wait_time = refresh_wait_time_estimate(current_dt)
    if not should_run_call_batch(current):
        return {
            "processed": False,
            "reason": "not_due",
            "minute": minute_label,
            "timed_out_count": timed_out_count,
            "sent_count": 0,
            "failed_count": 0,
            "wait_time": latest_wait_time,
        }

    ensure_database_schema()
    runtime_settings = get_runtime_settings()
    auto_call_count = runtime_settings["auto_call_count"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            auto_rows = []
            if auto_call_count > 0:
                # 先に該当行を確保して状態を更新しておくことで、並行実行や手動呼出しとの競合で重複通知が送られるのを防ぐ
                cur.execute(
                    """
                        UPDATE reservations
                        SET status = %s, called_at = CURRENT_TIMESTAMP
                        WHERE id IN (
                            SELECT id FROM reservations
                            WHERE status = %s
                            ORDER BY id ASC
                            FOR UPDATE SKIP LOCKED
                            LIMIT %s
                        )
                        RETURNING id, user_id
                    """,
                    (STATUS_CALLED, STATUS_WAITING, auto_call_count),
                )
                auto_rows = cur.fetchall()
                conn.commit()

    sent_ids = []
    failed_ids = []
    for res_id, user_id in auto_rows:
        try:
            send_push_message(user_id, build_call_message(res_id))
            sent_ids.append(res_id)
        except Exception:
            failed_ids.append(res_id)
            app.logger.exception("Failed to send LINE push message for reservation %s", res_id)

    if failed_ids:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                        UPDATE reservations
                        SET status = %s, called_at = NULL
                        WHERE id = ANY(%s) AND status = %s
                    """,
                    (STATUS_WAITING, failed_ids, STATUS_CALLED),
                )
                conn.commit()

    # 送信成功分は選択時点で状態を既に更新しているため、ここで再度更新する必要はない

    previous_summary = runtime_settings["latest_auto_call"]
    settings_to_save = {
        "last_auto_call_run_at": minute_label,
        "last_auto_call_sent_count": str(len(sent_ids)),
        "last_auto_call_failed_count": str(len(failed_ids)),
        "last_auto_call_selected_count": str(len(auto_rows)),
    }
    if previous_summary["run_at"]:
        settings_to_save.update({
            "previous_auto_call_run_at": previous_summary["run_at"],
            "previous_auto_call_sent_count": str(previous_summary["sent_count"]),
            "previous_auto_call_failed_count": str(previous_summary["failed_count"]),
            "previous_auto_call_selected_count": str(previous_summary["selected_count"]),
        })
    set_settings(settings_to_save)
    latest_wait_time = refresh_wait_time_estimate(current_dt)

    return {
        "processed": True,
        "reason": "ok",
        "minute": minute_label,
        "timed_out_count": timed_out_count,
        "auto_call_count": auto_call_count,
        "auto_selected_count": len(auto_rows),
        "sent_count": len(sent_ids),
        "failed_count": len(failed_ids),
        "failed_ids": failed_ids,
        "wait_time": latest_wait_time,
    }


def refresh_wait_time_estimate(now=None, owner_admin_id=None):
    # 目安待ち時間は「前に並んでいる人数 × 0.5 + 2分」で算出し、整数分で保存する。
    current_dt = datetime.now(JST) if now is None else now
    minute_label = current_dt.strftime("%m-%d %H:%M")
    default_result = {
        "run_at": minute_label,
        "waiting_count": 0,
        "avg_service_seconds": 0,
        "estimated_seconds": 0,
        "message": "現在の目安待ち時間: 2分",
    }
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if owner_admin_id is None:
                    cur.execute("SELECT COUNT(*) FROM reservations WHERE status = %s", (STATUS_WAITING,))
                    waiting_count = int(cur.fetchone()[0] or 0)
                else:
                    cur.execute(
                        """
                            SELECT COUNT(*)
                            FROM reservations r
                            JOIN reservation_types t ON r.type_id = t.id
                            WHERE r.status = %s AND t.owner_admin_id = %s
                        """,
                        (STATUS_WAITING, owner_admin_id),
                    )
                    waiting_count = int(cur.fetchone()[0] or 0)
        estimated_minutes = calculate_wait_time_minutes(waiting_count)
        estimated_seconds = estimated_minutes * 60
        if owner_admin_id is None:
            set_settings(
                {
                    "last_wait_time_run_at": minute_label,
                    "last_wait_time_estimated_seconds": str(estimated_seconds),
                    "last_wait_time_waiting_count": str(waiting_count),
                    "last_wait_time_avg_service_seconds": "0",
                }
            )
        return {
            "run_at": minute_label,
            "waiting_count": waiting_count,
            "avg_service_seconds": 0,
            "estimated_seconds": estimated_seconds,
            "message": f"現在の目安待ち時間: {estimated_minutes}分",
        }
    except Exception:
        app.logger.exception("Failed to refresh wait time estimate")
        return default_result


def get_latest_wait_time_summary(values=None):
    values = get_settings(WAIT_TIME_SETTING_KEYS) if values is None else values
    run_at = (values.get("last_wait_time_run_at") or "").strip()
    if not run_at:
        return {
            "run_at": "",
            "estimated_seconds": 0,
            "waiting_count": 0,
            "avg_service_seconds": 0,
            "message": "現在の目安待ち時間: 算出中",
        }
    estimated_seconds_raw = (values.get("last_wait_time_estimated_seconds") or "0").strip()
    waiting_count_raw = (values.get("last_wait_time_waiting_count") or "0").strip()
    avg_service_seconds_raw = (values.get("last_wait_time_avg_service_seconds") or "0").strip()
    estimated_seconds = int(estimated_seconds_raw) if estimated_seconds_raw.isdigit() else 0
    waiting_count = int(waiting_count_raw) if waiting_count_raw.isdigit() else 0
    avg_service_seconds = int(avg_service_seconds_raw) if avg_service_seconds_raw.isdigit() else 0
    estimated_minutes = max(0, math.ceil(estimated_seconds / 60))
    return {
        "run_at": run_at,
        "estimated_seconds": estimated_seconds,
        "waiting_count": waiting_count,
        "avg_service_seconds": avg_service_seconds,
        "message": f"現在の目安待ち時間: {estimated_minutes}分",
    }


def ensure_reservations_table():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reservations (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'waiting',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS user_id TEXT
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS message TEXT
            """)
            cur.execute("""
                ALTER TABLE reservations
                ALTER COLUMN message SET DEFAULT ''
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS status TEXT
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS type_id INTEGER
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS called_at TIMESTAMP
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP
            """)
            cur.execute("""
                ALTER TABLE reservations
                ALTER COLUMN status SET DEFAULT 'waiting'
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_reservations_status_id
                ON reservations (status, id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_reservations_user_id_id
                ON reservations (user_id, id DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_reservations_status_type_id_id
                ON reservations (status, type_id, id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_reservations_status_created_at_id
                ON reservations (status, created_at DESC, id DESC)
            """)
            cur.execute(
                """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_reservations_user_active
                    ON reservations (user_id)
                    WHERE status IN ('waiting', 'called')
                """
            )
            conn.commit()


def ensure_database_schema():
    global SCHEMA_READY
    if SCHEMA_READY:
        return
    with SCHEMA_LOCK:
        if SCHEMA_READY:
            return
        ensure_reservations_table()
        ensure_admin_accounts_table()
        ensure_types_table()
        ensure_settings_table()
        ensure_admin_login_logs_table()
        ensure_rate_limit_tables()
        migrate_legacy_queued_calls()
        SCHEMA_READY = True


def migrate_legacy_queued_calls():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reservations SET status = %s WHERE status = %s",
                (STATUS_WAITING, "queued_call"),
            )
            conn.commit()


def ensure_admin_login_logs_table():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_login_logs (
                    id SERIAL PRIMARY KEY,
                    login_result TEXT NOT NULL DEFAULT 'success',
                    admin_role TEXT NOT NULL,
                    ip_address TEXT,
                    user_agent TEXT,
                    logged_in_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                ALTER TABLE admin_login_logs
                ADD COLUMN IF NOT EXISTS login_result TEXT NOT NULL DEFAULT 'success'
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_admin_login_logs_logged_in_at
                ON admin_login_logs (logged_in_at DESC)
            """)
            conn.commit()


def ensure_admin_accounts_table():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                    CREATE TABLE IF NOT EXISTS admin_accounts (
                        id SERIAL PRIMARY KEY,
                        login_id TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        role TEXT NOT NULL,
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """
            )
            cur.execute(
                """
                    CREATE INDEX IF NOT EXISTS idx_admin_accounts_role_active
                    ON admin_accounts (role, active)
                """
            )
            cur.execute(
                """
                    ALTER TABLE admin_accounts
                    ADD COLUMN IF NOT EXISTS reservation_start_minute INTEGER
                """
            )
            cur.execute(
                """
                    ALTER TABLE admin_accounts
                    ADD COLUMN IF NOT EXISTS reservation_end_minute INTEGER
                """
            )
            cur.execute(
                """
                    INSERT INTO admin_accounts (login_id, password_hash, role)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (login_id) DO NOTHING
                """,
                ("admin", ADMIN_PASSWORD_HASH, ROLE_ADMIN),
            )
            if AUDIT_ADMIN_PASSWORD_HASH:
                cur.execute(
                    """
                        INSERT INTO admin_accounts (login_id, password_hash, role)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (login_id) DO NOTHING
                    """,
                    ("audit", AUDIT_ADMIN_PASSWORD_HASH, ROLE_AUDIT_ADMIN),
                )
            conn.commit()


def ensure_rate_limit_tables():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS login_attempt_records (
                    id SERIAL PRIMARY KEY,
                    ip_address TEXT NOT NULL,
                    attempted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_login_attempt_records_ip_attempted_at
                ON login_attempt_records (ip_address, attempted_at DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS webhook_request_records (
                    id SERIAL PRIMARY KEY,
                    ip_address TEXT NOT NULL,
                    requested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_webhook_request_records_ip_requested_at
                ON webhook_request_records (ip_address, requested_at DESC)
            """)
            conn.commit()


def record_admin_login(role: str, ip_address: str, user_agent: str, login_result: str = "success"):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO admin_login_logs (login_result, admin_role, ip_address, user_agent)
                    VALUES (%s, %s, %s, %s)
                """,
                (login_result, role, ip_address, (user_agent or "")[:300]),
            )
            conn.commit()

def verify_admin_password(candidate: str) -> bool:
    if not candidate:
        return False
    return check_password_hash(ADMIN_PASSWORD_HASH, candidate)


def verify_audit_admin_password(candidate: str) -> bool:
    if not candidate or not AUDIT_ADMIN_PASSWORD_HASH:
        return False
    return check_password_hash(AUDIT_ADMIN_PASSWORD_HASH, candidate)


def authenticate_admin_account(login_id: str, candidate: str):
    normalized_login_id = (login_id or "").strip().lower()
    if not normalized_login_id or not candidate:
        return None
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                        SELECT id, login_id, password_hash, role
                        FROM admin_accounts
                        WHERE login_id = %s AND active = TRUE
                        LIMIT 1
                    """,
                    (normalized_login_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                if not check_password_hash(row[2], candidate):
                    return None
                return {
                    "id": row[0],
                    "login_id": row[1],
                    "role": row[3],
                }
    except Exception:
        app.logger.exception("Failed to authenticate admin account")
        return None


def has_audit_admin_account() -> bool:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM admin_accounts WHERE role = %s AND active = TRUE LIMIT 1",
                    (ROLE_AUDIT_ADMIN,),
                )
                return bool(cur.fetchone())
    except Exception:
        return bool(AUDIT_ADMIN_PASSWORD_HASH)


def is_local_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1"}


def enforce_host_allowlist():
    if not ALLOWED_HOSTS:
        return
    host = (request.host.split(":", 1)[0] if request.host else "").lower()
    if host not in ALLOWED_HOSTS:
        abort(400)


def enforce_https():
    if not FORCE_HTTPS:
        return
    host = (request.host.split(":", 1)[0] if request.host else "").lower()
    if is_local_host(host):
        return
    if request.is_secure:
        return
    forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if forwarded_proto == "https":
        return
    secure_url = request.url.replace("http://", "https://", 1)
    return redirect(secure_url, code=301)


def start_admin_session(role: str, admin_account_id: int, admin_login_id: str):
    now = time.time()
    session.clear()
    session["logged_in"] = True
    session["admin_role"] = role
    session["admin_account_id"] = admin_account_id
    session["admin_login_id"] = admin_login_id
    session["issued_at"] = now
    session["last_activity"] = now
    session["_csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True


def is_authenticated_as(role: str, update_activity: bool = True) -> bool:
    if not session.get("logged_in"):
        return False
    if session.get("admin_role") != role:
        return False
    admin_account_id = session.get("admin_account_id")
    if not isinstance(admin_account_id, int) or admin_account_id <= 0:
        session.clear()
        return False
    last_activity = session.get("last_activity")
    if not isinstance(last_activity, (int, float)):
        session.clear()
        return False
    now = time.time()
    if now - last_activity > SESSION_IDLE_TIMEOUT_SECONDS:
        session.clear()
        return False
    if update_activity:
        session["last_activity"] = now
        session.modified = True
    return True


def is_admin_authenticated(update_activity: bool = True) -> bool:
    return is_authenticated_as(ROLE_ADMIN, update_activity)


def is_audit_admin_authenticated(update_activity: bool = True) -> bool:
    return is_authenticated_as(ROLE_AUDIT_ADMIN, update_activity)


def get_current_admin_account_id():
    admin_account_id = session.get("admin_account_id")
    if isinstance(admin_account_id, int) and admin_account_id > 0:
        return admin_account_id
    return None


def normalize_type_name(value: str) -> str:
    return " ".join((value or "").split())


def validate_type_name(value: str) -> bool:
    if not value or len(value) > MAX_TYPE_NAME_LENGTH:
        return False
    return bool(TYPE_NAME_PATTERN.fullmatch(value))

def get_csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token

def validate_csrf():
    token = session.get("_csrf_token")
    request_token = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
    if not token or not request_token or not secrets.compare_digest(token, request_token):
        abort(403)


def validate_batch_runner_token() -> bool:
    if not BATCH_CALL_RUNNER_TOKEN:
        return False
    auth_header = (request.headers.get("Authorization") or "").strip()
    if auth_header.startswith("Bearer "):
        candidate = auth_header[7:].strip()
        return secrets.compare_digest(candidate, BATCH_CALL_RUNNER_TOKEN)
    header_token = (request.headers.get("X-Task-Token") or "").strip()
    if header_token:
        return secrets.compare_digest(header_token, BATCH_CALL_RUNNER_TOKEN)
    return False


@app.teardown_appcontext
def close_request_connection(_exception=None):
    connection = getattr(g, "_db_connection", None)
    if connection is not None and not connection.closed:
        connection.close()
    g.pop("_db_connection", None)


@app.before_request
def security_preflight():
    enforce_host_allowlist()
    secure_redirect = enforce_https()
    if secure_redirect:
        return secure_redirect


@app.before_request
def initialize_database_once():
    if request.endpoint == "static":
        return
    if request.path == "/login" and request.method == "GET":
        # ログイン画面の表示だけはDB初期化なしで通す。POST時や他画面では従来どおり初期化する。
        return
    ensure_database_schema()


@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if request.path in ("/callback", "/tasks/process-call-queue"):
            return
        validate_csrf()


@app.after_request
def apply_security_headers(response):
    csp = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; "
        "connect-src 'self' https://cdn.jsdelivr.net; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    # Allow camera and microphone for same-origin pages (required for getUserMedia)
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(self), geolocation=()")
    forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if FORCE_HTTPS and (request.is_secure or forwarded_proto == "https"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path.startswith("/admin") or request.path.startswith("/login"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response

LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "10"))
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "300"))

def is_login_rate_limited(ip: str) -> bool:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                window_start = datetime.utcnow() - timedelta(seconds=LOGIN_WINDOW_SECONDS)
                cur.execute(
                    "SELECT COUNT(*) FROM login_attempt_records WHERE ip_address = %s AND attempted_at > %s",
                    (ip, window_start),
                )
                return cur.fetchone()[0] >= LOGIN_MAX_ATTEMPTS
    except Exception:
        app.logger.exception("Failed to check login rate limit for %s", ip)
        return False

def record_login_failure(ip: str):
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO login_attempt_records (ip_address) VALUES (%s)",
                    (ip,),
                )
                conn.commit()
    except Exception:
        app.logger.exception("Failed to record login failure for %s", ip)


def is_webhook_rate_limited(ip: str) -> bool:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                window_start = datetime.utcnow() - timedelta(seconds=WEBHOOK_RATE_LIMIT_WINDOW_SECONDS)
                cur.execute(
                    "SELECT COUNT(*) FROM webhook_request_records WHERE ip_address = %s AND requested_at > %s",
                    (ip, window_start),
                )
                count = cur.fetchone()[0]
                if count >= WEBHOOK_RATE_LIMIT_COUNT:
                    return True
                cur.execute(
                    "INSERT INTO webhook_request_records (ip_address) VALUES (%s)",
                    (ip,),
                )
                conn.commit()
                return False
    except Exception:
        app.logger.exception("Failed to check webhook rate limit for %s", ip)
        return False

# --- ルーティング ---

@app.route("/")
def index():
    return redirect(url_for("login"))


@app.route("/qr")
def qr_reader():
    return render_template("qr_reader.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    ip = request.remote_addr or "unknown"
    if request.method == "POST":
        if is_login_rate_limited(ip):
            abort(429)
        login_id = (request.form.get("login_id") or "").strip().lower()
        password = request.form.get("password")
        account = authenticate_admin_account(login_id, password)
        if account:
            start_admin_session(account["role"], account["id"], account["login_id"])
            record_admin_login(account["role"], ip, request.headers.get("User-Agent"))
            if account["role"] == ROLE_AUDIT_ADMIN:
                return redirect(url_for("admin_login_logs_page"))
            return redirect(url_for("admin_page"))
        else:
            record_admin_login("unknown", ip, request.headers.get("User-Agent"), login_result="failure")
            record_login_failure(ip)
            error = "パスワードが正しくありません"
    return render_template(
        "login.html",
        error=error,
        csrf_token=get_csrf_token(),
        audit_admin_enabled=has_audit_admin_account(),
    )

def ensure_types_table():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reservation_types (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    owner_admin_id INTEGER REFERENCES admin_accounts(id) ON DELETE RESTRICT,
                    accepting BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS type_id INTEGER
                REFERENCES reservation_types(id) ON DELETE SET NULL
            """)
            cur.execute("""
                ALTER TABLE reservation_types
                ADD COLUMN IF NOT EXISTS accepting BOOLEAN NOT NULL DEFAULT TRUE
            """)
            cur.execute("""
                ALTER TABLE reservation_types
                ADD COLUMN IF NOT EXISTS owner_admin_id INTEGER
                REFERENCES admin_accounts(id) ON DELETE RESTRICT
            """)
            cur.execute(
                "SELECT id FROM admin_accounts WHERE role = %s AND active = TRUE ORDER BY id ASC LIMIT 1",
                (ROLE_ADMIN,),
            )
            admin_row = cur.fetchone()
            if admin_row:
                cur.execute(
                    "UPDATE reservation_types SET owner_admin_id = %s WHERE owner_admin_id IS NULL",
                    (admin_row[0],),
                )
            conn.commit()

def ensure_settings_table():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            cur.execute("""
                INSERT INTO app_settings (key, value)
                VALUES ('accepting_new', 'true')
                ON CONFLICT (key) DO NOTHING
            """)
            cur.execute("""
                INSERT INTO app_settings (key, value)
                VALUES ('auto_call_count', '0')
                ON CONFLICT (key) DO NOTHING
            """)
            for key, value in (
                ("last_auto_call_run_at", ""),
                ("last_auto_call_sent_count", "0"),
                ("last_auto_call_failed_count", "0"),
                ("last_auto_call_selected_count", "0"),
                ("previous_auto_call_run_at", ""),
                ("previous_auto_call_sent_count", "0"),
                ("previous_auto_call_failed_count", "0"),
                ("previous_auto_call_selected_count", "0"),
                ("last_wait_time_run_at", ""),
                ("last_wait_time_estimated_seconds", "0"),
                ("last_wait_time_waiting_count", "0"),
                ("last_wait_time_avg_service_seconds", "0"),
            ):
                cur.execute(
                    """
                        INSERT INTO app_settings (key, value)
                        VALUES (%s, %s)
                        ON CONFLICT (key) DO NOTHING
                    """,
                    (key, value),
                )
            conn.commit()


def get_setting(key: str, default: str) -> str:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else default


def set_setting(key: str, value: str):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO app_settings (key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (key, value),
            )
            conn.commit()


def get_settings(keys):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM app_settings WHERE key = ANY(%s)", (list(keys),))
            return {row[0]: row[1] for row in cur.fetchall()}


def set_settings(settings):
    with get_connection() as conn:
        with conn.cursor() as cur:
            for key, value in settings.items():
                cur.execute(
                    """
                        INSERT INTO app_settings (key, value)
                        VALUES (%s, %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    (key, value),
                )
            conn.commit()

def is_accepting_new():
    return get_setting("accepting_new", "true") == "true"

def set_accepting_new(flag: bool):
    set_setting("accepting_new", 'true' if flag else 'false')


def get_auto_call_count() -> int:
    raw = get_setting("auto_call_count", "0").strip()
    return int(raw) if raw.isdigit() else 0


def set_auto_call_count(count: int):
    set_setting("auto_call_count", str(max(0, count)))


def build_auto_call_summary(values, prefix: str):
    run_at = (values.get(f"{prefix}_auto_call_run_at") or "").strip()
    sent_count = int((values.get(f"{prefix}_auto_call_sent_count") or "0").strip() or "0")
    failed_count = int((values.get(f"{prefix}_auto_call_failed_count") or "0").strip() or "0")
    selected_count = int((values.get(f"{prefix}_auto_call_selected_count") or "0").strip() or "0")
    if not run_at:
        return {
            "run_at": "",
            "sent_count": 0,
            "failed_count": 0,
            "selected_count": 0,
            "message": "まだ自動呼出は実行されていません。",
        }
    return {
        "run_at": run_at,
        "sent_count": sent_count,
        "failed_count": failed_count,
        "selected_count": selected_count,
        "message": f"前回: {run_at} / 選択 {selected_count}人 / 呼出 {sent_count}人 / 失敗 {failed_count}人",
    }


def get_auto_call_summary(prefix: str, values=None):
    values = values or get_settings(AUTO_CALL_SETTING_KEYS)
    return build_auto_call_summary(values, prefix)


def get_last_auto_call_summary(values=None):
    values = values or get_settings(AUTO_CALL_SETTING_KEYS)
    previous_summary = build_auto_call_summary(values, "previous")
    if previous_summary["run_at"]:
        return previous_summary
    return build_auto_call_summary(values, "last")


def get_runtime_settings():
    values = get_settings(RUNTIME_SETTING_KEYS)
    raw_auto_call_count = (values.get("auto_call_count") or "0").strip()
    auto_call_count = int(raw_auto_call_count) if raw_auto_call_count.isdigit() else 0
    return {
        "accepting_new": (values.get("accepting_new") or "true") == "true",
        "auto_call_count": auto_call_count,
        "last_auto_call": get_last_auto_call_summary(values),
        "latest_auto_call": get_auto_call_summary("last", values),
        "latest_wait_time": get_latest_wait_time_summary(values),
    }


def serialize_active_rows(rows):
    return [
        {
            "id": row[0],
            "status": row[1],
            "type_id": row[2],
            "type": row[3],
            "created_at": format_dt(row[4]),
        }
        for row in rows
    ]


def fetch_type_counts(cur, owner_admin_id: int):
    cur.execute(
        """
            SELECT t.name, COUNT(*)
            FROM reservations r
            JOIN reservation_types t ON r.type_id = t.id
                        WHERE r.status IN (%s, %s)
              AND t.owner_admin_id = %s
            GROUP BY t.name
            ORDER BY COUNT(*) DESC, t.name ASC
        """,
        (STATUS_WAITING, STATUS_CALLED, owner_admin_id),
    )
    return cur.fetchall()


def serialize_type_counts(rows):
    return [{"name": row[0], "count": row[1]} for row in rows]


def get_active_rows(cur, owner_admin_id: int, current_type_id=None, sort_by="id", sort_order="asc"):
    params = [STATUS_WAITING, STATUS_CALLED]
    where = "WHERE r.status IN (%s, %s) AND t.owner_admin_id = %s"
    params.append(owner_admin_id)
    if current_type_id is not None:
        where += " AND r.type_id = %s"
        params.append(current_type_id)
    order_map = {
        "id": "r.id",
        "status": "r.status",
        "type": "t.name",
    }
    order_by = order_map[sort_by]
    cur.execute(
        f"""
            SELECT r.id, r.status, t.id, t.name, r.created_at
            FROM reservations r
            LEFT JOIN reservation_types t ON r.type_id = t.id
            {where}
            ORDER BY {order_by} {sort_order.upper()}, r.id ASC
        """,
        params,
    )
    return cur.fetchall()


def get_accepting_type_names(cur):
    cur.execute("SELECT name FROM reservation_types WHERE accepting = TRUE ORDER BY id ASC")
    return [row[0] for row in cur.fetchall()]

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin/login-logs")
def admin_login_logs_page():
    if not is_audit_admin_authenticated():
        return redirect(url_for("login"))
    if not has_audit_admin_account():
        abort(404)

    account_error = request.args.get("account_error")
    account_success = request.args.get("account_success")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                    SELECT id, login_result, admin_role, ip_address, user_agent, logged_in_at
                    FROM admin_login_logs
                    ORDER BY logged_in_at DESC, id DESC
                    LIMIT 500
                """
            )
            rows = cur.fetchall()
            # 時刻をフォーマット済み文字列に変換（日本時間対応）
            rows = [
                (row[0], row[1], row[2], row[3], row[4], format_dt(row[5]))
                for row in rows
            ]
            cur.execute(
                """
                    SELECT id, login_id, role, active, created_at
                    FROM admin_accounts
                    ORDER BY id ASC
                """
            )
            admin_accounts = [
                (row[0], row[1], row[2], row[3], format_dt(row[4]))
                for row in cur.fetchall()
            ]
    return render_template(
        "login_logs.html",
        rows=rows,
        admin_accounts=admin_accounts,
        account_error=account_error,
        account_success=account_success,
        csrf_token=get_csrf_token(),
    )


@app.route("/admin/admin-accounts", methods=["POST"])
def admin_accounts_create():
    if not is_audit_admin_authenticated():
        return redirect(url_for("login"))

    login_id = (request.form.get("login_id") or "").strip().lower()
    password = request.form.get("password") or ""
    bulk_accounts_raw = request.form.get("bulk_accounts") or ""
    bulk_lines = [line.strip() for line in bulk_accounts_raw.splitlines() if line.strip()]

    if bulk_lines:
        accounts_to_create = []
        seen_login_ids = set()
        for idx, line in enumerate(bulk_lines, start=1):
            if "," not in line:
                return redirect(
                    url_for(
                        "admin_login_logs_page",
                        account_error=f"{idx}行目の形式が不正です。login_id,password 形式で入力してください。",
                    )
                )
            raw_login_id, raw_password = line.split(",", 1)
            parsed_login_id = raw_login_id.strip().lower()
            parsed_password = raw_password.strip()
            if not LOGIN_ID_PATTERN.fullmatch(parsed_login_id):
                return redirect(
                    url_for(
                        "admin_login_logs_page",
                        account_error=f"{idx}行目のログインIDが不正です。3〜32文字の英小文字・数字・_-で入力してください。",
                    )
                )
            if len(parsed_password) < 8:
                return redirect(
                    url_for(
                        "admin_login_logs_page",
                        account_error=f"{idx}行目のパスワードは8文字以上で入力してください。",
                    )
                )
            if parsed_login_id in seen_login_ids:
                return redirect(
                    url_for(
                        "admin_login_logs_page",
                        account_error=f"入力内でログインID「{parsed_login_id}」が重複しています。",
                    )
                )
            seen_login_ids.add(parsed_login_id)
            accounts_to_create.append((parsed_login_id, parsed_password))

        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    login_ids = [item[0] for item in accounts_to_create]
                    cur.execute(
                        "SELECT login_id FROM admin_accounts WHERE login_id = ANY(%s)",
                        (login_ids,),
                    )
                    existing_ids = {row[0] for row in cur.fetchall()}
                    if existing_ids:
                        existing_label = sorted(existing_ids)[0]
                        return redirect(
                            url_for(
                                "admin_login_logs_page",
                                account_error=f"ログインID「{existing_label}」は既に存在します。",
                            )
                        )
                    for parsed_login_id, parsed_password in accounts_to_create:
                        cur.execute(
                            """
                                INSERT INTO admin_accounts (login_id, password_hash, role, active)
                                VALUES (%s, %s, %s, TRUE)
                            """,
                            (parsed_login_id, generate_password_hash(parsed_password), ROLE_ADMIN),
                        )
                    conn.commit()
        except psycopg2.IntegrityError:
            return redirect(url_for("admin_login_logs_page", account_error="同じログインIDが既に存在します。"))

        return redirect(
            url_for(
                "admin_login_logs_page",
                account_success=f"管理者アカウントを{len(accounts_to_create)}件作成しました。",
            )
        )

    if not LOGIN_ID_PATTERN.fullmatch(login_id):
        return redirect(
            url_for(
                "admin_login_logs_page",
                account_error="ログインIDは3〜32文字の英小文字・数字・_-で入力してください。",
            )
        )
    if len(password) < 8:
        return redirect(
            url_for(
                "admin_login_logs_page",
                account_error="パスワードは8文字以上で入力してください。",
            )
        )

    password_hash = generate_password_hash(password)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                        INSERT INTO admin_accounts (login_id, password_hash, role, active)
                        VALUES (%s, %s, %s, TRUE)
                    """,
                    (login_id, password_hash, ROLE_ADMIN),
                )
                conn.commit()
    except psycopg2.IntegrityError:
        return redirect(url_for("admin_login_logs_page", account_error="同じログインIDが既に存在します。"))

    return redirect(url_for("admin_login_logs_page", account_success=f"管理者アカウント「{login_id}」を作成しました。"))


@app.route("/admin/admin-accounts/<int:account_id>/login-id", methods=["POST"])
def admin_accounts_update_login_id(account_id):
    if not is_audit_admin_authenticated():
        return redirect(url_for("login"))

    login_id = (request.form.get("login_id") or "").strip().lower()
    if not LOGIN_ID_PATTERN.fullmatch(login_id):
        return redirect(
            url_for(
                "admin_login_logs_page",
                account_error="ログインIDは3〜32文字の英小文字・数字・_-で入力してください。",
            )
        )

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE admin_accounts SET login_id = %s WHERE id = %s",
                    (login_id, account_id),
                )
                if cur.rowcount == 0:
                    return redirect(url_for("admin_login_logs_page", account_error="対象アカウントが存在しません。"))
                conn.commit()
    except psycopg2.IntegrityError:
        return redirect(url_for("admin_login_logs_page", account_error="同じログインIDが既に存在します。"))

    return redirect(url_for("admin_login_logs_page", account_success=f"ログインIDを「{login_id}」に更新しました。"))

@app.route("/admin")
def admin_page():
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    type_error = request.args.get("type_error")
    type_id = request.args.get("type_id", "").strip()
    current_type_id = int(type_id) if type_id.isdigit() else None
    sort_by = request.args.get("sort_by", "id").strip()
    sort_order = request.args.get("sort_order", "asc").strip().lower()
    if sort_by not in ("id", "status", "type"):
        sort_by = "id"
    if sort_order not in ("asc", "desc"):
        sort_order = "asc"
    runtime_settings = get_runtime_settings()
    with get_connection() as conn:
        with conn.cursor() as cur:
            rows = get_active_rows(
                cur,
                owner_admin_id=current_admin_account_id,
                current_type_id=current_type_id,
                sort_by=sort_by,
                sort_order=sort_order,
            )
            active_rows = serialize_active_rows(rows)
            cur.execute(
                "SELECT id, name FROM reservation_types WHERE owner_admin_id = %s ORDER BY id ASC",
                (current_admin_account_id,),
            )
            types = cur.fetchall()
            type_counts = serialize_type_counts(fetch_type_counts(cur, current_admin_account_id))
    return render_template(
        "admin.html",
        rows=active_rows,
        types=types,
        type_error=type_error,
        current_type_id=current_type_id,
        type_counts=type_counts,
        sort_by=sort_by,
        sort_order=sort_order,
        accepting_new=runtime_settings["accepting_new"],
        auto_call_count=runtime_settings["auto_call_count"],
        last_auto_call=runtime_settings["last_auto_call"],
        latest_auto_call=runtime_settings["latest_auto_call"],
        admin_refresh_interval_ms=ADMIN_REFRESH_INTERVAL_MS,
        csrf_token=get_csrf_token()
    )


@app.route("/admin/reservation-hours", methods=["POST"])
def admin_reservation_hours():
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    start_text = (request.form.get("reservation_start_time") or "").strip()
    end_text = (request.form.get("reservation_end_time") or "").strip()

    if not start_text and not end_text:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE admin_accounts SET reservation_start_minute = NULL, reservation_end_minute = NULL WHERE id = %s",
                    (current_admin_account_id,),
                )
                conn.commit()
        return redirect(url_for("admin_types_page", schedule_success="予約受付時間の制限を解除しました。"))

    if not start_text or not end_text:
        return redirect(url_for("admin_types_page", schedule_error="開始時刻と終了時刻は両方入力してください。"))

    start_minute = parse_hhmm_to_minute_of_day(start_text)
    end_minute = parse_hhmm_to_minute_of_day(end_text)
    if start_minute is None or end_minute is None:
        return redirect(url_for("admin_types_page", schedule_error="時刻形式が不正です。HH:MM で入力してください。"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                    UPDATE admin_accounts
                    SET reservation_start_minute = %s, reservation_end_minute = %s
                    WHERE id = %s
                """,
                (start_minute, end_minute, current_admin_account_id),
            )
            conn.commit()

    return redirect(
        url_for(
            "admin_types_page",
            schedule_success=f"予約受付時間を {format_minute_of_day(start_minute)}〜{format_minute_of_day(end_minute)} に更新しました。",
        )
    )

@app.route("/admin/data")
def admin_data():
    if not is_admin_authenticated():
        return jsonify({"error": "unauthorized"}), 401
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        return jsonify({"error": "unauthorized"}), 401

    with get_connection() as conn:
        with conn.cursor() as cur:
            rows = get_active_rows(cur, owner_admin_id=current_admin_account_id)
            type_counts = serialize_type_counts(fetch_type_counts(cur, current_admin_account_id))
    runtime_settings = get_runtime_settings()
    return jsonify({
        "rows": serialize_active_rows(rows),
        "meta": {
            "last_auto_call": runtime_settings["last_auto_call"],
            "latest_auto_call": runtime_settings["latest_auto_call"],
            "type_counts": type_counts,
        },
    })

@app.route("/admin/type_counts")
def admin_type_counts():
    if not is_admin_authenticated():
        return jsonify({"error": "unauthorized"}), 401
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        return jsonify({"error": "unauthorized"}), 401

    with get_connection() as conn:
        with conn.cursor() as cur:
            counts = fetch_type_counts(cur, current_admin_account_id)
    return jsonify({
        "counts": serialize_type_counts(counts)
    })

@app.route("/admin/types", methods=["GET", "POST"])
def admin_types_page():
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    accepting_new = is_accepting_new()
    type_error = request.args.get("type_error")
    type_success = request.args.get("type_success")
    schedule_error = request.args.get("schedule_error")
    schedule_success = request.args.get("schedule_success")
    start_minute, end_minute = get_admin_reservation_window(current_admin_account_id)
    reservation_start_time = format_minute_of_day(start_minute)
    reservation_end_time = format_minute_of_day(end_minute)
    if request.method == "POST":
        name = normalize_type_name(request.form.get("name"))
        if not validate_type_name(name):
            return redirect(
                url_for(
                    "admin_types_page",
                    type_error=f"種類名は1〜{MAX_TYPE_NAME_LENGTH}文字、英数字/日本語/スペース/記号(-_・)のみ使用できます。",
                )
            )
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO reservation_types (name, owner_admin_id) VALUES (%s, %s)",
                        (name, current_admin_account_id),
                    )
                    conn.commit()
            return redirect(url_for("admin_types_page", type_success="種類を追加しました。"))
        except psycopg2.IntegrityError:
            return redirect(url_for("admin_types_page", type_error="同じ名前の種類が既に存在します。"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, accepting FROM reservation_types WHERE owner_admin_id = %s ORDER BY id ASC",
                (current_admin_account_id,),
            )
            types = cur.fetchall()
    return render_template(
        "types.html",
        types=types,
        accepting_new=accepting_new,
        type_error=type_error,
        type_success=type_success,
        schedule_error=schedule_error,
        schedule_success=schedule_success,
        reservation_start_time=reservation_start_time,
        reservation_end_time=reservation_end_time,
        csrf_token=get_csrf_token()
    )

@app.route("/admin/types/delete/<int:type_id>", methods=["POST"])
def admin_types_delete(type_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM reservation_types WHERE id = %s AND owner_admin_id = %s",
                (type_id, current_admin_account_id),
            )
            if cur.rowcount == 0:
                abort(403)
            conn.commit()
    return redirect(url_for("admin_types_page"))

@app.route("/admin/types/toggle/<int:type_id>", methods=["POST"])
def admin_types_toggle(type_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reservation_types SET accepting = NOT accepting WHERE id = %s AND owner_admin_id = %s",
                (type_id, current_admin_account_id),
            )
            if cur.rowcount == 0:
                abort(403)
            conn.commit()
    return redirect(url_for("admin_types_page"))

@app.route("/admin/history")
def admin_history():
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    history_page_size = 200
    with get_connection() as conn:
        with conn.cursor() as cur:
            page_raw = request.args.get("page", "1").strip()
            page = int(page_raw) if page_raw.isdigit() and int(page_raw) > 0 else 1
            offset = (page - 1) * history_page_size
            type_id = request.args.get("type_id", "").strip()
            current_type_id = int(type_id) if type_id.isdigit() else None
            sort_by = request.args.get("sort_by", "id").strip()
            sort_order = request.args.get("sort_order", "desc").strip().lower()
            if sort_by not in ("id", "status", "type", "created_at", "service_duration"):
                sort_by = "id"
            if sort_order not in ("asc", "desc"):
                sort_order = "desc"
            params = [STATUS_DONE, STATUS_CANCELLED, current_admin_account_id]
            where = "WHERE r.status IN (%s, %s) AND t.owner_admin_id = %s"
            if current_type_id is not None:
                where += " AND r.type_id = %s"
                params.append(current_type_id)
            order_map = {
                "id": "r.id",
                "status": "r.status",
                "type": "t.name",
                "created_at": "r.created_at",
                "service_duration": "(EXTRACT(EPOCH FROM (r.completed_at - r.called_at)))"
            }
            order_by = order_map[sort_by]
            cur.execute(f"""
                SELECT
                    r.id,
                    r.status,
                    t.name,
                    t.id,
                    r.created_at,
                    r.called_at,
                    r.completed_at,
                    EXTRACT(EPOCH FROM (r.completed_at - r.called_at)) AS service_duration_seconds
                FROM reservations r
                LEFT JOIN reservation_types t ON r.type_id = t.id
                {where}
                ORDER BY {order_by} {sort_order.upper()}, r.id DESC
                LIMIT %s OFFSET %s
            """, params + [history_page_size + 1, offset])
            rows = cur.fetchall()
            # 時刻をフォーマット済み文字列に変換（日本時間対応）
            rows = [
                (row[0], row[1], row[2], row[3], format_dt(row[4]), format_dt(row[5]), format_dt(row[6]), row[7])
                for row in rows
            ]
            cur.execute(
                "SELECT id, name FROM reservation_types WHERE owner_admin_id = %s ORDER BY id ASC",
                (current_admin_account_id,),
            )
            types = cur.fetchall()
    has_next = len(rows) > history_page_size
    rows = rows[:history_page_size]
    return render_template(
        "history.html",
        rows=rows,
        types=types,
        page=page,
        has_prev=page > 1,
        has_next=has_next,
        current_type_id=current_type_id,
        sort_by=sort_by,
        sort_order=sort_order,
        csrf_token=get_csrf_token()
    )


@app.route("/admin/history/export.csv")
def admin_history_export():
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    type_id = request.args.get("type_id", "").strip()
    current_type_id = int(type_id) if type_id.isdigit() else None
    sort_by = request.args.get("sort_by", "id").strip()
    sort_order = request.args.get("sort_order", "desc").strip().lower()
    if sort_by not in ("id", "status", "type", "created_at", "service_duration"):
        sort_by = "id"
    if sort_order not in ("asc", "desc"):
        sort_order = "desc"

    params = [STATUS_DONE, STATUS_CANCELLED, current_admin_account_id]
    where = "WHERE r.status IN (%s, %s) AND t.owner_admin_id = %s"
    if current_type_id is not None:
        where += " AND r.type_id = %s"
        params.append(current_type_id)
    order_map = {
        "id": "r.id",
        "status": "r.status",
        "type": "t.name",
        "created_at": "r.created_at",
        "service_duration": "(EXTRACT(EPOCH FROM (r.completed_at - r.called_at)))"
    }

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "番号",
            "種類",
            "状態",
            "受付時刻",
            "呼出時刻",
            "完了時刻",
            "受付から呼出",
            "受付から完了",
            "呼出から完了",
        ])
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        connection = create_connection()
        try:
            cursor_name = f"history_export_{int(time.time() * 1000)}"
            with connection.cursor(name=cursor_name) as cur:
                cur.itersize = 500
                cur.execute(
                    f"""
                        SELECT
                            r.id,
                            COALESCE(t.name, ''),
                            r.status,
                            r.created_at,
                            r.called_at,
                            r.completed_at,
                            EXTRACT(EPOCH FROM (r.called_at - r.created_at)) AS call_duration_seconds,
                            EXTRACT(EPOCH FROM (r.completed_at - r.created_at)) AS completion_wait_seconds,
                            EXTRACT(EPOCH FROM (r.completed_at - r.called_at)) AS service_duration_seconds
                        FROM reservations r
                        LEFT JOIN reservation_types t ON r.type_id = t.id
                        {where}
                        ORDER BY {order_map[sort_by]} {sort_order.upper()}, r.id DESC
                    """,
                    params,
                )
                for row in cur:
                    writer.writerow([
                        row[0],
                        row[1],
                        row[2],
                        format_dt(row[3]),
                        format_dt(row[4]),
                        format_dt(row[5]),
                        format_duration_from_seconds(row[6]) or "-",
                        format_duration_from_seconds(row[7]) or "-",
                        format_duration_from_seconds(row[8]) or "-",
                    ])
                    yield output.getvalue()
                    output.seek(0)
                    output.truncate(0)
        finally:
            connection.close()

    filename = f"history-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        stream_with_context(generate_csv()),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.route("/admin/call/<int:res_id>", methods=["POST"])
def admin_call(res_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                    UPDATE reservations
                    SET status = %s, called_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                      AND status = %s
                      AND type_id IN (
                          SELECT id FROM reservation_types WHERE owner_admin_id = %s
                      )
                    RETURNING user_id
                """,
                (STATUS_CALLED, res_id, STATUS_WAITING, current_admin_account_id),
            )
            row = cur.fetchone()
            if not row:
                abort(404)
            user_id = row[0]
            conn.commit()

    try:
        send_push_message(user_id, build_call_message(res_id))
    except Exception:
        app.logger.exception("Failed to send LINE push message for reservation %s", res_id)
        with get_connection() as rollback_conn:
            with rollback_conn.cursor() as rollback_cur:
                rollback_cur.execute(
                    "UPDATE reservations SET status = %s, called_at = NULL WHERE id = %s AND status = %s",
                    (STATUS_WAITING, res_id, STATUS_CALLED),
                )
                rollback_conn.commit()
        abort(502)
    return redirect(url_for("admin_page"))

@app.route("/admin/finish/<int:res_id>", methods=["POST"])
def admin_finish(res_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                    UPDATE reservations
                    SET status = %s, completed_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                      AND status = %s
                      AND type_id IN (
                          SELECT id FROM reservation_types WHERE owner_admin_id = %s
                      )
                    RETURNING id
                """,
                (STATUS_DONE, res_id, STATUS_CALLED, current_admin_account_id),
            )
            if not cur.fetchone():
                abort(404)
            conn.commit()
    return redirect(url_for("admin_page"))


@app.route("/admin/toggle-accepting", methods=["POST"])
def admin_toggle_accepting():
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    set_accepting_new(not is_accepting_new())
    return redirect(url_for("admin_page"))


@app.route("/admin/auto-call-count", methods=["POST"])
def admin_auto_call_count():
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    raw_value = (request.form.get("auto_call_count") or "").strip()
    if raw_value.isdigit():
        count = min(int(raw_value), 50)
    else:
        count = 0
    set_auto_call_count(count)
    return redirect(url_for("admin_page"))


@app.route("/tasks/process-call-queue", methods=["POST"])
def process_call_queue_task():
    if not BATCH_CALL_RUNNER_TOKEN:
        return jsonify({"error": "batch runner token is not configured"}), 503
    if not validate_batch_runner_token():
        abort(403)
    result = process_queued_calls()
    return jsonify(result)

# --- LINE Webhook ---
@app.route("/callback", methods=['POST'])
def callback():
    ip = request.remote_addr or "unknown"
    if is_webhook_rate_limited(ip):
        abort(429)
    signature = request.headers.get('X-Line-Signature')
    if not signature:
        abort(400)
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception:
        # ハンドラー内の一時的な失敗で5xxを返すとLINEが再送し、二重処理につながるため200で吸収する。
        app.logger.exception("Failed to process LINE webhook event")
        return 'OK'
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text.strip()
    user_id = event.source.user_id
    process_reservation(event, user_id, user_message)

def process_reservation(event, user_id, user_message):
    normalized = user_message.strip()
    if not normalized:
        send_reply_message(
            event.reply_token,
            "メッセージを受け付けました。予約は「予約」、キャンセルは「キャンセル」、待ち時間は「待ち時間」と送信してください。",
        )
        return
    if len(normalized) > MAX_USER_MESSAGE_CHARS:
        send_reply_message(event.reply_token, f"メッセージは{MAX_USER_MESSAGE_CHARS}文字以内で送信してください。")
        return

    accepting_new = is_accepting_new()
    with get_connection() as conn:
        with conn.cursor() as cur:
            if normalized.startswith('予約'):
                if not accepting_new:
                    reply = "現在、新規の予約受付は停止中です。"
                    send_reply_message(event.reply_token, reply)
                    return
                requested_type_name = normalize_type_name(normalized[2:])
                type_id = None
                type_name = None
                if requested_type_name:
                    if not validate_type_name(requested_type_name):
                        reply = (
                            f"種類名は1〜{MAX_TYPE_NAME_LENGTH}文字で指定してください。"
                            "\n例: 予約 相談"
                        )
                        send_reply_message(event.reply_token, reply)
                        return
                    cur.execute(
                        "SELECT id, name, accepting, owner_admin_id FROM reservation_types WHERE name = %s",
                        (requested_type_name,),
                    )
                    type_row = cur.fetchone()
                    if not type_row:
                        names = get_accepting_type_names(cur)
                        if names:
                            reply = f"指定した種類「{requested_type_name}」は存在しません。\n利用可能: " + " / ".join(names)
                        else:
                            reply = "予約の種類がまだ登録されていません。管理画面で追加してください。"
                        send_reply_message(event.reply_token, reply)
                        return
                    type_id, type_name, type_accepting, type_owner_admin_id = type_row
                    if not type_accepting:
                        names = get_accepting_type_names(cur)
                        if names:
                            reply = f"「{type_name}」の新規受付は停止中です。\n利用可能: " + " / ".join(names)
                        else:
                            reply = f"「{type_name}」の新規受付は停止中です。"
                        send_reply_message(event.reply_token, reply)
                        return
                    if type_owner_admin_id is None:
                        reply = "この種類は管理者に割り当てられていないため予約できません。管理者へお問い合わせください。"
                        send_reply_message(event.reply_token, reply)
                        return
                else:
                    names = get_accepting_type_names(cur)
                    if names:
                        reply = "予約の種類を指定してください。\n利用可能: " + " / ".join(names) + "\n例: 予約 相談"
                    else:
                        reply = "現在受付可能な予約の種類がありません。管理画面で受付を再開してください。"
                    send_reply_message(event.reply_token, reply)
                    return

                cur.execute(
                    """
                        SELECT r.id, r.status, r.type_id, t.name, t.owner_admin_id
                        FROM reservations r
                        LEFT JOIN reservation_types t ON r.type_id = t.id
                        WHERE r.user_id = %s AND r.status IN (%s, %s)
                        ORDER BY r.id DESC LIMIT 1
                    """,
                    (user_id, STATUS_WAITING, STATUS_CALLED)
                )
                existing = cur.fetchone()
                if existing:
                    res_id, status, existing_type_id, existing_type_name, existing_owner_admin_id = existing
                    if status == STATUS_WAITING:
                        if existing_owner_admin_id is not None:
                            waiting_people_ahead = count_waiting_people_ahead_by_owner(
                                cur,
                                reservation_id=res_id,
                                owner_admin_id=existing_owner_admin_id,
                            )
                            reply = f"予約済みです。番号: {res_id} / 種類: {existing_type_name} / 待ち: {waiting_people_ahead}人"
                        else:
                            cur.execute("SELECT COUNT(*) FROM reservations WHERE status = %s AND id < %s", (STATUS_WAITING, res_id))
                            reply = f"予約済みです。番号: {res_id} / 待ち: {cur.fetchone()[0]}人"
                    elif status == STATUS_CALLED:
                        if existing_type_name:
                            reply = f"【呼出中】番号: {res_id} / 種類: {existing_type_name} 会場へお越しください！"
                        else:
                            reply = f"【呼出中】番号: {res_id} 会場へお越しください！"
                else:
                    if type_owner_admin_id:
                        start_minute, end_minute = get_admin_reservation_window(type_owner_admin_id, cur=cur)
                        if start_minute is not None and end_minute is not None:
                            current_dt = datetime.now(JST)
                            current_minute = current_dt.hour * 60 + current_dt.minute
                            if not is_minute_in_window(current_minute, int(start_minute), int(end_minute)):
                                window_label = f"{format_minute_of_day(start_minute)}〜{format_minute_of_day(end_minute)}"
                                reply = f"「{type_name}」の予約受付時間は {window_label} です。現在は受付時間外です。"
                                send_reply_message(event.reply_token, reply)
                                return
                    try:
                        cur.execute(
                            "INSERT INTO reservations (user_id, message, type_id) VALUES (%s, %s, %s) RETURNING id",
                            (user_id, "", type_id),
                        )
                        new_id = cur.fetchone()[0]
                    except psycopg2.IntegrityError:
                        conn.rollback()
                        cur.execute(
                            """
                                SELECT r.id, r.status, r.type_id, t.name, t.owner_admin_id
                                FROM reservations r
                                LEFT JOIN reservation_types t ON r.type_id = t.id
                                WHERE r.user_id = %s AND r.status IN (%s, %s)
                                ORDER BY r.id DESC LIMIT 1
                            """,
                            (user_id, STATUS_WAITING, STATUS_CALLED),
                        )
                        existing_after_conflict = cur.fetchone()
                        if existing_after_conflict:
                            res_id, status, _existing_type_id, existing_type_name, existing_owner_admin_id = existing_after_conflict
                            if status == STATUS_WAITING:
                                if existing_owner_admin_id is not None:
                                    waiting_people_ahead = count_waiting_people_ahead_by_owner(
                                        cur,
                                        reservation_id=res_id,
                                        owner_admin_id=existing_owner_admin_id,
                                    )
                                    reply = f"予約済みです。番号: {res_id} / 種類: {existing_type_name} / 待ち: {waiting_people_ahead}人"
                                else:
                                    cur.execute(
                                        "SELECT COUNT(*) FROM reservations WHERE status = %s AND id < %s",
                                        (STATUS_WAITING, res_id),
                                    )
                                    reply = f"予約済みです。番号: {res_id} / 待ち: {cur.fetchone()[0]}人"
                            elif status == STATUS_CALLED:
                                if existing_type_name:
                                    reply = f"【呼出中】番号: {res_id} / 種類: {existing_type_name} 会場へお越しください！"
                                else:
                                    reply = f"【呼出中】番号: {res_id} 会場へお越しください！"
                            send_reply_message(event.reply_token, reply)
                            return
                        raise
                    conn.commit()
                    if type_owner_admin_id:
                        waiting_people_ahead = count_waiting_people_ahead_by_owner(
                            cur,
                            reservation_id=new_id,
                            owner_admin_id=type_owner_admin_id,
                        )
                        reply = f"【受付完了】番号: {new_id} / 種類: {type_name} / 待ち: {waiting_people_ahead}人"
                    else:
                        cur.execute("SELECT COUNT(*) FROM reservations WHERE status = %s AND id < %s", (STATUS_WAITING, new_id))
                        waiting_people_ahead = int(cur.fetchone()[0] or 0)
                        reply = f"【受付完了】番号: {new_id} / 待ち: {waiting_people_ahead}人"
                    refresh_wait_time_estimate(owner_admin_id=type_owner_admin_id)
                    estimated_minutes = calculate_wait_time_minutes(waiting_people_ahead)
                    reply += f"\n現在の目安待ち時間: {estimated_minutes}分"
            elif normalized == 'キャンセル':
                cur.execute(
                    """
                        UPDATE reservations SET status = %s
                        WHERE id = (
                            SELECT id FROM reservations
                            WHERE user_id = %s AND status IN (%s, %s)
                            ORDER BY id DESC LIMIT 1
                        )
                        RETURNING id
                    """,
                    (STATUS_CANCELLED, user_id, STATUS_WAITING, STATUS_CALLED)
                )
                cancelled = cur.fetchone()
                if cancelled:
                    conn.commit()
                    reply = f"予約番号 {cancelled[0]} をキャンセルしました。"
                else:
                    reply = "キャンセル対象の予約はありません。"
            elif normalized == '待ち時間':
                cur.execute(
                    """
                        SELECT r.id, r.status, t.name, t.owner_admin_id
                        FROM reservations r
                        LEFT JOIN reservation_types t ON r.type_id = t.id
                        WHERE r.user_id = %s AND r.status IN (%s, %s)
                        ORDER BY r.id DESC LIMIT 1
                    """,
                    (user_id, STATUS_WAITING, STATUS_CALLED),
                )
                existing = cur.fetchone()
                if not existing:
                    reply = "待ち時間を確認できる予約がありません。まず「予約 種類名」と送信してください。"
                else:
                    res_id, status, type_name, owner_admin_id = existing
                    if status == STATUS_WAITING:
                        if owner_admin_id is not None:
                            waiting_people_ahead = count_waiting_people_ahead_by_owner(
                                cur,
                                reservation_id=res_id,
                                owner_admin_id=owner_admin_id,
                            )
                        else:
                            cur.execute(
                                "SELECT COUNT(*) FROM reservations WHERE status = %s AND id < %s",
                                (STATUS_WAITING, res_id),
                            )
                            waiting_people_ahead = int(cur.fetchone()[0] or 0)
                        estimated_minutes = calculate_wait_time_minutes(waiting_people_ahead)
                        if type_name:
                            reply = (
                                f"番号: {res_id} / 種類: {type_name} / あなたの前: {waiting_people_ahead}人"
                                f"\n現在の目安待ち時間: {estimated_minutes}分"
                            )
                        else:
                            reply = (
                                f"番号: {res_id} / あなたの前: {waiting_people_ahead}人"
                                f"\n現在の目安待ち時間: {estimated_minutes}分"
                            )
                    else:
                        reply = f"【呼出中】番号: {res_id} です。会場へお越しください。"
            else:
                reply = "メッセージを受け付けました。予約は「予約」、キャンセルは「キャンセル」、待ち時間は「待ち時間」と送信してください。"
    send_reply_message(event.reply_token, reply)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
