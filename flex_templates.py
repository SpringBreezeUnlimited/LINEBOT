from typing import Dict


def bubble_from_title_and_text(title: str, text: str) -> Dict:
    return {
        "type": "flex",
        "altText": title + " - 通知",
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [{"type": "text", "text": title, "weight": "bold", "size": "lg"}],
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [{"type": "text", "text": text, "wrap": True}],
            },
        },
    }


def reservation_confirmation(res_id: int, type_name: str | None, waiting: int, estimated_minutes: int) -> Dict:
    title = "受付完了"
    lines = [f"番号: {res_id}"]
    if type_name:
        lines.append(f"種類: {type_name}")
    lines.append(f"あなたの前: {waiting}人")
    lines.append(f"現在の目安待ち時間: {estimated_minutes}分")
    body_text = "\n".join(lines)
    return bubble_from_title_and_text(title, body_text)


def call_notification(res_id: int, timeout_label: str, call_minutes: int) -> Dict:
    title = "呼出中"
    body_text = (
        f"番号: {res_id}\n{call_minutes}分以内（{timeout_label}まで）にお越しください。"
        "\n時間を過ぎると自動でキャンセルされます。"
    )
    return bubble_from_title_and_text(title, body_text)


def wait_time_status(res_id: int | None, waiting: int, estimated_minutes: int, type_name: str | None = None) -> Dict:
    title = "現在の待ち時間"
    if res_id:
        line = f"番号: {res_id} / あなたの前: {waiting}人"
    else:
        line = f"現在の待ち人数: {waiting}人"
    if type_name:
        line = f"{line} / 種類: {type_name}"
    body_text = f"{line}\n目安: {estimated_minutes}分"
    return bubble_from_title_and_text(title, body_text)


def cancel_notification(res_id: int | None) -> Dict:
    title = "キャンセル完了"
    body_text = f"キャンセルした番号: {res_id}" if res_id else "キャンセルが完了しました。"
    return bubble_from_title_and_text(title, body_text)


def auto_cancel_notification(res_id: int) -> Dict:
    title = "自動キャンセル"
    body_text = f"番号 {res_id} は時間切れのためキャンセルされました。"
    return bubble_from_title_and_text(title, body_text)
