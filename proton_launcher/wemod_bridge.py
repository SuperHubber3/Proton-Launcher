# SPDX-License-Identifier: GPL-3.0-only
"""Start WeMod and a game with separate pre-Proton environments."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from .wemod_state import (
        WeModGameMapping,
        discover_custom_mapping,
        load_cached_mapping,
        save_cached_mapping,
    )
except ImportError:  # Direct execution by runner.py.
    from wemod_state import (  # type: ignore[no-redef]
        WeModGameMapping,
        discover_custom_mapping,
        load_cached_mapping,
        save_cached_mapping,
    )

WEMOD_VARIABLE = "PL_WEMOD_WINEDLLOVERRIDES"
STEAM_LIBRARY_VARIABLE = "PL_WEMOD_STEAM_LIBRARY"
STEAM_APP_ID_VARIABLE = "PL_WEMOD_STEAM_APP_ID"
GAME_WRAPPER_VARIABLE = "PL_GAME_WRAPPER_ARGUMENTS"
GAME_ENVIRONMENT_VARIABLE = "PL_GAME_ONLY_ENVIRONMENT"
DISPLAY_NOTICE_VARIABLE = "PL_WEMOD_DISPLAY_NOTICE"
STEAM_RETRY_HELPER = (
    Path(__file__).resolve().parent.parent / "helpers" / "steam-retry-helper.exe"
)
STEAM_RETRY_MARKER = ".proton-launcher-steam-retry"
# Proton forces its built-in steam.exe for every title except Locoland. Use
# that exception while WeMod starts the retry helper, then let the helper set
# the selected game's real Steam IDs before it creates the game process.
STEAM_RETRY_NATIVE_APP_ID = "352130"
STEAM_RETRY_NATIVE_OVERRIDE = "steam.exe=n,b"
# Keep Electron diagnostics available without forcing a graphics backend.
WEMOD_RENDER_ARGUMENTS = ("--enable-logging=file",)
WEMOD_READY_TIMEOUT = 45.0
WEMOD_READY_SETTLE_TIME = 2.0
REGISTRY_TIMEOUT = 30.0


def _legacy_steam_retry_directory(pfx: Path) -> Path:
    return pfx / "drive_c" / "ProtonLauncher" / "Steam"


def _steam_retry_paths(library: Path) -> tuple[Path, Path]:
    return library / "Steam.exe", library / STEAM_RETRY_MARKER


def _remove_steam_retry_helper(library: Path) -> list[Path]:
    destination, marker = _steam_retry_paths(library.resolve(strict=False))
    if not marker.is_file():
        return []
    if destination.exists() and not destination.is_file():
        raise OSError(f"Refusing to remove a non-file path: {destination}")
    removed = []
    if destination.is_file():
        destination.unlink()
        removed.append(destination)
    marker.unlink()
    removed.append(marker)
    return removed


def reset_wemod_prefix(prefix: Path, steam_library: Path | None = None) -> list[Path]:
    """Remove launcher-managed WeMod setup files."""
    pfx = prefix.resolve(strict=False) / "pfx"
    candidates = (
        pfx / ".wemod_installer",
        pfx / "drive_c" / "users" / "steamuser" / "AppData" / "Roaming" / "WeMod",
    )
    removed: list[Path] = []
    for path in candidates:
        if path.is_symlink() or (path == candidates[0] and path.is_file()):
            path.unlink()
            removed.append(path)
    retry_root = _legacy_steam_retry_directory(pfx)
    if retry_root.is_symlink():
        retry_root.unlink()
        removed.append(retry_root)
    elif retry_root.is_dir():
        shutil.rmtree(retry_root)
        removed.append(retry_root)
    if steam_library is not None:
        removed.extend(_remove_steam_retry_helper(steam_library))
    return removed


def _wine_paths(path: Path, prefix: Path) -> list[str]:
    host = path.resolve(strict=True)
    mappings: list[tuple[int, str, Path]] = []
    for mapping in (prefix / "pfx" / "dosdevices").glob("?:"):
        try:
            root = mapping.resolve(strict=True)
            relative = host.relative_to(root)
        except (OSError, ValueError):
            continue
        mappings.append((len(root.parts), mapping.name[0].upper(), relative))
    if not mappings:
        raise OSError(f"No Wine drive maps this path: {host}")
    paths = []
    for _, drive, relative in sorted(mappings, key=lambda item: item[0], reverse=True):
        suffix = "\\".join(relative.parts)
        paths.append(f"{drive}:\\{suffix}" if suffix else f"{drive}:\\")
    return paths


def _to_wine_path(path: Path, prefix: Path) -> str:
    return _wine_paths(path, prefix)[0]


def _prepare_steam_retry_helper(
    library: Path,
    helper: Path = STEAM_RETRY_HELPER,
) -> Path:
    if not helper.is_file():
        raise OSError(f"Steam retry helper is missing: {helper}")
    library = library.resolve(strict=True)
    destination, marker = _steam_retry_paths(library)
    if destination.exists() and not destination.is_file():
        raise OSError(f"Refusing to replace a non-file path: {destination}")
    if destination.is_file() and not marker.is_file():
        if destination.read_bytes() != helper.read_bytes():
            raise OSError(f"Refusing to replace an existing Steam.exe: {destination}")
    shutil.copy2(helper, destination)
    marker.write_text("Managed by Proton Launcher.\n", encoding="utf-8")
    return library


def _remove_legacy_steam_retry_directory(prefix: Path) -> None:
    retry_root = _legacy_steam_retry_directory(prefix / "pfx")
    if retry_root.is_symlink():
        retry_root.unlink()
    elif retry_root.is_dir():
        shutil.rmtree(retry_root)


def _configure_steam_retry_environment(
    prefix: Path,
    game_arguments: list[str],
    game_environment: dict[str, str],
    wemod_environment: dict[str, str],
    app_id: str,
) -> bool:
    if len(game_arguments) < 2 or game_arguments[0] != "run":
        return False
    try:
        target = _to_wine_path(Path(game_arguments[1]), prefix)
        directory = _to_wine_path(Path.cwd(), prefix)
    except OSError as error:
        print(f"Warning: WeMod game retry is unavailable: {error}", file=sys.stderr)
        return False
    wemod_environment["PL_STEAM_RETRY_TARGET"] = target
    wemod_environment["PL_STEAM_RETRY_ARGUMENTS"] = subprocess.list2cmdline(
        game_arguments[2:]
    )
    wemod_environment["PL_STEAM_RETRY_DIRECTORY"] = directory
    if "WINEDLLOVERRIDES" in game_environment:
        wemod_environment["PL_STEAM_RETRY_HAS_WINEDLLOVERRIDES"] = "1"
        wemod_environment["PL_STEAM_RETRY_WINEDLLOVERRIDES"] = game_environment[
            "WINEDLLOVERRIDES"
        ]
    else:
        wemod_environment.pop("PL_STEAM_RETRY_HAS_WINEDLLOVERRIDES", None)
        wemod_environment.pop("PL_STEAM_RETRY_WINEDLLOVERRIDES", None)
    if app_id:
        wemod_environment["PL_STEAM_RETRY_STEAM_APP_ID"] = app_id
    else:
        wemod_environment.pop("PL_STEAM_RETRY_STEAM_APP_ID", None)
    overrides = wemod_environment.get("WINEDLLOVERRIDES", "")
    names = {
        name.strip().casefold()
        for item in overrides.split(";")
        if item.strip()
        for name in item.partition("=")[0].split(",")
        if name.strip()
    }
    if "steam.exe" not in names:
        wemod_environment["WINEDLLOVERRIDES"] = ";".join(
            item for item in (overrides, STEAM_RETRY_NATIVE_OVERRIDE) if item
        )
    wemod_environment["SteamGameId"] = STEAM_RETRY_NATIVE_APP_ID
    return True


def _register_steam_library(
    proton: str,
    prefix: Path,
    library: Path,
    environment: dict[str, str],
) -> bool:
    try:
        wine_library = _to_wine_path(library, prefix)
    except OSError as error:
        print(
            f"Warning: WeMod Steam detection was not configured: {error}",
            file=sys.stderr,
        )
        return False
    command = [
        proton,
        "runinprefix",
        "reg",
        "add",
        r"HKLM\Software\Valve\Steam",
        "/v",
        "InstallPath",
        "/t",
        "REG_SZ",
        "/d",
        wine_library,
        "/f",
        "/reg:32",
    ]
    registry_environment = dict(environment)
    registry_environment.pop("WINEDLLOVERRIDES", None)
    try:
        result = subprocess.run(
            command,
            env=registry_environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=REGISTRY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(
            "Warning: WeMod Steam detection registry update timed out after "
            f"{REGISTRY_TIMEOUT:.0f} seconds",
            file=sys.stderr,
        )
        return False
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        print(
            "Warning: WeMod Steam detection registry update failed"
            + (f": {detail}" if detail else ""),
            file=sys.stderr,
        )
        return False
    print(f"Registered Steam library for WeMod detection: {wine_library}", flush=True)
    return True


def _custom_game_mapping(
    wemod_executable: Path,
    prefix: Path,
    game_arguments: list[str],
    use_cache: bool = True,
) -> WeModGameMapping | None:
    if len(game_arguments) < 2 or game_arguments[0] != "run":
        return None
    executable = Path(game_arguments[1])
    if use_cache:
        mapping = load_cached_mapping(executable)
        if mapping:
            return mapping
    try:
        wine_executables = _wine_paths(executable, prefix)
    except OSError:
        return None
    mapping = discover_custom_mapping(
        wemod_executable,
        executable,
        wine_executables,
    )
    if mapping:
        try:
            save_cached_mapping(mapping)
        except OSError as error:
            print(
                f"Warning: could not save the WeMod match: {error}",
                file=sys.stderr,
            )
    return mapping


def _selected_proton_version(proton: Path) -> str:
    """Read the prefix version declared by the selected Proton installation."""
    version_file = proton.resolve(strict=True).parent / "version"
    if version_file.is_file():
        value = version_file.read_text(errors="replace").strip()
        # GE/DW builds commonly prefix their actual name with a build timestamp.
        fields = value.split(maxsplit=1)
        if len(fields) == 2 and fields[0].isdigit():
            return fields[1].strip()
        if value:
            return value
    launcher = proton.read_text(errors="replace")
    match = re.search(r'^CURRENT_PREFIX_VERSION=["\']([^"\']+)', launcher, re.M)
    if match:
        return match.group(1)
    raise RuntimeError(f"Cannot determine the selected Proton version from {proton}")


def _initializer_command(wemod_executable: Path, proton: str) -> list[str]:
    """Build an init-only invocation using wemod-launcher's own environment."""
    try:
        launcher_root = wemod_executable.resolve(strict=True).parents[2]
    except (IndexError, OSError) as error:
        raise RuntimeError(
            f"Cannot locate wemod-launcher from {wemod_executable}"
        ) from error
    source = launcher_root / "src"
    python = source / "wemod_venv" / "bin" / "python"
    module = source / "wemod.py"
    if not python.is_file() or not module.is_file():
        raise RuntimeError(
            "wemod-launcher's virtual environment or src/wemod.py is missing"
        )
    code = (
        "import sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "import wemod; "
        "wemod.init(sys.argv[2])"
    )
    return [str(python), "-c", code, str(source), proton]


def _initialize_prefix(
    proton: str,
    wemod_executable: Path,
    game_environment: dict[str, str],
    wemod_environment: dict[str, str],
) -> bool:
    """Create the base prefix, then let wemod-launcher prepare its dependencies."""
    print("WeMod prefix is not initialized; preparing it now...", flush=True)
    bootstrap_environment = dict(wemod_environment)
    for name in (
        "LD_PRELOAD",
        "SteamAppId",
        "SteamGameId",
        "SteamOverlayGameId",
        "WINEDLLOVERRIDES",
    ):
        bootstrap_environment.pop(name, None)
    bootstrap = subprocess.run(
        [proton, "runinprefix", "cmd", "/c", "exit"],
        env=bootstrap_environment,
        check=False,
    )
    if bootstrap.returncode:
        print(
            f"Could not create the base Proton prefix (exit {bootstrap.returncode})",
            file=sys.stderr,
        )
        return False

    try:
        selected_version = _selected_proton_version(Path(proton))
        prefix = Path(game_environment["STEAM_COMPAT_DATA_PATH"])
        (prefix / "version").write_text(selected_version + "\n")
    except (KeyError, OSError, RuntimeError) as error:
        print(
            "Refusing WeMod initialization because the selected Proton version "
            f"could not be recorded: {error}",
            file=sys.stderr,
        )
        return False
    print(f"Initializing for selected Proton version: {selected_version}", flush=True)

    try:
        command = _initializer_command(wemod_executable, proton)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return False
    print("Starting wemod-launcher's prefix initializer", flush=True)
    initializer_environment = dict(wemod_environment)
    # Search beside the target so wemod-launcher can offer a compatible setup
    # from another initialized prefix in the same compatdata directory.
    initializer_environment["SCANFOLDER"] = str(
        Path(game_environment["STEAM_COMPAT_DATA_PATH"]).parent
    )
    initialized = subprocess.run(
        command,
        env=initializer_environment,
        cwd=str(wemod_executable.parent),
        check=False,
    )
    if initialized.returncode:
        print(
            "wemod-launcher prefix initialization failed with exit "
            f"{initialized.returncode}",
            file=sys.stderr,
        )
        return False
    return True


def _wemod_processes(proc_root: Path = Path("/proc")) -> dict[int, str]:
    """Return live WeMod command lines without depending on ps or tasklist."""
    found: dict[int, str] = {}
    try:
        entries = proc_root.iterdir()
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command = raw.replace(b"\0", b" ").decode(errors="replace").strip()
        if "wemod.exe" in command.casefold():
            found[int(entry.name)] = command
    return found


def _wait_until_wemod_ready(
    process: subprocess.Popen[bytes], baseline: set[int]
) -> bool:
    """Wait for a new Electron renderer, then let its backend settle briefly."""
    deadline = time.monotonic() + WEMOD_READY_TIMEOUT
    ready_since: float | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        current = {
            pid: command
            for pid, command in _wemod_processes().items()
            if pid not in baseline
        }
        renderer_ready = any(
            "--type=renderer" in command.casefold() for command in current.values()
        )
        if renderer_ready:
            if ready_since is None:
                ready_since = time.monotonic()
            elif time.monotonic() - ready_since >= WEMOD_READY_SETTLE_TIME:
                return True
        else:
            ready_since = None
        time.sleep(0.25)
    return False


def main() -> int:
    wemod_only = len(sys.argv) == 5 and sys.argv[4] == "--wemod-only"
    if len(sys.argv) not in {4, 5} or (len(sys.argv) == 5 and not wemod_only):
        print(
            "Usage: wemod_bridge.py PROTON WEMOD_EXE GAME_ARGUMENTS_JSON "
            "[--wemod-only]",
            file=sys.stderr,
        )
        return 2
    proton, wemod_executable, encoded_arguments = sys.argv[1:4]
    game_arguments = json.loads(encoded_arguments)
    if not isinstance(game_arguments, list) or not all(
        isinstance(item, str) for item in game_arguments
    ):
        print("Invalid game argument list", file=sys.stderr)
        return 2

    game_environment = dict(os.environ)
    display_notice = game_environment.pop(DISPLAY_NOTICE_VARIABLE, "")
    encoded_wrappers = game_environment.pop(GAME_WRAPPER_VARIABLE, "[]")
    try:
        game_wrappers = json.loads(encoded_wrappers)
    except json.JSONDecodeError:
        print("Invalid game wrapper argument list", file=sys.stderr)
        return 2
    if not isinstance(game_wrappers, list) or not all(
        isinstance(item, str) for item in game_wrappers
    ):
        print("Invalid game wrapper argument list", file=sys.stderr)
        return 2
    encoded_game_environment = game_environment.pop(GAME_ENVIRONMENT_VARIABLE, "[]")
    try:
        game_only_environment = json.loads(encoded_game_environment)
    except json.JSONDecodeError:
        print("Invalid game-only environment list", file=sys.stderr)
        return 2
    if not isinstance(game_only_environment, list) or not all(
        isinstance(item, str) for item in game_only_environment
    ):
        print("Invalid game-only environment list", file=sys.stderr)
        return 2
    prefix = Path(game_environment.get("STEAM_COMPAT_DATA_PATH", ""))
    if display_notice:
        print(
            "Native Wayland disabled because WeMod and the game share one Wine server",
            flush=True,
        )
    marker = prefix / "pfx" / ".wemod_installer"
    wemod_overrides = game_environment.pop(WEMOD_VARIABLE, "")
    steam_library = game_environment.pop(STEAM_LIBRARY_VARIABLE, "")
    steam_app_id = game_environment.pop(STEAM_APP_ID_VARIABLE, "")
    wemod_environment = dict(game_environment)
    for name in game_only_environment:
        wemod_environment.pop(name, None)
    if wemod_overrides:
        wemod_environment["WINEDLLOVERRIDES"] = wemod_overrides
    else:
        wemod_environment.pop("WINEDLLOVERRIDES", None)

    # Steam's native overlay belongs in the game, not Electron. Injecting its
    # preload libraries into WeMod can prevent the Electron window from opening.
    for name in (
        "LD_PRELOAD",
        "SteamAppId",
        "SteamGameId",
        "SteamOverlayGameId",
    ):
        wemod_environment.pop(name, None)

    if not marker.is_file():
        initialized = _initialize_prefix(
            proton,
            Path(wemod_executable),
            game_environment,
            wemod_environment,
        )
        if not initialized or not marker.is_file():
            print(
                "WeMod prefix initialization did not create " + str(marker),
                file=sys.stderr,
            )
            return 2
        print("WeMod prefix initialization completed", flush=True)

    if steam_library:
        library = Path(steam_library)
        if not wemod_only:
            try:
                retry_environment = dict(wemod_environment)
                configured = _configure_steam_retry_environment(
                    prefix,
                    game_arguments,
                    game_environment,
                    retry_environment,
                    steam_app_id,
                )
                if not configured:
                    raise OSError("the selected launch mode cannot be retried")
                _prepare_steam_retry_helper(library)
                wemod_environment.clear()
                wemod_environment.update(retry_environment)
            except OSError as error:
                print(
                    "Warning: WeMod can detect this Steam game but cannot "
                    f"restart it: {error}",
                    file=sys.stderr,
                )
        try:
            _remove_legacy_steam_retry_directory(prefix)
        except OSError as error:
            print(
                f"Warning: could not remove the old WeMod Steam view: {error}",
                file=sys.stderr,
            )
        _register_steam_library(
            proton,
            prefix,
            library,
            wemod_environment,
        )

    mapping = None
    if not steam_library and not wemod_only:
        mapping = _custom_game_mapping(
            Path(wemod_executable),
            prefix,
            game_arguments,
        )
        if mapping:
            print(
                "Opening the saved WeMod game "
                f"(title {mapping.title_id}, game {mapping.game_id})",
                flush=True,
            )
        else:
            print(
                "No WeMod match is saved for this executable. Select it once "
                "in WeMod; Proton Launcher will remember the match.",
                flush=True,
            )

    game_overrides = game_environment.get("WINEDLLOVERRIDES", "<unset>")
    effective_wemod_overrides = wemod_environment.get("WINEDLLOVERRIDES", "<unset>")
    print(f"Game WINEDLLOVERRIDES: {game_overrides}", flush=True)
    print(f"WeMod WINEDLLOVERRIDES: {effective_wemod_overrides}", flush=True)

    wemod_command = [
        proton,
        "run",
        wemod_executable,
        *WEMOD_RENDER_ARGUMENTS,
    ]
    if mapping:
        wemod_command.append(mapping.uri)
    print("WeMod: $ " + " ".join(wemod_command), flush=True)
    baseline = set(_wemod_processes())
    wemod = subprocess.Popen(
        wemod_command,
        env=wemod_environment,
        cwd=str(Path(wemod_executable).parent),
    )

    # A fixed sleep races Electron startup: depending on shader caches, network,
    # and prefix maintenance, ten seconds may be either excessive or too short.
    # The renderer is the first stable indication that the UI and its IPC backend
    # exist. Give that renderer a short settling period before exposing the game.
    if _wait_until_wemod_ready(wemod, baseline):
        print("WeMod renderer is ready", flush=True)
    elif wemod.poll() is not None:
        print(
            f"WeMod exited during startup with code {wemod.returncode}",
            file=sys.stderr,
        )
        return wemod.returncode or 1
    else:
        print(
            "Warning: could not confirm WeMod renderer readiness within "
            f"{WEMOD_READY_TIMEOUT:.0f} seconds"
            + ("" if wemod_only else "; launching the game anyway"),
            file=sys.stderr,
            flush=True,
        )

    if wemod_only:
        print("WeMod is running in the selected prefix", flush=True)
        return wemod.wait()

    if not steam_library and mapping is None:
        mapping = _custom_game_mapping(
            Path(wemod_executable),
            prefix,
            game_arguments,
            use_cache=False,
        )
        if mapping:
            activation_command = [proton, "run", wemod_executable, mapping.uri]
            print("WeMod title: $ " + " ".join(activation_command), flush=True)
            subprocess.Popen(
                activation_command,
                env=wemod_environment,
                cwd=str(Path(wemod_executable).parent),
            )

    game_command = [*game_wrappers, proton, *game_arguments]
    print("Game: $ " + " ".join(game_command), flush=True)
    game = subprocess.Popen(game_command, env=game_environment)
    return_code = game.wait()
    if not steam_library:
        learned = _custom_game_mapping(
            Path(wemod_executable),
            prefix,
            game_arguments,
            use_cache=False,
        )
        if learned and learned != mapping:
            print(
                "Saved the WeMod match for " + Path(learned.executable).name,
                flush=True,
            )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
