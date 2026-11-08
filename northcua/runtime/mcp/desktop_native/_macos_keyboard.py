"""Explicit Quartz keyboard event state for macOS native applications.

Posting separate modifier events through PyAutoGUI does not reliably carry those
modifiers to the following event (notably with Tk 9 and an active input method).
Set flags and character payload on every event instead of inheriting OS state.
Pointer operations continue to use the supplied PyAutoGUI module.
"""

from __future__ import annotations

import time


class MacOSKeyboardAdapter:
    def __init__(self, pyautogui, quartz):
        self._module = pyautogui
        self._quartz = quartz
        self._held = set()

    def __getattr__(self, name):
        return getattr(self._module, name)

    def _modifier(self, key):
        q = self._quartz
        return {
            "command": q.kCGEventFlagMaskCommand,
            "commandleft": q.kCGEventFlagMaskCommand,
            "commandright": q.kCGEventFlagMaskCommand,
            "ctrl": q.kCGEventFlagMaskControl,
            "ctrlleft": q.kCGEventFlagMaskControl,
            "ctrlright": q.kCGEventFlagMaskControl,
            "alt": q.kCGEventFlagMaskAlternate,
            "altleft": q.kCGEventFlagMaskAlternate,
            "altright": q.kCGEventFlagMaskAlternate,
            "option": q.kCGEventFlagMaskAlternate,
            "shift": q.kCGEventFlagMaskShift,
            "shiftleft": q.kCGEventFlagMaskShift,
            "shiftright": q.kCGEventFlagMaskShift,
        }.get(key, 0)

    def _event(self, key, down, flags):
        q = self._quartz
        code = self._module.platformModule.keyboardMapping.get(key)
        if code is None:
            raise ValueError(f"Unsupported macOS key: {key}")
        event = q.CGEventCreateKeyboardEvent(None, code, down)
        if event is None:
            raise RuntimeError("macOS could not create a keyboard event")
        q.CGEventSetFlags(event, flags)
        text = key if len(key) == 1 else {"enter": "\r", "return": "\r", "tab": "\t", "space": " "}.get(key, "")
        q.CGEventKeyboardSetUnicodeString(event, len(text.encode("utf-16-le")) // 2, text)
        q.CGEventPost(q.kCGHIDEventTap, event)
        time.sleep(0.01)

    def hotkey(self, *keys, interval=0):
        keys = [key.lower() for key in keys]
        flags = self._held_flags()
        pressed = []
        try:
            for key in keys:
                if key in self._held:
                    continue
                flags |= self._modifier(key)
                self._event(key, True, flags)
                pressed.append(key)
                time.sleep(interval)
        finally:
            for key in reversed(pressed):
                flags &= ~self._modifier(key)
                flags |= self._held_flags()
                self._event(key, False, flags)
                time.sleep(interval)
        time.sleep(self._module.PAUSE)

    def press(self, keys, presses=1, interval=0):
        keys = [keys] if isinstance(keys, str) else keys
        for _ in range(presses):
            for key in keys:
                self._event(key.lower(), True, self._held_flags() | self._modifier(key.lower()))
                self._event(key.lower(), False, self._held_flags())
            time.sleep(interval)
        time.sleep(self._module.PAUSE)

    def _held_flags(self):
        flags = 0
        for key in self._held:
            flags |= self._modifier(key)
        return flags

    def keyDown(self, key):
        key = key.lower()
        self._event(key, True, self._held_flags() | self._modifier(key))
        self._held.add(key)

    def keyUp(self, key):
        key = key.lower()
        self._held.discard(key)
        self._event(key, False, self._held_flags())
