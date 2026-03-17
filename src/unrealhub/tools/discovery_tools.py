import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

import httpx
import psutil

from mcp.server.fastmcp import FastMCP

from unrealhub.ue_client import UEMCPClient
from unrealhub.utils.process import find_unreal_editor_processes

logger = logging.getLogger(__name__)

STALE_INSTANCE_HOURS = 1.0
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


# ------------------------------------------------------------------
# Probe: verify endpoint is an Unreal MCP server
# ------------------------------------------------------------------

def _parse_response(resp: httpx.Response) -> dict | None:
    """Parse JSON-RPC response from either plain JSON or SSE format."""
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                try:
                    return json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
        return None
    try:
        return resp.json()
    except Exception:
        return None


async def probe_unreal_mcp(url: str, timeout: float = 3.0) -> dict | None:
    """Probe endpoint and verify it is an Unreal MCP server.

    Returns {"server_name": ...} if the endpoint responds with a serverInfo
    whose name contains "unreal" (case-insensitive). Returns None otherwise.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "method": "initialize", "id": 1,
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "unrealhub-probe", "version": "0.1.0"},
                },
            }, headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            })
            if resp.status_code not in (200, 201, 202):
                return None
            data = _parse_response(resp)
            if not data:
                return None
            result = data.get("result", {})
            server_name = result.get("serverInfo", {}).get("name", "")
            if "unreal" in server_name.lower():
                return {"server_name": server_name}
            return None
    except Exception:
        return None


def _candidate_urls_for_port(port: int) -> list[str]:
    """Return loopback URL candidates for a port, preferring numeric loopback."""
    return [f"http://{host}:{port}/mcp" for host in LOOPBACK_HOSTS]


def candidate_urls_for_url(url: str) -> list[str]:
    """Return URL candidates for a known endpoint, expanding local loopback hosts."""
    if not url:
        return []

    candidates = [url]
    parsed = urlparse(url)
    if parsed.port and parsed.hostname in LOOPBACK_HOSTS:
        for candidate in _candidate_urls_for_port(parsed.port):
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


async def probe_unreal_mcp_with_fallback(
    url: str,
    timeout: float = 3.0,
) -> tuple[str, dict] | None:
    """Probe an endpoint and loopback variants, returning the first verified match."""
    for candidate in candidate_urls_for_url(url):
        info = await probe_unreal_mcp(candidate, timeout=timeout)
        if info:
            return candidate, info
    return None


# ------------------------------------------------------------------
# Identify: ask the instance who it is
# ------------------------------------------------------------------

def _find_uproject_in_dir(project_dir: str) -> str:
    d = Path(project_dir)
    if not d.is_dir():
        return ""
    for f in d.iterdir():
        if f.suffix.lower() == ".uproject":
            return str(f)
    return ""


async def _identify_via_mcp(url: str) -> dict | None:
    """Ask the instance about itself via MCP get_unreal_state tool."""
    for candidate in candidate_urls_for_url(url):
        try:
            client = UEMCPClient(candidate, timeout_connect=3.0, timeout_read=10.0)
            result = await client.call_tool("get_unreal_state", {})
            if not result.get("success"):
                continue
            for item in result.get("content", []):
                if item.get("type") != "text":
                    continue
                data = json.loads(item["text"])
                if data.get("status") != "connected":
                    continue
                paths = data.get("paths", {})
                project_dir = paths.get("project_dir", "")
                engine_dir = paths.get("engine_dir", "")
                uproject = _find_uproject_in_dir(project_dir) if project_dir else ""
                return {
                    "project_path": uproject or project_dir,
                    "project_name": Path(uproject).stem if uproject else Path(project_dir).name if project_dir else "",
                    "engine_root": engine_dir,
                    "url": candidate,
                }
        except Exception:
            logger.debug("_identify_via_mcp failed for %s", candidate, exc_info=True)
    return None


def _identify_via_psutil(port: int) -> dict | None:
    """Fallback: find UE process listening on the given port via psutil."""
    for proc in find_unreal_editor_processes():
        try:
            conns = psutil.Process(proc["pid"]).net_connections(kind="tcp")
            if any(c.laddr.port == port and c.status == "LISTEN" for c in conns):
                pp = proc.get("project_path") or ""
                return {
                    "project_path": pp,
                    "project_name": Path(pp).stem if pp else "",
                    "engine_root": "",
                    "pid": proc["pid"],
                }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


async def _identify_instance(port: int, url: str) -> dict:
    """Identify instance via MCP first, psutil fallback."""
    info = await _identify_via_mcp(url)
    if info:
        ps = _identify_via_psutil(port)
        if ps:
            info["pid"] = ps["pid"]
        return info
    return _identify_via_psutil(port) or {
        "project_path": "", "project_name": "", "engine_root": "",
    }


# ------------------------------------------------------------------
# Scan helpers
# ------------------------------------------------------------------

async def _scan_ports(ports: list[int]) -> list[dict]:
    """Probe ports concurrently, return list of {port, url, server_name}."""
    results: list[dict] = []

    async def _probe(port: int) -> None:
        for url in _candidate_urls_for_port(port):
            info = await probe_unreal_mcp(url, timeout=5.0)
            if info:
                results.append({"port": port, "url": url, **info})
                return

    await asyncio.gather(*(_probe(p) for p in ports))
    return results


# ------------------------------------------------------------------
# Shared discovery primitives (used by both discover_instances and watcher)
# ------------------------------------------------------------------

def register_orphan_processes(state) -> list[str]:
    """Find UE Editor processes not yet registered and add them as port=0 offline.

    Returns report lines for newly registered orphans and extra processes.
    """
    registered_pids = {inst.pid for inst in state.list_instances() if inst.pid}
    report: list[str] = []

    for proc in find_unreal_editor_processes():
        pid = proc["pid"]
        if pid in registered_pids:
            continue
        project_path = proc.get("project_path") or ""
        if project_path:
            existing = state.find_by_project_path(project_path)
            if existing:
                attached = False
                for inst in existing:
                    if not inst.pid:
                        state.update_status(inst.key, inst.status, pid=pid)
                        attached = True
                        break
                if not attached:
                    name = Path(project_path).stem
                    matched_key = existing[0].key
                    report.append(f"  PID {pid}: {name} [extra process, see {matched_key}]")
                    logger.info("Extra UE process PID %d for %s (already tracked as %s)", pid, name, matched_key)
                continue

        instance = state.upsert(
            port=0,
            project_path=project_path,
            pid=pid,
            status="offline",
        )
        name = Path(project_path).stem if project_path else f"PID {pid}"
        report.append(f"  {instance.key}: {name} (PID {pid}) [NO MCP]")
        logger.info("Registered orphan UE process: %s (PID %d)", instance.key, pid)

    return report


async def reprobe_offline_instances(state) -> list[str]:
    """Re-probe offline instances with known ports; mark online if MCP responds.

    Returns keys of instances that came back online.
    """
    recovered: list[str] = []
    for inst in state.list_instances():
        if inst.status == "online" or inst.port == 0:
            continue
        candidates = candidate_urls_for_url(inst.url)
        if not candidates:
            candidates = _candidate_urls_for_port(inst.port)

        for url in candidates:
            info = await probe_unreal_mcp(url, timeout=3.0)
            if not info:
                continue
            state.upsert(
                port=inst.port,
                project_path=inst.project_path,
                url=url,
                engine_root=inst.engine_root,
                pid=inst.pid,
                status="online",
            )
            recovered.append(inst.key)
            logger.info("Instance %s came back online via %s", inst.key, url)
            break
    return recovered


async def scan_ports_for_new(state, scan_ports: list[int]) -> list[str]:
    """Scan ports for new MCP instances not yet registered as online.

    Returns keys of newly registered instances.
    """
    known_online_ports = {
        inst.port for inst in state.list_instances() if inst.status == "online"
    }
    new_keys: list[str] = []
    for port in scan_ports:
        if port in known_online_ports:
            continue
        for url in _candidate_urls_for_port(port):
            info = await probe_unreal_mcp(url, timeout=3.0)
            if not info:
                continue
            inst = state.upsert(port=port, url=url, status="online")
            new_keys.append(inst.key)
            logger.info("Discovered new instance on port %d via %s: %s", port, url, inst.key)
            break
    return new_keys


# ------------------------------------------------------------------
# Tool registration
# ------------------------------------------------------------------

def register_discovery_tools(mcp: FastMCP, get_config, get_state) -> None:

    @mcp.tool()
    async def discover_instances(
        rescan: bool = False,
        extra_ports: str = "",
    ) -> str:
        """Discover and list UE MCP instances.

        rescan: If True, actively scans ports for running Unreal MCP endpoints.
                Priority ports are scanned first; if none found, extended range
                (8000-8999) is scanned. Discovered instances are auto-registered.
                If False, lists instances from stored state (fast).
        extra_ports: Comma-separated additional ports to scan (e.g. "9500,9501").
                     These are always scanned alongside priority ports, useful when
                     you know the UE instance is running on a non-standard port.
        """
        state = get_state()

        if not rescan:
            summary = state.list_instances_summary()
            return summary or "No instances registered. Run discover_instances(rescan=True) first."

        config = get_config()

        user_ports: list[int] = []
        if extra_ports:
            for tok in extra_ports.replace(" ", "").split(","):
                try:
                    user_ports.append(int(tok))
                except ValueError:
                    pass

        # Phase 1: priority ports + user-specified ports
        priority_ports = config.get_scan_ports()
        phase1_ports = list(dict.fromkeys(priority_ports + user_ports))
        found = await _scan_ports(phase1_ports)

        # Phase 2: extended scan if Phase 1 found nothing
        if not found:
            extended = config.get_extended_ports()
            already = set(phase1_ports)
            extra = [p for p in extended if p not in already]
            found = await _scan_ports(extra)

        all_scanned_ports = set(phase1_ports)
        if not found:
            all_scanned_ports |= set(config.get_extended_ports())

        # Phase 3: mark non-responding ports offline
        # "online" means MCP endpoint is reachable, not just process alive
        responded_ports = {r["port"] for r in found}
        for inst in state.list_instances():
            if inst.status != "online":
                continue
            if inst.port in all_scanned_ports and inst.port not in responded_ports:
                state.update_status(inst.key, "offline")
                logger.info("Marked %s offline (port %d not responding)", inst.key, inst.port)

        if not found:
            register_orphan_processes(state)
            state.cleanup(max_age_hours=STALE_INSTANCE_HOURS)

            ue_procs = find_unreal_editor_processes()
            lines = [
                f"No Unreal MCP instances found on priority ports {priority_ports} "
                f"or extended range.",
            ]
            if ue_procs:
                lines.append(f"\nFound {len(ue_procs)} UE Editor process(es) running:")
                for proc in ue_procs:
                    pid = proc["pid"]
                    project = proc.get("project_path") or "unknown project"
                    lines.append(f"  PID {pid}: {project}")
                lines.append(
                    "\nUE is running but MCP endpoint not responding. "
                    "Check if RemoteMCP plugin is enabled and loaded."
                )
            else:
                lines.append("No UE Editor processes found. Is UE Editor running?")
            return "\n".join(lines)

        # Phase 4: identify and upsert each discovered instance
        report_lines: list[str] = []
        for r in found:
            info = await _identify_instance(r["port"], r["url"])
            instance = state.upsert(
                port=r["port"],
                project_path=info.get("project_path", ""),
                url=info.get("url", r["url"]),
                engine_root=info.get("engine_root", ""),
                pid=info.get("pid"),
                status="online",
            )
            tag = "UPDATED" if info.get("project_path") else "NEW (unknown project)"
            report_lines.append(f"  {instance.key}: {tag}")

        # Phase 5: find UE processes without MCP and register as offline
        orphan_lines = register_orphan_processes(state)

        state.cleanup(max_age_hours=STALE_INSTANCE_HOURS)

        lines = [f"Discovered {len(found)} Unreal MCP instance(s):"]
        lines.extend(report_lines)
        if orphan_lines:
            lines.append(f"\nAlso found {len(orphan_lines)} UE process(es) without MCP:")
            lines.extend(orphan_lines)

        active = state.get_active_instance()
        if active:
            lines.append(f"\nActive instance: {active.key}")

        return "\n".join(lines)

    @mcp.tool()
    async def manage_instance(
        action: str,
        instance: str = "",
        url: str = "",
        port: int = 0,
    ) -> str:
        """Manage UE MCP instances: register, unregister, or switch active.

        action: 'register', 'unregister', or 'use'.
        instance: Instance key, port, or project name (for unregister/use).
        url: MCP endpoint URL (for register, e.g. 'http://localhost:8422/mcp').
        port: Port number for register (auto-extracted from URL if 0).
        """
        state = get_state()

        if action == "register":
            if not url:
                return "url is required for 'register' action."
            if not port:
                try:
                    parsed = urlparse(url)
                    port = parsed.port or 8422
                except Exception:
                    port = 8422
            inst = state.upsert(port=port, url=url)
            return f"Registered instance: {inst.key} at {url}"

        if action == "unregister":
            if not instance:
                return "instance is required for 'unregister' action."
            if state.unregister_instance(instance):
                return f"Instance '{instance}' removed."
            return f"Instance '{instance}' not found."

        if action == "use":
            if not instance:
                return "instance is required for 'use' action."
            inst = state.get_instance(instance)
            if not inst:
                available = state.list_instances_summary()
                return f"Instance '{instance}' not found.\n{available}"
            state.set_active(instance)
            return (
                f"Active instance switched to: {inst.key}\n"
                f"URL: {inst.url}, Status: {inst.status}"
            )

        return f"Unknown action '{action}'. Use 'register', 'unregister', or 'use'."
