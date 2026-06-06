import csv
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from werkzeug.exceptions import BadRequest


def flex_message_text(message):
    assert message["type"] == "flex"
    header = message["contents"]["header"]["contents"]
    body = message["contents"]["body"]["contents"]
    title = header[0]["text"] if header else ""
    body_text = "\n".join(item["text"] for item in body if item.get("type") == "text")
    return f"{title}\n{body_text}".strip()


def test_parse_bool_env_true_false_default(app_module, monkeypatch):
    monkeypatch.setenv("TEST_BOOL", "true")
    assert app_module.parse_bool_env("TEST_BOOL", False) is True

    monkeypatch.setenv("TEST_BOOL", "off")
    assert app_module.parse_bool_env("TEST_BOOL", True) is False

    monkeypatch.delenv("TEST_BOOL", raising=False)
    assert app_module.parse_bool_env("TEST_BOOL", True) is True


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
def test_parse_bool_env_truthy_variants(app_module, monkeypatch, raw):
    monkeypatch.setenv("TEST_BOOL", raw)
    assert app_module.parse_bool_env("TEST_BOOL", False) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", " random "])
def test_parse_bool_env_falsy_variants(app_module, monkeypatch, raw):
    monkeypatch.setenv("TEST_BOOL", raw)
    assert app_module.parse_bool_env("TEST_BOOL", True) is False


def test_normalize_db_url_remote_adds_sslmode(app_module):
    normalized = app_module.normalize_db_url("postgres://u:p@example.com:5432/db")
    assert normalized.startswith("postgresql://")
    assert "sslmode=require" in normalized


def test_normalize_db_url_localhost_keeps_no_sslmode(app_module):
    normalized = app_module.normalize_db_url("postgresql://u:p@localhost:5432/db")
    assert "sslmode=require" not in normalized


def test_normalize_db_url_invalid_raises(app_module):
    with pytest.raises(RuntimeError):
        app_module.normalize_db_url("not-a-url")


def test_parse_allowed_hosts_supports_multiple_separators(app_module):
    parsed = app_module.parse_allowed_hosts("example.com, api.example.com  admin.example.com")
    assert parsed == {"example.com", "api.example.com", "admin.example.com"}


def test_enforce_host_allowlist_accepts_multiple_hosts(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "ALLOWED_HOSTS",
        {"example.com", "api.example.com"},
    )
    with app_module.app.test_request_context("/", base_url="https://api.example.com"):
        app_module.enforce_host_allowlist()


def test_enforce_host_allowlist_rejects_unknown_host(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "ALLOWED_HOSTS",
        {"example.com", "api.example.com"},
    )
    with app_module.app.test_request_context("/", base_url="https://evil.example.net"):
        with pytest.raises(BadRequest):
            app_module.enforce_host_allowlist()


def test_ensure_reservations_table_adds_type_id_column(app_module, monkeypatch):
    queries = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())

    app_module.ensure_reservations_table()

    normalized_queries = [" ".join(query.split()) for query, _ in queries]
    assert any(
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS type_id INTEGER" in query
        for query in normalized_queries
    )
    assert any(
        "ALTER TABLE reservations ALTER COLUMN user_id SET NOT NULL" in query
        for query in normalized_queries
    )
    assert any(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_reservations_user_active ON reservations (user_id) WHERE status IN ('waiting', 'called')"
        in query
        for query in normalized_queries
    )


def test_process_reservation_persists_user_id_on_new_booking(app_module, monkeypatch):
    queries = []

    class FakeCursor:
        def __init__(self):
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))
            if "FROM reservation_types WHERE name = %s" in query:
                self._last = (1, "相談", True, 7)
            elif "WHERE r.user_id = %s AND r.status IN" in query:
                self._last = None
            elif "FROM admin_accounts WHERE id = %s" in query:
                self._last = (None, None)
            elif "INSERT INTO reservations (user_id, message, type_id)" in query:
                self._last = (10,)
            elif (
                "JOIN reservation_types t ON r.type_id = t.id" in query
                and "r.id < %s" in query
            ):
                self._last = (2,)
            else:
                raise AssertionError(f"Unexpected query: {query}")

        def fetchone(self):
            return self._last

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "is_accepting_new", lambda: True)
    monkeypatch.setattr(
        app_module,
        "refresh_wait_time_estimate",
        lambda now=None, owner_admin_id=None: {
            "message": "現在の目安待ち時間: 6分",
            "estimated_seconds": 360,
        },
    )

    sent_texts = []
    monkeypatch.setattr(
        app_module,
        "send_flex_notice",
        lambda _reply_token, _title, body: sent_texts.append(body),
    )

    event = SimpleNamespace(reply_token="reply-token")
    app_module.process_reservation(event, "U-123", "予約 相談")

    insert_queries = [
        params
        for query, params in queries
        if "INSERT INTO reservations (user_id, message, type_id)" in query
    ]
    assert insert_queries == [("U-123", "", 1)]
    assert sent_texts


def test_handle_message_ignores_specific_url(app_module, monkeypatch):
    called = []

    monkeypatch.setattr(
        app_module,
        "process_reservation",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    event = SimpleNamespace(
        message=SimpleNamespace(text="https://ukweb.ikura.workers.dev/"),
        source=SimpleNamespace(user_id="U-ignore"),
        reply_token="reply-token",
    )

    app_module.handle_message(event)

    assert called == []


def test_ensure_types_table_adds_type_foreign_key(app_module, monkeypatch):
    queries = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())

    app_module.ensure_types_table()

    normalized_queries = [" ".join(query.split()) for query, _ in queries]
    assert any(
        "ADD CONSTRAINT fk_reservations_type_id" in query
        for query in normalized_queries
    )
    assert any("ON DELETE RESTRICT" in query for query in normalized_queries)


def test_format_duration_from_seconds(app_module):
    assert app_module.format_duration_from_seconds(None) == ""
    assert app_module.format_duration_from_seconds(5) == "5秒"
    assert app_module.format_duration_from_seconds(61) == "1分1秒"
    assert app_module.format_duration_from_seconds(3661) == "1時間1分"


def test_format_duration_from_seconds_negative(app_module):
    assert app_module.format_duration_from_seconds(-3) == "0秒"


def test_format_dt_with_naive_datetime(app_module):
    value = datetime(2026, 4, 16, 0, 0, 0)
    assert app_module.format_dt(value) == "04-16 09:00"


def test_format_dt_with_aware_datetime(app_module):
    utc = datetime(2026, 4, 16, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert app_module.format_dt(utc) == "04-16 09:00"


def test_should_run_call_batch(app_module):
    assert app_module.should_run_call_batch(SimpleNamespace(tm_min=10)) is True
    assert app_module.should_run_call_batch(SimpleNamespace(tm_min=11)) is False


def test_parse_hhmm_to_minute_of_day(app_module):
    assert app_module.parse_hhmm_to_minute_of_day("09:30") == 570
    assert app_module.parse_hhmm_to_minute_of_day("23:59") == 1439
    assert app_module.parse_hhmm_to_minute_of_day("") is None
    assert app_module.parse_hhmm_to_minute_of_day("24:00") is None
    assert app_module.parse_hhmm_to_minute_of_day("9:30") is None


def test_is_minute_in_window_supports_overnight(app_module):
    assert app_module.is_minute_in_window(600, 540, 1020) is True
    assert app_module.is_minute_in_window(500, 540, 1020) is False
    assert app_module.is_minute_in_window(30, 1320, 120) is True
    assert app_module.is_minute_in_window(800, 1320, 120) is False
    assert app_module.is_minute_in_window(123, 500, 500) is True


def test_get_admin_reservation_window_uses_existing_cursor(app_module, monkeypatch):
    class FakeCursor:
        def __init__(self):
            self._last = None

        def execute(self, query, params=None):
            assert "FROM admin_accounts WHERE id = %s" in query
            assert params == (7,)
            self._last = (570, 1020)

        def fetchone(self):
            return self._last

    monkeypatch.setattr(
        app_module,
        "get_connection",
        lambda: (_ for _ in ()).throw(
            AssertionError("get_connection should not be used")
        ),
    )
    start_minute, end_minute = app_module.get_admin_reservation_window(
        7, cur=FakeCursor()
    )
    assert start_minute == 570
    assert end_minute == 1020


def test_send_push_message_uses_retry_key(app_module, monkeypatch):
    captured = []

    class DummyApiClient:
        def __init__(self, _config):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyMessagingApi:
        def __init__(self, _api_client):
            return None

        def push_message(self, request_payload, x_line_retry_key=None):
            captured.append(
                (request_payload.to, request_payload.messages[0].text, x_line_retry_key)
            )
            return None

    monkeypatch.setattr(app_module, "ApiClient", DummyApiClient)
    monkeypatch.setattr(app_module, "MessagingApi", DummyMessagingApi)
    app_module.send_push_message("U1", "hello", retry_key="retry-key-1")
    assert captured == [("U1", "hello", "retry-key-1")]


def test_send_push_message_retries_with_same_key(app_module, monkeypatch):
    attempt_keys = []

    class RetryableError(Exception):
        status = 500

    class DummyApiClient:
        def __init__(self, _config):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyMessagingApi:
        calls = 0

        def __init__(self, _api_client):
            return None

        def push_message(self, _request_payload, x_line_retry_key=None):
            DummyMessagingApi.calls += 1
            attempt_keys.append(x_line_retry_key)
            if DummyMessagingApi.calls == 1:
                raise RetryableError("temporary failure")
            return None

    monkeypatch.setattr(app_module, "ApiClient", DummyApiClient)
    monkeypatch.setattr(app_module, "MessagingApi", DummyMessagingApi)
    monkeypatch.setattr(app_module, "LINE_PUSH_MAX_RETRIES", 2)
    monkeypatch.setattr(app_module.time, "sleep", lambda _secs: None)
    app_module.send_push_message("U2", "hello", retry_key="retry-fixed")
    assert attempt_keys == ["retry-fixed", "retry-fixed"]


def test_send_push_message_treats_409_as_success(app_module, monkeypatch):
    class ConflictError(Exception):
        status = 409

    class DummyApiClient:
        def __init__(self, _config):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyMessagingApi:
        def __init__(self, _api_client):
            return None

        def push_message(self, _request_payload, x_line_retry_key=None):
            raise ConflictError("already accepted")

    monkeypatch.setattr(app_module, "ApiClient", DummyApiClient)
    monkeypatch.setattr(app_module, "MessagingApi", DummyMessagingApi)
    # 409は受理済み扱いで例外を送出しない
    app_module.send_push_message("U3", "hello", retry_key="retry-409")


def test_normalize_and_validate_type_name(app_module):
    assert app_module.normalize_type_name("  A   B  ") == "A B"
    assert app_module.validate_type_name("相談") is True
    assert app_module.validate_type_name("") is False


def test_validate_type_name_length_boundary(app_module):
    max_len_name = "A" * app_module.MAX_TYPE_NAME_LENGTH
    over_name = "A" * (app_module.MAX_TYPE_NAME_LENGTH + 1)
    assert app_module.validate_type_name(max_len_name) is True
    assert app_module.validate_type_name(over_name) is False


def test_validate_type_name_invalid_chars(app_module):
    assert app_module.validate_type_name("<script>") is False


def test_build_auto_call_summary_empty(app_module):
    summary = app_module.build_auto_call_summary({}, "last")
    assert summary["run_at"] == ""
    assert "まだ自動呼出は実行されていません" in summary["message"]


def test_build_auto_call_summary_with_values(app_module):
    values = {
        "last_auto_call_run_at": "04-16 10:00",
        "last_auto_call_sent_count": "2",
        "last_auto_call_failed_count": "1",
        "last_auto_call_selected_count": "3",
    }
    summary = app_module.build_auto_call_summary(values, "last")
    assert summary["run_at"] == "04-16 10:00"
    assert summary["sent_count"] == 2
    assert summary["failed_count"] == 1
    assert summary["selected_count"] == 3


def test_get_latest_wait_time_summary_empty(app_module):
    summary = app_module.get_latest_wait_time_summary({})
    assert summary["run_at"] == ""
    assert "算出中" in summary["message"]


def test_get_latest_wait_time_summary_with_values(app_module):
    values = {
        "last_wait_time_run_at": "04-17 10:00",
        "last_wait_time_estimated_seconds": "420",
        "last_wait_time_waiting_count": "3",
        "last_wait_time_avg_service_seconds": "140",
    }
    summary = app_module.get_latest_wait_time_summary(values)
    assert summary["run_at"] == "04-17 10:00"
    assert summary["estimated_seconds"] == 420
    assert summary["waiting_count"] == 3
    assert summary["avg_service_seconds"] == 140
    assert "7分" in summary["message"]


def test_calculate_wait_time_minutes_formula(app_module):
    assert app_module.calculate_wait_time_minutes(0) == 2
    assert app_module.calculate_wait_time_minutes(1) == 3
    assert app_module.calculate_wait_time_minutes(2) == 3
    assert app_module.calculate_wait_time_minutes(3) == 4


@pytest.mark.parametrize(
    ("people_ahead", "expected_minutes"),
    [
        (0, 2),
        (1, 3),
        (2, 3),
        (3, 4),
        (4, 4),
        (5, 5),
        (6, 5),
    ],
)
def test_calculate_wait_time_minutes_boundaries(
    app_module, people_ahead, expected_minutes
):
    assert app_module.calculate_wait_time_minutes(people_ahead) == expected_minutes


def test_validate_batch_runner_token_authorization_header(app_module):
    app_module.BATCH_CALL_RUNNER_TOKEN = "token123"
    with app_module.app.test_request_context(
        "/tasks/process-call-queue", headers={"Authorization": "Bearer token123"}
    ):
        assert app_module.validate_batch_runner_token() is True


def test_validate_batch_runner_token_custom_header(app_module):
    app_module.BATCH_CALL_RUNNER_TOKEN = "token123"
    with app_module.app.test_request_context(
        "/tasks/process-call-queue", headers={"X-Task-Token": "token123"}
    ):
        assert app_module.validate_batch_runner_token() is True


def test_validate_batch_runner_token_missing(app_module):
    app_module.BATCH_CALL_RUNNER_TOKEN = ""
    with app_module.app.test_request_context("/tasks/process-call-queue"):
        assert app_module.validate_batch_runner_token() is False


def test_get_csrf_token_generates_and_reuses(app_module):
    with app_module.app.test_request_context("/"):
        first = app_module.get_csrf_token()
        second = app_module.get_csrf_token()
        assert first == second
        assert isinstance(first, str)
        assert len(first) > 20


def test_start_admin_session_preserves_existing_csrf_token(app_module):
    with app_module.app.test_request_context("/login"):
        app_module.session["_csrf_token"] = "shared-token"
        app_module.start_admin_session(app_module.ROLE_ADMIN, 1, "admin")

        assert app_module.session["_csrf_token"] == "shared-token"
        assert app_module.session["logged_in"] is True
        assert app_module.session["admin_role"] == app_module.ROLE_ADMIN


def test_validate_csrf_success(app_module):
    with app_module.app.test_request_context(
        "/dummy", method="POST", data={"_csrf_token": "abc"}
    ):
        app_module.session["_csrf_token"] = "abc"
        app_module.validate_csrf()


def test_validate_csrf_failure(app_module):
    with app_module.app.test_request_context(
        "/dummy", method="POST", data={"_csrf_token": "wrong"}
    ):
        app_module.session["_csrf_token"] = "abc"
        with pytest.raises(Forbidden):
            app_module.validate_csrf()


def test_admin_post_without_active_session_redirects_to_login(
    client, app_module, monkeypatch
):
    monkeypatch.setattr(app_module, "set_accepting_new", lambda _value: None)

    response = client.post("/admin/toggle-accepting")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_admin_post_with_active_session_still_rejects_invalid_csrf(
    app_module, monkeypatch
):
    with app_module.app.test_request_context(
        "/admin/toggle-accepting", method="POST", data={"_csrf_token": "wrong"}
    ):
        app_module.session["logged_in"] = True
        app_module.session["admin_role"] = app_module.ROLE_ADMIN
        app_module.session["admin_account_id"] = 1
        app_module.session["last_activity"] = 1000.0
        app_module.session["_csrf_token"] = "expected-token"
        monkeypatch.setattr(app_module.time, "time", lambda: 1005.0)

        with pytest.raises(Forbidden):
            app_module.csrf_protect()


def test_is_authenticated_as_success(app_module):
    with app_module.app.test_request_context("/"):
        now = 1000.0
        app_module.session["logged_in"] = True
        app_module.session["admin_role"] = app_module.ROLE_ADMIN
        app_module.session["admin_account_id"] = 1
        app_module.session["last_activity"] = now
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(app_module.time, "time", lambda: now + 10)
            assert app_module.is_authenticated_as(app_module.ROLE_ADMIN) is True


def test_is_authenticated_as_timeout_clears_session(app_module):
    with app_module.app.test_request_context("/"):
        now = 1000.0
        app_module.session["logged_in"] = True
        app_module.session["admin_role"] = app_module.ROLE_ADMIN
        app_module.session["admin_account_id"] = 1
        app_module.session["last_activity"] = now
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                app_module.time,
                "time",
                lambda: now + app_module.SESSION_IDLE_TIMEOUT_SECONDS + 1,
            )
            assert app_module.is_authenticated_as(app_module.ROLE_ADMIN) is False
        assert app_module.session.get("logged_in") is None


def test_apply_security_headers_admin_page(app_module):
    app_module.FORCE_HTTPS = True
    with app_module.app.test_request_context(
        "/admin", headers={"X-Forwarded-Proto": "https"}
    ):
        response = app_module.app.response_class("ok")
        result = app_module.apply_security_headers(response)
        assert "Content-Security-Policy" in result.headers
        assert result.headers.get("X-Frame-Options") == "DENY"
        assert "Strict-Transport-Security" in result.headers
        assert "no-store" in result.headers.get("Cache-Control", "")


def test_is_login_rate_limited_on_exception_returns_true(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "get_connection",
        lambda: (_ for _ in ()).throw(RuntimeError("db error")),
    )
    assert app_module.is_login_rate_limited("127.0.0.1") is True


def test_record_login_failure_on_exception_does_not_raise(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "get_connection",
        lambda: (_ for _ in ()).throw(RuntimeError("db error")),
    )
    app_module.record_login_failure("127.0.0.1")


def test_is_webhook_rate_limited_on_exception_returns_true(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "get_connection",
        lambda: (_ for _ in ()).throw(RuntimeError("db error")),
    )
    assert app_module.is_webhook_rate_limited("127.0.0.1") is True


def test_login_get_ok(client):
    response = client.get("/login")
    assert response.status_code == 200


def test_login_post_admin_success_redirect(client, csrf_token, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_login_rate_limited", lambda _ip: False)
    monkeypatch.setattr(
        app_module,
        "authenticate_admin_account",
        lambda _login_id, _pwd: {
            "id": 1,
            "login_id": "admin",
            "role": app_module.ROLE_ADMIN,
        },
    )
    monkeypatch.setattr(app_module, "record_admin_login", lambda *args, **kwargs: None)

    response = client.post(
        "/login",
        data={"login_id": "admin", "password": "admin-pass", "_csrf_token": csrf_token},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin")


def test_login_post_audit_success_redirect(client, csrf_token, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_login_rate_limited", lambda _ip: False)
    monkeypatch.setattr(
        app_module,
        "authenticate_admin_account",
        lambda _login_id, _pwd: {
            "id": 2,
            "login_id": "audit",
            "role": app_module.ROLE_AUDIT_ADMIN,
        },
    )
    monkeypatch.setattr(app_module, "record_admin_login", lambda *args, **kwargs: None)

    response = client.post(
        "/login",
        data={"login_id": "audit", "password": "audit-pass", "_csrf_token": csrf_token},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/login-logs")


def test_login_post_failure_shows_error(client, csrf_token, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_login_rate_limited", lambda _ip: False)
    monkeypatch.setattr(
        app_module, "authenticate_admin_account", lambda _login_id, _pwd: None
    )
    monkeypatch.setattr(app_module, "record_admin_login", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "record_login_failure", lambda _ip: None)

    response = client.post(
        "/login",
        data={"login_id": "admin", "password": "wrong", "_csrf_token": csrf_token},
    )
    assert response.status_code == 200
    assert "ログインIDまたはパスワードが正しくありません" in response.get_data(as_text=True)


def test_login_post_rate_limited(client, csrf_token, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_login_rate_limited", lambda _ip: True)
    response = client.post(
        "/login",
        data={"login_id": "admin", "password": "x", "_csrf_token": csrf_token},
    )
    assert response.status_code == 429


def test_admin_page_shows_version_badge(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(
        app_module,
        "get_runtime_settings",
        lambda: {
            "accepting_new": True,
            "auto_call_count": 0,
            "last_auto_call": {},
            "latest_auto_call": {},
        },
    )

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            normalized_query = " ".join(query.split())
            if normalized_query.startswith(
                "SELECT id, name FROM reservation_types WHERE owner_admin_id = %s ORDER BY id ASC"
            ):
                self._rows = []
            elif (
                normalized_query.startswith("SELECT")
                and "FROM reservations" in normalized_query
            ):
                self._rows = []
            else:
                self._rows = []

        def fetchall(self):
            return getattr(self, "_rows", [])

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())

    response = client.get("/admin")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert f"Version: {app_module.APP_VERSION}" in text


def test_types_page_shows_version_badge(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module, "is_accepting_new", lambda: True)
    monkeypatch.setattr(
        app_module, "get_admin_reservation_window", lambda _admin_id: (None, None)
    )

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            self._rows = []

        def fetchall(self):
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())

    response = client.get("/admin/types")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert f"Version: {app_module.APP_VERSION}" in text
    assert 'id="global-accepting-badge"' in text
    assert "static/js/types.js" in text


def test_admin_types_delete_blocks_types_with_reservations(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)

    rollback_called = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            if "DELETE FROM reservation_types" in query:
                raise app_module.psycopg2.IntegrityError("fk violation")

        @property
        def rowcount(self):
            return 1

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def rollback(self):
            rollback_called.append(True)

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context("/admin/types/delete/1", method="POST"):
        response = app_module.admin_types_delete(1)

    assert response.status_code == 302
    assert "type_error=" in response.headers["Location"]
    assert rollback_called == [True]


def test_admin_accounts_create_requires_audit_auth(app_module):
    with app_module.app.test_request_context(
        "/admin/admin-accounts",
        method="POST",
        data={"login_id": "manager01", "password": "superpower"},
    ):
        response = app_module.admin_accounts_create()
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_admin_accounts_create_success(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)

    calls = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/admin-accounts",
        method="POST",
        data={"login_id": "manager01", "password": "superpower"},
    ):
        response = app_module.admin_accounts_create()

    assert response.status_code == 302
    assert "account_success" in response.headers["Location"]
    assert calls


def test_admin_accounts_create_duplicate_login_id(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _query, _params=None):
            raise app_module.psycopg2.IntegrityError()

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/admin-accounts",
        method="POST",
        data={"login_id": "manager01", "password": "superpower"},
    ):
        response = app_module.admin_accounts_create()

    assert response.status_code == 302
    assert "account_error" in response.headers["Location"]


def test_admin_accounts_bulk_create_success(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)

    calls = []

    class FakeCursor:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))
            if "SELECT login_id FROM admin_accounts" in query:
                self._rows = []

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return None

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/admin-accounts",
        method="POST",
        data={
            "login_id": "",
            "password": "",
            "bulk_accounts": "manager01,superpower01\nmanager02,superpower02",
        },
    ):
        response = app_module.admin_accounts_create()

    assert response.status_code == 302
    assert "account_success" in response.headers["Location"]
    assert any("SELECT login_id FROM admin_accounts" in query for query, _ in calls)


def test_admin_accounts_bulk_create_invalid_line_format(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)

    with app_module.app.test_request_context(
        "/admin/admin-accounts",
        method="POST",
        data={
            "login_id": "",
            "password": "",
            "bulk_accounts": "manager01-superpower01",
        },
    ):
        response = app_module.admin_accounts_create()

    assert response.status_code == 302
    assert "account_error" in response.headers["Location"]


def test_admin_accounts_update_login_id_requires_audit_auth(app_module):
    with app_module.app.test_request_context(
        "/admin/admin-accounts/1/login-id",
        method="POST",
        data={"login_id": "manager02"},
    ):
        response = app_module.admin_accounts_update_login_id(1)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_admin_accounts_update_login_id_success(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)

    calls = []

    class FakeCursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/admin-accounts/1/login-id",
        method="POST",
        data={"login_id": "manager02"},
    ):
        response = app_module.admin_accounts_update_login_id(1)

    assert response.status_code == 302
    assert "account_success" in response.headers["Location"]
    assert calls


def test_admin_accounts_update_login_id_duplicate(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _query, _params=None):
            raise app_module.psycopg2.IntegrityError()

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/admin-accounts/1/login-id",
        method="POST",
        data={"login_id": "manager02"},
    ):
        response = app_module.admin_accounts_update_login_id(1)

    assert response.status_code == 302
    assert "account_error" in response.headers["Location"]


def test_admin_data_unauthorized(client):
    response = client.get("/admin/data")
    assert response.status_code == 401


def test_admin_data_includes_runtime_controls(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module, "get_active_rows", lambda _cur, owner_admin_id=None: [])
    monkeypatch.setattr(
        app_module,
        "fetch_type_counts",
        lambda _cur, owner_admin_id: [],
    )
    monkeypatch.setattr(
        app_module,
        "get_runtime_settings",
        lambda: {
            "accepting_new": False,
            "auto_call_count": 7,
            "last_auto_call": {"message": "last"},
            "latest_auto_call": {"message": "latest"},
        },
    )

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())

    response = client.get("/admin/data")

    assert response.status_code == 200
    body = response.get_json()
    assert body["meta"]["accepting_new"] is False
    assert body["meta"]["auto_call_count"] == 7
    assert body["meta"]["last_auto_call"]["message"] == "last"
    assert body["meta"]["latest_auto_call"]["message"] == "latest"


def test_process_call_queue_task_without_token_returns_503(client, app_module):
    app_module.BATCH_CALL_RUNNER_TOKEN = ""
    response = client.post("/tasks/process-call-queue")
    assert response.status_code == 503


def test_process_call_queue_task_invalid_token_returns_403(
    client, app_module, monkeypatch
):
    app_module.BATCH_CALL_RUNNER_TOKEN = "token"
    monkeypatch.setattr(app_module, "validate_batch_runner_token", lambda: False)
    response = client.post("/tasks/process-call-queue")
    assert response.status_code == 403


def test_process_call_queue_task_success_returns_json(client, app_module, monkeypatch):
    app_module.BATCH_CALL_RUNNER_TOKEN = "token"
    monkeypatch.setattr(app_module, "validate_batch_runner_token", lambda: True)
    monkeypatch.setattr(
        app_module,
        "process_queued_calls",
        lambda: {"processed": True, "reason": "ok", "sent_count": 1, "failed_count": 0},
    )
    response = client.post("/tasks/process-call-queue")
    assert response.status_code == 200
    body = response.get_json()
    assert body["processed"] is True
    assert body["sent_count"] == 1


def test_callback_missing_signature_returns_400(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_webhook_rate_limited", lambda _ip: False)
    response = client.post("/callback", data="{}", content_type="application/json")
    assert response.status_code == 400


def test_callback_invalid_signature_returns_400(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_webhook_rate_limited", lambda _ip: False)

    class InvalidSignatureHandler:
        @staticmethod
        def handle(_body, _signature):
            raise app_module.InvalidSignatureError("bad")

    monkeypatch.setattr(app_module, "handler", InvalidSignatureHandler())
    response = client.post(
        "/callback",
        data="{}",
        headers={"X-Line-Signature": "sig"},
        content_type="application/json",
    )
    assert response.status_code == 400


def test_callback_rate_limited_returns_429(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_webhook_rate_limited", lambda _ip: True)
    response = client.post(
        "/callback",
        data="{}",
        headers={"X-Line-Signature": "sig"},
        content_type="application/json",
    )
    assert response.status_code == 429


def test_callback_success_returns_ok(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_webhook_rate_limited", lambda _ip: False)

    class DummyHandler:
        @staticmethod
        def handle(_body, _signature):
            return None

    monkeypatch.setattr(app_module, "handler", DummyHandler())

    response = client.post(
        "/callback",
        data="{}",
        headers={"X-Line-Signature": "sig"},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "OK"


def test_callback_processing_error_returns_ok(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_webhook_rate_limited", lambda _ip: False)

    class FailingHandler:
        @staticmethod
        def handle(_body, _signature):
            raise RuntimeError("temporary downstream failure")

    monkeypatch.setattr(app_module, "handler", FailingHandler())

    response = client.post(
        "/callback",
        data="{}",
        headers={"X-Line-Signature": "sig"},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "OK"


def test_should_run_call_batch_uses_localtime_when_now_none(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module.time, "localtime", lambda: SimpleNamespace(tm_min=15)
    )
    assert app_module.should_run_call_batch() is True


def test_build_call_message_includes_timeout_minutes_and_deadline(app_module):
    called_at = datetime(2026, 4, 19, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    message = app_module.build_call_message(15, called_at=called_at)
    text = flex_message_text(message)
    assert "呼出中" in text
    assert "番号: 15" in text
    assert f"{app_module.CALL_TIMEOUT_MINUTES}分以内" in text
    assert "10:15" in text
    assert "自動でキャンセル" in text


def test_expire_called_reservations_updates_called_rows(app_module, monkeypatch):
    sent_messages = []

    class FakeCursor:
        def __init__(self):
            self._rows = [(10, "U-1"), (11, "U-2"), (12, "U-3")]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            assert "UPDATE reservations" in query
            assert "called_at <=" in query
            assert "RETURNING id, user_id" in query
            assert params[0] == app_module.STATUS_CANCELLED
            assert params[1] == app_module.STATUS_CALLED
            assert params[2] == app_module.CALL_TIMEOUT_MINUTES

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(
        app_module,
        "send_push_message",
        lambda user_id, message: sent_messages.append((user_id, message)),
    )
    assert app_module.expire_called_reservations() == 3
    assert len(sent_messages) == 3
    assert sent_messages[0][0] == "U-1"
    assert "自動キャンセル" in flex_message_text(sent_messages[0][1])


def test_expire_called_reservations_ignores_push_failure(app_module, monkeypatch):
    class FakeCursor:
        def __init__(self):
            self._rows = [(20, "U-timeout")]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _query, _params=None):
            return None

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(
        app_module,
        "send_push_message",
        lambda _user_id, _text: (_ for _ in ()).throw(RuntimeError("push fail")),
    )
    assert app_module.expire_called_reservations() == 1


def test_process_queued_calls_not_due_returns_early(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "should_run_call_batch", lambda _now: False)
    monkeypatch.setattr(app_module, "expire_called_reservations", lambda: 2)
    monkeypatch.setattr(
        app_module,
        "refresh_wait_time_estimate",
        lambda _now=None: {
            "message": "現在の目安待ち時間: 6分0秒",
            "estimated_seconds": 360,
        },
    )
    now = datetime(2026, 4, 16, 10, 1, tzinfo=ZoneInfo("Asia/Tokyo"))
    result = app_module.process_queued_calls(now=now)
    assert result["processed"] is False
    assert result["reason"] == "not_due"
    assert result["timed_out_count"] == 2
    assert result["wait_time"]["estimated_seconds"] == 360


def test_process_queued_calls_rolls_back_failed_push_rows(app_module, monkeypatch):
    executed = []
    commits = []

    class FakeCursor:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            normalized_query = " ".join(query.split())
            executed.append((normalized_query, params))
            if "RETURNING id, user_id" in normalized_query:
                self._rows = [(10, "U-ok"), (11, "U-fail")]
            else:
                self._rows = []

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            commits.append(True)

    def fake_send_push(user_id, _text):
        if user_id == "U-fail":
            raise RuntimeError("push failed")

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "should_run_call_batch", lambda _now: True)
    monkeypatch.setattr(app_module, "expire_called_reservations", lambda: 0)
    monkeypatch.setattr(
        app_module,
        "refresh_wait_time_estimate",
        lambda _now=None: {
            "message": "現在の目安待ち時間: 2分",
            "estimated_seconds": 120,
        },
    )
    monkeypatch.setattr(app_module, "ensure_database_schema", lambda: None)
    monkeypatch.setattr(
        app_module,
        "get_runtime_settings",
        lambda: {
            "auto_call_count": 2,
            "latest_auto_call": {
                "run_at": "",
                "sent_count": 0,
                "failed_count": 0,
                "selected_count": 0,
            },
        },
    )
    saved_settings = {}
    monkeypatch.setattr(
        app_module, "set_settings", lambda values: saved_settings.update(values)
    )
    monkeypatch.setattr(app_module, "send_push_message", fake_send_push)

    now = datetime(2026, 4, 16, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    result = app_module.process_queued_calls(now=now)

    assert result["sent_count"] == 1
    assert result["failed_count"] == 1
    assert result["failed_ids"] == [11]
    rollback_queries = [item for item in executed if "called_at = NULL" in item[0]]
    assert rollback_queries == [
        (
            "UPDATE reservations SET status = %s, called_at = NULL WHERE id = ANY(%s) AND status = %s",
            (app_module.STATUS_WAITING, [11], app_module.STATUS_CALLED),
        )
    ]
    assert saved_settings["last_auto_call_failed_count"] == "1"
    assert len(commits) == 2


def test_process_queued_calls_uses_skip_locked_and_total_limit(app_module, monkeypatch):
    executed = []
    sent_messages = []

    class FakeCursor:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            normalized_query = " ".join(query.split())
            executed.append((normalized_query, params))
            if "RETURNING id, user_id" in normalized_query:
                assert "FOR UPDATE SKIP LOCKED" in normalized_query
                assert "AND status = %s" in normalized_query
                self._rows = [(10, "U-total-1")]
            else:
                self._rows = []

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "should_run_call_batch", lambda _now: True)
    monkeypatch.setattr(app_module, "expire_called_reservations", lambda: 0)
    monkeypatch.setattr(
        app_module,
        "refresh_wait_time_estimate",
        lambda _now=None: {
            "message": "現在の目安待ち時間: 2分",
            "estimated_seconds": 120,
        },
    )
    monkeypatch.setattr(
        app_module,
        "get_runtime_settings",
        lambda: {
            "auto_call_count": 1,
            "latest_auto_call": {
                "run_at": "",
                "sent_count": 0,
                "failed_count": 0,
                "selected_count": 0,
            },
        },
    )
    monkeypatch.setattr(app_module, "set_settings", lambda _values: None)
    monkeypatch.setattr(
        app_module,
        "send_push_message",
        lambda user_id, text: sent_messages.append((user_id, text)),
    )

    now = datetime(2026, 4, 16, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    result = app_module.process_queued_calls(now=now)

    assert result["sent_count"] == 1
    assert result["failed_count"] == 0
    assert result["auto_selected_count"] == 1
    assert [item[0] for item in sent_messages] == ["U-total-1"]


def test_process_reservation_new_booking_replies_with_latest_wait_time(
    app_module, monkeypatch
):
    queries = []

    class FakeCursor:
        def __init__(self):
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))
            if "FROM reservation_types WHERE name = %s" in query:
                self._last = (1, "相談", True, 7)
            elif "WHERE r.user_id = %s AND r.status IN" in query:
                self._last = None
            elif "FROM admin_accounts WHERE id = %s" in query:
                self._last = (None, None)
            elif "INSERT INTO reservations (user_id, message, type_id)" in query:
                self._last = (10,)
            elif (
                "JOIN reservation_types t ON r.type_id = t.id" in query
                and "r.id < %s" in query
            ):
                self._last = (2,)
            else:
                raise AssertionError(f"Unexpected query: {query}")

        def fetchone(self):
            return self._last

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "is_accepting_new", lambda: True)
    monkeypatch.setattr(
        app_module,
        "refresh_wait_time_estimate",
        lambda now=None, owner_admin_id=None: {
            "message": "現在の目安待ち時間: 6分",
            "estimated_seconds": 360,
        },
    )

    sent_texts = []

    monkeypatch.setattr(
        app_module,
        "send_flex_notice",
        lambda _reply_token, _title, body: sent_texts.append(body),
    )

    event = SimpleNamespace(reply_token="reply-token")
    app_module.process_reservation(event, "U-123", "予約 相談")

    assert sent_texts
    assert "【受付完了】番号: 10 / 種類: 相談 / 待ち: 2人" in sent_texts[0]
    assert "現在の目安待ち時間: 3分" in sent_texts[0]


def test_process_reservation_blocks_outside_admin_window(app_module, monkeypatch):
    class FixedDateTime:
        @staticmethod
        def now(tz=None):
            # 08:00 JSTに固定して、09:30〜17:00の受付時間外を再現する
            value = datetime(2026, 4, 20, 8, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(app_module, "datetime", FixedDateTime)

    class FakeCursor:
        def __init__(self):
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            if "FROM reservation_types WHERE name = %s" in query:
                self._last = (1, "相談", True, 7)
            elif "WHERE r.user_id = %s AND r.status IN" in query:
                self._last = None
            elif "FROM admin_accounts WHERE id = %s" in query:
                self._last = (570, 1020)
            else:
                raise AssertionError(f"Unexpected query: {query}")

        def fetchone(self):
            return self._last

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "is_accepting_new", lambda: True)

    sent_texts = []
    monkeypatch.setattr(
        app_module,
        "send_flex_notice",
        lambda _reply_token, _title, body: sent_texts.append(body),
    )

    event = SimpleNamespace(reply_token="reply-token")
    app_module.process_reservation(event, "U-456", "予約 相談")

    assert sent_texts
    assert "予約受付時間は 09:30〜17:00" in sent_texts[-1]
    assert "受付時間外" in sent_texts[-1]


def test_process_reservation_wait_time_reply_for_waiting_user(app_module, monkeypatch):
    class FakeCursor:
        def __init__(self):
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            if (
                "FROM reservations r" in query
                and "WHERE r.user_id = %s AND r.status IN" in query
            ):
                self._last = (12, app_module.STATUS_WAITING, "相談", 7)
            elif (
                "JOIN reservation_types t ON r.type_id = t.id" in query
                and "r.id < %s" in query
            ):
                self._last = (3,)
            else:
                raise AssertionError(f"Unexpected query: {query}")

        def fetchone(self):
            return self._last

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "is_accepting_new", lambda: True)

    sent_texts = []
    monkeypatch.setattr(
        app_module,
        "send_flex_notice",
        lambda _reply_token, _title, body: sent_texts.append(body),
    )

    event = SimpleNamespace(reply_token="reply-token")
    app_module.process_reservation(event, "U-789", "待ち時間")

    assert sent_texts
    assert "あなたの前: 3人" in sent_texts[-1]
    assert "現在の目安待ち時間: 4分" in sent_texts[-1]


def test_process_reservation_wait_time_reply_without_active_reservation(
    app_module, monkeypatch
):
    class FakeCursor:
        def __init__(self):
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            if (
                "FROM reservations r" in query
                and "WHERE r.user_id = %s AND r.status IN" in query
            ):
                self._last = None
            else:
                raise AssertionError(f"Unexpected query: {query}")

        def fetchone(self):
            return self._last

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "is_accepting_new", lambda: True)

    sent_texts = []
    monkeypatch.setattr(
        app_module,
        "send_flex_notice",
        lambda _reply_token, _title, body: sent_texts.append(body),
    )

    event = SimpleNamespace(reply_token="reply-token")
    app_module.process_reservation(event, "U-999", "待ち時間")

    assert sent_texts
    assert "待ち時間を確認できる予約がありません" in sent_texts[-1]


def test_process_reservation_cancel_commits_when_cancelled(app_module, monkeypatch):
    commits = []

    class FakeCursor:
        def __init__(self):
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            if (
                "UPDATE reservations SET status = %s" in query
                and "RETURNING id" in query
            ):
                self._last = (42,)
            else:
                raise AssertionError(f"Unexpected query: {query}")

        def fetchone(self):
            return self._last

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            commits.append(True)

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "is_accepting_new", lambda: True)

    sent_texts = []
    monkeypatch.setattr(
        app_module,
        "send_flex_notice",
        lambda _reply_token, _title, body: sent_texts.append(body),
    )

    event = SimpleNamespace(reply_token="reply-token")
    app_module.process_reservation(event, "U-cancel", "キャンセル")

    assert sent_texts
    assert "予約番号 42 をキャンセルしました。" in sent_texts[-1]
    assert commits


def test_admin_reservation_hours_updates_window(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 5)

    calls = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/reservation-hours",
        method="POST",
        data={"reservation_start_time": "09:30", "reservation_end_time": "17:00"},
    ):
        response = app_module.admin_reservation_hours()

    assert response.status_code == 302
    assert "schedule_success" in response.headers["Location"]
    assert "/admin/types" in response.headers["Location"]
    assert any("UPDATE admin_accounts" in query for query, _ in calls)


def test_admin_history_export_includes_extended_columns(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 7)

    queries = []

    class FakeCursor:
        def __init__(self):
            self._rows = [
                (
                    12,
                    "相談",
                    app_module.STATUS_DONE,
                    datetime(2026, 4, 20, 2, 15, tzinfo=ZoneInfo("UTC")),
                    datetime(2026, 4, 20, 2, 25, tzinfo=ZoneInfo("UTC")),
                    datetime(2026, 4, 20, 3, 0, tzinfo=ZoneInfo("UTC")),
                    600,
                    2700,
                    2100,
                )
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))

        def __iter__(self):
            return iter(self._rows)

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def cursor(self, name=None):
            return FakeCursor()

        def close(self):
            self.closed = True

    monkeypatch.setattr(app_module, "create_connection", lambda: FakeConnection())

    with app_module.app.test_request_context("/admin/history/export.csv"):
        response = app_module.admin_history_export()
        text = response.get_data(as_text=True)

    rows = list(csv.reader(text.splitlines()))
    assert rows[0] == [
        "番号",
        "種類",
        "状態",
        "受付時刻",
        "呼出時刻",
        "完了時刻",
        "受付から呼出",
        "受付から完了",
        "呼出から完了",
    ]
    assert rows[1] == [
        "12",
        "相談",
        app_module.STATUS_DONE,
        "04-20 11:15",
        "04-20 11:25",
        "04-20 12:00",
        "10分0秒",
        "45分0秒",
        "35分0秒",
    ]
    assert any("r.called_at" in query for query, _ in queries)


def test_admin_history_export_requires_login(client):
    response = client.get("/admin/history/export.csv")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_admin_history_export_null_values_are_formatted_safely(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 7)

    class FakeCursor:
        def __init__(self):
            self._rows = [
                (
                    99,
                    "",
                    app_module.STATUS_CANCELLED,
                    datetime(2026, 4, 21, 0, 0, tzinfo=ZoneInfo("UTC")),
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _query, _params=None):
            return None

        def __iter__(self):
            return iter(self._rows)

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def cursor(self, name=None):
            return FakeCursor()

        def close(self):
            self.closed = True

    monkeypatch.setattr(app_module, "create_connection", lambda: FakeConnection())

    with app_module.app.test_request_context("/admin/history/export.csv"):
        response = app_module.admin_history_export()
        text = response.get_data(as_text=True)

    rows = list(csv.reader(text.splitlines()))
    assert rows[1] == [
        "99",
        "",
        app_module.STATUS_CANCELLED,
        "04-21 09:00",
        "",
        "",
        "-",
        "-",
        "-",
    ]


def test_admin_history_export_invalid_query_params_fall_back_to_defaults(
    app_module, monkeypatch
):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 7)

    calls = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))

        def __iter__(self):
            return iter([])

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def cursor(self, name=None):
            return FakeCursor()

        def close(self):
            self.closed = True

    monkeypatch.setattr(app_module, "create_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/history/export.csv?sort_by=unknown&sort_order=sideways&type_id=abc"
    ):
        response = app_module.admin_history_export()
        _ = response.get_data(as_text=True)

    assert calls
    query, params = calls[0]
    assert "ORDER BY r.id DESC, r.id DESC" in query
    assert params == [app_module.STATUS_DONE, app_module.STATUS_CANCELLED, 7]
