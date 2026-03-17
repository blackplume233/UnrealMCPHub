import asyncio
import logging
import threading
from typing import Callable

from unrealhub.tools.discovery_tools import probe_unreal_mcp_with_fallback

logger = logging.getLogger(__name__)

PURGE_INTERVAL_CYCLES = 60
DISCOVER_INTERVAL_CYCLES = 6   # auto-discover every 6 cycles (30s at 5s interval)
STALE_HOURS = 1.0


class ProcessWatcher:
    """Background thread that monitors UE instance health and auto-discovers instances."""

    def __init__(self, get_state, get_config=None, interval: float = 5.0):
        self._get_state = get_state
        self._get_config = get_config
        self._interval = interval
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._on_crash_callbacks: list[Callable] = []
        self._cycle_count: int = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ProcessWatcher")
        self._thread.start()
        logger.info("ProcessWatcher started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("ProcessWatcher stopped")

    def on_crash(self, callback: Callable) -> None:
        """Register a callback for crash events. callback(instance_key: str)"""
        self._on_crash_callbacks.append(callback)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while not self._stop_event.is_set():
                try:
                    loop.run_until_complete(self._check_all())
                except Exception as e:
                    logger.error(f"Watcher check failed: {e}")
                self._stop_event.wait(self._interval)
        finally:
            loop.close()

    async def _check_all(self) -> None:
        state = self._get_state()
        for instance in state.list_instances():
            if instance.status != "online":
                continue
            await self._check_instance(state, instance)

        self._cycle_count += 1

        if self._cycle_count % DISCOVER_INTERVAL_CYCLES == 0:
            await self._auto_discover(state)

        if self._cycle_count >= PURGE_INTERVAL_CYCLES:
            self._cycle_count = 0
            purged = state.cleanup(max_age_hours=STALE_HOURS)
            if purged:
                logger.info("Watcher auto-cleaned stale instances: %s", purged)

    async def _check_instance(self, state, instance) -> None:
        from unrealhub.utils.process import is_process_alive

        probe = await probe_unreal_mcp_with_fallback(instance.url, timeout=2.0)

        if probe:
            matched_url, _ = probe
            state.upsert(
                port=instance.port,
                project_path=instance.project_path,
                url=matched_url,
                engine_root=instance.engine_root,
                pid=instance.pid,
                status="online",
            )
            return

        pid_alive = instance.pid and is_process_alive(instance.pid)
        if not pid_alive:
            was_online = instance.status == "online"
            state.update_status(instance.key, "offline")
            if was_online:
                state.increment_crash_count(instance.key)
                logger.warning("Instance %s crashed (PID gone, HTTP down)", instance.key)
                for cb in self._on_crash_callbacks:
                    try:
                        cb(instance.key)
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # Auto-discover: lightweight periodic scan
    # ------------------------------------------------------------------

    async def _auto_discover(self, state) -> None:
        """Periodically re-probe offline instances, scan priority ports,
        and register orphan UE processes. Reuses shared primitives from
        discovery_tools to avoid logic duplication."""
        from unrealhub.tools.discovery_tools import (
            reprobe_offline_instances,
            scan_ports_for_new,
            register_orphan_processes,
        )

        await reprobe_offline_instances(state)

        scan_ports = (
            self._get_config().get_scan_ports()
            if self._get_config
            else [8422, 8423, 8424, 8425]
        )
        await scan_ports_for_new(state, scan_ports)

        register_orphan_processes(state)
