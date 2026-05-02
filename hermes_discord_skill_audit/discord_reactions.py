from __future__ import annotations

from typing import Any
import logging

import discord

from . import state
from .audit_db import (
    delete_skill_audit_reports_by_turn,
    get_existing_user_review_by_turn,
    move_existing_user_review_to_message,
    persist_skill_audit_report,
    should_delete_turn_review_on_remove,
)
from .config import ReactionAuditConfig
from .scores import REACTION_SCORE_MAP, review_score_to_string
from .turn_map import get_message_ids_for_turn, get_skill_report_for_message


def register_reaction_audit_handlers(
    bot_obj: Any,
    *,
    config: ReactionAuditConfig,
    logger_: logging.Logger | None = None,
) -> None:
    state.configure_reaction_audit(config, logger_=logger_, bot_=bot_obj)
    bot_obj.event(on_raw_reaction_add)
    bot_obj.event(on_raw_reaction_remove)

async def sync_turn_reaction(*, channel, origin_message_id: int, turn_id: str, emoji: str, action: str) -> None:
    if not turn_id or action not in {"add", "remove"}:
        return

    bot_member = state.bot.user
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
            state.logger.warning("Turn reaction sync skipped missing message: turn_id=%s message_id=%s", turn_id, message_id)
        except discord.HTTPException:
            state.logger.exception(
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
        state.logger.warning("Extra user reaction already missing: message_id=%s emoji=%s", getattr(message, "id", None), emoji)
        return False
    except discord.HTTPException:
        state.logger.exception(
            "Failed to remove extra user reaction: message_id=%s emoji=%s member=%s",
            getattr(message, "id", None),
            emoji,
            getattr(member, "id", member),
        )
        return False



async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if state.bot.user and payload.user_id == state.bot.user.id:
        return
    reaction_emoji = str(payload.emoji)
    review_score = REACTION_SCORE_MAP.get(reaction_emoji)
    if review_score is None:
        return
    if state.REACTION_ALLOWED_USER_IDS and payload.user_id not in state.REACTION_ALLOWED_USER_IDS:
        return

    try:
        channel = state.bot.get_channel(payload.channel_id)
        if channel is None:
            channel = await state.bot.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        if int(getattr(message.author, "id", 0) or 0) != state.HERMES_AGENT_USER_ID:
            return

        report = get_skill_report_for_message(payload.message_id)
        turn_id = str(report.get("turn_id") or "")
        existing_review = get_existing_user_review_by_turn(
            turn_id=turn_id,
            reacted_by_user_id=payload.user_id,
        )
        if existing_review:
            existing_emoji = str(existing_review.get("emoji") or "")
            existing_score = existing_review.get("user_review_score")
            existing_message_id = str(existing_review.get("discord_message_id") or "")

            if existing_emoji == reaction_emoji:
                # The user clicked the same emoji on another chunk of the same turn.
                # Move the stored review ownership to this message so removing it here later
                # clears the whole turn consistently.
                move_existing_user_review_to_message(
                    turn_id=turn_id,
                    reacted_by_user_id=payload.user_id,
                    message_id=payload.message_id,
                    emoji=reaction_emoji,
                )
                # Remove the bot's mirrored reaction from this chunk to prevent a count of 2.
                if state.bot.user is not None:
                    try:
                        await message.remove_reaction(reaction_emoji, state.bot.user)
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
                    f"⚠️ <@{payload.user_id}> 这个 Hermes 回复轮次已经记录过评分。"
                    f"你当前已对 turn_id=`{turn_id}` 记录了 `{existing_emoji}`"
                    f"（{review_score_to_string(existing_score)}），"
                    f"对应消息是 `{existing_message_id}`。"
                    + ("已自动移除你刚刚新增的多余 reaction。" if removed_extra else "请手动抹除你刚刚新增的多余 reaction。")
                ),
                reference=message,
                mention_author=False,
            )
            state.logger.info(
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
        state.logger.info(
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
            state.SKILL_AUDIT_DB_PATH,
        )
    except RuntimeError as exc:
        if "turn 尚未解析完成" in str(exc):
            state.logger.warning(
                "Reaction skill audit skipped because turn mapping is still pending: message=%s channel=%s reacted_by=%s emoji=%s error=%s",
                payload.message_id,
                payload.channel_id,
                payload.user_id,
                reaction_emoji,
                exc,
            )
            return
        state.logger.exception(
            "Failed to process reaction skill audit for message=%s channel=%s reacted_by=%s emoji=%s",
            payload.message_id,
            payload.channel_id,
            payload.user_id,
            reaction_emoji,
        )
    except Exception:
        state.logger.exception(
            "Failed to process reaction skill audit for message=%s channel=%s reacted_by=%s emoji=%s",
            payload.message_id,
            payload.channel_id,
            payload.user_id,
            reaction_emoji,
        )


async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if state.bot.user and payload.user_id == state.bot.user.id:
        return
    reaction_emoji = str(payload.emoji)
    review_score = REACTION_SCORE_MAP.get(reaction_emoji)
    if review_score is None:
        return
    if state.REACTION_ALLOWED_USER_IDS and payload.user_id not in state.REACTION_ALLOWED_USER_IDS:
        return

    try:
        report = get_skill_report_for_message(payload.message_id)
        turn_id = str(report.get("turn_id") or "")
        channel = state.bot.get_channel(payload.channel_id)
        if channel is None:
            channel = await state.bot.fetch_channel(payload.channel_id)

        existing_review = get_existing_user_review_by_turn(
            turn_id=turn_id,
            reacted_by_user_id=payload.user_id,
        )
        if not should_delete_turn_review_on_remove(
            payload_message_id=payload.message_id,
            reaction_emoji=reaction_emoji,
            existing_review=existing_review,
        ):
            state.logger.info(
                "Reaction removal ignored because it does not match stored turn review: channel=%s guild=%s message=%s reacted_by=%s emoji=%s turn_id=%s existing_message_id=%s existing_emoji=%s",
                payload.channel_id,
                payload.guild_id,
                payload.message_id,
                payload.user_id,
                reaction_emoji,
                turn_id,
                (existing_review or {}).get("discord_message_id"),
                (existing_review or {}).get("emoji"),
            )
            return

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
        state.logger.info(
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
            state.SKILL_AUDIT_DB_PATH,
        )
    except RuntimeError as exc:
        if "turn 尚未解析完成" in str(exc):
            state.logger.warning(
                "Reaction skill audit remove skipped because turn mapping is still pending: message=%s channel=%s reacted_by=%s emoji=%s error=%s",
                payload.message_id,
                payload.channel_id,
                payload.user_id,
                reaction_emoji,
                exc,
            )
            return
        state.logger.exception(
            "Failed to remove reaction skill audit for message=%s channel=%s reacted_by=%s emoji=%s",
            payload.message_id,
            payload.channel_id,
            payload.user_id,
            reaction_emoji,
        )
    except Exception:
        state.logger.exception(
            "Failed to remove reaction skill audit for message=%s channel=%s reacted_by=%s emoji=%s",
            payload.message_id,
            payload.channel_id,
            payload.user_id,
            reaction_emoji,
        )


