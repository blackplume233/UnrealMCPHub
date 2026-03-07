import asyncio
import logging
import os
import subprocess
import sys
import time

import psutil

from mcp.server.fastmcp import FastMCP

from unrealhub.ue_client import UEMCPClient
from unrealhub.utils.process import find_unreal_editor_processes, is_process_alive
from unrealhub.utils.ue_paths import UEPathResolver

logger = logging.getLogger(__name__)


def register_launch_tools(
    mcp: FastMCP, get_config, get_state, get_ue_client_factory
) -> None:
    async def _kill_editor(state, active) -> str:
        """Kill the active editor process and update state."""
        if not active:
            return "No active instance."
        if not active.pid or not is_process_alive(active.pid):
            state.update_status(active.auto_id, "offline")
            state.save()
            return f"Instance '{active.auto_id}' process not running (already dead)."

        try:
            proc = psutil.Process(active.pid)
            proc.terminate()
            gone, alive = psutil.wait_procs([proc], timeout=5)
            if alive:
                alive[0].kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

        state.update_status(active.auto_id, "offline")
        state.save()
        return f"Editor stopped (PID: {active.pid}, instance: {active.auto_id})."

    def _make_clean_env() -> dict[str, str]:
        """Build an env dict that strips Hub's Python artifacts.

        Even though UE 5.7 defaults bIsolateInterpreterEnvironment=true,
        older engines honour PYTHON* vars, and stray env vars can still
        confuse child process behaviour on Windows.
        """
        env = os.environ.copy()
        for key in list(env):
            upper = key.upper()
            if upper.startswith("PYTHON") or upper in (
                "VIRTUAL_ENV", "CONDA_DEFAULT_ENV", "CONDA_PREFIX",
                "PIP_PREFIX", "PIP_TARGET",
            ):
                del env[key]
        return env

    def _subprocess_kwargs() -> dict:
        """Platform-specific kwargs for create_subprocess_exec.

        On Windows, detach UE from the Hub's process tree so it does not
        inherit Job Objects, console handles, or scheduling constraints
        that Cursor / the MCP host may impose.
        """
        kwargs: dict = dict(
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=_make_clean_env(),
        )
        if sys.platform == "win32":
            flags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_BREAKAWAY_FROM_JOB
            )
            kwargs["creationflags"] = flags
        return kwargs

    async def _start_editor(config, state, paths, project, headless, extra_args,
                            exec_cmds, wait_for_mcp, timeout) -> str:
        """Launch editor subprocess, optionally wait for MCP."""
        cmd = [paths.editor_exe, paths.uproject_path]
        mode_label = "normal"

        if headless:
            cmd.extend(["-nullrhi", "-nosplash", "-unattended"])
            mode_label = "headless (-nullrhi)"

        if exec_cmds:
            cmd.append(f'-ExecCmds="{exec_cmds}"')
            mode_label += f" +ExecCmds"

        if extra_args:
            cmd.extend(extra_args.split())

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, **_subprocess_kwargs(),
            )
            editor_pid = process.pid or 0
        except FileNotFoundError:
            return f"Editor not found at: {paths.editor_exe}"
        except Exception as e:
            logger.exception("launch_editor failed")
            return f"Failed to launch editor: {e}"

        if not wait_for_mcp:
            return (
                f"Editor launched in {mode_label} mode (PID: {editor_pid}). "
                "Not waiting for MCP."
            )

        mcp_url = f"http://localhost:{project.mcp_port}/mcp"
        start = time.monotonic()
        poll_interval = 2.0

        while (time.monotonic() - start) < timeout:
            if await UEMCPClient.probe_endpoint(mcp_url, timeout=2.0):
                elapsed = round(time.monotonic() - start, 1)
                instance = state.register_instance(
                    url=mcp_url,
                    port=project.mcp_port,
                    project_path=project.uproject_path,
                    engine_root=project.engine_root,
                    pid=editor_pid,
                )
                state.update_status(instance.auto_id, "online", pid=editor_pid)
                state.save()
                return (
                    f"Editor launched ({mode_label}) and MCP ready in {elapsed}s!\n"
                    f"PID: {editor_pid}\n"
                    f"MCP: {mcp_url}\n"
                    f"Instance: {instance.auto_id}"
                )
            await asyncio.sleep(poll_interval)
            poll_interval = min(poll_interval + 1.0, 10.0)

        return (
            f"Editor launched ({mode_label}, PID: {editor_pid}) but MCP did not "
            f"become ready within {timeout}s.\n"
            "Check if RemoteMCP plugin is enabled and MCP.Start has been run."
        )

    @mcp.tool()
    async def launch_editor(
        action: str = "start",
        headless: bool = False,
        wait_for_mcp: bool = True,
        timeout: int = 120,
        exec_cmds: str = "",
        extra_args: str = "",
    ) -> str:
        """Manage UE Editor lifecycle for the active project.

        action: 'start' (launch), 'restart' (kill then launch), or 'stop' (kill).
        headless: If True, launches with -nullrhi -nosplash -unattended (no rendering).
        wait_for_mcp: If True, polls until RemoteMCP responds (start/restart only).
        timeout: Max seconds to wait for MCP readiness.
        exec_cmds: UE console commands to execute on startup, comma-separated
                   (e.g. 'stat fps, stat unit'). Maps to UE -ExecCmds flag.
        extra_args: Additional UE command-line arguments (e.g. '-log -verbose').

        Requires project configured via setup_project.
        """
        config = get_config()
        project = config.get_active_project()
        if not project:
            return "No project configured. Call setup_project() first."

        state = get_state()

        try:
            paths = UEPathResolver.resolve_from_uproject(
                project.uproject_path, project.engine_root
            )
        except ValueError as e:
            return f"Path resolution failed: {e}"

        project_norm = project.uproject_path.replace("\\", "/").lower()

        def _find_project_procs() -> list[dict]:
            """Return running UE processes that belong to the active project."""
            result = []
            for proc in find_unreal_editor_processes():
                proc_path = (proc.get("project_path") or "").replace("\\", "/").lower()
                if proc_path == project_norm:
                    result.append(proc)
            return result

        async def _kill_all_project_editors() -> str:
            """Kill every UE process for this project + mark state offline."""
            msgs = []
            # Kill via OS process scan (catches editors not tracked in state)
            for proc_info in _find_project_procs():
                pid = proc_info["pid"]
                try:
                    p = psutil.Process(pid)
                    p.terminate()
                    gone, alive = psutil.wait_procs([p], timeout=5)
                    if alive:
                        alive[0].kill()
                    msgs.append(f"Killed PID {pid}")
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    msgs.append(f"PID {pid} already gone")
            # Mark all tracked instances for this project as offline
            for inst in state.list_instances():
                if inst.status in ("online", "starting"):
                    inst_proj = (getattr(inst, "project_path", "") or "").replace("\\", "/").lower()
                    if inst_proj == project_norm:
                        state.update_status(inst.auto_id, "offline")
            state.save()
            return "; ".join(msgs) if msgs else "No running editors found."

        if action == "stop":
            result = await _kill_all_project_editors()
            return f"Stop: {result}"

        if action == "restart":
            kill_msg = await _kill_all_project_editors()
            if _find_project_procs():
                await asyncio.sleep(3)

            start_msg = await _start_editor(
                config, state, paths, project,
                headless, extra_args, exec_cmds, wait_for_mcp, timeout,
            )
            return f"Stop: {kill_msg}\n{start_msg}"

        # action == "start" (default)
        running_for_project = _find_project_procs()
        if running_for_project:
            pids = ", ".join(str(p["pid"]) for p in running_for_project)
            return (
                f"Editor already running for this project (PIDs: {pids}). "
                f"MCP port: {project.mcp_port}\n"
                f"Use launch_editor(action='restart') to force restart."
            )

        return await _start_editor(
            config, state, paths, project,
            headless, extra_args, exec_cmds, wait_for_mcp, timeout,
        )

    @mcp.tool()
    async def get_editor_status() -> str:
        """Check if UE Editor processes are currently running."""
        procs = find_unreal_editor_processes()
        if not procs:
            return "No UE Editor processes found."

        lines = [f"Found {len(procs)} UE Editor process(es):"]
        for p in procs:
            project_path = p.get("project_path", "unknown")
            lines.append(f"  PID: {p['pid']}, Project: {project_path}")
        return "\n".join(lines)
