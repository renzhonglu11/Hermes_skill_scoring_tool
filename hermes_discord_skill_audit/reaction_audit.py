from __future__ import annotations

import sys
import types

from . import state
from .audit_db import (
    delete_skill_audit_report,
    delete_skill_audit_reports_by_turn,
    ensure_dirs,
    ensure_skill_audit_db,
    get_existing_user_review,
    get_existing_user_review_by_turn,
    move_existing_user_review_to_message,
    persist_skill_audit_report,
    should_delete_turn_review_on_remove,
)
from .config import ReactionAuditConfig, parse_int_set
from .discord_reactions import (
    on_raw_reaction_add,
    on_raw_reaction_remove,
    register_reaction_audit_handlers,
    remove_user_reaction,
    sync_turn_reaction,
)
from .message_format import build_skill_report_message
from .scores import REACTION_SCORE_MAP, UserReviewScore, review_score_to_string
from .state import configure_reaction_audit
from .turn_map import get_message_ids_for_turn, get_skill_report_for_message

_STATE_EXPORTS = {
    "DATA_DIR",
    "logger",
    "bot",
    "HERMES_AGENT_USER_ID",
    "TURN_MAP_DB_PATH",
    "HERMES_STATE_DB_PATH",
    "SKILL_AUDIT_DB_PATH",
    "TURN_MAP_DEFAULT_WINDOW_SECONDS",
    "TURN_MAP_RESCUE_WINDOW_SECONDS",
    "REACTION_ALLOWED_USER_IDS",
    "DUPLICATE_WARNING_LANGUAGE",
}


def __getattr__(name: str):
    if name in _STATE_EXPORTS:
        return getattr(state, name)
    raise AttributeError(name)


class _ReactionAuditModule(types.ModuleType):
    def __setattr__(self, name: str, value):
        if name in _STATE_EXPORTS:
            setattr(state, name, value)
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _ReactionAuditModule


__all__ = [
    "ReactionAuditConfig",
    "register_reaction_audit_handlers",
    "configure_reaction_audit",
    "parse_int_set",
    "UserReviewScore",
    "REACTION_SCORE_MAP",
    "review_score_to_string",
    "ensure_dirs",
    "ensure_skill_audit_db",
    "persist_skill_audit_report",
    "delete_skill_audit_report",
    "get_existing_user_review",
    "get_existing_user_review_by_turn",
    "move_existing_user_review_to_message",
    "delete_skill_audit_reports_by_turn",
    "should_delete_turn_review_on_remove",
    "get_message_ids_for_turn",
    "get_skill_report_for_message",
    "build_skill_report_message",
    "sync_turn_reaction",
    "remove_user_reaction",
    "on_raw_reaction_add",
    "on_raw_reaction_remove",
    *_STATE_EXPORTS,
]
