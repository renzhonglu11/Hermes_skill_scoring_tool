from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from . import state
from .scores import REACTION_SCORE_MAP, UserReviewScore


def ensure_dirs() -> None:
    state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    state.SKILL_AUDIT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def ensure_skill_audit_db() -> None:
    ensure_dirs()
    conn = sqlite3.connect(state.SKILL_AUDIT_DB_PATH)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reaction_skill_audits (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              discord_message_id TEXT NOT NULL,
              reacted_by_user_id TEXT NOT NULL,
              channel_id TEXT,
              guild_id TEXT,
              emoji TEXT,
              user_review_score INTEGER,
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

            CREATE INDEX IF NOT EXISTS idx_rsa_message_id
            ON reaction_skill_audits(discord_message_id);

            CREATE INDEX IF NOT EXISTS idx_rsa_turn_id
            ON reaction_skill_audits(turn_id);

            CREATE INDEX IF NOT EXISTS idx_rsa_created_at
            ON reaction_skill_audits(created_at);
            """
        )

        cols = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(reaction_skill_audits)"
            ).fetchall()
        }
        if "user_review_score" not in cols:
            conn.execute(
                "ALTER TABLE reaction_skill_audits ADD COLUMN user_review_score INTEGER"
            )

        conn.commit()
    finally:
        conn.close()


def persist_skill_audit_report(
    *,
    report: dict,
    reacted_by_user_id: int,
    channel_id: int,
    guild_id: int | None,
    emoji: str,
) -> int:
    ensure_skill_audit_db()
    now = datetime.now(timezone.utc).timestamp()
    function_counts = report.get("function_counts") or {}
    status_counts = report.get("status_counts") or {}
    review_score = REACTION_SCORE_MAP.get(emoji)

    conn = sqlite3.connect(state.SKILL_AUDIT_DB_PATH)
    try:
        cur = conn.execute(
            """
            INSERT INTO reaction_skill_audits (
              discord_message_id,
              reacted_by_user_id,
              channel_id,
              guild_id,
              emoji,
              user_review_score,
              session_id,
              turn_id,
              assistant_db_id,
              mapping_status,
              resolution_source,
              reply_to_message_id,
              previous_user_preview,
              skill_event_count,
              skill_view_count,
              skills_list_count,
              skill_manage_count,
              succeeded_count,
              failed_count,
              unknown_count,
              raw_report_json,
              created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(report.get("message_id") or ""),
                str(reacted_by_user_id),
                str(channel_id),
                str(guild_id) if guild_id is not None else None,
                emoji,
                int(review_score) if review_score is not None else None,
                str(report.get("session_id") or ""),
                str(report.get("turn_id") or ""),
                (
                    int(report.get("assistant_db_id") or 0)
                    if report.get("assistant_db_id") is not None
                    else None
                ),
                str(report.get("mapping_status") or ""),
                str(report.get("resolution_source") or ""),
                str(report.get("reply_to_message_id") or ""),
                str(report.get("previous_user_preview") or ""),
                len(report.get("events") or []),
                int(function_counts.get("skill_view", 0) or 0),
                int(function_counts.get("skills_list", 0) or 0),
                int(function_counts.get("skill_manage", 0) or 0),
                int(status_counts.get("succeeded", 0) or 0),
                int(status_counts.get("failed", 0) or 0),
                int(status_counts.get("unknown", 0) or 0),
                json.dumps(report, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def delete_skill_audit_report(
    *, message_id: int, reacted_by_user_id: int, emoji: str
) -> int:
    ensure_skill_audit_db()
    conn = sqlite3.connect(state.SKILL_AUDIT_DB_PATH)
    try:
        cur = conn.execute(
            """
            DELETE FROM reaction_skill_audits
            WHERE discord_message_id = ?
              AND reacted_by_user_id = ?
              AND emoji = ?
            """,
            (str(message_id), str(reacted_by_user_id), emoji),
        )
        conn.commit()
        return int(cur.rowcount)
    finally:
        conn.close()


def get_existing_user_review(
    *, message_id: int, reacted_by_user_id: int
) -> dict | None:
    ensure_skill_audit_db()
    conn = sqlite3.connect(state.SKILL_AUDIT_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, emoji, user_review_score, created_at
            FROM reaction_skill_audits
            WHERE discord_message_id = ?
              AND reacted_by_user_id = ?
              AND user_review_score IS NOT NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (str(message_id), str(reacted_by_user_id)),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_existing_user_review_by_turn(
    *, turn_id: str, reacted_by_user_id: int
) -> dict | None:
    ensure_skill_audit_db()
    conn = sqlite3.connect(state.SKILL_AUDIT_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, discord_message_id, turn_id, emoji, user_review_score, created_at
            FROM reaction_skill_audits
            WHERE turn_id = ?
              AND reacted_by_user_id = ?
              AND user_review_score IS NOT NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (str(turn_id), str(reacted_by_user_id)),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def move_existing_user_review_to_message(
    *, turn_id: str, reacted_by_user_id: int, message_id: int, emoji: str
) -> int:
    ensure_skill_audit_db()
    conn = sqlite3.connect(state.SKILL_AUDIT_DB_PATH)
    try:
        cur = conn.execute(
            """
            UPDATE reaction_skill_audits
            SET discord_message_id = ?,
                created_at = ?
            WHERE turn_id = ?
              AND reacted_by_user_id = ?
              AND emoji = ?
              AND user_review_score IS NOT NULL
            """,
            (
                str(message_id),
                datetime.now(timezone.utc).timestamp(),
                str(turn_id),
                str(reacted_by_user_id),
                str(emoji),
            ),
        )
        conn.commit()
        return int(cur.rowcount)
    finally:
        conn.close()


def delete_skill_audit_reports_by_turn(
    *, turn_id: str, reacted_by_user_id: int, emoji: str
) -> int:
    ensure_skill_audit_db()
    conn = sqlite3.connect(state.SKILL_AUDIT_DB_PATH)
    try:
        cur = conn.execute(
            """
            DELETE FROM reaction_skill_audits
            WHERE turn_id = ?
              AND reacted_by_user_id = ?
              AND emoji = ?
              AND user_review_score IS NOT NULL
            """,
            (str(turn_id), str(reacted_by_user_id), emoji),
        )
        conn.commit()
        return int(cur.rowcount)
    finally:
        conn.close()


def should_delete_turn_review_on_remove(
    *, payload_message_id: int, reaction_emoji: str, existing_review: dict | None
) -> bool:
    if not existing_review:
        return False
    stored_emoji = str(existing_review.get("emoji") or "").strip()
    return stored_emoji == str(reaction_emoji)
