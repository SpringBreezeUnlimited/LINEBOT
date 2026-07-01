"""save_type_image_upload / reservation_type_image のテスト。

これらは main.py の中でも未カバーだった領域:
- 拒否されるべき拡張子・空ファイル・破損画像データの扱い（save_type_image_upload）
- 画像配信用の公開エンドポイント /reservation-type-images/<id>（reservation_type_image）
"""

from io import BytesIO

import pytest


# ---------------------------------------------------------------------------
# save_type_image_upload: 異常系・境界値
# ---------------------------------------------------------------------------


def test_save_type_image_upload_returns_empty_when_filename_missing(app_module):
    buf = BytesIO(b"irrelevant")
    buf.filename = ""

    data, mimetype, filename = app_module.save_type_image_upload(buf)

    assert (data, mimetype, filename) == (b"", "", "")


def test_save_type_image_upload_returns_empty_when_file_body_empty(app_module):
    buf = BytesIO(b"")
    buf.filename = "empty.png"

    data, mimetype, filename = app_module.save_type_image_upload(buf)

    assert (data, mimetype, filename) == (b"", "", "")


@pytest.mark.parametrize(
    "filename",
    ["malware.exe", "script.svg", "archive.zip", "noext", "shell.php.png.php"],
)
def test_save_type_image_upload_rejects_disallowed_extensions(app_module, filename):
    buf = BytesIO(b"fake-bytes")
    buf.filename = filename

    with pytest.raises(ValueError):
        app_module.save_type_image_upload(buf)


def test_save_type_image_upload_rejects_corrupt_image_data(app_module):
    # 拡張子は許可されているが、中身が画像として読めないデータ
    buf = BytesIO(b"this is not a real image file")
    buf.filename = "fake.png"

    with pytest.raises(ValueError):
        app_module.save_type_image_upload(buf)


def test_save_type_image_upload_handles_animated_gif(app_module):
    from PIL import Image

    frame1 = Image.new("RGB", (100, 50), color="red")
    frame2 = Image.new("RGB", (100, 50), color="blue")
    buf = BytesIO()
    frame1.save(buf, format="GIF", save_all=True, append_images=[frame2], loop=0)
    buf.seek(0)
    buf.filename = "anim.gif"

    data, mimetype, filename = app_module.save_type_image_upload(buf)

    # アニメーションは静止画（先頭フレーム）として保存され、
    # Flex Message で安全な拡張子(jpg/png)に正規化される
    assert data
    assert mimetype in {"image/jpeg", "image/png"}
    assert filename.endswith((".jpg", ".png"))


def test_save_type_image_upload_uses_secure_filename_for_stem(app_module):
    from PIL import Image

    image = Image.new("RGB", (10, 10), color="green")
    buf = BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)
    buf.filename = "../../etc/passwd.png"

    data, mimetype, filename = app_module.save_type_image_upload(buf)

    assert data
    assert "/" not in filename and ".." not in filename


# ---------------------------------------------------------------------------
# /reservation-type-images/<type_id> : 公開の画像配信エンドポイント
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        assert "FROM reservation_types" in query

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _FakeCursor(self._row)


def test_reservation_type_image_returns_stored_binary(app_module, client, monkeypatch):
    row = (b"binary-image-data", "image/png", None)
    monkeypatch.setattr(app_module, "get_connection", lambda: _FakeConnection(row))

    response = client.get("/reservation-type-images/1")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.get_data() == b"binary-image-data"


def test_reservation_type_image_returns_404_when_type_missing(
    app_module, client, monkeypatch
):
    monkeypatch.setattr(app_module, "get_connection", lambda: _FakeConnection(None))

    response = client.get("/reservation-type-images/999")

    assert response.status_code == 404


def test_reservation_type_image_returns_404_when_no_image_data_or_path(
    app_module, client, monkeypatch
):
    row = (None, None, None)
    monkeypatch.setattr(app_module, "get_connection", lambda: _FakeConnection(row))

    response = client.get("/reservation-type-images/2")

    assert response.status_code == 404


def test_reservation_type_image_falls_back_to_legacy_path_missing_file(
    app_module, client, monkeypatch
):
    # image_data は無いが legacy な image_path が指すファイルが実在しないケース
    row = (None, None, "img/does-not-exist.png")
    monkeypatch.setattr(app_module, "get_connection", lambda: _FakeConnection(row))

    response = client.get("/reservation-type-images/3")

    assert response.status_code == 404