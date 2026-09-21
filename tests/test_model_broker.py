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


IMAGE = "ubuntu:24.04"

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

CLIENT_CALL = r"""
import http.client, sys
port, token = sys.argv[1], sys.argv[2]
c = http.client.HTTPConnection("127.0.0.1", int(port), timeout=30)
c.request("POST", "/v1/messages?beta=true", body=b'{"hello": "broker"}', headers={"x-api-key": token, "content-type": "application/json", "anthropic-version": "2023-06-01"})
r = c.getresponse(); print(r.status); print(r.read().decode())
"""


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


@pytest.fixture(scope="session")
def container():
    if not docker_available():
        pytest.skip("no Docker daemon (set MODEL_BROKER_SKIP_DOCKER=1 to silence)")
    name = f"model-broker-test-{uuid.uuid4().hex[:8]}"
    # --init: an init process reaps the broker when it exits, as systemd does
    # on a runner; without it the exited broker would linger as a zombie.
    subprocess.run(["docker", "run", "-d", "--rm", "--init", "--name", name, "--add-host", "api.anthropic.com:127.0.0.1",
                    "-v", f"{ACTION_DIR}:/action:ro", IMAGE, "sleep", "900"], check=True, capture_output=True)
    c = Container(name)
    try:
        c.run("sh", "-c", "apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "
              "python3 sudo ca-certificates openssl procps util-linux >/dev/null")
        # The runner user of a GitHub-hosted runner: unprivileged, passwordless sudo.
        c.run("sh", "-c", "useradd -m -u 1001 runner && echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner && chmod 440 /etc/sudoers.d/runner")
        # A test CA the container trusts, and a certificate for api.anthropic.com.
        c.run("sh", "-c", "mkdir -p /root/ca /root/upstream && chmod 700 /root/upstream && cd /root/ca"
              " && openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -subj /CN=model-broker-test-ca -days 2 2>/dev/null"
              " && openssl req -newkey rsa:2048 -nodes -keyout srv.key -out srv.csr -subj /CN=api.anthropic.com 2>/dev/null"
              " && printf 'subjectAltName=DNS:api.anthropic.com' > ext"
              " && openssl x509 -req -in srv.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out srv.crt -days 2 -extfile ext 2>/dev/null"
              " && cp ca.crt /usr/local/share/ca-certificates/model-broker-test-ca.crt && update-ca-certificates >/dev/null")
        c.write("/root/fake_upstream.py", FAKE_TLS_UPSTREAM)
        subprocess.run(["docker", "exec", "-d", name, "python3", "/root/fake_upstream.py"], check=True)
        for _ in range(100):
            if c.run("test", "-f", "/root/upstream/ready", check=False).returncode == 0:
                break
            time.sleep(0.1)
        yield c
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture(scope="session")
def started(container: Container) -> dict:
    """The action's start step, verbatim, as the runner user with sudo; then sudo removed as harden-runner does."""
    step = start_step()
    container.write("/tmp/start.sh", step["run"], "0755")
    container.run("touch", "/tmp/github_output", user="runner")
    r = container.run("bash", "--noprofile", "--norc", "-eo", "pipefail", "/tmp/start.sh", user="runner", env={
        "MODEL_BROKER_API_KEY": REAL_KEY, "BROKER_PORT": "8317", "BROKER_LIFETIME_MINUTES": "10",
        "STOP_FILE": "/tmp/model-broker.stop", "ACTION_PATH": "/action", "GITHUB_OUTPUT": "/tmp/github_output",
    })
    outputs = {}
    lines = container.run("cat", "/tmp/github_output", user="runner").stdout.splitlines()
    i = 0
    while i < len(lines):
        m = re.fullmatch(r"([a-z-]+)<<EOF", lines[i])
        assert m, lines[i]
        j = lines.index("EOF", i + 1)
        outputs[m.group(1)] = "\n".join(lines[i + 1:j])
        i = j + 1
    # What harden-runner's disable-sudo-and-containers does next in the job.
    container.run("rm", "/etc/sudoers.d/runner")
    return {"stdout": r.stdout, "outputs": outputs}


def test_start_script_starts_the_broker_as_its_own_user_and_reports_only_the_token(started, container):
    outputs = started["outputs"]
    assert outputs["base-url"] == "http://127.0.0.1:8317"
    assert re.fullmatch(r"broker-run-[0-9a-f]{48}", outputs["token"])
    assert outputs["stop-file"] == "/tmp/model-broker.stop"
    assert REAL_KEY not in started["stdout"] and REAL_KEY not in json.dumps(outputs)
    ps = container.run("ps", "-o", "user:20=,args=", "-C", "python3").stdout
    assert re.search(r"^model-broker\s+/usr/bin/python3 /var/lib/model-broker/model_broker\.py", ps, re.M), ps
    assert REAL_KEY not in ps


def has_yama(container: Container) -> bool:
    return container.run("test", "-f", "/proc/sys/kernel/yama/ptrace_scope", check=False).returncode == 0


def test_check_isolation_passes_once_sudo_is_gone(started, container):
    r = container.run("bash", "/action/check_isolation.sh", user="runner", check=False)
    errors = [line for line in r.stdout.splitlines() if line.startswith("::error::")]
    if has_yama(container):
        assert r.returncode == 0 and errors == [], r.stdout + r.stderr
        assert "model broker isolation holds" in r.stdout
    else:
        # A kernel without Yama (Docker Desktop's, not a GitHub runner's): the
        # check must refuse for that reason and no other.
        assert r.returncode == 1
        assert len(errors) == 1 and "ptrace_scope" in errors[0], r.stdout


def test_check_isolation_fails_while_sudo_still_works(container):
    # A fresh sudoers entry for the check only: with sudo back, the check
    # must refuse, since sudo means the key is one command away.
    container.run("sh", "-c", "echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner-again && chmod 440 /etc/sudoers.d/runner-again")
    try:
        r = container.run("bash", "/action/check_isolation.sh", user="runner", check=False)
        assert r.returncode == 1
        assert "sudo still works" in r.stdout
    finally:
        container.run("rm", "/etc/sudoers.d/runner-again")


def test_the_runner_user_cannot_read_the_key_from_any_file_or_process(started, container):
    # The key file is gone (read once, deleted); the token file and the
    # directory are closed; the broker's environ and memory are closed.
    pid = re.search(r"^pid=(\d+)$", container.run("cat", "/var/lib/model-broker/ready", user="runner").stdout, re.M).group(1)
    for path in ("/var/lib/model-broker/key", "/var/lib/model-broker/token", f"/proc/{pid}/environ", f"/proc/{pid}/mem"):
        assert container.run("cat", path, user="runner", check=False).returncode != 0, path
    assert container.run("ls", "/var/lib/model-broker", user="runner", check=False).returncode != 0
    assert container.run("ls", f"/proc/{pid}/fd", user="runner", check=False).returncode != 0
    # Every file and /proc entry the runner user can read, swept for the key.
    # (The pattern travels on stdin, so no process of the sweep carries the
    # key in its own command line.)
    sweep = container.run("sh", "-c",
                          "needle=$(cat);"
                          " printf '%s\\n' \"$needle\" | grep -rIl --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev -f - / 2>/dev/null;"
                          " printf '%s\\n' \"$needle\" | grep -l -f - /proc/[0-9]*/environ /proc/[0-9]*/cmdline 2>/dev/null; true",
                          user="runner", stdin=REAL_KEY)
    assert sweep.stdout.strip() == "", f"the key is readable at: {sweep.stdout}"


def test_a_call_through_the_broker_reaches_the_pinned_upstream_with_the_real_key(started, container):
    container.write("/tmp/client.py", CLIENT_CALL)
    r = container.run("python3", "/tmp/client.py", "8317", started["outputs"]["token"], user="runner")
    assert r.stdout.splitlines()[0] == "200", r.stdout
    assert "data: 0" in r.stdout and "data: 2" in r.stdout
    seen = [json.loads(line) for line in container.run("cat", "/root/upstream/seen.jsonl").stdout.splitlines()]
    assert seen[-1]["path"] == "/v1/messages?beta=true" and seen[-1]["body"] == '{"hello": "broker"}'
    headers = {k.lower(): v for k, v in seen[-1]["headers"].items()}
    assert headers["x-api-key"] == REAL_KEY
    assert headers["host"] == "api.anthropic.com"
    assert started["outputs"]["token"] not in json.dumps(seen[-1]["headers"])
    # The request is in the broker's log by method and path only.
    log = container.run("cat", "/var/lib/model-broker/broker.log", user="runner").stdout
    assert "POST /v1/messages?beta=true -> 200" in log
    assert REAL_KEY not in log and started["outputs"]["token"] not in log


def test_a_wrong_token_and_a_foreign_operation_never_reach_the_upstream(started, container):
    before = container.run("wc", "-l", "/root/upstream/seen.jsonl").stdout.split()[0]
    r = container.run("python3", "/tmp/client.py", "8317", "broker-run-" + "0" * 48, user="runner")
    assert r.stdout.splitlines()[0] == "401"
    assert container.run("wc", "-l", "/root/upstream/seen.jsonl").stdout.split()[0] == before


def test_the_stop_file_ends_the_broker(started, container):
    container.run("touch", "/tmp/model-broker.stop", user="runner")
    for _ in range(100):
        if container.run("pgrep", "-u", "model-broker", "-x", "python3", check=False).returncode != 0:
            break
        time.sleep(0.1)
    assert container.run("pgrep", "-u", "model-broker", "-x", "python3", check=False).returncode != 0
    assert "stop file present; exiting" in container.run("cat", "/var/lib/model-broker/broker.log", user="runner").stdout
