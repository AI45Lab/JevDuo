# Contributing to agent-desktop

agent-desktop is a Rust CLI and C library. It reads native accessibility trees and returns structured JSON with ref-based element ids, so AI agents can observe and act on desktop apps.

## Contributions that fit

- Bug fixes, such as wrong JSON output, incorrect ref resolution, or `STALE_REF` on a stable target.
- New commands. Follow the steps in the Architecture section of [AGENTS.md](AGENTS.md#architecture).
- Platform adapter fixes and features for macOS, Windows and Linux. Each adapter implements `PlatformAdapter`.
- App-specific quirks, such as Electron or game engine edge cases, documented under `skills/`.
- Docs and skill files. Keep `skills/agent-desktop*/` accurate when behavior changes.

Out of scope: embedding LLMs, GUI or TUI interfaces, browser automation (use agent-browser for that), and macro recording.

## Code of conduct

Follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Prerequisites

| Requirement | Notes |
|---|---|
| Rust toolchain | 1.89.0, pinned in `rust-toolchain.toml`. `rust-version` 1.89 is the MSRV. `rustup` installs the pinned channel. |
| macOS 13 or later | Needed to build and run the macOS adapter (`crates/macos/`). Requires the Xcode command-line tools. |
| Windows | Builds the Windows adapter (`crates/windows/`). Windows only. Requires the MSVC build tools. |
| Linux | Builds the Linux adapter (`crates/linux/`). Linux only. Live tests need an AT-SPI session. See `tests/e2e-linux/README.md`. |
| Python 3 | `scripts/check-rust-file-size.sh` runs a Python helper and fails when no Python 3 is on `PATH`. |
| Accessibility permission | macOS, for integration and E2E tests against real apps. Add the terminal you test from under **System Settings > Privacy & Security > Accessibility**. |
| Screen Recording permission | macOS, for `screenshot` tests. Add the terminal under **System Settings > Privacy & Security > Screen Recording**. |

## Getting started

Clone the repository and turn on the pre-commit hook. Run the `git config` line once per clone.

```bash
git clone https://github.com/vercel-labs/agent-desktop
cd agent-desktop
git config core.hooksPath .githooks
```

Every platform adapter compiles only on its own OS, and the workspace names all three. An unscoped `cargo build` or `cargo test --workspace` fails on every host, so scope cargo to the host's packages. The example uses macOS. On Windows, replace `agent-desktop-macos` with `agent-desktop-windows`. On Linux, use `agent-desktop-linux`.

```bash
HOST_PKGS=(-p agent-desktop-core -p agent-desktop-macos -p agent-desktop -p agent-desktop-ffi)

cargo build "${HOST_PKGS[@]}"                  # debug build
cargo build --release -p agent-desktop         # release CLI; CI fails it above 15 MB
./target/release/agent-desktop snapshot --app Finder -i
```

`HOST_PKGS` is an array, so each entry expands to a separate argument in bash and zsh. zsh is the macOS default shell.

## Quality gates

Every gate below must pass before a PR merges. Set `HOST_PKGS` as shown above.

```bash
cargo fmt --all -- --check
cargo clippy "${HOST_PKGS[@]}" --all-targets -- -D warnings
cargo test "${HOST_PKGS[@]}" --lib             # adapter stubs and golden fixtures; skips the binary crate
cargo test -p agent-desktop                    # CLI contract, dispatch, batch and policy tests
cargo test -p agent-desktop-ffi --tests        # C ABI, enum fuzzing and out-param zeroing
cargo tree -p agent-desktop-core               # must name no platform crate
AGENT_DESKTOP_E2E_EXCLUSIVE=1 bash tests/e2e/run.sh   # macOS only; needs an exclusive desktop and Accessibility permission
```

CI runs its test steps through `scripts/cargo-test-isolated-home.sh`, which gives cargo a throwaway `HOME`, so tests never touch `~/.agent-desktop`. The pre-commit hook does the same for its `--lib` step. Run the script locally for the same isolation:

```bash
scripts/cargo-test-isolated-home.sh test "${HOST_PKGS[@]}" --lib
```

`tests/e2e/run.sh` builds the release CLI, the macOS helper and the FFI library itself. It refuses to run without `AGENT_DESKTOP_E2E_EXCLUSIVE=1`, because headed cases move the real cursor. See `tests/e2e/README.md` for what it asserts and how to add a scenario.

CI also checks what a local run can miss:

- The 15 MB limit on each shipped executable.
- The Rust 1.89 MSRV build.
- Drift between the committed header `crates/ffi/include/agent_desktop.h` and the Rust ABI. Regenerate it with `scripts/update-ffi-header.sh`, which needs cbindgen 0.29.4.
- The `cargo-deny` policy in `deny.toml`.
- Secret scanning, CodeQL and workflow linting.

## Pre-commit hook

`.githooks/pre-commit` mirrors the CI gates. The script is the authoritative list. For a commit that stages `.rs` or `.toml` files, the hook runs these over the host's package set:

- `scripts/check-rust-file-size.sh`, `scripts/check-architecture.sh`, `scripts/check-no-phase-references.sh` and `scripts/check-stale-ref-constructor-misuse.sh`
- `cargo fmt --all -- --check`
- Clippy with `-D warnings`
- The `--lib` tests

When the commit touches `crates/ffi/`, the hook also runs the stub-adapter passthrough test and the cbindgen header-drift check. The drift check is skipped when cbindgen 0.29.4 is not installed. CI still runs it.

A commit with no Rust or FFI changes that touches `scripts/`, `.githooks/`, `.github/workflows/` or `tests/e2e-linux/` runs the Bash 3 compatibility and no-phase-reference checks.

Bypass the hook only in an emergency:

```bash
git commit --no-verify
# or
SKIP_PRECOMMIT=1 git commit -m "..."
```

## Engineering rules

[AGENTS.md](AGENTS.md) holds the engineering rules for people and coding agents. They cover Conventional Commits, the 400-line cap per Rust file, no inline comments, no delivery-plan references in shipped source, one domain type per file, error handling, core isolation and the steps for adding a command. The hook and CI enforce most of them.

Skills stay in sync. A new command or a changed CLI flag must update the matching skill. Cross-platform behavior goes in `skills/agent-desktop/`. Platform behavior goes in `skills/agent-desktop-macos/`, `skills/agent-desktop-windows/` or `skills/agent-desktop-linux/`. C ABI changes go in `skills/agent-desktop-ffi/`. The binary embeds these skills and agents read them, so they must match the code.

## Submitting a pull request

1. Branch from `main` and open the PR into `main`. This applies to every platform, core, FFI and Linux work.
2. Keep each PR to one logical change. Separate refactors from features.
3. Pass every quality gate locally before pushing.
4. Use a Conventional Commit title. The squash merge uses it as the commit message.
5. Link related issues with `Fixes #N` or `Refs #N` in the description.
6. If you changed a crate dependency, check that `cargo tree -p agent-desktop-core` is still clean.
7. CI runs on every push. A red CI blocks the merge.

## Publishing source and diagnostic evidence

Publish from a reviewed Git revision or through the release workflow. Never upload the working directory or local internal-document archives. Ignored files can hold agent settings, transcripts, screenshots and personal file paths. Review browser captures and demo recordings before adding them to Git. Ignore rules only stop accidental staging. They do not sanitize files or block force-adds.

Run the secret and privacy scan before publishing. It is gitleaks with `.gitleaks.toml`. Its regression check is `node scripts/check-gitleaks-policy.mjs ./gitleaks`. Credentials must stay detectable in lockfiles, fixtures and lines that also contain safe example values. Review media by eye. A text scanner cannot show that a video, screenshot or audio track is safe.

`.gitleaksignore` holds two exact fingerprints for Windows machine SIDs in an old E2E plan. Those SIDs are not in the current tree and are not credentials. The entries record that the SIDs exist in published history. They do not erase that history, and they do not cover new SIDs, other commits or real credentials. Do not replace them with broad path or rule exclusions.

Commits keep the author and committer name and email. To keep a personal address out of future commits, set your Git email to your verified GitHub noreply address. Changing local Git config does not rewrite history, and existing attribution is not a credential leak. Do not rewrite shared history to clear an audit warning.

## Reporting bugs and security issues

Bugs and feature requests: use the [issue templates](https://github.com/vercel-labs/agent-desktop/issues/new/choose). [SUPPORT.md](SUPPORT.md) lists what to include.

Security vulnerabilities: do not open a public issue. Follow [SECURITY.md](SECURITY.md).

## Writing with AI coding agents

If an AI coding agent writes any prose in your contribution (docs, READMEs, skills, changelog entries, commit messages, PR descriptions or issues), it must use the [`technical-writer` skill](.agents/skills/technical-writer/SKILL.md). The skill sets the repo's voice and asks agents to check technical claims against the source. AGENTS.md tells agents to use it.

## License

Contributions are licensed under the [Apache-2.0 License](LICENSE) that covers this project.
