// Records where this GLASS folder is, so the installed app can run (and update) from it.
// Run by `npm run dist` / `npm run dist:mac` before packaging.
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const root = path.join(__dirname, '..');
let commit = '';
try { commit = execFileSync('git', ['rev-parse', '--short', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(); } catch {}
fs.writeFileSync(path.join(root, 'build-info.json'),
  JSON.stringify({ sourceDir: root, commit, builtAt: new Date().toISOString() }, null, 2));
console.log(`build-info.json: GLASS folder ${root} (${commit || 'no git'})`);
