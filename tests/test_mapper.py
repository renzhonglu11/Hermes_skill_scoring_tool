from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import mapper


class _DummyResult:
    def __init__(self, message_id=None, raw_response=None):
        self.message_id = message_id
        self.raw_response = raw_response


def test_get_message_ids_deduplicates_primary_and_chunk_ids():
    result = _DummyResult(
        message_id=111,
        raw_response={"message_ids": ["111", 222, "222", 333]},
    )

    assert mapper._get_message_ids(result) == ["111", "222", "333"]


def test_load_session_id_reads_session_id_from_sessions_json(tmp_path, monkeypatch):
    sessions_json = tmp_path / "sessions.json"
    sessions_json.write_text(
        '{"session-key": {"session_id": "session-123"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(mapper, "SESSIONS_JSON", sessions_json)

    assert mapper._load_session_id("session-key") == "session-123"


def test_ensure_db_migrates_legacy_schema_to_pending_and_resolved_rows(
    tmp_path, monkeypatch
):
    data_dir = tmp_path / "data"
    db_path = data_dir / "discord_turn_map.db"
    monkeypatch.setattr(mapper, "DATA_DIR", data_dir)
    monkeypatch.setattr(mapper, "DB_PATH", db_path)

    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE discord_message_turn_map (
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
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO discord_message_turn_map (
              discord_message_id, session_key, session_id, thread_id, chat_id,
              platform, turn_id, assistant_db_id, reply_to_message_id,
              is_first_chunk, chunk_index, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "msg-resolved",
                "session-key",
                "session-1",
                "thread-1",
                "chat-1",
                "discord",
                "session-1:42",
                42,
                None,
                1,
                0,
                1000.0,
                1001.0,
            ),
        )
        conn.execute(
            """
            INSERT INTO discord_message_turn_map (
              discord_message_id, session_key, session_id, thread_id, chat_id,
              platform, turn_id, assistant_db_id, reply_to_message_id,
              is_first_chunk, chunk_index, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "msg-pending",
                "session-key",
                "session-1",
                "thread-1",
                "chat-1",
                "discord",
                None,
                None,
                None,
                0,
                1,
                1002.0,
                1003.0,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    mapper._ensure_db()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT discord_message_id, status, resolution_source, sent_at, resolved_at FROM discord_message_turn_map ORDER BY discord_message_id"
        ).fetchall()
    finally:
        conn.close()

    by_id = {row["discord_message_id"]: row for row in rows}
    assert by_id["msg-pending"]["status"] == "pending"
    assert by_id["msg-pending"]["resolution_source"] is None
    assert by_id["msg-pending"]["resolved_at"] is None
    assert by_id["msg-pending"]["sent_at"] == 1002.0

    assert by_id["msg-resolved"]["status"] == "resolved"
    assert by_id["msg-resolved"]["resolution_source"] == "migrated"
    assert by_id["msg-resolved"]["resolved_at"] == 1001.0
    assert by_id["msg-resolved"]["sent_at"] == 1000.0
