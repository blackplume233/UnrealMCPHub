import asyncio
import json
import logging
import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from unrealhub.utils.process import find_unreal_editor_processes, is_process_alive

logger = logging.getLogger(__name__)


def register_proxy_tools(mcp: FastMCP, get_state, get_client) -> None:
    """
    get_state: callable returning StateStore
    get_client: callable(instance_id: str | None) -> UEMCPClient | None
        If instance_id is None, returns client for active instance.
    """

    def _offline_message() -> str:
        state = get_state()
        summary = state.list_instances_summary()
        active = state.get_active_instance()
        if active and active.status == "offline" and active.crash_count > 0:
            return (
                f"UE instance '{active.key}' is OFFLINE (crashed x{active.crash_count}).\n"
                f"Use get_log(source='crash') for details, or launch_editor(action='restart') to restart.\n"
                f"\n{summary}"
            )
        return (
            f"No active UE instance online.\n"
            f"Use launch_editor() to start the editor, or discover_instances() to find running ones.\n"
            f"\n{summary}"
        )

    def _format_tool_result(result: dict[str, Any]) -> str:
        if not result.get("success", False):
            return f"[UE Tool Error] {result.get('error', 'Unknown error')}"

        content = result.get("content")
        if not isinstance(content, list):
            return str(content) if content is not None else "(empty result)"

        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            ctype = item.get("type", "")
            if ctype == "text":
                parts.append(item.get("text", ""))
            elif ctype == "image":
                mime = item.get("mimeType") or item.get("mime_type", "unknown")
                data = item.get("data")
                if isinstance(data, str):
                    size = len(data)
                    parts.append(f"[Image: {mime}, {size} chars base64]")
                else:
                    parts.append(f"[Image: {mime}]")
            else:
                if "repr" in item:
                    parts.append(item["repr"])
                elif "text" in item:
                    parts.append(item["text"])
                else:
                    parts.append(str(item))

        return "\n".join(parts) if parts else "(empty result)"

    class _UECrashed(Exception):
        """Raised by *_with_crash_guard* when the UE process dies mid-call."""

    def _refresh_pid_if_stale(state, active) -> int | None:
        """Refresh a stale tracked PID from currently running editor processes.

        Hub state can retain an old PID after restart while the MCP endpoint is
        already serving from a new editor process. If we trust the stale PID,
        crash-guard logic will immediately produce a false crash.
        """
        if not active or not active.project_path:
            return active.pid if active else None
        if active.pid and is_process_alive(active.pid):
            return active.pid

        project_norm = active.project_path.replace("\\", "/").lower()
        for proc in find_unreal_editor_processes():
            proc_path = (proc.get("project_path") or "").replace("\\", "/").lower()
            if proc_path != project_norm:
                continue
            new_pid = proc["pid"]
            state.update_status(active.key, active.status, pid=new_pid)
            active.pid = new_pid
            logger.info(
                "Refreshed stale PID for %s: %s -> %s",
                active.key, active.pid, new_pid,
            )
            return new_pid
        return active.pid

    async def _with_crash_guard(coro, pid: int | None):  # noqa: ANN001
        """Race *coro* against a PID-alive monitor.

        Returns the coroutine's result on success.
        Raises ``_UECrashed`` if the UE process (identified by *pid*)
        dies while the call is still in flight.
        """
        if pid is None:
            return await coro

        from unrealhub.utils.process import is_process_alive

        async def _watch_pid():
            while is_process_alive(pid):
                await asyncio.sleep(0.5)

        call_task = asyncio.create_task(coro)
        pid_task = asyncio.create_task(_watch_pid())

        done, pending = await asyncio.wait(
            {call_task, pid_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=2.0)

        if call_task in done:
            return call_task.result()

        raise _UECrashed()

    def _handle_crash(state, active, client) -> None:
        """Mark the instance as offline (crashed) and invalidate the client."""
        if active and active.status == "online":
            state.update_status(active.key, "offline")
            state.increment_crash_count(active.key)
            logger.warning(
                "UE instance '%s' (PID %s) crashed during tool call",
                active.key,
                active.pid,
            )
        if client:
            client._reachable = False

    def _crash_message(active) -> str:
        pid_info = f" (PID {active.pid})" if active and active.pid else ""
        inst_key = active.key if active else "unknown"
        return (
            f"[UE CRASHED] Instance '{inst_key}'{pid_info} crashed during tool execution.\n"
            f"Use get_log(source='crash') for crash details, "
            f"or launch_editor(action='restart') to restart."
        )

    def _check_crash_fallback(failed: bool, state, active, client) -> str | None:
        """After a failed call, check whether UE actually crashed (PID dead)."""
        if not failed or not active or not active.pid:
            return None
        from unrealhub.utils.process import is_process_alive

        if not is_process_alive(active.pid):
            _handle_crash(state, active, client)
            return _crash_message(active)
        return None

    @mcp.tool()
    async def ue_status() -> str:
        """Get the status of the current active UE instance.
        Shows: online/offline/crashed, PID, port, project path."""
        state = get_state()
        active = state.get_active_instance()
        if not active:
            return "No active instance.\n" + (
                state.list_instances_summary() or "No instances registered. Run discover_instances()."
            )

        lines = [
            f"Active instance: {active.key}",
            f"Status: {active.status}",
            f"URL: {active.url}",
            f"PID: {active.pid or 'unknown'}",
            f"Project: {active.project_path or 'unknown'}",
            f"Crashes: {active.crash_count}",
            f"Last seen: {active.last_seen}",
        ]
        return "\n".join(lines)

    @mcp.tool()
    async def ue_list_domains() -> str:
        """List all available tool domains from the active UE instance.

        Returns domain names with descriptions. Use ue_list_tools(domain="<name>")
        to see tools within a specific domain, then ue_call() to invoke them.
        """
        client = get_client(None)
        if not client:
            return _offline_message()

        state = get_state()
        active = state.get_active_instance()
        pid = _refresh_pid_if_stale(state, active)

        try:
            result = await _with_crash_guard(
                client.call_tool("get_dispatch", {"domain": ""}), pid
            )
        except _UECrashed:
            _handle_crash(state, active, client)
            return _crash_message(active)

        crash_msg = _check_crash_fallback(
            not result.get("success"), state, active, client
        )
        if crash_msg:
            return crash_msg

        if not result.get("success"):
            return _format_tool_result(result)

        raw_text = _format_tool_result(result)
        try:
            data = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError):
            return raw_text

        domains_info = data.get("domains_info", [])
        domain_names = data.get("domains", [])

        if not domains_info and not domain_names:
            return "No domains registered on the active UE instance."

        inst_key = active.key if active else "unknown"
        lines = [f"UE Instance '{inst_key}' has {len(domains_info or domain_names)} domain(s):\n"]
        if domains_info:
            for info in domains_info:
                name = info.get("domain", "?")
                desc = info.get("description", "")
                lines.append(f"  {name}: {desc}" if desc else f"  {name}")
        else:
            for name in domain_names:
                lines.append(f"  {name}")

        lines.append("")
        lines.append("Use ue_list_tools(domain=\"<name>\") to see tools in a domain.")
        return "\n".join(lines)

    @mcp.tool()
    async def ue_list_tools(domain: str = "") -> str:
        """List available tools from the active UE instance.

        domain: If empty, lists all MCP-level tools with parameter schemas.
                If specified, queries the dispatch system for tools in that domain.

        Use this to discover available tools before calling ue_call().
        """
        client = get_client(None)
        if not client:
            return _offline_message()

        state = get_state()
        active = state.get_active_instance()
        pid = _refresh_pid_if_stale(state, active)

        if domain:
            try:
                result = await _with_crash_guard(
                    client.call_tool("get_dispatch", {"domain": domain}), pid
                )
            except _UECrashed:
                _handle_crash(state, active, client)
                return _crash_message(active)
            crash_msg = _check_crash_fallback(
                not result.get("success"), state, active, client
            )
            if crash_msg:
                return crash_msg
            return _format_tool_result(result)

        try:
            tools = await _with_crash_guard(client.list_tools(), pid)
        except _UECrashed:
            _handle_crash(state, active, client)
            return _crash_message(active)
        crash_msg = _check_crash_fallback(not tools, state, active, client)
        if crash_msg:
            return crash_msg

        if not tools:
            return "No tools returned from UE instance."

        inst_key = active.key if active else "unknown"
        lines = [f"UE Instance '{inst_key}' has {len(tools)} tool(s):\n"]
        for t in tools:
            lines.append(f"### {t.get('name', '?')}")
            if t.get("description"):
                lines.append(f"  {t['description'][:200]}")
            schema = t.get("inputSchema", {})
            props = schema.get("properties", {})
            if props:
                required = set(schema.get("required", []))
                params = []
                for pname, pinfo in props.items():
                    ptype = pinfo.get("type", "any")
                    req = " (required)" if pname in required else ""
                    desc = pinfo.get("description", "")
                    params.append(
                        f"    {pname}: {ptype}{req}"
                        + (f" - {desc[:80]}" if desc else "")
                    )
                lines.append("  Parameters:")
                lines.extend(params)
            lines.append("")

        return "\n".join(lines)

    @mcp.tool()
    async def ue_call(
        tool_name: str, arguments: dict[str, Any] | None = None, domain: str = ""
    ) -> str:
        """Call a tool on the active UE instance.

        tool_name: Name of the tool (e.g. 'search_console_commands').
        arguments: Tool arguments as a dict (e.g. {"keyword": "stat"}).
        domain: If specified, calls via the dispatch system (e.g. 'level', 'blueprint').
                If empty, calls the tool directly.

        Use ue_list_tools() first to see available tools and their parameter schemas.
        """
        client = get_client(None)
        if not client:
            return _offline_message()

        state = get_state()
        active = state.get_active_instance()
        pid = _refresh_pid_if_stale(state, active)
        start = time.time()

        if domain:
            coro = client.call_tool(
                "call_dispatch_tool",
                {
                    "domain": domain,
                    "tool_name": tool_name,
                    "arguments": json.dumps(arguments) if arguments else "{}",
                },
            )
            log_name = f"{domain}/{tool_name}"
        else:
            coro = client.call_tool(tool_name, arguments or {})
            log_name = tool_name

        try:
            result = await _with_crash_guard(coro, pid)
        except _UECrashed:
            _handle_crash(state, active, client)
            return _crash_message(active)

        duration = (time.time() - start) * 1000

        if active:
            state.record_tool_call(active.key, log_name, result["success"], duration)
            state.save()

        crash_msg = _check_crash_fallback(
            not result.get("success"), state, active, client
        )
        if crash_msg:
            return crash_msg

        return _format_tool_result(result)

    @mcp.tool()
    async def ue_run_python(script: str) -> str:
        """Execute a Python script in the UE Editor. The 'result' variable will be returned.
        Operates on the active instance (switch with manage_instance(action='use'))."""
        client = get_client(None)
        if not client:
            return _offline_message()

        state = get_state()
        active = state.get_active_instance()
        pid = _refresh_pid_if_stale(state, active)

        start = time.time()
        try:
            result = await _with_crash_guard(
                client.call_tool("run_python_script", {"script": script}), pid
            )
        except _UECrashed:
            _handle_crash(state, active, client)
            return _crash_message(active)

        duration = (time.time() - start) * 1000

        if active:
            state.record_tool_call(
                active.key, "run_python_script", result["success"], duration
            )
            state.save()

        crash_msg = _check_crash_fallback(
            not result.get("success"), state, active, client
        )
        if crash_msg:
            return crash_msg

        return _format_tool_result(result)

