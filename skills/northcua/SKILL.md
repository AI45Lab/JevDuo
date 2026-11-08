---
name: northcua
description: Install, configure and operate the NorthCUA Harness, including Desktop/Web tasks, local browser batches and evidence checks. Use for running this project, not its training or data-generation research.
---

# Operate NorthCUA from a clean checkout

The project is **NorthCUA**, the shipped component is **NorthCUA Harness**, and the
package/CLI is `northcua`. This skill is distributed with the repository. Resolve
the real location of this file (including symlinks); the repository root is two
directories above its folder. Run commands there, or use an installed release
and the corresponding version of these guides. Do not copy this file alone:
its linked guides are part of the skill's operating contract.

For a new installation, use the `main` branch of
[NorthCUA](https://github.com/beipo-ai/NorthCUA), following
the first-run guide's clone/install commands. Check [resource availability](../../docs/resources.md)
for the current report and model links; do not assume a referenced model
checkpoint has been published or that this project is already available on PyPI.

## Choose the user's workflow

- **Nothing installed / first task:** follow [from-zero setup](../../docs/first-run.md).
  It includes Bash/zsh and PowerShell setup, model templates, native permissions,
  a credential-free browser/native check and success criteria.
- **Model or parameter configuration:** [configuration](../../docs/configuration.md).
  `config init --model ... --base-url ...` populates both agents. The user normally
  supplies only model, key and optional endpoint via [the env template](../../.env.example).
  Read [models](../../docs/models.md) for endpoint requirements and weight availability.
  Use the matching shell instructions: PowerShell does not source a Bash `.env` file.
  Keep credentials as environment references; do not ask for keys in chat.
  Before a Desktop run, match the model's coordinate convention: for 0–1000
  relative output, set `desktop.coordinate_policy.input_space: normalized_1000`
  in the app YAML used by that run; `clamp_to_bounds: true` keeps edge positions
  inside the capture. The default is `image_pixels`. Read
  [coordinate configuration](../../docs/configuration.md#desktop-coordinates)
  for the mapping. Web refs and viewport CSS-pixel actions follow their own
  contract; this Desktop setting does not change them.
- **Several local tasks / recovery:** follow [local batch](../../docs/local-batch.md)
  end to end. It starts a real controller, real browser workers and the existing
  scheduler without cloud services. Then consult [batch operations](../../docs/batch.md).
- **Python, serving, remote device:** [execution modes](../../docs/running.md).
  Local application WebSocket and controller submission are different protocols.
- **Custom runtime skill:** [skill loading](../../docs/skills.md).
- **Failure, missing browser, model error or stuck task:** [troubleshooting](../../docs/troubleshooting.md).
- **Result inspection / evaluation:** [evaluation](../../docs/evaluation.md).

Read the chosen tutorial before executing; it contains dependencies and expected
outputs. Do not infer command flags from another CUA project or old experiment
scripts. Use `northcua <group> --help` for optional controls and `config schema`
for current app/batch fields. `config validate` does not call the model.

## Execution contract

1. Determine the requested target (Web or native Desktop), execution mode, model
   endpoint and success criterion. Use the local scripted demo when checking the
   installation without credentials; label it as such. Use the user's real
   model for their actual task. Do not substitute demo success for task success.
2. Use uv 0.13.6+ for source or wheel installation; older uv releases can omit
   Darwin PyObjC dependencies. First read the [known upstream dependency risk](../../docs/security/dependencies.md):
   the retained NexAU constraint requires affected Transformers 4.x; do not load
   untrusted tokenizer/model configuration or treat the policy exception as a fix.
   Install from this repository's lock or matching release wheels. Activate the
   environment in every terminal. `uv run --no-sync` preserves installed extras.
   Create configuration in a new writable directory; preserve existing outputs.
3. Run one bounded task first. Desktop GUI tools return screenshots directly;
   there is no alternative observation mode. A physical display is exclusive:
   do not run concurrent Desktop tasks or use another agent's GUI session.
4. Inspect terminal status, the actual outcome, saved trace and evaluator verdict
   separately. A passing configuration check or actor completion is not outcome
   verification. Retain failed evidence and report unverified modes honestly.
5. For local batching use `local-process`, real browser workers and the existing
   batch commands. The `fake` provider is only a scheduling simulation. Start
   with concurrency 1–2; keep task IDs/output paths stable for resume. Drain and
   wait for the current runner to exit before resuming. Do not hand-edit state DBs.
6. Stop the processes you started and confirm cleanup. Preserve results for the
   user. The local provider shares the host OS and network; it is not a VM.

The self-contained path needs no NorthJetty, cloud key, private dataset or
historical deployment checkout. E2B deployments and prepared Desktop HTTP
endpoints are optional environments managed separately. Use the
[recommended Desktop settings](../../examples/desktop-settings/README.md) through
the ordinary Desktop entry points when those model/prompt recipes are useful.

Report the exact commands/mode, source revision, model type (scripted or real),
actual outcomes, evidence paths, recovery/cleanup result and any remaining blocker.
For an independent acceptance run, use a fresh output directory and follow these
instructions without relying on author-provided success claims.
