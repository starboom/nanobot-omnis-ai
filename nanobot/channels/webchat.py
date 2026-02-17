"""WebChat channel: serves a browser chat UI over WebSocket."""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import WebChatConfig

try:
    import websockets
    from websockets.asyncio.server import serve as ws_serve
    from websockets.http11 import Response as WsResponse
    from websockets.datastructures import Headers as WsHeaders
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Static file loading
# ---------------------------------------------------------------------------
_STATIC_DIR = Path(__file__).parent / "static"
_HTML_CACHE: dict[str, str] = {}


def _load_html(name: str) -> str:
    """Load and cache an HTML file from the static directory."""
    if name not in _HTML_CACHE:
        path = _STATIC_DIR / name
        _HTML_CACHE[name] = path.read_text(encoding="utf-8")
    return _HTML_CACHE[name]


# ---------------------------------------------------------------------------
# Employee registry — updated by EmployeeManager at runtime
# ---------------------------------------------------------------------------
_employee_registry: list[dict[str, Any]] = []

# Paths injected by EmployeeManager at startup for API endpoints
_workspace_path: Path | None = None
_config_path: Path | None = None


def update_employee_registry(employees: list[dict[str, Any]]) -> None:
    """Replace the employee registry (called by EmployeeManager)."""
    global _employee_registry
    _employee_registry = list(employees)


def set_paths(workspace_path: Path, config_path: Path) -> None:
    """Set workspace and config paths (called by EmployeeManager at startup)."""
    global _workspace_path, _config_path
    _workspace_path = workspace_path
    _config_path = config_path


def _json_response(status: int, body: dict) -> "WsResponse":
    """Build a JSON HTTP response."""
    data = json.dumps(body, ensure_ascii=False).encode()
    headers = WsHeaders([
        ("Content-Type", "application/json; charset=utf-8"),
        ("Access-Control-Allow-Origin", "*"),
    ])
    phrase = "OK" if status == 200 else "Error"
    return WsResponse(status, phrase, headers, data)


def _handle_list_skills() -> "WsResponse":
    """GET /api/skills — return available skill directory names."""
    skills: list[str] = []
    if _workspace_path:
        skills_dir = _workspace_path / "skills"
        if skills_dir.is_dir():
            skills = sorted(
                d.name for d in skills_dir.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            )
    return _json_response(200, skills)


def _generate_skill_template(eid: str, name: str, skill: str) -> str:
    """Generate a default SKILL.md template for a new employee."""
    return f"""---
description: "{name} 数字员工 {eid}"
always: true
metadata: '{{"nanobot": {{"always": true}}}}'
---

# 数字员工 {eid} — {name}

你是一名 **{name}**。

## 你的身份

- 工号：{eid}
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

- 所有工作记录保存在 ~/.nanobot/workspace/agent/{eid}/ 目录下
- 工作日志记录到 ~/.nanobot/workspace/agent/{eid}/log.md
- 工作笔记记录到 ~/.nanobot/workspace/agent/{eid}/notes.md
"""


def _handle_create_employee(payload: dict) -> dict:
    """Create a new employee from payload dict. Returns result dict."""
    if not _config_path or not _config_path.exists():
        return {"ok": False, "error": "config.json 路径未配置"}

    eid = str(payload.get("id", "")).strip()
    name = str(payload.get("name", "")).strip()
    skill = str(payload.get("skill", "")).strip()
    port = int(payload.get("port", 0))
    # Optional: list of {"name": "xxx.md", "content": "..."} dicts
    skill_files = payload.get("skill_files", [])

    if not eid or not name or not skill:
        return {"ok": False, "error": "id、name、skill 为必填字段"}

    # Read → merge → write config.json
    try:
        raw = _config_path.read_text(encoding="utf-8")
        config_data = json.loads(raw)
    except Exception as e:
        return {"ok": False, "error": f"读取 config.json 失败: {e}"}

    employees = config_data.setdefault("employees", {})
    if eid in employees:
        return {"ok": False, "error": f"工号 {eid} 已存在"}

    new_emp = {"name": name, "skill": skill, "port": port, "enabled": True}
    employees[eid] = new_emp

    try:
        _config_path.write_text(
            json.dumps(config_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        return {"ok": False, "error": f"写入 config.json 失败: {e}"}

    # Initialize skill directory if it doesn't exist
    skill_created = False
    if _workspace_path:
        skill_dir = _workspace_path / "skills" / skill
        if not skill_dir.exists():
            try:
                skill_dir.mkdir(parents=True, exist_ok=True)
                if skill_files:
                    # Write uploaded files
                    for sf in skill_files:
                        fname = sf.get("name", "").strip()
                        fcontent = sf.get("content", "")
                        if fname and fcontent:
                            (skill_dir / fname).write_text(fcontent, encoding="utf-8")
                    # Ensure SKILL.md exists even if not uploaded
                    if not (skill_dir / "SKILL.md").exists():
                        (skill_dir / "SKILL.md").write_text(
                            _generate_skill_template(eid, name, skill),
                            encoding="utf-8",
                        )
                else:
                    # No files uploaded — generate template
                    (skill_dir / "SKILL.md").write_text(
                        _generate_skill_template(eid, name, skill),
                        encoding="utf-8",
                    )
                skill_created = True
                logger.info(f"Skill directory created: skills/{skill}/")
            except Exception as e:
                logger.warning(f"Failed to create skill directory: {e}")

    logger.info(f"New employee registered via dashboard: #{eid} ({name})")
    return {"ok": True, "employee": {"id": eid, **new_emp}, "skill_created": skill_created}


class WebChatChannel(BaseChannel):
    """Browser-based chat channel using WebSocket.

    Serves an HTML chat page at http://host:port/ and accepts
    WebSocket connections at ws://host:port/ws for real-time messaging.

    When agent_id is set, serves the employee chat UI.
    When agent_id is None (main webchat), serves the employee dashboard.
    """

    name = "webchat"

    def __init__(self, config: WebChatConfig, bus: MessageBus,
                 agent_id: str | None = None, skill: str | None = None,
                 agent_name: str | None = None, dashboard_port: int | None = None):
        super().__init__(config, bus)
        self.config = config
        self.agent_id = agent_id
        self.agent_name = agent_name or ""
        self.skill = skill or ""
        self.dashboard_port = dashboard_port
        if agent_id:
            self.name = f"webchat_{agent_id}"
        self._clients: dict[str, Any] = {}
        self._response_queues: dict[str, asyncio.Queue] = {}

    def _build_page(self) -> str:
        """Build the HTML page to serve."""
        if self.agent_id:
            # Employee chat page
            html = _load_html("chat.html")
            agent_info = json.dumps({
                "id": self.agent_id,
                "name": self.agent_name,
                "dashboard_url": f"http://{{host}}:{self.dashboard_port}/"
                                 if self.dashboard_port else "/",
            }, ensure_ascii=False)
            return html.replace(
                "</head>",
                f"<script>window.__AGENT__={agent_info};</script>\n</head>",
            )
        else:
            # Main dashboard
            html = _load_html("dashboard.html")
            data = json.dumps(_employee_registry, ensure_ascii=False)
            return html.replace(
                "</head>",
                f"<script>window.__EMPLOYEES__={data};</script>\n</head>",
            )

    async def start(self) -> None:
        if not WEBSOCKETS_AVAILABLE:
            logger.error("websockets not installed. Run: pip install websockets")
            return

        self._running = True
        asyncio.create_task(self._dispatch_responses())

        async def handler(websocket):
            if websocket.request and websocket.request.path == "/":
                pass

            session_id = str(uuid.uuid4())[:8]
            self._clients[session_id] = websocket
            self._response_queues[session_id] = asyncio.Queue()
            logger.info(f"WebChat client connected: {session_id}")

            try:
                await websocket.send(json.dumps({
                    "type": "connected",
                    "session_id": session_id,
                }))

                sender_task = asyncio.create_task(
                    self._send_responses(session_id, websocket)
                )

                async for raw in websocket:
                    try:
                        data = json.loads(raw)

                        # Dashboard: create employee via WebSocket
                        if data.get("type") == "create_employee":
                            result = _handle_create_employee(data.get("data", {}))
                            await websocket.send(json.dumps({
                                "type": "create_employee_result",
                                **result,
                            }))
                            continue

                        content = data.get("content", "").strip()
                        if not content:
                            continue

                        await websocket.send(json.dumps({"type": "typing"}))

                        metadata = {}
                        if self.skill:
                            metadata["skill"] = self.skill
                        if self.agent_id:
                            metadata["agent_id"] = self.agent_id
                        await self._handle_message(
                            sender_id=session_id,
                            chat_id=session_id,
                            content=content,
                            metadata=metadata,
                        )
                    except json.JSONDecodeError:
                        await websocket.send(json.dumps({
                            "type": "error",
                            "content": "Invalid message format",
                        }))
            except websockets.ConnectionClosed:
                pass
            finally:
                sender_task.cancel()
                self._clients.pop(session_id, None)
                self._response_queues.pop(session_id, None)
                logger.info(f"WebChat client disconnected: {session_id}")

        this = self

        async def process_request(connection, request):
            """Serve HTML page for non-WebSocket HTTP requests."""
            if request.path == "/" or request.path == "/index.html":
                # Resolve {host} placeholder with actual request host
                html = this._build_page()
                host = request.headers.get("Host", "").split(":")[0] or "localhost"
                html = html.replace("{host}", host)
                headers = WsHeaders([("Content-Type", "text/html; charset=utf-8")])
                return WsResponse(200, "OK", headers, html.encode())

            if request.path == "/api/employees":
                # GET only: list employees
                data = json.dumps(_employee_registry, ensure_ascii=False)
                headers = WsHeaders([
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Access-Control-Allow-Origin", "*"),
                ])
                return WsResponse(200, "OK", headers, data.encode())

            if request.path == "/api/skills":
                return _handle_list_skills()

            return None

        server = await ws_serve(
            handler,
            self.config.host,
            self.config.port,
            process_request=process_request,
        )

        logger.info(f"WebChat running at http://{self.config.host}:{self.config.port}/")

        while self._running:
            await asyncio.sleep(1)

        server.close()
        await server.wait_closed()

    async def stop(self) -> None:
        self._running = False
        for ws in list(self._clients.values()):
            try:
                await ws.close()
            except Exception:
                pass
        logger.info("WebChat stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Route agent response to the correct WebSocket client."""
        q = self._response_queues.get(msg.chat_id)
        if q:
            await q.put(msg.content)
        else:
            logger.warning(f"WebChat: no client for chat_id={msg.chat_id}")

    async def _send_responses(self, session_id: str, websocket) -> None:
        """Send queued responses to a specific client."""
        q = self._response_queues.get(session_id)
        if not q:
            return
        try:
            while True:
                content = await q.get()
                await websocket.send(json.dumps({
                    "type": "message",
                    "content": content,
                }))
        except (asyncio.CancelledError, Exception):
            pass

    async def _dispatch_responses(self) -> None:
        """Listen for outbound messages and route to clients."""
        pass
