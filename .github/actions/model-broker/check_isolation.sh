#!/usr/bin/env bash
# Check, as the runner user and with exactly the privileges the agent will
# have, that the model broker's key is out of reach. Run after harden-runner
# (which disables sudo and containers) and before the agent step; any failed
# check fails the job before the agent starts. Everything here is a negative
# probe: nothing reads the key, and the broker's only network call is never
# made (a wrong token stops at 401, a disallowed operation at 403).
set -uo pipefail

home=/var/lib/model-broker
status=0
fail() {
  echo "::error::model broker isolation: $*"
  status=1
}

# Privilege escalation must be gone, or the runner user is root in all but name.
if sudo -n true 2>/dev/null; then
  fail "sudo still works for the runner user; harden-runner's disable-sudo-and-containers did not take effect"
fi
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  fail "the Docker daemon is still reachable; a privileged container could read the broker's files"
fi

# Yama ptrace scope 1 or stricter: a process may only trace its own
# descendants, so nothing the agent starts can read the broker's memory or the
# runner process's (which holds the job's secrets by GitHub's design).
scope=$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo missing)
case "$scope" in
  1|2|3) ;;
  *) fail "kernel.yama.ptrace_scope is '$scope'; 1 or stricter is required" ;;
esac

# The broker's ready file names its pid and port; both must be numbers.
pid=""; port=""
if [ -r "$home/ready" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      pid) [[ "$value" =~ ^[0-9]+$ ]] && pid="$value" ;;
      port) [[ "$value" =~ ^[0-9]+$ ]] && port="$value" ;;
    esac
  done < "$home/ready"
fi
if [ -z "$pid" ] || [ -z "$port" ]; then
  fail "$home/ready is missing or malformed; the broker is not running"
  exit "$status"
fi
if ! [ -d "/proc/$pid" ]; then
  fail "the broker (pid $pid) is not running"
  exit "$status"
fi
if [ "$(stat -c %U "/proc/$pid" 2>/dev/null)" != "model-broker" ]; then
  fail "the broker (pid $pid) is not running as model-broker"
fi

# The key file is deleted at start-up; the token file and the directory
# listing must be unreadable to this user either way.
for name in key token; do
  if cat "$home/$name" >/dev/null 2>&1; then
    fail "$home/$name is readable by the runner user"
  fi
done
if ls "$home" >/dev/null 2>&1; then
  fail "$home is listable by the runner user"
fi

# The process boundary: environment, memory and open files of another
# user's process are closed to this one.
for name in environ mem; do
  if cat "/proc/$pid/$name" >/dev/null 2>&1; then
    fail "/proc/$pid/$name is readable by the runner user"
  fi
done
if ls "/proc/$pid/fd" >/dev/null 2>&1; then
  fail "/proc/$pid/fd is listable by the runner user"
fi
# cmdline is world-readable, so it must carry only the interpreter, the
# script and flags whose values are paths and numbers, never key material.
cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline")
if ! [[ "$cmdline" =~ ^/usr/bin/python3\ /var/lib/model-broker/model_broker\.py\ (--(port|lifetime-minutes)\ [0-9]+\ |--(key-file|token-file|ready-file|stop-file)\ /[A-Za-z0-9._/-]+\ )+$ ]]; then
  fail "the broker's command line is not the expected one: $cmdline"
fi

# The broker answers on loopback, refuses an unknown token before any
# upstream call, and refuses an operation outside the Messages API.
probe() {
  /usr/bin/python3 - "$port" "$1" "$2" "$3" <<'PY'
import http.client, sys
port, method, path, token = sys.argv[1:]
conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=10)
conn.request(method, path, body=b"{}", headers={"x-api-key": token, "Content-Type": "application/json"})
print(conn.getresponse().status)
PY
}
expect() {
  local got
  got=$(probe "$1" "$2" "$3" 2>/dev/null || echo error)
  if [ "$got" != "$4" ]; then
    fail "$1 $2 answered $got, expected $4"
  fi
}
expect GET /healthz "" 200
expect POST /v1/messages wrong-token 401
expect GET /v1/models wrong-token 403
expect POST /v1/complete wrong-token 403

if [ "$status" = 0 ]; then
  echo "model broker isolation holds: pid $pid runs as model-broker on 127.0.0.1:$port; sudo, containers, its files and its /proc entries are closed to the runner user"
fi
exit "$status"
