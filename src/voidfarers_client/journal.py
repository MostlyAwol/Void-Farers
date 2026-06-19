from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .app_state import SystemState


JOURNAL_EVENTS_WITH_SYSTEM = {"Location", "FSDJump", "CarrierJump"}

StatusCallback = Callable[[str], None]


@dataclass
class JournalContext:
    path: Path
    commander_name: str = ""
    system_address: str = ""
    system_name: str = ""
    game_mode: str = ""
    group: str = ""
    in_game: bool = False

    def to_state(self) -> SystemState | None:
        if not self.system_address or not self.system_name:
            return None

        return SystemState(
            system_address=self.system_address,
            system_name=self.system_name,
            game_mode=self.game_mode,
            group=self.group,
            commander_name=self.commander_name,
            in_game=self.in_game,
        )


def normalize_commander_name(value: str | None) -> str:
    if not value:
        return ""

    value = value.strip()

    # Treat "CMDR Bob" and "Bob" as the same.
    value = re.sub(r"^cmdr\s+", "", value, flags=re.IGNORECASE)

    value = re.sub(r"\s+", " ", value)
    return value.casefold()


def commander_names_match(a: str | None, b: str | None) -> bool:
    return bool(normalize_commander_name(a)) and normalize_commander_name(a) == normalize_commander_name(b)


def default_journal_dir() -> Path:
    return (
        Path.home()
        / "Saved Games"
        / "Frontier Developments"
        / "Elite Dangerous"
    )


def journal_files(journal_dir: Path, max_files: int = 30) -> list[Path]:
    if not journal_dir.exists():
        return []

    files = list(journal_dir.glob("Journal.*.log"))

    return sorted(
        files,
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:max_files]


def latest_journal_file(journal_dir: Path) -> Path | None:
    files = journal_files(journal_dir, max_files=1)
    return files[0] if files else None


def _read_recent_lines(path: Path, max_lines: int = 4000) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    return lines[-max_lines:]


def _apply_event_to_context(ctx: JournalContext, event: dict) -> None:
    event_name = event.get("event")

    if event_name == "Commander":
        # Example:
        # {"event":"Commander","FID":"F123456","Name":"MostlyAwol"}
        commander = event.get("Name")
        if commander:
            ctx.commander_name = str(commander)

    elif event_name == "LoadGame":
        # Example:
        # {"event":"LoadGame","Commander":"MostlyAwol","GameMode":"Open"}
        ctx.in_game = True

        commander = event.get("Commander")
        if commander:
            ctx.commander_name = str(commander)

        game_mode = event.get("GameMode")
        if game_mode:
            ctx.game_mode = str(game_mode)

        group = event.get("Group")
        if group:
            ctx.group = str(group)

    elif event_name in JOURNAL_EVENTS_WITH_SYSTEM:
        ctx.in_game = True

        system_address = event.get("SystemAddress")
        system_name = event.get("StarSystem")

        if system_address is not None:
            ctx.system_address = str(system_address)

        if system_name:
            ctx.system_name = str(system_name)

        # Usually GameMode is on LoadGame, but keep these just in case Frontier
        # includes them on other events or future journal changes.
        game_mode = event.get("GameMode")
        if game_mode:
            ctx.game_mode = str(game_mode)

        group = event.get("Group")
        if group:
            ctx.group = str(group)

    elif event_name == "Shutdown":
        ctx.in_game = False


def read_journal_context(path: Path, max_lines: int = 4000) -> JournalContext:
    ctx = JournalContext(path=path)

    for line in _read_recent_lines(path, max_lines=max_lines):
        if not line.strip():
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        _apply_event_to_context(ctx, event)

    return ctx


def read_last_commander_name(journal_dir: Path, max_lines: int = 4000) -> str | None:
    for path in journal_files(journal_dir, max_files=30):
        ctx = read_journal_context(path, max_lines=max_lines)

        if ctx.commander_name:
            return ctx.commander_name

    return None


def find_matching_journal_context(
    journal_dir: Path,
    expected_commander_name: str | None,
    *,
    max_files: int = 30,
) -> JournalContext | None:
    files = journal_files(journal_dir, max_files=max_files)

    if not files:
        return None

    if not expected_commander_name:
        return read_journal_context(files[0])

    inactive_match: JournalContext | None = None

    for path in files:
        ctx = read_journal_context(path)

        if not ctx.commander_name:
            continue

        if commander_names_match(ctx.commander_name, expected_commander_name):
            # Prefer the matching commander that appears to be actively in game.
            if ctx.in_game:
                return ctx

            if inactive_match is None:
                inactive_match = ctx

    return inactive_match


def read_last_system_state(
    journal_dir: Path,
    max_lines: int = 4000,
    expected_commander_name: str | None = None,
) -> SystemState | None:
    ctx = find_matching_journal_context(
        journal_dir,
        expected_commander_name,
        max_files=30,
    )

    if not ctx:
        return None

    if expected_commander_name and ctx.commander_name:
        if not commander_names_match(ctx.commander_name, expected_commander_name):
            return None

    return ctx.to_state()


def watch_system_changes(
    journal_dir: Path,
    poll_seconds: float = 1.0,
    expected_commander_name: str | None = None,
    on_status: StatusCallback | None = None,
) -> Iterator[SystemState]:
    current_file: Path | None = None
    current_pos = 0
    current_ctx: JournalContext | None = None
    last_state: SystemState | None = None
    last_status = ""

    def emit_status(message: str) -> None:
        nonlocal last_status

        if not message or message == last_status:
            return

        last_status = message

        if on_status:
            on_status(message)

    while True:
        matching_ctx = find_matching_journal_context(
            journal_dir,
            expected_commander_name,
            max_files=30,
        )

        latest_file = latest_journal_file(journal_dir)
        latest_ctx = read_journal_context(latest_file) if latest_file else None

        if expected_commander_name and latest_ctx and latest_ctx.commander_name:
            if not commander_names_match(latest_ctx.commander_name, expected_commander_name):
                if not matching_ctx:
                    emit_status(
                        f"Verified as {expected_commander_name}, but latest journal is "
                        f"{latest_ctx.commander_name}. Waiting for {expected_commander_name} journal..."
                    )

        if not matching_ctx:
            if expected_commander_name:
                emit_status(
                    f"Waiting for Elite Dangerous journal for verified commander "
                    f"{expected_commander_name}..."
                )
            else:
                emit_status("Waiting for Elite Dangerous journal...")
            time.sleep(poll_seconds)
            continue

        if expected_commander_name and matching_ctx.commander_name:
            if not commander_names_match(matching_ctx.commander_name, expected_commander_name):
                emit_status(
                    f"Journal commander {matching_ctx.commander_name} does not match "
                    f"verified commander {expected_commander_name}."
                )
                time.sleep(poll_seconds)
                continue

        chosen_file = matching_ctx.path

        if chosen_file != current_file:
            current_file = chosen_file
            current_ctx = matching_ctx

            try:
                current_pos = os.path.getsize(chosen_file)
            except OSError:
                current_pos = 0

            state = current_ctx.to_state()

            if state and state != last_state:
                last_state = state
                yield state

        if not current_file or not current_ctx:
            time.sleep(poll_seconds)
            continue

        try:
            current_size = os.path.getsize(current_file)
        except OSError:
            current_file = None
            current_pos = 0
            current_ctx = None
            time.sleep(poll_seconds)
            continue

        if current_size < current_pos:
            current_pos = 0

        if current_size == current_pos:
            time.sleep(poll_seconds)
            continue

        try:
            with current_file.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(current_pos)
                new_lines = handle.readlines()
                current_pos = handle.tell()
        except OSError:
            current_file = None
            current_pos = 0
            current_ctx = None
            time.sleep(poll_seconds)
            continue

        for line in new_lines:
            if not line.strip():
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            _apply_event_to_context(current_ctx, event)

            if expected_commander_name and current_ctx.commander_name:
                if not commander_names_match(current_ctx.commander_name, expected_commander_name):
                    emit_status(
                        f"Journal commander {current_ctx.commander_name} does not match "
                        f"verified commander {expected_commander_name}. Voice not connected."
                    )
                    current_file = None
                    current_pos = 0
                    current_ctx = None
                    last_state = None
                    break

            state = current_ctx.to_state()

            if state and state != last_state:
                last_state = state
                yield state