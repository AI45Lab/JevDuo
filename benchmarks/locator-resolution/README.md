# Locator resolution benchmark

A deterministic harness that compares the legacy full-snapshot locator path with the handle-free observed-tree evaluator. Both run over synthetic Chromium and Electron accessibility trees. The trees cover deep anonymous wrapper chains, duplicate role and name candidates, moving bounds, `AXIdentifier` and `AXDOMIdentifier` set at once, containment predicates and large trees.

Run it without network access:

```bash
cargo run -p agent-desktop-core --release --example locator_benchmark \
  > /tmp/agent-desktop-locator-synthetic.json
```

The JSON report contains:

- p50 and p95 wall-clock latency over 31 measured runs
- Candidate nodes, predicate work and the synthetic attribute values requested
- Cardinality and correctness

The three live modes measure different things:

- `live_find_selected_refs` uses the same selected-match-only materialization as the default CLI `find` path.
- `live_arena_direct` is a lower-overhead direct-target metric, reported separately.
- `live_count_no_refmap` checks that count-only requests perform no action or settable-evidence reads.

The harness does not report absolute native AX call counts, because role-dependent action and settable probes make that count inaccurate. Native Slack and Electron measurements need Accessibility permission. Run them with the opt-in live harness, `tests/e2e/electron-live.sh`, not with this benchmark.

To produce the repo-wide report, run `bash scripts/perf-baseline-compare.sh`, optionally with `--apps "Slack,Google Chrome"`. It writes `report.html`. The report covers the HEAD vs merge-base fixture A/B, the optional read-only app probes and this benchmark.

## Electron and Chromium activation (macOS)

For Electron and Chromium apps, the macOS adapter turns on the accessibility tree before it reads the app. It sets `AXManualAccessibility` when the app supports it, and otherwise `AXEnhancedUserInterface`. It skips the set when the value is already `true`. It then waits, within the request deadline, until the attribute reports ready. Native apps that do not expose the attribute are left unchanged. This follows [Electron's accessibility guidance for third-party assistive software](https://github.com/electron/electron/blob/main/docs/tutorial/accessibility.md). The benchmark reads readiness after the set, because a successful set does not mean the tree is ready.
