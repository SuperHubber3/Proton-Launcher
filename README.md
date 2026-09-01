# Proton Launcher

Proton Launcher runs Windows games, tools, trainers, patchers, and Wine
commands inside Steam Proton prefixes. It finds Steam games, non-Steam
shortcuts, libraries, Proton builds, and existing prefix metadata, so most
launches only need a game and an executable.

Use it when Steam's Play button is too limited: choose a Proton build, set
environment variables, run as a Wine administrator, start WeMod, inject the
Steam overlay, or launch a second program after the game starts.

## Features

- Finds Steam games in every detected library and filters runtimes,
  redistributables, and Proton packages from the list.
- Reads Steam's named launch options and lets you choose the game, launcher, or
  configuration tool before a direct launch.
- Reads non-Steam shortcuts from each Steam user's `shortcuts.vdf`.
- Shows ProtonDB ratings for Steam games and non-Steam shortcuts with a local
  Steam App ID.
- Finds Steam-managed, community, and system Proton installations, including
  tools under `steamapps/common`.
- Uses the Proton recorded in an existing non-Steam prefix when possible.
- Provides an autosaved Default profile plus named profiles for each game.
- Accepts executables, Wine commands, Linux paths, and `Z:\...` working
  directories.
- Supports Wine administrator launches, Steam launches, direct Steam overlay
  injection, online-fix overrides, WeMod, and delayed follow-ups.
- Adds per-profile GameMode, MangoHud, Gamescope, native Wayland, GPU, input,
  HDR, and compatibility switches to direct launches.
- Keeps game and follow-up sessions available after the window closes, with
  separate Stop controls and an optional system tray.
- Lets you open, replace, or delete a prefix from the toolbar.
- Switches between accounts already saved by Steam and can disable Steam's
  shader pre-caching during the switch.

## Requirements

- Linux with a native Steam installation
- Python 3.10 or newer
- [PySide6](https://pypi.org/project/PySide6/) 6.7 or newer
- [vdf](https://pypi.org/project/vdf/) 3.4 or newer
- At least one Proton installation

GameMode, MangoHud, and Gamescope are optional. Their toggles are disabled when
the matching host command is not installed.

A running systemd user manager is recommended. Proton Launcher uses transient
user services to track the full process tree when they are available. It falls
back to process groups and `/proc` tracking otherwise.

## Install and run

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
./run.sh
```

Install the desktop entry with:

```bash
./install-desktop-entry.sh
```

Run that command again if you move the repository. `run.sh` builds the bundled
administrator helper when its source changes; rebuilding it requires
`winegcc`.

## First launch

1. Select a game.
2. Keep **Launcher default** or choose a Proton build.
3. Check the detected executable and working directory.
4. Add arguments or environment variables if the game needs them.
5. Click **Launch**.

Every game has a Default profile. Changes to it save automatically after a
short delay. Named profiles save only when you click **Save**, which makes them
useful for alternate launch setups.

The launcher default follows Steam's selected compatibility tool unless you
choose a fixed Proton build in **Settings > General**. A profile can override
that choice. Select **No Proton (native Linux)** to run a Linux executable or
command directly. Wine-only options are disabled in this mode; GameMode,
MangoHud, Gamescope, environment variables, and host-side working directories
remain available.

## Launch options

### Executables and commands

Steam games with multiple Windows launch entries show a **Steam launch option**
selector above the executable path. Choosing an entry fills its executable,
arguments, and working directory. Editing those fields switches the selector to
**Custom executable**. This controls direct launches; **Launch through Steam**
still leaves the choice to Steam.

Executable mode runs:

```text
<proton>/proton run <executable> <arguments...>
```

Command mode uses Proton's prefix command support. The Explorer, Winecfg,
Regedit, and CMD buttons fill in common commands. `wine explorer` and
`explorer` are treated the same because Proton already supplies Wine.

Arguments are parsed without a shell. Shell operators such as `|`, `&&`, `>`,
wildcards, and `$()` are passed as ordinary text rather than executed.

### Working directory

Linux and Wine paths are both accepted:

```text
/home/alice/Games/Example
Z:\home\alice\Games\Example
```

Wine paths are resolved through the selected prefix's drive mappings. Explorer
receives `.` when no target is given, so it opens in the chosen directory.

### Environment variables

Enter one assignment per line:

```text
DXVK_HUD=fps
WINEDLLOVERRIDES="d3d8=n,b;msvcrt=n,b"
TOKEN=a=b=c
```

Only the first `=` separates the name and value. Matching outer quotes are
removed before launch. Quotes are optional for spaces, commas, and semicolons
because the values do not pass through a shell.

### Run as administrator

**Run as administrator** uses Wine's Windows `runas` behavior. It does not run
the game or Proton Launcher as Linux root. The bundled x86-64 helper can also
start 32-bit Windows games in a normal Proton WoW64 prefix.

### GameMode, MangoHud, Gamescope, and Wayland

The **Launch options** row applies to direct launches and is saved with the
profile:

- **GameMode** runs the game through `gamemoderun`.
- **MangoHud** wraps the Proton command. With Gamescope enabled, the launcher
  uses Gamescope's `--mangoapp` integration instead.
- **Gamescope** provides window mode, game and output resolutions, refresh and
  FPS limits, scaling, FSR/NIS filters, sharpness, adaptive sync, HDR, and an
  extra-arguments field under **Configure**.
- **Native Wayland** enables Proton's Wine-Wayland driver. Support depends on
  the selected Proton build. The Steam overlay and Steam Input may not work on
  this display path.

**Configure** also contains discrete-GPU preference, HDR, forced NVAPI, raw
Wayland mouse input, SDL controller input, and DXVK HUD presets. The
troubleshooting tab can disable Esync or Fsync, replace DXVK with WineD3D,
write a Proton log, force large-address-aware mode, or set Wine debug channels.
These settings are left off by default because they fix specific compatibility
problems rather than improve every game.

HDR enables native Wayland as well as Proton and Gamescope HDR flags. Discrete
GPU preference runs the command through `switcherooctl`, which selects the
system's first discrete GPU. Custom environment entries still apply; an
enabled toggle takes precedence when both set the same variable.

## Steam and overlay options

**Switch account** lists the accounts already saved by Steam. Proton Launcher
closes Steam, changes its saved-account selector, and restarts it. It does not
store passwords or session tokens. Steam asks for a sign-in when the selected
account has no remembered login.

Steam can queue the same shader depots again after an account change. Enable
**Disable Steam shader pre-caching** in the switch dialog to stop those
downloads. Games will compile missing shaders during play instead, which can
cause stutter. The setting belongs to Steam and remains in effect until you
turn it back off in the same dialog or in Steam's settings.

Small game updates after a switch are normal Steam depot updates when the
installed build is behind the current manifest. Account switching does not
change appmanifest state or suppress game updates.

**Launch through Steam** asks Steam to start the selected game or shortcut.
Steam then owns the Proton choice, launch options, and game process.

**Skip update** sets the selected Steam game's appmanifest `StateFlags` value
to `4`. Exit Steam before using it; a running Steam client can overwrite the
manifest. **Tools > Skip all updates** applies the same edit to every installed
Steam game found by the launcher.

An already-running Steam client cannot inherit new environment variables from
Proton Launcher. Use **Tools > Copy Steam Launch Options** and paste the result
into the game's Steam launch options when those variables must apply to a
Steam-managed launch.

**Inject Steam overlay into direct launch** loads Steam's 32-bit and 64-bit
overlay renderers and sets the selected App ID. Steam must be running. Direct
injection is experimental and can fail with some graphics paths, launchers, or
anti-cheat systems.

**Apply online-fix overrides** sets:

```text
WINEDLLOVERRIDES=OnlineFix64=n;SteamOverlay64=n;winmm=n,b;dnet=n;steam_api64=n;winhttp=n,b
```

Enabling it also enables direct overlay injection. You can turn the overlay
back off without changing the overrides.

## WeMod

Set the wemod-launcher executable under **Settings > Integrations**, then
enable **Launch with WeMod** in a profile. Proton Launcher prepares an empty
prefix with the selected Proton build when needed, starts WeMod, waits for its
Electron renderer, and starts the game in the same prefix.

**Launch WeMod** opens WeMod by itself in the selected game's prefix. The same
button also prepares an uninitialized prefix before opening WeMod.

For Steam games, Proton Launcher registers the selected library's real Wine
path so WeMod sees the same executable path as the running game. A managed
`Steam.exe` retry helper in that library lets WeMod relaunch the game after a
crash. It uses the profile's executable, arguments, working directory, and game
DLL overrides without asking the native Steam client to start an App ID that it
still considers active. Proton Launcher refuses to replace an unmanaged
`Steam.exe`; managed files are identified by `.proton-launcher-steam-retry`.

For a non-Steam game, find the title in WeMod and select the game's executable
once. Proton Launcher reads that saved association after the game closes. On
later launches, WeMod opens the matching title and edition automatically.
Proton Launcher keeps the learned IDs in
`~/.config/proton-launcher/wemod-games.json`; it does not write to WeMod's
Chromium database.

During setup, wemod-launcher can offer to copy a compatible setup from another
initialized prefix in the same `compatdata` directory. **Delete WeMod** removes
the selected prefix's WeMod marker and local data link so setup runs again. It
also removes Proton Launcher's managed retry helper from the selected Steam
library. The game prefix, saves, shared login data, and WeMod installation are
kept. .NET files and registry changes remain because they cannot be separated
safely from the Wine prefix.

The game and WeMod use separate environments. Steam overlay preload variables
are kept out of WeMod's Electron process so they cannot block its window.

If an embedded WeMod map freezes or turns the window black, use **Settings >
Integrations > Open maps in browser**. The reversible patch sends the same map
to the system browser and keeps a backup for **Restore in-app maps**.

The configured wemod-launcher installation must contain `WeMod.exe`. Its setup
prompts may appear the first time a prefix is prepared. WeMod can be used only
with a direct executable launch, not with **Launch through Steam** or **Run as
administrator**.

## Follow-up launch

Enable **Follow-up launch** to start another executable or command after a
process appears in the selected prefix.

- **Wait for** matches an exact executable name, case-insensitively.
- **Use first executable** fills that name from the main launch target.
- **Then wait** adds a delay after the process appears.
- **Launch follow-up now** skips detection and the delay.

The game and follow-up have separate Stop buttons. **Stop Game** also cancels a
follow-up that is still waiting, but it does not stop one that has already
started.

## Prefixes and sessions

The toolbar can set a custom prefix, open the current prefix, or delete it.
Deleting a prefix can remove prefix-local saves and settings, so the launcher
shows the exact path and asks for confirmation first.

With systemd supervision, detached Proton and Wine descendants remain in the
session's control group and respond to Stop. The fallback backend cannot always
contain a program that deliberately starts a completely separate session.

Session records and logs are stored under `$XDG_STATE_HOME/proton-launcher`.
The default is:

```text
~/.local/state/proton-launcher/
```

Active sessions are restored when the GUI starts again. When closing the
window, you can hide to the tray, keep sessions running, stop everything, or
cancel.

Each game has its own console and session controls. Switching games changes the
visible output and the target of **Stop Game**, **Stop Follow-up**, **Stop All
for Game**, **Clear**, and **Copy**. Sessions for other games keep running, and
they do not disable **Launch** for the selected game. Use **Tools > Stop all
running sessions** when you need to stop every game at once.

## Discovery and settings

Default Steam and Proton locations include:

```text
~/.local/share/Steam
~/.steam/steam
~/.local/share/Steam/compatibilitytools.d
/usr/share/steam/compatibilitytools.d
<steam-library>/steamapps/common
```

Use **Settings > Locations** to add, edit, remove, or open custom Steam roots,
libraries, and Proton locations. Detected entries are shown separately and are
read-only.

The config file is stored at:

```text
~/.config/proton-launcher/config.json
```

It is validated before use and saved atomically with rotating backups. Safe
problems are repaired automatically. If a value needs a decision, the launcher
shows the failing field and offers repair, backup restore, manual editing,
temporary defaults, or exit. Environment values are plain text, so do not put
secrets in profiles.

## Development

```bash
python3 -m pip install -r requirements-dev.txt
black proton_launcher tests
ruff check proton_launcher tests
python3 -m unittest discover -v
bash -n run.sh build-helper.sh build-steam-helper.sh install-desktop-entry.sh
```

GitHub Actions checks formatting, lint, shell syntax, and tests on Python 3.10
and 3.13.

After changing `helpers/steam-retry-helper.c`, rebuild the bundled PE with
`./build-steam-helper.sh`. It requires Clang, LLD, and Wine development files.

## License

Proton Launcher is licensed under the [GNU General Public License v3.0](LICENSE)
(`GPL-3.0-only`).
