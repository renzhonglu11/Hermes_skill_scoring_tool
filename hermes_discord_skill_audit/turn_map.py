from __future__ import annotations

from collections import Counter
import json
import sqlite3
from datetime import datetime, timezone

from . import state

def get_message_ids_for_turn(turn_id: str) -> list[int]:
    if not state.TURN_MAP_DB_PATH.exists() or not turn_id:
        return []

    conn = sqlite3.connect(state.TURN_MAP_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT discord_message_id
            FROM discord_message_turn_map
            WHERE turn_id = ?
            ORDER BY CAST(discord_message_id AS INTEGER) ASC
            """,
            (str(turn_id),),
        ).fetchall()
        return [int(row["discord_message_id"]) for row in rows if str(row["discord_message_id"] or "").strip()]
    finally:
        conn.close()


def _fetch_turn_map_row(message_id: int) -> dict | None:
    if not state.TURN_MAP_DB_PATH.exists():
        return None

    conn = sqlite3.connect(state.TURN_MAP_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT *
            FROM discord_message_turn_map
            WHERE discord_message_id = ?
            ORDER BY CASE WHEN status = 'resolved' THEN 0 ELSE 1 END, updated_at DESC
            LIMIT 1
            """,
            (str(message_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _find_assistant_after_sent(
    session_id: str,
    sent_at: float,
    window_seconds: int = state.TURN_MAP_DEFAULT_WINDOW_SECONDS,
) -> int | None:
    if not session_id or not state.HERMES_STATE_DB_PATH.exists():
        return None

    lower = float(sent_at) - 3.0
    upper = float(sent_at) + float(window_seconds)

    conn = sqlite3.connect(state.HERMES_STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id
            FROM messages
            WHERE session_id = ?
              AND role = 'assistant'
              AND TRIM(COALESCE(content, '')) != ''
              AND timestamp >= ?
              AND timestamp <= ?
            ORDER BY timestamp ASC, id ASC
            LIMIT 1
            """,
            (session_id, lower, upper),
        ).fetchone()
        return int(row["id"]) if row else None
    finally:
        conn.close()


def _persist_turn_map_resolution(
    message_id: int,
    session_id: str,
    assistant_db_id: int,
    resolution_source: str,
) -> None:
    if not state.TURN_MAP_DB_PATH.exists():
        return

    now = datetime.now(timezone.utc).timestamp()
    conn = sqlite3.connect(state.TURN_MAP_DB_PATH)
    try:
        conn.execute(
            """
            UPDATE discord_message_turn_map
            SET assistant_db_id = ?,
                turn_id = ?,
                status = 'resolved',
                resolution_source = ?,
                resolved_at = COALESCE(resolved_at, ?),
                updated_at = ?
            WHERE discord_message_id = ?
            """,
            (
                assistant_db_id,
                f"{session_id}:{assistant_db_id}",
                resolution_source,
                now,
                now,
                str(message_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _resolve_turn_map_row(message_id: int, rescue_window_seconds: int | None = None) -> dict | None:
    row = _fetch_turn_map_row(message_id)
    if not row:
        return None

    if row.get("assistant_db_id") and row.get("turn_id"):
        return row

    session_id = str(row.get("session_id") or "")
    sent_at = row.get("sent_at")
    if not session_id or sent_at is None:
        return row

    assistant_db_id = _find_assistant_after_sent(session_id, float(sent_at), state.TURN_MAP_DEFAULT_WINDOW_SECONDS)
    resolution_source = "reaction_fallback_timestamp"

    if not assistant_db_id and rescue_window_seconds and rescue_window_seconds > state.TURN_MAP_DEFAULT_WINDOW_SECONDS:
        assistant_db_id = _find_assistant_after_sent(session_id, float(sent_at), rescue_window_seconds)
        if assistant_db_id:
            resolution_source = f"reaction_rescue_window_{int(rescue_window_seconds)}s"

    if not assistant_db_id:
        return row

    row["assistant_db_id"] = assistant_db_id
    row["turn_id"] = f"{session_id}:{assistant_db_id}"
    row["status"] = "resolved"
    row["resolution_source"] = resolution_source
    _persist_turn_map_resolution(message_id, session_id, assistant_db_id, resolution_source)
    return row


def _safe_json_loads(raw_value: str | None) -> dict | list | None:
    if not raw_value:
        return None
    try:
        return json.loads(raw_value)
    except Exception:
        return None


def _tool_result_status(raw_content: str | None) -> tuple[str, str | None]:
    parsed = _safe_json_loads(raw_content)
    if isinstance(parsed, dict):
        if parsed.get("success") is False:
            return "failed", str(parsed.get("error") or "unknown error")
        if parsed.get("error"):
            return "failed", str(parsed.get("error"))
        return "succeeded", None
    return "succeeded", None


def _skill_target_name(function_name: str, args: dict) -> str:
    if function_name in {"skill_view", "skill_manage"}:
        return str(args.get("name") or "(unknown)")
    if function_name == "skills_list":
        category = str(args.get("category") or "").strip()
        return f"category:{category}" if category else "(all skills)"
    return "(unknown)"


def get_skill_report_for_message(message_id: int) -> dict:
    map_row = _resolve_turn_map_row(message_id, rescue_window_seconds=state.TURN_MAP_RESCUE_WINDOW_SECONDS)
    if not map_row:
        raise RuntimeError(f"未找到 message_id={message_id} 的 turn 映射")

    session_id = str(map_row.get("session_id") or "")
    assistant_db_id = map_row.get("assistant_db_id")
    if not session_id or not assistant_db_id:
        raise RuntimeError(
            f"message_id={message_id} 已找到映射，但 turn 尚未解析完成（status={map_row.get('status') or 'unknown'}）"
        )

    if not state.HERMES_STATE_DB_PATH.exists():
        raise RuntimeError(f"Hermes state.db 不存在: {state.HERMES_STATE_DB_PATH}")

    assistant_db_id = int(assistant_db_id)
    conn = sqlite3.connect(state.HERMES_STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        user_row = conn.execute(
            """
            SELECT id, content, timestamp
            FROM messages
            WHERE session_id = ?
              AND role = 'user'
              AND id < ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id, assistant_db_id),
        ).fetchone()
        start_id = int(user_row["id"]) if user_row else assistant_db_id

        rows = conn.execute(
            """
            SELECT id, role, content, tool_calls, tool_call_id, tool_name, finish_reason, timestamp
            FROM messages
            WHERE session_id = ?
              AND id BETWEEN ? AND ?
            ORDER BY id ASC
            """,
            (session_id, start_id, assistant_db_id),
        ).fetchall()
    finally:
        conn.close()

    tool_results: dict[str, tuple[str, str | None]] = {}
    for row in rows:
        tool_call_id = row["tool_call_id"]
        if row["role"] == "tool" and tool_call_id:
            tool_results[str(tool_call_id)] = _tool_result_status(row["content"])

    events: list[dict] = []
    for row in rows:
        if not row["tool_calls"]:
            continue
        parsed = _safe_json_loads(row["tool_calls"])
        if not isinstance(parsed, list):
            continue

        for call in parsed:
            function = (call.get("function") or {}).get("name")
            if function not in {"skill_view", "skills_list", "skill_manage"}:
                continue

            raw_args = (call.get("function") or {}).get("arguments")
            args = _safe_json_loads(raw_args) if isinstance(raw_args, str) else raw_args
            if not isinstance(args, dict):
                args = {}

            call_id = str(call.get("call_id") or call.get("id") or "")
            status, error = tool_results.get(call_id, ("unknown", None))
            events.append(
                {
                    "message_db_id": int(row["id"]),
                    "function": function,
                    "target": _skill_target_name(function, args),
                    "status": status,
                    "error": error,
                }
            )

    status_counts = Counter(event["status"] for event in events)
    function_counts = Counter(event["function"] for event in events)

    return {
        "message_id": str(message_id),
        "turn_id": str(map_row.get("turn_id") or f"{session_id}:{assistant_db_id}"),
        "assistant_db_id": assistant_db_id,
        "session_id": session_id,
        "mapping_status": str(map_row.get("status") or "resolved"),
        "resolution_source": str(map_row.get("resolution_source") or "unknown"),
        "reply_to_message_id": str(map_row.get("reply_to_message_id") or ""),
        "previous_user_preview": str((user_row["content"] if user_row else "") or "").strip(),
        "events": events,
        "status_counts": dict(status_counts),
        "function_counts": dict(function_counts),
    }


