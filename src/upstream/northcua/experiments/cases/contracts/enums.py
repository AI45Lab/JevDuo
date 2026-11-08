"""Enums that keep case-suite specs stable across runners and reports."""

from __future__ import annotations

import re
from enum import Enum

_PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class OpenProfileId(str, Enum):
    """String enum that keeps built-ins typed while permitting catalog extensions."""

    @classmethod
    def _missing_(cls, value: object) -> "OpenProfileId | None":
        if not isinstance(value, str) or not _PROFILE_ID_PATTERN.fullmatch(value):
            return None
        pseudo = str.__new__(cls, value)
        pseudo._name_ = f"CUSTOM_{value.upper().replace('.', '_').replace('-', '_')}"
        pseudo._value_ = value
        return pseudo


class TemplateProfileId(OpenProfileId):
    """Known E2B template profile families used by the case catalog."""

    BASE_RUNTIME = "base-runtime"
    OFFICE_WEB = "office-web"
    CREATIVE = "creative"
    DEV = "dev"
    DESKTOP_FULL = "desktop-full"


class CaseRunMode(str, Enum):
    """Execution mode for case-suite reports and resume semantics."""

    DRY_RUN = "dry_run"
    LIVE = "live"


class CaseSetupPhase(str, Enum):
    """Ordered phases in the per-case sandbox startup contract."""

    SETUP = "setup"
    INIT = "init"


class CaseDisposition(str, Enum):
    """Whether a case may enter the sandbox allocation path."""

    RUNNABLE = "runnable"
    BLOCKED = "blocked"


class CaseDispositionReasonCode(OpenProfileId):
    """Stable audit code explaining a non-runnable case disposition."""

    REQUIRED_EVIDENCE_UNAVAILABLE = "required_evidence_unavailable"
    REQUIRED_CAPABILITY_UNAVAILABLE = "required_capability_unavailable"


class CaseEvidenceAvailability(str, Enum):
    """Audited availability of evidence required to run a case."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class CaseEvidenceKind(OpenProfileId):
    """Known evidence families used by case disposition audits."""

    RELEASE_ASSET = "release_asset"
    RELEASE_HASH = "release_hash"
    CAPABILITY_PROBE = "capability_probe"


class PostRunAcceptancePolicy(str, Enum):
    """How primary post-run hook results affect case acceptance."""

    REPORT_ONLY = "report_only"
    REQUIRE_PRIMARY_PASS = "require_primary_pass"


class CaseExpectedFeasibility(str, Enum):
    """Expected feasibility semantics for benchmark-style cases."""

    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"


class CaseSourceKind(str, Enum):
    """External case source formats accepted by a case suite."""

    FILE = "file"
    DIRECTORY = "directory"
    JSONL = "jsonl"


class SandboxProviderId(OpenProfileId):
    """Sandbox providers currently supported by case-suite specs."""

    E2B = "e2b"
    FAKE = "fake"


class RuntimeInjectionMode(str, Enum):
    """How NorthCUA runtime dependencies reach a sandbox template."""

    BAKED_IN = "baked_in"
    UPLOAD_WHEELS = "upload_wheels"


class AppProvisioningMode(str, Enum):
    """How an app becomes available in a selected sandbox template."""

    BAKED = "baked"
    TASK_SETUP = "task_setup"


class AppProfileId(OpenProfileId):
    """Known app profiles for first-party case recipes."""

    BROWSER_CHROME = "browser.chrome"
    BROWSER_FIREFOX = "browser.firefox"
    LIBREOFFICE_CALC = "libreoffice.calc"
    LIBREOFFICE_WRITER = "libreoffice.writer"
    LIBREOFFICE_IMPRESS = "libreoffice.impress"
    GIMP = "creative.gimp"
    INKSCAPE = "creative.inkscape"
    FILE_MANAGER = "desktop.file_manager"
    TEXT_EDITOR = "desktop.text_editor"
    IMAGE_VIEWER = "desktop.image_viewer"
    PDF_VIEWER = "desktop.pdf_viewer"
    VLC = "media.vlc"
    THUNDERBIRD = "mail.thunderbird"
    DEV_TERMINAL = "dev.terminal"
    VS_CODE = "dev.vscode"
    DESKTOP_GENERIC = "desktop.generic"


class FixtureRole(str, Enum):
    """Semantic role for a fixture in a task bundle."""

    INPUT = "input"
    SUPPORT = "support"
    SETUP_SCRIPT = "setup_script"
    EXPECTED = "expected"


class FixtureVisibility(str, Enum):
    """Execution boundary allowed to consume a fixture."""

    GUEST = "guest"
    EVALUATOR = "evaluator"
    HOST = "host"


class LargeArtifactDeliveryMode(str, Enum):
    """Supported delivery paths for content-addressed large fixtures."""

    GUEST_DOWNLOAD = "guest_download"
    CHUNKED_UPLOAD = "chunked_upload"


class FixtureSourceKind(str, Enum):
    """Where a fixture's content comes from before upload."""

    SOURCE = "source"
    URL = "url"
    GENERATED = "generated"


class FixtureGeneratorId(str, Enum):
    """Deterministic fixture generators available in the materializer."""

    HTML_SIMPLE_PAGE = "html.simple_page"
    SPREADSHEET_CSV = "spreadsheet.csv"
    SPREADSHEET_XLSX = "spreadsheet.xlsx"
    TEXT_FILE = "text.file"
    IMAGE_PPM = "image.ppm"
    SETUP_SCRIPT = "setup.script"


class GuestCapability(OpenProfileId):
    """Capability required inside a guest, separate from worker protocol tools.

    The enum is open so benchmark-specific catalogs can add stable capability
    ids without extending the controller/device protocol capability enum.
    """

    DESKTOP = "desktop"
    GUI = "gui"
    SHELL = "shell"
    NETWORK = "network"
    INTERNET = "internet"
    DBUS_SESSION = "desktop.dbus"
    ACCESSIBILITY = "desktop.accessibility"
    CLIPBOARD = "desktop.clipboard"
    AUDIO = "desktop.audio"
    FUSE = "linux.fuse"
    APPIMAGE_FUSE = "appimage.fuse"
    SNAP = "linux.snap"
    SNAP_SYSTEMD = "snap.systemd"
    SNAP_SYSTEMD_FIREFOX_4793 = "snap.systemd.firefox.4793"
    SNAP_SYSTEMD_AUDACITY_1212 = "snap.systemd.audacity.1212"
    FLATPAK_PORTAL = "desktop.flatpak_portal"
    SYSTEMD = "systemd"
    CGROUPS = "cgroups"
    NESTED_DOCKER = "nested.docker"
    DOCKER_COMPOSE = "docker.compose"
    FANOTIFY = "kernel.fanotify"
    I386 = "linux.i386"
    WINE_DXVK = "graphics.dxvk"
    OPENGL = "graphics.opengl"
    VULKAN = "graphics.vulkan"
    CAP_SYS_TIME = "linux.cap_sys_time"
    CAP_SYS_ADMIN = "kernel.cap_sys_admin"
    DISK_FREE_1500_MB = "disk.free.1500mb"
    DISK_FREE_1800_MB = "disk.free.1800mb"


class RecipeKind(str, Enum):
    """Allowlisted initialization and readiness recipes."""

    OPEN_FILE = "open_file"
    OPEN_URL = "open_url"
    OPEN_BLANK_APP = "open_blank_app"
    OPEN_DIRECTORY = "open_directory"
    LAUNCH_COMMAND = "launch_command"
    RUN_SETUP_SCRIPT = "run_setup_script"
    WAIT_WINDOW = "wait_window"
    ASSERT_BINARY = "assert_binary"
    ASSERT_FILE_EXISTS = "assert_file_exists"
