from datetime import datetime
import time

import pytest
from linebot.v3.messaging import TextMessage


def test_parse_int_env_uses_default_for_invalid_and_clamps_bounds(app_module, monkeypatch):
    monkeypatch.setenv("TEST_INT", "not-an-int")
    assert app_module.parse_int_env("TEST_INT", 7, 1, 10) == 7

    monkeypatch.setenv("TEST_INT", "-5")
    assert app_module.parse_int_env("TEST_INT", 7, 1, 10) == 1

    monkeypatch.setenv("TEST_INT", "99")
    assert app_module.parse_int_env("TEST_INT", 7, 1, 10) == 10


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/admin?tab=history", "/admin?tab=history"),
        ("  /login  ", "/login"),
        ("https://evil.example/", None),
        ("//evil.example/", None),
        ("", None),
        (None, None),
    ],
)
def test_sanitize_next_path_allows_only_local_paths(app_module, value, expected):
    assert app_module.sanitize_next_path(value) == expected


@pytest.mark.parametrize(
    "hour, expected",
    [(9, 0), (10, 1), (11, 2), (12, 3), (13, 4), (14, 5), (15, 0)],
)
def test_hour_digit_is_limited_to_call_hours(app_module, hour, expected):
    assert app_module.hour_digit(datetime(2026, 7, 28, hour, 0)) == expected


@pytest.mark.parametrize(
    "minute, expected",
    [(0, True), (5, False), (55, False), (1, False), (59, False)],
)
def test_should_run_midnight_cancel_requires_exact_midnight(app_module, minute, expected):
    now = time.struct_time((2026, 7, 28, 0, minute, 0, 1, 209, 0))
    assert app_module.should_run_midnight_cancel(now) is expected


def test_build_line_message_converts_text_messages_and_none_safely(app_module):
    message = app_module.build_line_message(None)
    assert isinstance(message, TextMessage)
    assert message.text == ""

    message = app_module.build_line_message({"type": "text", "text": 123})
    assert isinstance(message, TextMessage)
    assert message.text == "123"


@pytest.mark.parametrize(
    "status, expected",
    [(None, True), (429, True), (500, True), (400, False), (404, False)],
)
def test_push_error_retry_policy_distinguishes_transient_failures(
    app_module, status, expected
):
    error = RuntimeError("failed")
    if status is not None:
        error.status_code = status
    assert app_module.is_retryable_push_error(error) is expected


def test_format_call_origin_has_safe_fallback_for_unknown_values(app_module):
    assert app_module.format_call_origin(app_module.CALL_ORIGIN_AUTO)
    assert app_module.format_call_origin("unexpected-origin") == "不明"
    assert app_module.format_call_origin(None) == "不明"
