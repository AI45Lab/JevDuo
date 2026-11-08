"""Built-in recipe renderers for E2B desktop cases."""

from __future__ import annotations

from pathlib import PurePosixPath

from northcua.experiments.cases.contracts import (
    AppProfileId,
    AppProfileSpec,
    AssertBinaryRecipeArgs,
    AssertFileExistsRecipeArgs,
    DesktopSessionSpec,
    InitRecipeSpec,
    LaunchCommandRecipeArgs,
    OpenBlankAppRecipeArgs,
    OpenDirectoryRecipeArgs,
    OpenFileRecipeArgs,
    OpenUrlRecipeArgs,
    RecipeKind,
    RunSetupScriptRecipeArgs,
    WaitWindowRecipeArgs,
)
from northcua.experiments.cases.recipes.base import RecipeRenderer
from northcua.experiments.cases.recipes.context import RecipeRenderContext
from northcua.experiments.cases.recipes.shell import (
    bash_strict,
    launch_background,
    launch_background_shell,
    quote,
    wait_for_process_or_window,
)
from northcua.sandboxes.contracts import SandboxCommandSpec


class _LaunchRecipeBase(RecipeRenderer):
    """Shared launch helpers for recipes that start GUI applications."""

    def _app(self, recipe_app: AppProfileId | None, context: RecipeRenderContext) -> AppProfileSpec:
        return context.app_for(recipe_app)

    @staticmethod
    def _desktop_session(context: RecipeRenderContext) -> DesktopSessionSpec | None:
        """Return the session contract resolved by case materialization."""

        return context.case.desktop_session

    def _launch_command(
        self,
        *,
        app: AppProfileSpec,
        context: RecipeRenderContext,
        args: list[str],
        timeout_seconds: float,
        wait_window: str,
        label: str,
        track_recent_process_exit: bool = False,
    ) -> SandboxCommandSpec:
        launch = [*(app.launch_argv or [app.launch_binary]), *args]
        stem = label.replace("/", "-")
        out = f"{context.remote_workdir}/{stem}.out"
        err = f"{context.remote_workdir}/{stem}.err"
        pid = f"{context.remote_workdir}/{stem}.pid"
        desktop_session = self._desktop_session(context)
        strict_desktop_session = desktop_session is not None and desktop_session.required
        window_contains = wait_window or app.window_hint
        script = [
            *([] if strict_desktop_session else ["export DISPLAY=${DISPLAY:-:0}"]),
            f"mkdir -p {quote(context.remote_workdir)}",
            launch_background(
                launch,
                stdout_path=out,
                stderr_path=err,
                pid_path=pid,
                user=app.launch_user,
                desktop_session=desktop_session,
            ),
            *wait_for_process_or_window(
                process_contains=app.process_hint,
                window_contains=window_contains,
                timeout_seconds=timeout_seconds,
                pid_path=pid,
                allow_process_fallback_with_window=app.app_profile == AppProfileId.VS_CODE,
                track_recent_process_exit=track_recent_process_exit,
                strict_desktop_session=strict_desktop_session,
                launch_user=app.launch_user,
            ),
        ]
        return SandboxCommandSpec(command=bash_strict(script), label=label, timeout_seconds=timeout_seconds + 10)

    def _launch_command_with_dynamic_path(
        self,
        *,
        app: AppProfileSpec,
        context: RecipeRenderContext,
        dynamic_path_variable: str,
        prelaunch_lines: list[str],
        timeout_seconds: float,
        wait_window: str,
        label: str,
    ) -> SandboxCommandSpec:
        """Render a launch command whose document path is prepared in bash.

        Some GUI applications need a lightweight preparation step before the
        stable launch target is known. LibreOffice Calc is the main example:
        opening CSV directly can trigger import and first-run dialogs, while a
        native ODS converted in the sandbox matches the proven live case path.
        """

        launch = " ".join(quote(item) for item in (app.launch_argv or [app.launch_binary]))
        stem = label.replace("/", "-")
        out = f"{context.remote_workdir}/{stem}.out"
        err = f"{context.remote_workdir}/{stem}.err"
        pid = f"{context.remote_workdir}/{stem}.pid"
        command_script = f'setsid {launch} "${dynamic_path_variable}" </dev/null >{quote(out)} 2>{quote(err)} & echo $! > {quote(pid)}'
        desktop_session = self._desktop_session(context)
        strict_desktop_session = desktop_session is not None and desktop_session.required
        script = [
            *([] if strict_desktop_session else ["export DISPLAY=${DISPLAY:-:0}"]),
            f"mkdir -p {quote(context.remote_workdir)}",
            *([] if strict_desktop_session else prelaunch_lines),
            launch_background_shell(
                command_script,
                stdout_path=out,
                stderr_path=err,
                pid_path=pid,
                user=app.launch_user,
                passthrough_env_vars=(dynamic_path_variable,),
                desktop_session=desktop_session,
                prelaunch_lines=tuple(prelaunch_lines) if strict_desktop_session else (),
            ),
            *wait_for_process_or_window(
                process_contains=app.process_hint,
                window_contains=wait_window or app.window_hint,
                timeout_seconds=timeout_seconds,
                pid_path=pid,
                allow_process_fallback_with_window=app.app_profile == AppProfileId.VS_CODE,
                strict_desktop_session=strict_desktop_session,
                launch_user=app.launch_user,
            ),
        ]
        return SandboxCommandSpec(command=bash_strict(script), label=label, timeout_seconds=timeout_seconds + 20)


class OpenFileRecipe(_LaunchRecipeBase):
    """Render commands that open a staged file with a catalog app."""

    kind = RecipeKind.OPEN_FILE

    def render(self, recipe: InitRecipeSpec, context: RecipeRenderContext) -> list[SandboxCommandSpec]:
        """Render an open-file startup command.

        Args:
            recipe: Init recipe with OpenFileRecipeArgs.
            context: Case/catalog context used to resolve the app.

        Returns:
            Sandbox command that launches the app and waits for readiness.
        """

        args = recipe.args
        assert isinstance(args, OpenFileRecipeArgs)
        app = self._app(args.app, context)
        label = recipe.label or f"open-file-{app.app_profile.value}"
        if app.app_profile == AppProfileId.LIBREOFFICE_CALC and args.path.lower().endswith(".csv"):
            return [
                self._launch_calc_csv_as_native_ods(
                    app=app,
                    context=context,
                    csv_path=args.path,
                    timeout_seconds=args.timeout_seconds,
                    wait_window=args.wait_window,
                    label=label,
                )
            ]
        return [
            self._launch_command(
                app=app,
                context=context,
                args=[args.path],
                timeout_seconds=args.timeout_seconds,
                wait_window=args.wait_window or app.window_hint or args.path.split("/")[-1],
                label=label,
                track_recent_process_exit=True,
            )
        ]

    def _launch_calc_csv_as_native_ods(
        self,
        *,
        app: AppProfileSpec,
        context: RecipeRenderContext,
        csv_path: str,
        timeout_seconds: float,
        wait_window: str,
        label: str,
    ) -> SandboxCommandSpec:
        """Convert a CSV fixture to ODS before opening it in Calc.

        The static E2B live-five Calc case already proved this pattern. Keeping
        it in the shared recipe renderer makes old and model-generated case
        specs more robust without asking the environment builder to hand-write
        LibreOffice-specific setup scripts.
        """

        csv_name = PurePosixPath(csv_path).name
        native_name = PurePosixPath(csv_name).with_suffix(".ods").name
        native_path = f"{context.remote_workdir}/{native_name}"
        convert_out = f"{context.remote_workdir}/{label.replace('/', '-')}-convert.out"
        convert_err = f"{context.remote_workdir}/{label.replace('/', '-')}-convert.err"
        desktop_session = self._desktop_session(context)
        strict_desktop_session = desktop_session is not None and desktop_session.required
        if strict_desktop_session:
            conversion_prefix = (
                'cd "$northcua_launch_home" && runuser -u "$northcua_launch_user" -- env '
                'HOME="$northcua_launch_home" USER="$northcua_launch_user" LOGNAME="$northcua_launch_user" '
                'DISPLAY="$launch_display" XAUTHORITY="$launch_xauthority" XDG_RUNTIME_DIR="$session_runtime" '
                'DBUS_SESSION_BUS_ADDRESS="$launch_dbus" AT_SPI_BUS_ADDRESS="$launch_at_spi" '
            )
        elif app.launch_user:
            launch_user = quote(app.launch_user)
            launch_home = quote(f"/home/{app.launch_user}")
            conversion_prefix = (
                f"cd {launch_home} && runuser -u {launch_user} -- env HOME={launch_home} "
                f"USER={launch_user} LOGNAME={launch_user} "
                "DISPLAY=${DISPLAY:-:0} XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/1000} "
                "DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/1000/bus} "
            )
        else:
            conversion_prefix = (
                "env DISPLAY=${DISPLAY:-:0} XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/1000} "
                "DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/1000/bus} "
            )
        prelaunch_lines = [
            f"native_path={quote(native_path)}",
            f"source_path={quote(csv_path)}",
            'launch_path="$source_path"',
            'rm -f "$native_path"',
            (
                conversion_prefix + "timeout 75s libreoffice --headless --convert-to ods "
                f'--outdir {quote(context.remote_workdir)} "$source_path" '
                f">{quote(convert_out)} 2>{quote(convert_err)} || true"
            ),
            'if [ -s "$native_path" ]; then launch_path="$native_path"; fi',
        ]
        return self._launch_command_with_dynamic_path(
            app=app,
            context=context,
            dynamic_path_variable="launch_path",
            prelaunch_lines=prelaunch_lines,
            timeout_seconds=max(timeout_seconds, 90.0),
            wait_window=wait_window,
            label=label,
        )


class OpenUrlRecipe(_LaunchRecipeBase):
    """Render commands that open a URL with a browser-like app."""

    kind = RecipeKind.OPEN_URL

    def render(self, recipe: InitRecipeSpec, context: RecipeRenderContext) -> list[SandboxCommandSpec]:
        """Render an open-url startup command.

        Args:
            recipe: Init recipe with OpenUrlRecipeArgs.
            context: Case/catalog context used to resolve the app.

        Returns:
            Sandbox command that launches the app and waits for readiness.
        """

        args = recipe.args
        assert isinstance(args, OpenUrlRecipeArgs)
        app = self._app(args.app, context)
        label = recipe.label or f"open-url-{app.app_profile.value}"
        return [
            self._launch_command(
                app=app,
                context=context,
                args=[args.url],
                timeout_seconds=args.timeout_seconds,
                wait_window=args.wait_window,
                label=label,
            )
        ]


class OpenBlankAppRecipe(_LaunchRecipeBase):
    """Render commands that launch an empty GUI app session."""

    kind = RecipeKind.OPEN_BLANK_APP

    def render(self, recipe: InitRecipeSpec, context: RecipeRenderContext) -> list[SandboxCommandSpec]:
        """Render an open-blank-app startup command.

        Args:
            recipe: Init recipe with OpenBlankAppRecipeArgs.
            context: Case/catalog context used to resolve the app.

        Returns:
            Sandbox command that launches the app and waits for readiness.
        """

        args = recipe.args
        assert isinstance(args, OpenBlankAppRecipeArgs)
        app = self._app(args.app, context)
        label = recipe.label or f"open-blank-{app.app_profile.value}"
        launch_args = [f"--working-directory={args.working_directory}"] if args.working_directory else []
        return [
            self._launch_command(
                app=app,
                context=context,
                args=launch_args,
                timeout_seconds=args.timeout_seconds,
                wait_window=args.wait_window,
                label=label,
            )
        ]


class OpenDirectoryRecipe(_LaunchRecipeBase):
    """Render commands that open a directory in the case app."""

    kind = RecipeKind.OPEN_DIRECTORY

    def render(self, recipe: InitRecipeSpec, context: RecipeRenderContext) -> list[SandboxCommandSpec]:
        """Render an open-directory startup command.

        Args:
            recipe: Init recipe with OpenDirectoryRecipeArgs.
            context: Case/catalog context used to resolve the app.

        Returns:
            Sandbox command that opens the directory and waits for readiness.
        """

        args = recipe.args
        assert isinstance(args, OpenDirectoryRecipeArgs)
        label = recipe.label or "open-directory"
        app = self._app(args.app, context)
        return [
            self._launch_command(
                app=app,
                context=context,
                args=[args.path],
                timeout_seconds=args.timeout_seconds,
                wait_window=args.wait_window or app.window_hint or args.path.split("/")[-1],
                label=label,
            )
        ]


class LaunchCommandRecipe(_LaunchRecipeBase):
    """Render commands that launch a stable sandbox command before the task."""

    kind = RecipeKind.LAUNCH_COMMAND

    def render(self, recipe: InitRecipeSpec, context: RecipeRenderContext) -> list[SandboxCommandSpec]:
        """Render a launch-command startup recipe.

        Args:
            recipe: Init recipe with LaunchCommandRecipeArgs.
            context: Case/catalog context used for app readiness defaults.

        Returns:
            Sandbox command that launches the command and waits for readiness.
        """

        args = recipe.args
        assert isinstance(args, LaunchCommandRecipeArgs)
        app = self._app(args.app, context)
        label = recipe.label or f"launch-command-{app.app_profile.value}"
        stem = label.replace("/", "-")
        out = f"{context.remote_workdir}/{stem}.out"
        err = f"{context.remote_workdir}/{stem}.err"
        pid = f"{context.remote_workdir}/{stem}.pid"
        desktop_session = self._desktop_session(context)
        strict_desktop_session = desktop_session is not None and desktop_session.required
        script = [
            *([] if strict_desktop_session else ["export DISPLAY=${DISPLAY:-:0}"]),
            f"mkdir -p {quote(context.remote_workdir)}",
            launch_background(
                ["bash", "-lc", args.command],
                stdout_path=out,
                stderr_path=err,
                pid_path=pid,
                user=app.launch_user,
                desktop_session=desktop_session,
            ),
            *wait_for_process_or_window(
                process_contains=args.process_contains or app.process_hint,
                window_contains=args.wait_window or app.window_hint,
                timeout_seconds=args.timeout_seconds,
                pid_path=pid,
                allow_process_fallback_with_window=app.app_profile == AppProfileId.VS_CODE,
                strict_desktop_session=strict_desktop_session,
                launch_user=app.launch_user,
            ),
        ]
        return [SandboxCommandSpec(command=bash_strict(script), label=label, timeout_seconds=args.timeout_seconds + 10)]


class RunSetupScriptRecipe(RecipeRenderer):
    """Render commands that execute a staged setup script."""

    kind = RecipeKind.RUN_SETUP_SCRIPT

    def render(self, recipe: InitRecipeSpec, context: RecipeRenderContext) -> list[SandboxCommandSpec]:
        """Render a checksum-guarded setup-script command.

        Args:
            recipe: Init recipe with RunSetupScriptRecipeArgs.
            context: Case/catalog context; currently unused by this recipe.

        Returns:
            Sandbox command that validates and executes the setup script.
        """

        _ = context
        args = recipe.args
        assert isinstance(args, RunSetupScriptRecipeArgs)
        label = recipe.label or "run-setup-script"
        checksum_lines = []
        if args.checksum:
            checksum_lines = [
                f"echo {quote(args.checksum + '  ' + args.path)} | sha256sum -c -",
            ]
        script = [
            "export DISPLAY=${DISPLAY:-:0}",
            *checksum_lines,
            f"chmod +x {quote(args.path)}",
        ]
        if args.run_as_user:
            user = quote(args.run_as_user)
            command = quote(args.path)
            script.extend(
                [
                    f"northcua_setup_user={user}",
                    'northcua_setup_home="$(getent passwd "$northcua_setup_user" | cut -d: -f6)"',
                    'test -n "$northcua_setup_home"',
                    'if test "$(id -un)" = "$northcua_setup_user"; then',
                    f'  env HOME="$northcua_setup_home" USER="$northcua_setup_user" '
                    f'LOGNAME="$northcua_setup_user" DISPLAY="$DISPLAY" {command}',
                    'elif test "$(id -u)" -eq 0; then',
                    f'  runuser -u "$northcua_setup_user" -- env HOME="$northcua_setup_home" '
                    f'USER="$northcua_setup_user" LOGNAME="$northcua_setup_user" DISPLAY="$DISPLAY" {command}',
                    "else",
                    f'  sudo -n -u "$northcua_setup_user" -- env HOME="$northcua_setup_home" '
                    f'USER="$northcua_setup_user" LOGNAME="$northcua_setup_user" DISPLAY="$DISPLAY" {command}',
                    "fi",
                ]
            )
        else:
            script.append(quote(args.path))
        if args.result_owner:
            paths = " ".join(quote(path) for path in args.result_paths)
            script.extend(
                [
                    f"northcua_result_owner={quote(args.result_owner)}",
                    'northcua_result_uid="$(id -u "$northcua_result_owner")"',
                    'northcua_result_gid="$(id -g "$northcua_result_owner")"',
                    f"for northcua_result_path in {paths}; do",
                    '  test -e "$northcua_result_path" || test -L "$northcua_result_path"',
                    '  northcua_owner_drift="$(find "$northcua_result_path" -xdev '
                    '\\( ! -uid "$northcua_result_uid" -o ! -gid "$northcua_result_gid" \\) -print -quit)"',
                    '  test -z "$northcua_owner_drift"',
                    "done",
                ]
            )
        return [SandboxCommandSpec(command=bash_strict(script), label=label, timeout_seconds=args.timeout_seconds)]


class WaitWindowRecipe(RecipeRenderer):
    """Render commands that wait for a GUI window or process."""

    kind = RecipeKind.WAIT_WINDOW

    def render(self, recipe: InitRecipeSpec, context: RecipeRenderContext) -> list[SandboxCommandSpec]:
        """Render a window/process readiness probe.

        Args:
            recipe: Init recipe with WaitWindowRecipeArgs.
            context: Case/catalog context used for fallback process hints.

        Returns:
            Sandbox command that waits until the app is ready.
        """

        args = recipe.args
        assert isinstance(args, WaitWindowRecipeArgs)
        label = recipe.label or "wait-window"
        script = [
            "export DISPLAY=${DISPLAY:-:0}",
            *wait_for_process_or_window(
                process_contains=args.process_contains or context.app.process_hint,
                window_contains=args.title_contains,
                timeout_seconds=args.timeout_seconds,
            ),
        ]
        return [SandboxCommandSpec(command=bash_strict(script), label=label, timeout_seconds=args.timeout_seconds + 5)]


class AssertBinaryRecipe(RecipeRenderer):
    """Render commands that assert a binary exists in the sandbox."""

    kind = RecipeKind.ASSERT_BINARY

    def render(self, recipe: InitRecipeSpec, context: RecipeRenderContext) -> list[SandboxCommandSpec]:
        """Render a command-v binary assertion.

        Args:
            recipe: Init recipe with AssertBinaryRecipeArgs.
            context: Case/catalog context; currently unused by this recipe.

        Returns:
            Sandbox command that fails when the binary is missing.
        """

        _ = context
        args = recipe.args
        assert isinstance(args, AssertBinaryRecipeArgs)
        label = recipe.label or f"assert-binary-{args.binary}"
        return [
            SandboxCommandSpec(
                command=bash_strict([f"command -v {quote(args.binary)}", f"{quote(args.binary)} --version || true"]),
                label=label,
                timeout_seconds=args.timeout_seconds,
            )
        ]


class AssertFileExistsRecipe(RecipeRenderer):
    """Render commands that assert a file exists in the sandbox."""

    kind = RecipeKind.ASSERT_FILE_EXISTS

    def render(self, recipe: InitRecipeSpec, context: RecipeRenderContext) -> list[SandboxCommandSpec]:
        """Render a file-existence assertion.

        Args:
            recipe: Init recipe with AssertFileExistsRecipeArgs.
            context: Case/catalog context; currently unused by this recipe.

        Returns:
            Sandbox command that fails when the file is missing.
        """

        _ = context
        args = recipe.args
        assert isinstance(args, AssertFileExistsRecipeArgs)
        label = recipe.label or "assert-file-exists"
        return [
            SandboxCommandSpec(
                command=bash_strict([f"test -f {quote(args.path)}"]),
                label=label,
                timeout_seconds=args.timeout_seconds,
            )
        ]
