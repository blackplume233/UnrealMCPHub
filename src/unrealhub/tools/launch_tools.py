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

_job_breakaway_ready = False


def _setup_win32_job_breakaway() -> None:
    """Create a nested Job Object that permits child process breakaway.

    When the Hub runs as a PyInstaller .exe, the host (e.g. Cursor) may
    wrap it in a Job Object that does NOT set BREAKAWAY_OK.  That causes
    CREATE_BREAKAWAY_FROM_JOB to be silently ignored and every child
    process (including UE) stays in the same Job — getting killed when
    the host tears down the job.

    Fix: create our own nested Job Object (Windows 8+ supports nesting)
    with BREAKAWAY_OK, then assign the current process to it.  After
    this, children created with CREATE_BREAKAWAY_FROM_JOB will actually
    leave our job and become fully independent.

    Called once at module registration time.  Fails silently.
    """
    global _job_breakaway_ready
    if sys.platform != "win32" or _job_breakaway_ready:
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
        JobObjectExtendedLimitInformation = 9

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_BREAKAWAY_OK

        if not kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return

        kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
        # Intentionally leak the handle — job must outlive the process.
        _job_breakaway_ready = True
        logger.debug("Nested Job Object with BREAKAWAY_OK created")
    except Exception:
        logger.debug("Failed to create nested Job Object", exc_info=True)


_GRACEFUL_QUIT_SCRIPT = """\
import unreal
try:
    unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
except Exception:
    pass
unreal.SystemLibrary.quit_editor()
"""

_GRACEFUL_QUIT_TIMEOUT = 15
_FORCE_KILL_TIMEOUT = 5


def register_launch_tools(
    mcp: FastMCP, get_config, get_state, get_ue_client_factory
) -> None:

    _setup_win32_job_breakaway()

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
        """Build kwargs for subprocess.Popen that launch UE as a fully
        independent process — not a child of the Hub.

        Uses subprocess.Popen (not asyncio) so no Transport holds a
        handle to the editor process.  On Windows the creation flags
        detach from console, process group, and Job Object.  On POSIX
        start_new_session calls setsid().
        """
        kwargs: dict = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_make_clean_env(),
        )
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_BREAKAWAY_FROM_JOB
            )
        else:
            kwargs["start_new_session"] = True
        return kwargs

    async def _start_editor(config, state, paths, project, headless, extra_args,
                            exec_cmds, wait_for_mcp, timeout,
                            build_config="Development") -> str:
        """Launch editor subprocess, optionally wait for MCP."""
        if build_config != "Development":
            editor_exe = UEPathResolver.editor_exe_for_config(
                paths.engine_root, build_config,
            )
            if not os.path.isfile(editor_exe):
                return (
                    f"{build_config} editor not found: {editor_exe}\n"
                    "Build the editor in this configuration first."
                )
        else:
            editor_exe = paths.editor_exe

        cmd = [editor_exe, paths.uproject_path]
        mode_label = build_config if build_config != "Development" else "normal"

        if headless:
            cmd.extend(["-nullrhi", "-nosplash", "-unattended"])
            mode_label = "headless (-nullrhi)"

        if exec_cmds:
            cmd.append(f'-ExecCmds="{exec_cmds}"')
            mode_label += f" +ExecCmds"

        if extra_args:
            cmd.extend(extra_args.split())

        try:
            proc = subprocess.Popen(cmd, **_subprocess_kwargs())
            editor_pid = proc.pid
            handle = getattr(proc, "_handle", None)
            if sys.platform == "win32" and handle is not None:
                handle.Close()
                proc.returncode = 0
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

        from unrealhub.tools.discovery_tools import probe_unreal_mcp_with_fallback

        default_mcp_url = f"http://localhost:{project.mcp_port}/mcp"
        start = time.monotonic()
        poll_interval = 2.0

        while (time.monotonic() - start) < timeout:
            probe = await probe_unreal_mcp_with_fallback(default_mcp_url, timeout=2.0)
            if probe:
                mcp_url, _ = probe
                elapsed = round(time.monotonic() - start, 1)
                instance = state.upsert(
                    port=project.mcp_port,
                    project_path=project.uproject_path,
                    url=mcp_url,
                    engine_root=project.engine_root,
                    pid=editor_pid,
                    status="online",
                )
                return (
                    f"Editor launched ({mode_label}) and MCP ready in {elapsed}s!\n"
                    f"PID: {editor_pid}\n"
                    f"MCP: {mcp_url}\n"
                    f"Instance: {instance.key}"
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
        build_config: str = "Development",
    ) -> str:
        """Manage UE Editor lifecycle for the active project.

        action: 'start' (launch), 'restart' (kill then launch), or 'stop' (kill).
        headless: If True, launches with -nullrhi -nosplash -unattended (no rendering).
        wait_for_mcp: If True, polls until RemoteMCP responds (start/restart only).
        timeout: Max seconds to wait for MCP readiness.
        exec_cmds: UE console commands to execute on startup, comma-separated
                   (e.g. 'stat fps, stat unit'). Maps to UE -ExecCmds flag.
        extra_args: Additional UE command-line arguments (e.g. '-log -verbose').
        build_config: Editor build configuration — 'Development' (default),
                      'DebugGame', or 'Debug'. Source builds only; launcher
                      installs only have Development.

        Requires project configured via setup_project.
        """
        if build_config not in UEPathResolver.VALID_BUILD_CONFIGS:
            return (
                f"Invalid build_config '{build_config}'. "
                f"Must be one of: {', '.join(UEPathResolver.VALID_BUILD_CONFIGS)}"
            )
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

        async def _fire_graceful_quit(inst) -> None:
            """Send save-all + quit via a proper MCP session.

            The call will almost certainly fail (connection reset) because
            the editor tears down its MCP server during shutdown.  That's
            expected — we only need the request to *reach* the game thread.
            """
            client = UEMCPClient(inst.url, timeout_connect=5.0, timeout_read=10.0)
            try:
                await client.call_tool(
                    "run_python_script", {"script": _GRACEFUL_QUIT_SCRIPT},
                )
            except Exception:
                pass

        async def _wait_for_pids_exit(pids: set[int], timeout: float) -> set[int]:
            """Poll until all *pids* are gone or *timeout* elapses.
            Returns the set of PIDs that are still alive."""
            deadline = time.monotonic() + timeout
            remaining = set(pids)
            while remaining and time.monotonic() < deadline:
                remaining = {p for p in remaining if is_process_alive(p)}
                if remaining:
                    await asyncio.sleep(1.0)
            return remaining

        def _force_kill_pid(pid: int) -> str:
            try:
                p = psutil.Process(pid)
                p.terminate()
                gone, alive = psutil.wait_procs([p], timeout=_FORCE_KILL_TIMEOUT)
                if alive:
                    alive[0].kill()
                return f"Force-killed PID {pid}"
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                return f"PID {pid} already gone"

        async def _kill_all_project_editors() -> str:
            """Gracefully shut down all editors for this project via MCP,
            falling back to OS-level kill for any that don't exit in time."""
            msgs: list[str] = []

            # Phase 1: fire quit command to all online tracked instances,
            # and collect PIDs to watch (regardless of MCP call result).
            project_instances = [
                inst for inst in state.list_instances()
                if inst.status == "online"
                and (inst.project_path or "").replace("\\", "/").lower() == project_norm
            ]
            watch_pids: set[int] = set()
            bg_tasks: list[asyncio.Task] = []
            if project_instances:
                for inst in project_instances:
                    bg_tasks.append(
                        asyncio.create_task(_fire_graceful_quit(inst))
                    )
                    if inst.pid and is_process_alive(inst.pid):
                        watch_pids.add(inst.pid)
                    msgs.append(
                        f"Quit signal sent to '{inst.key}' (PID {inst.pid})"
                    )

            # Also include any untracked OS-level UE processes for this project
            for proc_info in _find_project_procs():
                watch_pids.add(proc_info["pid"])

            if not watch_pids:
                for t in bg_tasks:
                    t.cancel()
                return "No running editors found."

            # Phase 2: poll PIDs while quit commands run in background.
            # The MCP calls may hang until the editor shuts down (or timeout),
            # so we don't await them — PID disappearance is the real signal.
            still_alive = await _wait_for_pids_exit(
                watch_pids, _GRACEFUL_QUIT_TIMEOUT,
            )
            for pid in watch_pids - still_alive:
                msgs.append(f"PID {pid} exited gracefully")

            # Phase 3: force-kill anything that didn't exit in time
            if still_alive:
                msgs.append(
                    f"PIDs {still_alive} did not exit in "
                    f"{_GRACEFUL_QUIT_TIMEOUT}s, force-killing"
                )
                for pid in still_alive:
                    msgs.append(_force_kill_pid(pid))

            # Clean up background MCP tasks (they may still be waiting)
            for t in bg_tasks:
                t.cancel()

            for inst in state.list_instances():
                if inst.status == "online":
                    inst_proj = (inst.project_path or "").replace("\\", "/").lower()
                    if inst_proj == project_norm:
                        state.update_status(inst.key, "offline")
            state.save()
            return "; ".join(msgs)

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
                build_config,
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
            build_config,
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
