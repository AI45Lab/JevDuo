#!/usr/bin/env node

const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { mkdirSync, readFileSync, writeFileSync } = require('node:fs');
const { join } = require('node:path');
const { PLATFORMS, tarballName } = require('../npm/lib/platform');
const { checksumFor } = require('../npm/scripts/postinstall');

const archiveDirectory = process.argv[2];
if (!archiveDirectory) throw new Error('Usage: node scripts/prepare-npm-binaries.js ARCHIVE_DIRECTORY');
const npmDirectory = join(__dirname, '..', 'npm');
const { version } = require('../npm/package.json');
const checksums = readFileSync(join(archiveDirectory, 'checksums.txt'), 'utf8');
const binDirectory = join(npmDirectory, 'bin');
mkdirSync(binDirectory, { recursive: true });

for (const [key, entry] of Object.entries(PLATFORMS)) {
  if (!entry.released) continue;
  const archiveName = tarballName(version, entry.target);
  const archivePath = join(archiveDirectory, archiveName);
  const digest = createHash('sha256').update(readFileSync(archivePath)).digest('hex');
  if (digest !== checksumFor(checksums, archiveName)) {
    throw new Error(`Checksum mismatch for ${archiveName}`);
  }
  const listing = execFileSync('tar', ['-tzf', archivePath], { encoding: 'utf8' })
    .trim().split(/\r?\n/).sort();
  if (JSON.stringify(listing) !== JSON.stringify([...entry.entries].sort())) {
    throw new Error(`Unexpected archive entries for ${archiveName}`);
  }
  for (const name of entry.entries) {
    const destinationName = name === entry.entries[0] ? entry.binaryName : `${name}-${key}`;
    const payload = execFileSync('tar', ['-xOzf', archivePath, name], {
      maxBuffer: 32 * 1024 * 1024,
    });
    if (payload.length === 0) throw new Error(`Empty executable: ${archiveName}/${name}`);
    writeFileSync(join(binDirectory, destinationName), payload, { mode: 0o755 });
    writeFileSync(join(binDirectory, `${destinationName}.sha256`),
      `${createHash('sha256').update(payload).digest('hex')}\n`);
  }
}
