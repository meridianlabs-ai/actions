"""Tests for the run: scripts of inspect-ai-scheduled-tests.yml and inspect-swe-nightly-tests.yml.

Both workflows take free-form workflow_dispatch inputs (a ref and extra
pytest arguments) in steps whose env: holds provider API keys, and the
scheduled workflow trusts a `last-inspect-ai-sha` artifact from an earlier
run. None of that text may be parsed as syntax: inputs reach bash through
env: and are validated or split into an array, the artifact is accepted only
from a scheduled run on the default branch and only when it is a 40-hex SHA,
and step outputs travel to later steps through env:, never through `${{ }}`
inside a script. The jobs that run the day's dependency closure and the test
suites hold nothing that reaches beyond the job: a token declared read-only,
no persisted checkout credential, no Actions cache; and the report job, the
only producer of the two artifacts later runs trust, refuses a name another
job took and records the identity of the triage-context it uploaded for the
triage consumer (tests/test_triage_workflow.py checks that consumer against
this producer). The scripts are lifted from the YAML and executed under bash
with stand-ins for gh and pytest on PATH.

Run with `python3 -m pytest` from the repo root (needs pytest, PyYAML and jq;
`.github/workflows/tests.yml` does the same in CI).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
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
"""Stand-in for gh api: canned run and artifact listings from $FAKE_GH_DIR and artifact zips by id."""
import json, os, pathlib, re, subprocess, sys

args = sys.argv[1:]
store = pathlib.Path(os.environ["FAKE_GH_DIR"])
with (store / "calls.log").open("a") as log:
    log.write(" ".join(args) + "\n")
if args[:1] != ["api"]:
    sys.exit(f"fake gh: unexpected call {args}")
jq = args[args.index("--jq") + 1] if "--jq" in args else None
path = [a for a in args[1:] if not a.startswith("-") and a != jq][0].split("?")[0]
if "/actions/workflows/inspect-ai-scheduled-tests.yml/runs" in path:
    data = (store / "runs.json").read_text()
elif m := re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/(\d+)/artifacts", path):
    data = json.dumps({"artifacts": json.loads((store / "artifacts.json").read_text()).get(m.group(1), [])})
elif m := re.fullmatch(r"repos/[^/]+/[^/]+/actions/artifacts/(\d+)/zip", path):
    zip_file = store / "zips" / f"{m.group(1)}.zip"
    zip_file.exists() or sys.exit(f"fake gh: HTTP 404 for {path}")
    sys.stdout.buffer.write(zip_file.read_bytes())
    sys.exit(0)
else:
    sys.exit(f"fake gh: unexpected call {args}")
if jq is not None:
    data = subprocess.run(["jq", "-r", jq], input=data, text=True, capture_output=True, check=True).stdout
sys.stdout.write(data)
'''

ARTIFACT = "last-inspect-ai-sha"
# Newest first, as the API lists them: a push run of a modified workflow on a
# branch, a manual run on main, then the genuine scheduled run on main.
RUNS = [(300, "push", "evil"), (200, "workflow_dispatch", "main"), (100, "schedule", "main")]


def sha_zip(content: bytes) -> bytes:
    """A last-inspect-ai-sha artifact as upload-artifact stores it: a zip holding last_sha.txt."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("last_sha.txt", content)
    return buf.getvalue()


def download(tmp_path: Path, *, runs=RUNS, artifacts: dict, content: bytes) -> tuple[dict, subprocess.CompletedProcess, list[str]]:
    """Run the download step. `artifacts` maps a run id to its artifacts: a name (the
    zip holds `content`, id = run id * 10 + position) or a dict with name, id, expired
    and content."""
    store = tmp_path / "gh"
    (store / "zips").mkdir(parents=True)
    (store / "runs.json").write_text(json.dumps({"workflow_runs": [{"id": i, "conclusion": "success", "event": e, "head_branch": b} for i, e, b in runs]}))
    listing: dict[str, list] = {}
    for run_id, entries in artifacts.items():
        for pos, entry in enumerate(entries):
            a = {"name": entry, "id": run_id * 10 + pos, "expired": False, "content": content} if isinstance(entry, str) else {"expired": False, "content": content, **entry}
            (store / "zips" / f"{a['id']}.zip").write_bytes(sha_zip(a.pop("content")))
            listing.setdefault(str(run_id), []).append(a)
    (store / "artifacts.json").write_text(json.dumps(listing))
    gh_out = tmp_path / "output.txt"
    gh_out.touch()
    s = step(SCHEDULED, "check-commit", "download_artifact")
    assert s["env"]["DEFAULT_BRANCH"] == "${{ github.event.repository.default_branch || 'main' }}"
    env = {"PATH": f"{bin_dir(tmp_path, gh=FAKE_GH)}:{os.environ['PATH']}", "FAKE_GH_DIR": str(store), "GITHUB_OUTPUT": str(gh_out), "DEFAULT_BRANCH": "main"}
    r = run_bash(render(s["run"], github={"repository": REPO}), cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = (store / "calls.log").read_text().splitlines()
    return parse_outputs(gh_out.read_text()), r, calls


def zip_downloads(calls: list[str]) -> list[str]:
    return [re.search(r"/actions/artifacts/(\d+)/zip", c).group(1) for c in calls if "/zip" in c]


def test_only_a_scheduled_run_on_the_default_branch_is_a_producer(tmp_path):
    outputs, r, calls = download(tmp_path, artifacts={300: [ARTIFACT], 200: [ARTIFACT], 100: [ARTIFACT]}, content=f"{SHA}\n".encode())
    assert outputs == {"last_sha": SHA}
    assert "Found run with artifact: 100 (artifact 1000)" in r.stdout
    assert zip_downloads(calls) == ["1000"]
    assert not any("/runs/300/" in c or "/runs/200/" in c for c in calls)


def test_a_push_run_artifact_alone_is_never_selected(tmp_path):
    outputs, r, calls = download(tmp_path, artifacts={300: [ARTIFACT]}, content=SHA.encode())
    assert outputs == {"last_sha": ""}
    assert "No previous successful scheduled runs with artifacts found" in r.stdout
    assert zip_downloads(calls) == []


def test_the_newest_artifact_of_the_name_is_taken_when_a_run_holds_several(tmp_path):
    # A successful re-run: attempt 1 failed after a test job squatted the
    # name (the report job refused it, so the attempt failed); attempt 2
    # passed and its report job uploaded the genuine one, necessarily later.
    # The run's artifact list holds both; the newest (highest id) is taken,
    # and an expired artifact is not a candidate however new.
    squat = {"name": ARTIFACT, "id": 5001, "content": b"f" * 40}
    genuine = {"name": ARTIFACT, "id": 5002, "content": f"{SHA}\n".encode()}
    expired = {"name": ARTIFACT, "id": 5003, "expired": True, "content": b"e" * 40}
    outputs, r, calls = download(tmp_path, artifacts={100: [squat, genuine, expired]}, content=b"unused")
    assert outputs == {"last_sha": SHA}
    assert "Found run with artifact: 100 (artifact 5002)" in r.stdout
    assert zip_downloads(calls) == ["5002"]
    assert "f" * 40 not in r.stdout


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


# --- The token, checkout and cache contract of the jobs that run third-party code ---
#
# slow-tests, static-analysis and nightly-tests install the day's dependency
# closure and run pytest or mypy over it, so whatever they hold reaches code
# this repo does not control. The contract: a job token declared read-only in
# the file (not left to the repository's default setting), no token persisted
# into a checkout, and no Actions cache restored or saved by such a job (a
# cache saved after that code ran would be installed by every later run).


def workflow_jobs(workflow: Path) -> dict:
    return yaml.safe_load(workflow.read_text())["jobs"]


def runs_third_party_code(job: dict) -> bool:
    return any("pip install" in (s.get("run") or "") for s in job["steps"])


@pytest.mark.parametrize("workflow", [SCHEDULED, NIGHTLY], ids=REF_IDS)
def test_the_job_token_is_declared_read_only_everywhere(workflow):
    wf = yaml.safe_load(workflow.read_text())
    assert wf["permissions"] == {"contents": "read"}
    for name, job in wf["jobs"].items():
        perms = job.get("permissions", {})
        assert set(perms) <= {"contents", "actions"}, (name, perms)
        assert set(perms.values()) <= {"read"}, (name, perms)


def test_only_the_jobs_that_read_this_repos_run_data_add_actions_read():
    jobs = workflow_jobs(SCHEDULED)
    # check-commit lists runs and downloads the skip-cache artifact; report
    # lists this run's artifacts before it uploads. The jobs that run
    # third-party code inherit the workflow-level contents: read and nothing else.
    assert jobs["check-commit"]["permissions"] == {"contents": "read", "actions": "read"}
    assert jobs["report"]["permissions"] == {"contents": "read", "actions": "read"}
    third_party = {name for name, job in jobs.items() if runs_third_party_code(job)}
    assert third_party == {"slow-tests", "static-analysis"}
    assert all("permissions" not in jobs[name] for name in third_party)
    nightly = workflow_jobs(NIGHTLY)
    assert {name for name, job in nightly.items() if runs_third_party_code(job)} == {"nightly-tests"}
    assert all("permissions" not in job for job in nightly.values())


@pytest.mark.parametrize("workflow", [SCHEDULED, NIGHTLY], ids=REF_IDS)
def test_no_checkout_persists_the_job_token(workflow):
    checkouts = [(name, s) for name, job in workflow_jobs(workflow).items() for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts, "expected at least one checkout"
    for name, s in checkouts:
        assert s["with"].get("persist-credentials") is False, (name, s.get("name"))


@pytest.mark.parametrize("workflow", [SCHEDULED, NIGHTLY], ids=REF_IDS)
def test_no_job_restores_or_saves_an_actions_cache(workflow):
    for name, job in workflow_jobs(workflow).items():
        for s in job["steps"]:
            uses = str(s.get("uses", ""))
            assert "cache" not in uses.lower(), (name, uses)
            if uses.startswith("actions/setup-python@"):
                assert "cache" not in s.get("with", {}) and "cache-dependency-path" not in s.get("with", {}), (name, s["with"])


# --- The report job's artifact names ------------------------------------------------
#
# The report job is the only intended producer of last-inspect-ai-sha and
# triage-context, but any job of the run can claim those names first, and the
# test jobs run third-party code before report starts. Names are unique per
# attempt, and a re-run may upload them again, so the step refuses a name
# taken since its attempt started (tampering) and notes, without refusing,
# one from a previous attempt.

FAKE_GH_RUN = r'''#!/usr/bin/env python3
"""Stand-in for gh api over one run: its artifact list and its run object from $FAKE_GH_DIR."""
import json, os, pathlib, re, subprocess, sys

args = sys.argv[1:]
store = pathlib.Path(os.environ["FAKE_GH_DIR"])
with (store / "calls.log").open("a") as log:
    log.write(" ".join(args) + "\n")
assert args[0] == "api", args
path = [a for a in args[1:] if not a.startswith("-") and a != (args[args.index("--jq") + 1] if "--jq" in args else None)][0].split("?")[0]
if (store / "fail").exists():
    sys.exit("fake gh: API failure")
if re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/\d+/artifacts", path):
    data = json.dumps({"artifacts": json.loads((store / "artifacts.json").read_text())})
elif re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/\d+", path):
    data = (store / "run.json").read_text()
else:
    sys.exit(f"fake gh: unexpected call {args}")
if "--jq" in args:
    data = subprocess.run(["jq", "-r", args[args.index("--jq") + 1]], input=data, text=True, capture_output=True, check=True).stdout
sys.stdout.write(data)
'''

STARTED = "2026-09-22T10:15:25Z"


def artifact(name: str, id: int, created: str, expired: bool = False) -> dict:
    return {"id": id, "name": name, "created_at": created, "expired": expired, "digest": "sha256:" + "0" * 64}


def check_names(tmp_path: Path, *, artifacts: list, tests_passed: bool, event: str = "schedule", fail_api: bool = False, no_started: bool = False) -> subprocess.CompletedProcess:
    store = Path(tempfile.mkdtemp(prefix="gh-", dir=tmp_path))  # one store per call; a test may call twice
    (store / "artifacts.json").write_text(json.dumps(artifacts))
    run = {"id": 35714973776, "run_attempt": 2, "run_started_at": STARTED}
    if no_started:
        del run["run_started_at"]
    (store / "run.json").write_text(json.dumps(run))
    if fail_api:
        (store / "fail").touch()
    s = step(SCHEDULED, "report", "names")
    assert s["name"] == "Refuse an artifact name another job of this attempt took"
    assert s["env"]["UPLOAD_TESTED_SHA"] == "${{ needs.slow-tests.result == 'success' && needs.static-analysis.result == 'success' && github.event_name == 'schedule' }}"
    assert s["env"]["UPLOAD_TRIAGE_CONTEXT"] == "${{ needs.slow-tests.result != 'success' || needs.static-analysis.result != 'success' }}"
    env = {
        "PATH": f"{bin_dir(tmp_path, gh=FAKE_GH_RUN)}:{os.environ['PATH']}",
        "FAKE_GH_DIR": str(store),
        "GH_TOKEN": "fake",
        "REPO": REPO,
        "RUN_ID": "35714973776",
        "RUN_ATTEMPT": "2",
        "UPLOAD_TESTED_SHA": str(tests_passed and event == "schedule").lower(),
        "UPLOAD_TRIAGE_CONTEXT": str(not tests_passed).lower(),
    }
    return run_bash(s["run"], cwd=tmp_path, env=env)


def test_report_uploads_when_no_job_took_its_artifact_name(tmp_path):
    r = check_names(tmp_path, artifacts=[artifact("something-else", 1, "2026-09-22T10:20:00Z")], tests_passed=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "No job of attempt 2 has taken the name(s) triage-context." in r.stdout


def test_report_refuses_a_name_a_job_of_this_attempt_took_and_calls_it_tampering(tmp_path):
    # A test job that ran third-party code created triage-context during this
    # attempt (after run_started_at) and before report ran.
    r = check_names(tmp_path, artifacts=[artifact("triage-context", 900, "2026-09-22T10:28:00Z")], tests_passed=False)
    assert r.returncode == 1
    assert "::error::An artifact named triage-context (id 900, created 2026-09-22T10:28:00Z) already exists in this run, created during attempt 2 (started 2026-09-22T10:15:25Z) before this job ran." in r.stdout
    assert "tampering" in r.stdout


@pytest.mark.parametrize("name, tests_passed", [("triage-context", False), ("last-inspect-ai-sha", True)], ids=["failing-rerun", "passing-rerun"])
def test_report_uploads_beside_a_previous_attempts_artifact_of_the_same_name(tmp_path, name, tests_passed):
    # Review round 1, B1: a re-run is a new attempt and may upload the name
    # again (names are unique per attempt, not per run). An artifact created
    # before this attempt started is noted, not refused: a second failing
    # attempt still publishes its own Slack-thread context, and re-running a
    # green scheduled run still records its tested SHA.
    r = check_names(tmp_path, artifacts=[artifact(name, 800, "2026-09-22T08:30:00Z")], tests_passed=tests_passed)
    assert r.returncode == 0, r.stdout
    assert f"An artifact named {name} (id 800, created 2026-09-22T08:30:00Z) exists from before attempt 2 started (2026-09-22T10:15:25Z): a previous attempt's." in r.stdout
    assert f"No job of attempt 2 has taken the name(s) {name}." in r.stdout
    assert "::error::" not in r.stdout and "tampering" not in r.stdout


def test_report_uploads_nothing_when_the_attempt_start_is_unknown(tmp_path):
    # Without the attempt's start a leftover cannot be told from a squat: fail closed.
    r = check_names(tmp_path, artifacts=[artifact("triage-context", 800, "2026-09-22T08:30:00Z")], tests_passed=False, no_started=True)
    assert r.returncode == 1
    assert "::error::Could not read when attempt 2 started" in r.stdout


def test_report_checks_only_the_name_it_is_about_to_upload(tmp_path):
    # A passing scheduled attempt uploads last-inspect-ai-sha; a triage-context
    # squatted in this very attempt is not its concern (that name is not uploaded).
    r = check_names(tmp_path, artifacts=[artifact("triage-context", 800, "2026-09-22T10:28:00Z")], tests_passed=True)
    assert r.returncode == 0, r.stdout
    assert "No job of attempt 2 has taken the name(s) last-inspect-ai-sha." in r.stdout
    # ... but a squatted last-inspect-ai-sha stops the upload the skip cache would trust.
    r = check_names(tmp_path, artifacts=[artifact("last-inspect-ai-sha", 901, "2026-09-22T10:28:00Z")], tests_passed=True)
    assert r.returncode == 1
    assert "An artifact named last-inspect-ai-sha (id 901" in r.stdout


def test_report_ignores_expired_artifacts_and_uploads_nothing_on_a_passing_push_run(tmp_path):
    r = check_names(tmp_path, artifacts=[artifact("triage-context", 700, "2026-09-22T10:28:00Z", expired=True)], tests_passed=False)
    assert r.returncode == 0, r.stdout
    r = check_names(tmp_path, artifacts=[], tests_passed=True, event="push")
    assert r.returncode == 0 and "This run uploads no artifact." in r.stdout


def test_report_uploads_nothing_when_the_artifact_list_cannot_be_read(tmp_path):
    r = check_names(tmp_path, artifacts=[], tests_passed=False, fail_api=True)
    assert r.returncode == 1
    assert "::error::Could not list this run's artifacts" in r.stdout


def test_the_uploads_wait_for_the_name_check_and_the_identity_is_recorded_from_the_upload_outputs():
    report = workflow_jobs(SCHEDULED)["report"]
    names = [s.get("name") for s in report["steps"]]
    assert names[0] == "Refuse an artifact name another job of this attempt took"
    for s in report["steps"]:
        if s.get("name") in ("Upload Slack thread info for triage", "Upload triage artifact"):
            assert "steps.names.outcome == 'success'" in s["if"], s["name"]
        if s.get("name") in ("Save tested inspect_ai SHA as artifact", "Upload tested SHA artifact"):
            # No always(): a failed name check skips them.
            assert "always()" not in s["if"], s["name"]
    upload = step(SCHEDULED, "report", "triage_context")
    assert upload["uses"].startswith("actions/upload-artifact@") and upload["with"]["name"] == "triage-context"
    assert "overwrite" not in upload["with"]
    record = step(SCHEDULED, "report", "Record the triage-context artifact identity")
    assert record["if"] == "always() && steps.triage_context.outcome == 'success'"
    assert record["env"] == {"ARTIFACT_ID": "${{ steps.triage_context.outputs.artifact-id }}", "ARTIFACT_DIGEST": "${{ steps.triage_context.outputs.artifact-digest }}"}


DIGEST = "63530825b48fcab4917b7b131b7922a24e326c849624ee64fb2ef9f2f9b30cd9"


def record_identity(tmp_path: Path, artifact_id: str, digest: str) -> subprocess.CompletedProcess:
    s = step(SCHEDULED, "report", "Record the triage-context artifact identity")
    return run_bash(s["run"], cwd=tmp_path, env={"ARTIFACT_ID": artifact_id, "ARTIFACT_DIGEST": digest})


def test_the_recorded_identity_is_one_fixed_shape_line(tmp_path):
    r = record_identity(tmp_path, "10689465503", DIGEST)
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"triage-context-artifact id=10689465503 digest=sha256:{DIGEST}\n"


@pytest.mark.parametrize("artifact_id, digest", [("", DIGEST), ("10689465503", ""), ("123; echo x", DIGEST), ("123", DIGEST.upper()), ("123", DIGEST[:-1])])
def test_a_malformed_upload_output_records_nothing_and_fails(tmp_path, artifact_id, digest):
    r = record_identity(tmp_path, artifact_id, digest)
    assert r.returncode == 1
    assert "triage-context-artifact id=" not in r.stdout
    assert "::error::upload-artifact returned no artifact id and digest" in r.stdout
