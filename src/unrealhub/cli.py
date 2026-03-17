import asyncio
import click
import logging
import sys


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
def main(verbose: bool):
    """UnrealMCPHub - Manage Unreal Engine MCP instances."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


@main.command()
@click.option("--stdio", is_flag=True, default=True, help="Run in stdio mode (default)")
@click.option("--http", is_flag=True, help="Run in HTTP mode")
@click.option("--port", default=9422, help="HTTP port (default 9422)")
@click.option("--host", default="0.0.0.0", help="HTTP host")
def serve(stdio: bool, http: bool, port: int, host: str):
    """Start the MCP server."""
    from unrealhub.server import run_stdio, run_http

    if http:
        click.echo(f"Starting Hub MCP server (HTTP) on {host}:{port}")
        run_http(host=host, port=port)
    else:
        run_stdio()


@main.command()
@click.argument("uproject_path")
@click.option("--engine", default="", help="Engine root path")
@click.option("--name", default="", help="Project name")
@click.option("--port", default=8422, help="MCP port")
def setup(uproject_path: str, engine: str, name: str, port: int):
    """Configure a UE project."""
    from pathlib import Path

    from unrealhub.server import get_config
    from unrealhub.utils.ue_paths import UEPathResolver

    config = get_config()
    uproject = Path(uproject_path)
    if not uproject.exists():
        click.echo(f"Error: {uproject_path} not found")
        sys.exit(1)

    if not name:
        name = uproject.stem

    resolved_engine = engine
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
        click.echo("Could not auto-detect engine. Use --engine to specify.")
        sys.exit(1)

    config.save_project(name, str(uproject), resolved_engine, engine_assoc, port)
    click.echo(f"Project '{name}' configured: {uproject} (engine: {resolved_engine})")


@main.command()
def status():
    """Show all instance statuses."""
    from unrealhub.server import get_config, get_state

    config = get_config()
    state = get_state()

    if config.is_configured():
        proj = config.get_active_project()
        if proj:
            click.echo(
                f"Active project: {config.get_active_project_name()} ({proj.uproject_path})"
            )
    else:
        click.echo("No project configured. Run: unrealhub setup <path.uproject>")

    summary = state.list_instances_summary()
    click.echo(summary or "No instances registered.")


@main.command()
def discover():
    """Discover running UE MCP instances."""
    from unrealhub.server import get_config, get_state
    from unrealhub.ue_client import UEMCPClient

    config = get_config()
    state = get_state()
    ports = config.get_scan_ports()

    click.echo(f"Scanning ports: {ports}")

    async def scan():
        from unrealhub.tools.discovery_tools import probe_unreal_mcp_with_fallback

        results = []
        for port in ports:
            url = f"http://localhost:{port}/mcp"
            probe = await probe_unreal_mcp_with_fallback(url, timeout=2.0)
            if probe:
                matched_url, _ = probe
                results.append({"port": port, "url": matched_url})
                click.echo(f"  Found Unreal MCP: port {port} ({matched_url})")

        if not results:
            click.echo("No Unreal MCP instances found.")
            return

        for r in results:
            inst = state.upsert(port=r["port"], url=r["url"], status="online")
            click.echo(f"  Registered: {inst.key}")

        state.save()

    asyncio.run(scan())


@main.command()
@click.option("--target", default="Editor", help="Build target")
@click.option("--config", "configuration", default="Development", help="Build configuration")
def compile(target: str, configuration: str):
    """Compile the active project."""
    import subprocess

    from unrealhub.server import get_config
    from unrealhub.utils.ue_paths import UEPathResolver

    config = get_config()
    proj = config.get_active_project()
    if not proj:
        click.echo("No project configured.")
        sys.exit(1)

    paths = UEPathResolver.resolve_from_uproject(proj.uproject_path, proj.engine_root)
    build_target = f"{paths.project_name}{target}"

    cmd = [
        paths.build_bat,
        build_target,
        "Win64",
        configuration,
        paths.uproject_path,
        "-waitmutex",
    ]
    click.echo(f"Building: {build_target} Win64 {configuration}")

    result = subprocess.run(cmd, capture_output=False)
    sys.exit(result.returncode)


@main.command()
def launch():
    """Launch the UE Editor for the active project."""
    import subprocess

    from unrealhub.server import get_config
    from unrealhub.utils.ue_paths import UEPathResolver

    config = get_config()
    proj = config.get_active_project()
    if not proj:
        click.echo("No project configured.")
        sys.exit(1)

    paths = UEPathResolver.resolve_from_uproject(proj.uproject_path, proj.engine_root)
    click.echo(f"Launching: {paths.editor_exe} {paths.uproject_path}")
    subprocess.Popen([paths.editor_exe, paths.uproject_path])
    click.echo(
        "Editor started. Use 'unrealhub discover' to register the MCP instance once ready."
    )


@main.command()
def monitor():
    """Start real-time monitoring of instances."""
    import time

    from unrealhub.server import get_state, get_watcher

    state = get_state()
    watcher = get_watcher()

    def on_crash(instance_id: str):
        click.echo(f"\n*** CRASH DETECTED: {instance_id} ***")

    watcher.on_crash(on_crash)
    watcher.start()

    click.echo("Monitoring started. Press Ctrl+C to stop.\n")
    try:
        while True:
            summary = state.list_instances_summary()
            click.clear()
            click.echo("=== UnrealMCPHub Monitor ===\n")
            click.echo(summary or "No instances.")
            time.sleep(5)
    except KeyboardInterrupt:
        watcher.stop()
        click.echo("\nMonitoring stopped.")


if __name__ == "__main__":
    main()
