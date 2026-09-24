"""環境変数のパース・アプリケーション全体の定数定義。"""
import os
import re
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo


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
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                urlencode(query),
                parsed.fragment,
            )
        )
    return url


def parse_allowed_hosts(raw_value: str) -> set[str]:
    return {
        host.strip().lower()
        for host in re.split(r"[,\s]+", raw_value)
        if host.strip()
    }


# --- セキュリティ設定 ---
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is required")

ADMIN_PASSWORD_HASH = (os.getenv("ADMIN_PASSWORD_HASH") or "").strip()
if not ADMIN_PASSWORD_HASH:
    raise RuntimeError("ADMIN_PASSWORD_HASH is required")
ADMIN_PASSWORD_DEPRECATED_SET = bool(os.getenv("ADMIN_PASSWORD"))

AUDIT_ADMIN_PASSWORD_HASH = (os.getenv("AUDIT_ADMIN_PASSWORD_HASH") or "").strip()

CHANNEL_ACCESS_TOKEN = (os.getenv("CHANNEL_ACCESS_TOKEN") or "").strip()
CHANNEL_SECRET = (os.getenv("CHANNEL_SECRET") or "").strip()
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("CHANNEL_ACCESS_TOKEN and CHANNEL_SECRET are required")

# 負荷テスト用: true にすると LINE への実際の push/reply 送信をスキップする。
# 本番では絶対に true にしないこと。
LOAD_TEST_MODE = (os.getenv("LOAD_TEST_MODE") or "").strip().lower() == "true"
LOAD_TEST_TOKEN = (os.getenv("LOAD_TEST_TOKEN") or "").strip()

raw_db_url = (os.getenv("DATABASE_URL") or "").strip()
if not raw_db_url:
    raise RuntimeError("DATABASE_URL is required")
DATABASE_URL = normalize_db_url(raw_db_url)
DB_CONNECT_TIMEOUT = parse_int_env("DB_CONNECT_TIMEOUT", 5, 1, 60)
REDIS_URL = (os.getenv("REDIS_URL") or "").strip()
REDIS_ENTRA_ID_ENABLED = parse_bool_env("REDIS_ENTRA_ID_ENABLED", False)
REDIS_ENTRA_IDENTITY_TYPE = (
    os.getenv("REDIS_ENTRA_IDENTITY_TYPE") or "system_assigned"
).strip().lower()
REDIS_ENTRA_ID_CLIENT_ID = (os.getenv("REDIS_ENTRA_ID_CLIENT_ID") or "").strip()
REDIS_ENTRA_ID_RESOURCE = (
    os.getenv("REDIS_ENTRA_ID_RESOURCE") or "https://redis.azure.com/"
).strip()

APP_VERSION = "v1.0.214"
APP_RELEASED_AT = "2026-09-23 00:00 JST"
MAX_BROADCAST_MESSAGE_CHARS = 5000
GLOBAL_RESERVATION_DELETE_ENABLED = parse_bool_env(
    "ENABLE_GLOBAL_RESERVATION_DELETE", False
)
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
ALLOWED_TYPE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
FLEX_SAFE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
MAX_TYPE_IMAGE_SIZE = (1920, 1080)
JPEG_QUALITY = 85

FORCE_HTTPS = parse_bool_env("FORCE_HTTPS", True)

# リバースプロキシが付与する Forwarded 系ヘッダーを信頼する段数。
# アプリを直接公開する場合は 0 にして、外部からのヘッダー偽装を防ぐ。
TRUSTED_PROXY_HOPS = parse_int_env("TRUSTED_PROXY_HOPS", 1, 0, 3)

MAX_REQUEST_BODY_BYTES = parse_int_env(
    "MAX_REQUEST_BODY_BYTES", 64 * 1024 * 1024, 1024, 256 * 1024 * 1024
)
MAX_BACKUP_FILE_BYTES = parse_int_env(
    "MAX_BACKUP_FILE_BYTES", 50 * 1024 * 1024, 1024, 256 * 1024 * 1024
)
MAX_TYPE_IMAGE_UPLOAD_BYTES = parse_int_env(
    "MAX_TYPE_IMAGE_UPLOAD_BYTES", 10 * 1024 * 1024, 1024, 100 * 1024 * 1024
)

ALLOWED_HOSTS = parse_allowed_hosts(os.getenv("ALLOWED_HOSTS", ""))

# 本番環境はデプロイ先にかかわらず明示的に指定する。APP_ENV の設定漏れ時も、
# Render と Azure Container Apps では安全側の production として扱う。
_DEFAULT_APP_ENV = (
    "production" if os.getenv("RENDER") or os.getenv("CONTAINER_APP_NAME") else "development"
)
APP_ENV = (os.getenv("APP_ENV") or _DEFAULT_APP_ENV).strip().lower()
if APP_ENV not in {"development", "production"}:
    raise RuntimeError("APP_ENV must be either development or production")
if APP_ENV == "production" and not ALLOWED_HOSTS:
    raise RuntimeError(
        "ALLOWED_HOSTS is required when APP_ENV=production. Set it to your app domain(s)"
    )

SESSION_IDLE_TIMEOUT_SECONDS = parse_int_env("SESSION_IDLE_TIMEOUT_SECONDS", 1800, 60, 86400)
MAX_TYPE_NAME_LENGTH = parse_int_env("MAX_TYPE_NAME_LENGTH", 40, 1, 255)
MAX_TYPE_FLAVOR_TEXT_CHARS = 100
MAX_TYPE_PRICE = parse_int_env("MAX_TYPE_PRICE", 99998, 1, 999999999)
MAX_USER_MESSAGE_CHARS = parse_int_env("MAX_USER_MESSAGE_CHARS", 100, 10, 10000)
TYPE_NAME_PATTERN = re.compile(
    rf"^[A-Za-z0-9ぁ-んァ-ヶー一-龠々・ 　_-]{{1,{MAX_TYPE_NAME_LENGTH}}}$"
)
LOGIN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}$")

USER_REQUEST_RATE_LIMIT_COUNT = parse_int_env(
    "USER_REQUEST_RATE_LIMIT_COUNT", 10, 1, 1000
)
USER_REQUEST_RATE_LIMIT_WINDOW_SECONDS = parse_int_env(
    "USER_REQUEST_RATE_LIMIT_WINDOW_SECONDS", 60, 1, 86400
)
WEBHOOK_ASYNC_WORKERS = parse_int_env("WEBHOOK_ASYNC_WORKERS", 4, 1, 32)
CALL_TIMEOUT_MINUTES = parse_int_env("CALL_TIMEOUT_MINUTES", 15, 1, 1440)
ADMIN_REFRESH_INTERVAL_MS = parse_int_env(
    "ADMIN_REFRESH_INTERVAL_MS", 15000, 1000, 300000
)
BATCH_CALL_RUNNER_TOKEN = (os.getenv("BATCH_CALL_RUNNER_TOKEN") or "").strip()
LINE_PUSH_MAX_RETRIES = parse_int_env("LINE_PUSH_MAX_RETRIES", 3, 1, 10)
LINE_PUSH_RETRY_BASE_SECONDS = parse_int_env("LINE_PUSH_RETRY_BASE_SECONDS", 1, 1, 30)
LINE_PUSH_RETRY_MAX_SECONDS = parse_int_env("LINE_PUSH_RETRY_MAX_SECONDS", 8, 1, 300)
LOGIN_MAX_ATTEMPTS = parse_int_env("LOGIN_MAX_ATTEMPTS", 10, 1, 1000)
LOGIN_WINDOW_SECONDS = parse_int_env("LOGIN_WINDOW_SECONDS", 300, 1, 86400)

JST = ZoneInfo("Asia/Tokyo")
SESSION_COOKIE_LIFETIME = timedelta(seconds=SESSION_IDLE_TIMEOUT_SECONDS)

STATUS_WAITING = "waiting"
STATUS_CALLED = "called"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
CALL_ORIGIN_AUTO = "auto"
CALL_ORIGIN_MANUAL = "manual"
CALL_ORIGIN_LABELS = {
    CALL_ORIGIN_AUTO: "自動",
    CALL_ORIGIN_MANUAL: "手動",
}

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
RUNTIME_SETTING_KEYS = (
    ("accepting_new",)
    + AUTO_CALL_SETTING_KEYS
    + WAIT_TIME_SETTING_KEYS
)
