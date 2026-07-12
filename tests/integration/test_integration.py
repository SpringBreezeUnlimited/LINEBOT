import base64
import hashlib
import hmac
import threading
from types import SimpleNamespace


def _line_signature(secret: str, body: str) -> str:
    mac = hmac.new(
        secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(mac).decode("utf-8")


def test_callback_accepts_valid_signature_and_rejects_fake(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_webhook_rate_limited", lambda _ip: False)
    monkeypatch.setattr(app_module, "ensure_database_schema", lambda: None)
    monkeypatch.setattr(app_module, "enforce_host_allowlist", lambda: None)
    monkeypatch.setattr(app_module, "enforce_https", lambda: None)
    app_module.app.config["TESTING"] = True

    body = '{"destination":"U1234567890","events":[]}'
    valid_signature = _line_signature("test-channel-secret", body)

    with app_module.app.test_client() as client:
        ok = client.post(
            "/callback",
            data=body,
            content_type="application/json",
            headers={"X-Line-Signature": valid_signature},
        )
        ng = client.post(
            "/callback",
            data=body,
            content_type="application/json",
            headers={"X-Line-Signature": "fake-signature"},
        )

    assert ok.status_code == 200
    assert ok.get_data(as_text=True) == "OK"
    assert ng.status_code == 400


def test_admin_call_concurrent_requests_only_one_succeeds(app_module, monkeypatch):
    app_module.app.config["TESTING"] = True
    monkeypatch.setattr(app_module, "ensure_database_schema", lambda: None)
    monkeypatch.setattr(app_module, "enforce_host_allowlist", lambda: None)
    monkeypatch.setattr(app_module, "enforce_https", lambda: None)
    monkeypatch.setattr(
        app_module, "is_admin_authenticated", lambda update_activity=True: True
    )
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module, "validate_csrf", lambda: None)

    state = {
        "status": app_module.STATUS_WAITING,
        "user_id": "U-TEST",
    }
    lock = threading.Lock()

    class FakeCursor:
        def __init__(self):
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            normalized_query = " ".join(query.split())
            if (
                normalized_query.startswith("UPDATE reservations SET status = %s, called_at = CURRENT_TIMESTAMP, call_origin = %s") or
                normalized_query.startswith("UPDATE reservations r SET status = %s, called_at = CURRENT_TIMESTAMP, call_origin = %s")
            ):
                with lock:
                    new_status, _call_origin, _res_id, expected_status, _owner_admin_id = params
                    if state["status"] == expected_status:
                        state["status"] = new_status
                        self._last = (state["user_id"], 1)
                    else:
                        self._last = None
            elif normalized_query.startswith(
                "SELECT status, COALESCE(reservation_no, id) FROM reservations WHERE id = %s"
            ) or normalized_query.startswith(
                "SELECT status FROM reservations WHERE id = %s"
            ):
                with lock:
                    self._last = (state["status"], 1)
            elif normalized_query.startswith(
                "UPDATE reservations SET status = %s, called_at = NULL, call_origin = NULL"
            ):
                with lock:
                    rollback_status, _res_id, expected_status = params
                    if state["status"] == expected_status:
                        state["status"] = rollback_status
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

    push_calls = []

    monkeypatch.setattr(
        app_module,
        "send_push_message",
        lambda user_id, _text: push_calls.append(user_id),
    )

    statuses = []
    barrier = threading.Barrier(2)

    def _hit_once():
        with app_module.app.test_client() as client:
            barrier.wait()
            response = client.post("/admin/call/1", data={"_csrf_token": "x"})
            statuses.append(response.status_code)

    t1 = threading.Thread(target=_hit_once)
    t2 = threading.Thread(target=_hit_once)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(statuses) == [302, 404]
    assert len(push_calls) == 1
    assert state["status"] == app_module.STATUS_CALLED
