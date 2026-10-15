// GLASS self-update from its own git checkout.
//
// GLASS runs from the folder it was cloned into (directly with `npm start`, or through the
// installed app, which is a thin launcher for that folder — see main.cjs). Updating is:
//   git fetch -> show what's new -> git pull --ff-only -> reinstall packages only when
//   their lists changed (npm / pip) -> rebuild the UI -> restart.
// Nothing here runs during a lesson; the caller checks that.
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const IS_WIN = process.platform === 'win32';

/** Run a command; resolves {code, out}. `onLine` gets each output line (for the progress log). */
function run(cmd, args, cwd, onLine, env = {}) {
  return new Promise((resolve) => {
    let out = '';
    let p;
    try {
      // npm is a .cmd script on Windows and needs a shell; git and python don't
      p = spawn(cmd, args, { cwd, shell: IS_WIN && cmd === 'npm', windowsHide: true,
        env: { ...process.env, GIT_TERMINAL_PROMPT: '0', ...env } });
    } catch (e) {
      resolve({ code: -1, out: String(e.message || e) });
      return;
    }
    const feed = (d) => {
      const s = d.toString();
      out += s;
      if (onLine) s.split(/\r?\n/).filter((l) => l.trim()).forEach((l) => onLine(l));
    };
    p.stdout.on('data', feed);
    p.stderr.on('data', feed);
    p.on('error', (e) => resolve({ code: -1, out: out + String(e.message || e) }));
    p.on('close', (code) => resolve({ code, out }));
  });
}

class Updater {
  /**
   * root: the git checkout GLASS runs from.
   * opts.python: path to the engine's Python (for pip), opts.run: injectable runner (tests).
   */
  constructor(root, opts = {}) {
    this.root = root;
    this.python = opts.python || null;  // a path, or a function returning one
    this.run = opts.run || run;
    this.running = false;
  }

  git(args, onLine) {
    return this.run('git', args, this.root, onLine);
  }

  isRepo() {
    return fs.existsSync(path.join(this.root, '.git'));
  }

  /** What's new on the remote. Never changes the working copy. */
  async check() {
    if (!this.isRepo()) {
      return { ok: false, reason: 'not_repo', message: 'This copy of GLASS was not installed from a git folder, so it can’t update itself. Re-install it from your GLASS folder once (npm run dist) and updates will work from then on.' };
    }
    const branch = (await this.git(['rev-parse', '--abbrev-ref', 'HEAD'])).out.trim();
    let upstream = (await this.git(['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'])).out.trim();
    if (!upstream || upstream.includes('fatal') || upstream.includes('@{u}')) upstream = `origin/${branch}`;
    const remoteBranch = upstream.replace(/^origin\//, '');
    const fetch = await this.git(['fetch', '--quiet', 'origin', remoteBranch]);
    if (fetch.code !== 0) {
      return { ok: false, reason: 'fetch', branch, message: /could not read|Authentication|403|401/i.test(fetch.out)
        ? 'GitHub refused the download — sign in to GitHub on this PC (open your GLASS folder and run "git pull" once).'
        : `Couldn’t reach GitHub: ${fetch.out.trim().split('\n').pop() || 'no connection'}` };
    }
    const head = (await this.git(['rev-parse', 'HEAD'])).out.trim();
    const behind = parseInt((await this.git(['rev-list', '--count', `HEAD..${upstream}`])).out.trim(), 10) || 0;
    const ahead = parseInt((await this.git(['rev-list', '--count', `${upstream}..HEAD`])).out.trim(), 10) || 0;
    const log = behind ? (await this.git(['log', '--format=%h\x1f%s\x1f%cr', `HEAD..${upstream}`])).out.trim() : '';
    const changes = log ? log.split('\n').map((l) => { const [hash, subject, when] = l.split('\x1f'); return { hash, subject, when }; }) : [];
    const dirty = (await this.git(['status', '--porcelain', '--untracked-files=no'])).out.trim();
    return {
      ok: true, branch, upstream, current: head.slice(0, 7), behind, ahead, changes,
      dirty: dirty ? dirty.split('\n').map((l) => l.slice(3)) : [],
      available: behind > 0, checkedAt: Date.now(),
    };
  }

  /**
   * Bring the checkout up to date and rebuild. progress(step, line) reports as it goes.
   * hooks.beforePip: stop the engine first (Windows locks loaded Python modules).
   * Returns {ok, restart, message, steps}.
   */
  async update(progress = () => {}, hooks = {}) {
    if (this.running) return { ok: false, message: 'An update is already running.' };
    this.running = true;
    const steps = [];
    const step = (name) => { steps.push(name); progress(name, null); };
    const log = (name) => (line) => progress(name, line);
    try {
      const info = await this.check();
      if (!info.ok) return { ok: false, message: info.message };
      if (!info.available) return { ok: true, restart: false, message: 'GLASS is already up to date.' };
      const old = (await this.git(['rev-parse', 'HEAD'])).out.trim();

      if (info.dirty.length) {
        // files changed on this PC (often package-lock.json after npm install): keep a backup, then update
        step('Saving local changes');
        const st = await this.git(['stash', 'push', '-m', `GLASS update backup ${new Date().toISOString()}`], log('Saving local changes'));
        if (st.code !== 0) return { ok: false, message: 'Couldn’t set aside the files changed on this PC. ' + st.out.trim() };
      }

      step('Downloading the update');
      const pull = await this.git(['merge', '--ff-only', info.upstream], log('Downloading the update'));
      if (pull.code !== 0) {
        return { ok: false, message: info.ahead
          ? 'This folder has its own changes that aren’t on GitHub, so it can’t update automatically.'
          : `The update couldn’t be applied: ${pull.out.trim().split('\n').pop()}` };
      }
      const changed = (await this.git(['diff', '--name-only', old, 'HEAD'])).out.split('\n').map((s) => s.trim()).filter(Boolean);
      const has = (re) => changed.some((f) => re.test(f));
      let electronChanged = false;
      if (has(/^package(-lock)?\.json$/)) {
        const was = (await this.git(['show', `${old}:package.json`])).out;
        const now = fs.readFileSync(path.join(this.root, 'package.json'), 'utf8');
        const ver = (s) => { try { return JSON.parse(s).devDependencies?.electron; } catch { return null; } };
        electronChanged = ver(was) !== ver(now);
        step('Installing app packages');
        const npm = await this.run('npm', ['install', '--no-audit', '--no-fund'], this.root, log('Installing app packages'));
        if (npm.code !== 0) return { ok: false, message: 'Installing app packages failed — see the log.', steps };
      }
      const req = process.platform === 'darwin' ? /^engine\/requirements-mac\.txt$/ : /^engine\/requirements\.txt$/;
      const python = typeof this.python === 'function' ? this.python() : this.python;
      if (has(req) && python) {
        step('Installing engine packages');
        if (hooks.beforePip) await hooks.beforePip();
        const file = path.join(this.root, 'engine', process.platform === 'darwin' ? 'requirements-mac.txt' : 'requirements.txt');
        const pip = await this.run(python, ['-m', 'pip', 'install', '--disable-pip-version-check', '-r', file], this.root, log('Installing engine packages'));
        if (pip.code !== 0) return { ok: false, message: 'Installing engine packages failed — see the log.', steps };
      }
      step('Building the app');
      const build = await this.run('npm', ['run', 'build'], this.root, log('Building the app'));
      if (build.code !== 0) return { ok: false, message: 'Building the app failed — see the log.', steps };
      return {
        ok: true, restart: true, steps, changed: changed.length,
        message: electronChanged
          ? 'Updated. This update also changes Electron itself: after restarting, run “npm run dist” in your GLASS folder and install it once.'
          : `Updated to the latest version (${info.behind} change${info.behind === 1 ? '' : 's'}). Restarting GLASS…`,
      };
    } finally {
      this.running = false;
    }
  }
}

module.exports = { Updater, run };
