import logging

from mcp.server.fastmcp import FastMCP

from unrealhub.tools.discovery_tools import probe_unreal_mcp_with_fallback

logger = logging.getLogger(__name__)


def register_monitor_tools(mcp: FastMCP, get_state, get_watcher) -> None:
    """get_watcher returns ProcessWatcher instance."""

    @mcp.tool()
    async def get_instance_health(instance: str = "") -> str:
        """Get detailed health status of a UE instance.
        If instance is empty, uses active instance."""
        state = get_state()
        if not instance:
            inst = state.get_active_instance()
        else:
            inst = state.get_instance(instance)

        if not inst:
            return "No instance found. Run discover_instances() first."

        from unrealhub.utils.process import get_process_info, is_process_alive

        lines = [
            f"Instance: {inst.key}",
            f"URL: {inst.url}",
            f"Status: {inst.status}",
            f"PID: {inst.pid or 'unknown'}",
            f"Project: {inst.project_path or 'unknown'}",
            f"Crash count: {inst.crash_count}",
            f"Last seen: {inst.last_seen}",
        ]

        if inst.pid:
            alive = is_process_alive(inst.pid)
            lines.append(f"Process alive: {alive}")
            if alive:
                info = get_process_info(inst.pid)
                if info:
                    lines.append(f"CPU: {info.get('cpu_percent', '?')}%, Memory: {info.get('memory_mb', '?')} MB")

        probe = await probe_unreal_mcp_with_fallback(inst.url, timeout=2.0)
        if probe:
            matched_url, _ = probe
            lines.append(f"Unreal MCP endpoint: responding via {matched_url}")
        else:
            lines.append("Unreal MCP endpoint: NOT responding")

        return "\n".join(lines)
