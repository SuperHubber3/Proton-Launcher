# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from .models import (
    DEFAULT_NON_STEAM_WINEDLLOVERRIDES,
    DEFAULT_WEMOD_WINEDLLOVERRIDES,
    GameEntry,
    LaunchProfile,
    LaunchSpec,
)
from .util import expanded_path

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WINDOWS_PATH = re.compile(r"^([A-Za-z]):[\\/](.*)$")
RUNAS_HELPER = (
    Path(__file__).resolve().parent.parent / "helpers" / "runas-helper.exe.so"
)
WEMOD_OVERRIDES_VARIABLE = "PL_WEMOD_WINEDLLOVERRIDES"
WEMOD_STEAM_LIBRARY_VARIABLE = "PL_WEMOD_STEAM_LIBRARY"
WEMOD_STEAM_APP_ID_VARIABLE = "PL_WEMOD_STEAM_APP_ID"
WEMOD_BRIDGE = Path(__file__).resolve().parent / "wemod_bridge.py"
SYSTEM_DATA_DIRS = ("/usr/local/share", "/usr/share")


def process_environment() -> dict[str, str]:
    """Copy the environment and keep system data files discoverable."""
    environment = dict(os.environ)
    data_dirs = [
        value for value in environment.get("XDG_DATA_DIRS", "").split(":") if value
    ]
    for directory in SYSTEM_DATA_DIRS:
        if directory not in data_dirs:
            data_dirs.append(directory)
    environment["XDG_DATA_DIRS"] = ":".join(data_dirs)
    return environment


def clean_process_output(text: str) -> str:
    """Hide expected dual-architecture Steam overlay preload warnings."""
    kept: list[str] = []
    for line in text.splitlines():
        lowered = line.casefold()
        benign = (
            "gameoverlayrenderer.so" in lowered
            and "wrong elf class" in lowered
            and "ignored" in lowered
        )
        if not benign:
            kept.append(line)
    return "\n".join(kept)


def prepare_compatdata_directory(prefix: Path) -> bool:
    """Create Proton's base compatdata directory before it opens pfx.lock."""
    prefix = prefix.resolve(strict=False)
    if prefix.exists():
        if not prefix.is_dir():
            raise ValueError(f"Compatibility-data path is not a directory: {prefix}")
        return False
    prefix.mkdir(parents=True, exist_ok=True)
    return True


def unquote_environment_value(value: str) -> str:
    """Remove shell-style outer quotes without altering the value inside."""
    value = value.strip()
    if value[:1] in {"'", '"'}:
        if len(value) < 2 or value[-1] != value[0]:
            raise ValueError("Unclosed quote in environment value")
        return value[1:-1]
    return value


def parse_environment_text(value: str) -> dict[str, str]:
    """Parse the profile environment editor without invoking a shell."""
    result: dict[str, str] = {}
    for number, line in enumerate(value.splitlines(), 1):
        if not line.strip():
            continue
        if "=" not in line:
            raise ValueError(f"Environment line {number} must be NAME=value")
        name, raw = line.split("=", 1)
        name = name.strip()
        if not ENV_NAME.match(name):
            raise ValueError(
                f"Invalid environment variable name on line {number}: {name}"
            )
        result[name] = unquote_environment_value(raw)
    return result


def resolve_wemod_executable(launcher: Path) -> Path:
    """Locate WeMod.exe belonging to the configured wemod-launcher checkout."""
    resolved = launcher.resolve(strict=True)
    root = resolved.parent.parent if resolved.parent.name == "src" else resolved.parent
    executable = root / "wemod_data" / "wemod_bin" / "WeMod.exe"
    if not executable.is_file():
        raise ValueError(f"Could not find the installed WeMod executable: {executable}")
    return executable


def resolve_working_directory(value: str, prefix: Path) -> Path:
    """Resolve either a host path or a Wine drive path to a host directory."""
    raw = value.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1]
    match = WINDOWS_PATH.match(raw)
    if not match:
        return expanded_path(raw)
    drive, remainder = match.groups()
    drive_mapping = prefix / "pfx" / "dosdevices" / f"{drive.casefold()}:"
    if not drive_mapping.exists():
        raise ValueError(f"Wine drive {drive.upper()}: is not mapped in this prefix")
    # Backslashes are separators in Wine paths, not literal host characters.
    parts = [part for part in re.split(r"[\\/]+", remainder) if part not in {"", "."}]
    if ".." in parts:
        raise ValueError("Working directory cannot contain '..' path segments")
    return drive_mapping.resolve(strict=False).joinpath(*parts)


def to_wine_path(path: Path, prefix: Path) -> str:
    """Translate a host path through the prefix's Wine drive mappings."""
    host = path.resolve(strict=False)
    mappings: list[tuple[int, str, Path]] = []
    for mapping in (prefix / "pfx" / "dosdevices").glob("?:"):
        try:
            root = mapping.resolve(strict=False)
            host.relative_to(root)
        except (OSError, ValueError):
            continue
        mappings.append((len(root.parts), mapping.name[0].upper(), root))
    if not mappings:
        raise ValueError(f"No Wine drive maps the executable path: {host}")
    _, drive, root = max(mappings)
    relative = host.relative_to(root)
    suffix = "\\".join(relative.parts)
    return f"{drive}:\\{suffix}" if suffix else f"{drive}:\\"


def build_launch_spec(
    game: GameEntry,
    profile: LaunchProfile,
    prefix: Path | None = None,
    wemod_path: str = "",
) -> LaunchSpec:
    proton = expanded_path(profile.proton_path)
    if not proton.is_file():
        raise ValueError(f"Proton launcher does not exist: {proton}")
    prefix_path = prefix or game.default_prefix
    arguments = ["run"]
    executable_path: Path | None = None
    if profile.mode == "executable":
        if not profile.executable.strip():
            raise ValueError("Choose an executable")
        executable_path = expanded_path(profile.executable)
        if not executable_path.is_file():
            raise ValueError(f"Executable does not exist: {executable_path}")
        executable_arguments = shlex.split(profile.arguments)
        if profile.run_as_admin:
            if not RUNAS_HELPER.is_file():
                raise ValueError(
                    f"Run as administrator helper is missing: {RUNAS_HELPER}"
                )
            windows_executable = to_wine_path(executable_path, prefix_path)
            arguments = [
                "runinprefix",
                str(RUNAS_HELPER),
                windows_executable,
                *executable_arguments,
            ]
        else:
            arguments.append(str(executable_path))
            arguments.extend(executable_arguments)
    else:
        command = shlex.split(profile.command)
        if not command:
            raise ValueError("Enter a command")
        # Proton's runinprefix verb already prepends its bundled Wine binary.
        # Accept "wine explorer" as a friendly spelling without passing a
        # second, invalid Wine executable as the Windows target.
        if command[0].casefold() in {"wine", "wine64"}:
            command = command[1:]
        if not command:
            raise ValueError("Enter a command after wine")
        # Unlike cmd, Explorer does not navigate to the process working
        # directory unless it receives an explicit target.
        if (
            len(command) == 1
            and not profile.arguments.strip()
            and command[0].casefold() in {"explorer", "explorer.exe"}
        ):
            command.append(".")
        arguments = ["runinprefix"]
        arguments.extend(command)
        arguments.extend(shlex.split(profile.arguments))
    environment = process_environment()
    for name, value in parse_environment_text(profile.environment_text).items():
        if name not in {"STEAM_COMPAT_DATA_PATH", "STEAM_COMPAT_CLIENT_INSTALL_PATH"}:
            environment[name] = value
    if profile.apply_online_fix:
        environment["WINEDLLOVERRIDES"] = DEFAULT_NON_STEAM_WINEDLLOVERRIDES
    environment["STEAM_COMPAT_DATA_PATH"] = str(prefix_path)
    environment["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(game.steam_root)
    if profile.inject_steam_overlay:
        app_id = profile.overlay_app_id.strip() or str(game.app_id)
        if not app_id.isdigit() or int(app_id) < 1:
            raise ValueError("Overlay app ID must be a positive integer")
        renderer32 = game.steam_root / "ubuntu12_32" / "gameoverlayrenderer.so"
        renderer64 = game.steam_root / "ubuntu12_64" / "gameoverlayrenderer.so"
        missing = [str(path) for path in (renderer32, renderer64) if not path.is_file()]
        if missing:
            raise ValueError("Steam overlay renderer is missing: " + ", ".join(missing))
        preload = ":".join((str(renderer32), str(renderer64)))
        if environment.get("LD_PRELOAD"):
            preload += ":" + environment["LD_PRELOAD"]
        environment["LD_PRELOAD"] = preload
        environment["SteamAppId"] = app_id
        environment["SteamGameId"] = app_id
        environment["SteamOverlayGameId"] = app_id
    if profile.working_directory:
        working = resolve_working_directory(profile.working_directory, prefix_path)
        if not working.is_dir():
            raise ValueError(f"Working directory does not exist: {working}")
    elif executable_path:
        working = executable_path.parent
    elif game.shortcut_start_dir and expanded_path(game.shortcut_start_dir).is_dir():
        working = expanded_path(game.shortcut_start_dir)
    else:
        working = Path.home()
    if profile.launch_wemod:
        if profile.mode != "executable":
            raise ValueError("Launch with WeMod can only be used with an executable")
        if profile.run_as_admin:
            raise ValueError(
                "Launch with WeMod cannot be combined with Run as administrator"
            )
        wemod = expanded_path(wemod_path)
        if not wemod.is_file():
            raise ValueError(f"WeMod launcher does not exist: {wemod}")
        game_overrides = environment.get("WINEDLLOVERRIDES", "")
        wemod_executable = resolve_wemod_executable(wemod)
        environment[WEMOD_OVERRIDES_VARIABLE] = DEFAULT_WEMOD_WINEDLLOVERRIDES
        if game.source.value == "steam":
            environment[WEMOD_STEAM_LIBRARY_VARIABLE] = str(game.library_root)
            environment[WEMOD_STEAM_APP_ID_VARIABLE] = str(game.app_id)
        if game_overrides:
            environment["WINEDLLOVERRIDES"] = game_overrides
        else:
            environment.pop("WINEDLLOVERRIDES", None)
        environment["STEAM_COMPAT_TOOL_PATHS"] = str(proton.parent)
        return LaunchSpec(
            sys.executable,
            [
                str(WEMOD_BRIDGE),
                str(proton),
                str(wemod_executable),
                json.dumps(arguments),
            ],
            environment,
            str(working),
        )
    return LaunchSpec(str(proton), arguments, environment, str(working))


def build_followup_launch_spec(
    game: GameEntry, profile: LaunchProfile, prefix: Path | None = None
) -> LaunchSpec:
    """Build the secondary launch using the primary profile's Proton context."""
    followup = LaunchProfile(
        id="follow-up",
        name="Follow-up",
        game_key=game.key,
        proton_path=profile.proton_path,
        mode=profile.followup_mode,
        executable=profile.followup_executable,
        command=profile.followup_command,
        arguments=profile.followup_arguments,
        working_directory=profile.working_directory,
        environment_text=profile.environment_text,
        apply_online_fix=profile.apply_online_fix,
        run_as_admin=profile.followup_run_as_admin,
    )
    return build_launch_spec(game, followup, prefix)


def build_wemod_launch_spec(
    game: GameEntry,
    profile: LaunchProfile,
    wemod_path: str,
    prefix: Path | None = None,
) -> LaunchSpec:
    """Build a standalone WeMod launch in the selected game's prefix."""
    proton = expanded_path(profile.proton_path)
    if not proton.is_file():
        raise ValueError(f"Proton launcher does not exist: {proton}")
    wemod = expanded_path(wemod_path)
    if not wemod.is_file():
        raise ValueError(f"WeMod launcher does not exist: {wemod}")

    prefix_path = prefix or game.default_prefix
    wemod_executable = resolve_wemod_executable(wemod)
    environment = process_environment()
    environment["STEAM_COMPAT_DATA_PATH"] = str(prefix_path)
    environment["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(game.steam_root)
    environment["STEAM_COMPAT_TOOL_PATHS"] = str(proton.parent)
    environment[WEMOD_OVERRIDES_VARIABLE] = DEFAULT_WEMOD_WINEDLLOVERRIDES
    if game.source.value == "steam":
        environment[WEMOD_STEAM_LIBRARY_VARIABLE] = str(game.library_root)
        environment[WEMOD_STEAM_APP_ID_VARIABLE] = str(game.app_id)

    return LaunchSpec(
        sys.executable,
        [
            str(WEMOD_BRIDGE),
            str(proton),
            str(wemod_executable),
            "[]",
            "--wemod-only",
        ],
        environment,
        str(Path.home()),
    )


def build_steam_launch_spec(game: GameEntry) -> LaunchSpec:
    """Ask the Steam client to launch a registered Steam or shortcut app."""
    program = shutil.which("steam")
    if not program:
        candidates = (game.steam_root / "steam.sh", game.steam_root / "steam")
        program = next((str(path) for path in candidates if path.is_file()), "")
    if not program:
        raise ValueError("Could not find the Steam client executable")
    working = (
        game.install_dir
        if game.install_dir and game.install_dir.is_dir()
        else Path.home()
    )
    if game.source.value == "shortcut":
        # Steam stores a signed/unsigned 32-bit shortcut app ID, but its
        # rungameid URI uses a 64-bit game ID with the shortcut type marker.
        game_id = ((game.app_id & 0xFFFFFFFF) << 32) | 0x02000000
        arguments = [f"steam://rungameid/{game_id}"]
    else:
        arguments = ["-applaunch", str(game.app_id)]
    return LaunchSpec(program, arguments, process_environment(), str(working))
