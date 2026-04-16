from types import SimpleNamespace

import pytest


def test_parse_bool_env_true_false_default(app_module, monkeypatch):
    monkeypatch.setenv("TEST_BOOL", "true")
    assert app_module.parse_bool_env("TEST_BOOL", False) is True

    monkeypatch.setenv("TEST_BOOL", "off")
    assert app_module.parse_bool_env("TEST_BOOL", True) is False

    monkeypatch.delenv("TEST_BOOL", raising=False)
    assert app_module.parse_bool_env("TEST_BOOL", True) is True


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


def test_should_run_call_batch(app_module):
    assert app_module.should_run_call_batch(SimpleNamespace(tm_min=10)) is True
    assert app_module.should_run_call_batch(SimpleNamespace(tm_min=11)) is False


def test_normalize_and_validate_type_name(app_module):
    assert app_module.normalize_type_name("  A   B  ") == "A B"
    assert app_module.validate_type_name("相談") is True
    assert app_module.validate_type_name("") is False


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


def test_login_get_ok(client):
    response = client.get("/login")
    assert response.status_code == 200


def test_login_post_rate_limited(client, csrf_token, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_login_rate_limited", lambda _ip: True)
    response = client.post("/login", data={"password": "x", "_csrf_token": csrf_token})
    assert response.status_code == 429


def test_admin_data_unauthorized(client):
    response = client.get("/admin/data")
    assert response.status_code == 401


def test_callback_missing_signature_returns_400(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_webhook_rate_limited", lambda _ip: False)
    response = client.post("/callback", data="{}", content_type="application/json")
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
