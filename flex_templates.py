from typing import Dict, Optional


def build_hero_image(url: str | None) -> Optional[Dict]:
    if not url:
        return None
    return {
        "type": "image",
        "url": url,
        "size": "full",
        "aspectRatio": "16:9",
        "aspectMode": "cover",
    }


def bubble_from_title_and_text(title: str, text: str, hero_url: str | None = None) -> Dict:
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": title, "weight": "bold", "size": "lg"}
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [{"type": "text", "text": text, "wrap": True}],
        },
    }


def ticket_status_card(
    title: str,
    reservation_no: int | str | None = None,
    lines: list[str] | None = None,
    hero_url: str | None = None,
) -> Dict:
    contents = [
        {"type": "text", "text": title, "align": "center", "weight": "bold", "size": "lg", "color": "#444444"}
    ]
    if reservation_no:
        no_str = f"{reservation_no:04d}" if isinstance(reservation_no, int) else str(reservation_no)
        contents.extend([
            {"type": "text", "text": "チケット番号", "align": "center", "weight": "bold", "size": "md", "color": "#444444", "margin": "lg"},
            {"type": "text", "text": no_str, "align": "center", "weight": "bold", "size": "4xl", "color": "#00A900", "margin": "sm"},
            {"type": "separator", "margin": "lg"},
        ])
    if lines:
        contents.append({"type": "text", "text": "\n".join(lines), "wrap": True, "size": "sm", "color": "#444444", "margin": "lg"})
    bubble = {"type": "bubble", "size": "mega", "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": contents}}
    hero = build_hero_image(hero_url)
    if hero:
        bubble["hero"] = hero
    return {"type": "flex", "altText": title + " - 通知", "contents": bubble}
    hero = build_hero_image(hero_url)
    if hero:
        bubble["hero"] = hero
    return {
        "type": "flex",
        "altText": title + " - 通知",
        "contents": bubble,
    }


def reservation_confirmation(
    reservation_no: int | str,
    type_name: str | None,
    owner_name: str | None,
    waiting: int,
    estimated_minutes: int,
    image_url: str | None = None,
) -> Dict:
    title = "受付完了"
    lines = [
        f"チケット番号: {reservation_no:04d}"
        if isinstance(reservation_no, int)
        else f"チケット番号: {reservation_no}"
    ]
    if type_name:
        lines.append(f"種類: {type_name}")
    if owner_name:
        lines.append(f"設定者: {owner_name}")
    lines.append(f"あなたの前: {waiting}人")
    lines.append(f"現在の目安待ち時間: {estimated_minutes}分")
    return ticket_status_card(title, reservation_no, lines[1:], hero_url=image_url)


def call_notification(
    reservation_no: int | str,
    timeout_label: str,
    call_minutes: int,
    shop_name: str = "admin",
    type_name: str | None = None,
) -> Dict:
    no_str = f"{reservation_no:04d}" if isinstance(reservation_no, int) else str(reservation_no)
    bubble = {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "text",
                    "text": "チケット番号",
                    "align": "center",
                    "weight": "bold",
                    "size": "lg",
                    "color": "#444444",
                },
                {
                    "type": "text",
                    "text": no_str,
                    "align": "center",
                    "weight": "bold",
                    "size": "4xl",
                    "color": "#00A900",
                    "margin": "lg",
                },
                {"type": "separator", "margin": "lg"},
                {
                    "type": "text",
                    "text": shop_name or "admin",
                    "align": "center",
                    "weight": "bold",
                    "size": "lg",
                    "color": "#444444",
                    "margin": "lg",
                    "maxLines": 1,
                    },
                *(
                    [
                        {
                            "type": "text",
                            "text": type_name,
                            "align": "center",
                            "weight": "bold",
                            "size": "lg",
                            "color": "#444444",
                            "margin": "none",
                            "maxLines": 1,
                        }
                    ]
                    if type_name
                    else []
                ),
                {
                    "type": "text",
                    "text": "ご用意ができました",
                    "align": "center",
                    "weight": "bold",
                    "size": "lg",
                    "color": "#444444",
                    "margin": "none",
                },
                {
                    "type": "text",
                    "text": (
                        f"{call_minutes}分以内（{timeout_label}まで）にお越しください。\n"
                        "時間を過ぎると自動でキャンセルされます。"
                    ),
                    "wrap": True,
                    "size": "sm",
                    "color": "#444444",
                    "margin": "xl",
                },
            ],
        },
    }
    return {"type": "flex", "altText": "ご用意ができました", "contents": bubble}


def wait_time_status(
    reservation_no: int | str | None,
    waiting: int,
    estimated_minutes: int,
    type_name: str | None = None,
) -> Dict:
    title = "現在の待ち時間"
    if reservation_no:
        no_str = f"{reservation_no:04d}" if isinstance(reservation_no, int) else str(reservation_no)
        line = f"チケット番号: {no_str} / あなたの前: {waiting}人"
    else:
        line = f"現在の待ち人数: {waiting}人"
    if type_name:
        line = f"{line} / 種類: {type_name}"
    detail_line = line.split(" / ", 1)[1] if reservation_no and " / " in line else line
    return ticket_status_card(title, reservation_no, [detail_line, f"目安: {estimated_minutes}分"])


def cancel_notification(reservation_no: int | str | None) -> Dict:
    title = "キャンセル完了"
    if reservation_no:
        no_str = f"{reservation_no:04d}" if isinstance(reservation_no, int) else str(reservation_no)
        body_text = f"キャンセルしたチケット番号: {no_str}"
    else:
        body_text = "キャンセルが完了しました。"
    return ticket_status_card(title, reservation_no, [body_text] if body_text else None)


def auto_cancel_notification(reservation_no: int | str) -> Dict:
    title = "自動キャンセル"
    no_str = f"{reservation_no:04d}" if isinstance(reservation_no, int) else str(reservation_no)
    body_text = f"チケット番号 {no_str} は時間切れのためキャンセルされました。"
    return ticket_status_card(title, reservation_no, ["時間切れのため自動キャンセルされました。"])
