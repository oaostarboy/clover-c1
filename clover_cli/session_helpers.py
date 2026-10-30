"""Bounded desktop helper expansion, shared by REST, RPC and project rows."""
import time


def session_helper_page(db, parent_id, *, limit=5, offset=0):
    limit = min(max(int(limit), 1), 99)
    offset = max(int(offset), 0)
    rows = db.list_session_helpers(parent_id, limit=limit + 1, offset=offset)
    has_more = len(rows) > limit
    rows = rows[:limit]
    now = time.time()
    for row in rows:
        row["helper_parent_id"] = parent_id
        row.pop("system_prompt", None)
        row.pop("model_config", None)
        row["is_active"] = row.get("ended_at") is None and now - row.get("last_active", 0) < 300
        row["archived"] = bool(row.get("archived"))
    return {"sessions": rows, "has_more": has_more, "limit": limit, "offset": offset}


def attach_session_helpers(db, rows):
    for row in rows:
        page = session_helper_page(db, row["id"])
        row["helpers"] = page["sessions"]
        row["helpers_has_more"] = page["has_more"]
    return rows
