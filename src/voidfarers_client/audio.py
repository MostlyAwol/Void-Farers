from __future__ import annotations

import queue
import threading

import numpy as np
import sounddevice as sd
from livekit import rtc

from .app_state import (
    BLOCKSIZE,
    FRAME_SAMPLES,
    MAX_OUTPUT_BUFFER_SECONDS,
    NUM_CHANNELS,
    SAMPLE_RATE,
)
from .ptt import PushToTalk


def audioframe_to_bytes(frame: rtc.AudioFrame) -> bytes:
    data = frame.data
    if hasattr(data, "tobytes"):
        return data.tobytes()
    return bytes(data)


def list_audio_devices() -> None:
    print(sd.query_devices())
    print("")
    print(f"Default input/output device: {sd.default.device}")


class AudioEngine:
    def __init__(
        self,
        *,
        ptt: PushToTalk,
        input_device: int | None = None,
        output_device: int | None = None,
    ) -> None:
        self.ptt = ptt
        self.input_device = input_device
        self.output_device = output_device

        self.running = False

        self.muted = False
        self.deafened = False

        self.mic_queue: queue.Queue[rtc.AudioFrame] = queue.Queue(maxsize=60)
        self.output_buffer = bytearray()
        self.output_lock = threading.Lock()

        self.input_stream: sd.InputStream | None = None
        self.output_stream: sd.OutputStream | None = None

        self.last_mic_level = 0.0
        self.last_input_status = ""
        self.last_output_status = ""
        self.frames_dropped = 0

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        if muted:
            self.last_mic_level = 0.0

    def set_deafened(self, deafened: bool) -> None:
        self.deafened = deafened
        if deafened:
            self.clear_output_buffer()

    def start(self) -> None:
        self.running = True

        #print("Starting audio devices...")

        try:
            self.input_stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCKSIZE,
                channels=NUM_CHANNELS,
                dtype="int16",
                device=self.input_device,
                callback=self._input_callback,
            )
            self.input_stream.start()
        except Exception as exc:
            self.running = False
            raise RuntimeError(
                f"Could not start input audio device {self.input_device!r}. "
                f"Open Settings > System > Sound, verify the microphone exists, "
                f"or select a different input device. Details: {exc}"
            ) from exc

        try:
            self.output_stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCKSIZE,
                channels=NUM_CHANNELS,
                dtype="int16",
                device=self.output_device,
                callback=self._output_callback,
            )
            self.output_stream.start()
        except Exception as exc:
            if self.input_stream:
                self.input_stream.stop()
                self.input_stream.close()
                self.input_stream = None

            self.running = False
            raise RuntimeError(
                f"Could not start output audio device {self.output_device!r}. "
                f"Open Settings > System > Sound, verify the speaker/headset exists, "
                f"or select a different output device. Details: {exc}"
            ) from exc

        #print("Audio devices started.")

    def stop(self) -> None:
        self.running = False

        if self.input_stream:
            self.input_stream.stop()
            self.input_stream.close()
            self.input_stream = None

        if self.output_stream:
            self.output_stream.stop()
            self.output_stream.close()
            self.output_stream = None

    def clear_output_buffer(self) -> None:
        with self.output_lock:
            self.output_buffer.clear()

    def append_output_audio(self, audio_bytes: bytes) -> None:
        if self.deafened:
            return

        with self.output_lock:
            self.output_buffer.extend(audio_bytes)

            max_buffer_bytes = int(
                SAMPLE_RATE
                * NUM_CHANNELS
                * 2
                * MAX_OUTPUT_BUFFER_SECONDS
            )

            if len(self.output_buffer) > max_buffer_bytes:
                overflow = len(self.output_buffer) - max_buffer_bytes
                del self.output_buffer[:overflow]

    def output_buffer_ms(self) -> int:
        with self.output_lock:
            return int(
                len(self.output_buffer)
                / (SAMPLE_RATE * NUM_CHANNELS * 2)
                * 1000
            )

    def _input_callback(self, indata, frames, time_info, status) -> None:
        if status:
            self.last_input_status = str(status)

        if not self.running:
            return

        if self.ptt.active and not self.muted:
            samples = indata[:, 0].copy()
            float_samples = samples.astype(np.float32)
            rms = float(np.sqrt(np.mean(float_samples * float_samples)) / 32768.0)
            self.last_mic_level = min(1.0, rms)
        else:
            samples = np.zeros(frames, dtype=np.int16)
            self.last_mic_level = 0.0

        offset = 0
        while offset < len(samples):
            chunk = samples[offset:offset + FRAME_SAMPLES]
            offset += FRAME_SAMPLES

            if len(chunk) < FRAME_SAMPLES:
                padded = np.zeros(FRAME_SAMPLES, dtype=np.int16)
                padded[:len(chunk)] = chunk
                chunk = padded

            frame = rtc.AudioFrame(
                data=chunk.tobytes(),
                samples_per_channel=FRAME_SAMPLES,
                sample_rate=SAMPLE_RATE,
                num_channels=NUM_CHANNELS,
            )

            try:
                self.mic_queue.put_nowait(frame)
            except queue.Full:
                self.frames_dropped += 1

    def _output_callback(self, outdata, frames, time_info, status) -> None:
        if status:
            self.last_output_status = str(status)

        bytes_needed = frames * NUM_CHANNELS * 2

        if self.deafened:
            outdata[:, 0] = np.zeros(frames, dtype=np.int16)
            return

        with self.output_lock:
            available = len(self.output_buffer)

            if available >= bytes_needed:
                chunk = self.output_buffer[:bytes_needed]
                del self.output_buffer[:bytes_needed]
            else:
                chunk = self.output_buffer[:available]
                del self.output_buffer[:available]
                chunk += bytes(bytes_needed - available)

        outdata[:, 0] = np.frombuffer(chunk, dtype=np.int16, count=frames)