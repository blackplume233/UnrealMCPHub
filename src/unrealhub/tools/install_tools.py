import asyncio
import io
import json
import logging
import shutil
import zipfile
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from unrealhub.config import CONFIG_DIR

logger = logging.getLogger(__name__)

CACHE_DIR = CONFIG_DIR / "cache"


async def _download_plugin_zip(repo_url: str) -> Path | None:
    """Download a zip from repo_url and extract it. Returns extracted dir or None."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = CACHE_DIR / "RemoteMCP-latest.zip"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True) as client:
            resp = await client.get(repo_url)
            resp.raise_for_status()
            zip_path.write_bytes(resp.content)
    except Exception as e:
        logger.error("Failed to download plugin zip: %s", e)
        return None

    extract_dir = CACHE_DIR / "RemoteMCP-extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)

    try:
        with zipfile.ZipFile(io.BytesIO(zip_path.read_bytes())) as zf:
            zf.extractall(extract_dir)
    except Exception as e:
        logger.error("Failed to extract plugin zip: %s", e)
        return None

    for child in extract_dir.iterdir():
        if child.is_dir() and (child / "RemoteMCP.uplugin").exists():
            return child

    return None


def _find_local_plugin(config, project_dir: Path) -> str | None:
    """Search local paths for an existing RemoteMCP source directory."""
    cache_path = config.get_plugin_cache()
    candidates = [
        Path(cache_path) if cache_path else None,
        Path(__file__).resolve().parents[4] / "RemoteMCP",
        project_dir / "Plugins" / "RemoteMCP",
    ]
    for c in candidates:
        if c and c.is_dir() and (c / "RemoteMCP.uplugin").exists():
            return str(c)
    return None


async def _run_install_deps(python_dir: Path) -> str:
    """Run env.bat to install Python dependencies. Returns status message."""
    env_bat = python_dir / "env.bat"
    if not env_bat.exists():
        return "Python deps: env.bat not found, install manually."

    try:
        proc = await asyncio.create_subprocess_exec(
            "cmd", "/c", str(env_bat),
            cwd=str(python_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        text = output.decode("utf-8", errors="replace")
        if proc.returncode == 0:
            return "Python deps: INSTALLED"
        return f"Python deps: install FAILED (exit {proc.returncode})\n{text}"
    except asyncio.TimeoutError:
        return "Python deps: install timed out (120s)."
    except Exception as e:
        logger.exception("_run_install_deps failed")
        return f"Python deps: install failed: {e}"


def _copy_and_enable(source: Path, dest: Path, uproject_path: Path) -> str:
    """Copy plugin source to dest and enable in .uproject."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(
            str(source), str(dest),
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".git", "Intermediate", "Binaries",
            ),
        )
    except Exception as e:
        logger.exception("Plugin copy failed")
        return f"Failed to copy plugin: {e}"

    lines = [f"RemoteMCP installed to: {dest}"]
    lines.append(_enable_plugins_in_uproject(str(uproject_path)))
    return "\n".join(lines)


def _enable_plugins_in_uproject(uproject_path: str) -> str:
    try:
        with open(uproject_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        plugins = data.setdefault("Plugins", [])
        plugin_map = {p["Name"]: p for p in plugins if "Name" in p}

        changed = False
        for name in ["PythonScriptPlugin", "RemoteMCP"]:
            if name in plugin_map:
                if not plugin_map[name].get("Enabled", False):
                    plugin_map[name]["Enabled"] = True
                    changed = True
            else:
                plugins.append({"Name": name, "Enabled": True})
                changed = True

        if changed:
            with open(uproject_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return "Plugins enabled in .uproject: PythonScriptPlugin, RemoteMCP"
        return "Plugins already enabled in .uproject."
    except Exception as e:
        logger.exception("_enable_plugins_in_uproject failed")
        return f"Failed to update .uproject: {e}"


async def perform_install_plugin(config, uproject_path: str) -> str:
    """Install RemoteMCP plugin to a project. Reusable from setup_project or standalone.

    Handles: already-installed (idempotent), local auto-detect, GitHub download,
    .uproject enabling, and Python dependency installation.
    """
    target_path = Path(uproject_path)
    if not target_path.exists():
        return f"Plugin install skipped: .uproject not found at {uproject_path}"

    project_dir = target_path.parent
    dest_dir = project_dir / "Plugins" / "RemoteMCP"

    if dest_dir.exists() and (dest_dir / "RemoteMCP.uplugin").exists():
        enable_result = _enable_plugins_in_uproject(str(target_path))
        python_dir = dest_dir / "Content" / "Python"
        deps_result = ""
        if python_dir.exists() and not (python_dir / "Lib" / "site-packages" / "mcp").exists():
            deps_result = "\n" + await _run_install_deps(python_dir)
        return f"RemoteMCP already installed at: {dest_dir}\n{enable_result}{deps_result}"

    # --- Tier 1: local auto-detect (sibling dir / cache / config) ---
    local = _find_local_plugin(config, project_dir)
    if local:
        result = _copy_and_enable(Path(local), dest_dir, target_path)
        deps_result = await _run_install_deps(dest_dir / "Content" / "Python")
        return f"{result}\n{deps_result}"

    # --- Tier 2: download from GitHub ---
    repo_url = config.get_plugin_repo()
    download_msg = f"Downloading RemoteMCP from:\n  {repo_url}\n"

    extracted = await _download_plugin_zip(repo_url)
    if not extracted:
        return (
            f"{download_msg}"
            "Download FAILED. Check your network or re-run "
            "setup_project(plugin_repo=...) with a valid URL."
        )

    config.set_plugin_cache(str(extracted))
    result = _copy_and_enable(extracted, dest_dir, target_path)
    deps_result = await _run_install_deps(dest_dir / "Content" / "Python")
    return f"{download_msg}Download OK.\n\n{result}\n{deps_result}"


def register_install_tools(mcp: FastMCP, get_config) -> None:

    @mcp.tool()
    async def check_plugin_status(target_project: str = "") -> str:
        """Check if RemoteMCP plugin is installed and enabled."""
        config = get_config()
        if not target_project:
            proj = config.get_active_project()
            if not proj:
                return "No project configured."
            target_project = proj.uproject_path

        target_path = Path(target_project)
        project_dir = target_path.parent
        plugin_dir = project_dir / "Plugins" / "RemoteMCP"

        from unrealhub.config import PLUGIN_TAG
        lines = [f"Project: {target_project}", f"Hub pinned plugin tag: {PLUGIN_TAG}"]

        if plugin_dir.exists() and (plugin_dir / "RemoteMCP.uplugin").exists():
            lines.append("Plugin directory: INSTALLED")
        else:
            lines.append("Plugin directory: NOT FOUND")
            lines.append("  Run setup_project() with install_plugin=True to install.")
            return "\n".join(lines)

        try:
            with open(target_project, "r", encoding="utf-8") as f:
                data = json.load(f)
            plugins_map = {
                p.get("Name"): p.get("Enabled", False)
                for p in data.get("Plugins", [])
                if "Name" in p
            }
            for name in ["RemoteMCP", "PythonScriptPlugin"]:
                if name in plugins_map:
                    status = "ENABLED" if plugins_map[name] else "DISABLED"
                else:
                    status = "NOT IN .uproject"
                lines.append(f"{name}: {status}")
        except Exception as e:
            lines.append(f"Error reading .uproject: {e}")

        python_dir = plugin_dir / "Content" / "Python"
        if (python_dir / "Lib" / "site-packages" / "mcp").exists():
            lines.append("Python deps: INSTALLED")
        else:
            lines.append("Python deps: NOT FOUND (run setup_project to install)")

        return "\n".join(lines)
