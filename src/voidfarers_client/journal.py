from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .app_state import SystemState


JOURNAL_EVENTS_WITH_SYSTEM = {"Location", "FSDJump", "CarrierJump"}
JOURNAL_EVENTS_WITH_COMMANDER = {"LoadGame", "Commander"}
JOURNAL_EVENTS_WITH_GAMEMODE = {"LoadGame", "Location"}


@dataclass
class JournalContext:
    commander_name: str = ""
    game_mode: str = ""
    group: str = ""
    in_game: bool = False
    system_address: str = ""
    system_name: str = ""

    def to_system_state(self) -> SystemState | None:
        if not self.system_address or not self.system_name:
            return None

        return SystemState(
            system_address=str(self.system_address),
            system_name=str(self.system_name),
            game_mode=str(self.game_mode or ""),
            group=str(self.group or ""),
            commander_name=str(self.commander_name or ""),
            in_game=bool(self.in_game),
        )


def default_journal_dir() -> Path:
    return (
        Path.home()
        / "Saved Games"
        / "Frontier Developments"
        / "Elite Dangerous"
    )


def latest_journal_file(journal_dir: Path) -> Path | None:
    if not journal_dir.exists():
        return None

    files = sorted(
        journal_dir.glob("Journal.*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def update_context_from_event(ctx: JournalContext, event: dict) -> SystemState | None:
    event_name = str(event.get("event", ""))

    if event_name == "LoadGame":
        ctx.in_game = True

    if event_name == "Shutdown":
        ctx.in_game = False
        return ctx.to_system_state()

    commander = event.get("Commander")
    if commander:
        ctx.commander_name = str(commander)

    game_mode = event.get("GameMode")
    if game_mode:
        ctx.game_mode = str(game_mode)

    group = event.get("Group")
    if group:
        ctx.group = str(group)
    elif game_mode and str(game_mode).lower() not in {"group", "privategroup", "private_group"}:
        ctx.group = ""

    if event_name in JOURNAL_EVENTS_WITH_SYSTEM:
        system_address = event.get("SystemAddress")
        system_name = event.get("StarSystem")

        if system_address is not None:
            ctx.system_address = str(system_address)

        if system_name:
            ctx.system_name = str(system_name)

        # If a journal has Location/FSDJump/CarrierJump, assume the player is active
        # unless a Shutdown event later says otherwise.
        ctx.in_game = True

        return ctx.to_system_state()

    return None


def read_last_journal_context(journal_dir: Path, max_lines: int = 4000) -> JournalContext:
    ctx = JournalContext()
    journal_file = latest_journal_file(journal_dir)

    if journal_file is None:
        return ctx

    try:
        with journal_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return ctx

    for line in lines[-max_lines:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        update_context_from_event(ctx, event)

    return ctx


def read_last_system_state(journal_dir: Path, max_lines: int = 4000) -> SystemState | None:
    ctx = read_last_journal_context(journal_dir, max_lines=max_lines)
    return ctx.to_system_state()


def read_last_commander_name(journal_dir: Path, max_lines: int = 4000) -> str:
    ctx = read_last_journal_context(journal_dir, max_lines=max_lines)
    return ctx.commander_name


def watch_system_changes(
    journal_dir: Path,
    poll_seconds: float = 1.0,
) -> Iterator[SystemState]:
    current_file: Path | None = None
    current_pos = 0
    last_state: SystemState | None = None
    ctx = JournalContext()

    initial_ctx = read_last_journal_context(journal_dir)
    ctx = initial_ctx

    initial = ctx.to_system_state()
    if initial:
        last_state = initial
        yield initial

    while True:
        newest = latest_journal_file(journal_dir)

        if newest is None:
            time.sleep(poll_seconds)
            continue

        if newest != current_file:
            current_file = newest
            ctx = read_last_journal_context(journal_dir)
            latest_state = ctx.to_system_state()

            if latest_state and latest_state != last_state:
                last_state = latest_state
                yield latest_state

            try:
                current_pos = os.path.getsize(newest)
            except OSError:
                current_pos = 0

        try:
            with newest.open("r", encoding="utf-8") as f:
                f.seek(current_pos)
                lines = f.readlines()
                current_pos = f.tell()
        except OSError:
            time.sleep(poll_seconds)
            continue

        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            state = update_context_from_event(ctx, event)
            if state and state != last_state:
                last_state = state
                yield state

        time.sleep(poll_seconds)