import asyncio
import time
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from unrealhub.watcher import ProcessWatcher
from unrealhub.state import StateStore, InstanceState


class TestProcessWatcher:
    def _make_state(self, tmp_home):
        store = StateStore()
        return store

    def test_start_stop(self, tmp_home):
        store = self._make_state(tmp_home)
        watcher = ProcessWatcher(lambda: store, interval=0.1)
        watcher.start()
        assert watcher._thread is not None
        assert watcher._thread.is_alive()

        watcher.start()

        watcher.stop()
        assert not watcher._thread.is_alive()

    def test_on_crash_callback(self, tmp_home):
        store = self._make_state(tmp_home)
        watcher = ProcessWatcher(lambda: store, interval=60)
        crashed_keys = []
        watcher.on_crash(lambda key: crashed_keys.append(key))
        assert len(watcher._on_crash_callbacks) == 1

    @pytest.mark.asyncio
    async def test_check_all_skips_offline(self, tmp_home):
        store = self._make_state(tmp_home)
        store.upsert(port=8422, project_path="G:/Proj/A.uproject", status="offline")

        watcher = ProcessWatcher(lambda: store, interval=60)
        with patch.object(watcher, "_check_instance", new_callable=AsyncMock) as mock_check:
            await watcher._check_all()
            mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_all_checks_online(self, tmp_home):
        store = self._make_state(tmp_home)
        store.upsert(port=8422, project_path="G:/Proj/A.uproject", pid=1234)

        watcher = ProcessWatcher(lambda: store, interval=60)
        with patch.object(watcher, "_check_instance", new_callable=AsyncMock) as mock_check:
            await watcher._check_all()
            mock_check.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_instance_crash(self, tmp_home):
        store = self._make_state(tmp_home)
        store.upsert(
            port=8422, project_path="G:/Proj/A.uproject", pid=99999, status="online"
        )

        crashed_keys = []
        watcher = ProcessWatcher(lambda: store, interval=60)
        watcher.on_crash(lambda key: crashed_keys.append(key))

        with patch("unrealhub.utils.process.is_process_alive", return_value=False), \
             patch("unrealhub.watcher.probe_unreal_mcp_with_fallback", new_callable=AsyncMock, return_value=None):
            await watcher._check_instance(store, store.get_instance("A:8422"))

        updated = store.get_instance("A:8422")
        assert updated.status == "offline"
        assert updated.crash_count == 1
        assert "A:8422" in crashed_keys

    @pytest.mark.asyncio
    async def test_check_instance_healthy(self, tmp_home):
        store = self._make_state(tmp_home)
        store.upsert(port=8422, project_path="G:/Proj/A.uproject", pid=1234)

        watcher = ProcessWatcher(lambda: store, interval=60)

        with patch("unrealhub.utils.process.is_process_alive", return_value=True), \
             patch(
                 "unrealhub.watcher.probe_unreal_mcp_with_fallback",
                 new_callable=AsyncMock,
                 return_value=("http://127.0.0.1:8422/mcp", {"server_name": "Remote Unreal MCP"}),
             ):
            await watcher._check_instance(store, store.get_instance("A:8422"))

        updated = store.get_instance("A:8422")
        assert updated.status == "online"
        assert updated.url == "http://127.0.0.1:8422/mcp"

    @pytest.mark.asyncio
    async def test_check_instance_pid_alive_http_down(self, tmp_home):
        """PID alive but HTTP down: status stays online (not a crash)."""
        store = self._make_state(tmp_home)
        store.upsert(port=8422, project_path="G:/Proj/A.uproject", pid=1234)

        watcher = ProcessWatcher(lambda: store, interval=60)

        with patch("unrealhub.utils.process.is_process_alive", return_value=True), \
             patch("unrealhub.watcher.probe_unreal_mcp_with_fallback", new_callable=AsyncMock, return_value=None):
            await watcher._check_instance(store, store.get_instance("A:8422"))

        updated = store.get_instance("A:8422")
        assert updated.status == "online"

    @pytest.mark.asyncio
    async def test_no_double_crash(self, tmp_home):
        store = self._make_state(tmp_home)
        store.upsert(port=8422, project_path="G:/Proj/A.uproject", pid=99999)
        store.update_status("A:8422", "offline")
        store.increment_crash_count("A:8422")

        watcher = ProcessWatcher(lambda: store, interval=60)
        with patch("unrealhub.utils.process.is_process_alive", return_value=False), \
             patch("unrealhub.watcher.probe_unreal_mcp_with_fallback", new_callable=AsyncMock, return_value=None):
            await watcher._check_instance(store, store.get_instance("A:8422"))

        assert store.get_instance("A:8422").crash_count == 1
