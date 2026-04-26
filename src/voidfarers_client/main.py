from __future__ import annotations

import argparse
import asyncio
import queue
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import sounddevice as sd
from livekit import rtc
from pynput import keyboard

from .backend import request_livekit_token
from .journal import SystemState, default_journal_dir, watch_system_changes


DEFAULT_BACKEND_URL = "https://backend.voidfarers.space"
DEFAULT_SYSTEM_ADDRESS = "10477373803"
DEFAULT_SYSTEM_NAME = "Sol"

SAMPLE_RATE = 48000
NUM_CHANNELS = 1
FRAME_SAMPLES = 480        # 10 ms at 48 kHz
BLOCKSIZE = 480            # sounddevice callback size


def audioframe_to_bytes(frame: rtc.AudioFrame) -> bytes:
    data = frame.data
    if hasattr(data, "tobytes"):
        return data.tobytes()
    return bytes(data)


class PushToTalk:
    def __init__(self, key_name: str = "f12") -> None:
        self.key_name = key_name.lower()
        self.active = False
        self._listener: keyboard.Listener | None = None

    def _matches(self, key) -> bool:
        wanted = self.key_name

        # Special keys like f12, ctrl_r, alt_r
        if isinstance(key, keyboard.Key):
            return key.name and key.name.lower() == wanted

        # Normal character keys
        if isinstance(key, keyboard.KeyCode) and key.char:
            return key.char.lower() == wanted

        return False

    def start(self) -> None:
        def on_press(key):
            if self._matches(key):
                self.active = True

        def on_release(key):
            if self._matches(key):
                self.active = False

        self._listener = keyboard.Listener(
            on_press=on_press,
            on_release=on_release,
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None


class VoiceClient:
    def __init__(
        self,
        *,
        backend_url: str,
        client_id: str,
        display_name: str,
        ptt_key: str,
        input_device: int | None = None,
        output_device: int | None = None,
    ) -> None:
        self.backend_url = backend_url
        self.client_id = client_id
        self.display_name = display_name
        self.input_device = input_device
        self.output_device = output_device

        self.room: rtc.Room | None = None
        self.source: rtc.AudioSource | None = None

        self.current_state: SystemState | None = None
        self.running = True

        self.ptt = PushToTalk(ptt_key)

        self.mic_queue: queue.Queue[rtc.AudioFrame] = queue.Queue(maxsize=200)
        self.output_buffer = bytearray()
        self.output_lock = threading.Lock()

        self.input_stream: sd.InputStream | None = None
        self.output_stream: sd.OutputStream | None = None

        self.remote_tasks: set[asyncio.Task] = set()

    def start_audio_devices(self) -> None:
        print("Starting audio devices...")

        self.input_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCKSIZE,
            channels=NUM_CHANNELS,
            dtype="int16",
            device=self.input_device,
            callback=self._input_callback,
        )
        self.input_stream.start()

        self.output_stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCKSIZE,
            channels=NUM_CHANNELS,
            dtype="int16",
            device=self.output_device,
            callback=self._output_callback,
        )
        self.output_stream.start()

        print("Audio devices started.")

    def stop_audio_devices(self) -> None:
        if self.input_stream:
            self.input_stream.stop()
            self.input_stream.close()
            self.input_stream = None

        if self.output_stream:
            self.output_stream.stop()
            self.output_stream.close()
            self.output_stream = None

    def _input_callback(self, indata, frames, time_info, status) -> None:
        if status:
            print(f"Input status: {status}")

        if not self.running:
            return

        # Use silence unless PTT is held.
        if self.ptt.active:
            samples = indata[:, 0].copy()
        else:
            samples = np.zeros(frames, dtype=np.int16)

        # We expect frames == FRAME_SAMPLES because blocksize is 480.
        # If Windows/audio driver gives a different size, split safely.
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
                # Drop frames rather than building latency.
                pass

    def _output_callback(self, outdata, frames, time_info, status) -> None:
        if status:
            print(f"Output status: {status}")

        bytes_needed = frames * NUM_CHANNELS * 2  # int16 mono

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

    async def connect_to_system(self, state: SystemState) -> None:
        print(f"Requesting token for {state.system_name} / {state.system_address}...")

        token_data = request_livekit_token(
            backend_url=self.backend_url,
            client_id=self.client_id,
            display_name=self.display_name,
            system_address=state.system_address,
            system_name=state.system_name,
        )

        if self.room:
            await self.disconnect_room()

        self.current_state = state
        self.source = rtc.AudioSource(SAMPLE_RATE, NUM_CHANNELS)
        self.room = rtc.Room()

        self._attach_room_handlers(self.room)

        await self.room.connect(token_data["url"], token_data["token"])

        print(f"Connected to room: {token_data['room']}")
        print(f"Current system: {state.system_name} ({state.system_address})")

        if self.room.remote_participants:
            print("Participants already in room:")
            for participant in self.room.remote_participants.values():
                print(f"  {participant.identity} / {participant.name}")
        else:
            print("No other participants in room.")

        track = rtc.LocalAudioTrack.create_audio_track("mic", self.source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE

        publication = await self.room.local_participant.publish_track(track, options)
        print(f"Published microphone track: {publication.sid}")

    async def disconnect_room(self) -> None:
        for task in list(self.remote_tasks):
            task.cancel()

        self.remote_tasks.clear()

        if self.room:
            await self.room.disconnect()
            self.room = None

        self.source = None

        with self.output_lock:
            self.output_buffer.clear()

    def _attach_room_handlers(self, room: rtc.Room) -> None:
        @room.on("connected")
        def on_connected() -> None:
            print("LiveKit connected.")

        @room.on("disconnected")
        def on_disconnected(reason=None) -> None:
            print(f"LiveKit disconnected. Reason: {reason}")

        @room.on("participant_connected")
        def on_participant_connected(participant: rtc.RemoteParticipant) -> None:
            print(f"Participant joined: {participant.identity} / {participant.name}")

        @room.on("participant_disconnected")
        def on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
            print(f"Participant left: {participant.identity} / {participant.name}")

        @room.on("track_subscribed")
        def on_track_subscribed(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return

            print(f"Subscribed to audio from: {participant.identity} / {participant.name}")
            task = asyncio.create_task(self._receive_remote_audio(track, participant))
            self.remote_tasks.add(task)
            task.add_done_callback(lambda t: self.remote_tasks.discard(t))

    async def _receive_remote_audio(
        self,
        track: rtc.Track,
        participant: rtc.RemoteParticipant,
    ) -> None:
        stream = rtc.AudioStream(
            track,
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )

        try:
            async for frame_event in stream:
                if not self.running:
                    break

                audio_bytes = audioframe_to_bytes(frame_event.frame)

                with self.output_lock:
                    self.output_buffer.extend(audio_bytes)

                    # Keep buffer bounded to avoid runaway latency.
                    max_buffer_bytes = SAMPLE_RATE * NUM_CHANNELS * 2 * 2  # 2 seconds
                    if len(self.output_buffer) > max_buffer_bytes:
                        del self.output_buffer[: len(self.output_buffer) - max_buffer_bytes]

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"Remote audio receive error from {participant.identity}: {exc}")

    async def mic_publish_loop(self) -> None:
        print("Mic publish loop started.")
        while self.running:
            if not self.source:
                await asyncio.sleep(0.05)
                continue

            try:
                frame = self.mic_queue.get(timeout=0.1)
            except queue.Empty:
                await asyncio.sleep(0)
                continue

            try:
                await self.source.capture_frame(frame)
            except Exception as exc:
                print(f"Error publishing mic frame: {exc}")
                await asyncio.sleep(0.05)

    async def run_static_room(self, state: SystemState) -> None:
        self.ptt.start()
        self.start_audio_devices()

        try:
            await self.connect_to_system(state)
            publish_task = asyncio.create_task(self.mic_publish_loop())

            print("")
            print(f"Hold {self.ptt.key_name.upper()} to talk.")
            print("Press Ctrl+C to quit.")
            print("")

            while self.running:
                await asyncio.sleep(1)

        finally:
            self.running = False
            publish_task.cancel()
            self.ptt.stop()
            self.stop_audio_devices()
            await self.disconnect_room()

    async def run_with_journal(self, journal_dir: Path) -> None:
        self.ptt.start()
        self.start_audio_devices()

        publish_task = asyncio.create_task(self.mic_publish_loop())
        loop = asyncio.get_running_loop()
        state_queue: asyncio.Queue[SystemState] = asyncio.Queue()

        def watcher_thread() -> None:
            try:
                for state in watch_system_changes(journal_dir):
                    if not self.running:
                        break
                    loop.call_soon_threadsafe(state_queue.put_nowait, state)
            except Exception as exc:
                print(f"Journal watcher error: {exc}")

        thread = threading.Thread(target=watcher_thread, daemon=True)
        thread.start()

        print("")
        print(f"Watching journal folder: {journal_dir}")
        print(f"Hold {self.ptt.key_name.upper()} to talk.")
        print("Press Ctrl+C to quit.")
        print("")

        try:
            while self.running:
                state = await state_queue.get()

                if state == self.current_state:
                    continue

                await self.connect_to_system(state)

        finally:
            self.running = False
            publish_task.cancel()
            self.ptt.stop()
            self.stop_audio_devices()
            await self.disconnect_room()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Voidfarers voice client")

    parser.add_argument("--backend-url", default=DEFAULT_BACKEND_URL)
    parser.add_argument("--client-id", default=None)
    parser.add_argument("--display-name", default="CMDR Test")

    parser.add_argument("--system-address", default=DEFAULT_SYSTEM_ADDRESS)
    parser.add_argument("--system-name", default=DEFAULT_SYSTEM_NAME)

    parser.add_argument("--ptt-key", default="f12")

    parser.add_argument("--input-device", type=int, default=None)
    parser.add_argument("--output-device", type=int, default=None)

    parser.add_argument(
        "--journal",
        action="store_true",
        help="Use Elite Dangerous journal system switching",
    )
    parser.add_argument(
        "--journal-dir",
        default=str(default_journal_dir()),
        help="Elite Dangerous journal folder",
    )

    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()

    client_id = args.client_id or f"vf-{uuid.uuid4()}"

    client = VoiceClient(
        backend_url=args.backend_url,
        client_id=client_id,
        display_name=args.display_name,
        ptt_key=args.ptt_key,
        input_device=args.input_device,
        output_device=args.output_device,
    )

    try:
        if args.journal:
            await client.run_with_journal(Path(args.journal_dir))
        else:
            await client.run_static_room(
                SystemState(
                    system_address=str(args.system_address),
                    system_name=str(args.system_name),
                )
            )
    except KeyboardInterrupt:
        print("Exiting...")
        client.running = False


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()