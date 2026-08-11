# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .profiles import ConfigStore
from .proton import DEFAULT_ROOTS
from .sessions import state_root
from .steam import DEFAULT_STEAM_ROOTS
from .wemod_map import (
    apply_map_browser_patch,
    map_patch_state,
    restore_wemod_maps,
    wemod_asar_path,
)


class LocationEditor(QGroupBox):
    def __init__(
        self,
        title: str,
        values: list[str],
        automatic: list[str] | tuple[str, ...] = (),
        parent=None,
    ):
        super().__init__(title, parent)
        self.automatic = list(automatic)
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        layout.addWidget(self.list)
        row = QHBoxLayout()
        for label, callback in (
            ("Add…", self.add),
            ("Edit…", self.edit),
            ("Remove", self.remove),
            ("Open", self.open),
            ("Reset custom", self.reset),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        self.set_values(values)

    def set_values(self, values: list[str]) -> None:
        self.list.clear()
        for path in self.automatic:
            item = QListWidgetItem(f"Detected: {Path(path).expanduser()}")
            item.setData(Qt.UserRole, (False, path))
            # QPalette.mid() can be almost indistinguishable from the base on
            # dark themes. PlaceholderText is intended to remain readable
            # while still visually separating detected and custom entries.
            item.setForeground(self.palette().color(QPalette.PlaceholderText))
            self.list.addItem(item)
        for path in values:
            item = QListWidgetItem(path)
            item.setData(Qt.UserRole, (True, path))
            self.list.addItem(item)

    def values(self) -> list[str]:
        result: list[str] = []
        for index in range(self.list.count()):
            custom, path = self.list.item(index).data(Qt.UserRole)
            if custom:
                result.append(path)
        return result

    def _selected_custom(self) -> tuple[QListWidgetItem | None, str]:
        item = self.list.currentItem()
        if not item:
            return None, ""
        custom, path = item.data(Qt.UserRole)
        return (item, path) if custom else (None, path)

    def add(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, f"Add {self.title()}", str(Path.home())
        )
        if path and path not in self.values():
            item = QListWidgetItem(path)
            item.setData(Qt.UserRole, (True, path))
            self.list.addItem(item)

    def edit(self) -> None:
        item, path = self._selected_custom()
        if not item:
            return
        replacement = QFileDialog.getExistingDirectory(
            self, f"Edit {self.title()}", path
        )
        if replacement:
            item.setText(replacement)
            item.setData(Qt.UserRole, (True, replacement))

    def remove(self) -> None:
        item, _path = self._selected_custom()
        if item:
            self.list.takeItem(self.list.row(item))

    def reset(self) -> None:
        self.set_values([])

    def open(self) -> None:
        item = self.list.currentItem()
        if not item:
            return
        _custom, path = item.data(Qt.UserRole)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).expanduser())))


class SettingsDialog(QDialog):
    def __init__(
        self,
        store: ConfigStore,
        protons,
        steam_default_name: str,
        steam_roots: list[Path],
        libraries: list[Path],
        supervision_name: str,
        refresh_callback,
        parent=None,
        initial_tab: str = "general",
    ):
        super().__init__(parent)
        self.setWindowTitle("Proton Launcher Settings")
        self.resize(760, 560)
        self.store = store
        self.protons = protons
        self.refresh_callback = refresh_callback
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        general = QWidget()
        form = QFormLayout(general)
        self.default_proton = QComboBox()
        self.default_proton.addItem(
            f"Follow Steam ({steam_default_name or 'not configured'})", ("steam", "")
        )
        for proton in protons:
            self.default_proton.addItem(
                proton.display_name, ("explicit", str(proton.launcher))
            )
        configured = store.settings["default_proton"]
        wanted = (configured["mode"], configured.get("path", ""))
        index = self.default_proton.findData(wanted)
        self.default_proton.setCurrentIndex(max(0, index))
        self.close_behavior = QComboBox()
        for label, value in (
            ("Ask while a session is running", "ask"),
            ("Hide to tray", "tray"),
            ("Exit and keep sessions running", "keep-running"),
            ("Stop all and exit", "stop-and-exit"),
        ):
            self.close_behavior.addItem(label, value)
        self.close_behavior.setCurrentIndex(
            max(0, self.close_behavior.findData(store.settings["close_behavior"]))
        )
        self.auto_hide = QCheckBox(
            "Automatically hide to tray after a successful launch"
        )
        self.auto_hide.setChecked(store.settings["auto_hide_after_launch"])
        backend = QLabel(supervision_name)
        if "fallback" in supervision_name:
            backend.setStyleSheet("color: #d08c34")
        form.addRow("Launcher default Proton", self.default_proton)
        form.addRow("Closing with sessions", self.close_behavior)
        form.addRow("", self.auto_hide)
        form.addRow("Session supervision", backend)
        folders = QHBoxLayout()
        for label, path in (
            ("Config folder", store.path.parent),
            ("State / logs", state_root()),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _checked=False, value=path: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(value))
                )
            )
            folders.addWidget(button)
        folders.addStretch()
        form.addRow("Open", folders)
        self.tabs.addTab(general, "General")

        locations = QWidget()
        location_layout = QVBoxLayout(locations)
        settings = store.settings
        self.steam_editor = LocationEditor(
            "Steam root", settings["custom_steam_roots"], DEFAULT_STEAM_ROOTS
        )
        self.library_editor = LocationEditor(
            "Steam library",
            settings["custom_libraries"],
            [
                str(path)
                for path in libraries
                if str(path) not in settings["custom_libraries"]
            ],
        )
        self.proton_editor = LocationEditor(
            "Proton location", settings["custom_proton_locations"], DEFAULT_ROOTS
        )
        location_layout.addWidget(self.steam_editor)
        location_layout.addWidget(self.library_editor)
        location_layout.addWidget(self.proton_editor)
        self.tabs.addTab(locations, "Locations")

        integrations = QWidget()
        integration_form = QFormLayout(integrations)
        self.wemod_path = QLineEdit(store.settings["wemod_launcher_path"])
        self.wemod_path.editingFinished.connect(self._update_map_status)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_wemod)
        path_row = QHBoxLayout()
        path_row.addWidget(self.wemod_path, 1)
        path_row.addWidget(browse)
        launch_mode = QLabel("Separate Proton processes and environments")
        integration_form.addRow("WeMod Launcher", path_row)
        integration_form.addRow("Launch mode", launch_mode)
        map_row = QHBoxLayout()
        self.map_status = QLabel()
        self.map_status.setWordWrap(True)
        map_row.addWidget(self.map_status, 1)
        self.map_patch_button = QPushButton("Open maps in browser")
        self.map_patch_button.clicked.connect(self._patch_wemod_maps)
        map_row.addWidget(self.map_patch_button)
        self.map_restore_button = QPushButton("Restore in-app maps")
        self.map_restore_button.clicked.connect(self._restore_wemod_maps)
        map_row.addWidget(self.map_restore_button)
        integration_form.addRow("Maps", map_row)
        renderers = []
        for root_path in steam_roots:
            for architecture in ("ubuntu12_32", "ubuntu12_64"):
                renderer = root_path / architecture / "gameoverlayrenderer.so"
                if renderer.is_file():
                    renderers.append(str(renderer))
        overlay_status = QLabel(
            "Both renderer architectures found"
            if len(renderers) >= 2
            else "Overlay renderer installation is incomplete"
        )
        overlay_status.setToolTip("\n".join(renderers) or "No renderer files found")
        integration_form.addRow("Steam overlay", overlay_status)
        self.tabs.addTab(integrations, "Integrations")
        self.tabs.setCurrentIndex(
            {"general": 0, "locations": 1, "integrations": 2}.get(initial_tab, 0)
        )
        self._update_map_status()

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _browse_wemod(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose WeMod Launcher", self.wemod_path.text() or str(Path.home())
        )
        if path:
            self.wemod_path.setText(path)
            self._update_map_status()

    def _update_map_status(self) -> None:
        configured = self.wemod_path.text().strip()
        state = (
            map_patch_state(wemod_asar_path(configured)) if configured else "missing"
        )
        labels = {
            "missing": "WeMod installation not found",
            "available": "Embedded in WeMod",
            "patched": "Opens in the system browser",
            "unsupported": "Unsupported WeMod build",
        }
        self.map_status.setText(labels[state])
        self.map_patch_button.setEnabled(state == "available")
        self.map_restore_button.setEnabled(state == "patched")

    def _patch_wemod_maps(self) -> None:
        asar = wemod_asar_path(self.wemod_path.text().strip())
        caught: OSError | ValueError | None = None
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            apply_map_browser_patch(asar)
            self._update_map_status()
        except (OSError, ValueError) as error:
            caught = error
        finally:
            QApplication.restoreOverrideCursor()
        if caught is not None:
            QMessageBox.warning(self, "Could not patch WeMod maps", str(caught))
            return
        QMessageBox.information(
            self,
            "WeMod maps",
            "WeMod maps will open in the system browser. The original app.asar "
            "was backed up and can be restored here. Restart WeMod if it is "
            "currently running.",
        )

    def _restore_wemod_maps(self) -> None:
        asar = wemod_asar_path(self.wemod_path.text().strip())
        caught: OSError | ValueError | None = None
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            restore_wemod_maps(asar)
            self._update_map_status()
        except (OSError, ValueError) as error:
            caught = error
        finally:
            QApplication.restoreOverrideCursor()
        if caught is not None:
            QMessageBox.warning(self, "Could not restore WeMod maps", str(caught))
            return
        QMessageBox.information(
            self,
            "WeMod maps",
            "In-app maps were restored. Restart WeMod if it is currently running.",
        )

    def _save(self) -> None:
        mode, path = self.default_proton.currentData()
        settings = self.store.settings
        settings["default_proton"] = {"mode": mode, "path": path}
        settings["close_behavior"] = self.close_behavior.currentData()
        settings["auto_hide_after_launch"] = self.auto_hide.isChecked()
        settings["custom_steam_roots"] = self.steam_editor.values()
        settings["custom_libraries"] = self.library_editor.values()
        settings["custom_proton_locations"] = self.proton_editor.values()
        settings["wemod_launcher_path"] = self.wemod_path.text().strip()
        self.store.save()
        self.refresh_callback()
        self.accept()
