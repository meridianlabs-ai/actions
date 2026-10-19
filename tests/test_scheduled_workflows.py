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
no persisted checkout credential, no Actions cache and a token that cannot
save one (`cache-mode: read`); and the report job, the
only producer of the two artifacts later runs trust, refuses a name another
job took and records the identity of the triage-context it uploaded for the
triage consumer (tests/test_triage_workflow.py checks that consumer against
this producer). A failed or cancelled nightly uploads its Inspect logs only
after a check that withholds any log holding a provider key. The scripts are
lifted from the YAML and executed under bash with stand-ins for gh and pytest
on PATH.

Run with `python3 -m pytest` from the repo root (needs pytest, PyYAML, jq and
inspect_ai;
`.github/workflows/tests.yml` does the same in CI).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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


# --- inspect_sentinel install (TEMPORARY) -------------------------------------------

SENTINEL_STEP = "Install inspect_sentinel (TEMPORARY)"
SENTINEL_SPEC = f"inspect_sentinel @ git+https://github.com/meridianlabs-ai/inspect_sentinel@{SHA}"
FAKE_PIP = """#!/usr/bin/env bash
# Record each pip call; `pip show` succeeds only when FAKE_PIP_SHOW=1.
printf '%s\\n' "$*" >> "$FAKE_PIP_CALLS"
if [ "$1" = show ]; then [ "${FAKE_PIP_SHOW:-0}" = 1 ]; fi
"""


DECOY_SPEC = SENTINEL_SPEC.replace(SHA, "d" * 40)


def build_yml(*specs: str, temporary: str = """      - name: Install inspect_sentinel (TEMPORARY)
        if: env.RUN_TESTS == 'true'
        run: uv pip install --no-deps "{spec}"
""") -> str:
    """An upstream build.yml with one TEMPORARY install step per spec (two jobs pin it today), between steps that pin a decoy."""
    out = "jobs:\n"
    for i, spec in enumerate(specs):
        out += f"""  job{i}:
    steps:
      - name: Install dev deps
        run: uv pip install "{DECOY_SPEC}"
{temporary.format(spec=spec)}      - name: Run tests
        run: echo "{DECOY_SPEC}"
"""
    return out


def run_sentinel_step(tmp_path: Path, build: str | None, *, installed: bool = False, sentinel_tree: bool = True) -> tuple[subprocess.CompletedProcess, list[str]]:
    if sentinel_tree:
        (tmp_path / "src" / "inspect_ai" / "_sentinel").mkdir(parents=True)
    if build is not None:
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / ".github" / "workflows" / "build.yml").write_text(build)
    calls = tmp_path / "pip_calls"
    env = {"PATH": f"{bin_dir(tmp_path, pip=FAKE_PIP)}:{os.environ['PATH']}", "FAKE_PIP_CALLS": str(calls), "FAKE_PIP_SHOW": "1" if installed else "0"}
    r = run_bash(step(SCHEDULED, "slow-tests", SENTINEL_STEP)["run"], cwd=tmp_path, env=env)
    return r, calls.read_text().splitlines() if calls.exists() else []


def test_both_jobs_install_inspect_sentinel_with_the_same_script_between_install_and_use():
    for job, user in [("slow-tests", "Run slow tests"), ("static-analysis", "Run mypy")]:
        names = [s.get("name", "") for s in steps(SCHEDULED, job)]
        sentinel = names.index(SENTINEL_STEP)
        assert names.index("Install dependencies") < sentinel < next(i for i, n in enumerate(names) if n.startswith(user)), job
    assert step(SCHEDULED, "slow-tests", SENTINEL_STEP)["run"] == step(SCHEDULED, "static-analysis", SENTINEL_STEP)["run"]


@pytest.mark.parametrize(
    "temporary",
    [
        """      - name: Install inspect_sentinel (TEMPORARY)
        if: env.RUN_TESTS == 'true'
        run: uv pip install --no-deps "{spec}"
""",
        """      - name: Install inspect_sentinel (TEMPORARY)
        run: |
          uv pip install --no-deps \\
            "{spec}"
""",
    ],
    ids=["one-line", "block"],
)
@pytest.mark.parametrize("installed", [False, True], ids=["absent", "already-installed"])
def test_sentinel_pin_is_read_from_the_temporary_step_only_and_installed_without_deps(temporary, installed, tmp_path):
    r, calls = run_sentinel_step(tmp_path, build_yml(SENTINEL_SPEC, SENTINEL_SPEC, temporary=temporary), installed=installed)
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls == [f"install --no-deps {SENTINEL_SPEC}"]


@pytest.mark.parametrize("installed", [False, True], ids=["absent", "already-installed"])
def test_a_main_pin_is_installed(installed, tmp_path):
    spec = SENTINEL_SPEC.replace(SHA, "main")
    r, calls = run_sentinel_step(tmp_path, build_yml(spec, spec), installed=installed)
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls == [f"install --no-deps {spec}"]


def test_no_pin_is_a_notice_when_the_dev_install_already_provides_inspect_sentinel(tmp_path):
    r, calls = run_sentinel_step(tmp_path, "jobs: {}\n", installed=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "::notice::No inspect_sentinel pin" in r.stdout and "remove this one" in r.stdout
    assert calls == ["show inspect_sentinel"]


def test_no_pin_is_a_notice_for_a_commit_that_predates_inspect_sentinel(tmp_path):
    r, calls = run_sentinel_step(tmp_path, "jobs: {}\n", sentinel_tree=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "::notice::This inspect_ai commit predates inspect_sentinel" in r.stdout
    assert calls == []


@pytest.mark.parametrize(
    "build",
    [
        None,
        "jobs: {}\n",
        build_yml(SENTINEL_SPEC, SENTINEL_SPEC.replace(SHA, "f" * 40)),
        build_yml(SENTINEL_SPEC.replace("meridianlabs-ai/", "someone-else/")),
        build_yml(SENTINEL_SPEC.replace(SHA, "develop")),
        build_yml(SENTINEL_SPEC.replace(SHA, "mainline")),
        build_yml(SENTINEL_SPEC.replace(SHA, "main/x")),
        build_yml(SENTINEL_SPEC + "ff"),
        build_yml(SENTINEL_SPEC + "#subdirectory=x"),
        build_yml("my_" + SENTINEL_SPEC),
        build_yml(SENTINEL_SPEC, temporary="""      # - name: Install inspect_sentinel (TEMPORARY)
      #   run: uv pip install --no-deps "{spec}"
"""),
        build_yml(SENTINEL_SPEC, temporary="""      - name: Install inspect_sentinel (TEMPORARY)
        uses: some/action@v1
"""),
    ],
    ids=["no-build-yml", "no-pin", "two-pins", "other-repo", "branch-not-sha-or-main", "longer-than-main", "under-main", "longer-than-sha", "trailing-fragment", "other-name", "step-commented-out", "step-without-run"],
)
def test_a_missing_ambiguous_or_unexpected_sentinel_pin_fails_before_installing(build, tmp_path):
    r, calls = run_sentinel_step(tmp_path, build)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "::error::Expected one inspect_sentinel git pin" in r.stdout
    other = SENTINEL_SPEC.replace(SHA, "f" * 40)
    found = f"{SENTINEL_SPEC}; {other}" if build is not None and other in build else "none"
    assert f"found: {found} (" in r.stdout, r.stdout
    assert not any(c.startswith("install") for c in calls)


def test_only_the_matched_sentinel_spec_reaches_pip(tmp_path):
    r, calls = run_sentinel_step(tmp_path, build_yml(SENTINEL_SPEC.replace(SHA, SHA + '"; echo INJECTED; "')))
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls == [f"install --no-deps {SENTINEL_SPEC}"]
    assert "INJECTED" not in r.stdout + r.stderr


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
# cache saved after that code ran would be installed by every later run). The
# step checks below cover what the file asks for; `cache-mode: read` makes the
# token itself refuse a save, which is what stops the closure from saving one
# through the runtime token Runner.Worker holds.


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


@pytest.mark.parametrize("workflow", [SCHEDULED, NIGHTLY], ids=REF_IDS)
def test_every_job_token_can_restore_caches_but_not_save_them(workflow):
    # Claude Security findings 4773471 (scheduled) and 4773276 (nightly).
    # Workflow level covers every job; a job-level key would override it, so
    # the file carries exactly one, at column 0.
    assert yaml.safe_load(workflow.read_text())["cache-mode"] == "read"
    keys = [l for l in workflow.read_text().splitlines() if re.match(r"\s*cache-mode\s*:", l)]
    assert keys == ["cache-mode: read"], keys


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


# --- The nightly's .eval logs ---------------------------------------------------------
#
# A failed or cancelled nightly uploads the Inspect logs its tests wrote. The
# logs record model inputs and outputs, not the provider keys, but the step
# before the upload checks every log for the literal key values and copies
# only the regular files that pass into the directory the upload takes, so
# the uploaded set is exactly the checked set. It names no file, and any error
# fails it with a fixed message.

ANTHROPIC = "sk-ant-api03-" + "a" * 40
GOOGLE = "AIza" + "g" * 35
OPENAI = "sk-proj-" + "o" * 40
LOG_DIR_EXPR = "${{ runner.temp }}/inspect-logs"
CHECKED_DIR_EXPR = "${{ runner.temp }}/inspect-logs-checked"
FAILED = "::error::The log check did not complete, so no logs will be uploaded.\n"


def zstd_zipfile():
    """zipfile as Inspect patches it: zstd, the compression inspect_ai writes
    .eval members with, and its multi-frame zstd members. The step reads the
    logs through the same module, so the log checks need inspect_ai."""
    import zipfile

    pytest.importorskip("inspect_ai._util.zipfile", reason="the log checks need inspect_ai: pip install inspect_ai")
    return zipfile


def members(path: Path) -> dict[str, bytes]:
    zipfile = zstd_zipfile()
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name) for name in z.namelist()}


def write_eval(path: Path, members: dict[str, str]) -> Path:
    zipfile = zstd_zipfile()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_ZSTANDARD) as z:
        for name, text in members.items():
            z.writestr(name, text)
    return path


def scan_logs(tmp_path: Path, log_dir: Path, **keys: str) -> subprocess.CompletedProcess:
    zstd_zipfile()
    s = step(NIGHTLY, "nightly-tests", "log_scan")
    # The step runs the job's `python`; here that is the interpreter running pytest.
    path = f"{Path(sys.executable).parent}:{os.environ['PATH']}"
    env = {"PATH": path, "INSPECT_LOG_DIR": str(log_dir), "CHECKED_LOG_DIR": str(tmp_path / "checked"), "ANTHROPIC_API_KEY": "", "GOOGLE_API_KEY": "", "OPENAI_API_KEY": "", **keys}
    # run_bash, with a timeout: a scan that opens a fifo blocks forever.
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", s["run"]], cwd=tmp_path, env={**os.environ, **env}, text=True, capture_output=True, check=False, timeout=60)


def uploaded(tmp_path: Path) -> list[str]:
    """What the upload would take: every path under the checked directory, links included."""
    root = tmp_path / "checked"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() or p.is_symlink())


ALL_KEYS = {"ANTHROPIC_API_KEY": ANTHROPIC, "GOOGLE_API_KEY": GOOGLE, "OPENAI_API_KEY": OPENAI}


def assert_no_key_printed(r: subprocess.CompletedProcess) -> None:
    out = r.stdout + r.stderr
    for key in ALL_KEYS.values():
        assert key not in out and key[-20:] not in out
    assert "Traceback" not in out


def test_the_tests_scan_and_upload_share_one_log_directory():
    run_tests = step(NIGHTLY, "nightly-tests", "Run tests (fast + slow)")
    scan = step(NIGHTLY, "nightly-tests", "log_scan")
    upload = step(NIGHTLY, "nightly-tests", "eval_logs")
    assert run_tests["env"]["INSPECT_LOG_DIR"] == LOG_DIR_EXPR
    assert scan["env"] == {
        "ANTHROPIC_API_KEY": "${{ secrets.ANTHROPIC_API_KEY }}",
        "GOOGLE_API_KEY": "${{ secrets.GOOGLE_API_KEY }}",
        "OPENAI_API_KEY": "${{ secrets.OPENAI_API_KEY }}",
        "INSPECT_LOG_DIR": LOG_DIR_EXPR,
        "CHECKED_LOG_DIR": CHECKED_DIR_EXPR,
    }
    # The scan covers every key the tests held.
    assert {k for k in run_tests["env"] if k.endswith("_API_KEY")} == {k for k in scan["env"] if k.endswith("_API_KEY")}
    # The upload takes the checked copies, never the log directory itself.
    assert upload["with"]["path"] == CHECKED_DIR_EXPR + "/"


def test_the_logs_upload_only_after_a_failure_and_a_clean_scan():
    names = [s.get("id") or s.get("name") for s in steps(NIGHTLY, "nightly-tests")]
    assert names.index("log_scan") == names.index("Run tests (fast + slow)") + 1
    assert names.index("eval_logs") == names.index("log_scan") + 1
    assert step(NIGHTLY, "nightly-tests", "log_scan")["if"] == "failure() || cancelled()"
    upload = step(NIGHTLY, "nightly-tests", "eval_logs")
    assert upload["if"] == "(failure() || cancelled()) && steps.log_scan.outcome == 'success'"
    assert re.fullmatch(r"actions/upload-artifact@[0-9a-f]{40}", upload["uses"])
    assert upload["with"] == {
        "name": "inspect-swe-eval-logs-${{ github.run_id }}-${{ github.run_attempt }}",
        "path": CHECKED_DIR_EXPR + "/",
        "retention-days": 14,
        "if-no-files-found": "warn",
    }


def test_the_slack_report_only_compares_the_upload_flag():
    # The flag comes from the job that ran third-party code, so it may decide
    # between two fixed messages but never become text in the payload.
    jobs = workflow_jobs(NIGHTLY)
    assert jobs["nightly-tests"]["outputs"]["eval_logs"] == "${{ steps.eval_logs.outputs.artifact-id != '' }}"
    payload = jobs["report"]["steps"][0]["with"]["payload"]
    uses = re.findall(r"needs\.nightly-tests\.outputs\.eval_logs[^&|}]*", payload)
    assert uses == ["needs.nightly-tests.outputs.eval_logs == 'true' "]


def test_clean_logs_are_all_copied_and_the_originals_left_alone(tmp_path):
    logs = tmp_path / "logs"
    (logs / "sub").mkdir(parents=True)
    write_eval(logs / "a.eval", {"header.json": '{"model": "anthropic/claude"}', "samples/1.json": '{"messages": []}'})
    (logs / "sub" / "b.json").write_text('{"status": "error"}')
    r = scan_logs(tmp_path, logs, **ALL_KEYS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert uploaded(tmp_path) == ["a.eval", "sub/b.json"]
    # An .eval is rebuilt from its members, so the members match, not the bytes.
    assert members(tmp_path / "checked" / "a.eval") == members(logs / "a.eval")
    assert (tmp_path / "checked" / "sub" / "b.json").read_bytes() == (logs / "sub" / "b.json").read_bytes()
    assert not (tmp_path / "checked" / "a.eval").is_symlink()
    assert sorted(str(p.relative_to(logs)) for p in logs.rglob("*") if p.is_file()) == ["a.eval", "sub/b.json"]
    assert r.stdout == "Checked 2 log entries; withheld 0.\n"


@pytest.mark.parametrize("key_var, key", list(ALL_KEYS.items()))
def test_a_log_holding_any_key_is_withheld_and_the_key_is_not_printed(tmp_path, key_var, key):
    logs = tmp_path / "logs"
    (logs / "sub").mkdir(parents=True)
    write_eval(logs / "clean.eval", {"header.json": "{}"})
    # The key sits in a compressed member, so only a scan of the
    # decompressed content finds it; the padding puts it across the scan's
    # 1 MiB chunk boundary.
    head = '{"output": "'
    leak = f"env: {key_var}="
    padding = "x" * ((1 << 20) - 10 - len(head) - len(leak))
    sample = head + padding + leak + key + '"}'
    assert sample.index(key) < 1 << 20 < sample.index(key) + len(key)
    write_eval(logs / "leak.eval", {"header.json": "{}", "samples/1.json": sample})
    (logs / "sub" / "leak.json").write_text(f'{{"error": "Bearer {key}"}}')
    r = scan_logs(tmp_path, logs, **ALL_KEYS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert uploaded(tmp_path) == ["clean.eval"]
    assert "::warning::Withheld 2 of 3 log entries from the upload: 2 held a provider key, 0 could not be read, 0 were links or special files." in r.stdout
    assert "Checked 3 log entries; withheld 2." in r.stdout
    assert_no_key_printed(r)


@pytest.mark.parametrize("where", ["file name", "directory name", "member name"])
def test_a_key_in_a_name_is_withheld_and_never_printed(tmp_path, where):
    # Review pass 1, F1: the artifact carries the log's path and its members'
    # names, so both are checked, and the step never names a file (a withheld
    # file's name may be the leak).
    logs = tmp_path / "logs"
    logs.mkdir()
    write_eval(logs / "clean.eval", {"header.json": "{}"})
    name, directory, member = "leak.eval", logs, "header.json"
    if where == "file name":
        name = f"run-{OPENAI}.eval"
    elif where == "directory name":
        directory = logs / f"task-{OPENAI}"
        directory.mkdir()
    elif where == "member name":
        member = f"samples/{OPENAI}.json"
    write_eval(directory / name, {member: '{"clean": true}'})
    r = scan_logs(tmp_path, logs, OPENAI_API_KEY=OPENAI)
    assert r.returncode == 0, r.stdout + r.stderr
    assert uploaded(tmp_path) == ["clean.eval"]
    assert "::warning::Withheld 1 of 2 log entries from the upload: 1 held a provider key, 0 could not be read, 0 were links or special files." in r.stdout
    assert_no_key_printed(r)


def local_member_start(raw: bytes, info) -> int:
    """Where a member's compressed bytes start: after its local header."""
    import struct

    name_length, extra_length = struct.unpack_from("<HH", raw, info.header_offset + 26)
    return info.header_offset + 30 + name_length + extra_length


@pytest.mark.parametrize("where", ["archive comment", "member comment", "member extra field", "pruned member"])
def test_a_key_outside_the_listed_members_is_never_uploaded(tmp_path, where):
    # Review pass 4, F1: an .eval can carry bytes no listed member reads:
    # comments, extra fields, and members Inspect pruned from the central
    # directory without compacting (still compressed, so a raw-byte check
    # cannot see the key). The step rebuilds an .eval from its listed members
    # only, so none of that reaches the upload, and the log is still uploaded.
    import struct

    zipfile = zstd_zipfile()
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "run.eval"
    if where == "pruned member":
        # The review's reproduction: the idiom Inspect's recorder uses.
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_ZSTANDARD) as z:
            z.writestr("old.json", f'{{"key": "{OPENAI}"}}')
            dead = z.getinfo("old.json")
            z.writestr("header.json", '{"status": "error"}')
            z.filelist = [i for i in z.filelist if i.filename != "old.json"]
            z.NameToInfo.pop("old.json")
        raw = path.read_bytes()
        assert OPENAI.encode() not in raw
        import zstandard

        start = local_member_start(raw, dead)
        hidden = zstandard.ZstdDecompressor().decompressobj().decompress(raw[start : start + dead.compress_size])
        assert OPENAI.encode() in hidden  # the fixture really hides the key
    else:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_ZSTANDARD) as z:
            info = zipfile.ZipInfo("header.json")
            info.compress_type = zipfile.ZIP_ZSTANDARD
            if where == "member comment":
                info.comment = f"key {OPENAI}".encode()
            elif where == "member extra field":
                payload = f"key {OPENAI}".encode()
                info.extra = struct.pack("<HH", 0xCAFE, len(payload)) + payload
            z.writestr(info, '{"status": "error"}')
            if where == "archive comment":
                z.comment = f"key {OPENAI}".encode()
        assert OPENAI.encode() in path.read_bytes()
    r = scan_logs(tmp_path, logs, OPENAI_API_KEY=OPENAI)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == "Checked 1 log entries; withheld 0.\n"
    rebuilt = tmp_path / "checked" / "run.eval"
    assert members(rebuilt) == {"header.json": b'{"status": "error"}'}
    raw = rebuilt.read_bytes()
    assert OPENAI.encode() not in raw and b"old.json" not in raw and b"\xfe\xca" not in raw
    with zipfile.ZipFile(rebuilt) as z:
        assert z.comment == b""
        assert [(i.filename, i.comment, i.extra) for i in z.infolist()] == [("header.json", b"", b"")]
    assert_no_key_printed(r)


def write_multiframe_eval(path: Path, text: str, frame_input: int) -> Path:
    """An .eval whose member Inspect's writer split into zstd frames of
    frame_input bytes (Inspect uses 200 MiB; a small cap keeps the fixture
    small)."""
    zstd_zipfile()
    import inspect_ai._util.zipfile as inspect_zipfile

    original = inspect_zipfile._MAX_INPUT_PER_FRAME
    inspect_zipfile._MAX_INPUT_PER_FRAME = frame_input
    try:
        write_eval(path, {"samples/1.json": text})
    finally:
        inspect_zipfile._MAX_INPUT_PER_FRAME = original
    import zipfile

    import zstandard

    raw = path.read_bytes()
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("samples/1.json")
    start = local_member_start(raw, info)
    compressed = raw[start : start + info.compress_size]
    # A reader that stops at the first frame (bare zipfile_zstd) gets only part.
    first = zstandard.ZstdDecompressor().decompressobj().decompress(compressed)
    assert len(first) < len(text), "fixture is a single frame"
    return path


def test_a_clean_multi_frame_member_is_read_and_uploaded(tmp_path):
    # Review pass 4, F2: Inspect writes members over 200 MiB as several zstd
    # frames, which zipfile_zstd alone cannot read; the step reads them with
    # Inspect's own reader instead of withholding a valid large log.
    logs = tmp_path / "logs"
    logs.mkdir()
    text = '{"output": "' + "x" * 10_000 + '"}'
    write_multiframe_eval(logs / "big.eval", text, frame_input=1_000)
    r = scan_logs(tmp_path, logs, OPENAI_API_KEY=OPENAI)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == "Checked 1 log entries; withheld 0.\n"
    assert members(tmp_path / "checked" / "big.eval") == {"samples/1.json": text.encode()}


def test_a_key_in_a_later_frame_is_found(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    text = '{"output": "' + "x" * 10_000 + OPENAI + '"}'
    write_multiframe_eval(logs / "big.eval", text, frame_input=1_000)
    r = scan_logs(tmp_path, logs, OPENAI_API_KEY=OPENAI)
    assert r.returncode == 0, r.stdout + r.stderr
    assert uploaded(tmp_path) == []
    assert "1 held a provider key, 0 could not be read" in r.stdout


def test_a_duplicate_member_name_prints_nothing(tmp_path):
    # Writing a duplicate name makes zipfile warn, naming the member; the
    # step's output stays counts only.
    import warnings

    zipfile = zstd_zipfile()
    logs = tmp_path / "logs"
    logs.mkdir()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(logs / "run.eval", "w", compression=zipfile.ZIP_ZSTANDARD) as z:
            z.writestr("samples/dup.json", "{}")
            z.writestr("samples/dup.json", '{"second": true}')
    r = scan_logs(tmp_path, logs, OPENAI_API_KEY=OPENAI)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == "Checked 1 log entries; withheld 0.\n" and r.stderr == ""


def test_a_member_whose_bytes_do_not_match_its_crc_is_withheld(tmp_path):
    zipfile = zstd_zipfile()
    logs = tmp_path / "logs"
    logs.mkdir()
    path = write_eval(logs / "run.eval", {"header.json": '{"status": "error"}'})
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("header.json")
    raw = bytearray(path.read_bytes())
    central = raw.rindex(b"PK\x01\x02")  # its central directory entry; CRC-32 at +16
    raw[central + 16 : central + 20] = (info.CRC ^ 0xFFFFFFFF).to_bytes(4, "little")
    path.write_bytes(bytes(raw))
    r = scan_logs(tmp_path, logs, OPENAI_API_KEY=OPENAI)
    assert r.returncode == 0, r.stdout + r.stderr
    assert uploaded(tmp_path) == []
    assert "0 held a provider key, 1 could not be read" in r.stdout


def test_a_rebuilt_eval_log_reads_back_with_inspect(tmp_path, monkeypatch):
    # The rebuilt archive is deflate, not zstd; Inspect's reader (and so the
    # viewer it feeds) must still open it as the same log.
    zstd_zipfile()
    from inspect_ai import Task, eval as inspect_eval
    from inspect_ai.dataset import Sample
    from inspect_ai.log import read_eval_log
    from inspect_ai.solver import generate

    monkeypatch.setenv("INSPECT_DISPLAY", "none")
    logs = tmp_path / "logs"
    (log,) = inspect_eval(Task(dataset=[Sample(input="hi"), Sample(input="there")], solver=generate()), model="mockllm/model", log_dir=str(logs))
    (original,) = list(logs.glob("*.eval"))
    r = scan_logs(tmp_path, logs, OPENAI_API_KEY=OPENAI)
    assert r.returncode == 0, r.stdout + r.stderr
    rebuilt = tmp_path / "checked" / original.name
    before, after = read_eval_log(str(original)), read_eval_log(str(rebuilt))
    assert after.status == before.status == "success"
    assert after.eval.model_dump() == before.eval.model_dump()
    assert after.results == before.results
    assert [s.model_dump() for s in after.samples] == [s.model_dump() for s in before.samples]


@pytest.mark.parametrize("target", ["directory", "file"])
def test_a_link_is_never_followed_or_copied(tmp_path, target):
    # Review pass 2, F1: upload-artifact follows links, and the walk does
    # not, so a link could carry unchecked files (or a key in its own name)
    # into the artifact. Links are left out of the checked copy and counted.
    logs = tmp_path / "logs"
    logs.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    write_eval(outside / "leak.eval", {"samples/1.json": f'{{"output": "{OPENAI}"}}'})
    write_eval(logs / "clean.eval", {"header.json": "{}"})
    (logs / "linked").symlink_to(outside if target == "directory" else outside / "leak.eval")
    (logs / f"named-{OPENAI}").symlink_to(outside / "leak.eval")
    r = scan_logs(tmp_path, logs, OPENAI_API_KEY=OPENAI)
    assert r.returncode == 0, r.stdout + r.stderr
    assert uploaded(tmp_path) == ["clean.eval"]
    # The key-named link is caught by its path before it is ever opened.
    assert "::warning::Withheld 2 of 3 log entries from the upload: 1 held a provider key, 0 could not be read, 1 were links or special files." in r.stdout
    assert_no_key_printed(r)


# Review pass 3, F1: a log can change while the step runs. The step opens each
# log once, copies it from that descriptor and checks the copy, so the check
# and the upload see the same bytes. These tests change the log at a chosen
# line of the step's script (a trace hook, as in the review's reproduction):
# just before the log is opened, and after it was copied but before the copy
# is checked.

INTERLEAVE = r"""
import os, pathlib, sys
source, marker, mode, log, outside = sys.argv[1:]
lines = source.splitlines()
(target,) = [i for i, line in enumerate(lines, 1) if line.strip().startswith(marker)]
fired = []

def act():
    log_path = pathlib.Path(log)
    if mode == "rewrite":
        log_path.write_text(pathlib.Path(outside).read_text())
        return
    log_path.unlink()
    if mode == "symlink":
        log_path.symlink_to(outside)
    elif mode == "fifo":
        os.mkfifo(log_path)

def trace(frame, event, arg):
    if not fired and event == "line" and frame.f_code.co_filename == "<log-scan>" and frame.f_lineno == target:
        fired.append(True)
        act()
    return trace

sys.settrace(trace)
try:
    exec(compile(source, "<log-scan>", "exec"), {"__name__": "__main__"})
finally:
    sys.settrace(None)
    print("FIRED" if fired else "NOT FIRED", file=sys.stderr)
"""

BEFORE_OPEN = "source = os.open(path, flags)"
AFTER_COPY = "check(copy)"


def scan_with_interleaving(tmp_path: Path, marker: str, mode: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
    zstd_zipfile()
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "run.json"
    log.write_text('{"status": "error"}')
    outside = tmp_path / "outside.json"
    outside.write_text(f'{{"error": "{OPENAI}"}}')
    run = step(NIGHTLY, "nightly-tests", "log_scan")["run"]
    source = run.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    env = {**os.environ, "INSPECT_LOG_DIR": str(logs), "CHECKED_LOG_DIR": str(tmp_path / "checked"), "ANTHROPIC_API_KEY": "", "GOOGLE_API_KEY": "", "OPENAI_API_KEY": OPENAI}
    r = subprocess.run([sys.executable, "-I", "-c", INTERLEAVE, source, marker, mode, str(log), str(outside)], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=60)
    assert r.stderr.endswith("FIRED\n") and not r.stderr.endswith("NOT FIRED\n"), r.stderr
    return r, tmp_path / "checked", log


def assert_checked_dir_holds_no_key_or_link(checked: Path) -> None:
    for p in checked.rglob("*"):
        assert not p.is_symlink(), p
        if p.is_file():
            assert OPENAI.encode() not in p.read_bytes(), p


@pytest.mark.parametrize("mode", ["rewrite", "symlink"])
def test_a_log_changed_after_it_was_copied_changes_nothing_uploaded(tmp_path, mode):
    # The review's interleaving: the log passes, then is rewritten with a key
    # or swapped for a link to one. The check reads the copy, and nothing
    # reads the log again, so the clean copy is what goes up.
    r, checked, _ = scan_with_interleaving(tmp_path, AFTER_COPY, mode)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == "Checked 1 log entries; withheld 0.\n"
    assert (checked / "run.json").read_text() == '{"status": "error"}'
    assert_checked_dir_holds_no_key_or_link(checked)


@pytest.mark.parametrize(
    "mode, counts",
    [
        ("rewrite", "1 held a provider key, 0 could not be read, 0 were links or special files."),
        ("symlink", "0 held a provider key, 0 could not be read, 1 were links or special files."),
        ("fifo", "0 held a provider key, 0 could not be read, 1 were links or special files."),
        ("delete", "0 held a provider key, 1 could not be read, 0 were links or special files."),
    ],
)
def test_a_log_changed_after_the_walk_found_it_is_judged_by_what_is_opened(tmp_path, mode, counts):
    # Between the walk and the open the log may be rewritten, swapped for a
    # link or a fifo, or removed. The open does not follow a link or wait for
    # a fifo's writer, and the descriptor, not an earlier path lookup, decides
    # what the log is.
    r, checked, _ = scan_with_interleaving(tmp_path, BEFORE_OPEN, mode)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"::warning::Withheld 1 of 1 log entries from the upload: {counts}" in r.stdout
    assert [p for p in checked.rglob("*") if p.is_file() or p.is_symlink()] == []
    assert OPENAI not in r.stdout + r.stderr


def test_a_linked_log_directory_fails_the_check(tmp_path):
    real = tmp_path / "real-logs"
    real.mkdir()
    write_eval(real / "a.eval", {"header.json": "{}"})
    (tmp_path / "logs").symlink_to(real)
    r = scan_logs(tmp_path, tmp_path / "logs", OPENAI_API_KEY=OPENAI)
    assert r.returncode == 1
    assert r.stdout == FAILED and r.stderr == ""
    assert uploaded(tmp_path) == []


def test_a_fifo_is_withheld_without_being_read(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    os.mkfifo(logs / "pipe.json")
    r = scan_logs(tmp_path, logs, OPENAI_API_KEY=OPENAI)
    assert r.returncode == 0, r.stdout + r.stderr
    assert uploaded(tmp_path) == []
    assert "0 held a provider key, 0 could not be read, 1 were links or special files." in r.stdout


def test_an_unreadable_eval_log_is_withheld(tmp_path):
    # A log cut off mid-write (a cancelled run) cannot be checked, so it is not uploaded.
    logs = tmp_path / "logs"
    logs.mkdir()
    write_eval(logs / "ok.eval", {"header.json": "{}"})
    (logs / "partial.eval").write_bytes(write_eval(tmp_path / "whole.eval", {"header.json": "{}" * 500}).read_bytes()[:60])
    r = scan_logs(tmp_path, logs, ANTHROPIC_API_KEY=ANTHROPIC)
    assert r.returncode == 0, r.stdout + r.stderr
    assert uploaded(tmp_path) == ["ok.eval"]
    assert "::warning::Withheld 1 of 2 log entries from the upload: 0 held a provider key, 1 could not be read, 0 were links or special files." in r.stdout


NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")


@NOT_ROOT
def test_an_unreadable_file_is_withheld_and_its_key_bearing_name_is_not_printed(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / f"{GOOGLE}.json").write_text("{}")
    (logs / f"{GOOGLE}.json").chmod(0)
    r = scan_logs(tmp_path, logs, ANTHROPIC_API_KEY=ANTHROPIC)
    assert r.returncode == 0, r.stdout + r.stderr
    assert uploaded(tmp_path) == []
    assert "::warning::Withheld 1 of 1 log entries from the upload: 0 held a provider key, 1 could not be read, 0 were links or special files." in r.stdout
    assert_no_key_printed(r)


@NOT_ROOT
def test_a_key_named_log_in_a_read_only_directory_is_withheld_without_a_delete(tmp_path):
    # Review pass 2, F2: withholding used to delete the log, and a failed
    # delete printed its key-bearing path in a traceback. Nothing is deleted
    # now; the log is just not copied.
    logs = tmp_path / "logs"
    (logs / "ro").mkdir(parents=True)
    write_eval(logs / "ro" / f"run-{OPENAI}.eval", {"header.json": "{}"})
    (logs / "ro").chmod(0o555)
    try:
        r = scan_logs(tmp_path, logs, OPENAI_API_KEY=OPENAI)
    finally:
        (logs / "ro").chmod(0o755)
    assert r.returncode == 0, r.stdout + r.stderr
    assert uploaded(tmp_path) == []
    assert (logs / "ro" / f"run-{OPENAI}.eval").exists()
    assert_no_key_printed(r)


@NOT_ROOT
def test_a_directory_that_cannot_be_listed_fails_the_check_without_naming_it(tmp_path):
    # Review pass 2, F2: a traversal error is not a partial success. The
    # step fails (so nothing is uploaded) and says only that it failed.
    logs = tmp_path / "logs"
    (logs / f"task-{OPENAI}").mkdir(parents=True)
    write_eval(logs / "clean.eval", {"header.json": "{}"})
    (logs / f"task-{OPENAI}").chmod(0)
    try:
        r = scan_logs(tmp_path, logs, OPENAI_API_KEY=OPENAI)
    finally:
        (logs / f"task-{OPENAI}").chmod(0o755)
    assert r.returncode == 1
    assert r.stdout == FAILED and r.stderr == ""
    assert_no_key_printed(r)


def test_a_leftover_checked_directory_fails_the_check(tmp_path):
    # The checked directory must be fresh, so it holds only what this run
    # checked; a leftover one fails the step rather than uploading a mix.
    logs = tmp_path / "logs"
    logs.mkdir()
    write_eval(logs / "run.eval", {"header.json": "{}"})
    (tmp_path / "checked").mkdir()
    r = scan_logs(tmp_path, logs, OPENAI_API_KEY=OPENAI)
    assert r.returncode == 1
    assert r.stdout == FAILED and r.stderr == ""


def test_an_unset_key_is_not_an_empty_pattern_that_matches_every_log(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    write_eval(logs / "a.eval", {"header.json": '{"model": "openai/gpt"}'})
    r = scan_logs(tmp_path, logs, OPENAI_API_KEY=OPENAI)
    assert r.returncode == 0, r.stdout + r.stderr
    assert uploaded(tmp_path) == ["a.eval"]
    assert r.stdout == "Checked 1 log entries; withheld 0.\n"
    import shutil

    shutil.rmtree(tmp_path / "checked")
    r = scan_logs(tmp_path, logs)
    assert r.returncode == 0, r.stdout + r.stderr
    assert uploaded(tmp_path) == ["a.eval"]


def test_no_log_directory_is_a_warning_not_a_failure(tmp_path):
    # e.g. the install step failed before any test ran.
    r = scan_logs(tmp_path, tmp_path / "missing", ANTHROPIC_API_KEY=ANTHROPIC)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == "::warning::No Inspect logs were written; there is nothing to upload.\n"
    assert uploaded(tmp_path) == []
