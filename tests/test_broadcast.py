from urllib.parse import unquote


def test_broadcast_page_requires_audit_admin(app_module, monkeypatch):
    monkeypatch.setattr(app_module.admin_routes, "is_audit_admin_authenticated", lambda: False)
    with app_module.app.test_request_context("/admin/broadcast"):
        response = app_module.admin_broadcast_page()

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_broadcast_send_requires_typed_confirmation(app_module, monkeypatch):
    monkeypatch.setattr(app_module.admin_routes, "is_audit_admin_authenticated", lambda: True)
    send_calls = []
    monkeypatch.setattr(
        app_module.admin_routes,
        "send_broadcast_message",
        lambda message: send_calls.append(message),
    )
    with app_module.app.test_request_context(
        "/admin/broadcast/send",
        method="POST",
        data={"message": "緊急のお知らせ", "confirmation": "送信"},
    ):
        response = app_module.admin_broadcast_send()

    assert response.status_code == 302
    assert "確認欄に" in unquote(response.headers["Location"])
    assert send_calls == []


def test_broadcast_send_sends_after_confirmation(app_module, monkeypatch):
    monkeypatch.setattr(app_module.admin_routes, "is_audit_admin_authenticated", lambda: True)
    send_calls = []
    monkeypatch.setattr(
        app_module.admin_routes,
        "send_broadcast_message",
        lambda message: send_calls.append(message),
    )
    with app_module.app.test_request_context(
        "/admin/broadcast/send",
        method="POST",
        data={"message": "緊急のお知らせ", "confirmation": "緊急送信"},
    ):
        response = app_module.admin_broadcast_send()

    assert response.status_code == 302
    assert "ブロードキャスト送信を受け付けました" in unquote(response.headers["Location"])
    assert send_calls == ["緊急のお知らせ"]
