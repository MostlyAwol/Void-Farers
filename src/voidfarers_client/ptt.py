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


class JoystickPoller:
    """
    One persistent pygame joystick poller for the whole app session.

    Do not repeatedly init/quit pygame joystick devices for capture/live PTT.
    That causes stale joystick state on some Windows setups.
    """

    def __init__(self) -> None:
        self.running = False
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()

        self.lock = threading.Lock()
        self.button_states: dict[tuple[int, int], bool] = {}
        self.press_counter = 0
        self.last_press: tuple[int, str] | None = None

        self.joysticks = []
        self.last_device_refresh = 0.0

    def start(self) -> None:
        if pygame is None:
            return

        if self.running:
            return

        self.running = True
        self.stop_event.clear()

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def is_button_pressed(self, joystick_index: int, button_index: int) -> bool:
        self.start()

        with self.lock:
            return bool(self.button_states.get((joystick_index, button_index), False))

    def get_press_counter(self) -> int:
        with self.lock:
            return self.press_counter

    def get_press_after(self, counter: int) -> str | None:
        with self.lock:
            if self.last_press and self.last_press[0] > counter:
                return self.last_press[1]
        return None

    def _refresh_devices(self) -> None:
        if pygame is None:
            return

        now = time.monotonic()

        # Refresh occasionally so newly connected devices can appear.
        if self.joysticks and now - self.last_device_refresh < 2.0:
            return

        self.last_device_refresh = now

        with contextlib.suppress(Exception):
            pygame.init()
            pygame.joystick.init()

        joysticks = []

        with contextlib.suppress(Exception):
            count = pygame.joystick.get_count()

            for joystick_index in range(count):
                joystick = pygame.joystick.Joystick(joystick_index)
                joystick.init()
                joysticks.append(joystick)

        self.joysticks = joysticks

    def _run(self) -> None:
        if pygame is None:
            return

        with contextlib.suppress(Exception):
            pygame.init()
            pygame.joystick.init()

        previous_states: dict[tuple[int, int], bool] = {}

        while not self.stop_event.is_set():
            self._refresh_devices()

            with contextlib.suppress(Exception):
                pygame.event.pump()

            new_states: dict[tuple[int, int], bool] = {}

            for joystick_index, joystick in enumerate(self.joysticks):
                try:
                    button_count = joystick.get_numbuttons()
                except Exception:
                    continue

                for button_index in range(button_count):
                    key = (joystick_index, button_index)

                    try:
                        pressed = bool(joystick.get_button(button_index))
                    except Exception:
                        pressed = False

                    new_states[key] = pressed

                    was_pressed = previous_states.get(key, False)

                    if pressed and not was_pressed:
                        binding = f"joystick:{joystick_index}:button:{button_index}"

                        with self.lock:
                            self.press_counter += 1
                            self.last_press = (self.press_counter, binding)

            previous_states = new_states

            with self.lock:
                self.button_states = new_states

            time.sleep(0.02)


_JOYSTICK_POLLER = JoystickPoller()


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
        self.active = False

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
            _JOYSTICK_POLLER.start()
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

        self._joystick_stop.clear()

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
        joystick_index = self.binding.joystick_index or 0

        try:
            button_index = int(self.binding.value)
        except ValueError:
            return

        while not self._joystick_stop.is_set():
            self.active = _JOYSTICK_POLLER.is_button_pressed(
                joystick_index,
                button_index,
            )
            time.sleep(0.02)

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

    _JOYSTICK_POLLER.start()
    joystick_start_counter = _JOYSTICK_POLLER.get_press_counter()

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

    end_time = time.monotonic() + timeout_seconds

    while time.monotonic() < end_time:
        if done.is_set() or cancel_event.is_set():
            break

        joystick_binding = _JOYSTICK_POLLER.get_press_after(joystick_start_counter)

        if joystick_binding:
            finish(joystick_binding)
            break

        time.sleep(0.03)

    with contextlib.suppress(Exception):
        keyboard_listener.stop()

    with contextlib.suppress(Exception):
        mouse_listener.stop()

    if cancel_event.is_set():
        return None

    return result["binding"]