from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

LOGGER = logging.getLogger("discord-turn-map")

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = os.getenv("ENV_FILE", ".env")
load_dotenv(PROJECT_DIR / ENV_FILE)

DATA_DIR = PROJECT_DIR / "data"


def _resolve_env_path(env_val: str, default_rel: str) -> Path:
    p = Path(os.getenv(env_val, default_rel)).expanduser()
    if not p.is_absolute():
        p = PROJECT_DIR / p
    return p.resolve()


DB_PATH = _resolve_env_path("DISCORD_TURN_MAP_DB_PATH", "data/discord_turn_map.db")
SESSIONS_JSON = Path(
    os.getenv(
        "HERMES_SESSIONS_JSON_PATH",
        str(Path.home() / ".hermes" / "sessions" / "sessions.json"),
    )
).expanduser()
STATE_DB = Path(
    os.getenv("HERMES_STATE_DB_PATH", str(Path.home() / ".hermes" / "state.db"))
).expanduser()

_PATCH_LOCK = threading.Lock()
_PATCHED = False
_ORIGINAL_SEND = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS discord_message_turn_map (
  discord_message_id TEXT PRIMARY KEY,
  session_key TEXT,
  session_id TEXT,
  thread_id TEXT,
  chat_id TEXT,
  platform TEXT NOT NULL DEFAULT 'discord',
  turn_id TEXT,
  assistant_db_id INTEGER,
  reply_to_message_id TEXT,
  is_first_chunk INTEGER NOT NULL DEFAULT 0,
  chunk_index INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  resolution_source TEXT,
  last_error TEXT,
  sent_at REAL NOT NULL,
  resolved_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dmtm_status
ON discord_message_turn_map(status);

CREATE INDEX IF NOT EXISTS idx_dmtm_turn_id
ON discord_message_turn_map(turn_id);

CREATE INDEX IF NOT EXISTS idx_dmtm_session_id
ON discord_message_turn_map(session_id);

CREATE INDEX IF NOT EXISTS idx_dmtm_thread_id
ON discord_message_turn_map(thread_id);

CREATE INDEX IF NOT EXISTS idx_dmtm_chat_id
ON discord_message_turn_map(chat_id);

CREATE INDEX IF NOT EXISTS idx_dmtm_reply_to
ON discord_message_turn_map(reply_to_message_id);

CREATE INDEX IF NOT EXISTS idx_dmtm_assistant_db_id
ON discord_message_turn_map(assistant_db_id);

CREATE INDEX IF NOT EXISTS idx_dmtm_sent_at
ON discord_message_turn_map(sent_at);
"""


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _ensure_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        table_exists = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='discord_message_turn_map'"
            ).fetchone()
            is not None
        )

        if not table_exists:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
            return

        cols = _column_names(conn, "discord_message_turn_map")
        required = {"status", "sent_at", "resolved_at", "resolution_source"}
        if not required.issubset(cols):
            # Migrate old schema in place.
            conn.execute(
                "ALTER TABLE discord_message_turn_map RENAME TO discord_message_turn_map_old"
            )
            conn.executescript(SCHEMA_SQL)
            old_cols = _column_names(conn, "discord_message_turn_map_old")
            copyable = [
                c
                for c in [
                    "discord_message_id",
                    "session_key",
                    "session_id",
                    "thread_id",
                    "chat_id",
                    "platform",
                    "turn_id",
                    "assistant_db_id",
                    "reply_to_message_id",
                    "is_first_chunk",
                    "chunk_index",
                    "created_at",
                    "updated_at",
                ]
                if c in old_cols
            ]
            if copyable:
                src = ", ".join(copyable)
                now = time.time()
                conn.execute(
                    f"""
                    INSERT INTO discord_message_turn_map (
                      {src}, status, sent_at, resolved_at, resolution_source
                    )
                    SELECT
                      {src},
                      CASE WHEN assistant_db_id IS NULL OR turn_id IS NULL THEN 'pending' ELSE 'resolved' END,
                      COALESCE(created_at, ?),
                      CASE WHEN assistant_db_id IS NULL OR turn_id IS NULL THEN NULL ELSE COALESCE(updated_at, ?) END,
                      CASE WHEN assistant_db_id IS NULL OR turn_id IS NULL THEN NULL ELSE 'migrated' END
                    FROM discord_message_turn_map_old
                    """,
                    (now, now),
                )
            conn.execute("DROP TABLE discord_message_turn_map_old")
        else:
            conn.executescript(SCHEMA_SQL)

        conn.commit()
    finally:
        conn.close()


def _load_session_id(session_key: str) -> str | None:
    if not session_key or not SESSIONS_JSON.exists():
        return None
    try:
        data = json.loads(SESSIONS_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Failed to read %s: %s", SESSIONS_JSON, exc)
        return None
    entry = data.get(session_key) or {}
    session_id = entry.get("session_id")
    return str(session_id) if session_id else None


def _get_message_ids(result: Any) -> list[str]:
    ids: list[str] = []
    first_id = getattr(result, "message_id", None)
    if first_id:
        ids.append(str(first_id))
    raw = getattr(result, "raw_response", None)
    if isinstance(raw, dict):
        for msg_id in raw.get("message_ids") or []:
            msg_id = str(msg_id)
            if msg_id not in ids:
                ids.append(msg_id)
    return ids


def _find_assistant_message_exact(session_id: str, content: str | None) -> int | None:
    if not session_id or not STATE_DB.exists():
        return None
    normalized = (content or "").strip()
    if not normalized:
        return None

    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id
            FROM messages
            WHERE session_id = ?
              AND role = 'assistant'
              AND TRIM(content) = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id, normalized),
        ).fetchone()
        if row:
            return int(row["id"])
        return None
    finally:
        conn.close()


def _find_first_assistant_after_sent(
    session_id: str,
    sent_at: float,
    *,
    window_seconds: int = 180,
) -> int | None:
    if not session_id or not STATE_DB.exists():
        return None

    lower = float(sent_at) - 3.0
    upper = float(sent_at) + float(window_seconds)

    conn = sqlite3.connect(STATE_DB)
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
        if row:
            return int(row["id"])
        return None
    finally:
        conn.close()


def _find_mapped_session_by_message_id(
    discord_message_id: str | None,
) -> tuple[str | None, str | None]:
    if not discord_message_id or not DB_PATH.exists():
        return None, None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT session_key, session_id
            FROM discord_message_turn_map
            WHERE discord_message_id = ?
            ORDER BY CASE WHEN status='resolved' THEN 0 ELSE 1 END, updated_at DESC
            LIMIT 1
            """,
            (str(discord_message_id),),
        ).fetchone()
        if not row:
            return None, None
        return row["session_key"], row["session_id"]
    finally:
        conn.close()


def _find_recent_mapped_session(
    thread_id: str | None, chat_id: str | None
) -> tuple[str | None, str | None]:
    if not DB_PATH.exists():
        return None, None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if thread_id:
            row = conn.execute(
                """
                SELECT session_key, session_id
                FROM discord_message_turn_map
                WHERE thread_id = ?
                  AND session_id IS NOT NULL
                ORDER BY CASE WHEN status='resolved' THEN 0 ELSE 1 END, updated_at DESC
                LIMIT 1
                """,
                (str(thread_id),),
            ).fetchone()
            if row:
                return row["session_key"], row["session_id"]

        if chat_id:
            row = conn.execute(
                """
                SELECT session_key, session_id
                FROM discord_message_turn_map
                WHERE chat_id = ?
                  AND session_id IS NOT NULL
                ORDER BY CASE WHEN status='resolved' THEN 0 ELSE 1 END, updated_at DESC
                LIMIT 1
                """,
                (str(chat_id),),
            ).fetchone()
            if row:
                return row["session_key"], row["session_id"]

        return None, None
    finally:
        conn.close()


def _insert_pending(
    *,
    message_ids: Iterable[str],
    session_key: str,
    session_id: str,
    thread_id: str,
    chat_id: str,
    reply_to_message_id: str,
) -> None:
    now = time.time()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executemany(
            """
            INSERT INTO discord_message_turn_map (
              discord_message_id,
              session_key,
              session_id,
              thread_id,
              chat_id,
              platform,
              reply_to_message_id,
              is_first_chunk,
              chunk_index,
              status,
              sent_at,
              created_at,
              updated_at
            ) VALUES (?, ?, ?, ?, ?, 'discord', ?, ?, ?, 'pending', ?, ?, ?)
            ON CONFLICT(discord_message_id) DO UPDATE SET
              session_key = COALESCE(excluded.session_key, discord_message_turn_map.session_key),
              session_id = COALESCE(excluded.session_id, discord_message_turn_map.session_id),
              thread_id = COALESCE(excluded.thread_id, discord_message_turn_map.thread_id),
              chat_id = COALESCE(excluded.chat_id, discord_message_turn_map.chat_id),
              reply_to_message_id = COALESCE(excluded.reply_to_message_id, discord_message_turn_map.reply_to_message_id),
              is_first_chunk = excluded.is_first_chunk,
              chunk_index = excluded.chunk_index,
              sent_at = excluded.sent_at,
              updated_at = excluded.updated_at
            """,
            [
                (
                    msg_id,
                    session_key or None,
                    session_id or None,
                    thread_id or None,
                    chat_id or None,
                    reply_to_message_id or None,
                    1 if idx == 0 else 0,
                    idx,
                    now,
                    now,
                    now,
                )
                for idx, msg_id in enumerate(message_ids)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _resolve_ids(
    *,
    discord_message_ids: list[str],
    session_key: str,
    session_id: str,
    assistant_db_id: int,
    source: str,
) -> int:
    if not discord_message_ids:
        return 0
    now = time.time()
    turn_id = f"{session_id}:{assistant_db_id}"
    placeholders = ",".join("?" for _ in discord_message_ids)

    conn = sqlite3.connect(DB_PATH)
    try:
        params: list[Any] = [
            session_key or None,
            session_id,
            turn_id,
            assistant_db_id,
            source,
            now,
            now,
            *discord_message_ids,
        ]
        cur = conn.execute(
            f"""
            UPDATE discord_message_turn_map
            SET
              session_key = COALESCE(?, session_key),
              session_id = ?,
              turn_id = ?,
              assistant_db_id = ?,
              status = 'resolved',
              resolution_source = ?,
              resolved_at = ?,
              updated_at = ?
            WHERE discord_message_id IN ({placeholders})
            """,
            params,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _resolve_recent_pending(
    *,
    session_key: str,
    session_id: str,
    thread_id: str,
    chat_id: str,
    reply_to_message_id: str,
    assistant_db_id: int,
    source: str,
    lookback_seconds: int = 45,
) -> int:
    now = time.time()
    cutoff = now - float(lookback_seconds)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        where_parts: list[str] = ["status = 'pending'", "sent_at >= ?"]
        params: list[Any] = [cutoff]

        if thread_id:
            where_parts.append("thread_id = ?")
            params.append(thread_id)
        elif chat_id:
            where_parts.append("chat_id = ?")
            params.append(chat_id)

        if reply_to_message_id:
            where_parts.append(
                "(reply_to_message_id IS NULL OR reply_to_message_id = ?)"
            )
            params.append(reply_to_message_id)

        query = f"""
            SELECT discord_message_id
            FROM discord_message_turn_map
            WHERE {' AND '.join(where_parts)}
            ORDER BY sent_at ASC
        """
        rows = conn.execute(query, params).fetchall()
        message_ids = [str(r["discord_message_id"]) for r in rows]
    finally:
        conn.close()

    if not message_ids:
        return 0

    return _resolve_ids(
        discord_message_ids=message_ids,
        session_key=session_key,
        session_id=session_id,
        assistant_db_id=assistant_db_id,
        source=source,
    )


def _reconcile_pending_for_session(
    *,
    session_key: str,
    session_id: str,
    thread_id: str,
    chat_id: str,
    lookback_seconds: int = 900,
) -> int:
    if not session_id or not DB_PATH.exists():
        return 0

    cutoff = time.time() - float(lookback_seconds)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        where = ["status = 'pending'", "session_id = ?", "sent_at >= ?"]
        params: list[Any] = [session_id, cutoff]
        if thread_id:
            where.append("thread_id = ?")
            params.append(thread_id)
        elif chat_id:
            where.append("chat_id = ?")
            params.append(chat_id)

        rows = conn.execute(
            f"""
            SELECT discord_message_id, sent_at
            FROM discord_message_turn_map
            WHERE {' AND '.join(where)}
            ORDER BY sent_at ASC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    resolved = 0
    for row in rows:
        msg_id = str(row["discord_message_id"])
        sent_at = float(row["sent_at"])
        assistant_db_id = _find_first_assistant_after_sent(session_id, sent_at)
        if not assistant_db_id:
            continue
        resolved += _resolve_ids(
            discord_message_ids=[msg_id],
            session_key=session_key,
            session_id=session_id,
            assistant_db_id=assistant_db_id,
            source="deferred_timestamp",
        )
    return resolved


def _record_send_mapping(
    *,
    result: Any,
    chat_id: str,
    content: str,
    reply_to: str | None,
    metadata: dict[str, Any] | None,
) -> None:
    try:
        from gateway.session_context import get_session_env
    except Exception as exc:
        LOGGER.debug("session_context unavailable: %s", exc)
        return

    message_ids = _get_message_ids(result)
    if not message_ids:
        return

    meta = metadata or {}
    thread_id = str(
        meta.get("thread_id") or get_session_env("HERMES_SESSION_THREAD_ID", "") or ""
    )
    effective_chat_id = str(
        chat_id or get_session_env("HERMES_SESSION_CHAT_ID", "") or ""
    )
    reply_to_message_id = str(reply_to or "")

    session_key = str(get_session_env("HERMES_SESSION_KEY", "") or "")
    session_id = _load_session_id(session_key) if session_key else None

    if not session_id and reply_to_message_id:
        mapped_key, mapped_session_id = _find_mapped_session_by_message_id(
            reply_to_message_id
        )
        if mapped_session_id:
            session_key = session_key or str(mapped_key or "")
            session_id = mapped_session_id

    if not session_id:
        mapped_key, mapped_session_id = _find_recent_mapped_session(
            thread_id, effective_chat_id
        )
        if mapped_session_id:
            session_key = session_key or str(mapped_key or "")
            session_id = mapped_session_id

    if session_id:
        deferred_count = _reconcile_pending_for_session(
            session_key=session_key,
            session_id=str(session_id),
            thread_id=thread_id,
            chat_id=effective_chat_id,
        )
        if deferred_count:
            LOGGER.info(
                "Reconciled %s pending mapping row(s) for session_id=%s before current send",
                deferred_count,
                session_id,
            )

    _insert_pending(
        message_ids=message_ids,
        session_key=session_key,
        session_id=str(session_id or ""),
        thread_id=thread_id,
        chat_id=effective_chat_id,
        reply_to_message_id=reply_to_message_id,
    )

    if not session_id:
        LOGGER.info(
            "Recorded pending mapping for %s (session unresolved)",
            ",".join(message_ids),
        )
        return

    assistant_db_id = _find_assistant_message_exact(session_id, content)
    if not assistant_db_id:
        LOGGER.info(
            "Recorded pending mapping for %s (assistant unresolved; strict exact match)",
            ",".join(message_ids),
        )
        return

    # Resolve current chunk(s) + pending rows in the same short window.
    direct_count = _resolve_ids(
        discord_message_ids=message_ids,
        session_key=session_key,
        session_id=session_id,
        assistant_db_id=assistant_db_id,
        source="exact_content",
    )
    backfill_count = _resolve_recent_pending(
        session_key=session_key,
        session_id=session_id,
        thread_id=thread_id,
        chat_id=effective_chat_id,
        reply_to_message_id=reply_to_message_id,
        assistant_db_id=assistant_db_id,
        source="window_backfill",
    )

    LOGGER.info(
        "Mapped Discord message(s) %s -> turn_id=%s:%s (direct=%s, backfill=%s)",
        ",".join(message_ids),
        session_id,
        assistant_db_id,
        direct_count,
        backfill_count,
    )


def install_patch() -> bool:
    global _PATCHED, _ORIGINAL_SEND
    with _PATCH_LOCK:
        if _PATCHED:
            return False

        _ensure_db()
        try:
            from gateway.platforms.discord import DiscordAdapter
        except Exception as exc:
            LOGGER.warning("DiscordAdapter import failed: %s", exc)
            return False

        _ORIGINAL_SEND = DiscordAdapter.send

        async def wrapped_send(
            self,
            chat_id: str,
            content: str,
            reply_to: str | None = None,
            metadata: dict[str, Any] | None = None,
        ):
            result = await _ORIGINAL_SEND(
                self, chat_id, content, reply_to=reply_to, metadata=metadata
            )
            try:
                if getattr(result, "success", False):
                    _record_send_mapping(
                        result=result,
                        chat_id=chat_id,
                        content=content,
                        reply_to=reply_to,
                        metadata=metadata,
                    )
            except Exception:
                LOGGER.exception("Failed to record Discord turn mapping")
            return result

        DiscordAdapter.send = wrapped_send
        _PATCHED = True
        LOGGER.info(
            "Installed DiscordAdapter.send patch for two-phase message->turn mapping"
        )
        return True


def main() -> int:
    installed = install_patch()
    print(f"install_patch={installed} db={DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
