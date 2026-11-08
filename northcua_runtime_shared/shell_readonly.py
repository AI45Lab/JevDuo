"""Allowlisted readonly command parsing shared by evaluator/device paths."""

from __future__ import annotations

import shlex
from pathlib import PurePath


_READONLY_EXECUTABLES = frozenset(
    {
        "cat",
        "date",
        "df",
        "du",
        "file",
        "fgrep",
        "grep",
        "head",
        "id",
        "ls",
        "md5sum",
        "printenv",
        "ps",
        "pwd",
        "sha1sum",
        "sha256sum",
        "stat",
        "tail",
        "uname",
        "wc",
        "whoami",
    }
)
_SHELL_CONTROL_TOKENS = frozenset({";", "&&", "||", "|", "&", ">", ">>", "<", "<<", "2>", "2>>"})
_FORBIDDEN_ARG_CHARS = frozenset({"\x00", "\n", "\r"})


def readonly_shell_argv(command: str) -> tuple[str, ...]:
    """Return an allowlisted argv tuple for a readonly probe.

    The returned argv is intended for ``create_subprocess_exec``. This helper
    deliberately does not support shell syntax, Python snippets, package
    managers, network clients, or arbitrary executables.
    """

    value = command.strip()
    if not value:
        raise ValueError("readonly shell command is required")
    try:
        argv = tuple(shlex.split(value, posix=True))
    except ValueError as exc:
        raise ValueError(f"readonly shell command cannot be parsed: {exc}") from exc
    if not argv:
        raise ValueError("readonly shell command is required")
    executable = PurePath(argv[0]).name
    if argv[0] != executable:
        raise ValueError("readonly shell command must use an allowlisted executable name")
    if executable not in _READONLY_EXECUTABLES:
        raise ValueError(f"readonly shell command executable {executable!r} is not allowlisted")
    for arg in argv:
        if arg in _SHELL_CONTROL_TOKENS:
            raise ValueError("readonly shell command cannot use shell control operators")
        if any(char in arg for char in _FORBIDDEN_ARG_CHARS):
            raise ValueError("readonly shell command contains an invalid argument character")
    _validate_readonly_args(executable, argv[1:])
    return argv


def _validate_readonly_args(executable: str, args: tuple[str, ...]) -> None:
    if executable == "date":
        for arg in args:
            if arg == "-u" or arg == "--utc" or arg.startswith("+"):
                continue
            raise ValueError("readonly shell command date only allows UTC and output format arguments")


def validate_readonly_shell_command(command: str) -> str:
    """Return a normalized allowlisted command string or raise."""

    return shlex.join(readonly_shell_argv(command))


__all__ = ["readonly_shell_argv", "validate_readonly_shell_command"]
