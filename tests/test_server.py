from unittest.mock import patch, MagicMock

import pytest

from unrealhub.config import ProjectConfig
from unrealhub.state import StateStore
from unrealhub.ue_client import UEMCPClient


class TestGetClient:
    def test_returns_none_no_active(self, tmp_home):
        from unrealhub import server
        old_state = server._state
        old_clients = server._clients.copy()
        try:
            server._state = StateStore()
            server._clients.clear()

            result = server.get_client(None)
            assert result is None
        finally:
            server._state = old_state
            server._clients = old_clients

    def test_returns_none_offline(self, tmp_home):
        from unrealhub import server
        old_state = server._state
        old_clients = server._clients.copy()
        try:
            store = StateStore()
            store.upsert(port=8422, project_path="G:/Proj/A.uproject", status="offline")
            server._state = store
            server._clients.clear()

            client = server.get_client(None)
            assert client is None
        finally:
            server._state = old_state
            server._clients = old_clients

    def test_returns_client_online(self, tmp_home):
        from unrealhub import server
        old_state = server._state
        old_clients = server._clients.copy()
        try:
            store = StateStore()
            store.upsert(
                port=8422, project_path="G:/Proj/A.uproject", pid=1234
            )
            server._state = store
            server._clients.clear()

            client = server.get_client(None)
            assert client is not None
            assert isinstance(client, UEMCPClient)
            assert client.url == "http://localhost:8422/mcp"
        finally:
            server._state = old_state
            server._clients = old_clients

    def test_returns_cached_client(self, tmp_home):
        from unrealhub import server
        old_state = server._state
        old_clients = server._clients.copy()
        try:
            store = StateStore()
            store.upsert(
                port=8422, project_path="G:/Proj/A.uproject", pid=1234
            )
            server._state = store
            server._clients.clear()

            c1 = server.get_client(None)
            c2 = server.get_client(None)
            assert c1 is c2
        finally:
            server._state = old_state
            server._clients = old_clients

    def test_returns_none_unknown_id(self, tmp_home):
        from unrealhub import server
        old_state = server._state
        old_config = server._config
        old_clients = server._clients.copy()
        try:
            server._state = StateStore()
            server._config = ProjectConfig()
            server._clients.clear()

            assert server.get_client("ue999") is None
        finally:
            server._state = old_state
            server._config = old_config
            server._clients = old_clients

    def test_prefers_active_project_when_same_port_was_reused(self, tmp_home):
        from unrealhub import server
        old_state = server._state
        old_config = server._config
        old_clients = server._clients.copy()
        try:
            store = StateStore()
            store.upsert(
                port=8422,
                project_path="G:/Proj/OldProject.uproject",
                url="http://127.0.0.1:8422/mcp",
                pid=1234,
                status="online",
            )

            config = ProjectConfig()
            config.save_project(
                "OldProject",
                "G:/Proj/OldProject.uproject",
                "G:/UE",
                port=8422,
            )
            config.save_project(
                "NewProject",
                "G:/Proj/NewProject.uproject",
                "G:/UE",
                port=8422,
            )
            assert config.set_active_project("NewProject")

            server._state = store
            server._config = config
            server._clients.clear()

            client = server.get_client(None)

            assert client is not None
            rebound = store.get_instance("NewProject:8422")
            assert rebound is not None
            assert rebound.status == "online"
            assert rebound.pid == 1234
            assert store.get_active_instance().key == "NewProject:8422"
            assert client.url == "http://127.0.0.1:8422/mcp"
        finally:
            server._state = old_state
            server._config = old_config
            server._clients = old_clients


class TestCreateHubMcp:
    def test_creates_mcp(self):
        from unrealhub.server import create_hub_mcp
        mcp = create_hub_mcp()
        assert mcp is not None
        assert mcp.name == "UnrealMCPHub"
