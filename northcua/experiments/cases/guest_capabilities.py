"""Fail-closed runtime probes for capabilities required inside case guests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from northcua.experiments.cases.contracts import DesktopSessionSpec, GuestCapability, StrictCaseModel
from northcua.experiments.cases.recipes.shell import bash_strict, desktop_session_resolution_lines, quote
from northcua.sandboxes.contracts import SandboxCommandSpec
from northcua.sandboxes.guest_capability_evidence import (
    GUEST_CAPABILITY_EVIDENCE_PREFIX,
    GUEST_CAPABILITY_GATE_LABEL,
    guest_capability_evidence_payloads,
)

_I386_EXIT_ZERO_ELF_BASE64 = (
    "f0VMRgEBAQAAAAAAAAAAAAIAAwABAAAAVIAECDQAAAAAAAAAAAAAADQAIAABAAAAAAAAAAEAAAAAAAAAAIAE"
    "CACABAhdAAAAXQAAAAUAAAAAEAAAuAEAAAAx282A"
)


class GuestCapabilityProbeEvidence(StrictCaseModel):
    """One machine-readable result emitted by the fresh-guest runtime gate."""

    capability: GuestCapability
    status: Literal["available", "unavailable"]
    probe: str
    exit_code: int


@dataclass(frozen=True, slots=True)
class _ProbeDefinition:
    """Shell function implementing one actual guest capability check."""

    probe_id: str
    body: tuple[str, ...]
    requires_desktop_session: bool = False


class GuestCapabilityGateRenderer:
    """Validate and render the pre-init runtime gate for guest capabilities."""

    def validate(self, capabilities: set[GuestCapability]) -> None:
        """Reject declarations that cannot be verified in a fresh guest."""

        definitions = self._definitions("python3")
        unsupported = sorted(capability.value for capability in capabilities if capability not in definitions)
        if unsupported:
            raise ValueError(f"required guest capabilities have no runtime probe: {unsupported}")

    def requires_desktop_session(self, capabilities: set[GuestCapability]) -> bool:
        """Return whether any required probe must join the graphical session."""

        return bool(self._desktop_capabilities(capabilities))

    def validate_runtime_context(
        self,
        capabilities: set[GuestCapability],
        *,
        desktop_session: DesktopSessionSpec | None,
    ) -> None:
        """Reject requirements that cannot execute in the resolved guest context."""

        self.validate(capabilities)
        desktop_capabilities = self._desktop_capabilities(capabilities)
        if desktop_capabilities and (desktop_session is None or not desktop_session.required):
            desktop_ids = sorted(capability.value for capability in desktop_capabilities)
            raise ValueError(f"guest capabilities require a live desktop session: {desktop_ids}")

    def _desktop_capabilities(self, capabilities: set[GuestCapability]) -> set[GuestCapability]:
        definitions = self._definitions("python3")
        return {
            capability
            for capability in capabilities
            if (definition := definitions.get(capability)) is not None and definition.requires_desktop_session
        }

    def render(
        self,
        capabilities: set[GuestCapability],
        *,
        desktop_session: DesktopSessionSpec | None,
        python_executable: str,
    ) -> SandboxCommandSpec | None:
        """Render one aggregate gate that probes every declaration before init."""

        if not capabilities:
            return None
        self.validate_runtime_context(capabilities, desktop_session=desktop_session)
        definitions = self._definitions(python_executable)
        ordered = sorted(capabilities, key=lambda capability: capability.value)

        lines = [
            "northcua_guest_capability_unavailable_count=0",
            "northcua_guest_capability_probe() {",
            '  local capability="$1" probe_id="$2" probe_function="$3" output_file exit_code status evidence',
            "  output_file=$(mktemp)",
            '  if ( "$probe_function" ) >"$output_file" 2>&1; then',
            '    exit_code=0; status="available"',
            "  else",
            '    exit_code=$?; status="unavailable"',
            "    northcua_guest_capability_unavailable_count=$((northcua_guest_capability_unavailable_count + 1))",
            "  fi",
            f"  printf -v evidence '{GUEST_CAPABILITY_EVIDENCE_PREFIX}"
            '{"capability":"%s","status":"%s","probe":"%s","exit_code":%d}'
            '\' "$capability" "$status" "$probe_id" "$exit_code"',
            '  printf "%s\\n" "$evidence"',
            '  if [ "$status" = "unavailable" ]; then',
            '    tail -c 600 "$output_file" >&2 || true',
            '    printf "\\n%s\\n" "$evidence" >&2',
            "  fi",
            '  rm -f "$output_file"',
            "}",
        ]
        if self.requires_desktop_session(capabilities):
            assert desktop_session is not None
            lines.extend(self._desktop_session_helpers(desktop_session))
        for index, capability in enumerate(ordered):
            definition = definitions[capability]
            function_name = f"northcua_probe_{index}"
            lines.extend(
                [
                    f"{function_name}() {{",
                    *[f"  {line} || return $?" for line in definition.body],
                    "}",
                ]
            )
            lines.append(
                f"northcua_guest_capability_probe {quote(capability.value)} "
                f"{quote(definition.probe_id)} {function_name}"
            )
        lines.append('[ "$northcua_guest_capability_unavailable_count" -eq 0 ] || exit 1')
        timeout_seconds = 180.0
        if desktop_session is not None and self.requires_desktop_session(capabilities):
            timeout_seconds += desktop_session.readiness_timeout_seconds
        return SandboxCommandSpec(
            command=bash_strict(lines),
            label=GUEST_CAPABILITY_GATE_LABEL,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _desktop_session_helpers(desktop_session: DesktopSessionSpec) -> list[str]:
        resolution = desktop_session_resolution_lines(desktop_session)
        declared_variables = " ".join(
            (
                "northcua_launch_user",
                "northcua_launch_home",
                "launch_uid",
                "launch_display",
                "launch_xauthority",
                "launch_dbus",
                "launch_at_spi",
                "session_runtime",
                "northcua_xdotool_path",
            )
        )
        return [
            "northcua_session_env_file=$(mktemp)",
            "northcua_session_diagnostic_file=$(mktemp)",
            'trap \'rm -f "$northcua_session_env_file" "$northcua_session_diagnostic_file"\' EXIT',
            "northcua_resolve_desktop_session() {",
            *[f"  {line}" for line in resolution],
            f'  declare -p {declared_variables} > "$northcua_session_env_file"',
            "}",
            'if ( northcua_resolve_desktop_session ) >"$northcua_session_diagnostic_file" 2>&1; then',
            '  source "$northcua_session_env_file"',
            "  northcua_desktop_session_available=1",
            "else",
            "  northcua_desktop_session_available=0",
            "fi",
            "northcua_require_desktop_session() {",
            '  if [ "$northcua_desktop_session_available" -ne 1 ]; then',
            '    cat "$northcua_session_diagnostic_file" >&2',
            "    return 1",
            "  fi",
            "}",
            "northcua_desktop_exec() {",
            "  northcua_require_desktop_session",
            '  timeout 20s runuser -u "$northcua_launch_user" -- env DISPLAY="$launch_display" '
            'XAUTHORITY="$launch_xauthority" XDG_RUNTIME_DIR="$session_runtime" '
            'DBUS_SESSION_BUS_ADDRESS="$launch_dbus" AT_SPI_BUS_ADDRESS="$launch_at_spi" '
            'HOME="$northcua_launch_home" USER="$northcua_launch_user" LOGNAME="$northcua_launch_user" "$@"',
            "}",
            "northcua_desktop_shell() {",
            '  local script="$1"',
            '  northcua_desktop_exec bash -lc "$script"',
            "}",
        ]

    @staticmethod
    def _definitions(python_executable: str) -> dict[GuestCapability, _ProbeDefinition]:
        python = quote(python_executable)
        network_probe = "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1); s.close()"
        internet_probe = (
            "import socket; endpoints=(('1.1.1.1',443),('8.8.8.8',443)); errors=[]; "
            "\nfor endpoint in endpoints:\n"
            "    try:\n"
            "        socket.create_connection(endpoint, 5).close(); break\n"
            "    except OSError as exc: errors.append(str(exc))\n"
            "else: raise OSError('; '.join(errors))"
        )
        fanotify_probe = (
            "import ctypes, os; libc=ctypes.CDLL(None,use_errno=True); "
            "fd=libc.fanotify_init(1, os.O_RDONLY|getattr(os,'O_LARGEFILE',0)); "
            "\nif fd < 0: raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))\n"
            "os.close(fd)"
        )
        i386_probe = (
            "import base64,os,subprocess,tempfile; fd,path=tempfile.mkstemp(); "
            f"os.write(fd,base64.b64decode('{_I386_EXIT_ZERO_ELF_BASE64}')); os.close(fd); os.chmod(path,0o700); "
            "\ntry: subprocess.run([path],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)\n"
            "finally: os.unlink(path)"
        )
        clipboard_probe = (
            "import ctypes; x11=ctypes.CDLL('libX11.so.6'); "
            "x11.XOpenDisplay.argtypes=[ctypes.c_char_p]; x11.XOpenDisplay.restype=ctypes.c_void_p; "
            "x11.XInternAtom.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_int]; "
            "x11.XInternAtom.restype=ctypes.c_ulong; "
            "x11.XGetSelectionOwner.argtypes=[ctypes.c_void_p,ctypes.c_ulong]; "
            "x11.XGetSelectionOwner.restype=ctypes.c_ulong; "
            "x11.XSync.argtypes=[ctypes.c_void_p,ctypes.c_int]; "
            "x11.XCloseDisplay.argtypes=[ctypes.c_void_p]; display=x11.XOpenDisplay(None); "
            "\nif not display: raise RuntimeError('cannot open X11 display')\n"
            "\ntry:\n"
            "    clipboard=x11.XInternAtom(display,b'CLIPBOARD',1)\n"
            "    if not clipboard: raise RuntimeError('cannot resolve CLIPBOARD selection atom')\n"
            "    x11.XGetSelectionOwner(display,clipboard); x11.XSync(display,0)\n"
            "finally: x11.XCloseDisplay(display)"
        )
        cap_sys_time_probe = (
            "import re; status=open('/proc/self/status',encoding='ascii').read(); "
            "cap_eff=re.search(r'^CapEff:\\s*([0-9a-fA-F]+)$',status,re.MULTILINE); "
            "seccomp=re.search(r'^Seccomp:\\s*([0-9]+)$',status,re.MULTILINE); "
            "\nif cap_eff is None or seccomp is None: raise RuntimeError('capability evidence unavailable')\n"
            "\nif not int(cap_eff.group(1),16) & (1 << 25): raise PermissionError('CAP_SYS_TIME is not effective')\n"
            "\nif int(seccomp.group(1)) != 0: raise PermissionError('clock syscall policy cannot be proven')"
        )
        appimage_mount_probe = "\n".join(
            (
                "set -euo pipefail",
                "test -c /dev/fuse",
                "command -v mountpoint >/dev/null",
                "if command -v fusermount3 >/dev/null 2>&1; then unmount_helper=fusermount3; "
                "elif command -v fusermount >/dev/null 2>&1; then unmount_helper=fusermount; else exit 127; fi",
                "appimage=$(find /opt/osworld-appimages -maxdepth 1 -type f -name '*.AppImage' -perm -u+x "
                "-print -quit 2>/dev/null)",
                'test -n "$appimage"',
                "probe_dir=$(mktemp -d)",
                "mount_path=",
                "mount_pid=",
                "cleanup() {",
                "  set +e",
                '  if [ -n "$mount_path" ] && mountpoint -q "$mount_path"; then "$unmount_helper" -u "$mount_path"; fi',
                '  if [ -n "$mount_pid" ] && kill -0 "$mount_pid" 2>/dev/null; then kill "$mount_pid"; fi',
                '  if [ -n "$mount_pid" ]; then wait "$mount_pid" 2>/dev/null; fi',
                '  rm -rf "$probe_dir"',
                "}",
                "trap cleanup EXIT HUP INT TERM",
                '"$appimage" --appimage-mount >"$probe_dir/stdout" 2>"$probe_dir/stderr" &',
                "mount_pid=$!",
                "for ((attempt=0; attempt<100; attempt++)); do",
                '  mount_path=$(head -n 1 "$probe_dir/stdout" 2>/dev/null || true)',
                '  if [ -n "$mount_path" ] && mountpoint -q "$mount_path"; then break; fi',
                '  if ! kill -0 "$mount_pid" 2>/dev/null; then cat "$probe_dir/stderr" >&2; exit 1; fi',
                "  sleep 0.05",
                "done",
                'test -n "$mount_path"',
                'mountpoint -q "$mount_path"',
                'test -x "$mount_path/AppRun"',
                '"$unmount_helper" -u "$mount_path"',
                "for ((attempt=0; attempt<100; attempt++)); do",
                '  if ! mountpoint -q "$mount_path"; then break; fi',
                "  sleep 0.05",
                "done",
                'if mountpoint -q "$mount_path"; then exit 1; fi',
                'if kill -0 "$mount_pid" 2>/dev/null; then kill "$mount_pid"; fi',
                'wait "$mount_pid" 2>/dev/null || true',
                "mount_pid=",
            )
        )

        def pinned_snap_probe(*, snap_name: str, revision: str) -> str:
            snap_mount = f"/snap/{snap_name}/{revision}"
            return "\n".join(
                (
                    "set -euo pipefail",
                    'test "$(cat /proc/1/comm)" = systemd',
                    'test "$(systemctl show --property=LoadState --value snapd.service)" = loaded',
                    "systemctl is-active --quiet snapd.socket",
                    f"active_revision=$(timeout 20s snap list {snap_name} | awk 'NR == 2 {{ print $3 }}')",
                    f'test "$active_revision" = {revision}',
                    f"snap_mount={snap_mount}",
                    f'test "$(readlink -f /snap/{snap_name}/current)" = "$snap_mount"',
                    'mountpoint -q "$snap_mount"',
                    'test "$(findmnt --noheadings --output FSTYPE --target "$snap_mount" | xargs)" = squashfs',
                    f"test -e /snap/bin/{snap_name}",
                    "probe_root=$(mktemp -d)",
                    "trap 'rm -rf \"$probe_root\"' EXIT HUP INT TERM",
                    'mkdir -p "$probe_root/home" "$probe_root/config" "$probe_root/data"',
                    'HOME="$probe_root/home" XDG_CONFIG_HOME="$probe_root/config" XDG_DATA_HOME="$probe_root/data" '
                    f"timeout 20s snap run --shell {snap_name} -c "
                    f'\'test "$SNAP_REVISION" = {revision} && test "$SNAP" = {snap_mount}\'',
                )
            )

        pinned_firefox_snap_probe = pinned_snap_probe(snap_name="firefox", revision="4793")
        pinned_audacity_snap_probe = pinned_snap_probe(snap_name="audacity", revision="1212")
        wine_dxvk_probe = "\n".join(
            (
                "set -euo pipefail",
                "probe_root=$(mktemp -d)",
                'export HOME="$probe_root/home"',
                'export XDG_CONFIG_HOME="$probe_root/config"',
                'export XDG_DATA_HOME="$probe_root/data"',
                'export WINEPREFIX="$probe_root/prefix"',
                "export WINEARCH=win64",
                "export WINEDEBUG=-all",
                "export WINEDLLOVERRIDES=winemenubuilder.exe=d",
                'mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME"',
                "cleanup() {",
                "  set +e",
                '  WINEPREFIX="$WINEPREFIX" wineserver -k >/dev/null 2>&1',
                '  WINEPREFIX="$WINEPREFIX" timeout 10s wineserver -w >/dev/null 2>&1',
                '  rm -rf "$probe_root"',
                "}",
                "trap cleanup EXIT HUP INT TERM",
                "timeout 45s wineboot -u >/dev/null",
                "dxvk_setup=$(command -v setup_dxvk.sh || command -v setup_dxvk || true)",
                'if [ -z "$dxvk_setup" ]; then for candidate in /usr/share/dxvk/setup_dxvk.sh '
                '/usr/lib/dxvk/setup_dxvk.sh; do if [ -x "$candidate" ]; then dxvk_setup=$candidate; break; fi; done; fi',
                'test -n "$dxvk_setup"',
                'timeout 45s "$dxvk_setup" install --symlink >/dev/null',
                'dxvk_dll="$WINEPREFIX/drive_c/windows/system32/dxgi.dll"',
                'test -r "$dxvk_dll"',
                'strings "$dxvk_dll" | grep -q DXVK',
                'WINEDLLOVERRIDES="dxgi=n;winemenubuilder.exe=d" timeout 45s wine rundll32.exe '
                "dxgi.dll,DXGIDeclareAdapterRemovalSupport >/dev/null",
                "timeout 30s vulkaninfo --summary >/dev/null",
            )
        )
        docker_compose_probe = "\n".join(
            (
                "set -euo pipefail",
                "probe_dir=$(mktemp -d)",
                'project="northcua-capability-$$-$RANDOM"',
                "cleanup() {",
                "  set +e",
                '  docker compose --project-name "$project" --file "$probe_dir/compose.yaml" '
                "down --remove-orphans >/dev/null 2>&1",
                '  rm -rf "$probe_dir"',
                "}",
                "trap cleanup EXIT HUP INT TERM",
                "printf '%s\\n' 'services:' '  probe:' '    image: hello-world:latest' "
                "'    pull_policy: never' >\"$probe_dir/compose.yaml\"",
                "docker compose version >/dev/null",
                'docker compose --project-name "$project" --file "$probe_dir/compose.yaml" config --quiet',
                'timeout 45s docker compose --project-name "$project" --file "$probe_dir/compose.yaml" '
                "run --rm --no-deps --pull never probe >/dev/null",
                'docker compose --project-name "$project" --file "$probe_dir/compose.yaml" '
                "down --remove-orphans >/dev/null",
            )
        )
        desktop_list_names = (
            "northcua_desktop_exec dbus-send --session --print-reply=literal --dest=org.freedesktop.DBus "
            "/org/freedesktop/DBus org.freedesktop.DBus.ListNames >/dev/null"
        )
        return {
            GuestCapability.DESKTOP: _ProbeDefinition(
                "desktop-session-display",
                ('northcua_desktop_exec "$northcua_xdotool_path" getdisplaygeometry >/dev/null',),
                True,
            ),
            GuestCapability.GUI: _ProbeDefinition(
                "x11-display-roundtrip",
                ('northcua_desktop_exec "$northcua_xdotool_path" getdisplaygeometry >/dev/null',),
                True,
            ),
            GuestCapability.SHELL: _ProbeDefinition("bash-execution", ("bash -uc 'test -n \"$BASH_VERSION\"'",)),
            GuestCapability.NETWORK: _ProbeDefinition("network-socket", (f"{python} -c {quote(network_probe)}",)),
            GuestCapability.INTERNET: _ProbeDefinition("internet-egress", (f"{python} -c {quote(internet_probe)}",)),
            GuestCapability.DBUS_SESSION: _ProbeDefinition("dbus-session-call", (desktop_list_names,), True),
            GuestCapability.ACCESSIBILITY: _ProbeDefinition(
                "at-spi-bus-call",
                (
                    'northcua_require_desktop_session && timeout 20s runuser -u "$northcua_launch_user" -- '
                    'dbus-send --bus="$launch_at_spi" --print-reply=literal --dest=org.freedesktop.DBus '
                    "/org/freedesktop/DBus org.freedesktop.DBus.ListNames >/dev/null",
                ),
                True,
            ),
            GuestCapability.CLIPBOARD: _ProbeDefinition(
                "x11-clipboard-selection-query",
                (f"northcua_desktop_exec {python} -c {quote(clipboard_probe)}",),
                True,
            ),
            GuestCapability.AUDIO: _ProbeDefinition(
                "audio-server-call",
                (
                    "northcua_desktop_shell "
                    + quote(
                        "if command -v pactl >/dev/null 2>&1; then pactl info >/dev/null; "
                        "elif command -v wpctl >/dev/null 2>&1; then wpctl status >/dev/null; else exit 127; fi"
                    ),
                ),
                True,
            ),
            GuestCapability.FUSE: _ProbeDefinition(
                "fuse-appimage-mount-unmount",
                (f"timeout 20s bash -lc {quote(appimage_mount_probe)}",),
            ),
            GuestCapability.APPIMAGE_FUSE: _ProbeDefinition(
                "appimage-fuse-user-mount-unmount",
                ("northcua_desktop_shell " + quote(appimage_mount_probe),),
                True,
            ),
            GuestCapability.SNAP: _ProbeDefinition("snap-daemon-call", ("timeout 20s snap list >/dev/null",)),
            GuestCapability.SNAP_SYSTEMD: _ProbeDefinition(
                "snap-systemd-unpinned-fail-closed",
                (
                    'test "$(cat /proc/1/comm)" = systemd',
                    'test "$(systemctl show --property=LoadState --value snapd.service)" = loaded',
                    "systemctl is-active --quiet snapd.socket",
                    "timeout 20s snap list >/dev/null",
                    "printf 'snap.systemd requires an app-and-revision-specific capability id\\n' >&2",
                    "false",
                ),
            ),
            GuestCapability.SNAP_SYSTEMD_FIREFOX_4793: _ProbeDefinition(
                "snap-firefox-4793-pinned-mount-launch",
                (f"timeout 45s bash -lc {quote(pinned_firefox_snap_probe)}",),
            ),
            GuestCapability.SNAP_SYSTEMD_AUDACITY_1212: _ProbeDefinition(
                "snap-audacity-1212-pinned-mount-launch",
                (f"timeout 45s bash -lc {quote(pinned_audacity_snap_probe)}",),
            ),
            GuestCapability.FLATPAK_PORTAL: _ProbeDefinition(
                "flatpak-portal-ping",
                (
                    "northcua_desktop_exec dbus-send --session --print-reply=literal "
                    "--dest=org.freedesktop.portal.Desktop /org/freedesktop/portal/desktop "
                    "org.freedesktop.DBus.Peer.Ping >/dev/null",
                ),
                True,
            ),
            GuestCapability.SYSTEMD: _ProbeDefinition(
                "systemd-manager-call",
                (
                    'test "$(cat /proc/1/comm)" = systemd',
                    "systemctl list-units --no-legend --no-pager >/dev/null",
                ),
            ),
            GuestCapability.CGROUPS: _ProbeDefinition(
                "cgroup-filesystem",
                (
                    "test -r /proc/self/cgroup",
                    "northcua_cgroup_fs=$(stat --file-system --format=%T /sys/fs/cgroup)",
                    'case "$northcua_cgroup_fs" in cgroup2fs) test -r /sys/fs/cgroup/cgroup.controllers ;; '
                    "tmpfs) findmnt --noheadings --types cgroup --output TARGET | "
                    "grep -q '^/sys/fs/cgroup/' ;; *) false ;; esac",
                ),
            ),
            GuestCapability.NESTED_DOCKER: _ProbeDefinition(
                "nested-docker-container",
                (
                    "timeout 20s docker info >/dev/null",
                    "timeout 45s docker run --rm --pull=never hello-world >/dev/null",
                ),
            ),
            GuestCapability.DOCKER_COMPOSE: _ProbeDefinition(
                "docker-compose-container",
                (f"timeout 90s bash -lc {quote(docker_compose_probe)}",),
            ),
            GuestCapability.FANOTIFY: _ProbeDefinition(
                "fanotify-init-syscall", (f"{python} -c {quote(fanotify_probe)}",)
            ),
            GuestCapability.I386: _ProbeDefinition("i386-elf-execution", (f"{python} -c {quote(i386_probe)}",)),
            GuestCapability.WINE_DXVK: _ProbeDefinition(
                "wine-dxvk-isolated-prefix",
                ("northcua_desktop_shell " + quote(wine_dxvk_probe),),
                True,
            ),
            GuestCapability.OPENGL: _ProbeDefinition(
                "opengl-context",
                ("northcua_desktop_exec glxinfo -B >/dev/null",),
                True,
            ),
            GuestCapability.VULKAN: _ProbeDefinition(
                "vulkan-device",
                ("northcua_desktop_exec vulkaninfo --summary >/dev/null",),
                True,
            ),
            GuestCapability.CAP_SYS_TIME: _ProbeDefinition(
                "effective-cap-sys-time",
                (f"{python} -c {quote(cap_sys_time_probe)}",),
            ),
            GuestCapability.CAP_SYS_ADMIN: _ProbeDefinition(
                "bind-mount-syscall",
                (
                    "northcua_mount_dir=$(mktemp -d)",
                    'trap \'mountpoint -q "$northcua_mount_dir" && umount "$northcua_mount_dir"; '
                    'rmdir "$northcua_mount_dir"\' RETURN',
                    'mount --bind "$northcua_mount_dir" "$northcua_mount_dir"',
                    'umount "$northcua_mount_dir"',
                    'rmdir "$northcua_mount_dir"',
                    "trap - RETURN",
                ),
            ),
            GuestCapability.DISK_FREE_1500_MB: _ProbeDefinition(
                "disk-free-home-filesystem-1500mb",
                (
                    "findmnt --noheadings --output TARGET,SOURCE,FSTYPE --target /home/user",
                    f"{python} -c "
                    + quote(
                        "import os; stat=os.statvfs('/home/user'); available=stat.f_bavail*stat.f_frsize//1024; "
                        "print(available); raise SystemExit(available < 1500000)"
                    ),
                ),
            ),
            GuestCapability.DISK_FREE_1800_MB: _ProbeDefinition(
                "disk-free-home-filesystem-1800mb",
                (
                    "findmnt --noheadings --output TARGET,SOURCE,FSTYPE --target /home/user",
                    f"{python} -c "
                    + quote(
                        "import os; stat=os.statvfs('/home/user'); available=stat.f_bavail*stat.f_frsize//1024; "
                        "print(available); raise SystemExit(available < 1800000)"
                    ),
                ),
            ),
        }


def parse_guest_capability_evidence(value: str) -> list[GuestCapabilityProbeEvidence]:
    """Extract and validate guest-capability JSON records from command errors."""

    return [GuestCapabilityProbeEvidence.model_validate(item) for item in guest_capability_evidence_payloads(value)]
