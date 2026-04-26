from __future__ import annotations

import asyncio
import contextlib
import threading
import uuid
from pathlib import Path
from typing import Any

import sounddevice as sd
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QRadioButton,
    QVBoxLayout,
    QWidget,
    QListWidget,
)

from .app_state import (
    DEFAULT_BACKEND_URL,
    DEFAULT_SYSTEM_ADDRESS,
    DEFAULT_SYSTEM_NAME,
    ClientSettings,
    SystemState,
)
from .audio import AudioEngine
from .config import default_config_path, load_config, save_config
from .journal import default_journal_dir, watch_system_changes
from .ptt import PushToTalk
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


class VoiceWorker(QObject):
    log = Signal(str)
    error = Signal(str)
    connected = Signal(str, str)
    disconnected = Signal()
    system_changed = Signal(str, str)
    participant_joined = Signal(str, str)
    participant_left = Signal(str, str)
    participants_snapshot = Signal(list)
    stats = Signal(bool, float, int, int)

    def __init__(self, settings: ClientSettings, use_journal: bool) -> None:
        super().__init__()
        self.settings = settings
        self.use_journal = use_journal

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
            audio=self.audio,
            on_log=self.log.emit,
            on_error=self.error.emit,
            on_system_changed=lambda state: self.system_changed.emit(
                state.system_name,
                state.system_address,
            ),
            on_participant_joined=self.participant_joined.emit,
            on_participant_left=self.participant_left.emit,
        )

        self.ptt.start()
        self.audio.start()

        self._publish_task = asyncio.create_task(self.voice.mic_publish_loop())
        self._stats_task = asyncio.create_task(self._stats_loop())

        try:
            if self.use_journal:
                await self._run_journal_mode()
            else:
                state = SystemState(
                    system_address=self.settings.system_address,
                    system_name=self.settings.system_name,
                )
                await self.voice.connect_to_system(state)
                self.connected.emit(state.system_name, state.system_address)
                self.emit_participants_snapshot()

                while self.voice.running and not self._stop_requested:
                    await asyncio.sleep(0.2)

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

        self.setWindowTitle("Voidfarers Voice Client")
        self.resize(850, 650)

        self.config_path = default_config_path()
        self.config = load_config(self.config_path)

        self.worker_thread: QThread | None = None
        self.worker: VoiceWorker | None = None

        self.input_devices: list[tuple[int, str]] = []
        self.output_devices: list[tuple[int, str]] = []

        self._build_ui()
        self._load_settings_into_ui()
        self._populate_audio_devices()

        self.stats_timer = QTimer(self)
        self.stats_timer.setInterval(1000)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)

        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("font-weight: bold;")
        root_layout.addWidget(self.status_label)

        identity_box = QGroupBox("Identity")
        identity_layout = QFormLayout(identity_box)

        self.display_name_edit = QLineEdit()
        self.client_id_edit = QLineEdit()
        self.client_id_edit.setPlaceholderText("Generated automatically if empty")
        self.backend_url_edit = QLineEdit()

        identity_layout.addRow("Display name:", self.display_name_edit)
        identity_layout.addRow("Client ID:", self.client_id_edit)
        identity_layout.addRow("Backend URL:", self.backend_url_edit)

        root_layout.addWidget(identity_box)

        mode_box = QGroupBox("Mode")
        mode_layout = QVBoxLayout(mode_box)

        self.static_radio = QRadioButton("Static test room")
        self.journal_radio = QRadioButton("Elite Dangerous journal mode")
        self.static_radio.setChecked(True)

        mode_layout.addWidget(self.static_radio)
        mode_layout.addWidget(self.journal_radio)

        static_layout = QFormLayout()
        self.system_name_edit = QLineEdit()
        self.system_address_edit = QLineEdit()

        static_layout.addRow("System name:", self.system_name_edit)
        static_layout.addRow("System address:", self.system_address_edit)
        mode_layout.addLayout(static_layout)

        journal_layout = QHBoxLayout()
        self.journal_dir_edit = QLineEdit()
        self.browse_journal_button = QPushButton("Browse...")
        self.browse_journal_button.clicked.connect(self._browse_journal_dir)

        journal_layout.addWidget(self.journal_dir_edit)
        journal_layout.addWidget(self.browse_journal_button)

        mode_layout.addWidget(QLabel("Journal folder:"))
        mode_layout.addLayout(journal_layout)

        root_layout.addWidget(mode_box)

        audio_box = QGroupBox("Audio")
        audio_layout = QFormLayout(audio_box)

        self.input_device_combo = QComboBox()
        self.output_device_combo = QComboBox()
        self.refresh_devices_button = QPushButton("Refresh devices")
        self.refresh_devices_button.clicked.connect(self._populate_audio_devices)

        self.ptt_key_edit = QLineEdit()
        self.ptt_key_edit.setPlaceholderText("Example: f12, f11, ctrl_r")

        self.mute_checkbox = QCheckBox("Mute microphone")
        self.deafen_checkbox = QCheckBox("Deafen output")

        audio_layout.addRow("Input device:", self.input_device_combo)
        audio_layout.addRow("Output device:", self.output_device_combo)
        audio_layout.addRow("", self.refresh_devices_button)
        audio_layout.addRow("PTT key:", self.ptt_key_edit)
        audio_layout.addRow("", self.mute_checkbox)
        audio_layout.addRow("", self.deafen_checkbox)

        self.mute_checkbox.toggled.connect(self._on_mute_toggled)
        self.deafen_checkbox.toggled.connect(self._on_deafen_toggled)

        root_layout.addWidget(audio_box)

        status_box = QGroupBox("Live Status")
        status_layout = QFormLayout(status_box)

        self.current_system_label = QLabel("None")
        self.ptt_status_label = QLabel("--")
        self.output_buffer_label = QLabel("0 ms")
        self.dropped_label = QLabel("0")

        self.mic_meter = QProgressBar()
        self.mic_meter.setRange(0, 100)
        self.mic_meter.setValue(0)
        self.mic_meter.setTextVisible(True)

        status_layout.addRow("Current system:", self.current_system_label)
        status_layout.addRow("PTT:", self.ptt_status_label)
        status_layout.addRow("Mic level:", self.mic_meter)
        status_layout.addRow("Output buffer:", self.output_buffer_label)
        status_layout.addRow("Dropped frames:", self.dropped_label)

        root_layout.addWidget(status_box)

        buttons_layout = QHBoxLayout()
        self.connect_button = QPushButton("Connect")
        self.disconnect_button = QPushButton("Disconnect")
        self.save_button = QPushButton("Save Settings")

        self.disconnect_button.setEnabled(False)

        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.save_button.clicked.connect(self._save_settings_from_ui)

        participants_box = QGroupBox("Participants")
        participants_layout = QVBoxLayout(participants_box)

        self.participants_list = QListWidget()
        participants_layout.addWidget(self.participants_list)

        root_layout.addWidget(participants_box)

        buttons_layout.addWidget(self.connect_button)
        buttons_layout.addWidget(self.disconnect_button)
        buttons_layout.addStretch()
        buttons_layout.addWidget(self.save_button)

        root_layout.addLayout(buttons_layout)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(1000)
        root_layout.addWidget(QLabel("Log:"))
        root_layout.addWidget(self.log_output, stretch=1)

        self.setCentralWidget(root)

    def _load_settings_into_ui(self) -> None:
        client_id = config_get(self.config, "client_id", f"vf-{uuid.uuid4()}")
        display_name = config_get(self.config, "display_name", "CMDR Test")

        self.display_name_edit.setText(str(display_name))
        self.client_id_edit.setText(str(client_id))

        self.system_name_edit.setText(str(config_get(self.config, "system_name", DEFAULT_SYSTEM_NAME)))
        self.system_address_edit.setText(str(config_get(self.config, "system_address", DEFAULT_SYSTEM_ADDRESS)))

        self.ptt_key_edit.setText(str(config_get(self.config, "ptt_key", "f12")))

        journal_dir = config_get(self.config, "journal_dir", str(default_journal_dir()))
        self.journal_dir_edit.setText(str(journal_dir))
        self.backend_url_edit.setText(str(config_get(self.config, "backend_url", DEFAULT_BACKEND_URL)))
        self.mute_checkbox.setChecked(bool(config_get(self.config, "muted", False)))
        self.deafen_checkbox.setChecked(bool(config_get(self.config, "deafened", False)))

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

    def _browse_journal_dir(self) -> None:
        start_dir = self.journal_dir_edit.text().strip() or str(default_journal_dir())
        chosen = QFileDialog.getExistingDirectory(self, "Select Elite Dangerous Journal Folder", start_dir)

        if chosen:
            self.journal_dir_edit.setText(chosen)

    def _settings_from_ui(self) -> ClientSettings:
        client_id = self.client_id_edit.text().strip() or f"vf-{uuid.uuid4()}"
        display_name = self.display_name_edit.text().strip() or "CMDR Test"
        ptt_key = self.ptt_key_edit.text().strip() or "f12"

        system_name = self.system_name_edit.text().strip() or DEFAULT_SYSTEM_NAME
        system_address = self.system_address_edit.text().strip() or DEFAULT_SYSTEM_ADDRESS

        journal_dir_text = self.journal_dir_edit.text().strip()
        journal_dir = Path(journal_dir_text) if journal_dir_text else default_journal_dir()
        muted=self.mute_checkbox.isChecked(),
        deafened=self.deafen_checkbox.isChecked(),

        return ClientSettings(
            backend_url=self.backend_url_edit.text().strip() or DEFAULT_BACKEND_URL,
            client_id=client_id,
            display_name=display_name,
            ptt_key=ptt_key,
            input_device=self.input_device_combo.currentData(),
            output_device=self.output_device_combo.currentData(),
            journal_dir=journal_dir,
            system_name=system_name,
            system_address=system_address,
            muted=self.mute_checkbox.isChecked(),
            deafened=self.deafen_checkbox.isChecked(),
        )

    def _save_settings_from_ui(self) -> None:
        settings = self._settings_from_ui()

        save_config(
            {
                "client_id": settings.client_id,
                "display_name": settings.display_name,
                "backend_url": settings.backend_url,
                "ptt_key": settings.ptt_key,
                "input_device": settings.input_device,
                "output_device": settings.output_device,
                "journal_dir": str(settings.journal_dir),
                "system_address": settings.system_address,
                "system_name": settings.system_name,
                "muted": settings.muted,
                "deafened": settings.deafened,
            },
            self.config_path,
        )

        self.client_id_edit.setText(settings.client_id)
        self.log(f"Settings saved: {self.config_path}")

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.display_name_edit.setEnabled(enabled)
        self.client_id_edit.setEnabled(enabled)
        self.backend_url_edit.setEnabled(enabled)
        self.static_radio.setEnabled(enabled)
        self.journal_radio.setEnabled(enabled)
        self.system_name_edit.setEnabled(enabled)
        self.system_address_edit.setEnabled(enabled)
        self.journal_dir_edit.setEnabled(enabled)
        self.browse_journal_button.setEnabled(enabled)
        self.input_device_combo.setEnabled(enabled)
        self.output_device_combo.setEnabled(enabled)
        self.refresh_devices_button.setEnabled(enabled)
        self.ptt_key_edit.setEnabled(enabled)
        self.save_button.setEnabled(enabled)

    def _connect(self) -> None:
        if self.worker_thread is not None:
            return

        settings = self._settings_from_ui()
        self._save_settings_from_ui()

        use_journal = self.journal_radio.isChecked()

        self.worker_thread = QThread()
        self.worker = VoiceWorker(settings=settings, use_journal=use_journal)
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.start)

        self.worker.log.connect(self.log)
        self.worker.error.connect(self._on_error)
        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.system_changed.connect(self._on_system_changed)
        self.worker.participant_joined.connect(self._on_participant_joined)
        self.worker.participant_left.connect(self._on_participant_left)
        self.worker.participants_snapshot.connect(self._on_participants_snapshot)
        self.worker.stats.connect(self._on_stats)

        self.worker.disconnected.connect(self.worker_thread.quit)
        self.worker.disconnected.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.finished.connect(self._clear_worker_refs)

        self.status_label.setText("Connecting...")
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self._set_controls_enabled(False)

        self.worker_thread.start()

    def _disconnect(self) -> None:
        if self.worker:
            self.status_label.setText("Disconnecting...")
            self.worker.request_stop()

        self.disconnect_button.setEnabled(False)

    @Slot()
    def _clear_worker_refs(self) -> None:
        self.worker_thread = None
        self.worker = None

    @Slot(str, str)
    def _on_connected(self, system_name: str, system_address: str) -> None:
        self.status_label.setText("Connected")
        self.current_system_label.setText(f"{system_name} ({system_address})")
        self.log(f"Connected: {system_name} ({system_address})")

    @Slot()
    def _on_disconnected(self) -> None:
        self.status_label.setText("Disconnected")
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self._set_controls_enabled(True)
        self.ptt_status_label.setText("--")
        self.mic_meter.setValue(0)
        self.output_buffer_label.setText("0 ms")
        self.participants_list.clear()

    @Slot(str, str)
    def _on_system_changed(self, system_name: str, system_address: str) -> None:
        self.current_system_label.setText(f"{system_name} ({system_address})")

    @Slot(str, str)
    def _on_participant_joined(self, identity: str, name: str) -> None:
        self.log(f"Participant joined: {identity} / {name}")

        # Remove placeholder if present.
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
    def _on_stats(self, ptt_active: bool, mic_level: float, output_ms: int, dropped: int) -> None:
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
        if self.worker:
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