"""Employee manager: creates and manages webchat channels for digital employees."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config, EmployeeConfig, WebChatConfig

# Port range for auto-assignment when port is 0 or conflicts
_AUTO_PORT_START = 19001
_AUTO_PORT_END = 19999


class EmployeeManager:
    """Manage digital employee lifecycle.

    Each enabled employee gets a dedicated WebChatChannel registered
    in the ChannelManager.  The manager also handles hot-reload by
    diffing old vs new employee configs.
    """

    def __init__(
        self,
        config: Config,
        channel_manager: ChannelManager,
        bus: MessageBus,
        workspace: Path,
    ) -> None:
        self.config = config
        self.channel_manager = channel_manager
        self.bus = bus
        self.workspace = workspace
        self._active: dict[str, EmployeeConfig] = {}
        # Track which ports are in use: port -> employee_id
        self._used_ports: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Register all enabled employees as webchat channels."""
        # Inject workspace/config paths into webchat module for API endpoints
        from nanobot.channels.webchat import set_paths
        from nanobot.config.loader import get_config_path
        set_paths(self.workspace, get_config_path())

        # Collect reserved ports (gateway, main webchat, etc.)
        self._collect_reserved_ports()

        for eid, emp in self.config.employees.items():
            if emp.enabled:
                await self._add_employee(eid, emp)

    async def stop(self) -> None:
        """Stop all employee channels."""
        for eid in list(self._active):
            await self._remove_employee(eid)

    async def reload(self, new_config: Config) -> None:
        """Hot-reload: diff employees and apply changes."""
        old_ids = set(self._active)
        new_employees = {
            eid: emp
            for eid, emp in new_config.employees.items()
            if emp.enabled
        }
        new_ids = set(new_employees)

        # Remove employees no longer present or disabled
        for eid in old_ids - new_ids:
            logger.info(f"Employee #{eid} removed or disabled, stopping channel")
            await self._remove_employee(eid)

        # Add new employees
        for eid in new_ids - old_ids:
            logger.info(f"Employee #{eid} added, starting channel")
            await self._add_employee(eid, new_employees[eid])

        # Update changed employees (port or skill changed)
        for eid in old_ids & new_ids:
            old = self._active[eid]
            new = new_employees[eid]
            if old.port != new.port or old.skill != new.skill:
                logger.info(f"Employee #{eid} config changed, restarting channel")
                await self._remove_employee(eid)
                await self._add_employee(eid, new)

        self.config = new_config

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_reserved_ports(self) -> None:
        """Collect ports already used by gateway and channels to avoid conflicts."""
        # Gateway port
        gw_port = self.config.gateway.port
        if gw_port:
            self._used_ports[gw_port] = "__gateway__"

        # Main webchat port
        wc_port = self.config.channels.webchat.port
        if self.config.channels.webchat.enabled and wc_port:
            self._used_ports[wc_port] = "__webchat__"

    def _resolve_port(self, eid: str, requested: int) -> int:
        """Resolve a valid port for the employee.

        - If requested port > 0 and available, use it.
        - If requested port conflicts, auto-assign and warn.
        - If requested port is 0, auto-assign.
        """
        if requested > 0:
            conflict = self._used_ports.get(requested)
            if conflict is None:
                return requested
            logger.warning(
                f"Employee #{eid}: port {requested} conflicts with "
                f"{'employee #' + conflict if not conflict.startswith('__') else conflict.strip('_')}. "
                f"Auto-assigning a new port."
            )

        # Auto-assign from range
        for port in range(_AUTO_PORT_START, _AUTO_PORT_END + 1):
            if port not in self._used_ports:
                if requested > 0:
                    logger.info(f"Employee #{eid}: reassigned to port {port}")
                else:
                    logger.info(f"Employee #{eid}: auto-assigned port {port}")
                return port

        raise RuntimeError(
            f"Employee #{eid}: no available port in range "
            f"{_AUTO_PORT_START}-{_AUTO_PORT_END}"
        )

    def _ensure_work_dir(self, employee_id: str, emp: EmployeeConfig) -> Path:
        """Initialize the full working environment for an employee.

        Creates:
          agent/{id}/knowledge/   — 知识库（用户放 .md 资料）
          agent/{id}/log.md       — 工作日志
          agent/{id}/notes.md     — 工作笔记
        """
        work_dir = self.workspace / "agent" / employee_id
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "knowledge").mkdir(exist_ok=True)

        # Initialize work log
        log_file = work_dir / "log.md"
        if not log_file.exists():
            log_file.write_text(
                f"# 工作日志 — {emp.name}（{employee_id}）\n\n",
                encoding="utf-8",
            )

        # Initialize work notes
        notes_file = work_dir / "notes.md"
        if not notes_file.exists():
            notes_file.write_text(
                f"# 工作笔记 — {emp.name}（{employee_id}）\n\n",
                encoding="utf-8",
            )

        return work_dir

    async def _add_employee(self, eid: str, emp: EmployeeConfig) -> None:
        from nanobot.channels.webchat import WebChatChannel

        self._ensure_work_dir(eid, emp)

        # Resolve port (conflict detection + auto-assign)
        port = self._resolve_port(eid, emp.port)
        self._used_ports[port] = eid

        # Dashboard port for the back link in chat UI
        dashboard_port = (
            self.config.channels.webchat.port
            if self.config.channels.webchat.enabled else None
        )

        channel_name = f"webchat_{eid}"
        wc_config = WebChatConfig(
            enabled=True,
            host="0.0.0.0",
            port=port,
        )
        channel = WebChatChannel(
            wc_config, self.bus,
            agent_id=eid, skill=emp.skill,
            agent_name=emp.name, dashboard_port=dashboard_port,
        )
        await self.channel_manager.add_channel(channel_name, channel)
        self._active[eid] = emp
        logger.info(
            f"Employee #{eid} ({emp.name}) online — "
            f"port {port}, skill={emp.skill or 'default'}"
        )

        self._sync_registry()

    async def _remove_employee(self, eid: str) -> None:
        channel_name = f"webchat_{eid}"
        await self.channel_manager.remove_channel(channel_name)
        # Release port
        for port, owner in list(self._used_ports.items()):
            if owner == eid:
                del self._used_ports[port]
                break
        self._active.pop(eid, None)
        logger.info(f"Employee #{eid} offline")

        self._sync_registry()

    def _sync_registry(self) -> None:
        """Push current employee list to the webchat dashboard registry."""
        from nanobot.channels.webchat import update_employee_registry

        entries = []
        for eid, emp in self._active.items():
            # Find the actual port assigned to this employee
            port = next(
                (p for p, owner in self._used_ports.items() if owner == eid),
                0,
            )
            entries.append({
                "id": eid,
                "name": emp.name,
                "skill": emp.skill,
                "port": port,
                "enabled": emp.enabled,
            })
        update_employee_registry(entries)
