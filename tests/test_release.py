# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from proton_launcher.models import LaunchSpec, ProtonInstallation
from proton_launcher.profiles import (
    ConfigStore,
    ConfigValidationError,
    ConfigValidator,
    default_config,
)
from proton_launcher.proton import (
    discover_steam_default_tool,
    read_prefix_metadata,
    resolve_proton_choice,
)
from proton_launcher.runner import parse_environment_text
from proton_launcher.sessions import SessionKind, SessionManager


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_first_public_config_validates(self):
        report = ConfigValidator.validate(default_config())
        self.assertFalse(report.errors)

    def test_pre_release_config_is_rejected_without_migration(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps({"schema_version": 1, "profiles": {}}))
        with self.assertRaisesRegex(ConfigValidationError, "first-public-release"):
            ConfigStore(path)

    def test_missing_safe_fields_are_repaired(self):
        value = default_config()
        del value["settings"]["close_behavior"]
        report = ConfigValidator.validate(value)
        self.assertFalse(report.errors)
        self.assertEqual(report.repaired["settings"]["close_behavior"], "ask")

    def test_obsolete_custom_wemod_overrides_are_removed(self):
        store = ConfigStore(self.tmp / "config.json")
        key = "shortcut:/steam:1"
        store.ensure_game(key)
        value = store.data
        value["games"][key]["profiles"]["default"]["wemod_dll_overrides"] = "custom=n"

        report = ConfigValidator.validate(value)

        self.assertFalse(report.errors)
        self.assertNotIn(
            "wemod_dll_overrides",
            report.repaired["games"][key]["profiles"]["default"],
        )

    def test_default_profile_is_permanent(self):
        store = ConfigStore(self.tmp / "config.json")
        store.ensure_game("shortcut:/steam:1")
        profile = store.profiles("shortcut:/steam:1")[0]
        self.assertEqual(profile.id, "default")
        with self.assertRaisesRegex(ValueError, "cannot be deleted"):
            store.delete_profile(profile)

    def test_future_saves_create_rotating_backup(self):
        path = self.tmp / "config.json"
        store = ConfigStore(path)
        store.save()
        store.data["last_game"] = "one"
        store.save()
        self.assertTrue(path.with_name("config.json.bak.1").is_file())

    def test_environment_text_parser_preserves_semicolons_and_equals(self):
        parsed = parse_environment_text(
            'WINEDLLOVERRIDES="version=n,b;winhttp=n,b"\nTOKEN=a=b=c'
        )
        self.assertEqual(parsed["WINEDLLOVERRIDES"], "version=n,b;winhttp=n,b")
        self.assertEqual(parsed["TOKEN"], "a=b=c")

    def test_steam_default_mapping_and_resolution(self):
        root = self.tmp / "Steam"
        config = root / "config" / "config.vdf"
        config.parent.mkdir(parents=True)
        config.write_text(
            '"InstallConfigStore"\n{\n'
            '  "Software"\n  {\n    "Valve"\n    {\n      "Steam"\n'
            '      {\n        "CompatToolMapping"\n        {\n'
            '          "0"\n          {\n            "name" "GE-Test"\n'
            "          }\n        }\n      }\n    }\n  }\n}\n"
        )
        launcher = self.tmp / "GE-Test" / "proton"
        launcher.parent.mkdir()
        launcher.touch()
        installation = ProtonInstallation(
            "GE Test Display", launcher, launcher.parent, "test", "GE-Test"
        )
        name = discover_steam_default_tool([root])
        chosen, warning = resolve_proton_choice(
            [installation], {"mode": "steam", "path": ""}, name
        )
        self.assertEqual(name, "GE-Test")
        self.assertEqual(chosen, installation)
        self.assertEqual(warning, "")

    def test_prefix_metadata_matches_recorded_proton_root(self):
        proton_root = self.tmp / "DW-Proton Latest"
        launcher = proton_root / "proton"
        launcher.parent.mkdir()
        launcher.touch()
        prefix = self.tmp / "compatdata" / "42"
        prefix.mkdir(parents=True)
        (prefix / "config_info").write_text(
            "dwproton-11\n"
            f"{proton_root}/files/share/fonts/\n"
            f"{proton_root}/files/lib/\n"
        )
        metadata = read_prefix_metadata(
            prefix,
            [ProtonInstallation("DW-Proton Latest", launcher, proton_root, "test")],
        )
        self.assertEqual(metadata.state, "known")
        self.assertEqual(metadata.badge, "Prefix: DW-Proton Latest")

    def test_fallback_session_stops_its_process_group(self):
        environment = {
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_RUNTIME_DIR": str(self.tmp / "runtime"),
        }
        with (
            patch.dict(os.environ, environment),
            patch.object(SessionManager, "_detect_systemd", return_value=False),
        ):
            manager = SessionManager()
            record = manager.start(
                SessionKind.PRIMARY,
                LaunchSpec("/usr/bin/sleep", ["30"], dict(os.environ), str(self.tmp)),
                "game",
                "Game",
                self.tmp / "prefix",
            )
            for _ in range(30):
                if manager.is_active(record):
                    break
                time.sleep(0.05)
            self.assertTrue(manager.is_active(record))
            manager.stop(record)
            for _ in range(60):
                if not manager.is_active(record):
                    break
                time.sleep(0.05)
            self.assertFalse(manager.is_active(record))

    def test_background_followup_watcher_runs_without_gui(self):
        environment = {
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_RUNTIME_DIR": str(self.tmp / "runtime"),
        }
        prefix = self.tmp / "prefix"
        marker = self.tmp / "followup-ran"
        trigger = None
        with (
            patch.dict(os.environ, environment),
            patch.object(SessionManager, "_detect_systemd", return_value=False),
        ):
            manager = SessionManager()
            spec = LaunchSpec(
                "/usr/bin/python3",
                ["-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                dict(os.environ),
                str(self.tmp),
            )
            record = manager.start(
                SessionKind.FOLLOWUP,
                spec,
                "game",
                "Game",
                prefix,
                watch_target="trigger.exe",
            )
            trigger_env = dict(os.environ)
            trigger_env["STEAM_COMPAT_DATA_PATH"] = str(prefix)
            trigger = subprocess.Popen(
                ["/usr/bin/bash", "-c", "exec -a trigger.exe /usr/bin/sleep 2"],
                env=trigger_env,
            )
            for _ in range(80):
                if marker.exists():
                    break
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            manager.stop(record)
            for _ in range(20):
                if not manager.is_active(record):
                    break
                time.sleep(0.02)
        if trigger and trigger.poll() is None:
            trigger.terminate()
            trigger.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
