# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .process_watcher import find_matching_pids


def _update_record(path: Path, phase: str) -> None:
    try:
        data = json.loads(path.read_text())
        data["phase"] = phase
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError):
        pass


def _redirect_log(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(descriptor, 1)
    os.dup2(descriptor, 2)
    if descriptor > 2:
        os.close(descriptor)


def _exec(spec: dict[str, Any]) -> None:
    program = str(spec["program"])
    arguments = [str(item) for item in spec.get("arguments", [])]
    environment = {str(key): str(value) for key, value in spec["environment"].items()}
    os.chdir(str(spec["working_directory"]))
    print("$", " ".join([program, *arguments]), flush=True)
    os.execvpe(program, [program, *arguments], environment)


def _wait_and_exec(payload: dict[str, Any], record: Path) -> None:
    watch = payload["watch"]
    target = str(watch["target"])
    prefix = Path(watch["prefix"])
    baseline = {int(item) for item in watch.get("baseline", [])}
    print(f"Watching for {target} in {prefix}", flush=True)
    while True:
        launched = find_matching_pids(target, prefix) - baseline
        if launched:
            break
        time.sleep(0.25)
    delay = float(watch.get("delay_seconds", 0))
    print(
        f"Detected {target} (PID {min(launched)}); waiting {delay:g} seconds",
        flush=True,
    )
    if delay:
        time.sleep(delay)
    _update_record(record, "running")
    _exec(payload["launch_spec"])


def _steam_managed(payload: dict[str, Any], record: Path) -> int:
    spec = payload["launch_spec"]
    prefix = Path(payload["prefix"])
    baseline = _prefix_pids(prefix)
    environment = {str(key): str(value) for key, value in spec["environment"].items()}
    request = subprocess.Popen(
        [str(spec["program"]), *[str(item) for item in spec.get("arguments", [])]],
        cwd=str(spec["working_directory"]),
        env=environment,
    )
    tracked: set[int] = set()
    stopping = False

    def terminate(_signal, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, terminate)
    deadline = time.monotonic() + 90
    empty_since: float | None = None
    while True:
        current = _prefix_pids(prefix) - baseline
        tracked |= current
        if current:
            _update_record(record, "running")
            empty_since = None
        elif tracked:
            empty_since = empty_since or time.monotonic()
            if time.monotonic() - empty_since >= 2:
                return 0
        elif request.poll() is not None and time.monotonic() >= deadline:
            return request.returncode or 0
        if stopping:
            for pid in _prefix_pids(prefix) - baseline:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            time.sleep(2)
            for pid in _prefix_pids(prefix) - baseline:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return 0
        time.sleep(0.25)


def _prefix_pids(prefix: Path) -> set[int]:
    expected = os.fsencode(f"STEAM_COMPAT_DATA_PATH={prefix}")
    result: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if expected in (entry / "environ").read_bytes().split(b"\0"):
                result.add(int(entry.name))
        except OSError:
            continue
    return result


def run(spec_path: Path) -> int:
    payload = json.loads(spec_path.read_text())
    spec_path.unlink(missing_ok=True)
    record = Path(payload["record_path"])
    _redirect_log(Path(payload["log_path"]))
    if payload.get("watch"):
        _wait_and_exec(payload, record)
        return 0
    _update_record(record, "running")
    if payload.get("steam_managed"):
        return _steam_managed(payload, record)
    _exec(payload["launch_spec"])
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: python -m proton_launcher.session_worker SPEC.json", file=sys.stderr
        )
        return 2
    try:
        return run(Path(sys.argv[1]))
    except Exception as exc:
        print(f"Session worker failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
