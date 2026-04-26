from __future__ import annotations

import asyncio
import queue
from typing import Callable

from livekit import rtc

from .app_state import NUM_CHANNELS, SAMPLE_RATE, SystemState
from .audio import AudioEngine, audioframe_to_bytes
from .backend import request_livekit_token


LogCallback = Callable[[str], None]
SystemCallback = Callable[[SystemState], None]
ParticipantCallback = Callable[[str, str], None]
ErrorCallback = Callable[[str], None]


class VoiceClient:
    def __init__(
        self,
        *,
        backend_url: str,
        client_id: str,
        display_name: str,
        audio: AudioEngine,
        on_log: LogCallback | None = None,
        on_system_changed: SystemCallback | None = None,
        on_participant_joined: ParticipantCallback | None = None,
        on_participant_left: ParticipantCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self.backend_url = backend_url
        self.client_id = client_id
        self.display_name = display_name
        self.audio = audio

        self.room: rtc.Room | None = None
        self.source: rtc.AudioSource | None = None
        self.current_state: SystemState | None = None
        self.running = True

        self.remote_tasks: set[asyncio.Task] = set()

        self.on_log = on_log or print
        self.on_system_changed = on_system_changed
        self.on_participant_joined = on_participant_joined
        self.on_participant_left = on_participant_left
        self.on_error = on_error

    def log(self, message: str) -> None:
        self.on_log(message)

    def error(self, message: str) -> None:
        if self.on_error:
            self.on_error(message)
        else:
            self.log(message)

    async def connect_to_system(self, state: SystemState) -> None:
        self.log(f"Requesting token for {state.system_name} / {state.system_address}...")

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

        self.log(f"Connected to room: {token_data['room']}")
        self.log(f"Current system: {state.system_name} ({state.system_address})")

        if self.on_system_changed:
            self.on_system_changed(state)

        if self.room.remote_participants:
            self.log("Participants already in room:")
            for participant in self.room.remote_participants.values():
                self.log(f"  {participant.identity} / {participant.name}")
        else:
            self.log("No other participants in room.")

        track = rtc.LocalAudioTrack.create_audio_track("mic", self.source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE

        publication = await self.room.local_participant.publish_track(track, options)
        self.log(f"Published microphone track: {publication.sid}")

    async def disconnect_room(self) -> None:
        for task in list(self.remote_tasks):
            task.cancel()

        self.remote_tasks.clear()

        if self.room:
            await self.room.disconnect()
            self.room = None

        self.source = None
        self.audio.clear_output_buffer()

    def _attach_room_handlers(self, room: rtc.Room) -> None:
        @room.on("connected")
        def on_connected() -> None:
            self.log("LiveKit connected.")

        @room.on("disconnected")
        def on_disconnected(reason=None) -> None:
            self.log(f"LiveKit disconnected. Reason: {reason}")

        @room.on("participant_connected")
        def on_participant_connected(participant: rtc.RemoteParticipant) -> None:
            self.log(f"Participant joined: {participant.identity} / {participant.name}")
            if self.on_participant_joined:
                self.on_participant_joined(participant.identity, participant.name)

        @room.on("participant_disconnected")
        def on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
            self.log(f"Participant left: {participant.identity} / {participant.name}")
            if self.on_participant_left:
                self.on_participant_left(participant.identity, participant.name)

        @room.on("track_subscribed")
        def on_track_subscribed(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return

            self.log(f"Subscribed to audio from: {participant.identity} / {participant.name}")
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
                self.audio.append_output_audio(audio_bytes)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.error(f"Remote audio receive error from {participant.identity}: {exc}")

    async def mic_publish_loop(self) -> None:
        self.log("Mic publish loop started.")

        while self.running:
            if not self.source:
                await asyncio.sleep(0.05)
                continue

            try:
                frame = self.audio.mic_queue.get(timeout=0.05)
            except queue.Empty:
                await asyncio.sleep(0)
                continue

            try:
                await self.source.capture_frame(frame)
            except Exception as exc:
                self.error(f"Error publishing mic frame: {exc}")
                await asyncio.sleep(0.05)