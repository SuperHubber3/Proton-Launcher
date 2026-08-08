# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

DEFAULT_NON_STEAM_WINEDLLOVERRIDES = (
    "OnlineFix64=n;SteamOverlay64=n;winmm=n,b;dnet=n;steam_api64=n;winhttp=n,b"
)
DEFAULT_WEMOD_WINEDLLOVERRIDES = "version=n,b"


class GameSource(str, Enum):
    STEAM = "steam"
    SHORTCUT = "shortcut"


@dataclass(slots=True)
class GameEntry:
    source: GameSource
    app_id: int
    name: str
    steam_root: Path
    library_root: Path
    install_dir: Path | None = None
    shortcut_exe: str = ""
    shortcut_start_dir: str = ""
    default_executable: str = ""

    @property
    def key(self) -> str:
        return f"{self.source.value}:{self.steam_root}:{self.app_id}"

    @property
    def default_prefix(self) -> Path:
        return self.library_root / "steamapps" / "compatdata" / str(self.app_id)

    @property
    def label(self) -> str:
        suffix = "non-Steam" if self.source == GameSource.SHORTCUT else "Steam"
        return f"{self.name} - {self.app_id} ({suffix})"


@dataclass(slots=True)
class ProtonInstallation:
    display_name: str
    launcher: Path
    root: Path
    source: str
    steam_tool_id: str = ""


@dataclass(slots=True)
class PrefixMetadata:
    prefix: Path
    state: str
    display_name: str
    version: str = ""
    proton_root: Path | None = None

    @property
    def badge(self) -> str:
        if self.state == "uninitialized":
            return "Prefix: Uninitialized"
        if self.state == "known":
            return f"Prefix: {self.display_name}"
        return f"Prefix: Unknown ({self.version or 'unrecognized'})"


@dataclass(slots=True)
class LaunchProfile:
    id: str
    name: str
    game_key: str
    proton_path: str = ""
    mode: str = "executable"
    executable: str = ""
    command: str = ""
    arguments: str = ""
    working_directory: str = ""
    environment_text: str = ""
    run_as_admin: bool = False
    launch_through_steam: bool = False
    inject_steam_overlay: bool = False
    overlay_app_id: str = ""
    apply_online_fix: bool = False
    launch_wemod: bool = False
    use_default_proton: bool = True
    followup_enabled: bool = False
    wait_for_executable: str = ""
    wait_for_primary_executable: bool = False
    followup_delay: float = 0.0
    followup_mode: str = "executable"
    followup_executable: str = ""
    followup_command: str = ""
    followup_arguments: str = ""
    followup_run_as_admin: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LaunchProfile:
        fields = cls.__dataclass_fields__
        return cls(**{key: item for key, item in value.items() if key in fields})


@dataclass(slots=True)
class LaunchSpec:
    program: str
    arguments: list[str]
    environment: dict[str, str]
    working_directory: str


@dataclass(slots=True)
class DiscoveryIssue:
    path: Path
    message: str
    severity: str = "warning"
