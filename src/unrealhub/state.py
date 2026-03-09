from pydantic import BaseModel
from pathlib import Path
from datetime import datetime
from typing import Callable, Literal
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

STATE_PATH = Path.home() / ".unrealhub" / "state.json"


class Note(BaseModel):
    timestamp: str
    content: str


class ToolCallRecord(BaseModel):
    timestamp: str
    tool_name: str
    success: bool
    duration_ms: float = 0


class ProcessMetrics(BaseModel):
    cpu_percent: float = 0
    memory_mb: float = 0
    last_updated: str = ""


class InstanceState(BaseModel):
    auto_id: str
    alias: str | None = None
    url: str
    port: int
    project_path: str = ""
    engine_root: str = ""
    pid: int | None = None
    status: Literal["online", "offline", "crashed", "starting"] = "offline"
    first_seen: str = ""
    last_seen: str = ""
    last_health_check: str = ""
    crash_count: int = 0
    notes: list[Note] = []
    call_history: list[ToolCallRecord] = []
    metrics: ProcessMetrics = ProcessMetrics()


def _normalize_path(p: str) -> str:
    """Normalize a filesystem path for reliable comparison."""
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(p))


class StateStore:
    def __init__(self):
        self._instances: dict[str, InstanceState] = {}
        self._active_instance_id: str = ""
        self._next_id: int = 1
        self._lock = threading.Lock()
        self._on_unregister_callbacks: list[Callable[[str], None]] = []
        self._load()

    def on_unregister(self, callback: Callable[[str], None]) -> None:
        """Register a callback invoked with the auto_id when an instance is removed."""
        self._on_unregister_callbacks.append(callback)

    def _resolve(self, identifier: str) -> str | None:
        with self._lock:
            if identifier in self._instances:
                return identifier
            for auto_id, inst in self._instances.items():
                if inst.alias == identifier:
                    return auto_id
            return None

    def _load(self) -> None:
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                self._instances = {
                    k: InstanceState.model_validate(v)
                    for k, v in data.get("instances", {}).items()
                }
                self._active_instance_id = data.get("active_instance_id", "")
                loaded_next = data.get("next_id", 1)
                max_id = 0
                for auto_id in self._instances:
                    if auto_id.startswith("ue") and auto_id[2:].isdigit():
                        max_id = max(max_id, int(auto_id[2:]))
                self._next_id = max(loaded_next, max_id + 1)
            except (json.JSONDecodeError, Exception):
                pass

    def save(self) -> None:
        with self._lock:
            data = {
                "instances": {k: v.model_dump() for k, v in self._instances.items()},
                "active_instance_id": self._active_instance_id,
                "next_id": self._next_id,
            }
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _pick_alive_active(self) -> str:
        """Return the auto_id of a living instance, or '' if none."""
        for auto_id, inst in self._instances.items():
            if inst.status in ("online", "starting"):
                return auto_id
        return ""

    def register_instance(
        self,
        url: str,
        port: int,
        project_path: str = "",
        engine_root: str = "",
        pid: int | None = None,
    ) -> InstanceState:
        now = datetime.now().isoformat()
        auto_id = f"ue{self._next_id}"
        self._next_id += 1
        instance = InstanceState(
            auto_id=auto_id,
            url=url,
            port=port,
            project_path=project_path,
            engine_root=engine_root,
            pid=pid,
            status="online" if pid else "offline",
            first_seen=now,
            last_seen=now,
            last_health_check=now,
        )
        with self._lock:
            self._instances[auto_id] = instance
            current = self._instances.get(self._active_instance_id)
            if not current or current.status not in ("online", "starting"):
                self._active_instance_id = auto_id
        self.save()
        return instance

    def unregister_instance(self, instance_id: str) -> bool:
        resolved = self._resolve(instance_id)
        if resolved is None:
            return False
        with self._lock:
            del self._instances[resolved]
            if self._active_instance_id == resolved:
                self._active_instance_id = (
                    next(iter(self._instances), "") if self._instances else ""
                )
        self.save()
        self._fire_unregister(resolved)
        return True

    def _fire_unregister(self, auto_id: str) -> None:
        for cb in self._on_unregister_callbacks:
            try:
                cb(auto_id)
            except Exception:
                logger.debug("on_unregister callback error for %s", auto_id, exc_info=True)

    def get_instance(self, identifier: str) -> InstanceState | None:
        resolved = self._resolve(identifier)
        if resolved is None:
            return None
        with self._lock:
            return self._instances.get(resolved)

    def get_active_instance(self) -> InstanceState | None:
        with self._lock:
            inst = self._instances.get(self._active_instance_id)
            if inst and inst.status in ("online", "starting"):
                return inst
            # Active is dead or missing — try to find a living one
            alive_id = self._pick_alive_active()
            if alive_id:
                self._active_instance_id = alive_id
                return self._instances[alive_id]
            return inst

    def set_active(self, identifier: str) -> bool:
        resolved = self._resolve(identifier)
        if resolved is None:
            return False
        with self._lock:
            self._active_instance_id = resolved
        self.save()
        return True

    def set_alias(self, identifier: str, alias: str) -> bool:
        resolved = self._resolve(identifier)
        if resolved is None:
            return False
        with self._lock:
            self._instances[resolved].alias = alias or None
        self.save()
        return True

    def list_instances(self) -> list[InstanceState]:
        with self._lock:
            return list(self._instances.values())

    def list_instances_summary(self) -> str:
        with self._lock:
            if not self._instances:
                return "  (no instances)"

            online = sum(1 for i in self._instances.values() if i.status == "online")
            lines = [f"Instances ({len(self._instances)} total, {online} online):"]

            for inst in self._instances.values():
                marker = "* " if inst.auto_id == self._active_instance_id else "  "
                name = inst.alias or inst.auto_id
                pid_str = f"PID={inst.pid}" if inst.pid else "PID=?"
                line = f"  {marker}{name} (port {inst.port}) {inst.status.upper()}  {pid_str}"
                lines.append(line)

                if inst.project_path:
                    lines.append(f"      Project: {inst.project_path}")
                if inst.last_seen:
                    extra = f" (crashed x{inst.crash_count})" if inst.crash_count else ""
                    lines.append(f"      Last seen: {inst.last_seen}{extra}")

            return "\n".join(lines)

    # ------------------------------------------------------------------
    # Project-directory-based lookup & lifecycle
    # ------------------------------------------------------------------

    def find_by_project_path(self, project_path: str) -> list[InstanceState]:
        """Return all instances whose project_path matches (path-normalized)."""
        norm = _normalize_path(project_path)
        if not norm:
            return []
        with self._lock:
            return [
                inst
                for inst in self._instances.values()
                if _normalize_path(inst.project_path) == norm
            ]

    def find_by_port(self, port: int) -> list[InstanceState]:
        """Return all instances registered on *port*."""
        with self._lock:
            return [inst for inst in self._instances.values() if inst.port == port]

    def reactivate_instance(
        self,
        auto_id: str,
        *,
        url: str | None = None,
        port: int | None = None,
        project_path: str | None = None,
        engine_root: str | None = None,
        pid: int | None = None,
    ) -> InstanceState | None:
        """Bring an existing instance back online, updating mutable fields."""
        resolved = self._resolve(auto_id)
        if resolved is None:
            return None
        now = datetime.now().isoformat()
        with self._lock:
            inst = self._instances[resolved]
            inst.status = "online"
            inst.last_seen = now
            inst.last_health_check = now
            if url is not None:
                inst.url = url
            if port is not None:
                inst.port = port
            if project_path is not None and project_path:
                inst.project_path = project_path
            if engine_root is not None and engine_root:
                inst.engine_root = engine_root
            if pid is not None:
                inst.pid = pid
        self.save()
        return self.get_instance(resolved)

    def purge_dead_instances(
        self,
        *,
        project_path: str = "",
        port: int = 0,
        exclude_id: str = "",
        max_age_hours: float = 0,
    ) -> list[str]:
        """Remove crashed/offline instances matching the given criteria.

        Matching is OR-based: an instance is a candidate if its project_path
        matches OR its port matches.  If neither *project_path* nor *port*
        is provided but *max_age_hours* > 0, all stale instances are candidates.

        Returns the list of removed auto_ids.
        """
        norm = _normalize_path(project_path)
        now = datetime.now()
        removed: list[str] = []

        with self._lock:
            to_remove: list[str] = []
            for auto_id, inst in self._instances.items():
                if auto_id == exclude_id:
                    continue
                if inst.status in ("online", "starting"):
                    continue

                match = False
                if norm and _normalize_path(inst.project_path) == norm:
                    match = True
                if port and inst.port == port:
                    match = True

                if not match and max_age_hours > 0 and inst.last_seen:
                    try:
                        last = datetime.fromisoformat(inst.last_seen)
                        if (now - last).total_seconds() > max_age_hours * 3600:
                            match = True
                    except ValueError:
                        pass

                if not match and not norm and not port and max_age_hours <= 0:
                    continue
                if match:
                    to_remove.append(auto_id)

            for auto_id in to_remove:
                del self._instances[auto_id]
                if self._active_instance_id == auto_id:
                    self._active_instance_id = (
                        next(iter(self._instances), "") if self._instances else ""
                    )
                removed.append(auto_id)

        if removed:
            self.save()
            for rid in removed:
                self._fire_unregister(rid)
            logger.info("Purged dead instances: %s", removed)

        return removed

    def dedup_dead_by_project(self) -> list[str]:
        """Consolidate dead instances per project_path.

        For each project with multiple dead (crashed/offline) instances,
        keep only the most recently seen one and remove the rest.
        Alive (online/starting) instances are never touched.

        Returns the list of removed auto_ids.
        """
        from collections import defaultdict

        dead_groups: dict[str, list[str]] = defaultdict(list)
        with self._lock:
            for auto_id, inst in self._instances.items():
                if inst.status in ("online", "starting"):
                    continue
                norm = _normalize_path(inst.project_path)
                if not norm:
                    continue
                dead_groups[norm].append(auto_id)

            removed: list[str] = []
            for _norm_path, ids in dead_groups.items():
                if len(ids) <= 1:
                    continue

                ids.sort(key=lambda aid: self._instances[aid].last_seen or "", reverse=True)

                for aid in ids[1:]:
                    del self._instances[aid]
                    if self._active_instance_id == aid:
                        self._active_instance_id = (
                            next(iter(self._instances), "") if self._instances else ""
                        )
                    removed.append(aid)

            if self._active_instance_id not in self._instances and self._instances:
                self._active_instance_id = next(iter(self._instances))

        if removed:
            self.save()
            for rid in removed:
                self._fire_unregister(rid)
            logger.info("Dedup removed dead instances: %s", removed)

        return removed

    # ------------------------------------------------------------------

    def update_status(
        self,
        instance_id: str,
        status: Literal["online", "offline", "crashed", "starting"],
        pid: int | None = None,
    ) -> None:
        resolved = self._resolve(instance_id)
        if resolved is None:
            return
        with self._lock:
            inst = self._instances[resolved]
            inst.status = status
            inst.last_seen = datetime.now().isoformat()
            if pid is not None:
                inst.pid = pid
            if status in ("offline", "crashed") and self._active_instance_id == resolved:
                self._active_instance_id = self._pick_alive_active()
        self.save()

    def record_health_check(self, instance_id: str, healthy: bool) -> None:
        resolved = self._resolve(instance_id)
        if resolved is None:
            return
        with self._lock:
            inst = self._instances[resolved]
            inst.last_health_check = datetime.now().isoformat()
            inst.status = "online" if healthy else "offline"
        self.save()

    def increment_crash_count(self, instance_id: str) -> None:
        resolved = self._resolve(instance_id)
        if resolved is None:
            return
        with self._lock:
            self._instances[resolved].crash_count += 1
            self._instances[resolved].status = "crashed"
        self.save()

    def add_note(self, instance_id: str, content: str) -> None:
        resolved = self._resolve(instance_id)
        if resolved is None:
            return
        note = Note(
            timestamp=datetime.now().isoformat(),
            content=content,
        )
        with self._lock:
            self._instances[resolved].notes.append(note)
        self.save()

    def get_notes(self, instance_id: str) -> list[Note]:
        resolved = self._resolve(instance_id)
        if resolved is None:
            return []
        with self._lock:
            return list(self._instances[resolved].notes)

    def record_tool_call(
        self,
        instance_id: str,
        tool_name: str,
        success: bool,
        duration_ms: float = 0,
    ) -> None:
        resolved = self._resolve(instance_id)
        if resolved is None:
            return
        record = ToolCallRecord(
            timestamp=datetime.now().isoformat(),
            tool_name=tool_name,
            success=success,
            duration_ms=duration_ms,
        )
        with self._lock:
            self._instances[resolved].call_history.append(record)
        self.save()

    def get_call_history(
        self, instance_id: str, limit: int = 50
    ) -> list[ToolCallRecord]:
        resolved = self._resolve(instance_id)
        if resolved is None:
            return []
        with self._lock:
            history = self._instances[resolved].call_history
            return list(history[-limit:])
