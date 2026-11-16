const { readFileSync } = require('node:fs');

function releaseVersion(toml) {
  const section = toml.split(/^\[workspace\.package\][ \t]*\r?$/m)[1]?.split(/^\[/m)[0];
  const version = section?.match(/^version[ \t]*=[ \t]*"([^"\r\n]+)"[ \t]*\r?$/m)?.[1];
  if (!version || !/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$/.test(version)) {
    throw new Error('Cargo.toml must contain a valid workspace.package.version');
  }
  return version;
}

if (require.main === module) {
  console.log(releaseVersion(readFileSync(process.argv[2] === '-' ? 0 : 'Cargo.toml', 'utf8')));
}
module.exports = { releaseVersion };
