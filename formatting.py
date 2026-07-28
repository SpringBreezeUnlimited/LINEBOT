"""日時・秒数の表示用フォーマット関数（main.py・admin_routes.py共用）。"""
from datetime import timezone

from config import JST


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
