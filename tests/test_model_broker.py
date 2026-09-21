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

IMAGE = "ubuntu:24.04"
ACTIONS_MOUNT = ROOT / ".github" / "actions"
AGENT_USER = "agent"
WRITE_DIR = "/tmp/agent-out"
RUNNER_TEMP = "/tmp/rt"
SENTINEL_SECRET = "SENTINEL-RUNNER-WORKER-SECRET-" + "z" * 40

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
import os, socket, sys, time
secret = sys.argv[1]  # noqa: F841 -- kept in memory, never on disk/argv beyond this
sock = f"{os.environ.get('TMPDIR', '/tmp')}/dotnet-diagnostic-{os.getpid()}-1-socket"
try: os.unlink(sock)
except FileNotFoundError: pass
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(sock); os.chmod(sock, 0o600); s.listen(1)
open("/tmp/sentinel.pid", "w").write(str(os.getpid()))
while True: time.sleep(1)
'''

# A stand-in `claude`: records who it ran as, its whole environment, cwd and
# argv into the write-dir the prompt named (forwarded as AGENT_RECORD_DIR), so
# the test can prove the run happened as the agent under env -i.
FAKE_CLAUDE = r'''#!/usr/bin/env bash
d="${AGENT_RECORD_DIR:?the agent got no record dir}"
id -un > "$d/whoami"
/usr/bin/env > "$d/env.txt"
pwd > "$d/cwd"
printf '%s\n' "$@" > "$d/argv"
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

    def run(self, *args: str, user: str | None = None, env: dict | None = None, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["docker", "exec", "-i"]
        if user:
            cmd += ["-u", user]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        r = subprocess.run(cmd + [self.name, *args], input=stdin, text=True, capture_output=True, check=False)
        if check and r.returncode != 0:
            raise AssertionError(f"{args} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return r

    def write(self, path: str, text: str, mode: str = "0644") -> None:
        self.run("sh", "-c", f"cat > {path} && chmod {mode} {path}", stdin=text)

    def bash_step(self, step: dict, env: dict, *, user: str = "runner", check: bool = True) -> subprocess.CompletedProcess:
        """Lift a composite step's `run:` body and execute it verbatim."""
        self.write("/tmp/step.sh", step["run"], "0755")
        return self.run("bash", "--noprofile", "--norc", "-eo", "pipefail", "/tmp/step.sh", user=user, env=env, check=check)


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
              "python3 sudo ca-certificates openssl procps util-linux git >/dev/null")
        # The runner user: unprivileged but with passwordless sudo, KEPT (the
        # new design does not drop the runner's sudo; the agent user is the
        # one that has none).
        c.run("sh", "-c", "useradd -m -u 1001 runner && echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner && chmod 440 /etc/sudoers.d/runner")
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
    # The sentinel .NET-like runner process (owned by runner, secret in memory).
    subprocess.run(["docker", "exec", "-d", "-u", "runner", "-e", "TMPDIR=/tmp", container.name,
                    "python3", "/usr/local/lib/sentinel.py", SENTINEL_SECRET], check=True)
    for _ in range(100):
        if container.run("test", "-f", "/tmp/sentinel.pid", check=False).returncode == 0:
            break
        time.sleep(0.1)
    # isolated-agent SETUP (verbatim, as runner with sudo).
    container.bash_step(ia_step("Set up the isolated agent user"), {
        "AGENT_USER": AGENT_USER, "WRITE_DIR": WRITE_DIR, "AGENT_PROMPT": "analyze the thing",
        "AGENT_SETTINGS": '{"permissions": {"allow": ["Read"]}}', "WORK_DIR": "/tmp",
        "RUNNER_TEMP": RUNNER_TEMP, "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    })
    outputs["sentinel_pid"] = container.run("cat", "/tmp/sentinel.pid").stdout.strip()
    return outputs


def as_agent(container: Container, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return container.run("sudo", "-n", "-u", AGENT_USER, "--", *args, user="runner", check=check)


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


# --- Check: the isolation boundary (B1/B2) ---------------------------------------


def run_check(container: Container) -> subprocess.CompletedProcess:
    # Exactly what the action's check step runs, with a runner-owned command
    # file to prove it is not agent-writable.
    container.run("sh", "-c", "echo x > /tmp/ghenv && chmod 644 /tmp/ghenv && chown runner:runner /tmp/ghenv")
    return container.run(
        "sudo", "-n", "-u", AGENT_USER, "-H", "--", "env", "-i",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        f"EXPECT_AGENT_USER={AGENT_USER}", "BROKER_HOME=/var/lib/model-broker", "TMPDIR=/tmp",
        "RUNNER_COMMAND_FILES=/tmp/ghenv",
        "bash", "/actions/isolated-agent/check_isolation.sh",
        user="runner", check=False,
    )


def test_check_isolation_holds_for_the_agent(env, container):
    r = run_check(container)
    errors = [ln for ln in r.stdout.splitlines() if ln.startswith("::error::")]
    if has_yama(container):
        assert r.returncode == 0 and errors == [], r.stdout + r.stderr
        assert "agent isolation holds" in r.stdout
    else:
        # No Yama (Docker Desktop): the ONLY allowed failure is ptrace_scope.
        # Every UID-boundary probe below still passes without it.
        assert r.returncode == 1
        assert len(errors) == 1 and "ptrace_scope" in errors[0], r.stdout


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
    # The secret the sentinel holds in memory is nowhere the agent can read
    # (needle on stdin so the sweep's own argv is not a hit).
    sweep = container.run("sudo", "-n", "-u", AGENT_USER, "sh", "-c",
                          "needle=$(cat); printf '%s\\n' \"$needle\" | grep -rIl -f - /proc/[0-9]*/environ /proc/[0-9]*/cmdline 2>/dev/null; true",
                          user="runner", stdin=SENTINEL_SECRET, check=False)
    assert sweep.stdout.strip() == "", f"the sentinel secret is readable at: {sweep.stdout}"


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
    container.run("sh", "-c", "echo x > /tmp/ghenv-bad && chmod 666 /tmp/ghenv-bad")
    r = container.run(
        "sudo", "-n", "-u", AGENT_USER, "-H", "--", "env", "-i",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        f"EXPECT_AGENT_USER={AGENT_USER}", "BROKER_HOME=/var/lib/model-broker", "TMPDIR=/tmp",
        "RUNNER_COMMAND_FILES=/tmp/ghenv-bad",
        "bash", "/actions/isolated-agent/check_isolation.sh",
        user="runner", check=False,
    )
    assert any("is writable by the agent" in ln for ln in r.stdout.splitlines()), r.stdout


# --- Run: the whole claude process as the agent, under env -i --------------------


def test_run_step_launches_claude_as_the_agent_under_env_i(env, container):
    r = container.run("touch", "/tmp/run_output", user="runner")
    out = container.bash_step(ia_step("Run the agent"), {
        "AGENT_USER": AGENT_USER, "WORK_DIR": "/tmp",
        "BASE_URL": env["base-url"], "TOKEN": env["token"], "GH_TOKEN_IN": "job-token-123",
        "CLAUDE_ARGS": "--model fable --allowedTools Bash,Read", "FORWARD_ENV": f"AGENT_RECORD_DIR={WRITE_DIR}",
        "RUNNER_TEMP": RUNNER_TEMP, "GITHUB_OUTPUT": "/tmp/run_output",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        # Vars that MUST be stripped by env -i, planted in the runner step's env:
        "CI_PERF_ANTHROPIC_API_KEY": REAL_KEY, "GITHUB_ENV": "/tmp/ghenv",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-secret", "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example",
    })
    assert parse_outputs_file(container.run("cat", "/tmp/run_output", user="runner").stdout)["conclusion"] == "success"
    # It ran as the agent.
    assert container.run("cat", f"{WRITE_DIR}/whoami").stdout.strip() == AGENT_USER
    assert container.run("cat", f"{WRITE_DIR}/cwd").stdout.strip() == "/tmp"
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
