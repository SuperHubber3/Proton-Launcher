# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import shutil

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .models import LaunchProfile


class RuntimeOptionsDialog(QDialog):
    def __init__(self, profile: LaunchProfile, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Launch options")
        self.resize(600, 520)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._general_tab(profile), "General")
        self.tabs.addTab(self._gamescope_tab(profile), "Gamescope")
        self.tabs.addTab(self._troubleshooting_tab(profile), "Troubleshooting")
        if profile.launch_wemod:
            self.tabs.setTabEnabled(1, False)
            self.tabs.setTabToolTip(
                1, "Unavailable because Gamescope is incompatible with WeMod"
            )
        if profile.use_native_runtime:
            self.tabs.setTabEnabled(2, False)
            self.tabs.setTabToolTip(
                2, "These options only apply to Proton and Wine programs"
            )

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(buttons)

    @staticmethod
    def _spin(value: int, maximum: int = 16384, automatic: bool = True) -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(0, maximum)
        if automatic:
            widget.setSpecialValueText("Automatic")
        widget.setValue(value)
        return widget

    def _general_tab(self, profile: LaunchProfile) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.discrete_gpu = QCheckBox("Prefer the discrete GPU")
        self.discrete_gpu.setChecked(profile.prefer_discrete_gpu)
        self.hdr = QCheckBox("Enable HDR (also enables native Wayland)")
        self.hdr.setChecked(profile.enable_hdr)
        self.force_nvapi = QCheckBox("Force NVAPI")
        self.force_nvapi.setChecked(profile.force_nvapi)
        self.raw_input = QCheckBox("Use unaccelerated mouse input on native Wayland")
        self.raw_input.setChecked(profile.enable_wayland_raw_input)
        if profile.launch_wemod:
            unavailable = (
                "Unavailable with WeMod because WeMod and the game share one Wine "
                "display driver"
            )
            for widget in (self.hdr, self.raw_input):
                widget.setEnabled(False)
                widget.setToolTip(unavailable)
        self.sdl_input = QCheckBox("Prefer SDL controller input")
        self.sdl_input.setChecked(profile.prefer_sdl_input)
        self.dxvk_hud = QComboBox()
        for label, value in (
            ("Off", "off"),
            ("FPS", "fps"),
            ("GPU and FPS", "1"),
            ("Full", "full"),
        ):
            self.dxvk_hud.addItem(label, value)
        self.dxvk_hud.setCurrentIndex(max(0, self.dxvk_hud.findData(profile.dxvk_hud)))
        if profile.use_native_runtime:
            self.hdr.setText("Enable HDR through Gamescope")
            unavailable = "This option only applies to Proton and Wine programs"
            for widget in (
                self.force_nvapi,
                self.raw_input,
                self.sdl_input,
                self.dxvk_hud,
            ):
                widget.setEnabled(False)
                widget.setToolTip(unavailable)
        form.addRow("GPU", self.discrete_gpu)
        form.addRow("HDR", self.hdr)
        form.addRow("NVIDIA", self.force_nvapi)
        form.addRow("Mouse", self.raw_input)
        form.addRow("Controller", self.sdl_input)
        form.addRow("DXVK HUD", self.dxvk_hud)
        return tab

    def _gamescope_tab(self, profile: LaunchProfile) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        available = shutil.which("gamescope")
        status = QLabel(
            f"Found {available}" if available else "Gamescope is not installed"
        )
        form.addRow("Status", status)
        self.gamescope_mode = QComboBox()
        for label, value in (
            ("Borderless", "borderless"),
            ("Fullscreen", "fullscreen"),
            ("Windowed", "windowed"),
        ):
            self.gamescope_mode.addItem(label, value)
        self.gamescope_mode.setCurrentIndex(
            max(0, self.gamescope_mode.findData(profile.gamescope_window_mode))
        )
        self.game_width = self._spin(profile.gamescope_game_width)
        self.game_height = self._spin(profile.gamescope_game_height)
        self.output_width = self._spin(profile.gamescope_output_width)
        self.output_height = self._spin(profile.gamescope_output_height)
        self.refresh_rate = self._spin(profile.gamescope_refresh_rate, 1000)
        self.fps_limit = self._spin(profile.gamescope_fps_limit, 1000)
        self.gamescope_scaler = QComboBox()
        for value in ("auto", "integer", "fit", "fill", "stretch"):
            self.gamescope_scaler.addItem(value.capitalize(), value)
        self.gamescope_scaler.setCurrentIndex(
            max(0, self.gamescope_scaler.findData(profile.gamescope_scaler))
        )
        self.gamescope_filter = QComboBox()
        for value in ("linear", "nearest", "fsr", "nis", "pixel"):
            self.gamescope_filter.addItem(value.upper(), value)
        self.gamescope_filter.setCurrentIndex(
            max(0, self.gamescope_filter.findData(profile.gamescope_filter))
        )
        self.sharpness = self._spin(profile.gamescope_sharpness, 20, automatic=False)
        self.adaptive_sync = QCheckBox("Allow adaptive sync")
        self.adaptive_sync.setChecked(profile.gamescope_adaptive_sync)
        self.gamescope_extra = QLineEdit(profile.gamescope_extra_arguments)
        self.gamescope_extra.setPlaceholderText("Additional Gamescope arguments")

        game_size = QHBoxLayout()
        game_size.addWidget(self.game_width)
        game_size.addWidget(QLabel("×"))
        game_size.addWidget(self.game_height)
        output_size = QHBoxLayout()
        output_size.addWidget(self.output_width)
        output_size.addWidget(QLabel("×"))
        output_size.addWidget(self.output_height)
        form.addRow("Window mode", self.gamescope_mode)
        form.addRow("Game resolution", game_size)
        form.addRow("Output resolution", output_size)
        form.addRow("Refresh rate", self.refresh_rate)
        form.addRow("FPS limit", self.fps_limit)
        form.addRow("Scaling", self.gamescope_scaler)
        form.addRow("Filter", self.gamescope_filter)
        form.addRow("FSR/NIS sharpness", self.sharpness)
        form.addRow("", self.adaptive_sync)
        form.addRow("Extra arguments", self.gamescope_extra)
        return tab

    def _troubleshooting_tab(self, profile: LaunchProfile) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.disable_esync = QCheckBox("Disable Esync")
        self.disable_esync.setChecked(profile.disable_esync)
        self.disable_fsync = QCheckBox("Disable Fsync")
        self.disable_fsync.setChecked(profile.disable_fsync)
        self.wined3d = QCheckBox("Use WineD3D instead of DXVK")
        self.wined3d.setChecked(profile.use_wined3d)
        self.proton_log = QCheckBox("Write a Proton log")
        self.proton_log.setChecked(profile.enable_proton_log)
        self.large_address = QCheckBox("Force large-address-aware mode")
        self.large_address.setChecked(profile.force_large_address_aware)
        self.wine_debug = QLineEdit(profile.wine_debug)
        self.wine_debug.setPlaceholderText("For example: +relay,+seh")
        for widget in (
            self.disable_esync,
            self.disable_fsync,
            self.wined3d,
            self.proton_log,
            self.large_address,
        ):
            form.addRow("", widget)
        form.addRow("Wine debug channels", self.wine_debug)
        note = QLabel(
            "These switches are compatibility tools, not general performance boosts."
        )
        note.setWordWrap(True)
        form.addRow("", note)
        return tab

    def values(self) -> dict[str, object]:
        return {
            "prefer_discrete_gpu": self.discrete_gpu.isChecked(),
            "enable_hdr": self.hdr.isChecked(),
            "force_nvapi": self.force_nvapi.isChecked(),
            "enable_wayland_raw_input": self.raw_input.isChecked(),
            "prefer_sdl_input": self.sdl_input.isChecked(),
            "dxvk_hud": str(self.dxvk_hud.currentData()),
            "gamescope_window_mode": str(self.gamescope_mode.currentData()),
            "gamescope_game_width": self.game_width.value(),
            "gamescope_game_height": self.game_height.value(),
            "gamescope_output_width": self.output_width.value(),
            "gamescope_output_height": self.output_height.value(),
            "gamescope_refresh_rate": self.refresh_rate.value(),
            "gamescope_fps_limit": self.fps_limit.value(),
            "gamescope_scaler": str(self.gamescope_scaler.currentData()),
            "gamescope_filter": str(self.gamescope_filter.currentData()),
            "gamescope_sharpness": self.sharpness.value(),
            "gamescope_adaptive_sync": self.adaptive_sync.isChecked(),
            "gamescope_extra_arguments": self.gamescope_extra.text(),
            "disable_esync": self.disable_esync.isChecked(),
            "disable_fsync": self.disable_fsync.isChecked(),
            "use_wined3d": self.wined3d.isChecked(),
            "enable_proton_log": self.proton_log.isChecked(),
            "force_large_address_aware": self.large_address.isChecked(),
            "wine_debug": self.wine_debug.text(),
        }
