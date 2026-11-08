"""Small helpers for stable shell command generation."""

from __future__ import annotations

import shlex

from northcua.experiments.cases.contracts import DesktopSessionSpec


def quote(value: str) -> str:
    """Shell-quote one value.

    Args:
        value: Raw shell argument.

    Returns:
        POSIX shell-quoted argument.
    """

    return shlex.quote(value)


def bash_strict(lines: list[str]) -> str:
    """Return a strict bash script from logical lines.

    Args:
        lines: Bash statements to execute after strict-mode setup.

    Returns:
        A single bash -lc command string.
    """

    return "bash -lc " + shlex.quote("\n".join(["set -euo pipefail", *lines]))


def launch_background(
    command: list[str],
    *,
    stdout_path: str,
    stderr_path: str,
    pid_path: str,
    user: str = "",
    working_directory: str = "",
    desktop_session: DesktopSessionSpec | None = None,
    strict_desktop_session: bool = False,
) -> str:
    """Render a setsid background launch command.

    Args:
        command: Executable argv to launch.
        stdout_path: Remote file that captures stdout.
        stderr_path: Remote file that captures stderr.
        pid_path: Remote file that receives the launched pid.
        user: Optional Linux user to launch under with runuser.
        working_directory: Optional cwd for the launched process. User-scoped
            GUI launches default to the user's home directory.
        desktop_session: Typed graphical-session identity to resolve.
        strict_desktop_session: Deprecated compatibility switch for callers
            that have not yet passed a DesktopSessionSpec.

    Returns:
        Bash line that starts the command in the background.
    """

    argv = " ".join(shlex.quote(item) for item in command)
    command_script = (
        f"setsid {argv} </dev/null >{quote(stdout_path)} 2>{quote(stderr_path)} & echo $! > {quote(pid_path)}"
    )
    return launch_background_shell(
        command_script,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        pid_path=pid_path,
        user=user,
        working_directory=working_directory,
        desktop_session=desktop_session,
        strict_desktop_session=strict_desktop_session,
    )


def launch_background_shell(
    command_script: str,
    *,
    stdout_path: str,
    stderr_path: str,
    pid_path: str,
    user: str = "",
    working_directory: str = "",
    passthrough_env_vars: tuple[str, ...] = (),
    desktop_session: DesktopSessionSpec | None = None,
    strict_desktop_session: bool = False,
    prelaunch_lines: tuple[str, ...] = (),
) -> str:
    """Render a setsid background launch from a shell fragment."""

    if desktop_session is None and strict_desktop_session:
        desktop_session = DesktopSessionSpec(target_user=user)
    required_desktop_session = desktop_session is not None and desktop_session.required
    if prelaunch_lines and not required_desktop_session:
        command_script = "\n".join([*prelaunch_lines, command_script])
    if user or required_desktop_session:
        user_arg = quote(user)
        home_dir = quote(f"/home/{user}")
        cwd = quote(working_directory or f"/home/{user}") if not required_desktop_session else quote(working_directory)
        stdout_file = quote(stdout_path)
        stderr_file = quote(stderr_path)
        pid_file = quote(pid_path)
        writable_files = f"{stdout_file} {stderr_file} {pid_file}"
        passthrough_items = []
        for name in passthrough_env_vars:
            if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
                raise ValueError(f"unsafe passthrough env var name: {name!r}")
            passthrough_items.append(f'{name}="${{{name}:-}}"')
        passthrough = " ".join(passthrough_items)
        passthrough = f" {passthrough}" if passthrough else ""
        # Keep the directory change out of the same AND-OR list as the GUI
        # launch. Otherwise `cd <dir> && setsid app ... &` backgrounds the
        # whole `cd && setsid` list, leaving a shell alive with the E2B command
        # stream open until the GUI exits.
        if required_desktop_session:
            inner = (
                f"cd {cwd} || exit 1; {command_script}"
                if working_directory
                else f'cd "$HOME" || exit 1; {command_script}'
            )
        else:
            inner = f"cd {cwd} || exit 1; {command_script}"
        if required_desktop_session:
            assert desktop_session is not None
            return _strict_user_launcher(
                desktop_session=desktop_session,
                requested_user=user,
                command_script=inner,
                pid_path=pid_path,
                pid_file=pid_file,
                writable_files=writable_files,
                passthrough=passthrough,
                prelaunch_lines=prelaunch_lines,
            )
        launcher = "\n".join(
            [
                f"launch_uid=$(id -u {user_arg})",
                'session_runtime="/run/user/${launch_uid}"',
                "for _northcua_session_wait in 1 2 3 4 5 6 7 8 9 10; do",
                '  [ -d "$session_runtime" ] && [ -S "$session_runtime/bus" ] && break',
                "  sleep 0.5",
                "done",
                '[ -d "$session_runtime" ] || { echo "desktop runtime directory is missing: $session_runtime" >&2; exit 1; }',
                'runtime_owner=$(stat -Lc %u "$session_runtime" 2>/dev/null || true)',
                '[ "$runtime_owner" = "$launch_uid" ] || { echo "desktop runtime directory has the wrong owner" >&2; exit 1; }',
                'runtime_dir="$session_runtime"',
                "session_env=()",
                'if [ -d "$session_runtime" ] && [ -S "$session_runtime/bus" ]; then',
                '  runtime_dir="$session_runtime"',
                '  session_env+=(DBUS_SESSION_BUS_ADDRESS="unix:path=$session_runtime/bus")',
                '  if [ -S "$session_runtime/at-spi/bus" ]; then',
                '    session_env+=(AT_SPI_BUS_ADDRESS="unix:path=$session_runtime/at-spi/bus")',
                "  fi",
                "fi",
                f"touch {writable_files} && chown {user_arg}:{user_arg} {writable_files}",
                f'runuser -u {user_arg} -- env DISPLAY="${{DISPLAY:-:0}}" HOME={home_dir} '
                f'USER={user_arg} LOGNAME={user_arg} XDG_RUNTIME_DIR="$runtime_dir" '
                'XDG_CURRENT_DESKTOP="${XDG_CURRENT_DESKTOP:-GNOME}" DESKTOP_SESSION="${DESKTOP_SESSION:-gnome}" '
                'GDK_BACKEND="${GDK_BACKEND:-x11}" QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}" '
                'LANG="${LANG:-C.UTF-8}" LC_ALL="${LC_ALL:-C.UTF-8}" '
                f'"${{session_env[@]}}"{passthrough} bash -c {quote(inner)}',
            ]
        )
        return launcher
    if working_directory:
        command_script = f"cd {quote(working_directory)} || exit 1; {command_script}"
    return command_script


def _strict_user_launcher(
    *,
    desktop_session: DesktopSessionSpec,
    requested_user: str,
    command_script: str,
    pid_path: str,
    pid_file: str,
    writable_files: str,
    passthrough: str,
    prelaunch_lines: tuple[str, ...],
) -> str:
    """Render a launch bound to a validated live graphical session."""

    window_snapshot = quote(f"{pid_path}.windows-before")
    lines = [
        *_desktop_session_resolution_lines(desktop_session, requested_user=requested_user),
        f"touch {writable_files} {window_snapshot}",
        f'chown "$northcua_launch_user:$northcua_launch_user" {writable_files} {window_snapshot}',
        f": > {pid_file}",
        *prelaunch_lines,
        'runuser -u "$northcua_launch_user" -- env DISPLAY="$launch_display" '
        f'XAUTHORITY="$launch_xauthority" "$northcua_xdotool_path" search --name ".*" > {window_snapshot} '
        "2>/dev/null || true",
        f"sort -nu -o {window_snapshot} {window_snapshot}",
        'runuser -u "$northcua_launch_user" -- env DISPLAY="$launch_display" XAUTHORITY="$launch_xauthority" '
        'XDG_RUNTIME_DIR="$session_runtime" DBUS_SESSION_BUS_ADDRESS="$launch_dbus" '
        'AT_SPI_BUS_ADDRESS="$launch_at_spi" HOME="$northcua_launch_home" '
        'USER="$northcua_launch_user" LOGNAME="$northcua_launch_user" '
        "XDG_CURRENT_DESKTOP=GNOME DESKTOP_SESSION=gnome "
        'GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb LANG="${LANG:-C.UTF-8}" LC_ALL="${LC_ALL:-C.UTF-8}" '
        f"{passthrough.lstrip()} bash -c {quote(command_script)}",
        f"northcua_launch_pid=$(cat {pid_file} 2>/dev/null || true)",
        'case "$northcua_launch_pid" in ""|*[!0-9]*) northcua_desktop_session_failure '
        '"launcher did not record a numeric pid" ;; esac',
        'northcua_launch_actual_uid=""',
        'northcua_launch_sid=""',
        "for _northcua_identity_wait in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do",
        '  northcua_launch_actual_uid=$(stat -Lc %u "/proc/$northcua_launch_pid" 2>/dev/null || true)',
        '  northcua_launch_sid=$(ps -o sid= -p "$northcua_launch_pid" 2>/dev/null | tr -d " " || true)',
        '  if [ "$northcua_launch_actual_uid" = "$launch_uid" ] && '
        '[ "$northcua_launch_sid" = "$northcua_launch_pid" ]; then break; fi',
        "  sleep 0.05",
        "done",
        '[ "$northcua_launch_actual_uid" = "$launch_uid" ] || northcua_desktop_session_failure '
        '"launched pid is missing or owned by uid ${northcua_launch_actual_uid:-unknown}"',
        '[ "$northcua_launch_sid" = "$northcua_launch_pid" ] || northcua_desktop_session_failure '
        '"launched pid did not establish its own session (sid=${northcua_launch_sid:-unknown})"',
        'northcua_launch_identity=$(stat -Lc "%d:%i:%Y" "/proc/$northcua_launch_pid" 2>/dev/null || true)',
        '[ -n "$northcua_launch_identity" ] || northcua_desktop_session_failure '
        '"could not record launched pid identity"',
        f"northcua_window_snapshot={window_snapshot}",
        "northcua_strict_session_ready=1",
    ]
    return "\n".join(lines)


def desktop_session_readiness_script(desktop_session: DesktopSessionSpec) -> str:
    """Render a standalone fail-closed desktop-session readiness probe."""

    return bash_strict(_desktop_session_resolution_lines(desktop_session, requested_user=""))


def desktop_session_resolution_lines(desktop_session: DesktopSessionSpec) -> list[str]:
    """Return reusable lines that resolve a required graphical session."""

    return _desktop_session_resolution_lines(desktop_session, requested_user="")


def _desktop_session_resolution_lines(
    desktop_session: DesktopSessionSpec,
    *,
    requested_user: str,
) -> list[str]:
    """Resolve and validate the environment of one live Linux GUI session."""

    timeout = max(1, int(desktop_session.readiness_timeout_seconds))
    target_user = desktop_session.target_user or requested_user
    target_uid = "" if desktop_session.target_uid is None else str(desktop_session.target_uid)
    return [
        f"northcua_requested_user={quote(target_user)}",
        f"northcua_requested_uid={quote(target_uid)}",
        "northcua_desktop_session_failure() {",
        '  echo "desktop session resolution failed: $*" >&2',
        '  echo "desktop session diagnostics: requested_user=${northcua_requested_user:-auto} '
        "requested_uid=${northcua_requested_uid:-auto} selected_user=${northcua_launch_user:-none} "
        "selected_uid=${launch_uid:-none} session_pid=${northcua_session_pid:-none} "
        'runtime=${session_runtime:-none}" >&2',
        "  ps -eo uid=,pid=,ppid=,sid=,comm=,args= 2>&1 | tail -40 >&2 || true",
        '  if [ -n "${session_runtime:-}" ]; then find "$session_runtime" -maxdepth 2 '
        '-printf "%M %U %G %p\\n" 2>&1 | tail -30 >&2 || true; fi',
        "  exit 1",
        "}",
        "northcua_session_env_value() {",
        '  local env_pid="$1" env_name="$2"',
        '  tr "\\000" "\\n" < "/proc/$env_pid/environ" 2>/dev/null | '
        'awk -v key="$env_name" \'index($0, key "=") == 1 { sub("^[^=]*=", ""); print; exit }\' || true',
        "}",
        'if [ -n "$northcua_requested_user" ]; then',
        '  requested_user_uid=$(id -u -- "$northcua_requested_user" 2>/dev/null || true)',
        '  [ -n "$requested_user_uid" ] || northcua_desktop_session_failure "target user does not exist"',
        '  if [ -n "$northcua_requested_uid" ] && [ "$northcua_requested_uid" != "$requested_user_uid" ]; then',
        '    northcua_desktop_session_failure "target user and target UID do not match"',
        "  fi",
        '  northcua_requested_uid="$requested_user_uid"',
        "fi",
        "northcua_try_session_pid() {",
        '  local candidate_pid="$1" candidate_uid candidate_runtime candidate_dbus candidate_at_spi',
        "  local candidate_display candidate_xauthority",
        '  case "$candidate_pid" in ""|*[!0-9]*) return 1 ;; esac',
        '  candidate_uid=$(stat -Lc %u "/proc/$candidate_pid" 2>/dev/null || true)',
        '  [ -n "$candidate_uid" ] || return 1',
        '  if [ -n "$northcua_requested_uid" ] && [ "$candidate_uid" != "$northcua_requested_uid" ]; then return 1; fi',
        '  [ -r "/proc/$candidate_pid/environ" ] || return 1',
        '  candidate_runtime=$(northcua_session_env_value "$candidate_pid" XDG_RUNTIME_DIR)',
        '  [ "$candidate_runtime" = "/run/user/$candidate_uid" ] || return 1',
        '  candidate_dbus=$(northcua_session_env_value "$candidate_pid" DBUS_SESSION_BUS_ADDRESS)',
        '  candidate_at_spi=$(northcua_session_env_value "$candidate_pid" AT_SPI_BUS_ADDRESS)',
        '  candidate_display=$(northcua_session_env_value "$candidate_pid" DISPLAY)',
        '  candidate_xauthority=$(northcua_session_env_value "$candidate_pid" XAUTHORITY)',
        '  [ -n "$candidate_dbus" ] && [ -n "$candidate_at_spi" ] && [ -n "$candidate_display" ] '
        '&& [ -n "$candidate_xauthority" ] || return 1',
        '  launch_uid="$candidate_uid"',
        '  session_runtime="$candidate_runtime"',
        '  launch_dbus="$candidate_dbus"',
        '  launch_at_spi="$candidate_at_spi"',
        '  launch_display="$candidate_display"',
        '  launch_xauthority="$candidate_xauthority"',
        '  northcua_session_pid="$candidate_pid"',
        "  return 0",
        "}",
        "northcua_find_session_for_uid() {",
        '  local expected_uid="$1" leader_pid="${2:-}" candidate_uid candidate_pid candidate_command',
        "  while read -r candidate_uid candidate_pid candidate_command; do",
        '    [ "$candidate_uid" = "$expected_uid" ] || continue',
        '    case "$candidate_command" in gnome-shell|gnome-session*|xfce4-session|plasmashell|mate-session|cinnamon) ;; '
        "*) continue ;; esac",
        '    if northcua_try_session_pid "$candidate_pid"; then return 0; fi',
        "  done < <(ps -eo uid=,pid=,comm= 2>/dev/null || true)",
        '  if [ -n "$leader_pid" ] && northcua_try_session_pid "$leader_pid"; then return 0; fi',
        "  return 1",
        "}",
        'northcua_session_pid=""',
        "northcua_resolution_deadline=$(($(date +%s) + " + str(timeout) + "))",
        'while [ -z "$northcua_session_pid" ]; do',
        "  if command -v loginctl >/dev/null 2>&1; then",
        "    while read -r session_id _; do",
        '      [ -n "$session_id" ] || continue',
        '      session_active=$(loginctl show-session "$session_id" -p Active --value 2>/dev/null || true)',
        '      session_type=$(loginctl show-session "$session_id" -p Type --value 2>/dev/null || true)',
        '      session_class=$(loginctl show-session "$session_id" -p Class --value 2>/dev/null || true)',
        '      session_uid=$(loginctl show-session "$session_id" -p User --value 2>/dev/null || true)',
        '      session_leader=$(loginctl show-session "$session_id" -p Leader --value 2>/dev/null || true)',
        '      [ "$session_active" = "yes" ] || continue',
        '      case "$session_type" in x11|wayland) ;; *) continue ;; esac',
        '      case "$session_class" in user|user-early|"") ;; *) continue ;; esac',
        '      if [ -n "$northcua_requested_uid" ] && [ "$session_uid" != "$northcua_requested_uid" ]; then continue; fi',
        '      if northcua_find_session_for_uid "$session_uid" "$session_leader"; then break; fi',
        "    done < <(loginctl list-sessions --no-legend 2>/dev/null || true)",
        "  fi",
        '  if [ -z "$northcua_session_pid" ]; then',
        "    while read -r candidate_uid candidate_pid candidate_command; do",
        '      case "$candidate_command" in gnome-shell|gnome-session*|xfce4-session|plasmashell|mate-session|cinnamon) ;; '
        "*) continue ;; esac",
        '      if northcua_try_session_pid "$candidate_pid"; then break; fi',
        "    done < <(ps -eo uid=,pid=,comm= 2>/dev/null || true)",
        "  fi",
        '  [ -n "$northcua_session_pid" ] && break',
        '  [ "$(date +%s)" -lt "$northcua_resolution_deadline" ] || break',
        "  sleep 1",
        "done",
        '[ -n "$northcua_session_pid" ] || northcua_desktop_session_failure '
        '"no live graphical session exposed DISPLAY, XAUTHORITY, DBus, AT-SPI, and XDG_RUNTIME_DIR"',
        'northcua_user_record=$(getent passwd "$launch_uid" 2>/dev/null || true)',
        '[ -n "$northcua_user_record" ] || northcua_desktop_session_failure "session UID has no passwd entry"',
        'northcua_launch_user="${northcua_user_record%%:*}"',
        'northcua_launch_home=$(printf "%s\\n" "$northcua_user_record" | cut -d: -f6)',
        '[ -n "$northcua_launch_home" ] || northcua_desktop_session_failure "session user has no home directory"',
        '[ -d "$session_runtime" ] || northcua_desktop_session_failure "runtime directory is missing"',
        'resolved_runtime=$(readlink -f "$session_runtime" 2>/dev/null || true)',
        '[ "$resolved_runtime" = "/run/user/$launch_uid" ] || northcua_desktop_session_failure '
        '"runtime directory does not resolve to /run/user/$launch_uid"',
        'runtime_owner=$(stat -Lc %u "$session_runtime" 2>/dev/null || true)',
        '[ "$runtime_owner" = "$launch_uid" ] || northcua_desktop_session_failure '
        '"runtime directory has wrong owner ${runtime_owner:-unknown}"',
        'runtime_mode=$(stat -Lc %a "$session_runtime" 2>/dev/null || true)',
        'case "$runtime_mode" in ""|*[!0-7]*) northcua_desktop_session_failure "runtime directory mode is invalid" ;; esac',
        "if (( (8#$runtime_mode & 077) != 0 )); then northcua_desktop_session_failure "
        '"runtime directory is accessible by group or other users"; fi',
        'northcua_dbus_path="${launch_dbus#unix:path=}"',
        '[ "$northcua_dbus_path" != "$launch_dbus" ] || northcua_desktop_session_failure '
        '"DBus address is not a unix:path address"',
        'northcua_dbus_path="${northcua_dbus_path%%,*}"',
        'resolved_dbus_path=$(readlink -f "$northcua_dbus_path" 2>/dev/null || true)',
        '[ "$resolved_dbus_path" = "$session_runtime/bus" ] || northcua_desktop_session_failure '
        '"DBus socket is stale or outside the session runtime: $northcua_dbus_path"',
        '[ -S "$northcua_dbus_path" ] || northcua_desktop_session_failure '
        '"DBus socket is missing: $northcua_dbus_path"',
        'dbus_owner=$(stat -Lc %u "$northcua_dbus_path" 2>/dev/null || true)',
        '[ "$dbus_owner" = "$launch_uid" ] || northcua_desktop_session_failure '
        '"DBus socket has wrong owner ${dbus_owner:-unknown}"',
        'northcua_at_spi_path="${launch_at_spi#unix:path=}"',
        '[ "$northcua_at_spi_path" != "$launch_at_spi" ] || northcua_desktop_session_failure '
        '"AT-SPI address is not a unix:path address"',
        'northcua_at_spi_path="${northcua_at_spi_path%%,*}"',
        'resolved_at_spi_path=$(readlink -f "$northcua_at_spi_path" 2>/dev/null || true)',
        'case "$resolved_at_spi_path" in "$session_runtime"/at-spi/*) ;; *) northcua_desktop_session_failure '
        '"AT-SPI socket is stale or outside the session runtime: $northcua_at_spi_path" ;; esac',
        '[ -S "$northcua_at_spi_path" ] || northcua_desktop_session_failure '
        '"AT-SPI socket is missing: $northcua_at_spi_path"',
        'at_spi_owner=$(stat -Lc %u "$northcua_at_spi_path" 2>/dev/null || true)',
        '[ "$at_spi_owner" = "$launch_uid" ] || northcua_desktop_session_failure '
        '"AT-SPI socket has wrong owner ${at_spi_owner:-unknown}"',
        'case "$launch_xauthority" in /*) ;; *) northcua_desktop_session_failure '
        '"XAUTHORITY is not an absolute path: $launch_xauthority" ;; esac',
        'resolved_xauthority=$(readlink -f "$launch_xauthority" 2>/dev/null || true)',
        '[ -f "$resolved_xauthority" ] || northcua_desktop_session_failure '
        '"XAUTHORITY is missing or stale: $launch_xauthority"',
        'xauthority_owner=$(stat -Lc %u "$resolved_xauthority" 2>/dev/null || true)',
        '[ "$xauthority_owner" = "$launch_uid" ] || northcua_desktop_session_failure '
        '"XAUTHORITY has wrong owner ${xauthority_owner:-unknown}"',
        'runuser -u "$northcua_launch_user" -- test -r "$resolved_xauthority" || '
        'northcua_desktop_session_failure "XAUTHORITY is not readable by the session user"',
        'launch_xauthority="$resolved_xauthority"',
        'kill -0 "$northcua_session_pid" 2>/dev/null || northcua_desktop_session_failure '
        '"selected session process is stale"',
        "northcua_dbus_send_path=$(command -v dbus-send || true)",
        '[ -n "$northcua_dbus_send_path" ] || northcua_desktop_session_failure "dbus-send is required"',
        'if ! timeout 5s runuser -u "$northcua_launch_user" -- env DBUS_SESSION_BUS_ADDRESS="$launch_dbus" '
        '"$northcua_dbus_send_path" --session --print-reply=literal --dest=org.freedesktop.DBus '
        "/org/freedesktop/DBus org.freedesktop.DBus.ListNames >/dev/null 2>&1; then "
        'northcua_desktop_session_failure "DBus session address is stale"; fi',
        'if ! timeout 5s runuser -u "$northcua_launch_user" -- '
        '"$northcua_dbus_send_path" --bus="$launch_at_spi" --print-reply=literal '
        "--dest=org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.ListNames "
        '>/dev/null 2>&1; then northcua_desktop_session_failure "AT-SPI bus address is stale"; fi',
        "northcua_xdotool_path=$(command -v xdotool || true)",
        '[ -n "$northcua_xdotool_path" ] || northcua_desktop_session_failure "xdotool is required"',
        'if ! timeout 5s runuser -u "$northcua_launch_user" -- env DISPLAY="$launch_display" '
        'XAUTHORITY="$launch_xauthority" "$northcua_xdotool_path" getdisplaygeometry >/dev/null 2>&1; then '
        'northcua_desktop_session_failure "DISPLAY/XAUTHORITY cannot access the target display"; fi',
        'export DISPLAY="$launch_display" XAUTHORITY="$launch_xauthority" XDG_RUNTIME_DIR="$session_runtime"',
        'export DBUS_SESSION_BUS_ADDRESS="$launch_dbus" AT_SPI_BUS_ADDRESS="$launch_at_spi"',
        "northcua_strict_session_ready=1",
    ]


def _recent_process_probe_line(process_contains: str, *, launch_user: str = "") -> str:
    """Return a bash line that marks a recent non-wrapper target process."""

    process = quote(process_contains)
    launch_uid_command = f"id -u {quote(launch_user)}" if launch_user else "id -u"
    return (
        f"if [ -n {process} ] && ps -eo uid=,pid=,etimes=,comm=,args= | "
        f'awk -v hint={process} -v expected_uid="$({launch_uid_command})" '
        f'-v max_age="$((now_seconds - start_seconds + 5))" -v self="$$" '
        "'$1 == expected_uid && $2 != self && $3 <= max_age "
        "&& $4 !~ /^(sh|bash|dash|zsh|ksh|fish|runuser|sudo|env|setsid|timeout|awk)$/ "
        "&& index($0, hint) > 0 "
        "{ found=1 } END { exit found ? 0 : 1 }'"
        "; then process_seen=1; fi"
    )


def wait_for_process_or_window(
    *,
    process_contains: str,
    window_contains: str,
    timeout_seconds: float,
    pid_path: str = "",
    allow_process_fallback_with_window: bool = False,
    track_recent_process_exit: bool = False,
    strict_desktop_session: bool = False,
    launch_user: str = "",
) -> list[str]:
    """Return bash lines that wait for a process or window title.

    Args:
        process_contains: Process command substring to probe.
        window_contains: Window-title substring to probe.
        timeout_seconds: Maximum wait duration.
        pid_path: Optional pid file produced by launch_background.
        allow_process_fallback_with_window: Whether a recently launched process
            can satisfy readiness when a window-title probe is also requested.
            This is reserved for apps with intentionally unstable window titles.
        track_recent_process_exit: Whether a recently exited process can mark
            diagnostics as having seen the launch target without satisfying
            readiness.
        strict_desktop_session: Require readiness to belong to the strict
            launch identity and reject windows that predate launch.
        launch_user: User expected to own the strict launch identity.

    Returns:
        Bash lines that exit successfully once readiness is observed.
    """

    if strict_desktop_session:
        return _strict_wait_for_process_or_window(
            process_contains=process_contains,
            window_contains=window_contains,
            timeout_seconds=timeout_seconds,
            pid_path=pid_path,
            allow_process_fallback_with_window=allow_process_fallback_with_window,
            launch_user=launch_user,
        )
    if not process_contains and not window_contains and not pid_path:
        return ["sleep 3"]
    timeout = max(1, int(timeout_seconds))
    if not process_contains and not window_contains:
        pid_file = quote(pid_path)
        return [
            'start_seconds="$(date +%s)"',
            f"deadline=$((start_seconds + {timeout}))",
            "while :; do",
            '  now_seconds="$(date +%s)"',
            '  [ "$now_seconds" -lt "$deadline" ] || break',
            f'  if [ -s {pid_file} ] && kill -0 "$(cat {pid_file})" >/dev/null 2>&1; then exit 0; fi',
            "  sleep 1",
            "done",
            "echo 'case recipe readiness probe timed out' >&2",
            "exit 1",
        ]
    window = quote(window_contains)
    if window_contains:
        pid_file = quote(pid_path) if pid_path else ""
        lines = [
            'start_seconds="$(date +%s)"',
            f"deadline=$((start_seconds + {timeout}))",
            "process_seen=0",
            "while :; do",
            '  now_seconds="$(date +%s)"',
            '  [ "$now_seconds" -lt "$deadline" ] || break',
            f"  if command -v wmctrl >/dev/null 2>&1 && wmctrl -lx 2>/dev/null | grep -i -- {window} >/dev/null 2>&1; then exit 0; fi",
            f"  if command -v xdotool >/dev/null 2>&1 && xdotool search --name {window} >/dev/null 2>&1; then exit 0; fi",
            f"  if command -v xdotool >/dev/null 2>&1 && xdotool search --class {window} >/dev/null 2>&1; then exit 0; fi",
        ]
        if pid_path:
            lines.append(
                f'  if [ -s {pid_file} ] && command -v xdotool >/dev/null 2>&1 && xdotool search --pid "$(cat {pid_file})" >/dev/null 2>&1; then exit 0; fi'
            )
        if process_contains:
            process = quote(process_contains)
            if allow_process_fallback_with_window:
                lines.insert(2, "process_fallback_deadline=$((start_seconds + 8))")
            if pid_path:
                recent_process_probe = _recent_process_probe_line(
                    process_contains,
                    launch_user=launch_user,
                )
                lines.append(f"  {recent_process_probe}")
                if allow_process_fallback_with_window:
                    lines.append(
                        f'  if [ -s {pid_file} ] && ! kill -0 "$(cat {pid_file})" >/dev/null 2>&1 && [ "$process_seen" != "1" ]; then echo "launched process exited before window appeared" >&2; fi'
                    )
                else:
                    exited_process_probe = (
                        ":"
                        if track_recent_process_exit
                        else 'echo "launched process exited before window appeared" >&2'
                    )
                    lines.append(
                        f'  if [ -s {pid_file} ] && ! kill -0 "$(cat {pid_file})" >/dev/null 2>&1 && [ "$process_seen" != "1" ]; then {exited_process_probe}; fi'
                    )
                lines.append(
                    f'  if [ -n {process} ] && command -v xdotool >/dev/null 2>&1; then for northcua_pid in $(pgrep -f -- {process} 2>/dev/null || true); do xdotool search --pid "$northcua_pid" >/dev/null 2>&1 && exit 0; done; fi'
                )
            else:
                lines.append(
                    f"  if [ -n {process} ] && pgrep -af -- {process} >/dev/null 2>&1; then process_seen=1; fi"
                )
                lines.append(
                    f'  if [ -n {process} ] && command -v xdotool >/dev/null 2>&1; then for northcua_pid in $(pgrep -f -- {process} 2>/dev/null || true); do xdotool search --pid "$northcua_pid" >/dev/null 2>&1 && exit 0; done; fi'
                )
                lines.append(
                    f"  if [ -n {process} ] && ! pgrep -af -- {process} >/dev/null 2>&1; then echo 'process not seen while waiting for window' >&2; fi"
                )
            if allow_process_fallback_with_window:
                lines.append(
                    '  if [ "$process_seen" = "1" ] && [ "$now_seconds" -ge "$process_fallback_deadline" ]; then exit 0; fi'
                )
        elif pid_path:
            lines.append(
                f'  if [ -s {pid_file} ] && ! kill -0 "$(cat {pid_file})" >/dev/null 2>&1; then echo "launched process exited before window appeared" >&2; fi'
            )
        lines.extend(
            [
                "  sleep 1",
                "done",
            ]
        )
        lines.extend(
            [
                f"echo 'case recipe window readiness diagnostics: expected_window={window_contains}' >&2",
                "if command -v wmctrl >/dev/null 2>&1; then echo 'case recipe wmctrl -lx snapshot:' >&2; wmctrl -lx 2>&1 | tail -20 >&2 || true; fi",
                f"if [ -n {quote(process_contains)} ]; then echo 'case recipe process snapshot:' >&2; pgrep -af -- {quote(process_contains)} 2>&1 | tail -20 >&2 || true; fi",
                'if [ "$process_seen" = "1" ]; then',
                "  echo 'case recipe window readiness probe timed out after process appeared' >&2",
                "else",
                "  echo 'case recipe window readiness probe timed out before process appeared' >&2",
                "fi",
                "exit 1",
            ]
        )
        return lines
    process = quote(process_contains)
    if pid_path:
        pid_file = quote(pid_path)
        recent_process_probe = _recent_process_probe_line(
            process_contains,
            launch_user=launch_user,
        )
        return [
            'start_seconds="$(date +%s)"',
            f"deadline=$((start_seconds + {timeout}))",
            "process_seen=0",
            "while :; do",
            '  now_seconds="$(date +%s)"',
            '  [ "$now_seconds" -lt "$deadline" ] || break',
            f"  {recent_process_probe}",
            '  [ "$process_seen" = "1" ] && exit 0',
            f'  if [ -s {pid_file} ] && ! kill -0 "$(cat {pid_file})" >/dev/null 2>&1; then echo "launched process exited before target process appeared" >&2; fi',
            f"  if [ ! -s {pid_file} ] && [ -n {process} ] && pgrep -af -- {process} >/dev/null 2>&1; then exit 0; fi",
            "  sleep 1",
            "done",
            "echo 'case recipe readiness probe timed out' >&2",
            "exit 1",
        ]
    return [
        'start_seconds="$(date +%s)"',
        f"deadline=$((start_seconds + {timeout}))",
        "while :; do",
        '  now_seconds="$(date +%s)"',
        '  [ "$now_seconds" -lt "$deadline" ] || break',
        f"  if [ -n {process} ] && pgrep -af -- {process} >/dev/null 2>&1; then exit 0; fi",
        "  sleep 1",
        "done",
        "echo 'case recipe readiness probe timed out' >&2",
        "exit 1",
    ]


def _strict_wait_for_process_or_window(
    *,
    process_contains: str,
    window_contains: str,
    timeout_seconds: float,
    pid_path: str,
    allow_process_fallback_with_window: bool,
    launch_user: str,
) -> list[str]:
    """Render readiness probes bound to one strict launch identity."""

    if not pid_path:
        return [
            "echo 'strict desktop readiness requires pid_path' >&2",
            "exit 1",
        ]
    timeout = max(1, int(timeout_seconds))
    pid_file = quote(pid_path)
    process = quote(process_contains)
    window = quote(window_contains)
    _ = launch_user
    lines = [
        f"northcua_expected_process={process}",
        f"northcua_expected_window={window}",
        "northcua_strict_readiness_diagnostics() {",
        '  echo "strict desktop readiness diagnostics: expected_process=$northcua_expected_process '
        "expected_window=$northcua_expected_window launch_pid=${northcua_launch_pid:-unknown} "
        'launch_uid=${launch_uid:-unknown}" >&2',
        '  echo "strict desktop readiness window snapshot: ${northcua_window_snapshot:-missing}" >&2',
        '  if [ -r "${northcua_window_snapshot:-}" ]; then tail -30 "$northcua_window_snapshot" >&2 || true; fi',
        '  echo "strict desktop readiness current windows:" >&2',
        '  if declare -p northcua_xdotool >/dev/null 2>&1; then "${northcua_xdotool[@]}" search --name ".*" '
        "2>&1 | tail -30 >&2 || true; fi",
        '  if [ -n "${launch_uid:-}" ]; then ps -u "$launch_uid" -o uid=,pid=,ppid=,sid=,etimes=,args= '
        "2>&1 | tail -40 >&2 || true; fi",
        "}",
        "northcua_process_is_bound() {",
        '  local candidate_pid="$1" candidate_uid candidate_sid candidate_identity',
        '  case "$candidate_pid" in ""|*[!0-9]*) return 1 ;; esac',
        '  candidate_uid=$(stat -Lc %u "/proc/$candidate_pid" 2>/dev/null || true)',
        '  [ "$candidate_uid" = "$launch_uid" ] || return 1',
        '  if [ "$candidate_pid" = "$northcua_launch_pid" ]; then',
        '    candidate_identity=$(stat -Lc "%d:%i:%Y" "/proc/$candidate_pid" 2>/dev/null || true)',
        '    [ "$candidate_identity" = "$northcua_launch_identity" ]',
        "    return",
        "  fi",
        '  candidate_sid=$(ps -o sid= -p "$candidate_pid" 2>/dev/null | tr -d " " || true)',
        '  [ "$candidate_sid" = "$northcua_launch_pid" ]',
        "}",
        "northcua_bound_process_seen() {",
        "  local candidate_pid candidate_comm candidate_args",
        '  [ -n "$northcua_expected_process" ] || return 1',
        "  while read -r candidate_pid; do",
        '    northcua_process_is_bound "$candidate_pid" || continue',
        '    candidate_comm=$(cat "/proc/$candidate_pid/comm" 2>/dev/null || true)',
        '    case "$candidate_comm" in sh|bash|dash|zsh|ksh|fish|runuser|sudo|env|setsid|timeout) continue ;; esac',
        "    candidate_args=$(tr '\\000' ' ' < \"/proc/$candidate_pid/cmdline\" 2>/dev/null || true)",
        '    if [[ "$candidate_comm $candidate_args" == *"$northcua_expected_process"* ]]; then return 0; fi',
        '  done < <(ps -eo pid=,sid= 2>/dev/null | awk -v expected_sid="$northcua_launch_pid" '
        "'$2 == expected_sid { print $1 }')",
        "  return 1",
        "}",
        "northcua_new_bound_window_seen() {",
        "  local window_id window_pid",
        "  while read -r window_id; do",
        '    case "$window_id" in ""|*[!0-9]*) continue ;; esac',
        '    grep -Fqx -- "$window_id" "$northcua_window_snapshot" 2>/dev/null && continue',
        '    window_pid=$("${northcua_xdotool[@]}" getwindowpid "$window_id" 2>/dev/null || true)',
        '    if northcua_process_is_bound "$window_pid"; then return 0; fi',
        '  done < <({ "${northcua_xdotool[@]}" search --name "$northcua_expected_window" 2>/dev/null || true; '
        '"${northcua_xdotool[@]}" search --class "$northcua_expected_window" 2>/dev/null || true; } | sort -nu)',
        "  return 1",
        "}",
        '[ "${northcua_strict_session_ready:-0}" = "1" ] || { '
        "echo 'strict desktop readiness has no resolved session launch' >&2; exit 1; }",
        f"recorded_launch_pid=$(cat {pid_file} 2>/dev/null || true)",
        '[ "$recorded_launch_pid" = "$northcua_launch_pid" ] || { '
        "echo 'strict desktop readiness pid file does not match launch identity' >&2; "
        "northcua_strict_readiness_diagnostics; exit 1; }",
        '[ -r "$northcua_window_snapshot" ] || { '
        "echo 'strict desktop readiness is missing the pre-launch window snapshot' >&2; "
        "northcua_strict_readiness_diagnostics; exit 1; }",
        'northcua_xdotool=(runuser -u "$northcua_launch_user" -- env DISPLAY="$DISPLAY" '
        'XAUTHORITY="$XAUTHORITY" "$northcua_xdotool_path")',
        'start_seconds="$(date +%s)"',
        f"deadline=$((start_seconds + {timeout}))",
        "process_seen=0",
    ]
    if window_contains and allow_process_fallback_with_window:
        lines.append("process_fallback_deadline=$((start_seconds + 8))")
    lines.extend(
        [
            "while :; do",
            '  now_seconds="$(date +%s)"',
            '  [ "$now_seconds" -lt "$deadline" ] || break',
        ]
    )
    if window_contains:
        lines.append("  if northcua_new_bound_window_seen; then exit 0; fi")
        lines.append("  if northcua_bound_process_seen; then process_seen=1; fi")
        if allow_process_fallback_with_window:
            lines.append(
                '  if [ "$process_seen" = "1" ] && [ "$now_seconds" -ge "$process_fallback_deadline" ]; then exit 0; fi'
            )
    else:
        lines.append("  if northcua_bound_process_seen; then exit 0; fi")
    lines.extend(
        [
            "  sleep 1",
            "done",
            "northcua_strict_readiness_diagnostics",
        ]
    )
    if window_contains:
        lines.extend(
            [
                'if [ "$process_seen" = "1" ]; then',
                "  echo 'strict desktop window readiness timed out after the launched process appeared' >&2",
                "else",
                "  echo 'strict desktop window readiness timed out before the launched process appeared' >&2",
                "fi",
            ]
        )
    else:
        lines.append("echo 'strict desktop process readiness timed out' >&2")
    lines.append("exit 1")
    return lines
