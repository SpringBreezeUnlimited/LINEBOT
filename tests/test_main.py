from datetime import datetime
from types import SimpleNamespace

import pytz
import pytest
from werkzeug.exceptions import Forbidden


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
    utc = pytz.utc.localize(datetime(2026, 4, 16, 0, 0, 0))
    assert app_module.format_dt(utc) == "04-16 09:00"


def test_should_run_call_batch(app_module):
    assert app_module.should_run_call_batch(SimpleNamespace(tm_min=10)) is True
    assert app_module.should_run_call_batch(SimpleNamespace(tm_min=11)) is False


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


def test_validate_csrf_success(app_module):
    with app_module.app.test_request_context("/dummy", method="POST", data={"_csrf_token": "abc"}):
        app_module.session["_csrf_token"] = "abc"
        app_module.validate_csrf()


def test_validate_csrf_failure(app_module):
    with app_module.app.test_request_context("/dummy", method="POST", data={"_csrf_token": "wrong"}):
        app_module.session["_csrf_token"] = "abc"
        with pytest.raises(Forbidden):
            app_module.validate_csrf()


def test_is_authenticated_as_success(app_module):
    with app_module.app.test_request_context("/"):
        now = 1000.0
        app_module.session["logged_in"] = True
        app_module.session["admin_role"] = app_module.ROLE_ADMIN
        app_module.session["last_activity"] = now
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(app_module.time, "time", lambda: now + 10)
            assert app_module.is_authenticated_as(app_module.ROLE_ADMIN) is True


def test_is_authenticated_as_timeout_clears_session(app_module):
    with app_module.app.test_request_context("/"):
        now = 1000.0
        app_module.session["logged_in"] = True
        app_module.session["admin_role"] = app_module.ROLE_ADMIN
        app_module.session["last_activity"] = now
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(app_module.time, "time", lambda: now + app_module.SESSION_IDLE_TIMEOUT_SECONDS + 1)
            assert app_module.is_authenticated_as(app_module.ROLE_ADMIN) is False
        assert app_module.session.get("logged_in") is None


def test_apply_security_headers_admin_page(app_module):
    app_module.FORCE_HTTPS = True
    with app_module.app.test_request_context("/admin", headers={"X-Forwarded-Proto": "https"}):
        response = app_module.app.response_class("ok")
        result = app_module.apply_security_headers(response)
        assert "Content-Security-Policy" in result.headers
        assert result.headers.get("X-Frame-Options") == "DENY"
        assert "Strict-Transport-Security" in result.headers
        assert "no-store" in result.headers.get("Cache-Control", "")


def test_is_login_rate_limited_on_exception_returns_false(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("db error")))
    assert app_module.is_login_rate_limited("127.0.0.1") is False


def test_record_login_failure_on_exception_does_not_raise(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("db error")))
    app_module.record_login_failure("127.0.0.1")


def test_is_webhook_rate_limited_on_exception_returns_false(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("db error")))
    assert app_module.is_webhook_rate_limited("127.0.0.1") is False


def test_login_get_ok(client):
    response = client.get("/login")
    assert response.status_code == 200


def test_login_post_admin_success_redirect(client, csrf_token, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_login_rate_limited", lambda _ip: False)
    monkeypatch.setattr(app_module, "verify_admin_password", lambda _pwd: True)
    monkeypatch.setattr(app_module, "verify_audit_admin_password", lambda _pwd: False)
    monkeypatch.setattr(app_module, "record_admin_login", lambda *args, **kwargs: None)

    response = client.post("/login", data={"password": "admin-pass", "_csrf_token": csrf_token})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin")


def test_login_post_audit_success_redirect(client, csrf_token, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_login_rate_limited", lambda _ip: False)
    monkeypatch.setattr(app_module, "verify_admin_password", lambda _pwd: False)
    monkeypatch.setattr(app_module, "verify_audit_admin_password", lambda _pwd: True)
    monkeypatch.setattr(app_module, "record_admin_login", lambda *args, **kwargs: None)

    response = client.post("/login", data={"password": "audit-pass", "_csrf_token": csrf_token})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/login-logs")


def test_login_post_failure_shows_error(client, csrf_token, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_login_rate_limited", lambda _ip: False)
    monkeypatch.setattr(app_module, "verify_admin_password", lambda _pwd: False)
    monkeypatch.setattr(app_module, "verify_audit_admin_password", lambda _pwd: False)
    monkeypatch.setattr(app_module, "record_admin_login", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "record_login_failure", lambda _ip: None)

    response = client.post("/login", data={"password": "wrong", "_csrf_token": csrf_token})
    assert response.status_code == 200
    assert "パスワードが正しくありません" in response.get_data(as_text=True)


def test_login_post_rate_limited(client, csrf_token, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_login_rate_limited", lambda _ip: True)
    response = client.post("/login", data={"password": "x", "_csrf_token": csrf_token})
    assert response.status_code == 429


def test_admin_data_unauthorized(client):
    response = client.get("/admin/data")
    assert response.status_code == 401


def test_process_call_queue_task_without_token_returns_503(client, app_module):
    app_module.BATCH_CALL_RUNNER_TOKEN = ""
    response = client.post("/tasks/process-call-queue")
    assert response.status_code == 503


def test_process_call_queue_task_invalid_token_returns_403(client, app_module, monkeypatch):
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


def test_should_run_call_batch_uses_localtime_when_now_none(app_module, monkeypatch):
    monkeypatch.setattr(app_module.time, "localtime", lambda: SimpleNamespace(tm_min=15))
    assert app_module.should_run_call_batch() is True


def test_process_queued_calls_not_due_returns_early(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "should_run_call_batch", lambda _now: False)
    now = pytz.timezone("Asia/Tokyo").localize(datetime(2026, 4, 16, 10, 1))
    result = app_module.process_queued_calls(now=now)
    assert result["processed"] is False
    assert result["reason"] == "not_due"
