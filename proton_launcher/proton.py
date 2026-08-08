# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from pathlib import Path

import vdf

from .models import DiscoveryIssue, PrefixMetadata, ProtonInstallation
from .util import expanded_path

DEFAULT_ROOTS = (
    "~/.local/share/Steam/compatibilitytools.d",
    "/usr/share/steam/compatibilitytools.d",
)


def _manifest_identity(root: Path) -> tuple[str, str]:
    manifest = root / "compatibilitytool.vdf"
    try:
        with manifest.open(encoding="utf-8", errors="replace") as handle:
            tools = (
                vdf.load(handle).get("compatibilitytools", {}).get("compat_tools", {})
            )
        if tools:
            tool_id, entry = next(iter(tools.items()))
            return str(entry.get("display_name") or root.name), str(tool_id)
    except (OSError, ValueError, AttributeError):
        pass
    return root.name, ""


def discover_protons(
    custom: list[str] | None = None,
    steam_roots: list[Path] | None = None,
    steam_libraries: list[Path] | None = None,
) -> tuple[list[ProtonInstallation], list[DiscoveryIssue]]:
    raw_roots = [
        *DEFAULT_ROOTS,
        *(str(root / "compatibilitytools.d") for root in steam_roots or []),
        *(str(library / "steamapps" / "common") for library in steam_libraries or []),
        *(custom or []),
    ]
    candidates: list[tuple[Path, str]] = []
    issues: list[DiscoveryIssue] = []
    for raw in raw_roots:
        path = expanded_path(raw)
        if path.is_file():
            candidates.append((path, "custom"))
        elif path.is_dir():
            if (path / "proton").is_file():
                candidates.append(
                    (
                        path / "proton",
                        "custom" if raw in (custom or []) else str(path.parent),
                    )
                )
            try:
                for child in path.iterdir():
                    if child.is_dir() and (child / "proton").is_file():
                        candidates.append((child / "proton", str(path)))
            except OSError as exc:
                issues.append(
                    DiscoveryIssue(path, f"Could not scan Proton directory: {exc}")
                )
    result: list[ProtonInstallation] = []
    seen: set[Path] = set()
    for launcher, source in candidates:
        canonical = launcher.resolve(strict=False)
        if canonical in seen:
            continue
        seen.add(canonical)
        display_name, tool_id = _manifest_identity(launcher.parent)
        result.append(
            ProtonInstallation(
                display_name,
                launcher,
                launcher.parent,
                source,
                tool_id,
            )
        )
    result.sort(key=lambda item: item.display_name.casefold())
    return result, issues


def discover_steam_default_tool(steam_roots: list[Path]) -> str:
    """Return Steam's default CompatToolMapping name, if configured."""
    for root in steam_roots:
        config = root / "config" / "config.vdf"
        try:
            with config.open(encoding="utf-8", errors="replace") as handle:
                parsed = vdf.load(handle)
            mapping = (
                parsed.get("InstallConfigStore", {})
                .get("Software", {})
                .get("Valve", {})
                .get("Steam", {})
                .get("CompatToolMapping", {})
                .get("0", {})
            )
            name = str(mapping.get("name", "")).strip()
            if name:
                return name
        except (OSError, ValueError, AttributeError):
            continue
    return ""


def resolve_proton_choice(
    installations: list[ProtonInstallation],
    default_setting: dict[str, str],
    steam_tool_name: str,
) -> tuple[ProtonInstallation | None, str]:
    """Resolve an explicit or Steam-following launcher default."""
    if not installations:
        return None, "No Proton installations were discovered"
    if default_setting.get("mode") == "explicit":
        wanted = (
            Path(default_setting.get("path", "")).expanduser().resolve(strict=False)
        )
        match = next(
            (
                item
                for item in installations
                if item.launcher.resolve(strict=False) == wanted
            ),
            None,
        )
        if match:
            return match, ""
        return (
            installations[0],
            "Configured default Proton is unavailable; using fallback",
        )
    wanted = steam_tool_name.casefold()
    match = next(
        (
            item
            for item in installations
            if wanted
            and wanted in {item.steam_tool_id.casefold(), item.display_name.casefold()}
        ),
        None,
    )
    if match:
        return match, ""
    message = (
        f"Steam default {steam_tool_name!r} was not discovered; using fallback"
        if steam_tool_name
        else "Steam has no default Proton mapping; using fallback"
    )
    return installations[0], message


def read_prefix_metadata(
    prefix: Path, installations: list[ProtonInstallation]
) -> PrefixMetadata:
    """Read the Proton build and installation root recorded in a prefix."""
    config_info = prefix / "config_info"
    version_file = prefix / "version"
    if not config_info.is_file() and not version_file.is_file():
        return PrefixMetadata(prefix, "uninitialized", "Uninitialized")
    version = ""
    proton_root: Path | None = None
    try:
        lines = config_info.read_text(errors="replace").splitlines()
        if lines:
            version = lines[0].strip()
        if len(lines) > 1 and lines[1].strip():
            fonts = Path(lines[1].strip()).resolve(strict=False)
            if len(fonts.parents) >= 3:
                proton_root = fonts.parents[2]
    except OSError:
        pass
    if not version:
        try:
            version = version_file.read_text(errors="replace").strip()
        except OSError:
            pass
    if proton_root:
        match = next(
            (
                item
                for item in installations
                if item.root.resolve(strict=False) == proton_root
            ),
            None,
        )
        if match:
            return PrefixMetadata(
                prefix, "known", match.display_name, version, proton_root
            )
    return PrefixMetadata(prefix, "unknown", "Unknown", version, proton_root)
