// Launch branded Google Chrome 137+ with an unpacked extension.
// Chrome ignores --load-extension there; the supported route is CDP `Extensions.loadUnpacked` over --remote-debugging-pipe.
// usage: node chrome_launcher.js <chrome.exe> <user-data-dir> <extension-dir> <cdp-port> <start-url> [--headless]
// Prints one JSON line {"ok":true,"extension_id":"..."} (or {"ok":false,"error":...}) then stays alive until killed; killing it kills Chrome.
const { spawn, spawnSync } = require('node:child_process');

const [chrome, profile, extDir, port, startUrl, ...flags] = process.argv.slice(2);
const args = [
  `--user-data-dir=${profile}`, '--remote-debugging-pipe', '--enable-unsafe-extension-debugging', `--remote-debugging-port=${port}`,
  '--no-first-run', '--no-default-browser-check', '--disable-sync', '--disable-background-networking', '--window-size=1100,850',
  '--force-device-scale-factor=1', ...(flags.includes('--headless') ? ['--headless=new'] : []),
  ...flags.filter((f) => f !== '--headless' && f.startsWith('--')), // any other flag is passed straight to Chrome (e.g. a fake microphone for voice tests)
  startUrl,
];
const child = spawn(chrome, args, { stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'] });
const toChrome = child.stdio[3];
const fromChrome = child.stdio[4];

let buf = '';
let done = false;
const finish = (obj) => { if (!done) { done = true; console.log(JSON.stringify(obj)); } };

fromChrome.on('data', (d) => {
  buf += d.toString('utf8');
  let i;
  while ((i = buf.indexOf('\0')) >= 0) {
    const msg = buf.slice(0, i); buf = buf.slice(i + 1);
    try {
      const m = JSON.parse(msg);
      if (m.id === 1) finish(m.error ? { ok: false, error: m.error } : { ok: true, extension_id: m.result.id });
    } catch (_) { /* ignore non-JSON noise */ }
  }
});
toChrome.write(JSON.stringify({ id: 1, method: 'Extensions.loadUnpacked', params: { path: extDir } }) + '\0');

const killTree = () => { try { spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F']); } catch (_) {} process.exit(0); };
process.on('SIGTERM', killTree);
process.on('SIGINT', killTree);
child.on('exit', (code) => { finish({ ok: false, error: `Chrome exited (${code}) before the extension loaded` }); process.exit(0); });
setTimeout(() => finish({ ok: false, error: 'Timed out waiting for Extensions.loadUnpacked' }), 30000);
