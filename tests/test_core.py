# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import json
import os
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import vdf

from proton_launcher.models import (
    DEFAULT_NON_STEAM_WINEDLLOVERRIDES,
    DEFAULT_WEMOD_WINEDLLOVERRIDES,
    GameEntry,
    GameSource,
    LaunchProfile,
    SteamLaunchOption,
)
from proton_launcher.process_watcher import (
    command_matches,
    find_matching_pids,
    primary_executable_name,
)
from proton_launcher.profiles import ConfigStore
from proton_launcher.proton import discover_protons
from proton_launcher.protondb import (
    CACHE_MAX_AGE_SECONDS,
    ProtonDBCache,
    game_url,
    parse_rating,
    protondb_app_id,
    summary_url,
)
from proton_launcher.runner import (
    WEMOD_GAME_WRAPPER_VARIABLE,
    WEMOD_STEAM_APP_ID_VARIABLE,
    WEMOD_STEAM_LIBRARY_VARIABLE,
    build_followup_launch_spec,
    build_launch_spec,
    build_steam_launch_spec,
    build_wemod_launch_spec,
    clean_process_output,
    prepare_compatdata_directory,
    process_environment,
    resolve_working_directory,
    unquote_environment_value,
)
from proton_launcher.steam import (
    is_component,
    parse_appinfo_launches,
    parse_manifest,
    parse_shortcuts,
    resolve_game_executable,
)
from proton_launcher.wemod_bridge import (
    STEAM_RETRY_NATIVE_APP_ID,
    STEAM_RETRY_NATIVE_OVERRIDE,
    WEMOD_RENDER_ARGUMENTS,
    _configure_steam_retry_environment,
    _custom_game_mapping,
    _initialize_prefix,
    _initializer_command,
    _prepare_steam_retry_helper,
    _register_steam_library,
    _selected_proton_version,
    _wemod_processes,
    reset_wemod_prefix,
)
from proton_launcher.wemod_bridge import (
    main as wemod_bridge_main,
)
from proton_launcher.wemod_map import (
    MAP_ANALYTICS,
    MAP_BROWSER,
    MAP_BROWSER_HOOK,
    MAP_IFRAME,
    apply_map_browser_patch,
    map_patch_backup,
    map_patch_state,
    restore_wemod_maps,
)
from proton_launcher.wemod_state import (
    WeModGameMapping,
    find_custom_mapping,
    load_cached_mapping,
    read_global_store,
    save_cached_mapping,
)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_protondb_urls_and_rating(self):
        self.assertEqual(
            summary_url(1593500),
            "https://www.protondb.com/api/v1/reports/summaries/1593500.json",
        )
        self.assertEqual(game_url(1593500), "https://www.protondb.com/app/1593500")
        self.assertEqual(parse_rating(b'{"tier":"gold"}'), "Gold")
        self.assertIsNone(parse_rating(b'{"tier":null}'))
        self.assertIsNone(parse_rating(b"not json"))

    def test_protondb_cache_persists_ratings_and_unrated_results(self):
        path = self.tmp / "cache" / "protondb.json"
        cache = ProtonDBCache(path)
        fetched_at = time.time()
        cache.put(1593500, "Gold", fetched_at=fetched_at)
        cache.put(42, None, fetched_at=fetched_at)

        loaded = ProtonDBCache(path)

        self.assertEqual(
            loaded.lookup(1593500, now=fetched_at + 1), (True, "Gold", True)
        )
        self.assertEqual(loaded.lookup(42, now=fetched_at + 1), (True, None, True))

    def test_protondb_cache_marks_old_values_stale(self):
        cache = ProtonDBCache(self.tmp / "protondb.json")
        cache.put(1593500, "Gold", fetched_at=1_000)

        self.assertEqual(
            cache.lookup(1593500, now=1_000 + CACHE_MAX_AGE_SECONDS + 1),
            (True, "Gold", False),
        )

    def test_empty_xdg_cache_home_uses_home_cache(self):
        home = self.tmp / "home"
        with patch.dict(os.environ, {"HOME": str(home), "XDG_CACHE_HOME": ""}):
            cache = ProtonDBCache()

        self.assertEqual(
            cache.path, home / ".cache" / "proton-launcher" / "protondb.json"
        )

    def test_non_steam_protondb_id_prefers_online_fix_real_app_id(self):
        game_dir = self.tmp / "Game"
        game_dir.mkdir()
        executable = game_dir / "Game.exe"
        executable.touch()
        metadata = game_dir / "Game" / "Binaries" / "Win64"
        metadata.mkdir(parents=True)
        (metadata / "OnlineFix.ini").write_text(
            "[Main]\nRealAppId=3844970\nFakeAppId=480\n"
        )
        (game_dir / "steam_appid.txt").write_text("1234\n")
        game = GameEntry(
            GameSource.SHORTCUT,
            42,
            "Example",
            self.tmp / "Steam",
            self.tmp,
            shortcut_exe=str(executable),
        )

        self.assertEqual(protondb_app_id(game), 3844970)

    def test_non_steam_protondb_id_falls_back_to_steam_appid(self):
        game_dir = self.tmp / "Game"
        game_dir.mkdir()
        executable = game_dir / "Game.exe"
        executable.touch()
        (game_dir / "onlinefix.INI").write_text("[MAIN]\nFakeAppId=480\n")
        (game_dir / "STEAM_APPID.TXT").write_text("  1593500\n")
        game = GameEntry(
            GameSource.SHORTCUT,
            42,
            "Example",
            self.tmp / "Steam",
            self.tmp,
            shortcut_exe=str(executable),
        )

        self.assertEqual(protondb_app_id(game), 1593500)
        (game_dir / "STEAM_APPID.TXT").write_text("\u00b2\n")
        self.assertIsNone(protondb_app_id(game))

    def test_bundled_steam_retry_helper_is_x64_pe(self):
        helper = (
            Path(__file__).resolve().parent.parent
            / "helpers"
            / "steam-retry-helper.exe"
        )
        data = helper.read_bytes()
        self.assertEqual(data[:2], b"MZ")
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
        self.assertEqual(data[pe_offset : pe_offset + 4], b"PE\0\0")
        self.assertEqual(struct.unpack_from("<H", data, pe_offset + 4)[0], 0x8664)

    def test_wemod_does_not_force_a_graphics_backend(self):
        self.assertEqual(
            WEMOD_RENDER_ARGUMENTS,
            ("--enable-logging=file",),
        )
        self.assertFalse(
            any(
                argument.startswith(("--disable-gpu", "--use-angle"))
                for argument in WEMOD_RENDER_ARGUMENTS
            )
        )

    def test_wemod_browser_map_patch_is_reversible(self):
        asar = self.tmp / "app.asar"
        original = b"before" + MAP_IFRAME + MAP_ANALYTICS + b"after"
        asar.write_bytes(original)
        map_patch_backup(asar).write_bytes(
            b"stale" + MAP_IFRAME + MAP_ANALYTICS + b"backup"
        )

        self.assertEqual(map_patch_state(asar), "available")
        self.assertTrue(apply_map_browser_patch(asar))
        self.assertEqual(map_patch_state(asar), "patched")
        self.assertIn(MAP_BROWSER, asar.read_bytes())
        self.assertIn(MAP_BROWSER_HOOK, asar.read_bytes())
        self.assertEqual(map_patch_backup(asar).read_bytes(), original)

        self.assertTrue(restore_wemod_maps(asar))
        self.assertEqual(map_patch_state(asar), "available")
        self.assertEqual(asar.read_bytes(), original)
        self.assertFalse(map_patch_backup(asar).exists())

        with patch(
            "proton_launcher.wemod_map.map_patch_state",
            side_effect=["available", "unsupported"],
        ):
            with self.assertRaisesRegex(OSError, "could not be verified"):
                apply_map_browser_patch(asar)
        self.assertEqual(asar.read_bytes(), original)

    def test_process_environment_preserves_vulkan_driver_discovery(self):
        with patch.dict(
            "proton_launcher.runner.os.environ",
            {"XDG_DATA_DIRS": "/opt/example/usr/share"},
            clear=True,
        ):
            environment = process_environment()

        self.assertEqual(
            environment["XDG_DATA_DIRS"],
            "/opt/example/usr/share:/usr/local/share:/usr/share",
        )

    def test_process_environment_does_not_duplicate_system_data_dirs(self):
        with patch.dict(
            "proton_launcher.runner.os.environ",
            {"XDG_DATA_DIRS": "/usr/share:/usr/local/share"},
            clear=True,
        ):
            environment = process_environment()

        self.assertEqual(
            environment["XDG_DATA_DIRS"],
            "/usr/share:/usr/local/share",
        )

    def test_wemod_process_scan_identifies_electron_roles(self):
        proc = self.tmp / "proc"
        renderer = proc / "120"
        unrelated = proc / "121"
        renderer.mkdir(parents=True)
        unrelated.mkdir()
        (renderer / "cmdline").write_bytes(
            b"C:\\Program Files\\WeMod\\WeMod.exe\0--type=renderer\0"
        )
        (unrelated / "cmdline").write_bytes(b"/usr/bin/python3\0script.py\0")

        self.assertEqual(
            _wemod_processes(proc),
            {120: r"C:\Program Files\WeMod\WeMod.exe --type=renderer"},
        )

    def test_wemod_custom_match_is_read_from_saved_feedback(self):
        executable = self.tmp / "Games" / "Big Walk" / "Big Walk.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        state = {
            "catalog": {"games": {"122174": {"id": "122174", "titleId": "112621"}}},
            "installedApps": {},
            "trainerFeedbackRequests": {
                r"ß:CuStOm:122174_z:\home\zenny\games\big walk\big walk.exe:1779869447": []
            },
        }

        mapping = find_custom_mapping(
            state,
            executable,
            r"Z:\home\zenny\games\big walk\big walk.exe",
        )

        self.assertEqual(
            mapping,
            WeModGameMapping(str(executable), "112621", "122174"),
        )
        self.assertEqual(
            mapping.uri,
            "wemod://titles/112621?gameId=122174",
        )

    def test_wemod_mapping_cache_round_trip(self):
        executable = self.tmp / "game.exe"
        executable.touch()
        cache = self.tmp / "wemod-games.json"
        mapping = WeModGameMapping(str(executable), "12", "34")

        save_cached_mapping(mapping, cache)

        self.assertEqual(load_cached_mapping(executable, cache), mapping)

    def test_wemod_global_store_is_read_from_leveldb_log(self):
        leveldb = self.tmp / "leveldb"
        leveldb.mkdir()
        state = {"installedApps": {}, "catalog": {"games": {}}}
        key = b"_file://\x00\x01infinity:globalStore"
        value = b"\x00" + json.dumps(state).encode("utf-16le")

        def varint(number):
            encoded = bytearray()
            while number >= 0x80:
                encoded.append((number & 0x7F) | 0x80)
                number >>= 7
            encoded.append(number)
            return bytes(encoded)

        batch = (
            struct.pack("<QI", 9, 1)
            + b"\x01"
            + varint(len(key))
            + key
            + varint(len(value))
            + value
        )
        physical = struct.pack("<IHB", 0, len(batch), 1) + batch
        (leveldb / "000001.log").write_bytes(physical)

        self.assertEqual(read_global_store(leveldb), state)

    def test_wemod_custom_mapping_prefers_cache(self):
        executable = self.tmp / "game.exe"
        executable.touch()
        prefix = self.tmp / "prefix"
        cached = WeModGameMapping(str(executable), "12", "34")

        with (
            patch(
                "proton_launcher.wemod_bridge.load_cached_mapping",
                return_value=cached,
            ),
            patch("proton_launcher.wemod_bridge.discover_custom_mapping") as discover,
        ):
            mapping = _custom_game_mapping(
                self.tmp / "WeMod.exe",
                prefix,
                ["run", str(executable)],
            )

        self.assertEqual(mapping, cached)
        discover.assert_not_called()

    def test_reset_wemod_prefix_keeps_game_files_and_shared_data(self):
        prefix = self.tmp / "compatdata" / "42"
        marker = prefix / "pfx" / ".wemod_installer"
        marker.parent.mkdir(parents=True)
        marker.touch()
        shared_data = self.tmp / "wemod_login"
        shared_data.mkdir()
        (shared_data / "account.json").touch()
        prefix_data = (
            prefix
            / "pfx"
            / "drive_c"
            / "users"
            / "steamuser"
            / "AppData"
            / "Roaming"
            / "WeMod"
        )
        prefix_data.parent.mkdir(parents=True)
        prefix_data.symlink_to(shared_data, target_is_directory=True)
        retry_root = prefix / "pfx" / "drive_c" / "ProtonLauncher" / "Steam"
        retry_root.mkdir(parents=True)
        (retry_root / "Steam.exe").touch()
        library = self.tmp / "library"
        library.mkdir()
        retry_helper = library / "Steam.exe"
        retry_marker = library / ".proton-launcher-steam-retry"
        retry_helper.touch()
        retry_marker.write_text("Managed by Proton Launcher.\n")
        game_file = prefix / "pfx" / "drive_c" / "game" / "save.dat"
        game_file.parent.mkdir(parents=True)
        game_file.touch()

        removed = reset_wemod_prefix(prefix, library)

        self.assertEqual(
            removed,
            [marker, prefix_data, retry_root, retry_helper, retry_marker],
        )
        self.assertFalse(marker.exists())
        self.assertFalse(prefix_data.exists())
        self.assertFalse(retry_root.exists())
        self.assertFalse(retry_helper.exists())
        self.assertFalse(retry_marker.exists())
        self.assertTrue(shared_data.is_dir())
        self.assertTrue(game_file.is_file())

        unmanaged_library = self.tmp / "unmanaged-library"
        unmanaged_library.mkdir()
        unmanaged_steam = unmanaged_library / "Steam.exe"
        unmanaged_steam.write_bytes(b"unmanaged")
        self.assertEqual(reset_wemod_prefix(prefix, unmanaged_library), [])
        self.assertEqual(unmanaged_steam.read_bytes(), b"unmanaged")

    def test_wemod_initializer_scans_sibling_prefixes(self):
        tool = self.tmp / "Proton"
        tool.mkdir()
        proton = tool / "proton"
        proton.touch()
        (tool / "version").write_text("GE-Proton9-1\n")
        prefix = self.tmp / "compatdata" / "42"
        prefix.mkdir(parents=True)
        wemod = self.tmp / "wemod-launcher" / "src" / "wemod.py"
        wemod.parent.mkdir(parents=True)
        wemod.touch()
        environment = {"STEAM_COMPAT_DATA_PATH": str(prefix)}

        with (
            patch(
                "proton_launcher.wemod_bridge._initializer_command",
                return_value=["initializer"],
            ),
            patch("proton_launcher.wemod_bridge.subprocess.run") as run,
        ):
            run.return_value.returncode = 0
            initialized = _initialize_prefix(
                str(proton), wemod, environment, environment
            )

        self.assertTrue(initialized)
        self.assertEqual(
            run.call_args_list[1].kwargs["env"]["SCANFOLDER"], str(prefix.parent)
        )

    def test_wemod_steam_detection_registers_mapped_library(self):
        prefix = self.tmp / "compatdata" / "42"
        dosdevices = prefix / "pfx" / "dosdevices"
        dosdevices.mkdir(parents=True)
        (dosdevices / "d:").symlink_to(self.tmp, target_is_directory=True)
        library = self.tmp / "Steam Library"
        library.mkdir()
        environment = {"TEST": "value", "WINEDLLOVERRIDES": "version=n,b"}

        with patch("proton_launcher.wemod_bridge.subprocess.run") as run:
            run.return_value.returncode = 0
            configured = _register_steam_library("proton", prefix, library, environment)

        self.assertTrue(configured)
        self.assertEqual(
            run.call_args.args[0],
            [
                "proton",
                "runinprefix",
                "reg",
                "add",
                r"HKLM\Software\Valve\Steam",
                "/v",
                "InstallPath",
                "/t",
                "REG_SZ",
                "/d",
                r"D:\Steam Library",
                "/f",
                "/reg:32",
            ],
        )
        self.assertEqual(run.call_args.kwargs["env"], {"TEST": "value"})
        self.assertEqual(run.call_args.kwargs["timeout"], 30.0)

        with patch(
            "proton_launcher.wemod_bridge.subprocess.run",
            side_effect=subprocess.TimeoutExpired("reg", 30),
        ):
            self.assertFalse(
                _register_steam_library("proton", prefix, library, environment)
            )

    def test_wemod_steam_retry_helper_is_copied_to_real_library(self):
        library = self.tmp / "library"
        helper = self.tmp / "steam-retry-helper.exe"
        (library / "steamapps").mkdir(parents=True)
        helper.write_bytes(b"helper")

        registered_root = _prepare_steam_retry_helper(library, helper)

        self.assertEqual(registered_root, library.resolve())
        self.assertEqual((library / "Steam.exe").read_bytes(), b"helper")
        self.assertEqual(
            (library / ".proton-launcher-steam-retry").read_text(),
            "Managed by Proton Launcher.\n",
        )

    def test_wemod_steam_retry_helper_does_not_replace_unknown_steam_exe(self):
        library = self.tmp / "library"
        helper = self.tmp / "steam-retry-helper.exe"
        library.mkdir()
        (library / "Steam.exe").write_bytes(b"existing")
        helper.write_bytes(b"helper")

        with self.assertRaisesRegex(OSError, "existing Steam.exe"):
            _prepare_steam_retry_helper(library, helper)

        self.assertEqual(
            (library / "Steam.exe").read_bytes(),
            b"existing",
        )

    def test_wemod_steam_retry_restores_game_launch_environment(self):
        game_environment = {
            "WINEDLLOVERRIDES": "game=n,b",
            "STEAM_COMPAT_DATA_PATH": "/prefix",
        }
        wemod_environment = {"WINEDLLOVERRIDES": "version=n,b"}
        with patch(
            "proton_launcher.wemod_bridge._to_wine_path",
            side_effect=[r"Z:\Games\game.exe", r"Z:\Games"],
        ):
            configured = _configure_steam_retry_environment(
                self.tmp / "prefix",
                ["run", str(self.tmp / "game.exe"), "--name", "two words"],
                game_environment,
                wemod_environment,
                "1593500",
            )

        self.assertTrue(configured)
        self.assertEqual(
            wemod_environment["PL_STEAM_RETRY_TARGET"], r"Z:\Games\game.exe"
        )
        self.assertEqual(
            wemod_environment["PL_STEAM_RETRY_ARGUMENTS"], '--name "two words"'
        )
        self.assertEqual(
            wemod_environment["PL_STEAM_RETRY_WINEDLLOVERRIDES"], "game=n,b"
        )
        self.assertEqual(wemod_environment["PL_STEAM_RETRY_HAS_WINEDLLOVERRIDES"], "1")
        self.assertEqual(wemod_environment["PL_STEAM_RETRY_STEAM_APP_ID"], "1593500")
        self.assertEqual(
            wemod_environment["WINEDLLOVERRIDES"],
            f"version=n,b;{STEAM_RETRY_NATIVE_OVERRIDE}",
        )
        self.assertEqual(wemod_environment["SteamGameId"], STEAM_RETRY_NATIVE_APP_ID)

    def test_wemod_steam_retry_keeps_an_existing_native_steam_override(self):
        wemod_environment = {
            "WINEDLLOVERRIDES": "steam.exe,other=n;version=n,b",
            "PL_STEAM_RETRY_STEAM_APP_ID": "stale",
        }
        with patch(
            "proton_launcher.wemod_bridge._to_wine_path",
            side_effect=[r"Z:\Games\game.exe", r"Z:\Games"],
        ):
            configured = _configure_steam_retry_environment(
                self.tmp / "prefix",
                ["run", str(self.tmp / "game.exe")],
                {},
                wemod_environment,
                "",
            )

        self.assertTrue(configured)
        self.assertEqual(
            wemod_environment["WINEDLLOVERRIDES"],
            "steam.exe,other=n;version=n,b",
        )
        self.assertNotIn("PL_STEAM_RETRY_STEAM_APP_ID", wemod_environment)

    def test_wemod_initializer_uses_launchers_virtual_environment(self):
        root = self.tmp / "wemod-launcher"
        executable = root / "wemod_data" / "wemod_bin" / "WeMod.exe"
        python = root / "src" / "wemod_venv" / "bin" / "python"
        executable.parent.mkdir(parents=True)
        python.parent.mkdir(parents=True)
        executable.touch()
        python.touch()
        (root / "src" / "wemod.py").touch()

        command = _initializer_command(executable, "/tools/proton")

        self.assertEqual(command[0], str(python))
        self.assertEqual(command[-2:], [str(root / "src"), "/tools/proton"])
        self.assertIn("wemod.init", command[2])

    def test_wemod_initializer_uses_selected_proton_version(self):
        root = self.tmp / "DW-Proton"
        root.mkdir()
        proton = root / "proton"
        proton.touch()
        (root / "version").write_text("1785863877 dwproton-11.0-10\n")

        self.assertEqual(_selected_proton_version(proton), "dwproton-11.0-10")

    def test_component_filters(self):
        self.assertTrue(
            is_component(
                228980, "Steamworks Common Redistributables", "Steamworks Shared"
            )
        )
        self.assertTrue(
            is_component(1, "Steam Linux Runtime 4.0", "SteamLinuxRuntime_4")
        )
        self.assertTrue(
            is_component(1493710, "Proton Experimental", "Proton - Experimental")
        )
        self.assertFalse(is_component(1593500, "God of War", "GodOfWar"))

    def test_manifest_parser(self):
        root, library = self.tmp / "Steam", self.tmp / "Library"
        manifest = self.tmp / "appmanifest_42.acf"
        manifest.write_text(
            '"AppState"\n{\n "appid" "42"\n "name" "A Game"\n "installdir" "A Game"\n}\n'
        )
        game = parse_manifest(manifest, root, library)
        self.assertIsNotNone(game)
        self.assertEqual(game.app_id, 42)
        self.assertEqual(game.install_dir, library / "steamapps" / "common" / "A Game")

    def test_manifest_uses_steam_launch_executable(self):
        root, library = self.tmp / "Steam", self.tmp / "Library"
        install = library / "steamapps" / "common" / "A Game"
        install.mkdir(parents=True)
        executable = install / "bin" / "ActualGame.exe"
        executable.parent.mkdir()
        executable.touch()
        manifest = self.tmp / "appmanifest_42.acf"
        manifest.write_text(
            '"AppState"\n{\n "appid" "42"\n "name" "A Game"\n "installdir" "A Game"\n}\n'
        )
        launcher = install / "Launcher.exe"
        launcher.touch()
        game = parse_manifest(
            manifest,
            root,
            library,
            [
                SteamLaunchOption("", r"BIN\actualgame.EXE"),
                SteamLaunchOption("Open launcher", "launcher.exe", "--settings"),
            ],
        )
        self.assertEqual(game.default_executable, str(executable))
        self.assertEqual(
            game.launch_options,
            (
                SteamLaunchOption("Play A Game", str(executable)),
                SteamLaunchOption("Open launcher", str(launcher), "--settings"),
            ),
        )

    def test_appinfo_parser_reads_all_windows_launch_options(self):
        appinfo = {
            "appinfo": {
                "config": {
                    "launch": {
                        "0": {
                            "executable": "Game.exe",
                            "type": "default",
                            "config": {"oslist": "windows"},
                        },
                        "1": {
                            "executable": "Launcher.exe",
                            "arguments": "--settings",
                            "type": "option2",
                            "description": "Open launcher",
                            "config": {"oslist": "windows"},
                        },
                        "2": {
                            "executable": "Game.app",
                            "type": "none",
                            "config": {"oslist": "macos"},
                        },
                        "3": {
                            "executable": "AllPlatforms.exe",
                            "description": "All platforms",
                            "config": {"oslist": ""},
                        },
                    }
                }
            }
        }
        keys: list[str] = []

        def collect(value):
            for key, item in value.items():
                if key not in keys:
                    keys.append(key)
                if isinstance(item, dict):
                    collect(item)

        def encode(value):
            result = bytearray()
            for key, item in value.items():
                if isinstance(item, dict):
                    result.extend(b"\x00" + struct.pack("<i", keys.index(key)))
                    result.extend(encode(item))
                else:
                    result.extend(b"\x01" + struct.pack("<i", keys.index(key)))
                    result.extend(str(item).encode() + b"\0")
            result.extend(b"\x08")
            return bytes(result)

        collect(appinfo)
        payload = encode(appinfo)
        record = struct.pack("<II", 42, 60 + len(payload)) + bytes(60) + payload
        terminator = struct.pack("<II", 0, 0)
        key_table_offset = 16 + len(record) + len(terminator)
        key_table = struct.pack("<I", len(keys)) + b"".join(
            key.encode() + b"\0" for key in keys
        )
        path = self.tmp / "appinfo.vdf"
        path.write_bytes(
            struct.pack("<IIQ", 0x07564429, 1, key_table_offset)
            + record
            + terminator
            + key_table
        )

        self.assertEqual(
            parse_appinfo_launches(path, {42})[42],
            [
                SteamLaunchOption("", "Game.exe"),
                SteamLaunchOption("Open launcher", "Launcher.exe", "--settings"),
                SteamLaunchOption("All platforms", "AllPlatforms.exe"),
            ],
        )

    def test_executable_fallback_is_conservative(self):
        install = self.tmp / "Only Game"
        install.mkdir()
        game = install / "Game.exe"
        game.touch()
        (install / "crash-uploader.exe").touch()
        self.assertEqual(resolve_game_executable(install), str(game))

    def test_shortcut_signed_id(self):
        root = self.tmp / "Steam"
        root.mkdir()
        path = self.tmp / "shortcuts.vdf"
        with path.open("wb") as handle:
            vdf.binary_dump(
                {
                    "shortcuts": {
                        "0": {
                            "appid": -2133027456,
                            "AppName": "Forza",
                            "exe": '"/games/a.exe"',
                            "StartDir": "/games",
                        }
                    }
                },
                handle,
            )
        game = parse_shortcuts(path, root, [root])[0]
        self.assertEqual(game.app_id, 2161939840)
        self.assertEqual(game.shortcut_exe, "/games/a.exe")

    def test_launch_spec(self):
        proton = self.tmp / "proton"
        proton.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        game = GameEntry(GameSource.STEAM, 42, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "Explorer",
            game.key,
            str(proton),
            "command",
            command='wine explorer "C:\\A B"',
            environment_text="FOO=bar\nSTEAM_COMPAT_DATA_PATH=bad",
        )
        spec = build_launch_spec(game, profile)
        self.assertEqual(spec.arguments, ["runinprefix", "explorer", "C:\\A B"])
        self.assertEqual(spec.environment["FOO"], "bar")
        self.assertTrue(
            spec.environment["STEAM_COMPAT_DATA_PATH"].endswith("compatdata/42")
        )

    def test_visible_cmd_preset_uses_wineconsole(self):
        proton = self.tmp / "proton"
        proton.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        game = GameEntry(GameSource.STEAM, 42, "Game", steam, steam)
        profile = LaunchProfile(
            "id", "CMD", game.key, str(proton), "command", command="wineconsole cmd"
        )
        spec = build_launch_spec(game, profile)
        self.assertEqual(spec.arguments, ["runinprefix", "wineconsole", "cmd"])

    def test_admin_executable_uses_shell_execute_helper(self):
        proton = self.tmp / "proton"
        proton.touch()
        executable = self.tmp / "Tool With Spaces.exe"
        executable.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        prefix = steam / "steamapps" / "compatdata" / "42"
        dosdevices = prefix / "pfx" / "dosdevices"
        dosdevices.mkdir(parents=True)
        (dosdevices / "z:").symlink_to("/")
        game = GameEntry(GameSource.STEAM, 42, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "Admin tool",
            game.key,
            str(proton),
            "executable",
            executable=str(executable),
            arguments='--name "A B"',
            run_as_admin=True,
        )
        spec = build_launch_spec(game, profile)
        self.assertEqual(spec.arguments[0], "runinprefix")
        self.assertTrue(spec.arguments[1].endswith("helpers/runas-helper.exe.so"))
        self.assertEqual(
            spec.arguments[2:],
            [f"Z:{str(executable).replace('/', chr(92))}", "--name", "A B"],
        )

    def test_normal_executable_still_uses_proton_run(self):
        proton = self.tmp / "proton"
        proton.touch()
        executable = self.tmp / "tool.exe"
        executable.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        game = GameEntry(GameSource.STEAM, 42, "Game", steam, steam)
        profile = LaunchProfile(
            "id", "Tool", game.key, str(proton), executable=str(executable)
        )
        spec = build_launch_spec(game, profile)
        self.assertEqual(spec.arguments, ["run", str(executable)])

    def test_runtime_switches_apply_supported_proton_environment(self):
        proton = self.tmp / "proton"
        proton.touch()
        executable = self.tmp / "game.exe"
        executable.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        game = GameEntry(GameSource.STEAM, 42, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "Options",
            game.key,
            str(proton),
            executable=str(executable),
            enable_wayland=True,
            prefer_discrete_gpu=True,
            force_nvapi=True,
            disable_esync=True,
            disable_fsync=True,
            use_wined3d=True,
            enable_proton_log=True,
            force_large_address_aware=True,
            prefer_sdl_input=True,
            enable_wayland_raw_input=True,
            dxvk_hud="fps",
            wine_debug="+seh",
        )

        with patch(
            "proton_launcher.runner.shutil.which",
            return_value="/usr/bin/switcherooctl",
        ):
            spec = build_launch_spec(game, profile)

        expected = {
            "PROTON_ENABLE_WAYLAND": "1",
            "PROTON_FORCE_NVAPI": "1",
            "PROTON_NO_ESYNC": "1",
            "PROTON_NO_FSYNC": "1",
            "PROTON_USE_WINED3D": "1",
            "PROTON_LOG": "1",
            "PROTON_FORCE_LARGE_ADDRESS_AWARE": "1",
            "PROTON_PREFER_SDL": "1",
            "WAYLANDDRV_RAWINPUT": "1",
            "DXVK_HUD": "fps",
            "WINEDEBUG": "+seh",
        }
        for name, value in expected.items():
            self.assertEqual(spec.environment[name], value)
        self.assertEqual(spec.program, "/usr/bin/switcherooctl")
        self.assertEqual(spec.arguments[:2], ["launch", str(proton)])

    def test_gamescope_wraps_gamemode_and_uses_mangoapp(self):
        proton = self.tmp / "proton"
        proton.touch()
        executable = self.tmp / "game.exe"
        executable.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        game = GameEntry(GameSource.STEAM, 42, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "Gamescope",
            game.key,
            str(proton),
            executable=str(executable),
            enable_gamescope=True,
            enable_gamemode=True,
            enable_mangohud=True,
            enable_hdr=True,
            gamescope_window_mode="fullscreen",
            gamescope_game_width=1920,
            gamescope_game_height=1080,
            gamescope_fps_limit=60,
            gamescope_filter="fsr",
            gamescope_sharpness=7,
            gamescope_adaptive_sync=True,
        )
        tools = {
            "gamescope": "/usr/bin/gamescope",
            "gamemoderun": "/usr/bin/gamemoderun",
            "mangoapp": "/usr/bin/mangoapp",
        }
        with patch("proton_launcher.runner.shutil.which", side_effect=tools.get):
            spec = build_launch_spec(game, profile)

        self.assertEqual(spec.program, "/usr/bin/gamescope")
        self.assertEqual(
            spec.arguments,
            [
                "-f",
                "-w",
                "1920",
                "-h",
                "1080",
                "--framerate-limit",
                "60",
                "-F",
                "fsr",
                "--sharpness",
                "7",
                "--adaptive-sync",
                "--hdr-enabled",
                "--mangoapp",
                "--",
                "/usr/bin/gamemoderun",
                str(proton),
                "run",
                str(executable),
            ],
        )
        self.assertEqual(spec.environment["PROTON_ENABLE_HDR"], "1")
        self.assertEqual(spec.environment["PROTON_ENABLE_WAYLAND"], "1")

    def test_wemod_keeps_host_wrappers_on_the_game_side(self):
        tool = self.tmp / "Proton"
        tool.mkdir()
        proton = tool / "proton"
        proton.touch()
        wemod = self.tmp / "wemod"
        wemod.touch()
        wemod_exe = self.tmp / "wemod_data" / "wemod_bin" / "WeMod.exe"
        wemod_exe.parent.mkdir(parents=True)
        wemod_exe.touch()
        executable = self.tmp / "game.exe"
        executable.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        game = GameEntry(GameSource.SHORTCUT, 42, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "WeMod",
            game.key,
            str(proton),
            executable=str(executable),
            launch_wemod=True,
            enable_mangohud=True,
        )
        with patch(
            "proton_launcher.runner.shutil.which", return_value="/usr/bin/mangohud"
        ):
            spec = build_launch_spec(game, profile, wemod_path=str(wemod))

        self.assertNotEqual(spec.program, "/usr/bin/mangohud")
        self.assertEqual(
            json.loads(spec.environment[WEMOD_GAME_WRAPPER_VARIABLE]),
            ["/usr/bin/mangohud"],
        )

    def test_steam_launch_spec_uses_registered_app_id(self):
        steam_root = self.tmp / "Steam"
        steam_root.mkdir()
        game = GameEntry(
            GameSource.SHORTCUT, 4180270141, "Shortcut", steam_root, steam_root
        )
        with patch(
            "proton_launcher.runner.shutil.which", return_value="/usr/bin/steam"
        ):
            spec = build_steam_launch_spec(game)
        self.assertEqual(spec.program, "/usr/bin/steam")
        self.assertEqual(spec.arguments, ["steam://rungameid/17954123544073863168"])

    def test_native_steam_game_uses_applaunch(self):
        steam_root = self.tmp / "Steam"
        steam_root.mkdir()
        game = GameEntry(
            GameSource.STEAM, 1593500, "God of War", steam_root, steam_root
        )
        with patch(
            "proton_launcher.runner.shutil.which", return_value="/usr/bin/steam"
        ):
            spec = build_steam_launch_spec(game)
        self.assertEqual(spec.arguments, ["-applaunch", "1593500"])

    def test_non_steam_default_override_value(self):
        self.assertEqual(
            DEFAULT_NON_STEAM_WINEDLLOVERRIDES,
            "OnlineFix64=n;SteamOverlay64=n;winmm=n,b;dnet=n;steam_api64=n;winhttp=n,b",
        )

    def test_online_fix_toggle_applies_hidden_override(self):
        proton = self.tmp / "proton"
        proton.touch()
        executable = self.tmp / "game.exe"
        executable.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        game = GameEntry(GameSource.SHORTCUT, 123, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "Online",
            game.key,
            str(proton),
            executable=str(executable),
            apply_online_fix=True,
        )
        spec = build_launch_spec(game, profile)
        self.assertEqual(
            spec.environment["WINEDLLOVERRIDES"], DEFAULT_NON_STEAM_WINEDLLOVERRIDES
        )

    def test_pre_release_environment_is_not_migrated(self):
        profile = LaunchProfile.from_dict(
            {
                "id": "id",
                "name": "Old",
                "game_key": "shortcut:root:1",
                "environment": {"WINEDLLOVERRIDES": DEFAULT_NON_STEAM_WINEDLLOVERRIDES},
            }
        )
        self.assertFalse(profile.apply_online_fix)
        self.assertEqual(profile.environment_text, "")

    def test_environment_text_round_trip(self):
        profile = LaunchProfile.from_dict(
            {
                "id": "id",
                "name": "Custom",
                "game_key": "steam:root:1",
                "environment_text": "WINEDLLOVERRIDES=version=n,b",
            }
        )
        self.assertFalse(profile.apply_online_fix)
        self.assertEqual(profile.environment_text, "WINEDLLOVERRIDES=version=n,b")

    def test_wemod_wraps_proton_and_supplies_tool_path(self):
        tool = self.tmp / "Proton With Spaces"
        tool.mkdir()
        proton = tool / "proton"
        proton.touch()
        wemod = self.tmp / "wemod"
        wemod.touch()
        wemod_exe = self.tmp / "wemod_data" / "wemod_bin" / "WeMod.exe"
        wemod_exe.parent.mkdir(parents=True)
        wemod_exe.touch()
        executable = self.tmp / "Game With Spaces.exe"
        executable.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        game = GameEntry(GameSource.SHORTCUT, 123, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "WeMod",
            game.key,
            str(proton),
            executable=str(executable),
            launch_wemod=True,
        )
        spec = build_launch_spec(game, profile, wemod_path=str(wemod))
        self.assertTrue(spec.program.endswith("python") or "python" in spec.program)
        self.assertEqual(spec.arguments[1:3], [str(proton), str(wemod_exe)])
        self.assertEqual(json.loads(spec.arguments[3]), ["run", str(executable)])
        self.assertEqual(spec.environment["STEAM_COMPAT_TOOL_PATHS"], str(tool))
        self.assertNotIn("WINEDLLOVERRIDES", spec.environment)
        self.assertEqual(
            spec.environment["PL_WEMOD_WINEDLLOVERRIDES"],
            DEFAULT_WEMOD_WINEDLLOVERRIDES,
        )
        self.assertNotIn(WEMOD_STEAM_LIBRARY_VARIABLE, spec.environment)

        library = self.tmp / "Steam Library"
        library.mkdir()
        steam_game = GameEntry(GameSource.STEAM, 456, "Steam Game", steam, library)
        steam_spec = build_launch_spec(steam_game, profile, wemod_path=str(wemod))
        self.assertEqual(
            steam_spec.environment[WEMOD_STEAM_LIBRARY_VARIABLE], str(library)
        )
        self.assertEqual(steam_spec.environment[WEMOD_STEAM_APP_ID_VARIABLE], "456")

    def test_standalone_wemod_uses_selected_prefix_and_proton(self):
        tool = self.tmp / "Proton"
        tool.mkdir()
        proton = tool / "proton"
        proton.touch()
        wemod_root = self.tmp / "wemod-launcher"
        wemod = wemod_root / "src" / "wemod.py"
        wemod.parent.mkdir(parents=True)
        wemod.touch()
        wemod_exe = wemod_root / "wemod_data" / "wemod_bin" / "WeMod.exe"
        wemod_exe.parent.mkdir(parents=True)
        wemod_exe.touch()
        steam = self.tmp / "Steam"
        library = self.tmp / "Library"
        steam.mkdir()
        library.mkdir()
        prefix = self.tmp / "compatdata" / "123"
        game = GameEntry(GameSource.STEAM, 123, "Game", steam, library)
        profile = LaunchProfile("id", "Default", game.key, str(proton))

        spec = build_wemod_launch_spec(game, profile, str(wemod), prefix)

        self.assertEqual(spec.arguments[1:3], [str(proton), str(wemod_exe)])
        self.assertEqual(json.loads(spec.arguments[3]), [])
        self.assertEqual(spec.arguments[4], "--wemod-only")
        self.assertEqual(spec.environment["STEAM_COMPAT_DATA_PATH"], str(prefix))
        self.assertEqual(spec.environment["STEAM_COMPAT_TOOL_PATHS"], str(tool))
        self.assertEqual(
            spec.environment["PL_WEMOD_WINEDLLOVERRIDES"],
            DEFAULT_WEMOD_WINEDLLOVERRIDES,
        )
        self.assertEqual(spec.environment[WEMOD_STEAM_LIBRARY_VARIABLE], str(library))
        self.assertEqual(spec.environment[WEMOD_STEAM_APP_ID_VARIABLE], "123")

    def test_wemod_bridge_launch_modes_keep_retry_setup_transactional(self):
        prefix = self.tmp / "compatdata" / "123"
        marker = prefix / "pfx" / ".wemod_installer"
        marker.parent.mkdir(parents=True)
        marker.touch()
        environment = {
            "STEAM_COMPAT_DATA_PATH": str(prefix),
            "PL_WEMOD_WINEDLLOVERRIDES": DEFAULT_WEMOD_WINEDLLOVERRIDES,
        }

        with (
            patch(
                "proton_launcher.wemod_bridge.sys.argv",
                [
                    "wemod_bridge.py",
                    "proton",
                    "WeMod.exe",
                    "[]",
                    "--wemod-only",
                ],
            ),
            patch.dict(
                "proton_launcher.wemod_bridge.os.environ", environment, clear=True
            ),
            patch("proton_launcher.wemod_bridge._wemod_processes", return_value={}),
            patch(
                "proton_launcher.wemod_bridge._wait_until_wemod_ready",
                return_value=True,
            ),
            patch("proton_launcher.wemod_bridge.subprocess.Popen") as popen,
        ):
            popen.return_value.wait.return_value = 0
            return_code = wemod_bridge_main()

        self.assertEqual(return_code, 0)
        popen.assert_called_once()
        self.assertEqual(
            popen.call_args.args[0],
            ["proton", "run", "WeMod.exe", *WEMOD_RENDER_ARGUMENTS],
        )
        popen.return_value.wait.assert_called_once_with()

        library = self.tmp / "Steam Library"
        library.mkdir()
        executable = self.tmp / "game.exe"
        executable.touch()
        steam_environment = {
            "STEAM_COMPAT_DATA_PATH": str(prefix),
            "PL_WEMOD_WINEDLLOVERRIDES": DEFAULT_WEMOD_WINEDLLOVERRIDES,
            "PL_WEMOD_STEAM_LIBRARY": str(library),
            "PL_WEMOD_STEAM_APP_ID": "123",
            "PL_GAME_WRAPPER_ARGUMENTS": json.dumps(["/usr/bin/gamescope", "-f", "--"]),
        }

        def configure_retry(_prefix, _arguments, _game, candidate, _app_id):
            candidate["WINEDLLOVERRIDES"] += ";steam.exe=n,b"
            candidate["SteamGameId"] = "352130"
            return True

        with (
            patch(
                "proton_launcher.wemod_bridge.sys.argv",
                [
                    "wemod_bridge.py",
                    "proton",
                    "WeMod.exe",
                    json.dumps(["run", str(executable)]),
                ],
            ),
            patch.dict(
                "proton_launcher.wemod_bridge.os.environ",
                steam_environment,
                clear=True,
            ),
            patch("proton_launcher.wemod_bridge._wemod_processes", return_value={}),
            patch(
                "proton_launcher.wemod_bridge._wait_until_wemod_ready",
                return_value=True,
            ),
            patch(
                "proton_launcher.wemod_bridge._configure_steam_retry_environment",
                side_effect=configure_retry,
            ),
            patch(
                "proton_launcher.wemod_bridge._prepare_steam_retry_helper",
                side_effect=OSError("install failed"),
            ),
            patch(
                "proton_launcher.wemod_bridge._register_steam_library",
                return_value=True,
            ),
            patch("proton_launcher.wemod_bridge.subprocess.Popen") as retry_popen,
        ):
            retry_popen.return_value.wait.return_value = 0
            return_code = wemod_bridge_main()

        self.assertEqual(return_code, 0)
        wemod_environment = retry_popen.call_args_list[0].kwargs["env"]
        self.assertEqual(
            wemod_environment["WINEDLLOVERRIDES"],
            DEFAULT_WEMOD_WINEDLLOVERRIDES,
        )
        self.assertNotIn("SteamGameId", wemod_environment)
        self.assertEqual(
            retry_popen.call_args_list[1].args[0],
            [
                "/usr/bin/gamescope",
                "-f",
                "--",
                "proton",
                "run",
                str(executable),
            ],
        )

    def test_wemod_and_game_receive_separate_dll_overrides(self):
        tool = self.tmp / "Proton"
        tool.mkdir()
        proton = tool / "proton"
        proton.touch()
        wemod_src = self.tmp / "wemod-launcher" / "src"
        wemod_src.mkdir(parents=True)
        wemod = wemod_src / "wemod.py"
        wemod.touch()
        wemod_exe = (
            self.tmp / "wemod-launcher" / "wemod_data" / "wemod_bin" / "WeMod.exe"
        )
        wemod_exe.parent.mkdir(parents=True)
        wemod_exe.touch()
        executable = self.tmp / "game.exe"
        executable.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        game = GameEntry(GameSource.SHORTCUT, 123, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "Separate overrides",
            game.key,
            str(proton),
            executable=str(executable),
            environment_text="WINEDLLOVERRIDES=game_hook=n,b",
            launch_wemod=True,
        )
        spec = build_launch_spec(game, profile, wemod_path=str(wemod))
        self.assertEqual(spec.environment["WINEDLLOVERRIDES"], "game_hook=n,b")
        self.assertEqual(
            spec.environment["PL_WEMOD_WINEDLLOVERRIDES"],
            DEFAULT_WEMOD_WINEDLLOVERRIDES,
        )

    def test_default_wemod_override_is_passed_as_complete_separate_set(self):
        tool = self.tmp / "Proton"
        tool.mkdir()
        proton = tool / "proton"
        proton.touch()
        wemod_src = self.tmp / "wemod-launcher" / "src"
        wemod_src.mkdir(parents=True)
        wemod = wemod_src / "wemod.py"
        wemod.touch()
        wemod_exe = (
            self.tmp / "wemod-launcher" / "wemod_data" / "wemod_bin" / "WeMod.exe"
        )
        wemod_exe.parent.mkdir(parents=True)
        wemod_exe.touch()
        executable = self.tmp / "game.exe"
        executable.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        game = GameEntry(GameSource.SHORTCUT, 123, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "Online fix",
            game.key,
            str(proton),
            executable=str(executable),
            apply_online_fix=True,
            launch_wemod=True,
        )
        spec = build_launch_spec(game, profile, wemod_path=str(wemod))
        self.assertEqual(
            spec.environment["WINEDLLOVERRIDES"],
            DEFAULT_NON_STEAM_WINEDLLOVERRIDES,
        )
        self.assertEqual(
            spec.environment["PL_WEMOD_WINEDLLOVERRIDES"],
            DEFAULT_WEMOD_WINEDLLOVERRIDES,
        )

    def test_bridge_does_not_require_batch_patch(self):
        tool = self.tmp / "Proton"
        tool.mkdir()
        proton = tool / "proton"
        proton.touch()
        wemod = self.tmp / "wemod"
        wemod.touch()
        wemod_exe = self.tmp / "wemod_data" / "wemod_bin" / "WeMod.exe"
        wemod_exe.parent.mkdir(parents=True)
        wemod_exe.touch()
        executable = self.tmp / "game.exe"
        executable.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        game = GameEntry(GameSource.SHORTCUT, 123, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "Shared",
            game.key,
            str(proton),
            executable=str(executable),
            environment_text="WINEDLLOVERRIDES=game_hook=n,b",
            launch_wemod=True,
        )
        spec = build_launch_spec(game, profile, wemod_path=str(wemod))
        self.assertEqual(spec.environment["WINEDLLOVERRIDES"], "game_hook=n,b")
        self.assertEqual(
            spec.environment["PL_WEMOD_WINEDLLOVERRIDES"],
            DEFAULT_WEMOD_WINEDLLOVERRIDES,
        )

    def test_bridge_supports_different_override_without_batch_patch(self):
        tool = self.tmp / "Proton"
        tool.mkdir()
        proton = tool / "proton"
        proton.touch()
        wemod = self.tmp / "wemod"
        wemod.touch()
        wemod_exe = self.tmp / "wemod_data" / "wemod_bin" / "WeMod.exe"
        wemod_exe.parent.mkdir(parents=True)
        wemod_exe.touch()
        executable = self.tmp / "game.exe"
        executable.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        game = GameEntry(GameSource.SHORTCUT, 123, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "Unsupported",
            game.key,
            str(proton),
            executable=str(executable),
            launch_wemod=True,
        )
        spec = build_launch_spec(game, profile, wemod_path=str(wemod))
        self.assertEqual(
            spec.environment["PL_WEMOD_WINEDLLOVERRIDES"],
            DEFAULT_WEMOD_WINEDLLOVERRIDES,
        )

    def test_direct_steam_overlay_injection_environment(self):
        proton = self.tmp / "proton"
        proton.touch()
        executable = self.tmp / "game.exe"
        executable.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        for architecture in ("ubuntu12_32", "ubuntu12_64"):
            renderer = steam / architecture / "gameoverlayrenderer.so"
            renderer.parent.mkdir()
            renderer.touch()
        game = GameEntry(GameSource.SHORTCUT, 123, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "Overlay",
            game.key,
            str(proton),
            executable=str(executable),
            inject_steam_overlay=True,
            overlay_app_id="480",
        )
        spec = build_launch_spec(game, profile)
        self.assertEqual(spec.environment["SteamAppId"], "480")
        self.assertEqual(spec.environment["SteamGameId"], "480")
        self.assertEqual(spec.environment["SteamOverlayGameId"], "480")
        self.assertEqual(
            spec.environment["LD_PRELOAD"],
            f"{steam}/ubuntu12_32/gameoverlayrenderer.so:{steam}/ubuntu12_64/gameoverlayrenderer.so",
        )

    def test_only_benign_overlay_elf_warnings_are_hidden(self):
        benign = "ERROR: ld.so: object '/steam/ubuntu12_32/gameoverlayrenderer.so' from LD_PRELOAD cannot be preloaded (wrong ELF class: ELFCLASS32): ignored."
        missing = "ERROR: ld.so: object '/steam/gameoverlayrenderer.so' from LD_PRELOAD cannot be preloaded (cannot open shared object file): ignored."
        crash = "wine: Unhandled page fault"
        self.assertEqual(
            clean_process_output(f"{benign}\n{missing}\n{crash}"), f"{missing}\n{crash}"
        )

    def test_prepares_missing_compatdata_directory(self):
        prefix = self.tmp / "steamapps" / "compatdata" / "2462212442"
        self.assertTrue(prepare_compatdata_directory(prefix))
        self.assertTrue(prefix.is_dir())
        self.assertFalse((prefix / "pfx.lock").exists())
        self.assertFalse(prepare_compatdata_directory(prefix))

    def test_rejects_compatdata_path_that_is_a_file(self):
        prefix = self.tmp / "not-a-directory"
        prefix.touch()
        with self.assertRaisesRegex(ValueError, "not a directory"):
            prepare_compatdata_directory(prefix)

    def test_explorer_without_target_opens_working_directory(self):
        proton = self.tmp / "proton"
        proton.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        working = self.tmp / "folder"
        working.mkdir()
        game = GameEntry(GameSource.STEAM, 42, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "Explorer",
            game.key,
            str(proton),
            "command",
            command="wine explorer",
            working_directory=str(working),
        )
        spec = build_launch_spec(game, profile)
        self.assertEqual(spec.arguments, ["runinprefix", "explorer", "."])
        self.assertEqual(spec.working_directory, str(working))

    def test_quoted_environment_value_is_unquoted(self):
        self.assertEqual(
            unquote_environment_value('"d3d8=n,b;msvcrt=n,b;wsock32=n,b"'),
            "d3d8=n,b;msvcrt=n,b;wsock32=n,b",
        )
        with self.assertRaisesRegex(ValueError, "Unclosed quote"):
            unquote_environment_value('"d3d8=n,b')

    def test_windows_working_directory_uses_wine_drive_mapping(self):
        prefix = self.tmp / "compatdata" / "42"
        dosdevices = prefix / "pfx" / "dosdevices"
        dosdevices.mkdir(parents=True)
        (dosdevices / "z:").symlink_to("/")
        self.assertEqual(
            resolve_working_directory(r'"Z:\home\alice\Games\Example"', prefix),
            Path("/home/alice/Games/Example"),
        )

    def test_linux_working_directory_is_unchanged(self):
        folder = self.tmp / "working"
        folder.mkdir()
        self.assertEqual(
            resolve_working_directory(str(folder), self.tmp / "prefix"), folder
        )

    def test_profile_store_round_trip(self):
        store = ConfigStore(self.tmp / "config.json")
        profile = LaunchProfile("id", "Trainer", "steam:root:1", command="winecfg")
        store.put_profile(profile)
        loaded = ConfigStore(self.tmp / "config.json")
        self.assertEqual(loaded.profiles(profile.game_key)[1:], [profile])
        loaded.delete_profile(profile)
        self.assertEqual(
            [item.id for item in loaded.profiles(profile.game_key)], ["default"]
        )

    def test_config_store_rejects_invalid_json(self):
        path = self.tmp / "config.json"
        path.write_text("{invalid")
        with self.assertRaisesRegex(ValueError, "Could not load configuration"):
            ConfigStore(path)

    def test_config_store_writes_trailing_newline(self):
        path = self.tmp / "config.json"
        store = ConfigStore(path)
        store.save()
        self.assertTrue(path.read_text().endswith("\n"))
        self.assertEqual(json.loads(path.read_text())["schema_version"], 1)

    def test_followup_profile_fields_round_trip(self):
        store = ConfigStore(self.tmp / "config.json")
        profile = LaunchProfile(
            "id",
            "Chain",
            "steam:root:1",
            followup_enabled=True,
            wait_for_executable="Game.exe",
            followup_delay=2.5,
            followup_mode="command",
            followup_command="wine explorer .",
            followup_arguments="--test",
        )
        store.put_profile(profile)
        self.assertEqual(
            ConfigStore(self.tmp / "config.json").profiles(profile.game_key)[1:],
            [profile],
        )

    def test_builds_followup_in_primary_proton_context(self):
        proton = self.tmp / "proton"
        proton.touch()
        steam = self.tmp / "Steam"
        steam.mkdir()
        working = self.tmp / "game"
        working.mkdir()
        prefix = self.tmp / "compatdata" / "42"
        game = GameEntry(GameSource.STEAM, 42, "Game", steam, steam)
        profile = LaunchProfile(
            "id",
            "Chain",
            game.key,
            str(proton),
            working_directory=str(working),
            environment_text='TEST_VALUE="with spaces"',
            followup_enabled=True,
            followup_mode="command",
            followup_command="wine explorer .",
            followup_arguments="--test",
        )
        spec = build_followup_launch_spec(game, profile, prefix)
        self.assertEqual(spec.arguments, ["runinprefix", "explorer", ".", "--test"])
        self.assertEqual(spec.environment["TEST_VALUE"], "with spaces")
        self.assertEqual(spec.environment["STEAM_COMPAT_DATA_PATH"], str(prefix))
        self.assertEqual(spec.working_directory, str(working))

    def test_process_name_matching_handles_windows_paths(self):
        self.assertTrue(
            command_matches(
                b"Z:\\Games\\Game.exe\0--flag\0", "wine64-preloader\n", "game.EXE"
            )
        )
        self.assertFalse(
            command_matches(b"Z:\\Games\\Other.exe\0", "wine64-preloader\n", "game.exe")
        )

    def test_derives_primary_executable_name(self):
        self.assertEqual(
            primary_executable_name("executable", "/games/Tool Name.exe", ""),
            "Tool Name.exe",
        )
        self.assertEqual(
            primary_executable_name("executable", r'"Z:\Games\Tool.exe"', ""),
            "Tool.exe",
        )
        self.assertEqual(
            primary_executable_name("command", "", 'wine "C:\\Games\\Tool.exe" --flag'),
            "Tool.exe",
        )

    def test_process_watcher_filters_by_prefix(self):
        proc = self.tmp / "proc"
        proc.mkdir()
        prefix = self.tmp / "prefix"
        for pid, selected_prefix in (("100", prefix), ("200", self.tmp / "other")):
            folder = proc / pid
            folder.mkdir()
            (folder / "environ").write_bytes(
                f"A=1\0STEAM_COMPAT_DATA_PATH={selected_prefix}\0".encode()
            )
            (folder / "cmdline").write_bytes(b"Z:\\Games\\Game.exe\0")
            (folder / "comm").write_text("wine64-preloader\n")
        self.assertEqual(find_matching_pids("Game.exe", prefix, proc), {100})

    def test_discovers_steam_managed_proton_from_common(self):
        library = self.tmp / "Library"
        proton = library / "steamapps" / "common" / "Proton - Experimental" / "proton"
        proton.parent.mkdir(parents=True)
        proton.touch()
        installations, issues = discover_protons(steam_libraries=[library])
        match = next(item for item in installations if item.launcher == proton)
        self.assertEqual(match.display_name, "Proton - Experimental")
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
