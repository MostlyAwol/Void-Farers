from __future__ import annotations

import re
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


def safe_room_part(value: str) -> str:
    """
    Make a journal/GameMode/Group value safe enough for a LiveKit room suffix.
    Keeps it readable while avoiding spaces and odd punctuation.
    """
    value = value.strip()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^A-Za-z0-9_.-]", "", value)
    return value or "unknown"


@dataclass(frozen=True)
class SystemState:
    system_address: str
    system_name: str
    game_mode: str = ""
    group: str = ""
    commander_name: str = ""
    in_game: bool = True

    @property
    def is_solo(self) -> bool:
        return self.game_mode.lower() == "solo"

    @property
    def is_private_group(self) -> bool:
        return self.game_mode.lower() in {"group", "privategroup", "private_group"}

    @property
    def voice_allowed(self) -> bool:
        return self.in_game and not self.is_solo and bool(self.system_address)

    @property
    def room_suffix(self) -> str:
        if self.is_private_group and self.group:
            return safe_room_part(self.group)

        if self.game_mode:
            return safe_room_part(self.game_mode.lower())

        return "unknown"

    @property
    def room_name(self) -> str:
        return f"system-{self.system_address}-{self.room_suffix}"


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
    game_mode: str = "Open"
    group: str = ""