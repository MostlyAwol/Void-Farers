from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass
from typing import Callable

from pynput import keyboard, mouse

try:
    import pygame
except Exception:
    pygame = None


@dataclass(frozen=True)
class PttBinding:
    kind: str
    value: str
    joystick_index: int | None = None

    @classmethod
    def parse(cls, raw: str) -> "PttBinding":
        raw = (raw or "keyboard:f12").strip().lower()

        # Backward compatibility with old config values like "f12"
        if ":" not in raw:
            return cls(kind="keyboard", value=raw)

        parts = raw.split(":")

        if parts[0] == "keyboard" and len(parts) >= 2:
            return cls(kind="keyboard", value=parts[1])

        if parts[0] == "mouse" and len(parts) >= 2:
            return cls(kind="mouse", value=parts[1])

        if parts[0] == "joystick" and len(parts) >= 4 and parts[2] == "button":
            try:
                joystick_index = int(parts[1])
            except ValueError:
                joystick_index = 0

            return cls(
                kind="joystick",
                joystick_index=joystick_index,
                value=parts[3],
            )

        return cls(kind="keyboard", value="f12")

    def to_config(self) -> str:
        if self.kind == "keyboard":
            return f"keyboard:{self.value}"

        if self.kind == "mouse":
            return f"mouse:{self.value}"

        if self.kind == "joystick":
            joystick_index = self.joystick_index or 0
            return f"joystick:{joystick_index}:button:{self.value}"

        return "keyboard:f12"


def normalize_keyboard_key(key) -> str | None:
    if isinstance(key, keyboard.KeyCode):
        if key.char:
            return key.char.lower()
        return None

    if isinstance(key, keyboard.Key):
        name = key.name
        if name:
            return name.lower()

    return None


def normalize_mouse_button(button) -> str | None:
    if button == mouse.Button.left:
        return "left"

    if button == mouse.Button.right:
        return "right"

    if button == mouse.Button.middle:
        return "middle"

    # pynput names these x1/x2 on platforms that expose them.
    name = getattr(button, "name", None)
    if name:
        return name.lower()

    return None


def describe_ptt_binding(raw: str) -> str:
    binding = PttBinding.parse(raw)

    if binding.kind == "keyboard":
        return f"Keyboard: {binding.value.upper()}"

    if binding.kind == "mouse":
        return f"Mouse: {binding.value.upper()}"

    if binding.kind == "joystick":
        return f"Joystick {binding.joystick_index or 0}: Button {binding.value}"

    return raw


class PushToTalk:
    def __init__(self, binding: str = "keyboard:f12") -> None:
        self.binding = PttBinding.parse(binding)
        self.active = False
        self.running = False

        self.keyboard_listener: keyboard.Listener | None = None
        self.mouse_listener: mouse.Listener | None = None

        self._joystick_thread: threading.Thread | None = None
        self._joystick_stop = threading.Event()

    def start(self) -> None:
        self.running = True

        self.keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self.keyboard_listener.start()

        self.mouse_listener = mouse.Listener(
            on_click=self._on_mouse_click,
        )
        self.mouse_listener.start()

        if self.binding.kind == "joystick":
            self._start_joystick_polling()

    def stop(self) -> None:
        self.running = False
        self.active = False

        if self.keyboard_listener:
            self.keyboard_listener.stop()
            self.keyboard_listener = None

        if self.mouse_listener:
            self.mouse_listener.stop()
            self.mouse_listener = None

        self._joystick_stop.set()

        if self._joystick_thread:
            self._joystick_thread.join(timeout=1.0)
            self._joystick_thread = None

    def _on_key_press(self, key) -> None:
        if self.binding.kind != "keyboard":
            return

        normalized = normalize_keyboard_key(key)
        if normalized == self.binding.value:
            self.active = True

    def _on_key_release(self, key) -> None:
        if self.binding.kind != "keyboard":
            return

        normalized = normalize_keyboard_key(key)
        if normalized == self.binding.value:
            self.active = False

    def _on_mouse_click(self, x, y, button, pressed: bool) -> None:
        if self.binding.kind != "mouse":
            return

        normalized = normalize_mouse_button(button)
        if normalized == self.binding.value:
            self.active = pressed

    def _start_joystick_polling(self) -> None:
        self._joystick_stop.clear()
        self._joystick_thread = threading.Thread(
            target=self._joystick_poll_loop,
            daemon=True,
        )
        self._joystick_thread.start()

    def _joystick_poll_loop(self) -> None:
        if pygame is None:
            return

        with contextlib.suppress(Exception):
            pygame.init()
            pygame.joystick.init()

        joystick_index = self.binding.joystick_index or 0

        try:
            if pygame.joystick.get_count() <= joystick_index:
                return

            joystick = pygame.joystick.Joystick(joystick_index)
            joystick.init()

            button_index = int(self.binding.value)

            while not self._joystick_stop.is_set():
                pygame.event.pump()

                try:
                    self.active = bool(joystick.get_button(button_index))
                except Exception:
                    self.active = False

                time.sleep(0.02)

        except Exception:
            self.active = False


def capture_ptt_binding(
    timeout_seconds: float = 15.0,
    cancel_event: threading.Event | None = None,
    ignore_mouse_click: Callable[[int, int], bool] | None = None,
) -> str | None:
    """
    Blocking helper. Run this in a background thread from the GUI.

    Returns one of:
      keyboard:f12
      mouse:x1
      joystick:0:button:4

    Returns None on timeout or cancel.
    """

    result: dict[str, str | None] = {"binding": None}
    done = threading.Event()

    if cancel_event is None:
        cancel_event = threading.Event()

    def finish(binding: str) -> None:
        if result["binding"] is None and not cancel_event.is_set():
            result["binding"] = binding
            done.set()

    def on_key_press(key) -> bool | None:
        if cancel_event.is_set():
            return False

        normalized = normalize_keyboard_key(key)
        if normalized:
            finish(f"keyboard:{normalized}")
            return False

        return None

    def on_mouse_click(x, y, button, pressed: bool) -> bool | None:
        if cancel_event.is_set():
            return False

        if not pressed:
            return None

        if ignore_mouse_click and ignore_mouse_click(int(x), int(y)):
            return None

        normalized = normalize_mouse_button(button)
        if normalized:
            finish(f"mouse:{normalized}")
            return False

        return None

    keyboard_listener = keyboard.Listener(on_press=on_key_press)
    mouse_listener = mouse.Listener(on_click=on_mouse_click)

    keyboard_listener.start()
    mouse_listener.start()

    joystick_thread_stop = threading.Event()

    def joystick_capture_loop() -> None:
        if pygame is None:
            return

        with contextlib.suppress(Exception):
            pygame.init()
            pygame.joystick.init()

        try:
            joysticks = []

            for joystick_index in range(pygame.joystick.get_count()):
                joystick = pygame.joystick.Joystick(joystick_index)
                joystick.init()
                joysticks.append(joystick)

            previous_states: dict[tuple[int, int], bool] = {}

            while (
                not joystick_thread_stop.is_set()
                and not done.is_set()
                and not cancel_event.is_set()
            ):
                pygame.event.pump()

                for joystick_index, joystick in enumerate(joysticks):
                    for button_index in range(joystick.get_numbuttons()):
                        key = (joystick_index, button_index)
                        pressed = bool(joystick.get_button(button_index))
                        was_pressed = previous_states.get(key, False)
                        previous_states[key] = pressed

                        if pressed and not was_pressed:
                            finish(f"joystick:{joystick_index}:button:{button_index}")
                            return

                time.sleep(0.02)

        except Exception:
            return

    joystick_thread = threading.Thread(target=joystick_capture_loop, daemon=True)
    joystick_thread.start()

    end_time = time.monotonic() + timeout_seconds

    while time.monotonic() < end_time:
        if done.is_set() or cancel_event.is_set():
            break
        time.sleep(0.03)

    joystick_thread_stop.set()

    with contextlib.suppress(Exception):
        keyboard_listener.stop()

    with contextlib.suppress(Exception):
        mouse_listener.stop()

    joystick_thread.join(timeout=1.0)

    if cancel_event.is_set():
        return None

    return result["binding"]