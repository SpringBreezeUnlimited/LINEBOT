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

import psycopg2  # type: ignore
from flask import request, abort, render_template, redirect, url_for, session, jsonify, Response, g, stream_with_context, send_file  # type: ignore
from linebot.v3.exceptions import InvalidSignatureError  # type: ignore
from werkzeug.utils import secure_filename  # type: ignore
from werkzeug.security import generate_password_hash  # type: ignore
from werkzeug.exceptions import HTTPException  # type: ignore
from flex_templates import (
    reservation_confirmation,
    call_notification,
    wait_time_status,
    cancel_notification,
    auto_cancel_notification,
)

from app import create_app, minify_css_files, minify_js_files

app = create_app()


from config import (
    parse_bool_env,
    parse_int_env,
    normalize_db_url,
    parse_allowed_hosts,
    SECRET_KEY,
    ADMIN_PASSWORD_HASH,
    ADMIN_PASSWORD_DEPRECATED_SET,
    AUDIT_ADMIN_PASSWORD_HASH,
    CHANNEL_ACCESS_TOKEN,
    CHANNEL_SECRET,
    LOAD_TEST_MODE,
    LOAD_TEST_TOKEN,
    DATABASE_URL,
    DB_CONNECT_TIMEOUT,
    OWNER_LINE_ID,
    APP_VERSION,
    APP_RELEASED_AT,
    PUBLIC_BASE_URL,
    ALLOWED_TYPE_IMAGE_EXTENSIONS,
    FLEX_SAFE_IMAGE_EXTENSIONS,
    MAX_TYPE_IMAGE_SIZE,
    JPEG_QUALITY,
    FORCE_HTTPS,
    ALLOWED_HOSTS,
    IS_PRODUCTION,
    SESSION_IDLE_TIMEOUT_SECONDS,
    MAX_TYPE_NAME_LENGTH,
    MAX_TYPE_FLAVOR_TEXT_CHARS,
    MAX_TYPE_PRICE,
    MAX_USER_MESSAGE_CHARS,
    TYPE_NAME_PATTERN,
    LOGIN_ID_PATTERN,
    WEBHOOK_RATE_LIMIT_COUNT,
    WEBHOOK_RATE_LIMIT_WINDOW_SECONDS,
    CALL_TIMEOUT_MINUTES,
    ADMIN_REFRESH_INTERVAL_MS,
    BATCH_CALL_RUNNER_TOKEN,
    LINE_PUSH_MAX_RETRIES,
    LINE_PUSH_RETRY_BASE_SECONDS,
    LINE_PUSH_RETRY_MAX_SECONDS,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_WINDOW_SECONDS,
    JST,
    STATUS_WAITING,
    STATUS_CALLED,
    STATUS_DONE,
    STATUS_CANCELLED,
    CALL_ORIGIN_AUTO,
    CALL_ORIGIN_MANUAL,
    CALL_ORIGIN_LABELS,
    AUTO_CALL_SETTING_KEYS,
    WAIT_TIME_SETTING_KEYS,
    ROLE_ADMIN,
    ROLE_AUDIT_ADMIN,
    RUNTIME_SETTING_KEYS,
)

import services.line_service as line_service
from services.line_service import (
    MESSAGING_CONFIGURATION,
    extract_http_status,
    is_retryable_push_error,
    build_flex_component,
    build_flex_message,
    build_line_message,
    strip_flex_hero,
    sanitize_flex_message,
    push_message_with_retry_key,
    send_push_message,
    send_reply_message,
    send_flex_notice,
    build_type_image_url,
    save_type_image_upload,
)
from database import (
    get_latest_wait_time_summary,
    is_accepting_new,
    set_accepting_new,
    get_auto_call_count,
    set_auto_call_count,
    build_auto_call_summary,
    get_auto_call_summary,
    get_last_auto_call_summary,
    get_runtime_settings,
    get_accepting_type_names,
)
import services.queue_service as queue_service
from services.queue_service import (
    calculate_wait_time_minutes,
    count_waiting_people_ahead_by_owner,
    should_run_call_batch,
    should_run_midnight_cancel,
    build_call_message,
    hour_digit,
    get_management_no,
    set_management_no,
    allocate_admin_reservation_no,
    fmt_no,
    format_call_origin,
    expire_called_reservations,
    cancel_active_reservations_without_notification,
    refresh_wait_time_estimate,
    process_queued_calls,
)

import auth
from auth import (
    AppSessionInterface,
    record_admin_login,
    verify_admin_password,
    verify_audit_admin_password,
    authenticate_admin_account,
    get_admin_account_by_id,
    get_active_admin_count,
    has_audit_admin_account,
    is_local_host,
    enforce_host_allowlist,
    enforce_https,
    start_admin_session,
    is_authenticated_as,
    is_admin_authenticated,
    is_audit_admin_authenticated,
    get_current_admin_account_id,
    has_active_auth_session,
    get_csrf_token,
    validate_csrf,
    validate_batch_runner_token,
    sanitize_next_path,
)

import blueprints.line_routes as line_routes
from blueprints.line_routes import handler, callback, handle_message, process_reservation, should_ignore_reply_message

import database
from database import (
    ManagedConnection,
    create_connection,
    get_connection,
    release_connection,
    ensure_reservations_table,
    sync_reservation_owner_numbers,
    ensure_types_table,
    ensure_admin_accounts_table,
    ensure_admin_login_logs_table,
    ensure_settings_table,
    ensure_rate_limit_tables,
    migrate_legacy_queued_calls,
    ensure_database_schema,
    get_setting,
    set_setting,
    get_settings,
    set_settings,
    cleanup_rate_limit_records,
    is_login_rate_limited,
    record_login_failure,
    is_webhook_rate_limited,
)

import blueprints.main_routes as main_routes
from blueprints.main_routes import reservation_type_image, favicon, index

import blueprints.admin_routes as admin_routes
from blueprints.admin_routes import (
    logout,
    admin_login_logs_page,
    admin_login_logs_data,
    admin_accounts_create,
    admin_accounts_update_login_id,
    admin_accounts_toggle_active,
    admin_accounts_delete,
    admin_page,
    admin_data,
    admin_type_counts,
    admin_types_page,
    admin_types_update_image,
    admin_types_delete,
    admin_types_toggle,
    admin_types_update_flavor,
    admin_types_update_name,
    admin_types_update_price,
    admin_history,
    admin_history_export,
    admin_call,
    admin_finish,
    admin_cancel,
    admin_toggle_accepting,
    admin_auto_call_count,
    admin_management_no,
)

from formatting import format_dt, format_duration_from_seconds


from validators import normalize_type_name, validate_type_name, validate_type_flavor_text



@app.teardown_appcontext
def close_request_connection(_exception=None):
    connection = getattr(g, "_db_connection", None)
    if connection is not None:
        try:
            if not connection.closed:
                release_connection(connection)
        finally:
            g.pop("_db_connection", None)
    else:
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
    if request.path in ("/health", "/healthz", "/readyz", "/loadtest/db"):
        return
    if request.path == "/login" and request.method == "GET":
        # ログイン画面の表示だけはDB初期化なしで通す。POST時や他画面では従来どおり初期化する。
        return
    ensure_database_schema()


@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if request.path in (
            "/callback",
            "/tasks/process-call-queue",
            "/loadtest/db",
        ):
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


@app.route("/health")
@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "version": APP_VERSION}), 200


@app.route("/readyz")
def readyz():
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return jsonify({"status": "ready", "version": APP_VERSION}), 200
    except Exception:
        app.logger.exception("readiness check failed")
        return jsonify({"status": "unready", "version": APP_VERSION}), 503


@app.route("/loadtest/db", methods=["POST"])
def loadtest_db():
    if not LOAD_TEST_MODE:
        abort(404)
    request_token = (request.headers.get("X-Loadtest-Token") or "").strip()
    if not LOAD_TEST_TOKEN or not secrets.compare_digest(
        request_token, LOAD_TEST_TOKEN
    ):
        abort(403)

    load_test_user_id = f"loadtest-{uuid.uuid4()}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO reservations (user_id, message, status)
                    VALUES (%s, %s, 'cancelled')
                    RETURNING id
                    """,
                    (load_test_user_id, "load test"),
                )
                reservation_id = cur.fetchone()[0]
                cur.execute(
                    "SELECT id FROM reservations WHERE id = %s AND user_id = %s",
                    (reservation_id, load_test_user_id),
                )
                if cur.fetchone() is None:
                    raise RuntimeError("load test reservation was not found")
                cur.execute("DELETE FROM reservations WHERE id = %s", (reservation_id,))
            conn.commit()
        return jsonify({"status": "ok", "version": APP_VERSION}), 200
    except Exception:
        app.logger.exception("load test database operation failed")
        return jsonify({"status": "error", "version": APP_VERSION}), 503


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




@app.route("/tasks/process-call-queue", methods=["POST"])
def process_call_queue_task():
    if not BATCH_CALL_RUNNER_TOKEN:
        return jsonify({"error": "batch runner token is not configured"}), 503
    if not validate_batch_runner_token():
        abort(403)
    result = process_queued_calls()
    return jsonify(result)


import blueprints.backup_routes as backup_routes
from blueprints.backup_routes import (
    admin_backup_page,
    admin_backup_export,
    admin_backup_export_account,
    admin_backup_import,
    admin_backup_import_account,
)


# --- バックアップ・リストア ---


# --- LINE Webhook ---


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)