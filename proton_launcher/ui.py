# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import shlex
import shutil
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QFontDatabase,
    QIcon,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QSystemTrayIcon,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .models import GameEntry, GameSource, LaunchProfile, ProtonInstallation
from .process_watcher import find_matching_pids, primary_executable_name
from .profiles import DEFAULT_PROFILE_ID, ConfigStore
from .proton import (
    discover_protons,
    discover_steam_default_tool,
    read_prefix_metadata,
    resolve_proton_choice,
)
from .protondb import (
    ProtonDBCache,
    game_url,
    parse_rating,
    protondb_app_id,
    summary_url,
)
from .runner import (
    build_followup_launch_spec,
    build_launch_spec,
    build_steam_launch_spec,
    build_wemod_launch_spec,
    clean_process_output,
    parse_environment_text,
    prepare_compatdata_directory,
)
from .runtime_options_dialog import RuntimeOptionsDialog
from .sessions import SessionKind, SessionManager, SessionRecord
from .settings_dialog import SettingsDialog
from .steam import (
    appmanifest_path,
    discover_games,
    discover_libraries,
    discover_steam_roots,
    set_manifest_state_flags,
)
from .wemod_bridge import reset_wemod_prefix

APP_ICON = Path(__file__).resolve().parent.parent / "assets" / "proton-launcher.svg"
RUNTIME_OPTION_FIELDS = (
    "prefer_discrete_gpu",
    "enable_hdr",
    "force_nvapi",
    "enable_wayland_raw_input",
    "prefer_sdl_input",
    "dxvk_hud",
    "gamescope_window_mode",
    "gamescope_game_width",
    "gamescope_game_height",
    "gamescope_output_width",
    "gamescope_output_height",
    "gamescope_refresh_rate",
    "gamescope_fps_limit",
    "gamescope_scaler",
    "gamescope_filter",
    "gamescope_sharpness",
    "gamescope_adaptive_sync",
    "gamescope_extra_arguments",
    "disable_esync",
    "disable_fsync",
    "use_wined3d",
    "enable_proton_log",
    "force_large_address_aware",
    "wine_debug",
)


class MainWindow(QMainWindow):
    def __init__(self, store: ConfigStore | None = None):
        super().__init__()
        self.setWindowTitle("Proton Launcher")
        self.resize(980, 760)
        icon = QIcon(str(APP_ICON))
        self.setWindowIcon(
            icon if not icon.isNull() else QIcon.fromTheme("applications-games")
        )
        self.store = store or ConfigStore()
        self.sessions = SessionManager()
        self.games: list[GameEntry] = []
        self.protons: list[ProtonInstallation] = []
        self.steam_roots: list[Path] = []
        self.libraries: list[Path] = []
        self.steam_default_tool = ""
        self.default_proton: ProtonInstallation | None = None
        self.current_profile: LaunchProfile | None = None
        self.loading_profile = False
        self.named_profile_dirty = False
        self.session_records: dict[str, SessionRecord] = {}
        self.log_offsets: dict[str, int] = {}
        self.console_lines: dict[str, list[str]] = {}
        self.active_sessions: list[SessionRecord] = []
        self.active_session_signature: (
            tuple[str, tuple[tuple[str, str, str], ...]] | None
        ) = None
        self.protondb_cache = ProtonDBCache()
        self.protondb_app_ids: dict[str, int | None] = {}
        self.protondb_pending: set[int] = set()
        self.current_protondb_app_id: int | None = None
        self.selecting_launch_option = False
        blank_profile = LaunchProfile("", "", "")
        self.runtime_option_values = {
            field: getattr(blank_profile, field) for field in RUNTIME_OPTION_FIELDS
        }
        self._force_quit = False

        self.protondb_network = QNetworkAccessManager(self)

        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)
        self.autosave_timer.setInterval(500)
        self.autosave_timer.timeout.connect(self._autosave_default)
        self.session_timer = QTimer(self)
        self.session_timer.setInterval(750)
        self.session_timer.timeout.connect(self._refresh_sessions)

        self._build_ui()
        self._build_tray()
        self._connect_profile_signals()
        if self.store.read_only:
            self._log(
                "Configuration recovery mode: changes are temporary and read-only"
            )
        for issue in self.store.validation_issues:
            self._log(f"Configuration repair: {issue.path}: {issue.message}")
        self.refresh()
        self.session_timer.start()
        if not self.sessions.systemd_available:
            self._log(
                "Warning: user systemd is unavailable; using degraded process-group supervision"
            )

    def _build_ui(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        settings_action = file_menu.addAction("Settings…")
        settings_action.triggered.connect(self.open_settings)
        quit_action = file_menu.addAction("Quit")
        quit_action.triggered.connect(self._quit_from_menu)
        tools_menu = self.menuBar().addMenu("Tools")
        copy_options = tools_menu.addAction("Copy Steam Launch Options")
        copy_options.triggered.connect(self.copy_steam_launch_options)
        stop_everything = tools_menu.addAction("Stop all running sessions")
        stop_everything.triggered.connect(self.stop_all_sessions)
        skip_everything = tools_menu.addAction("Skip all updates")
        skip_everything.triggered.connect(self.skip_all_updates)

        toolbar = QToolBar("Actions", self)
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        self.addToolBar(toolbar)
        for text, callback in (
            ("Refresh", self.refresh),
            ("Settings…", self.open_settings),
            ("Set prefix…", self.set_prefix),
            ("Open prefix", self.open_prefix),
            ("Delete prefix…", self.delete_prefix),
        ):
            action = QAction(text, self)
            action.triggered.connect(callback)
            toolbar.addAction(action)
            if text == "Delete prefix…":
                self.delete_prefix_action = action
            elif text == "Open prefix":
                self.open_prefix_action = action

        top = QWidget()
        form = QFormLayout(top)
        self.game_combo = QComboBox()
        self.game_combo.setEditable(True)
        self.game_combo.setInsertPolicy(QComboBox.NoInsert)
        self.game_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.game_combo.currentIndexChanged.connect(self.game_changed)
        game_row = QHBoxLayout()
        game_row.addWidget(self.game_combo, 1)
        self.game_count_label = QLabel()
        self.game_count_label.setStyleSheet("color: palette(mid);")
        game_row.addWidget(self.game_count_label)
        form.addRow("Game", game_row)

        game_details_row = QHBoxLayout()
        self.protondb_button = QPushButton("ProtonDB")
        self.protondb_button.setEnabled(False)
        self.protondb_button.clicked.connect(self.open_protondb)
        game_details_row.addWidget(self.protondb_button)
        self.skip_update_button = QPushButton("Skip update")
        self.skip_update_button.setEnabled(False)
        self.skip_update_button.clicked.connect(self.skip_update)
        game_details_row.addWidget(self.skip_update_button)
        self.prefix_badge = QLabel("Prefix: not selected")
        self.prefix_badge.setStyleSheet(
            "padding: 3px 7px; border: 1px solid palette(mid); border-radius: 5px;"
        )
        self.prefix_badge.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        game_details_row.addWidget(self.prefix_badge, 1)
        form.addRow("", game_details_row)

        self.proton_combo = QComboBox()
        self.proton_combo.currentIndexChanged.connect(self._proton_mode_changed)
        proton_row = QHBoxLayout()
        proton_row.addWidget(self.proton_combo, 1)
        self.proton_count_label = QLabel()
        self.proton_count_label.setStyleSheet("color: palette(mid);")
        proton_row.addWidget(self.proton_count_label)
        form.addRow("Proton", proton_row)

        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self.profile_changed)
        profile_row = QHBoxLayout()
        profile_row.addWidget(self.profile_combo, 1)
        for label, callback in (
            ("New", self.new_profile),
            ("Save", self.save_profile),
            ("Duplicate", self.duplicate_profile),
            ("Delete", self.delete_profile),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            profile_row.addWidget(button)
            if label == "Save":
                self.save_button = button
            elif label == "Delete":
                self.delete_button = button
        form.addRow("Profile", profile_row)

        self.steam_launch_checkbox = QCheckBox(
            "Launch through Steam (enables Steam overlay/context)"
        )
        self.steam_launch_checkbox.toggled.connect(self._steam_launch_mode_changed)
        form.addRow("", self.steam_launch_checkbox)
        overlay_row = QHBoxLayout()
        self.overlay_checkbox = QCheckBox("Inject Steam overlay into direct launch")
        self.overlay_checkbox.toggled.connect(self._overlay_mode_changed)
        overlay_row.addWidget(self.overlay_checkbox)
        overlay_row.addWidget(QLabel("App ID"))
        self.overlay_app_id_edit = QLineEdit()
        self.overlay_app_id_edit.setMaximumWidth(100)
        overlay_row.addWidget(self.overlay_app_id_edit)
        overlay_row.addStretch()
        form.addRow("", overlay_row)

        runtime_row = QHBoxLayout()
        self.gamemode_checkbox = QCheckBox("GameMode")
        self.mangohud_checkbox = QCheckBox("MangoHud")
        self.gamescope_checkbox = QCheckBox("Gamescope")
        self.wayland_checkbox = QCheckBox("Native Wayland")
        for widget, program in (
            (self.gamemode_checkbox, "gamemoderun"),
            (self.mangohud_checkbox, "mangohud"),
            (self.gamescope_checkbox, "gamescope"),
        ):
            available = shutil.which(program)
            widget.setEnabled(bool(available))
            if not available:
                widget.setToolTip(f"{program} is not installed")
            runtime_row.addWidget(widget)
        self.wayland_checkbox.setToolTip(
            "Experimental Proton/GE-Proton Wine-Wayland driver; Steam overlay may not work"
        )
        runtime_row.addWidget(self.wayland_checkbox)
        self.runtime_options_button = QPushButton("Configure…")
        self.runtime_options_button.clicked.connect(self.configure_runtime_options)
        runtime_row.addWidget(self.runtime_options_button)
        runtime_row.addStretch()
        form.addRow("Launch options", runtime_row)

        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._mode_changed)
        executable_tab = QWidget()
        executable_form = QFormLayout(executable_tab)
        self.launch_option_combo = QComboBox()
        self.launch_option_combo.currentIndexChanged.connect(
            self._launch_option_changed
        )
        self.launch_option_label = QLabel("Steam launch option")
        executable_form.addRow(self.launch_option_label, self.launch_option_combo)
        self.exe_edit = QLineEdit()
        self.exe_edit.textChanged.connect(self._update_wait_target)
        self.exe_edit.textChanged.connect(self._sync_launch_option)
        exe_row = QHBoxLayout()
        exe_row.addWidget(self.exe_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse_executable)
        exe_row.addWidget(browse)
        executable_form.addRow("Executable", exe_row)
        self.admin_checkbox = QCheckBox("Run as administrator")
        executable_form.addRow("", self.admin_checkbox)
        self.tabs.addTab(executable_tab, "Executable")

        command_tab = QWidget()
        command_layout = QVBoxLayout(command_tab)
        preset_row = QHBoxLayout()
        self.command_edit = QLineEdit()
        for name, command in (
            ("Explorer", "wine explorer ."),
            ("Winecfg", "winecfg"),
            ("Regedit", "regedit"),
            ("CMD", "wineconsole cmd"),
        ):
            button = QPushButton(name)
            button.clicked.connect(
                lambda _checked=False, value=command: self.command_edit.setText(value)
            )
            preset_row.addWidget(button)
        self.command_edit.setPlaceholderText("wine explorer")
        self.command_edit.textChanged.connect(self._update_wait_target)
        command_layout.addLayout(preset_row)
        command_layout.addWidget(self.command_edit)
        self.tabs.addTab(command_tab, "Command")

        details = QWidget()
        details_form = QFormLayout(details)
        self.arguments_edit = QLineEdit()
        self.arguments_edit.textChanged.connect(self._sync_launch_option)
        self.arguments_edit.setPlaceholderText(
            'Arguments, e.g. --flag "value with spaces"'
        )
        self.working_edit = QLineEdit()
        self.working_edit.textChanged.connect(self._sync_launch_option)
        work_row = QHBoxLayout()
        work_row.addWidget(self.working_edit, 1)
        work_browse = QPushButton("Browse…")
        work_browse.clicked.connect(self.browse_working)
        work_row.addWidget(work_browse)
        self.online_fix_checkbox = QCheckBox("Apply online-fix overrides")
        self.online_fix_checkbox.toggled.connect(self._online_fix_toggled)
        self.wemod_checkbox = QCheckBox("Launch with WeMod")
        self.wemod_checkbox.toggled.connect(self._wemod_mode_changed)
        self.wemod_status = QLabel()
        self.wemod_status.setStyleSheet("color: palette(mid);")
        wemod_row = QHBoxLayout()
        wemod_row.addWidget(self.wemod_checkbox)
        wemod_row.addWidget(self.wemod_status, 1)
        self.configure_wemod_button = QPushButton("Configure…")
        self.configure_wemod_button.clicked.connect(
            lambda _checked=False: self.open_settings("integrations")
        )
        wemod_row.addWidget(self.configure_wemod_button)
        self.launch_wemod_button = QPushButton("Launch WeMod")
        self.launch_wemod_button.clicked.connect(self.launch_wemod)
        wemod_row.addWidget(self.launch_wemod_button)
        self.delete_wemod_button = QPushButton("Delete WeMod…")
        self.delete_wemod_button.clicked.connect(self.delete_wemod)
        wemod_row.addWidget(self.delete_wemod_button)
        self.environment_edit = QPlainTextEdit()
        self.environment_edit.setPlaceholderText("One NAME=value per line")
        self.environment_edit.setMaximumHeight(100)
        details_form.addRow("Arguments", self.arguments_edit)
        details_form.addRow("Working directory", work_row)
        details_form.addRow("", self.online_fix_checkbox)
        details_form.addRow("", wemod_row)
        details_form.addRow("Environment", self.environment_edit)

        self.followup_group = QGroupBox("Follow-up launch")
        self.followup_group.setCheckable(True)
        self.followup_group.setChecked(False)
        self.followup_group.toggled.connect(self._update_followup_launch_button)
        followup_form = QFormLayout(self.followup_group)
        self.wait_exe_edit = QLineEdit()
        self.wait_exe_edit.setPlaceholderText("Process name, e.g. Game.exe")
        wait_row = QHBoxLayout()
        wait_row.addWidget(self.wait_exe_edit, 1)
        self.wait_primary_checkbox = QCheckBox("Use first executable")
        self.wait_primary_checkbox.toggled.connect(self._update_wait_target)
        wait_row.addWidget(self.wait_primary_checkbox)
        self.followup_delay_spin = QDoubleSpinBox()
        self.followup_delay_spin.setRange(0, 86400)
        self.followup_delay_spin.setDecimals(1)
        self.followup_delay_spin.setSuffix(" seconds")
        self.followup_mode_combo = QComboBox()
        self.followup_mode_combo.addItem("Executable", "executable")
        self.followup_mode_combo.addItem("Command", "command")
        self.followup_mode_combo.currentIndexChanged.connect(
            self._followup_mode_changed
        )
        self.followup_target_edit = QLineEdit()
        target_row = QHBoxLayout()
        target_row.addWidget(self.followup_target_edit, 1)
        self.followup_browse_button = QPushButton("Browse…")
        self.followup_browse_button.clicked.connect(self.browse_followup)
        target_row.addWidget(self.followup_browse_button)
        self.followup_arguments_edit = QLineEdit()
        self.followup_admin_checkbox = QCheckBox("Run follow-up as administrator")
        self.followup_launch_now_button = QPushButton("Launch follow-up now")
        self.followup_launch_now_button.clicked.connect(self.launch_followup_now)
        followup_form.addRow("Wait for", wait_row)
        followup_form.addRow("Then wait", self.followup_delay_spin)
        followup_form.addRow("Launch type", self.followup_mode_combo)
        followup_form.addRow("Executable / command", target_row)
        followup_form.addRow("Arguments", self.followup_arguments_edit)
        actions = QHBoxLayout()
        actions.addWidget(self.followup_admin_checkbox)
        actions.addStretch()
        actions.addWidget(self.followup_launch_now_button)
        followup_form.addRow("", actions)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.addWidget(top)
        editor_layout.addWidget(self.tabs)
        editor_layout.addWidget(details)
        editor_layout.addWidget(self.followup_group)
        editor_layout.addStretch()
        editor_scroll = QScrollArea()
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setWidget(editor)

        log_widget = QWidget()
        log_layout = QVBoxLayout(log_widget)
        log_controls = QHBoxLayout()
        self.status = QLabel("Ready")
        log_controls.addWidget(self.status, 1)
        self.launch_button = QPushButton("Launch")
        self.launch_button.setDefault(True)
        self.launch_button.clicked.connect(self.launch)
        log_controls.addWidget(self.launch_button)
        self.stop_game_button = QPushButton("Stop Game")
        self.stop_game_button.setToolTip(
            "Stop the primary session for the selected game"
        )
        self.stop_game_button.clicked.connect(self.stop_game)
        log_controls.addWidget(self.stop_game_button)
        self.stop_followup_button = QPushButton("Stop Follow-up")
        self.stop_followup_button.setToolTip(
            "Stop follow-up sessions for the selected game"
        )
        self.stop_followup_button.clicked.connect(self.stop_followup)
        log_controls.addWidget(self.stop_followup_button)
        self.stop_all_button = QPushButton("Stop All for Game")
        self.stop_all_button.setToolTip("Stop every session for the selected game")
        self.stop_all_button.clicked.connect(self.stop_all)
        log_controls.addWidget(self.stop_all_button)
        self.clear_log_button = QPushButton("Clear")
        self.clear_log_button.setToolTip("Clear the selected game's console")
        self.clear_log_button.clicked.connect(self.clear_console)
        log_controls.addWidget(self.clear_log_button)
        self.copy_log_button = QPushButton("Copy")
        self.copy_log_button.setToolTip("Copy the selected game's console")
        self.copy_log_button.clicked.connect(self.copy_console)
        log_controls.addWidget(self.copy_log_button)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        log_layout.addLayout(log_controls)
        log_layout.addWidget(self.log)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(editor_scroll)
        splitter.addWidget(log_widget)
        splitter.setSizes([535, 225])
        self.setCentralWidget(splitter)
        self._followup_mode_changed()
        self._update_session_buttons([])

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = self.tray.contextMenu() or None
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        show_action = menu.addAction("Show Proton Launcher")
        show_action.triggered.connect(self._show_from_tray)
        menu.addSeparator()
        self.tray_stop_game = menu.addAction("Stop Game")
        self.tray_stop_game.triggered.connect(self.stop_game)
        self.tray_stop_followup = menu.addAction("Stop Follow-up")
        self.tray_stop_followup.triggered.connect(self.stop_followup)
        self.tray_stop_all = menu.addAction("Stop All for Selected Game")
        self.tray_stop_all.triggered.connect(self.stop_all)
        menu.addSeparator()
        keep = menu.addAction("Quit and keep sessions")
        keep.triggered.connect(self._quit_keep_sessions)
        stop = menu.addAction("Stop all and quit")
        stop.triggered.connect(self._stop_and_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: (
                self._show_from_tray() if reason == QSystemTrayIcon.Trigger else None
            )
        )
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _connect_profile_signals(self) -> None:
        for widget in (
            self.exe_edit,
            self.command_edit,
            self.arguments_edit,
            self.working_edit,
            self.overlay_app_id_edit,
            self.wait_exe_edit,
            self.followup_target_edit,
            self.followup_arguments_edit,
        ):
            widget.textChanged.connect(self._field_changed)
        self.environment_edit.textChanged.connect(self._field_changed)
        for widget in (
            self.admin_checkbox,
            self.steam_launch_checkbox,
            self.overlay_checkbox,
            self.online_fix_checkbox,
            self.wemod_checkbox,
            self.gamemode_checkbox,
            self.mangohud_checkbox,
            self.gamescope_checkbox,
            self.wayland_checkbox,
            self.followup_group,
            self.wait_primary_checkbox,
            self.followup_admin_checkbox,
        ):
            widget.toggled.connect(self._field_changed)
        self.proton_combo.currentIndexChanged.connect(self._field_changed)
        self.tabs.currentChanged.connect(self._field_changed)
        self.followup_mode_combo.currentIndexChanged.connect(self._field_changed)
        self.followup_delay_spin.valueChanged.connect(self._field_changed)

    def refresh(self) -> None:
        self.protondb_app_ids.clear()
        selected = (
            self.game_combo.currentData()
            if self.game_combo.count()
            else self.store.data["last_game"]
        )
        settings = self.store.settings
        self.games, issues = discover_games(
            settings["custom_steam_roots"], settings["custom_libraries"]
        )
        self.steam_roots = discover_steam_roots(settings["custom_steam_roots"])
        self.libraries = []
        for root in self.steam_roots:
            self.libraries.extend(
                discover_libraries(root, settings["custom_libraries"])
            )
        self.protons, proton_issues = discover_protons(
            settings["custom_proton_locations"], self.steam_roots, self.libraries
        )
        self.steam_default_tool = discover_steam_default_tool(self.steam_roots)
        self.default_proton, warning = resolve_proton_choice(
            self.protons, settings["default_proton"], self.steam_default_tool
        )
        self.game_combo.blockSignals(True)
        self.game_combo.clear()
        for game in self.games:
            self.game_combo.addItem(game.label, game.key)
        self.game_combo.blockSignals(False)
        index = self.game_combo.findData(selected)
        self.game_combo.setCurrentIndex(
            index if index >= 0 else (0 if self.games else -1)
        )
        self.game_count_label.setText(f"{len(self.games)} games")
        self.proton_count_label.setText(f"{len(self.protons)} Proton versions")
        for issue in [*issues, *proton_issues]:
            self._log(f"Discovery warning: {issue.path}: {issue.message}")
        if warning:
            self._log(f"Proton default warning: {warning}")
        self.game_changed()
        self._update_wemod_status()

    def current_game(self) -> GameEntry | None:
        key = self.game_combo.currentData()
        return next((game for game in self.games if game.key == key), None)

    def _update_protondb_rating(self, game: GameEntry | None) -> None:
        app_id = None
        if game:
            if game.key not in self.protondb_app_ids:
                self.protondb_app_ids[game.key] = protondb_app_id(game)
            app_id = self.protondb_app_ids[game.key]
        self.current_protondb_app_id = app_id
        if not app_id:
            self.protondb_button.setText("ProtonDB")
            tooltip = (
                "No Steam App ID was found for this non-Steam game."
                if game
                else "Select a game to view its ProtonDB rating."
            )
            self.protondb_button.setToolTip(tooltip)
            self.protondb_button.setEnabled(False)
            return

        self.protondb_button.setEnabled(True)
        cached, rating, fresh = self.protondb_cache.lookup(app_id)
        if cached:
            self.protondb_button.setText(f"ProtonDB: {rating or 'Unrated'}")
            self.protondb_button.setToolTip(
                "Click to open this game's ProtonDB page."
                if fresh
                else "Showing a cached rating while ProtonDB is refreshed."
            )
            if fresh:
                return

        if not cached:
            self.protondb_button.setText("ProtonDB: Loading…")
            self.protondb_button.setToolTip(
                "Loading the ProtonDB rating. Click to open the game page."
            )
        if app_id in self.protondb_pending:
            return
        self.protondb_pending.add(app_id)
        request = QNetworkRequest(QUrl(summary_url(app_id)))
        request.setTransferTimeout(10_000)
        request.setHeader(QNetworkRequest.UserAgentHeader, "Proton Launcher/1.0")
        reply = self.protondb_network.get(request)
        reply.finished.connect(
            lambda app_id=app_id, current_reply=reply: self._protondb_finished(
                app_id, current_reply
            )
        )

    def _protondb_finished(self, app_id: int, reply: QNetworkReply) -> None:
        self.protondb_pending.discard(app_id)
        error = reply.error()
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if error == QNetworkReply.NoError:
            self.protondb_cache.put(app_id, parse_rating(bytes(reply.readAll())))
        elif status == 404:
            self.protondb_cache.put(app_id, None)
        reply.deleteLater()

        game = self.current_game()
        if not game or self.current_protondb_app_id != app_id:
            return
        if error != QNetworkReply.NoError and status != 404:
            cached, rating, _fresh = self.protondb_cache.lookup(app_id)
            if cached:
                self.protondb_button.setText(f"ProtonDB: {rating or 'Unrated'}")
                self.protondb_button.setToolTip(
                    "Showing a cached rating because ProtonDB could not be reached."
                )
            else:
                self.protondb_button.setText("ProtonDB: Offline")
                self.protondb_button.setToolTip(
                    "The rating could not be loaded. Click to open the game page."
                )
            return
        self._update_protondb_rating(game)

    def open_protondb(self) -> None:
        if not self.current_game() or not self.current_protondb_app_id:
            return
        if not QDesktopServices.openUrl(QUrl(game_url(self.current_protondb_app_id))):
            self.status.setText("Could not open the ProtonDB page")

    def _update_skip_update_button(self, game: GameEntry | None) -> None:
        available = bool(
            game
            and game.source == GameSource.STEAM
            and appmanifest_path(game).is_file()
        )
        self.skip_update_button.setEnabled(available)
        self.skip_update_button.setToolTip(
            "Set this game's Steam appmanifest StateFlags value to 4"
            if available
            else "Only available for installed Steam games"
        )

    def skip_update(self) -> None:
        game = self.current_game()
        if not game or game.source != GameSource.STEAM:
            return
        manifest = appmanifest_path(game)
        answer = QMessageBox.warning(
            self,
            "Skip Steam update?",
            f"Set StateFlags to 4 for “{game.name}”?\n\n"
            f"This edits:\n{manifest}\n\n"
            "Completely exit Steam first. If Steam is running, it may overwrite "
            "the change.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            set_manifest_state_flags(game)
        except ValueError as error:
            QMessageBox.critical(self, "Could not skip update", str(error))
            self._log(f"Skip update failed: {error}")
            return
        self.status.setText(f"Set StateFlags to 4 for {game.name}")
        self._log(f"Set StateFlags to 4 in {manifest}")

    def skip_all_updates(self) -> None:
        games = [
            game
            for game in self.games
            if game.source == GameSource.STEAM and appmanifest_path(game).is_file()
        ]
        if not games:
            QMessageBox.information(
                self, "No Steam games", "No installed Steam appmanifests were found."
            )
            return
        answer = QMessageBox.warning(
            self,
            "Skip updates for all Steam games?",
            f"Set StateFlags to 4 in {len(games)} installed game manifests?\n\n"
            "Completely exit Steam first. Steam can overwrite these changes while "
            "it is running.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        updated: list[Path] = []
        failures: list[str] = []
        progress = QProgressDialog(
            "Updating Steam appmanifests…", "Cancel", 0, len(games), self
        )
        progress.setWindowTitle("Skip all updates")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        cancelled = False
        try:
            for index, game in enumerate(games):
                progress.setLabelText(f"Updating {game.name}")
                progress.setValue(index)
                QApplication.processEvents()
                if progress.wasCanceled():
                    cancelled = True
                    break
                try:
                    updated.append(set_manifest_state_flags(game))
                except ValueError as error:
                    failures.append(f"{game.name}: {error}")
        finally:
            progress.close()
        self._log(f"Set StateFlags to 4 in {len(updated)} Steam appmanifests")
        if failures:
            QMessageBox.warning(
                self,
                "Some manifests could not be changed",
                "\n".join(failures),
            )
            self._log("Skip all updates failures:\n" + "\n".join(failures))
        self.status.setText(
            f"Changed {len(updated)} Steam appmanifests"
            + (f"; {len(failures)} failed" if failures else "")
            + ("; canceled" if cancelled else "")
        )

    def game_changed(self, *_args) -> None:
        if self.autosave_timer.isActive() and self.current_profile:
            self._autosave_default()
        game = self.current_game()
        if not game:
            self.game_combo.setToolTip("")
            self._render_console()
            self._update_protondb_rating(None)
            self._update_skip_update_button(None)
            self._update_wemod_status()
            self.active_session_signature = None
            self._update_session_view(self.active_sessions)
            return
        if (
            self.named_profile_dirty
            and self.current_profile
            and self.current_profile.game_key != game.key
        ):
            old_game = next(
                (
                    item
                    for item in self.games
                    if item.key == self.current_profile.game_key
                ),
                None,
            )
            answer = QMessageBox.question(
                self,
                "Unsaved profile",
                f"Save changes to “{self.current_profile.name}”?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if answer == QMessageBox.Cancel:
                self.game_combo.blockSignals(True)
                self.game_combo.setCurrentIndex(
                    self.game_combo.findData(self.current_profile.game_key)
                )
                self.game_combo.blockSignals(False)
                return
            if answer == QMessageBox.Save and old_game:
                self._save_current_profile(old_game)
        self.store.data["last_game"] = game.key
        self.game_combo.setToolTip(game.label)
        self._render_console()
        self._update_protondb_rating(game)
        self._update_skip_update_button(game)
        template = LaunchProfile(
            DEFAULT_PROFILE_ID,
            "Default",
            game.key,
            executable=game.shortcut_exe or game.default_executable,
            working_directory=game.shortcut_start_dir,
            overlay_app_id=(
                "480" if game.source.value == "shortcut" else str(game.app_id)
            ),
        )
        if game.source.value == "shortcut":
            existing = self.store.data["games"].get(game.key, {})
            override = str(existing.get("prefix_override", ""))
            prefix = (
                Path(override).expanduser() if override else game.default_prefix
            ).resolve(strict=False)
            metadata = read_prefix_metadata(prefix, self.protons)
            if metadata.state == "known" and metadata.proton_root:
                initialized_proton = next(
                    (
                        proton
                        for proton in self.protons
                        if proton.root.resolve(strict=False)
                        == metadata.proton_root.resolve(strict=False)
                    ),
                    None,
                )
                if initialized_proton:
                    template.proton_path = str(initialized_proton.launcher)
                    template.use_default_proton = False
        self.store.ensure_game(game.key, template)
        game_data = self.store.game_data(game.key)
        profiles = self.store.profiles(game.key)
        profiles.sort(
            key=lambda item: (item.id != DEFAULT_PROFILE_ID, item.name.casefold())
        )
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in profiles:
            self.profile_combo.addItem(profile.name, profile.id)
        wanted = game_data.get("last_profile_id", DEFAULT_PROFILE_ID)
        self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(wanted)))
        self.profile_combo.blockSignals(False)
        selected = next(
            (item for item in profiles if item.id == self.profile_combo.currentData()),
            profiles[0],
        )
        self.load_profile(selected)
        self._update_prefix_badge()
        self._update_wemod_status()
        self.active_session_signature = None
        self._update_session_view(self.active_sessions)

    def profile_changed(self, *_args) -> None:
        game = self.current_game()
        if not game or self.loading_profile:
            return
        if (
            self.autosave_timer.isActive()
            and self.current_profile
            and self.current_profile.id == DEFAULT_PROFILE_ID
        ):
            self._autosave_default()
        if (
            self.named_profile_dirty
            and self.current_profile
            and self.current_profile.id != DEFAULT_PROFILE_ID
        ):
            answer = QMessageBox.question(
                self,
                "Unsaved profile",
                f"Save changes to “{self.current_profile.name}”?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if answer == QMessageBox.Cancel:
                self.profile_combo.blockSignals(True)
                self.profile_combo.setCurrentIndex(
                    self.profile_combo.findData(self.current_profile.id)
                )
                self.profile_combo.blockSignals(False)
                return
            if answer == QMessageBox.Save:
                self._save_current_profile()
        profile_id = self.profile_combo.currentData()
        profile = next(
            (item for item in self.store.profiles(game.key) if item.id == profile_id),
            None,
        )
        if profile:
            self.store.game_data(game.key)["last_profile_id"] = profile.id
            self.store.save()
            self.load_profile(profile)

    def load_profile(self, profile: LaunchProfile) -> None:
        self.loading_profile = True
        self.current_profile = profile
        self.tabs.setCurrentIndex(0 if profile.mode == "executable" else 1)
        self.exe_edit.setText(profile.executable)
        self.command_edit.setText(profile.command)
        self.arguments_edit.setText(profile.arguments)
        self.working_edit.setText(profile.working_directory)
        self.environment_edit.setPlainText(profile.environment_text)
        self.admin_checkbox.setChecked(profile.run_as_admin)
        self.steam_launch_checkbox.setChecked(profile.launch_through_steam)
        self.overlay_checkbox.setChecked(profile.inject_steam_overlay)
        self.overlay_app_id_edit.setText(profile.overlay_app_id)
        self.online_fix_checkbox.setChecked(profile.apply_online_fix)
        self.wemod_checkbox.setChecked(profile.launch_wemod)
        self.gamemode_checkbox.setChecked(profile.enable_gamemode)
        self.mangohud_checkbox.setChecked(profile.enable_mangohud)
        self.gamescope_checkbox.setChecked(profile.enable_gamescope)
        self.wayland_checkbox.setChecked(profile.enable_wayland or profile.enable_hdr)
        self.runtime_option_values = {
            field: getattr(profile, field) for field in RUNTIME_OPTION_FIELDS
        }
        self.followup_group.setChecked(profile.followup_enabled)
        self.wait_exe_edit.setText(profile.wait_for_executable)
        self.wait_primary_checkbox.setChecked(profile.wait_for_primary_executable)
        self.followup_delay_spin.setValue(profile.followup_delay)
        self.followup_mode_combo.setCurrentIndex(
            max(0, self.followup_mode_combo.findData(profile.followup_mode))
        )
        self.followup_target_edit.setText(
            profile.followup_executable
            if profile.followup_mode == "executable"
            else profile.followup_command
        )
        self.followup_arguments_edit.setText(profile.followup_arguments)
        self.followup_admin_checkbox.setChecked(profile.followup_run_as_admin)
        self._populate_launch_options(profile)
        self._populate_proton_combo(profile)
        self.loading_profile = False
        self.named_profile_dirty = False
        self._update_profile_buttons()
        self._steam_launch_mode_changed()
        self._followup_mode_changed()
        self._proton_mode_changed()

    def _populate_launch_options(self, profile: LaunchProfile) -> None:
        game = self.current_game()
        options = game.launch_options if game else ()
        self.launch_option_combo.blockSignals(True)
        self.launch_option_combo.clear()
        for index, option in enumerate(options):
            self.launch_option_combo.addItem(option.label, index)
            tooltip = "\n".join(
                value for value in (option.executable, option.arguments) if value
            )
            self.launch_option_combo.setItemData(index, tooltip, Qt.ToolTipRole)
        if options:
            self.launch_option_combo.addItem("Custom executable", None)
        visible = len(options) > 1
        self.launch_option_label.setVisible(visible)
        self.launch_option_combo.setVisible(visible)

        selected = self._matching_launch_option(
            profile.executable,
            profile.arguments,
            profile.working_directory,
        )
        if selected is None and not profile.executable.strip() and options:
            selected = 0
            option = options[0]
            self.exe_edit.setText(option.executable)
            self.arguments_edit.setText(option.arguments)
            self.working_edit.setText(option.working_directory)
        self.launch_option_combo.setCurrentIndex(
            selected if selected is not None else self.launch_option_combo.count() - 1
        )
        self.launch_option_combo.blockSignals(False)

    def _matching_launch_option(
        self, executable: str, arguments: str, working_directory: str
    ) -> int | None:
        game = self.current_game()
        if not game or not executable.strip():
            return None
        executable_path = Path(executable).expanduser().resolve(strict=False)
        for index, option in enumerate(game.launch_options):
            if Path(option.executable).resolve(strict=False) != executable_path:
                continue
            if option.arguments.strip() != arguments.strip():
                continue
            if option.working_directory and (
                Path(option.working_directory).resolve(strict=False)
                != Path(working_directory).expanduser().resolve(strict=False)
            ):
                continue
            return index
        return None

    def _sync_launch_option(self, *_args) -> None:
        if self.loading_profile or self.selecting_launch_option:
            return
        game = self.current_game()
        if not game or not game.launch_options:
            return
        selected = self._matching_launch_option(
            self.exe_edit.text(),
            self.arguments_edit.text(),
            self.working_edit.text(),
        )
        self.launch_option_combo.blockSignals(True)
        self.launch_option_combo.setCurrentIndex(
            selected if selected is not None else self.launch_option_combo.count() - 1
        )
        self.launch_option_combo.blockSignals(False)

    def _launch_option_changed(self, index: int) -> None:
        game = self.current_game()
        option_index = self.launch_option_combo.itemData(index)
        if self.loading_profile or not game or option_index is None:
            return
        if not 0 <= option_index < len(game.launch_options):
            return
        option = game.launch_options[option_index]
        self.selecting_launch_option = True
        try:
            self.exe_edit.setText(option.executable)
            self.arguments_edit.setText(option.arguments)
            self.working_edit.setText(option.working_directory)
        finally:
            self.selecting_launch_option = False

    def _populate_proton_combo(self, profile: LaunchProfile) -> None:
        self.proton_combo.blockSignals(True)
        self.proton_combo.clear()
        default_name = (
            self.default_proton.display_name if self.default_proton else "Unavailable"
        )
        self.proton_combo.addItem(f"Launcher default: {default_name}", "__default__")
        self.proton_combo.addItem("No Proton (native Linux)", "__native__")
        for proton in self.protons:
            self.proton_combo.addItem(proton.display_name, str(proton.launcher))
        wanted = (
            "__native__"
            if profile.use_native_runtime
            else "__default__" if profile.use_default_proton else profile.proton_path
        )
        self.proton_combo.setCurrentIndex(max(0, self.proton_combo.findData(wanted)))
        self.proton_combo.blockSignals(False)
        self._update_proton_tooltip()

    def _profile_from_ui(
        self,
        name: str | None = None,
        new_id: bool = False,
        game_override: GameEntry | None = None,
    ) -> LaunchProfile:
        game = game_override or self.current_game()
        if not game:
            raise ValueError("Choose a game")
        current = self.current_profile
        followup_mode = str(self.followup_mode_combo.currentData())
        proton_data = str(self.proton_combo.currentData() or "__default__")
        return LaunchProfile(
            id=str(uuid.uuid4()) if new_id or not current else current.id,
            name=name or (current.name if current else "Profile"),
            game_key=game.key,
            proton_path=(
                "" if proton_data in {"__default__", "__native__"} else proton_data
            ),
            use_default_proton=proton_data == "__default__",
            use_native_runtime=proton_data == "__native__",
            mode="executable" if self.tabs.currentIndex() == 0 else "command",
            executable=self.exe_edit.text(),
            command=self.command_edit.text(),
            arguments=self.arguments_edit.text(),
            working_directory=self.working_edit.text(),
            environment_text=self.environment_edit.toPlainText(),
            run_as_admin=self.admin_checkbox.isChecked(),
            launch_through_steam=self.steam_launch_checkbox.isChecked(),
            inject_steam_overlay=self.overlay_checkbox.isChecked(),
            overlay_app_id=self.overlay_app_id_edit.text().strip(),
            apply_online_fix=self.online_fix_checkbox.isChecked(),
            launch_wemod=self.wemod_checkbox.isChecked(),
            enable_gamemode=self.gamemode_checkbox.isChecked(),
            enable_mangohud=self.mangohud_checkbox.isChecked(),
            enable_gamescope=self.gamescope_checkbox.isChecked(),
            enable_wayland=self.wayland_checkbox.isChecked(),
            **self.runtime_option_values,
            followup_enabled=self.followup_group.isChecked(),
            wait_for_executable=self.wait_exe_edit.text().strip(),
            wait_for_primary_executable=self.wait_primary_checkbox.isChecked(),
            followup_delay=self.followup_delay_spin.value(),
            followup_mode=followup_mode,
            followup_executable=(
                self.followup_target_edit.text()
                if followup_mode == "executable"
                else ""
            ),
            followup_command=(
                self.followup_target_edit.text() if followup_mode == "command" else ""
            ),
            followup_arguments=self.followup_arguments_edit.text(),
            followup_run_as_admin=self.followup_admin_checkbox.isChecked(),
        )

    def _resolved_profile(self, profile: LaunchProfile) -> LaunchProfile:
        if profile.use_native_runtime:
            return replace(profile, proton_path="")
        if not profile.use_default_proton:
            return profile
        if not self.default_proton:
            raise ValueError("No default Proton installation is available")
        return replace(profile, proton_path=str(self.default_proton.launcher))

    def _field_changed(self, *_args) -> None:
        if self.loading_profile or not self.current_profile:
            return
        if self.current_profile.id == DEFAULT_PROFILE_ID:
            self.autosave_timer.start()
            self.status.setText("Default profile changed…")
        else:
            self.named_profile_dirty = True
            index = self.profile_combo.currentIndex()
            if index >= 0 and not self.profile_combo.itemText(index).endswith(" *"):
                self.profile_combo.setItemText(index, f"{self.current_profile.name} *")
            self._update_profile_buttons()

    def _autosave_default(self) -> None:
        if not self.current_profile or self.current_profile.id != DEFAULT_PROFILE_ID:
            return
        game = next(
            (item for item in self.games if item.key == self.current_profile.game_key),
            None,
        )
        if not game:
            return
        self.autosave_timer.stop()
        profile = self._profile_from_ui(name="Default", game_override=game)
        profile.id = DEFAULT_PROFILE_ID
        self.store.put_profile(profile)
        self.current_profile = profile
        self.status.setText("Default profile saved")

    def new_profile(self) -> None:
        game = self.current_game()
        if not game:
            return
        name, ok = QInputDialog.getText(self, "New profile", "Name")
        if not ok or not name.strip():
            return
        profile = LaunchProfile(
            str(uuid.uuid4()),
            name.strip(),
            game.key,
            executable=game.shortcut_exe or game.default_executable,
            working_directory=game.shortcut_start_dir,
            overlay_app_id=(
                "480" if game.source.value == "shortcut" else str(game.app_id)
            ),
        )
        self.store.put_profile(profile)
        self.game_changed()
        self.profile_combo.setCurrentIndex(self.profile_combo.findData(profile.id))

    def _save_current_profile(self, game_override: GameEntry | None = None) -> None:
        profile = self._profile_from_ui(game_override=game_override)
        self.store.put_profile(profile)
        self.current_profile = profile
        self.named_profile_dirty = False
        index = self.profile_combo.findData(profile.id)
        if index >= 0:
            self.profile_combo.setItemText(index, profile.name)
        self._update_profile_buttons()
        self.status.setText(f"Saved {profile.name}")

    def save_profile(self) -> None:
        if not self.current_profile or self.current_profile.id == DEFAULT_PROFILE_ID:
            self._autosave_default()
            return
        self._save_current_profile()

    def duplicate_profile(self) -> None:
        if not self.current_profile:
            return
        name, ok = QInputDialog.getText(
            self,
            "Duplicate profile",
            "New name",
            text=f"{self.current_profile.name} copy",
        )
        if not ok or not name.strip():
            return
        profile = self._profile_from_ui(name.strip(), True)
        self.store.put_profile(profile)
        self.game_changed()
        self.profile_combo.setCurrentIndex(self.profile_combo.findData(profile.id))

    def delete_profile(self) -> None:
        if not self.current_profile or self.current_profile.id == DEFAULT_PROFILE_ID:
            return
        if (
            QMessageBox.question(
                self, "Delete profile", f"Delete “{self.current_profile.name}”?"
            )
            == QMessageBox.Yes
        ):
            self.store.delete_profile(self.current_profile)
            self.game_changed()

    def _update_profile_buttons(self) -> None:
        is_default = bool(
            self.current_profile and self.current_profile.id == DEFAULT_PROFILE_ID
        )
        self.save_button.setEnabled(not is_default and self.named_profile_dirty)
        self.delete_button.setEnabled(not is_default)

    def launch(self) -> None:
        followup_record = None
        try:
            game = self.current_game()
            if not game:
                raise ValueError("Choose a game")
            profile = self._resolved_profile(self._profile_from_ui())
            if profile.launch_through_steam and profile.launch_wemod:
                raise ValueError(
                    "Launch with WeMod cannot be combined with Launch through Steam"
                )
            if profile.launch_wemod and not self.store.settings["wemod_launcher_path"]:
                wemod, _ = QFileDialog.getOpenFileName(
                    self, "Choose WeMod Launcher", str(Path.home())
                )
                if not wemod:
                    raise ValueError("Choose the WeMod Launcher executable")
                self.store.settings["wemod_launcher_path"] = wemod
                self.store.save()
                self._update_wemod_status()
            prefix = self._prefix_for_game(game)
            if not profile.use_native_runtime and prepare_compatdata_directory(prefix):
                self._log(f"Created compatibility-data directory: {prefix}", game.key)
            if profile.followup_enabled:
                native_session_id = (
                    uuid.uuid4().hex if profile.use_native_runtime else ""
                )
                target = (
                    primary_executable_name(
                        profile.mode, profile.executable, profile.command
                    )
                    if profile.wait_for_primary_executable
                    else profile.wait_for_executable
                )
                if not target:
                    raise ValueError("Enter the executable name to wait for")
                followup_spec = build_followup_launch_spec(game, profile, prefix)
                watch_prefix = None if profile.use_native_runtime else prefix
                baseline = find_matching_pids(
                    target, watch_prefix, session_id=native_session_id
                )
                followup_record = self.sessions.start(
                    SessionKind.FOLLOWUP,
                    followup_spec,
                    game.key,
                    game.name,
                    prefix,
                    watch_target=target,
                    watch_baseline=baseline,
                    delay_seconds=profile.followup_delay,
                    watch_any_prefix=profile.use_native_runtime,
                    watch_session_id=native_session_id,
                )
                self._register_session(followup_record)
            else:
                native_session_id = ""
            spec = (
                build_steam_launch_spec(game)
                if profile.launch_through_steam
                else build_launch_spec(
                    game,
                    profile,
                    prefix,
                    self.store.settings["wemod_launcher_path"],
                    session_id=native_session_id,
                )
            )
            record = self.sessions.start(
                SessionKind.PRIMARY,
                spec,
                game.key,
                game.name,
                prefix,
                steam_managed=profile.launch_through_steam,
            )
            self._register_session(record)
            self._log("$ " + shlex.join([spec.program, *spec.arguments]), game.key)
            self.status.setText(f"Launched {game.name}")
            self._refresh_sessions()
            if self.store.settings["auto_hide_after_launch"]:
                if QSystemTrayIcon.isSystemTrayAvailable():
                    self.hide()
                    self.tray.showMessage("Proton Launcher", f"Launched {game.name}")
                else:
                    self._log(
                        "Auto-hide skipped because no system tray is available",
                        game.key,
                    )
        except (ValueError, OSError) as exc:
            if followup_record:
                self.sessions.stop(followup_record)
            QMessageBox.warning(self, "Cannot launch", str(exc))
            self._log(f"Launch rejected: {exc}")

    def launch_followup_now(self) -> None:
        try:
            game = self.current_game()
            if not game:
                raise ValueError("Choose a game")
            profile = self._resolved_profile(self._profile_from_ui())
            if not profile.followup_enabled:
                raise ValueError("Enable Follow-up launch first")
            self.stop_followup()
            prefix = self._prefix_for_game(game)
            if not profile.use_native_runtime:
                prepare_compatdata_directory(prefix)
            spec = build_followup_launch_spec(game, profile, prefix)
            record = self.sessions.start(
                SessionKind.FOLLOWUP, spec, game.key, game.name, prefix
            )
            self._register_session(record)
            self._log(
                "Follow-up: $ " + shlex.join([spec.program, *spec.arguments]),
                game.key,
            )
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Cannot launch follow-up", str(exc))

    def launch_wemod(self) -> None:
        try:
            game = self.current_game()
            if not game:
                raise ValueError("Choose a game")
            wemod_path = self.store.settings["wemod_launcher_path"]
            if not wemod_path:
                raise ValueError("Configure the WeMod Launcher path first")
            profile = self._resolved_profile(self._profile_from_ui())
            prefix = self._prefix_for_game(game)
            if prepare_compatdata_directory(prefix):
                self._log(f"Created compatibility-data directory: {prefix}", game.key)
            spec = build_wemod_launch_spec(game, profile, wemod_path, prefix)
            record = self.sessions.start(
                SessionKind.WEMOD,
                spec,
                game.key,
                game.name,
                prefix,
            )
            self._register_session(record)
            self._log(
                "WeMod: $ " + shlex.join([spec.program, *spec.arguments]), game.key
            )
            self.status.setText(f"Launched WeMod for {game.name}")
            self._refresh_sessions()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Cannot launch WeMod", str(exc))
            self._log(f"WeMod launch rejected: {exc}")

    def _register_session(self, record: SessionRecord) -> None:
        self.session_records[record.id] = record
        self.log_offsets.setdefault(record.id, 0)

    def _refresh_sessions(self) -> None:
        active = self.sessions.active()
        previous_selected_active = bool(self._selected_sessions(self.active_sessions))
        self.active_sessions = active
        for record in active:
            self._register_session(record)
        self._tail_session_logs()
        active_ids = {record.id for record in active}
        self.session_records = {
            record_id: record
            for record_id, record in self.session_records.items()
            if record_id in active_ids
        }
        self.log_offsets = {
            record_id: offset
            for record_id, offset in self.log_offsets.items()
            if record_id in active_ids
        }
        game = self.current_game()
        signature = (
            game.key if game else "",
            tuple((record.id, record.kind, record.phase) for record in active),
        )
        if signature == self.active_session_signature:
            return
        self.active_session_signature = signature
        self._update_session_view(active, sessions_finished=previous_selected_active)

    def _update_session_view(
        self, active: list[SessionRecord], *, sessions_finished: bool = False
    ) -> None:
        self._update_session_buttons(active)
        selected = self._selected_sessions(active)
        other_count = len(active) - len(selected)
        if selected:
            labels = [f"{item.kind}: {item.phase}" for item in selected]
            if other_count:
                labels.append(f"{other_count} other game session(s)")
            self.status.setText(" • ".join(labels))
        elif other_count:
            self.status.setText(f"{other_count} session(s) running for other games")
        elif sessions_finished:
            self.status.setText("Sessions finished")
            self._update_prefix_badge()
        else:
            self.status.setText("Ready")

    def _tail_session_logs(self) -> None:
        for record in self.session_records.values():
            path = Path(record.log_path)
            try:
                with path.open("rb") as handle:
                    offset = self.log_offsets.get(record.id, 0)
                    handle.seek(offset)
                    data = handle.read()
                    self.log_offsets[record.id] = handle.tell()
                if data:
                    prefix = {
                        SessionKind.FOLLOWUP.value: "[follow-up] ",
                        SessionKind.WEMOD.value: "[wemod] ",
                    }.get(record.kind, "")
                    cleaned = clean_process_output(data.decode(errors="replace"))
                    for line in cleaned.splitlines():
                        self._log(prefix + line, record.game_key)
            except OSError:
                continue

    def _selected_sessions(self, records: list[SessionRecord]) -> list[SessionRecord]:
        game = self.current_game()
        if not game:
            return []
        return [record for record in records if record.game_key == game.key]

    def _update_session_buttons(self, active: list[SessionRecord]) -> None:
        selected = self._selected_sessions(active)
        has_game = self.current_game() is not None
        primary = any(item.kind == SessionKind.PRIMARY.value for item in selected)
        followup = any(item.kind == SessionKind.FOLLOWUP.value for item in selected)
        wemod = any(item.kind == SessionKind.WEMOD.value for item in selected)
        self.launch_button.setEnabled(has_game and not primary)
        self.launch_wemod_button.setEnabled(self._wemod_is_configured() and not wemod)
        self.stop_game_button.setEnabled(primary)
        self.stop_followup_button.setEnabled(followup)
        self.stop_all_button.setEnabled(primary or followup or wemod)
        (
            self.tray_stop_game.setEnabled(primary)
            if hasattr(self, "tray_stop_game")
            else None
        )
        (
            self.tray_stop_followup.setEnabled(followup)
            if hasattr(self, "tray_stop_followup")
            else None
        )
        (
            self.tray_stop_all.setEnabled(primary or followup or wemod)
            if hasattr(self, "tray_stop_all")
            else None
        )
        self.followup_launch_now_button.setEnabled(
            has_game and self.followup_group.isChecked() and not followup
        )

    def _update_followup_launch_button(self, checked: bool) -> None:
        followup = any(
            record.kind == SessionKind.FOLLOWUP.value
            for record in self._selected_sessions(self.active_sessions)
        )
        self.followup_launch_now_button.setEnabled(
            self.current_game() is not None and checked and not followup
        )

    def stop_game(self) -> None:
        game = self.current_game()
        if not game:
            return
        active = self.sessions.active()
        for record in active:
            if record.game_key != game.key:
                continue
            if record.kind == SessionKind.PRIMARY.value:
                self.sessions.stop(record)
            elif record.kind == SessionKind.FOLLOWUP.value and record.phase in {
                "starting",
                "waiting",
            }:
                self.sessions.stop(record)
        self._log("Stop Game requested", game.key)
        self._refresh_sessions()

    def stop_followup(self) -> None:
        game = self.current_game()
        if not game:
            return
        for record in self.sessions.active():
            if (
                record.game_key == game.key
                and record.kind == SessionKind.FOLLOWUP.value
            ):
                self.sessions.stop(record)
        self._log("Stop Follow-up requested", game.key)
        self._refresh_sessions()

    def stop_all(self) -> None:
        game = self.current_game()
        if not game:
            return
        for record in self.sessions.active():
            if record.game_key == game.key:
                self.sessions.stop(record)
        self._log("Stop All for Game requested", game.key)
        self._refresh_sessions()

    def stop_all_sessions(self) -> None:
        self.sessions.stop_all()
        self._log("Stop all running sessions requested")
        self._refresh_sessions()

    def _prefix_for_game(self, game: GameEntry) -> Path:
        override = self.store.prefix_override(game.key)
        return (
            Path(override).expanduser() if override else game.default_prefix
        ).resolve(strict=False)

    def _update_prefix_badge(self) -> None:
        game = self.current_game()
        if not game:
            self.prefix_badge.setText("Prefix: not selected")
            return
        if self._native_runtime_selected():
            self.prefix_badge.setText("Prefix: Not used by native launch")
            self.prefix_badge.setToolTip(
                "The selected profile runs its target directly on Linux"
            )
            return
        prefix = self._prefix_for_game(game)
        metadata = read_prefix_metadata(prefix, self.protons)
        self.prefix_badge.setText(metadata.badge)
        details = [f"Prefix: {prefix}"]
        if metadata.version:
            details.append(f"Recorded version: {metadata.version}")
        if metadata.proton_root:
            details.append(f"Proton root: {metadata.proton_root}")
        if self.store.prefix_override(game.key):
            details.append("Custom prefix override")
        self.prefix_badge.setToolTip("\n".join(details))

    def open_settings(self, initial_tab: str = "general") -> None:
        # QAction.triggered supplies a bool; only named callers select a tab.
        if not isinstance(initial_tab, str):
            initial_tab = "general"
        dialog = SettingsDialog(
            self.store,
            self.protons,
            self.steam_default_tool,
            self.steam_roots,
            self.libraries,
            self.sessions.backend_name,
            self.refresh,
            self,
            initial_tab=initial_tab,
        )
        dialog.exec()
        self._update_wemod_status()

    def _update_wemod_status(self) -> None:
        path = self.store.settings["wemod_launcher_path"]
        game = self.current_game()
        initialized = bool(
            game
            and (self._prefix_for_game(game) / "pfx" / ".wemod_installer").is_file()
        )
        self.delete_wemod_button.setEnabled(initialized)
        wemod_running = any(
            record.kind == SessionKind.WEMOD.value
            for record in self._selected_sessions(self.active_sessions)
        )
        self.launch_wemod_button.setEnabled(
            self._wemod_is_configured() and not wemod_running
        )
        if not path:
            self.wemod_status.setText("Not configured")
            return
        self.wemod_status.setText(f"Using {Path(path).name}")
        self.wemod_status.setToolTip(path)

    def _wemod_is_configured(self) -> bool:
        path = self.store.settings["wemod_launcher_path"]
        return bool(
            self.current_game()
            and not self._native_runtime_selected()
            and path
            and Path(path).expanduser().is_file()
        )

    def delete_wemod(self) -> None:
        game = self.current_game()
        if not game:
            return
        prefix = self._prefix_for_game(game)
        marker = prefix / "pfx" / ".wemod_installer"
        if not marker.is_file():
            QMessageBox.information(
                self,
                "WeMod is not initialized",
                f"There is no WeMod setup to remove from:\n{prefix}",
            )
            self._update_wemod_status()
            return

        answer = QMessageBox.warning(
            self,
            "Delete WeMod setup?",
            f"Remove WeMod setup from the prefix for {game.name}?\n\n"
            f"{prefix}\n\n"
            "The game prefix, saves, and shared WeMod login data are kept. "
            "The managed retry helper in this Steam library is removed. "
            "The next Launch with WeMod will run setup again and may offer to "
            "copy a compatible initialized prefix.\n\n"
            "Installed .NET files and registry changes are shared with Wine and "
            "cannot be removed safely on their own.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        for record in self.sessions.active():
            if Path(record.prefix).resolve(strict=False) == prefix:
                self.sessions.stop(record)

        try:
            removed = reset_wemod_prefix(prefix, game.library_root)
        except OSError as error:
            QMessageBox.critical(self, "Could not delete WeMod setup", str(error))
            self._log(f"WeMod setup deletion failed for {prefix}: {error}")
            return

        self._log("Removed WeMod setup: " + ", ".join(map(str, removed)))
        self.status.setText(f"WeMod setup deleted for {game.name}")
        self._update_wemod_status()

    def set_prefix(self) -> None:
        game = self.current_game()
        if not game:
            return
        choice = QMessageBox(self)
        choice.setWindowTitle("Compatibility-data prefix")
        choice.setText(f"Manage the prefix for {game.name}")
        choose_button = choice.addButton(
            "Choose custom prefix…", QMessageBox.AcceptRole
        )
        reset_button = choice.addButton("Use default prefix", QMessageBox.ActionRole)
        choice.addButton(QMessageBox.Cancel)
        choice.exec()
        if choice.clickedButton() == reset_button:
            self.store.set_prefix_override(game.key, "")
            self._update_prefix_badge()
            return
        if choice.clickedButton() != choose_button:
            return
        current = self.store.prefix_override(game.key) or str(game.default_prefix)
        path = QFileDialog.getExistingDirectory(
            self, "Choose compatibility-data prefix", current
        )
        if path:
            self.store.set_prefix_override(game.key, path)
            self._update_prefix_badge()

    def delete_prefix(self) -> None:
        game = self.current_game()
        if not game:
            return
        prefix = self._prefix_for_game(game)
        if not prefix.exists():
            QMessageBox.information(
                self,
                "Prefix does not exist",
                f"There is no prefix to delete at:\n{prefix}",
            )
            return

        protected = {
            Path.home().resolve(strict=False),
            game.steam_root.resolve(strict=False),
            game.library_root.resolve(strict=False),
        }
        if game.install_dir:
            protected.add(game.install_dir.resolve(strict=False))
        if prefix == Path("/") or prefix in protected or len(prefix.parts) < 4:
            QMessageBox.critical(
                self,
                "Unsafe prefix path",
                f"Refusing to delete a protected or overly broad path:\n{prefix}",
            )
            return

        answer = QMessageBox.warning(
            self,
            "Delete compatibility prefix?",
            f"Delete the entire compatibility prefix for {game.name}?\n\n"
            f"{prefix}\n\n"
            "This removes the Wine prefix, installed dependencies, settings, "
            "and any game saves stored inside it. Cloud or externally stored "
            "saves are not affected. Running sessions for this prefix will be "
            "stopped first.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        for record in self.sessions.active():
            if Path(record.prefix).resolve(strict=False) == prefix:
                self.sessions.stop(record)

        try:
            gio = shutil.which("gio")
            if gio:
                result = subprocess.run(
                    [gio, "trash", str(prefix)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                if result.returncode:
                    raise OSError(result.stderr.strip() or "gio trash failed")
                destination = "Trash"
            else:
                shutil.rmtree(prefix)
                destination = "permanently"
        except (OSError, subprocess.TimeoutExpired) as error:
            QMessageBox.critical(self, "Could not delete prefix", str(error))
            self._log(f"Prefix deletion failed for {prefix}: {error}")
            return

        self._log(f"Deleted prefix {prefix} ({destination})")
        self.status.setText(f"Prefix deleted for {game.name}")
        self._update_prefix_badge()

    def open_prefix(self) -> None:
        game = self.current_game()
        if not game:
            return
        prefix = self._prefix_for_game(game)
        if not prefix.is_dir():
            QMessageBox.information(
                self,
                "Prefix does not exist",
                f"There is no prefix folder to open at:\n{prefix}",
            )
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(prefix))):
            QMessageBox.warning(
                self,
                "Could not open prefix",
                f"The desktop file manager could not open:\n{prefix}",
            )

    def browse_executable(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Windows executable",
            self.exe_edit.text() or str(Path.home()),
            "Windows executables (*.exe);;All files (*)",
        )
        if path:
            self.exe_edit.setText(path)

    def browse_working(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Choose working directory",
            self.working_edit.text() or str(Path.home()),
        )
        if path:
            self.working_edit.setText(path)

    def browse_followup(self) -> None:
        if self.followup_mode_combo.currentData() != "executable":
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose follow-up executable",
            self.followup_target_edit.text() or str(Path.home()),
            "Windows executables (*.exe);;All files (*)",
        )
        if path:
            self.followup_target_edit.setText(path)

    def configure_runtime_options(self) -> None:
        try:
            profile = self._profile_from_ui()
        except ValueError as error:
            QMessageBox.warning(self, "Cannot configure launch options", str(error))
            return
        dialog = RuntimeOptionsDialog(profile, self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        self.runtime_option_values.update(
            {field: values[field] for field in RUNTIME_OPTION_FIELDS if field in values}
        )
        if self.runtime_option_values["enable_hdr"]:
            self.wayland_checkbox.setChecked(True)
        self._field_changed()

    def _online_fix_toggled(self, checked: bool) -> None:
        if self.loading_profile:
            return
        if checked and not self.overlay_checkbox.isChecked():
            self.overlay_checkbox.setChecked(True)

    def _steam_launch_mode_changed(self, *_args) -> None:
        direct = not self.steam_launch_checkbox.isChecked()
        self.overlay_checkbox.setEnabled(direct)
        for widget, program in (
            (self.gamemode_checkbox, "gamemoderun"),
            (self.mangohud_checkbox, "mangohud"),
            (self.gamescope_checkbox, "gamescope"),
        ):
            widget.setEnabled(
                direct and (bool(shutil.which(program)) or widget.isChecked())
            )
        self._update_gamescope_availability()
        self._update_wayland_availability()
        self.runtime_options_button.setEnabled(direct)
        self._overlay_mode_changed()
        self._wemod_mode_changed()

    def _overlay_mode_changed(self, *_args) -> None:
        self.overlay_app_id_edit.setEnabled(
            self.overlay_checkbox.isChecked()
            and not self.steam_launch_checkbox.isChecked()
        )

    def _wemod_mode_changed(self, *_args) -> None:
        if not hasattr(self, "wemod_checkbox"):
            return
        compatible = (
            self.tabs.currentIndex() == 0
            and not self.steam_launch_checkbox.isChecked()
            and not self._native_runtime_selected()
        )
        self.wemod_checkbox.setEnabled(compatible)
        self._update_gamescope_availability()
        self._update_wayland_availability()

    def _update_gamescope_availability(self) -> None:
        if not hasattr(self, "gamescope_checkbox"):
            return
        if self.wemod_checkbox.isChecked():
            self.gamescope_checkbox.setEnabled(False)
            self.gamescope_checkbox.setToolTip(
                "Unavailable because Gamescope is incompatible with WeMod"
            )
            return
        direct = not self.steam_launch_checkbox.isChecked()
        installed = bool(shutil.which("gamescope"))
        self.gamescope_checkbox.setEnabled(
            direct and (installed or self.gamescope_checkbox.isChecked())
        )
        self.gamescope_checkbox.setToolTip(
            "" if installed else "gamescope is not installed"
        )

    def _update_wayland_availability(self) -> None:
        if not hasattr(self, "wayland_checkbox"):
            return
        if self.wemod_checkbox.isChecked():
            self.wayland_checkbox.setEnabled(False)
            self.wayland_checkbox.setToolTip(
                "Unavailable with WeMod because WeMod and the game share one Wine "
                "display driver"
            )
            return
        if self._native_runtime_selected():
            self.wayland_checkbox.setEnabled(False)
            self.wayland_checkbox.setToolTip(
                "Native Linux programs already use their host display backend"
            )
            return
        direct = not self.steam_launch_checkbox.isChecked()
        self.wayland_checkbox.setEnabled(direct)
        self.wayland_checkbox.setToolTip(
            "Experimental Proton/GE-Proton Wine-Wayland driver; Steam overlay may "
            "not work"
        )

    def _mode_changed(self, *_args) -> None:
        self._update_wait_target()
        self._wemod_mode_changed()

    def _followup_mode_changed(self, *_args) -> None:
        executable = self.followup_mode_combo.currentData() == "executable"
        self.followup_browse_button.setEnabled(executable)
        self.followup_admin_checkbox.setEnabled(
            executable and not self._native_runtime_selected()
        )
        self.followup_target_edit.setPlaceholderText(
            "/path/to/tool.exe" if executable else "wine explorer ."
        )

    def _update_wait_target(self, *_args) -> None:
        if not hasattr(self, "wait_primary_checkbox"):
            return
        automatic = self.wait_primary_checkbox.isChecked()
        self.wait_exe_edit.setEnabled(not automatic)
        if automatic:
            mode = "executable" if self.tabs.currentIndex() == 0 else "command"
            try:
                target = primary_executable_name(
                    mode, self.exe_edit.text(), self.command_edit.text()
                )
            except ValueError:
                target = ""
            self.wait_exe_edit.setText(target)

    def _update_proton_tooltip(self, *_args) -> None:
        data = str(self.proton_combo.currentData() or "")
        if data == "__default__" and self.default_proton:
            data = str(self.default_proton.launcher)
        self.proton_combo.setToolTip(
            "Runs the executable directly without Proton"
            if data == "__native__"
            else data
        )

    def _native_runtime_selected(self) -> bool:
        return self.proton_combo.currentData() == "__native__"

    def _proton_mode_changed(self, *_args) -> None:
        self._update_proton_tooltip()
        if not hasattr(self, "admin_checkbox"):
            return
        native = self._native_runtime_selected()
        self.steam_launch_checkbox.setEnabled(not native)
        self.admin_checkbox.setEnabled(not native)
        self.followup_admin_checkbox.setEnabled(
            not native and self.followup_mode_combo.currentData() == "executable"
        )
        self.online_fix_checkbox.setEnabled(not native)
        if native:
            self.steam_launch_checkbox.setChecked(False)
            self.admin_checkbox.setChecked(False)
            self.followup_admin_checkbox.setChecked(False)
            self.online_fix_checkbox.setChecked(False)
            self.wemod_checkbox.setChecked(False)
        self._wemod_mode_changed()
        self._update_wayland_availability()
        self._update_prefix_badge()

    def copy_steam_launch_options(self) -> None:
        try:
            profile = self._profile_from_ui()
            environment = parse_environment_text(profile.environment_text)
            if profile.apply_online_fix:
                from .models import DEFAULT_NON_STEAM_WINEDLLOVERRIDES

                environment["WINEDLLOVERRIDES"] = DEFAULT_NON_STEAM_WINEDLLOVERRIDES
            assignments = [
                f"{name}={shlex.quote(value)}" for name, value in environment.items()
            ]
            arguments = shlex.split(profile.arguments)
            result = " ".join(
                [*assignments, "%command%", *(shlex.quote(item) for item in arguments)]
            )
            QApplication.clipboard().setText(result)
            self.status.setText("Steam Launch Options copied")
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot generate Steam Launch Options", str(exc))

    def _console_key(self) -> str:
        return str(self.game_combo.currentData() or "__application__")

    def _log(self, text: str, game_key: str | None = None) -> None:
        key = game_key or self._console_key()
        lines = self.console_lines.setdefault(key, [])
        lines.extend(text.splitlines() or [""])
        if len(lines) > 5000:
            del lines[:-5000]
        if key == self._console_key():
            self.log.appendPlainText(text)

    def _render_console(self) -> None:
        self.log.setPlainText(
            "\n".join(self.console_lines.get(self._console_key(), []))
        )
        scrollbar = self.log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def clear_console(self) -> None:
        self.console_lines.pop(self._console_key(), None)
        self.log.clear()

    def copy_console(self) -> None:
        QApplication.clipboard().setText(self.log.toPlainText())

    def _show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit_from_menu(self) -> None:
        self.close()

    def _quit_keep_sessions(self) -> None:
        self._force_quit = True
        QApplication.quit()

    def _stop_and_quit(self) -> None:
        self.sessions.stop_all()
        self._force_quit = True
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.autosave_timer.isActive():
            self._autosave_default()
        if self._force_quit:
            event.accept()
            return
        active = self.sessions.active()
        if not active:
            event.accept()
            QTimer.singleShot(0, QApplication.quit)
            return
        behavior = self.store.settings["close_behavior"]
        if behavior == "ask":
            box = QMessageBox(self)
            box.setWindowTitle("Sessions are still running")
            box.setText("What should Proton Launcher do with the running sessions?")
            hide_button = box.addButton("Hide to tray", QMessageBox.AcceptRole)
            keep_button = box.addButton(
                "Exit and keep running", QMessageBox.DestructiveRole
            )
            box.addButton("Stop all and exit", QMessageBox.DestructiveRole)
            cancel_button = box.addButton(QMessageBox.Cancel)
            remember = QCheckBox("Remember my choice")
            box.setCheckBox(remember)
            box.exec()
            clicked = box.clickedButton()
            if clicked == cancel_button:
                event.ignore()
                return
            behavior = (
                "tray"
                if clicked == hide_button
                else "keep-running" if clicked == keep_button else "stop-and-exit"
            )
            if remember.isChecked():
                self.store.settings["close_behavior"] = behavior
                self.store.save()
        if behavior == "tray":
            if not QSystemTrayIcon.isSystemTrayAvailable():
                QMessageBox.warning(
                    self,
                    "System tray unavailable",
                    "The window cannot be hidden because no system tray is available.",
                )
                event.ignore()
                return
            self.hide()
            event.ignore()
            return
        if behavior == "stop-and-exit":
            self.sessions.stop_all()
        event.accept()
        self._force_quit = True
        QTimer.singleShot(0, QApplication.quit)
