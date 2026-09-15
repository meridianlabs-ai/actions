"""Tests for the run: scripts of inspect-ai-scheduled-tests.yml and inspect-swe-nightly-tests.yml.

Both workflows take free-form workflow_dispatch inputs (a ref and extra
pytest arguments) in steps whose env: holds provider API keys, and the
scheduled workflow trusts a `last-inspect-ai-sha` artifact from an earlier
run. None of that text may be parsed as syntax: inputs reach bash through
env: and are validated or split into an array, the artifact is accepted only
from a scheduled run on the default branch and only when it is a 40-hex SHA,
and step outputs travel to later steps through env:, never through `${{ }}`
inside a script. The scripts are lifted from the YAML and executed under
bash with stand-ins for gh and pytest on PATH.

Run with `python3 -m pytest` from the repo root (needs pytest, PyYAML and jq;
`.github/workflows/tests.yml` does the same in CI).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEDULED = ROOT / ".github" / "workflows" / "inspect-ai-scheduled-tests.yml"
NIGHTLY = ROOT / ".github" / "workflows" / "inspect-swe-nightly-tests.yml"
SHA = "0123456789abcdef0123456789abcdef01234567"
REPO = "meridianlabs-ai/actions"


def steps(workflow: Path, job: str) -> list[dict]:
    return yaml.safe_load(workflow.read_text())["jobs"][job]["steps"]


def step(workflow: Path, job: str, name: str) -> dict:
    """The step whose id is `name` or whose name starts with it."""
    for s in steps(workflow, job):
        if s.get("id") == name or str(s.get("name", "")).startswith(name):
            return s
    raise KeyError(name)


def render(script: str, **contexts: dict) -> str:
    """Substitute the `${{ matrix.* }}` / `${{ github.* }}` literals the workflow file itself supplies."""
    out = re.sub(r"\$\{\{\s*(matrix|github)\.(\w+)\s*\}\}", lambda m: str(contexts[m.group(1)][m.group(2)]), script)
    assert "${{" not in out, out
    return out


def run_bash(script: str, *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    # GitHub runs `run:` scripts with `bash --noprofile --norc -eo pipefail`.
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=cwd, env={**os.environ, **env}, text=True, capture_output=True, check=False)


def parse_outputs(text: str) -> dict:
    """Parse $GITHUB_OUTPUT written as `key=value` lines; any other shape is a forged output."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.fullmatch(r"([a-z_]+)=(.*)", line)
        assert m, f"unexpected output line {line!r}"
        assert m.group(1) not in out, f"output {m.group(1)} written twice"
        out[m.group(1)] = m.group(2)
    return out


def bin_dir(tmp_path: Path, **scripts: str) -> Path:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for name, body in scripts.items():
        (d / name).write_text(body)
        (d / name).chmod(0o755)
    return d


# --- No expression reaches a shell ------------------------------------------------


@pytest.mark.parametrize("workflow", [SCHEDULED, NIGHTLY], ids=["scheduled", "nightly"])
def test_no_input_output_or_event_expression_inside_a_run_script(workflow):
    for job_name, job in yaml.safe_load(workflow.read_text())["jobs"].items():
        for s in job["steps"]:
            hit = re.search(r"\$\{\{\s*(inputs|steps|needs|secrets|github\.event)\b", s.get("run") or "")
            assert hit is None, f"{workflow.name} {job_name} / {s.get('name')}: {hit.group(0)} inside run:"


@pytest.mark.parametrize(
    "workflow, job, validate, checkout",
    [(SCHEDULED, "check-commit", "Validate inspect_ai_ref input", "Checkout inspect_ai repository"), (NIGHTLY, "nightly-tests", "Validate inspect_swe_ref input", "Checkout inspect_swe repository")],
    ids=["scheduled", "nightly"],
)
def test_the_ref_is_validated_before_it_is_checked_out(workflow, job, validate, checkout):
    names = [s.get("name") for s in steps(workflow, job)]
    assert names.index(validate) < names.index(checkout)


# --- Ref inputs --------------------------------------------------------------------

REF_STEPS = [(SCHEDULED, "check-commit", "Validate inspect_ai_ref input", "INSPECT_AI_REF"), (NIGHTLY, "nightly-tests", "Validate inspect_swe_ref input", "INSPECT_SWE_REF")]
REF_IDS = ["scheduled", "nightly"]


@pytest.mark.parametrize("workflow, job, name, var", REF_STEPS, ids=REF_IDS)
@pytest.mark.parametrize("ref", ["", "main", "v0.3.100", "feature/some-thing", "0123abcd", "release-1.0_rc2"])
def test_ref_validation_accepts_refs(workflow, job, name, var, ref, tmp_path):
    s = step(workflow, job, name)
    assert s["env"] == {var: f"${{{{ inputs.{var.lower()} }}}}"}
    r = run_bash(s["run"], cwd=tmp_path, env={var: ref})
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("workflow, job, name, var", REF_STEPS, ids=REF_IDS)
@pytest.mark.parametrize("ref", ["-x", "--upload-pack=touch /tmp/x", "a..b", "main x", "main;id", "main\nsha=forged", "$(id)", "`id`", "main|x", "tag~1", "über"])
def test_ref_validation_rejects_anything_but_ref_characters(workflow, job, name, var, ref, tmp_path):
    r = run_bash(step(workflow, job, name)["run"], cwd=tmp_path, env={var: ref})
    assert r.returncode == 1
    assert f"::error::{var.lower()} must be a branch, tag or commit" in r.stdout
    assert ref not in r.stdout


def git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], cwd=tmp_path, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True, capture_output=True, check=True).stdout.strip()


@pytest.mark.parametrize(
    "workflow, job, step_id, var",
    [(SCHEDULED, "check-commit", "inspect_commit", "INSPECT_AI_REF"), (NIGHTLY, "nightly-tests", "swe_commit", "INSPECT_SWE_REF")],
    ids=REF_IDS,
)
@pytest.mark.parametrize("ref, expected", [("", "main"), ("feature/x", "feature/x")])
def test_commit_info_takes_the_ref_from_env_and_defaults_to_main(workflow, job, step_id, var, ref, expected, tmp_path):
    head = git_repo(tmp_path)
    gh_out = tmp_path / "output.txt"
    gh_out.touch()
    r = run_bash(step(workflow, job, step_id)["run"], cwd=tmp_path, env={var: ref, "GITHUB_OUTPUT": str(gh_out)})
    assert r.returncode == 0, r.stderr
    assert parse_outputs(gh_out.read_text()) == {"sha": head, "short_sha": head[:7], "ref": expected}


# --- pytest_args --------------------------------------------------------------------

FAKE_PYTEST = """#!/usr/bin/env bash
# Record the argument vector pytest would have received, one per line.
printf '%s\\n' "$@" > "$FAKE_PYTEST_ARGV"
"""

PYTEST_STEPS = [(SCHEDULED, "slow-tests", "Run slow tests", "--timeout-method=thread"), (NIGHTLY, "nightly-tests", "Run tests (fast + slow)", "log_level=warning")]
MATRIX = {"mode": "asyncio", "pytest_flags": "", "test_timeout": 900}


def run_pytest_step(tmp_path: Path, workflow: Path, job: str, name: str, pytest_args: str) -> tuple[subprocess.CompletedProcess, list[str] | None]:
    s = step(workflow, job, name)
    assert s["env"]["PYTEST_ARGS"] == "${{ inputs.pytest_args }}"
    argv = tmp_path / "argv"
    env = {"PATH": f"{bin_dir(tmp_path, pytest=FAKE_PYTEST)}:{os.environ['PATH']}", "FAKE_PYTEST_ARGV": str(argv), "PYTEST_ARGS": pytest_args}
    r = run_bash(render(s["run"], matrix=MATRIX), cwd=tmp_path, env=env)
    return r, argv.read_text().splitlines() if argv.exists() else None


@pytest.mark.parametrize("workflow, job, name, last_fixed", PYTEST_STEPS, ids=REF_IDS)
def test_pytest_args_are_split_on_spaces_and_appended(workflow, job, name, last_fixed, tmp_path):
    r, argv = run_pytest_step(tmp_path, workflow, job, name, "")
    assert r.returncode == 0, r.stdout + r.stderr
    assert argv is not None and argv[-1] == last_fixed

    r, argv = run_pytest_step(tmp_path, workflow, job, name, "  -k not_slow   --maxfail=1 tests/test_x.py::test_y ")
    assert r.returncode == 0, r.stdout + r.stderr
    assert argv is not None and argv[-5:] == [last_fixed, "-k", "not_slow", "--maxfail=1", "tests/test_x.py::test_y"]


@pytest.mark.parametrize("workflow, job, name, last_fixed", PYTEST_STEPS, ids=REF_IDS)
@pytest.mark.parametrize("pytest_args", ["; echo INJECTED", "$(echo INJECTED)", "`echo INJECTED`", "-k 'not slow'", '-k "not slow"', "--maxfail=1\necho INJECTED", "a && echo INJECTED", "-k not_slow > /dev/null", "*"])
def test_pytest_args_outside_the_allowlist_fail_before_pytest_runs(workflow, job, name, last_fixed, pytest_args, tmp_path):
    r, argv = run_pytest_step(tmp_path, workflow, job, name, pytest_args)
    assert r.returncode == 1
    assert "::error::pytest_args may only contain" in r.stdout
    assert "INJECTED" not in r.stdout + r.stderr
    assert argv is None


# --- The last-inspect-ai-sha artifact ---------------------------------------------------

FAKE_GH = r'''#!/usr/bin/env python3
"""Stand-in for gh: canned run and artifact listings from $FAKE_GH_DIR, and a recorded download."""
import json, os, pathlib, re, subprocess, sys

args = sys.argv[1:]
store = pathlib.Path(os.environ["FAKE_GH_DIR"])
with (store / "calls.log").open("a") as log:
    log.write(" ".join(args) + "\n")
if args[:1] == ["api"]:
    path = args[1]
    if "/actions/workflows/inspect-ai-scheduled-tests.yml/runs" in path:
        data = (store / "runs.json").read_text()
    else:
        run_id = re.search(r"/actions/runs/(\d+)/artifacts$", path).group(1)
        names = json.loads((store / "artifacts.json").read_text()).get(run_id, [])
        data = json.dumps({"artifacts": [{"name": n} for n in names]})
    if "--jq" in args:
        data = subprocess.run(["jq", "-r", args[args.index("--jq") + 1]], input=data, text=True, capture_output=True, check=True).stdout
    sys.stdout.write(data)
elif args[:2] == ["run", "download"]:
    dest = pathlib.Path(args[args.index("--dir") + 1])
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "last_sha.txt").write_bytes((store / "last_sha.txt").read_bytes())
else:
    sys.exit(f"fake gh: unexpected call {args}")
'''

ARTIFACT = "last-inspect-ai-sha"
# Newest first, as the API lists them: a push run of a modified workflow on a
# branch, a manual run on main, then the genuine scheduled run on main.
RUNS = [(300, "push", "evil"), (200, "workflow_dispatch", "main"), (100, "schedule", "main")]


def download(tmp_path: Path, *, runs=RUNS, artifacts: dict, content: bytes) -> tuple[dict, subprocess.CompletedProcess, list[str]]:
    store = tmp_path / "gh"
    store.mkdir()
    (store / "runs.json").write_text(json.dumps({"workflow_runs": [{"id": i, "conclusion": "success", "event": e, "head_branch": b} for i, e, b in runs]}))
    (store / "artifacts.json").write_text(json.dumps({str(k): v for k, v in artifacts.items()}))
    (store / "last_sha.txt").write_bytes(content)
    gh_out = tmp_path / "output.txt"
    gh_out.touch()
    s = step(SCHEDULED, "check-commit", "download_artifact")
    assert s["env"]["DEFAULT_BRANCH"] == "${{ github.event.repository.default_branch || 'main' }}"
    env = {"PATH": f"{bin_dir(tmp_path, gh=FAKE_GH)}:{os.environ['PATH']}", "FAKE_GH_DIR": str(store), "GITHUB_OUTPUT": str(gh_out), "DEFAULT_BRANCH": "main"}
    r = run_bash(render(s["run"], github={"repository": REPO}), cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = (store / "calls.log").read_text().splitlines()
    return parse_outputs(gh_out.read_text()), r, calls


def test_only_a_scheduled_run_on_the_default_branch_is_a_producer(tmp_path):
    outputs, r, calls = download(tmp_path, artifacts={300: [ARTIFACT], 200: [ARTIFACT], 100: [ARTIFACT]}, content=f"{SHA}\n".encode())
    assert outputs == {"last_sha": SHA}
    assert "Found run with artifact: 100" in r.stdout
    assert [c for c in calls if c.startswith("run download")] == [f"run download 100 --name {ARTIFACT} --dir .last-inspect-ai-sha --repo {REPO}"]
    assert not any("/runs/300/" in c or "/runs/200/" in c for c in calls)


def test_a_push_run_artifact_alone_is_never_selected(tmp_path):
    outputs, r, calls = download(tmp_path, artifacts={300: [ARTIFACT]}, content=SHA.encode())
    assert outputs == {"last_sha": ""}
    assert "No previous successful scheduled runs with artifacts found" in r.stdout
    assert not any(c.startswith("run download") for c in calls)


@pytest.mark.parametrize(
    "content",
    [b'x"; echo INJECTED #', f"{SHA}\nforged=INJECTED\n".encode(), SHA.upper().encode(), SHA[:-1].encode(), b"", b"$(echo INJECTED)"],
    ids=["quote-break", "extra-output-line", "uppercase", "short", "empty", "subshell"],
)
def test_artifact_content_that_is_not_a_sha_is_dropped_unechoed(tmp_path, content):
    outputs, r, _ = download(tmp_path, artifacts={100: [ARTIFACT]}, content=content)
    assert outputs == {"last_sha": ""}
    assert "Ignoring artifact from run 100" in r.stdout
    assert "INJECTED" not in r.stdout + r.stderr
    if content.strip():
        assert content.decode().strip() not in r.stdout


# --- Consumers of the outputs ---------------------------------------------------------


@pytest.mark.parametrize("last, skip", [(SHA, "true"), ("", "false"), ("f" * 40, "false")])
def test_skip_decision_reads_both_shas_from_env(tmp_path, last, skip):
    s = step(SCHEDULED, "check-commit", "check_commit_artifact")
    assert s["env"] == {"CURRENT_SHA": "${{ steps.inspect_commit.outputs.sha }}", "LAST_SHA": "${{ steps.download_artifact.outputs.last_sha }}"}
    gh_out = tmp_path / "output.txt"
    gh_out.touch()
    r = run_bash(s["run"], cwd=tmp_path, env={"CURRENT_SHA": SHA, "LAST_SHA": last, "GITHUB_OUTPUT": str(gh_out)})
    assert r.returncode == 0, r.stderr
    assert parse_outputs(gh_out.read_text()) == {"skip_tests": skip}


def test_triage_context_is_built_by_jq_from_env(tmp_path):
    s = step(SCHEDULED, "report", "Upload Slack thread info for triage")
    assert set(s["env"]) == {"CHANNEL", "THREAD_TS", "SHA", "REF"}
    values = {"CHANNEL": 'C01"}\n{"x":"y', "THREAD_TS": "1726000000.123456", "SHA": SHA, "REF": "feature/x"}
    r = run_bash(s["run"], cwd=tmp_path, env=values)
    assert r.returncode == 0, r.stderr
    assert json.loads((tmp_path / ".triage" / "slack_thread.json").read_text()) == {
        "channel": values["CHANNEL"],
        "thread_ts": values["THREAD_TS"],
        "inspect_ai_sha": SHA,
        "inspect_ai_ref": "feature/x",
    }
