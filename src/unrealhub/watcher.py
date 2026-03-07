import asyncio
import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


PURGE_INTERVAL_CYCLES = 60  # run purge every N health-check cycles
STALE_HOURS = 1.0


class ProcessWatcher:
    """Background thread that monitors UE instance health."""

    def __init__(self, get_state, interval: float = 5.0):
        """
        get_state: callable returning StateStore
        interval: seconds between health checks
        """
        self._get_state = get_state
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
        """Register a callback for crash events. callback(instance_id: str)"""
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
            if instance.status not in ("online", "starting"):
                continue
            await self._check_instance(state, instance)

        self._cycle_count += 1
        if self._cycle_count >= PURGE_INTERVAL_CYCLES:
            self._cycle_count = 0
            purged = state.purge_dead_instances(max_age_hours=STALE_HOURS)
            if purged:
                logger.info("Watcher auto-purged stale instances: %s", purged)

    async def _check_instance(self, state, instance) -> None:
        from unrealhub.utils.process import is_process_alive
        from unrealhub.ue_client import UEMCPClient

        pid_alive = True
        if instance.pid:
            pid_alive = is_process_alive(instance.pid)

        http_ok = await UEMCPClient.probe_endpoint(instance.url, timeout=2.0)

        if not pid_alive and not http_ok:
            if instance.status != "crashed":
                state.update_status(instance.auto_id, "crashed")
                state.increment_crash_count(instance.auto_id)
                state.save()
                logger.warning(f"Instance {instance.auto_id} CRASHED (PID gone, HTTP down)")
                for cb in self._on_crash_callbacks:
                    try:
                        cb(instance.auto_id)
                    except Exception:
                        pass
        elif http_ok:
            state.record_health_check(instance.auto_id, True)
            if instance.status != "online":
                state.update_status(instance.auto_id, "online")
                state.save()
        elif not http_ok and pid_alive:
            state.record_health_check(instance.auto_id, False)
