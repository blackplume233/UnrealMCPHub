"""Tests for zombie cleanup and dead-instance dedup logic."""
from unittest.mock import patch, MagicMock

import pytest

from unrealhub.state import StateStore
from unrealhub.tools.discovery_tools import _mark_zombies_offline


class TestDedupDeadByProject:
    """dedup_dead_by_project: consolidate dead instances per project_path."""

    def test_no_duplicates_noop(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422,
                                project_path="G:/Proj/A.uproject")
        store.update_status("ue1", "crashed")
        removed = store.dedup_dead_by_project()
        assert removed == []
        assert len(store.list_instances()) == 1

    def test_two_dead_same_project_keeps_newest(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422,
                                project_path="G:/Proj/A.uproject")
        store.register_instance(url="http://localhost:8423/mcp", port=8423,
                                project_path="G:/Proj/A.uproject")
        store.update_status("ue1", "crashed")
        store.update_status("ue2", "offline")
        removed = store.dedup_dead_by_project()
        assert len(removed) == 1
        assert "ue1" in removed
        remaining = store.list_instances()
        assert len(remaining) == 1
        assert remaining[0].auto_id == "ue2"

    def test_three_dead_same_project_keeps_one(self, tmp_home):
        store = StateStore()
        for port in (8422, 8423, 8424):
            store.register_instance(url=f"http://localhost:{port}/mcp",
                                    port=port,
                                    project_path="G:/Proj/A.uproject")
        store.update_status("ue1", "crashed")
        store.update_status("ue2", "crashed")
        store.update_status("ue3", "offline")
        removed = store.dedup_dead_by_project()
        assert len(removed) == 2
        remaining = store.list_instances()
        assert len(remaining) == 1
        assert remaining[0].auto_id == "ue3"

    def test_online_instances_untouched(self, tmp_home):
        """Multiple online instances for the same project must NOT be removed."""
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422,
                                project_path="G:/Proj/A.uproject", pid=100)
        store.register_instance(url="http://localhost:8423/mcp", port=8423,
                                project_path="G:/Proj/A.uproject", pid=200)
        removed = store.dedup_dead_by_project()
        assert removed == []
        assert len(store.list_instances()) == 2

    def test_mixed_online_and_dead_keeps_all_alive(self, tmp_home):
        """Online stays, dead duplicates get consolidated."""
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422,
                                project_path="G:/Proj/A.uproject", pid=100)
        store.register_instance(url="http://localhost:8423/mcp", port=8423,
                                project_path="G:/Proj/A.uproject")
        store.register_instance(url="http://localhost:8424/mcp", port=8424,
                                project_path="G:/Proj/A.uproject")
        store.update_status("ue2", "crashed")
        store.update_status("ue3", "offline")

        removed = store.dedup_dead_by_project()
        assert len(removed) == 1
        remaining = store.list_instances()
        assert len(remaining) == 2
        remaining_ids = {i.auto_id for i in remaining}
        assert "ue1" in remaining_ids

    def test_different_projects_independent(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422,
                                project_path="G:/Proj/A.uproject")
        store.register_instance(url="http://localhost:8423/mcp", port=8423,
                                project_path="G:/Proj/B.uproject")
        store.update_status("ue1", "crashed")
        store.update_status("ue2", "crashed")
        removed = store.dedup_dead_by_project()
        assert removed == []
        assert len(store.list_instances()) == 2

    def test_no_project_path_ignored(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        store.register_instance(url="http://localhost:8423/mcp", port=8423)
        store.update_status("ue1", "crashed")
        store.update_status("ue2", "crashed")
        removed = store.dedup_dead_by_project()
        assert removed == []

    def test_active_instance_reassigned_on_removal(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422,
                                project_path="G:/Proj/A.uproject")
        store.register_instance(url="http://localhost:8423/mcp", port=8423,
                                project_path="G:/Proj/A.uproject")
        store.set_active("ue1")
        store.update_status("ue1", "crashed")
        store.update_status("ue2", "offline")
        store.dedup_dead_by_project()
        active = store.get_active_instance()
        assert active is not None


class TestMarkZombiesOffline:
    """_mark_zombies_offline: detect and mark zombie instances."""

    def test_no_zombies_when_all_respond(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422, pid=100)
        marked = _mark_zombies_offline(store, [8422], {8422})
        assert marked == []
        assert store.get_instance("ue1").status == "online"

    @patch("unrealhub.tools.discovery_tools.is_process_alive", return_value=False)
    def test_zombie_marked_offline(self, mock_alive, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422, pid=100)
        marked = _mark_zombies_offline(store, [8422], set())
        assert marked == ["ue1"]
        assert store.get_instance("ue1").status == "offline"

    @patch("unrealhub.tools.discovery_tools.is_process_alive", return_value=True)
    def test_alive_pid_not_marked(self, mock_alive, tmp_home):
        """Port didn't respond but PID is alive -> not a zombie (maybe temporarily busy)."""
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422, pid=100)
        marked = _mark_zombies_offline(store, [8422], set())
        assert marked == []
        assert store.get_instance("ue1").status == "online"

    @patch("unrealhub.tools.discovery_tools.is_process_alive", return_value=False)
    def test_no_pid_zombie(self, mock_alive, tmp_home):
        """No PID + port unresponsive = zombie."""
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        store.update_status("ue1", "online")
        marked = _mark_zombies_offline(store, [8422], set())
        assert marked == ["ue1"]

    def test_offline_instance_skipped(self, tmp_home):
        """Already offline instances should not be touched."""
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        marked = _mark_zombies_offline(store, [8422], set())
        assert marked == []

    @patch("unrealhub.tools.discovery_tools.is_process_alive", return_value=False)
    def test_port_not_in_scan_range_skipped(self, mock_alive, tmp_home):
        """Instance on a port outside scan range should not be touched."""
        store = StateStore()
        store.register_instance(url="http://localhost:9999/mcp", port=9999, pid=100)
        marked = _mark_zombies_offline(store, [8422, 8423], set())
        assert marked == []
        assert store.get_instance("ue1").status == "online"

    @patch("unrealhub.tools.discovery_tools.is_process_alive", return_value=False)
    def test_multiple_zombies(self, mock_alive, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422, pid=100)
        store.register_instance(url="http://localhost:8423/mcp", port=8423, pid=200)
        marked = _mark_zombies_offline(store, [8422, 8423], set())
        assert len(marked) == 2
        assert store.get_instance("ue1").status == "offline"
        assert store.get_instance("ue2").status == "offline"

    @patch("unrealhub.tools.discovery_tools.is_process_alive", return_value=False)
    def test_partial_response(self, mock_alive, tmp_home):
        """One port responds, one doesn't -> only the non-responding one is zombie."""
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422, pid=100)
        store.register_instance(url="http://localhost:8423/mcp", port=8423, pid=200)
        marked = _mark_zombies_offline(store, [8422, 8423], {8422})
        assert marked == ["ue2"]
        assert store.get_instance("ue1").status == "online"
        assert store.get_instance("ue2").status == "offline"


class TestZombieAndDedupIntegration:
    """End-to-end: zombie marking + dedup work together."""

    @patch("unrealhub.tools.discovery_tools.is_process_alive", return_value=False)
    def test_zombie_then_dedup(self, mock_alive, tmp_home):
        """Zombies get marked offline, then dead duplicates per project are consolidated."""
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422,
                                project_path="G:/Proj/A.uproject", pid=100)
        store.register_instance(url="http://localhost:8423/mcp", port=8423,
                                project_path="G:/Proj/A.uproject", pid=200)
        store.register_instance(url="http://localhost:8424/mcp", port=8424,
                                project_path="G:/Proj/A.uproject")
        store.update_status("ue3", "crashed")

        zombie_ids = _mark_zombies_offline(store, [8422, 8423, 8424], set())
        assert set(zombie_ids) == {"ue1", "ue2"}

        dedup_ids = store.dedup_dead_by_project()
        assert len(dedup_ids) == 2

        remaining = store.list_instances()
        assert len(remaining) == 1
