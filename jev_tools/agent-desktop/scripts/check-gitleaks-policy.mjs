import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const gitleaks = path.resolve(process.argv[2] ?? path.join(repoRoot, "gitleaks"));
const config = path.join(repoRoot, ".gitleaks.toml");
const scratch = mkdtempSync(path.join(os.tmpdir(), "agent-desktop-gitleaks-"));
const credential = ["ghp_", "012345678901234567890123456789012345"].join("");

function scan(files) {
  const root = path.join(scratch, `scan-${Math.random().toString(16).slice(2)}`);
  for (const [name, content] of Object.entries(files)) {
    const file = path.join(root, name);
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, content);
  }

  const result = spawnSync(gitleaks, [
    "dir", root, "--config", config, "--report-format", "json", "--report-path", "-",
    "--redact", "--no-banner", "--no-color",
  ], { encoding: "utf8" });
  assert.equal(result.error, undefined, result.error?.message);
  assert.ok(result.status === 0 || result.status === 1, result.stderr);
  return JSON.parse(result.stdout);
}

function git(root, args) {
  const result = spawnSync("git", ["-C", root, ...args], { encoding: "utf8" });
  assert.equal(result.error, undefined, result.error?.message);
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}

const benign = scan({
  "paths.txt": ["/home/example-user/docs\n/Users/runner/work/project\n/home/linuxbrew/.linuxbrew/bin\n"].join(""),
  "addresses.txt": [
    "person@example.com",
    "org.gnome.Shell@wayland.service",
    "user@1000.service",
    "ubuntu-appindicators@ubuntu.com",
    "noreply@github.com",
    "12345+agent@users.noreply.github.com",
    "secret@router.example",
  ].join("\n"),
  "redacted.txt": "S-1-5-21-<redacted>\n",
});
assert.equal(benign.length, 0, `benign privacy examples triggered: ${benign.map((f) => f.RuleID).join(", ")}`);

for (const prefix of ["/home/example-user", "/home/linuxbrew"]) {
  const boundary = scan({ "paths.txt": `${prefix}2/docs\n` });
  assert.ok(boundary.some((finding) => finding.RuleID === "ad-home-directory-path"),
    "a username extending a documented placeholder was allowlisted");
}

const findings = scan({
  "Cargo.lock": `token = ${credential}\n`,
  "tests/fixtures/credential.json": `token = ${credential}\n`,
  "mixed.txt": `token = ${credential} # <redacted> noreply@example.com\n`,
  "assets/Videos/demo.mp4": `token = ${credential}\n`,
  "assets/Videos/address.mp4": "operator@example.com\n",
});
assert.equal(findings.filter((finding) => finding.RuleID === "github-pat").length, 4,
  "credential rules must scan lockfiles, fixtures, mixed marker lines, and media");
assert.ok(!findings.some((finding) => finding.RuleID === "ad-personal-email" && finding.File.endsWith("address.mp4")),
  "the custom email privacy rule should ignore compressed media bytes");

const historyRoot = path.join(scratch, "history-positive-control");
const historyFile = "docs/plans/2026-08-16-001-feat-windows-2-12-fixture-e2e-harness-plan.md";
const historyPath = path.join(historyRoot, historyFile);
const syntheticSid = ["S-1-5-21-", "12345-", "23456-", "34567-", "1001"].join("");
mkdirSync(path.dirname(historyPath), { recursive: true });
writeFileSync(historyPath, `${Array(67).fill("").join("\n")}\nmachine = ${syntheticSid}\n`);
git(historyRoot, ["init", "--quiet"]);
git(historyRoot, ["config", "user.name", "Gitleaks fixture"]);
git(historyRoot, ["config", "user.email", "fixture@example.com"]);
git(historyRoot, ["add", historyFile]);
git(historyRoot, ["commit", "--quiet", "-m", "fixture"]);
const positiveCommit = git(historyRoot, ["rev-parse", "HEAD"]);
const positive = spawnSync(gitleaks, [
  "git", historyRoot, "--log-opts=HEAD", "--config", config,
  "--gitleaks-ignore-path", repoRoot, "--report-format", "json", "--report-path", "-",
  "--redact", "--no-banner",
], { encoding: "utf8" });
assert.equal(positive.error, undefined, positive.error?.message);
assert.equal(positive.status, 1, positive.stderr);
const positiveFindings = JSON.parse(positive.stdout);
assert.ok(positiveFindings.some((finding) => finding.RuleID === "ad-windows-account-sid"
  && finding.File === historyFile && finding.StartLine === 68 && finding.Commit === positiveCommit),
"the exact historical exceptions must not suppress a same-shape SID in another commit");

console.log("OK: Gitleaks exceptions stay exact; credentials and new privacy findings remain detectable.");
