// node --test electron/   — the self-updater against real git repositories (npm/pip are stubbed).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');
const { Updater, run } = require('./updater.cjs');

const git = (cwd, ...args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();

function setup() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'glass-upd-'));
  const remote = path.join(dir, 'remote.git');
  const dev = path.join(dir, 'dev');
  const student = path.join(dir, 'student');
  git(dir, 'init', '--bare', '-b', 'main', remote);
  git(dir, 'clone', remote, dev);
  for (const r of [dev]) { git(r, 'config', 'user.email', 't@t'); git(r, 'config', 'user.name', 't'); }
  fs.writeFileSync(path.join(dev, 'package.json'), JSON.stringify({ name: 'glass', devDependencies: { electron: '44.7.0' } }));
  fs.mkdirSync(path.join(dev, 'engine'));
  fs.writeFileSync(path.join(dev, 'engine', 'requirements.txt'), 'numpy\n');
  fs.writeFileSync(path.join(dev, 'app.js'), 'v1\n');
  git(dev, 'add', '-A'); git(dev, 'commit', '-m', 'first'); git(dev, 'push', 'origin', 'main');
  git(dir, 'clone', remote, student);
  git(student, 'config', 'user.email', 's@s'); git(student, 'config', 'user.name', 's');
  return { dir, dev, student };
}

function push(dev, file, content, msg) {
  fs.writeFileSync(path.join(dev, file), content);
  git(dev, 'add', '-A'); git(dev, 'commit', '-m', msg); git(dev, 'push', 'origin', 'main');
}

function updater(root, calls) {
  // real git; npm and pip are recorded instead of run
  return new Updater(root, {
    python: '/venv/python',
    run: (cmd, args, cwd, onLine) => {
      if (cmd === 'git') return run(cmd, args, cwd, onLine);
      calls.push(`${path.basename(cmd)} ${args.join(' ')}`);
      return Promise.resolve({ code: 0, out: '' });
    },
  });
}

test('check reports what is new without touching the folder', async () => {
  const { dev, student } = setup();
  push(dev, 'app.js', 'v2\n', 'Snip reads powers from the image');
  const u = updater(student, []);
  const info = await u.check();
  assert.equal(info.ok, true);
  assert.equal(info.available, true);
  assert.equal(info.behind, 1);
  assert.equal(info.changes[0].subject, 'Snip reads powers from the image');
  assert.equal(fs.readFileSync(path.join(student, 'app.js'), 'utf8'), 'v1\n');
});

test('update pulls, only rebuilds when no package lists changed', async () => {
  const { dev, student } = setup();
  push(dev, 'app.js', 'v2\n', 'change');
  const calls = [];
  const steps = [];
  const res = await updater(student, calls).update((s, line) => { if (!line) steps.push(s); });
  assert.equal(res.ok, true, res.message);
  assert.equal(res.restart, true);
  assert.equal(fs.readFileSync(path.join(student, 'app.js'), 'utf8'), 'v2\n');
  assert.deepEqual(calls, ['npm run build']);
  assert.deepEqual(steps, ['Downloading the update', 'Building the app']);
  assert.equal((await updater(student, []).check()).available, false);
});

test('changed package lists reinstall npm and pip packages (engine stopped first)', async () => {
  const { dev, student } = setup();
  fs.writeFileSync(path.join(dev, 'engine', 'requirements.txt'), 'numpy\nsympy\n');
  push(dev, 'package.json', JSON.stringify({ name: 'glass', devDependencies: { electron: '44.7.0' }, dependencies: { katex: '1' } }), 'deps');
  const calls = [];
  let stopped = false;
  const res = await updater(student, calls).update(() => {}, { beforePip: async () => { stopped = true; } });
  assert.equal(res.ok, true, res.message);
  assert.equal(stopped, true);
  if (process.platform !== 'darwin') {
    assert.deepEqual(calls, ['npm install --no-audit --no-fund',
      `python -m pip install --disable-pip-version-check -r ${path.join(student, 'engine', 'requirements.txt')}`, 'npm run build']);
  }
  assert.doesNotMatch(res.message, /Electron/);
});

test('an Electron upgrade is reported (the launcher must be reinstalled once)', async () => {
  const { dev, student } = setup();
  push(dev, 'package.json', JSON.stringify({ name: 'glass', devDependencies: { electron: '45.0.0' } }), 'electron');
  const res = await updater(student, []).update();
  assert.equal(res.ok, true);
  assert.match(res.message, /Electron/);
});

test('files changed on this PC are kept as a backup, not lost', async () => {
  const { dev, student } = setup();
  push(dev, 'app.js', 'v2\n', 'change');
  fs.writeFileSync(path.join(student, 'package.json'), '{"name":"glass","local":true}');
  const res = await updater(student, []).update();
  assert.equal(res.ok, true, res.message);
  assert.match(git(student, 'stash', 'list'), /GLASS update backup/);
});

test('already up to date and not-a-repo are explained', async () => {
  const { student } = setup();
  assert.match((await updater(student, []).update()).message, /up to date/);
  const plain = fs.mkdtempSync(path.join(os.tmpdir(), 'glass-plain-'));
  const info = await updater(plain, []).check();
  assert.equal(info.ok, false);
  assert.equal(info.reason, 'not_repo');
});
