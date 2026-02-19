"""Bridge to the EmployeeManager employee registry and config.json."""

import json
from pathlib import Path
from typing import Any, Callable

from loguru import logger

_registry_fn: Callable[[], list[dict[str, Any]]] | None = None
_config_path: Path | None = None
_workspace_path: Path | None = None


def set_registry_source(fn: Callable[[], list[dict[str, Any]]]) -> None:
    global _registry_fn
    _registry_fn = fn


def set_config_source(config_path: Path) -> None:
    """Store the config.json path so we can write employee entries."""
    global _config_path
    _config_path = config_path


def set_workspace_source(workspace_path: Path) -> None:
    """Store the workspace path for skill file I/O."""
    global _workspace_path
    _workspace_path = workspace_path


def get_employees() -> list[dict[str, Any]]:
    return _registry_fn() if _registry_fn else []


def get_agent_port(employee_id: str) -> int | None:
    for emp in get_employees():
        if str(emp.get("id")) == str(employee_id):
            return emp.get("port")
    return None


def _read_config() -> dict:
    """Read config.json as raw dict (camelCase keys)."""
    if not _config_path or not _config_path.exists():
        return {}
    with open(_config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_config(data: dict) -> None:
    """Write dict back to config.json."""
    if not _config_path:
        return
    _config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def add_employee_to_config(employee_id: str, name: str, skill: str, port: int = 0) -> bool:
    """Add or update an employee in config.json. Returns True on success."""
    try:
        data = _read_config()
        employees = data.setdefault("employees", {})
        employees[employee_id] = {
            "name": name,
            "skill": skill,
            "port": port,
            "enabled": True,
        }
        _write_config(data)
        logger.info(f"Portal: added employee '{employee_id}' to config.json")
        return True
    except Exception as e:
        logger.error(f"Portal: failed to add employee to config.json: {e}")
        return False


def remove_employee_from_config(employee_id: str) -> bool:
    """Disable an employee in config.json. Returns True on success."""
    try:
        data = _read_config()
        employees = data.get("employees", {})
        if employee_id in employees:
            employees[employee_id]["enabled"] = False
            _write_config(data)
            logger.info(f"Portal: disabled employee '{employee_id}' in config.json")
        return True
    except Exception as e:
        logger.error(f"Portal: failed to disable employee in config.json: {e}")
        return False


def read_skill_content(skill_name: str) -> str | None:
    """Read SKILL.md content for a given skill. Returns None if not found."""
    if not _workspace_path or not skill_name:
        return None
    skill_file = _workspace_path / "skills" / skill_name / "SKILL.md"
    if skill_file.exists():
        return skill_file.read_text(encoding="utf-8")
    return None


def save_skill_content(skill_name: str, content: str) -> bool:
    """Write SKILL.md content for a given skill. Creates directory if needed."""
    if not _workspace_path or not skill_name or not content:
        return False
    try:
        skill_dir = _workspace_path / "skills" / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        logger.info(f"Portal: saved skill content for '{skill_name}'")
        return True
    except Exception as e:
        logger.error(f"Portal: failed to save skill '{skill_name}': {e}")
        return False


def generate_skill_template(employee_id: str, name: str, skill: str) -> str:
    """Generate a default SKILL.md template for a new employee."""
    return f"""---
description: "{name} 数字员工 {employee_id}"
always: true
metadata: '{{"nanobot": {{"always": true}}}}'
---

# 数字员工 {employee_id} — {name}

你是一名 **{name}**。

## 你的身份

- 工号：{employee_id}
- 岗位：{name}
- 专长：请在此处描述专长领域
- 风格：请在此处描述工作风格

## 核心职责

请在此处描述核心职责和工作内容。

## 工作方式

- 先理解需求再行动
- 输出内容要有结构感
- 不确定的内容如实说明

## 行为约束

- 所有工作记录保存在 ~/.nanobot/workspace/agent/{employee_id}/ 目录下
- 工作日志记录到 ~/.nanobot/workspace/agent/{employee_id}/log.md
- 工作笔记记录到 ~/.nanobot/workspace/agent/{employee_id}/notes.md
"""
