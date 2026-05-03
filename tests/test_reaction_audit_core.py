from __future__ import annotations

import sqlite3

import pytest

from hermes_discord_skill_audit import reaction_audit as core
from hermes_discord_skill_audit import state


def test_core_should_delete_turn_review_for_same_emoji_from_any_message_in_turn():
    review = {"discord_message_id": "1001", "emoji": "✅"}

    assert core.should_delete_turn_review_on_remove(
        payload_message_id=1001,
        reaction_emoji="✅",
        existing_review=review,
    )
    assert core.should_delete_turn_review_on_remove(
        payload_message_id=1002,
        reaction_emoji="✅",
        existing_review=review,
    )
    assert not core.should_delete_turn_review_on_remove(
        payload_message_id=1001,
        reaction_emoji="❌",
        existing_review=review,
    )
    assert not core.should_delete_turn_review_on_remove(
        payload_message_id=1001,
        reaction_emoji="✅",
        existing_review=None,
    )


def test_core_move_existing_user_review_to_message_updates_only_matching_turn_user_emoji(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "skill_audit.db"
    monkeypatch.setattr(state, "SKILL_AUDIT_DB_PATH", db_path)
    monkeypatch.setattr(state, "DATA_DIR", tmp_path)
    core.ensure_skill_audit_db()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO reaction_skill_audits (
              discord_message_id, reacted_by_user_id, emoji, user_review_score,
              session_id, turn_id, raw_report_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("1001", "123", "✅", 0, "session-1", "session-1:44", "{}", 1.0),
        )
        conn.execute(
            """
            INSERT INTO reaction_skill_audits (
              discord_message_id, reacted_by_user_id, emoji, user_review_score,
              session_id, turn_id, raw_report_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2001", "123", "❌", 1, "session-1", "session-1:44", "{}", 1.0),
        )
        conn.commit()
    finally:
        conn.close()

    updated = core.move_existing_user_review_to_message(
        turn_id="session-1:44",
        reacted_by_user_id=123,
        message_id=1002,
        emoji="✅",
    )

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT discord_message_id, emoji FROM reaction_skill_audits ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert updated == 1
    assert rows == [("1002", "✅"), ("2001", "❌")]


def test_register_reaction_audit_handlers_exposes_discord_event_callbacks():
    class DummyBot:
        def __init__(self):
            self.handlers = {}
            self.user = None

        def event(self, fn):
            self.handlers[fn.__name__] = fn
            return fn

    bot = DummyBot()
    config = core.ReactionAuditConfig(
        hermes_agent_user_id=1492290496222072925,
        turn_map_db_path="/tmp/turn-map.db",
        hermes_state_db_path="/tmp/state.db",
        skill_audit_db_path="/tmp/audit.db",
    )

    core.register_reaction_audit_handlers(bot, config=config)

    assert "on_raw_reaction_add" in bot.handlers
    assert "on_raw_reaction_remove" in bot.handlers
    assert callable(bot.handlers["on_raw_reaction_add"])
    assert callable(bot.handlers["on_raw_reaction_remove"])
