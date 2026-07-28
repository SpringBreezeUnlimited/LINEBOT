"""管理者認証・パスワード検証、セッション状態判定、CSRF保護、セキュリティ・ホスト検証。"""
import logging
import secrets
import time

from flask import request, session, redirect, abort  # type: ignore
from flask.sessions import SecureCookieSessionInterface  # type: ignore
from werkzeug.security import check_password_hash  # type: ignore

from config import (
    ADMIN_PASSWORD_HASH,
    AUDIT_ADMIN_PASSWORD_HASH,
    ROLE_ADMIN,
    ROLE_AUDIT_ADMIN,
    ALLOWED_HOSTS,
    FORCE_HTTPS,
    SESSION_IDLE_TIMEOUT_SECONDS,
    BATCH_CALL_RUNNER_TOKEN,
)
from database import get_connection

logger = logging.getLogger("auth")


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
        logger.exception(
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
        logger.exception("Failed to fetch admin account id=%s", account_id)
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
        logger.exception("Failed to count active admin accounts role=%s", role)
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
