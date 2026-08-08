# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import json
import tempfile
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
)
from proton_launcher.process_watcher import (
    command_matches,
    find_matching_pids,
    primary_executable_name,
)
from proton_launcher.profiles import ConfigStore
from proton_launcher.proton import discover_protons
from proton_launcher.runner import (
    build_followup_launch_spec,
    build_launch_spec,
    build_steam_launch_spec,
    clean_process_output,
    prepare_compatdata_directory,
    resolve_working_directory,
    unquote_environment_value,
)
from proton_launcher.steam import (
    is_component,
    parse_manifest,
    parse_shortcuts,
    resolve_game_executable,
)
from proton_launcher.wemod_bridge import (
    _initialize_prefix,
    _initializer_command,
    _selected_proton_version,
    _wemod_processes,
    reset_wemod_prefix,
)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

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
        game_file = prefix / "pfx" / "drive_c" / "game" / "save.dat"
        game_file.parent.mkdir(parents=True)
        game_file.touch()

        removed = reset_wemod_prefix(prefix)

        self.assertEqual(removed, [marker, prefix_data])
        self.assertFalse(marker.exists())
        self.assertFalse(prefix_data.exists())
        self.assertTrue(shared_data.is_dir())
        self.assertTrue(game_file.is_file())

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
        game = parse_manifest(manifest, root, library, r"bin\ActualGame.exe")
        self.assertEqual(game.default_executable, str(executable))

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
