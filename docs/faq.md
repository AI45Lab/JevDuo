# agent-desktop FAQ

## What is agent-desktop?

agent-desktop is a CLI and C library. It reads the accessibility tree of desktop apps on macOS, Windows, and Linux and returns it as JSON. Each element an agent can act on gets a ref. The calling agent runs the observe, act, and verify loop, because agent-desktop has no model inside. It does not automate web pages. Use [agent-browser](https://agent-browser.dev) for those. The [introduction](https://agent-browser.dev/desktop) covers the rest.

## Does agent-desktop require screenshots or pixel matching?

No. Commands that take a ref find the element in the accessibility tree, not by matching pixels. `screenshot` is a separate command. An app that publishes no accessibility tree is not visible to agent-desktop.

## How does agent-desktop work?

`snapshot` reads an app's accessibility tree and prints it as JSON, with a ref for each element an agent can act on. Actions take a ref from a snapshot, and each command prints one JSON envelope. The [introduction](https://agent-browser.dev/desktop) shows the components, from the CLI or C library down to the platform accessibility API.

## Which platforms are supported?

macOS, Windows, and Linux. The [platform table in the README](../README.md#platform-support) lists each requirement. `agent-desktop skills get platform` prints the platform guide for the OS the binary runs on.

## How do I install agent-desktop?

```bash
npm install -g agent-desktop
```

[Installation](https://agent-browser.dev/desktop/installation) covers the other package managers, release archives, and building from source. [docs/install.md](install.md) has the same details for GitHub readers.

## How do I build the C library from source?

```bash
cargo build --profile release-ffi -p agent-desktop-ffi
```

The library is written to `target/release-ffi/`: `libagent_desktop_ffi.dylib` or `.so` on macOS and Linux, `agent_desktop_ffi.dll` on Windows. Only the `release-ffi` profile traps panics at the boundary and returns them as `AD_RESULT_ERR_INTERNAL`. A plain `--release` build does not. A source build also needs the platform helper beside the library. See [Build and link](https://agent-browser.dev/desktop/ffi/build-and-link).

## What is the ref system?

`snapshot` assigns refs in depth-first order. A ref carries the ID of the snapshot it came from, such as `@s8f3k2p9:e1`. Pass the full form to actions. A bare `@e1` fails with `INVALID_ARGS`.

An element gets a ref when its role is one of these, or when it advertises a primary action:

`button`, `textfield`, `checkbox`, `link`, `menuitem`, `tab`, `slider`, `combobox`, `treeitem`, `cell`, `radiobutton`, `incrementor`, `menubutton`, `switch`, `colorwell`, `dockitem`, `listbox`, `option`.

Snapshot lookup stays inside the selected session. A ref from a session needs that session's `--session` flag or `AGENT_DESKTOP_SESSION` value. When an action returns `STALE_REF`, take a new snapshot and retry with a ref from it.

## How does an agent learn the tool?

Run `agent-desktop skills get desktop` for the core guide. Run `agent-desktop skills get platform` for the guide for the OS the binary runs on. Run `agent-desktop <command> --help` for a command's flags.

## Is agent-desktop free and open source?

Yes. It is licensed under [Apache-2.0](../LICENSE).

## What data can contain private information?

Snapshots, window titles, clipboard reads, notifications, screenshots, and command results contain the desktop content you asked to read. `--debug --screenshot PATH.html` writes a local HTML page with a screenshot and the accessibility data. Session traces redact sensitive structured fields, but they can keep evidence artifacts. Keep these files and your agent's transcripts private. Redaction does not make a screenshot safe to publish.

The native CLI does not require a remote model service. The optional [JEV helper](../skills/jev-desktop/SKILL.md) sends your goal and screen descriptions to the model provider you configure. Use it only with data that provider may see.

Probe captures under `probes/` record technical details such as OS versions and window geometry. Review them before publishing, even after you redact identity fields.

## Where can I get help?

See [SUPPORT.md](../SUPPORT.md) for what to include in a bug report. Report security issues privately, as described in [SECURITY.md](../SECURITY.md).

- [Issues](https://github.com/vercel-labs/agent-desktop/issues)
- [Releases](https://github.com/vercel-labs/agent-desktop/releases)
- [npm package](https://www.npmjs.com/package/agent-desktop)
