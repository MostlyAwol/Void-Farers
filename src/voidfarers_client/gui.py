from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import uuid
import webbrowser
from pathlib import Path
from typing import Any


import sounddevice as sd
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot, QPoint
from PySide6.QtGui import QAction, QIcon
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
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .app_state import ClientSettings, DEFAULT_BACKEND_URL, SystemState
from .audio import AudioEngine
from .backend import BackendError, get_me, pair_client
from .config import default_config_path, load_config, save_config
from .journal import default_journal_dir, read_last_commander_name, watch_system_changes
from .ptt import PushToTalk, capture_ptt_binding, describe_ptt_binding
from .voice import VoiceClient


def config_get(config: dict[str, Any], key: str, fallback: Any) -> Any:
    value = config.get(key)
    return fallback if value is None else value


def get_audio_devices() -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    input_devices: list[tuple[int, str]] = []
    output_devices: list[tuple[int, str]] = []

    devices = sd.query_devices()

    for index, device in enumerate(devices):
        name = str(device.get("name", f"Device {index}"))
        max_input = int(device.get("max_input_channels", 0))
        max_output = int(device.get("max_output_channels", 0))

        label = f"{index}: {name}"

        if max_input > 0:
            input_devices.append((index, label))

        if max_output > 0:
            output_devices.append((index, label))

    return input_devices, output_devices


def resource_path(relative_path: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / relative_path
    return Path(__file__).resolve().parents[2] / relative_path


class AccountLinkDialog(QDialog):
    def __init__(
        self,
        parent: "MainWindow",
        *,
        first_run: bool = False,
    ) -> None:
        super().__init__(parent)

        self.parent_window = parent
        self.first_run = first_run

        self.setWindowTitle("Link Frontier Account")
        self.resize(520, 250)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Voidfarers requires a linked Frontier commander account before connecting "
            "to voice rooms.\n\n"
            "1. Open the link page.\n"
            "2. Log in with Frontier.\n"
            "3. Copy the pairing code.\n"
            "4. Paste it below and click Link Account."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.open_link_button = QPushButton("Open Account Link Page")
        self.open_link_button.clicked.connect(self._open_link_page)
        layout.addWidget(self.open_link_button)

        form = QFormLayout()
        self.pairing_code_edit = QLineEdit()
        self.pairing_code_edit.setPlaceholderText("VF-XXXX-XXXX")

        form.addRow("Pairing code:", self.pairing_code_edit)
        layout.addLayout(form)

        self.status_label = QLabel(parent.account_status_text())
        layout.addWidget(self.status_label)

        buttons_row = QHBoxLayout()

        self.link_button = QPushButton("Link Account")
        self.link_button.clicked.connect(self._link_account)

        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)

        buttons_row.addWidget(self.link_button)
        buttons_row.addStretch()
        buttons_row.addWidget(self.close_button)

        layout.addLayout(buttons_row)

    def _open_link_page(self) -> None:
        url = self.parent_window.backend_url.rstrip("/") + "/link"
        webbrowser.open(url)

    def _link_account(self) -> None:
        parent = self.parent_window
        pairing_code = self.pairing_code_edit.text().strip()

        if not pairing_code:
            QMessageBox.warning(self, "Pairing Code Required", "Enter a pairing code first.")
            return

        try:
            data = pair_client(
                backend_url=parent.backend_url,
                client_id=parent.client_id,
                pairing_code=pairing_code,
                platform="windows",
            )
        except BackendError as exc:
            QMessageBox.warning(self, "Pairing Failed", str(exc))
            return

        parent.session_token = data["session_token"]
        parent.verified = True
        parent.verified_commander_name = data["commander_name"]
        parent.verified_frontier_id = data["frontier_commander_id"]

        if parent.verified_commander_name:
            parent.display_name_edit.setText(parent.verified_commander_name)

        parent.account_status_label.setText(parent.account_status_text())
        parent._save_settings_from_ui()

        self.status_label.setText(parent.account_status_text())

        QMessageBox.information(
            self,
            "Account Linked",
            f"Linked as {parent.verified_commander_name}",
        )

        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, parent: "MainWindow") -> None:
        super().__init__(parent)

        self.parent_window = parent
        self.setWindowTitle("Voidfarers Settings")
        self.resize(540, 260)

        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.client_id_edit = QLineEdit(parent.client_id)
        self.client_id_edit.setPlaceholderText("Generated automatically if empty")

        self.backend_url_edit = QLineEdit(parent.backend_url)

        self.journal_dir_edit = QLineEdit(str(parent.journal_dir))
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self._browse_journal_dir)

        journal_row = QHBoxLayout()
        journal_row.addWidget(self.journal_dir_edit, 1)
        journal_row.addWidget(self.browse_button)

        self.start_minimized_checkbox = QCheckBox("Start minimized")
        self.start_minimized_checkbox.setChecked(parent.start_minimized)

        self.auto_connect_checkbox = QCheckBox("Auto-connect on launch")
        self.auto_connect_checkbox.setChecked(parent.auto_connect)

        self.minimize_to_tray_checkbox = QCheckBox("Minimize/close to tray")
        self.minimize_to_tray_checkbox.setChecked(parent.minimize_to_tray)

        form.addRow("Client ID:", self.client_id_edit)
        form.addRow("Backend URL:", self.backend_url_edit)
        form.addRow("Journal folder:", journal_row)
        form.addRow("", self.start_minimized_checkbox)
        form.addRow("", self.auto_connect_checkbox)
        form.addRow("", self.minimize_to_tray_checkbox)

        layout.addLayout(form)

        account_box = QGroupBox("Account")
        account_layout = QVBoxLayout(account_box)

        account_layout.addWidget(QLabel(parent.account_status_text()))

        link_button = QPushButton("Link Account...")
        link_button.clicked.connect(self._open_account_link)
        account_layout.addWidget(link_button)

        clear_button = QPushButton("Clear Linked Account")
        clear_button.clicked.connect(self._clear_linked_account)
        account_layout.addWidget(clear_button)

        layout.addWidget(account_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout.addWidget(buttons)

    def _browse_journal_dir(self) -> None:
        start_dir = self.journal_dir_edit.text().strip() or str(default_journal_dir())
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select Elite Dangerous Journal Folder",
            start_dir,
        )

        if chosen:
            self.journal_dir_edit.setText(chosen)

    def _open_account_link(self) -> None:
        dialog = AccountLinkDialog(self.parent_window)
        dialog.exec()

    def _clear_linked_account(self) -> None:
        parent = self.parent_window
        parent.session_token = ""
        parent.verified = False
        parent.verified_commander_name = ""
        parent.verified_frontier_id = ""
        parent.account_status_label.setText(parent.account_status_text())
        parent._save_settings_from_ui()
        QMessageBox.information(self, "Account Cleared", "Linked account cleared on this client.")

    def values(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id_edit.text().strip(),
            "backend_url": self.backend_url_edit.text().strip() or DEFAULT_BACKEND_URL,
            "journal_dir": self.journal_dir_edit.text().strip() or str(default_journal_dir()),
            "start_minimized": self.start_minimized_checkbox.isChecked(),
            "auto_connect": self.auto_connect_checkbox.isChecked(),
            "minimize_to_tray": self.minimize_to_tray_checkbox.isChecked(),
        }

class PttCaptureNotifier(QObject):
    finished = Signal(object)

class VoiceWorker(QObject):
    log = Signal(str)
    error = Signal(str)
    connected = Signal(str, str)
    disconnected = Signal()
    skipped_connection = Signal(str)
    system_changed = Signal(str, str)
    commander_detected = Signal(str)
    verified_identity = Signal(str)
    participant_joined = Signal(str, str)
    participant_left = Signal(str, str)
    participants_snapshot = Signal(list)
    stats = Signal(bool, float, int, int)

    def __init__(self, settings: ClientSettings) -> None:
        super().__init__()
        self.settings = settings

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread_id: int | None = None

        self._running = False
        self._stop_requested = False

        self.ptt: PushToTalk | None = None
        self.audio: AudioEngine | None = None
        self.voice: VoiceClient | None = None

        self._publish_task: asyncio.Task | None = None
        self._stats_task: asyncio.Task | None = None
        self._journal_thread: threading.Thread | None = None

    @Slot()
    def start(self) -> None:
        try:
            self._running = True
            self._stop_requested = False
            asyncio.run(self._run())
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self._running = False
            self.disconnected.emit()

    @Slot()
    def request_stop(self) -> None:
        self._stop_requested = True

        if self.voice:
            self.voice.running = False

    @Slot(bool)
    def set_muted(self, muted: bool) -> None:
        if self.audio:
            self.audio.set_muted(muted)

    @Slot(bool)
    def set_deafened(self, deafened: bool) -> None:
        if self.audio:
            self.audio.set_deafened(deafened)

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread_id = threading.get_ident()

        self.ptt = PushToTalk(self.settings.ptt_key)
        self.audio = AudioEngine(
            ptt=self.ptt,
            input_device=self.settings.input_device,
            output_device=self.settings.output_device,
        )
        self.audio.set_muted(self.settings.muted)
        self.audio.set_deafened(self.settings.deafened)

        self.voice = VoiceClient(
            backend_url=self.settings.backend_url,
            client_id=self.settings.client_id,
            display_name=self.settings.display_name,
            session_token=self.settings.session_token,
            audio=self.audio,
            on_log=self.log.emit,
            on_error=self.error.emit,
            on_system_changed=lambda state: self.system_changed.emit(
                state.system_name,
                state.system_address,
            ),
            on_participant_joined=self.participant_joined.emit,
            on_participant_left=self.participant_left.emit,
            on_verified_identity=self.verified_identity.emit,
        )

        self.ptt.start()
        self.audio.start()

        self._publish_task = asyncio.create_task(self.voice.mic_publish_loop())
        self._stats_task = asyncio.create_task(self._stats_loop())

        try:
            await self._run_journal_mode()
        finally:
            await self._shutdown()

    async def _run_journal_mode(self) -> None:
        assert self.voice is not None
        assert self._loop is not None

        journal_dir = self.settings.journal_dir or default_journal_dir()
        state_queue: asyncio.Queue[SystemState] = asyncio.Queue()

        def watcher_thread() -> None:
            try:
                for state in watch_system_changes(journal_dir):
                    if self._stop_requested or not self.voice or not self.voice.running:
                        break
                    self._loop.call_soon_threadsafe(state_queue.put_nowait, state)
            except Exception as exc:
                self.error.emit(f"Journal watcher error: {exc}")

        self._journal_thread = threading.Thread(target=watcher_thread, daemon=True)
        self._journal_thread.start()

        self.log.emit(f"Watching journal folder: {journal_dir}")

        while self.voice.running and not self._stop_requested:
            try:
                state = await asyncio.wait_for(state_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue

            if state == self.voice.current_state:
                continue

            await self._connect_or_skip(state)

    async def _connect_or_skip(self, state: SystemState) -> None:
        assert self.voice is not None

        if state.commander_name:
            self.commander_detected.emit(state.commander_name)

            if self.voice.display_name in {"", "CMDR Test"}:
                self.voice.display_name = state.commander_name

        self.system_changed.emit(state.system_name, state.system_address)

        if not state.in_game:
            if self.voice.room:
                await self.voice.disconnect_room()
            self.skipped_connection.emit("Waiting for active Elite Dangerous session...")
            return

        if state.is_solo:
            if self.voice.room:
                await self.voice.disconnect_room()
            self.skipped_connection.emit("Solo mode detected. Voice is not connected.")
            return

        await self.voice.connect_to_system(state)
        self.connected.emit(state.system_name, state.system_address)
        self.emit_participants_snapshot()

    async def _stats_loop(self) -> None:
        while not self._stop_requested:
            if self.ptt and self.audio:
                self.stats.emit(
                    self.ptt.active,
                    self.audio.last_mic_level,
                    self.audio.output_buffer_ms(),
                    self.audio.frames_dropped,
                )
            await asyncio.sleep(0.25)

    async def _shutdown(self) -> None:
        if self.voice:
            self.voice.running = False

        tasks = [self._publish_task, self._stats_task]

        for task in tasks:
            if task:
                task.cancel()

        for task in tasks:
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if self.voice:
            await self.voice.disconnect_room()

        if self.ptt:
            self.ptt.stop()

        if self.audio:
            self.audio.stop()

        self.log.emit("Disconnected.")

    def emit_participants_snapshot(self) -> None:
        if not self.voice or not self.voice.room:
            self.participants_snapshot.emit([])
            return

        participants = []

        for participant in self.voice.room.remote_participants.values():
            participants.append((participant.identity, participant.name))

        self.participants_snapshot.emit(participants)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle("Voidfarers Voice Client - Version b1.01a")
        self.resize(760, 580)
        self.setMinimumSize(760, 580)

        self.config_path = default_config_path()
        self.config = load_config(self.config_path)

        self.worker_thread: QThread | None = None
        self.worker: VoiceWorker | None = None
        self._really_quit = False

        self.input_devices: list[tuple[int, str]] = []
        self.output_devices: list[tuple[int, str]] = []

        self.client_id = str(config_get(self.config, "client_id", f"vf-{uuid.uuid4()}"))
        self.backend_url = str(config_get(self.config, "backend_url", DEFAULT_BACKEND_URL))
        self.journal_dir = Path(
            str(config_get(self.config, "journal_dir", str(default_journal_dir())))
        )
        self.start_minimized = bool(config_get(self.config, "start_minimized", False))
        self.auto_connect = bool(config_get(self.config, "auto_connect", False))
        self.minimize_to_tray = bool(config_get(self.config, "minimize_to_tray", True))

        self.session_token = str(config_get(self.config, "session_token", ""))
        self.verified = bool(config_get(self.config, "verified", False))
        self.verified_commander_name = str(config_get(self.config, "verified_commander_name", ""))
        self.verified_frontier_id = str(config_get(self.config, "verified_frontier_id", ""))

        self._build_menu()
        self._build_ui()
        self._load_settings_into_ui()
        self._populate_audio_devices()
        self._setup_tray()
        self._try_apply_commander_name_from_journal()

        if self.session_token:
            self._refresh_account_status(show_message=False)

        if not self.session_token:
            QTimer.singleShot(300, self._show_first_run_link_dialog)

        if self.start_minimized:
            QTimer.singleShot(0, self.hide)

        if self.auto_connect and self.session_token:
            QTimer.singleShot(500, self._connect)

        self.stats_timer = QTimer(self)
        self.stats_timer.setInterval(1000)

    def account_status_text(self) -> str:
        if self.verified:
            name = self.verified_commander_name or "Unknown Commander"
            fid = self.verified_frontier_id or "Unknown ID"
            return f"Verified as {name} ({fid})"
        return "Account not linked"

    def _show_first_run_link_dialog(self) -> None:
        if self.session_token:
            return

        dialog = AccountLinkDialog(self, first_run=True)
        dialog.exec()

    def _refresh_account_status(self, show_message: bool = False) -> None:
        if not self.session_token:
            self.verified = False
            self.verified_commander_name = ""
            self.verified_frontier_id = ""
            self.account_status_label.setText(self.account_status_text())
            if show_message:
                QMessageBox.information(self, "Account Status", "No linked account.")
            return

        try:
            data = get_me(
                backend_url=self.backend_url,
                client_id=self.client_id,
                session_token=self.session_token,
            )
        except BackendError as exc:
            if show_message:
                QMessageBox.warning(self, "Account Check Failed", str(exc))
            return

        self.verified = bool(data.get("verified", False))

        if self.verified:
            self.verified_commander_name = str(data.get("commander_name") or "")
            self.verified_frontier_id = str(data.get("frontier_commander_id") or "")

            if self.verified_commander_name:
                self.display_name_edit.setText(self.verified_commander_name)

            if show_message:
                QMessageBox.information(
                    self,
                    "Account Status",
                    self.account_status_text(),
                )
        else:
            self.verified_commander_name = ""
            self.verified_frontier_id = ""
            if show_message:
                QMessageBox.information(self, "Account Status", "Session is not verified.")

        self.account_status_label.setText(self.account_status_text())
        self._save_settings_from_ui()

    def _build_menu(self) -> None:
        menu_bar = QMenuBar(self)
        self.setMenuBar(menu_bar)

        app_menu = menu_bar.addMenu("Voidfarers")

        self.link_account_action = QAction("Link Account...", self)
        self.link_account_action.triggered.connect(self._open_account_link_dialog)
        app_menu.addAction(self.link_account_action)

        self.settings_action = QAction("Settings...", self)
        self.settings_action.triggered.connect(self._open_settings_dialog)
        app_menu.addAction(self.settings_action)

        app_menu.addSeparator()

        self.quit_menu_action = QAction("Quit", self)
        self.quit_menu_action.triggered.connect(self._quit_from_tray)
        app_menu.addAction(self.quit_menu_action)

    def _setup_tray(self) -> None:
        self.tray_icon = QSystemTrayIcon(self)

        icon_path = resource_path("assets/voidfarers.ico")

        if icon_path.exists():
            icon = QIcon(str(icon_path))
            self.log(f"Loaded icon: {icon_path}")
        else:
            icon = self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon)
            self.log(f"Icon not found, using fallback: {icon_path}")

        self.tray_icon.setIcon(icon)
        self.setWindowIcon(icon)

        tray_menu = QMenu(self)

        self.show_action = QAction("Show Voidfarers", self)
        self.show_action.triggered.connect(self._show_from_tray)
        tray_menu.addAction(self.show_action)

        self.connect_action = QAction("Connect", self)
        self.connect_action.triggered.connect(self._connect)
        tray_menu.addAction(self.connect_action)

        self.disconnect_action = QAction("Disconnect", self)
        self.disconnect_action.triggered.connect(self._disconnect)
        self.disconnect_action.setEnabled(False)
        tray_menu.addAction(self.disconnect_action)

        tray_menu.addSeparator()

        self.quit_action = QAction("Quit", self)
        self.quit_action.triggered.connect(self._quit_from_tray)
        tray_menu.addAction(self.quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.setToolTip("Voidfarers Voice Client")
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        self._really_quit = True

        if self.tray_icon:
            self.tray_icon.hide()

        if self.worker:
            self.status_label.setText("Quitting...")
            self.worker.request_stop()
            QTimer.singleShot(1000, self._force_quit)
        else:
            self._force_quit()

    def _force_quit(self) -> None:
        self._really_quit = True

        if self.tray_icon:
            self.tray_icon.hide()

        app = QApplication.instance()
        if app:
            app.quit()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_from_tray()

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 8, 10, 8)
        root_layout.setSpacing(8)

        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("font-weight: bold;")
        root_layout.addWidget(self.status_label)

        self.account_status_label = QLabel("Account not linked")
        root_layout.addWidget(self.account_status_label)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        identity_box = QGroupBox("Identity")
        identity_layout = QFormLayout(identity_box)
        identity_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.display_name_edit = QLineEdit()
        self.client_id_label = QLabel(self.client_id)

        self.link_account_button = QPushButton("Link Account...")
        self.link_account_button.clicked.connect(self._open_account_link_dialog)

        display_row = QHBoxLayout()
        display_row.addWidget(self.display_name_edit, 1)
        display_row.addWidget(self.link_account_button)

        identity_layout.addRow("Display Name:", display_row)
        identity_layout.addRow("Client ID:", self.client_id_label)

        participants_box = QGroupBox("Participants")
        participants_layout = QVBoxLayout(participants_box)
        participants_layout.setContentsMargins(8, 8, 8, 8)

        self.participants_list = QListWidget()
        self.participants_list.setMinimumHeight(70)
        participants_layout.addWidget(self.participants_list)

        top_row.addWidget(identity_box, 1)
        top_row.addWidget(participants_box, 1)

        root_layout.addLayout(top_row)

        audio_box = QGroupBox("Audio")
        audio_layout = QFormLayout(audio_box)
        audio_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.input_device_combo = QComboBox()
        self.output_device_combo = QComboBox()
        self.refresh_devices_button = QPushButton("Refresh devices")
        self.refresh_devices_button.clicked.connect(self._populate_audio_devices)

        self.ptt_key_edit = QLineEdit()
        self.ptt_key_edit.setReadOnly(True)
        self.ptt_key_edit.setPlaceholderText("keyboard:f12")

        self.set_ptt_button = QPushButton("Set...")
        self.set_ptt_button.clicked.connect(self._capture_ptt_binding)

        self.mute_checkbox = QCheckBox("Mute microphone")
        self.deafen_checkbox = QCheckBox("Deafen output")

        audio_layout.addRow("Input:", self.input_device_combo)
        audio_layout.addRow("Output:", self.output_device_combo)

        ptt_row = QHBoxLayout()
        ptt_row.addWidget(self.ptt_key_edit, 1)
        ptt_row.addWidget(self.set_ptt_button)

        audio_layout.addRow("PTT:", ptt_row)

        audio_controls_row = QHBoxLayout()
        audio_controls_row.addWidget(self.refresh_devices_button)
        audio_controls_row.addWidget(self.mute_checkbox)
        audio_controls_row.addWidget(self.deafen_checkbox)
        audio_controls_row.addStretch()

        audio_layout.addRow("", audio_controls_row)

        self.mute_checkbox.toggled.connect(self._on_mute_toggled)
        self.deafen_checkbox.toggled.connect(self._on_deafen_toggled)

        root_layout.addWidget(audio_box)

        status_box = QGroupBox("Live Status")
        status_layout = QHBoxLayout(status_box)
        status_layout.setSpacing(8)

        self.current_system_label = QLabel("None")
        self.current_room_label = QLabel("None")
        self.current_game_mode_label = QLabel("Unknown")
        self.ptt_status_label = QLabel("--")
        self.output_buffer_label = QLabel("0 ms")
        self.dropped_label = QLabel("0")

        self.mic_meter = QProgressBar()
        self.mic_meter.setRange(0, 100)
        self.mic_meter.setValue(0)
        self.mic_meter.setTextVisible(True)
        self.mic_meter.setMaximumWidth(150)

        status_layout.addWidget(QLabel("System:"))
        status_layout.addWidget(self.current_system_label, 2)
        status_layout.addWidget(QLabel("Mode:"))
        status_layout.addWidget(self.current_game_mode_label)
        status_layout.addWidget(QLabel("Room:"))
        status_layout.addWidget(self.current_room_label, 2)
        status_layout.addWidget(QLabel("PTT:"))
        status_layout.addWidget(self.ptt_status_label)
        status_layout.addWidget(QLabel("Mic:"))
        status_layout.addWidget(self.mic_meter)
        status_layout.addWidget(QLabel("Buf:"))
        status_layout.addWidget(self.output_buffer_label)
        status_layout.addWidget(QLabel("Drop:"))
        status_layout.addWidget(self.dropped_label)

        root_layout.addWidget(status_box)

        buttons_layout = QHBoxLayout()
        self.connect_button = QPushButton("Start")
        self.disconnect_button = QPushButton("Stop")
        self.save_button = QPushButton("Save")

        self.disconnect_button.setEnabled(False)

        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.save_button.clicked.connect(self._save_settings_from_ui)

        buttons_layout.addWidget(self.connect_button)
        buttons_layout.addWidget(self.disconnect_button)
        buttons_layout.addStretch()
        buttons_layout.addWidget(self.save_button)

        root_layout.addLayout(buttons_layout)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(1000)
        self.log_output.setMinimumHeight(90)

        root_layout.addWidget(QLabel("Log:"))
        root_layout.addWidget(self.log_output, stretch=1)

        self.setCentralWidget(root)

    def _load_settings_into_ui(self) -> None:
        display_name = config_get(self.config, "display_name", "CMDR Test")

        if self.verified_commander_name:
            display_name = self.verified_commander_name

        self.display_name_edit.setText(str(display_name))

        ptt_binding = str(config_get(self.config, "ptt_key", "keyboard:f12"))
        self.ptt_key_edit.setText(ptt_binding)
        self.ptt_key_edit.setToolTip(describe_ptt_binding(ptt_binding))

        self.mute_checkbox.setChecked(bool(config_get(self.config, "muted", False)))
        self.deafen_checkbox.setChecked(bool(config_get(self.config, "deafened", False)))

        self.account_status_label.setText(self.account_status_text())

    def _try_apply_commander_name_from_journal(self) -> None:
        if self.verified_commander_name:
            self.display_name_edit.setText(self.verified_commander_name)
            return

        commander_name = read_last_commander_name(self.journal_dir)

        if not commander_name:
            return

        current_display = self.display_name_edit.text().strip()
        if not current_display or current_display == "CMDR Test":
            self.display_name_edit.setText(commander_name)

    def _populate_audio_devices(self) -> None:
        saved_input = self.config.get("input_device")
        saved_output = self.config.get("output_device")

        current_input = self.input_device_combo.currentData()
        current_output = self.output_device_combo.currentData()

        if current_input is not None:
            saved_input = current_input

        if current_output is not None:
            saved_output = current_output

        self.input_device_combo.clear()
        self.output_device_combo.clear()

        self.input_device_combo.addItem("Default input device", None)
        self.output_device_combo.addItem("Default output device", None)

        self.input_devices, self.output_devices = get_audio_devices()

        for device_id, label in self.input_devices:
            self.input_device_combo.addItem(label, device_id)

        for device_id, label in self.output_devices:
            self.output_device_combo.addItem(label, device_id)

        self._set_combo_by_data(self.input_device_combo, saved_input)
        self._set_combo_by_data(self.output_device_combo, saved_output)

    def _set_combo_by_data(self, combo: QComboBox, data: Any) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i)
                return

    def _open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        values = dialog.values()

        self.client_id = values["client_id"] or f"vf-{uuid.uuid4()}"
        self.backend_url = values["backend_url"]
        self.journal_dir = Path(values["journal_dir"])
        self.start_minimized = bool(values["start_minimized"])
        self.auto_connect = bool(values["auto_connect"])
        self.minimize_to_tray = bool(values["minimize_to_tray"])

        self.client_id_label.setText(self.client_id)

        self._try_apply_commander_name_from_journal()
        self._save_settings_from_ui()

    def _open_account_link_dialog(self) -> None:
        dialog = AccountLinkDialog(self)
        dialog.exec()

    def _settings_from_ui(self) -> ClientSettings:
        client_id = self.client_id or f"vf-{uuid.uuid4()}"
        display_name = self.display_name_edit.text().strip() or "CMDR Test"
        ptt_key = self.ptt_key_edit.text().strip() or "keyboard:f12"

        return ClientSettings(
            backend_url=self.backend_url or DEFAULT_BACKEND_URL,
            client_id=client_id,
            display_name=display_name,
            session_token=self.session_token,
            verified=self.verified,
            verified_commander_name=self.verified_commander_name,
            verified_frontier_id=self.verified_frontier_id,
            ptt_key=ptt_key,
            input_device=self.input_device_combo.currentData(),
            output_device=self.output_device_combo.currentData(),
            muted=self.mute_checkbox.isChecked(),
            deafened=self.deafen_checkbox.isChecked(),
            start_minimized=self.start_minimized,
            auto_connect=self.auto_connect,
            minimize_to_tray=self.minimize_to_tray,
            journal_dir=self.journal_dir,
        )

    def _save_settings_from_ui(self) -> None:
        settings = self._settings_from_ui()

        self.client_id = settings.client_id
        self.backend_url = settings.backend_url
        self.journal_dir = settings.journal_dir or default_journal_dir()
        self.start_minimized = settings.start_minimized
        self.auto_connect = settings.auto_connect
        self.minimize_to_tray = settings.minimize_to_tray

        save_config(
            {
                "client_id": settings.client_id,
                "display_name": settings.display_name,
                "backend_url": settings.backend_url,
                "session_token": settings.session_token,
                "verified": settings.verified,
                "verified_commander_name": settings.verified_commander_name,
                "verified_frontier_id": settings.verified_frontier_id,
                "ptt_key": settings.ptt_key,
                "input_device": settings.input_device,
                "output_device": settings.output_device,
                "muted": settings.muted,
                "deafened": settings.deafened,
                "start_minimized": settings.start_minimized,
                "auto_connect": settings.auto_connect,
                "minimize_to_tray": settings.minimize_to_tray,
                "journal_dir": str(self.journal_dir),
            },
            self.config_path,
        )

        self.client_id_label.setText(settings.client_id)
        self.account_status_label.setText(self.account_status_text())
        self.log(f"Settings saved: {self.config_path}")

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.display_name_edit.setEnabled(enabled)
        self.input_device_combo.setEnabled(enabled)
        self.output_device_combo.setEnabled(enabled)
        self.refresh_devices_button.setEnabled(enabled)
        self.ptt_key_edit.setEnabled(enabled)
        self.set_ptt_button.setEnabled(enabled)
        self.save_button.setEnabled(enabled)
        self.settings_action.setEnabled(enabled)
        self.link_account_action.setEnabled(enabled)
        self.link_account_button.setEnabled(enabled)

    def _connect(self) -> None:
        if self.worker_thread is not None:
            return

        if not self.session_token:
            self._open_account_link_dialog()

        if not self.session_token:
            self.status_label.setText("Account not linked")
            self.log("Account not linked. Link your Frontier account before starting voice.")
            return

        settings = self._settings_from_ui()
        self._save_settings_from_ui()

        self.worker_thread = QThread()
        self.worker = VoiceWorker(settings=settings)
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.start)

        self.worker.log.connect(self.log)
        self.worker.error.connect(self._on_error)
        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.skipped_connection.connect(self._on_skipped_connection)
        self.worker.system_changed.connect(self._on_system_changed)
        self.worker.commander_detected.connect(self._on_commander_detected)
        self.worker.verified_identity.connect(self._on_verified_identity)
        self.worker.participant_joined.connect(self._on_participant_joined)
        self.worker.participant_left.connect(self._on_participant_left)
        self.worker.participants_snapshot.connect(self._on_participants_snapshot)
        self.worker.stats.connect(self._on_stats)

        self.worker.disconnected.connect(self.worker_thread.quit)
        self.worker.disconnected.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.finished.connect(self._clear_worker_refs)

        self.status_label.setText("Watching journal...")
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.connect_action.setEnabled(False)
        self.disconnect_action.setEnabled(True)
        self._set_controls_enabled(False)

        self.worker_thread.start()

    def _disconnect(self) -> None:
        if self.worker:
            self.status_label.setText("Stopping...")
            self.worker.request_stop()

        self.disconnect_button.setEnabled(False)

    def _capture_ptt_binding(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Set Push-to-Talk")
        dialog.resize(420, 140)

        layout = QVBoxLayout(dialog)

        label = QLabel(
            "Press a keyboard, mouse, or joystick button now.\n\n"
            "For mouse buttons, click outside this dialog.\n"
            "Clicks inside this dialog are ignored."
        )
        label.setWordWrap(True)
        layout.addWidget(label)

        buttons_row = QHBoxLayout()
        buttons_row.addStretch()

        cancel_button = QPushButton("Cancel")
        buttons_row.addWidget(cancel_button)

        layout.addLayout(buttons_row)

        cancel_event = threading.Event()
        result: dict[str, str | None] = {"binding": None}

        notifier = PttCaptureNotifier(dialog)

        def on_finished(binding: object) -> None:
            result["binding"] = binding if isinstance(binding, str) else None

            if dialog.isVisible():
                dialog.accept()

        notifier.finished.connect(on_finished)

        def ignore_mouse_click(x: int, y: int) -> bool:
            return dialog.frameGeometry().contains(QPoint(x, y))

        def capture_worker() -> None:
            binding = capture_ptt_binding(
                timeout_seconds=15.0,
                cancel_event=cancel_event,
                ignore_mouse_click=ignore_mouse_click,
            )
            notifier.finished.emit(binding)

        def cancel_capture() -> None:
            cancel_event.set()
            dialog.reject()

        cancel_button.clicked.connect(cancel_capture)

        thread = threading.Thread(target=capture_worker, daemon=True)
        thread.start()

        dialog.exec()

        cancel_event.set()
        thread.join(timeout=1.0)

        binding = result["binding"]

        if not binding:
            self.log("PTT binding capture cancelled or timed out.")
            return

        self.ptt_key_edit.setText(binding)
        self.ptt_key_edit.setToolTip(describe_ptt_binding(binding))
        self.log(f"PTT set to: {describe_ptt_binding(binding)}")

    @Slot()
    def _clear_worker_refs(self) -> None:
        self.worker_thread = None
        self.worker = None

    @Slot(str, str)
    def _on_connected(self, system_name: str, system_address: str) -> None:
        self.status_label.setText("Connected")
        self.current_system_label.setText(f"{system_name} ({system_address})")

        if self.worker and self.worker.voice and self.worker.voice.current_state:
            state = self.worker.voice.current_state
            self.current_room_label.setText(state.room_name)
            self.current_game_mode_label.setText(state.game_mode or "Unknown")

        self.log(f"Connected: {system_name} ({system_address})")
        self.tray_icon.setToolTip(f"Voidfarers Voice Client\nConnected: {system_name}")

    @Slot()
    def _on_disconnected(self) -> None:
        self.status_label.setText("Disconnected")
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self.connect_action.setEnabled(True)
        self.disconnect_action.setEnabled(False)
        self._set_controls_enabled(True)
        self.ptt_status_label.setText("--")
        self.mic_meter.setValue(0)
        self.output_buffer_label.setText("0 ms")
        self.participants_list.clear()
        self.current_room_label.setText("None")
        self.current_game_mode_label.setText("Unknown")
        self.tray_icon.setToolTip("Voidfarers Voice Client\nDisconnected")

        if self._really_quit:
            QTimer.singleShot(0, self._force_quit)

    @Slot(str)
    def _on_skipped_connection(self, reason: str) -> None:
        self.status_label.setText(reason)
        self.current_room_label.setText("Not connected")
        self.participants_list.clear()
        self.log(reason)

    @Slot(str, str)
    def _on_system_changed(self, system_name: str, system_address: str) -> None:
        self.current_system_label.setText(f"{system_name} ({system_address})")

    @Slot(str)
    def _on_commander_detected(self, commander_name: str) -> None:
        if self.verified_commander_name:
            return

        current_display = self.display_name_edit.text().strip()
        if not current_display or current_display == "CMDR Test":
            self.display_name_edit.setText(commander_name)

    @Slot(str)
    def _on_verified_identity(self, commander_name: str) -> None:
        self.verified = True
        self.verified_commander_name = commander_name
        if commander_name:
            self.display_name_edit.setText(commander_name)
        self.account_status_label.setText(self.account_status_text())
        self.log(self.account_status_text())
        self._save_settings_from_ui()

    @Slot(str, str)
    def _on_participant_joined(self, identity: str, name: str) -> None:
        self.log(f"Participant joined: {identity} / {name}")

        for i in range(self.participants_list.count()):
            if self.participants_list.item(i).text() == "No other participants":
                self.participants_list.takeItem(i)
                break

        label = f"{name} ({identity})" if name else identity

        existing = [
            self.participants_list.item(i).text()
            for i in range(self.participants_list.count())
        ]

        if label not in existing:
            self.participants_list.addItem(label)

    @Slot(str, str)
    def _on_participant_left(self, identity: str, name: str) -> None:
        self.log(f"Participant left: {identity} / {name}")

        for i in range(self.participants_list.count()):
            text = self.participants_list.item(i).text()
            if identity in text:
                self.participants_list.takeItem(i)
                break

        if self.participants_list.count() == 0:
            self.participants_list.addItem("No other participants")

    @Slot(bool, float, int, int)
    def _on_stats(
        self,
        ptt_active: bool,
        mic_level: float,
        output_ms: int,
        dropped: int,
    ) -> None:
        self.ptt_status_label.setText("TX" if ptt_active else "--")
        self.mic_meter.setValue(int(max(0.0, min(1.0, mic_level)) * 100))
        self.output_buffer_label.setText(f"{output_ms} ms")
        self.dropped_label.setText(str(dropped))

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self.log(f"ERROR: {message}")
        QMessageBox.warning(self, "Voidfarers Error", message)

    @Slot(bool)
    def _on_mute_toggled(self, checked: bool) -> None:
        if self.worker:
            self.worker.set_muted(checked)

    @Slot(bool)
    def _on_deafen_toggled(self, checked: bool) -> None:
        if self.worker:
            self.worker.set_deafened(checked)

    @Slot(list)
    def _on_participants_snapshot(self, participants: list) -> None:
        self.participants_list.clear()

        if not participants:
            self.participants_list.addItem("No other participants")
            return

        for identity, name in participants:
            label = f"{name} ({identity})" if name else identity
            self.participants_list.addItem(label)

    def log(self, message: str) -> None:
        self.log_output.appendPlainText(message)

    def closeEvent(self, event) -> None:
        if self.minimize_to_tray and not self._really_quit:
            event.ignore()
            self.hide()

            if hasattr(self, "tray_icon"):
                self.tray_icon.showMessage(
                    "Voidfarers still running",
                    "The client is still running in the system tray.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
            return

        if self.worker and not self._really_quit:
            self.worker.request_stop()
            event.ignore()
            QTimer.singleShot(500, self.close)
            return

        event.accept()


def main() -> None:
    app = QApplication([])
    window = MainWindow()
    window.show()
    app.exec()