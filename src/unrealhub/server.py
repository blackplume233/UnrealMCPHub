import logging
from mcp.server.fastmcp import FastMCP

from unrealhub.config import ProjectConfig
from unrealhub.state import StateStore, make_key
from unrealhub.watcher import ProcessWatcher
from unrealhub.ue_client import UEMCPClient

logger = logging.getLogger(__name__)

_config: ProjectConfig | None = None
_state: StateStore | None = None
_watcher: ProcessWatcher | None = None
_clients: dict[str, UEMCPClient] = {}


def get_config() -> ProjectConfig:
    global _config
    if _config is None:
        _config = ProjectConfig()
    return _config


def _on_instance_unregistered(key: str) -> None:
    """Callback: clean up cached client when an instance is removed."""
    removed = _clients.pop(key, None)
    if removed:
        logger.debug("Cleaned up cached client for %s", key)


def get_state() -> StateStore:
    global _state
    if _state is None:
        _state = StateStore()
        _state.on_unregister(_on_instance_unregistered)
    return _state


def get_watcher() -> ProcessWatcher:
    global _watcher
    if _watcher is None:
        _watcher = ProcessWatcher(get_state, get_config)
    return _watcher


def _prefer_active_project_instance(state: StateStore) -> str | None:
    """Prefer the instance that matches the configured active project.

    UE editors are routinely restarted onto the same MCP port across different
    projects. When Hub state still carries the previous project metadata for
    that port, route calls to the active project's key and rebind the instance
    metadata in-place.
    """
    config = get_config()
    project = config.get_active_project()
    if not project:
        return None

    desired_key = make_key(project.uproject_path, project.mcp_port)
    desired = state.get_instance(desired_key)
    if desired and desired.status == "online":
        return desired.key

    for candidate in state.find_by_port(project.mcp_port):
        if candidate.status != "online":
            continue
        rebound = state.upsert(
            port=project.mcp_port,
            project_path=project.uproject_path,
            url=candidate.url or f"http://localhost:{project.mcp_port}/mcp",
            engine_root=project.engine_root,
            pid=candidate.pid,
            status="online",
        )
        return rebound.key

    return None


def get_client(instance_id: str | None) -> UEMCPClient | None:
    state = get_state()

    if instance_id is None:
        preferred_key = _prefer_active_project_instance(state)
        if preferred_key:
            instance_id = preferred_key
        active = state.get_active_instance() if instance_id is None else None
        if not active:
            if instance_id is None:
                return None
        else:
            instance_id = active.key

    inst = state.get_instance(instance_id)
    if not inst or inst.status != "online":
        return None

    if inst.key not in _clients:
        _clients[inst.key] = UEMCPClient(inst.url)

    return _clients[inst.key]


def get_ue_client_factory():
    return None


def create_hub_mcp() -> FastMCP:
    mcp = FastMCP(
        "UnrealMCPHub",
        instructions=(
            "UnrealMCPHub manages Unreal Engine development lifecycle. "
            "Use setup_project to configure a project first. "
            "Use launch_editor to start UE, discover_instances to find running editors, "
            "and ue_* tools to interact with the engine."
        ),
    )

    @mcp.tool()
    async def setup_project(
        uproject_path: str,
        engine_root: str = "",
        name: str = "",
        port: int = 8422,
        install_plugin: bool = True,
        plugin_repo: str = "",
        plugin_local_path: str = "",
    ) -> str:
        """One-stop project onboarding: configure project and install RemoteMCP plugin.

        uproject_path: Full path to the .uproject file (required).
        engine_root: Engine installation root (auto-detected from .uproject if empty).
        name: Project name (auto-detected from filename if empty).
        port: MCP port for this project (default 8422).
        install_plugin: Auto-install RemoteMCP plugin, enable it, and install Python deps (default True).
        plugin_repo: GitHub zip URL for RemoteMCP plugin (optional, configures download source).
        plugin_local_path: Local path to RemoteMCP directory (optional, highest install priority).

        After setup, run build_project() then launch_editor() to start working.
        """
        from pathlib import Path
        from unrealhub.utils.ue_paths import UEPathResolver

        config = get_config()

        uproject = Path(uproject_path)
        if not uproject.exists():
            return f".uproject not found: {uproject_path}"

        if not name:
            name = uproject.stem

        resolved_engine = engine_root
        engine_assoc = ""
        if not resolved_engine:
            try:
                engine_assoc = UEPathResolver.parse_engine_association(str(uproject))
                resolved_engine = (
                    UEPathResolver.resolve_engine_from_registry(engine_assoc) or ""
                )
            except Exception:
                pass

        if not resolved_engine:
            return (
                f"Could not auto-detect engine root for '{engine_assoc or 'unknown'}'.\n"
                f"Please provide engine_root explicitly:\n"
                f'  setup_project(uproject_path="{uproject_path}", engine_root="D:/Epic/UE_5.5")'
            )

        try:
            paths = UEPathResolver.resolve_from_uproject(str(uproject), resolved_engine)
            missing = UEPathResolver.validate_paths(paths)
        except ValueError as e:
            return f"Path validation failed: {e}"

        entry = config.save_project(
            name, str(uproject), resolved_engine, engine_assoc, port
        )

        lines = [
            f"Project '{name}' configured successfully!",
            f"  .uproject: {uproject}",
            f"  Engine: {resolved_engine}",
            f"  Association: {engine_assoc or 'custom'}",
            f"  MCP Port: {port}",
        ]
        if missing:
            lines.append(f"\n  Warnings (missing paths): {', '.join(missing)}")

        if plugin_repo:
            config.set_plugin_repo(plugin_repo)
            lines.append(f"\n  Plugin repo: {plugin_repo}")

        if plugin_local_path:
            p = Path(plugin_local_path)
            if p.is_dir() and (p / "RemoteMCP.uplugin").exists():
                config.set_plugin_cache(plugin_local_path)
                lines.append(f"  Plugin local path: {plugin_local_path}")
            else:
                lines.append(f"  WARNING: Invalid plugin_local_path (no RemoteMCP.uplugin): {plugin_local_path}")

        if install_plugin:
            from unrealhub.tools.install_tools import perform_install_plugin
            lines.append("\n--- Plugin Install ---")
            install_result = await perform_install_plugin(config, str(uproject))
            lines.append(install_result)
            lines.append("\nNext steps:")
            lines.append("1. build_project() — compile the project")
            lines.append("2. launch_editor() — start the editor")

        return "\n".join(lines)

    @mcp.tool()
    async def get_project_config() -> str:
        """Get the current project configuration. If not configured, provides guidance."""
        config = get_config()
        if not config.is_configured():
            return (
                "No project configured yet.\n"
                'Call setup_project(uproject_path="/path/to/YourProject.uproject") to configure.\n'
                "Engine root will be auto-detected from the .uproject file."
            )

        projects = config.list_projects()
        active_name = config.get_active_project_name()

        lines = [f"Configured projects ({len(projects)}):"]
        for name, entry in projects.items():
            marker = " *" if name == active_name else ""
            lines.append(
                f"  {name}{marker}: {entry.uproject_path} (engine: {entry.engine_root}, port: {entry.mcp_port})"
            )
        lines.append(f"\nActive project: {active_name or '(none)'}")
        return "\n".join(lines)


    @mcp.tool()
    async def remove_project(name: str) -> str:
        """Remove a project configuration."""
        config = get_config()
        if config.remove_project(name):
            return f"Project '{name}' removed."
        return f"Project '{name}' not found."

    @mcp.tool()
    async def hub_status() -> str:
        """One-stop overview of the entire Hub state: project config, plugin install,
        UE instances, ProcessWatcher status, and plugin source config."""
        from pathlib import Path

        config = get_config()
        state = get_state()
        watcher = get_watcher()

        sections: list[str] = ["=== UnrealMCPHub Status ==="]

        # --- Project Config ---
        sections.append("\n[Project Config]")
        if config.is_configured():
            active_name = config.get_active_project_name()
            for name, entry in config.list_projects().items():
                marker = " *" if name == active_name else "  "
                sections.append(
                    f" {marker}{name}: {entry.uproject_path}\n"
                    f"    Engine: {entry.engine_root} ({entry.engine_association or 'custom'})\n"
                    f"    MCP Port: {entry.mcp_port}"
                )
        else:
            sections.append("  No project configured. Run setup_project().")

        # --- Plugin Source ---
        from unrealhub.config import PLUGIN_TAG
        sections.append("\n[Plugin Source]")
        sections.append(f"  Pinned tag: {PLUGIN_TAG}")
        sections.append(f"  Repo: {config.get_plugin_repo()}")
        cache = config.get_plugin_cache()
        sections.append(f"  Local cache: {cache or '(none)'}")

        # --- Plugin Install Status ---
        sections.append("\n[Plugin Install]")
        proj = config.get_active_project()
        if proj:
            project_dir = Path(proj.uproject_path).parent
            plugin_dir = project_dir / "Plugins" / "RemoteMCP"
            if plugin_dir.exists() and (plugin_dir / "RemoteMCP.uplugin").exists():
                sections.append("  Directory: INSTALLED")
                python_dir = plugin_dir / "Content" / "Python"
                has_deps = (python_dir / "Lib" / "site-packages" / "mcp").exists()
                sections.append(f"  Python deps: {'INSTALLED' if has_deps else 'MISSING'}")
            else:
                sections.append("  Directory: NOT FOUND (run setup_project to install)")
        else:
            sections.append("  (no active project)")

        # --- UE Instances ---
        sections.append("\n[UE Instances]")
        instances = state.list_instances()
        if instances:
            active_inst = state.get_active_instance()
            active_key = active_inst.key if active_inst else ""
            for inst in instances:
                marker = "*" if inst.key == active_key else " "
                sections.append(
                    f"  {marker} {inst.key}: "
                    f"{inst.status.upper()}, PID={inst.pid or '?'}"
                )
                if inst.project_path:
                    sections.append(f"    Project: {inst.project_path}")
                sections.append(f"    Last seen: {inst.last_seen or 'never'}")
                if inst.crash_count:
                    sections.append(f"    Crashes: {inst.crash_count}")
                sections.append(
                    f"    Tool calls: {len(inst.call_history)}, "
                    f"Notes: {len(inst.notes)}"
                )
        else:
            sections.append("  No instances registered. Run discover_instances().")

        # --- ProcessWatcher ---
        sections.append("\n[ProcessWatcher]")
        thread = watcher._thread
        running = thread is not None and thread.is_alive()
        sections.append(f"  Status: {'RUNNING' if running else 'STOPPED'}")
        sections.append(f"  Interval: {watcher._interval}s")

        return "\n".join(sections)

    from unrealhub.tools.build_tools import register_build_tools
    from unrealhub.tools.launch_tools import register_launch_tools
    from unrealhub.tools.install_tools import register_install_tools
    from unrealhub.tools.discovery_tools import register_discovery_tools
    from unrealhub.tools.monitor_tools import register_monitor_tools
    from unrealhub.tools.log_tools import register_log_tools
    from unrealhub.tools.proxy_tools import register_proxy_tools
    from unrealhub.tools.session_tools import register_session_tools
    from unrealhub.tools.help_tools import register_help_tools

    register_build_tools(mcp, get_config, get_state)
    register_launch_tools(mcp, get_config, get_state, get_ue_client_factory)
    register_install_tools(mcp, get_config)
    register_discovery_tools(mcp, get_config, get_state)
    register_monitor_tools(mcp, get_state, get_watcher)
    register_log_tools(mcp, get_config, get_state)
    register_proxy_tools(mcp, get_state, get_client)
    register_session_tools(mcp, get_state)
    register_help_tools(mcp)

    return mcp


def run_stdio() -> None:
    logging.basicConfig(level=logging.INFO)
    mcp = create_hub_mcp()

    watcher = get_watcher()
    watcher.start()

    try:
        mcp.run(transport="stdio")
    finally:
        watcher.stop()
        get_state().save()


def run_http(host: str = "0.0.0.0", port: int = 9422) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    mcp = create_hub_mcp()

    watcher = get_watcher()
    watcher.start()

    starlette_app = mcp.streamable_http_app()

    try:
        uvicorn.run(starlette_app, host=host, port=port)
    finally:
        watcher.stop()
        get_state().save()
