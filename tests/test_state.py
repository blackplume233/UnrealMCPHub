import json
from pathlib import Path
from unittest.mock import patch

import pytest

from unrealhub.state import StateStore, InstanceState, Note, ToolCallRecord


class TestStateStore:
    def test_empty_state(self, tmp_home):
        store = StateStore()
        assert store.list_instances() == []
        assert store.get_active_instance() is None

    def test_register_instance(self, tmp_home):
        store = StateStore()
        inst = store.register_instance(
            url="http://localhost:8422/mcp", port=8422, pid=1234
        )
        assert inst.auto_id == "ue1"
        assert inst.port == 8422
        assert inst.pid == 1234
        assert inst.status == "online"
        assert store.get_active_instance().auto_id == "ue1"

    def test_register_multiple(self, tmp_home):
        store = StateStore()
        i1 = store.register_instance(url="http://localhost:8422/mcp", port=8422)
        i2 = store.register_instance(url="http://localhost:8423/mcp", port=8423)
        assert i1.auto_id == "ue1"
        assert i2.auto_id == "ue2"
        assert len(store.list_instances()) == 2
        assert store.get_active_instance().auto_id == "ue2"

    def test_unregister_instance(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        assert store.unregister_instance("ue1")
        assert store.list_instances() == []
        assert store.get_active_instance() is None

    def test_unregister_active_fallback(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        store.register_instance(url="http://localhost:8423/mcp", port=8423)
        store.set_active("ue1")
        store.unregister_instance("ue1")
        active = store.get_active_instance()
        assert active is not None
        assert active.auto_id == "ue2"

    def test_unregister_nonexistent(self, tmp_home):
        store = StateStore()
        assert not store.unregister_instance("ghost")

    def test_get_instance_by_id(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        inst = store.get_instance("ue1")
        assert inst is not None
        assert inst.port == 8422
        assert store.get_instance("ue999") is None

    def test_get_instance_by_alias(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        store.set_alias("ue1", "myeditor")
        inst = store.get_instance("myeditor")
        assert inst is not None
        assert inst.auto_id == "ue1"

    def test_set_active(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        store.register_instance(url="http://localhost:8423/mcp", port=8423)
        assert store.set_active("ue2")
        assert store.get_active_instance().auto_id == "ue2"
        assert not store.set_active("ghost")

    def test_set_alias(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        assert store.set_alias("ue1", "dev")
        inst = store.get_instance("ue1")
        assert inst.alias == "dev"
        assert store.set_alias("ue1", "")
        inst = store.get_instance("ue1")
        assert inst.alias is None
        assert not store.set_alias("ghost", "name")

    def test_update_status(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        store.update_status("ue1", "crashed", pid=5678)
        inst = store.get_instance("ue1")
        assert inst.status == "crashed"
        assert inst.pid == 5678

    def test_update_status_nonexistent(self, tmp_home):
        store = StateStore()
        store.update_status("ghost", "online")

    def test_record_health_check(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        store.record_health_check("ue1", True)
        inst = store.get_instance("ue1")
        assert inst.status == "online"
        store.record_health_check("ue1", False)
        inst = store.get_instance("ue1")
        assert inst.status == "offline"

    def test_increment_crash_count(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        store.increment_crash_count("ue1")
        inst = store.get_instance("ue1")
        assert inst.crash_count == 1
        assert inst.status == "crashed"
        store.increment_crash_count("ue1")
        inst = store.get_instance("ue1")
        assert inst.crash_count == 2

    def test_add_and_get_notes(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        store.add_note("ue1", "Test note")
        store.add_note("ue1", "Another note")
        notes = store.get_notes("ue1")
        assert len(notes) == 2
        assert notes[0].content == "Test note"
        assert notes[1].content == "Another note"

    def test_get_notes_nonexistent(self, tmp_home):
        store = StateStore()
        assert store.get_notes("ghost") == []

    def test_record_tool_call(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        store.record_tool_call("ue1", "test_tool", True, 42.5)
        store.record_tool_call("ue1", "other_tool", False, 100.0)
        history = store.get_call_history("ue1")
        assert len(history) == 2
        assert history[0].tool_name == "test_tool"
        assert history[0].success is True
        assert history[0].duration_ms == 42.5

    def test_get_call_history_with_limit(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        for i in range(10):
            store.record_tool_call("ue1", f"tool_{i}", True, 1.0)
        history = store.get_call_history("ue1", limit=3)
        assert len(history) == 3
        assert history[0].tool_name == "tool_7"

    def test_get_call_history_nonexistent(self, tmp_home):
        store = StateStore()
        assert store.get_call_history("ghost") == []

    def test_list_instances_summary(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        store.set_alias("ue1", "dev")
        summary = store.list_instances_summary()
        assert "dev (port 8422)" in summary
        assert "*" in summary

    def test_list_instances_summary_empty(self, tmp_home):
        store = StateStore()
        assert "(no instances)" in store.list_instances_summary()

    def test_persist_and_reload(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422, pid=999)
        store.add_note("ue1", "persisted note")
        store.save()

        store2 = StateStore()
        inst = store2.get_instance("ue1")
        assert inst is not None
        assert inst.pid == 999
        notes = store2.get_notes("ue1")
        assert len(notes) == 1

    def test_next_id_survives_reload(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        store.register_instance(url="http://localhost:8423/mcp", port=8423)
        store.save()

        store2 = StateStore()
        i3 = store2.register_instance(url="http://localhost:8424/mcp", port=8424)
        assert i3.auto_id == "ue3"

    def test_load_corrupted_state(self, tmp_home):
        (tmp_home / "state.json").write_text("BROKEN", encoding="utf-8")
        store = StateStore()
        assert store.list_instances() == []

    def test_register_without_pid_is_offline(self, tmp_home):
        store = StateStore()
        inst = store.register_instance(url="http://localhost:8422/mcp", port=8422)
        assert inst.status == "offline"

    def test_resolve_by_alias(self, tmp_home):
        store = StateStore()
        store.register_instance(url="http://localhost:8422/mcp", port=8422)
        store.set_alias("ue1", "game")
        assert store.set_active("game")
        assert store.get_active_instance().auto_id == "ue1"
