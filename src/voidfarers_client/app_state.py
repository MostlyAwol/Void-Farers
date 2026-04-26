from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DEFAULT_BACKEND_URL = "https://backend.voidfarers.space"
DEFAULT_SYSTEM_ADDRESS = "10477373803"
DEFAULT_SYSTEM_NAME = "Sol"

SAMPLE_RATE = 48000
NUM_CHANNELS = 1
FRAME_SAMPLES = 480
BLOCKSIZE = 480

MAX_OUTPUT_BUFFER_SECONDS = 0.5


@dataclass(frozen=True)
class SystemState:
    system_address: str
    system_name: str


@dataclass
class ClientSettings:
    backend_url: str = DEFAULT_BACKEND_URL
    client_id: str = ""
    display_name: str = "CMDR Test"

    ptt_key: str = "f12"

    input_device: int | None = None
    output_device: int | None = None

    muted: bool = False
    deafened: bool = False

    start_minimized: bool = False
    auto_connect: bool = False
    minimize_to_tray: bool = True

    journal_dir: Path | None = None

    system_address: str = DEFAULT_SYSTEM_ADDRESS
    system_name: str = DEFAULT_SYSTEM_NAME