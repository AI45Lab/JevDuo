# macOS platform findings

Evidence ledger for macOS accessibility behavior that the adapter relies on. Each row maps one experiment to what was observed. The probe sources for the M1 rows are in `ax-remote-token/` (`tok.c`, `scan.c`, `bench.c`, `unapproved.m`, `cover.c`, `web.c` with `rows.html`); build each with `clang -framework ApplicationServices <file>.c`.

## How to read a row

| column | meaning |
| --- | --- |
| `id` | stable row id (`M<area>-<n>`) |
| `probe` | the probe and step that produced the row |
| `scope` | `api-contract` holds for any app that exposes accessibility; `app` is specific to the measured app |
| `observed` | the measured values the conclusion rests on |
| `conclusion` | what the row establishes |

## Environment

macOS 26.4 (arm64) in a lume VM, logged-in GUI session, accessibility granted to the SSH context. Apps: the E2E fixture app (`tests/fixture-app`, SwiftUI, ref-churn view) and Finder (AppKit, list view of a folder of plain files).

## M1: durable cross-process element tokens

| id | probe | scope | observed | conclusion |
| --- | --- | --- | --- | --- |
| M1-1 | `probe.c` symbol lookup via `dlsym` | api-contract | `_AXUIElementRemoteTokenCreate` and `_AXUIElementCreateWithRemoteToken` resolve in ApplicationServices; `_AXUIElementCopyRemoteToken` and `AXUIElementCreateWithRemoteToken` do not exist | The private pair exists on macOS 26.4. It is private API: look it up at runtime and fall back when absent. |
| M1-2 | `tok.c dump`, then `tok.c resolve` in a new process, fixture rows sharing identifier `shared-title` | api-contract | Alpha token `f802…6f636f6335…` and Beta token `…36…` each resolve in a new process to the same role, identifier and value (`Alpha`, `Beta`) | A token created in one process resolves to the same element in another. Format: pid, a 4-byte tag, a per-app element id (20 bytes; Finder list rows use a 48-byte form). |
| M1-3 | fixture: filter out the Alpha row, resolve both saved tokens | api-contract | Alpha token: `kAXErrorInvalidUIElement` (-25202). Beta token: still Beta. | A removed element's token dies. It does not land on the row that shifted into its place, which is the wrong-row case that attribute matching cannot rule out. |
| M1-4 | fixture: restore rows (a new, identical Alpha), compare tokens | api-contract | new Alpha token `…44…` differs from the old `…35…`; the old token still fails with -25202 | Tokens are not reused for an identical replacement, so a dead token never resurrects onto a look-alike. |
| M1-5 | fixture: restart the app, resolve a saved token | api-contract | -25204 (cannot complete) | Tokens die with the process; the process-generation check stays as a second guard. |
| M1-6 | fixture: three unrelated list re-renders, resolve the `stable-editor` token | app | still resolves to `stable-editor` | Tokens survive re-renders of unrelated parts of a SwiftUI view. |
| M1-7 | Finder list view: `scan.c dump` (253 elements, 55 rows), add 3 files so rows re-sort and shift, `scan.c check` | app | same=252, dead=0, changed=1 (the "8 items" status text became "11 items") | Tokens keep resolving through list re-sorting and row shifts in a dense AppKit list. `scan.c` compares role and label only, so this row does not by itself prove the element behind each token is the original object; M1-3 and M1-4 do that for the fixture. |
| M1-8 | Finder: delete `delta.txt`, `scan.c check` | app | same=243, dead=9 (the deleted row's elements), changed=1 | Deleting a row kills that row's tokens, and no saved token resolved to an element with a different role or label. |
| M1-9 | Finder: rename `golf.txt` to `golf2.txt`, `scan.c check` | app | the renamed row's token resolves to the same element showing `golf2.txt` | A token follows its element through a text change; identity is the object, not its label. |
| M1-10 | `bench.c` in Finder: walk to `hotel.txt`, then resolve its token 50 times | app | tree walk 53.2 ms over 206 elements; token reconstruction plus one role read: min 0.024 ms, avg 0.028 ms | Reconstructing an element from a token is far cheaper than a tree walk. This measures the token call and one read, not the full guarded resolver, which adds process, window and role checks. |
| M1-11 | `unapproved.m`: an approved SSH context creates a token for the fixture's `stable-editor` field; an ad hoc signed app launched through LaunchServices (so TCC charges the app) receives the token, creates the element, reads its value, sets it to `poisoned-by-unapproved` and presses it | api-contract | unapproved app: `AXIsProcessTrusted=0`, element created, read -25211, set -25211, press -25211 (`kAXErrorAPIDisabled`); the field still reads `Stable value` afterwards | A token is an address, not a capability: every use still requires the caller's own Accessibility permission. Storing tokens in the session refmap grants nothing an unapproved app could not already guess, and adds no shared process that could lend permissions. |
| M1-12 | `cover.c`: walk every element of an app (children and windows), create each element's token, recreate the element from it and compare with `CFEqual` | api-contract | Finder: 685 elements, 685 tokens, 685 equal. Safari on a Wikipedia article: 417 / 417 / 417. Chrome 148 with the same article: 9906 / 9906 / 9906. Token sizes 12 to 44 bytes | Every element exposed through accessibility has a token, across AppKit, WebKit and Chromium content, and every token reconstructs the identical element. |
| M1-13 | `web.c` + `rows.html` in Chrome: capture tokens for four identical `Delete` buttons, then the page removes one row, edits another row's text, and finally re-renders the whole list with `innerHTML` | app | t0 all live; after removing Beta: Beta dead, the other three live; after the text edit: unchanged; after the full re-render: all four dead | In Chromium web content, identical twins each keep their own token, a removed node's token dies without landing on a neighbor, a text change keeps the token, and replaced DOM nodes get new elements, so refs to them become stale. |

Not yet measured: non-reuse across providers other than the fixture and Chrome web content, Electron apps, apps that rebuild their accessibility tree on every refresh, and behavior across macOS major versions. A token that dies more often only makes a ref return `STALE_REF`; it never resolves to another element.
