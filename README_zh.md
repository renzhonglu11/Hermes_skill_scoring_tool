# Hermes Skill Scoring Support Tool (Hermes 技能评分支持工具)

[English](README.md) | [中文](README_zh.md)

[![Status](https://img.shields.io/badge/status-active%20prototype-blue)](./README_zh.md)
[![Scope](https://img.shields.io/badge/scope-discord%20turn%20mapping-5865F2)](./README_zh.md)
[![Storage](https://img.shields.io/badge/storage-sqlite-green)](./README_zh.md)
[![Integration](https://img.shields.io/badge/integration-Hermes%20Gateway-orange)](./README_zh.md)

用于 **在 Discord 上对 Hermes Agent 进行人工技能审查** 的基础设施。

本项目捕获可见的 Hermes 回复对应的 Discord `message_id`，将其映射回正确的 Hermes `turn_id`，并为 **人工参与的技能评分** 提供所需的基础支持。

---

## 概述

当用户在 Discord 中对 Hermes 的回复做出反应（reaction）时，评分系统需要知道 **实际正在评判的是哪个 Hermes 对话轮次（turn）**。

这听起来很简单，但在实际应用中，一个逻辑上的 Hermes 对话轮次可能会产生：

- 一个标准的可见 Discord 回复
- 多个被拆分的 Discord 消息块
- 一个工具阶段（tool-phase）消息加上一个最终的文本回复

Hermes 在发送时知道出站的 Discord `message_id`，但它在原生层面上并没有为下游审查工作流持久化存储可靠的 `message_id -> turn_id` 映射。

本仓库填补了这一空白。

## 为什么存在这个项目

长远目标是让用户能够 **对 Hermes Agent 使用的技能进行人工评分**。

未来的审查操作示例：

- `✅` = 好 (good)
- `❌` = 不好 (not good)
- `👌` = 还行 (okay)

但在任何评分工作流能够被信任之前，系统必须可靠地回答：

```text
这条 Discord 消息属于哪个 Hermes 对话轮次？
```

本仓库专注于首先解决这个问题。

---

## 特性

- **核心映射器 (Core Mapper)**：在启动时挂载 Hermes Gateway，在 SQLite 中记录 `discord_message_id -> turn_id` 的映射。它修补了 `DiscordAdapter.send()` 以捕获出站结果，处理拆分/分块回复，并提供用于本地检查和延迟对账的 CLI 工具。
- **参考 Sidecar 机器人 (Reference Sidecar Bot)**：一个全功能的机器人，支持基于反应的人工评分工作流 (`✅ / ❌ / 👌`)。它强制每个对话轮次只有一个评分，在同级 Discord 消息中镜像反应，并将人工审查记录存储在独立的审计数据库中。
- **计划中 (Planned)**：导出/报告辅助工具、仪表板，以及按技能、评分和日期范围划分的聚合视图。

---

## 架构与流程

1. **映射 (Mapping)**：当 Hermes 发送 Discord 回复时，核心映射器捕获出站的 Discord `message_id`，解析会话上下文，并将 `message_id -> turn_id` 映射写入 `discord_turn_map.db`。
2. **审查 (Reviewing)**：sidecar 审查机器人监听用户对这些回复的反应，使用映射数据库解析确切的 Hermes 对话轮次，从 `~/.hermes/state.db` 重构技能使用情况，并将人工审查结果持久化到审计数据库中。
3. **分析 (Analytics)**：下游工具之后可以分析审计数据库，以重构技能使用情况并聚合审查结果。

通过使用稳定的 `<session_id>:<assistant_db_id>` 标识符作为 `turn_id`，系统确保人工评分发生在 **Hermes 对话轮次级别**，而不是原始消息级别。这防止了对拆分回复的重复评分，以及工具阶段消息与最终消息之间的混淆。

---

## 仓库结构

```text
/path/to/hermes-discord-skill-audit/
  mapper.py                      # 核心映射逻辑与 DiscordAdapter 修补
  inspect_map.py                 # 用于检查和对账的本地 CLI
  data/discord_turn_map.db       # 生成的 SQLite 映射数据库
  examples/reaction_audit_bot.py # 用于基于反应评分的参考机器人
  .env.example                   # 参考机器人的环境配置模板

~/.hermes/hooks/discord-turn-map/
  HOOK.yaml                      # Gateway 启动挂钩注册
  handler.py                     # 加载 mapper.py 的挂钩入口点
```

---

## 快速开始

### 0) 安装依赖 (使用 uv)

本项目使用 `uv` 进行快速依赖管理。如果你还没安装 `uv`，[请先安装它](https://docs.astral.sh/uv/)。
然后，设置项目环境：

```bash
uv sync
```

### 1) 创建 Hermes Hook目录

```bash
mkdir -p ~/.hermes/hooks/discord-turn-map
```

### 2) 创建 `HOOK.yaml`

路径:

```text
~/.hermes/hooks/discord-turn-map/HOOK.yaml
```

内容:

```yaml
name: discord-turn-map
summary: Persist Discord message_id -> Hermes turn_id mappings on gateway startup.
events:
  - gateway:startup
```

### 3) 创建 `handler.py`

路径:

```text
~/.hermes/hooks/discord-turn-map/handler.py
```

内容:

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

### 4) 重启 Hermes Gateway

挂钩在 `gateway:startup` 时加载，因此在创建或更新挂钩后必须重启网关。

如果不重启 Hermes Gateway，将不会记录任何新的映射。重启后，映射数据库 (`data/discord_turn_map.db`) 将自动创建。

### 5) 运行 Sidecar 机器人

为了实际捕获人类反应并记录分数，启动examples里Discord 机器人（确保机器人获得对话、消息,以及reaction的读写权限）。

确保已将 `.env.example` 复制为 `.env` 并填写了你的 `DISCORD_BOT_TOKEN`，然后启动机器人：

```bash
uv run examples/reaction_audit_bot.py
```

现在，当你在 Discord 中对 Hermes 回复做出 `✅`, `❌`, 或 `👌` 反应时，机器人将解析映射并将技能得分记录在 `data/skill_audit.db` 中。

---

## 用法

### 显示最近的记录

```bash
python3 /path/to/hermes-discord-skill-audit/inspect_map.py --limit 20
```

### 仅显示等待中（pending）的记录

```bash
python3 /path/to/hermes-discord-skill-audit/inspect_map.py --status pending --limit 20
```

### 查询特定的 Discord 消息 ID

```bash
python3 /path/to/hermes-discord-skill-audit/inspect_map.py --message-id 1497000000000000000
```

### 对账延迟的 pending 记录

```bash
python3 /path/to/hermes-discord-skill-audit/inspect_map.py \
  --reconcile \
  --window-seconds 180 \
  --lookback-seconds 3600
```

---

## 映射数据库

由 `mapper.py` 创建的 SQLite 表结构：

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

### 重要字段

- `discord_message_id` — 可见的 Discord 消息 ID
- `session_id` — Hermes 会话标识符
- `turn_id` — 解析后的 Hermes 对话轮次标识符
- `assistant_db_id` — 在 `state.db` 中的助手消息行 ID
- `reply_to_message_id` — 有助于回退解析
- `status` — `pending` 或 `resolved`
- `chunk_index` / `is_first_chunk` — 拆分消息元数据
- `sent_at` / `resolved_at` — 用于延迟对账的时间数据

---


## 路线图 (Roadmap)

- **第 1 & 2 阶段：映射与评分基础（已完成）**
  - 核心映射层 (`discord_message_id -> turn_id`)，支持消息拆分。
  - Sidecar 机器人集成，用于基于反应的评分，配有独立的审计数据库。
- **第 3 阶段：基于轮次的用户体验（进行中）**
  - 实现反应镜像和安全的评分替换。
  - *下一步:* 改进延迟解析逻辑和部分解析轮次的诊断。
- **第 4 阶段：报告与分析（计划中）**
  - 从 `state.db` 重构每个轮次的技能使用情况。
  - 构建导出辅助工具、仪表板，以及按技能/评分汇总的聚合视图。
- **第 5 阶段：自动化技能管理（计划中）**
  - 设计一个技能监控系统，根据本工具收集的人工审查分数自动管理技能。

---

## 常见问题 (FAQ)

### 这已经是一个评分产品了吗？

还没有。

目前这个仓库是使可靠的人工评分工作流成为可能的 **基础设施层**。

### 这会修改 Hermes 源代码吗？

不会。

该项目使用 Hermes 挂钩并在运行时修补（monkeypatch）`DiscordAdapter.send()`，从而保持集成最小侵入性。

### 为什么不从外部轮询 Discord？

因为外部轮询是不准确的。

捕获可见 Discord `message_id` 最可靠的地方是在发送时，直接从适配器结果中获取。

### 这是否支持一个对话轮次包含多条 Discord 消息？

是的。

这是本项目的核心存在理由之一。拆分的回复和同级消息都可以解析为同一个 Hermes `turn_id`。

---

## 运维说明

- `agent:end` 挂钩上下文不直接暴露最终的出站 Discord `message_id`
- 如果后续发送中缺少 `HERMES_SESSION_KEY`，回退解析可能需要依次使用 `reply_to_message_id`, `thread_id`, 然后是 `chat_id`
- 如果在 Hermes Gateway 仍在运行时删除了映射数据库/表，请重启网关以让 `_ensure_db()` 重新干净地创建它
- 在延迟写入的情况下，有效的助手消息行可能会在初始对账窗口之后很久才出现；救援逻辑可能需要一个更宽的窗口，比如 600 秒

---

## 示例用例

![Reaction Scoring Demo](./video/example_use_case.gif)

*(在这里查看原始高质量视频：[`video/example_use_case.mp4`](./video/example_use_case.mp4))*

当用户对 Hermes 回复做出反应（例如 ✅）时，机器人将：
1. 通过 `discord_message_turn_map` 将 Discord `message_id` 映射到 `turn_id`。
2. 从 `~/.hermes/state.db` 中获取工具执行上下文。
3. 将评分和技能使用情况记录到 `skill_audit.db` 中。
