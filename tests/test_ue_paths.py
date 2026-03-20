import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from unrealhub.utils.ue_paths import UEPathResolver, ResolvedPaths


class TestParseEngineAssociation:
    def test_valid_uproject(self, fake_project):
        assoc = UEPathResolver.parse_engine_association(str(fake_project))
        assert assoc == "5.5"

    def test_missing_association(self, tmp_path):
        uproject = tmp_path / "NoAssoc.uproject"
        uproject.write_text(json.dumps({"FileVersion": 3}), encoding="utf-8")
        assoc = UEPathResolver.parse_engine_association(str(uproject))
        assert assoc == ""

    def test_nonexistent_file(self):
        with pytest.raises(ValueError, match="not found"):
            UEPathResolver.parse_engine_association("/nonexistent/file.uproject")

    def test_invalid_json(self, tmp_path):
        uproject = tmp_path / "Bad.uproject"
        uproject.write_text("NOT JSON", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid"):
            UEPathResolver.parse_engine_association(str(uproject))


class TestDerivePaths:
    def test_derive_paths(self):
        paths = UEPathResolver.derive_paths("/engine")
        assert "UnrealBuildTool" in paths["ubt_exe"]
        assert "RunUAT" in paths["uat_bat"]
        assert "UnrealEditor" in paths["editor_exe"]
        assert "Build.bat" in paths["build_bat"]


class TestResolveFromUproject:
    def test_valid_project(self, fake_project, fake_engine):
        paths = UEPathResolver.resolve_from_uproject(
            str(fake_project), str(fake_engine)
        )
        assert paths.project_name == "TestProject"
        assert paths.engine_root == str(fake_engine.resolve())
        assert "UnrealEditor" in paths.editor_exe

    def test_nonexistent_project(self, fake_engine):
        with pytest.raises(ValueError, match="not found"):
            UEPathResolver.resolve_from_uproject("/bad/path.uproject", str(fake_engine))

    def test_wrong_extension(self, tmp_path, fake_engine):
        txt = tmp_path / "test.txt"
        txt.write_text("hello")
        with pytest.raises(ValueError, match="Not a .uproject"):
            UEPathResolver.resolve_from_uproject(str(txt), str(fake_engine))

    def test_has_project_modules(self, tmp_path):
        with_modules = tmp_path / "WithModules.uproject"
        with_modules.write_text(
            json.dumps({"EngineAssociation": "5.7", "Modules": [{"Name": "Game"}]}),
            encoding="utf-8",
        )
        no_modules = tmp_path / "NoModules.uproject"
        no_modules.write_text(
            json.dumps({"EngineAssociation": "5.7", "Modules": []}),
            encoding="utf-8",
        )

        assert UEPathResolver.has_project_modules(str(with_modules)) is True
        assert UEPathResolver.has_project_modules(str(no_modules)) is False

    def test_get_editor_build_target(self, tmp_path):
        with_modules = tmp_path / "WithModules.uproject"
        with_modules.write_text(
            json.dumps({"EngineAssociation": "5.7", "Modules": [{"Name": "Game"}]}),
            encoding="utf-8",
        )
        no_modules = tmp_path / "NoModules.uproject"
        no_modules.write_text(
            json.dumps({"EngineAssociation": "5.7", "Modules": []}),
            encoding="utf-8",
        )

        assert UEPathResolver.get_editor_build_target(str(with_modules), "WithModules") == "WithModulesEditor"
        assert UEPathResolver.get_editor_build_target(str(no_modules), "NoModules") == "UnrealEditor"


class TestValidatePaths:
    def test_all_valid(self, fake_project, fake_engine):
        paths = UEPathResolver.resolve_from_uproject(
            str(fake_project), str(fake_engine)
        )
        missing = UEPathResolver.validate_paths(paths)
        assert missing == []

    def test_missing_paths(self):
        paths = ResolvedPaths(
            uproject_path="/nonexistent/test.uproject",
            project_dir="/nonexistent",
            project_name="test",
            engine_root="/nonexistent/engine",
            engine_association="5.5",
            ubt_exe="/nonexistent/ubt.exe",
            uat_bat="/nonexistent/uat.bat",
            editor_exe="/nonexistent/editor.exe",
            build_bat="/nonexistent/build.bat",
        )
        missing = UEPathResolver.validate_paths(paths)
        assert len(missing) == 6


class TestGetUbtLogPaths:
    def test_get_ubt_log_path(self):
        path = UEPathResolver.get_ubt_log_path()
        assert "UnrealBuildTool" in path
        assert path.endswith("Log.txt")

    def test_get_ubt_log_json_path(self):
        path = UEPathResolver.get_ubt_log_json_path()
        assert "UnrealBuildTool" in path
        assert path.endswith("Log.json")

    def test_fallback_without_localappdata(self):
        env = dict(os.environ)
        env.pop("LOCALAPPDATA", None)
        with patch.dict("os.environ", env, clear=True):
            path = UEPathResolver.get_ubt_log_path()
            assert "Log.txt" in path


class TestResolveEngineFromRegistry:
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_returns_none_for_empty(self):
        result = UEPathResolver.resolve_engine_from_registry("")
        assert result is None

    @pytest.mark.skipif(sys.platform == "win32", reason="Non-Windows")
    def test_returns_none_on_non_windows(self):
        result = UEPathResolver.resolve_engine_from_registry("5.5")
        assert result is None
