"""Keyboard and mouse chord parsing shared by the GUI and CLI."""
from __future__ import annotations

import threading

MODIFIERS = ("ctrl", "alt", "shift", "win")
ALIASES = {
    "control": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "shift_l": "shift", "shift_r": "shift",
    "cmd": "win", "cmd_l": "win", "cmd_r": "win", "windows": "win",
    "return": "enter", "escape": "esc", "spacebar": "space",
    "leftclick": "mouse_left", "rightclick": "mouse_right",
    "middleclick": "mouse_middle", "plus": "vk_187",
}


def key_token(key):
    name = getattr(key, "name", None)
    if name:
        return ALIASES.get(name, name)
    vk = getattr(key, "vk", None)
    if vk is not None and (48 <= vk <= 57 or 65 <= vk <= 90):
        return chr(vk).lower()
    char = getattr(key, "char", None)
    if char and len(char) == 1 and ord(char) >= 32:
        return "vk_187" if char == "+" else char.lower()
    return f"vk_{vk}" if vk is not None else None


def key_identity(key):
    name = getattr(key, "name", None)
    return ("key", name if name is not None else getattr(key, "vk", None) or key)


def format_tokens(tokens):
    return "+".join([key for key in MODIFIERS if key in tokens]
                    + sorted(set(tokens) - set(MODIFIERS)))


def normalize(binding):
    from pynput import keyboard, mouse

    parts = [part.strip().lower().removeprefix("key.")
             for part in str(binding).split("+")]
    if not parts or any(not part for part in parts):
        raise ValueError("快捷键不能为空，多个键用 + 连接")
    tokens = set()
    for part in parts:
        token = ALIASES.get(part, part)
        valid = token in MODIFIERS or len(token) == 1 or hasattr(keyboard.Key, token)
        if token.startswith("mouse_"):
            valid = hasattr(mouse.Button, token[6:])
        if token.startswith("vk_"):
            valid = token[3:].isdigit() and 1 <= int(token[3:]) <= 255
        if not valid:
            raise ValueError(f"无法识别按键 {part}，可以点录入后直接按键")
        tokens.add(token)
    return format_tokens(tokens)


class ChordTracker:
    def __init__(self, bindings):
        self.bindings = {name: frozenset(normalize(value).split("+"))
                         for name, value in bindings.items()}
        self.held = {}
        self.active = set()

    def update(self, identity, token, pressed):
        if not token:
            return []
        if pressed:
            self.held[identity] = token
        else:
            self.held.pop(identity, None)
        current = frozenset(self.held.values())
        modifiers = current & set(MODIFIERS)
        matching = {name for name, chord in self.bindings.items()
                    if chord <= current and chord & set(MODIFIERS) == modifiers}
        if matching:
            longest = max(len(self.bindings[name]) for name in matching)
            matching = {name for name in matching if len(self.bindings[name]) == longest}
        fired = matching - self.active if pressed else set()
        self.active = matching
        return sorted(fired)


class InputListener:
    """One chord state for keyboard and mouse, with repeat suppression."""

    def __init__(self, bindings, callback):
        from pynput import keyboard, mouse

        self.tracker = ChordTracker(bindings)
        self.callback = callback
        self.lock = threading.Lock()
        self.keyboard = keyboard.Listener(on_press=self._press, on_release=self._release)
        self.mouse = mouse.Listener(on_click=self._click)

    def _update(self, identity, token, pressed):
        with self.lock:
            actions = self.tracker.update(identity, token, pressed)
        for action in actions:
            self.callback(action)

    def _press(self, key):
        self._update(key_identity(key), key_token(key), True)

    def _release(self, key):
        self._update(key_identity(key), key_token(key), False)

    def _click(self, x, y, button, pressed):
        self._update(("mouse", button), f"mouse_{button.name}", pressed)

    def start(self):
        self.keyboard.start()
        self.mouse.start()
        return self

    def stop(self):
        self.keyboard.stop()
        self.mouse.stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.stop()
