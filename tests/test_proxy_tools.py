"""Test the helper functions in proxy_tools (format and offline logic)."""
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from unrealhub.state import StateStore


def _make_proxy_module():
    """Import register_proxy_tools and construct its internal helpers for testing."""
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test")

    store = MagicMock(spec=StateStore)
    store.list_instances_summary.return_value = "  ue1 (ue1): online *"
    store.get_active_instance.return_value = None

    get_client = MagicMock(return_value=None)

    from unrealhub.tools.proxy_tools import register_proxy_tools
    register_proxy_tools(mcp, lambda: store, get_client)

    return mcp, store, get_client


def _make_online_proxy():
    """Create proxy tools with a mocked online UE instance."""
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test")

    active = MagicMock()
    active.auto_id = "ue1"
    active.status = "online"
    active.alias = None
    active.url = "http://localhost:8422/mcp"
    active.pid = 1234
    active.project_path = "/test"
    active.crash_count = 0
    active.last_seen = "now"

    store = MagicMock(spec=StateStore)
    store.get_active_instance.return_value = active

    mock_client = AsyncMock()
    get_client = MagicMock(return_value=mock_client)

    from unrealhub.tools.proxy_tools import register_proxy_tools
    register_proxy_tools(mcp, lambda: store, get_client)

    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}
    return tools, mock_client, store


class TestRegisteredTools:
    def test_expected_tools_registered(self, tmp_home):
        mcp, store, get_client = _make_proxy_module()
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert tool_names == {"ue_status", "ue_list_tools", "ue_call", "ue_run_python"}

    def test_removed_tools_not_present(self, tmp_home):
        mcp, store, get_client = _make_proxy_module()
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        for removed in ["ue_test_state", "ue_get_project_dir", "ue_get_dispatch", "ue_call_dispatch"]:
            assert removed not in tool_names


class TestUeCall:
    @pytest.mark.asyncio
    async def test_none_arguments(self, tmp_home):
        """arguments=None should be treated as empty dict."""
        tools, mock_client, store = _make_online_proxy()
        mock_client.call_tool = AsyncMock(return_value={
            "success": True,
            "content": [{"type": "text", "text": "ok"}],
            "error": None,
        })
        result = await tools["ue_call"]("test_tool", None)
        assert "ok" in result
        mock_client.call_tool.assert_called_once_with("test_tool", {})

    @pytest.mark.asyncio
    async def test_direct_call(self, tmp_home):
        """ue_call without domain calls tool directly."""
        tools, mock_client, store = _make_online_proxy()
        mock_client.call_tool = AsyncMock(return_value={
            "success": True,
            "content": [{"type": "text", "text": "direct result"}],
            "error": None,
        })
        result = await tools["ue_call"]("some_tool", {"key": "val"})
        assert "direct result" in result
        mock_client.call_tool.assert_called_once_with("some_tool", {"key": "val"})

    @pytest.mark.asyncio
    async def test_dispatch_call(self, tmp_home):
        """ue_call with domain routes through call_dispatch_tool."""
        tools, mock_client, store = _make_online_proxy()
        mock_client.call_tool = AsyncMock(return_value={
            "success": True,
            "content": [{"type": "text", "text": "dispatch result"}],
            "error": None,
        })
        result = await tools["ue_call"]("get_actors", {"class": "Static"}, "level")
        assert "dispatch result" in result
        mock_client.call_tool.assert_called_once_with(
            "call_dispatch_tool",
            {
                "domain": "level",
                "tool_name": "get_actors",
                "arguments": '{"class": "Static"}',
            },
        )

    @pytest.mark.asyncio
    async def test_dispatch_records_domain_tool_name(self, tmp_home):
        """Tool call history should record domain/tool_name for dispatch calls."""
        tools, mock_client, store = _make_online_proxy()
        mock_client.call_tool = AsyncMock(return_value={
            "success": True,
            "content": [{"type": "text", "text": "ok"}],
            "error": None,
        })
        await tools["ue_call"]("spawn", None, "level")
        store.record_tool_call.assert_called_once()
        call_args = store.record_tool_call.call_args
        assert call_args[0][1] == "level/spawn"


class TestUeListTools:
    @pytest.mark.asyncio
    async def test_list_mcp_tools(self, tmp_home):
        """ue_list_tools() without domain lists MCP-level tools."""
        tools, mock_client, store = _make_online_proxy()
        mock_client.list_tools = AsyncMock(return_value=[
            {"name": "run_python_script", "description": "Run Python", "inputSchema": {"properties": {}}},
        ])
        result = await tools["ue_list_tools"]()
        assert "run_python_script" in result

    @pytest.mark.asyncio
    async def test_list_domain_tools(self, tmp_home):
        """ue_list_tools(domain='level') queries dispatch system."""
        tools, mock_client, store = _make_online_proxy()
        mock_client.call_tool = AsyncMock(return_value={
            "success": True,
            "content": [{"type": "text", "text": "level domain: get_actors, spawn_actor"}],
            "error": None,
        })
        result = await tools["ue_list_tools"]("level")
        assert "level domain" in result
        mock_client.call_tool.assert_called_once_with("get_dispatch", {"domain": "level"})


class TestProxyFormatting:
    @pytest.mark.asyncio
    async def test_format_success_text(self, tmp_home):
        tools, mock_client, store = _make_online_proxy()
        mock_client.call_tool = AsyncMock(return_value={
            "success": True,
            "content": [{"type": "text", "text": "hello world"}],
            "error": None,
        })
        result = await tools["ue_call"]("some_tool", {})
        assert "hello world" in result

    @pytest.mark.asyncio
    @patch("unrealhub.utils.process.is_process_alive", return_value=True)
    async def test_format_error(self, _mock_alive, tmp_home):
        tools, mock_client, store = _make_online_proxy()
        mock_client.call_tool = AsyncMock(return_value={
            "success": False,
            "content": [],
            "error": "Something broke",
        })
        result = await tools["ue_call"]("bad_tool", {})
        assert "Something broke" in result

    @pytest.mark.asyncio
    async def test_format_image(self, tmp_home):
        tools, mock_client, store = _make_online_proxy()
        mock_client.call_tool = AsyncMock(return_value={
            "success": True,
            "content": [{"type": "image", "mimeType": "image/png", "data": "abc123"}],
            "error": None,
        })
        result = await tools["ue_call"]("img_tool", {})
        assert "Image" in result
        assert "image/png" in result

    @pytest.mark.asyncio
    async def test_offline_message(self, tmp_home):
        mcp, store, get_client = _make_proxy_module()
        tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}
        result = await tools["ue_call"]("any", {})
        assert "no active" in result.lower() or "online" in result.lower()


class TestCrashGuard:
    @pytest.mark.asyncio
    @patch("unrealhub.utils.process.is_process_alive", return_value=False)
    async def test_ue_call_detects_crash(self, _mock_alive, tmp_home):
        """When PID dies during ue_call, crash message is returned immediately."""
        tools, mock_client, store = _make_online_proxy()

        async def _slow_call(*a, **kw):
            import asyncio
            await asyncio.sleep(10)
            return {"success": True, "content": [], "error": None}

        mock_client.call_tool = _slow_call
        result = await tools["ue_call"]("slow_tool", {})
        assert "CRASHED" in result
        assert "PID 1234" in result
        store.update_status.assert_called_once_with("ue1", "crashed")
        store.increment_crash_count.assert_called_once_with("ue1")

    @pytest.mark.asyncio
    @patch("unrealhub.utils.process.is_process_alive", return_value=False)
    async def test_ue_run_python_detects_crash(self, _mock_alive, tmp_home):
        """When PID dies during ue_run_python, crash message is returned."""
        tools, mock_client, store = _make_online_proxy()

        async def _slow_call(*a, **kw):
            import asyncio
            await asyncio.sleep(10)
            return {"success": True, "content": [], "error": None}

        mock_client.call_tool = _slow_call
        result = await tools["ue_run_python"]("print(1)")
        assert "CRASHED" in result

    @pytest.mark.asyncio
    @patch("unrealhub.utils.process.is_process_alive", return_value=False)
    async def test_ue_list_tools_detects_crash(self, _mock_alive, tmp_home):
        """When PID dies during ue_list_tools, crash message is returned."""
        tools, mock_client, store = _make_online_proxy()

        async def _slow_list(*a, **kw):
            import asyncio
            await asyncio.sleep(10)
            return []

        mock_client.list_tools = _slow_list
        result = await tools["ue_list_tools"]()
        assert "CRASHED" in result

    @pytest.mark.asyncio
    @patch("unrealhub.utils.process.is_process_alive", return_value=False)
    async def test_fallback_upgrades_error_to_crash(self, _mock_alive, tmp_home):
        """A fast connection error + dead PID is upgraded to crash notification."""
        tools, mock_client, store = _make_online_proxy()
        mock_client.call_tool = AsyncMock(return_value={
            "success": False,
            "content": [],
            "error": "Connection refused",
        })
        result = await tools["ue_call"]("any_tool", {})
        assert "CRASHED" in result

    @pytest.mark.asyncio
    @patch("unrealhub.utils.process.is_process_alive", return_value=True)
    async def test_no_false_crash_when_pid_alive(self, _mock_alive, tmp_home):
        """Successful calls with alive PID should not trigger crash guard."""
        tools, mock_client, store = _make_online_proxy()
        mock_client.call_tool = AsyncMock(return_value={
            "success": True,
            "content": [{"type": "text", "text": "all good"}],
            "error": None,
        })
        result = await tools["ue_call"]("ok_tool", {})
        assert "all good" in result
        assert "CRASHED" not in result
