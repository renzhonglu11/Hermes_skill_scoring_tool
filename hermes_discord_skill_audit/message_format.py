from __future__ import annotations


def build_skill_report_message(report: dict, reacted_by_user_id: int) -> str:
    events = report["events"]
    lines = [
        "✅ 已检测到对 Hermes 回复的勾选 reaction",
        f"- 触发用户: <@{reacted_by_user_id}>",
        f"- message_id: `{report['message_id']}`",
        f"- turn_id: `{report['turn_id']}`",
        f"- assistant_db_id: `{report['assistant_db_id']}`",
        f"- 映射状态: `{report['mapping_status']}` / `{report['resolution_source']}`",
    ]

    previous_user_preview = report.get("previous_user_preview", "")
    if previous_user_preview:
        preview = previous_user_preview.replace("\n", " ").strip()
        if len(preview) > 120:
            preview = preview[:117] + "..."
        lines.append(f"- 对应用户问题: {preview}")

    if not events:
        lines.append("- 结果: 该条回复未调用 `skill_view` / `skills_list` / `skill_manage`")
        return "\n".join(lines)

    status_counts = report["status_counts"]
    function_counts = report["function_counts"]
    lines.extend(
        [
            f"- skill 调用总数: {len(events)}",
            f"- 成功: {status_counts.get('succeeded', 0)} / 失败: {status_counts.get('failed', 0)} / 未知: {status_counts.get('unknown', 0)}",
            "- 调用类型: " + ", ".join(f"{name}×{count}" for name, count in sorted(function_counts.items())),
            "",
            "**技能明细**",
        ]
    )

    for event in events:
        status_icon = {
            "succeeded": "✅",
            "failed": "❌",
            "unknown": "❓",
        }.get(event["status"], "❓")
        detail = f"{status_icon} `{event['function']}` → `{event['target']}`"
        if event.get("error"):
            error_text = str(event["error"]).replace("\n", " ").strip()
            if len(error_text) > 120:
                error_text = error_text[:117] + "..."
            detail += f" ({error_text})"
        lines.append(detail)

    return "\n".join(lines)
