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

The 17 Python source modules were copied without code changes from the frozen
`o21-30x30-1112-111127/source-snapshot/jev_desktop/` evaluation snapshot. Packaging
metadata was restored from the original `jev/pyproject.toml` (version 0.1.0). The
Apache-2.0 license is retained from the original agent-desktop project. This Python
package was not part of the Rust repository's `ubuntu2204` commit.

The source is included here, so archived runs and the former sibling checkout are
not required for installation. The subtree import records this restored package as
a local source baseline; there is no separate public Python-package remote yet.

From the JevDuo root, the existing integration tests exercise this package through
scripted HTTP and fake desktop responses, without contacting a VM or model service:

```bash
venv/bin/python -m pytest -q tests/test_jev_subagent.py tests/test_jev_feedback.py tests/test_native_execution.py
```
