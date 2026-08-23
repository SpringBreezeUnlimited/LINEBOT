"""待ち時間・呼出人数の計算、呼出・タイムアウト処理、予約番号採番。"""
import logging
import math
import secrets
import time
from datetime import datetime, timedelta

from flex_templates import auto_cancel_notification, call_notification

from config import (
    JST,
    STATUS_WAITING,
    STATUS_CALLED,
    STATUS_CANCELLED,
    CALL_ORIGIN_AUTO,
    CALL_ORIGIN_LABELS,
    CALL_TIMEOUT_MINUTES,
)
from database import (
    get_connection,
    get_setting,
    set_setting,
    set_settings,
    cleanup_rate_limit_records,
    ensure_database_schema,
    get_runtime_settings,
)
from services.line_service import send_push_message

logger = logging.getLogger("queue_service")


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


def build_call_message(reservation_no: int, called_at=None, shop_name: str = "admin") -> dict:
    called_dt = datetime.now(JST) if called_at is None else called_at.astimezone(JST)
    timeout_at = called_dt + timedelta(minutes=CALL_TIMEOUT_MINUTES)
    timeout_label = timeout_at.strftime("%H:%M")
    return call_notification(
        reservation_no, timeout_label, CALL_TIMEOUT_MINUTES, shop_name=shop_name
    )


def hour_digit(now=None) -> int:
    """現在時刻（JST）から Y 桁を決定する。
    10時:1 / 11時:2 / 12時:3 / 13時:4 / 14時:5 / それ以外:0
    """
    dt = now or datetime.now(JST)
    return {10: 1, 11: 2, 12: 3, 13: 4, 14: 5}.get(dt.hour, 0)


def get_management_no(owner_admin_id: int | None = None) -> int:
    if owner_admin_id is not None:
        val = get_setting(f"management_no_{owner_admin_id}", "")
        if val.isdigit():
            return int(val) % 10
    val = get_setting("management_no", "0")
    return int(val) % 10 if val.isdigit() else 0


def set_management_no(val: int, owner_admin_id: int | None = None):
    digit = val % 10
    if owner_admin_id is not None:
        set_setting(f"management_no_{owner_admin_id}", str(digit))
    set_setting("management_no", str(digit))


def _try_allocate_with_seq(cur, owner_admin_id: int, seq: int, z_digit: int, a_digit: int) -> tuple[int | None, int]:
    """Try to allocate a reservation number with a given sequence.
    Returns (allocated_no, next_seq) or (None, seq) if all 10 candidates collide.
    """
    all_y = list(range(10))
    secrets.SystemRandom().shuffle(all_y)
    
    for y_digit in all_y:
        candidate = seq * 1000 + y_digit * 100 + z_digit * 10 + a_digit
        try:
            cur.execute(
                """
                    INSERT INTO reservations (user_id, type_id, owner_admin_id, reservation_no, message, status)
                    VALUES (EXCLUDED_PLACEHOLDER, NULL, %s, %s, '', %s)
                    ON CONFLICT DO NOTHING
                """,
                (owner_admin_id, candidate, STATUS_WAITING),
            )
            if cur.rowcount > 0:
                next_seq = seq + 1 if seq < 999 else 1
                return (candidate, next_seq)
        except Exception:
            pass
    
    return (None, seq)


def allocate_admin_reservation_no(cur, owner_admin_id: int) -> int:
    """申込順の連番 XXX（1〜999）、暗号学的なランダム数字 Y（0〜9）、
    管理者番号 Z（(owner_admin_id - 1) % 10）、管理番号 A（0〜9）からなる
    6 桁固定の整数 XXZYZA を採番して返す。
    表示には fmt_no() を使用する。
    
    改善: unique index の衝突を活用して、SELECT の繰り返しを削減。
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
    if seq > 999 or seq < 1:
        seq = 1

    z_digit = (owner_admin_id - 1) % 10
    a_digit = get_management_no(owner_admin_id)

    res_no = None
    for attempt in range(999):
        y_digit = secrets.randbelow(10)
        candidate = seq * 1000 + y_digit * 100 + z_digit * 10 + a_digit
        try:
            cur.execute(
                """
                    SELECT 1 FROM reservations 
                    WHERE owner_admin_id = %s AND reservation_no = %s 
                    LIMIT 1
                """,
                (owner_admin_id, candidate),
            )
            if not cur.fetchone():
                res_no = candidate
                break
        except Exception:
            pass
        
        if res_no is None and attempt % 10 == 9:
            seq = seq + 1 if seq < 999 else 1

    if res_no is None:
        y_digit = secrets.randbelow(10)
        res_no = seq * 1000 + y_digit * 100 + z_digit * 10 + a_digit

    next_seq = seq + 1 if seq < 999 else 1
    cur.execute(
        """
            UPDATE admin_accounts
            SET next_reservation_no = %s
            WHERE id = %s
        """,
        (next_seq, owner_admin_id),
    )
    return res_no


def fmt_no(reservation_no: int | str) -> str:
    """予約番号を 6 桁 0 埋め文字列（XXXYZA 形式）に変換する。
    int はそのままゼロ埋め。str はいったん int 変換を試み、
    失敗した場合（None 由来の空文字など）はそのまま返す。
    """
    if isinstance(reservation_no, int):
        return f"{reservation_no:06d}"
    try:
        return f"{int(reservation_no):06d}"
    except (ValueError, TypeError):
        return str(reservation_no)


def format_call_origin(call_origin: str | None) -> str:
    label = CALL_ORIGIN_LABELS.get((call_origin or "").strip())
    return label or "不明"


def expire_called_reservations() -> int:
    if CALL_TIMEOUT_MINUTES <= 0:
        return 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                        UPDATE reservations
                        SET status = %s, completed_at = CURRENT_TIMESTAMP
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
                logger.exception(
                    "Failed to send timeout message for reservation %s user_id=%s",
                    reservation_id,
                    user_id,
                )
        return len(timed_out_rows)
    except Exception:
        logger.exception(
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
                        SET status = %s, completed_at = CURRENT_TIMESTAMP
                        WHERE status IN (%s, %s)
                        RETURNING id
                    """,
                    (STATUS_CANCELLED, STATUS_WAITING, STATUS_CALLED),
                )
                cancelled_rows = cur.fetchall()
                conn.commit()
        return len(cancelled_rows)
    except Exception:
        logger.exception("Failed to cancel active reservations at midnight")
        return 0


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
        logger.exception(
            "Failed to refresh wait time estimate owner_admin_id=%s", owner_admin_id
        )
        return default_result


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
                        SET status = %s, called_at = CURRENT_TIMESTAMP, call_origin = %s
                        WHERE id IN (
                            SELECT id FROM selected_rows
                        )
                          AND status = %s
                        RETURNING id, user_id, COALESCE(reservation_no, id)
                    """,
                    (STATUS_WAITING, auto_call_count, STATUS_CALLED, CALL_ORIGIN_AUTO, STATUS_WAITING),
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
            logger.exception(
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
                        SET status = %s, called_at = NULL, call_origin = NULL
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
