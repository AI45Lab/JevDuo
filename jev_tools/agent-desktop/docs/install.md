# Installing agent-desktop

Install the `agent-desktop` npm package with npm, pnpm, Yarn, or Bun. It needs Node 18 or later and bundles native executables and platform helpers for macOS, Windows, and Linux, on x64 and ARM64:

```bash
npm install -g agent-desktop
agent-desktop version
```

Without `-g`, the command goes to `./node_modules/.bin` instead of your PATH. Run it with `npx agent-desktop`, or install globally.

## Homebrew

On macOS and Linux, install from Homebrew's default catalog:

```bash
brew install agent-desktop
agent-desktop version
```

The formula lives in `Homebrew/homebrew-core`. It builds from the tagged source, installs the platform helper beside
the CLI, and Homebrew manages its own bottles. Homebrew's automation picks up new release tags, so a new version can
reach Homebrew after npm and GitHub Releases. Upgrade with `brew upgrade agent-desktop`.

## Scoop and older installations

There is no official Scoop package yet. This repository no longer maintains a custom Homebrew tap or Scoop bucket;
existing custom installations remain pinned to their installed version.

If an older installation shadows the command, uninstall it through its original package manager. Check resolution
with `command -v agent-desktop` on macOS/Linux or `Get-Command agent-desktop` on Windows.

## Blocked install scripts

The npm package's `postinstall` script replaces the Node launcher with a link to the native binary. Some package managers block install scripts, and npm 12 does by default. Then every call runs through the Node launcher, which adds about 125 ms. The launcher prints a one-time notice to stderr. Allow the script and reinstall:

```bash
npm install -g agent-desktop --allow-scripts=agent-desktop
```

For a project install, run `npm install-scripts approve agent-desktop` inside the project, then `npm rebuild agent-desktop`. The approve command only edits the project's `package.json` and fails for a global install.

## Release archives

Download the archive for your OS and architecture from [GitHub Releases](https://github.com/vercel-labs/agent-desktop/releases). Extract the executable and its platform helper into one directory on your PATH. To upgrade, replace both files with the ones from the same release.

The release's `checksums.txt` catches corrupted downloads but does not prove where the archive came from. Verify its origin with the build attestation:

```bash
gh attestation verify <downloaded-archive> --repo vercel-labs/agent-desktop
```

## From source

You need the Rust toolchain pinned in `rust-toolchain.toml` (1.89.0). `rustup` installs it on first use. You also need a C toolchain: the Xcode command-line tools on macOS 13 or later, the MSVC build tools on Windows, or `build-essential` on Linux.

```bash
git clone https://github.com/vercel-labs/agent-desktop && cd agent-desktop
cargo build --release -p agent-desktop
```

Each platform crate builds only on its own OS. Build the helper for your platform. A source build puts it in `target/release/`, next to the CLI. Windows has no helper.

```bash
cargo build --release -p agent-desktop-macos --bin agent-desktop-macos-helper   # macOS
cargo build --release -p agent-desktop-linux --bins                             # Linux
```

The CLI looks for the helper in its own directory, so move the two together.

## Permissions

**macOS** needs Accessibility permission. Screenshots also need Screen Recording. Some commands need Automation permission for System Events, and report it when it is missing. `agent-desktop permissions` reports the state without prompting. `permissions --request` shows the system prompts. `permissions --guide` opens System Settings with an on-screen guide for Accessibility and Screen Recording.

**Windows** needs no grant for apps at your terminal's integrity level. An app running as administrator can be read, but only an elevated agent can send it input. The binary is unsigned. SmartScreen may warn on a browser download, and antivirus may quarantine it. On a clean Windows 11 install, Smart App Control blocks it, and no download method or launch mode avoids that. Turn Smart App Control off to run it. npm installs do not trigger SmartScreen. To restore a quarantined file, first check its SHA-256 checksum against the release.

**Linux** is primarily validated on Ubuntu 24.04 or later on x86_64 or aarch64, with a glibc 2.35 floor. Action enumeration uses `NActions` and `GetName(i)` to avoid the `GetActions` crash in older ATK bridges shipped with Ubuntu 22.04; full desktop support on 22.04 is not established. Run the CLI inside the user's graphical session, with `at-spi2-core` installed. A TTY, an SSH shell, or a container without that session's bus, display, and runtime environment sees no apps. `agent-desktop status` activates and checks the accessibility bus. An empty `supported_surfaces` list means the bus did not answer.

Keys need XTEST on X11 and GNOME Mutter RemoteDesktop on Wayland. The Wayland clipboard needs `wl-clipboard`. For the rest, run `agent-desktop skills get platform` on the Linux machine.

## Platform notes

**Windows.** `list-surfaces` lists each process's `window`, `focused`, `sheet`, and `menu` surfaces. `snapshot --surface` also reads `taskbar`, `system-tray`, `system-tray-overflow`, `start-menu`, and `action-center` without `--app`. `quick-settings` returns `PLATFORM_NOT_SUPPORTED` and names `action-center` as the surface that carries those controls.

`launch` takes an absolute path, or a bare name under System32 or the Windows directory, with or without `.exe`. It does not resolve a display name such as "Google Chrome". Use `list-apps` to find a running app.

**Linux.** Semantic actions work on X11 and Wayland. On Wayland, app pointer input returns `ACTION_NOT_SUPPORTED`, and window control is refused for native windows.
