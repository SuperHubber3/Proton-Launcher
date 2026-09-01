# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .models import LaunchSpec


class SessionKind(str, Enum):
    PRIMARY = "primary"
    FOLLOWUP = "followup"
    WEMOD = "wemod"


@dataclass(slots=True)
class SessionRecord:
    id: str
    kind: str
    game_key: str
    game_name: str
    prefix: str
    backend: str
    unit: str = ""
    pid: int = 0
    start_ticks: int = 0
    phase: str = "starting"
    log_path: str = ""
    parent_id: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SessionRecord:
        return cls(
            **{
                key: value.get(key, field.default)
                for key, field in cls.__dataclass_fields__.items()
            }
        )


def state_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "proton-launcher"


def runtime_root() -> Path:
    base = Path(
        os.environ.get("XDG_RUNTIME_DIR", f"/tmp/proton-launcher-{os.getuid()}")
    )
    return base / "proton-launcher"


def merge_record_fields(path: Path, updates: dict[str, Any]) -> None:
    """Merge fields into a record file under an exclusive lock.

    Both the manager and the session worker update the same record file from
    different processes; whole-file rewrites would let one side erase the
    other's fields.
    """
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        data.update(updates)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def process_start_ticks(pid: int) -> int:
    try:
        # The comm field can contain spaces and parentheses. Everything after
        # its final ')' begins with state; starttime is field 22 overall.
        tail = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(tail[19])
    except (OSError, ValueError, IndexError):
        return 0


class SessionManager:
    def __init__(self):
        self.root = state_root()
        self.records_dir = self.root / "sessions"
        self.logs_dir = self.root / "logs"
        self.runtime_dir = runtime_root()
        for directory in (self.records_dir, self.logs_dir, self.runtime_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._children: dict[str, subprocess.Popen] = {}
        self._finished_records: set[Path] = set()
        self.systemd_available = self._detect_systemd()
        self.cleanup_old_records()

    @property
    def backend_name(self) -> str:
        return (
            "systemd user services"
            if self.systemd_available
            else "process-group fallback"
        )

    @staticmethod
    def _detect_systemd() -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-system-running"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
            return result.stdout.strip() in {"running", "degraded"}
        except (OSError, subprocess.TimeoutExpired):
            return False

    def start(
        self,
        kind: SessionKind,
        spec: LaunchSpec,
        game_key: str,
        game_name: str,
        prefix: Path,
        *,
        watch_target: str = "",
        watch_baseline: set[int] | None = None,
        delay_seconds: float = 0.0,
        watch_any_prefix: bool = False,
        watch_session_id: str = "",
        steam_managed: bool = False,
    ) -> SessionRecord:
        if watch_target and watch_any_prefix and not watch_session_id:
            raise ValueError("Prefix-free process watching requires a session ID")
        session_id = uuid.uuid4().hex
        unit = f"proton-launcher-{kind.value}-{session_id}.service"
        log_path = self.logs_dir / f"{session_id}.log"
        backend = "systemd" if self.systemd_available else "process-group"
        record = SessionRecord(
            session_id,
            kind.value,
            game_key,
            game_name,
            str(prefix),
            backend,
            unit=unit if self.systemd_available else "",
            log_path=str(log_path),
            phase="waiting" if watch_target else "starting",
        )
        record_path = self._record_path(session_id)
        self._write_record(record)
        payload = {
            "record_path": str(record_path),
            "log_path": str(log_path),
            "launch_spec": {
                "program": spec.program,
                "arguments": spec.arguments,
                "environment": spec.environment,
                "working_directory": spec.working_directory,
            },
            "watch": (
                {
                    "target": watch_target,
                    "prefix": "" if watch_any_prefix else str(prefix),
                    "session_id": watch_session_id,
                    "baseline": sorted(watch_baseline or set()),
                    "delay_seconds": delay_seconds,
                }
                if watch_target
                else None
            ),
            "steam_managed": steam_managed,
            "prefix": str(prefix),
        }
        spec_path = self.runtime_dir / f"{session_id}.json"
        self._private_json(spec_path, payload)
        worker = [
            sys.executable,
            "-m",
            "proton_launcher.session_worker",
            str(spec_path),
        ]
        try:
            if self.systemd_available:
                project_root = Path(__file__).resolve().parent.parent
                command = [
                    "systemd-run",
                    "--user",
                    "--quiet",
                    "--collect",
                    f"--unit={unit}",
                    "--property=KillMode=control-group",
                    "--property=TimeoutStopSec=5s",
                    f"--working-directory={project_root}",
                    *worker,
                ]
                try:
                    result = subprocess.run(
                        command, capture_output=True, text=True, check=False, timeout=8
                    )
                except subprocess.TimeoutExpired as error:
                    # The unit may have been created anyway; stop it so a game
                    # the manager no longer tracks is not left running.
                    subprocess.run(
                        ["systemctl", "--user", "--no-block", "stop", unit],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                    raise OSError(f"systemd-run did not respond: {error}") from error
                if result.returncode:
                    raise OSError(result.stderr.strip() or result.stdout.strip())
            else:
                process = subprocess.Popen(
                    worker,
                    cwd=Path(__file__).resolve().parent.parent,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                record.pid = process.pid
                record.start_ticks = process_start_ticks(process.pid)
                self._children[record.id] = process
                merge_record_fields(
                    record_path,
                    {"pid": record.pid, "start_ticks": record.start_ticks},
                )
        except Exception:
            spec_path.unlink(missing_ok=True)
            record_path.unlink(missing_ok=True)
            raise
        return record

    ACTIVE_UNIT_STATES = frozenset(
        {"active", "activating", "deactivating", "reloading"}
    )

    def active(self) -> list[SessionRecord]:
        pending: list[tuple[Path, SessionRecord]] = []
        for path in self.records_dir.glob("*.json"):
            if path in self._finished_records:
                continue
            try:
                record = SessionRecord.from_dict(json.loads(path.read_text()))
            except (OSError, ValueError, TypeError):
                continue
            if record.phase == "finished":
                self._finished_records.add(path)
                continue
            pending.append((path, record))
        # One batched systemctl query per poll; per-record calls block the UI
        # thread for up to two seconds each when the user manager is slow.
        unit_states = self._unit_states(
            [
                record.unit
                for _, record in pending
                if record.backend == "systemd" and record.unit
            ]
        )
        result: list[SessionRecord] = []
        for path, record in pending:
            if record.backend == "systemd" and record.unit:
                alive = unit_states.get(record.unit, "") in self.ACTIVE_UNIT_STATES
            else:
                alive = self._local_process_alive(record)
            if alive:
                result.append(record)
            else:
                record.phase = "finished"
                self._write_record(record)
                self._finished_records.add(path)
        self._cancel_orphaned_followups(result)
        return sorted(result, key=lambda item: item.id)

    def _cancel_orphaned_followups(self, active_records: list[SessionRecord]) -> None:
        """Stop waiting follow-ups whose primary session already finished."""
        active_ids = {record.id for record in active_records}
        for record in active_records:
            if (
                record.kind == SessionKind.FOLLOWUP.value
                and record.phase == "waiting"
                and record.parent_id
                and record.parent_id not in active_ids
            ):
                self._append_log(
                    record,
                    "Stopping the follow-up: the game session ended before "
                    "the watched program appeared",
                )
                self.stop(record)
                record.phase = "stopping"

    @staticmethod
    def _append_log(record: SessionRecord, message: str) -> None:
        if not record.log_path:
            return
        try:
            with open(record.log_path, "a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        except OSError:
            pass

    @staticmethod
    def _unit_states(units: list[str]) -> dict[str, str]:
        if not units:
            return {}
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show", "--property=Id,ActiveState", *units],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        states: dict[str, str] = {}
        unit_id = ""
        for line in result.stdout.splitlines():
            if line.startswith("Id="):
                unit_id = line.removeprefix("Id=").strip()
            elif line.startswith("ActiveState=") and unit_id:
                states[unit_id] = line.removeprefix("ActiveState=").strip()
                unit_id = ""
        return states

    def is_active(self, record: SessionRecord) -> bool:
        if record.backend == "systemd" and record.unit:
            states = self._unit_states([record.unit])
            return states.get(record.unit, "") in self.ACTIVE_UNIT_STATES
        return self._local_process_alive(record)

    def _local_process_alive(self, record: SessionRecord) -> bool:
        child = self._children.get(record.id)
        if child is not None:
            if child.poll() is None:
                return True
            self._children.pop(record.id, None)
            return False
        return bool(
            record.pid
            and record.start_ticks
            and process_start_ticks(record.pid) == record.start_ticks
        )

    def stop(self, record: SessionRecord) -> None:
        child = self._children.get(record.id)
        if child is not None and child.poll() is not None:
            self._children.pop(record.id, None)
            record.phase = "finished"
            self._write_record(record)
            return
        if record.backend == "systemd" and record.unit:
            try:
                subprocess.run(
                    ["systemctl", "--user", "--no-block", "stop", record.unit],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=2,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif record.pid and process_start_ticks(record.pid) == record.start_ticks:
            try:
                os.killpg(record.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            threading.Thread(
                target=self._force_fallback_after_timeout,
                args=(record.pid, record.start_ticks),
                daemon=True,
            ).start()
        record.phase = "stopping"
        merge_record_fields(self._record_path(record.id), {"phase": "stopping"})

    @staticmethod
    def _force_fallback_after_timeout(pid: int, start_ticks: int) -> None:
        time.sleep(5)
        if process_start_ticks(pid) != start_ticks:
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def link_parent(self, record: SessionRecord, parent_id: str) -> None:
        """Tie a follow-up to its primary session for automatic cancellation."""
        record.parent_id = parent_id
        merge_record_fields(self._record_path(record.id), {"parent_id": parent_id})

    def records(self, kind: SessionKind | None = None) -> list[SessionRecord]:
        records = self.active()
        return [item for item in records if kind is None or item.kind == kind.value]

    def stop_kind(self, kind: SessionKind) -> None:
        for record in self.records(kind):
            self.stop(record)

    def stop_all(self) -> None:
        for record in self.active():
            self.stop(record)

    def log_text(self, record: SessionRecord, max_bytes: int = 256_000) -> str:
        try:
            with Path(record.log_path).open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - max_bytes))
                return handle.read().decode(errors="replace")
        except OSError:
            return ""

    def cleanup_old_records(self, max_age_days: int = 30) -> None:
        cutoff = time.time() - max_age_days * 86400
        for path in self.records_dir.glob("*.json"):
            try:
                record = SessionRecord.from_dict(json.loads(path.read_text()))
                if path.stat().st_mtime >= cutoff:
                    if record.phase == "finished":
                        self._finished_records.add(path)
                    continue
                if record.phase != "finished" and self.is_active(record):
                    continue
                path.unlink()
                Path(record.log_path).unlink(missing_ok=True)
            except (OSError, ValueError, TypeError):
                continue

    def _record_path(self, session_id: str) -> Path:
        return self.records_dir / f"{session_id}.json"

    def _write_record(self, record: SessionRecord) -> None:
        self._private_json(self._record_path(record.id), asdict(record))

    @staticmethod
    def _private_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
