"""予約種類名・説明文の入力バリデーション（main.py・admin_routes.py共用）。"""
from config import MAX_TYPE_NAME_LENGTH, MAX_TYPE_FLAVOR_TEXT_CHARS, TYPE_NAME_PATTERN


def normalize_type_name(value: str) -> str:
    return " ".join((value or "").split())


def validate_type_name(value: str) -> bool:
    if not value or len(value) > MAX_TYPE_NAME_LENGTH:
        return False
    return bool(TYPE_NAME_PATTERN.fullmatch(value))


def validate_type_flavor_text(value: str) -> bool:
    return len((value or "").strip()) <= MAX_TYPE_FLAVOR_TEXT_CHARS
