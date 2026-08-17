"""LINE Messaging API メッセージ送信、Flex Message構築、画像処理。"""
import io
import json
import logging
import time
import uuid
from pathlib import Path

from flask import request, has_request_context, url_for  # type: ignore
from PIL import Image, ImageOps, UnidentifiedImageError  # type: ignore
from werkzeug.utils import secure_filename  # type: ignore

from linebot.v3.messaging import (  # type: ignore
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.messaging.models.flex_box import FlexBox  # type: ignore
from linebot.v3.messaging.models.flex_bubble import FlexBubble  # type: ignore
from linebot.v3.messaging.models.flex_carousel import FlexCarousel  # type: ignore
from linebot.v3.messaging.models.flex_image import FlexImage  # type: ignore
from linebot.v3.messaging.models.flex_message import FlexMessage  # type: ignore
from linebot.v3.messaging.models.flex_button import FlexButton  # type: ignore
from linebot.v3.messaging.models.flex_text import FlexText  # type: ignore
from linebot.v3.messaging.exceptions import ApiException  # type: ignore

from flex_templates import bubble_from_title_and_text

from config import (
    LOAD_TEST_MODE,
    CHANNEL_ACCESS_TOKEN,
    LINE_PUSH_MAX_RETRIES,
    LINE_PUSH_RETRY_BASE_SECONDS,
    LINE_PUSH_RETRY_MAX_SECONDS,
    PUBLIC_BASE_URL,
    ALLOWED_TYPE_IMAGE_EXTENSIONS,
    FLEX_SAFE_IMAGE_EXTENSIONS,
    MAX_TYPE_IMAGE_SIZE,
    JPEG_QUALITY,
)

logger = logging.getLogger("line_service")

MESSAGING_CONFIGURATION = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
_MESSAGING_API = None


def get_messaging_api():
    global _MESSAGING_API
    current_factory = MessagingApi
    if _MESSAGING_API is None:
        api_client = ApiClient(MESSAGING_CONFIGURATION)
        _MESSAGING_API = current_factory(api_client)
        return _MESSAGING_API

    if not isinstance(_MESSAGING_API, current_factory):
        api_client = ApiClient(MESSAGING_CONFIGURATION)
        _MESSAGING_API = current_factory(api_client)
    return _MESSAGING_API


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


def push_message_with_retry_key(
    messaging_api: MessagingApi, request_payload: PushMessageRequest, retry_key: str
):
    try:
        return messaging_api.push_message(request_payload, x_line_retry_key=retry_key)
    except TypeError as error:
        message = str(error)
        if "x_line_retry_key" not in message:
            raise
        logger.warning(
            "line-bot-sdk does not support x_line_retry_key argument; fallback without retry key"
        )
        return messaging_api.push_message(request_payload)


def send_push_message(user_id: str, message: str | dict, retry_key: str | None = None):
    if LOAD_TEST_MODE:
        logger.info("LOAD_TEST_MODE: push message skipped user_id=%s", user_id)
        return
    stable_retry_key = retry_key or str(uuid.uuid4())
    payload = PushMessageRequest(
        to=user_id,
        messages=[build_line_message(message)],
    )
    messaging_api = get_messaging_api()
    for attempt in range(1, LINE_PUSH_MAX_RETRIES + 1):
        try:
            push_message_with_retry_key(messaging_api, payload, stable_retry_key)
            return
        except Exception as error:
            status = extract_http_status(error)
            if status == 409:
                # 同じリトライキーで受理済み。重複送信は行われていないので成功扱いにする。
                logger.info(
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
            logger.warning(
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
        logger.info(
            "LOAD_TEST_MODE: reply message skipped reply_token=%s", reply_token
        )
        return
    try:
        if isinstance(message, dict):
            message = sanitize_flex_message(message)
        payload = ReplyMessageRequest(
            reply_token=reply_token, messages=[build_line_message(message)]
        )
        get_messaging_api().reply_message(payload)
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
                    get_messaging_api().reply_message(payload)
                    return
                except Exception:
                    logger.exception(
                        "Fallback reply without hero also failed reply_token=%s",
                        reply_token,
                    )
        logger.exception("Failed to send reply message reply_token=%s", reply_token)
    except Exception:
        logger.exception("Failed to send reply message reply_token=%s", reply_token)


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
