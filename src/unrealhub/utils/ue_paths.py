from dataclasses import dataclass
from pathlib import Path
import json
import os
import sys

if sys.platform == "win32":
    import winreg


@dataclass
class ResolvedPaths:
    uproject_path: str
    project_dir: str
    project_name: str
    engine_root: str
    engine_association: str
    ubt_exe: str
    uat_bat: str
    editor_exe: str
    build_bat: str


class UEPathResolver:
    @staticmethod
    def read_uproject_data(uproject_path: str) -> dict:
        path = Path(uproject_path)
        if not path.exists():
            raise ValueError(f"Project file not found: {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid .uproject JSON: {e}") from e

    @staticmethod
    def resolve_from_uproject(
        uproject_path: str, engine_root: str | None = None
    ) -> ResolvedPaths:
        uproj = Path(uproject_path).resolve()
        if not uproj.exists():
            raise ValueError(f"Project file not found: {uproj}")
        if uproj.suffix.lower() != ".uproject":
            raise ValueError(f"Not a .uproject file: {uproj}")

        project_dir = str(uproj.parent)
        project_name = uproj.stem
        engine_association = UEPathResolver.parse_engine_association(str(uproj))

        if engine_root is None:
            resolved = UEPathResolver.resolve_engine_from_registry(engine_association)
            if resolved is None:
                raise ValueError(
                    f"Cannot resolve engine for association '{engine_association}'"
                )
            engine_root = resolved

        engine_root = str(Path(engine_root).resolve())
        derived = UEPathResolver.derive_paths(engine_root)

        return ResolvedPaths(
            uproject_path=str(uproj),
            project_dir=project_dir,
            project_name=project_name,
            engine_root=engine_root,
            engine_association=engine_association,
            ubt_exe=derived["ubt_exe"],
            uat_bat=derived["uat_bat"],
            editor_exe=derived["editor_exe"],
            build_bat=derived["build_bat"],
        )

    @staticmethod
    def parse_engine_association(uproject_path: str) -> str:
        data = UEPathResolver.read_uproject_data(uproject_path)
        assoc = data.get("EngineAssociation")
        if assoc is None:
            return ""
        return str(assoc)

    @staticmethod
    def has_project_modules(uproject_path: str) -> bool:
        data = UEPathResolver.read_uproject_data(uproject_path)
        modules = data.get("Modules", [])
        return bool(modules)

    @staticmethod
    def get_editor_build_target(uproject_path: str, project_name: str) -> str:
        if UEPathResolver.has_project_modules(uproject_path):
            return f"{project_name}Editor"
        return "UnrealEditor"

    @staticmethod
    def resolve_engine_from_registry(engine_association: str) -> str | None:
        if sys.platform != "win32":
            return None
        if not engine_association:
            return None

        try:
            key_path = r"SOFTWARE\EpicGames\Unreal Engine"
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ
            ) as key:
                try:
                    subkey = winreg.OpenKey(key, engine_association, 0, winreg.KEY_READ)
                    with subkey:
                        val, _ = winreg.QueryValueEx(subkey, "InstalledDirectory")
                        return val
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
        except OSError:
            pass

        try:
            key_path = r"Software\Epic Games\Unreal Engine\Builds"
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ
            ) as key:
                try:
                    subkey = winreg.OpenKey(key, engine_association, 0, winreg.KEY_READ)
                    with subkey:
                        val, _ = winreg.QueryValueEx(subkey, "")
                        return val
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
        except OSError:
            pass

        return None

    VALID_BUILD_CONFIGS = ("Development", "DebugGame", "Debug")

    _PLATFORM_TAG = {
        "win32": "Win64",
        "darwin": "Mac",
        "linux": "Linux",
    }

    @staticmethod
    def derive_paths(engine_root: str) -> dict[str, str]:
        root = Path(engine_root)
        return {
            "ubt_exe": str(
                root / "Engine/Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.exe"
            ),
            "uat_bat": str(root / "Engine/Build/BatchFiles/RunUAT.bat"),
            "editor_exe": str(root / "Engine/Binaries/Win64/UnrealEditor.exe"),
            "build_bat": str(root / "Engine/Build/BatchFiles/Build.bat"),
        }

    @staticmethod
    def editor_exe_for_config(
        engine_root: str, build_config: str = "Development"
    ) -> str:
        """Return the editor executable path for a given build configuration.

        Naming convention (Win64 example):
          Development  → UnrealEditor.exe
          DebugGame    → UnrealEditor-Win64-DebugGame.exe
          Debug        → UnrealEditor-Win64-Debug.exe
        """
        if build_config not in UEPathResolver.VALID_BUILD_CONFIGS:
            raise ValueError(
                f"Invalid build_config '{build_config}'. "
                f"Must be one of: {', '.join(UEPathResolver.VALID_BUILD_CONFIGS)}"
            )

        root = Path(engine_root)
        tag = UEPathResolver._PLATFORM_TAG.get(sys.platform, "Linux")
        ext = ".exe" if sys.platform == "win32" else ""
        bin_dir = root / "Engine" / "Binaries" / tag

        if build_config == "Development":
            return str(bin_dir / f"UnrealEditor{ext}")
        return str(bin_dir / f"UnrealEditor-{tag}-{build_config}{ext}")

    @staticmethod
    def validate_paths(paths: ResolvedPaths) -> list[str]:
        missing: list[str] = []
        checks = [
            (paths.uproject_path, "uproject file"),
            (paths.engine_root, "engine root"),
            (paths.ubt_exe, "UnrealBuildTool"),
            (paths.uat_bat, "RunUAT.bat"),
            (paths.editor_exe, "UnrealEditor"),
            (paths.build_bat, "Build.bat"),
        ]
        for p, desc in checks:
            if not Path(p).exists():
                missing.append(f"{desc}: {p}")
        return missing

    @staticmethod
    def get_ubt_log_path() -> str:
        base = os.environ.get("LOCALAPPDATA", "")
        if not base:
            base = Path.home() / "AppData" / "Local"
        else:
            base = Path(base)
        return str(base / "UnrealBuildTool" / "Log.txt")

    @staticmethod
    def get_ubt_log_json_path() -> str:
        base = os.environ.get("LOCALAPPDATA", "")
        if not base:
            base = Path.home() / "AppData" / "Local"
        else:
            base = Path(base)
        return str(base / "UnrealBuildTool" / "Log.json")
