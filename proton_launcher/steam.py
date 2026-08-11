# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import vdf

from .models import DiscoveryIssue, GameEntry, GameSource
from .util import expanded_path, unquote_path

DEFAULT_STEAM_ROOTS = ("~/.local/share/Steam", "~/.steam/steam")


def _text_vdf(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        return vdf.load(handle)


def discover_steam_roots(custom: list[str] | None = None) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for raw in [*DEFAULT_STEAM_ROOTS, *(custom or [])]:
        path = expanded_path(raw)
        if path.is_dir() and path not in seen:
            seen.add(path)
            result.append(path)
    return result


def discover_libraries(steam_root: Path, custom: list[str] | None = None) -> list[Path]:
    values = [steam_root]
    config = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        folders = _text_vdf(config).get("libraryfolders", {})
        values.extend(
            Path(entry["path"])
            for entry in folders.values()
            if isinstance(entry, dict) and entry.get("path")
        )
    except (OSError, ValueError, KeyError):
        pass
    values.extend(expanded_path(path) for path in custom or [])
    result: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        path = expanded_path(value)
        if (path / "steamapps").is_dir() and path not in seen:
            seen.add(path)
            result.append(path)
    return result


def is_component(app_id: int, name: str, install_dir: str) -> bool:
    n, directory = name.strip().casefold(), install_dir.strip().casefold()
    return (
        app_id == 228980
        or n == "steamworks common redistributables"
        or directory == "steamworks shared"
        or n.startswith("steam linux runtime")
        or directory.startswith("steamlinuxruntime")
        or n == "proton experimental"
        or n.startswith("proton ")
        or directory.startswith("proton ")
    )


def parse_manifest(
    path: Path, steam_root: Path, library: Path, launch_executable: str = ""
) -> GameEntry | None:
    app = _text_vdf(path).get("AppState", {})
    app_id = int(app["appid"])
    name, install_dir = str(app["name"]), str(app.get("installdir", ""))
    if is_component(app_id, name, install_dir):
        return None
    installed = library / "steamapps" / "common" / install_dir if install_dir else None
    default_executable = (
        resolve_game_executable(installed, launch_executable) if installed else ""
    )
    return GameEntry(
        GameSource.STEAM,
        app_id,
        name.strip(),
        steam_root,
        library,
        installed,
        default_executable=default_executable,
    )


def parse_appinfo_launches(path: Path, wanted_app_ids: set[int]) -> dict[int, str]:
    """Read default Windows launch executables from Steam appinfo V29."""
    result: dict[int, str] = {}
    if not wanted_app_ids or not path.is_file():
        return result
    file_size = path.stat().st_size
    with path.open("rb") as handle:
        header = handle.read(8)
        if len(header) != 8:
            raise ValueError("Truncated appinfo header")
        magic, _universe = struct.unpack("<II", header)
        if magic != 0x07564429:
            raise ValueError(f"Unsupported appinfo format 0x{magic:08x}")
        offset_data = handle.read(8)
        if len(offset_data) != 8:
            raise ValueError("Truncated appinfo key-table offset")
        key_table_offset = struct.unpack("<Q", offset_data)[0]
        if not 16 <= key_table_offset < file_size:
            raise ValueError("Invalid appinfo key-table offset")
        handle.seek(key_table_offset)
        count_data = handle.read(4)
        if len(count_data) != 4:
            raise ValueError("Truncated appinfo key count")
        key_count = struct.unpack("<I", count_data)[0]
        if key_count > 1_000_000:
            raise ValueError("Unreasonable appinfo key count")
        keys: list[str] = []
        for _ in range(key_count):
            value = bytearray()
            while (character := handle.read(1)) != b"\0":
                if not character:
                    raise ValueError("Truncated appinfo key table")
                value.extend(character)
            keys.append(value.decode(errors="replace"))
        handle.seek(16)
        while True:
            start = handle.tell()
            header = handle.read(8)
            if len(header) != 8:
                break
            app_id, size = struct.unpack("<II", header)
            if app_id == 0:
                break
            end = start + 8 + size
            if end > key_table_offset or end < start + 68:
                raise ValueError(f"Invalid appinfo record size for app {app_id}")
            if app_id in wanted_app_ids:
                handle.seek(start + 68)
                data = vdf.binary_load(handle, key_table=keys).get("appinfo", {})
                launches = data.get("config", {}).get("launch", {})
                for launch in launches.values():
                    config = (
                        launch.get("config", {}) if isinstance(launch, dict) else {}
                    )
                    oslist = str(config.get("oslist", "windows")).casefold()
                    launch_type = (
                        str(launch.get("type", "default")).casefold()
                        if isinstance(launch, dict)
                        else ""
                    )
                    executable = (
                        str(launch.get("executable", ""))
                        if isinstance(launch, dict)
                        else ""
                    )
                    if executable and "windows" in oslist and launch_type != "none":
                        result[app_id] = executable
                        break
            handle.seek(end)
    return result


def resolve_game_executable(install_dir: Path, steam_executable: str = "") -> str:
    if steam_executable:
        candidate = install_dir.joinpath(
            *steam_executable.replace("\\", "/").split("/")
        )
        if candidate.is_file():
            return str(candidate)
    if not install_dir.is_dir():
        return ""
    excluded = (
        "unins",
        "uninstall",
        "crash",
        "report",
        "upload",
        "vcredist",
        "dxsetup",
        "setup.exe",
    )
    candidates = [
        path
        for path in install_dir.rglob("*.exe")
        if not any(word in path.name.casefold() for word in excluded)
    ]
    root_candidates = [path for path in candidates if path.parent == install_dir]
    pool = root_candidates or candidates
    if len(pool) == 1:
        return str(pool[0])
    normalized = "".join(
        character for character in install_dir.name.casefold() if character.isalnum()
    )
    named = [
        path
        for path in pool
        if "".join(
            character for character in path.stem.casefold() if character.isalnum()
        )
        in {normalized, "game"}
    ]
    return str(named[0]) if len(named) == 1 else ""


def parse_shortcuts(
    path: Path, steam_root: Path, libraries: list[Path]
) -> list[GameEntry]:
    with path.open("rb") as handle:
        shortcuts = vdf.binary_load(handle).get("shortcuts", {})
    result: list[GameEntry] = []
    for item in shortcuts.values():
        app_id = int(item["appid"]) & 0xFFFFFFFF
        library = steam_root
        for candidate in libraries:
            if (candidate / "steamapps" / "compatdata" / str(app_id)).exists():
                library = candidate
                break
        result.append(
            GameEntry(
                GameSource.SHORTCUT,
                app_id,
                str(item.get("AppName") or f"Shortcut {app_id}"),
                steam_root,
                library,
                None,
                unquote_path(str(item.get("exe", ""))),
                unquote_path(str(item.get("StartDir", ""))),
            )
        )
    return result


def discover_games(
    custom_roots: list[str] | None = None, custom_libraries: list[str] | None = None
) -> tuple[list[GameEntry], list[DiscoveryIssue]]:
    games: list[GameEntry] = []
    issues: list[DiscoveryIssue] = []
    seen: set[str] = set()
    for root in discover_steam_roots(custom_roots):
        libraries = discover_libraries(root, custom_libraries)
        manifests = [
            manifest
            for library in libraries
            for manifest in (library / "steamapps").glob("appmanifest_*.acf")
        ]
        app_ids = {
            int(manifest.stem.removeprefix("appmanifest_"))
            for manifest in manifests
            if manifest.stem.removeprefix("appmanifest_").isdigit()
        }
        try:
            launch_executables = parse_appinfo_launches(
                root / "appcache" / "appinfo.vdf", app_ids
            )
        except (
            OSError,
            ValueError,
            TypeError,
            struct.error,
            SyntaxError,
            IndexError,
        ) as exc:
            launch_executables = {}
            issues.append(
                DiscoveryIssue(
                    root / "appcache" / "appinfo.vdf",
                    f"Could not parse launch metadata: {exc}",
                )
            )
        for library in libraries:
            for manifest in (library / "steamapps").glob("appmanifest_*.acf"):
                try:
                    manifest_id = int(manifest.stem.removeprefix("appmanifest_"))
                    game = parse_manifest(
                        manifest, root, library, launch_executables.get(manifest_id, "")
                    )
                    if game and game.key not in seen:
                        games.append(game)
                        seen.add(game.key)
                except (OSError, ValueError, KeyError) as exc:
                    issues.append(
                        DiscoveryIssue(manifest, f"Could not parse manifest: {exc}")
                    )
        for shortcuts in (root / "userdata").glob("*/config/shortcuts.vdf"):
            try:
                for game in parse_shortcuts(shortcuts, root, libraries):
                    if game.key not in seen:
                        games.append(game)
                        seen.add(game.key)
            except (OSError, ValueError, KeyError) as exc:
                issues.append(
                    DiscoveryIssue(shortcuts, f"Could not parse shortcuts: {exc}")
                )
    games.sort(key=lambda game: (game.name.casefold(), game.app_id))
    return games, issues
