"""Admin route helper functions extracted from admin_routes for smaller route modules."""

from __future__ import annotations

from config import STATUS_WAITING, STATUS_CALLED
from formatting import format_dt
from services.queue_service import fmt_no, format_call_origin


def serialize_active_rows(rows):
    return [
        {
            "id": row[0],
            "display_no": fmt_no(row[1]) if isinstance(row[1], int) else row[1],
            "status": row[2],
            "type_id": row[3],
            "type": row[4],
            "created_at": format_dt(row[5]),
            "call_origin": row[6],
            "call_origin_label": format_call_origin(row[6]) if row[6] else "",
        }
        for row in rows
    ]


def fetch_type_counts(cur, owner_admin_id: int):
    cur.execute(
        """
            SELECT t.name, COUNT(*)
            FROM reservations r
            JOIN reservation_types t ON r.type_id = t.id
                        WHERE r.status IN (%s, %s)
              AND COALESCE(r.owner_admin_id, t.owner_admin_id) = %s
            GROUP BY t.name
            ORDER BY COUNT(*) DESC, t.name ASC
        """,
        (STATUS_WAITING, STATUS_CALLED, owner_admin_id),
    )
    return cur.fetchall()


def serialize_type_counts(rows):
    return [{"name": row[0], "count": row[1]} for row in rows]


def get_admin_login_log_rows(cur, limit: int = 500):
    cur.execute(
        """
            SELECT id, login_result, admin_role, admin_login_id, ip_address, user_agent, logged_in_at
            FROM admin_login_logs
            ORDER BY logged_in_at DESC, id DESC
            LIMIT %s
        """,
        (limit,),
    )
    rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "login_result": row[1],
            "admin_role": row[2],
            "admin_login_id": row[3],
            "ip_address": row[4],
            "user_agent": row[5],
            "logged_in_at": format_dt(row[6]),
        }
        for row in rows
    ]


def get_active_rows(
    cur, owner_admin_id: int, current_type_id=None, sort_by="id", sort_order="asc"
):
    params = [STATUS_WAITING, STATUS_CALLED]
    where = "WHERE r.status IN (%s, %s) AND COALESCE(r.owner_admin_id, t.owner_admin_id) = %s"
    params.append(owner_admin_id)
    if current_type_id is not None:
        where += " AND r.type_id = %s"
        params.append(current_type_id)
    order_map = {
        "id": "COALESCE(r.reservation_no, r.id)",
        "status": "r.status",
        "type": "t.name",
    }
    order_by = order_map[sort_by]
    cur.execute(
        f"""
            SELECT r.id, COALESCE(r.reservation_no, r.id), r.status, t.id, t.name, r.created_at, r.call_origin
            FROM reservations r
            LEFT JOIN reservation_types t ON r.type_id = t.id
            {where}
            ORDER BY {order_by} {sort_order.upper()}, r.id ASC
        """,
        params,
    )
    return cur.fetchall()
