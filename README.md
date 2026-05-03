# Hermes Discord Skill Audit

[English](README.md) | [中文](README_zh.md)

[![Status](https://img.shields.io/badge/status-active%20prototype-blue)](./README.md)
[![Scope](https://img.shields.io/badge/scope-turn%20mapping%20%2B%20reaction%20audit-5865F2)](./README.md)
[![Storage](https://img.shields.io/badge/storage-sqlite-green)](./README.md)
[![Integration](https://img.shields.io/badge/integration-Hermes%20Gateway-orange)](./README.md)

A small, non-invasive toolkit for collecting **human feedback on Hermes Agent skill usage in Discord**.

The project now has two layers:

1. **Turn mapper**: records which visible Discord `message_id` belongs to which Hermes assistant turn.
2. **Reaction audit package**: lets a sidecar Discord bot watch `✅ / ❌ / 👌` reactions, reconstruct the skills used by that turn, and persist a structured review row in SQLite.

No Hermes source-code fork is required. The mapping layer is installed through a Hermes Gateway startup hook, while the review layer is a normal Python package that can be imported by any Discord bot.

---

## What problem this solves

A single logical Hermes answer may appear in Discord as more than one message:

- a tool-phase status message plus the final text reply
- multiple chunks when Discord splits long output
- follow-up messages in a thread/topic

For manual scoring, the important question is not simply:

```text
Which Discord message got a reaction?
```

It is:

```text
Which Hermes assistant turn did the user review, and which skills were used in that turn?
```

This repository provides that bridge.

---

## Current architecture

```text
Hermes Gateway sends a Discord reply
  -> mapper.py patches DiscordAdapter.send()
  -> data/discord_turn_map.db stores discord_message_id -> turn_id

User reacts to a Hermes reply with ✅ / ❌ / 👌
  -> sidecar Discord bot receives on_raw_reaction_add / remove
  -> hermes_discord_skill_audit resolves message_id -> turn_id
  -> reads ~/.hermes/state.db for skill_view / skills_list / skill_manage events
  -> stores one turn-level review in skill_audit.db
  -> mirrors reaction state across sibling Discord messages for the same turn
```

The synthetic `turn_id` is:

```text
<session_id>:<assistant_db_id>
```

This is intentionally turn-level, not message-level. If one Hermes turn produced three Discord messages, all three should resolve to the same scoring slot.

---

## Feature summary

### Turn mapping layer

- Captures outbound Discord `message_id` values at send time.
- Supports chunked Discord replies through `raw_response.message_ids`.
- Stores durable mappings in `data/discord_turn_map.db`.
- Tracks `pending` vs `resolved` rows for delayed Hermes `state.db` writes.
- Provides `inspect_map.py` for local inspection and reconciliation.

### Reaction audit package

- Importable package: `hermes_discord_skill_audit`.
- Thin reference bot: `examples/reaction_audit_bot.py`.
- Supported review reactions:
  - `✅` -> `good` -> `0`
  - `❌` -> `not_good` -> `1`
  - `👌` -> `okay` -> `2`
- Enforces **one active score per user per Hermes turn**.
- Prevents duplicate scoring across split messages from the same turn.
- Mirrors reactions across sibling messages from the same turn.
- Allows removing a score from any mirrored chunk/message.
- Persists structured review rows in a separate `skill_audit.db`.
- Keeps the audit DB separate from the turn-map DB.

### Compatibility layer

`hermes_discord_skill_audit.reaction_audit` remains a compatibility facade for older integrations/tests that imported helpers from the original monolithic reference bot.

---

## Repository layout

```text
hermes-discord-skill-audit/
  mapper.py                         # Hermes Gateway send hook / Discord turn mapper
  inspect_map.py                    # CLI for inspecting and reconciling mapping rows
  pyproject.toml                    # package metadata and test config
  .env.example                      # reference bot environment template

  hermes_discord_skill_audit/
    __init__.py
    reaction_audit.py               # compatibility facade / re-exports
    config.py                       # ReactionAuditConfig and env parsing
    state.py                        # runtime config shared by handlers
    scores.py                       # reaction -> score enum mapping
    audit_db.py                     # skill_audit.db schema and review persistence
    turn_map.py                     # message_id -> turn_id resolution and skill report builder
    message_format.py               # human-readable report formatting helpers
    discord_reactions.py            # on_raw_reaction_add/remove handlers

  examples/
    reaction_audit_bot.py           # minimal sidecar Discord bot using the package

  tests/
    test_mapper.py
    test_inspect_map.py
    test_reaction_audit_core.py
    test_reaction_audit_bot.py

  data/
    discord_turn_map.db             # generated mapper DB, not source code
    skill_audit.db                  # generated audit DB if using default paths

  video/
    example_use_case.gif            # optional demo media
    example_use_case.mp4
```

---

## Database responsibilities

### Mapping DB: `discord_turn_map.db`

Owned by the Hermes Gateway mapping hook.

Main table:

```sql
CREATE TABLE IF NOT EXISTS discord_message_turn_map (
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
```

Important behavior:

- New outbound messages may start as `pending` if the final assistant row is not yet visible in `~/.hermes/state.db`.
- Later reconciliation or reaction-time rescue can update them to `resolved`.
- Split chunks and tool/final sibling messages may share the same `turn_id`.

### Audit DB: `skill_audit.db`

Owned by the sidecar bot / reaction audit package.

Main table:

```sql
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
```

Important behavior:

- Review uniqueness is enforced by logic at `(turn_id, reacted_by_user_id)`, not by raw `discord_message_id`.
- `raw_report_json` stores the full reconstructed skill report for later analytics.
- The package performs lightweight schema migration, such as adding `user_review_score` to older DBs.

---

## Quick start

### 1) Install project dependencies

```bash
cd /path/to/hermes-discord-skill-audit
uv sync
```

For production reuse from another bot project, install this package into that bot's virtual environment:

```bash
/path/to/sidecar-bot/.venv/bin/pip install -e /path/to/hermes-discord-skill-audit
```

### 2) Install the Hermes Gateway mapping hook

Create a Hermes hook directory:

```bash
mkdir -p ~/.hermes/hooks/discord-turn-map
```

Create `~/.hermes/hooks/discord-turn-map/HOOK.yaml`:

```yaml
name: discord-turn-map
summary: Persist Discord message_id -> Hermes turn_id mappings on gateway startup.
events:
  - gateway:startup
```

Create `~/.hermes/hooks/discord-turn-map/handler.py`:

```python
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

LOGGER = logging.getLogger("discord-turn-map-hook")
PROJECT_DIR = Path("/path/to/hermes-discord-skill-audit")
MAPPER_PATH = PROJECT_DIR / "mapper.py"
_MODULE = None


def _load_mapper_module():
    global _MODULE
    if _MODULE is not None:
        return _MODULE
    spec = importlib.util.spec_from_file_location("hermes_discord_turn_map", MAPPER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load mapper from {MAPPER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _MODULE = module
    return module


async def handle(event_type: str, context: dict):
    if event_type != "gateway:startup":
        return
    module = _load_mapper_module()
    installed = module.install_patch()
    LOGGER.info("discord-turn-map hook initialized: installed=%s", installed)
```

Restart Hermes Gateway after creating or updating the hook. The mapper is installed only on `gateway:startup`.

### 3) Configure the sidecar reaction audit bot

Copy the environment template:

```bash
cp .env.example .env
```

Set at least:

```env
DISCORD_BOT_TOKEN=your_sidecar_bot_token
HERMES_AGENT_USER_ID=1492290496222072925
DISCORD_TURN_MAP_DB_PATH=/path/to/hermes-discord-skill-audit/data/discord_turn_map.db
HERMES_STATE_DB_PATH=~/.hermes/state.db
SKILL_AUDIT_DB_PATH=/path/to/sidecar-or-project/data/skill_audit.db
REACTION_ALLOWED_USER_IDS=
TURN_MAP_DEFAULT_WINDOW_SECONDS=180
TURN_MAP_RESCUE_WINDOW_SECONDS=600
```

The sidecar Discord bot needs permission to:

- read messages in the target channels/threads
- receive reaction events
- add/remove reactions for mirroring
- optionally manage messages if you want `clear_reaction()` cleanup behavior

### 4) Run the reference bot

```bash
uv run python examples/reaction_audit_bot.py
```

Now, when an allowed user reacts to a Hermes Agent reply with `✅`, `❌`, or `👌`, the bot resolves the Hermes turn and stores a row in `skill_audit.db`.

---

## Integrating into an existing Discord bot

The intended production pattern is to keep unrelated bot features outside this package, then register the audit handlers from your own bot application:

```python
import logging
from discord.ext import commands

from hermes_discord_skill_audit.config import ReactionAuditConfig
from hermes_discord_skill_audit.discord_reactions import register_reaction_audit_handlers

logger = logging.getLogger("reaction-audit")
bot = commands.Bot(command_prefix="!")

config = ReactionAuditConfig.from_env()
register_reaction_audit_handlers(bot, config=config, logger_=logger)
```

This keeps the reusable audit logic here while allowing a production bot to keep its own dashboards, panels, commands, and cron widgets elsewhere.

---

## Inspecting and reconciling the turn map

Show recent mapping rows:

```bash
uv run python inspect_map.py --limit 20
```

Show unresolved rows:

```bash
uv run python inspect_map.py --status pending --limit 20
```

Inspect a specific Discord message:

```bash
uv run python inspect_map.py --message-id 1497000000000000000
```

Reconcile delayed pending rows:

```bash
uv run python inspect_map.py \
  --reconcile \
  --window-seconds 180 \
  --lookback-seconds 3600
```

Reaction-time lookup also performs a wider rescue pass controlled by `TURN_MAP_RESCUE_WINDOW_SECONDS`.

---

## Reaction UX rules

The package is designed around turn-level scoring:

1. A user adds `✅ / ❌ / 👌` to any message from a Hermes turn.
2. The package stores one review row for `(turn_id, reacted_by_user_id)`.
3. The bot mirrors that emoji to sibling Discord messages for the same turn.
4. If the same user clicks the same emoji on a mirrored sibling, review ownership moves to the clicked message so later removal works naturally.
5. If the same user tries a different score on the same turn, the extra reaction is removed when possible and a warning is sent.
6. If the user removes the stored score from any mirrored chunk/message, the turn-level review is deleted and mirrored reactions are cleaned up.

This avoids duplicate rows when one Hermes answer appears as several Discord messages.

---

## Development

Run tests:

```bash
uv run pytest
```

Run formatting:

```bash
uv run black .
```

The package discovery config intentionally includes only `hermes_discord_skill_audit*` and excludes generated/runtime folders such as `data`, `video`, `examples`, and `tests` from package installation.

---

## Operational notes

- Restart Hermes Gateway after changing the mapping hook or `mapper.py`.
- If the mapping DB/table is deleted while Gateway is already running, restart Gateway so the hook can recreate the table and resume writes.
- Prefer absolute paths for `DISCORD_TURN_MAP_DB_PATH`, `HERMES_STATE_DB_PATH`, and `SKILL_AUDIT_DB_PATH` in production.
- If a reaction audit says the turn mapping is still pending, wait or increase the rescue window; delayed `state.db` writes are expected under some workloads.
- Do not mix reaction audit rows into the turn-map DB. The two databases have separate responsibilities.
- The reference bot is intentionally minimal. Production bots should import the package instead of copying unrelated operational code into this repository.

---

## Roadmap

Completed:

- Discord message -> Hermes turn mapping.
- Importable reaction audit package.
- Reference sidecar bot.
- Turn-level review uniqueness.
- Reaction mirroring and safe removal across sibling messages.
- Reaction-time rescue for delayed pending rows.

Planned:

- Export helpers for aggregate review data.
- Skill-level summaries grouped by score, time range, and user.
- Optional bridge/exporter for downstream skill curation workflows.
