const { existsSync, realpathSync, readFileSync, writeFileSync } = require('fs');
const { dirname, join, relative, resolve } = require('path');

module.exports = function linkWindowsBin(directory, binaryName) {
  const binary = resolve(directory, binaryName);
  if (!existsSync(binary) || /[%!\r\n"]/.test(binary)) return false;
  const entry = join(directory, 'agent-desktop');
  let ancestor = directory;
  let linked = false;
  for (;;) {
    for (const destination of [ancestor, join(ancestor, '.bin')]) {
      const targets = [relative(destination, entry).replace(/\\/g, '/')];
      const alias = join(ancestor, 'agent-desktop', 'bin', 'agent-desktop');
      try {
        if (existsSync(alias) && realpathSync(alias) === realpathSync(entry)) {
          targets.push(relative(destination, alias).replace(/\\/g, '/'));
        }
      } catch {}
      const matches = (contents) => targets.some((target) =>
        contents.includes(`${target}"`) || contents.includes(`${target}'`));
      const command = join(destination, 'agent-desktop.cmd');
      try {
        const contents = readFileSync(command, 'utf8').replace(/\\/g, '/');
        if (!matches(contents)) continue;
        writeFileSync(command, `@echo off\r\n"${binary}" %*\r\nexit /b %errorlevel%\r\n`);
        const powershell = join(destination, 'agent-desktop.ps1');
        if (existsSync(powershell) && matches(readFileSync(powershell, 'utf8').replace(/\\/g, '/'))) {
          writeFileSync(powershell, `& '${binary.replace(/'/g, "''")}' @args\r\nexit $LASTEXITCODE\r\n`);
        }
        linked = true;
      } catch (error) {
        if (error.code !== 'ENOENT') process.stderr.write(`agent-desktop: Native command optimization skipped: ${error.message}\n`);
      }
    }
    const parent = dirname(ancestor);
    if (parent === ancestor) return linked;
    ancestor = parent;
  }
};
