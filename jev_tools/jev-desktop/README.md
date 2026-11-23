# jev-desktop

Python caller that connects the Jev decision service to the `agent-desktop` CLI.
The distribution is `jev-desktop`; the import is `jev_desktop`. It requires Python
3.10 or newer and has no third-party runtime dependencies.

## Install in JevDuo

From the JevDuo repository root:

```bash
uv pip install --python venv/bin/python --no-deps -e jev_tools/jev-desktop
venv/bin/python -c 'from jev_desktop import AgentDesktop, DesktopConfig, JevLoop, Limits, RunOptions, Scope, TypeSafeModel'
venv/bin/python -m jev_desktop --help
```

`scripts/setup_env.sh` installs this in-repository package by default. Set
`JEVDUO_JEV_PACKAGE` to an alternative package directory to override that source.
Every worker must be able to read the editable source path.

## Responsibilities

- `JevLoop`: goal loop, target and operation selection, budgets and cleanup.
- `AgentDesktop`: native observations, ref operations and delivery evidence.
- `DesktopConfig`, `LocalTransport`, `RemoteTransport`: local CLI and VM HTTP execution.
- `TypeSafeModel`: Jev decision-service requests.
- `ChatTextModel`: optional standalone text generation. JevDuo supplies its own shared Slow-model callback.

This package runs in the agent's Python environment. The Rust CLI and Linux
clipboard helper live in `jev_tools/agent-desktop/` and must be built and installed
inside the OSWorld VM separately. The VM also needs its graphical session and
accessibility services; real Jev inference needs the service credentials described
in the repository's root README. Importing the package or asking for `--help`
does not make a model request or execute a desktop action.

## Source provenance and validation

The initial package was restored from the frozen
`o21-30x30-1112-111127/source-snapshot/jev_desktop/` evaluation snapshot. On
2026-11-19 the 35 Python source modules were replaced with the CLI-trajectory-adapted
SDK from the local `agent-desktop/jev` development checkout. The runtime modules
are included here byte for byte; no import of that external checkout is needed.
The corresponding unit tests are included under `tests/`, with imports sorted
for this package layout. Their local-process fixture allows shared-storage
interpreter startup time while preserving its short timeout case.
Packaging metadata (version 0.1.0) and the Apache-2.0 license
are retained. This Python update does not rebuild or redeploy the guest Rust CLI.

For an explicitly configured private HTTP model gateway, set `TYPESAFE_ALLOW_HTTP=1`
alongside `TYPESAFE_BASE_URL` (the full `/v1/systemone` endpoint), `TYPESAFE_MODEL`
and `TYPESAFE_API_KEY`. Remote HTTP is rejected by default; the opt-in does not
permit credentials embedded in URLs, invalid ports or non-HTTP schemes.

The source is included here, so archived runs and the former sibling checkout are
not required for installation. The subtree import records this restored package as
a local source baseline; there is no separate public Python-package remote yet.

From the JevDuo root, the existing integration tests exercise this package through
scripted HTTP and fake desktop responses, without contacting a VM or model service:

```bash
venv/bin/python -m pytest -q tests/test_jev_subagent.py tests/test_jev_feedback.py tests/test_native_execution.py
venv/bin/python -m unittest discover -s jev_tools/jev-desktop/tests -q
```

The JevDuo adapter enables the guarded clipboard backend by default, with
`desktop.clipboard_text: false` as an explicit override. The SDK's standalone
default remains off. `TYPE_TEXT` still means whole-field replacement; the backend
is considered only after a positively undelivered safe native refusal. It proves
focus and the complete old selection before pasting once and verifies exact AX
readback afterward. Readback verifies field content, not application save/commit.

`RunOptions.resources` binds at most 16 absolute guest paths to finite
`OPEN_RESOURCE:n` choices. The JevDuo caller supplies `agent_input.resources`, or
static `gui_jev.options.resources`. Paths are literal arguments, never shell or
free-form launch flags. Unknown delivery is not retried. See
[the integration contract](../../docs/JEV_CLI_ADAPTATION.md) for runtime options,
native compatibility limitations and verification commands.
