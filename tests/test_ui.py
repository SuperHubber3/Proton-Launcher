# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import vdf

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QMessageBox,
    QToolBar,
)

from proton_launcher.models import (
    GameEntry,
    GameSource,
    LaunchProfile,
    ProtonInstallation,
    SteamLaunchOption,
)  # noqa: E402
from proton_launcher.profiles import ConfigStore  # noqa: E402
from proton_launcher.protondb import protondb_app_id  # noqa: E402
from proton_launcher.runtime_options_dialog import RuntimeOptionsDialog  # noqa: E402
from proton_launcher.sessions import SessionRecord  # noqa: E402
from proton_launcher.ui import MainWindow  # noqa: E402


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self.directory.name)
        self.environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.tmp),
                "XDG_STATE_HOME": str(self.tmp / "state"),
                "XDG_RUNTIME_DIR": str(self.tmp / "runtime"),
                "XDG_CACHE_HOME": str(self.tmp / "cache"),
            },
        )
        self.environment.start()
        self.window = MainWindow(ConfigStore(self.tmp / "config.json"))
        steam = self.tmp / "Steam"
        library = self.tmp / "Library"
        executable = library / "steamapps" / "common" / "Game" / "Game.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        proton = self.tmp / "Proton" / "proton"
        proton.parent.mkdir()
        proton.touch()
        game = GameEntry(
            GameSource.SHORTCUT,
            42,
            "Example",
            steam,
            library,
            shortcut_exe=str(executable),
        )
        installation = ProtonInstallation(
            "Test Proton", proton, proton.parent, "test", "test-proton"
        )
        self.window.games = [game]
        self.window.protons = [installation]
        self.window.default_proton = installation
        self.window.game_combo.blockSignals(True)
        self.window.game_combo.clear()
        self.window.game_combo.addItem(game.label, game.key)
        self.window.game_combo.blockSignals(False)
        self.window.game_combo.setCurrentIndex(0)
        self.window.game_changed()

    def tearDown(self):
        self.window._force_quit = True
        self.window.close()
        self.app.processEvents()
        self.environment.stop()
        self.directory.cleanup()

    def test_toolbar_is_not_movable(self):
        self.assertFalse(self.window.findChild(QToolBar).isMovable())
        self.assertEqual(self.window.open_prefix_action.text(), "Open prefix")
        self.assertEqual(self.window.delete_prefix_action.text(), "Delete prefix…")

    def test_open_prefix_uses_desktop_file_manager(self):
        prefix = self.window.current_game().default_prefix
        prefix.mkdir(parents=True)
        with patch(
            "proton_launcher.ui.QDesktopServices.openUrl", return_value=True
        ) as open_url:
            self.window.open_prefix()
        self.assertEqual(open_url.call_count, 1)
        self.assertEqual(open_url.call_args.args[0].toLocalFile(), str(prefix))

    def test_delete_prefix_requires_confirmation(self):
        prefix = self.window.current_game().default_prefix
        prefix.mkdir(parents=True)
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.No):
            with patch("proton_launcher.ui.subprocess.run") as run:
                self.window.delete_prefix()
        run.assert_not_called()
        self.assertTrue(prefix.exists())

    def test_requested_labels_and_default_profile(self):
        self.assertEqual(
            self.window.online_fix_checkbox.text(), "Apply online-fix overrides"
        )
        self.assertEqual(self.window.wemod_checkbox.text(), "Launch with WeMod")
        self.assertEqual(self.window.launch_wemod_button.text(), "Launch WeMod")
        self.assertEqual(self.window.profile_combo.itemText(0), "Default")
        self.assertFalse(self.window.delete_button.isEnabled())
        self.assertEqual(self.window.protondb_button.text(), "ProtonDB")
        self.assertFalse(self.window.protondb_button.isEnabled())
        self.assertFalse(self.window.skip_update_button.isEnabled())
        self.assertFalse(hasattr(self.window, "console_game_label"))
        self.assertGreaterEqual(
            self.window.proton_combo.findData("__native__"),
            0,
        )

    def test_native_runtime_selection_is_saved_and_disables_wine_options(self):
        self.window.proton_combo.setCurrentIndex(
            self.window.proton_combo.findData("__native__")
        )

        profile = self.window._profile_from_ui()

        self.assertTrue(profile.use_native_runtime)
        self.assertFalse(profile.use_default_proton)
        self.assertEqual(profile.proton_path, "")

        self.window._autosave_default()
        loaded = ConfigStore(self.tmp / "config.json")
        saved = next(
            item
            for item in loaded.profiles(self.window.current_game().key)
            if item.id == "default"
        )
        self.assertTrue(saved.use_native_runtime)
        self.assertFalse(saved.use_default_proton)
        self.assertEqual(saved.proton_path, "")
        self.assertFalse(self.window.admin_checkbox.isEnabled())
        self.assertFalse(self.window.steam_launch_checkbox.isEnabled())
        self.assertFalse(self.window.steam_launch_checkbox.isChecked())
        self.assertFalse(self.window.online_fix_checkbox.isEnabled())
        self.assertFalse(self.window.wemod_checkbox.isEnabled())
        self.assertFalse(self.window.wayland_checkbox.isEnabled())
        self.assertIn("directly", self.window.proton_combo.toolTip())

        dialog = RuntimeOptionsDialog(profile)
        self.assertFalse(dialog.force_nvapi.isEnabled())
        self.assertFalse(dialog.raw_input.isEnabled())
        self.assertFalse(dialog.sdl_input.isEnabled())
        self.assertFalse(dialog.dxvk_hud.isEnabled())
        self.assertFalse(dialog.tabs.isTabEnabled(2))
        self.assertTrue(dialog.tabs.isTabEnabled(1))
        dialog.close()

    def test_skip_update_edits_selected_steam_manifest(self):
        old_game = self.window.current_game()
        steamapps = old_game.library_root / "steamapps"
        manifest = steamapps / "appmanifest_42.acf"
        manifest.write_text(
            '"AppState"\n{\n\t"appid"\t\t"42"\n'
            '\t"StateFlags"\t\t"6"\n\t"name"\t\t"Example"\n}\n'
        )
        game = GameEntry(
            GameSource.STEAM,
            42,
            "Example",
            old_game.steam_root,
            old_game.library_root,
        )
        self.window.games = [game]
        self.window.game_combo.blockSignals(True)
        self.window.game_combo.clear()
        self.window.game_combo.addItem(game.label, game.key)
        self.window.game_combo.blockSignals(False)
        self.window.game_combo.setCurrentIndex(0)
        self.window.game_changed()

        self.assertTrue(self.window.skip_update_button.isEnabled())
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.Yes):
            self.window.skip_update_button.click()

        with manifest.open() as handle:
            self.assertEqual(vdf.load(handle)["AppState"]["StateFlags"], "4")

    def test_skip_all_updates_edits_every_installed_steam_game(self):
        old_game = self.window.current_game()
        steamapps = old_game.library_root / "steamapps"
        games = []
        for app_id in (41, 42):
            manifest = steamapps / f"appmanifest_{app_id}.acf"
            manifest.write_text(
                '"AppState"\n{\n'
                f'\t"appid"\t\t"{app_id}"\n'
                '\t"StateFlags"\t\t"6"\n'
                f'\t"name"\t\t"Game {app_id}"\n}}\n'
            )
            games.append(
                GameEntry(
                    GameSource.STEAM,
                    app_id,
                    f"Game {app_id}",
                    old_game.steam_root,
                    old_game.library_root,
                )
            )
        self.window.games = games

        with patch.object(QMessageBox, "warning", return_value=QMessageBox.Yes):
            self.window.skip_all_updates()

        for app_id in (41, 42):
            with (steamapps / f"appmanifest_{app_id}.acf").open() as handle:
                self.assertEqual(vdf.load(handle)["AppState"]["StateFlags"], "4")

    def test_runtime_options_round_trip_through_profile_editor(self):
        self.window.gamemode_checkbox.setChecked(True)
        self.window.mangohud_checkbox.setChecked(True)
        self.window.gamescope_checkbox.setChecked(True)
        self.window.wayland_checkbox.setChecked(True)
        self.window.runtime_option_values.update(
            {
                "prefer_discrete_gpu": True,
                "gamescope_fps_limit": 72,
                "gamescope_filter": "fsr",
                "wine_debug": "+seh",
            }
        )

        profile = self.window._profile_from_ui()
        self.assertTrue(profile.enable_gamemode)
        self.assertTrue(profile.enable_mangohud)
        self.assertTrue(profile.enable_gamescope)
        self.assertTrue(profile.enable_wayland)
        self.assertTrue(profile.prefer_discrete_gpu)
        self.assertEqual(profile.gamescope_fps_limit, 72)
        self.assertEqual(profile.gamescope_filter, "fsr")
        self.assertEqual(profile.wine_debug, "+seh")

        self.window.load_profile(profile)
        self.assertTrue(self.window.gamemode_checkbox.isChecked())
        self.assertEqual(self.window.runtime_option_values["gamescope_fps_limit"], 72)

    def test_wemod_disables_wayland_options_without_clearing_them(self):
        self.window.wayland_checkbox.setChecked(True)
        self.window.gamescope_checkbox.setChecked(True)
        self.window.wemod_checkbox.setChecked(True)

        self.assertTrue(self.window.wayland_checkbox.isChecked())
        self.assertFalse(self.window.wayland_checkbox.isEnabled())
        self.assertIn("Unavailable with WeMod", self.window.wayland_checkbox.toolTip())
        self.assertTrue(self.window.gamescope_checkbox.isChecked())
        self.assertFalse(self.window.gamescope_checkbox.isEnabled())
        self.assertIn(
            "incompatible with WeMod", self.window.gamescope_checkbox.toolTip()
        )

        dialog = RuntimeOptionsDialog(
            LaunchProfile(
                id="test",
                name="Test",
                game_key="shortcut:42",
                launch_wemod=True,
                enable_hdr=True,
                enable_wayland_raw_input=True,
            )
        )
        self.assertTrue(dialog.hdr.isChecked())
        self.assertFalse(dialog.hdr.isEnabled())
        self.assertTrue(dialog.raw_input.isChecked())
        self.assertFalse(dialog.raw_input.isEnabled())
        self.assertFalse(dialog.tabs.isTabEnabled(1))
        dialog.close()

        self.window.wemod_checkbox.setChecked(False)
        self.assertTrue(self.window.wayland_checkbox.isChecked())
        self.assertTrue(self.window.wayland_checkbox.isEnabled())
        self.assertTrue(self.window.gamescope_checkbox.isChecked())
        self.assertTrue(self.window.gamescope_checkbox.isEnabled())

    def test_runtime_dialog_ignores_unknown_and_quick_toggle_values(self):
        dialog = MagicMock()
        dialog.exec.return_value = QDialog.Accepted
        dialog.values.return_value = {
            "prefer_discrete_gpu": True,
            "enable_gamemode": False,
            "unknown_option": "ignored",
        }
        self.window.gamemode_checkbox.setChecked(True)

        with patch("proton_launcher.ui.RuntimeOptionsDialog", return_value=dialog):
            self.window.configure_runtime_options()

        self.assertTrue(self.window.runtime_option_values["prefer_discrete_gpu"])
        self.assertNotIn("enable_gamemode", self.window.runtime_option_values)
        self.assertNotIn("unknown_option", self.window.runtime_option_values)
        self.assertTrue(self.window._profile_from_ui().enable_gamemode)

    def test_followup_launch_button_tracks_group_without_session_change(self):
        self.assertFalse(self.window.followup_launch_now_button.isEnabled())

        self.window.followup_group.setChecked(True)
        self.assertTrue(self.window.followup_launch_now_button.isEnabled())

        self.window.followup_group.setChecked(False)
        self.assertFalse(self.window.followup_launch_now_button.isEnabled())

    def test_session_controls_and_console_follow_selected_game(self):
        first = self.window.current_game()
        second_executable = self.tmp / "Second" / "Second.exe"
        second_executable.parent.mkdir()
        second_executable.touch()
        second = GameEntry(
            GameSource.SHORTCUT,
            43,
            "Second",
            first.steam_root,
            first.library_root,
            shortcut_exe=str(second_executable),
        )
        self.window.games.append(second)
        self.window.game_combo.addItem(second.label, second.key)
        first_record = SessionRecord(
            "first-session",
            "primary",
            first.key,
            first.name,
            str(first.default_prefix),
            "process-group",
        )

        self.window._log("first output", first.key)
        self.window._update_session_buttons([first_record])
        self.assertFalse(self.window.launch_button.isEnabled())

        self.window.game_combo.setCurrentIndex(1)
        self.window._log("second output", second.key)
        self.window._update_session_buttons([first_record])
        self.assertTrue(self.window.launch_button.isEnabled())
        self.assertEqual(self.window.log.toPlainText(), "second output")

        self.window.clear_log_button.click()
        self.assertEqual(self.window.log.toPlainText(), "")
        self.window.game_combo.setCurrentIndex(0)
        self.assertEqual(self.window.log.toPlainText(), "first output")

    def test_stop_all_only_stops_selected_game_sessions(self):
        game = self.window.current_game()
        selected = SessionRecord(
            "selected",
            "primary",
            game.key,
            game.name,
            str(game.default_prefix),
            "process-group",
        )
        other = SessionRecord(
            "other",
            "primary",
            "shortcut:99",
            "Other",
            str(self.tmp / "other-prefix"),
            "process-group",
        )
        with (
            patch.object(
                self.window.sessions, "active", return_value=[selected, other]
            ),
            patch.object(self.window.sessions, "stop") as stop,
            patch.object(self.window, "_refresh_sessions"),
        ):
            self.window.stop_all()

        stop.assert_called_once_with(selected)

    def test_steam_launch_option_updates_direct_launch_fields(self):
        old_game = self.window.current_game()
        game = GameEntry(
            GameSource.STEAM,
            42,
            "Example",
            old_game.steam_root,
            old_game.library_root,
            launch_options=(
                SteamLaunchOption("Play game", "/games/Game.exe"),
                SteamLaunchOption(
                    "Open launcher",
                    "/games/Launcher.exe",
                    "--settings",
                    "/games",
                ),
            ),
        )
        self.window.games = [game]
        self.window.game_combo.clear()
        self.window.game_combo.addItem(game.label, game.key)
        self.window.current_profile.game_key = game.key
        self.window.current_profile.executable = ""
        self.window.load_profile(self.window.current_profile)

        self.assertFalse(self.window.launch_option_combo.isHidden())
        self.assertEqual(self.window.launch_option_combo.currentText(), "Play game")
        self.assertEqual(self.window.exe_edit.text(), "/games/Game.exe")

        self.window.launch_option_combo.setCurrentIndex(1)

        self.assertEqual(self.window.exe_edit.text(), "/games/Launcher.exe")
        self.assertEqual(self.window.arguments_edit.text(), "--settings")
        self.assertEqual(self.window.working_edit.text(), "/games")

        self.window.arguments_edit.setText("--custom")
        self.assertEqual(
            self.window.launch_option_combo.currentText(), "Custom executable"
        )

    def test_protondb_button_shows_rating_and_opens_game_page(self):
        old_game = self.window.current_game()
        game = GameEntry(
            GameSource.STEAM,
            1593500,
            "God of War",
            old_game.steam_root,
            old_game.library_root,
            old_game.install_dir,
            default_executable=old_game.shortcut_exe,
        )
        self.window.protondb_cache[game.app_id] = "Gold"
        self.window.games = [game]
        self.window.game_combo.blockSignals(True)
        self.window.game_combo.clear()
        self.window.game_combo.addItem(game.label, game.key)
        self.window.game_combo.blockSignals(False)
        self.window.game_combo.setCurrentIndex(0)
        self.window.game_changed()

        self.assertEqual(self.window.protondb_button.text(), "ProtonDB: Gold")
        self.assertTrue(self.window.protondb_button.isEnabled())
        with patch(
            "proton_launcher.ui.QDesktopServices.openUrl", return_value=True
        ) as open_url:
            self.window.protondb_button.click()
        self.assertEqual(
            open_url.call_args.args[0].toString(),
            "https://www.protondb.com/app/1593500",
        )

    def test_non_steam_protondb_button_uses_online_fix_real_app_id(self):
        game = self.window.current_game()
        executable = Path(game.shortcut_exe)
        (executable.parent / "OnlineFix.ini").write_text(
            "[Main]\nRealAppId=3844970\nFakeAppId=480\n"
        )
        self.window.protondb_cache[3844970] = "Platinum"
        self.window.protondb_app_ids.clear()

        with patch(
            "proton_launcher.ui.protondb_app_id",
            wraps=protondb_app_id,
        ) as resolve_app_id:
            self.window.game_changed()
            self.window.game_changed()
        resolve_app_id.assert_called_once_with(game)

        self.assertEqual(self.window.protondb_button.text(), "ProtonDB: Platinum")
        self.assertTrue(self.window.protondb_button.isEnabled())
        with patch(
            "proton_launcher.ui.QDesktopServices.openUrl", return_value=True
        ) as open_url:
            self.window.protondb_button.click()
        self.assertEqual(
            open_url.call_args.args[0].toString(),
            "https://www.protondb.com/app/3844970",
        )

    def test_online_fix_enables_but_does_not_lock_overlay(self):
        self.window.overlay_checkbox.setChecked(False)
        self.window.online_fix_checkbox.setChecked(True)
        self.assertTrue(self.window.overlay_checkbox.isChecked())
        self.window.overlay_checkbox.setChecked(False)
        self.assertFalse(self.window.overlay_checkbox.isChecked())
        self.assertTrue(self.window.overlay_checkbox.isEnabled())

    def test_default_profile_autosaves_raw_text(self):
        self.window.environment_edit.setPlainText('VALUE="unfinished')
        self.window._autosave_default()
        loaded = ConfigStore(self.tmp / "config.json")
        profile = loaded.profiles(self.window.current_game().key)[0]
        self.assertEqual(profile.environment_text, 'VALUE="unfinished')

    def test_new_non_steam_default_uses_prefix_recorded_proton(self):
        game = self.window.current_game()
        self.window.autosave_timer.stop()
        self.window.current_profile = None
        self.window.store.data["games"].pop(game.key)
        prefix = game.default_prefix
        prefix.mkdir(parents=True)
        proton_root = self.window.protons[0].root
        (prefix / "config_info").write_text(
            f"test-proton\n{proton_root}/files/share/fonts/\n"
        )
        self.window.game_changed()
        profile = self.window.store.profiles(game.key)[0]
        self.assertFalse(profile.use_default_proton)
        self.assertEqual(profile.proton_path, str(self.window.protons[0].launcher))

    def test_wemod_configure_opens_integrations_tab(self):
        with patch("proton_launcher.ui.SettingsDialog") as dialog:
            self.window.configure_wemod_button.click()
        self.assertEqual(dialog.call_args.kwargs["initial_tab"], "integrations")

    def test_launch_wemod_uses_selected_game_prefix(self):
        wemod_root = self.tmp / "wemod-launcher"
        launcher = wemod_root / "src" / "wemod.py"
        launcher.parent.mkdir(parents=True)
        launcher.touch()
        executable = wemod_root / "wemod_data" / "wemod_bin" / "WeMod.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        self.window.store.settings["wemod_launcher_path"] = str(launcher)
        self.window._update_wemod_status()
        self.assertTrue(self.window.launch_wemod_button.isEnabled())
        self.window.active_sessions = [
            SessionRecord(
                "wemod",
                "wemod",
                self.window.current_game().key,
                self.window.current_game().name,
                str(self.window.current_game().default_prefix),
                "process-group",
            )
        ]
        self.window._update_wemod_status()
        self.assertFalse(self.window.launch_wemod_button.isEnabled())
        self.window.active_sessions = []
        self.window._update_wemod_status()

        with (
            patch.object(self.window.sessions, "start") as start,
            patch.object(self.window, "_register_session"),
            patch.object(self.window, "_refresh_sessions"),
        ):
            self.window.launch_wemod()

        kind, spec, game_key, game_name, prefix = start.call_args.args
        self.assertEqual(kind.value, "wemod")
        self.assertEqual(game_key, self.window.current_game().key)
        self.assertEqual(game_name, self.window.current_game().name)
        self.assertEqual(
            prefix,
            self.window.current_game().default_prefix.resolve(strict=False),
        )
        self.assertEqual(spec.arguments[-1], "--wemod-only")

    def test_delete_wemod_requires_confirmation_and_keeps_prefix(self):
        prefix = self.window.current_game().default_prefix
        marker = prefix / "pfx" / ".wemod_installer"
        marker.parent.mkdir(parents=True)
        marker.touch()
        game_file = prefix / "pfx" / "drive_c" / "game" / "save.dat"
        game_file.parent.mkdir(parents=True)
        game_file.touch()
        self.window._update_wemod_status()
        self.assertTrue(self.window.delete_wemod_button.isEnabled())

        with patch.object(QMessageBox, "warning", return_value=QMessageBox.No):
            self.window.delete_wemod()

        self.assertTrue(marker.is_file())
        self.assertTrue(game_file.is_file())


if __name__ == "__main__":
    unittest.main()
