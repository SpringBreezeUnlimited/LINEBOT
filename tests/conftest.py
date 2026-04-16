import importlib
import os
import re
import sys

import pytest
from werkzeug.security import generate_password_hash


@pytest.fixture(scope="session")
def app_module():
    os.environ["SECRET_KEY"] = "test-secret-key"
    os.environ["ADMIN_PASSWORD_HASH"] = generate_password_hash("admin-pass")
    os.environ["AUDIT_ADMIN_PASSWORD_HASH"] = generate_password_hash("audit-pass")
    os.environ["CHANNEL_ACCESS_TOKEN"] = "test-channel-access-token"
    os.environ["CHANNEL_SECRET"] = "test-channel-secret"
    os.environ["DATABASE_URL"] = "postgresql://user:pass@localhost:5432/testdb"
    os.environ["FORCE_HTTPS"] = "false"
    os.environ["SESSION_COOKIE_SECURE"] = "false"

    if "main" in sys.modules:
        del sys.modules["main"]
    module = importlib.import_module("main")
    return module


@pytest.fixture()
def client(app_module, monkeypatch):
    app_module.app.config["TESTING"] = True
    monkeypatch.setattr(app_module, "ensure_database_schema", lambda: None)
    monkeypatch.setattr(app_module, "enforce_host_allowlist", lambda: None)
    monkeypatch.setattr(app_module, "enforce_https", lambda: None)
    return app_module.app.test_client()


@pytest.fixture()
def csrf_token(client):
    response = client.get("/login")
    text = response.get_data(as_text=True)
    match = re.search(r'name="_csrf_token" value="([^"]+)"', text)
    assert match, "CSRF token not found in login page"
    return match.group(1)
