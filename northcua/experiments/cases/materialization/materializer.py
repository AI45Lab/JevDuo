"""Projection from reusable case suites to batch manifests."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, replace
from pathlib import Path

from northcua.batch.contracts import BatchDefaults, BatchManifest, BatchTaskSpec
from northcua.batch.runner.bindings import SandboxDeviceBindingFactory
from northcua.experiments.cases.catalog import CaseCatalog, CaseCatalogLoader
from northcua.experiments.cases.contracts import (
    AppProfileId,
    AppProfileSpec,
    AppProvisioningMode,
    CaseSetupPhase,
    CaseSpec,
    CaseSuiteSpec,
    DesktopSessionSpec,
    FixtureRole,
    GuestCapability,
    InitRecipeSpec,
    MaterializedFixtureReference,
    RecipeKind,
    RunSetupScriptRecipeArgs,
    TemplateAppBindingSpec,
    TemplateProfileId,
    TemplateProfileSpec,
)
from northcua.experiments.cases.guest_capabilities import GuestCapabilityGateRenderer
from northcua.experiments.cases.materialization.fixtures import FixtureMaterializer, MaterializedFixture
from northcua.experiments.cases.recipes import RecipeRegistry, RecipeRenderContext, default_recipe_registry
from northcua.experiments.cases.recipes.shell import desktop_session_readiness_script
from northcua.experiments.planner.evaluator_capabilities import EvaluatorCapabilityPlanner
from northcua.sandboxes.contracts import SandboxAllocationStrategy, SandboxCommandSpec, SandboxProviderSpec
from northcua_protocol.capabilities import Capability


@dataclass(frozen=True, slots=True)
class MaterializedCase:
    """One case after fixture staging and recipe rendering.

    Attributes:
        case: Case spec with its resolved template profile.
        fixtures: Local fixture upload records for the case.
        startup_commands: Sandbox commands produced by setup/init recipes and
            desktop-session readiness.
        setup_phases: Typed setup/init projection with commands per phase.
        pre_submit_commands: Commands run after device readiness and before
            controller submission.
        sandbox: Per-task sandbox provider override.
    """

    case: CaseSpec
    fixtures: tuple[MaterializedFixture, ...]
    startup_commands: tuple[SandboxCommandSpec, ...]
    sandbox: SandboxProviderSpec
    pre_submit_commands: tuple[SandboxCommandSpec, ...] = ()
    setup_phases: tuple[MaterializedSetupPhase, ...] = ()

    @property
    def consumer_fixture_references(self) -> tuple[MaterializedFixtureReference, ...]:
        """Return typed host/evaluator fixture references without guest fixtures."""

        return tuple(
            fixture.reference
            for fixture in self.fixtures
            if fixture.upload.visibility != "guest" and fixture.reference is not None
        )


@dataclass(frozen=True, slots=True)
class MaterializedSetupPhase:
    """One typed setup phase after recipe rendering."""

    phase: CaseSetupPhase
    recipes: tuple[InitRecipeSpec, ...]
    commands: tuple[SandboxCommandSpec, ...]


@dataclass(frozen=True, slots=True)
class MaterializedCaseSuite:
    """Materialized suite plus its runnable batch manifest.

    Attributes:
        spec: Suite spec used for materialization.
        output_dir: Run directory containing manifests and reports.
        fixture_dir: Local directory for staged generated fixtures.
        cases: Materialized cases in manifest order.
        manifest: Batch manifest projected for the existing runner.
    """

    spec: CaseSuiteSpec
    output_dir: Path
    fixture_dir: Path
    cases: tuple[MaterializedCase, ...]
    manifest: BatchManifest


class CaseMaterializer:
    """Materialize case suites into the existing batch runner IR."""

    def __init__(
        self,
        *,
        catalog: CaseCatalog | None = None,
        fixture_materializer: FixtureMaterializer | None = None,
        recipe_registry: RecipeRegistry | None = None,
        binding_factory: SandboxDeviceBindingFactory | None = None,
        evaluator_capabilities: EvaluatorCapabilityPlanner | None = None,
        guest_capability_gate: GuestCapabilityGateRenderer | None = None,
    ) -> None:
        self._catalog = catalog or CaseCatalogLoader().load()
        self._fixture_materializer = fixture_materializer or FixtureMaterializer()
        self._recipe_registry = recipe_registry or default_recipe_registry()
        self._binding_factory = binding_factory or SandboxDeviceBindingFactory()
        self._evaluator_capabilities = evaluator_capabilities or EvaluatorCapabilityPlanner()
        self._guest_capability_gate = guest_capability_gate or GuestCapabilityGateRenderer()

    def materialize(
        self,
        spec: CaseSuiteSpec,
        *,
        output_dir: Path,
        spec_dir: Path | None = None,
        controller_url: str = "",
        submitter_controller_url: str = "",
        run_id: str = "",
    ) -> MaterializedCaseSuite:
        """Materialize a case suite and build its batch manifest.

        Args:
            spec: Validated reusable case suite.
            output_dir: Root directory for run artifacts.
            spec_dir: Directory used to resolve relative fixture paths.
            controller_url: Public controller URL exposed to devices.
            submitter_controller_url: Local submitter URL used by the runner.
            run_id: Optional batch/run id for resumable output paths.

        Returns:
            Materialized suite with fixtures, startup commands, and manifest.

        Raises:
            ValueError: When suite settings or catalog compatibility fail.
        """

        self.validate_suite(spec)
        batch_id = run_id or spec.suite_id
        suite_dir = output_dir / spec.suite_id / batch_id if run_id else output_dir / spec.suite_id
        fixture_dir = suite_dir / "fixtures"
        fixture_dir.mkdir(parents=True, exist_ok=True)
        cases = tuple(
            (
                self._materialize_case(
                    case,
                    suite=spec,
                    batch_id=batch_id,
                    fixture_dir=fixture_dir,
                    spec_dir=spec_dir,
                )
                if case.disposition.is_runnable
                else self._materialize_blocked_case(case, suite=spec, batch_id=batch_id)
            )
            for case in spec.tasks
        )
        manifest = self._manifest(
            spec,
            cases=cases,
            controller_url=controller_url,
            submitter_controller_url=submitter_controller_url,
            batch_id=batch_id,
        )
        return MaterializedCaseSuite(
            spec=spec,
            output_dir=suite_dir,
            fixture_dir=fixture_dir,
            cases=cases,
            manifest=manifest,
        )

    def validate_suite(self, spec: CaseSuiteSpec) -> None:
        """Validate catalog compatibility without writing fixtures.

        The case suite owns workload intent; the catalog owns concrete
        runtime placement. This method makes that contract explicit before any
        sandbox/template side effects happen.

        Args:
            spec: Case suite to validate against the current catalog.

        Raises:
            CaseCatalogError: When referenced profiles are missing.
            ValueError: When app/template compatibility checks fail.
        """

        for case in spec.tasks:
            app = self._catalog.app(case.app_profile)
            template = self._catalog.template(case.template_profile or app.template_profile)
            self._validate_case(case=case, app=app, template=template)

    def _materialize_case(
        self,
        case: CaseSpec,
        *,
        suite: CaseSuiteSpec,
        batch_id: str,
        fixture_dir: Path,
        spec_dir: Path | None,
    ) -> MaterializedCase:
        app = self._catalog.app(case.app_profile)
        template_profile_id = case.template_profile or app.template_profile
        template = self._catalog.template(template_profile_id)
        self._validate_case(case=case, app=app, template=template)
        required_guest_capabilities = self._effective_guest_capabilities(case=case, template=template)
        desktop_session = self._effective_desktop_session(
            case=case,
            template=template,
            required_guest_capabilities=required_guest_capabilities,
        )
        materialized_fixtures = tuple(
            self._fixture_materializer.materialize(
                fixture,
                task_id=case.task_id,
                fixture_dir=fixture_dir,
                spec_dir=spec_dir,
                app_profile=case.app_profile,
            )
            for fixture in [*case.fixtures, *case.dependency_injection.files]
        )
        fixture_user = (desktop_session.target_user if desktop_session is not None else "") or app.launch_user
        fixtures = tuple(
            self._fixture_with_default_ownership(fixture, user=fixture_user) for fixture in materialized_fixtures
        )
        resolved_case = self._case_with_resolved_setup_checksums(case, fixtures).model_copy(
            update={
                "template_profile": template_profile_id,
                "desktop_session": desktop_session,
                "required_guest_capabilities": required_guest_capabilities,
            }
        )
        context = RecipeRenderContext(
            case=resolved_case,
            app=app,
            app_profiles=self._catalog.apps,
            template=template,
            remote_workdir=f"/tmp/northcua-cases/{case.task_id}",
        )
        setup_phases = self._materialized_setup_phases(
            case=resolved_case,
            context=context,
            template=template,
        )
        readiness_command = self._desktop_session_readiness_command(desktop_session)
        startup_commands = self._startup_commands_with_desktop_readiness(
            setup_phases=setup_phases,
            readiness_command=readiness_command,
        )
        capability_gate = self._guest_capability_gate.render(
            required_guest_capabilities,
            desktop_session=desktop_session,
            python_executable=template.device_python,
        )
        if capability_gate is not None:
            startup_commands = (capability_gate, *startup_commands)
        sandbox = self._resolved_sandbox(
            suite=suite,
            case=resolved_case,
            template=template,
            fixtures=fixtures,
            batch_id=batch_id,
        )
        return MaterializedCase(
            case=resolved_case,
            fixtures=fixtures,
            startup_commands=startup_commands,
            sandbox=sandbox,
            pre_submit_commands=tuple(resolved_case.pre_submit_commands),
            setup_phases=setup_phases,
        )

    def _materialize_blocked_case(
        self,
        case: CaseSpec,
        *,
        suite: CaseSuiteSpec,
        batch_id: str,
    ) -> MaterializedCase:
        """Resolve audit metadata without staging fixtures or rendering commands."""

        app = self._catalog.app(case.app_profile)
        template_profile_id = case.template_profile or app.template_profile
        template = self._catalog.template(template_profile_id)
        self._validate_case(case=case, app=app, template=template)
        resolved_case = case.model_copy(
            update={
                "template_profile": template_profile_id,
                "desktop_session": self._effective_desktop_session(case=case, template=template),
            }
        )
        sandbox = self._resolved_sandbox(
            suite=suite,
            case=resolved_case,
            template=template,
            fixtures=(),
            batch_id=batch_id,
        )
        return MaterializedCase(
            case=resolved_case,
            fixtures=(),
            startup_commands=(),
            sandbox=sandbox,
            pre_submit_commands=(),
        )

    @staticmethod
    def _fixture_with_default_ownership(
        fixture: MaterializedFixture,
        *,
        user: str,
    ) -> MaterializedFixture:
        """Project legacy guest fixture ownership into the guarded upload lifecycle."""

        upload = fixture.upload
        if not user or upload.visibility != "guest":
            return fixture
        updates: dict[str, str] = {}
        if not upload.owner and not upload.group:
            updates.update(owner=user, group=user)
        if not upload.parent_owner and not upload.parent_group:
            updates.update(parent_owner=user, parent_group=user)
        if not updates:
            return fixture
        return replace(fixture, upload=upload.model_copy(update=updates))

    def _manifest(
        self,
        spec: CaseSuiteSpec,
        *,
        cases: tuple[MaterializedCase, ...],
        controller_url: str,
        submitter_controller_url: str,
        batch_id: str,
    ) -> BatchManifest:
        device_controller_url = controller_url or spec.controller.public_url
        defaults = BatchDefaults(
            controller_url=submitter_controller_url or device_controller_url,
            device_controller_url=device_controller_url,
            token="",
            concurrency=spec.concurrency,
            sandbox=spec.sandbox,
        )
        evaluator_capabilities = self._evaluator_capabilities.from_app_config_path(spec.controller.app_config_path)
        controller_metadata = {
            "controller_app_config_path": spec.controller.app_config_path,
            **SandboxDeviceBindingFactory.desktop_coordinate_metadata(
                {"controller_app_config_path": spec.controller.app_config_path}
            ),
        }
        tasks: list[BatchTaskSpec] = []
        for item in cases:
            if not item.case.disposition.is_runnable:
                continue
            template = self._catalog.template(item.case.template_profile or TemplateProfileId.BASE_RUNTIME)
            guest_fixtures = [fixture for fixture in item.fixtures if fixture.upload.visibility == "guest"]
            consumer_fixture_references = [
                reference.model_dump(mode="json") for reference in item.consumer_fixture_references
            ]
            large_artifact_delivery_evidence = [
                fixture.large_artifact_evidence.model_dump(mode="json")
                for fixture in item.fixtures
                if fixture.large_artifact_evidence is not None
            ]
            binding_metadata = self._binding_metadata(case=item.case, template=template)
            tasks.append(
                BatchTaskSpec(
                    task_id=item.case.task_id,
                    agent_kind=item.case.agent_kind,
                    query=item.case.query,
                    max_steps=item.case.max_steps,
                    max_session_steps=item.case.max_session_steps,
                    metadata={
                        **item.case.metadata,
                        **controller_metadata,
                        "case_suite_id": spec.suite_id,
                        "app_profile": item.case.app_profile.value,
                        "template_profile": (item.case.template_profile or TemplateProfileId.BASE_RUNTIME).value,
                        "template_alias": item.sandbox.template,
                        "involved_app_profiles": [app.value for app in item.case.involved_app_profiles],
                        "involved_app_count": item.case.involved_app_count,
                        "expected_feasibility": item.case.expected_feasibility.value,
                        "setup_contract": item.case.setup_contract_source,
                        "setup_phases": [
                            {
                                "phase": phase.phase.value,
                                "recipe_kinds": [recipe.recipe.value for recipe in phase.recipes],
                                "command_labels": [command.label for command in phase.commands],
                            }
                            for phase in item.setup_phases
                        ],
                        "required_guest_capabilities": sorted(
                            capability.value for capability in item.case.required_guest_capabilities
                        ),
                        **self._desktop_session_metadata(item.case.desktop_session),
                        "template_app_bindings": binding_metadata,
                        "fixture_remote_paths": [fixture.spec.remote_path for fixture in guest_fixtures],
                        "fixture_sha256": {
                            fixture.spec.remote_path: fixture.spec.sha256
                            for fixture in guest_fixtures
                            if fixture.spec.sha256
                        },
                        **(
                            {"large_artifact_delivery_evidence": large_artifact_delivery_evidence}
                            if large_artifact_delivery_evidence
                            else {}
                        ),
                        "materialized_fixture_references": consumer_fixture_references,
                    },
                    required_capabilities=(
                        self._binding_factory.default_capabilities(item.case.agent_kind)
                        | self._effective_protocol_capabilities(case=item.case, template=template)
                        | evaluator_capabilities
                    ),
                    sandbox=item.sandbox,
                    startup_commands=list(item.startup_commands),
                    pre_submit_commands=list(item.pre_submit_commands),
                    post_run_hooks=list(item.case.post_run_hooks),
                    timeout_seconds=item.case.timeout_seconds,
                    max_retries=item.case.max_retries,
                )
            )
        return BatchManifest(batch_id=batch_id, defaults=defaults, tasks=tasks)

    @staticmethod
    def _validate_unique_remote_paths(case: CaseSpec) -> None:
        remote_paths = [fixture.remote_path for fixture in [*case.fixtures, *case.dependency_injection.files]]
        duplicates = sorted({path for path in remote_paths if remote_paths.count(path) > 1})
        if duplicates:
            raise ValueError(f"case fixture remote_path values must be unique: {duplicates}")

    def _validate_case(self, *, case: CaseSpec, app: AppProfileSpec, template: TemplateProfileSpec) -> None:
        self._validate_unique_remote_paths(case)
        for referenced_app in self._referenced_apps(case, primary=app):
            self._validate_catalog_compatibility(case=case, app=referenced_app, template=template)
        self._validate_task_setup_phase(case=case, template=template)
        required_guest_capabilities = self._effective_guest_capabilities(case=case, template=template)
        desktop_session = self._effective_desktop_session(
            case=case,
            template=template,
            required_guest_capabilities=required_guest_capabilities,
        )
        if case.disposition.is_runnable:
            self._guest_capability_gate.validate_runtime_context(
                required_guest_capabilities,
                desktop_session=desktop_session,
            )

    def _validate_task_setup_phase(self, *, case: CaseSpec, template: TemplateProfileSpec) -> None:
        if not case.disposition.is_runnable:
            return
        task_setup_apps = [
            app.app_profile
            for app in self._referenced_apps(case)
            if (binding := template.app_binding(app.app_profile)) is not None
            and binding.provisioning_mode == AppProvisioningMode.TASK_SETUP
        ]
        if not task_setup_apps:
            return
        setup_recipes = next(
            (phase.recipes for phase in case.effective_setup_phases() if phase.phase == CaseSetupPhase.SETUP),
            [],
        )
        app_ids = sorted(app.value for app in task_setup_apps)
        if not setup_recipes:
            raise ValueError(
                f"case {case.task_id} task_setup bindings require an actual "
                f"dependency_injection.setup_recipes phase: {app_ids}"
            )
        invalid = sorted(
            {recipe.recipe.value for recipe in setup_recipes if recipe.recipe != RecipeKind.RUN_SETUP_SCRIPT}
        )
        if invalid:
            raise ValueError(
                f"case {case.task_id} task_setup phase only allows run_setup_script recipes; found: {invalid}"
            )

    @staticmethod
    def _validate_catalog_compatibility(
        *,
        case: CaseSpec,
        app: AppProfileSpec,
        template: TemplateProfileSpec,
    ) -> None:
        binding = template.app_binding(app.app_profile)
        if binding is None and template.installed_apps and app.app_profile not in template.installed_apps:
            raise ValueError(
                f"case {case.task_id} app_profile={app.app_profile.value} is not installed in "
                f"template_profile={template.profile_id.value}"
            )
        if binding is None and template.app_bindings and app.app_profile not in template.installed_apps:
            raise ValueError(
                f"case {case.task_id} app_profile={app.app_profile.value} has no app binding in "
                f"template_profile={template.profile_id.value}"
            )
        if binding is not None and binding.provisioning_mode == AppProvisioningMode.TASK_SETUP:
            return
        template_binaries = set(template.required_binaries)
        missing_binaries = sorted(binary for binary in app.required_binaries if binary not in template_binaries)
        if missing_binaries:
            raise ValueError(
                f"case {case.task_id} template_profile={template.profile_id.value} is missing binaries "
                f"for app_profile={app.app_profile.value}: {missing_binaries}"
            )

    @staticmethod
    def _referenced_app_profile_ids(case: CaseSpec) -> tuple[AppProfileId, ...]:
        app_ids: list[AppProfileId] = [case.app_profile, *case.involved_app_profiles]
        for phase in case.effective_setup_phases():
            for recipe in phase.recipes:
                recipe_app = getattr(recipe.args, "app", None)
                if isinstance(recipe_app, AppProfileId):
                    app_ids.append(recipe_app)
        return tuple(dict.fromkeys(app_ids))

    def _referenced_apps(self, case: CaseSpec, *, primary: AppProfileSpec | None = None) -> tuple[AppProfileSpec, ...]:
        apps: list[AppProfileSpec] = []
        for app_profile in self._referenced_app_profile_ids(case):
            if primary is not None and app_profile == primary.app_profile:
                apps.append(primary)
            else:
                apps.append(self._catalog.app(app_profile))
        return tuple(apps)

    def _effective_guest_capabilities(
        self,
        *,
        case: CaseSpec,
        template: TemplateProfileSpec,
    ) -> set[GuestCapability]:
        required = set(template.required_guest_capabilities) | set(case.required_guest_capabilities)
        for app in self._referenced_apps(case):
            required.update(app.required_guest_capabilities)
            binding = template.app_binding(app.app_profile)
            if binding is not None:
                required.update(binding.required_guest_capabilities)
        return required

    def _effective_desktop_session(
        self,
        *,
        case: CaseSpec,
        template: TemplateProfileSpec,
        required_guest_capabilities: set[GuestCapability] | None = None,
    ) -> DesktopSessionSpec | None:
        """Resolve case/template intent without changing legacy source specs."""

        guest_capabilities = required_guest_capabilities or set()
        capability_requires_desktop = self._guest_capability_gate.requires_desktop_session(guest_capabilities)
        if capability_requires_desktop and not template.desktop:
            capability_ids = sorted(capability.value for capability in guest_capabilities)
            raise ValueError(
                f"case {case.task_id} requires desktop guest capabilities but "
                f"template_profile={template.profile_id.value} is not a desktop template: {capability_ids}"
            )
        desktop_workflow = template.desktop and (case.agent_kind == "desktop" or capability_requires_desktop)
        if case.desktop_session is not None:
            session = case.desktop_session
        elif desktop_workflow:
            session = template.desktop_session or DesktopSessionSpec(required=True)
        else:
            return None
        if session.required and not template.desktop:
            raise ValueError(
                f"case {case.task_id} requires a desktop session but "
                f"template_profile={template.profile_id.value} is not a desktop template"
            )
        launch_users = {app.launch_user for app in self._referenced_apps(case) if app.launch_user}
        if len(launch_users) > 1:
            raise ValueError(
                f"case {case.task_id} desktop apps declare conflicting launch users: {sorted(launch_users)}"
            )
        launch_user = next(iter(launch_users), "")
        if session.target_user and launch_user and session.target_user != launch_user:
            raise ValueError(
                f"case {case.task_id} desktop_session target_user={session.target_user!r} "
                f"conflicts with app launch_user={launch_user!r}"
            )
        if not session.target_user and launch_user:
            session = session.model_copy(update={"target_user": launch_user})
        return session

    @staticmethod
    def _desktop_session_readiness_command(
        desktop_session: DesktopSessionSpec | None,
    ) -> SandboxCommandSpec | None:
        if desktop_session is None or not desktop_session.required:
            return None
        return SandboxCommandSpec(
            command=desktop_session_readiness_script(desktop_session),
            label="desktop-session-readiness",
            timeout_seconds=desktop_session.readiness_timeout_seconds + 10.0,
        )

    @staticmethod
    def _startup_commands_with_desktop_readiness(
        *,
        setup_phases: tuple[MaterializedSetupPhase, ...],
        readiness_command: SandboxCommandSpec | None,
    ) -> tuple[SandboxCommandSpec, ...]:
        commands: list[SandboxCommandSpec] = []
        readiness_added = False
        for phase in setup_phases:
            if phase.phase == CaseSetupPhase.INIT and readiness_command is not None:
                commands.append(readiness_command)
                readiness_added = True
            commands.extend(phase.commands)
        if readiness_command is not None and not readiness_added:
            commands.append(readiness_command)
        return tuple(commands)

    @staticmethod
    def _desktop_session_metadata(desktop_session: DesktopSessionSpec | None) -> dict[str, object]:
        if desktop_session is None:
            return {}
        return {"desktop_session": desktop_session.model_dump(mode="json")}

    def _effective_protocol_capabilities(
        self,
        *,
        case: CaseSpec,
        template: TemplateProfileSpec,
    ) -> set[Capability]:
        required = set(template.required_capabilities) | set(case.required_capabilities)
        for app in self._referenced_apps(case):
            required.update(app.required_capabilities)
        return required

    def _materialized_setup_phases(
        self,
        *,
        case: CaseSpec,
        context: RecipeRenderContext,
        template: TemplateProfileSpec,
    ) -> tuple[MaterializedSetupPhase, ...]:
        recipes_by_phase = {phase.phase: tuple(phase.recipes) for phase in case.effective_setup_phases()}
        post_setup_commands = self._post_setup_probe_commands(case=case, template=template)
        materialized: list[MaterializedSetupPhase] = []
        for phase in CaseSetupPhase:
            recipes = recipes_by_phase.get(phase, ())
            commands = [command for recipe in recipes for command in self._recipe_registry.render(recipe, context)]
            if phase == CaseSetupPhase.SETUP:
                commands.extend(post_setup_commands)
            if recipes or commands:
                materialized.append(
                    MaterializedSetupPhase(
                        phase=phase,
                        recipes=recipes,
                        commands=tuple(commands),
                    )
                )
        return tuple(materialized)

    def _post_setup_probe_commands(
        self,
        *,
        case: CaseSpec,
        template: TemplateProfileSpec,
    ) -> list[SandboxCommandSpec]:
        commands: list[SandboxCommandSpec] = []
        for app in self._referenced_apps(case):
            binding = template.app_binding(app.app_profile)
            if binding is None:
                continue
            for binary in binding.post_setup.binary_probes:
                commands.append(
                    SandboxCommandSpec(
                        command=f"command -v {shlex.quote(binary)} >/dev/null",
                        label=f"post-setup-binary-{app.app_profile.value}",
                        timeout_seconds=30,
                    )
                )
            for version_command in binding.post_setup.version_probes:
                commands.append(
                    SandboxCommandSpec(
                        command=shlex.join(shlex.split(version_command)),
                        label=f"post-setup-version-{app.app_profile.value}",
                        timeout_seconds=30,
                    )
                )
        return commands

    def _binding_metadata(self, *, case: CaseSpec, template: TemplateProfileSpec) -> dict[str, object]:
        bindings: dict[str, object] = {}
        for app_profile in self._referenced_app_profile_ids(case):
            binding: TemplateAppBindingSpec | None = template.app_binding(app_profile)
            if binding is None:
                continue
            bindings[app_profile.value] = {
                "provisioning_mode": binding.provisioning_mode.value,
                "first_run_variant": binding.first_run_variant,
            }
        return bindings

    @staticmethod
    def _case_with_resolved_setup_checksums(
        case: CaseSpec,
        fixtures: tuple[MaterializedFixture, ...],
    ) -> CaseSpec:
        setup_checksums = {
            fixture.spec.remote_path: fixture.spec.sha256
            for fixture in fixtures
            if fixture.spec.role == FixtureRole.SETUP_SCRIPT
            and fixture.upload.visibility == "guest"
            and fixture.spec.sha256
        }

        def _resolve_recipe(recipe: InitRecipeSpec) -> InitRecipeSpec:
            """Attach generated setup-script checksums to matching recipes."""

            if recipe.recipe.value != "run_setup_script" or not isinstance(recipe.args, RunSetupScriptRecipeArgs):
                return recipe
            expected = setup_checksums.get(recipe.args.path)
            if not expected:
                raise ValueError(f"run_setup_script path must reference a setup_script fixture: {recipe.args.path}")
            if recipe.args.checksum and recipe.args.checksum != expected:
                raise ValueError(f"run_setup_script checksum mismatch for {recipe.args.path}")
            return recipe.model_copy(update={"args": recipe.args.model_copy(update={"checksum": expected})})

        if case.setup is not None:
            resolved_phases = [
                phase.model_copy(update={"recipes": [_resolve_recipe(recipe) for recipe in phase.recipes]})
                for phase in case.setup.phases
            ]
            resolved_setup = case.setup.model_copy(update={"phases": resolved_phases})
            dependency = case.dependency_injection.model_copy(
                update={"setup_recipes": resolved_setup.recipes_for(CaseSetupPhase.SETUP)}
            )
            return case.model_copy(
                update={
                    "setup": resolved_setup,
                    "dependency_injection": dependency,
                    "init": resolved_setup.recipes_for(CaseSetupPhase.INIT),
                }
            )
        dependency = case.dependency_injection.model_copy(
            update={
                "setup_recipes": [_resolve_recipe(recipe) for recipe in case.dependency_injection.setup_recipes],
            }
        )
        return case.model_copy(
            update={
                "dependency_injection": dependency,
                "init": [_resolve_recipe(recipe) for recipe in case.init],
            }
        )

    @staticmethod
    def _resolved_sandbox(
        *,
        suite: CaseSuiteSpec,
        case: CaseSpec,
        template: TemplateProfileSpec,
        fixtures: tuple[MaterializedFixture, ...],
        batch_id: str,
    ) -> SandboxProviderSpec:
        """Merge suite sandbox defaults with the selected template profile.

        Merge policy is intentionally template-authoritative for provider,
        alias, desktop mode, runtime resources, resolution, and device Python.
        Suite-level sandbox config remains the place for allocation strategy,
        metadata, DPI, existing sandbox id, and shared file uploads.
        """

        return suite.sandbox.model_copy(
            update={
                "provider": template.provider.value,
                "template": template.alias,
                "desktop": template.desktop,
                "resources": template.resources,
                "resolution": template.resolution,
                "device_python": template.device_python,
                "file_uploads": [
                    *suite.sandbox.file_uploads,
                    *(fixture.upload for fixture in fixtures if fixture.upload.visibility == "guest"),
                ],
                "metadata": {
                    **suite.sandbox.metadata,
                    "northcua": "true",
                    "case_suite_id": suite.suite_id,
                    "batch_id": batch_id,
                    "task_id": case.task_id,
                    "app_profile": case.app_profile.value,
                    "template_profile": template.profile_id.value,
                    "template_alias": template.alias,
                    "required_guest_capabilities": sorted(
                        capability.value for capability in case.required_guest_capabilities
                    ),
                    **CaseMaterializer._desktop_session_metadata(case.desktop_session),
                    "sandbox_merge_policy": "template_profile_runtime_overrides_suite_defaults",
                },
            }
        )
