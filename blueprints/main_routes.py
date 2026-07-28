"""パブリックルート（トップページ、favicon、予約種類の画像配信）。

注: url_for()の互換性を壊さないよう、ここではFlask Blueprintを使わず
素のビュー関数として定義し、main.py側でapp.add_url_rule()により
元のエンドポイント名（プレフィックスなし）で登録する。
Blueprintの@bp.route()はendpoint=を指定してもblueprint名がprefixされる
（例: "main_routes.reservation_type_image"）ため、テンプレート内の
url_for('reservation_type_image', ...)等が壊れてしまう。
"""
import io
import mimetypes
from pathlib import Path

from flask import abort, current_app, redirect, send_file, url_for, Response  # type: ignore

from database import get_connection


def reservation_type_image(type_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                    SELECT image_data, image_mime_type, image_path
                    FROM reservation_types
                    WHERE id = %s
                """,
                (type_id,),
            )
            row = cur.fetchone()
    if not row:
        abort(404)
    image_data, image_mime_type, image_path = row
    if image_data:
        return send_file(
            io.BytesIO(image_data),
            mimetype=image_mime_type or "application/octet-stream",
        )
    if image_path:
        legacy_path = Path(current_app.root_path) / "static" / image_path.lstrip("/")
        if legacy_path.is_file():
            mimetype, _ = mimetypes.guess_type(legacy_path.name)
            return send_file(
                legacy_path, mimetype=mimetype or "application/octet-stream"
            )
    abort(404)


def favicon():
    icon_path = Path(current_app.static_folder or "") / "favicon.ico"
    if icon_path.is_file():
        return send_file(icon_path, mimetype="image/x-icon")
    return Response(status=204, mimetype="image/x-icon")


def index():
    return redirect(url_for("login"))
