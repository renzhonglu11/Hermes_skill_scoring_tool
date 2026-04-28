from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from examples import reaction_audit_bot as rab


def test_parse_int_set_skips_invalid_values():
    assert rab.parse_int_set("1, 2, abc, , 3") == {1, 2, 3}


def test_ensure_skill_audit_db_creates_table_and_migrates_column(tmp_path, monkeypatch):
    db_path = tmp_path / "skill_audit.db"
    monkeypatch.setattr(rab, "SKILL_AUDIT_DB_PATH", db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE reaction_skill_audits (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              discord_message_id TEXT NOT NULL,
              reacted_by_user_id TEXT NOT NULL,
              channel_id TEXT,
              guild_id TEXT,
              emoji TEXT,
              session_id TEXT,
              turn_id TEXT,
              assistant_db_id INTEGER,
              mapping_status TEXT,
              resolution_source TEXT,
              reply_to_message_id TEXT,
              previous_user_preview TEXT,
              skill_event_count INTEGER NOT NULL DEFAULT 0,
              skill_view_count INTEGER NOT NULL DEFAULT 0,
              skills_list_count INTEGER NOT NULL DEFAULT 0,
              skill_manage_count INTEGER NOT NULL DEFAULT 0,
              succeeded_count INTEGER NOT NULL DEFAULT 0,
              failed_count INTEGER NOT NULL DEFAULT 0,
              unknown_count INTEGER NOT NULL DEFAULT 0,
              raw_report_json TEXT NOT NULL,
              created_at REAL NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    rab.ensure_skill_audit_db()

    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(reaction_skill_audits)").fetchall()}
    finally:
        conn.close()

    assert "user_review_score" in cols


def test_get_message_ids_for_turn_returns_sorted_ids(tmp_path, monkeypatch):
    turn_map_db = tmp_path / "discord_turn_map.db"
    monkeypatch.setattr(rab, "TURN_MAP_DB_PATH", turn_map_db)

    conn = sqlite3.connect(turn_map_db)
    try:
        conn.executescript(
            """
            CREATE TABLE discord_message_turn_map (
              discord_message_id TEXT PRIMARY KEY,
              turn_id TEXT,
              updated_at REAL
            );
            INSERT INTO discord_message_turn_map (discord_message_id, turn_id, updated_at)
            VALUES
              ('1003', 'session-1:42', 3),
              ('1001', 'session-1:42', 1),
              ('1002', 'session-1:42', 2);
            """
        )
        conn.commit()
    finally:
        conn.close()

    assert rab.get_message_ids_for_turn("session-1:42") == [1001, 1002, 1003]


def test_get_skill_report_for_message_collects_skill_events(tmp_path, monkeypatch):
    turn_map_db = tmp_path / "discord_turn_map.db"
    state_db = tmp_path / "state.db"
    monkeypatch.setattr(rab, "TURN_MAP_DB_PATH", turn_map_db)
    monkeypatch.setattr(rab, "HERMES_STATE_DB_PATH", state_db)
    monkeypatch.setattr(rab, "TURN_MAP_DEFAULT_WINDOW_SECONDS", 180)
    monkeypatch.setattr(rab, "TURN_MAP_RESCUE_WINDOW_SECONDS", 600)

    conn = sqlite3.connect(turn_map_db)
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
            INSERT INTO discord_message_turn_map (
              discord_message_id, session_id, turn_id, assistant_db_id, status,
              resolution_source, sent_at, created_at, updated_at
            ) VALUES ('1001', 'session-1', 'session-1:44', 44, 'resolved', 'send_exact', 1000.0, 1000.0, 1000.0);
            """
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(state_db)
    try:
        conn.executescript(
            """
            CREATE TABLE messages (
              id INTEGER PRIMARY KEY,
              session_id TEXT NOT NULL,
              role TEXT NOT NULL,
              content TEXT,
              tool_calls TEXT,
              tool_call_id TEXT,
              tool_name TEXT,
              finish_reason TEXT,
              timestamp REAL NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, tool_calls, tool_call_id, tool_name, finish_reason, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (40, 'session-1', 'user', 'please inspect skills', None, None, None, None, 995.0),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, tool_calls, tool_call_id, tool_name, finish_reason, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                41,
                'session-1',
                'assistant',
                '',
                json.dumps([
                    {
                        "id": "call_1",
                        "function": {
                            "name": "skill_view",
                            "arguments": json.dumps({"name": "hermes-discord-message-turn-map-hook"}),
                        },
                    }
                ]),
                None,
                None,
                'tool_calls',
                996.0,
            ),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, tool_calls, tool_call_id, tool_name, finish_reason, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                44,
                'session-1',
                'assistant',
                'done',
                None,
                None,
                None,
                'stop',
                997.0,
            ),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, tool_calls, tool_call_id, tool_name, finish_reason, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (43, 'session-1', 'tool', '{"success": true}', None, 'call_1', 'skill_view', None, 996.5),
        )
        conn.commit()
    finally:
        conn.close()

    report = rab.get_skill_report_for_message(1001)

    assert report["turn_id"] == "session-1:44"
    assert report["assistant_db_id"] == 44
    assert report["function_counts"] == {"skill_view": 1}
    assert report["status_counts"] == {"succeeded": 1}
    assert report["events"][0]["target"] == "hermes-discord-message-turn-map-hook"


import pytest
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.mark.asyncio
async def test_on_raw_reaction_add_ignores_bot_itself():
    # Setup mock payload
    payload = MagicMock()
    payload.user_id = 999
    
    # Mock bot user
    bot_user_mock = MagicMock()
    bot_user_mock.id = 999
    
    with patch.object(rab, "bot") as mock_bot:
        mock_bot.user = bot_user_mock
        
        # Call the event
        await rab.on_raw_reaction_add(payload)
        
        # Ensure it returns early
        mock_bot.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_on_raw_reaction_add_processes_valid_reaction(monkeypatch):
    monkeypatch.setattr(rab, "REACTION_ALLOWED_USER_IDS", {123})
    payload = MagicMock()
    payload.user_id = 123
    payload.emoji = "✅"
    payload.channel_id = 456
    payload.message_id = 789
    payload.guild_id = 101112

    # Mock bot
    bot_user_mock = MagicMock()
    bot_user_mock.id = 999
    
    mock_channel = AsyncMock()
    mock_message = AsyncMock()
    mock_message.author.id = rab.HERMES_AGENT_USER_ID
    mock_channel.fetch_message.return_value = mock_message

    with patch.object(rab, "bot") as mock_bot, \
         patch.object(rab, "get_skill_report_for_message") as mock_get_report, \
         patch.object(rab, "get_existing_user_review_by_turn", return_value=None), \
         patch.object(rab, "persist_skill_audit_report", return_value=1) as mock_persist, \
         patch.object(rab, "sync_turn_reaction", new_callable=AsyncMock) as mock_sync:
        
        mock_bot.user = bot_user_mock
        mock_bot.get_channel.return_value = mock_channel
        mock_get_report.return_value = {"turn_id": "session-1:44", "message_id": "789"}
        
        await rab.on_raw_reaction_add(payload)

        # Assertions
        mock_persist.assert_called_once()
        mock_sync.assert_called_once_with(
            channel=mock_channel,
            origin_message_id=789,
            turn_id="session-1:44",
            emoji="✅",
            action="add"
        )


@pytest.mark.asyncio
async def test_on_raw_reaction_add_handles_existing_review(monkeypatch):
    monkeypatch.setattr(rab, "REACTION_ALLOWED_USER_IDS", {123})
    payload = MagicMock()
    payload.user_id = 123
    payload.emoji = "✅"
    payload.channel_id = 456
    payload.message_id = 789
    payload.guild_id = 101112
    payload.member = MagicMock()

    bot_user_mock = MagicMock()
    bot_user_mock.id = 999
    
    mock_channel = AsyncMock()
    mock_message = AsyncMock()
    mock_message.author.id = rab.HERMES_AGENT_USER_ID
    mock_channel.fetch_message.return_value = mock_message

    existing_review = {
        "discord_message_id": "1234",
        "turn_id": "session-1:44",
        "emoji": "❌",
        "user_review_score": 1,
    }

    with patch.object(rab, "bot") as mock_bot, \
         patch.object(rab, "get_skill_report_for_message") as mock_get_report, \
         patch.object(rab, "get_existing_user_review_by_turn", return_value=existing_review), \
         patch.object(rab, "remove_user_reaction", new_callable=AsyncMock, return_value=True) as mock_remove, \
         patch.object(rab, "persist_skill_audit_report") as mock_persist, \
         patch.object(rab, "sync_turn_reaction", new_callable=AsyncMock) as mock_sync:
        
        mock_bot.user = bot_user_mock
        mock_bot.get_channel.return_value = mock_channel
        mock_get_report.return_value = {"turn_id": "session-1:44", "message_id": "789"}
        
        await rab.on_raw_reaction_add(payload)

        # Assertions
        mock_persist.assert_not_called()
        mock_sync.assert_not_called()
        mock_remove.assert_called_once_with(
            message=mock_message,
            emoji="✅",
            member=payload.member
        )
        mock_channel.send.assert_called_once()


@pytest.mark.asyncio
async def test_on_raw_reaction_remove_processes_valid_reaction(monkeypatch):
    monkeypatch.setattr(rab, "REACTION_ALLOWED_USER_IDS", {123})
    payload = MagicMock()
    payload.user_id = 123
    payload.emoji = "✅"
    payload.channel_id = 456
    payload.message_id = 789
    payload.guild_id = 101112

    bot_user_mock = MagicMock()
    bot_user_mock.id = 999
    
    mock_channel = AsyncMock()
    
    with patch.object(rab, "bot") as mock_bot, \
         patch.object(rab, "get_skill_report_for_message") as mock_get_report, \
         patch.object(rab, "delete_skill_audit_reports_by_turn", return_value=1) as mock_delete, \
         patch.object(rab, "sync_turn_reaction", new_callable=AsyncMock) as mock_sync:
        
        mock_bot.user = bot_user_mock
        mock_bot.get_channel.return_value = mock_channel
        mock_get_report.return_value = {"turn_id": "session-1:44"}
        
        await rab.on_raw_reaction_remove(payload)

        # Assertions
        mock_delete.assert_called_once_with(
            turn_id="session-1:44",
            reacted_by_user_id=123,
            emoji="✅"
        )
        mock_sync.assert_called_once_with(
            channel=mock_channel,
            origin_message_id=789,
            turn_id="session-1:44",
            emoji="✅",
            action="remove"
        )

