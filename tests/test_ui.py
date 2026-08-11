# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QMessageBox,
    QPushButton,
    QToolBar,
)

from proton_launcher.models import (
    GameEntry,
    GameSource,
    ProtonInstallation,
)  # noqa: E402
from proton_launcher.profiles import ConfigStore  # noqa: E402
from proton_launcher.protondb import protondb_app_id  # noqa: E402
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
        configure = next(
            button
            for button in self.window.findChildren(QPushButton)
            if button.text() == "Configure…"
        )
        with patch("proton_launcher.ui.SettingsDialog") as dialog:
            configure.click()
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
        with patch.object(self.window.sessions, "records", return_value=[object()]):
            self.window._update_wemod_status()
        self.assertFalse(self.window.launch_wemod_button.isEnabled())
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
