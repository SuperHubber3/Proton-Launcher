# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import os
import shlex
from pathlib import Path


def executable_name(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].casefold()


def primary_executable_name(mode: str, executable: str, command: str) -> str:
    if mode == "executable":
        raw = executable.strip().strip("\"'")
        return raw.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    parts = shlex.split(command)
    if parts and parts[0].casefold() in {"wine", "wine64"}:
        parts = parts[1:]
    if not parts:
        return ""
    return parts[0].replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def command_matches(cmdline: bytes, comm: str, target: str) -> bool:
    wanted = executable_name(target)
    if not wanted:
        return False
    if executable_name(comm.strip()) == wanted:
        return True
    return any(
        executable_name(part.decode(errors="replace")) == wanted
        for part in cmdline.split(b"\0")
        if part
    )


def find_matching_pids(
    target: str,
    prefix: Path | None,
    proc_root: Path = Path("/proc"),
    session_id: str = "",
) -> set[int]:
    result: set[int] = set()
    expected = (
        os.fsencode(f"STEAM_COMPAT_DATA_PATH={prefix}") if prefix is not None else None
    )
    expected_session = (
        os.fsencode(f"PROTON_LAUNCHER_SESSION_ID={session_id}") if session_id else None
    )
    if prefix is None and expected_session is None:
        raise ValueError("Process matching requires a prefix or session ID")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environment = (entry / "environ").read_bytes().split(b"\0")
            if expected is not None and expected not in environment:
                continue
            if expected_session is not None and expected_session not in environment:
                continue
            cmdline = (entry / "cmdline").read_bytes()
            comm = (entry / "comm").read_text(errors="replace")
            if command_matches(cmdline, comm, target):
                result.add(int(entry.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
    return result
