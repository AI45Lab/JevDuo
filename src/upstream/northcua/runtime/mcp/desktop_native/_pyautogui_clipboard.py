"""Clipboard and platform text-entry helpers for pyautogui desktop actions."""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import time
from typing import Optional, Protocol

from northcua_runtime_shared.desktop_surface.models import TextTargetKind

from ._models import DesktopNativeError

logger = logging.getLogger(__name__)

_CLIPBOARD_READ_TIMEOUT_SECONDS = 2.0
_CLIPBOARD_WRITE_TIMEOUT_SECONDS = 2.0
_CLIPBOARD_OWNER_EXIT_TIMEOUT_SECONDS = 1.0


class _PyAutoGuiHotkeyModule(Protocol):
    """Minimal pyautogui interface used by clipboard paste shortcuts."""

    def hotkey(self, *keys: str) -> None:
        """Press a platform shortcut."""


class PyAutoGuiClipboardMixin:
    """Clipboard-backed text entry helpers."""

    def _import_pyautogui(self) -> _PyAutoGuiHotkeyModule:
        """Return the concrete pyautogui module supplied by the backend class."""

        raise NotImplementedError

    def _type_text_macos(self, text: str) -> bool:
        """Type text on macOS using System Events keystroke semantics."""

        command = [
            "osascript",
            "-e",
            "on run argv",
            "-e",
            'tell application "System Events" to keystroke (item 1 of argv)',
            "-e",
            "end run",
            text,
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            return True
        except Exception as exc:
            logger.warning(
                "DesktopNativeActionExecutor macOS keystroke typing failed; falling back to pyautogui.write: %s",
                exc,
            )
            return False

    def _type_text_via_clipboard(self, text: str, target_kind: TextTargetKind = TextTargetKind.AUTO) -> bool:
        """Paste text through the system clipboard."""

        target_kind = TextTargetKind.coerce(target_kind)
        previous_text: Optional[str] = None
        clipboard_updated = False
        owner_process: subprocess.Popen[str] | None = None
        owner_finished = False
        try:
            try:
                previous_text = self._get_clipboard_text()
            except Exception as exc:
                logger.info(
                    "DesktopNativeActionExecutor could not read previous clipboard text before paste; "
                    "continuing without restore.",
                    extra={
                        "target_kind": target_kind.value,
                        "error": str(exc),
                    },
                )
            owner_process = self._set_clipboard_text(text)
            clipboard_updated = True
            time.sleep(0.05)
            self._paste_clipboard_shortcut(target_kind=target_kind)
            time.sleep(0.05)
            self._finish_clipboard_owner(owner_process)
            owner_finished = True
            return True
        except Exception as exc:
            logger.warning(
                "DesktopNativeActionExecutor clipboard text paste failed; falling back to platform typing: %s",
                exc,
            )
            return False
        finally:
            if owner_process is not None and not owner_finished:
                self._finish_clipboard_owner(owner_process)
            if clipboard_updated and previous_text is not None and platform.system() != "Linux":
                try:
                    self._set_clipboard_text(previous_text)
                except Exception as exc:
                    logger.warning(
                        "DesktopNativeActionExecutor failed to restore clipboard contents after text paste: %s",
                        exc,
                    )

    def _get_clipboard_text(self) -> str:
        """Read the current system clipboard as plain text."""

        system_name = platform.system()
        if system_name == "Darwin":
            result = subprocess.run(
                ["pbpaste"],
                check=True,
                capture_output=True,
                text=True,
                timeout=_CLIPBOARD_READ_TIMEOUT_SECONDS,
            )
            return result.stdout

        if system_name == "Windows":
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-Clipboard -Raw",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=_CLIPBOARD_READ_TIMEOUT_SECONDS,
            )
            return result.stdout

        paste_command = self._linux_clipboard_command(read=True)
        if paste_command is None:
            raise DesktopNativeError("No Linux clipboard reader is available. Install wl-paste, xclip, or xsel.")

        result = subprocess.run(
            paste_command,
            check=True,
            capture_output=True,
            text=True,
            timeout=_CLIPBOARD_READ_TIMEOUT_SECONDS,
        )
        return result.stdout

    def _set_clipboard_text(self, text: str) -> subprocess.Popen[str] | None:
        """Write plain text into the system clipboard.

        On X11, clipboard writers such as xclip own the clipboard selection from
        a live process. Return that owner process so callers can paste first and
        then reap it instead of waiting before the paste can happen.
        """

        system_name = platform.system()
        if system_name == "Darwin":
            subprocess.run(
                ["pbcopy"],
                check=True,
                capture_output=True,
                text=True,
                input=text,
                timeout=_CLIPBOARD_WRITE_TIMEOUT_SECONDS,
            )
            return None

        if system_name == "Windows":
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Set-Clipboard -Value ([Console]::In.ReadToEnd())",
                ],
                check=True,
                capture_output=True,
                text=True,
                input=text,
                timeout=_CLIPBOARD_WRITE_TIMEOUT_SECONDS,
            )
            return None

        copy_command = self._linux_clipboard_command(read=False)
        if copy_command is None:
            raise DesktopNativeError("No Linux clipboard writer is available. Install wl-copy, xclip, or xsel.")

        process = subprocess.Popen(
            copy_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if process.stdin is None:
            process.terminate()
            raise DesktopNativeError("Linux clipboard writer did not expose stdin.")
        try:
            process.stdin.write(text)
            process.stdin.close()
        except Exception as exc:
            logger.warning(
                "DesktopNativeActionExecutor failed to write clipboard owner stdin; terminating owner process.",
                extra={"pid": process.pid, "error": str(exc)},
            )
            process.terminate()
            raise
        return process

    def _finish_clipboard_owner(self, process: subprocess.Popen[str] | None) -> None:
        """Reap a Linux clipboard owner process after the paste shortcut."""

        if process is None:
            return
        try:
            process.wait(timeout=_CLIPBOARD_OWNER_EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.info(
                "DesktopNativeActionExecutor clipboard owner stayed alive after paste; terminating it.",
                extra={"pid": process.pid},
            )
            process.terminate()
            try:
                process.wait(timeout=_CLIPBOARD_OWNER_EXIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=_CLIPBOARD_OWNER_EXIT_TIMEOUT_SECONDS)

    def _paste_clipboard_shortcut(self, target_kind: TextTargetKind = TextTargetKind.AUTO) -> None:
        """Paste the current clipboard contents into the focused control."""

        target_kind = TextTargetKind.coerce(target_kind)
        pyautogui = self._import_pyautogui()
        if platform.system() == "Darwin":
            pyautogui.hotkey("command", "v")
            return

        if platform.system() == "Linux" and target_kind == TextTargetKind.TERMINAL:
            pyautogui.hotkey("ctrl", "shift", "v")
            return

        pyautogui.hotkey("ctrl", "v")

    def _linux_clipboard_command(self, read: bool) -> Optional[list[str]]:
        """Resolve a clipboard command for Linux environments."""

        if shutil.which("wl-copy") and shutil.which("wl-paste"):
            return ["wl-paste"] if read else ["wl-copy"]

        if shutil.which("xclip"):
            return (
                ["xclip", "-selection", "clipboard", "-o"]
                if read
                else ["xclip", "-selection", "clipboard", "-loops", "1"]
            )

        if shutil.which("xsel"):
            return ["xsel", "--clipboard", "--output"] if read else ["xsel", "--clipboard", "--input"]

        return None
