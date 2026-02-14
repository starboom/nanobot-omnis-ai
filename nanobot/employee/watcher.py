"""Config file watcher for hot-reloading employee changes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from loguru import logger


class ConfigWatcher:
    """Watch config.json for changes and trigger employee reload.

    Uses watchdog to monitor the file.  Only employees-related changes
    trigger a reload — other config sections are ignored.
    """

    def __init__(self, config_path: Path, on_change) -> None:
        self.config_path = config_path
        self._on_change = on_change  # async callable(new_config)
        self._observer = None
        self._last_employees: dict | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start watching the config file."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            logger.warning("watchdog not installed, config hot-reload disabled")
            return

        self._last_employees = self._read_employees()
        self._loop = loop

        parent_dir = str(self.config_path.parent)
        filename = self.config_path.name

        class _Handler(FileSystemEventHandler):
            def __init__(self, watcher: ConfigWatcher):
                self.watcher = watcher

            def on_modified(self, event):
                if event.is_directory:
                    return
                if Path(event.src_path).name == filename:
                    self.watcher._handle_change()

        self._observer = Observer()
        self._observer.schedule(_Handler(self), parent_dir, recursive=False)
        self._observer.daemon = True
        self._observer.start()
        logger.info(f"ConfigWatcher started, monitoring {self.config_path}")

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
            logger.info("ConfigWatcher stopped")

    def _read_employees(self) -> dict | None:
        try:
            with open(self.config_path) as f:
                data = json.load(f)
            return data.get("employees")
        except Exception:
            return None

    def _handle_change(self) -> None:
        new_employees = self._read_employees()
        if new_employees is None:
            return
        if new_employees == self._last_employees:
            return
        self._last_employees = new_employees
        logger.info("Config change detected in employees section, reloading...")
        asyncio.run_coroutine_threadsafe(self._reload(), self._loop)

    async def _reload(self) -> None:
        try:
            from nanobot.config.loader import load_config
            new_config = load_config(self.config_path)
            await self._on_change(new_config)
        except Exception as e:
            logger.error(f"Failed to reload config: {e}")
