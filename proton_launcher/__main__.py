# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from .profiles import ConfigStore, ConfigValidationError, ConfigValidator
from .ui import MainWindow


def _recover_store(app: QApplication) -> ConfigStore | None:
    del app
    while True:
        try:
            return ConfigStore()
        except ConfigValidationError as exc:
            box = QMessageBox()
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle("Configuration needs attention")
            details = "\n".join(
                f"{issue.path}: {issue.message}" for issue in exc.issues
            )
            box.setText(str(exc))
            if details:
                box.setDetailedText(details)
            backup = exc.path.with_name(f"{exc.path.name}.bak.1")
            restore = box.addButton("Restore last good backup", QMessageBox.AcceptRole)
            restore.setEnabled(backup.is_file())
            repair = box.addButton("Repair invalid values…", QMessageBox.ActionRole)
            repair.setEnabled(bool(exc.issues) and exc.path.is_file())
            open_button = box.addButton("Open config folder", QMessageBox.ActionRole)
            retry = box.addButton("Retry", QMessageBox.ActionRole)
            temporary = box.addButton(
                "Use temporary read-only defaults", QMessageBox.ActionRole
            )
            exit_button = box.addButton(QMessageBox.Close)
            box.exec()
            clicked = box.clickedButton()
            if clicked == restore:
                try:
                    shutil.copy2(backup, exc.path)
                except OSError as restore_error:
                    QMessageBox.critical(None, "Restore failed", str(restore_error))
                continue
            if clicked == repair:
                _interactive_repair(exc)
                continue
            if clicked == open_button:
                exc.path.parent.mkdir(parents=True, exist_ok=True)
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(exc.path.parent)))
                continue
            if clicked == retry:
                continue
            if clicked == temporary:
                path = (
                    Path(tempfile.mkdtemp(prefix="proton-launcher-read-only-"))
                    / "config.json"
                )
                return ConfigStore(path, read_only=True)
            if clicked == exit_button:
                return None


def _find_json_value(value, wanted: str, path: str = "$"):
    if path == wanted:
        return None, None, value
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if child_path == wanted:
                return value, key, child
            result = _find_json_value(child, wanted, child_path)
            if result is not None:
                return result
        # Validator errors also name keys that are absent (e.g. a missing
        # schema_version); attach them here so the replacement does not become
        # the whole document.
        if wanted.startswith(f"{path}."):
            remainder = wanted[len(path) + 1 :]
            if "." not in remainder:
                return value, remainder, None
    return None


def _interactive_repair(error: ConfigValidationError) -> None:
    try:
        original = json.loads(error.path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        QMessageBox.warning(None, "Cannot repair interactively", str(exc))
        return
    repaired = deepcopy(original)
    for issue in error.issues:
        if issue.severity != "error":
            continue
        located = _find_json_value(repaired, issue.path)
        current = located[2] if located else None
        prompt = (
            f"{issue.path}\n{issue.message}\n\n"
            "Enter the replacement as JSON. Strings need double quotes."
        )
        text, accepted = QInputDialog.getMultiLineText(
            None,
            "Repair configuration value",
            prompt,
            json.dumps(current, indent=2),
        )
        if not accepted:
            return
        try:
            replacement = json.loads(text)
        except json.JSONDecodeError as exc:
            QMessageBox.warning(
                None,
                "Invalid replacement",
                f"Line {exc.lineno}, column {exc.colno}: {exc.msg}",
            )
            return
        if located is None or located[0] is None:
            repaired = replacement
        else:
            parent, key, _old = located
            parent[key] = replacement
    report = ConfigValidator.validate(repaired)
    if report.errors:
        QMessageBox.warning(
            None,
            "Configuration still invalid",
            "\n".join(f"{item.path}: {item.message}" for item in report.errors),
        )
        return
    final = report.repaired or repaired
    error.path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="config-repair-", dir=error.path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(final, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, error.path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Proton Launcher")
    app.setQuitOnLastWindowClosed(False)
    store = _recover_store(app)
    if store is None:
        return 1
    try:
        window = MainWindow(store)
    except Exception as exc:
        QMessageBox.critical(None, "Proton Launcher could not start", str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
