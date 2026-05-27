from flex_templates import (
    reservation_confirmation,
    call_notification,
    wait_time_status,
    cancel_notification,
    auto_cancel_notification,
)


def test_reservation_confirmation_structure():
    d = reservation_confirmation(123, "相談", 3, 4)
    assert isinstance(d, dict)
    assert d.get("type") == "flex"
    assert "contents" in d


def test_call_notification_structure():
    d = call_notification(123, "15:30", 15)
    assert isinstance(d, dict)
    assert d.get("type") == "flex"


def test_wait_time_status_structure():
    d = wait_time_status(123, 2, 5, "相談")
    assert isinstance(d, dict)
    assert d.get("type") == "flex"


def test_cancel_and_auto_cancel():
    d1 = cancel_notification(12)
    d2 = auto_cancel_notification(13)
    assert d1.get("type") == "flex"
    assert d2.get("type") == "flex"
