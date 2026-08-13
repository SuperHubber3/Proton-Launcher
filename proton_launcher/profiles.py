# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import json
import os
import shutil
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import LaunchProfile

CONFIG_FORMAT = "proton-launcher"
SCHEMA_VERSION = 1
DEFAULT_PROFILE_ID = "default"


def default_settings() -> dict[str, Any]:
    return {
        "custom_steam_roots": [],
        "custom_libraries": [],
        "custom_proton_locations": [],
        "default_proton": {"mode": "steam", "path": ""},
        "wemod_launcher_path": "",
        "close_behavior": "ask",
        "auto_hide_after_launch": False,
    }


def default_config() -> dict[str, Any]:
    return {
        "format": CONFIG_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "settings": default_settings(),
        "last_game": "",
        "games": {},
    }


def default_profile(game_key: str) -> LaunchProfile:
    return LaunchProfile(DEFAULT_PROFILE_ID, "Default", game_key)


@dataclass(slots=True)
class ValidationIssue:
    path: str
    severity: str
    message: str
    auto_repairable: bool = False
    proposed_value: Any = None


@dataclass(slots=True)
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)
    repaired: dict[str, Any] | None = None

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def changed(self) -> bool:
        return self.repaired is not None


class ConfigValidationError(ValueError):
    def __init__(
        self,
        path: Path,
        message: str,
        issues: list[ValidationIssue] | None = None,
    ):
        super().__init__(f"Could not load configuration {path}: {message}")
        self.path = path
        self.issues = issues or []


class ConfigValidator:
    """Validate the public configuration format and apply lossless repairs."""

    PROFILE_STRINGS = {
        "id",
        "name",
        "game_key",
        "proton_path",
        "mode",
        "executable",
        "command",
        "arguments",
        "working_directory",
        "environment_text",
        "overlay_app_id",
        "wait_for_executable",
        "followup_mode",
        "followup_executable",
        "followup_command",
        "followup_arguments",
        "dxvk_hud",
        "wine_debug",
        "gamescope_window_mode",
        "gamescope_scaler",
        "gamescope_filter",
        "gamescope_extra_arguments",
    }
    PROFILE_BOOLEANS = {
        "use_default_proton",
        "run_as_admin",
        "launch_through_steam",
        "inject_steam_overlay",
        "apply_online_fix",
        "launch_wemod",
        "enable_gamemode",
        "enable_mangohud",
        "enable_gamescope",
        "enable_wayland",
        "prefer_discrete_gpu",
        "enable_hdr",
        "force_nvapi",
        "disable_esync",
        "disable_fsync",
        "use_wined3d",
        "enable_proton_log",
        "force_large_address_aware",
        "prefer_sdl_input",
        "enable_wayland_raw_input",
        "gamescope_adaptive_sync",
        "followup_enabled",
        "wait_for_primary_executable",
        "followup_run_as_admin",
    }
    PROFILE_INTEGERS = {
        "gamescope_game_width",
        "gamescope_game_height",
        "gamescope_output_width",
        "gamescope_output_height",
        "gamescope_refresh_rate",
        "gamescope_fps_limit",
        "gamescope_sharpness",
    }

    @classmethod
    def validate(cls, value: Any) -> ValidationReport:
        issues: list[ValidationIssue] = []
        if not isinstance(value, dict):
            return ValidationReport(
                [ValidationIssue("$", "error", "the top-level value must be an object")]
            )
        data = deepcopy(value)
        changed = False
        if data.get("format") != CONFIG_FORMAT:
            issues.append(
                ValidationIssue(
                    "$.format",
                    "error",
                    "this is not the first-public-release configuration format",
                )
            )
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            message = (
                "configuration was created by a newer Proton Launcher"
                if isinstance(version, int) and version > SCHEMA_VERSION
                else f"expected schema version {SCHEMA_VERSION}"
            )
            issues.append(ValidationIssue("$.schema_version", "error", message))

        settings = data.get("settings")
        if settings is None:
            settings = default_settings()
            data["settings"] = settings
            changed = True
            issues.append(
                ValidationIssue(
                    "$.settings", "warning", "added missing settings", True, settings
                )
            )
        elif not isinstance(settings, dict):
            issues.append(
                ValidationIssue("$.settings", "error", "settings must be an object")
            )
            settings = {}

        defaults = default_settings()
        for key, default in defaults.items():
            if key not in settings:
                settings[key] = deepcopy(default)
                changed = True
                issues.append(
                    ValidationIssue(
                        f"$.settings.{key}",
                        "warning",
                        "added missing setting",
                        True,
                        default,
                    )
                )
        for key in (
            "custom_steam_roots",
            "custom_libraries",
            "custom_proton_locations",
        ):
            cls._validate_path_list(settings, key, issues)
            if isinstance(settings.get(key), list):
                cleaned = list(dict.fromkeys(item for item in settings[key] if item))
                if cleaned != settings[key]:
                    settings[key] = cleaned
                    changed = True
        if not isinstance(settings.get("wemod_launcher_path"), str):
            issues.append(
                ValidationIssue(
                    "$.settings.wemod_launcher_path",
                    "error",
                    "WeMod launcher path must be text",
                )
            )
        if settings.get("close_behavior") not in {
            "ask",
            "tray",
            "keep-running",
            "stop-and-exit",
        }:
            settings["close_behavior"] = "ask"
            changed = True
            issues.append(
                ValidationIssue(
                    "$.settings.close_behavior",
                    "warning",
                    "reset invalid close behavior to ask",
                    True,
                    "ask",
                )
            )
        if not isinstance(settings.get("auto_hide_after_launch"), bool):
            issues.append(
                ValidationIssue(
                    "$.settings.auto_hide_after_launch",
                    "error",
                    "auto-hide setting must be true or false",
                )
            )
        proton = settings.get("default_proton")
        if not isinstance(proton, dict):
            issues.append(
                ValidationIssue(
                    "$.settings.default_proton",
                    "error",
                    "default Proton setting must be an object",
                )
            )
        elif proton.get("mode") not in {"steam", "explicit"}:
            proton["mode"] = "steam"
            proton["path"] = ""
            changed = True
            issues.append(
                ValidationIssue(
                    "$.settings.default_proton.mode",
                    "warning",
                    "reset invalid Proton mode to Steam",
                    True,
                    "steam",
                )
            )
        elif not isinstance(proton.get("path", ""), str):
            issues.append(
                ValidationIssue(
                    "$.settings.default_proton.path",
                    "error",
                    "default Proton path must be text",
                )
            )

        if not isinstance(data.get("last_game", ""), str):
            data["last_game"] = ""
            changed = True
            issues.append(
                ValidationIssue(
                    "$.last_game",
                    "warning",
                    "cleared invalid last-game value",
                    True,
                    "",
                )
            )
        games = data.get("games")
        if games is None:
            games = {}
            data["games"] = games
            changed = True
            issues.append(
                ValidationIssue("$.games", "warning", "added games object", True, {})
            )
        elif not isinstance(games, dict):
            issues.append(
                ValidationIssue("$.games", "error", "games must be an object")
            )
            games = {}
        for game_key, game in games.items():
            game_path = f"$.games.{game_key}"
            if not isinstance(game, dict):
                issues.append(
                    ValidationIssue(game_path, "error", "game must be an object")
                )
                continue
            if not isinstance(game.get("prefix_override", ""), str):
                issues.append(
                    ValidationIssue(
                        f"{game_path}.prefix_override",
                        "error",
                        "prefix override must be text",
                    )
                )
            profiles = game.get("profiles")
            if profiles is None:
                profiles = {}
                game["profiles"] = profiles
                changed = True
            if not isinstance(profiles, dict):
                issues.append(
                    ValidationIssue(
                        f"{game_path}.profiles", "error", "profiles must be an object"
                    )
                )
                continue
            if DEFAULT_PROFILE_ID not in profiles:
                profiles[DEFAULT_PROFILE_ID] = default_profile(game_key).to_dict()
                changed = True
                issues.append(
                    ValidationIssue(
                        f"{game_path}.profiles.default",
                        "warning",
                        "created missing Default profile",
                        True,
                        profiles[DEFAULT_PROFILE_ID],
                    )
                )
            for profile_id, profile in profiles.items():
                if cls._validate_profile(
                    profile,
                    game_key,
                    profile_id,
                    f"{game_path}.profiles.{profile_id}",
                    issues,
                ):
                    changed = True
            last_profile = game.get("last_profile_id", DEFAULT_PROFILE_ID)
            if not isinstance(last_profile, str) or last_profile not in profiles:
                game["last_profile_id"] = DEFAULT_PROFILE_ID
                changed = True
        repaired = (
            data if changed and not any(i.severity == "error" for i in issues) else None
        )
        return ValidationReport(issues, repaired)

    @staticmethod
    def _validate_path_list(
        settings: dict[str, Any], key: str, issues: list[ValidationIssue]
    ) -> None:
        value = settings.get(key)
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            issues.append(
                ValidationIssue(
                    f"$.settings.{key}",
                    "error",
                    "location list must contain only text paths",
                )
            )

    @classmethod
    def _validate_profile(
        cls,
        profile: Any,
        game_key: str,
        profile_id: str,
        path: str,
        issues: list[ValidationIssue],
    ) -> bool:
        if not isinstance(profile, dict):
            issues.append(ValidationIssue(path, "error", "profile must be an object"))
            return False
        changed = False
        if "wemod_dll_overrides" in profile:
            del profile["wemod_dll_overrides"]
            changed = True
            issues.append(
                ValidationIssue(
                    f"{path}.wemod_dll_overrides",
                    "warning",
                    "removed obsolete custom WeMod DLL overrides",
                    True,
                )
            )
        expected = default_profile(game_key).to_dict()
        missing_fields: list[str] = []
        for key, default in expected.items():
            if key not in profile:
                profile[key] = deepcopy(default)
                changed = True
                missing_fields.append(key)
        if missing_fields:
            issues.append(
                ValidationIssue(
                    path,
                    "warning",
                    "added missing profile fields: " + ", ".join(missing_fields),
                    True,
                )
            )
        if profile.get("id") != profile_id:
            profile["id"] = profile_id
            changed = True
        if profile.get("game_key") != game_key:
            profile["game_key"] = game_key
            changed = True
        if profile_id == DEFAULT_PROFILE_ID:
            if profile.get("name") != "Default":
                profile["name"] = "Default"
                changed = True
        for key in cls.PROFILE_STRINGS:
            if not isinstance(profile.get(key), str):
                issues.append(
                    ValidationIssue(f"{path}.{key}", "error", f"{key} must be text")
                )
        for key in cls.PROFILE_BOOLEANS:
            if not isinstance(profile.get(key), bool):
                issues.append(
                    ValidationIssue(
                        f"{path}.{key}", "error", f"{key} must be true or false"
                    )
                )
        for key in cls.PROFILE_INTEGERS:
            value = profile.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                issues.append(
                    ValidationIssue(
                        f"{path}.{key}",
                        "error",
                        f"{key} must be a non-negative integer",
                    )
                )
        allowed_values = {
            "dxvk_hud": {"off", "fps", "1", "full"},
            "gamescope_window_mode": {"borderless", "fullscreen", "windowed"},
            "gamescope_scaler": {"auto", "integer", "fit", "fill", "stretch"},
            "gamescope_filter": {"linear", "nearest", "fsr", "nis", "pixel"},
        }
        for key, allowed in allowed_values.items():
            if profile.get(key) not in allowed:
                issues.append(
                    ValidationIssue(
                        f"{path}.{key}",
                        "error",
                        f"{key} must be one of: {', '.join(sorted(allowed))}",
                    )
                )
        if (
            isinstance(profile.get("gamescope_sharpness"), int)
            and not 0 <= profile["gamescope_sharpness"] <= 20
        ):
            issues.append(
                ValidationIssue(
                    f"{path}.gamescope_sharpness",
                    "error",
                    "gamescope_sharpness must be between 0 and 20",
                )
            )
        delay = profile.get("followup_delay")
        if not isinstance(delay, int | float) or isinstance(delay, bool):
            issues.append(
                ValidationIssue(
                    f"{path}.followup_delay",
                    "error",
                    "follow-up delay must be a number",
                )
            )
        return changed


class ConfigStore:
    def __init__(self, path: Path | None = None, read_only: bool = False):
        self.path = path or Path.home() / ".config" / "proton-launcher" / "config.json"
        self.read_only = read_only
        self.data = default_config()
        self.validation_issues: list[ValidationIssue] = []
        self.load()

    @property
    def settings(self) -> dict[str, Any]:
        return self.data["settings"]

    def load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except json.JSONDecodeError as exc:
            raise ConfigValidationError(
                self.path,
                f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
            ) from exc
        except OSError as exc:
            raise ConfigValidationError(self.path, str(exc)) from exc
        report = ConfigValidator.validate(loaded)
        if report.errors:
            details = "; ".join(
                f"{issue.path}: {issue.message}" for issue in report.errors
            )
            raise ConfigValidationError(self.path, details, report.issues)
        self.data = report.repaired or loaded
        self.validation_issues = report.issues
        if report.repaired is not None:
            self.save(create_backup=True)

    def save(self, create_backup: bool = True) -> None:
        if self.read_only:
            return
        report = ConfigValidator.validate(self.data)
        if report.errors:
            details = "; ".join(
                f"{issue.path}: {issue.message}" for issue in report.errors
            )
            raise ConfigValidationError(self.path, details, report.issues)
        if report.repaired is not None:
            self.data = report.repaired
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if create_backup and self.path.exists():
            self._rotate_backups()
        fd, temporary = tempfile.mkstemp(
            prefix="config-", suffix=".json", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _rotate_backups(self) -> None:
        for number in range(3, 1, -1):
            older = self.path.with_name(f"{self.path.name}.bak.{number - 1}")
            newer = self.path.with_name(f"{self.path.name}.bak.{number}")
            if older.exists():
                os.replace(older, newer)
        shutil.copy2(self.path, self.path.with_name(f"{self.path.name}.bak.1"))

    def ensure_game(self, game_key: str, template: LaunchProfile | None = None) -> None:
        if game_key in self.data["games"]:
            return
        profile = template or default_profile(game_key)
        profile.id = DEFAULT_PROFILE_ID
        profile.name = "Default"
        profile.game_key = game_key
        self.data["games"][game_key] = {
            "prefix_override": "",
            "last_profile_id": DEFAULT_PROFILE_ID,
            "profiles": {DEFAULT_PROFILE_ID: profile.to_dict()},
        }
        self.save()

    def game_data(self, game_key: str) -> dict[str, Any]:
        self.ensure_game(game_key)
        return self.data["games"][game_key]

    def profiles(self, game_key: str) -> list[LaunchProfile]:
        game = self.game_data(game_key)
        return [LaunchProfile.from_dict(item) for item in game["profiles"].values()]

    def put_profile(self, profile: LaunchProfile) -> None:
        game = self.game_data(profile.game_key)
        if profile.id == DEFAULT_PROFILE_ID:
            profile.name = "Default"
        game["profiles"][profile.id] = profile.to_dict()
        game["last_profile_id"] = profile.id
        self.save()

    def delete_profile(self, profile: LaunchProfile) -> None:
        if profile.id == DEFAULT_PROFILE_ID:
            raise ValueError("The Default profile cannot be deleted")
        game = self.game_data(profile.game_key)
        game["profiles"].pop(profile.id, None)
        game["last_profile_id"] = DEFAULT_PROFILE_ID
        self.save()

    def prefix_override(self, game_key: str) -> str:
        return str(self.game_data(game_key).get("prefix_override", ""))

    def set_prefix_override(self, game_key: str, path: str) -> None:
        self.game_data(game_key)["prefix_override"] = path
        self.save()
