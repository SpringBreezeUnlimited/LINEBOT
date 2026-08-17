"""DB接続管理、スキーマ初期化・マイグレーション、設定テーブル・レートリミットDB操作。"""
import logging
import math
from datetime import datetime, timedelta, timezone

import psycopg2  # type: ignore
from psycopg2.pool import ThreadedConnectionPool  # type: ignore
from flask import g, has_request_context  # type: ignore

from config import (
    DATABASE_URL,
    DB_CONNECT_TIMEOUT,
    ADMIN_PASSWORD_HASH,
    AUDIT_ADMIN_PASSWORD_HASH,
    ROLE_ADMIN,
    ROLE_AUDIT_ADMIN,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_WINDOW_SECONDS,
    WEBHOOK_RATE_LIMIT_COUNT,
    WEBHOOK_RATE_LIMIT_WINDOW_SECONDS,
    STATUS_WAITING,
    WAIT_TIME_SETTING_KEYS,
    AUTO_CALL_SETTING_KEYS,
    RUNTIME_SETTING_KEYS,
)

logger = logging.getLogger("database")

from threading import Lock

SCHEMA_LOCK = Lock()
SCHEMA_READY = False
_CONNECTION_POOL = None


def get_connection_pool():
    global _CONNECTION_POOL
    if _CONNECTION_POOL is None:
        with SCHEMA_LOCK:
            if _CONNECTION_POOL is None:
                connection_kwargs = psycopg2.extensions.parse_dsn(DATABASE_URL)
                connection_kwargs["connect_timeout"] = DB_CONNECT_TIMEOUT
                _CONNECTION_POOL = ThreadedConnectionPool(
                    minconn=5,
                    maxconn=20,
                    **connection_kwargs,
                )
    return _CONNECTION_POOL


def release_connection(connection):
    if connection is None:
        return
    try:
        pool = get_connection_pool()
        if connection.closed:
            return
        pool.putconn(connection)
    except Exception:
        try:
            connection.close()
        except Exception:
            pass


def reset_schema_cache():
    """バックアップ復元後などにスキーマ確認キャッシュをリセットする。"""
    global SCHEMA_READY
    SCHEMA_READY = False


class ManagedConnection:
    def __init__(self, connection, close_on_exit: bool):
        self._connection = connection
        self._close_on_exit = close_on_exit

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __enter__(self):
        return self._connection

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None and not self._connection.closed:
            try:
                self._connection.rollback()
            except Exception:
                try:
                    self._connection.close()
                except Exception:
                    pass
        if self._close_on_exit and not self._connection.closed:
            release_connection(self._connection)
        return False


def create_connection():
    pool = get_connection_pool()
    return pool.getconn()


def get_connection():
    if has_request_context():
        connection = getattr(g, "_db_connection", None)
        if connection is None or connection.closed:
            connection = create_connection()
            g._db_connection = connection
        return ManagedConnection(connection, close_on_exit=False)
    return ManagedConnection(create_connection(), close_on_exit=True)


def ensure_reservations_table():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reservations (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'waiting',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    call_origin TEXT
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
                ADD COLUMN IF NOT EXISTS call_origin TEXT
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
                ADD COLUMN IF NOT EXISTS price INTEGER
            """)
            cur.execute("""
                ALTER TABLE reservation_types
                ALTER COLUMN price DROP NOT NULL
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
                        accepting_new BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            cur.execute("""
                ALTER TABLE admin_accounts
                ADD COLUMN IF NOT EXISTS next_reservation_no INTEGER NOT NULL DEFAULT 1
            """)
            cur.execute("""
                ALTER TABLE admin_accounts
                ADD COLUMN IF NOT EXISTS accepting_new BOOLEAN NOT NULL DEFAULT TRUE
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


def migrate_legacy_queued_calls():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reservations SET status = %s WHERE status = %s",
                (STATUS_WAITING, "queued_call"),
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
        logger.exception("Failed to cleanup rate limit records")


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
        logger.exception("Failed to check login rate limit for ip=%s", ip)
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
        logger.exception("Failed to record login failure for ip=%s", ip)


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
        logger.exception("Failed to check webhook rate limit for ip=%s", ip)
        return True


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


def is_accepting_new(admin_id: int | None = None) -> bool:
    if admin_id is not None:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT accepting_new, active FROM admin_accounts WHERE id = %s", (admin_id,))
                row = cur.fetchone()
                if row is None:
                    return True
                accepting_new, active = row
                return bool(accepting_new) and bool(active)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM admin_accounts WHERE active = TRUE AND accepting_new = TRUE)")
            row = cur.fetchone()
            return row[0] if row is not None else False


def set_accepting_new(flag: bool, admin_id: int | None = None):
    if admin_id is None:
        set_setting("accepting_new", "true" if flag else "false")
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_accounts SET accepting_new = %s WHERE id = %s",
                (flag, admin_id),
            )
            conn.commit()


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


def get_runtime_settings(admin_id: int | None = None):
    values = get_settings(RUNTIME_SETTING_KEYS)
    raw_auto_call_count = (values.get("auto_call_count") or "0").strip()
    auto_call_count = int(raw_auto_call_count) if raw_auto_call_count.isdigit() else 0
    return {
        "accepting_new": is_accepting_new(admin_id),
        "auto_call_count": auto_call_count,
        "last_auto_call": get_last_auto_call_summary(values),
        "latest_auto_call": get_auto_call_summary("last", values),
        "latest_wait_time": get_latest_wait_time_summary(values),
    }


def get_accepting_type_names(cur):
    cur.execute(
        """
            SELECT t.name 
            FROM reservation_types t
            JOIN admin_accounts a ON t.owner_admin_id = a.id
            WHERE t.accepting = TRUE AND a.accepting_new = TRUE
            ORDER BY t.id ASC
        """
    )
    return [row[0] for row in cur.fetchall()]
