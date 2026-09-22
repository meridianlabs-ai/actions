"""Tests for .github/actions/model-broker: the broker that holds the model key.

Two layers. The proxy itself (model_broker.py) is tested in-process against a
plain-HTTP upstream on loopback: the key replaces whatever credential the
client presented, the client's Host and credentials never reach upstream,
only the Messages API routes are forwarded, absolute-form targets and CONNECT
are refused, streamed bodies arrive intact, and the stop file and lifetime
end the server. `main` is checked to build the fixed TLS upstream and to
delete the key file once read.

The isolation is tested end to end under Docker, when a daemon is available
(it is on ubuntu-latest, where tests.yml runs): the action's start script is
lifted from action.yml and run verbatim as an unprivileged sudo-capable user
in an Ubuntu container whose api.anthropic.com resolves to a fake TLS
upstream signed by a test CA; sudo is then removed the way harden-runner's
disable-sudo-and-containers does, check_isolation.sh is run as that user, a
request through the broker reaches the fake upstream with the real key, and
a sweep of every file and /proc entry that user can read finds no trace of
the key. Set MODEL_BROKER_SKIP_DOCKER=1 to skip that layer.

Run with `python3 -m pytest` from the repo root (needs pytest and PyYAML).
"""

from __future__ import annotations

import http.client
import http.server
import importlib.util
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION_DIR = ROOT / ".github" / "actions" / "model-broker"
REAL_KEY = "sk-ant-api03-REAL-KEY-SENTINEL-0123456789abcdef"
TOKEN = "broker-run-0123456789abcdef0123456789abcdef0123456789abcdef"


def load_module():
    spec = importlib.util.spec_from_file_location("model_broker", ACTION_DIR / "model_broker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mb = load_module()


# --- A fake upstream and a broker in front of it -------------------------------


class FakeUpstream(http.server.ThreadingHTTPServer):
    """Records every request; answers /v1/messages with a three-event SSE stream."""

    def __init__(self):
        self.seen: list[dict] = []
        super().__init__(("127.0.0.1", 0), FakeUpstreamHandler)


class FakeUpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: FakeUpstream

    def log_message(self, *args):
        return

    def handle_any(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        self.server.seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers.items()), "body": body})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("request-id", "req_test")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i in range(3):
            chunk = f"event: e{i}\ndata: {{\"n\": {i}}}\n\n".encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
            time.sleep(0.02)
        self.wfile.write(b"0\r\n\r\n")

    do_POST = do_GET = do_PUT = do_DELETE = handle_any


@pytest.fixture
def upstream():
    server = FakeUpstream()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def broker(upstream):
    lines: list[str] = []
    server = mb.Broker(("127.0.0.1", 0), api_key=REAL_KEY, token=TOKEN,
                       upstream=mb.Upstream("127.0.0.1", upstream.server_address[1], tls=False), log=lines.append)
    server.lines = lines
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def call(broker, method: str, path: str, headers: dict | None = None, body: bytes = b"") -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", broker.server_address[1], timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    result = (response.status, {k.lower(): v for k, v in response.getheaders()}, data)
    conn.close()
    return result


# --- The proxy -----------------------------------------------------------------


def test_forwards_a_messages_call_with_the_key_and_without_the_clients_credentials(broker, upstream):
    body = json.dumps({"model": "claude", "messages": []}).encode()
    status, headers, data = call(broker, "POST", "/v1/messages?beta=true", {
        "x-api-key": TOKEN, "authorization": "Bearer something-the-agent-typed", "cookie": "a=b",
        "Host": "attacker.example", "anthropic-version": "2023-06-01", "anthropic-beta": "x", "content-type": "application/json",
    }, body)
    assert status == 200
    assert headers["content-type"] == "text/event-stream" and headers["request-id"] == "req_test"
    assert headers["connection"] == "close"
    assert data == b'event: e0\ndata: {"n": 0}\n\nevent: e1\ndata: {"n": 1}\n\nevent: e2\ndata: {"n": 2}\n\n'
    [seen] = upstream.seen
    got = {k.lower(): v for k, v in seen["headers"].items()}
    assert seen["method"] == "POST" and seen["path"] == "/v1/messages?beta=true" and seen["body"] == body
    assert got["x-api-key"] == REAL_KEY
    assert got["host"] == "127.0.0.1"  # the upstream's host, never the client's
    assert got["anthropic-version"] == "2023-06-01" and got["anthropic-beta"] == "x"
    assert got["content-length"] == str(len(body))
    for name in ("authorization", "cookie", "transfer-encoding", "connection"):
        assert name not in got
    assert TOKEN not in json.dumps(seen["headers"])
    assert broker.lines[-1] == "POST /v1/messages?beta=true -> 200"


def test_accepts_the_token_as_a_bearer_credential(broker, upstream):
    status, _, _ = call(broker, "POST", "/v1/messages/count_tokens", {"authorization": f"Bearer {TOKEN}"}, b"{}")
    assert status == 200
    assert {k.lower(): v for k, v in upstream.seen[0]["headers"].items()}["x-api-key"] == REAL_KEY


@pytest.mark.parametrize("headers", [{}, {"x-api-key": "wrong"}, {"x-api-key": REAL_KEY}, {"authorization": "Bearer wrong"},
                                     {"authorization": f"Basic {TOKEN}"}])
def test_an_unknown_token_is_refused_before_any_upstream_call(broker, upstream, headers):
    status, _, data = call(broker, "POST", "/v1/messages", headers, b"{}")
    assert status == 401 and b"unknown broker token" in data
    assert upstream.seen == []


@pytest.mark.parametrize("method, path", [
    ("GET", "/v1/models"), ("GET", "/v1/messages"), ("POST", "/v1/complete"), ("POST", "/v1/messages/batches"),
    ("DELETE", "/v1/files/abc"), ("POST", "/v1/messagesx"), ("POST", "/v1/messages/"), ("POST", "/V1/MESSAGES"),
    ("POST", "/v1/messages/../complete"), ("PUT", "/v1/messages"), ("OPTIONS", "/v1/messages"), ("POST", "/healthz"),
])
def test_anything_but_the_messages_api_is_refused_before_any_upstream_call(broker, upstream, method, path):
    status, _, data = call(broker, method, path, {"x-api-key": TOKEN}, b"{}")
    assert status == 403 and b"not allowed" in data
    assert upstream.seen == []


@pytest.mark.parametrize("target", ["http://attacker.example/v1/messages", "https://api.anthropic.com/v1/messages", "//attacker.example/v1/messages"])
def test_an_absolute_form_target_is_refused(broker, upstream, target):
    # `//host/...` is refused as absolute-form, or, on Pythons whose request
    # parser already collapses the leading slashes, as a path outside the
    # Messages API; either way nothing reaches upstream.
    status, _, _ = call(broker, "POST", target, {"x-api-key": TOKEN}, b"{}")
    assert status in (400, 403)
    assert upstream.seen == []


def test_connect_is_refused(broker, upstream):
    status, _, _ = call(broker, "CONNECT", "attacker.example:443", {"x-api-key": TOKEN})
    assert status == 405
    assert upstream.seen == []


def test_reachability_probes_are_answered_locally(broker, upstream):
    assert call(broker, "GET", "/healthz")[0] == 200
    assert call(broker, "HEAD", "/api/hello")[0] == 200
    assert call(broker, "GET", "/api/hello")[0] == 200
    assert upstream.seen == []


def test_chunked_and_oversized_request_bodies_are_refused(broker, upstream):
    status, _, _ = call(broker, "POST", "/v1/messages", {"x-api-key": TOKEN, "Transfer-Encoding": "chunked"}, b"0\r\n\r\n")
    assert status == 411
    conn = http.client.HTTPConnection("127.0.0.1", broker.server_address[1], timeout=10)
    conn.putrequest("POST", "/v1/messages")
    conn.putheader("x-api-key", TOKEN)
    conn.putheader("Content-Length", str(mb.MAX_BODY_BYTES + 1))
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()
    assert upstream.seen == []


def test_an_unreachable_upstream_is_a_502_not_a_hang():
    server = mb.Broker(("127.0.0.1", 0), api_key=REAL_KEY, token=TOKEN, upstream=mb.Upstream("127.0.0.1", 9, tls=False, timeout=2))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, _, data = call(server, "POST", "/v1/messages", {"x-api-key": TOKEN}, b"{}")
        assert status == 502 and b"upstream request failed" in data
    finally:
        server.shutdown()
        server.server_close()


def test_the_log_never_carries_a_header_or_a_body(broker, upstream):
    call(broker, "POST", "/v1/messages", {"x-api-key": TOKEN, "anthropic-beta": "secret-beta"}, b'{"prompt": "private"}')
    call(broker, "POST", "/v1/messages", {"x-api-key": "wrong"}, b"{}")
    text = "\n".join(broker.lines)
    for needle in (REAL_KEY, TOKEN, "wrong", "secret-beta", "private"):
        assert needle not in text


def test_the_stop_file_and_the_lifetime_end_the_server(tmp_path, upstream):
    def running_broker():
        server = mb.Broker(("127.0.0.1", 0), api_key=REAL_KEY, token=TOKEN, upstream=mb.Upstream("127.0.0.1", upstream.server_address[1], tls=False))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    server, thread = running_broker()
    stop = tmp_path / "stop"
    watcher = threading.Thread(target=mb.watch, args=(server,), kwargs={"stop_file": stop, "deadline": time.monotonic() + 60, "poll": 0.05}, daemon=True)
    watcher.start()
    time.sleep(0.2)
    assert thread.is_alive()
    stop.touch()
    thread.join(timeout=5)
    assert not thread.is_alive()

    server, thread = running_broker()
    threading.Thread(target=mb.watch, args=(server,), kwargs={"stop_file": None, "deadline": time.monotonic() + 0.2, "poll": 0.05}, daemon=True).start()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_main_pins_the_upstream_deletes_the_key_file_and_reports_its_pid(tmp_path):
    assert (mb.UPSTREAM_HOST, mb.UPSTREAM_PORT) == ("api.anthropic.com", 443)
    key_file, token_file, ready, stop = (tmp_path / n for n in ("key", "token", "ready", "stop"))
    key_file.write_text(REAL_KEY + "\n")
    token_file.write_text(TOKEN)
    argv = ["--port", "0", "--key-file", str(key_file), "--token-file", str(token_file), "--ready-file", str(ready),
            "--stop-file", str(stop), "--lifetime-minutes", "5"]
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(mb.main(argv)), daemon=True)
    thread.start()
    for _ in range(100):
        if ready.exists():
            break
        time.sleep(0.05)
    fields = dict(line.split("=", 1) for line in ready.read_text().splitlines())
    assert fields["pid"] == str(os.getpid()) and fields["port"].isdigit() and int(fields["port"]) > 0
    assert not key_file.exists(), "the key file must be deleted once read"
    assert token_file.exists()
    # The listener is up and answers the local probe without touching the upstream.
    conn = http.client.HTTPConnection("127.0.0.1", int(fields["port"]), timeout=10)
    conn.request("GET", "/healthz")
    assert conn.getresponse().status == 200
    conn.close()
    stop.touch()
    thread.join(timeout=10)
    assert result == [0]


def test_main_offers_no_way_to_choose_the_upstream():
    source = (ACTION_DIR / "model_broker.py").read_text()
    for flag in re.findall(r'add_argument\("(--[a-z-]+)"', source):
        assert "upstream" not in flag and "host" not in flag and "url" not in flag, flag
    assert "os.environ" not in source, "the broker reads no environment variable"


# --- action.yml: how the workflow runs it -----------------------------------------


def start_step() -> dict:
    [step] = yaml.safe_load((ACTION_DIR / "action.yml").read_text())["runs"]["steps"]
    return step


def test_action_passes_inputs_through_env_and_writes_outputs_with_heredocs():
    step = start_step()
    assert "${{" not in step["run"], "no expression is expanded inside the script"
    assert step["env"]["MODEL_BROKER_API_KEY"] == "${{ inputs.api-key }}"
    assert step["shell"] == "bash"
    for output in ("base-url", "token", "stop-file"):
        assert f'echo "{output}<<EOF"' in step["run"]
    # The broker's argv is exactly the flags model_broker.py takes; nothing
    # selects an upstream, and the key travels by file, not argument.
    assert re.search(r"--key-file \"\$home/key\"", step["run"])
    assert "--upstream" not in step["run"]
    assert "MODEL_BROKER_API_KEY" not in step["run"].split("printf '%s' \"$MODEL_BROKER_API_KEY\" | sudo")[1]


# --- The isolation, end to end under Docker --------------------------------------
#
# The whole option-2 lifecycle in an Ubuntu container: a sudo-capable `runner`
# user (as on a hosted runner, KEEPING sudo — harden-runner's pre hook, not a
# step, is what would drop it, and B1 is precisely that we must not), the model
# broker as its own user, a dedicated unprivileged `agent` user that runs the
# whole Claude process, and a sentinel that stands in for the .NET
# Runner.Worker: a runner-owned process holding a secret in memory with a
# 0600 dotnet-diagnostic-<pid>-socket, the same-UID memory-dump channel B2
# named. The three isolated-agent step scripts are lifted from action.yml and
# run verbatim as the runner user (each drops to the agent with sudo, exactly
# as the composite does). api.anthropic.com resolves to a fake TLS upstream
# under a test CA. Docker Desktop's kernel has no Yama, so there the isolation
# check's ONLY allowed failure is the ptrace_scope one; every UID-boundary
# probe still passes (DAC blocks a cross-UID mem read or 0600-socket connect
# without Yama), so the container still proves the boundary that closes B2.
# Set MODEL_BROKER_SKIP_DOCKER=1 to skip this layer.
#
# The filesystem layout is the hosted runner's, not /tmp: the workspace,
# RUNNER_TEMP and the runner's install directory all sit under the runner's
# PRIVATE home (/home/runner, 0750 — Ubuntu's useradd default, and what the
# 2026-09-22 ci-perf and triage runs hit: "fatal: failed to stat
# '/home/runner/work/actions/actions': Permission denied" from the first git
# run as the agent), with this repo's actions checked out inside the
# workspace as `actions-repo`, where both callers put them. The stand-in
# Runner.Worker is started from that install directory next to 0600
# .credentials files, so the check step's runner-root lookup and its
# credential probe run against the real thing.

IMAGE = "ubuntu:24.04"
ACTIONS_MOUNT = ROOT / ".github" / "actions"
AGENT_USER = "claude-agent"
RUNNER_HOME = "/home/runner"
WORKSPACE = f"{RUNNER_HOME}/work/actions/actions"
RUNNER_TEMP = f"{RUNNER_HOME}/work/_temp"
WRITE_DIR = f"{RUNNER_TEMP}/agent-out"
ACTION_PATH = f"{WORKSPACE}/actions-repo/.github/actions/isolated-agent"
RUNNER_ROOT = f"{RUNNER_HOME}/runners/2.0.0"
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SENTINEL_SECRET = "SENTINEL-RUNNER-WORKER-SECRET-" + "z" * 40
EXPOSED_SECRET = "EXPOSED-IN-ARGV-POSITIVE-CONTROL-" + "q" * 40

FAKE_TLS_UPSTREAM = r"""
import http.server, json, ssl, time
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0); body = self.rfile.read(n)
        with open("/root/upstream/seen.jsonl", "a") as f:
            f.write(json.dumps({"path": self.path, "headers": dict(self.headers.items()), "body": body.decode("utf-8", "replace")}) + "\n")
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
        for i in range(3):
            c = f"data: {i}\n\n".encode(); self.wfile.write(b"%x\r\n%s\r\n" % (len(c), c)); self.wfile.flush(); time.sleep(0.02)
        self.wfile.write(b"0\r\n\r\n")
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain("/root/ca/srv.crt", "/root/ca/srv.key")
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 443), H); srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
open("/root/upstream/ready", "w").write("1")
srv.serve_forever()
"""

# Stand-in for the .NET Runner.Worker: runs as `runner`, holds a secret only
# in memory, and opens a 0600 diagnostic socket named like the runtime's.
SENTINEL = r'''
import os, socket, time
# The secret arrives through the environment (a 0400 /proc/environ channel a
# different UID cannot read), is kept only in this Python variable, and is
# cleared from the process environment immediately -- never in argv/cmdline,
# which is world-readable.
secret = os.environ.pop("SENTINEL_SECRET")  # noqa: F841 -- kept in memory only
sock = f"{os.environ.get('TMPDIR', '/tmp')}/dotnet-diagnostic-{os.getpid()}-1-socket"
try: os.unlink(sock)
except FileNotFoundError: pass
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(sock); os.chmod(sock, 0o600); s.listen(1)
open("/tmp/sentinel.pid", "w").write(str(os.getpid()))
while True: time.sleep(1)
'''

# A stand-in `claude`: records who it ran as, its whole environment, cwd, argv
# and a listing of its working directory into the write-dir the prompt named
# (forwarded as AGENT_RECORD_DIR), so the test can prove the run happened as
# the agent under env -i, in a workspace the agent can read.
FAKE_CLAUDE = r'''#!/usr/bin/env bash
d="${AGENT_RECORD_DIR:?the agent got no record dir}"
id -un > "$d/whoami"
/usr/bin/env > "$d/env.txt"
pwd > "$d/cwd"
printf '%s\n' "$@" > "$d/argv"
ls -A . > "$d/workdir-listing" 2>&1
echo "analysis report" > "$d/report.md"
exit 0
'''

CLIENT_CALL = r"""
import http.client, sys
port, token = sys.argv[1], sys.argv[2]
c = http.client.HTTPConnection("127.0.0.1", int(port), timeout=30)
c.request("POST", "/v1/messages?beta=true", body=b'{"hello": "broker"}', headers={"x-api-key": token, "content-type": "application/json", "anthropic-version": "2023-06-01"})
r = c.getresponse(); print(r.status); print(r.read().decode())
"""


CONNECT_PROBE = r'''
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(5)
try:
    s.connect(sys.argv[1]); print("connected"); sys.exit(0)
except OSError as e:
    print("refused:", e.__class__.__name__); sys.exit(1)
finally:
    s.close()
'''


def ia_step(name_substr: str) -> dict:
    for s in yaml.safe_load((ACTIONS_MOUNT / "isolated-agent" / "action.yml").read_text())["runs"]["steps"]:
        if name_substr.lower() in s["name"].lower():
            return s
    raise KeyError(name_substr)


def docker_available() -> bool:
    if os.environ.get("MODEL_BROKER_SKIP_DOCKER") or not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


class Container:
    def __init__(self, name: str):
        self.name = name

    def run(self, *args: str, user: str | None = None, env: dict | None = None, stdin: str | None = None,
            cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["docker", "exec", "-i"]
        if user:
            cmd += ["-u", user]
        if cwd:
            cmd += ["-w", cwd]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        r = subprocess.run(cmd + [self.name, *args], input=stdin, text=True, capture_output=True, check=False)
        if check and r.returncode != 0:
            raise AssertionError(f"{args} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return r

    def write(self, path: str, text: str, mode: str = "0644") -> None:
        self.run("sh", "-c", f"cat > {path} && chmod {mode} {path}", stdin=text)

    def bash_step(self, step: dict, env: dict, *, user: str = "runner", cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        """Lift a composite step's `run:` body and execute it verbatim (from the
        workspace, as the runner runs a step, when `cwd` says so)."""
        self.write("/tmp/step.sh", step["run"], "0755")
        return self.run("bash", "--noprofile", "--norc", "-eo", "pipefail", "/tmp/step.sh", user=user, env=env, cwd=cwd, check=check)


@pytest.fixture(scope="session")
def container():
    if not docker_available():
        pytest.skip("no Docker daemon (set MODEL_BROKER_SKIP_DOCKER=1 to silence)")
    name = f"isolated-agent-test-{uuid.uuid4().hex[:8]}"
    # --init reaps exited children (the broker) as systemd does on a runner.
    subprocess.run(["docker", "run", "-d", "--rm", "--init", "--name", name, "--add-host", "api.anthropic.com:127.0.0.1",
                    "-v", f"{ACTIONS_MOUNT}:/actions:ro", IMAGE, "sleep", "1200"], check=True, capture_output=True)
    c = Container(name)
    try:
        c.run("sh", "-c", "apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "
              "python3 sudo ca-certificates openssl procps util-linux git acl >/dev/null")
        # The runner user: unprivileged but with passwordless sudo, KEPT (the
        # new design does not drop the runner's sudo; the agent user is the
        # one that has none). Its home is private, as useradd makes it on
        # Ubuntu (HOME_MODE 0750) and as the hosted failure implies; the
        # chmod pins that so the test does not depend on the image's
        # login.defs.
        c.run("sh", "-c", "useradd -m -u 1001 runner && chmod 0750 /home/runner"
              " && echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner && chmod 440 /etc/sudoers.d/runner")
        # The hosted layout under that private home, all runner-owned with
        # umask 022 (world-readable, like a checkout): the workspace holding
        # this repo's actions as `actions-repo`, RUNNER_TEMP, the runner's
        # install directory with a Runner.Worker (a python3 copy that runs the
        # sentinel below), its registration files as a hosted runner keeps
        # them (.credentials 0644: OAuth client id and token URL;
        # .credentials_rsaparams 0600: the private key), and a private 0600
        # file and 0700 directory the traversal grants must not open.
        c.run("sh", "-c",
              f"umask 022 && mkdir -p {WORKSPACE}/actions-repo/.github {RUNNER_TEMP} {RUNNER_ROOT}/bin"
              f" && cp -r /actions {WORKSPACE}/actions-repo/.github/actions"
              f" && cp -L /usr/bin/python3 {RUNNER_ROOT}/bin/Runner.Worker"
              f" && echo '{{\"scheme\":\"OAuth\",\"data\":{{\"clientId\":\"c\"}}}}' > {RUNNER_ROOT}/.credentials"
              f" && (umask 077 && echo 'rsa-private-key' > {RUNNER_ROOT}/.credentials_rsaparams"
              f"     && echo runner-private > {RUNNER_HOME}/.private && mkdir {RUNNER_HOME}/private-dir"
              f"     && echo runner-private > {RUNNER_HOME}/private-dir/x)",
              user="runner")
        # A `docker` binary that reports the daemon unreachable, so the check's
        # docker probe is meaningful (there is no daemon in the container).
        c.write("/usr/local/bin/docker", "#!/bin/sh\necho 'Cannot connect to the Docker daemon' >&2\nexit 1\n", "0755")
        # Test CA + a cert for api.anthropic.com.
        c.run("sh", "-c", "mkdir -p /root/ca /root/upstream && chmod 700 /root/upstream && cd /root/ca"
              " && openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -subj /CN=isolated-agent-test-ca -days 2 2>/dev/null"
              " && openssl req -newkey rsa:2048 -nodes -keyout srv.key -out srv.csr -subj /CN=api.anthropic.com 2>/dev/null"
              " && printf 'subjectAltName=DNS:api.anthropic.com' > ext"
              " && openssl x509 -req -in srv.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out srv.crt -days 2 -extfile ext 2>/dev/null"
              " && cp ca.crt /usr/local/share/ca-certificates/isolated-agent-test-ca.crt && update-ca-certificates >/dev/null")
        c.write("/root/fake_upstream.py", FAKE_TLS_UPSTREAM)
        subprocess.run(["docker", "exec", "-d", name, "python3", "/root/fake_upstream.py"], check=True)
        # A `claude` on PATH so the setup script skips its npm install.
        c.write("/usr/local/bin/claude", FAKE_CLAUDE, "0755")
        c.write("/usr/local/lib/sentinel.py", SENTINEL, "0644")
        c.write("/usr/local/lib/connect_probe.py", CONNECT_PROBE, "0644")
        for _ in range(100):
            if c.run("test", "-f", "/root/upstream/ready", check=False).returncode == 0:
                break
            time.sleep(0.1)
        yield c
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def parse_outputs_file(text: str) -> dict:
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.fullmatch(r"([a-z-]+)<<EOF", lines[i])
        if m:
            j = lines.index("EOF", i + 1)
            out[m.group(1)] = "\n".join(lines[i + 1:j]); i = j + 1
        else:
            m2 = re.fullmatch(r"([a-z-]+)=(.*)", lines[i])
            assert m2, lines[i]
            out[m2.group(1)] = m2.group(2); i += 1
    return out


@pytest.fixture(scope="session")
def env(container: Container) -> dict:
    """Start the broker, the runner sentinel, and run the isolated-agent SETUP
    step — the trusted bootstrap, in order, before the check and the run."""
    # Broker (model-broker action's start step, verbatim, as runner).
    container.run("touch", "/tmp/broker_output", user="runner")
    container.bash_step(start_step(), {
        "MODEL_BROKER_API_KEY": REAL_KEY, "BROKER_PORT": "8317", "BROKER_LIFETIME_MINUTES": "20",
        "STOP_FILE": "/tmp/model-broker.stop", "ACTION_PATH": "/actions/model-broker", "GITHUB_OUTPUT": "/tmp/broker_output",
    })
    outputs = parse_outputs_file(container.run("cat", "/tmp/broker_output", user="runner").stdout)
    # The sentinel .NET-like runner process (owned by runner, secret in
    # memory), started as Runner.Worker from the runner's install directory so
    # the check step finds that directory the way it does on a hosted runner.
    subprocess.run(["docker", "exec", "-d", "-u", "runner", "-e", "TMPDIR=/tmp",
                    "-e", f"SENTINEL_SECRET={SENTINEL_SECRET}", container.name,
                    f"{RUNNER_ROOT}/bin/Runner.Worker", "/usr/local/lib/sentinel.py"], check=True)
    for _ in range(100):
        if container.run("test", "-f", "/tmp/sentinel.pid", check=False).returncode == 0:
            break
        time.sleep(0.1)
    # isolated-agent SETUP (verbatim, as runner with sudo), from the workspace
    # as a composite step runs, with the paths the callers pass.
    setup = container.bash_step(ia_step("Set up the isolated agent user"), {
        "AGENT_USER": AGENT_USER, "WRITE_DIR": WRITE_DIR, "AGENT_PROMPT": "analyze the thing",
        "AGENT_SETTINGS": '{"permissions": {"allow": ["Read"]}}', "WORK_DIR": WORKSPACE,
        "RUNNER_TEMP": RUNNER_TEMP, "GITHUB_ACTION_PATH": ACTION_PATH, "PATH": SYSTEM_PATH,
    }, cwd=WORKSPACE)
    outputs["setup_log"] = setup.stdout
    outputs["sentinel_pid"] = container.run("cat", "/tmp/sentinel.pid").stdout.strip()
    return outputs


def as_user(container: Container, user: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return container.run("sudo", "-n", "-u", user, "--", *args, user="runner", check=check)


def as_agent(container: Container, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return as_user(container, AGENT_USER, *args, check=check)


def has_yama(container: Container) -> bool:
    return container.run("test", "-f", "/proc/sys/kernel/yama/ptrace_scope", check=False).returncode == 0


# --- Setup: the users and the tools ----------------------------------------------


def test_setup_creates_an_unprivileged_agent_separate_from_runner_and_broker(env, container):
    ids = {u: container.run("id", u).stdout for u in (AGENT_USER, "runner", "model-broker")}
    uids = {u: re.search(r"uid=(\d+)", v).group(1) for u, v in ids.items()}
    assert len({uids[AGENT_USER], uids["runner"], uids["model-broker"]}) == 3, uids
    # The agent is in no sudo or docker group.
    assert "sudo" not in ids[AGENT_USER] and "docker" not in ids[AGENT_USER], ids[AGENT_USER]
    # The prompt/settings the agent will read are runner-owned and not
    # agent-writable; the write-dir is the agent's.
    assert as_agent(container, "test", "-w", f"{RUNNER_TEMP}/isolated-agent/prompt.txt").returncode != 0
    assert as_agent(container, "test", "-w", WRITE_DIR).returncode == 0


# --- Bootstrap: reaching the workspace under the runner's private home -----------


def test_the_private_runner_home_blocks_git_run_as_another_user_from_the_workspace(env, container):
    # The 2026-09-22 hosted failure, reproduced with a fresh user that got no
    # traversal grant: the workspace is runner-owned and world-readable
    # (0755), yet `git config --global` run as that user with the workspace as
    # its working directory dies stat-ing it, because /home/runner (0750)
    # denies the user search permission. Ownership and the target's own mode
    # do not decide reachability; every ancestor does.
    container.run("sudo", "-n", "useradd", "--system", "--user-group", "--create-home", "repro-agent", user="runner")
    try:
        assert container.run("stat", "-c", "%a %U", WORKSPACE).stdout.strip() == "755 runner"
        assert as_user(container, "repro-agent", "test", "-x", WORKSPACE).returncode != 0
        git = container.run("sudo", "-n", "-u", "repro-agent", "-H", "git", "config", "--global", "--add", "safe.directory", "*",
                            user="runner", cwd=WORKSPACE, check=False)
        assert git.returncode == 128, git.stdout + git.stderr
        assert f"failed to stat '{WORKSPACE}'" in git.stderr and "Permission denied" in git.stderr, git.stderr
        # The fix's first half: the same command from / succeeds.
        container.run("sudo", "-n", "-u", "repro-agent", "-H", "git", "config", "--global", "--add", "safe.directory", "*",
                      user="runner", cwd="/")
    finally:
        container.run("sudo", "-n", "userdel", "-r", "repro-agent", user="runner", check=False)


def test_setup_grants_search_only_on_the_ancestor_that_denies_traversal_and_nothing_else(env, container):
    # The runner's home is the one ancestor that blocked the agent; it gets a
    # named-user ACL entry with search permission only, and the setup log
    # names it with the mode it worked around. Directories that already
    # allowed traversal (755) get no entry.
    assert f"granted {AGENT_USER} search-only (--x) access on {RUNNER_HOME}, which was drwxr-x--- runner:runner, keeping its ACL mask r-x" in env["setup_log"], env["setup_log"]
    assert env["setup_log"].count("granted") == 1, env["setup_log"]
    acl = container.run("getfacl", "-c", "-p", "-E", RUNNER_HOME).stdout.split()
    # The one new entry, and the mask setfacl would have derived anyway (the
    # group bits), written explicitly so nothing else's effective rights move.
    assert f"user:{AGENT_USER}:--x" in acl and "mask::r-x" in acl and "group::r-x" in acl, acl
    for untouched in (f"{RUNNER_HOME}/work", WORKSPACE, RUNNER_TEMP, ACTION_PATH, WRITE_DIR):
        assert f"user:{AGENT_USER}" not in container.run("getfacl", "-c", "-p", untouched).stdout, untouched
    # Other users gained nothing: the home's other-bits are still ---.
    assert container.run("stat", "-c", "%a", RUNNER_HOME).stdout.strip() == "750"


def test_agent_reaches_exactly_the_paths_it_needs_and_not_the_runners_private_files(env, container):
    # Through the granted ancestor the agent reaches what the check and run
    # steps use...
    assert as_agent(container, "ls", WORKSPACE).returncode == 0
    assert as_agent(container, "test", "-r", f"{ACTION_PATH}/check_isolation.sh").returncode == 0
    assert as_agent(container, "test", "-r", f"{RUNNER_TEMP}/isolated-agent/prompt.txt").returncode == 0
    assert as_agent(container, "test", "-w", WRITE_DIR).returncode == 0
    # ...but cannot list the home itself (search is not read), write the
    # runner-owned workspace, or open what the runner keeps private: a 0600
    # file, a 0700 directory, and the runner's registration private key.
    assert as_agent(container, "ls", RUNNER_HOME).returncode != 0
    assert as_agent(container, "touch", f"{WORKSPACE}/agent-was-here").returncode != 0
    for closed in (f"{RUNNER_HOME}/.private", f"{RUNNER_HOME}/private-dir/x", f"{RUNNER_ROOT}/.credentials_rsaparams"):
        assert as_agent(container, "cat", closed).returncode != 0, closed
    # What the runner leaves world-readable under its home is reachable by
    # name: the documented residual, here the runner's non-secret OAuth
    # client record (the check above passed with it so).
    assert as_agent(container, "cat", f"{RUNNER_ROOT}/.credentials").returncode == 0


def masked_dir(container: Container, path: str, acl: str) -> None:
    # A root-owned 0750 ancestor (no search for others, as /home/runner)
    # carrying an ACL with a named entry for `nobody` that its mask holds
    # down; the workdir below is runner-owned 755.
    container.run("sudo", "-n", "sh", "-c",
                  f"rm -rf {path} && install -d -o root -g root -m 0750 {path} && install -d -o runner -g runner -m 0755 {path}/work"
                  f" && setfacl -m {acl} {path}", user="runner")


def test_grant_keeps_the_existing_acl_mask_so_another_principal_gains_nothing(container):
    # Review round 1 (Blocking): an ancestor with `user:nobody:rwx` masked to
    # `r-x`. A bare `setfacl -m u:agent:--x` would recalculate the mask to rwx
    # and hand `nobody` write access to the directory. The actual setup must
    # leave nobody unable to write, keep the mask, and still let the agent
    # through to the workdir below.
    masked_dir(container, "/srv/masked", "u:nobody:rwx,m::r-x")
    try:
        assert as_user(container, "nobody", "touch", "/srv/masked/before").returncode != 0, "fixture: nobody must start out masked"
        r = run_setup(container, AGENT_USER, WRITE_DIR, workdir="/srv/masked/work", runner_temp=f"{RUNNER_HOME}/work/_temp-masked")
        assert f"access on /srv/masked, which was drwxr-x--- root:root, keeping its ACL mask r-x" in r.stdout, r.stdout
        acl = container.run("getfacl", "-c", "-p", "-E", "/srv/masked").stdout.split()
        assert "mask::r-x" in acl and "user:nobody:rwx" in acl and f"user:{AGENT_USER}:--x" in acl, acl
        assert as_user(container, "nobody", "touch", "/srv/masked/after").returncode != 0, "nobody gained write access"
        assert as_agent(container, "test", "-x", "/srv/masked/work").returncode == 0
        assert as_agent(container, "ls", "/srv/masked/work").returncode == 0
    finally:
        container.run("sudo", "-n", "rm", "-rf", "/srv/masked", user="runner")


def test_grant_fails_closed_when_raising_the_mask_would_widen_another_principal(container):
    # The mask has no x, so the agent's --x would be ineffective, and raising
    # the mask would give `nobody` (entry rwx, effective r--) search access it
    # does not have. Setup refuses and leaves the ACL as it found it.
    masked_dir(container, "/srv/nox", "u:nobody:rwx,m::r--")
    try:
        r = run_setup(container, AGENT_USER, WRITE_DIR, workdir="/srv/nox/work", runner_temp=f"{RUNNER_HOME}/work/_temp-nox", check=False)
        assert r.returncode != 0
        assert "cannot grant claude-agent search access on /srv/nox without widening another principal: its ACL mask is 'r--'" in r.stdout, r.stdout + r.stderr
        acl = container.run("getfacl", "-c", "-p", "-E", "/srv/nox").stdout.split()
        assert "mask::r--" in acl and f"user:{AGENT_USER}:--x" not in acl, acl
        assert as_user(container, "nobody", "test", "-x", "/srv/nox").returncode != 0
    finally:
        container.run("sudo", "-n", "rm", "-rf", "/srv/nox", user="runner")


def test_grant_raises_a_mask_without_x_only_when_no_entry_would_gain(container):
    # A plain 0700 root-owned ancestor: its implied mask is --- and the only
    # group-class entry (the owning group, ---) carries no x, so adding x to
    # the mask widens nobody; the agent traverses, the group still cannot.
    container.run("sudo", "-n", "sh", "-c",
                  "rm -rf /srv/seven && install -d -o root -g root -m 0700 /srv/seven && install -d -o runner -g runner -m 0755 /srv/seven/work", user="runner")
    try:
        r = run_setup(container, AGENT_USER, WRITE_DIR, workdir="/srv/seven/work", runner_temp=f"{RUNNER_HOME}/work/_temp-seven")
        assert "access on /srv/seven, which was drwx------ root:root, keeping its ACL mask --x" in r.stdout, r.stdout
        acl = container.run("getfacl", "-c", "-p", "-E", "/srv/seven").stdout.split()
        assert "mask::--x" in acl and "group::---" in acl, acl
        assert as_agent(container, "ls", "/srv/seven/work").returncode == 0
    finally:
        container.run("sudo", "-n", "rm", "-rf", "/srv/seven", user="runner")


def test_setup_fails_closed_when_a_needed_path_is_still_unreadable(container):
    # A workdir the agent can be walked into but not read (root-owned 0700):
    # the search-only grant is not a read grant, so the verification at the
    # end of setup must name the gap and fail instead of letting the run step
    # discover it inside claude.
    container.run("sudo", "-n", "sh", "-c", "rm -rf /srv/closed && install -d -o root -g root -m 0700 /srv/closed", user="runner")
    try:
        # A separate RUNNER_TEMP so this setup does not re-stage the prompt
        # and settings the run tests below read.
        r = run_setup(container, AGENT_USER, WRITE_DIR, workdir="/srv/closed", runner_temp=f"{RUNNER_HOME}/work/_temp-closed", check=False)
        assert r.returncode != 0
        assert f"not reachable by {AGENT_USER} after the traversal grants: workdir=/srv/closed" in r.stdout, r.stdout + r.stderr
    finally:
        container.run("sudo", "-n", "rm", "-rf", "/srv/closed", user="runner")


# --- Check: the isolation boundary (B1/B2) ---------------------------------------


def make_command_file(container: Container, path: str, mode: str, owner: str = "runner", group: str = "runner") -> None:
    # Deterministic regardless of any prior owner (sudo rm first, create as the
    # named owner/group/mode). A plain `> file` as the container's default user
    # was flaky under the CI docker daemon.
    container.run("sudo", "-n", "sh", "-c",
                  f"rm -f {path} && install -m {mode} -o {owner} -g {group} /dev/null {path}", user="runner")


def run_check(container: Container, command_file: str = "/tmp/ghenv") -> subprocess.CompletedProcess:
    # The action's check step, verbatim, as the runner from the workspace: it
    # finds the runner's install directory from the Runner.Worker process and
    # drops to the agent for check_isolation.sh. A runner-owned command file
    # stands in for $GITHUB_ENV to prove it is not agent-writable.
    if command_file == "/tmp/ghenv":
        make_command_file(container, command_file, "644")
    return container.bash_step(ia_step("Check the agent is isolated"), {
        "AGENT_USER": AGENT_USER, "BROKER_HOME": "/var/lib/model-broker", "TMPDIR": "/tmp",
        "GITHUB_ACTION_PATH": ACTION_PATH, "GITHUB_ENV": command_file, "PATH": SYSTEM_PATH,
    }, cwd=WORKSPACE, check=False)


def check_errors(r: subprocess.CompletedProcess) -> list[str]:
    return [ln for ln in r.stdout.splitlines() if ln.startswith("::error::")]


def test_check_isolation_holds_for_the_agent(env, container):
    r = run_check(container)
    errors = check_errors(r)
    if has_yama(container):
        assert r.returncode == 0 and errors == [], r.stdout + r.stderr
        assert "agent isolation holds" in r.stdout
    else:
        # No Yama (Docker Desktop): the ONLY allowed failure is ptrace_scope.
        # Every UID-boundary probe below still passes without it.
        assert r.returncode == 1
        assert len(errors) == 1 and "ptrace_scope" in errors[0], r.stdout


def test_check_flags_a_runner_private_key_the_agent_can_read(env, container):
    # The check step located the runner's install directory from the
    # Runner.Worker process (there is no other way this probe could fire), and
    # a registration private key there that the traversal grant made
    # reachable AND whose own mode lets the agent read it fails the check.
    container.run("chmod", "0644", f"{RUNNER_ROOT}/.credentials_rsaparams", user="runner")
    try:
        r = run_check(container)
        assert r.returncode == 1
        assert any(f"{RUNNER_ROOT}/.credentials_rsaparams (the runner's registration private key) is readable" in e for e in check_errors(r)), r.stdout
    finally:
        container.run("chmod", "0600", f"{RUNNER_ROOT}/.credentials_rsaparams", user="runner")
    # Back at 0600 the probe is quiet again (with .credentials still 0644).
    assert not any("registration private key" in e for e in check_errors(run_check(container)))


def test_agent_cannot_escalate_or_reach_docker(env, container):
    assert as_agent(container, "sudo", "-n", "true").returncode != 0
    assert as_agent(container, "docker", "info").returncode != 0


def test_agent_cannot_read_the_brokers_key_token_or_memory(env, container):
    pid = re.search(r"pid=(\d+)", container.run("cat", "/var/lib/model-broker/ready", user="runner").stdout).group(1)
    for path in ("/var/lib/model-broker/key", "/var/lib/model-broker/token", f"/proc/{pid}/environ", f"/proc/{pid}/mem"):
        assert as_agent(container, "cat", path).returncode != 0, path
    assert as_agent(container, "ls", "/var/lib/model-broker").returncode != 0


def test_agent_cannot_read_the_runner_worker_memory_or_its_diagnostic_socket(env, container):
    pid = env["sentinel_pid"]
    # B2: the runner-owned process's memory and environment are closed...
    assert as_agent(container, "cat", f"/proc/{pid}/environ").returncode != 0
    assert as_agent(container, "cat", f"/proc/{pid}/mem").returncode != 0
    # ...and its .NET diagnostic socket is not connectable by the agent (the
    # same-UID dump channel Yama would not have blocked).
    sock = container.run("sh", "-c", f"ls /tmp/dotnet-diagnostic-{pid}-*-socket").stdout.strip()
    assert sock, "the sentinel opened no diagnostic socket"
    probe = as_agent(container, "python3", "/usr/local/lib/connect_probe.py", sock)
    assert probe.returncode != 0 and "refused" in probe.stdout, probe.stdout
    # F4: a BINARY-SAFE sweep (grep -a, not -I which skips the NUL-bearing
    # cmdline/environ) of every process file the agent can read finds the
    # sentinel's in-memory secret nowhere -- it was never in argv or a readable
    # environ. The needle travels on stdin so the sweep's own argv is not a hit.
    def sweep(needle: str, grep_opts: str) -> subprocess.CompletedProcess:
        return container.run(
            "sudo", "-n", "-u", AGENT_USER, "sh", "-c",
            f"n=$(cat); printf '%s\\n' \"$n\" | grep {grep_opts} -f - /proc/[0-9]*/cmdline /proc/[0-9]*/environ 2>/dev/null; true",
            user="runner", stdin=needle, check=False)
    assert sweep(SENTINEL_SECRET, "-a -l").stdout.strip() == "", \
        f"the sentinel secret is readable at: {sweep(SENTINEL_SECRET, '-a -l').stdout}"
    # Positive control: a secret deliberately exposed in a runner process's
    # argv IS found by the same binary-safe sweep, so it is not vacuously
    # empty -- and the old `grep -I` MISSES it, which is the F4 bug.
    subprocess.run(["docker", "exec", "-d", "-u", "runner", container.name,
                    "python3", "-c", "import time; time.sleep(300)", EXPOSED_SECRET], check=True)
    for _ in range(50):
        if EXPOSED_SECRET in container.run("sh", "-c", "cat /proc/[0-9]*/cmdline 2>/dev/null | tr '\\000' ' '", check=False).stdout:
            break
        time.sleep(0.05)
    try:
        assert sweep(EXPOSED_SECRET, "-a -l").stdout.strip() != "", "binary-safe sweep should find an argv-exposed secret"
        assert sweep(EXPOSED_SECRET, "-r -I -l").stdout.strip() == "", "grep -I skips NUL cmdline: the F4 false negative"
    finally:
        container.run("pkill", "-f", EXPOSED_SECRET, check=False)


def test_check_fails_closed_when_the_agent_can_sudo(env, container):
    # Grant the agent sudo transiently: the check must refuse (return 1) so the
    # job would fail before the agent ran.
    container.run("sh", "-c", f"echo '{AGENT_USER} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent-oops && chmod 440 /etc/sudoers.d/agent-oops")
    try:
        r = run_check(container)
        assert r.returncode == 1
        assert any("can sudo" in ln for ln in r.stdout.splitlines()), r.stdout
    finally:
        container.run("rm", "-f", "/etc/sudoers.d/agent-oops")


def test_check_flags_a_writable_runner_command_file(env, container):
    # A command file the agent CAN write. To make writability deterministic
    # across hosts (the CI runner's /tmp neither honours group-write across
    # sudo nor "other"-write for a file there), the file is agent-OWNED; the
    # probe opens it for append, which needs only file-owner write. The check
    # must flag it. Precondition: confirm the agent really can write it.
    make_command_file(container, "/tmp/ghenv-bad", "644", owner=AGENT_USER, group=AGENT_USER)
    assert as_agent(container, "sh", "-c", "echo probe >> /tmp/ghenv-bad").returncode == 0, \
        "fixture: the agent should be able to write its own command file"
    r = run_check(container, command_file="/tmp/ghenv-bad")
    assert any("is writable by the agent" in ln for ln in r.stdout.splitlines()), r.stdout


# --- Run: the whole claude process as the agent, under env -i --------------------


def test_run_step_launches_claude_as_the_agent_under_env_i(env, container):
    r = container.run("touch", "/tmp/run_output", user="runner")
    out = container.bash_step(ia_step("Run the agent"), {
        "AGENT_USER": AGENT_USER, "WORK_DIR": WORKSPACE,
        "BASE_URL": env["base-url"], "TOKEN": env["token"], "GH_TOKEN_IN": "job-token-123",
        "CLAUDE_ARGS": "--model fable --allowedTools Bash,Read", "FORWARD_ENV": f"AGENT_RECORD_DIR={WRITE_DIR}",
        "RUNNER_TEMP": RUNNER_TEMP, "GITHUB_OUTPUT": "/tmp/run_output", "PATH": SYSTEM_PATH,
        # Vars that MUST be stripped by env -i, planted in the runner step's env:
        "CI_PERF_ANTHROPIC_API_KEY": REAL_KEY, "GITHUB_ENV": "/tmp/ghenv",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-secret", "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example",
    }, cwd=WORKSPACE)
    assert parse_outputs_file(container.run("cat", "/tmp/run_output", user="runner").stdout)["conclusion"] == "success"
    # It ran as the agent, in the workspace under the runner's private home,
    # and could read it.
    assert container.run("cat", f"{WRITE_DIR}/whoami").stdout.strip() == AGENT_USER
    assert container.run("cat", f"{WRITE_DIR}/cwd").stdout.strip() == WORKSPACE
    assert "actions-repo" in container.run("cat", f"{WRITE_DIR}/workdir-listing").stdout.split()
    agent_env = container.run("cat", f"{WRITE_DIR}/env.txt").stdout
    names = {ln.split("=", 1)[0] for ln in agent_env.splitlines() if "=" in ln}
    # Only the intended variables reach the agent.
    assert f"ANTHROPIC_API_KEY={env['token']}" in agent_env
    assert f"ANTHROPIC_BASE_URL={env['base-url']}" in agent_env
    assert "GH_TOKEN=job-token-123" in agent_env
    assert f"AGENT_RECORD_DIR={WRITE_DIR}" in agent_env
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in agent_env
    # The reusable key and every runner/OIDC channel are absent.
    assert REAL_KEY not in agent_env
    for forbidden in ("CI_PERF_ANTHROPIC_API_KEY", "GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH",
                      "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL", "MODEL_BROKER_API_KEY"):
        assert forbidden not in names, f"{forbidden} leaked into the agent env"
    # --settings was passed, pointing at the runner-owned settings file.
    argv = container.run("cat", f"{WRITE_DIR}/argv").stdout
    assert "--settings" in argv and f"{RUNNER_TEMP}/isolated-agent/settings.json" in argv
    assert "--model" in argv and "fable" in argv


def test_the_agent_output_carries_no_reusable_key(env, container):
    # Whatever the agent wrote (here its recorded env and a report) — the sink
    # ci-perf/triage publish — contains no reusable key, because the agent
    # never held one. Its ANTHROPIC_API_KEY is the per-run broker token.
    dump = container.run("sh", "-c", f"cat {WRITE_DIR}/* 2>/dev/null").stdout
    assert REAL_KEY not in dump
    assert env["token"] in dump  # present, and worthless off this runner


# --- The broker still forwards, refuses and shuts down ---------------------------


def test_a_call_through_the_broker_reaches_the_pinned_upstream_with_the_real_key(env, container):
    container.write("/tmp/client.py", CLIENT_CALL)
    r = as_agent(container, "python3", "/tmp/client.py", "8317", env["token"], check=True)
    assert r.stdout.splitlines()[0] == "200", r.stdout
    seen = [json.loads(line) for line in container.run("cat", "/root/upstream/seen.jsonl").stdout.splitlines()]
    headers = {k.lower(): v for k, v in seen[-1]["headers"].items()}
    assert seen[-1]["path"] == "/v1/messages?beta=true"
    assert headers["x-api-key"] == REAL_KEY and headers["host"] == "api.anthropic.com"
    assert env["token"] not in json.dumps(seen[-1]["headers"])


def test_a_wrong_token_never_reaches_the_upstream(env, container):
    before = container.run("wc", "-l", "/root/upstream/seen.jsonl").stdout.split()[0]
    r = as_agent(container, "python3", "/tmp/client.py", "8317", "broker-run-" + "0" * 48, check=True)
    assert r.stdout.splitlines()[0] == "401"
    assert container.run("wc", "-l", "/root/upstream/seen.jsonl").stdout.split()[0] == before



def test_the_stop_file_ends_the_broker(env, container):
    container.run("touch", "/tmp/model-broker.stop", user="runner")
    for _ in range(100):
        if container.run("pgrep", "-u", "model-broker", "-x", "python3", check=False).returncode != 0:
            break
        time.sleep(0.1)
    assert container.run("pgrep", "-u", "model-broker", "-x", "python3", check=False).returncode != 0
    assert "stop file present; exiting" in container.run("cat", "/var/lib/model-broker/broker.log", user="runner").stdout


# --- F1: the agent's home does not collide with harden-runner's /home/agent --


def run_setup(container: Container, agent_user: str, write_dir: str, *, workdir: str = WORKSPACE,
              runner_temp: str = RUNNER_TEMP, check: bool = True) -> subprocess.CompletedProcess:
    return container.bash_step(ia_step("Set up the isolated agent user"), {
        "AGENT_USER": agent_user, "WRITE_DIR": write_dir, "AGENT_PROMPT": "p",
        "AGENT_SETTINGS": "", "WORK_DIR": workdir, "RUNNER_TEMP": runner_temp,
        "GITHUB_ACTION_PATH": ACTION_PATH, "PATH": SYSTEM_PATH,
    }, cwd=WORKSPACE, check=check)


def test_agent_home_is_not_harden_runners_directory(env, container):
    home = container.run("sh", "-c", f"getent passwd {AGENT_USER} | cut -d: -f6").stdout.strip()
    assert home == f"/home/{AGENT_USER}" and AGENT_USER != "agent"
    # /home/agent is harden-runner's; setup created /home/claude-agent and
    # never touched /home/agent (it does not exist in this container).
    assert container.run("test", "-e", "/home/agent", check=False).returncode != 0


def test_setup_refuses_the_name_agent(container):
    r = run_setup(container, "agent", "/tmp/agent-out", check=False)
    assert r.returncode != 0 and "must not be 'agent'" in r.stdout, r.stdout


def test_setup_refuses_a_colliding_pre_existing_home(container):
    # A pre-existing home for a not-yet-created user (a trusted directory such
    # as harden-runner's) makes setup refuse and leaves it untouched.
    container.run("sudo", "-n", "sh", "-c",
                  "rm -rf /home/collide-agent && install -d -o root -g root -m 0755 /home/collide-agent && echo trusted > /home/collide-agent/x",
                  user="runner")
    try:
        r = run_setup(container, "collide-agent", "/tmp/collide-out", check=False)
        assert r.returncode != 0 and "already exists" in r.stdout, r.stdout
        assert container.run("stat", "-c", "%U", "/home/collide-agent").stdout.strip() == "root"
        assert container.run("id", "-u", "collide-agent", check=False).returncode != 0
    finally:
        container.run("sudo", "-n", "rm", "-rf", "/home/collide-agent", user="runner")


# --- F2: the landing/output dir is writable by both the runner and the agent -


def test_write_dir_is_writable_by_both_runner_and_agent(env, container):
    # emit-landing (triage) runs as the runner and must create manifest.json in
    # the write-dir; the agent must also be able to write its manifest and body
    # files. setgid + group-write (2775, runner:agent-group) gives both without
    # handing the agent any runner control file.
    perms = container.run("stat", "-c", "%a %U %G", WRITE_DIR).stdout.strip()
    assert perms == f"2775 runner {AGENT_USER}", perms
    # The runner (owner) creates manifest.json, as emit-landing would.
    assert container.run("sh", "-c", f"echo m > {WRITE_DIR}/manifest.json", user="runner", check=False).returncode == 0
    # The agent (group) creates its manifest-extra and a body file.
    assert as_agent(container, "sh", "-c", f"echo e > {WRITE_DIR}/manifest-extra.json").returncode == 0
    assert as_agent(container, "sh", "-c", f"echo b > {WRITE_DIR}/issue.md").returncode == 0
    # Each can read the other's file (setgid group + world-read).
    assert container.run("cat", f"{WRITE_DIR}/issue.md", user="runner").stdout.strip() == "b"
    assert as_agent(container, "cat", f"{WRITE_DIR}/manifest.json", check=True).stdout.strip() == "m"


# --- F3: a nonzero agent exit fails the job even when nothing is published ---


def test_run_records_failure_and_the_gate_fails_the_job(env, container):
    # The run step exits 0 (so `conclusion` propagates) but records failure
    # when Claude exits nonzero; the gate step then fails the job, independently
    # of any publisher (ci-perf skips publish for dry-run/non-main refs, F3).
    container.run("mkdir", "-p", "/tmp/failbin")
    container.write("/tmp/failbin/claude", "#!/bin/sh\nexit 23\n", "0755")
    container.run("chmod", "0755", "/tmp/failbin")
    container.run("touch", "/tmp/failout", user="runner")
    run = container.bash_step(ia_step("Run the agent"), {
        "AGENT_USER": AGENT_USER, "WORK_DIR": WORKSPACE,
        "BASE_URL": "http://127.0.0.1:8317", "TOKEN": "t", "GH_TOKEN_IN": "g",
        "CLAUDE_ARGS": "--model fable", "FORWARD_ENV": "",
        "RUNNER_TEMP": RUNNER_TEMP, "GITHUB_OUTPUT": "/tmp/failout",
        "PATH": f"/tmp/failbin:{SYSTEM_PATH}",
    }, cwd=WORKSPACE, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert parse_outputs_file(container.run("cat", "/tmp/failout", user="runner").stdout)["conclusion"] == "failure"
    # The gate fails on a non-success conclusion, passes on success.
    gate = ia_step("Fail the job if the agent did not succeed")
    fail = container.bash_step(gate, {"CONCLUSION": "failure"}, check=False)
    assert fail.returncode != 0 and "did not succeed" in fail.stdout, fail.stdout
    assert container.bash_step(gate, {"CONCLUSION": "success"}, check=False).returncode == 0


# --- On a hosted runner: the actual layout, in place -------------------------------
#
# The container above models the hosted layout; these tests run the same three
# step scripts on the hosted runner itself when the suite runs there (tests.yml
# on ubuntu-latest: GITHUB_ACTIONS is set, the runner user has sudo), against
# the real /home/runner, the real Runner.Worker and .NET diagnostic sockets,
# Yama, the real command files and the real registration credentials — what
# Docker Desktop's kernel and a stand-in process cannot supply. The broker is
# the action's start step with a dummy key; the check never calls upstream. A
# stand-in `claude` on PATH keeps the run step off the network. Skipped
# anywhere else; on a runner the skip reason names what is missing.


def hosted_runner_unavailable() -> str:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return "not on a GitHub Actions runner"
    if not Path("/proc/sys/kernel").exists():
        return "not Linux"
    if subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False).returncode != 0:
        return "the runner user has no passwordless sudo"
    return ""


class Host:
    """The Container interface, on this machine: `user` runs the command through
    sudo, and a step runs with the job's own environment plus the step env."""

    def run(self, *args: str, user: str | None = None, env: dict | None = None, stdin: str | None = None,
            cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["sudo", "-n", "-u", user, "-H", "--", *args] if user and user != "runner" else list(args)
        r = subprocess.run(cmd, input=stdin, text=True, capture_output=True, check=False, cwd=cwd,
                           env={**os.environ, **(env or {})})
        if check and r.returncode != 0:
            raise AssertionError(f"{args} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return r

    def bash_step(self, step: dict, env: dict, *, user: str = "runner", cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        script = Path(os.environ["RUNNER_TEMP"]) / "isolated-agent-hosted-test" / f"step-{uuid.uuid4().hex[:8]}.sh"
        script.write_text(step["run"])
        return self.run("bash", "--noprofile", "--norc", "-eo", "pipefail", str(script), user=user, env=env, cwd=cwd, check=check)


@pytest.fixture(scope="session")
def hosted() -> dict:
    reason = hosted_runner_unavailable()
    if reason:
        pytest.skip(f"hosted-runner tests: {reason}")
    host = Host()
    base = Path(os.environ["RUNNER_TEMP"]) / "isolated-agent-hosted-test"
    base.mkdir(exist_ok=True)
    (base / "bin").mkdir(exist_ok=True)
    (base / "bin" / "claude").write_text(FAKE_CLAUDE)
    (base / "bin" / "claude").chmod(0o755)
    path = f"{base / 'bin'}:{os.environ['PATH']}"
    workspace = os.environ["GITHUB_WORKSPACE"]
    write_dir = str(base / "out")
    # The broker, from the action's start step, holding a dummy key.
    (base / "broker_output").touch()
    host.bash_step(start_step(), {
        "MODEL_BROKER_API_KEY": REAL_KEY, "BROKER_PORT": "8317", "BROKER_LIFETIME_MINUTES": "15",
        "STOP_FILE": "/tmp/model-broker.stop", "ACTION_PATH": str(ACTION_DIR), "GITHUB_OUTPUT": str(base / "broker_output"),
    })
    outputs = parse_outputs_file((base / "broker_output").read_text())
    # isolated-agent SETUP, verbatim, from the workspace as the runner runs it.
    setup = host.bash_step(ia_step("Set up the isolated agent user"), {
        "AGENT_USER": AGENT_USER, "WRITE_DIR": write_dir, "AGENT_PROMPT": "analyze the thing",
        "AGENT_SETTINGS": '{"permissions": {"allow": ["Read"]}}', "WORK_DIR": workspace,
        "GITHUB_ACTION_PATH": str(ACTIONS_MOUNT / "isolated-agent"), "PATH": path,
    }, cwd=workspace)
    granted = re.findall(r"granted \S+ search-only \(--x\) access on (\S+), which was (\S+ \S+)", setup.stdout)
    outputs.update(host=host, base=base, path=path, workspace=workspace, write_dir=write_dir,
                   setup_log=setup.stdout, granted=granted)
    # Evidence for the run's summary: what blocked the agent and what was granted.
    ancestors = []
    p = Path(workspace)
    while p != p.parent:
        ancestors.append(p)
        p = p.parent
    lines = ["### isolated-agent bootstrap on this runner", "", "Ancestors of the workspace (mode owner:group):", ""]
    lines += [f"- `{a}`: `{host.run('stat', '-c', '%A %U:%G', str(a)).stdout.strip()}`" for a in reversed(ancestors)]
    lines += ["", "Setup granted search-only access on:", ""] + [f"- `{d}` (was `{mode}`)" for d, mode in granted]
    # The runner's registration files: modes, and the key names (never the
    # values) of the world-readable .credentials, so the record shows what
    # the traversal grant leaves reachable there.
    worker = host.run("sh", "-c", "for p in $(pgrep -x Runner.Worker; pgrep -x Runner.Listener); do readlink -f /proc/$p/exe && break; done", check=False).stdout.strip()
    if worker:
        root = Path(worker).parent.parent
        lines += ["", f"Runner install directory `{root}`:", ""]
        for name in (".credentials", ".credentials_rsaparams", ".runner"):
            lines.append(f"- `{name}`: `{host.run('stat', '-c', '%A %U:%G', str(root / name), check=False).stdout.strip() or 'absent'}`")
        keys = host.run("sh", "-c", f"python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get(\"scheme\"), sorted((d.get(\"data\") or {{}}).keys()))' {root / '.credentials'}", check=False)
        lines.append(f"- `.credentials` scheme and data keys: `{keys.stdout.strip() or keys.stderr.strip()}`")
    lines.append("")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(lines) + "\n")
    yield outputs
    Path("/tmp/model-broker.stop").touch()


def test_hosted_setup_reaches_every_path_and_names_each_grant(hosted):
    host: Host = hosted["host"]
    assert "ready" in hosted["setup_log"] and "can reach" in hosted["setup_log"], hosted["setup_log"]
    # Whatever the runner's layout denied is now traversable, by a
    # search-only entry for the agent alone; the agent still cannot list it.
    for directory, mode in hosted["granted"]:
        assert f"user:{AGENT_USER}:--x" in host.run("getfacl", "-c", "-p", directory).stdout, directory
        assert host.run("ls", directory, user=AGENT_USER, check=False).returncode != 0, f"{directory} ({mode}) is listable by the agent"
    # A hosted runner's home is private; that is the ancestor the 2026-09-22
    # runs died on. If this assertion fails the image changed and the grant
    # was not needed; the test above still holds.
    assert "/home/runner" in {d for d, _ in hosted["granted"]}, hosted["granted"]
    assert host.run("test", "-r", hosted["workspace"], user=AGENT_USER, check=False).returncode == 0
    assert host.run("touch", f"{hosted['workspace']}/agent-was-here", user=AGENT_USER, check=False).returncode != 0


def test_hosted_check_isolation_holds_against_the_real_runner(hosted):
    # The check step, verbatim, as the agent, against the real Runner.Worker
    # (memory, environ, .NET diagnostic socket), Yama, the job's own command
    # files, the broker and the runner's registration credentials.
    r = hosted["host"].bash_step(ia_step("Check the agent is isolated"), {
        "AGENT_USER": AGENT_USER, "BROKER_HOME": "/var/lib/model-broker",
        "GITHUB_ACTION_PATH": str(ACTIONS_MOUNT / "isolated-agent"), "PATH": hosted["path"],
    }, cwd=hosted["workspace"], check=False)
    assert r.returncode == 0 and check_errors(r) == [], r.stdout + r.stderr
    assert "agent isolation holds" in r.stdout


def test_hosted_run_step_runs_claude_as_the_agent_in_the_workspace(hosted):
    host: Host = hosted["host"]
    out = hosted["base"] / "run_output"
    out.touch()
    host.bash_step(ia_step("Run the agent"), {
        "AGENT_USER": AGENT_USER, "WORK_DIR": hosted["workspace"], "BASE_URL": hosted["base-url"], "TOKEN": hosted["token"],
        "GH_TOKEN_IN": "job-token-123", "CLAUDE_ARGS": "--model fable", "FORWARD_ENV": f"AGENT_RECORD_DIR={hosted['write_dir']}",
        "GITHUB_OUTPUT": str(out), "PATH": hosted["path"],
    }, cwd=hosted["workspace"])
    assert parse_outputs_file(out.read_text())["conclusion"] == "success"
    record = Path(hosted["write_dir"])
    assert (record / "whoami").read_text().strip() == AGENT_USER
    assert (record / "cwd").read_text().strip() == hosted["workspace"]
    assert ".github" in (record / "workdir-listing").read_text().split()
    assert (record / "report.md").exists()
    # The write-dir is shared work product: the runner can still write it.
    (record / "manifest.json").write_text("m\n")
