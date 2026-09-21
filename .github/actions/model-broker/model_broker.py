"""Model broker: holds the Anthropic API key for an agent that must not read it.

The broker is a loopback HTTP server that runs as its own Unix user
(``model-broker``, created by the composite action next to this file). The
agent's Claude Code process is pointed at it through ``ANTHROPIC_BASE_URL``
and presents a per-run token instead of the key; the broker swaps the token
for the key it read at start-up and forwards the request to the one upstream
it knows, ``https://api.anthropic.com``. The key therefore sits in the
memory of a process the agent's user cannot read (``/proc/<pid>/environ`` and
``/proc/<pid>/mem`` are owned by ``model-broker``; sudo and containers are
disabled by harden-runner before the agent starts) and in no file the agent
can open. What the agent can disclose, its ``ANTHROPIC_API_KEY``, is the
per-run token: usable only against 127.0.0.1 on this runner, and only while
the broker is alive.

What the broker will not do, so that it cannot be turned into a relay or a
credential forwarder by a prompt-injected agent:

- it connects to ``api.anthropic.com:443`` and nowhere else: the request
  line's host, the ``Host`` header and any absolute-form URI are ignored or
  refused, and ``CONNECT`` is refused;
- it forwards only the Messages API operations Claude Code needs
  (``ALLOWED_ROUTES``) and answers its reachability probe itself
  (``LOCAL_PROBES``); anything else is answered 403 without an upstream
  call;
- it drops every credential header the client sent (``x-api-key``,
  ``authorization``, ``proxy-authorization``, ``cookie``) and sets the key
  itself, so the key is never echoed and a client credential is never
  forwarded;
- it exits when the stop file appears or its lifetime runs out, and it logs
  one line per request (method, path, status; never a header or a body).

Standard library only, so it runs under the runner's ``/usr/bin/python3``
with nothing installed. ``main`` builds the upstream from the constants; the
tests construct a ``Broker`` with a plain-HTTP upstream on loopback instead.
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import http.server
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from pathlib import Path

UPSTREAM_HOST = "api.anthropic.com"
UPSTREAM_PORT = 443

# (method, path) pairs the broker forwards. The query string (Claude Code
# sends `?beta=true`) is forwarded as-is on an allowed path.
ALLOWED_ROUTES = frozenset(
    {
        ("POST", "/v1/messages"),
        ("POST", "/v1/messages/count_tokens"),
    }
)

# Answered locally with 200: the workflow's health check and Claude Code's
# reachability probe. No upstream call, no token needed, nothing forwarded.
LOCAL_PROBES = frozenset({"/healthz", "/api/hello"})

MAX_BODY_BYTES = 64 * 1024 * 1024
UPSTREAM_TIMEOUT_SECONDS = 600
CHUNK = 64 * 1024

HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
     "te", "trailer", "transfer-encoding", "upgrade"}
)
# Request headers that never reach upstream: hop-by-hop, the host (fixed),
# the length (recomputed from the body actually read) and every credential.
DROPPED_REQUEST_HEADERS = HOP_BY_HOP | frozenset({"host", "content-length", "x-api-key", "authorization", "cookie"})
DROPPED_RESPONSE_HEADERS = HOP_BY_HOP | frozenset({"content-length"})


class Upstream:
    """The one destination the broker talks to."""

    def __init__(self, host: str, port: int, *, tls: bool = True, timeout: float = UPSTREAM_TIMEOUT_SECONDS):
        self.host = host
        self.port = port
        self.tls = tls
        self.timeout = timeout

    def connect(self) -> http.client.HTTPConnection:
        if self.tls:
            return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=ssl.create_default_context())
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)


class Broker(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], *, api_key: str, token: str, upstream: Upstream, log=None):
        self.api_key = api_key
        self.token = token
        self.upstream = upstream
        self.log = log or (lambda line: None)
        self.requests_served = 0
        super().__init__(address, Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "model-broker"
    sys_version = ""
    server: Broker

    # One entry point for every method, so nothing is forwarded by default.
    def do_GET(self):
        self.relay()

    def do_POST(self):
        self.relay()

    def do_PUT(self):
        self.relay()

    def do_PATCH(self):
        self.relay()

    def do_DELETE(self):
        self.relay()

    def do_HEAD(self):
        self.relay()

    def do_OPTIONS(self):
        self.relay()

    def do_CONNECT(self):
        self.refuse(405, "CONNECT is not supported")

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler's signature
        # BaseHTTPRequestHandler logs the request line to stderr; the broker's
        # own log line (method, path, status) replaces it.
        return

    def version_string(self):
        return self.server_version

    def refuse(self, status: int, reason: str) -> None:
        self.close_connection = True
        body = (reason + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.server.log(f"{self.command} {self.path} -> {status} ({reason})")

    def presented_token(self) -> str:
        key = self.headers.get("x-api-key")
        if key:
            return key.strip()
        auth = self.headers.get("authorization", "")
        scheme, _, credential = auth.strip().partition(" ")
        if scheme.lower() == "bearer":
            return credential.strip()
        return ""

    def relay(self) -> None:
        self.close_connection = True
        # Absolute-form request targets (`GET https://host/...`) name a host;
        # the broker has exactly one and refuses to be asked for another.
        if "://" in self.path.split("?", 1)[0] or self.path.startswith("//"):
            self.refuse(400, "absolute-form request target refused")
            return
        parsed = urllib.parse.urlsplit(self.path)
        if self.command in ("GET", "HEAD") and parsed.path in LOCAL_PROBES:
            # Reachability probes answered here, without upstream or a token:
            # /healthz for the workflow's own check, /api/hello for the probe
            # Claude Code sends before its first request.
            self.refuse(200, "ok")
            return
        if (self.command, parsed.path) not in ALLOWED_ROUTES:
            self.refuse(403, "operation not allowed by the model broker")
            return
        if not hmac.compare_digest(self.presented_token().encode(), self.server.token.encode()):
            self.refuse(401, "unknown broker token")
            return
        if self.headers.get("transfer-encoding"):
            self.refuse(411, "chunked request bodies are not supported; send Content-Length")
            return
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            self.refuse(400, "bad Content-Length")
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self.refuse(413, "request body too large")
            return
        body = self.read_exactly(length)
        if body is None:
            self.refuse(400, "request body shorter than Content-Length")
            return
        self.forward(body)

    def read_exactly(self, length: int) -> bytes | None:
        parts = []
        remaining = length
        while remaining:
            chunk = self.rfile.read(min(remaining, CHUNK))
            if not chunk:
                return None
            parts.append(chunk)
            remaining -= len(chunk)
        return b"".join(parts)

    def forward(self, body: bytes) -> None:
        upstream = self.server.upstream
        conn = upstream.connect()
        try:
            try:
                # The path is forwarded verbatim (an allowed route plus its
                # query); the host is the connection's, never the client's.
                conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                conn.putheader("Host", upstream.host)
                for name, value in self.headers.items():
                    if name.lower() not in DROPPED_REQUEST_HEADERS:
                        conn.putheader(name, value)
                conn.putheader("x-api-key", self.server.api_key)
                conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body)
                response = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                self.refuse(502, f"upstream request failed: {type(exc).__name__}")
                return
            self.send_response(response.status, response.reason)
            for name, value in response.getheaders():
                if name.lower() not in DROPPED_RESPONSE_HEADERS:
                    self.send_header(name, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            # read1 returns as soon as bytes arrive, so a streamed (SSE)
            # response reaches the client token by token rather than after
            # the whole body.
            while True:
                chunk = response.read1(CHUNK)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            self.server.requests_served += 1
            self.server.log(f"{self.command} {self.path} -> {response.status}")
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.server.log(f"{self.command} {self.path} -> client went away")
        finally:
            conn.close()


def read_secret_file(path: Path) -> str:
    value = path.read_text().strip()
    if not value:
        raise SystemExit(f"model-broker: {path} is empty")
    return value


def stderr(line: str) -> None:
    sys.stderr.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + line + "\n")
    sys.stderr.flush()


def watch(server: Broker, *, stop_file: Path | None, deadline: float, poll: float = 1.0) -> None:
    """Shut the server down when the stop file appears or the lifetime ends."""
    while True:
        if stop_file is not None and stop_file.exists():
            server.log("stop file present; exiting")
            break
        if time.monotonic() >= deadline:
            server.log("lifetime reached; exiting")
            break
        time.sleep(poll)
    server.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, required=True, help="loopback port to listen on")
    parser.add_argument("--key-file", type=Path, required=True, help="file holding the Anthropic API key; deleted once read")
    parser.add_argument("--token-file", type=Path, required=True, help="file holding the per-run token clients must present")
    parser.add_argument("--ready-file", type=Path, required=True, help="written (pid and port) once the broker is listening")
    parser.add_argument("--stop-file", type=Path, required=True, help="the broker exits when this file exists")
    parser.add_argument("--lifetime-minutes", type=int, required=True, help="the broker exits after this long regardless")
    args = parser.parse_args(argv)

    api_key = read_secret_file(args.key_file)
    args.key_file.unlink()
    token = read_secret_file(args.token_file)
    upstream = Upstream(UPSTREAM_HOST, UPSTREAM_PORT, tls=True)
    server = Broker(("127.0.0.1", args.port), api_key=api_key, token=token, upstream=upstream, log=stderr)
    deadline = time.monotonic() + args.lifetime_minutes * 60
    threading.Thread(target=watch, args=(server,), kwargs={"stop_file": args.stop_file, "deadline": deadline}, daemon=True).start()
    args.ready_file.write_text(f"pid={os.getpid()}\nport={server.server_address[1]}\n")
    stderr(f"listening on 127.0.0.1:{server.server_address[1]} for {upstream.host}:{upstream.port}; lifetime {args.lifetime_minutes} min")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        stderr(f"exited after {server.requests_served} forwarded request(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
