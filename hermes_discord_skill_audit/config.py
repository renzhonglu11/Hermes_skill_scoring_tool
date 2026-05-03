from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path


def parse_int_set(raw_value: str) -> set[int]:
    values: set[int] = set()
    for part in raw_value.split(","):
        cleaned = part.strip()
        if not cleaned:
            continue
        try:
            values.add(int(cleaned))
        except ValueError:
            logging.getLogger("reaction-audit").warning(
                "Ignoring invalid integer config value: %s", cleaned
            )
    return values


@dataclass(slots=True)
class ReactionAuditConfig:
    hermes_agent_user_id: int
    turn_map_db_path: str | Path
    hermes_state_db_path: str | Path
    skill_audit_db_path: str | Path
    allowed_user_ids: set[int] | None = None
    default_window_seconds: int = 180
    rescue_window_seconds: int = 600
    duplicate_warning_language: str = "zh"

    @classmethod
    def from_env(
        cls, *, default_skill_audit_db_path: str | Path | None = None
    ) -> "ReactionAuditConfig":
        return cls(
            hermes_agent_user_id=int(
                os.getenv("HERMES_AGENT_USER_ID", "1492290496222072925")
                or 1492290496222072925
            ),
            turn_map_db_path=Path(
                os.getenv(
                    "DISCORD_TURN_MAP_DB_PATH",
                    "/home/rz/projects/hermes-discord-skill-audit/data/discord_turn_map.db",
                )
            ).expanduser(),
            hermes_state_db_path=Path(
                os.getenv(
                    "HERMES_STATE_DB_PATH", str(Path.home() / ".hermes" / "state.db")
                )
            ).expanduser(),
            skill_audit_db_path=Path(
                os.getenv(
                    "SKILL_AUDIT_DB_PATH",
                    str(default_skill_audit_db_path or "data/skill_audit.db"),
                )
            ).expanduser(),
            allowed_user_ids=parse_int_set(os.getenv("REACTION_ALLOWED_USER_IDS", "")),
            default_window_seconds=int(
                os.getenv("TURN_MAP_DEFAULT_WINDOW_SECONDS", "180") or 180
            ),
            rescue_window_seconds=int(
                os.getenv("TURN_MAP_RESCUE_WINDOW_SECONDS", "600") or 600
            ),
        )
