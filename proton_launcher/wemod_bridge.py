# SPDX-License-Identifier: GPL-3.0-only
"""Start WeMod and a game with separate pre-Proton environments."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

WEMOD_VARIABLE = "PL_WEMOD_WINEDLLOVERRIDES"
WEMOD_READY_TIMEOUT = 45.0
WEMOD_READY_SETTLE_TIME = 2.0


def reset_wemod_prefix(prefix: Path) -> list[Path]:
    """Remove WeMod's prefix marker and its prefix-local data link."""
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
    return removed


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
    bootstrap_environment = dict(game_environment)
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
    if len(sys.argv) != 4:
        print(
            "Usage: wemod_bridge.py PROTON WEMOD_EXE GAME_ARGUMENTS_JSON",
            file=sys.stderr,
        )
        return 2
    proton, wemod_executable, encoded_arguments = sys.argv[1:]
    game_arguments = json.loads(encoded_arguments)
    if not isinstance(game_arguments, list) or not all(
        isinstance(item, str) for item in game_arguments
    ):
        print("Invalid game argument list", file=sys.stderr)
        return 2

    game_environment = dict(os.environ)
    prefix = Path(game_environment.get("STEAM_COMPAT_DATA_PATH", ""))
    marker = prefix / "pfx" / ".wemod_installer"
    wemod_overrides = game_environment.pop(WEMOD_VARIABLE, "")
    wemod_environment = dict(game_environment)
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

    game_overrides = game_environment.get("WINEDLLOVERRIDES", "<unset>")
    effective_wemod_overrides = wemod_environment.get("WINEDLLOVERRIDES", "<unset>")
    print(f"Game WINEDLLOVERRIDES: {game_overrides}", flush=True)
    print(f"WeMod WINEDLLOVERRIDES: {effective_wemod_overrides}", flush=True)

    wemod_command = [
        proton,
        "run",
        wemod_executable,
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-gpu-compositing",
    ]
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
            f"{WEMOD_READY_TIMEOUT:.0f} seconds; launching the game anyway",
            file=sys.stderr,
            flush=True,
        )

    game_command = [proton, *game_arguments]
    print("Game: $ " + " ".join(game_command), flush=True)
    game = subprocess.Popen(game_command, env=game_environment)
    return game.wait()


if __name__ == "__main__":
    raise SystemExit(main())
