from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import state

BERLIN_TZ = ZoneInfo("Europe/Berlin")

def format_berlin_time(dt: datetime, include_date: bool = True) -> str:
    local_dt = dt.astimezone(BERLIN_TZ)
    return local_dt.strftime("%Y-%m-%d %H:%M:%S") if include_date else local_dt.strftime("%H:%M:%S")


def get_hermes_bin() -> str:
    """在 systemd 环境中稳定找到 hermes 可执行文件。"""
    candidates = [
        os.getenv("HERMES_BIN", "").strip(),
        shutil.which("hermes") or "",
        str(Path.home() / ".local" / "bin" / "hermes"),
        "/home/rz/.local/bin/hermes",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return "hermes"


def format_cron_datetime(raw_value: Optional[str]) -> str:
    if not raw_value:
        return "未设置"
    try:
        value = str(raw_value).split()[0]
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return format_berlin_time(dt)
    except Exception:
        return str(raw_value)


def get_cron_status() -> dict:
    """获取 Hermes cron 状态。优先解析 JSON，回退解析 CLI 文本输出。"""
    try:
        hermes_bin = get_hermes_bin()
        result = subprocess.run(
            [hermes_bin, "cron", "list"],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "HOME": str(Path.home())},
        )
        if result.returncode != 0:
            return {"error": (result.stderr or result.stdout or "unknown error").strip()}

        raw = (result.stdout or "").strip()
        if not raw:
            return {"crons": [], "error": None}

        crons: list[dict] = []
        try:
            jobs = json.loads(raw)
            for job in jobs if isinstance(jobs, list) else []:
                enabled = bool(job.get("enabled", True))
                paused = bool(job.get("paused", False))
                status = "⏸️" if paused or not enabled else "✅"
                crons.append(
                    {
                        "id": str(job.get("id") or job.get("job_id") or ""),
                        "name": str(job.get("name") or "(unnamed)"),
                        "status": status,
                        "next_run_display": format_cron_datetime(job.get("next_run") or job.get("next_run_at")),
                        "schedule": str(job.get("schedule") or ""),
                    }
                )
            return {"crons": crons, "error": None}
        except json.JSONDecodeError:
            pass

        current: dict[str, str] | None = None
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("┌") or stripped.startswith("└") or stripped.startswith("│"):
                continue

            if stripped.endswith("[active]") or stripped.endswith("[paused]") or stripped.endswith("[inactive]"):
                if current:
                    crons.append(current)
                parts = stripped.split()
                job_id = parts[0]
                status_token = parts[-1].strip("[]") if parts else "active"
                current = {
                    "id": job_id,
                    "name": job_id,
                    "status": "⏸️" if status_token in {"paused", "inactive"} else "✅",
                    "next_run_display": "未设置",
                    "schedule": "",
                }
                continue

            if current is None or ":" not in stripped:
                continue

            key, value = [part.strip() for part in stripped.split(":", 1)]
            if key == "Name":
                current["name"] = value or current["name"]
            elif key == "Schedule":
                current["schedule"] = value
            elif key == "Next run":
                current["next_run_display"] = format_cron_datetime(value)

        if current:
            crons.append(current)

        return {"crons": crons, "error": None}
    except Exception as e:
        state.logger.error("Failed to get cron status: %s", e)
        return {"error": str(e)}


def get_vps_stats() -> dict:
    """获取 VPS 资源状态"""
    try:
        import psutil

        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        boot_time = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
        uptime = datetime.now(timezone.utc) - boot_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        uptime_str = f"{days}天{hours}小时{minutes}分" if days > 0 else f"{hours}小时{minutes}分"

        return {
            "cpu": f"{cpu_percent:.1f}%",
            "memory": f"{memory.used / (1024 ** 3):.2f}G/{memory.total / (1024 ** 3):.2f}G ({memory.percent:.1f}%)",
            "disk": f"{disk.used / (1024 ** 3):.1f}G/{disk.total / (1024 ** 3):.1f}G ({disk.percent:.0f}%)",
            "uptime": uptime_str,
            "sampled_at": format_berlin_time(datetime.now(timezone.utc), include_date=False),
            "error": None,
        }
    except Exception as e:
        state.logger.error("Failed to get VPS stats: %s", e)
        return {"error": str(e)}


