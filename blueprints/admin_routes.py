"""管理画面ルート（ダッシュボード、予約種類設定、履歴、アカウント管理、ログイン履歴等）。

注: main_routes.py と同じ理由で、Flask Blueprintではなく素のビュー関数として
定義し、main.py側でapp.add_url_rule()により元のエンドポイント名で登録する。
"""
import csv
import io
import logging
import time
import uuid
from pathlib import Path

import psycopg2  # type: ignore
from flask import (  # type: ignore
    request,
    session,
    redirect,
    url_for,
    render_template,
    jsonify,
    Response,
    abort,
    stream_with_context,
)
from werkzeug.security import generate_password_hash  # type: ignore
from werkzeug.utils import secure_filename  # type: ignore

from config import (
    STATUS_WAITING,
    STATUS_CALLED,
    STATUS_DONE,
    STATUS_CANCELLED,
    CALL_ORIGIN_MANUAL,
    ROLE_ADMIN,
    ROLE_AUDIT_ADMIN,
    LOGIN_ID_PATTERN,
    MAX_TYPE_NAME_LENGTH,
    MAX_TYPE_FLAVOR_TEXT_CHARS,
    MAX_TYPE_PRICE,
    ALLOWED_TYPE_IMAGE_EXTENSIONS,
    ADMIN_REFRESH_INTERVAL_MS,
)
from blueprints.admin_helpers import (
    serialize_active_rows,
    fetch_type_counts,
    serialize_type_counts,
    get_admin_login_log_rows,
    get_active_rows,
)
from database import (
    get_connection,
    create_connection,
    is_accepting_new,
    set_accepting_new,
    set_auto_call_count,
    get_runtime_settings,
    get_accepting_type_names,
)
import auth
from auth import (
    is_admin_authenticated,
    is_audit_admin_authenticated,
    get_current_admin_account_id,
    get_admin_account_by_id,
    get_active_admin_count,
    has_audit_admin_account,
    get_csrf_token,
)
import services.line_service as line_service
from services.line_service import (
    save_type_image_upload,
    send_push_message,
)
import services.queue_service as queue_service
from services.queue_service import (
    fmt_no,
    format_call_origin,
    get_management_no,
    set_management_no,
    build_call_message,
)

from validators import normalize_type_name, validate_type_name, validate_type_flavor_text
from formatting import format_dt, format_duration_from_seconds

logger = logging.getLogger("admin_routes")

def logout():
    session.clear()
    return redirect(url_for("login"))


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
        is_audit_admin=True,
    )


def admin_login_logs_data():
    if not is_audit_admin_authenticated():
        return jsonify({"error": "unauthorized"}), 401
    if not has_audit_admin_account():
        abort(404)

    with get_connection() as conn:
        with conn.cursor() as cur:
            rows = get_admin_login_log_rows(cur)
    return jsonify({"rows": rows})


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
    runtime_settings = get_runtime_settings(current_admin_account_id)
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
        management_no=get_management_no(current_admin_account_id),
        last_auto_call=runtime_settings["last_auto_call"],
        latest_auto_call=runtime_settings["latest_auto_call"],
        admin_refresh_interval_ms=ADMIN_REFRESH_INTERVAL_MS,
        csrf_token=get_csrf_token(),
    )


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
    runtime_settings = get_runtime_settings(current_admin_account_id)
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


def admin_types_page():
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    accepting_new = is_accepting_new(current_admin_account_id)
    type_error = request.args.get("type_error")
    type_success = request.args.get("type_success")
    schedule_error = request.args.get("schedule_error")
    schedule_success = request.args.get("schedule_success")
    if request.method == "POST":
        name = normalize_type_name(request.form.get("name"))
        flavor_text = (request.form.get("flavor_text") or "").strip()
        price_raw = (request.form.get("price") or "").strip()
        image_file = request.files.get("image")
        if not validate_type_flavor_text(flavor_text):
            return redirect(
                url_for(
                    "admin_types_page",
                    type_error=f"説明は{MAX_TYPE_FLAVOR_TEXT_CHARS}文字以内で入力してください。",
                )
            )
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
        if price_raw == "":
            price = None
        else:
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
            if price > MAX_TYPE_PRICE:
                return redirect(
                    url_for(
                        "admin_types_page",
                        type_error=f"価格は{MAX_TYPE_PRICE:,}円以下で入力してください。",
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

def admin_types_update_flavor(type_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    flavor_text = (request.form.get("flavor_text") or "").strip()
    if not validate_type_flavor_text(flavor_text):
        return redirect(
            url_for(
                "admin_types_page",
                type_error=f"説明は{MAX_TYPE_FLAVOR_TEXT_CHARS}文字以内で入力してください。",
            )
        )
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


def admin_types_update_price(type_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if not current_admin_account_id:
        session.clear()
        return redirect(url_for("login"))

    price_raw = (request.form.get("price") or "").strip()
    if price_raw == "":
        price = None
    else:
        if not price_raw.isdigit():
            return redirect(url_for("admin_types_page", type_error="価格には正の整数を入力してください。"))
        price = int(price_raw)
        if price < 0:
            return redirect(url_for("admin_types_page", type_error="価格には0以上の値を入力してください。"))
        if price > MAX_TYPE_PRICE:
            return redirect(
                url_for(
                    "admin_types_page",
                    type_error=f"価格は{MAX_TYPE_PRICE:,}円以下で入力してください。",
                )
            )

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
            where = "WHERE r.status IN (%s, %s) AND (COALESCE(r.owner_admin_id, t.owner_admin_id) = %s OR COALESCE(r.owner_admin_id, t.owner_admin_id) IS NULL)"
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
                    r.call_origin,
                    r.called_at,
                    r.completed_at,
                    CASE
                        WHEN r.status = %s THEN EXTRACT(EPOCH FROM (r.completed_at - r.created_at))
                        ELSE EXTRACT(EPOCH FROM (r.completed_at - r.called_at))
                    END AS service_duration_seconds
                FROM reservations r
                LEFT JOIN reservation_types t ON r.type_id = t.id
                {where}
                ORDER BY {order_by} {sort_order.upper()}, r.id DESC
                LIMIT %s OFFSET %s
            """,
                [STATUS_CANCELLED] + params + [history_page_size + 1, offset],
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
                    row[6],
                    format_dt(row[7]),
                    format_dt(row[8]),
                    row[9],
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
    where = "WHERE r.status IN (%s, %s) AND (COALESCE(r.owner_admin_id, t.owner_admin_id) = %s OR COALESCE(r.owner_admin_id, t.owner_admin_id) IS NULL)"
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
                "呼出方法",
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
                            r.call_origin,
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
                            format_call_origin(row[4]),
                            format_dt(row[5]),
                            format_dt(row[6]),
                            format_dt(row[7]),
                            format_duration_from_seconds(row[8]) or "-",
                            format_duration_from_seconds(row[9]) or "-",
                            format_duration_from_seconds(row[10]) or "-",
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
                                        SET status = %s, called_at = CURRENT_TIMESTAMP, call_origin = %s
                    FROM reservation_types t
                    WHERE r.id = %s
                      AND r.status = %s
                      AND r.type_id = t.id
                      AND COALESCE(r.owner_admin_id, t.owner_admin_id) = %s
                    RETURNING user_id, COALESCE(reservation_no, r.id)
                """,
                (STATUS_CALLED, CALL_ORIGIN_MANUAL, res_id, STATUS_WAITING, current_admin_account_id),
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
        logger.exception(
            "Failed to send LINE push message for reservation %s user_id=%s",
            res_id,
            user_id,
        )
        with get_connection() as rollback_conn:
            with rollback_conn.cursor() as rollback_cur:
                rollback_cur.execute(
                    "UPDATE reservations SET status = %s, called_at = NULL, call_origin = NULL WHERE id = %s AND status = %s",
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
                    SET status = %s, completed_at = CURRENT_TIMESTAMP, owner_admin_id = COALESCE(r.owner_admin_id, %s)
                    FROM reservation_types t
                    WHERE r.id = %s
                      AND r.status = %s
                      AND r.type_id = t.id
                      AND COALESCE(r.owner_admin_id, t.owner_admin_id) = %s
                    RETURNING r.id
                """,
                (STATUS_DONE, current_admin_account_id, res_id, STATUS_CALLED, current_admin_account_id),
            )
            if not cur.fetchone():
                abort(404)
            conn.commit()
    return redirect(url_for("admin_page"))


def admin_cancel(res_id):
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
                    SET status = %s, completed_at = CURRENT_TIMESTAMP, owner_admin_id = COALESCE(r.owner_admin_id, %s)
                    FROM reservation_types t
                    WHERE r.id = %s
                      AND r.status IN (%s, %s)
                      AND r.type_id = t.id
                      AND COALESCE(r.owner_admin_id, t.owner_admin_id) = %s
                    RETURNING r.id
                """,
                (STATUS_CANCELLED, current_admin_account_id, res_id, STATUS_WAITING, STATUS_CALLED, current_admin_account_id),
            )
            if not cur.fetchone():
                abort(404)
            conn.commit()
    return redirect(url_for("admin_page"))


def admin_toggle_accepting():
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    current_admin_account_id = get_current_admin_account_id()
    if current_admin_account_id:
        set_accepting_new(not is_accepting_new(current_admin_account_id), current_admin_account_id)
    return redirect(url_for("admin_page"))


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


def admin_management_no():
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    raw_value = (request.form.get("management_no") or "").strip()
    if raw_value.isdigit():
        val = int(raw_value) % 10
        current_admin_account_id = get_current_admin_account_id()
        set_management_no(val, current_admin_account_id)
    return redirect(url_for("admin_page"))
