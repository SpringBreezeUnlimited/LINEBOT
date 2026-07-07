import base64
import csv
import mimetypes
import json
import io
import math
import os
import re
import secrets
import time
import uuid
from datetime import timedelta, datetime, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import psycopg2  # type: ignore
from PIL import Image, ImageOps, UnidentifiedImageError  # type: ignore
from flask import Flask, request, abort, render_template, redirect, url_for, session, jsonify, Response, g, has_request_context, stream_with_context, send_file  # type: ignore
from flask.sessions import SecureCookieSessionInterface  # type: ignore
from linebot.v3 import WebhookHandler  # type: ignore
from linebot.v3.exceptions import InvalidSignatureError  # type: ignore
from linebot.v3.messaging import ApiClient, Configuration, MessagingApi, PushMessageRequest, ReplyMessageRequest, TextMessage  # type: ignore
from linebot.v3.messaging.models.flex_box import FlexBox  # type: ignore
from linebot.v3.messaging.models.flex_bubble import FlexBubble  # type: ignore
from linebot.v3.messaging.models.flex_carousel import FlexCarousel  # type: ignore
from linebot.v3.messaging.models.flex_image import FlexImage  # type: ignore
from linebot.v3.messaging.models.flex_message import FlexMessage  # type: ignore
from linebot.v3.messaging.models.flex_button import FlexButton  # type: ignore
from linebot.v3.messaging.models.flex_text import FlexText  # type: ignore
from linebot.v3.webhooks import MessageEvent, TextMessageContent  # type: ignore
from linebot.v3.messaging.exceptions import ApiException  # type: ignore
from werkzeug.middleware.proxy_fix import ProxyFix  # type: ignore
from werkzeug.utils import secure_filename  # type: ignore
from werkzeug.security import check_password_hash, generate_password_hash  # type: ignore
from werkzeug.exceptions import HTTPException  # type: ignore
from flex_templates import (
    reservation_confirmation,
    call_notification,
    wait_time_status,
    cancel_notification,
    auto_cancel_notification,
)
from flex_templates import bubble_from_title_and_text

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 600
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


# --- セキュリティ設定 ---
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is required")
app.secret_key = SECRET_KEY

ADMIN_PASSWORD_HASH = (os.getenv("ADMIN_PASSWORD_HASH") or "").strip()
if not ADMIN_PASSWORD_HASH:
    raise RuntimeError("ADMIN_PASSWORD_HASH is required")
if os.getenv("ADMIN_PASSWORD"):
    app.logger.warning(
        "ADMIN_PASSWORD is deprecated and ignored. Use ADMIN_PASSWORD_HASH only."
    )
AUDIT_ADMIN_PASSWORD_HASH = (os.getenv("AUDIT_ADMIN_PASSWORD_HASH") or "").strip()

CHANNEL_ACCESS_TOKEN = (os.getenv("CHANNEL_ACCESS_TOKEN") or "").strip()
CHANNEL_SECRET = (os.getenv("CHANNEL_SECRET") or "").strip()
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("CHANNEL_ACCESS_TOKEN and CHANNEL_SECRET are required")

# 負荷テスト用: true にすると LINE への実際の push/reply 送信をスキップする。
# 本番では絶対に true にしないこと。
LOAD_TEST_MODE = (os.getenv("LOAD_TEST_MODE") or "").strip().lower() == "true"

raw_db_url = (os.getenv("DATABASE_URL") or "").strip()
if not raw_db_url:
    raise RuntimeError("DATABASE_URL is required")
DATABASE_URL = normalize_db_url(raw_db_url)
DB_CONNECT_TIMEOUT = parse_int_env("DB_CONNECT_TIMEOUT", 5, 1, 60)

OWNER_LINE_ID = os.getenv("OWNER_LINE_ID", "").strip()

APP_VERSION = "v1.0.150"
APP_RELEASED_AT = "2026-07-01 00:00 JST"
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
ALLOWED_TYPE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
FLEX_SAFE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
MAX_TYPE_IMAGE_SIZE = (1920, 1080)
JPEG_QUALITY = 85

FORCE_HTTPS = parse_bool_env("FORCE_HTTPS", True)
def parse_allowed_hosts(raw_value: str) -> set[str]:
    return {
        host.strip().lower()
        for host in re.split(r"[,\s]+", raw_value)
        if host.strip()
    }


ALLOWED_HOSTS = parse_allowed_hosts(os.getenv("ALLOWED_HOSTS", ""))

# 本番環境での安全性チェック
IS_PRODUCTION = bool(os.getenv("RENDER"))
if IS_PRODUCTION and not ALLOWED_HOSTS:
    raise RuntimeError(
        "ALLOWED_HOSTS is required in production environment. Set it to your Render app domain(s)"
    )

SESSION_IDLE_TIMEOUT_SECONDS = parse_int_env("SESSION_IDLE_TIMEOUT_SECONDS", 1800, 60, 86400)
MAX_TYPE_NAME_LENGTH = parse_int_env("MAX_TYPE_NAME_LENGTH", 40, 1, 255)
MAX_USER_MESSAGE_CHARS = parse_int_env("MAX_USER_MESSAGE_CHARS", 100, 10, 10000)
TYPE_NAME_PATTERN = re.compile(
    rf"^[A-Za-z0-9ぁ-んァ-ヶー一-龠々・ 　_-]{{1,{MAX_TYPE_NAME_LENGTH}}}$"
)
LOGIN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}$")

WEBHOOK_RATE_LIMIT_COUNT = parse_int_env("WEBHOOK_RATE_LIMIT_COUNT", 120, 1, 10000)
WEBHOOK_RATE_LIMIT_WINDOW_SECONDS = parse_int_env(
    "WEBHOOK_RATE_LIMIT_WINDOW_SECONDS", 60, 1, 86400
)
CALL_TIMEOUT_MINUTES = parse_int_env("CALL_TIMEOUT_MINUTES", 15, 1, 1440)
ADMIN_REFRESH_INTERVAL_MS = parse_int_env(
    "ADMIN_REFRESH_INTERVAL_MS", 15000, 1000, 300000
)
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
    SESSION_COOKIE_NAME=(
        "__Host-session" if parse_bool_env("SESSION_COOKIE_SECURE", True) else "session"
    ),
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_IDLE_TIMEOUT_SECONDS),
)
app.jinja_env.autoescape = True


class AppSessionInterface(SecureCookieSessionInterface):
    def should_set_cookie(self, app, session):  # type: ignore[override]
        # /static/ 以下や、ファビコンなどの静的ファイルには絶対にクッキーを発行しない
        if (
            request.endpoint == "static"
            or request.path.startswith("/static/")
            or request.path in ("/favicon.ico", "/robots.txt")
        ):
            return False
            
        #★追加：トップページ（ログイン画面へのリダイレクトのみ）もクッキー不要
        if request.path == "/":
            return False
            
        return super().should_set_cookie(app, session)

app.session_interface = AppSessionInterface()


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
RUNTIME_SETTING_KEYS = (
    ("accepting_new", "auto_call_count")
    + AUTO_CALL_SETTING_KEYS
    + WAIT_TIME_SETTING_KEYS
)


class ManagedConnection:
    def __init__(self, connection, close_on_exit: bool):
        self._connection = connection
        self._close_on_exit = close_on_exit

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __enter__(self):
        return self._connection

    def __exit__(self, exc_type, exc, tb):
        if self._close_on_exit and not self._connection.closed:
            self._connection.close()
        return False


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


def push_message_with_retry_key(
    messaging_api: MessagingApi, request_payload: PushMessageRequest, retry_key: str
):
    try:
        return messaging_api.push_message(request_payload, x_line_retry_key=retry_key)
    except TypeError as error:
        message = str(error)
        if "x_line_retry_key" not in message:
            raise
        app.logger.warning(
            "line-bot-sdk does not support x_line_retry_key argument; fallback without retry key"
        )
        return messaging_api.push_message(request_payload)


def build_flex_component(component):
    if isinstance(component, (FlexBubble, FlexBox, FlexText, FlexCarousel)):
        return component
    if not isinstance(component, dict):
        return component

    component_type = component.get("type")
    if component_type == "text":
        return FlexText(
            text="" if component.get("text") is None else str(component.get("text")),
            flex=component.get("flex"),
            size=component.get("size"),
            align=component.get("align"),
            gravity=component.get("gravity"),
            color=component.get("color"),
            weight=component.get("weight"),
            style=component.get("style"),
            decoration=component.get("decoration"),
            wrap=component.get("wrap"),
            lineSpacing=component.get("lineSpacing"),
            margin=component.get("margin"),
            position=component.get("position"),
            offsetTop=component.get("offsetTop"),
            offsetBottom=component.get("offsetBottom"),
            offsetStart=component.get("offsetStart"),
            offsetEnd=component.get("offsetEnd"),
            action=component.get("action"),
            maxLines=component.get("maxLines"),
            adjustMode=component.get("adjustMode"),
            scaling=component.get("scaling"),
        )
    if component_type == "box":
        return FlexBox(
            layout=component.get("layout") or "vertical",
            flex=component.get("flex"),
            contents=[
                build_flex_component(item) for item in component.get("contents") or []
            ],
            spacing=component.get("spacing"),
            margin=component.get("margin"),
            position=component.get("position"),
            offsetTop=component.get("offsetTop"),
            offsetBottom=component.get("offsetBottom"),
            offsetStart=component.get("offsetStart"),
            offsetEnd=component.get("offsetEnd"),
            backgroundColor=component.get("backgroundColor"),
            borderColor=component.get("borderColor"),
            borderWidth=component.get("borderWidth"),
            cornerRadius=component.get("cornerRadius"),
            width=component.get("width"),
            maxWidth=component.get("maxWidth"),
            height=component.get("height"),
            maxHeight=component.get("maxHeight"),
            paddingAll=component.get("paddingAll"),
            paddingTop=component.get("paddingTop"),
            paddingBottom=component.get("paddingBottom"),
            paddingStart=component.get("paddingStart"),
            paddingEnd=component.get("paddingEnd"),
            action=component.get("action"),
            justifyContent=component.get("justifyContent"),
            alignItems=component.get("alignItems"),
            background=component.get("background"),
        )
    if component_type == "bubble":
        return FlexBubble(
            direction=component.get("direction"),
            styles=component.get("styles"),
            header=build_flex_component(component.get("header")),
            hero=build_flex_component(component.get("hero")),
            body=build_flex_component(component.get("body")),
            footer=build_flex_component(component.get("footer")),
            size=component.get("size"),
            action=component.get("action"),
        )
    if component_type == "carousel":
        return FlexCarousel(
            contents=[
                build_flex_component(item) for item in component.get("contents") or []
            ]
        )
    if component_type == "button":
        return FlexButton.from_dict(component)
    if component_type == "image":
        url = (component.get("url") or "").strip()
        if not url:
            return None
        return FlexImage(
            url=url,
            flex=component.get("flex"),
            margin=component.get("margin"),
            position=component.get("position"),
            offsetTop=component.get("offsetTop"),
            offsetBottom=component.get("offsetBottom"),
            offsetStart=component.get("offsetStart"),
            offsetEnd=component.get("offsetEnd"),
            align=component.get("align"),
            gravity=component.get("gravity"),
            size=component.get("size") or "md",
            aspectRatio=component.get("aspectRatio"),
            aspectMode=component.get("aspectMode"),
            backgroundColor=component.get("backgroundColor"),
            action=component.get("action"),
            animated=component.get("animated", False),
        )
    return component


def build_flex_message(message: dict):
    alt_text = (
        message.get("altText") or message.get("alt_text") or "通知"
    ).strip() or "通知"
    contents = build_flex_component(message.get("contents"))
    if contents is None:
        raise ValueError("flex message contents is required")
    return FlexMessage(altText=alt_text, contents=contents)


def build_line_message(message: str | dict):
    if isinstance(message, dict):
        message_type = message.get("type")
        if message_type == "flex" or ("altText" in message and "contents" in message):
            return build_flex_message(message)
        if message_type == "text":
            return TextMessage(
                text="" if message.get("text") is None else str(message.get("text"))
            )
        try:
            return TextMessage(
                text=json.dumps(message, ensure_ascii=False, separators=(",", ":"))
            )
        except Exception:
            return TextMessage(text="(invalid message)")
    return TextMessage(text="" if message is None else str(message))


def send_push_message(user_id: str, message: str | dict, retry_key: str | None = None):
    if LOAD_TEST_MODE:
        app.logger.info("LOAD_TEST_MODE: push message skipped user_id=%s", user_id)
        return
    stable_retry_key = retry_key or str(uuid.uuid4())
    payload = PushMessageRequest(
        to=user_id,
        messages=[build_line_message(message)],
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
                app.logger.info(
                    "Push already accepted (409) retry_key=%s user_id=%s",
                    stable_retry_key,
                    user_id,
                )
                return
            if attempt >= LINE_PUSH_MAX_RETRIES or not is_retryable_push_error(error):
                raise
            delay_seconds = min(
                LINE_PUSH_RETRY_MAX_SECONDS,
                LINE_PUSH_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
            )
            app.logger.warning(
                "Push failed (attempt %s/%s, status=%s). Retry after %ss retry_key=%s",
                attempt,
                LINE_PUSH_MAX_RETRIES,
                status,
                delay_seconds,
                stable_retry_key,
            )
            time.sleep(delay_seconds)


def send_reply_message(reply_token: str, message: str | dict):
    if LOAD_TEST_MODE:
        app.logger.info(
            "LOAD_TEST_MODE: reply message skipped reply_token=%s", reply_token
        )
        return
    try:
        if isinstance(message, dict):
            message = sanitize_flex_message(message)
        payload = ReplyMessageRequest(
            reply_token=reply_token, messages=[build_line_message(message)]
        )
        with ApiClient(MESSAGING_CONFIGURATION) as api_client:
            MessagingApi(api_client).reply_message(payload)
    except ApiException as error:
        status = extract_http_status(error)
        if status == 400 and isinstance(message, dict):
            fallback_message = strip_flex_hero(message)
            if fallback_message is not None:
                try:
                    payload = ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[build_line_message(fallback_message)],
                    )
                    with ApiClient(MESSAGING_CONFIGURATION) as api_client:
                        MessagingApi(api_client).reply_message(payload)
                    return
                except Exception:
                    app.logger.exception(
                        "Fallback reply without hero also failed reply_token=%s",
                        reply_token,
                    )
        app.logger.exception("Failed to send reply message reply_token=%s", reply_token)
    except Exception:
        app.logger.exception("Failed to send reply message reply_token=%s", reply_token)


def send_flex_notice(reply_token: str, title: str, body: str, hero_url: str | None = None):
    send_reply_message(
        reply_token, bubble_from_title_and_text(title, body, hero_url=hero_url)
    )


def build_type_image_url(type_id: int | None) -> str | None:
    if not type_id:
        return None
    path = f"/reservation-type-images/{type_id}"
    if has_request_context():
        path = url_for("reservation_type_image", type_id=type_id)
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}{path}"
    if has_request_context():
        base_url = (request.url_root or "").rstrip("/")
        if base_url.startswith("http://"):
            base_url = "https://" + base_url[len("http://") :]
        if base_url:
            return f"{base_url}{path}"
    return None


@app.route("/reservation-type-images/<int:type_id>")
def reservation_type_image(type_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                    SELECT image_data, image_mime_type, image_path
                    FROM reservation_types
                    WHERE id = %s
                """,
                (type_id,),
            )
            row = cur.fetchone()
    if not row:
        abort(404)
    image_data, image_mime_type, image_path = row
    if image_data:
        return send_file(
            io.BytesIO(image_data),
            mimetype=image_mime_type or "application/octet-stream",
        )
    if image_path:
        legacy_path = Path(app.root_path) / "static" / image_path.lstrip("/")
        if legacy_path.is_file():
            mimetype, _ = mimetypes.guess_type(legacy_path.name)
            return send_file(
                legacy_path, mimetype=mimetype or "application/octet-stream"
            )
    abort(404)


def strip_flex_hero(message: dict) -> dict | None:
    if not isinstance(message, dict):
        return None
    if message.get("type") != "flex":
        return None
    contents = message.get("contents")
    if not isinstance(contents, dict):
        return None
    updated = dict(message)
    updated_contents = dict(contents)
    if updated_contents.get("type") == "bubble":
        bubble = dict(updated_contents)
        bubble.pop("hero", None)
        updated["contents"] = bubble
        return updated
    if updated_contents.get("type") == "carousel":
        bubbles = []
        for bubble in updated_contents.get("contents") or []:
            if isinstance(bubble, dict):
                clone = dict(bubble)
                clone.pop("hero", None)
                bubbles.append(clone)
            else:
                bubbles.append(bubble)
        updated_contents["contents"] = bubbles
        updated["contents"] = updated_contents
        return updated
    return None


def sanitize_flex_message(message: dict) -> dict:
    sanitized = dict(message)
    contents = sanitized.get("contents")
    if not isinstance(contents, dict):
        return sanitized

    contents_type = contents.get("type")
    if contents_type == "bubble":
        bubble = dict(contents)
        hero = bubble.get("hero")
        if isinstance(hero, dict):
            hero_url = (hero.get("url") or "").strip()
            if not hero_url.startswith("https://"):
                bubble.pop("hero", None)
        sanitized["contents"] = bubble
        return sanitized

    if contents_type == "carousel":
        carousel = dict(contents)
        cleaned_bubbles = []
        for item in carousel.get("contents") or []:
            if not isinstance(item, dict):
                cleaned_bubbles.append(item)
                continue
            clone = dict(item)
            hero = clone.get("hero")
            if isinstance(hero, dict):
                hero_url = (hero.get("url") or "").strip()
                if not hero_url.startswith("https://"):
                    clone.pop("hero", None)
            cleaned_bubbles.append(clone)
        carousel["contents"] = cleaned_bubbles
        sanitized["contents"] = carousel
        return sanitized

    return sanitized


def save_type_image_upload(image_file) -> tuple[bytes, str, str]:
    filename = (getattr(image_file, "filename", "") or "").strip()
    if not filename:
        return b"", "", ""
    suffix = Path(secure_filename(filename)).suffix.lower()
    if suffix not in ALLOWED_TYPE_IMAGE_EXTENSIONS:
        raise ValueError("画像は jpg, jpeg, png, gif, webp のみアップロードできます。")
    raw_data = image_file.read()
    if not raw_data:
        return b"", "", ""

    try:
        with Image.open(io.BytesIO(raw_data)) as source:
            source = ImageOps.exif_transpose(source)
            source.load()
            has_alpha = "A" in source.getbands()
            is_animated = bool(getattr(source, "is_animated", False))
            if is_animated:
                source.seek(0)
                frame = source.convert("RGBA" if has_alpha else "RGB")
            else:
                frame = source.convert("RGBA" if has_alpha else "RGB")

            needs_resize = frame.width > MAX_TYPE_IMAGE_SIZE[0] or frame.height > MAX_TYPE_IMAGE_SIZE[1]
            if needs_resize:
                frame.thumbnail(MAX_TYPE_IMAGE_SIZE, Image.Resampling.LANCZOS)

            use_png = has_alpha or suffix == ".png"
            if is_animated:
                use_png = False

            buffer = io.BytesIO()
            if use_png:
                output_ext = ".png"
                output_mimetype = "image/png"
                frame.save(buffer, format="PNG", optimize=True)
            else:
                output_ext = ".jpg"
                output_mimetype = "image/jpeg"
                if frame.mode != "RGB":
                    frame = frame.convert("RGB")
                frame.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
            if output_ext not in FLEX_SAFE_IMAGE_EXTENSIONS:
                raise ValueError("Flex Message で利用できない画像形式です。")
            data = buffer.getvalue()
            if not data:
                return b"", "", ""
            base_name = Path(secure_filename(filename)).stem or "image"
            return data, output_mimetype, f"{base_name}{output_ext}"
    except UnidentifiedImageError as error:
        raise ValueError("画像ファイルとして認識できませんでした。") from error


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


def calculate_wait_time_minutes(people_ahead: int) -> int:
    ahead = max(0, int(people_ahead))
    return max(0, math.ceil(ahead * 0.5 + 2))


def count_waiting_people_ahead_by_owner(
    cur, reservation_id: int, owner_admin_id: int
) -> int:
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


def should_run_midnight_cancel(now=None) -> bool:
    current = now or time.localtime()
    return current.tm_hour == 0 and current.tm_min == 0


def build_call_message(reservation_no: int, called_at=None) -> dict:
    called_dt = datetime.now(JST) if called_at is None else called_at.astimezone(JST)
    timeout_at = called_dt + timedelta(minutes=CALL_TIMEOUT_MINUTES)
    timeout_label = timeout_at.strftime("%H:%M")
    return call_notification(reservation_no, timeout_label, CALL_TIMEOUT_MINUTES)


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
                        RETURNING id, user_id, COALESCE(reservation_no, id)
                    """,
                    (STATUS_CANCELLED, STATUS_CALLED, CALL_TIMEOUT_MINUTES),
                )
                timed_out_rows = cur.fetchall()
                conn.commit()
        for timed_out_row in timed_out_rows:
            reservation_id = timed_out_row[0]
            user_id = timed_out_row[1]
            reservation_no = (
                timed_out_row[2] if len(timed_out_row) > 2 else reservation_id
            )
            try:
                flex = auto_cancel_notification(reservation_no or reservation_id)
                send_push_message(user_id, flex)
            except Exception:
                app.logger.exception(
                    "Failed to send timeout message for reservation %s user_id=%s",
                    reservation_id,
                    user_id,
                )
        return len(timed_out_rows)
    except Exception:
        app.logger.exception(
            "Failed to expire called reservations CALL_TIMEOUT_MINUTES=%s",
            CALL_TIMEOUT_MINUTES,
        )
        return 0


def cancel_active_reservations_without_notification() -> int:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                        UPDATE reservations
                        SET status = %s
                        WHERE status IN (%s, %s)
                        RETURNING id
                    """,
                    (STATUS_CANCELLED, STATUS_WAITING, STATUS_CALLED),
                )
                cancelled_rows = cur.fetchall()
                conn.commit()
        return len(cancelled_rows)
    except Exception:
        app.logger.exception("Failed to cancel active reservations at midnight")
        return 0


def process_queued_calls(now=None):
    # 日本時間で現在時刻を取得
    current_dt = datetime.now(JST) if now is None else now
    current = current_dt.timetuple()
    minute_label = current_dt.strftime("%m-%d %H:%M")
    midnight_cancel_count = 0
    timed_out_count = 0
    if should_run_midnight_cancel(current):
        midnight_cancel_count = cancel_active_reservations_without_notification()
    else:
        timed_out_count = expire_called_reservations()
    cleanup_rate_limit_records()
    latest_wait_time = refresh_wait_time_estimate(current_dt)
    if not should_run_call_batch(current):
        return {
            "processed": False,
            "reason": "not_due",
            "minute": minute_label,
            "timed_out_count": timed_out_count,
            "midnight_cancel_count": midnight_cancel_count,
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
                # 先に該当行をロックして状態を更新しておくことで、並行実行や手動呼出しとの競合で重複通知が送られるのを防ぐ
                cur.execute(
                    """
                        WITH selected_rows AS (
                            SELECT r.id, r.user_id, COALESCE(r.reservation_no, r.id)
                            FROM reservations r
                            JOIN reservation_types t ON r.type_id = t.id
                            WHERE r.status = %s
                            ORDER BY r.id ASC
                            FOR UPDATE SKIP LOCKED
                            LIMIT %s
                        )
                        UPDATE reservations
                        SET status = %s, called_at = CURRENT_TIMESTAMP
                        WHERE id IN (
                            SELECT id FROM selected_rows
                        )
                          AND status = %s
                        RETURNING id, user_id, COALESCE(reservation_no, id)
                    """,
                    (STATUS_WAITING, auto_call_count, STATUS_CALLED, STATUS_WAITING),
                )
                auto_rows = cur.fetchall()
                conn.commit()

    sent_ids = []
    failed_ids = []
    for auto_row in auto_rows:
        res_id = auto_row[0]
        user_id = auto_row[1]
        reservation_no = auto_row[2] if len(auto_row) > 2 else res_id
        try:
            # Build Flex call notification; alt text will be used as fallback when needed
            timeout_at = (
                datetime.now(JST) + timedelta(minutes=CALL_TIMEOUT_MINUTES)
            ).strftime("%H:%M")
            flex = call_notification(reservation_no or res_id, timeout_at, CALL_TIMEOUT_MINUTES)
            send_push_message(user_id, flex)
            sent_ids.append(res_id)
        except Exception:
            failed_ids.append(res_id)
            app.logger.exception(
                "Failed to send LINE push message for reservation %s user_id=%s",
                res_id,
                user_id,
            )

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
        settings_to_save.update(
            {
                "previous_auto_call_run_at": previous_summary["run_at"],
                "previous_auto_call_sent_count": str(previous_summary["sent_count"]),
                "previous_auto_call_failed_count": str(
                    previous_summary["failed_count"]
                ),
                "previous_auto_call_selected_count": str(
                    previous_summary["selected_count"]
                ),
            }
        )
    set_settings(settings_to_save)
    latest_wait_time = refresh_wait_time_estimate(current_dt)

    return {
        "processed": True,
        "reason": "ok",
        "minute": minute_label,
        "timed_out_count": timed_out_count,
        "midnight_cancel_count": midnight_cancel_count,
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
                    cur.execute(
                        "SELECT COUNT(*) FROM reservations WHERE status = %s",
                        (STATUS_WAITING,),
                    )
                    waiting_count = int(cur.fetchone()[0] or 0)
                else:
                    cur.execute(
                        """
                            SELECT COUNT(*)
                            FROM reservations r
                            JOIN reservation_types t ON r.type_id = t.id
                            WHERE r.status = %s AND COALESCE(r.owner_admin_id, t.owner_admin_id) = %s
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
        app.logger.exception(
            "Failed to refresh wait time estimate owner_admin_id=%s", owner_admin_id
        )
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
    estimated_seconds_raw = (
        values.get("last_wait_time_estimated_seconds") or "0"
    ).strip()
    waiting_count_raw = (values.get("last_wait_time_waiting_count") or "0").strip()
    avg_service_seconds_raw = (
        values.get("last_wait_time_avg_service_seconds") or "0"
    ).strip()
    estimated_seconds = (
        int(estimated_seconds_raw) if estimated_seconds_raw.isdigit() else 0
    )
    waiting_count = int(waiting_count_raw) if waiting_count_raw.isdigit() else 0
    avg_service_seconds = (
        int(avg_service_seconds_raw) if avg_service_seconds_raw.isdigit() else 0
    )
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
                ADD COLUMN IF NOT EXISTS owner_admin_id INTEGER
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS reservation_no INTEGER
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS user_id TEXT
            """)
            cur.execute("""
                ALTER TABLE reservations
                ALTER COLUMN user_id SET NOT NULL
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
                CREATE INDEX IF NOT EXISTS idx_reservations_owner_admin_id_id
                ON reservations (owner_admin_id, id DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_reservations_owner_admin_id_reservation_no
                ON reservations (owner_admin_id, reservation_no DESC)
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
            cur.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_reservations_user_active
                    ON reservations (user_id)
                    WHERE status IN ('waiting', 'called')
                """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_reservations_owner_reservation_no
                ON reservations (owner_admin_id, reservation_no)
                WHERE owner_admin_id IS NOT NULL AND reservation_no IS NOT NULL
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
                    admin_account_id INTEGER REFERENCES admin_accounts(id) ON DELETE SET NULL,
                    admin_login_id TEXT,
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
                ALTER TABLE admin_login_logs
                ADD COLUMN IF NOT EXISTS admin_account_id INTEGER
            """)
            cur.execute("""
                ALTER TABLE admin_login_logs
                ADD COLUMN IF NOT EXISTS admin_login_id TEXT
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'admin_login_logs_admin_account_id_fkey'
                    ) THEN
                        ALTER TABLE admin_login_logs
                        ADD CONSTRAINT admin_login_logs_admin_account_id_fkey
                        FOREIGN KEY (admin_account_id) REFERENCES admin_accounts(id) ON DELETE SET NULL;
                    END IF;
                END
                $$;
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_admin_login_logs_admin_account_id
                ON admin_login_logs (admin_account_id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_admin_login_logs_logged_in_at
                ON admin_login_logs (logged_in_at DESC)
            """)
            conn.commit()


def ensure_admin_accounts_table():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                    CREATE TABLE IF NOT EXISTS admin_accounts (
                        id SERIAL PRIMARY KEY,
                        login_id TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        role TEXT NOT NULL,
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        next_reservation_no INTEGER NOT NULL DEFAULT 1,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            cur.execute("""
                ALTER TABLE admin_accounts
                ADD COLUMN IF NOT EXISTS next_reservation_no INTEGER NOT NULL DEFAULT 1
            """)
            cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_admin_accounts_role_active
                    ON admin_accounts (role, active)
                """)
            cur.execute(
                """
                    INSERT INTO admin_accounts (login_id, password_hash, role)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (login_id) DO UPDATE SET password_hash = EXCLUDED.password_hash
                """,
                ("admin", ADMIN_PASSWORD_HASH, ROLE_ADMIN),
            )
            if AUDIT_ADMIN_PASSWORD_HASH:
                cur.execute(
                    """
                        INSERT INTO admin_accounts (login_id, password_hash, role)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (login_id) DO UPDATE SET password_hash = EXCLUDED.password_hash
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


def sync_reservation_owner_numbers(cur):
    cur.execute(
        """
            UPDATE reservations r
            SET owner_admin_id = COALESCE(r.owner_admin_id, t.owner_admin_id)
            FROM reservation_types t
            WHERE r.type_id = t.id
              AND r.owner_admin_id IS NULL
              AND t.owner_admin_id IS NOT NULL
        """
    )
    cur.execute(
        """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'fk_reservations_owner_admin_id'
                ) THEN
                    ALTER TABLE reservations
                    ADD CONSTRAINT fk_reservations_owner_admin_id
                    FOREIGN KEY (owner_admin_id)
                    REFERENCES admin_accounts(id)
                    ON DELETE RESTRICT;
                END IF;
            END
            $$;
        """
    )
    cur.execute(
        """
            WITH numbered AS (
                SELECT
                    r.id,
                    r.owner_admin_id,
                    COALESCE(max_existing.max_reservation_no, 0)
                    + row_number() OVER (
                        PARTITION BY r.owner_admin_id
                        ORDER BY r.created_at ASC NULLS LAST, r.id ASC
                    ) AS reservation_no
                FROM reservations r
                LEFT JOIN (
                    SELECT owner_admin_id, MAX(reservation_no) AS max_reservation_no
                    FROM reservations
                    WHERE owner_admin_id IS NOT NULL
                      AND reservation_no IS NOT NULL
                    GROUP BY owner_admin_id
                ) AS max_existing
                    ON max_existing.owner_admin_id = r.owner_admin_id
                WHERE r.owner_admin_id IS NOT NULL
                  AND r.reservation_no IS NULL
            )
            UPDATE reservations r
            SET reservation_no = numbered.reservation_no
            FROM numbered
            WHERE r.id = numbered.id
        """
    )
    cur.execute(
        """
            UPDATE admin_accounts a
            SET next_reservation_no = GREATEST(
                a.next_reservation_no,
                COALESCE(next_numbers.next_reservation_no, 1)
            )
            FROM (
                SELECT owner_admin_id, COALESCE(MAX(reservation_no), 0) + 1 AS next_reservation_no
                FROM reservations
                WHERE owner_admin_id IS NOT NULL
                  AND reservation_no IS NOT NULL
                GROUP BY owner_admin_id
            ) AS next_numbers
            WHERE a.id = next_numbers.owner_admin_id
        """
    )


def hour_digit(now=None) -> int:
    """現在時刻（JST）から Y 桁を決定する。
    10時:1 / 11時:2 / 12時:3 / 13時:4 / 14時:5 / それ以外:0
    """
    dt = now or datetime.now(JST)
    return {10: 1, 11: 2, 12: 3, 13: 4, 14: 5}.get(dt.hour, 0)


def allocate_admin_reservation_no(cur, owner_admin_id: int) -> int:
    """申込順の連番 XXX（1〜999 ループ）と時間帯 Y（hour_digit()）を合成した
    4 桁固定の整数 XXXY を採番して返す。
    表示には fmt_no() を使うこと。
    """
    cur.execute(
        """
            SELECT next_reservation_no
            FROM admin_accounts
            WHERE id = %s
            FOR UPDATE
        """,
        (owner_admin_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError("owner admin account not found")
    seq = int(row[0] or 1)
    if seq > 999:
        seq = 1

    # 999 予約を超えると seq がループするため、既存の (owner_admin_id, XXXY) と
    # 衝突する可能性がある（履歴・キャンセル済みの行も含め reservation_no は
    # 削除されないため）。UNIQUE 制約違反で予約作成そのものが失敗しないよう、
    # 同じ XXX 帯で未使用の Y を探し、全て埋まっていれば次の XXX に進める。
    preferred_digit = hour_digit()
    for _ in range(999):
        cur.execute(
            """
                SELECT reservation_no FROM reservations
                WHERE owner_admin_id = %s AND reservation_no >= %s AND reservation_no < %s
            """,
            (owner_admin_id, seq * 10, seq * 10 + 10),
        )
        used_digits = {r[0] % 10 for r in cur.fetchall()}
        if preferred_digit not in used_digits:
            digit = preferred_digit
            break
        available_digits = [d for d in range(10) if d not in used_digits]
        if available_digits:
            digit = secrets.choice(available_digits)
            break
        seq = seq + 1 if seq < 999 else 1
    else:
        digit = preferred_digit

    next_seq = seq + 1 if seq < 999 else 1
    cur.execute(
        """
            UPDATE admin_accounts
            SET next_reservation_no = %s
            WHERE id = %s
        """,
        (next_seq, owner_admin_id),
    )
    return seq * 10 + digit


def fmt_no(reservation_no: int | str) -> str:
    """予約番号を 4 桁 0 埋め文字列（XXXY 形式）に変換する。
    int はそのままゼロ埋め。str はいったん int 変換を試み、
    失敗した場合（None 由来の空文字など）はそのまま返す。
    """
    if isinstance(reservation_no, int):
        return f"{reservation_no:04d}"
    try:
        return f"{int(reservation_no):04d}"
    except (ValueError, TypeError):
        return str(reservation_no)


def cleanup_rate_limit_records():
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM login_attempt_records WHERE attempted_at < CURRENT_TIMESTAMP - INTERVAL '1 day'"
                )
                cur.execute(
                    "DELETE FROM webhook_request_records WHERE requested_at < CURRENT_TIMESTAMP - INTERVAL '1 day'"
                )
                conn.commit()
    except Exception:
        app.logger.exception("Failed to cleanup rate limit records")


def record_admin_login(
    role: str,
    admin_account_id: int | None,
    admin_login_id: str | None,
    ip_address: str,
    user_agent: str,
    login_result: str = "success",
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO admin_login_logs (
                        login_result, admin_role, admin_account_id, admin_login_id, ip_address, user_agent
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    login_result,
                    role,
                    admin_account_id,
                    (admin_login_id or None),
                    ip_address,
                    (user_agent or "")[:300],
                ),
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
        app.logger.exception(
            "Failed to authenticate admin account login_id=%s", normalized_login_id
        )
        return None


def get_admin_account_by_id(account_id: int):
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                        SELECT id, login_id, role, active
                        FROM admin_accounts
                        WHERE id = %s
                        LIMIT 1
                    """,
                    (account_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "login_id": row[1],
                    "role": row[2],
                    "active": bool(row[3]),
                }
    except Exception:
        app.logger.exception("Failed to fetch admin account id=%s", account_id)
        return None


def get_active_admin_count(role: str | None = None) -> int:
    query = "SELECT COUNT(*) FROM admin_accounts WHERE active = TRUE"
    params = ()
    if role is not None:
        query += " AND role = %s"
        params = (role,)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
                return int(row[0] if row else 0)
    except Exception:
        app.logger.exception("Failed to count active admin accounts role=%s", role)
        return 0


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
    if host not in ALLOWED_HOSTS and not any(
        host.endswith(f".{allowed_host}") for allowed_host in ALLOWED_HOSTS
    ):
        abort(400)


def enforce_https():
    if not FORCE_HTTPS:
        return
    host = (request.host.split(":", 1)[0] if request.host else "").lower()
    if is_local_host(host):
        return
    if request.is_secure:
        return
    forwarded_proto = (
        (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    )
    if forwarded_proto == "https":
        return
    secure_url = request.url.replace("http://", "https://", 1)
    return redirect(secure_url, code=301)


def start_admin_session(role: str, admin_account_id: int, admin_login_id: str):
    now = time.time()
    csrf_token = session.get("_csrf_token")
    session.clear()
    session["logged_in"] = True
    session["admin_role"] = role
    session["admin_account_id"] = admin_account_id
    session["admin_login_id"] = admin_login_id
    session["issued_at"] = now
    session["last_activity"] = now
    # Keep the same token across login so already-open tabs keep working.
    session["_csrf_token"] = csrf_token or secrets.token_urlsafe(32)
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
    current_account = get_admin_account_by_id(admin_account_id)
    if not current_account or not current_account["active"] or (
        current_account["role"] != role
    ):
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


def has_active_auth_session(role: str | None = None) -> bool:
    if not session.get("logged_in"):
        return False
    if role is not None and session.get("admin_role") != role:
        return False
    admin_account_id = session.get("admin_account_id")
    if not isinstance(admin_account_id, int) or admin_account_id <= 0:
        return False
    last_activity = session.get("last_activity")
    if not isinstance(last_activity, (int, float)):
        return False
    return time.time() - last_activity <= SESSION_IDLE_TIMEOUT_SECONDS


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
    request_token = request.form.get("_csrf_token") or request.headers.get(
        "X-CSRF-Token"
    )
    if (
        not token
        or not request_token
        or not secrets.compare_digest(token, request_token)
    ):
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


def sanitize_next_path(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value.startswith("/"):
        return None
    if value.startswith("//"):
        return None
    return value


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


@app.after_request
def apply_security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )

    if request.endpoint != "static" and (
        request.path == "/login" or request.path.startswith("/admin")
    ):
        response.headers["Cache-Control"] = "no-store"

    forwarded_proto = (
        (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    )
    if request.is_secure or forwarded_proto == "https":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

    return response


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
        if request.path.startswith("/admin/login-logs") or request.path.startswith(
            "/admin/admin-accounts"
        ):
            if not has_active_auth_session(ROLE_AUDIT_ADMIN):
                return
        elif request.path.startswith("/admin") or request.path == "/logout":
            if not (
                has_active_auth_session(ROLE_ADMIN)
                or has_active_auth_session(ROLE_AUDIT_ADMIN)
            ):
                return
        try:
            validate_csrf()
        except HTTPException as error:
            if error.code != 403:
                raise
            login_redirect = url_for(
                "login",
                next=sanitize_next_path(request.path),
                notice="session_expired",
            )
            return redirect(login_redirect)


LOGIN_MAX_ATTEMPTS = parse_int_env("LOGIN_MAX_ATTEMPTS", 10, 1, 1000)
LOGIN_WINDOW_SECONDS = parse_int_env("LOGIN_WINDOW_SECONDS", 300, 1, 86400)


def is_login_rate_limited(ip: str) -> bool:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                window_start = datetime.now(timezone.utc) - timedelta(
                    seconds=LOGIN_WINDOW_SECONDS
                )
                cur.execute(
                    "SELECT COUNT(*) FROM login_attempt_records WHERE ip_address = %s AND attempted_at > %s",
                    (ip, window_start),
                )
                return cur.fetchone()[0] >= LOGIN_MAX_ATTEMPTS
    except Exception:
        app.logger.exception("Failed to check login rate limit for ip=%s", ip)
        return True


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
        app.logger.exception("Failed to record login failure for ip=%s", ip)


def is_webhook_rate_limited(ip: str) -> bool:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                window_start = datetime.now(timezone.utc) - timedelta(
                    seconds=WEBHOOK_RATE_LIMIT_WINDOW_SECONDS
                )
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
        app.logger.exception("Failed to check webhook rate limit for ip=%s", ip)
        return True


# --- ルーティング ---


@app.route("/")
def index():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    notice = request.args.get("notice")
    next_path = sanitize_next_path(request.args.get("next") or request.form.get("next"))
    ip = request.remote_addr or "unknown"
    if request.method == "POST":
        if is_login_rate_limited(ip):
            abort(429)
        login_id = (request.form.get("login_id") or "").strip().lower()
        password = request.form.get("password")
        account = authenticate_admin_account(login_id, password)
        if account:
            start_admin_session(account["role"], account["id"], account["login_id"])
            record_admin_login(
                account["role"],
                account["id"],
                account["login_id"],
                ip,
                request.headers.get("User-Agent"),
            )
            if account["role"] == ROLE_AUDIT_ADMIN:
                return redirect(next_path or url_for("admin_login_logs_page"))
            return redirect(next_path or url_for("admin_page"))
        else:
            record_admin_login(
                "unknown",
                None,
                None,
                ip,
                request.headers.get("User-Agent"),
                login_result="failure",
            )
            record_login_failure(ip)
            error = "ログインIDまたはパスワードが正しくありません"
    return render_template(
        "login.html",
        error=error,
        notice=notice,
        next_path=next_path,
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
            """)
            cur.execute("""
                UPDATE reservations r
                SET type_id = NULL
                WHERE r.type_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM reservation_types t
                      WHERE t.id = r.type_id
                  )
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'fk_reservations_type_id'
                    ) THEN
                        ALTER TABLE reservations
                        ADD CONSTRAINT fk_reservations_type_id
                        FOREIGN KEY (type_id)
                        REFERENCES reservation_types(id)
                        ON DELETE RESTRICT;
                    END IF;
                END
                $$;
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
            cur.execute("""
                ALTER TABLE reservation_types
                ADD COLUMN IF NOT EXISTS flavor_text TEXT NOT NULL DEFAULT ''
                """)
            cur.execute("""
                ALTER TABLE reservation_types
                ADD COLUMN IF NOT EXISTS image_data BYTEA
            """)
            cur.execute("""
                ALTER TABLE reservation_types
                ADD COLUMN IF NOT EXISTS image_mime_type TEXT NOT NULL DEFAULT ''
            """)
            cur.execute("""
                ALTER TABLE reservation_types
                ADD COLUMN IF NOT EXISTS image_filename TEXT NOT NULL DEFAULT ''
            """)
            cur.execute("""
                ALTER TABLE reservation_types
                ADD COLUMN IF NOT EXISTS image_path TEXT NOT NULL DEFAULT ''
                """)
            cur.execute("""
                ALTER TABLE reservation_types
                ADD COLUMN IF NOT EXISTS price INTEGER NOT NULL DEFAULT 0
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
            sync_reservation_owner_numbers(cur)
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
            cur.execute(
                "SELECT key, value FROM app_settings WHERE key = ANY(%s)", (list(keys),)
            )
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
    set_setting("accepting_new", "true" if flag else "false")


def get_auto_call_count() -> int:
    raw = get_setting("auto_call_count", "0").strip()
    return int(raw) if raw.isdigit() else 0


def set_auto_call_count(count: int):
    set_setting("auto_call_count", str(max(0, count)))


def build_auto_call_summary(values, prefix: str):
    run_at = (values.get(f"{prefix}_auto_call_run_at") or "").strip()
    sent_count = int(
        (values.get(f"{prefix}_auto_call_sent_count") or "0").strip() or "0"
    )
    failed_count = int(
        (values.get(f"{prefix}_auto_call_failed_count") or "0").strip() or "0"
    )
    selected_count = int(
        (values.get(f"{prefix}_auto_call_selected_count") or "0").strip() or "0"
    )
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
            "display_no": fmt_no(row[1]) if isinstance(row[1], int) else row[1],
            "status": row[2],
            "type_id": row[3],
            "type": row[4],
            "created_at": format_dt(row[5]),
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
              AND COALESCE(r.owner_admin_id, t.owner_admin_id) = %s
            GROUP BY t.name
            ORDER BY COUNT(*) DESC, t.name ASC
        """,
        (STATUS_WAITING, STATUS_CALLED, owner_admin_id),
    )
    return cur.fetchall()


def serialize_type_counts(rows):
    return [{"name": row[0], "count": row[1]} for row in rows]


def get_admin_login_log_rows(cur, limit: int = 500):
    cur.execute(
        """
            SELECT id, login_result, admin_role, admin_login_id, ip_address, user_agent, logged_in_at
            FROM admin_login_logs
            ORDER BY logged_in_at DESC, id DESC
            LIMIT %s
        """,
        (limit,),
    )
    rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "login_result": row[1],
            "admin_role": row[2],
            "admin_login_id": row[3],
            "ip_address": row[4],
            "user_agent": row[5],
            "logged_in_at": format_dt(row[6]),
        }
        for row in rows
    ]


def get_active_rows(
    cur, owner_admin_id: int, current_type_id=None, sort_by="id", sort_order="asc"
):
    params = [STATUS_WAITING, STATUS_CALLED]
    where = "WHERE r.status IN (%s, %s) AND COALESCE(r.owner_admin_id, t.owner_admin_id) = %s"
    params.append(owner_admin_id)
    if current_type_id is not None:
        where += " AND r.type_id = %s"
        params.append(current_type_id)
    order_map = {
        "id": "COALESCE(r.reservation_no, r.id)",
        "status": "r.status",
        "type": "t.name",
    }
    order_by = order_map[sort_by]
    cur.execute(
        f"""
            SELECT r.id, COALESCE(r.reservation_no, r.id), r.status, t.id, t.name, r.created_at
            FROM reservations r
            LEFT JOIN reservation_types t ON r.type_id = t.id
            {where}
            ORDER BY {order_by} {sort_order.upper()}, r.id ASC
        """,
        params,
    )
    return cur.fetchall()


def get_accepting_type_names(cur):
    cur.execute(
        "SELECT name FROM reservation_types WHERE accepting = TRUE ORDER BY id ASC"
    )
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
            rows = get_admin_login_log_rows(cur)
            cur.execute("""
                    SELECT id, login_id, role, active, created_at
                    FROM admin_accounts
                    ORDER BY id ASC
                """)
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
        current_admin_account_id=get_current_admin_account_id(),
        admin_refresh_interval_ms=ADMIN_REFRESH_INTERVAL_MS,
        csrf_token=get_csrf_token(),
    )


@app.route("/admin/login-logs/data")
def admin_login_logs_data():
    if not is_audit_admin_authenticated():
        return jsonify({"error": "unauthorized"}), 401
    if not has_audit_admin_account():
        abort(404)

    with get_connection() as conn:
        with conn.cursor() as cur:
            rows = get_admin_login_log_rows(cur)
    return jsonify({"rows": rows})


@app.route("/admin/admin-accounts", methods=["POST"])
def admin_accounts_create():
    if not is_audit_admin_authenticated():
        return redirect(url_for("login"))

    login_id = (request.form.get("login_id") or "").strip().lower()
    password = request.form.get("password") or ""
    bulk_accounts_raw = request.form.get("bulk_accounts") or ""
    bulk_lines = [
        line.strip() for line in bulk_accounts_raw.splitlines() if line.strip()
    ]

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
                            (
                                parsed_login_id,
                                generate_password_hash(parsed_password),
                                ROLE_ADMIN,
                            ),
                        )
                    conn.commit()
        except psycopg2.IntegrityError:
            return redirect(
                url_for(
                    "admin_login_logs_page",
                    account_error="同じログインIDが既に存在します。",
                )
            )

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
        return redirect(
            url_for(
                "admin_login_logs_page",
                account_error="同じログインIDが既に存在します。",
            )
        )

    return redirect(
        url_for(
            "admin_login_logs_page",
            account_success=f"管理者アカウント「{login_id}」を作成しました。",
        )
    )


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
                    return redirect(
                        url_for(
                            "admin_login_logs_page",
                            account_error="対象アカウントが存在しません。",
                        )
                    )
                conn.commit()
    except psycopg2.IntegrityError:
        return redirect(
            url_for(
                "admin_login_logs_page",
                account_error="同じログインIDが既に存在します。",
            )
        )

    return redirect(
        url_for(
            "admin_login_logs_page",
            account_success=f"ログインIDを「{login_id}」に更新しました。",
        )
    )


@app.route("/admin/admin-accounts/<int:account_id>/active", methods=["POST"])
def admin_accounts_toggle_active(account_id):
    if not is_audit_admin_authenticated():
        return redirect(url_for("login"))

    current_admin_account_id = get_current_admin_account_id()
    if current_admin_account_id == account_id:
        return redirect(
            url_for(
                "admin_login_logs_page",
                account_error="自分のアカウントは無効化できません。",
            )
        )

    account = get_admin_account_by_id(account_id)
    if not account:
        return redirect(
            url_for(
                "admin_login_logs_page",
                account_error="対象アカウントが存在しません。",
            )
        )

    if account["active"]:
        if account["role"] == ROLE_AUDIT_ADMIN and get_active_admin_count(ROLE_AUDIT_ADMIN) <= 1:
            return redirect(
                url_for(
                    "admin_login_logs_page",
                    account_error="最後の監査管理者は無効化できません。",
                )
            )
        new_active = False
        success_message = f"アカウント「{account['login_id']}」を無効化しました。"
    else:
        new_active = True
        success_message = f"アカウント「{account['login_id']}」を有効化しました。"

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE admin_accounts SET active = %s WHERE id = %s",
                    (new_active, account_id),
                )
                if cur.rowcount == 0:
                    return redirect(
                        url_for(
                            "admin_login_logs_page",
                            account_error="対象アカウントが存在しません。",
                        )
                    )
                conn.commit()
    except psycopg2.IntegrityError:
        return redirect(
            url_for(
                "admin_login_logs_page",
                account_error="このアカウントは参照中データがあるため更新できません。",
            )
        )

    return redirect(
        url_for("admin_login_logs_page", account_success=success_message)
    )


@app.route("/admin/admin-accounts/<int:account_id>/delete", methods=["POST"])
def admin_accounts_delete(account_id):
    if not is_audit_admin_authenticated():
        return redirect(url_for("login"))

    current_admin_account_id = get_current_admin_account_id()
    if current_admin_account_id == account_id:
        return redirect(
            url_for(
                "admin_login_logs_page",
                account_error="自分のアカウントは削除できません。",
            )
        )

    account = get_admin_account_by_id(account_id)
    if not account:
        return redirect(
            url_for(
                "admin_login_logs_page",
                account_error="対象アカウントが存在しません。",
            )
        )
    if account["role"] == ROLE_AUDIT_ADMIN and get_active_admin_count(ROLE_AUDIT_ADMIN) <= 1:
        return redirect(
            url_for(
                "admin_login_logs_page",
                account_error="最後の監査管理者は削除できません。",
            )
        )

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM admin_accounts WHERE id = %s", (account_id,))
                if cur.rowcount == 0:
                    return redirect(
                        url_for(
                            "admin_login_logs_page",
                            account_error="対象アカウントが存在しません。",
                        )
                    )
                conn.commit()
    except psycopg2.IntegrityError:
        return redirect(
            url_for(
                "admin_login_logs_page",
                account_error="このアカウントは参照中データがあるため削除できません。",
            )
        )

    return redirect(
        url_for(
            "admin_login_logs_page",
            account_success=f"アカウント「{account['login_id']}」を削除しました。",
        )
    )


@app.route("/admin")
def admin_page():
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    type_error = request.args.get("type_error")
    call_error = request.args.get("call_error")
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
            type_counts = serialize_type_counts(
                fetch_type_counts(cur, current_admin_account_id)
            )
    return render_template(
        "admin.html",
        rows=active_rows,
        types=types,
        type_error=type_error,
        call_error=call_error,
        current_type_id=current_type_id,
        type_counts=type_counts,
        sort_by=sort_by,
        sort_order=sort_order,
        accepting_new=runtime_settings["accepting_new"],
        auto_call_count=runtime_settings["auto_call_count"],
        last_auto_call=runtime_settings["last_auto_call"],
        latest_auto_call=runtime_settings["latest_auto_call"],
        admin_refresh_interval_ms=ADMIN_REFRESH_INTERVAL_MS,
        csrf_token=get_csrf_token(),
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
            type_counts = serialize_type_counts(
                fetch_type_counts(cur, current_admin_account_id)
            )
    runtime_settings = get_runtime_settings()
    return jsonify(
        {
            "rows": serialize_active_rows(rows),
            "meta": {
                "accepting_new": runtime_settings["accepting_new"],
                "auto_call_count": runtime_settings["auto_call_count"],
                "last_auto_call": runtime_settings["last_auto_call"],
                "latest_auto_call": runtime_settings["latest_auto_call"],
                "type_counts": type_counts,
            },
        }
    )


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
    return jsonify({"counts": serialize_type_counts(counts)})


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
    if request.method == "POST":
        name = normalize_type_name(request.form.get("name"))
        flavor_text = (request.form.get("flavor_text") or "").strip()
        price_raw = (request.form.get("price") or "0").strip()
        image_file = request.files.get("image")
        if image_file and getattr(image_file, "filename", "").strip():
            suffix = Path(secure_filename(image_file.filename)).suffix.lower()
            if suffix not in ALLOWED_TYPE_IMAGE_EXTENSIONS:
                return redirect(
                    url_for(
                        "admin_types_page",
                        type_error="画像は jpg, jpeg, png, gif, webp のみアップロードできます。",
                    )
                )
        if not validate_type_name(name):
            return redirect(
                url_for(
                    "admin_types_page",
                    type_error=f"種類名は1〜{MAX_TYPE_NAME_LENGTH}文字、英数字/日本語/スペース/記号(-_・)のみ使用できます。",
                )
            )
        if not price_raw.isdigit():
            return redirect(
                url_for(
                    "admin_types_page",
                    type_error="価格には正の整数を入力してください。",
                )
            )
        price = int(price_raw)
        if price < 0:
            return redirect(
                url_for(
                    "admin_types_page",
                    type_error="価格には0以上の値を入力してください。",
                )
            )
        try:
            image_data = None
            image_mime_type = ""
            image_filename = ""
            if image_file and getattr(image_file, "filename", "").strip():
                image_data, image_mime_type, image_filename = save_type_image_upload(
                    image_file
                )
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                            INSERT INTO reservation_types
                                (name, flavor_text, owner_admin_id, image_data, image_mime_type, image_filename, price)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                        """,
                        (
                            name,
                            flavor_text,
                            current_admin_account_id,
                            image_data or None,
                            image_mime_type,
                            image_filename,
                            price,
                        ),
                    )
                    conn.commit()
            return redirect(
                url_for("admin_types_page", type_success="種類を追加しました。")
            )
        except ValueError as error:
            return redirect(url_for("admin_types_page", type_error=str(error)))
        except psycopg2.IntegrityError:
            if image_file and getattr(image_file, "filename", "").strip():
                # INSERT が失敗した場合に保存済みファイルが残らないようにする
                # save_type_image_upload は INSERT 後に呼ぶため通常は発生しないが、念のため吸収する。
                pass
            return redirect(
                url_for(
                    "admin_types_page", type_error="同じ名前の種類が既に存在します。"
                )
            )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                    SELECT id, name, accepting, flavor_text, image_mime_type, image_filename, price, owner_admin_id
                    FROM reservation_types
                    WHERE owner_admin_id = %s
                    ORDER BY id ASC
                """,
                (current_admin_account_id,),
            )
            types = cur.fetchall()
            type_owner_login_ids = {}
            owner_admin_ids = {
                row[7] for row in types if len(row) > 7 and row[7] is not None
            }
            if owner_admin_ids:
                cur.execute(
                    "SELECT id, login_id FROM admin_accounts WHERE id = ANY(%s)",
                    (list(owner_admin_ids),),
                )
                type_owner_login_ids = {row[0]: row[1] for row in cur.fetchall()}
    return render_template(
        "types.html",
        types=types,
        type_owner_login_ids=type_owner_login_ids,
        accepting_new=accepting_new,
        type_error=type_error,
        type_success=type_success,
        schedule_error=schedule_error,
        schedule_success=schedule_success,
        csrf_token=get_csrf_token(),
    )


@app.route("/admin/types/<int:type_id>/image", methods=["POST"])
def admin_types_update_image(type_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    image_file = request.files.get("image")
    if not image_file or not getattr(image_file, "filename", "").strip():
        return redirect(url_for("admin_types_page", type_error="画像ファイルを選択してください。"))

    try:
        image_data, image_mime_type, image_filename = save_type_image_upload(image_file)
    except ValueError as error:
        return redirect(url_for("admin_types_page", type_error=str(error)))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT image_data, image_mime_type, image_filename FROM reservation_types WHERE id = %s AND owner_admin_id = %s",
                (type_id, current_admin_account_id),
            )
            row = cur.fetchone()
            if not row:
                abort(403)
            cur.execute(
                """
                    UPDATE reservation_types
                    SET image_data = %s,
                        image_mime_type = %s,
                        image_filename = %s
                    WHERE id = %s AND owner_admin_id = %s
                """,
                (
                    image_data or None,
                    image_mime_type,
                    image_filename,
                    type_id,
                    current_admin_account_id,
                ),
            )
            if cur.rowcount == 0:
                abort(403)
            conn.commit()
    return redirect(url_for("admin_types_page", type_success="画像を更新しました。"))


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
                "SELECT image_data, image_mime_type, image_filename FROM reservation_types WHERE id = %s AND owner_admin_id = %s",
                (type_id, current_admin_account_id),
            )
            row = cur.fetchone()
            try:
                cur.execute(
                    "DELETE FROM reservation_types WHERE id = %s AND owner_admin_id = %s",
                    (type_id, current_admin_account_id),
                )
                if cur.rowcount == 0:
                    abort(403)
                conn.commit()
            except psycopg2.IntegrityError:
                conn.rollback()
                return redirect(
                    url_for(
                        "admin_types_page",
                        type_error="この種類に紐づく予約があるため削除できません。",
                    )
                )
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

@app.route("/admin/types/<int:type_id>/flavor", methods=["POST"])
def admin_types_update_flavor(type_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    flavor_text = (request.form.get("flavor_text") or "").strip()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reservation_types SET flavor_text = %s WHERE id = %s AND owner_admin_id = %s",
                (flavor_text, type_id, current_admin_account_id),
            )
            if cur.rowcount == 0:
                abort(403)
            conn.commit()
    return redirect(url_for("admin_types_page", type_success="説明を更新しました。"))


@app.route("/admin/types/<int:type_id>/name", methods=["POST"])
def admin_types_update_name(type_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

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
                    "UPDATE reservation_types SET name = %s WHERE id = %s AND owner_admin_id = %s",
                    (name, type_id, current_admin_account_id),
                )
                if cur.rowcount == 0:
                    abort(403)
                conn.commit()
    except psycopg2.IntegrityError:
        return redirect(
            url_for("admin_types_page", type_error="同じ名前の種類が既に存在します。")
        )
    return redirect(url_for("admin_types_page", type_success="種類名を更新しました。"))


@app.route("/admin/types/<int:type_id>/price", methods=["POST"])
def admin_types_update_price(type_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    price_raw = (request.form.get("price") or "0").strip()
    if not price_raw.isdigit():
        return redirect(url_for("admin_types_page", type_error="価格には正の整数を入力してください。"))
    price = int(price_raw)
    if price < 0:
        return redirect(url_for("admin_types_page", type_error="価格には0以上の値を入力してください。"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reservation_types SET price = %s WHERE id = %s AND owner_admin_id = %s",
                (price, type_id, current_admin_account_id),
            )
            if cur.rowcount == 0:
                abort(403)
            conn.commit()
    return redirect(url_for("admin_types_page", type_success="価格を更新しました。"))


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
            if sort_by not in (
                "id",
                "status",
                "type",
                "created_at",
                "service_duration",
            ):
                sort_by = "id"
            if sort_order not in ("asc", "desc"):
                sort_order = "desc"
            params = [STATUS_DONE, STATUS_CANCELLED, current_admin_account_id]
            where = "WHERE r.status IN (%s, %s) AND COALESCE(r.owner_admin_id, t.owner_admin_id) = %s"
            if current_type_id is not None:
                where += " AND r.type_id = %s"
                params.append(current_type_id)
            order_map = {
                "id": "COALESCE(r.reservation_no, r.id)",
                "status": "r.status",
                "type": "t.name",
                "created_at": "r.created_at",
                "service_duration": "(EXTRACT(EPOCH FROM (r.completed_at - r.called_at)))",
            }
            order_by = order_map[sort_by]
            cur.execute(
                f"""
                SELECT
                    r.id,
                    COALESCE(r.reservation_no, r.id) AS display_no,
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
            """,
                params + [history_page_size + 1, offset],
            )
            rows = cur.fetchall()
            # 時刻をフォーマット済み文字列に変換（日本時間対応）
            rows = [
                (
                    row[0],
                    fmt_no(row[1]) if isinstance(row[1], int) else row[1],
                    row[2],
                    row[3],
                    row[4],
                    format_dt(row[5]),
                    format_dt(row[6]),
                    format_dt(row[7]),
                    row[8],
                )
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
        csrf_token=get_csrf_token(),
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
    where = "WHERE r.status IN (%s, %s) AND COALESCE(r.owner_admin_id, t.owner_admin_id) = %s"
    if current_type_id is not None:
        where += " AND r.type_id = %s"
        params.append(current_type_id)
    order_map = {
        "id": "COALESCE(r.reservation_no, r.id)",
        "status": "r.status",
        "type": "t.name",
        "created_at": "r.created_at",
        "service_duration": "(EXTRACT(EPOCH FROM (r.completed_at - r.called_at)))",
    }

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "番号",
                "種類",
                "状態",
                "受付時刻",
                "呼出時刻",
                "完了時刻",
                "受付から呼出",
                "受付から完了",
                "呼出から完了",
            ]
        )
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        connection = create_connection()
        try:
            cursor_name = f"history_export_{uuid.uuid4().hex}"
            with connection.cursor(name=cursor_name) as cur:
                cur.itersize = 500
                cur.execute(
                    f"""
                        SELECT
                            r.id,
                            COALESCE(r.reservation_no, r.id) AS display_no,
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
                    writer.writerow(
                        [
                            fmt_no(row[1]) if isinstance(row[1], int) else row[1],
                            row[2],
                            row[3],
                            format_dt(row[4]),
                            format_dt(row[5]),
                            format_dt(row[6]),
                            format_duration_from_seconds(row[7]) or "-",
                            format_duration_from_seconds(row[8]) or "-",
                            format_duration_from_seconds(row[9]) or "-",
                        ]
                    )
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
                    UPDATE reservations r
                    SET status = %s, called_at = CURRENT_TIMESTAMP
                    FROM reservation_types t
                    WHERE r.id = %s
                      AND r.status = %s
                      AND r.type_id = t.id
                      AND COALESCE(r.owner_admin_id, t.owner_admin_id) = %s
                    RETURNING user_id, COALESCE(reservation_no, r.id)
                """,
                (STATUS_CALLED, res_id, STATUS_WAITING, current_admin_account_id),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "SELECT status, COALESCE(reservation_no, id) FROM reservations WHERE id = %s",
                    (res_id,),
                )
                existing = cur.fetchone()
                if existing and existing[0] == STATUS_CANCELLED:
                    conn.commit()
                    return redirect(
                        url_for(
                            "admin_page",
                            call_error=f"受付番号 {fmt_no(existing[1] or res_id)} は直前にキャンセルされたため呼出できませんでした。",
                        )
                    )
                abort(404)
            user_id = row[0]
            display_no = row[1] or res_id
            conn.commit()

    try:
        send_push_message(user_id, build_call_message(display_no))
    except Exception:
        app.logger.exception(
            "Failed to send LINE push message for reservation %s user_id=%s",
            res_id,
            user_id,
        )
        with get_connection() as rollback_conn:
            with rollback_conn.cursor() as rollback_cur:
                rollback_cur.execute(
                    "UPDATE reservations SET status = %s, called_at = NULL WHERE id = %s AND status = %s",
                    (STATUS_WAITING, res_id, STATUS_CALLED),
                )
                rollback_conn.commit()
        return redirect(
            url_for(
                "admin_page",
                call_error="呼出メッセージの送信に失敗しました。状態は待機中に戻しました。",
            )
        )
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
                    UPDATE reservations r
                    SET status = %s, completed_at = CURRENT_TIMESTAMP
                    FROM reservation_types t
                    WHERE r.id = %s
                      AND r.status = %s
                      AND r.type_id = t.id
                      AND COALESCE(r.owner_admin_id, t.owner_admin_id) = %s
                    RETURNING r.id
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


# --- バックアップ・リストア ---

BACKUP_TABLES = [
    "reservation_types",
    "admin_accounts",
    "reservations",
    "app_settings",
    "admin_login_logs",
    "login_attempt_records",
    "webhook_request_records",
]


def _serialize_value(val):
    """DB から取得した値を JSON シリアライズ可能な形式に変換する。
    bytes は base64 文字列に、datetime/date は ISO 形式文字列に変換する。
    """
    if isinstance(val, (bytes, memoryview)):
        raw = bytes(val) if isinstance(val, memoryview) else val
        return {"__type__": "bytes", "data": base64.b64encode(raw).decode("ascii")}
    if isinstance(val, datetime):
        return {"__type__": "datetime", "data": val.isoformat()}
    # date のみの場合（datetime のサブクラスでないもの）
    try:
        from datetime import date as _date
        if type(val) is _date:
            return {"__type__": "date", "data": val.isoformat()}
    except ImportError:
        pass
    return val


def _deserialize_value(val):
    """JSON からロードした値をDB挿入用の Python オブジェクトに戻す。"""
    if not isinstance(val, dict):
        return val
    type_tag = val.get("__type__")
    if type_tag == "bytes":
        return base64.b64decode(val["data"])
    if type_tag == "datetime":
        return datetime.fromisoformat(val["data"])
    if type_tag == "date":
        from datetime import date as _date
        return _date.fromisoformat(val["data"])
    return val


def _export_table(table_name: str):
    """指定テーブルの全行を [{col: val, ...}, ...] で返す。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table_name} ORDER BY id ASC")
            cols = [desc[0] for desc in cur.description]
            rows = []
            for row in cur.fetchall():
                rows.append(
                    {col: _serialize_value(val) for col, val in zip(cols, row)}
                )
    return {"columns": cols, "rows": rows}


def _import_table(cur, table_name: str, table_data: dict):
    """テーブルをトランケートしてバックアップデータを挿入する。"""
    if not isinstance(table_data, dict):
        raise ValueError(f"Invalid backup data for table {table_name}")
    columns = table_data.get("columns", [])
    rows = table_data.get("rows", [])
    # columns はアップロードされた JSON 由来のため、SQL 識別子として安全な
    # 文字のみで構成されていることを検証する（インジェクション対策）。
    for c in columns:
        if not isinstance(c, str) or not re.fullmatch(r"[A-Za-z0-9_]+", c):
            raise ValueError(f"Invalid column name in backup data: {c!r}")
    # 外部キー制約の順序を考慮して CASCADE TRUNCATE を使用する
    cur.execute(f"TRUNCATE TABLE {table_name} RESTART IDENTITY CASCADE")
    if not columns or not rows:
        return
    col_list = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    insert_sql = (
        f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})"
        f" ON CONFLICT DO NOTHING"
    )
    for row_dict in rows:
        values = tuple(
            _deserialize_value(row_dict.get(col)) for col in columns
        )
        cur.execute(insert_sql, values)


def _reset_sequence(cur, table_name: str):
    """テーブルの SERIAL シーケンスを最大 id 値にリセットする。"""
    cur.execute(
        f"""
            SELECT setval(
                pg_get_serial_sequence('{table_name}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table_name}), 0) + 1,
                false
            )
        """
    )


@app.route("/admin/backup")
def admin_backup_page():
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    import_error = request.args.get("import_error")
    import_success = request.args.get("import_success")
    return render_template(
        "backup.html",
        import_error=import_error,
        import_success=import_success,
        csrf_token=get_csrf_token(),
    )


@app.route("/admin/backup/export")
def admin_backup_export():
    if not is_admin_authenticated():
        return jsonify({"error": "unauthorized"}), 401

    export_data = {
        "version": APP_VERSION,
        "exported_at": datetime.now(JST).isoformat(),
        "tables": {},
    }
    for table_name in BACKUP_TABLES:
        try:
            export_data["tables"][table_name] = _export_table(table_name)
        except Exception:
            app.logger.exception("Failed to export table %s", table_name)
            export_data["tables"][table_name] = {"columns": [], "rows": []}

    filename = f"backup-{datetime.now(JST).strftime('%Y%m%d-%H%M%S')}.json"
    json_bytes = json.dumps(export_data, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        json_bytes,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/admin/backup/import", methods=["POST"])
def admin_backup_import():
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    uploaded_file = request.files.get("backup_file")
    if not uploaded_file or not getattr(uploaded_file, "filename", "").strip():
        return redirect(
            url_for("admin_backup_page", import_error="バックアップファイルを選択してください。")
        )

    filename = uploaded_file.filename or ""
    if not filename.lower().endswith(".json"):
        return redirect(
            url_for("admin_backup_page", import_error="JSONファイル (.json) を選択してください。")
        )

    try:
        raw = uploaded_file.read()
        backup_data = json.loads(raw.decode("utf-8"))
    except Exception:
        return redirect(
            url_for("admin_backup_page", import_error="ファイルの読み込みに失敗しました。有効なJSONファイルを選択してください。")
        )

    if not isinstance(backup_data, dict) or "tables" not in backup_data:
        return redirect(
            url_for("admin_backup_page", import_error="バックアップの形式が無効です。")
        )

    tables = backup_data["tables"]
    if not isinstance(tables, dict):
        return redirect(
            url_for("admin_backup_page", import_error="バックアップの形式が無効です。")
        )

    # インポート順序: 外部キーの親テーブルから先に復元する
    import_order = [
        "admin_accounts",
        "reservation_types",
        "reservations",
        "app_settings",
        "admin_login_logs",
        "login_attempt_records",
        "webhook_request_records",
    ]

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for table_name in import_order:
                    if table_name in tables:
                        _import_table(cur, table_name, tables[table_name])
                # シーケンスをリセット（id を持つテーブル）
                for table_name in import_order:
                    if table_name in tables:
                        try:
                            _reset_sequence(cur, table_name)
                        except Exception:
                            # シーケンスが存在しないテーブル（app_settings など）は無視
                            app.logger.debug(
                                "Sequence reset skipped for table %s", table_name
                            )
            conn.commit()
    except Exception:
        app.logger.exception("Failed to import backup")
        return redirect(
            url_for("admin_backup_page", import_error="インポートに失敗しました。バックアップファイルを確認してください。")
        )

    # スキーマキャッシュをリセットしてログアウト
    global SCHEMA_READY
    SCHEMA_READY = False
    session.clear()

    return redirect(
        url_for(
            "login",
            notice="backup_restored",
        )
    )


# --- LINE Webhook ---
@app.route("/callback", methods=["POST"])
def callback():
    ip = request.remote_addr or "unknown"
    if is_webhook_rate_limited(ip):
        abort(429)
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        abort(400)
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception:
        # ハンドラー内の一時的な失敗で5xxを返すとLINEが再送し、二重処理につながるため200で吸収する。
        app.logger.exception(
            "Failed to process LINE webhook event ip=%s signature=%s body_len=%s",
            ip,
            (signature or "")[:64],
            len(body) if body is not None else 0,
        )
        return "OK"
    return "OK"

IGNORED_REPLY_MESSAGE = "https://ukweb.ikura.workers.dev/"


def should_ignore_reply_message(message: str) -> bool:
    normalized = message.strip()
    return normalized in {IGNORED_REPLY_MESSAGE, "使い方"}


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text.strip()
    if should_ignore_reply_message(user_message):
        return
    user_id = event.source.user_id
    process_reservation(event, user_id, user_message)


def process_reservation(event, user_id, user_message):
    normalized = user_message.strip()
    if not normalized:
        send_flex_notice(
            event.reply_token,
            "ご案内",
            "メッセージを受け付けました。予約は「予約」、キャンセルは「キャンセル」、待ち時間は「待ち時間」と送信してください。",
        )
        return
    if len(normalized) > MAX_USER_MESSAGE_CHARS:
        send_flex_notice(
            event.reply_token,
            "エラー",
            f"メッセージは{MAX_USER_MESSAGE_CHARS}文字以内で送信してください。",
        )
        return

    accepting_new = is_accepting_new()
    with get_connection() as conn:
        with conn.cursor() as cur:
            if normalized.startswith("予約"):
                if not accepting_new:
                    send_flex_notice(
                        event.reply_token,
                        "予約停止中",
                        "現在、新規の予約受付は停止中です。",
                    )
                    return
                requested_type_name = normalize_type_name(normalized[2:])
                type_id = None
                type_name = None
                if requested_type_name:
                    if not validate_type_name(requested_type_name):
                        send_flex_notice(
                            event.reply_token,
                            "種類名エラー",
                            f"種類名は1〜{MAX_TYPE_NAME_LENGTH}文字で指定してください。\n例: 予約 相談",
                        )
                        return
                    cur.execute(
                        """
                            SELECT id, name, accepting, owner_admin_id, flavor_text, image_mime_type, price
                            FROM reservation_types
                            WHERE name = %s
                        """,
                        (requested_type_name,),
                    )
                    type_row = cur.fetchone()
                    if not type_row:
                        names = get_accepting_type_names(cur)
                        if names:
                            body = (
                                f"指定した種類「{requested_type_name}」は存在しません。\n利用可能: "
                                + " / ".join(names)
                            )
                        else:
                            body = "予約の種類がまだ登録されていません。管理画面で追加してください。"
                        send_flex_notice(event.reply_token, "種類がありません", body)
                        return
                    type_id = type_row[0]
                    type_name = type_row[1]
                    type_accepting = type_row[2]
                    type_owner_admin_id = type_row[3]
                    type_flavor_text = type_row[4]
                    type_image_mime_type = type_row[5]
                    type_price = type_row[6] if len(type_row) > 6 else 0
                    type_owner_login_id = None
                    if type_owner_admin_id is not None:
                        cur.execute(
                            "SELECT login_id FROM admin_accounts WHERE id = %s",
                            (type_owner_admin_id,),
                        )
                        owner_row = cur.fetchone()
                        if owner_row:
                            type_owner_login_id = owner_row[0]
                    type_image_url = build_type_image_url(type_id)
                    if not type_accepting:
                        names = get_accepting_type_names(cur)
                        if names:
                            body = (
                                f"「{type_name}」の新規受付は停止中です。\n利用可能: "
                                + " / ".join(names)
                            )
                        else:
                            body = f"「{type_name}」の新規受付は停止中です。"
                        send_flex_notice(event.reply_token, "受付停止", body)
                        return
                    if type_owner_admin_id is None:
                        send_flex_notice(
                            event.reply_token,
                            "受付不可",
                            "この種類は管理者に割り当てられていないため予約できません。管理者へお問い合わせください。",
                        )
                        return
                else:
                    cur.execute(
                        """
                            SELECT id, name, flavor_text, accepting, image_mime_type, price, owner_admin_id
                            FROM reservation_types
                            ORDER BY id ASC
                        """
                    )
                    type_rows = cur.fetchall()
                    owner_admin_ids = {
                        row[6] for row in type_rows if len(row) > 6 and row[6] is not None
                    }
                    owner_login_ids = {}
                    if owner_admin_ids:
                        cur.execute(
                            "SELECT id, login_id FROM admin_accounts WHERE id = ANY(%s)",
                            (list(owner_admin_ids),),
                        )
                        owner_login_ids = {row[0]: row[1] for row in cur.fetchall()}
                    if not type_rows:
                        send_flex_notice(
                            event.reply_token,
                            "種類がありません",
                            "現在、予約可能な種類が登録されていません。",
                        )
                        return

                    carousel_bubbles = []
                    for type_row in type_rows[:10]:
                        type_id = type_row[0]
                        name = type_row[1]
                        flavor_text = type_row[2]
                        accepting = type_row[3]
                        image_mime_type = type_row[4]
                        price = type_row[5] if len(type_row) > 5 else 0
                        owner_admin_id = type_row[6] if len(type_row) > 6 else None
                        owner_login_id = owner_login_ids.get(owner_admin_id)
                        image_url = build_type_image_url(type_id) if image_mime_type else None
                        # header box
                        header = {
                            "type": "box",
                            "layout": "vertical",
                            "backgroundColor": "#1e293b",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": name,
                                    "weight": "bold",
                                    "size": "xl",
                                    "color": "#ffffff",
                                    "wrap": True,
                                }
                            ],
                            "paddingAll": "20px",
                        }
                        
                        # status pill
                        status_color = "#10b981" if accepting else "#ef4444"
                        status_bg = "#d1fae5" if accepting else "#fee2e2"
                        status_text = "受付中" if accepting else "受付停止中"
                        
                        body_contents = [
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {
                                        "type": "box",
                                        "layout": "vertical",
                                        "backgroundColor": status_bg,
                                        "cornerRadius": "md",
                                        "paddingStart": "8px",
                                        "paddingEnd": "8px",
                                        "paddingTop": "2px",
                                        "paddingBottom": "2px",
                                        "contents": [
                                            {
                                                "type": "text",
                                                "text": status_text,
                                                "color": status_color,
                                                "size": "xs",
                                                "weight": "bold",
                                                "align": "center",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                        
                        body_contents.append(
                            {
                                "type": "text",
                                "text": f"設定者: {owner_login_id}" if owner_login_id else "設定者: 不明",
                                "wrap": True,
                                "size": "xs",
                                "color": "#64748b",
                                "margin": "sm",
                            }
                        )
                        body_contents.append(
                            {
                                "type": "text",
                                "text": flavor_text if flavor_text else "説明はありません。",
                                "wrap": True,
                                "size": "sm",
                                "color": "#475569" if flavor_text else "#94a3b8",
                                "style": "normal" if flavor_text else "italic",
                                "margin": "lg",
                            }
                        )
                        if price:
                            body_contents.append(
                                {
                                    "type": "box",
                                    "layout": "horizontal",
                                    "margin": "md",
                                    "contents": [
                                        {
                                            "type": "text",
                                            "text": "価格",
                                            "size": "sm",
                                            "color": "#64748b",
                                            "flex": 1,
                                        },
                                        {
                                            "type": "text",
                                            "text": f"{price:,}円",
                                            "size": "sm",
                                            "color": "#0f172a",
                                            "weight": "bold",
                                            "align": "end",
                                            "flex": 2,
                                        },
                                    ],
                                }
                            )
                        
                        body = {
                            "type": "box",
                            "layout": "vertical",
                            "contents": body_contents,
                            "paddingAll": "20px",
                        }
                        
                        # footer
                        if accepting:
                            footer = {
                                "type": "box",
                                "layout": "vertical",
                                "contents": [
                                    {
                                        "type": "button",
                                        "action": {
                                            "type": "message",
                                            "label": "この種類で予約する",
                                            "text": f"予約 {name}",
                                        },
                                        "style": "primary",
                                        "color": "#0284c7",
                                    }
                                ],
                                "paddingAll": "10px",
                            }
                        else:
                            footer = {
                                "type": "box",
                                "layout": "vertical",
                                "contents": [
                                    {
                                        "type": "box",
                                        "layout": "vertical",
                                        "backgroundColor": "#f1f5f9",
                                        "cornerRadius": "md",
                                        "paddingTop": "10px",
                                        "paddingBottom": "10px",
                                        "contents": [
                                            {
                                                "type": "text",
                                                "text": "現在受付停止中",
                                                "color": "#94a3b8",
                                                "align": "center",
                                                "weight": "bold",
                                                "size": "sm",
                                            }
                                        ],
                                    }
                                ],
                                "paddingAll": "10px",
                            }
                        
                        bubble = {
                            "type": "bubble",
                            "size": "mega",
                            "header": header,
                            "body": body,
                            "footer": footer,
                        }
                        if image_url:
                            bubble["hero"] = {
                                "type": "image",
                                "url": image_url,
                                "size": "full",
                                "aspectRatio": "16:9",
                                "aspectMode": "cover",
                            }
                        carousel_bubbles.append(bubble)
                    
                    flex_msg = {
                        "type": "flex",
                        "altText": "予約の種類一覧",
                        "contents": {
                            "type": "carousel",
                            "contents": carousel_bubbles,
                        },
                    }
                    send_reply_message(event.reply_token, flex_msg)
                    return

                cur.execute(
                    """
                        SELECT r.id, COALESCE(r.reservation_no, r.id), r.status, r.type_id, t.name, COALESCE(r.owner_admin_id, t.owner_admin_id)
                        FROM reservations r
                        LEFT JOIN reservation_types t ON r.type_id = t.id
                        WHERE r.user_id = %s AND r.status IN (%s, %s)
                        ORDER BY r.id DESC LIMIT 1
                    """,
                    (user_id, STATUS_WAITING, STATUS_CALLED),
                )
                existing = cur.fetchone()
                if existing:
                    (
                        res_id,
                        display_no,
                        status,
                        existing_type_id,
                        existing_type_name,
                        existing_owner_admin_id,
                    ) = existing
                    if status == STATUS_WAITING:
                        if existing_owner_admin_id is not None:
                            waiting_people_ahead = count_waiting_people_ahead_by_owner(
                                cur,
                                reservation_id=res_id,
                                owner_admin_id=existing_owner_admin_id,
                            )
                            body = f"予約済みです。番号: {fmt_no(display_no)} / 種類: {existing_type_name} / 待ち: {waiting_people_ahead}人"
                        else:
                            cur.execute(
                                "SELECT COUNT(*) FROM reservations WHERE status = %s AND id < %s",
                                (STATUS_WAITING, res_id),
                            )
                            body = f"予約済みです。番号: {fmt_no(display_no)} / 待ち: {cur.fetchone()[0]}人"
                    elif status == STATUS_CALLED:
                        if existing_type_name:
                            body = f"【呼出中】番号: {fmt_no(display_no)} / 種類: {existing_type_name} 会場へお越しください！"
                        else:
                            body = f"【呼出中】番号: {fmt_no(display_no)} 会場へお越しください！"
                    send_flex_notice(event.reply_token, "予約状況", body)
                    return
                else:
                    try:
                        reservation_no = allocate_admin_reservation_no(
                            cur, type_owner_admin_id
                        )
                        cur.execute(
                            """
                                INSERT INTO reservations (
                                    user_id, message, type_id, owner_admin_id, reservation_no
                                ) VALUES (%s, %s, %s, %s, %s)
                                RETURNING id
                            """,
                            (user_id, "", type_id, type_owner_admin_id, reservation_no),
                        )
                        new_id = cur.fetchone()[0]
                    except psycopg2.IntegrityError:
                        conn.rollback()
                        cur.execute(
                            """
                                SELECT r.id, COALESCE(r.reservation_no, r.id), r.status, r.type_id, t.name, COALESCE(r.owner_admin_id, t.owner_admin_id)
                                FROM reservations r
                                LEFT JOIN reservation_types t ON r.type_id = t.id
                                WHERE r.user_id = %s AND r.status IN (%s, %s)
                                ORDER BY r.id DESC LIMIT 1
                            """,
                            (user_id, STATUS_WAITING, STATUS_CALLED),
                        )
                        existing_after_conflict = cur.fetchone()
                        if existing_after_conflict:
                            (
                                res_id,
                                display_no,
                                status,
                                _existing_type_id,
                                existing_type_name,
                                existing_owner_admin_id,
                            ) = existing_after_conflict
                            if status == STATUS_WAITING:
                                if existing_owner_admin_id is not None:
                                    waiting_people_ahead = (
                                        count_waiting_people_ahead_by_owner(
                                            cur,
                                            reservation_id=res_id,
                                            owner_admin_id=existing_owner_admin_id,
                                        )
                                    )
                                    body = f"予約済みです。番号: {fmt_no(display_no)} / 種類: {existing_type_name} / 待ち: {waiting_people_ahead}人"
                                else:
                                    cur.execute(
                                        "SELECT COUNT(*) FROM reservations WHERE status = %s AND id < %s",
                                        (STATUS_WAITING, res_id),
                                    )
                                    body = f"予約済みです。番号: {fmt_no(display_no)} / 待ち: {cur.fetchone()[0]}人"
                            elif status == STATUS_CALLED:
                                if existing_type_name:
                                    body = f"【呼出中】番号: {fmt_no(display_no)} / 種類: {existing_type_name} 会場へお越しください！"
                                else:
                                    body = f"【呼出中】番号: {fmt_no(display_no)} 会場へお越しください！"
                            send_flex_notice(event.reply_token, "予約状況", body)
                            return
                        raise
                    conn.commit()
                    app.logger.info(
                        "Created reservation %s by user %s type_id=%s",
                        new_id,
                        user_id,
                        type_id,
                    )
                    if type_owner_admin_id:
                        waiting_people_ahead = count_waiting_people_ahead_by_owner(
                            cur,
                            reservation_id=new_id,
                            owner_admin_id=type_owner_admin_id,
                        )
                        price_text = f" / 価格: {type_price:,}円" if type_price else ""
                        owner_text = (
                            f" / 設定者: {type_owner_login_id}"
                            if type_owner_login_id
                            else ""
                        )
                        body = f"【受付完了】番号: {fmt_no(reservation_no)} / 種類: {type_name}{owner_text}{price_text} / 待ち: {waiting_people_ahead}人"
                    else:
                        cur.execute(
                            "SELECT COUNT(*) FROM reservations WHERE status = %s AND id < %s",
                            (STATUS_WAITING, new_id),
                        )
                        waiting_people_ahead = int(cur.fetchone()[0] or 0)
                        body = f"【受付完了】番号: {fmt_no(reservation_no)} / 待ち: {waiting_people_ahead}人"
                    refresh_wait_time_estimate(owner_admin_id=type_owner_admin_id)
                    estimated_minutes = calculate_wait_time_minutes(
                        waiting_people_ahead
                    )
                    body += f"\n現在の目安待ち時間: {estimated_minutes}分"
                    send_flex_notice(
                        event.reply_token,
                        "受付完了",
                        body,
                        hero_url=type_image_url,
                    )
                    return
            elif normalized == "キャンセル":
                cur.execute(
                    """
                        UPDATE reservations SET status = %s
                        WHERE id = (
                            SELECT id FROM reservations
                            WHERE user_id = %s AND status IN (%s, %s)
                            ORDER BY id DESC LIMIT 1
                        )
                        RETURNING id, COALESCE(reservation_no, id)
                    """,
                    (STATUS_CANCELLED, user_id, STATUS_WAITING, STATUS_CALLED),
                )
                cancelled = cur.fetchone()
                if cancelled:
                    conn.commit()
                    cancelled_no = cancelled[1] if len(cancelled) > 1 else cancelled[0]
                    send_flex_notice(
                        event.reply_token,
                        "キャンセル完了",
                        f"受付番号 {fmt_no(cancelled_no)} をキャンセルしました。",
                    )
                else:
                    send_flex_notice(
                        event.reply_token,
                        "キャンセル",
                        "キャンセル対象の予約はありません。",
                    )
                return
            elif normalized == "待ち時間":
                cur.execute(
                    """
                        SELECT r.id, COALESCE(r.reservation_no, r.id), r.status, t.name, COALESCE(r.owner_admin_id, t.owner_admin_id)
                        FROM reservations r
                        LEFT JOIN reservation_types t ON r.type_id = t.id
                        WHERE r.user_id = %s AND r.status IN (%s, %s)
                        ORDER BY r.id DESC LIMIT 1
                    """,
                    (user_id, STATUS_WAITING, STATUS_CALLED),
                )
                existing = cur.fetchone()
                if not existing:
                    send_flex_notice(
                        event.reply_token,
                        "待ち時間",
                        "待ち時間を確認できる予約がありません。まず「予約 種類名」と送信してください。",
                    )
                else:
                    res_id = existing[0]
                    display_no = existing[1]
                    status = existing[2]
                    type_name = existing[3] if len(existing) > 3 else None
                    owner_admin_id = existing[4] if len(existing) > 4 else None
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
                        estimated_minutes = calculate_wait_time_minutes(
                            waiting_people_ahead
                        )
                        if type_name:
                            body = (
                                f"番号: {fmt_no(display_no)} / 種類: {type_name} / あなたの前: {waiting_people_ahead}人"
                                f"\n現在の目安待ち時間: {estimated_minutes}分"
                            )
                        else:
                            body = (
                                f"番号: {fmt_no(display_no)} / あなたの前: {waiting_people_ahead}人"
                                f"\n現在の目安待ち時間: {estimated_minutes}分"
                            )
                        send_flex_notice(event.reply_token, "待ち時間", body)
                    else:
                        send_flex_notice(
                            event.reply_token,
                            "呼出中",
                            f"【呼出中】番号: {fmt_no(display_no)} です。会場へお越しください。",
                        )
                return
            else:
                send_flex_notice(
                    event.reply_token,
                    "ご案内",
                    "メッセージを受け付けました。予約は「予約」、キャンセルは「キャンセル」、待ち時間は「待ち時間」と送信してください。",
                )


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)