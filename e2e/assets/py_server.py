#!/usr/bin/env python3
"""Real Python listener. Double-forks so the scanner does not climb into the test runner.

GET /grow allocates and touches ~520 MiB so a later scan can see memory growth.
"""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port = int(os.environ["PORT"])

if not os.environ.get("WTP_DAEMONIZED"):
    pid = os.fork()
    if pid > 0:
        os._exit(0)
    os.setsid()
    pid = os.fork()
    if pid > 0:
        os._exit(0)
    os.chdir(os.getcwd())
    os.environ["WTP_DAEMONIZED"] = "1"

pidfile = os.environ.get("PIDFILE")
if pidfile:
    with open(pidfile, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

blob = bytearray()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        global blob
        if self.path.startswith("/grow"):
            size = 520 * 1024 * 1024
            blob = bytearray(size)
            for i in range(0, size, 4096):
                blob[i] = 1
            body = b"grown"
        else:
            body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


ThreadingHTTPServer.allow_reuse_address = True
ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
