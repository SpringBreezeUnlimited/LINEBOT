"""blueprints/backup_routes.py のテスト。

これまでほぼ無テストだった領域（Stage 1レビューで指摘）:
- _serialize_value / _deserialize_value のラウンドトリップ
- _import_table / _import_account_tables のカラム名SQLインジェクション対策
- _import_account_tables のアカウント間IDコンフリクト検出（データ分離の要）
- _reset_sequence
- 各ルートの認可ガード（監査アカウント専用チェック）とファイルバリデーション
"""
import base64
import json
from datetime import datetime, date
from io import BytesIO

import pytest


# ---------------------------------------------------------------------------
# _serialize_value / _deserialize_value
# ---------------------------------------------------------------------------


def test_serialize_deserialize_bytes_roundtrip(app_module):
    br = app_module.backup_routes
    original = b"\x00\x01\xffhello"
    serialized = br._serialize_value(original)
    assert serialized == {
        "__type__": "bytes",
        "data": base64.b64encode(original).decode("ascii"),
    }
    restored = br._deserialize_value(serialized)
    assert restored == original


def test_serialize_deserialize_datetime_roundtrip(app_module):
    br = app_module.backup_routes
    original = datetime(2026, 7, 28, 12, 30, 0)
    serialized = br._serialize_value(original)
    assert serialized == {"__type__": "datetime", "data": original.isoformat()}
    restored = br._deserialize_value(serialized)
    assert restored == original


def test_serialize_deserialize_date_roundtrip(app_module):
    br = app_module.backup_routes
    original = date(2026, 7, 28)
    serialized = br._serialize_value(original)
    assert serialized == {"__type__": "date", "data": original.isoformat()}
    restored = br._deserialize_value(serialized)
    assert restored == original


@pytest.mark.parametrize("value", [None, 1, "text", 3.14, True])
def test_serialize_deserialize_plain_values_passthrough(app_module, value):
    br = app_module.backup_routes
    assert br._serialize_value(value) == value
    assert br._deserialize_value(value) == value


def test_deserialize_value_ignores_unknown_type_tag(app_module):
    br = app_module.backup_routes
    # __type__ が未知のタグの場合はそのまま辞書を返す（クラッシュしない）
    payload = {"__type__": "unknown", "data": "x"}
    assert br._deserialize_value(payload) == payload


# ---------------------------------------------------------------------------
# _import_table: カラム名インジェクション対策
# ---------------------------------------------------------------------------


class _RecordingCursor:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))


@pytest.mark.parametrize(
    "malicious_column",
    [
        "id; DROP TABLE reservations;--",
        "id) VALUES (1); DROP TABLE reservations;--",
        "name\"; DELETE FROM admin_accounts;--",
        "1=1",
        "",
        "name; SELECT",
    ],
)
def test_import_table_rejects_sql_injection_in_column_names(app_module, malicious_column):
    br = app_module.backup_routes
    cur = _RecordingCursor()
    table_data = {"columns": [malicious_column], "rows": [{malicious_column: "x"}]}
    with pytest.raises(ValueError):
        br._import_table(cur, "reservation_types", table_data)


def test_import_table_accepts_valid_column_names(app_module):
    br = app_module.backup_routes
    cur = _RecordingCursor()
    table_data = {
        "columns": ["id", "name", "owner_admin_id"],
        "rows": [{"id": 1, "name": "A", "owner_admin_id": 2}],
    }
    br._import_table(cur, "reservation_types", table_data)
    # TRUNCATE + 1 INSERT が発行されているはず
    assert any("TRUNCATE" in q for q, _ in cur.queries)
    assert any("INSERT INTO reservation_types" in q for q, _ in cur.queries)


def test_import_table_raises_on_non_dict_data(app_module):
    br = app_module.backup_routes
    cur = _RecordingCursor()
    with pytest.raises(ValueError):
        br._import_table(cur, "reservation_types", ["not", "a", "dict"])


def test_import_table_truncates_only_when_columns_or_rows_empty(app_module):
    br = app_module.backup_routes
    cur = _RecordingCursor()
    br._import_table(cur, "reservation_types", {"columns": [], "rows": []})
    assert len(cur.queries) == 1
    assert "TRUNCATE" in cur.queries[0][0]


def test_import_table_deserializes_values_before_insert(app_module):
    br = app_module.backup_routes
    cur = _RecordingCursor()
    raw_bytes = b"\x01\x02"
    table_data = {
        "columns": ["id", "image_data"],
        "rows": [
            {
                "id": 1,
                "image_data": {
                    "__type__": "bytes",
                    "data": base64.b64encode(raw_bytes).decode("ascii"),
                },
            }
        ],
    }
    br._import_table(cur, "reservation_types", table_data)
    insert_query, insert_params = cur.queries[-1]
    assert "INSERT INTO reservation_types" in insert_query
    assert insert_params == (1, raw_bytes)


# ---------------------------------------------------------------------------
# _reset_sequence
# ---------------------------------------------------------------------------


class _SequenceCursor:
    def __init__(self, seq_name):
        self.seq_name = seq_name
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append(query)

    def fetchone(self):
        return (self.seq_name,)


def test_reset_sequence_calls_setval_when_sequence_exists(app_module):
    br = app_module.backup_routes
    cur = _SequenceCursor("reservations_id_seq")
    br._reset_sequence(cur, "reservations")
    assert len(cur.queries) == 2
    assert "setval" in cur.queries[1]
    assert "reservations_id_seq" in cur.queries[1]


def test_reset_sequence_skips_when_no_sequence(app_module):
    br = app_module.backup_routes
    cur = _SequenceCursor(None)
    br._reset_sequence(cur, "app_settings")
    # SELECT pg_get_serial_sequence のみ実行され、setvalは呼ばれない
    assert len(cur.queries) == 1


# ---------------------------------------------------------------------------
# _import_account_tables: アカウント間のデータ分離
# ---------------------------------------------------------------------------


class _AccountImportCursor:
    """conflict_ids に含まれるIDだけ「他アカウント所有」として返すフェイクカーソル。"""

    def __init__(self, conflict_ids=()):
        self.conflict_ids = set(conflict_ids)
        self.queries = []
        self._last_conflict_check = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "WHERE id = ANY(%s) AND owner_admin_id != %s" in query:
            requested_ids = params[0]
            self._last_conflict_check = [
                i for i in requested_ids if i in self.conflict_ids
            ]

    def fetchall(self):
        return [(i,) for i in self._last_conflict_check]


def test_import_account_tables_rejects_column_injection(app_module):
    br = app_module.backup_routes
    cur = _AccountImportCursor()
    tables = {
        "reservation_types": {
            "columns": ["id; DROP TABLE x;--"],
            "rows": [],
        },
        "reservations": {"columns": [], "rows": []},
    }
    with pytest.raises(ValueError):
        br._import_account_tables(cur, owner_admin_id=1, tables=tables)


def test_import_account_tables_rejects_conflicting_type_ids(app_module):
    br = app_module.backup_routes
    # id=5 は別アカウント(owner_admin_id!=1)の種類データとして「存在する」と偽装
    cur = _AccountImportCursor(conflict_ids=[5])
    tables = {
        "reservation_types": {
            "columns": ["id", "name", "owner_admin_id"],
            "rows": [{"id": 5, "name": "他人の種類", "owner_admin_id": 1}],
        },
        "reservations": {"columns": [], "rows": []},
    }
    with pytest.raises(ValueError, match="競合"):
        br._import_account_tables(cur, owner_admin_id=1, tables=tables)


def test_import_account_tables_rejects_conflicting_reservation_ids(app_module):
    br = app_module.backup_routes
    cur = _AccountImportCursor(conflict_ids=[9])
    tables = {
        "reservation_types": {"columns": [], "rows": []},
        "reservations": {
            "columns": ["id", "owner_admin_id"],
            "rows": [{"id": 9, "owner_admin_id": 1}],
        },
    }
    with pytest.raises(ValueError, match="競合"):
        br._import_account_tables(cur, owner_admin_id=1, tables=tables)


def test_import_account_tables_forces_owner_admin_id_on_insert(app_module):
    br = app_module.backup_routes
    cur = _AccountImportCursor(conflict_ids=[])
    tables = {
        "reservation_types": {
            "columns": ["id", "name", "owner_admin_id"],
            # バックアップ内では owner_admin_id=999 という別アカウントの値になっていても、
            "rows": [{"id": 1, "name": "A", "owner_admin_id": 999}],
        },
        "reservations": {"columns": [], "rows": []},
    }
    br._import_account_tables(cur, owner_admin_id=42, tables=tables)
    insert_queries = [
        (q, p) for q, p in cur.queries if q.startswith("INSERT INTO reservation_types")
    ]
    assert len(insert_queries) == 1
    _, params = insert_queries[0]
    # owner_admin_id は強制的に呼び出し元の owner_admin_id (42) に上書きされる
    assert params[-1] == 42


def test_import_account_tables_skips_insert_when_no_rows(app_module):
    br = app_module.backup_routes
    cur = _AccountImportCursor()
    tables = {
        "reservation_types": {"columns": [], "rows": []},
        "reservations": {"columns": [], "rows": []},
    }
    br._import_account_tables(cur, owner_admin_id=1, tables=tables)
    insert_queries = [q for q, _ in cur.queries if q.startswith("INSERT")]
    assert insert_queries == []


# ---------------------------------------------------------------------------
# admin_backup_page: 認可ガード
# ---------------------------------------------------------------------------


def test_admin_backup_page_redirects_to_login_when_not_authenticated(app_module, monkeypatch):
    monkeypatch.setattr(app_module.backup_routes, "is_audit_admin_authenticated", lambda: False)
    monkeypatch.setattr(app_module.backup_routes, "is_admin_authenticated", lambda: False)
    with app_module.app.test_request_context("/admin/backup"):
        response = app_module.backup_routes.admin_backup_page()
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]


def test_admin_backup_page_forbidden_for_regular_admin(app_module, monkeypatch):
    monkeypatch.setattr(app_module.backup_routes, "is_audit_admin_authenticated", lambda: False)
    monkeypatch.setattr(app_module.backup_routes, "is_admin_authenticated", lambda: True)
    with app_module.app.test_request_context("/admin/backup"):
        with pytest.raises(Exception):
            app_module.backup_routes.admin_backup_page()


# ---------------------------------------------------------------------------
# admin_backup_export: 認可ガードと成功パス
# ---------------------------------------------------------------------------


def test_admin_backup_export_unauthorized_returns_401(app_module, monkeypatch):
    monkeypatch.setattr(app_module.backup_routes, "is_audit_admin_authenticated", lambda: False)
    with app_module.app.test_request_context("/admin/backup/export"):
        response = app_module.backup_routes.admin_backup_export()
        assert response[1] == 401


def test_admin_backup_export_success_includes_all_backup_tables(app_module, monkeypatch):
    monkeypatch.setattr(app_module.backup_routes, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(
        app_module.backup_routes,
        "_export_table",
        lambda table_name: {"columns": ["id"], "rows": [{"id": 1}]},
    )
    with app_module.app.test_request_context("/admin/backup/export"):
        response = app_module.backup_routes.admin_backup_export()
        payload = json.loads(response.get_data(as_text=True))
        assert set(payload["tables"].keys()) == set(app_module.backup_routes.BACKUP_TABLES)
        assert payload["scope"] == "full"
        assert "attachment" in response.headers["Content-Disposition"]


def test_admin_backup_export_continues_when_one_table_fails(app_module, monkeypatch):
    monkeypatch.setattr(app_module.backup_routes, "is_audit_admin_authenticated", lambda: True)

    def _flaky_export(table_name):
        if table_name == "reservations":
            raise RuntimeError("boom")
        return {"columns": [], "rows": []}

    monkeypatch.setattr(app_module.backup_routes, "_export_table", _flaky_export)
    with app_module.app.test_request_context("/admin/backup/export"):
        response = app_module.backup_routes.admin_backup_export()
        payload = json.loads(response.get_data(as_text=True))
        # 失敗したテーブルも空データとして出力に含まれ、全体は失敗しない
        assert payload["tables"]["reservations"] == {"columns": [], "rows": []}


# ---------------------------------------------------------------------------
# admin_backup_import: ファイルバリデーション
# ---------------------------------------------------------------------------


def test_admin_backup_import_redirects_to_login_when_not_authenticated(app_module, monkeypatch):
    monkeypatch.setattr(app_module.backup_routes, "is_audit_admin_authenticated", lambda: False)
    monkeypatch.setattr(app_module.backup_routes, "is_admin_authenticated", lambda: False)
    with app_module.app.test_request_context("/admin/backup/import", method="POST"):
        response = app_module.backup_routes.admin_backup_import()
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]


def test_admin_backup_import_missing_file_shows_error(app_module, monkeypatch):
    monkeypatch.setattr(app_module.backup_routes, "is_audit_admin_authenticated", lambda: True)
    with app_module.app.test_request_context("/admin/backup/import", method="POST"):
        response = app_module.backup_routes.admin_backup_import()
        assert response.status_code == 302
        assert "import_error" in response.headers["Location"]


def test_admin_backup_import_rejects_non_json_extension(app_module, monkeypatch):
    monkeypatch.setattr(app_module.backup_routes, "is_audit_admin_authenticated", lambda: True)
    data = {"backup_file": (BytesIO(b"not json"), "backup.txt")}
    with app_module.app.test_request_context(
        "/admin/backup/import", method="POST", data=data, content_type="multipart/form-data"
    ):
        response = app_module.backup_routes.admin_backup_import()
        assert response.status_code == 302
        assert "JSON" in response.headers["Location"] or "import_error" in response.headers["Location"]


def test_admin_backup_import_rejects_invalid_json(app_module, monkeypatch):
    monkeypatch.setattr(app_module.backup_routes, "is_audit_admin_authenticated", lambda: True)
    data = {"backup_file": (BytesIO(b"{not valid json"), "backup.json")}
    with app_module.app.test_request_context(
        "/admin/backup/import", method="POST", data=data, content_type="multipart/form-data"
    ):
        response = app_module.backup_routes.admin_backup_import()
        assert response.status_code == 302
        assert "import_error" in response.headers["Location"]


def test_admin_backup_import_rejects_missing_tables_key(app_module, monkeypatch):
    monkeypatch.setattr(app_module.backup_routes, "is_audit_admin_authenticated", lambda: True)
    payload = json.dumps({"version": "v1"}).encode("utf-8")
    data = {"backup_file": (BytesIO(payload), "backup.json")}
    with app_module.app.test_request_context(
        "/admin/backup/import", method="POST", data=data, content_type="multipart/form-data"
    ):
        response = app_module.backup_routes.admin_backup_import()
        assert response.status_code == 302
        assert "import_error" in response.headers["Location"]


def test_admin_backup_import_success_resets_schema_and_clears_session(app_module, monkeypatch):
    monkeypatch.setattr(app_module.backup_routes, "is_audit_admin_authenticated", lambda: True)

    class _FakeCursor:
        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return (None,)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeConnection:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(app_module.backup_routes, "get_connection", lambda: _FakeConnection())

    reset_calls = []
    monkeypatch.setattr(
        app_module.backup_routes.database, "reset_schema_cache", lambda: reset_calls.append(True)
    )

    payload = json.dumps({"tables": {"app_settings": {"columns": [], "rows": []}}}).encode("utf-8")
    data = {"backup_file": (BytesIO(payload), "backup.json")}
    with app_module.app.test_request_context(
        "/admin/backup/import", method="POST", data=data, content_type="multipart/form-data"
    ):
        response = app_module.backup_routes.admin_backup_import()
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]
        assert reset_calls == [True]


# ---------------------------------------------------------------------------
# admin_backup_export_account / admin_backup_import_account: アカウント単位
# ---------------------------------------------------------------------------


def test_admin_backup_export_account_not_found_returns_404(app_module, monkeypatch):
    monkeypatch.setattr(app_module.backup_routes, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.backup_routes, "get_admin_account_by_id", lambda _id: None)
    with app_module.app.test_request_context("/admin/backup/export/99"):
        response = app_module.backup_routes.admin_backup_export_account(99)
        assert response[1] == 404


def test_admin_backup_import_account_not_found_shows_error(app_module, monkeypatch):
    monkeypatch.setattr(app_module.backup_routes, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.backup_routes, "get_admin_account_by_id", lambda _id: None)
    with app_module.app.test_request_context("/admin/backup/import/99", method="POST"):
        response = app_module.backup_routes.admin_backup_import_account(99)
        assert response.status_code == 302
        assert "import_error" in response.headers["Location"]
