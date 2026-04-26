from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterator

from .app_state import SystemState


JOURNAL_EVENTS_WITH_SYSTEM = {"Location", "FSDJump", "CarrierJump"}


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


def parse_system_from_event(event: dict) -> SystemState | None:
    event_name = event.get("event")

    if event_name not in JOURNAL_EVENTS_WITH_SYSTEM:
        return None

    system_address = event.get("SystemAddress")
    system_name = event.get("StarSystem")

    if system_address is None or not system_name:
        return None

    return SystemState(
        system_address=str(system_address),
        system_name=str(system_name),
    )


def read_last_system_state(journal_dir: Path, max_lines: int = 2000) -> SystemState | None:
    journal_file = latest_journal_file(journal_dir)
    if journal_file is None:
        return None

    try:
        with journal_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None

    for line in reversed(lines[-max_lines:]):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        state = parse_system_from_event(event)
        if state:
            return state

    return None


def watch_system_changes(
    journal_dir: Path,
    poll_seconds: float = 1.0,
) -> Iterator[SystemState]:
    current_file: Path | None = None
    current_pos = 0
    last_state: SystemState | None = None

    initial = read_last_system_state(journal_dir)
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

            state = parse_system_from_event(event)
            if state and state != last_state:
                last_state = state
                yield state

        time.sleep(poll_seconds)