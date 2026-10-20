'use strict';
// Real Node listener used by the end-to-end suite. Double-forks so the process
// is reparented to init and is not grouped under the test runner.
const http = require('http');
const fs = require('fs');
const {spawn} = require('child_process');

const port = Number(process.env.PORT);
if (!port) {
    console.error('PORT is required');
    process.exit(1);
}

if (!process.env.WTP_DAEMONIZED) {
    const child = spawn(process.execPath, [__filename], {
        detached: true,
        stdio: 'ignore',
        env: {...process.env, WTP_DAEMONIZED: '1'},
        cwd: process.cwd(),
    });
    child.unref();
    process.exit(0);
}

if (process.env.PIDFILE) {
    fs.writeFileSync(process.env.PIDFILE, String(process.pid));
}

http.createServer((_req, res) => {
    res.writeHead(200, {'content-type': 'text/plain'});
    res.end('ok');
}).listen(port, '127.0.0.1');
