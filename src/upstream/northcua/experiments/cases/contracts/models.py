"""Pydantic models for case-suite materialization."""

from __future__ import annotations

import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

from northcua.batch.contracts import BatchRunResult
from northcua.batch.post_run.contracts import PostRunHookSpec
from northcua.batch.preparation.contracts import PreparedSandboxPolicy
from northcua.experiments.cases.contracts.enums import (
    AppProfileId,
    AppProvisioningMode,
    CaseDisposition,
    CaseDispositionReasonCode,
    CaseEvidenceAvailability,
    CaseEvidenceKind,
    CaseExpectedFeasibility,
    CaseRunMode,
    CaseSetupPhase,
    CaseSourceKind,
    FixtureGeneratorId,
    FixtureRole,
    FixtureSourceKind,
    FixtureVisibility,
    GuestCapability,
    LargeArtifactDeliveryMode,
    PostRunAcceptancePolicy,
    RecipeKind,
    RuntimeInjectionMode,
    SandboxProviderId,
    TemplateProfileId,
)
from northcua.experiments.contracts import ExperimentControllerSpec, TraceExpectationSpec
from northcua.sandboxes.contracts import (
    SandboxCommandSpec,
    SandboxProviderSpec,
    SandboxResourceSpec,
    is_non_routable_e2b_template,
)
from northcua_protocol.capabilities import Capability, JsonObject

_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_LINUX_USER_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\$?$")
_LINUX_PRINCIPAL_PATTERN = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_.-]{0,31}\$?|[0-9]+)$")
_SAFE_REMOTE_PREFIXES = (
    "/tmp/northcua-",
    "/home/user/project/",
    "/home/user/Projects/",
    "/home/user/Desktop/",
    "/home/oai/share/",
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT_FIXTURE_BYTES = 1024 * 1024
_MAX_SCRIPT_COMMANDS = 50
_MAX_SCRIPT_COMMAND_CHARS = 2000
_MAX_SPREADSHEET_ROWS = 500
_MAX_SPREADSHEET_COLUMNS = 64
_MAX_CELL_CHARS = 1024


class StrictCaseModel(BaseModel):
    """Base model for case IR that rejects accidental schema drift.

    Attributes:
        model_config: Pydantic config shared by typed case contracts.
    """

    model_config: ConfigDict = ConfigDict(extra="forbid")


class CaseRequiredEvidenceSpec(StrictCaseModel):
    """One auditable input or proof required before a case can run."""

    evidence_id: str
    kind: CaseEvidenceKind
    locator: str
    source: str = ""
    availability: CaseEvidenceAvailability
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str = ""
    detail: str = ""

    @model_validator(mode="after")
    def _validate_evidence(self) -> "CaseRequiredEvidenceSpec":
        if not _SLUG_PATTERN.fullmatch(self.evidence_id):
            raise ValueError("case evidence_id must be a path-safe slug")
        if not self.locator.strip():
            raise ValueError("case disposition evidence locator cannot be empty")
        if self.sha256 and not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("case disposition evidence sha256 must be a 64-character lowercase sha256")
        return self


class CaseDispositionSpec(StrictCaseModel):
    """Pre-allocation decision plus the evidence needed to audit it."""

    kind: CaseDisposition = CaseDisposition.RUNNABLE
    reason_code: CaseDispositionReasonCode | None = None
    reason: str = ""
    required_capabilities: set[GuestCapability] = Field(default_factory=set)
    required_evidence: list[CaseRequiredEvidenceSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_disposition(self) -> "CaseDispositionSpec":
        has_audit_details = bool(
            self.reason_code or self.reason or self.required_capabilities or self.required_evidence
        )
        if self.kind == CaseDisposition.RUNNABLE:
            if has_audit_details:
                raise ValueError("runnable case disposition cannot declare blocker audit details")
            return self
        if self.reason_code is None:
            raise ValueError("blocked case disposition requires reason_code")
        if not self.reason.strip():
            raise ValueError("blocked case disposition requires reason")
        if not self.required_capabilities and not self.required_evidence:
            raise ValueError("blocked case disposition requires capabilities or evidence")
        return self

    @property
    def is_runnable(self) -> bool:
        """Return whether the case may be projected into a batch manifest."""

        return self.kind == CaseDisposition.RUNNABLE


class HtmlSimplePageParams(StrictCaseModel):
    """Parameters for deterministic HTML page fixtures."""

    title: str = Field(default="NorthCUA Case Fixture", max_length=200)
    heading: str = Field(default="", max_length=200)
    body: str = Field(default="This page is a deterministic case fixture.", max_length=20_000)


class TextFileParams(StrictCaseModel):
    """Parameters for deterministic UTF-8 text fixtures."""

    text: str = Field(default="NorthCUA deterministic text fixture.\n", max_length=_MAX_TEXT_FIXTURE_BYTES)


CellValue = str | int | float | bool


class SpreadsheetRowsParams(StrictCaseModel):
    """Parameters for deterministic CSV/XLSX spreadsheet fixtures."""

    rows: list[list[CellValue]] = Field(
        default_factory=lambda: [["item", "value"], ["alpha", "12"], ["beta", "18"], ["gamma", "30"]]
    )

    @model_validator(mode="after")
    def _validate_rows(self) -> "SpreadsheetRowsParams":
        if len(self.rows) > _MAX_SPREADSHEET_ROWS:
            raise ValueError(f"spreadsheet fixture rows cannot exceed {_MAX_SPREADSHEET_ROWS}")
        for row in self.rows:
            if len(row) > _MAX_SPREADSHEET_COLUMNS:
                raise ValueError(f"spreadsheet fixture columns cannot exceed {_MAX_SPREADSHEET_COLUMNS}")
            for value in row:
                if len(str(value)) > _MAX_CELL_CHARS:
                    raise ValueError(f"spreadsheet fixture cell text cannot exceed {_MAX_CELL_CHARS} characters")
        return self


class ImagePpmParams(StrictCaseModel):
    """Parameters for deterministic PPM image fixtures."""

    width: int = Field(default=12, ge=1, le=256)
    height: int = Field(default=12, ge=1, le=256)
    first: tuple[int, int, int] = (220, 30, 70)
    second: tuple[int, int, int] = (40, 120, 220)

    @field_validator("first", "second")
    @classmethod
    def _validate_rgb(cls, value: tuple[int, int, int]) -> tuple[int, int, int]:
        if any(channel < 0 or channel > 255 for channel in value):
            raise ValueError("image fixture RGB channels must be between 0 and 255")
        return value


class SetupScriptParams(StrictCaseModel):
    """Parameters for deterministic setup-script fixtures."""

    commands: list[str] = Field(default_factory=lambda: ["true"], min_length=1, max_length=_MAX_SCRIPT_COMMANDS)

    @field_validator("commands")
    @classmethod
    def _validate_commands(cls, commands: list[str]) -> list[str]:
        for command in commands:
            if not command.strip():
                raise ValueError("setup script commands cannot be empty")
            if len(command) > _MAX_SCRIPT_COMMAND_CHARS:
                raise ValueError(f"setup script command cannot exceed {_MAX_SCRIPT_COMMAND_CHARS} characters")
            if "\x00" in command:
                raise ValueError("setup script command cannot contain NUL bytes")
        return commands


FixtureGeneratorParams = Annotated[
    HtmlSimplePageParams | TextFileParams | SpreadsheetRowsParams | ImagePpmParams | SetupScriptParams,
    Field(union_mode="left_to_right"),
]


_GENERATOR_PARAM_MODELS: dict[FixtureGeneratorId, type[BaseModel]] = {
    FixtureGeneratorId.HTML_SIMPLE_PAGE: HtmlSimplePageParams,
    FixtureGeneratorId.TEXT_FILE: TextFileParams,
    FixtureGeneratorId.SPREADSHEET_CSV: SpreadsheetRowsParams,
    FixtureGeneratorId.SPREADSHEET_XLSX: SpreadsheetRowsParams,
    FixtureGeneratorId.IMAGE_PPM: ImagePpmParams,
    FixtureGeneratorId.SETUP_SCRIPT: SetupScriptParams,
}


class GeneratedFixtureSpec(StrictCaseModel):
    """Deterministic fixture generated by NorthCUA before a case starts."""

    generator: FixtureGeneratorId
    params: FixtureGeneratorParams

    @model_validator(mode="before")
    @classmethod
    def _coerce_params(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        generator = FixtureGeneratorId(data.get("generator"))
        params_model = _GENERATOR_PARAM_MODELS[generator]
        return {**data, "params": params_model.model_validate(data.get("params") or {})}


class LargeArtifactFixtureSpec(StrictCaseModel):
    """Content address and bounded delivery policy for a large guest fixture."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    delivery: LargeArtifactDeliveryMode
    chunk_size_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=50 * 1024 * 1024)


class LargeArtifactDeliveryEvidence(StrictCaseModel):
    """Auditable binding from a trusted large artifact to its guest consumer."""

    task_id: str
    app_profile: AppProfileId
    fixture_remote_path: str
    source_kind: Literal["url", "local_path"]
    source: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    delivery: LargeArtifactDeliveryMode
    chunk_size_bytes: int = Field(ge=1, le=50 * 1024 * 1024)
    host_verification: Literal[
        "sha256_and_size_verified",
        "endpoint_and_size_verified",
        "endpoint_verified",
    ]
    guest_verification: Literal["sha256_and_size_before_init"] = "sha256_and_size_before_init"

    @model_validator(mode="after")
    def _validate_binding(self) -> "LargeArtifactDeliveryEvidence":
        if not _SLUG_PATTERN.fullmatch(self.task_id):
            raise ValueError("large artifact evidence task_id must be a path-safe slug")
        _validate_safe_remote_path(self.fixture_remote_path)
        if not self.source.strip():
            raise ValueError("large artifact evidence source cannot be empty")
        return self


class MaterializedFixtureReference(StrictCaseModel):
    """JSON-safe host/evaluator reference to materialized fixture bytes."""

    local_path: Path | None = None
    remote_path: str
    visibility: FixtureVisibility
    role: FixtureRole
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_consumer_location(self) -> "MaterializedFixtureReference":
        if self.visibility != FixtureVisibility.GUEST and self.local_path is None:
            raise ValueError("host/evaluator fixture references require a local_path")
        return self


class FixtureSpec(StrictCaseModel):
    """One fixture staged into a sandbox before recipe startup commands run."""

    source_kind: FixtureSourceKind = FixtureSourceKind.SOURCE
    source_path: Path | None = Field(default=None, validation_alias=AliasChoices("source_path", "source"))
    url: str = ""
    generated: GeneratedFixtureSpec | None = None
    large_artifact: LargeArtifactFixtureSpec | None = Field(
        default=None,
        validation_alias=AliasChoices("large_artifact", "artifact"),
    )
    remote_path: str
    mode: str = "0644"
    owner: str = ""
    group: str = ""
    parent_mode: str = ""
    parent_owner: str = ""
    parent_group: str = ""
    visibility: FixtureVisibility = FixtureVisibility.GUEST
    role: FixtureRole = FixtureRole.INPUT
    sha256: str = ""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @model_validator(mode="after")
    def _validate_fixture_source(self) -> "FixtureSpec":
        _validate_safe_remote_path(self.remote_path)
        _validate_octal_mode(self.mode, field_name="fixture mode")
        _validate_octal_mode(self.parent_mode, field_name="fixture parent_mode")
        if any((self.parent_mode, self.parent_owner, self.parent_group)):
            parent_path = str(PurePosixPath(self.remote_path).parent)
            if not _is_safe_remote_path(parent_path):
                raise ValueError("fixture parent ownership/mode cannot target a shared system directory")
        source_count = sum(bool(item) for item in (self.source_path, self.url, self.generated))
        if source_count != 1:
            raise ValueError("fixture requires exactly one source_path, url, or generated")
        expected_kind = (
            FixtureSourceKind.GENERATED
            if self.generated is not None
            else FixtureSourceKind.URL
            if self.url
            else FixtureSourceKind.SOURCE
        )
        if self.source_kind != expected_kind:
            self.source_kind = expected_kind
        if self.large_artifact is not None:
            self._validate_large_artifact()
        if self.source_kind == FixtureSourceKind.URL and not self.sha256:
            raise ValueError("url fixtures require an expected sha256")
        if self.source_kind == FixtureSourceKind.URL and urlparse(self.url).scheme not in {"http", "https"}:
            raise ValueError("url fixtures only support http:// and https:// sources")
        if self.role == FixtureRole.EXPECTED and self.visibility == FixtureVisibility.GUEST:
            raise ValueError("expected fixtures must use evaluator or host visibility")
        if self.sha256 and not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("fixture sha256 must be a 64-character lowercase sha256")
        return self

    def _validate_large_artifact(self) -> None:
        artifact = self.large_artifact
        assert artifact is not None
        if self.visibility != FixtureVisibility.GUEST:
            raise ValueError("large artifact delivery is only valid for guest-visible fixtures")
        if self.sha256 and self.sha256 != artifact.sha256:
            raise ValueError("large artifact sha256 must match fixture sha256")
        self.sha256 = artifact.sha256
        if artifact.delivery == LargeArtifactDeliveryMode.GUEST_DOWNLOAD and not self.url:
            raise ValueError("guest_download large artifacts require a URL source")
        if artifact.delivery == LargeArtifactDeliveryMode.CHUNKED_UPLOAD and self.source_path is None:
            raise ValueError("chunked_upload large artifacts require a source_path")

    @field_validator("owner", "group", "parent_owner", "parent_group")
    @classmethod
    def _validate_principal(cls, value: str) -> str:
        if value and not _LINUX_PRINCIPAL_PATTERN.fullmatch(value):
            raise ValueError("fixture owner/group values must be Linux names or numeric ids")
        return value


class OpenFileRecipeArgs(StrictCaseModel):
    """Arguments for opening a staged file with a catalog app.

    Fields:
        app: Optional app override when the case app is not the launcher.
        path: Remote sandbox path to open.
        wait_window: Optional window-title readiness hint.
        timeout_seconds: Startup readiness timeout.
    """

    app: AppProfileId | None = None
    path: str
    wait_window: str = ""
    timeout_seconds: float = Field(default=60.0, gt=0)


class OpenUrlRecipeArgs(StrictCaseModel):
    """Arguments for opening a URL with a browser-like app.

    Fields:
        app: Optional app override when the case app is not the launcher.
        url: HTTP, HTTPS, or local file URL to open.
        wait_window: Optional window-title readiness hint.
        timeout_seconds: Startup readiness timeout.
    """

    app: AppProfileId | None = None
    url: str
    wait_window: str = ""
    timeout_seconds: float = Field(default=60.0, gt=0)


class OpenBlankAppRecipeArgs(StrictCaseModel):
    """Arguments for launching an app without a document target.

    Fields:
        app: Optional app override when the case app is not the launcher.
        working_directory: Optional remote directory used by terminal-like apps.
        wait_window: Optional window-title readiness hint.
        timeout_seconds: Startup readiness timeout.
    """

    app: AppProfileId | None = None
    working_directory: str = ""
    wait_window: str = ""
    timeout_seconds: float = Field(default=60.0, gt=0)


class OpenDirectoryRecipeArgs(StrictCaseModel):
    """Arguments for opening a directory in the configured file manager.

    Fields:
        app: Optional app override when the case app is not the launcher.
        path: Remote sandbox directory path to open.
        wait_window: Optional window-title readiness hint.
        timeout_seconds: Startup readiness timeout.
    """

    app: AppProfileId | None = None
    path: str
    wait_window: str = ""
    timeout_seconds: float = Field(default=60.0, gt=0)


class LaunchCommandRecipeArgs(StrictCaseModel):
    """Arguments for launching a bounded sandbox command before the task.

    Fields:
        app: Optional app profile whose launch user and readiness hints should
            be reused.
        command: Shell command executed inside the sandbox with DISPLAY set.
        wait_window: Optional window-title readiness hint.
        process_contains: Optional process-command readiness hint.
        timeout_seconds: Startup readiness timeout.
    """

    app: AppProfileId | None = None
    command: str
    wait_window: str = ""
    process_contains: str = ""
    timeout_seconds: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _validate_launch_command(self) -> "LaunchCommandRecipeArgs":
        self.command = self.command.strip()
        if not self.command:
            raise ValueError("launch_command recipe requires command")
        if "\n" in self.command or "\r" in self.command:
            raise ValueError("launch_command recipe command must be a single shell line")
        return self


class RunSetupScriptRecipeArgs(StrictCaseModel):
    """Arguments for running a staged, checksum-guarded setup script.

    Fields:
        path: Remote sandbox path to an executable setup script.
        checksum: Optional sha256 expected before execution.
        run_as_user: Optional Linux identity used to execute the script.
        result_owner: Expected owner/group for every declared result path.
        result_paths: Guest paths whose complete trees must match result_owner.
        timeout_seconds: Maximum runtime for the setup script.
    """

    path: str
    checksum: str = ""
    run_as_user: str = ""
    result_owner: str = ""
    result_paths: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=120.0, gt=0)

    @model_validator(mode="after")
    def _validate_setup_script(self) -> "RunSetupScriptRecipeArgs":
        _validate_safe_remote_path(self.path)
        if self.checksum and not _SHA256_PATTERN.fullmatch(self.checksum):
            raise ValueError("run_setup_script checksum must be a 64-character lowercase sha256")
        for field_name, value in (("run_as_user", self.run_as_user), ("result_owner", self.result_owner)):
            if value and not _LINUX_USER_PATTERN.fullmatch(value):
                raise ValueError(f"run_setup_script {field_name} must be a valid Linux user name")
        if bool(self.result_paths) != bool(self.result_owner):
            raise ValueError("run_setup_script result_paths and result_owner must be provided together")
        for result_path in self.result_paths:
            if not result_path.startswith(("/home/user/", "/tmp/northcua-")):
                raise ValueError("run_setup_script result_paths must be under /home/user or /tmp/northcua-")
            if "\\" in result_path or any(char in result_path for char in ("\x00", "\n", "\r")):
                raise ValueError("run_setup_script result path contains unsafe characters")
            if ".." in PurePosixPath(result_path).parts:
                raise ValueError("run_setup_script result path cannot contain traversal segments")
        return self


class WaitWindowRecipeArgs(StrictCaseModel):
    """Arguments for waiting until a process or GUI window is ready.

    Fields:
        title_contains: Window-title substring used by wmctrl or xdotool.
        process_contains: Process-command substring used as a fallback.
        timeout_seconds: Readiness timeout.
    """

    title_contains: str = ""
    process_contains: str = ""
    timeout_seconds: float = Field(default=30.0, gt=0)


class AssertBinaryRecipeArgs(StrictCaseModel):
    """Arguments for asserting that a sandbox binary is installed.

    Fields:
        binary: Executable name or path to resolve with command -v.
        timeout_seconds: Probe timeout for the assertion command.
    """

    binary: str
    timeout_seconds: float = Field(default=15.0, gt=0)


class AssertFileExistsRecipeArgs(StrictCaseModel):
    """Arguments for asserting that a remote sandbox file exists.

    Fields:
        path: Remote sandbox file path to test.
        timeout_seconds: Probe timeout for the assertion command.
    """

    path: str
    timeout_seconds: float = Field(default=15.0, gt=0)


RecipeArgs = Annotated[
    OpenFileRecipeArgs
    | OpenUrlRecipeArgs
    | OpenBlankAppRecipeArgs
    | OpenDirectoryRecipeArgs
    | LaunchCommandRecipeArgs
    | RunSetupScriptRecipeArgs
    | WaitWindowRecipeArgs
    | AssertBinaryRecipeArgs
    | AssertFileExistsRecipeArgs,
    Field(union_mode="left_to_right"),
]


_RECIPE_ARG_MODELS: dict[RecipeKind, type[BaseModel]] = {
    RecipeKind.OPEN_FILE: OpenFileRecipeArgs,
    RecipeKind.OPEN_URL: OpenUrlRecipeArgs,
    RecipeKind.OPEN_BLANK_APP: OpenBlankAppRecipeArgs,
    RecipeKind.OPEN_DIRECTORY: OpenDirectoryRecipeArgs,
    RecipeKind.LAUNCH_COMMAND: LaunchCommandRecipeArgs,
    RecipeKind.RUN_SETUP_SCRIPT: RunSetupScriptRecipeArgs,
    RecipeKind.WAIT_WINDOW: WaitWindowRecipeArgs,
    RecipeKind.ASSERT_BINARY: AssertBinaryRecipeArgs,
    RecipeKind.ASSERT_FILE_EXISTS: AssertFileExistsRecipeArgs,
}


class InitRecipeSpec(StrictCaseModel):
    """One allowlisted init or readiness recipe with typed args."""

    recipe: RecipeKind
    args: RecipeArgs
    label: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_args(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        recipe = RecipeKind(data.get("recipe"))
        args_model = _RECIPE_ARG_MODELS[recipe]
        raw_args = data.get("args", {})
        return {**data, "args": args_model.model_validate(raw_args)}


class CaseSetupPhaseSpec(StrictCaseModel):
    """One ordered phase of typed case startup recipes."""

    phase: CaseSetupPhase
    recipes: list[InitRecipeSpec] = Field(min_length=1)


class CaseSetupSpec(StrictCaseModel):
    """Typed setup/init phases projected to legacy startup commands."""

    phases: list[CaseSetupPhaseSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_phases(self) -> "CaseSetupSpec":
        phase_ids = [phase.phase for phase in self.phases]
        if len(phase_ids) != len(set(phase_ids)):
            raise ValueError("case setup phases must be unique")
        expected_order = sorted(phase_ids, key=list(CaseSetupPhase).index)
        if phase_ids != expected_order:
            raise ValueError("case setup phases must be ordered as setup then init")
        return self

    def recipes_for(self, phase: CaseSetupPhase) -> list[InitRecipeSpec]:
        """Return recipes for one phase without exposing mutable internals."""

        item = next((item for item in self.phases if item.phase == phase), None)
        return list(item.recipes) if item is not None else []


class DependencyInjectionPlan(StrictCaseModel):
    """Reserved future extension point for per-query injected dependencies."""

    env: dict[str, str] = Field(default_factory=dict)
    files: list[FixtureSpec] = Field(default_factory=list)
    setup_recipes: list[InitRecipeSpec] = Field(default_factory=list)
    auth_context_refs: list[str] = Field(default_factory=list)


class RuntimeInjectionSpec(StrictCaseModel):
    """Suite-level runtime dependency delivery policy."""

    mode: RuntimeInjectionMode = RuntimeInjectionMode.BAKED_IN


class TemplateAppPostSetupSpec(StrictCaseModel):
    """Readiness probes run after task-scoped app provisioning."""

    binary_probes: list[str] = Field(default_factory=list)
    version_probes: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_probe_aliases(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        for alias, field_name in (
            ("binary", "binary_probes"),
            ("binaries", "binary_probes"),
            ("version", "version_probes"),
            ("versions", "version_probes"),
        ):
            if alias in normalized and field_name not in normalized:
                normalized[field_name] = normalized.pop(alias)
        for field_name in ("binary_probes", "version_probes"):
            value = normalized.get(field_name)
            if isinstance(value, str):
                normalized[field_name] = [value]
        return normalized

    @field_validator("binary_probes", "version_probes")
    @classmethod
    def _validate_probes(cls, values: list[str], info: ValidationInfo) -> list[str]:
        normalized: list[str] = []
        for value in values:
            probe = value.strip()
            if not probe:
                raise ValueError("template app post_setup probes cannot be empty")
            if "\x00" in probe:
                raise ValueError("template app post_setup probes cannot contain NUL bytes")
            if any(character in probe for character in ";&|<>`$\n\r"):
                raise ValueError("template app post_setup probes cannot contain shell operators")
            try:
                argv = shlex.split(probe)
            except ValueError as exc:
                raise ValueError("template app post_setup probes must use valid shell-free argv") from exc
            if not argv or shlex.join(argv) != probe:
                raise ValueError("template app post_setup probes must use canonical shell-free argv")
            if info.field_name == "binary_probes" and len(argv) != 1:
                raise ValueError("template app binary probes must name one executable")
            normalized.append(probe)
        return normalized


class TemplateAppBindingSpec(StrictCaseModel):
    """Bind an app profile to one template and its provisioning contract."""

    app_profile: AppProfileId
    provisioning_mode: AppProvisioningMode = AppProvisioningMode.BAKED
    first_run_variant: str = ""
    post_setup: TemplateAppPostSetupSpec = Field(default_factory=TemplateAppPostSetupSpec)
    required_guest_capabilities: set[GuestCapability] = Field(default_factory=set)

    @model_validator(mode="before")
    @classmethod
    def _normalize_flattened_post_setup(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        post_setup = dict(normalized.get("post_setup") or {})
        for field_name in ("binary_probes", "version_probes"):
            for flattened in (f"post_setup_{field_name}", f"post_setup_{field_name.removesuffix('s')}"):
                if flattened in normalized:
                    post_setup[field_name] = normalized.pop(flattened)
        if post_setup or "post_setup" in normalized:
            normalized["post_setup"] = post_setup
        return normalized

    @field_validator("first_run_variant")
    @classmethod
    def _validate_first_run_variant(cls, value: str) -> str:
        value = value.strip()
        if "\x00" in value:
            raise ValueError("template app first_run_variant cannot contain NUL bytes")
        return value

    @property
    def post_setup_binary_probes(self) -> list[str]:
        """Return binary probes using the flattened contract spelling."""

        return self.post_setup.binary_probes

    @property
    def post_setup_version_probes(self) -> list[str]:
        """Return version probes using the flattened contract spelling."""

        return self.post_setup.version_probes


class DesktopSessionSpec(StrictCaseModel):
    """Required Linux graphical-session identity for a desktop workflow.

    The runtime resolves environment values from a live process or logind
    session belonging to this identity. Paths and bus addresses are never
    synthesized when a required session cannot be resolved.
    """

    required: bool = True
    target_user: str = Field(
        default="",
        validation_alias=AliasChoices("target_user", "user", "launch_user"),
    )
    target_uid: int | None = Field(default=None, ge=0)
    readiness_timeout_seconds: float = Field(default=30.0, gt=0, le=300.0)

    @field_validator("target_user")
    @classmethod
    def _validate_target_user(cls, value: str) -> str:
        value = value.strip()
        if value and not _LINUX_USER_PATTERN.fullmatch(value):
            raise ValueError("desktop session target_user must be a valid Linux user name")
        return value


class TemplateProfileSpec(StrictCaseModel):
    """Template metadata used to choose sandboxes and validate readiness."""

    profile_id: TemplateProfileId
    alias: str
    provider: SandboxProviderId = SandboxProviderId.E2B
    base_template: str = ""
    desktop: bool = True
    resources: SandboxResourceSpec = Field(default_factory=SandboxResourceSpec)
    resolution: tuple[int, int] = (1280, 900)
    device_python: str = "python3.12"
    installed_apps: list[AppProfileId] = Field(default_factory=list)
    app_bindings: list[TemplateAppBindingSpec] = Field(default_factory=list)
    desktop_session: DesktopSessionSpec | None = None
    required_binaries: list[str] = Field(default_factory=list)
    required_capabilities: set[Capability] = Field(default_factory=set)
    required_guest_capabilities: set[GuestCapability] = Field(default_factory=set)
    doctor_commands: list[SandboxCommandSpec] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)

    @field_validator("alias")
    @classmethod
    def _reject_build_seed_alias(cls, value: str) -> str:
        if is_non_routable_e2b_template(value):
            raise ValueError("template profile cannot route a privileged E2B build seed")
        return value

    @model_validator(mode="before")
    @classmethod
    def _normalize_app_binding_map(cls, data: object) -> object:
        if not isinstance(data, dict) or not isinstance(data.get("app_bindings"), dict):
            return data
        normalized = dict(data)
        bindings: list[object] = []
        for app_profile, binding in normalized["app_bindings"].items():
            payload = dict(binding or {}) if isinstance(binding, dict) or binding is None else binding
            if not isinstance(payload, dict):
                raise ValueError("template app binding map values must be objects")
            bindings.append({"app_profile": app_profile, **payload})
        normalized["app_bindings"] = bindings
        return normalized

    @model_validator(mode="after")
    def _validate_app_bindings(self) -> "TemplateProfileSpec":
        app_ids = [binding.app_profile for binding in self.app_bindings]
        if len(app_ids) != len(set(app_ids)):
            raise ValueError("template app_bindings must have unique app_profile values")
        return self

    def app_binding(self, app_profile: AppProfileId) -> TemplateAppBindingSpec | None:
        """Return the explicit binding for an app, when the catalog declares one."""

        return next((binding for binding in self.app_bindings if binding.app_profile == app_profile), None)


class AppProfileSpec(StrictCaseModel):
    """Catalog entry describing one app family and its default template."""

    app_profile: AppProfileId
    template_profile: TemplateProfileId
    display_name: str
    launch_binary: str
    launch_argv: list[str] = Field(default_factory=list)
    launch_user: str = ""
    process_hint: str = ""
    window_hint: str = ""
    required_binaries: list[str] = Field(default_factory=list)
    required_capabilities: set[Capability] = Field(default_factory=set)
    required_guest_capabilities: set[GuestCapability] = Field(default_factory=set)
    default_recipe: RecipeKind = RecipeKind.OPEN_BLANK_APP
    metadata: JsonObject = Field(default_factory=dict)

    @field_validator("launch_user")
    @classmethod
    def _validate_launch_user(cls, value: str) -> str:
        if value and not _LINUX_USER_PATTERN.fullmatch(value):
            raise ValueError("app launch_user must be a valid Linux user name")
        return value


class CaseSpec(StrictCaseModel):
    """One query prepared for a real or fake batch run."""

    task_id: str
    query: str
    app_profile: AppProfileId
    template_profile: TemplateProfileId | None = None
    agent_kind: Literal["desktop", "web"] = "desktop"
    desktop_session: DesktopSessionSpec | None = None
    fixtures: list[FixtureSpec] = Field(default_factory=list)
    setup: CaseSetupSpec | None = None
    init: list[InitRecipeSpec] = Field(default_factory=list)
    pre_submit_commands: list[SandboxCommandSpec] = Field(default_factory=list)
    post_run_hooks: list[PostRunHookSpec] = Field(default_factory=list)
    max_steps: int = Field(default=100, gt=0)
    max_session_steps: int = Field(default=0, ge=0)
    max_retries: int = Field(default=0, ge=0)
    timeout_seconds: float = Field(default=900.0, gt=0)
    required_capabilities: set[Capability] = Field(default_factory=set)
    required_guest_capabilities: set[GuestCapability] = Field(default_factory=set)
    dependency_injection: DependencyInjectionPlan = Field(default_factory=DependencyInjectionPlan)
    involved_app_profiles: list[AppProfileId] = Field(default_factory=list)
    involved_app_count: int = Field(default=0, ge=0)
    disposition: CaseDispositionSpec = Field(default_factory=CaseDispositionSpec)
    expected_feasibility: CaseExpectedFeasibility = CaseExpectedFeasibility.FEASIBLE
    metadata: JsonObject = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _project_typed_setup_to_legacy(self) -> "CaseSpec":
        if self.setup is None:
            return self
        setup_recipes = self.setup.recipes_for(CaseSetupPhase.SETUP)
        init_recipes = self.setup.recipes_for(CaseSetupPhase.INIT)
        legacy_setup = self.dependency_injection.setup_recipes
        if legacy_setup and legacy_setup != setup_recipes:
            raise ValueError("typed setup phase conflicts with dependency_injection.setup_recipes")
        if self.init and self.init != init_recipes:
            raise ValueError("typed init phase conflicts with legacy init recipes")
        self.dependency_injection = self.dependency_injection.model_copy(update={"setup_recipes": setup_recipes})
        self.init = init_recipes
        return self

    @model_validator(mode="after")
    def _validate_task_id(self) -> "CaseSpec":
        if not self.task_id:
            raise ValueError("case task_id cannot be empty")
        if not _SLUG_PATTERN.fullmatch(self.task_id):
            raise ValueError("case task_id must be a path-safe slug")
        if not self.query.strip():
            raise ValueError("case query cannot be empty")
        if not self.involved_app_profiles:
            self.involved_app_profiles = [self.app_profile]
        expected_count = len(set(self.involved_app_profiles))
        if self.involved_app_count == 0:
            self.involved_app_count = expected_count
        if self.involved_app_count != expected_count:
            raise ValueError("case involved_app_count must match unique involved_app_profiles")
        return self

    @model_serializer(mode="wrap")
    def _serialize_without_empty_typed_setup(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, object]:
        serialized = handler(self)
        if not isinstance(serialized, dict):
            raise TypeError("CaseSpec serialization must produce an object")
        if self.setup is None:
            serialized.pop("setup", None)
        if self.desktop_session is None:
            serialized.pop("desktop_session", None)
        return serialized

    @property
    def setup_contract_source(self) -> Literal["typed", "legacy"]:
        """Return the authoring contract used for setup phase reporting."""

        return "typed" if self.setup is not None else "legacy"

    def effective_setup_phases(self) -> tuple[CaseSetupPhaseSpec, ...]:
        """Return canonical phases while preserving legacy command ordering."""

        if self.setup is not None:
            return tuple(self.setup.phases)
        setup_recipes = list(self.dependency_injection.setup_recipes)
        init_recipes = list(self.init)
        while init_recipes and init_recipes[0].recipe == RecipeKind.RUN_SETUP_SCRIPT:
            setup_recipes.append(init_recipes.pop(0))
        phases: list[CaseSetupPhaseSpec] = []
        if setup_recipes:
            phases.append(CaseSetupPhaseSpec(phase=CaseSetupPhase.SETUP, recipes=setup_recipes))
        if init_recipes:
            phases.append(CaseSetupPhaseSpec(phase=CaseSetupPhase.INIT, recipes=init_recipes))
        return tuple(phases)


class CaseSourceSpec(StrictCaseModel):
    """A typed source of external case definitions for a suite."""

    kind: CaseSourceKind
    path: Path
    glob: str = "*.case.yaml"
    recursive: bool = False

    @model_validator(mode="before")
    @classmethod
    def _coerce_string_ref(cls, data: object) -> object:
        if isinstance(data, str):
            return {"kind": CaseSourceKind.FILE.value, "path": data}
        return data

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: Path) -> Path:
        path_text = str(value)
        if not path_text.strip():
            raise ValueError("case source path cannot be empty")
        if "\x00" in path_text:
            raise ValueError("case source path cannot contain NUL bytes")
        return value

    @field_validator("glob")
    @classmethod
    def _validate_glob(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("case source glob cannot be empty")
        if "\x00" in value or "/" in value or "\\" in value:
            raise ValueError("case source glob must be a file-name pattern, not a path")
        return value

    @model_validator(mode="after")
    def _validate_kind_specific_fields(self) -> "CaseSourceSpec":
        if self.kind != CaseSourceKind.DIRECTORY and (self.glob != "*.case.yaml" or self.recursive):
            raise ValueError("case source glob/recursive are only valid for directory sources")
        return self


class CaseFileSpec(StrictCaseModel):
    """One reusable case asset stored outside a suite file."""

    version: Literal["v1"] = "v1"
    case: CaseSpec
    metadata: JsonObject = Field(default_factory=dict)


class CaseSuiteSpec(StrictCaseModel):
    """Root suite spec for reusable E2B case batches."""

    version: Literal["v1"] = "v1"
    suite_id: str = "case-suite"
    description: str = ""
    controller: ExperimentControllerSpec = Field(default_factory=ExperimentControllerSpec)
    sandbox: SandboxProviderSpec = Field(default_factory=SandboxProviderSpec)
    runtime: RuntimeInjectionSpec = Field(default_factory=RuntimeInjectionSpec)
    prepared_sandbox_policy: PreparedSandboxPolicy = Field(default_factory=PreparedSandboxPolicy)
    concurrency: int = Field(default=1, gt=0)
    trace_expectations: TraceExpectationSpec = Field(default_factory=TraceExpectationSpec)
    post_run_acceptance_policy: PostRunAcceptancePolicy = PostRunAcceptancePolicy.REPORT_ONLY
    case_sources: list[CaseSourceSpec] = Field(default_factory=list)
    tasks: list[CaseSpec] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_tasks(self) -> "CaseSuiteSpec":
        if not self.tasks and not self.case_sources:
            raise ValueError("case suite requires at least one task or case source")
        if not _SLUG_PATTERN.fullmatch(self.suite_id):
            raise ValueError("case suite_id must be a path-safe slug")
        seen: set[str] = set()
        duplicates: set[str] = set()
        for task in self.tasks:
            if task.task_id in seen:
                duplicates.add(task.task_id)
            seen.add(task.task_id)
        if duplicates:
            raise ValueError(f"case task_id values must be unique: {sorted(duplicates)}")
        return self


def _validate_safe_remote_path(remote_path: str) -> None:
    if not remote_path.startswith("/"):
        raise ValueError("fixture remote_path must be absolute")
    if "\\" in remote_path or "\x00" in remote_path or "\n" in remote_path or "\r" in remote_path:
        raise ValueError("fixture remote_path contains unsafe characters")
    path = PurePosixPath(remote_path)
    if ".." in path.parts:
        raise ValueError("fixture remote_path cannot contain traversal segments")
    if not _is_safe_remote_path(remote_path):
        prefixes = ", ".join(_SAFE_REMOTE_PREFIXES)
        raise ValueError(f"fixture remote_path must be under one of: {prefixes}")


def _validate_octal_mode(mode: str, *, field_name: str) -> None:
    if not mode:
        return
    if not mode.isdigit() or any(char not in "01234567" for char in mode):
        raise ValueError(f"{field_name} must be an octal digit string")
    if len(mode) not in {3, 4}:
        raise ValueError(f"{field_name} must be three or four octal digits")


def _is_safe_remote_path(remote_path: str) -> bool:
    for prefix in _SAFE_REMOTE_PREFIXES:
        if remote_path.startswith(prefix):
            return True
        if prefix.endswith("/") and remote_path == prefix.rstrip("/"):
            return True
    return False


class CaseRunResult(StrictCaseModel):
    """Summary returned by the reusable case-suite runner."""

    suite_id: str
    run_id: str
    run_mode: CaseRunMode
    dry_run: bool = False
    total: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    canceled: int = Field(ge=0)
    incomplete: int = Field(default=0, ge=0)
    output_dir: str
    batch_result: BatchRunResult
    case_report_path: str
    batch_manifest_path: str
    runnable: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_disposition_counts(self) -> "CaseRunResult":
        if self.runnable == 0 and self.blocked == 0:
            self.runnable = self.total
        if self.runnable + self.blocked != self.total:
            raise ValueError("case run total must equal runnable plus blocked")
        dispatched = self.succeeded + self.failed + self.canceled + self.incomplete
        if dispatched != self.runnable:
            raise ValueError("case run terminal counts must equal runnable cases")
        if self.batch_result.total != self.runnable:
            raise ValueError("case run batch total must equal runnable cases")
        state_ids = [state.task_id for state in self.batch_result.task_states]
        if len(state_ids) > self.runnable:
            raise ValueError("case run task states cannot exceed runnable cases")
        if len(state_ids) != len(set(state_ids)):
            raise ValueError("case run task states must have unique task ids")
        batch_counts = (
            self.batch_result.succeeded,
            self.batch_result.failed,
            self.batch_result.canceled,
            self.batch_result.incomplete,
        )
        if batch_counts != (self.succeeded, self.failed, self.canceled, self.incomplete):
            raise ValueError("case run terminal counts must match the batch result")
        return self
