# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)

from .steam_accounts import SteamAccountState


class SteamAccountDialog(QDialog):
    def __init__(self, state: SteamAccountState, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Switch Steam account")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.account_combo = QComboBox()
        current_index = 0
        for index, account in enumerate(state.accounts):
            suffix = " - current" if account.steam_id == state.current_steam_id else ""
            self.account_combo.addItem(account.label + suffix, account.steam_id)
            if account.steam_id == state.current_steam_id:
                current_index = index
        self.account_combo.setCurrentIndex(current_index)
        form.addRow("Account", self.account_combo)

        self.disable_shader_cache = QCheckBox("Disable Steam shader pre-caching")
        self.disable_shader_cache.setChecked(state.shader_cache_disabled)
        form.addRow("Shaders", self.disable_shader_cache)
        layout.addLayout(form)

        explanation = QLabel(
            "Steam can requeue large shader depots after an account change. "
            "Disabling pre-caching prevents those downloads, but games may compile "
            "more shaders while you play. This setting applies to Steam itself."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_steam_id(self) -> str:
        return str(self.account_combo.currentData())
