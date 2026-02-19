"""Portal service: marketplace, auth, xiandou transactions."""

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanobot.portal import bridge
from nanobot.portal.db import init_db

try:
    from websockets.asyncio.server import serve as ws_serve
    from websockets.http11 import Response as WsResponse
    from websockets.datastructures import Headers as WsHeaders
    WEBSOCKETS_OK = True
except ImportError:
    WEBSOCKETS_OK = False

_STATIC_DIR = Path(__file__).parent / "static"
_HTML_CACHE: dict[str, str] = {}


def _load_html(name: str) -> str:
    if name not in _HTML_CACHE:
        path = _STATIC_DIR / name
        _HTML_CACHE[name] = path.read_text(encoding="utf-8")
    return _HTML_CACHE[name]


class PortalService:
    """Business portal running on its own port.

    Architecture:
    - GET endpoints served via process_request hook (HTML, read-only JSON APIs)
    - All mutations (auth, listings, transactions) via WebSocket messages
    - websockets 15 only allows GET in process_request, so POST is not possible
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 18880,
        http_port: int | None = None,
        employee_registry_fn: Callable[[], list[dict[str, Any]]] | None = None,
        config_path: Path | None = None,
        workspace_path: Path | None = None,
    ):
        self.host = host
        self.port = port
        self.http_port = http_port or (port + 1)
        self._running = False
        if employee_registry_fn:
            bridge.set_registry_source(employee_registry_fn)
        if config_path:
            bridge.set_config_source(config_path)
        if workspace_path:
            bridge.set_workspace_source(workspace_path)

    async def start(self) -> None:
        if not WEBSOCKETS_OK:
            logger.error("Portal: websockets library not available")
            return

        await init_db()
        logger.info("Portal: database initialized")

        async def ws_handler(websocket: Any) -> None:
            """Handle WebSocket connections for mutations and real-time."""
            from nanobot.portal.handlers import handle_ws_message
            try:
                async for raw in websocket:
                    try:
                        msg = json.loads(raw)
                        result = await handle_ws_message(msg)
                        await websocket.send(json.dumps(result, ensure_ascii=False))
                    except json.JSONDecodeError:
                        await websocket.send(json.dumps({"error": "invalid JSON"}))
                    except Exception as e:
                        logger.error(f"Portal WS error: {e}")
                        await websocket.send(json.dumps({"error": str(e)}))
            except Exception:
                pass

        async def process_request(connection: Any, request: Any) -> "WsResponse | None":
            """Handle GET requests only (websockets 15 rejects non-GET)."""
            if request.path == "/ws":
                return None  # upgrade to WebSocket

            from nanobot.portal.handlers import handle_get_request

            # Extract auth token from query string
            path = request.path
            token = None
            query_params = {}
            if "?" in path:
                path, qs = path.split("?", 1)
                for param in qs.split("&"):
                    if "=" in param:
                        k, v = param.split("=", 1)
                        if k == "token":
                            token = v
                        else:
                            query_params[k] = v

            # Also check Authorization header
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]

            resp = await handle_get_request(
                path=path,
                token=token,
                serve_html_fn=self._serve_portal_html,
                query_params=query_params,
            )
            return resp

        server = await ws_serve(
            ws_handler,
            self.host,
            self.port,
            process_request=process_request,
        )
        self._running = True
        logger.info(f"Portal WS  at http://{self.host}:{self.port}/")
        logger.info(f"Portal API at http://{self.host}:{self.http_port}/")

        try:
            await asyncio.gather(
                self._run_http_api(),
                self._wait_loop(),
            )
        finally:
            server.close()
            await server.wait_closed()

    async def stop(self) -> None:
        self._running = False

    async def _wait_loop(self) -> None:
        while self._running:
            await asyncio.sleep(1)

    async def _run_http_api(self) -> None:
        """Raw asyncio HTTP server for POST API (bypasses websockets GET-only limit)."""
        from nanobot.portal.handlers import handle_get_request, handle_ws_message

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                parts = line.decode(errors="replace").split()
                if len(parts) < 2:
                    return
                method, raw_path = parts[0], parts[1]

                headers: dict[str, str] = {}
                content_length = 0
                while True:
                    hline = await asyncio.wait_for(reader.readline(), timeout=10)
                    if hline in (b"\r\n", b"\n", b""):
                        break
                    decoded = hline.decode(errors="replace").strip()
                    if ":" in decoded:
                        k, v = decoded.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                    if hline.lower().startswith(b"content-length:"):
                        content_length = int(hline.split(b":", 1)[1].strip())

                body = await reader.read(content_length) if content_length else b""

                # CORS preflight
                if method == "OPTIONS":
                    resp_body = b""
                    status = "204 No Content"
                    extra_headers = (
                        "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                        "Access-Control-Allow-Headers: Content-Type, Authorization\r\n"
                    )
                elif method == "POST":
                    status, resp_body = await self._handle_post(
                        raw_path, body, headers, handle_ws_message
                    )
                    extra_headers = ""
                elif method == "GET":
                    status, resp_body = await self._handle_get(
                        raw_path, headers, handle_get_request
                    )
                    extra_headers = ""
                else:
                    resp_body = json.dumps({"error": "method not allowed"}).encode()
                    status = "405 Method Not Allowed"
                    extra_headers = ""

                response = (
                    f"HTTP/1.1 {status}\r\n"
                    f"Content-Type: application/json; charset=utf-8\r\n"
                    f"Access-Control-Allow-Origin: *\r\n"
                    f"{extra_headers}"
                    f"Content-Length: {len(resp_body)}\r\n"
                    f"Connection: close\r\n\r\n"
                ).encode() + resp_body
                writer.write(response)
                await writer.drain()
            except Exception as e:
                logger.debug(f"Portal HTTP API error: {e}")
            finally:
                writer.close()

        server = await asyncio.start_server(handle, self.host, self.http_port)
        while self._running:
            await asyncio.sleep(1)
        server.close()
        await server.wait_closed()

    def _serve_portal_html(self) -> "WsResponse":
        html = _load_html("portal.html")
        html = html.replace("{host}", f"{self.host}:{self.port}")
        headers = WsHeaders([("Content-Type", "text/html; charset=utf-8")])
        return WsResponse(200, "OK", headers, html.encode())

    async def _handle_post(self, raw_path: str, body: bytes, headers: dict,
                           handle_ws_message: Any) -> tuple[str, bytes]:
        """Route POST requests to existing WS message handlers."""
        token = None
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]

        try:
            msg = json.loads(body) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "400 Bad Request", json.dumps({"error": "invalid JSON"}).encode()

        if not msg.get("type"):
            return "400 Bad Request", json.dumps({"error": "missing 'type' field"}).encode()

        if token:
            msg["token"] = token

        result = await handle_ws_message(msg)
        resp_body = json.dumps(result, ensure_ascii=False).encode()
        ok = result.get("ok", False)
        status = "200 OK" if ok else "400 Bad Request"
        if result.get("error") == "unauthorized":
            status = "401 Unauthorized"
        return status, resp_body

    async def _handle_get(self, raw_path: str, headers: dict,
                          handle_get_request: Any) -> tuple[str, bytes]:
        """Route GET requests to existing GET handlers."""
        path = raw_path
        token = None
        query_params: dict[str, str] = {}
        if "?" in path:
            path, qs = path.split("?", 1)
            for param in qs.split("&"):
                if "=" in param:
                    k, v = param.split("=", 1)
                    if k == "token":
                        token = v
                    else:
                        query_params[k] = v

        auth = headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]

        resp = await handle_get_request(
            path=path, token=token,
            serve_html_fn=None,
            query_params=query_params,
        )
        if resp is None:
            return "404 Not Found", json.dumps({"error": "not found"}).encode()
        return f"{resp.status_code} {resp.reason_phrase}", resp.body
