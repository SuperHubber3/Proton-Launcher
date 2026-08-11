# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

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
        steam_managed: bool = False,
    ) -> SessionRecord:
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
                    "prefix": str(prefix),
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
                result = subprocess.run(
                    command, capture_output=True, text=True, check=False, timeout=8
                )
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
                self._write_record(record)
        except Exception:
            spec_path.unlink(missing_ok=True)
            record_path.unlink(missing_ok=True)
            raise
        return record

    def active(self) -> list[SessionRecord]:
        result: list[SessionRecord] = []
        for path in self.records_dir.glob("*.json"):
            try:
                record = SessionRecord.from_dict(json.loads(path.read_text()))
            except (OSError, ValueError, TypeError):
                continue
            if self.is_active(record):
                result.append(record)
            elif record.phase != "finished":
                record.phase = "finished"
                self._write_record(record)
        return sorted(result, key=lambda item: item.id)

    def is_active(self, record: SessionRecord) -> bool:
        if record.backend == "systemd" and record.unit:
            try:
                result = subprocess.run(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        "--property=ActiveState",
                        "--value",
                        record.unit,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=2,
                )
                return result.stdout.strip() in {"active", "activating", "reloading"}
            except (OSError, subprocess.TimeoutExpired):
                return False
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
            subprocess.run(
                ["systemctl", "--user", "stop", record.unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
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
        self._write_record(record)

    @staticmethod
    def _force_fallback_after_timeout(pid: int, start_ticks: int) -> None:
        time.sleep(5)
        if process_start_ticks(pid) != start_ticks:
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

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
                if path.stat().st_mtime >= cutoff or self.is_active(record):
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
