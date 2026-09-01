# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import vdf

from .vdf_edit import atomic_write_group


@dataclass(frozen=True, slots=True)
class SteamAccount:
    steam_id: str
    account_name: str
    persona_name: str
    remember_password: bool

    @property
    def label(self) -> str:
        if self.persona_name and self.persona_name != self.account_name:
            return f"{self.persona_name} ({self.account_name})"
        return self.persona_name or self.account_name or self.steam_id


@dataclass(frozen=True, slots=True)
class SteamAccountState:
    accounts: tuple[SteamAccount, ...]
    current_steam_id: str | None
    shader_cache_disabled: bool


def default_registry_path() -> Path:
    return Path.home() / ".steam" / "registry.vdf"


def _load_vdf(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            document = vdf.load(handle)
    except (OSError, ValueError, SyntaxError, AttributeError, TypeError) as error:
        raise ValueError(f"Could not read {path}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"Steam configuration is not an object: {path}")
    return document


def _nested(document: dict[str, Any], *keys: str) -> Any:
    value: Any = document
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def load_account_state(
    steam_root: Path, registry_path: Path | None = None
) -> SteamAccountState:
    loginusers_path = steam_root / "config" / "loginusers.vdf"
    config_path = steam_root / "config" / "config.vdf"
    registry_path = registry_path or default_registry_path()
    users = _nested(_load_vdf(loginusers_path), "users")
    if not isinstance(users, dict):
        raise ValueError(f"No saved Steam accounts were found in {loginusers_path}")

    accounts: list[SteamAccount] = []
    auto_login_ids: list[str] = []
    most_recent_ids: list[str] = []
    for steam_id, value in users.items():
        if not isinstance(value, dict):
            continue
        account = SteamAccount(
            steam_id=str(steam_id),
            account_name=str(value.get("AccountName", "")),
            persona_name=str(value.get("PersonaName", "")),
            remember_password=str(value.get("RememberPassword", "0")) == "1",
        )
        if not account.account_name:
            continue
        accounts.append(account)
        if str(value.get("AutoLogin", "0")) == "1":
            auto_login_ids.append(account.steam_id)
        if str(value.get("MostRecent", "0")) == "1":
            most_recent_ids.append(account.steam_id)
    if not accounts:
        raise ValueError(f"No saved Steam accounts were found in {loginusers_path}")
    marked_ids = auto_login_ids or most_recent_ids

    registry = _load_vdf(registry_path)
    auto_login_user = str(
        _nested(
            registry, "Registry", "HKCU", "Software", "Valve", "Steam", "AutoLoginUser"
        )
        or ""
    ).casefold()
    current = next(
        (
            account.steam_id
            for account in accounts
            if account.account_name.casefold() == auto_login_user
        ),
        marked_ids[0] if len(marked_ids) == 1 else None,
    )
    config = _load_vdf(config_path)
    disabled = (
        str(
            _nested(
                config,
                "InstallConfigStore",
                "Software",
                "Valve",
                "Steam",
                "ShaderCacheManager",
                "DisableShaderCache",
            )
            or "0"
        )
        == "1"
    )
    return SteamAccountState(tuple(accounts), current, disabled)


def switch_account(
    steam_root: Path,
    steam_id: str,
    disable_shader_cache: bool,
    registry_path: Path | None = None,
) -> SteamAccount:
    """Select a saved account. Steam must be fully stopped before this call."""
    registry_path = registry_path or default_registry_path()
    state = load_account_state(steam_root, registry_path)
    target = next(
        (account for account in state.accounts if account.steam_id == str(steam_id)),
        None,
    )
    if target is None:
        raise ValueError(f"{steam_id} is not a saved Steam account")

    loginusers_path = steam_root / "config" / "loginusers.vdf"
    config_path = steam_root / "config" / "config.vdf"
    login_document = _load_vdf(loginusers_path)
    users = _nested(login_document, "users")
    if not isinstance(users, dict):
        raise ValueError(f"Steam account selectors are missing from {loginusers_path}")
    for user_id, user in users.items():
        if not isinstance(user, dict):
            continue
        selected = "1" if str(user_id) == target.steam_id else "0"
        for marker in ("AutoLogin", "MostRecent"):
            if marker in user or selected == "1":
                user[marker] = selected

    registry_document = _load_vdf(registry_path)
    _set_nested_value(
        registry_document,
        ("Registry", "HKCU", "Software", "Valve", "Steam"),
        "AutoLoginUser",
        target.account_name,
    )

    try:
        updates: dict[Path, bytes] = {
            loginusers_path: _serialize_vdf(login_document),
            registry_path: _serialize_vdf(registry_document),
        }
        if disable_shader_cache != state.shader_cache_disabled:
            config_document = _load_vdf(config_path)
            _set_nested_value(
                config_document,
                (
                    "InstallConfigStore",
                    "Software",
                    "Valve",
                    "Steam",
                    "ShaderCacheManager",
                ),
                "DisableShaderCache",
                "1" if disable_shader_cache else "0",
            )
            updates[config_path] = _serialize_vdf(config_document)
        atomic_write_group(updates)
    except OSError as error:
        raise ValueError(
            f"Could not update Steam's account settings: {error}"
        ) from error
    return target


def _set_nested_value(
    document: dict[str, Any], object_path: tuple[str, ...], key: str, value: str
) -> None:
    """Set a scalar, creating missing objects along the path."""
    node = document
    for part in object_path:
        child = next(
            (
                existing
                for existing_key, existing in node.items()
                if existing_key.casefold() == part.casefold()
                and isinstance(existing, dict)
            ),
            None,
        )
        if child is None:
            child = {}
            node[part] = child
        node = child
    existing_key = next(
        (name for name in node if name.casefold() == key.casefold()), key
    )
    node[existing_key] = value


def _serialize_vdf(document: dict[str, Any]) -> bytes:
    text = vdf.dumps(document, pretty=True)
    if vdf.loads(text) != document:
        raise ValueError("The Steam configuration did not survive re-serialization")
    return text.encode("utf-8")


def default_pid_file() -> Path:
    return Path.home() / ".steam" / "steam.pid"


def _is_steam_process(process: Path, steam_root: Path | None) -> bool:
    try:
        if process.stat().st_uid != os.getuid():
            return False
        stat = (process / "stat").read_text(errors="replace")
        comm = (process / "comm").read_text(errors="replace").strip()
    except OSError:
        return False
    state = stat.rpartition(")")[2].split()
    if state and state[0] == "Z":
        return False
    if comm != "steam":
        return False
    if steam_root is not None:
        # A sandboxed (Flatpak/Snap) client is also named "steam" but runs
        # from outside the selected installation; only exclude it when the
        # executable is actually readable.
        try:
            executable = (process / "exe").readlink().resolve()
        except OSError:
            return True
        return executable.is_relative_to(steam_root.resolve())
    return True


def steam_is_running(
    steam_root: Path | None = None,
    proc_root: Path = Path("/proc"),
    pid_file: Path | None = None,
) -> bool:
    """Report whether the selected Steam installation has a live client.

    The native client records its PID in ~/.steam/steam.pid; that file is
    authoritative when present. Without it, fall back to scanning the process
    list for this user's live "steam" processes.
    """
    pid_file = pid_file or default_pid_file()
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        pid = None
    if pid is not None:
        return _is_steam_process(proc_root / str(pid), steam_root)
    try:
        processes = list(proc_root.iterdir())
    except OSError:
        return False
    return any(
        _is_steam_process(process, steam_root)
        for process in processes
        if process.name.isdigit()
    )
