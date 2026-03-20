from pydantic import BaseModel
from pathlib import Path
from datetime import datetime
import json

CONFIG_DIR = Path.home() / ".unrealhub"
CONFIG_PATH = CONFIG_DIR / "config.json"


class ProjectEntry(BaseModel):
    uproject_path: str
    engine_root: str
    engine_association: str = ""
    mcp_port: int = 8422
    configured_at: str = ""


PLUGIN_GITHUB_OWNER = "blackplume233"
PLUGIN_GITHUB_REPO = "UnrealRemoteMCP"

PLUGIN_TAG = "v1.0.0"

def _plugin_zip_url(ref: str = PLUGIN_TAG) -> str:
    """Build the GitHub archive download URL for a given git ref (tag or branch)."""
    if "/" not in ref and ref != "master" and ref != "main":
        path = f"refs/tags/{ref}"
    else:
        path = f"refs/heads/{ref}"
    return f"https://github.com/{PLUGIN_GITHUB_OWNER}/{PLUGIN_GITHUB_REPO}/archive/{path}.zip"

DEFAULT_PLUGIN_REPO = _plugin_zip_url(PLUGIN_TAG)


class HubConfig(BaseModel):
    projects: dict[str, ProjectEntry] = {}
    active_project: str = ""
    scan_ports: list[int] = [8422, 8423, 8424, 8425]
    scan_ports_extended: list[int] = list(range(8000, 9000))
    plugin_repo: str = DEFAULT_PLUGIN_REPO
    plugin_local_cache: str = ""


class ProjectConfig:
    def __init__(self):
        self._config: HubConfig = HubConfig()
        self._load()

    def _load(self) -> None:
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                self._config = HubConfig.model_validate(data)
            except (json.JSONDecodeError, Exception):
                pass

    def _save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            self._config.model_dump_json(indent=2), encoding="utf-8"
        )

    def is_configured(self) -> bool:
        return len(self._config.projects) > 0

    def get_active_project(self) -> ProjectEntry | None:
        if not self._config.active_project:
            return None
        return self._config.projects.get(self._config.active_project)

    def get_active_project_name(self) -> str:
        return self._config.active_project or ""

    def save_project(
        self,
        name: str,
        uproject_path: str,
        engine_root: str,
        engine_association: str = "",
        port: int = 8422,
    ) -> ProjectEntry:
        entry = ProjectEntry(
            uproject_path=uproject_path,
            engine_root=engine_root,
            engine_association=engine_association,
            mcp_port=port,
            configured_at=datetime.now().isoformat(),
        )
        self._config.projects[name] = entry
        if not self._config.active_project:
            self._config.active_project = name
        self._save()
        return entry

    def remove_project(self, name: str) -> bool:
        if name not in self._config.projects:
            return False
        del self._config.projects[name]
        if self._config.active_project == name:
            self._config.active_project = (
                next(iter(self._config.projects), "") if self._config.projects else ""
            )
        self._save()
        return True

    def set_active_project(self, name: str) -> bool:
        if name not in self._config.projects:
            return False
        self._config.active_project = name
        self._save()
        return True

    def list_projects(self) -> dict[str, ProjectEntry]:
        return dict(self._config.projects)

    def get_scan_ports(self) -> list[int]:
        return list(self._config.scan_ports)

    def get_extended_ports(self) -> list[int]:
        return list(self._config.scan_ports_extended)

    def get_plugin_repo(self) -> str:
        return self._config.plugin_repo

    def set_plugin_repo(self, url: str) -> None:
        self._config.plugin_repo = url
        self._save()

    def get_plugin_cache(self) -> str:
        return self._config.plugin_local_cache

    def set_plugin_cache(self, path: str) -> None:
        self._config.plugin_local_cache = path
        self._save()
