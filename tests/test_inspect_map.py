from __future__ import annotations

import sqlite3

import inspect_map


def _create_mapping_db(db_path):
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
              status TEXT NOT NULL DEFAULT 'pending',
              resolution_source TEXT,
              last_error TEXT,
              sent_at REAL NOT NULL,
              resolved_at REAL,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _create_state_db(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE messages (
              id INTEGER PRIMARY KEY,
              session_id TEXT NOT NULL,
              role TEXT NOT NULL,
              content TEXT,
              timestamp REAL NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_reconcile_pending_resolves_matching_assistant_row(tmp_path, monkeypatch):
    mapping_db = tmp_path / "discord_turn_map.db"
    state_db = tmp_path / "state.db"
    _create_mapping_db(mapping_db)
    _create_state_db(state_db)

    monkeypatch.setattr(inspect_map, "DB_PATH", mapping_db)
    monkeypatch.setattr(inspect_map, "STATE_DB", state_db)

    conn = sqlite3.connect(mapping_db)
    try:
        conn.execute(
            """
            INSERT INTO discord_message_turn_map (
              discord_message_id, session_key, session_id, thread_id, chat_id,
              turn_id, assistant_db_id, reply_to_message_id, is_first_chunk,
              chunk_index, status, resolution_source, last_error, sent_at,
              resolved_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "msg-1",
                "session-key",
                "session-1",
                "thread-1",
                "chat-1",
                None,
                None,
                None,
                1,
                0,
                "pending",
                None,
                None,
                1000.0,
                None,
                1000.0,
                1000.0,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(state_db)
    try:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (42, "session-1", "assistant", "final answer", 1005.0),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(inspect_map.time, "time", lambda: 1100.0)

    updated = inspect_map.reconcile_pending(window_seconds=180, lookback_seconds=3600)

    assert updated == 1

    conn = sqlite3.connect(mapping_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT turn_id, assistant_db_id, status, resolution_source FROM discord_message_turn_map WHERE discord_message_id = ?",
            ("msg-1",),
        ).fetchone()
    finally:
        conn.close()

    assert row["turn_id"] == "session-1:42"
    assert row["assistant_db_id"] == 42
    assert row["status"] == "resolved"
    assert row["resolution_source"] == "inspect_reconcile_timestamp"


def test_fetch_recent_filters_by_status_when_supported(tmp_path, monkeypatch):
    mapping_db = tmp_path / "discord_turn_map.db"
    _create_mapping_db(mapping_db)
    monkeypatch.setattr(inspect_map, "DB_PATH", mapping_db)

    conn = sqlite3.connect(mapping_db)
    try:
        conn.executemany(
            """
            INSERT INTO discord_message_turn_map (
              discord_message_id, session_key, session_id, thread_id, chat_id,
              turn_id, assistant_db_id, reply_to_message_id, is_first_chunk,
              chunk_index, status, resolution_source, last_error, sent_at,
              resolved_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "msg-pending",
                    "session-key",
                    "session-1",
                    "thread-1",
                    "chat-1",
                    None,
                    None,
                    None,
                    1,
                    0,
                    "pending",
                    None,
                    None,
                    1000.0,
                    None,
                    1000.0,
                    1000.0,
                ),
                (
                    "msg-resolved",
                    "session-key",
                    "session-1",
                    "thread-1",
                    "chat-1",
                    "session-1:42",
                    42,
                    None,
                    1,
                    0,
                    "resolved",
                    "send_exact",
                    None,
                    1001.0,
                    1002.0,
                    1001.0,
                    1002.0,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    rows = inspect_map.fetch_recent(limit=10, status="pending")

    assert len(rows) == 1
    assert rows[0]["discord_message_id"] == "msg-pending"
    assert rows[0]["status"] == "pending"
