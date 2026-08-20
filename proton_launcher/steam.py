# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import os
import re
import struct
import tempfile
from pathlib import Path
from typing import Any

import vdf

from .models import DiscoveryIssue, GameEntry, GameSource, SteamLaunchOption
from .util import expanded_path, unquote_path

DEFAULT_STEAM_ROOTS = ("~/.local/share/Steam", "~/.steam/steam")
VDF_TOKEN = re.compile(rb'//[^\r\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+')


def _direct_app_state_value_spans(
    data: bytes, wanted_key: str
) -> list[tuple[int, int]]:
    """Find scalar values belonging directly to the top-level AppState object."""
    contexts: list[dict[str, Any]] = [{"pending": None, "app_state": False}]
    spans: list[tuple[int, int]] = []
    for match in VDF_TOKEN.finditer(data):
        token = match.group()
        if token.startswith(b"//"):
            continue
        context = contexts[-1]
        if token == b"{":
            key = context["pending"]
            is_app_state = len(contexts) == 1 and key == "appstate"
            context["pending"] = None
            contexts.append({"pending": None, "app_state": is_app_state})
            continue
        if token == b"}":
            if len(contexts) > 1:
                contexts.pop()
            continue

        value = token[1:-1] if token.startswith(b'"') else token
        text = value.decode("utf-8", errors="replace").casefold()
        if context["pending"] is None:
            context["pending"] = text
            continue
        if context["app_state"] and context["pending"] == wanted_key.casefold():
            start, end = match.span()
            if token.startswith(b'"'):
                start += 1
                end -= 1
            spans.append((start, end))
        context["pending"] = None
    return spans


def _text_vdf(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        return vdf.load(handle)


def appmanifest_path(game: GameEntry) -> Path:
    if game.source != GameSource.STEAM:
        raise ValueError("Skip update is only available for Steam games")
    return game.library_root / "steamapps" / f"appmanifest_{game.app_id}.acf"


def set_manifest_state_flags(game: GameEntry, state_flags: int = 4) -> Path:
    """Atomically replace a Steam game's top-level StateFlags value."""
    manifest = appmanifest_path(game)
    try:
        app_state = _text_vdf(manifest).get("AppState", {})
    except (OSError, ValueError, SyntaxError, AttributeError) as error:
        raise ValueError(f"Could not read {manifest}: {error}") from error
    if str(app_state.get("appid", "")) != str(game.app_id):
        raise ValueError(f"Manifest app ID does not match {game.app_id}: {manifest}")

    try:
        original = manifest.read_bytes()
    except OSError as error:
        raise ValueError(f"Could not read {manifest}: {error}") from error
    spans = _direct_app_state_value_spans(original, "StateFlags")
    if len(spans) != 1:
        raise ValueError(
            f"Expected one direct AppState StateFlags entry in {manifest}, "
            f"found {len(spans)}"
        )
    start, end = spans[0]
    updated = original[:start] + str(state_flags).encode("ascii") + original[end:]

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=manifest.parent,
            prefix=f".{manifest.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(updated)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.chmod(manifest.stat().st_mode)
        os.replace(temporary_path, manifest)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ValueError(f"Could not update {manifest}: {error}") from error
    return manifest


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
    path: Path,
    steam_root: Path,
    library: Path,
    launch_options: list[SteamLaunchOption] | None = None,
) -> GameEntry | None:
    app = _text_vdf(path).get("AppState", {})
    app_id = int(app["appid"])
    name, install_dir = str(app["name"]), str(app.get("installdir", ""))
    if is_component(app_id, name, install_dir):
        return None
    installed = library / "steamapps" / "common" / install_dir if install_dir else None
    resolved_options: list[SteamLaunchOption] = []
    if installed:
        for option in launch_options or []:
            executable = _resolve_install_file(installed, option.executable)
            if not executable:
                continue
            working_directory = _resolve_install_path(
                installed, option.working_directory
            )
            resolved_options.append(
                SteamLaunchOption(
                    option.label.strip() or f"Play {name.strip()}",
                    executable,
                    option.arguments,
                    working_directory,
                )
            )
    default_executable = (
        resolved_options[0].executable
        if resolved_options
        else (resolve_game_executable(installed) if installed else "")
    )
    return GameEntry(
        GameSource.STEAM,
        app_id,
        name.strip(),
        steam_root,
        library,
        installed,
        default_executable=default_executable,
        launch_options=tuple(resolved_options),
    )


def _read_exact(handle, size: int) -> bytes:
    value = handle.read(size)
    if len(value) != size:
        raise ValueError("Truncated binary VDF value")
    return value


def _read_cstring(handle, wide: bool = False) -> str:
    terminator = b"\0\0" if wide else b"\0"
    value = bytearray()
    unit = 2 if wide else 1
    while True:
        part = _read_exact(handle, unit)
        if part == terminator:
            return value.decode("utf-16-le" if wide else "utf-8", errors="replace")
        value.extend(part)


def _load_key_table_vdf(handle, keys: list[str]) -> dict[str, Any]:
    """Decode the key-indexed binary VDF used by current appinfo files."""
    containers: list[dict[str, Any]] = [{}]
    while True:
        value_type = _read_exact(handle, 1)[0]
        if value_type == 8:
            if len(containers) == 1:
                return containers[0]
            containers.pop()
            continue
        key_index = struct.unpack("<i", _read_exact(handle, 4))[0]
        if not 0 <= key_index < len(keys):
            raise ValueError(f"Invalid appinfo key index {key_index}")
        key = keys[key_index]
        if value_type == 0:
            child: dict[str, Any] = {}
            containers[-1][key] = child
            containers.append(child)
        elif value_type == 1:
            containers[-1][key] = _read_cstring(handle)
        elif value_type == 2:
            containers[-1][key] = struct.unpack("<i", _read_exact(handle, 4))[0]
        elif value_type == 3:
            containers[-1][key] = struct.unpack("<f", _read_exact(handle, 4))[0]
        elif value_type in {4, 6}:
            containers[-1][key] = struct.unpack("<I", _read_exact(handle, 4))[0]
        elif value_type == 5:
            containers[-1][key] = _read_cstring(handle, wide=True)
        elif value_type == 7:
            containers[-1][key] = struct.unpack("<Q", _read_exact(handle, 8))[0]
        elif value_type == 10:
            containers[-1][key] = struct.unpack("<q", _read_exact(handle, 8))[0]
        else:
            raise ValueError(f"Unsupported binary VDF type {value_type}")


def _windows_launch_options(data: dict[str, Any]) -> list[SteamLaunchOption]:
    launches = data.get("config", {}).get("launch", {})
    result: list[SteamLaunchOption] = []
    if not isinstance(launches, dict):
        return result
    for launch in launches.values():
        if not isinstance(launch, dict):
            continue
        config = launch.get("config", {})
        config = config if isinstance(config, dict) else {}
        oslist = str(config.get("oslist", "")).strip().casefold()
        executable = str(launch.get("executable", "")).strip()
        if not executable or (oslist and "windows" not in oslist):
            continue
        if str(launch.get("type", "default")).casefold() == "none":
            continue
        description = str(launch.get("description", "")).strip()
        if not description:
            localized = launch.get("description_loc", {})
            if isinstance(localized, dict):
                description = str(localized.get("english", "")).strip()
        result.append(
            SteamLaunchOption(
                description,
                executable,
                str(launch.get("arguments", "")),
                str(launch.get("workingdir", "")),
            )
        )
    return result


def parse_appinfo_launches(
    path: Path, wanted_app_ids: set[int]
) -> dict[int, list[SteamLaunchOption]]:
    """Read Windows launch choices from Steam appinfo V29."""
    result: dict[int, list[SteamLaunchOption]] = {}
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
                data = _load_key_table_vdf(handle, keys).get("appinfo", {})
                options = _windows_launch_options(data)
                if options:
                    result[app_id] = options
            handle.seek(end)
    return result


def _resolve_install_path(install_dir: Path, value: str) -> str:
    value = value.strip().strip('"')
    if not value:
        return ""
    normalized = value.replace("%INSTALLDIR%", "").lstrip("/\\")
    path = _case_insensitive_path(install_dir, normalized)
    return str(path) if path.is_dir() else ""


def _resolve_install_file(install_dir: Path, value: str) -> str:
    value = value.strip().strip('"')
    if not value:
        return ""
    path = _case_insensitive_path(install_dir, value.lstrip("/\\"))
    return str(path) if path.is_file() else ""


def _case_insensitive_path(root: Path, relative: str) -> Path:
    current = root
    for part in relative.replace("\\", "/").split("/"):
        if not part or part == ".":
            continue
        direct = current / part
        if direct.exists():
            current = direct
            continue
        try:
            match = next(
                child
                for child in current.iterdir()
                if child.name.casefold() == part.casefold()
            )
        except (OSError, StopIteration):
            return direct
        current = match
    return current


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
            launch_options = parse_appinfo_launches(
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
            launch_options = {}
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
                        manifest, root, library, launch_options.get(manifest_id)
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
