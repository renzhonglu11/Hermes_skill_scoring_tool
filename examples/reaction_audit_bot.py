from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PROJECT_ROOT = BASE_DIR.parent
ENV_FILE = os.getenv("ENV_FILE", ".env")
ENV_PATH = PROJECT_ROOT / ENV_FILE

load_dotenv(ENV_PATH)

def _resolve_env_path(env_val: str, default_rel: str) -> Path:
    p = Path(os.getenv(env_val, default_rel)).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
HERMES_AGENT_USER_ID = int(os.getenv("HERMES_AGENT_USER_ID", "1492290496222072925") or 1492290496222072925)
TURN_MAP_DB_PATH = _resolve_env_path("DISCORD_TURN_MAP_DB_PATH", "data/discord_turn_map.db")
HERMES_STATE_DB_PATH = Path(os.getenv("HERMES_STATE_DB_PATH", str(Path.home() / ".hermes" / "state.db"))).expanduser()
SKILL_AUDIT_DB_PATH = _resolve_env_path("SKILL_AUDIT_DB_PATH", "data/skill_audit.db")
TURN_MAP_DEFAULT_WINDOW_SECONDS = int(os.getenv("TURN_MAP_DEFAULT_WINDOW_SECONDS", "180") or 180)
TURN_MAP_RESCUE_WINDOW_SECONDS = int(os.getenv("TURN_MAP_RESCUE_WINDOW_SECONDS", "600") or 600)


class UserReviewScore(IntEnum):
    GOOD = 0
    NOT_GOOD = 1
    OKAY = 2


REACTION_SCORE_MAP: dict[str, UserReviewScore] = {
    "✅": UserReviewScore.GOOD,
    "❌": UserReviewScore.NOT_GOOD,
    "👌": UserReviewScore.OKAY,
}


def parse_int_set(raw_value: str) -> set[int]:
    values: set[int] = set()
    for part in raw_value.split(","):
        cleaned = part.strip()
        if not cleaned:
            continue
        try:
            values.add(int(cleaned))
        except ValueError:
            logging.getLogger("reaction-audit-bot").warning("Ignoring invalid integer config value: %s", cleaned)
    return values


REACTION_ALLOWED_USER_IDS = parse_int_set(os.getenv("REACTION_ALLOWED_USER_IDS", ""))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("reaction-audit-bot")


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SKILL_AUDIT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def ensure_skill_audit_db() -> None:
    ensure_dirs()
    conn = sqlite3.connect(SKILL_AUDIT_DB_PATH)
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
        cols = {row[1] for row in conn.execute("PRAGMA table_info(reaction_skill_audits)").fetchall()}
        if "user_review_score" not in cols:
            conn.execute("ALTER TABLE reaction_skill_audits ADD COLUMN user_review_score INTEGER")
        conn.commit()
    finally:
        conn.close()


def review_score_to_string(score: int | UserReviewScore | None) -> str:
    if score is None:
        return "unknown"
    try:
        return UserReviewScore(int(score)).name.lower()
    except Exception:
        return f"unknown:{score}"


def persist_skill_audit_report(*, report: dict, reacted_by_user_id: int, channel_id: int, guild_id: int | None, emoji: str) -> int:
    ensure_skill_audit_db()
    now = datetime.now(timezone.utc).timestamp()
    function_counts = report.get("function_counts") or {}
    status_counts = report.get("status_counts") or {}
    review_score = REACTION_SCORE_MAP.get(emoji)

    conn = sqlite3.connect(SKILL_AUDIT_DB_PATH)
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
                int(report.get("assistant_db_id") or 0) if report.get("assistant_db_id") is not None else None,
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


def get_existing_user_review_by_turn(*, turn_id: str, reacted_by_user_id: int) -> dict | None:
    ensure_skill_audit_db()
    conn = sqlite3.connect(SKILL_AUDIT_DB_PATH)
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


def delete_skill_audit_reports_by_turn(*, turn_id: str, reacted_by_user_id: int, emoji: str) -> int:
    ensure_skill_audit_db()
    conn = sqlite3.connect(SKILL_AUDIT_DB_PATH)
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


def get_message_ids_for_turn(turn_id: str) -> list[int]:
    if not TURN_MAP_DB_PATH.exists() or not turn_id:
        return []

    conn = sqlite3.connect(TURN_MAP_DB_PATH)
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


async def sync_turn_reaction(*, channel, origin_message_id: int, turn_id: str, emoji: str, action: str) -> None:
    if not turn_id or action not in {"add", "remove"}:
        return

    bot_member = bot.user
    for message_id in get_message_ids_for_turn(turn_id):
        try:
            message = await channel.fetch_message(int(message_id))
            if action == "add":
                if int(message_id) == int(origin_message_id):
                    # User added their reaction here, remove the bot's mirrored reaction to prevent a count of 2
                    if bot_member is not None:
                        try:
                            await message.remove_reaction(emoji, bot_member)
                        except discord.HTTPException:
                            pass
                else:
                    await message.add_reaction(emoji)
            elif action == "remove":
                try:
                    # Clear the emoji entirely from this chunk so the user's original reaction is also cleaned up
                    await message.clear_reaction(emoji)
                except discord.Forbidden:
                    # Fallback if the bot lacks Manage Messages permissions
                    if bot_member is not None:
                        try:
                            await message.remove_reaction(emoji, bot_member)
                        except discord.HTTPException:
                            pass
                except discord.HTTPException:
                    pass
        except discord.NotFound:
            logger.warning("Turn reaction sync skipped missing message: turn_id=%s message_id=%s", turn_id, message_id)
        except discord.HTTPException:
            logger.exception(
                "Turn reaction sync failed: action=%s turn_id=%s message_id=%s emoji=%s",
                action,
                turn_id,
                message_id,
                emoji,
            )


async def remove_user_reaction(*, message, emoji: str, member) -> bool:
    if member is None:
        return False
    try:
        await message.remove_reaction(emoji, member)
        return True
    except discord.NotFound:
        logger.warning("Extra user reaction already missing: message_id=%s emoji=%s", getattr(message, "id", None), emoji)
        return False
    except discord.HTTPException:
        logger.exception(
            "Failed to remove extra user reaction: message_id=%s emoji=%s member=%s",
            getattr(message, "id", None),
            emoji,
            getattr(member, "id", member),
        )
        return False


def _fetch_turn_map_row(message_id: int) -> dict | None:
    if not TURN_MAP_DB_PATH.exists():
        return None

    conn = sqlite3.connect(TURN_MAP_DB_PATH)
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


def _find_assistant_after_sent(session_id: str, sent_at: float, window_seconds: int = TURN_MAP_DEFAULT_WINDOW_SECONDS) -> int | None:
    if not session_id or not HERMES_STATE_DB_PATH.exists():
        return None

    lower = float(sent_at) - 3.0
    upper = float(sent_at) + float(window_seconds)

    conn = sqlite3.connect(HERMES_STATE_DB_PATH)
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


def _persist_turn_map_resolution(message_id: int, session_id: str, assistant_db_id: int, resolution_source: str) -> None:
    if not TURN_MAP_DB_PATH.exists():
        return

    now = datetime.now(timezone.utc).timestamp()
    conn = sqlite3.connect(TURN_MAP_DB_PATH)
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

    assistant_db_id = _find_assistant_after_sent(session_id, float(sent_at), TURN_MAP_DEFAULT_WINDOW_SECONDS)
    resolution_source = "reaction_fallback_timestamp"

    if not assistant_db_id and rescue_window_seconds and rescue_window_seconds > TURN_MAP_DEFAULT_WINDOW_SECONDS:
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
    map_row = _resolve_turn_map_row(message_id, rescue_window_seconds=TURN_MAP_RESCUE_WINDOW_SECONDS)
    if not map_row:
        raise RuntimeError(f"No mapping found for message_id={message_id}")

    session_id = str(map_row.get("session_id") or "")
    assistant_db_id = map_row.get("assistant_db_id")
    if not session_id or not assistant_db_id:
        raise RuntimeError(
            f"Mapping found for message_id={message_id}, but turn is still unresolved (status={map_row.get('status') or 'unknown'})"
        )

    if not HERMES_STATE_DB_PATH.exists():
        raise RuntimeError(f"Hermes state.db not found: {HERMES_STATE_DB_PATH}")

    assistant_db_id = int(assistant_db_id)
    conn = sqlite3.connect(HERMES_STATE_DB_PATH)
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


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if bot.user and payload.user_id == bot.user.id:
        return
    reaction_emoji = str(payload.emoji)
    review_score = REACTION_SCORE_MAP.get(reaction_emoji)
    if review_score is None:
        return
    if REACTION_ALLOWED_USER_IDS and payload.user_id not in REACTION_ALLOWED_USER_IDS:
        return

    try:
        channel = bot.get_channel(payload.channel_id)
        if channel is None:
            channel = await bot.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        if int(getattr(message.author, "id", 0) or 0) != HERMES_AGENT_USER_ID:
            return

        report = get_skill_report_for_message(payload.message_id)
        turn_id = str(report.get("turn_id") or "")
        existing_review = get_existing_user_review_by_turn(turn_id=turn_id, reacted_by_user_id=payload.user_id)
        if existing_review:
            existing_emoji = str(existing_review.get("emoji") or "")
            existing_score = existing_review.get("user_review_score")
            existing_message_id = str(existing_review.get("discord_message_id") or "")

            if existing_emoji == reaction_emoji:
                # The user clicked the exact same emoji on another chunk of the same turn.
                # Just remove the bot's mirrored reaction from this chunk to prevent a count of 2.
                if bot.user is not None:
                    try:
                        await message.remove_reaction(reaction_emoji, bot.user)
                    except discord.HTTPException:
                        pass
                return

            review_member = payload.member if getattr(payload, "member", None) is not None else None
            if review_member is None:
                try:
                    review_member = await channel.guild.fetch_member(payload.user_id) if getattr(channel, "guild", None) else None
                except Exception:
                    review_member = None
            removed_extra = await remove_user_reaction(message=message, emoji=reaction_emoji, member=review_member)
            await channel.send(
                (
                    f"⚠️ <@{payload.user_id}> this Hermes turn already has a stored review. "
                    f"You already recorded `{existing_emoji}` ({review_score_to_string(existing_score)}) "
                    f"for turn_id=`{turn_id}` on message `{existing_message_id}`. "
                    + ("I removed your extra reaction automatically." if removed_extra else "Please remove your extra reaction manually.")
                ),
                reference=message,
                mention_author=False,
            )
            logger.info(
                "Reaction skill audit skipped due to existing turn review: channel=%s guild=%s message=%s reacted_by=%s turn_id=%s new_emoji=%s existing_emoji=%s existing_score=%s(%s) existing_message_id=%s removed_extra=%s",
                payload.channel_id,
                payload.guild_id,
                payload.message_id,
                payload.user_id,
                turn_id,
                reaction_emoji,
                existing_emoji,
                existing_score,
                review_score_to_string(existing_score),
                existing_message_id,
                removed_extra,
            )
            return

        audit_id = persist_skill_audit_report(
            report=report,
            reacted_by_user_id=payload.user_id,
            channel_id=payload.channel_id,
            guild_id=payload.guild_id,
            emoji=reaction_emoji,
        )
        await sync_turn_reaction(
            channel=channel,
            origin_message_id=payload.message_id,
            turn_id=turn_id,
            emoji=reaction_emoji,
            action="add",
        )
        logger.info(
            "Reaction skill audit stored: audit_id=%s channel=%s guild=%s message=%s reacted_by=%s emoji=%s user_review_score=%s(%s) turn_id=%s skills=%s status_counts=%s db=%s",
            audit_id,
            payload.channel_id,
            payload.guild_id,
            payload.message_id,
            payload.user_id,
            reaction_emoji,
            int(review_score),
            review_score_to_string(review_score),
            report.get("turn_id"),
            report.get("function_counts"),
            report.get("status_counts"),
            SKILL_AUDIT_DB_PATH,
        )
    except Exception:
        logger.exception(
            "Failed to process reaction skill audit for message=%s channel=%s reacted_by=%s emoji=%s",
            payload.message_id,
            payload.channel_id,
            payload.user_id,
            reaction_emoji,
        )


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if bot.user and payload.user_id == bot.user.id:
        return
    reaction_emoji = str(payload.emoji)
    review_score = REACTION_SCORE_MAP.get(reaction_emoji)
    if review_score is None:
        return
    if REACTION_ALLOWED_USER_IDS and payload.user_id not in REACTION_ALLOWED_USER_IDS:
        return

    try:
        report = get_skill_report_for_message(payload.message_id)
        turn_id = str(report.get("turn_id") or "")
        channel = bot.get_channel(payload.channel_id)
        if channel is None:
            channel = await bot.fetch_channel(payload.channel_id)

        removed_count = delete_skill_audit_reports_by_turn(
            turn_id=turn_id,
            reacted_by_user_id=payload.user_id,
            emoji=reaction_emoji,
        )
        await sync_turn_reaction(
            channel=channel,
            origin_message_id=payload.message_id,
            turn_id=turn_id,
            emoji=reaction_emoji,
            action="remove",
        )
        logger.info(
            "Reaction skill audit removed: channel=%s guild=%s message=%s reacted_by=%s emoji=%s user_review_score=%s(%s) turn_id=%s removed_count=%s db=%s",
            payload.channel_id,
            payload.guild_id,
            payload.message_id,
            payload.user_id,
            reaction_emoji,
            int(review_score),
            review_score_to_string(review_score),
            turn_id,
            removed_count,
            SKILL_AUDIT_DB_PATH,
        )
    except Exception:
        logger.exception(
            "Failed to remove reaction skill audit for message=%s channel=%s reacted_by=%s emoji=%s",
            payload.message_id,
            payload.channel_id,
            payload.user_id,
            reaction_emoji,
        )


@bot.event
async def on_ready():
    logger.info("Reaction audit bot logged in as %s (%s)", bot.user, getattr(bot.user, "id", None))
    logger.info("Watching Hermes agent user id: %s", HERMES_AGENT_USER_ID)
    logger.info("Turn map DB: %s", TURN_MAP_DB_PATH)
    logger.info("Skill audit DB: %s", SKILL_AUDIT_DB_PATH)


def main() -> None:
    ensure_dirs()
    ensure_skill_audit_db()
    if not TOKEN:
        raise RuntimeError(f"Missing DISCORD_BOT_TOKEN in {ENV_PATH}")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
