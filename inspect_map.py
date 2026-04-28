#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = os.getenv("ENV_FILE", ".env")
load_dotenv(PROJECT_ROOT / ENV_FILE)


def _resolve_env_path(env_val: str, default_rel: str) -> Path:
    p = Path(os.getenv(env_val, default_rel)).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


DB_PATH = _resolve_env_path("DISCORD_TURN_MAP_DB_PATH", "data/discord_turn_map.db")
STATE_DB = Path(
    os.getenv("HERMES_STATE_DB_PATH", str(Path.home() / ".hermes" / "state.db"))
).expanduser()


def _find_assistant_after_sent(
    session_id: str, sent_at: float, window_seconds: int
) -> int | None:
    if not STATE_DB.exists() or not session_id:
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
        return int(row["id"]) if row else None
    finally:
        conn.close()


def reconcile_pending(window_seconds: int = 180, lookback_seconds: int = 3600) -> int:
    if not DB_PATH.exists():
        return 0

    cutoff = time.time() - float(lookback_seconds)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT discord_message_id, session_key, session_id, sent_at
            FROM discord_message_turn_map
            WHERE status = 'pending'
              AND sent_at >= ?
            ORDER BY sent_at ASC
            """,
            (cutoff,),
        ).fetchall()

        updated = 0
        now = time.time()
        for row in rows:
            session_id = str(row["session_id"] or "")
            assistant_db_id = _find_assistant_after_sent(
                session_id, float(row["sent_at"]), window_seconds
            )
            if not assistant_db_id:
                continue

            turn_id = f"{session_id}:{assistant_db_id}"
            cur = conn.execute(
                """
                UPDATE discord_message_turn_map
                SET
                  turn_id = ?,
                  assistant_db_id = ?,
                  status = 'resolved',
                  resolution_source = 'inspect_reconcile_timestamp',
                  resolved_at = ?,
                  updated_at = ?
                WHERE discord_message_id = ?
                  AND status = 'pending'
                """,
                (turn_id, assistant_db_id, now, now, str(row["discord_message_id"])),
            )
            updated += cur.rowcount

        conn.commit()
        return updated
    finally:
        conn.close()


def fetch_recent(limit: int, status: str | None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cols = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(discord_message_turn_map)"
            ).fetchall()
        }
        has_status = "status" in cols
        has_resolution_source = "resolution_source" in cols
        has_sent_at = "sent_at" in cols

        selected = [
            "discord_message_id",
            "session_id",
            "thread_id",
            "turn_id",
            "assistant_db_id",
            "reply_to_message_id",
            "is_first_chunk",
            "chunk_index",
            "created_at",
        ]
        if has_status:
            selected.insert(6, "status")
        if has_resolution_source:
            selected.append("resolution_source")
        if has_sent_at:
            selected.append("sent_at")

        query = (
            f"SELECT {', '.join(selected)} FROM discord_message_turn_map"
            + (" WHERE status = ?" if status and has_status else "")
            + " ORDER BY created_at DESC LIMIT ?"
        )
        params = (status, limit) if status and has_status else (limit,)
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def fetch_one(message_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM discord_message_turn_map WHERE discord_message_id = ?",
            (message_id,),
        ).fetchone()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--message-id")
    parser.add_argument("--status", choices=["pending", "resolved"])
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="尝试按 sent_at + session_id 回填 pending 映射",
    )
    parser.add_argument("--window-seconds", type=int, default=180)
    parser.add_argument("--lookback-seconds", type=int, default=3600)
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return 1

    if args.reconcile:
        updated = reconcile_pending(
            window_seconds=args.window_seconds, lookback_seconds=args.lookback_seconds
        )
        print(f"reconciled_rows={updated}")

    if args.message_id:
        row = fetch_one(args.message_id)
        if not row:
            print("No mapping found")
            return 1
        for k in row.keys():
            print(f"{k}: {row[k]}")
        return 0

    rows = fetch_recent(args.limit, args.status)
    if not rows:
        print("No rows")
        return 0

    for row in rows:
        status_val = row["status"] if "status" in row.keys() else "resolved"
        source_val = (
            row["resolution_source"] if "resolution_source" in row.keys() else "legacy"
        )
        print(
            f"message_id={row['discord_message_id']} | status={status_val} | "
            f"turn_id={row['turn_id']} | session_id={row['session_id']} | "
            f"thread_id={row['thread_id']} | assistant_db_id={row['assistant_db_id']} | "
            f"reply_to={row['reply_to_message_id']} | chunk={row['chunk_index']} | source={source_val}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
