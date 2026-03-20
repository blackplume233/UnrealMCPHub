from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp.server.fastmcp import FastMCP
from unrealhub.state import StateStore
from unrealhub.tools.launch_tools import register_launch_tools


def _setup(tmp_home):
    mcp = FastMCP("test")
    config = MagicMock()
    project = MagicMock()
    project.uproject_path = "G:/Proj/A.uproject"
    project.engine_root = "G:/UE"
    project.mcp_port = 8422
    config.get_active_project.return_value = project
    store = StateStore()

    with patch("unrealhub.tools.launch_tools._setup_win32_job_breakaway"):
        register_launch_tools(mcp, lambda: config, lambda: store, MagicMock)

    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}
    return store, project, tools


class TestLaunchEditorLoopbackFallback:
    @pytest.mark.asyncio
    async def test_wait_for_mcp_uses_verified_loopback_url(self, tmp_home):
        store, project, tools = _setup(tmp_home)
        fake_paths = MagicMock()
        fake_paths.engine_root = project.engine_root
        fake_paths.uproject_path = project.uproject_path
        fake_paths.editor_exe = "G:/UE/Engine/Binaries/Win64/UnrealEditor.exe"

        fake_proc = MagicMock()
        fake_proc.pid = 4321
        fake_proc._handle = None

        with patch(
            "unrealhub.tools.launch_tools.UEPathResolver.resolve_from_uproject",
            return_value=fake_paths,
        ), patch(
            "unrealhub.tools.launch_tools.UEPathResolver.get_editor_build_target",
            return_value="AEditor",
        ), patch(
            "unrealhub.tools.launch_tools._compile",
            new_callable=AsyncMock,
            return_value="Build SUCCEEDED\nExit code: 0",
        ), patch(
            "unrealhub.tools.launch_tools.find_unreal_editor_processes",
            return_value=[],
        ), patch(
            "unrealhub.tools.launch_tools.subprocess.Popen",
            return_value=fake_proc,
        ), patch(
            "unrealhub.tools.launch_tools.probe_unreal_mcp_with_fallback",
            new_callable=AsyncMock,
            return_value=("http://127.0.0.1:8422/mcp", {"server_name": "Remote Unreal MCP"}),
        ):
            result = await tools["launch_editor"](wait_for_mcp=True, timeout=5)

        assert "127.0.0.1:8422/mcp" in result
        inst = store.get_instance("A:8422")
        assert inst is not None
        assert inst.url == "http://127.0.0.1:8422/mcp"

    @pytest.mark.asyncio
    async def test_launch_editor_stops_when_compile_fails(self, tmp_home):
        _, project, tools = _setup(tmp_home)
        fake_paths = MagicMock()
        fake_paths.engine_root = project.engine_root
        fake_paths.uproject_path = project.uproject_path
        fake_paths.editor_exe = "G:/UE/Engine/Binaries/Win64/UnrealEditor.exe"

        with patch(
            "unrealhub.tools.launch_tools.UEPathResolver.resolve_from_uproject",
            return_value=fake_paths,
        ), patch(
            "unrealhub.tools.launch_tools.UEPathResolver.get_editor_build_target",
            return_value="AEditor",
        ), patch(
            "unrealhub.tools.launch_tools.find_unreal_editor_processes",
            return_value=[],
        ), patch(
            "unrealhub.tools.launch_tools._compile",
            new_callable=AsyncMock,
            return_value="Build FAILED\nExit code: 6",
        ), patch(
            "unrealhub.tools.launch_tools.subprocess.Popen",
        ) as mock_popen:
            result = await tools["launch_editor"](wait_for_mcp=False)

        assert "Compile before launch FAILED" in result
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_launch_editor_uses_engine_editor_target_for_blueprint_only_project(self, tmp_home):
        _, project, tools = _setup(tmp_home)
        fake_paths = MagicMock()
        fake_paths.engine_root = project.engine_root
        fake_paths.uproject_path = project.uproject_path
        fake_paths.editor_exe = "G:/UE/Engine/Binaries/Win64/UnrealEditor.exe"

        fake_proc = MagicMock()
        fake_proc.pid = 9876
        fake_proc._handle = None

        with patch(
            "unrealhub.tools.launch_tools.UEPathResolver.resolve_from_uproject",
            return_value=fake_paths,
        ), patch(
            "unrealhub.tools.launch_tools.UEPathResolver.get_editor_build_target",
            return_value="UnrealEditor",
        ), patch(
            "unrealhub.tools.launch_tools.find_unreal_editor_processes",
            return_value=[],
        ), patch(
            "unrealhub.tools.launch_tools._compile",
            new_callable=AsyncMock,
            return_value="Build SUCCEEDED\nExit code: 0",
        ) as mock_compile, patch(
            "unrealhub.tools.launch_tools.subprocess.Popen",
            return_value=fake_proc,
        ) as mock_popen:
            result = await tools["launch_editor"](wait_for_mcp=False)

        assert "Compile before launch SUCCEEDED" in result
        assert mock_compile.await_args.kwargs["build_target_override"] == "UnrealEditor"
        mock_popen.assert_called_once()
