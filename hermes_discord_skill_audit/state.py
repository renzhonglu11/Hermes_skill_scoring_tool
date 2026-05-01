from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .config import ReactionAuditConfig

DATA_DIR = Path(os.getenv("REACTION_AUDIT_DATA_DIR", ".")).expanduser()
logger = logging.getLogger("reaction-audit")
bot: Any | None = None
HERMES_AGENT_USER_ID = 1492290496222072925
TURN_MAP_DB_PATH = Path("data/discord_turn_map.db")
HERMES_STATE_DB_PATH = Path.home() / ".hermes" / "state.db"
SKILL_AUDIT_DB_PATH = Path("data/skill_audit.db")
TURN_MAP_DEFAULT_WINDOW_SECONDS = 180
TURN_MAP_RESCUE_WINDOW_SECONDS = 600
REACTION_ALLOWED_USER_IDS: set[int] = set()
DUPLICATE_WARNING_LANGUAGE = "zh"


def configure_reaction_audit(
    config: ReactionAuditConfig,
    *,
    logger_: logging.Logger | None = None,
    bot_: Any | None = None,
) -> None:
    global logger, bot, HERMES_AGENT_USER_ID, TURN_MAP_DB_PATH, HERMES_STATE_DB_PATH, SKILL_AUDIT_DB_PATH
    global TURN_MAP_DEFAULT_WINDOW_SECONDS, TURN_MAP_RESCUE_WINDOW_SECONDS, REACTION_ALLOWED_USER_IDS, DUPLICATE_WARNING_LANGUAGE
    if logger_ is not None:
        logger = logger_
    if bot_ is not None:
        bot = bot_
    HERMES_AGENT_USER_ID = int(config.hermes_agent_user_id)
    TURN_MAP_DB_PATH = Path(config.turn_map_db_path).expanduser()
    HERMES_STATE_DB_PATH = Path(config.hermes_state_db_path).expanduser()
    SKILL_AUDIT_DB_PATH = Path(config.skill_audit_db_path).expanduser()
    TURN_MAP_DEFAULT_WINDOW_SECONDS = int(config.default_window_seconds)
    TURN_MAP_RESCUE_WINDOW_SECONDS = int(config.rescue_window_seconds)
    REACTION_ALLOWED_USER_IDS = set(config.allowed_user_ids or set())
    DUPLICATE_WARNING_LANGUAGE = config.duplicate_warning_language
