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


def update_employee_registry(employees: list[dict[str, Any]]) -> None:
    """Replace the employee registry (called by EmployeeManager)."""
    global _employee_registry
    _employee_registry = list(employees)


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
                data = json.dumps(_employee_registry, ensure_ascii=False)
                headers = WsHeaders([
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Access-Control-Allow-Origin", "*"),
                ])
                return WsResponse(200, "OK", headers, data.encode())
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
