from __future__ import annotations

import argparse
import asyncio
import contextlib
import threading
import uuid
from pathlib import Path
from typing import Any

from .app_state import (
    DEFAULT_BACKEND_URL,
    DEFAULT_SYSTEM_ADDRESS,
    DEFAULT_SYSTEM_NAME,
    ClientSettings,
    SystemState,
)
from .audio import AudioEngine, list_audio_devices
from .config import default_config_path, load_config, save_config
from .journal import default_journal_dir, watch_system_changes
from .ptt import PushToTalk
from .voice import VoiceClient


def config_get(config: dict[str, Any], key: str, fallback: Any) -> Any:
    value = config.get(key)
    return fallback if value is None else value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Voidfarers voice client")

    parser.add_argument("--backend-url", default=None)
    parser.add_argument("--client-id", default=None)
    parser.add_argument("--display-name", default=None)

    parser.add_argument("--system-address", default=None)
    parser.add_argument("--system-name", default=None)

    parser.add_argument("--ptt-key", default=None)

    parser.add_argument("--input-device", type=int, default=None)
    parser.add_argument("--output-device", type=int, default=None)

    parser.add_argument(
        "--journal",
        action="store_true",
        help="Use Elite Dangerous journal system switching",
    )
    parser.add_argument(
        "--journal-dir",
        default=None,
        help="Elite Dangerous journal folder",
    )

    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List audio devices and exit",
    )

    parser.add_argument(
        "--config",
        default=None,
        help="Optional config file path",
    )

    return parser.parse_args()


def settings_from_args_and_config(args: argparse.Namespace) -> tuple[ClientSettings, Path]:
    config_path = Path(args.config) if args.config else default_config_path()
    config = load_config(config_path)

    client_id = args.client_id or config_get(config, "client_id", f"vf-{uuid.uuid4()}")
    display_name = args.display_name or config_get(config, "display_name", "CMDR Test")
    backend_url = args.backend_url or config_get(config, "backend_url", DEFAULT_BACKEND_URL)

    ptt_key = args.ptt_key or config_get(config, "ptt_key", "f12")

    input_device = (
        args.input_device
        if args.input_device is not None
        else config.get("input_device")
    )

    output_device = (
        args.output_device
        if args.output_device is not None
        else config.get("output_device")
    )

    journal_dir = Path(
        args.journal_dir
        or config_get(config, "journal_dir", str(default_journal_dir()))
    )

    system_address = args.system_address or config_get(
        config,
        "system_address",
        DEFAULT_SYSTEM_ADDRESS,
    )

    system_name = args.system_name or config_get(
        config,
        "system_name",
        DEFAULT_SYSTEM_NAME,
    )

    settings = ClientSettings(
        backend_url=backend_url,
        client_id=client_id,
        display_name=display_name,
        ptt_key=ptt_key,
        input_device=input_device,
        output_device=output_device,
        journal_dir=journal_dir,
        system_address=str(system_address),
        system_name=str(system_name),
    )

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
        },
        config_path,
    )

    return settings, config_path


async def status_loop(
    *,
    voice: VoiceClient,
    audio: AudioEngine,
    ptt: PushToTalk,
) -> None:
    while voice.running:
        ptt_state = "TX" if ptt.active else "--"
        level_blocks = int(audio.last_mic_level * 20)
        meter = "#" * level_blocks + "-" * (20 - level_blocks)

        out_ms = audio.output_buffer_ms()
        system = voice.current_state.system_name if voice.current_state else "None"

        print(
            f"\r[{ptt_state}] Mic [{meter}] "
            f"OutBuf {out_ms:04d}ms "
            f"Dropped {audio.frames_dropped} "
            f"System {system}      ",
            end="",
            flush=True,
        )

        await asyncio.sleep(0.5)


async def run_static_room(
    *,
    voice: VoiceClient,
    audio: AudioEngine,
    ptt: PushToTalk,
    state: SystemState,
) -> None:
    publish_task: asyncio.Task | None = None
    status_task: asyncio.Task | None = None

    ptt.start()
    audio.start()

    try:
        await voice.connect_to_system(state)
        publish_task = asyncio.create_task(voice.mic_publish_loop())
        status_task = asyncio.create_task(status_loop(voice=voice, audio=audio, ptt=ptt))

        print("")
        print(f"Hold {ptt.key_name.upper()} to talk.")
        print("Press Ctrl+C to quit.")
        print("")

        while voice.running:
            await asyncio.sleep(1)

    finally:
        voice.running = False

        if publish_task:
            publish_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await publish_task

        if status_task:
            status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await status_task

        print("")
        ptt.stop()
        audio.stop()
        await voice.disconnect_room()


async def run_with_journal(
    *,
    voice: VoiceClient,
    audio: AudioEngine,
    ptt: PushToTalk,
    journal_dir: Path,
) -> None:
    publish_task: asyncio.Task | None = None
    status_task: asyncio.Task | None = None

    ptt.start()
    audio.start()

    publish_task = asyncio.create_task(voice.mic_publish_loop())
    status_task = asyncio.create_task(status_loop(voice=voice, audio=audio, ptt=ptt))

    loop = asyncio.get_running_loop()
    state_queue: asyncio.Queue[SystemState] = asyncio.Queue()

    def watcher_thread() -> None:
        try:
            for state in watch_system_changes(journal_dir):
                if not voice.running:
                    break
                loop.call_soon_threadsafe(state_queue.put_nowait, state)
        except Exception as exc:
            print(f"\nJournal watcher error: {exc}")

    thread = threading.Thread(target=watcher_thread, daemon=True)
    thread.start()

    print("")
    print(f"Watching journal folder: {journal_dir}")
    print(f"Hold {ptt.key_name.upper()} to talk.")
    print("Press Ctrl+C to quit.")
    print("")

    try:
        while voice.running:
            state = await state_queue.get()

            if state == voice.current_state:
                continue

            print("")
            await voice.connect_to_system(state)

    finally:
        voice.running = False

        if publish_task:
            publish_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await publish_task

        if status_task:
            status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await status_task

        print("")
        ptt.stop()
        audio.stop()
        await voice.disconnect_room()


async def async_main() -> None:
    args = parse_args()

    if args.list_devices:
        list_audio_devices()
        return

    settings, config_path = settings_from_args_and_config(args)

    print(f"Using config: {config_path}")
    print(f"Display name: {settings.display_name}")
    print(f"Client ID: {settings.client_id}")

    ptt = PushToTalk(settings.ptt_key)

    audio = AudioEngine(
        ptt=ptt,
        input_device=settings.input_device,
        output_device=settings.output_device,
    )

    voice = VoiceClient(
        backend_url=settings.backend_url,
        client_id=settings.client_id,
        display_name=settings.display_name,
        audio=audio,
    )

    try:
        if args.journal:
            await run_with_journal(
                voice=voice,
                audio=audio,
                ptt=ptt,
                journal_dir=settings.journal_dir or default_journal_dir(),
            )
        else:
            await run_static_room(
                voice=voice,
                audio=audio,
                ptt=ptt,
                state=SystemState(
                    system_address=settings.system_address,
                    system_name=settings.system_name,
                ),
            )
    except KeyboardInterrupt:
        print("\nExiting...")
        voice.running = False


def main() -> None:
    asyncio.run(async_main())

if __name__ == "__main__":
    main()