# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import vdf

from proton_launcher.steam_accounts import (
    load_account_state,
    steam_is_running,
    switch_account,
)

LOGINUSERS = """"users"
{
    "100"
    {
        "AccountName" "alice"
        "PersonaName" "Alice"
        "RememberPassword" "1"
        "AutoLogin" "1"
        "MostRecent" "1"
    }
    "200"
    {
        "AccountName" "bob"
        "PersonaName" "Bob"
        "RememberPassword" "0"
        "AutoLogin" "0"
        "MostRecent" "0"
    }
}
"""

REGISTRY = """"Registry"
{
    "HKCU"
    {
        "Software"
        {
            "Valve"
            {
                "Steam"
                {
                    "AutoLoginUser" "alice"
                }
            }
        }
    }
}
"""

CONFIG = """"InstallConfigStore"
{
    "Software"
    {
        "Valve"
        {
            "Steam"
            {
                "ShaderCacheManager"
                {
                    "DisableShaderCache" "0"
                }
            }
        }
    }
}
"""


class SteamAccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.steam_root = base / "Steam"
        (self.steam_root / "config").mkdir(parents=True)
        self.registry = base / ".steam" / "registry.vdf"
        self.registry.parent.mkdir()
        (self.steam_root / "config" / "loginusers.vdf").write_text(LOGINUSERS)
        (self.steam_root / "config" / "config.vdf").write_text(CONFIG)
        self.registry.write_text(REGISTRY)

    def test_loads_saved_accounts_and_current_settings(self):
        state = load_account_state(self.steam_root, self.registry)

        self.assertEqual(
            [account.steam_id for account in state.accounts], ["100", "200"]
        )
        self.assertEqual(state.current_steam_id, "100")
        self.assertFalse(state.shader_cache_disabled)
        self.assertTrue(state.accounts[0].remember_password)
        self.assertFalse(state.accounts[1].remember_password)

    def test_switches_account_and_shader_cache(self):
        switch_account(self.steam_root, "200", True, self.registry)

        users = self._load(self.steam_root / "config" / "loginusers.vdf")["users"]
        registry = self._load(self.registry)["Registry"]["HKCU"]["Software"]["Valve"][
            "Steam"
        ]
        shader = self._load(self.steam_root / "config" / "config.vdf")[
            "InstallConfigStore"
        ]["Software"]["Valve"]["Steam"]["ShaderCacheManager"]
        self.assertEqual(users["100"]["AutoLogin"], "0")
        self.assertEqual(users["100"]["MostRecent"], "0")
        self.assertEqual(users["200"]["AutoLogin"], "1")
        self.assertEqual(users["200"]["MostRecent"], "1")
        self.assertEqual(registry["AutoLoginUser"], "bob")
        self.assertEqual(shader["DisableShaderCache"], "1")
        self.assertTrue(
            (self.registry.parent / "registry.vdf.proton-launcher.bak").is_file()
        )

    def test_unknown_account_does_not_modify_steam_files(self):
        paths = (
            self.steam_root / "config" / "loginusers.vdf",
            self.registry,
            self.steam_root / "config" / "config.vdf",
        )
        originals = {path: path.read_bytes() for path in paths}

        with self.assertRaisesRegex(ValueError, "not a saved Steam account"):
            switch_account(self.steam_root, "999", False, self.registry)

        self.assertEqual({path: path.read_bytes() for path in paths}, originals)

    def test_adds_missing_shader_cache_setting_when_disabling(self):
        config = self.steam_root / "config" / "config.vdf"
        config.write_text(CONFIG.replace('"DisableShaderCache" "0"\n', ""))

        switch_account(self.steam_root, "200", True, self.registry)

        shader = self._load(config)["InstallConfigStore"]["Software"]["Valve"]["Steam"][
            "ShaderCacheManager"
        ]
        self.assertEqual(shader["DisableShaderCache"], "1")

    def test_creates_missing_shader_cache_manager_object(self):
        config = self.steam_root / "config" / "config.vdf"
        config.write_text(
            '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
            '\t\t\t"Steam"\n\t\t\t{\n\t\t\t}\n\t\t}\n\t}\n}\n'
        )

        switch_account(self.steam_root, "200", True, self.registry)

        shader = self._load(config)["InstallConfigStore"]["Software"]["Valve"]["Steam"][
            "ShaderCacheManager"
        ]
        self.assertEqual(shader["DisableShaderCache"], "1")

    def test_creates_missing_auto_login_user_in_registry(self):
        self.registry.write_text(
            '"Registry"\n{\n\t"HKCU"\n\t{\n\t\t"Software"\n\t\t{\n\t\t\t"Valve"\n'
            '\t\t\t{\n\t\t\t\t"Steam"\n\t\t\t\t{\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n'
        )

        switch_account(self.steam_root, "200", False, self.registry)

        registry = self._load(self.registry)["Registry"]["HKCU"]["Software"]["Valve"][
            "Steam"
        ]
        self.assertEqual(registry["AutoLoginUser"], "bob")

    def test_most_recent_marks_current_account_when_auto_login_disabled(self):
        (self.steam_root / "config" / "loginusers.vdf").write_text(
            LOGINUSERS.replace('"AutoLogin" "1"', '"AutoLogin" "0"')
        )
        self.registry.write_text(
            REGISTRY.replace('"AutoLoginUser" "alice"', '"AutoLoginUser" ""')
        )

        state = load_account_state(self.steam_root, self.registry)

        self.assertEqual(state.current_steam_id, "100")

    def _fake_process(self, proc: Path, pid: str, comm: str, state: str) -> Path:
        entry = proc / pid
        entry.mkdir(parents=True)
        (entry / "comm").write_text(f"{comm}\n")
        (entry / "stat").write_text(f"{pid} ({comm}) {state} 1 0 0\n")
        return entry

    def test_steam_is_running_scan_ignores_zombies_and_other_names(self):
        proc = Path(self.temporary.name) / "proc"
        missing_pid_file = proc / "steam.pid"
        self._fake_process(proc, "10", "steamwebhelper", "S")
        self._fake_process(proc, "11", "steam", "Z")
        self.assertFalse(steam_is_running(proc_root=proc, pid_file=missing_pid_file))

        self._fake_process(proc, "12", "steam", "S")
        self.assertTrue(steam_is_running(proc_root=proc, pid_file=missing_pid_file))

    def test_steam_is_running_trusts_the_pid_file(self):
        proc = Path(self.temporary.name) / "proc"
        pid_file = Path(self.temporary.name) / "steam.pid"
        # An unrelated live "steam" process (for example a sandboxed client)
        # must not count when the recorded native PID is dead.
        self._fake_process(proc, "12", "steam", "S")
        pid_file.write_text("99\n")
        self.assertFalse(steam_is_running(proc_root=proc, pid_file=pid_file))

        pid_file.write_text("12\n")
        self.assertTrue(steam_is_running(proc_root=proc, pid_file=pid_file))

    def test_steam_is_running_checks_the_executable_against_the_root(self):
        proc = Path(self.temporary.name) / "proc"
        pid_file = Path(self.temporary.name) / "steam.pid"
        entry = self._fake_process(proc, "12", "steam", "S")
        outside = Path(self.temporary.name) / "flatpak" / "steam"
        outside.parent.mkdir()
        outside.touch()
        (entry / "exe").symlink_to(outside)
        pid_file.write_text("12\n")
        self.assertFalse(
            steam_is_running(self.steam_root, proc_root=proc, pid_file=pid_file)
        )

        inside = self.steam_root / "ubuntu12_32" / "steam"
        inside.parent.mkdir(parents=True)
        inside.touch()
        (entry / "exe").unlink()
        (entry / "exe").symlink_to(inside)
        self.assertTrue(
            steam_is_running(self.steam_root, proc_root=proc, pid_file=pid_file)
        )

    @staticmethod
    def _load(path: Path):
        with path.open() as handle:
            return vdf.load(handle)


if __name__ == "__main__":
    unittest.main()
