from __future__ import annotations

from pynput import keyboard


class PushToTalk:
    def __init__(self, key_name: str = "f12") -> None:
        self.key_name = key_name.lower()
        self.active = False
        self._listener: keyboard.Listener | None = None

    def _matches(self, key) -> bool:
        wanted = self.key_name

        if isinstance(key, keyboard.Key):
            return key.name and key.name.lower() == wanted

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