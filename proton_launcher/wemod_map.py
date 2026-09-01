# SPDX-License-Identifier: GPL-3.0-only
"""Apply the reversible WeMod embedded-map compatibility patch."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

MAP_IFRAME = b"this.iframeEl.src=t.url;"
MAP_BROWSER = b"x(t.url);this.loading=0;"
MAP_ANALYTICS = b'this.#s.screenView({name:this.titleInfo.name,class:"Map"})'
MAP_BROWSER_HOOK = b'globalThis.x=e=>require("electron").shell.openExternal(e) '

if len(MAP_IFRAME) != len(MAP_BROWSER) or len(MAP_ANALYTICS) != len(MAP_BROWSER_HOOK):
    raise RuntimeError("WeMod map replacements must preserve the archive length")


def wemod_asar_path(launcher: str | Path) -> Path:
    """Resolve app.asar from a configured wemod-launcher executable."""
    configured = Path(launcher).expanduser().resolve(strict=False)
    root = (
        configured.parent.parent
        if configured.parent.name == "src"
        else configured.parent
    )
    return root / "wemod_data" / "wemod_bin" / "resources" / "app.asar"


def map_patch_state(asar: Path) -> str:
    """Return missing, available, patched, or unsupported."""
    if not asar.is_file():
        return "missing"
    try:
        data = asar.read_bytes()
    except OSError:
        return "unsupported"
    original = MAP_IFRAME in data and MAP_ANALYTICS in data
    patched = MAP_BROWSER in data and MAP_BROWSER_HOOK in data
    if original and not patched:
        return "available"
    if patched and not original:
        return "patched"
    return "unsupported"


def map_patch_backup(asar: Path) -> Path:
    return asar.with_name(asar.name + ".proton-launcher-map-backup")


def _replace_file(path: Path, data: bytes) -> None:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        # Restoring from backup can recreate a deleted app.asar.
        mode = 0o644
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_name = temporary.name
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def apply_map_browser_patch(asar: Path) -> bool:
    """Open WeMod maps in the system browser instead of its Wine iframe."""
    state = map_patch_state(asar)
    if state == "patched":
        return False
    if state != "available":
        raise ValueError("This WeMod build does not contain the supported map code")

    backup = map_patch_backup(asar)
    if backup.exists():
        if map_patch_state(backup) != "available":
            raise ValueError(f"The existing map backup is not usable: {backup}")
        if backup.read_bytes() != asar.read_bytes():
            shutil.copy2(asar, backup)
    else:
        shutil.copy2(asar, backup)

    data = asar.read_bytes()
    patched = data.replace(MAP_IFRAME, MAP_BROWSER).replace(
        MAP_ANALYTICS, MAP_BROWSER_HOOK
    )
    _replace_file(asar, patched)
    if map_patch_state(asar) != "patched":
        _replace_file(asar, backup.read_bytes())
        raise OSError("The WeMod map patch could not be verified")
    return True


def restore_wemod_maps(asar: Path) -> bool:
    """Restore the app.asar saved before the browser-map patch."""
    state = map_patch_state(asar)
    if state == "available":
        return False
    if state not in ("patched", "missing"):
        raise ValueError("The installed WeMod map code is not recognized")
    backup = map_patch_backup(asar)
    if map_patch_state(backup) != "available":
        raise ValueError(f"The original WeMod map backup is missing: {backup}")
    _replace_file(asar, backup.read_bytes())
    if map_patch_state(asar) != "available":
        raise OSError("The restored WeMod map code could not be verified")
    backup.unlink(missing_ok=True)
    return True
