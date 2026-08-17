"""バックアップのエクスポート・インポート（監査アカウント専用）。

注: main_routes.py / admin_routes.py と同じ理由で、Flask Blueprintではなく
素のビュー関数として定義し、main.py側でapp.add_url_rule()により
元のエンドポイント名で登録する。
"""
import base64
import json
import logging
import re
from datetime import datetime

from flask import request, session, redirect, url_for, render_template, jsonify, Response, abort  # type: ignore

from config import JST, APP_VERSION, ROLE_ADMIN
from database import get_connection
import database
import auth
from auth import (
    is_admin_authenticated,
    is_audit_admin_authenticated,
    get_admin_account_by_id,
    get_csrf_token,
)

logger = logging.getLogger("backup_routes")


BACKUP_TABLES = [
    "reservation_types",
    "admin_accounts",
    "reservations",
    "app_settings",
    "admin_login_logs",
    "login_attempt_records",
    "webhook_request_records",
]
_VALID_BACKUP_TABLES = frozenset(BACKUP_TABLES)


def validate_table_name(table_name: str) -> str:
    """バックアップ対象テーブル名をホワイトリストで検証する。"""
    if not isinstance(table_name, str):
        raise ValueError(f"Invalid table_name: {table_name!r}")
    candidate = table_name.strip()
    if not candidate or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", candidate):
        raise ValueError(f"Invalid table_name: {table_name!r}")
    if candidate not in _VALID_BACKUP_TABLES:
        raise ValueError(f"Unsupported table_name: {candidate!r}")
    return candidate


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
    safe_table_name = validate_table_name(table_name)
    with get_connection() as conn:
        with conn.cursor() as cur:
            # カラム名を調べるために LIMIT 0 でクエリ
            cur.execute(f"SELECT * FROM {safe_table_name} LIMIT 0")
            cols = [desc[0] for desc in cur.description]
            if "id" in cols:
                cur.execute(f"SELECT * FROM {safe_table_name} ORDER BY id ASC")
            elif "key" in cols:
                cur.execute(f"SELECT * FROM {safe_table_name} ORDER BY key ASC")
            else:
                cur.execute(f"SELECT * FROM {safe_table_name}")

            rows = []
            for row in cur.fetchall():
                rows.append(
                    {col: _serialize_value(val) for col, val in zip(cols, row)}
                )
    return {"columns": cols, "rows": rows}


def _import_table(cur, table_name: str, table_data: dict):
    """テーブルをトランケートしてバックアップデータを挿入する。"""
    safe_table_name = validate_table_name(table_name)
    if not isinstance(table_data, dict):
        raise ValueError(f"Invalid backup data for table {safe_table_name}")
    columns = table_data.get("columns", [])
    rows = table_data.get("rows", [])
    # columns はアップロードされた JSON 由来のため、SQL 識別子として安全な
    # 文字のみで構成されていることを検証する（インジェクション対策）。
    for c in columns:
        if not isinstance(c, str) or not re.fullmatch(r"[A-Za-z0-9_]+", c):
            raise ValueError(f"Invalid column name in backup data: {c!r}")
    # 外部キー制約の順序を考慮して CASCADE TRUNCATE を使用する
    cur.execute(f"TRUNCATE TABLE {safe_table_name} RESTART IDENTITY CASCADE")
    if not columns or not rows:
        return
    col_list = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    insert_sql = (
        f"INSERT INTO {safe_table_name} ({col_list}) VALUES ({placeholders})"
        f" ON CONFLICT DO NOTHING"
    )
    for row_dict in rows:
        values = tuple(
            _deserialize_value(row_dict.get(col)) for col in columns
        )
        cur.execute(insert_sql, values)


def _reset_sequence(cur, table_name: str):
    """テーブルの SERIAL シーケンスを最大 id 値にリセットする。"""
    safe_table_name = validate_table_name(table_name)
    cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (safe_table_name, "id"))
    seq = cur.fetchone()[0]
    if seq:
        seq_name = str(seq).strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?", seq_name):
            safe_seq_name = seq_name.replace("\"", "\"\"")
            safe_table_name_quoted = safe_table_name.replace('"', '""')
            cur.execute(
                "SELECT setval('"
                + safe_seq_name
                + "', COALESCE((SELECT MAX(id) FROM \""
                + safe_table_name_quoted
                + "\"), 0) + 1, false)"
            )



def _export_account_tables(owner_admin_id: int) -> dict:
    """指定アカウントに紐づく reservation_types・reservations のみをエクスポートする。"""
    result = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table_name, filter_col in (
                ("reservation_types", "owner_admin_id"),
                ("reservations", "owner_admin_id"),
            ):
                cur.execute(
                    f"SELECT * FROM {table_name} WHERE {filter_col} = %s ORDER BY id ASC",
                    (owner_admin_id,),
                )
                if cur.description:
                    cols = [desc[0] for desc in cur.description]
                    rows = [
                        {col: _serialize_value(val) for col, val in zip(cols, row)}
                        for row in cur.fetchall()
                    ]
                    result[table_name] = {"columns": cols, "rows": rows}
                else:
                    result[table_name] = {"columns": [], "rows": []}
    return result


def _import_account_tables(cur, owner_admin_id: int, tables: dict):
    """アカウントに紐づく reservation_types・reservations のみを部分的に復元する。
    他のアカウントのデータや admin_accounts などのシステムテーブルは変更しない。
    バックアップの ID が他アカウントと競合する場合は ValueError を送出する。
    """
    types_data = tables.get("reservation_types", {}) if isinstance(tables, dict) else {}
    res_data = tables.get("reservations", {}) if isinstance(tables, dict) else {}

    types_columns = types_data.get("columns", []) if isinstance(types_data, dict) else []
    types_rows = types_data.get("rows", []) if isinstance(types_data, dict) else []
    res_columns = res_data.get("columns", []) if isinstance(res_data, dict) else []
    res_rows = res_data.get("rows", []) if isinstance(res_data, dict) else []

    # カラム名インジェクション対策
    for c in types_columns + res_columns:
        if not isinstance(c, str) or not re.fullmatch(r"[A-Za-z0-9_]+", c):
            raise ValueError(f"バックアップデータのカラム名が無効です: {c!r}")

    # バックアップの ID が他のアカウントのレコードと競合しないか確認
    type_ids = [
        int(row["id"]) for row in types_rows
        if "id" in row and str(row["id"]).lstrip("-").isdigit()
    ]
    if type_ids:
        cur.execute(
            "SELECT id FROM reservation_types WHERE id = ANY(%s) AND owner_admin_id != %s",
            (type_ids, owner_admin_id),
        )
        conflicts = [r[0] for r in cur.fetchall()]
        if conflicts:
            raise ValueError(
                f"バックアップのID（{conflicts}）が他のアカウントの種類データと競合しています。"
                "このバックアップファイルはこのアカウントでは復元できません。"
            )

    res_ids = [
        int(row["id"]) for row in res_rows
        if "id" in row and str(row["id"]).lstrip("-").isdigit()
    ]
    if res_ids:
        cur.execute(
            "SELECT id FROM reservations WHERE id = ANY(%s) AND owner_admin_id != %s",
            (res_ids, owner_admin_id),
        )
        conflicts = [r[0] for r in cur.fetchall()]
        if conflicts:
            raise ValueError(
                f"バックアップのID（{conflicts}）が他のアカウントの予約データと競合しています。"
                "このバックアップファイルはこのアカウントでは復元できません。"
            )

    # 外部キー参照順に自分のレコードのみ削除
    cur.execute("DELETE FROM reservations WHERE owner_admin_id = %s", (owner_admin_id,))
    cur.execute("DELETE FROM reservation_types WHERE owner_admin_id = %s", (owner_admin_id,))

    # reservation_types を挿入（owner_admin_id を強制上書きして安全性を確保）
    if types_columns and types_rows:
        col_list = ", ".join(f'"{c}"' for c in types_columns)
        placeholders = ", ".join(["%s"] * len(types_columns))
        insert_sql = (
            f"INSERT INTO reservation_types ({col_list}) VALUES ({placeholders})"
            " ON CONFLICT DO NOTHING"
        )
        for row_dict in types_rows:
            values = [_deserialize_value(row_dict.get(col)) for col in types_columns]
            if "owner_admin_id" in types_columns:
                values[types_columns.index("owner_admin_id")] = owner_admin_id
            cur.execute(insert_sql, tuple(values))

    # reservations を挿入（owner_admin_id を強制上書き）
    if res_columns and res_rows:
        col_list = ", ".join(f'"{c}"' for c in res_columns)
        placeholders = ", ".join(["%s"] * len(res_columns))
        insert_sql = (
            f"INSERT INTO reservations ({col_list}) VALUES ({placeholders})"
            " ON CONFLICT DO NOTHING"
        )
        for row_dict in res_rows:
            values = [_deserialize_value(row_dict.get(col)) for col in res_columns]
            if "owner_admin_id" in res_columns:
                values[res_columns.index("owner_admin_id")] = owner_admin_id
            cur.execute(insert_sql, tuple(values))

    # 両テーブルのシーケンスをリセット
    for tbl in ("reservation_types", "reservations"):
        try:
            _reset_sequence(cur, tbl)
        except Exception:
            logger.debug("Sequence reset skipped for table %s", tbl)


def admin_backup_page():
    # バックアップ機能は監査アカウント専用
    if not is_audit_admin_authenticated():
        if is_admin_authenticated():
            # 通常アカウントはバックアップページへのアクセス不可
            abort(403)
        return redirect(url_for("login"))
    import_error = request.args.get("import_error")
    import_success = request.args.get("import_success")
    # 監査アカウント向けにアカウント一覧を取得
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, login_id, role, active
                FROM admin_accounts
                WHERE role = %s
                ORDER BY id ASC
            """, (ROLE_ADMIN,))
            admin_accounts = [
                {"id": row[0], "login_id": row[1], "role": row[2], "active": bool(row[3])}
                for row in cur.fetchall()
            ]
    return render_template(
        "backup.html",
        import_error=import_error,
        import_success=import_success,
        csrf_token=get_csrf_token(),
        is_audit_admin=True,
        admin_accounts=admin_accounts,
    )


def admin_backup_export():
    # バックアップエクスポートは監査アカウント専用
    if not is_audit_admin_authenticated():
        return jsonify({"error": "unauthorized"}), 401

    now = datetime.now(JST)
    now_str = now.strftime("%Y-%m-%d_%H%M")
    raw_login_id = session.get("admin_login_id", "") or ""
    safe_login_id = re.sub(r"[^\w\-]", "_", raw_login_id, flags=re.ASCII)

    # 監査アカウント: DB 全体をエクスポート
    export_data = {
        "version": APP_VERSION,
        "exported_at": now.isoformat(),
        "scope": "full",
        "tables": {},
    }
    for table_name in BACKUP_TABLES:
        try:
            export_data["tables"][table_name] = _export_table(table_name)
        except Exception:
            logger.exception("Failed to export table %s", table_name)
            export_data["tables"][table_name] = {"columns": [], "rows": []}
    filename = f"backup_full_{safe_login_id}_{now_str}.json"

    json_bytes = json.dumps(export_data, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        json_bytes,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def admin_backup_export_account(account_id):
    """監査アカウントが特定アカウントのデータのみをエクスポートする。"""
    if not is_audit_admin_authenticated():
        return jsonify({"error": "unauthorized"}), 401

    # 対象アカウントの存在確認
    target_account = get_admin_account_by_id(account_id)
    if not target_account:
        return jsonify({"error": "account not found"}), 404

    now = datetime.now(JST)
    now_str = now.strftime("%Y-%m-%d_%H%M")
    safe_login_id = re.sub(r"[^\w\-]", "_", target_account["login_id"], flags=re.ASCII)

    try:
        account_tables = _export_account_tables(account_id)
    except Exception:
        logger.exception(
            "Failed to export account tables account_id=%s", account_id
        )
        return jsonify({"error": "export failed"}), 500

    export_data = {
        "version": APP_VERSION,
        "exported_at": now.isoformat(),
        "scope": "account",
        "owner_admin_id": account_id,
        "owner_login_id": target_account["login_id"],
        "tables": account_tables,
    }
    filename = f"backup_account_{safe_login_id}_{now_str}.json"

    json_bytes = json.dumps(export_data, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        json_bytes,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def admin_backup_import():
    # バックアップインポートは監査アカウント専用
    if not is_audit_admin_authenticated():
        if is_admin_authenticated():
            abort(403)
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

    # 監査アカウント: DB 全体を復元
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
                            logger.debug(
                                "Sequence reset skipped for table %s", table_name
                            )
            conn.commit()
    except Exception:
        logger.exception("Failed to import backup")
        return redirect(
            url_for("admin_backup_page", import_error="インポートに失敗しました。バックアップファイルを確認してください。")
        )

    # スキーマキャッシュをリセットしてログアウト
    database.reset_schema_cache()
    session.clear()
    return redirect(url_for("login", notice="backup_restored"))


def admin_backup_import_account(account_id):
    """監査アカウントが特定アカウントのデータのみを部分復元する。"""
    if not is_audit_admin_authenticated():
        if is_admin_authenticated():
            abort(403)
        return redirect(url_for("login"))

    # 対象アカウントの存在確認
    target_account = get_admin_account_by_id(account_id)
    if not target_account:
        return redirect(
            url_for("admin_backup_page", import_error="指定されたアカウントが見つかりません。")
        )

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

    backup_scope = backup_data.get("scope")
    if backup_scope == "full":
        return redirect(
            url_for(
                "admin_backup_page",
                import_error="このバックアップはDB全体のバックアップです。アカウント別復元には使用できません。全体復元を使用してください。",
            )
        )
    if backup_scope == "account":
        backup_owner_id = backup_data.get("owner_admin_id")
        if backup_owner_id is not None and int(backup_owner_id) != account_id:
            backup_login_id = backup_data.get("owner_login_id", "不明")
            return redirect(
                url_for(
                    "admin_backup_page",
                    import_error=(
                        f"このバックアップはアカウント「{backup_login_id}」のものです。"
                        f"アカウント「{target_account['login_id']}」への復元には使用できません。"
                    ),
                )
            )
    else:
        # scope なし = 旧形式の全体バックアップ
        return redirect(
            url_for(
                "admin_backup_page",
                import_error=(
                    "このバックアップは旧形式の全体バックアップです。"
                    "アカウント別復元には使用できません。"
                ),
            )
        )

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _import_account_tables(cur, account_id, tables)
            conn.commit()
    except ValueError as error:
        return redirect(
            url_for("admin_backup_page", import_error=str(error))
        )
    except Exception:
        logger.exception(
            "Failed to import account backup account_id=%s", account_id
        )
        return redirect(
            url_for("admin_backup_page", import_error="インポートに失敗しました。バックアップファイルを確認してください。")
        )

    return redirect(
        url_for(
            "admin_backup_page",
            import_success=f"アカウント「{target_account['login_id']}」のバックアップを復元しました。",
        )
    )
