'use strict';
// CloudCLI plugin server: reuse or spawn the harness tracker (python), then
// expose a loopback reverse proxy and report its port on stdout.
// CloudCLI forwards /api/plugins/harness-tracker/rpc/* to this proxy.
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const TRACKER_HOST = '127.0.0.1';
const TRACKER_PORT = parseInt(process.env.HARNESS_TRACKER_PORT || '8787', 10);
// realpath: this file is symlinked into the CloudCLI plugin dir; resolve back to the repo.
const SERVER_PY = path.resolve(path.dirname(fs.realpathSync(__filename)), '..', 'server.py');

function request(opts, body) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: TRACKER_HOST, port: TRACKER_PORT, ...opts }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({ res, body: Buffer.concat(chunks) }));
    });
    req.setTimeout(opts.timeout || 30000, () => req.destroy(new Error('timeout')));
    req.on('error', reject);
    if (body && body.length) req.write(body);
    req.end();
  });
}

async function trackerUp() {
  try {
    const { res, body } = await request({ path: '/api/state', method: 'GET', timeout: 2000 });
    if (res.statusCode !== 200) return false;
    JSON.parse(body.toString('utf8'));
    return true;
  } catch (_) {
    return false;
  }
}

let ensuring = null;
function ensureTracker() {
  if (ensuring) return ensuring;
  ensuring = (async () => {
    if (await trackerUp()) return true;
    if (!fs.existsSync(SERVER_PY)) return false;
    const child = spawn('python3', [SERVER_PY], {
      detached: true,
      stdio: 'ignore',
      env: { ...process.env, HARNESS_TRACKER_PORT: String(TRACKER_PORT) },
    });
    child.on('error', () => {});
    child.unref();
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 250));
      if (await trackerUp()) return true;
    }
    return false;
  })().finally(() => { ensuring = null; });
  return ensuring;
}

function fail(res, status, msg) {
  if (res.headersSent) return res.end();
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify({ error: msg, tracker: `http://${TRACKER_HOST}:${TRACKER_PORT}`, server_py: SERVER_PY }));
}

async function forward(req, res, body) {
  const headers = { ...req.headers, host: `${TRACKER_HOST}:${TRACKER_PORT}` };
  delete headers['content-length'];
  if (body.length) headers['content-length'] = body.length;
  const { res: up, body: out } = await request({ path: req.url, method: req.method, headers }, body);
  res.writeHead(up.statusCode, up.headers);
  res.end(out);
}

const server = http.createServer((req, res) => {
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', async () => {
    const body = Buffer.concat(chunks);
    try {
      await forward(req, res, body);
    } catch (_) {
      // tracker down: try to bring it up once, then retry
      if (await ensureTracker()) {
        try { return await forward(req, res, body); } catch (e) { return fail(res, 502, 'tracker request failed: ' + e.message); }
      }
      fail(res, 502, 'harness tracker is not reachable and could not be started');
    }
  });
});

(async () => {
  await ensureTracker(); // best effort; proxy still starts and reports 502 until tracker is up
  server.listen(0, '127.0.0.1', () => {
    process.stdout.write(JSON.stringify({ ready: true, port: server.address().port }) + '\n');
  });
})();

for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => server.close(() => process.exit(0)));
