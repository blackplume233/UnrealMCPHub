"""Test discovery_tools (discover_instances, manage_instance)."""
from unittest.mock import MagicMock, patch

import pytest

from mcp.server.fastmcp import FastMCP
from unrealhub.state import StateStore
from unrealhub.tools.discovery_tools import register_discovery_tools


def _setup(tmp_home):
    mcp = FastMCP("test")
    config = MagicMock()
    config.get_scan_ports.return_value = [8422, 8423]
    config.get_extended_ports.return_value = list(range(8000, 8010))
    store = StateStore()
    register_discovery_tools(mcp, lambda: config, lambda: store)
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}
    return store, config, tools


class TestToolRegistration:
    def test_only_two_tools(self, tmp_home):
        _, _, tools = _setup(tmp_home)
        assert set(tools.keys()) == {"discover_instances", "manage_instance"}

    def test_removed_tools(self, tmp_home):
        _, _, tools = _setup(tmp_home)
        for removed in ("list_instances", "use_editor"):
            assert removed not in tools


class TestDiscoverInstancesNoRescan:
    @pytest.mark.asyncio
    async def test_empty_state(self, tmp_home):
        _, _, tools = _setup(tmp_home)
        result = await tools["discover_instances"]()
        assert "no instances" in result.lower()

    @pytest.mark.asyncio
    async def test_lists_known(self, tmp_home):
        store, _, tools = _setup(tmp_home)
        store.upsert(port=8422, project_path="G:/Proj/A.uproject", pid=100)
        result = await tools["discover_instances"]()
        assert "A:8422" in result


class TestManageInstanceRegister:
    @pytest.mark.asyncio
    async def test_register(self, tmp_home):
        store, _, tools = _setup(tmp_home)
        result = await tools["manage_instance"]("register", url="http://localhost:9999/mcp")
        assert "registered" in result.lower()
        assert len(store.list_instances()) == 1

    @pytest.mark.asyncio
    async def test_register_no_url(self, tmp_home):
        _, _, tools = _setup(tmp_home)
        result = await tools["manage_instance"]("register")
        assert "url is required" in result.lower()


class TestManageInstanceUnregister:
    @pytest.mark.asyncio
    async def test_unregister(self, tmp_home):
        store, _, tools = _setup(tmp_home)
        store.upsert(port=8422, project_path="G:/Proj/A.uproject")
        result = await tools["manage_instance"]("unregister", instance="A:8422")
        assert "removed" in result.lower()

    @pytest.mark.asyncio
    async def test_unregister_not_found(self, tmp_home):
        _, _, tools = _setup(tmp_home)
        result = await tools["manage_instance"]("unregister", instance="ghost")
        assert "not found" in result.lower()


class TestManageInstanceUse:
    @pytest.mark.asyncio
    async def test_use(self, tmp_home):
        store, _, tools = _setup(tmp_home)
        store.upsert(port=8422, project_path="G:/Proj/A.uproject")
        store.upsert(port=8423, project_path="G:/Proj/B.uproject")
        result = await tools["manage_instance"]("use", instance="B:8423")
        assert "switched" in result.lower()
        assert store.get_active_instance().key == "B:8423"

    @pytest.mark.asyncio
    async def test_use_by_port(self, tmp_home):
        store, _, tools = _setup(tmp_home)
        store.upsert(port=8422, project_path="G:/Proj/A.uproject")
        store.upsert(port=8423, project_path="G:/Proj/B.uproject")
        result = await tools["manage_instance"]("use", instance="8423")
        assert "switched" in result.lower()

    @pytest.mark.asyncio
    async def test_use_not_found(self, tmp_home):
        _, _, tools = _setup(tmp_home)
        result = await tools["manage_instance"]("use", instance="ghost")
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_use_no_instance(self, tmp_home):
        _, _, tools = _setup(tmp_home)
        result = await tools["manage_instance"]("use")
        assert "instance is required" in result.lower()


class TestManageInstanceUnknownAction:
    @pytest.mark.asyncio
    async def test_unknown(self, tmp_home):
        _, _, tools = _setup(tmp_home)
        result = await tools["manage_instance"]("fly")
        assert "unknown action" in result.lower()


class TestRegisterOrphanProcesses:
    def test_registers_unknown_ue_process(self, tmp_home):
        from unittest.mock import patch
        from unrealhub.tools.discovery_tools import register_orphan_processes

        store, _, _ = _setup(tmp_home)
        fake_procs = [
            {"pid": 5555, "name": "UnrealEditor.exe", "cmdline": [], "project_path": "G:/Proj/B.uproject"},
        ]
        with patch("unrealhub.tools.discovery_tools.find_unreal_editor_processes", return_value=fake_procs):
            report = register_orphan_processes(store)
        assert len(report) == 1
        assert "NO MCP" in report[0]
        inst = store.get_instance("B:0")
        assert inst is not None
        assert inst.pid == 5555
        assert inst.status == "offline"

    def test_skips_already_registered_by_pid(self, tmp_home):
        from unittest.mock import patch
        from unrealhub.tools.discovery_tools import register_orphan_processes

        store, _, _ = _setup(tmp_home)
        store.upsert(port=8422, project_path="G:/Proj/A.uproject", pid=1234)
        fake_procs = [
            {"pid": 1234, "name": "UnrealEditor.exe", "cmdline": [], "project_path": "G:/Proj/A.uproject"},
        ]
        with patch("unrealhub.tools.discovery_tools.find_unreal_editor_processes", return_value=fake_procs):
            report = register_orphan_processes(store)
        assert report == []
        assert len(store.list_instances()) == 1

    def test_reports_extra_process_for_same_project(self, tmp_home):
        from unittest.mock import patch
        from unrealhub.tools.discovery_tools import register_orphan_processes

        store, _, _ = _setup(tmp_home)
        store.upsert(port=8422, project_path="G:/Proj/A.uproject", pid=9999)
        fake_procs = [
            {"pid": 5555, "name": "UnrealEditor.exe", "cmdline": [], "project_path": "G:/Proj/A.uproject"},
        ]
        with patch("unrealhub.tools.discovery_tools.find_unreal_editor_processes", return_value=fake_procs):
            report = register_orphan_processes(store)
        assert len(report) == 1
        assert "5555" in report[0]
        assert "extra process" in report[0]

    def test_no_ue_processes(self, tmp_home):
        from unittest.mock import patch
        from unrealhub.tools.discovery_tools import register_orphan_processes

        store, _, _ = _setup(tmp_home)
        with patch("unrealhub.tools.discovery_tools.find_unreal_editor_processes", return_value=[]):
            report = register_orphan_processes(store)
        assert report == []

    def test_process_without_project_path(self, tmp_home):
        from unittest.mock import patch
        from unrealhub.tools.discovery_tools import register_orphan_processes

        store, _, _ = _setup(tmp_home)
        fake_procs = [
            {"pid": 7777, "name": "UnrealEditor.exe", "cmdline": [], "project_path": None},
        ]
        with patch("unrealhub.tools.discovery_tools.find_unreal_editor_processes", return_value=fake_procs):
            report = register_orphan_processes(store)
        assert len(report) == 1
        inst = store.get_instance("unknown:0")
        assert inst is not None
        assert inst.pid == 7777


class TestLoopbackFallbackAndVerification:
    @pytest.mark.asyncio
    async def test_scan_ports_prefers_numeric_loopback(self, tmp_home):
        from unrealhub.tools.discovery_tools import _scan_ports

        async def fake_probe(url, timeout=3.0):
            if url == "http://127.0.0.1:8422/mcp":
                return {"server_name": "Remote Unreal MCP"}
            return None

        with patch("unrealhub.tools.discovery_tools.probe_unreal_mcp", side_effect=fake_probe):
            found = await _scan_ports([8422])

        assert len(found) == 1
        assert found[0]["url"] == "http://127.0.0.1:8422/mcp"

    @pytest.mark.asyncio
    async def test_reprobe_offline_uses_unreal_verification(self, tmp_home):
        from unrealhub.tools.discovery_tools import reprobe_offline_instances

        store, _, _ = _setup(tmp_home)
        store.upsert(
            port=8422,
            project_path="G:/Proj/A.uproject",
            url="http://localhost:8422/mcp",
            status="offline",
        )

        async def fake_probe(url, timeout=3.0):
            return {"server_name": "Remote Unreal MCP"} if "127.0.0.1" in url else None

        with patch("unrealhub.tools.discovery_tools.probe_unreal_mcp", side_effect=fake_probe):
            recovered = await reprobe_offline_instances(store)

        assert recovered == ["A:8422"]
        inst = store.get_instance("A:8422")
        assert inst is not None
        assert inst.status == "online"
        assert inst.url == "http://127.0.0.1:8422/mcp"

    @pytest.mark.asyncio
    async def test_scan_ports_for_new_requires_unreal_server(self, tmp_home):
        from unrealhub.tools.discovery_tools import scan_ports_for_new

        store, _, _ = _setup(tmp_home)

        async def fake_probe(url, timeout=3.0):
            if "8422" in url:
                return {"server_name": "Remote Unreal MCP"}
            return None

        with patch("unrealhub.tools.discovery_tools.probe_unreal_mcp", side_effect=fake_probe):
            new_keys = await scan_ports_for_new(store, [8422, 8423])

        assert new_keys == ["unknown:8422"]
        inst = store.get_instance("unknown:8422")
        assert inst is not None
        assert inst.status == "online"
        assert inst.url == "http://127.0.0.1:8422/mcp"
