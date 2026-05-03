# Hermes Discord Skill Audit（Hermes Discord 技能审查）

[English](README.md) | [中文](README_zh.md)

[![Status](https://img.shields.io/badge/status-active%20prototype-blue)](./README_zh.md)
[![Scope](https://img.shields.io/badge/scope-turn%20mapping%20%2B%20reaction%20audit-5865F2)](./README_zh.md)
[![Storage](https://img.shields.io/badge/storage-sqlite-green)](./README_zh.md)
[![Integration](https://img.shields.io/badge/integration-Hermes%20Gateway-orange)](./README_zh.md)

这是一个小型、非侵入式工具集，用于在 Discord 中收集 **用户对 Hermes Agent 技能使用情况的人工反馈**。

当前项目已经从早期的「只做 message_id -> turn_id 映射」升级为两层架构：

1. **Turn Mapper（轮次映射层）**：记录可见 Discord `message_id` 属于哪个 Hermes assistant turn。
2. **Reaction Audit Package（reaction 审查包）**：让 sidecar Discord bot 监听 `✅ / ❌ / 👌` reaction，重构该 turn 使用过的技能，并把结构化审查结果写入 SQLite。

不需要 fork 或直接修改 Hermes 源码。映射层通过 Hermes Gateway startup hook 安装；审查层是普通 Python package，可以被任何 Discord bot 引入。

---

## 这个项目解决什么问题

一个逻辑上的 Hermes 回复，在 Discord 里可能会表现为多条消息：

- tool-phase 状态消息 + 最终文本回复
- 由于 Discord 长度限制拆分出的多个 chunk
- thread/topic 内的后续消息

人工评分时，真正重要的问题不是：

```text
哪条 Discord 消息被点了 reaction？
```

而是：

```text
用户评价的是哪个 Hermes assistant turn？这个 turn 使用了哪些 skills？
```

这个仓库提供的就是这层桥接能力。

---

## 当前架构

```text
Hermes Gateway 发送 Discord 回复
  -> mapper.py patch DiscordAdapter.send()
  -> data/discord_turn_map.db 存储 discord_message_id -> turn_id

用户在 Hermes 回复上添加 ✅ / ❌ / 👌 reaction
  -> sidecar Discord bot 收到 on_raw_reaction_add / remove
  -> hermes_discord_skill_audit 解析 message_id -> turn_id
  -> 读取 ~/.hermes/state.db 中的 skill_view / skills_list / skill_manage 事件
  -> 在 skill_audit.db 中写入一条 turn-level review
  -> 在同一个 turn 的 sibling Discord messages 上同步 reaction 状态
```

合成的 `turn_id` 格式为：

```text
<session_id>:<assistant_db_id>
```

这是有意设计成 turn-level，而不是 message-level。一个 Hermes turn 即使产生三条 Discord 消息，也应该共享同一个评分槽位。

---

## 功能概览

### Turn mapping layer

- 在发送时捕获出站 Discord `message_id`。
- 支持通过 `raw_response.message_ids` 记录拆分消息的多个 chunk。
- 将持久映射写入 `data/discord_turn_map.db`。
- 用 `pending` / `resolved` 表示 Hermes `state.db` 延迟写入场景。
- 提供 `inspect_map.py` 做本地检查和 pending 对账。

### Reaction audit package

- 可导入 package：`hermes_discord_skill_audit`。
- 极简参考 bot：`examples/reaction_audit_bot.py`。
- 支持的评分 reaction：
  - `✅` -> `good` -> `0`
  - `❌` -> `not_good` -> `1`
  - `👌` -> `okay` -> `2`
- 强制 **每个用户对每个 Hermes turn 只能有一个 active score**。
- 避免用户在同一个 turn 的多个 split messages 上重复评分。
- 在同一 turn 的 sibling messages 上镜像 reaction。
- 允许用户从任意 mirrored chunk/message 上移除评分。
- 将结构化 review row 持久化到独立的 `skill_audit.db`。
- 审查 DB 与 turn-map DB 分离，职责清晰。

### Compatibility layer

`hermes_discord_skill_audit.reaction_audit` 保留为兼容 facade，用于兼容早期从单文件参考 bot 中导入 helper 的集成或测试。

---

## 仓库结构

```text
hermes-discord-skill-audit/
  mapper.py                         # Hermes Gateway send hook / Discord turn mapper
  inspect_map.py                    # 查看和回填 mapping rows 的 CLI
  pyproject.toml                    # package metadata 和测试配置
  .env.example                      # 参考 bot 的环境变量模板

  hermes_discord_skill_audit/
    __init__.py
    reaction_audit.py               # 兼容 facade / re-exports
    config.py                       # ReactionAuditConfig 与 env 解析
    state.py                        # handlers 共享的运行时配置
    scores.py                       # reaction -> score enum 映射
    audit_db.py                     # skill_audit.db schema 与 review 持久化
    turn_map.py                     # message_id -> turn_id 解析与 skill report 构建
    message_format.py               # 人类可读 report 格式化 helper
    discord_reactions.py            # on_raw_reaction_add/remove handlers

  examples/
    reaction_audit_bot.py           # 使用 package 的极简 sidecar Discord bot

  tests/
    test_mapper.py
    test_inspect_map.py
    test_reaction_audit_core.py
    test_reaction_audit_bot.py

  data/
    discord_turn_map.db             # 运行时生成的 mapper DB，不是源码
    skill_audit.db                  # 使用默认路径时生成的 audit DB

  video/
    example_use_case.gif            # 可选 demo media
    example_use_case.mp4
```

---

## 数据库职责

### Mapping DB：`discord_turn_map.db`

由 Hermes Gateway mapping hook 写入和维护。

主表：

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

关键行为：

- 新发送的消息如果当时还看不到最终 assistant row，可能先进入 `pending`。
- 后续对账或 reaction-time rescue 可以把它更新成 `resolved`。
- split chunks、tool/final sibling messages 可以共享同一个 `turn_id`。

### Audit DB：`skill_audit.db`

由 sidecar bot / reaction audit package 写入和维护。

主表：

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

关键行为：

- review 唯一性由逻辑层按 `(turn_id, reacted_by_user_id)` 控制，而不是按原始 `discord_message_id` 控制。
- `raw_report_json` 保存完整重构出的 skill report，方便后续分析。
- package 会做轻量 schema migration，例如给旧 DB 添加 `user_review_score`。

---

## 快速开始

### 1) 安装项目依赖

```bash
cd /path/to/hermes-discord-skill-audit
uv sync
```

如果要从另一个生产 bot 项目复用这个 package，可以把它以 editable 模式安装进该 bot 的 venv：

```bash
/path/to/sidecar-bot/.venv/bin/pip install -e /path/to/hermes-discord-skill-audit
```

### 2) 安装 Hermes Gateway mapping hook

创建 Hermes hook 目录：

```bash
mkdir -p ~/.hermes/hooks/discord-turn-map
```

创建 `~/.hermes/hooks/discord-turn-map/HOOK.yaml`：

```yaml
name: discord-turn-map
summary: Persist Discord message_id -> Hermes turn_id mappings on gateway startup.
events:
  - gateway:startup
```

创建 `~/.hermes/hooks/discord-turn-map/handler.py`：

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

创建或更新 hook 后，需要重启 Hermes Gateway。mapper 只会在 `gateway:startup` 时安装。

### 3) 配置 sidecar reaction audit bot

复制环境变量模板：

```bash
cp .env.example .env
```

至少设置：

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

sidecar Discord bot 需要具备：

- 读取目标频道/thread 消息的权限
- 接收 reaction events 的能力
- 添加/移除 reactions 的权限，用于镜像同步
- 如果希望使用 `clear_reaction()` 清理行为，最好也具备 Manage Messages 权限

### 4) 运行参考 bot

```bash
uv run python examples/reaction_audit_bot.py
```

之后，当允许的用户对 Hermes Agent 回复添加 `✅`、`❌` 或 `👌` 时，bot 会解析 Hermes turn 并在 `skill_audit.db` 中写入一条记录。

---

## 集成到已有 Discord bot

推荐的生产模式是：把面板、命令、cron widgets 等无关功能留在你自己的 bot 项目里，只从本 package 注册审查 handlers：

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

这样可以让这个仓库保持为可复用的审查工具，而生产 bot 继续维护自己的 dashboards、panels、commands 和 cron widgets。

---

## 检查与回填 turn map

显示最近 mapping rows：

```bash
uv run python inspect_map.py --limit 20
```

显示未解析 rows：

```bash
uv run python inspect_map.py --status pending --limit 20
```

检查指定 Discord message：

```bash
uv run python inspect_map.py --message-id 1497000000000000000
```

回填延迟写入造成的 pending rows：

```bash
uv run python inspect_map.py \
  --reconcile \
  --window-seconds 180 \
  --lookback-seconds 3600
```

reaction-time lookup 也会执行更宽的 rescue pass，由 `TURN_MAP_RESCUE_WINDOW_SECONDS` 控制。

---

## Reaction UX 规则

package 围绕 turn-level scoring 设计：

1. 用户在某个 Hermes turn 的任意一条消息上添加 `✅ / ❌ / 👌`。
2. package 为 `(turn_id, reacted_by_user_id)` 存储一条 review row。
3. bot 把同一个 emoji 镜像到该 turn 的 sibling Discord messages 上。
4. 如果用户在镜像 sibling 上点击同一个 emoji，review ownership 会转移到当前点击的消息，之后从这里移除也能自然清理整个 turn。
5. 如果同一用户尝试对同一 turn 添加不同评分，会尽量自动移除多余 reaction，并发送 warning。
6. 如果用户从任意 mirrored chunk/message 上移除已存储评分，turn-level review 会被删除，并清理 mirrored reactions。

这样可以避免一个 Hermes answer 被拆成多条 Discord 消息后产生重复评分。

---

## 开发

运行测试：

```bash
uv run pytest
```

运行格式化：

```bash
uv run black .
```

package discovery 配置有意只包含 `hermes_discord_skill_audit*`，并从 package 安装中排除 `data`、`video`、`examples`、`tests` 等运行时/示例/测试目录。

---

## 运维注意事项

- 修改 mapping hook 或 `mapper.py` 后，需要重启 Hermes Gateway。
- 如果 Gateway 运行期间删除了 mapping DB/table，需要重启 Gateway，让 hook 重新建表并恢复写入。
- 生产环境建议对 `DISCORD_TURN_MAP_DB_PATH`、`HERMES_STATE_DB_PATH`、`SKILL_AUDIT_DB_PATH` 使用绝对路径。
- 如果 reaction audit 提示 turn mapping 仍是 pending，可以等待或增大 rescue window；某些 workload 下 `state.db` 延迟写入是正常现象。
- 不要把 reaction audit rows 混进 turn-map DB；两个数据库职责不同。
- 参考 bot 是刻意保持极简的。生产 bot 应该 import package，而不是把无关运维代码复制进这个仓库。

---

## Roadmap

已完成：

- Discord message -> Hermes turn 映射。
- 可导入的 reaction audit package。
- reference sidecar bot。
- turn-level review 唯一性。
- sibling messages 之间的 reaction mirroring 与安全删除。
- 针对 delayed pending rows 的 reaction-time rescue。

计划中：

- 聚合 review data 的 export helpers。
- 按 score、时间范围、用户聚合的 skill-level summaries。
- 面向后续 skill curation workflow 的可选 bridge/exporter。
