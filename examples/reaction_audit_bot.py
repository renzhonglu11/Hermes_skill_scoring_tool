from __future__ import annotations

import logging
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from hermes_discord_skill_audit import reaction_audit as core
from hermes_discord_skill_audit.reaction_audit import ReactionAuditConfig

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
ENV_FILE = os.getenv("ENV_FILE", ".env")
ENV_PATH = PROJECT_ROOT / ENV_FILE

load_dotenv(ENV_PATH)

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("reaction-audit-bot")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

config = ReactionAuditConfig.from_env(
    default_skill_audit_db_path=PROJECT_ROOT / "data" / "skill_audit.db"
)
core.register_reaction_audit_handlers(bot, config=config, logger_=logger)

# Compatibility re-exports for users/tests that imported helpers from this example.
parse_int_set = core.parse_int_set
ensure_skill_audit_db = core.ensure_skill_audit_db
review_score_to_string = core.review_score_to_string
persist_skill_audit_report = core.persist_skill_audit_report
get_existing_user_review_by_turn = core.get_existing_user_review_by_turn
move_existing_user_review_to_message = core.move_existing_user_review_to_message
delete_skill_audit_reports_by_turn = core.delete_skill_audit_reports_by_turn
should_delete_turn_review_on_remove = core.should_delete_turn_review_on_remove
get_message_ids_for_turn = core.get_message_ids_for_turn
sync_turn_reaction = core.sync_turn_reaction
remove_user_reaction = core.remove_user_reaction
get_skill_report_for_message = core.get_skill_report_for_message
on_raw_reaction_add = core.on_raw_reaction_add
on_raw_reaction_remove = core.on_raw_reaction_remove


@bot.event
async def on_ready():
    logger.info(
        "Reaction audit bot logged in as %s (%s)",
        bot.user,
        getattr(bot.user, "id", None),
    )
    logger.info("Watching Hermes agent user id: %s", core.HERMES_AGENT_USER_ID)
    logger.info("Turn map DB: %s", core.TURN_MAP_DB_PATH)
    logger.info("Skill audit DB: %s", core.SKILL_AUDIT_DB_PATH)


def main() -> None:
    core.ensure_dirs()
    core.ensure_skill_audit_db()
    if not TOKEN:
        raise RuntimeError(f"Missing DISCORD_BOT_TOKEN in {ENV_PATH}")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
