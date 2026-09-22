#!/usr/bin/env bash
# Runs AS THE AGENT USER (the isolated-agent action invokes it with
# `sudo -u <agent> env -i ... bash check_isolation.sh`), with exactly the
# identity and privileges the Claude agent will have a moment later. Every
# check is a negative probe: it reads no secret, and the broker's only
# upstream call is never made (a wrong token stops at 401, a foreign route at
# 403). Any failure fails the job before the agent runs — fail closed.
#
# What it proves, against review round 1's B1/B2:
#  - the agent is its own unprivileged user, not runner/model-broker/root,
#    and cannot sudo or reach the Docker daemon (so it cannot undo the egress
#    firewall or read another user's files as root);
#  - the model broker runs as model-broker and its key/token/dir and /proc
#    entries are closed to the agent;
#  - the GitHub runner process (Runner.Worker, .NET, which holds the job's
#    secrets in memory for masking) and every other non-agent process have
#    their memory and environment closed to the agent, and no .NET diagnostic
#    socket is connectable by the agent — the same-UID memory-dump channel
#    that Yama scope 1 alone does not close (B2);
#  - kernel.yama.ptrace_scope is 1 or stricter (defence in depth behind the
#    UID boundary);
#  - the runner command files handed to this check are not writable by the
#    agent, so it cannot rewrite a later trusted step's environment;
#  - the runner's own registration private key (.credentials_rsaparams in its
#    install directory, RUNNER_ROOT, found by the check step) is not readable
#    by the agent: setup grants the agent search-only access through the
#    runner's private home to reach the workspace, so that file's own mode
#    is what keeps it closed, and this makes that a checked fact rather than
#    an assumption.
set -uo pipefail

BROKER_HOME="${BROKER_HOME:-/var/lib/model-broker}"
status=0
fail() { echo "::error::agent isolation: $*"; status=1; }

me="$(id -un)"
case "$me" in
  root)         fail "the agent would run as root" ;;
  model-broker) fail "the agent shares the broker's user" ;;
esac
[ -n "${EXPECT_AGENT_USER:-}" ] && [ "$me" != "$EXPECT_AGENT_USER" ] && \
  fail "running as '$me', expected the agent user '$EXPECT_AGENT_USER'"

# No privilege escalation.
if sudo -n true 2>/dev/null; then fail "the agent user can sudo"; fi
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  fail "the agent can reach the Docker daemon"
fi

# Yama ptrace scope 1+ (defence in depth; the UID boundary below is the
# primary protection and holds even where Yama is absent).
scope=$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo missing)
case "$scope" in 1|2|3) ;; *) fail "kernel.yama.ptrace_scope is '$scope'; 1 or stricter is required" ;; esac

# The broker: running as model-broker, key/token/dir and /proc closed.
pid=""; port=""
if [ -r "$BROKER_HOME/ready" ]; then
  while IFS='=' read -r k v; do
    case "$k" in
      pid)  [[ "$v" =~ ^[0-9]+$ ]] && pid="$v" ;;
      port) [[ "$v" =~ ^[0-9]+$ ]] && port="$v" ;;
    esac
  done < "$BROKER_HOME/ready"
fi
if [ -z "$pid" ] || [ -z "$port" ]; then
  fail "$BROKER_HOME/ready is missing or malformed; the broker is not running"
  exit "$status"
fi
[ -d "/proc/$pid" ] || fail "the broker (pid $pid) is not running"
[ "$(stat -c %U "/proc/$pid" 2>/dev/null)" = "model-broker" ] || fail "the broker (pid $pid) is not running as model-broker"
for name in key token; do
  cat "$BROKER_HOME/$name" >/dev/null 2>&1 && fail "$BROKER_HOME/$name is readable by the agent"
done
ls "$BROKER_HOME" >/dev/null 2>&1 && fail "$BROKER_HOME is listable by the agent"
for name in environ mem; do
  cat "/proc/$pid/$name" >/dev/null 2>&1 && fail "/proc/$pid/$name (broker) is readable by the agent"
done

# The runner process and every other non-agent process: memory and
# environment closed. Requires at least one non-agent process to be present
# (in production the .NET Runner.Worker always is; the sentinel test supplies
# one) so the probe is never vacuously true.
non_agent=0
while read -r p owner; do
  [[ "$p" =~ ^[0-9]+$ ]] || continue
  [ "$owner" = "$me" ] && continue
  [ -d "/proc/$p" ] || continue
  non_agent=$((non_agent + 1))
  cat "/proc/$p/environ" >/dev/null 2>&1 && fail "/proc/$p/environ (user $owner) is readable by the agent"
  cat "/proc/$p/mem" >/dev/null 2>&1 && fail "/proc/$p/mem (user $owner) is readable by the agent"
done < <(ps -eo pid=,user= 2>/dev/null)
if [ "${REQUIRE_FOREIGN_PROCESS:-1}" = "1" ] && [ "$non_agent" = "0" ]; then
  fail "no process outside the agent's user was visible; cannot confirm the runner-process boundary"
fi

# .NET diagnostic sockets: none may be connectable by the agent. This is the
# channel B2 named — a same-UID client asks the runtime to dump its own
# memory, which Yama does not block; a different UID is refused at connect.
shopt -s nullglob
for sock in "${TMPDIR:-/tmp}"/dotnet-diagnostic-*-socket /tmp/dotnet-diagnostic-*-socket; do
  [ -S "$sock" ] || continue
  if /usr/bin/python3 - "$sock" <<'PY'
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
try:
    s.connect(sys.argv[1])
    sys.exit(0)   # connected -> reachable -> the boundary failed
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
  then fail "a .NET diagnostic socket ($sock) is connectable by the agent"; fi
done

# Runner command files must not be writable by the agent (it must not rewrite
# a later trusted step's environment). The agent's own run gets none of these
# in its environment (env -i), but they must also be unwritable on disk.
for f in ${RUNNER_COMMAND_FILES:-}; do
  [ -e "$f" ] || continue
  if ( : >>"$f" ) 2>/dev/null; then fail "runner command file $f is writable by the agent"; fi
done

# The runner's registration private key must stay closed. The agent has
# search-only access through the runner's home (setup granted it to reach the
# workspace), so a world-readable file there is reachable by name; this is
# the one file under that home that is a secret, and its own mode must deny
# the agent. Its sibling .credentials holds the OAuth client id and token
# URL, useless without the key, and is world-readable on hosted runners by
# the runner's own doing (runner 2.337.0, 2026-09-22), so it is not probed.
# A missing file reads as closed too (no runner root in the tests, or a
# runner that keeps it elsewhere).
if [ -n "${RUNNER_ROOT:-}" ]; then
  cat "$RUNNER_ROOT/.credentials_rsaparams" >/dev/null 2>&1 \
    && fail "$RUNNER_ROOT/.credentials_rsaparams (the runner's registration private key) is readable by the agent"
fi

# The broker answers on loopback; a wrong token and a foreign route are
# refused before any upstream call.
probe() {
  /usr/bin/python3 - "$port" "$1" "$2" "$3" <<'PY'
import http.client, sys
port, method, path, token = sys.argv[1:]
c = http.client.HTTPConnection("127.0.0.1", int(port), timeout=10)
c.request(method, path, body=b"{}", headers={"x-api-key": token, "Content-Type": "application/json"})
print(c.getresponse().status)
PY
}
expect() {
  local got
  got=$(probe "$1" "$2" "$3" 2>/dev/null || echo error)
  [ "$got" = "$4" ] || fail "$1 $2 answered $got, expected $4"
}
expect GET /healthz "" 200
expect POST /v1/messages wrong-token 401
expect GET /v1/models wrong-token 403

if [ "$status" = 0 ]; then
  echo "agent isolation holds: runs as '$me' (not runner/model-broker/root), no sudo or Docker, cannot read the broker or any other user's process or a .NET diagnostic socket, broker reachable on 127.0.0.1:$port"
fi
exit "$status"
