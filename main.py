import csv
import io
import os
import re
import secrets
import time
from datetime import timedelta, datetime
from threading import Lock
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import pytz # type: ignore

import psycopg2 # type: ignore
from psycopg2 import sql as psql # type: ignore
from flask import Flask, request, abort, render_template, redirect, url_for, session, jsonify, Response, g, has_request_context, stream_with_context # type: ignore
from linebot import LineBotApi, WebhookHandler # type: ignore
from linebot.exceptions import InvalidSignatureError # type: ignore
from linebot.models import MessageEvent, TextMessage, TextSendMessage # type: ignore
from werkzeug.middleware.proxy_fix import ProxyFix # type: ignore
from werkzeug.security import check_password_hash # type: ignore

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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

APP_VERSION = "v1.0.35"
APP_RELEASED_AT = "2026-04-15 23:20 JST"

FORCE_HTTPS = parse_bool_env("FORCE_HTTPS", True)
ALLOWED_HOSTS = {
    host.strip().lower() for host in os.getenv("ALLOWED_HOSTS", "").split(",") if host.strip()
}
SESSION_IDLE_TIMEOUT_SECONDS = int(os.getenv("SESSION_IDLE_TIMEOUT_SECONDS", "1800"))
MAX_TYPE_NAME_LENGTH = int(os.getenv("MAX_TYPE_NAME_LENGTH", "40"))
MAX_USER_MESSAGE_CHARS = int(os.getenv("MAX_USER_MESSAGE_CHARS", "100"))
TYPE_NAME_PATTERN = re.compile(
    rf"^[A-Za-z0-9ぁ-んァ-ヶー一-龠々・ 　_-]{{1,{MAX_TYPE_NAME_LENGTH}}}$"
)

WEBHOOK_RATE_LIMIT_COUNT = int(os.getenv("WEBHOOK_RATE_LIMIT_COUNT", "120"))
WEBHOOK_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("WEBHOOK_RATE_LIMIT_WINDOW_SECONDS", "60"))
BATCH_CALL_RUNNER_TOKEN = (os.getenv("BATCH_CALL_RUNNER_TOKEN") or "").strip()

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

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

STATUS_WAITING = "waiting"
STATUS_CALLED = "called"
STATUS_ARRIVED = "arrived"
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
ROLE_ADMIN = "admin"
ROLE_AUDIT_ADMIN = "audit_admin"
SCHEMA_LOCK = Lock()
SCHEMA_READY = False
RUNTIME_SETTING_KEYS = ("accepting_new", "auto_call_count") + AUTO_CALL_SETTING_KEYS

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


def format_dt(value):
    if not value:
        return ""
    # UTCまたはnaive datetimeの場合、JSTに変換
    if value.tzinfo is None:
        # naiveな場合、UTCとして扱う
        value = pytz.utc.localize(value)
    elif value.tzinfo != pytz.timezone('Asia/Tokyo'):
        # UTCまたは他のタイムゾーンの場合、JSTに変換
        value = value.astimezone(pytz.timezone('Asia/Tokyo'))
    else:
        # 既にJSTの場合
        if value.tzinfo != pytz.timezone('Asia/Tokyo'):
            value = value.astimezone(pytz.timezone('Asia/Tokyo'))
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


def should_run_call_batch(now=None) -> bool:
    current = now or time.localtime()
    return current.tm_min % 5 == 0


def process_queued_calls(now=None):
    # 日本時間で現在時刻を取得
    jst = pytz.timezone('Asia/Tokyo')
    current_dt = datetime.now(jst) if now is None else now
    current = current_dt.timetuple()
    minute_label = current_dt.strftime("%m-%d %H:%M")
    if not should_run_call_batch(current):
        return {
            "processed": False,
            "reason": "not_due",
            "minute": minute_label,
            "sent_count": 0,
            "failed_count": 0,
        }

    ensure_database_schema()
    runtime_settings = get_runtime_settings()
    auto_call_count = runtime_settings["auto_call_count"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            auto_rows = []
            if auto_call_count > 0:
                cur.execute(
                    """
                        SELECT r.id, r.user_id
                        FROM reservations r
                        WHERE r.status = %s
                        ORDER BY r.id ASC
                        LIMIT %s
                    """,
                    (STATUS_WAITING, auto_call_count),
                )
                auto_rows = cur.fetchall()

    sent_ids = []
    failed_ids = []
    for res_id, user_id in auto_rows:
        try:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=f"【順番が来ました】番号 {res_id} 番の方、会場へお越しください！"),
            )
            sent_ids.append(res_id)
        except Exception:
            failed_ids.append(res_id)
            app.logger.exception("Failed to send LINE push message for reservation %s", res_id)

    if sent_ids:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE reservations SET status = %s, called_at = CURRENT_TIMESTAMP WHERE id = ANY(%s) AND status = %s",
                    (STATUS_CALLED, sent_ids, STATUS_WAITING),
                )
                conn.commit()

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

    return {
        "processed": True,
        "reason": "ok",
        "minute": minute_label,
        "auto_call_count": auto_call_count,
        "auto_selected_count": len(auto_rows),
        "sent_count": len(sent_ids),
        "failed_count": len(failed_ids),
        "failed_ids": failed_ids,
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
                ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS called_at TIMESTAMP
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS arrived_at TIMESTAMP
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
            conn.commit()


def ensure_database_schema():
    global SCHEMA_READY
    if SCHEMA_READY:
        return
    with SCHEMA_LOCK:
        if SCHEMA_READY:
            return
        ensure_reservations_table()
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


def start_admin_session(role: str):
    now = time.time()
    session.clear()
    session["logged_in"] = True
    session["admin_role"] = role
    session["issued_at"] = now
    session["last_activity"] = now
    session["_csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True


def is_authenticated_as(role: str, update_activity: bool = True) -> bool:
    if not session.get("logged_in"):
        return False
    if session.get("admin_role") != role:
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
        "script-src 'self'; "
        "style-src 'self' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
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

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    ip = request.remote_addr or "unknown"
    if request.method == "POST":
        if is_login_rate_limited(ip):
            abort(429)
        password = request.form.get("password")
        if verify_admin_password(password):
            start_admin_session(ROLE_ADMIN)
            record_admin_login(ROLE_ADMIN, ip, request.headers.get("User-Agent"))
            return redirect(url_for("admin_page"))
        if verify_audit_admin_password(password):
            start_admin_session(ROLE_AUDIT_ADMIN)
            record_admin_login(ROLE_AUDIT_ADMIN, ip, request.headers.get("User-Agent"))
            return redirect(url_for("admin_login_logs_page"))
        else:
            record_admin_login("unknown", ip, request.headers.get("User-Agent"), login_result="failure")
            record_login_failure(ip)
            error = "パスワードが正しくありません"
    return render_template(
        "login.html",
        error=error,
        csrf_token=get_csrf_token(),
        audit_admin_enabled=bool(AUDIT_ADMIN_PASSWORD_HASH),
    )

def ensure_types_table():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reservation_types (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
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


def fetch_type_counts(cur):
    cur.execute(
        """
            SELECT COALESCE(t.name, '未設定') AS name, COUNT(*)
            FROM reservations r
            LEFT JOIN reservation_types t ON r.type_id = t.id
            WHERE r.status IN (%s, %s, %s)
            GROUP BY COALESCE(t.name, '未設定')
            ORDER BY COUNT(*) DESC, name ASC
        """,
        (STATUS_WAITING, STATUS_CALLED, STATUS_ARRIVED),
    )
    return cur.fetchall()


def serialize_type_counts(rows):
    return [{"name": row[0], "count": row[1]} for row in rows]


def get_active_rows(cur, current_type_id=None, sort_by="id", sort_order="asc"):
    params = [STATUS_WAITING, STATUS_CALLED, STATUS_ARRIVED]
    where = "WHERE r.status IN (%s, %s, %s)"
    if current_type_id is not None:
        where += " AND r.type_id = %s"
        params.append(current_type_id)
    order_map = {
        "id": psql.SQL("r.id"),
        "status": psql.SQL("r.status"),
        "type": psql.SQL("t.name"),
    }
    order_by = order_map.get(sort_by, order_map["id"])
    order_direction = psql.SQL("DESC") if sort_order == "desc" else psql.SQL("ASC")
    query = psql.SQL("""
            SELECT r.id, r.status, t.id, t.name, r.created_at AT TIME ZONE 'Asia/Tokyo'
            FROM reservations r
            LEFT JOIN reservation_types t ON r.type_id = t.id
            {where}
            ORDER BY {order_by} {order_direction}, r.id ASC
        """).format(
        where=psql.SQL(where),
        order_by=order_by,
        order_direction=order_direction,
    )
    cur.execute(query, params)
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
    if not AUDIT_ADMIN_PASSWORD_HASH:
        abort(404)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                    SELECT id, login_result, admin_role, ip_address, user_agent, logged_in_at AT TIME ZONE 'Asia/Tokyo'
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
    return render_template(
        "login_logs.html",
        rows=rows,
        csrf_token=get_csrf_token(),
    )

@app.route("/admin")
def admin_page():
    if not is_admin_authenticated():
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
            rows = get_active_rows(cur, current_type_id=current_type_id, sort_by=sort_by, sort_order=sort_order)
            active_rows = serialize_active_rows(rows)
            cur.execute("SELECT id, name FROM reservation_types ORDER BY id ASC")
            types = cur.fetchall()
            type_counts = serialize_type_counts(fetch_type_counts(cur))
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
        admin_initial_data={
            "rows": active_rows,
            "meta": {
                "last_auto_call": runtime_settings["last_auto_call"],
                "latest_auto_call": runtime_settings["latest_auto_call"],
                "type_counts": type_counts,
            },
        },
        csrf_token=get_csrf_token()
    )

@app.route("/admin/data")
def admin_data():
    if not is_admin_authenticated():
        return jsonify({"error": "unauthorized"}), 401

    with get_connection() as conn:
        with conn.cursor() as cur:
            rows = get_active_rows(cur)
            type_counts = serialize_type_counts(fetch_type_counts(cur))
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

    with get_connection() as conn:
        with conn.cursor() as cur:
            counts = fetch_type_counts(cur)
    return jsonify({
        "counts": serialize_type_counts(counts)
    })

@app.route("/admin/types", methods=["GET", "POST"])
def admin_types_page():
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    accepting_new = is_accepting_new()
    type_error = request.args.get("type_error")
    type_success = request.args.get("type_success")
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
                    cur.execute("INSERT INTO reservation_types (name) VALUES (%s)", (name,))
                    conn.commit()
            return redirect(url_for("admin_types_page", type_success="種類を追加しました。"))
        except psycopg2.IntegrityError:
            return redirect(url_for("admin_types_page", type_error="同じ名前の種類が既に存在します。"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, accepting FROM reservation_types ORDER BY id ASC")
            types = cur.fetchall()
    return render_template(
        "types.html",
        types=types,
        accepting_new=accepting_new,
        type_error=type_error,
        type_success=type_success,
        csrf_token=get_csrf_token()
    )

@app.route("/admin/types/delete/<int:type_id>", methods=["POST"])
def admin_types_delete(type_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reservation_types WHERE id = %s", (type_id,))
            conn.commit()
    return redirect(url_for("admin_types_page"))

@app.route("/admin/types/toggle/<int:type_id>", methods=["POST"])
def admin_types_toggle(type_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE reservation_types SET accepting = NOT accepting WHERE id = %s", (type_id,))
            conn.commit()
    return redirect(url_for("admin_types_page"))

@app.route("/admin/history")
def admin_history():
    if not is_admin_authenticated():
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
            params = []
            where = "WHERE r.status IN (%s, %s, %s)"
            params.extend([STATUS_DONE, STATUS_CANCELLED, STATUS_ARRIVED])
            if current_type_id is not None:
                where += " AND r.type_id = %s"
                params.append(current_type_id)
            order_map = {
                "id": psql.SQL("r.id"),
                "status": psql.SQL("r.status"),
                "type": psql.SQL("t.name"),
                "created_at": psql.SQL("r.created_at"),
                "service_duration": psql.SQL("(EXTRACT(EPOCH FROM (r.completed_at - r.arrived_at)))"),
            }
            order_by = order_map.get(sort_by, order_map["id"])
            order_direction = psql.SQL("DESC") if sort_order == "desc" else psql.SQL("ASC")
            query = psql.SQL("""
                SELECT
                    r.id,
                    r.status,
                    t.name,
                    t.id,
                    r.created_at AT TIME ZONE 'Asia/Tokyo',
                    r.arrived_at AT TIME ZONE 'Asia/Tokyo',
                    r.completed_at AT TIME ZONE 'Asia/Tokyo',
                    EXTRACT(EPOCH FROM (r.completed_at - r.arrived_at)) AS service_duration_seconds
                FROM reservations r
                LEFT JOIN reservation_types t ON r.type_id = t.id
                {where}
                ORDER BY {order_by} {order_direction}, r.id DESC
                LIMIT %s OFFSET %s
            """).format(
                where=psql.SQL(where),
                order_by=order_by,
                order_direction=order_direction,
            )
            cur.execute(query, params + [history_page_size + 1, offset])
            rows = cur.fetchall()
            # 時刻をフォーマット済み文字列に変換（日本時間対応）
            rows = [
                (row[0], row[1], row[2], row[3], format_dt(row[4]), format_dt(row[5]), format_dt(row[6]), row[7])
                for row in rows
            ]
            cur.execute("SELECT id, name FROM reservation_types ORDER BY id ASC")
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

    type_id = request.args.get("type_id", "").strip()
    current_type_id = int(type_id) if type_id.isdigit() else None
    sort_by = request.args.get("sort_by", "id").strip()
    sort_order = request.args.get("sort_order", "desc").strip().lower()
    if sort_by not in ("id", "status", "type", "created_at", "service_duration"):
        sort_by = "id"
    if sort_order not in ("asc", "desc"):
        sort_order = "desc"

    params = [STATUS_DONE, STATUS_CANCELLED, STATUS_ARRIVED]
    where = "WHERE r.status IN (%s, %s, %s)"
    if current_type_id is not None:
        where += " AND r.type_id = %s"
        params.append(current_type_id)
    order_map = {
        "id": psql.SQL("r.id"),
        "status": psql.SQL("r.status"),
        "type": psql.SQL("t.name"),
        "created_at": psql.SQL("r.created_at"),
        "service_duration": psql.SQL("(EXTRACT(EPOCH FROM (r.completed_at - r.arrived_at)))"),
    }

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["番号", "種類", "状態", "受付時刻", "到着時刻", "完了時刻", "到着から完了"])
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        connection = create_connection()
        try:
            cursor_name = f"history_export_{int(time.time() * 1000)}"
            with connection.cursor(name=cursor_name) as cur:
                cur.itersize = 500
                order_by = order_map.get(sort_by, order_map["id"])
                order_direction = psql.SQL("DESC") if sort_order == "desc" else psql.SQL("ASC")
                query = psql.SQL("""
                        SELECT
                            r.id,
                            COALESCE(t.name, ''),
                            r.status,
                            r.created_at AT TIME ZONE 'Asia/Tokyo',
                            r.arrived_at AT TIME ZONE 'Asia/Tokyo',
                            r.completed_at AT TIME ZONE 'Asia/Tokyo',
                            EXTRACT(EPOCH FROM (r.completed_at - r.arrived_at)) AS service_duration_seconds
                        FROM reservations r
                        LEFT JOIN reservation_types t ON r.type_id = t.id
                        {where}
                        ORDER BY {order_by} {order_direction}, r.id DESC
                    """).format(
                    where=psql.SQL(where),
                    order_by=order_by,
                    order_direction=order_direction,
                )
                cur.execute(query, params)
                for row in cur:
                    writer.writerow([
                        row[0],
                        row[1],
                        row[2],
                        format_dt(row[3]),
                        format_dt(row[4]),
                        format_dt(row[5]),
                        format_duration_from_seconds(row[6]) or "-",
                    ])
                    yield output.getvalue()
                    output.seek(0)
                    output.truncate(0)
        finally:
            connection.close()

    filename = f"espresso-history-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        stream_with_context(generate_csv()),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.route("/admin/call/<int:res_id>", methods=["POST"])
def admin_call(res_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reservations SET status = %s, called_at = CURRENT_TIMESTAMP WHERE id = %s AND status = %s RETURNING user_id",
                (STATUS_CALLED, res_id, STATUS_WAITING),
            )
            row = cur.fetchone()
            if not row:
                abort(404)
            user_id = row[0]
            conn.commit()

    try:
        line_bot_api.push_message(
            user_id,
            TextSendMessage(text=f"【順番が来ました】番号 {res_id} 番の方、会場へお越しください！"),
        )
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

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reservations SET status = %s, completed_at = CURRENT_TIMESTAMP WHERE id = %s AND status = %s RETURNING id",
                (STATUS_DONE, res_id, STATUS_ARRIVED),
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
        app.logger.exception("Unhandled error while processing webhook")
        abort(500)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message_raw = getattr(event.message, "text", None)
    user_id = getattr(event.source, "user_id", None)
    if not isinstance(user_message_raw, str) or not isinstance(user_id, str):
        app.logger.warning("Received malformed LINE message payload")
        return
    user_message = user_message_raw.strip()
    try:
        process_reservation(event, user_id, user_message)
    except Exception:
        app.logger.exception("Unhandled error while processing user message")
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="処理中にエラーが発生しました。時間をおいて再度お試しください。"),
            )
        except Exception:
            app.logger.exception("Failed to reply error message")

def process_reservation(event, user_id, user_message):
    normalized = user_message.strip()
    if not normalized:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="メッセージを受け付けました。予約は「予約」、キャンセルは「キャンセル」、到着は「到着」と送信してください。")
        )
        return
    if len(normalized) > MAX_USER_MESSAGE_CHARS:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"メッセージは{MAX_USER_MESSAGE_CHARS}文字以内で送信してください。"),
        )
        return

    accepting_new = is_accepting_new()
    with get_connection() as conn:
        with conn.cursor() as cur:
            if normalized.startswith('予約'):
                if not accepting_new:
                    reply = "現在、新規の予約受付は停止中です。"
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
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
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                        return
                    cur.execute("SELECT id, name, accepting FROM reservation_types WHERE name = %s", (requested_type_name,))
                    type_row = cur.fetchone()
                    if not type_row:
                        names = get_accepting_type_names(cur)
                        if names:
                            reply = f"指定した種類「{requested_type_name}」は存在しません。\n利用可能: " + " / ".join(names)
                        else:
                            reply = "予約の種類がまだ登録されていません。管理画面で追加してください。"
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                        return
                    type_id, type_name, type_accepting = type_row
                    if not type_accepting:
                        names = get_accepting_type_names(cur)
                        if names:
                            reply = f"「{type_name}」の新規受付は停止中です。\n利用可能: " + " / ".join(names)
                        else:
                            reply = f"「{type_name}」の新規受付は停止中です。"
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                        return
                else:
                    names = get_accepting_type_names(cur)
                    if names:
                        reply = "予約の種類を指定してください。\n利用可能: " + " / ".join(names) + "\n例: 予約 相談"
                    else:
                        reply = "現在受付可能な予約の種類がありません。管理画面で受付を再開してください。"
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return

                cur.execute(
                    """
                        SELECT r.id, r.status, r.type_id, t.name
                        FROM reservations r
                        LEFT JOIN reservation_types t ON r.type_id = t.id
                        WHERE r.user_id = %s AND r.status IN (%s, %s, %s)
                        ORDER BY r.id DESC LIMIT 1
                    """,
                    (user_id, STATUS_WAITING, STATUS_CALLED, STATUS_ARRIVED)
                )
                existing = cur.fetchone()
                if existing:
                    res_id, status, existing_type_id, existing_type_name = existing
                    if status == STATUS_WAITING:
                        if existing_type_id is not None:
                            cur.execute(
                                "SELECT COUNT(*) FROM reservations WHERE status = %s AND id < %s AND type_id = %s",
                                (STATUS_WAITING, res_id, existing_type_id),
                            )
                            reply = f"予約済みです。番号: {res_id} / 種類: {existing_type_name} / 待ち: {cur.fetchone()[0]}人"
                        else:
                            cur.execute("SELECT COUNT(*) FROM reservations WHERE status = %s AND id < %s", (STATUS_WAITING, res_id))
                            reply = f"予約済みです。番号: {res_id} / 待ち: {cur.fetchone()[0]}人"
                    elif status == STATUS_CALLED:
                        if existing_type_name:
                            reply = f"【呼出中】番号: {res_id} / 種類: {existing_type_name} 会場へお越しください！"
                        else:
                            reply = f"【呼出中】番号: {res_id} 会場へお越しください！"
                    else:
                        if existing_type_name:
                            reply = f"到着受付済みです。番号: {res_id} / 種類: {existing_type_name} / スタッフが確認します。"
                        else:
                            reply = f"到着受付済みです。番号: {res_id} / スタッフが確認します。"
                else:
                    cur.execute("INSERT INTO reservations (user_id, message, type_id) VALUES (%s, %s, %s) RETURNING id", (user_id, "", type_id))
                    new_id = cur.fetchone()[0]
                    conn.commit()
                    if type_id:
                        cur.execute("SELECT COUNT(*) FROM reservations WHERE status = %s AND id < %s AND type_id = %s", (STATUS_WAITING, new_id, type_id))
                        reply = f"【受付完了】番号: {new_id} / 種類: {type_name} / 待ち: {cur.fetchone()[0]}人"
                    else:
                        cur.execute("SELECT COUNT(*) FROM reservations WHERE status = %s AND id < %s", (STATUS_WAITING, new_id))
                        reply = f"【受付完了】番号: {new_id} / 待ち: {cur.fetchone()[0]}人"
            elif normalized == 'キャンセル':
                cur.execute(
                    """
                        UPDATE reservations SET status = %s
                        WHERE id = (
                            SELECT id FROM reservations
                            WHERE user_id = %s AND status IN (%s, %s, %s)
                            ORDER BY id DESC LIMIT 1
                        )
                        RETURNING id
                    """,
                    (STATUS_CANCELLED, user_id, STATUS_WAITING, STATUS_CALLED, STATUS_ARRIVED)
                )
                cancelled = cur.fetchone()
                if cancelled:
                    reply = f"予約番号 {cancelled[0]} をキャンセルしました。"
                else:
                    reply = "キャンセル対象の予約はありません。"
            elif normalized == '到着':
                cur.execute(
                    """
                        SELECT id, status FROM reservations
                        WHERE user_id = %s AND status IN (%s, %s, %s)
                        ORDER BY id DESC LIMIT 1
                    """,
                    (user_id, STATUS_WAITING, STATUS_CALLED, STATUS_ARRIVED)
                )
                existing = cur.fetchone()
                if not existing:
                    reply = "到着の対象となる予約がありません。"
                else:
                    res_id, status = existing
                    if status == STATUS_WAITING:
                        reply = "まだ呼出されていません。呼出後に「到着」と送信してください。"
                    else:
                        cur.execute(
                            "UPDATE reservations SET status = %s, arrived_at = CURRENT_TIMESTAMP WHERE id = %s",
                            (STATUS_ARRIVED, res_id),
                        )
                        conn.commit()
                        reply = f"到着を受け付けました。番号: {res_id} / スタッフが確認します。"
            else:
                reply = "メッセージを受け付けました。予約は「予約」、キャンセルは「キャンセル」、到着は「到着」と送信してください。"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
