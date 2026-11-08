# Working on NorthCUA

This repository publishes the Desktop/Web **NorthCUA Harness**.
CLI, packages and imports retain `northcua`. Public NexAU is pinned in the lock.
Read [clean scope](docs/clean-scope.md) before expanding runtime dependencies.

To operate the project, load [the NorthCUA skill](skills/northcua/SKILL.md). This file
is for source contributions, not a replacement for its usage tutorials.

- Keep changes in the requested scope. No NorthJetty, private inventories,
  one-off production repair scripts, data generation or training dependencies.
- Desktop has one tool-returned-image path. Preserve note/image grounding,
  evaluator lifetime, task ownership, artifact custody and cleanup guarantees.
- Model credentials stay out of tracked files, logs and public examples.
- Public app templates are mirrored in `config/` and `northcua/resources/config/`;
  update both when changing them. Wheels must work outside the source tree.
- Keep tutorial fixtures reusable and isolated. Native tests operate only on
  their disposable window. Coordinate before another reviewer uses the display.

Use uv 0.13.6+ (older releases can omit Darwin PyObjC dependencies).
Install development extras with
`uv sync --locked --extra local --extra device --extra dev --extra e2b --extra viewer`.
Use `uv run --no-sync ruff check .`, `ruff format --check .`, configured `pyright`
and the relevant pytest suite. [Testing](docs/testing.md) describes opt-in real
browser/native checks. New lifecycle behavior needs failure and cleanup coverage;
documentation needs working commands, not tests that merely match headings.

Record verification from actual output. Distinguish scripted model integration,
real hosted-model tasks, cloud provisioning and platform acceptance. Review
signoff must state the reviewed revision/scope and remaining limitations. Do not
upgrade earlier scoped evidence into an unqualified release claim.
