"""Tests for the setup-uv and install-python-dependencies composite actions.

The actions take their inputs through env:, never as ${{ }} inside run:, and
build uv commands as bash arrays with no eval. These tests run check_inputs.py
and install.sh off a runner: check_inputs.py under python3 with the inputs as
env vars, and install.sh under bash with a stand-in uv on PATH that records its
argument vector. They also check the static rules the workflows must keep: every
job with an unlocked step sets a safe cache-mode, and setup-uv pins uv 0.13.5 or
later. The self-test workflow (self-test-python-actions.yml) proves the Actions
cache and Action save behavior on a real runner.

Run with `python3 -m pytest` from the repo root (needs pytest and PyYAML;
.github/workflows/tests.yml does the same in CI).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SETUP_UV = ROOT / "setup-uv"
INSTALL = ROOT / "install-python-dependencies"
FIXTURES = ROOT / "tests" / "fixtures" / "python-actions"
SELF_TEST = ROOT / ".github" / "workflows" / "self-test-python-actions.yml"
REPO = "meridianlabs-ai/actions"

# Defaults from install-python-dependencies/action.yml, keyed by the env var the
# Check inputs step sets. versions and python-version have no default (required).
INSTALL_ENV_DEFAULTS = {
    "PROJECT_DIR": ".",
    "ENVIRONMENT_DIR": "",
    "PROJECT_INSTALL": "editable",
    "EXTRAS": "",
    "DEFAULT_GROUPS": "include",
    "REQUIREMENTS_FILE": "",
    "SCRIPT": "",
    "UNLOCKED_EXTRA": "",
    "WITHOUT_DEPS": "",
    "PROJECT_UV_SETTINGS": "apply",
    "PACKAGE_FORMAT": "any",
    "ACTIONS_CACHE": "read-write",
    "RECORDED_SETUP": "",
}


def run_check(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(script / "check_inputs.py")],
        env={**os.environ, **env},
        text=True,
        capture_output=True,
        check=False,
    )


def check_install(**inputs: str) -> subprocess.CompletedProcess:
    """Run install-python-dependencies/check_inputs.py with the given env vars
    over the action's defaults."""
    return run_check(INSTALL, {**INSTALL_ENV_DEFAULTS, **inputs})


def check_setup(**inputs: str) -> subprocess.CompletedProcess:
    base = {"PYTHON_VERSION": "", "ACTIONS_CACHE": "read-write", "RECORDED_SETUP": ""}
    return run_check(SETUP_UV, {**base, **inputs})


FAKE_UV = """#!/usr/bin/env bash
# Record the argument vector uv would have received, one call per line.
printf '%s\\n' "$*" >> "$FAKE_UV_CALLS"
"""


def run_install(tmp_path: Path, **inputs: str) -> list[str]:
    """Run install.sh with a stand-in uv on PATH; return the recorded uv calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "uv").write_text(FAKE_UV)
    (bin_dir / "uv").chmod(0o755)
    calls = tmp_path / "uv_calls"
    base = {
        "VERSIONS": "",
        "ENVIRONMENT_DIR": "",
        "PROJECT_INSTALL": "editable",
        "EXTRAS": "",
        "DEFAULT_GROUPS": "include",
        "REQUIREMENTS_FILE": "",
        "SCRIPT": "",
        "UNLOCKED_EXTRA": "",
        "WITHOUT_DEPS": "",
        "PROJECT_UV_SETTINGS": "apply",
        "PACKAGE_FORMAT": "any",
    }
    env = {
        **os.environ,
        **base,
        **inputs,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_UV_CALLS": str(calls),
    }
    r = subprocess.run(
        ["bash", str(INSTALL / "install.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return calls.read_text().splitlines() if calls.exists() else []


# --- setup-uv check_inputs ----------------------------------------------------


def test_setup_uv_accepts_each_cache_value():
    for cache in ("read-write", "restore-only", "none"):
        r = check_setup(PYTHON_VERSION="3.11", ACTIONS_CACHE=cache)
        assert r.returncode == 0, r.stdout


def test_setup_uv_rejects_an_unknown_cache_value():
    r = check_setup(PYTHON_VERSION="3.11", ACTIONS_CACHE="read")
    assert r.returncode == 1
    assert "::error::actions-cache: unknown value" in r.stdout


def test_setup_uv_rejects_a_newline_in_an_input():
    r = check_setup(PYTHON_VERSION="3.11\nx", ACTIONS_CACHE="none")
    assert r.returncode == 1
    assert "::error::python-version: must be one line." in r.stdout


def test_setup_uv_rejects_a_second_call_in_a_job():
    r = check_setup(PYTHON_VERSION="3.11", ACTIONS_CACHE="none", RECORDED_SETUP="3.11/none")
    assert r.returncode == 1
    assert "::error::setup-uv: already ran" in r.stdout


# --- install check_inputs: required, unknown, newline, list items -------------


def test_install_accepts_a_minimal_locked_call():
    assert check_install(VERSIONS="locked", PYTHON_VERSION="3.11").returncode == 0


def test_install_requires_versions():
    r = check_install(VERSIONS="", PYTHON_VERSION="3.11")
    assert r.returncode == 1 and "::error::versions: is required." in r.stdout


def test_install_requires_python_version():
    r = check_install(VERSIONS="locked", PYTHON_VERSION="")
    assert r.returncode == 1 and "::error::python-version: is required." in r.stdout


def test_install_rejects_an_unknown_versions_value():
    r = check_install(VERSIONS="newest", PYTHON_VERSION="3.11")
    assert r.returncode == 1 and "::error::versions: unknown value" in r.stdout


@pytest.mark.parametrize(
    "name, env",
    [
        ("project-install", {"PROJECT_INSTALL": "non-editable"}),
        ("default-dependency-groups", {"DEFAULT_GROUPS": "all"}),
        ("project-uv-settings", {"PROJECT_UV_SETTINGS": "use"}),
        ("package-format", {"PACKAGE_FORMAT": "sdist"}),
        ("actions-cache", {"ACTIONS_CACHE": "read"}),
    ],
)
def test_install_rejects_an_unknown_enum_value(name, env):
    r = check_install(VERSIONS="locked", PYTHON_VERSION="3.11", **env)
    assert r.returncode == 1 and f"::error::{name}: unknown value" in r.stdout


def test_install_accepts_each_enum_value():
    assert check_install(VERSIONS="locked", PYTHON_VERSION="3.11", PROJECT_INSTALL="skip").returncode == 0
    assert check_install(VERSIONS="locked", PYTHON_VERSION="3.11", DEFAULT_GROUPS="exclude").returncode == 0
    assert check_install(VERSIONS="locked", PYTHON_VERSION="3.11", ACTIONS_CACHE="restore-only").returncode == 0


def test_install_rejects_a_newline_in_a_one_line_input():
    r = check_install(VERSIONS="locked", PYTHON_VERSION="3.11\nsha=forged")
    assert r.returncode == 1 and "::error::python-version: must be one line." in r.stdout


@pytest.mark.parametrize(
    "env_var, value, error",
    [
        ("EXTRAS", "-x", "::error::extras:"),
        ("WITHOUT_DEPS", "--no-index", "::error::also-install-without-deps:"),
    ],
)
def test_install_rejects_a_list_item_that_starts_with_dash(env_var, value, error):
    # extras (locked) and also-install-without-deps (locked) both reject an option-like item.
    r = check_install(VERSIONS="locked", PYTHON_VERSION="3.11", **{env_var: value})
    assert r.returncode == 1
    assert error in r.stdout
    assert "starts with '-'" in r.stdout


# --- install check_inputs: inputs that apply to one versions value only --------


def test_requirements_file_is_required_with_hashed_requirements():
    r = check_install(VERSIONS="hashed-requirements", PYTHON_VERSION="3.11", PROJECT_UV_SETTINGS="ignore")
    assert r.returncode == 1 and "::error::requirements-file: is required" in r.stdout


def test_requirements_file_accepted_with_hashed_requirements():
    r = check_install(VERSIONS="hashed-requirements", PYTHON_VERSION="3.11", REQUIREMENTS_FILE="r.txt", PROJECT_UV_SETTINGS="ignore")
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("versions", ["locked", "unlocked"])
def test_requirements_file_only_applies_to_hashed_requirements(versions):
    extra = {"PROJECT_UV_SETTINGS": "ignore", "ACTIONS_CACHE": "none"} if versions == "unlocked" else {}
    r = check_install(VERSIONS=versions, PYTHON_VERSION="3.11", REQUIREMENTS_FILE="r.txt", **extra)
    assert r.returncode == 1 and "::error::requirements-file: applies only to versions: hashed-requirements." in r.stdout


@pytest.mark.parametrize("versions", ["hashed-requirements", "unlocked"])
def test_script_only_applies_to_locked(versions):
    extra = {"REQUIREMENTS_FILE": "r.txt"} if versions == "hashed-requirements" else {"ACTIONS_CACHE": "none"}
    r = check_install(VERSIONS=versions, PYTHON_VERSION="3.11", SCRIPT="s.py", PROJECT_UV_SETTINGS="ignore", **extra)
    assert r.returncode == 1 and "::error::script: applies only to versions: locked." in r.stdout


def test_script_accepted_with_locked():
    assert check_install(VERSIONS="locked", PYTHON_VERSION="3.11", SCRIPT="s.py").returncode == 0


@pytest.mark.parametrize("versions", ["hashed-requirements", "unlocked"])
def test_default_dependency_groups_only_applies_to_locked(versions):
    extra = {"REQUIREMENTS_FILE": "r.txt"} if versions == "hashed-requirements" else {"ACTIONS_CACHE": "none"}
    r = check_install(VERSIONS=versions, PYTHON_VERSION="3.11", DEFAULT_GROUPS="exclude", PROJECT_UV_SETTINGS="ignore", **extra)
    assert r.returncode == 1 and "::error::default-dependency-groups: applies only to versions: locked." in r.stdout


@pytest.mark.parametrize("versions", ["locked", "hashed-requirements"])
def test_also_install_unlocked_only_applies_to_unlocked(versions):
    extra = {"REQUIREMENTS_FILE": "r.txt", "PROJECT_UV_SETTINGS": "ignore"} if versions == "hashed-requirements" else {}
    r = check_install(VERSIONS=versions, PYTHON_VERSION="3.11", UNLOCKED_EXTRA="pytest-timeout", **extra)
    assert r.returncode == 1 and "::error::also-install-unlocked: applies only to versions: unlocked." in r.stdout


def test_also_install_unlocked_accepted_with_unlocked():
    r = check_install(VERSIONS="unlocked", PYTHON_VERSION="3.11", UNLOCKED_EXTRA="pytest-timeout", PROJECT_UV_SETTINGS="ignore", ACTIONS_CACHE="none")
    assert r.returncode == 0, r.stdout


def test_extras_do_not_apply_to_hashed_requirements():
    r = check_install(VERSIONS="hashed-requirements", PYTHON_VERSION="3.11", REQUIREMENTS_FILE="r.txt", EXTRAS="doc", PROJECT_UV_SETTINGS="ignore")
    assert r.returncode == 1 and "::error::extras: does not apply to versions: hashed-requirements." in r.stdout


def test_project_install_does_not_apply_to_hashed_requirements():
    r = check_install(VERSIONS="hashed-requirements", PYTHON_VERSION="3.11", REQUIREMENTS_FILE="r.txt", PROJECT_INSTALL="skip", PROJECT_UV_SETTINGS="ignore")
    assert r.returncode == 1 and "::error::project-install: does not apply to versions: hashed-requirements." in r.stdout


# --- install check_inputs: also-install-without-deps, wheels-only, unlocked ----


def test_also_install_without_deps_not_with_hashed_requirements():
    r = check_install(VERSIONS="hashed-requirements", PYTHON_VERSION="3.11", REQUIREMENTS_FILE="r.txt", WITHOUT_DEPS="./pkg", PROJECT_UV_SETTINGS="ignore")
    assert r.returncode == 1 and "::error::also-install-without-deps: does not apply to versions: hashed-requirements." in r.stdout


def test_locked_also_install_without_deps_must_be_a_local_path():
    r = check_install(VERSIONS="locked", PYTHON_VERSION="3.11", WITHOUT_DEPS="pytest-timeout")
    assert r.returncode == 1 and "must be a local path with versions: locked." in r.stdout


def test_locked_also_install_without_deps_accepts_a_local_path():
    assert check_install(VERSIONS="locked", PYTHON_VERSION="3.11", WITHOUT_DEPS="./tests/test_package").returncode == 0


def test_unlocked_also_install_without_deps_accepts_a_requirement():
    r = check_install(VERSIONS="unlocked", PYTHON_VERSION="3.11", WITHOUT_DEPS="some-wheel", PROJECT_UV_SETTINGS="ignore", ACTIONS_CACHE="none")
    assert r.returncode == 0, r.stdout


def test_wheels_only_does_not_apply_to_locked():
    r = check_install(VERSIONS="locked", PYTHON_VERSION="3.11", PACKAGE_FORMAT="wheels-only")
    assert r.returncode == 1 and "::error::package-format: wheels-only does not apply to versions: locked." in r.stdout


def test_wheels_only_accepted_with_hashed_requirements():
    r = check_install(VERSIONS="hashed-requirements", PYTHON_VERSION="3.11", REQUIREMENTS_FILE="r.txt", PACKAGE_FORMAT="wheels-only", PROJECT_UV_SETTINGS="ignore")
    assert r.returncode == 0, r.stdout


def test_unlocked_needs_actions_cache_none():
    r = check_install(VERSIONS="unlocked", PYTHON_VERSION="3.11", ACTIONS_CACHE="read-write", PROJECT_UV_SETTINGS="ignore")
    assert r.returncode == 1 and "::error::actions-cache: must be none with versions: unlocked." in r.stdout


def test_unlocked_needs_project_uv_settings_ignore():
    r = check_install(VERSIONS="unlocked", PYTHON_VERSION="3.11", ACTIONS_CACHE="none", PROJECT_UV_SETTINGS="apply")
    assert r.returncode == 1 and "::error::project-uv-settings: must be ignore with versions: unlocked." in r.stdout


def test_unlocked_minimal_call_is_accepted():
    r = check_install(VERSIONS="unlocked", PYTHON_VERSION="3.11", ACTIONS_CACHE="none", PROJECT_UV_SETTINGS="ignore")
    assert r.returncode == 0, r.stdout


def test_script_not_with_environment_dir():
    r = check_install(VERSIONS="locked", PYTHON_VERSION="3.11", SCRIPT="s.py", ENVIRONMENT_DIR="/tmp/env")
    assert r.returncode == 1 and "::error::environment-dir: does not apply with script." in r.stdout


def test_script_not_with_also_install_without_deps():
    r = check_install(VERSIONS="locked", PYTHON_VERSION="3.11", SCRIPT="s.py", WITHOUT_DEPS="./pkg")
    assert r.returncode == 1 and "::error::also-install-without-deps: does not apply with script." in r.stdout


def test_a_second_call_must_match_the_recorded_setup():
    r = check_install(VERSIONS="locked", PYTHON_VERSION="3.12", ACTIONS_CACHE="read-write", RECORDED_SETUP="3.11/read-write")
    assert r.returncode == 1 and "::error::python-version/actions-cache:" in r.stdout


def test_a_second_call_that_matches_the_recorded_setup_is_accepted():
    r = check_install(VERSIONS="locked", PYTHON_VERSION="3.11", ACTIONS_CACHE="read-write", RECORDED_SETUP="3.11/read-write")
    assert r.returncode == 0, r.stdout


# --- install.sh builds the exact uv commands of today's call sites -------------


def test_locked_runs_uv_sync_locked(tmp_path):
    calls = run_install(tmp_path, VERSIONS="locked")
    assert calls == ["sync --locked"]


def test_locked_adds_extras_groups_and_project_flags(tmp_path):
    calls = run_install(tmp_path, VERSIONS="locked", EXTRAS="doc\ndist", DEFAULT_GROUPS="exclude", PROJECT_INSTALL="skip")
    assert calls == ["sync --locked --extra doc --extra dist --no-default-groups --no-install-project"]


def test_locked_no_config_when_project_uv_settings_ignore(tmp_path):
    calls = run_install(tmp_path, VERSIONS="locked", PROJECT_UV_SETTINGS="ignore")
    assert calls == ["sync --no-config --locked"]


def test_locked_script_runs_uv_sync_locked_script(tmp_path):
    calls = run_install(tmp_path, VERSIONS="locked", SCRIPT="s.py")
    assert calls == ["sync --locked --script s.py"]


def test_locked_also_install_without_deps_gets_no_deps(tmp_path):
    calls = run_install(tmp_path, VERSIONS="locked", WITHOUT_DEPS="./a\n./b")
    assert calls[0] == "sync --locked"
    assert calls[1].startswith("pip install --python ") and calls[1].endswith(" --no-deps ./a")
    assert calls[2].endswith(" --no-deps ./b")


def test_hashed_runs_venv_then_require_hashes_no_deps(tmp_path):
    calls = run_install(tmp_path, VERSIONS="hashed-requirements", ENVIRONMENT_DIR="/tmp/env", REQUIREMENTS_FILE="r.txt", PROJECT_UV_SETTINGS="ignore")
    assert calls[0] == "venv /tmp/env"
    assert calls[1] == "pip install --no-config --python /tmp/env --require-hashes --no-deps -r r.txt"


def test_hashed_wheels_only_adds_only_binary(tmp_path):
    calls = run_install(tmp_path, VERSIONS="hashed-requirements", ENVIRONMENT_DIR="/tmp/env", REQUIREMENTS_FILE="r.txt", PACKAGE_FORMAT="wheels-only", PROJECT_UV_SETTINGS="ignore")
    assert calls[1] == "pip install --no-config --python /tmp/env --require-hashes --only-binary :all: --no-deps -r r.txt"


def test_unlocked_runs_venv_then_pip_install_with_exclude_newer_false(tmp_path):
    calls = run_install(tmp_path, VERSIONS="unlocked", ENVIRONMENT_DIR="/tmp/env", EXTRAS="dev", UNLOCKED_EXTRA="pytest-timeout", PROJECT_UV_SETTINGS="ignore")
    assert calls[0] == "venv /tmp/env"
    assert calls[1] == 'pip install --no-config --python /tmp/env --exclude-newer false -e .[dev] pytest-timeout'


def test_unlocked_skip_project_install_drops_the_dash_e(tmp_path):
    calls = run_install(tmp_path, VERSIONS="unlocked", ENVIRONMENT_DIR="/tmp/env", PROJECT_INSTALL="skip", UNLOCKED_EXTRA=".[doc]", PROJECT_UV_SETTINGS="ignore")
    assert calls[1] == "pip install --no-config --python /tmp/env --exclude-newer false .[doc]"


def test_unlocked_no_deps_items_use_exclude_newer_false_and_never_only_binary(tmp_path):
    calls = run_install(tmp_path, VERSIONS="unlocked", ENVIRONMENT_DIR="/tmp/env", WITHOUT_DEPS="./pkg", PACKAGE_FORMAT="wheels-only", PROJECT_UV_SETTINGS="ignore")
    # wheels-only reaches the main install, never the --no-deps item.
    assert calls[1] == "pip install --no-config --python /tmp/env --exclude-newer false --only-binary :all: -e ."
    assert calls[2] == "pip install --no-config --python /tmp/env --exclude-newer false --no-deps ./pkg"


def test_only_unlocked_sets_exclude_newer_false(tmp_path):
    for versions, extra in [("locked", {}), ("hashed-requirements", {"ENVIRONMENT_DIR": "/tmp/e", "REQUIREMENTS_FILE": "r.txt", "PROJECT_UV_SETTINGS": "ignore"})]:
        calls = run_install(tmp_path, VERSIONS=versions, **extra)
        assert not any("--exclude-newer" in c for c in calls), (versions, calls)


def test_no_command_runs_uv_lock(tmp_path):
    for versions, extra in [
        ("locked", {}),
        ("locked", {"SCRIPT": "s.py"}),
        ("hashed-requirements", {"ENVIRONMENT_DIR": "/tmp/e", "REQUIREMENTS_FILE": "r.txt", "PROJECT_UV_SETTINGS": "ignore"}),
        ("unlocked", {"ENVIRONMENT_DIR": "/tmp/e", "PROJECT_UV_SETTINGS": "ignore"}),
    ]:
        calls = run_install(tmp_path, VERSIONS=versions, **extra)
        assert all(not c.startswith("lock") and " lock" not in c for c in calls), (versions, calls)


def test_every_uv_sync_has_locked(tmp_path):
    for extra in [{}, {"SCRIPT": "s.py"}, {"EXTRAS": "doc"}]:
        calls = run_install(tmp_path, VERSIONS="locked", **extra)
        for c in calls:
            if c.startswith("sync"):
                assert "--locked" in c, c


# --- Static rules across this repo's workflows ---------------------------------

WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def job_has_unlocked_step(job: dict) -> bool:
    if not isinstance(job, dict):
        return False
    for s in job.get("steps", []) or []:
        with_ = s.get("with") or {}
        if str(with_.get("versions", "")).strip() == "unlocked":
            return True
    return False


def comment_block_above(text: str, job_name: str) -> str:
    """The contiguous comment lines directly above a job's key line."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"  {job_name}:"):
            out = []
            j = i - 1
            while j >= 0 and lines[j].lstrip().startswith("#"):
                out.append(lines[j])
                j -= 1
            return "\n".join(reversed(out))
    return ""


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_unlocked_job_sets_a_safe_cache_mode_and_warns(workflow):
    wf = yaml.safe_load(workflow.read_text())
    text = workflow.read_text()
    workflow_mode = wf.get("cache-mode")
    for name, job in (wf.get("jobs") or {}).items():
        if not job_has_unlocked_step(job):
            continue
        mode = job.get("cache-mode", workflow_mode)
        assert mode in ("none", "read"), f"{workflow.name} / {name}: unlocked job needs cache-mode none or read, got {mode!r}"
        comment = comment_block_above(text, name).lower()
        assert "unlocked" in comment and "malicious" in comment, f"{workflow.name} / {name}: unlocked job needs the warning comment"


def test_the_self_test_workflow_has_an_unlocked_job():
    # Guard the test above against passing because nothing is unlocked.
    wf = yaml.safe_load(SELF_TEST.read_text())
    assert any(job_has_unlocked_step(job) for job in wf["jobs"].values())


# --- setup-uv pins uv 0.13.5 or later -----------------------------------------


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def test_setup_uv_pins_uv_0_13_5_or_later():
    steps = yaml.safe_load((SETUP_UV / "action.yml").read_text())["runs"]["steps"]
    uv_steps = [s for s in steps if str(s.get("uses", "")).startswith("astral-sh/setup-uv@")]
    assert len(uv_steps) == 1, uv_steps
    version = str(uv_steps[0]["with"]["version"])
    assert _version_tuple(version) >= (0, 13, 5), version


def test_setup_uv_pins_astral_setup_uv_by_sha_with_a_version_comment():
    text = (SETUP_UV / "action.yml").read_text()
    lines = [l for l in text.splitlines() if "astral-sh/setup-uv@" in l]
    assert len(lines) == 1, lines
    # uses: astral-sh/setup-uv@<40-hex> # vX.Y.Z
    import re

    assert re.search(r"astral-sh/setup-uv@[0-9a-f]{40} # v\d+\.\d+\.\d+", lines[0]), lines[0]


# --- The Actions-cache check scripts of the self-test workflow ------------------
#
# check-action-save and cache-mode-read-blocks-write each read the Actions cache
# list with `gh api` and decide pass or fail from it. The runner proves the real
# cache behavior; these tests lift the run: script from the YAML and run it under
# bash with a stand-in gh on PATH that returns canned API JSON, so the check logic
# is covered off a runner.

FAKE_GH = r'''#!/usr/bin/env python3
"""Stand-in for `gh api`: serve the Actions cache list from $FAKE_GH_DIR, then
apply --jq with the real jq."""
import os, pathlib, re, subprocess, sys

args = sys.argv[1:]
store = pathlib.Path(os.environ["FAKE_GH_DIR"])
assert args[0] == "api", args
path = args[1].split("?")[0]
if re.fullmatch(r"repos/[^/]+/[^/]+/actions/caches", path):
    data = (store / "caches.json").read_text()
else:
    sys.exit(f"fake gh: unexpected call {args}")
if "--jq" in args:
    data = subprocess.run(["jq", "-r", args[args.index("--jq") + 1]], input=data, text=True, capture_output=True, check=True).stdout
sys.stdout.write(data)
'''


def run_bash(script: str, *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    # GitHub runs `run:` scripts with `bash --noprofile --norc -eo pipefail`.
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=cwd,
        env={**os.environ, **env},
        text=True,
        capture_output=True,
        check=False,
    )


def fake_gh_on_path(tmp_path: Path, *, caches: list[dict]) -> dict:
    """Write the canned API JSON and return the PATH-with-fake-gh env fragment."""
    store = tmp_path / "gh"
    store.mkdir(exist_ok=True)
    (store / "caches.json").write_text(json.dumps({"actions_caches": caches}))
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir(exist_ok=True)
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    return {"PATH": f"{gh.parent}:{os.environ['PATH']}", "FAKE_GH_DIR": str(store)}


def run_step(job: str, name_prefix: str) -> dict:
    """The step of a self-test job whose name starts with name_prefix."""
    for s in yaml.safe_load(SELF_TEST.read_text())["jobs"][job]["steps"]:
        if str(s.get("name", "")).startswith(name_prefix):
            return s
    raise KeyError(f"{job}/{name_prefix}")


def cache(key: str, created: str) -> dict:
    return {"key": key, "created_at": created}


STARTED = "2026-01-01T00:00:00Z"  # each job's recorded before-install time
FRESH = "2026-01-01T00:00:01Z"  # at or after the recorded time
OLD = "2025-12-31T23:59:59Z"  # before the recorded time
SAME_SECOND = "2026-01-01T00:00:00.500Z"  # fractional, same whole second as STARTED


def run_check_action_save(tmp_path: Path, caches: list[dict]) -> subprocess.CompletedProcess:
    path_env = fake_gh_on_path(tmp_path, caches=caches)
    s = run_step("check-action-save", "Only the read-write job saved")
    env = {
        **path_env,
        "GH_TOKEN": "fake",
        "REPO": REPO,
        "REF": "refs/heads/main",
        "RW_STARTED": STARTED,
        "RO_STARTED": STARTED,
        "NONE_STARTED": STARTED,
    }
    return run_bash(s["run"], cwd=tmp_path, env=env)


def test_check_action_save_passes_with_a_fresh_3_11_key(tmp_path):
    r = run_check_action_save(tmp_path, [cache("setup-uv-Linux-X64-python-3.11-abc", FRESH)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "read-write saved this attempt" in r.stdout


def test_check_action_save_fails_when_only_an_old_3_11_key_exists(tmp_path):
    # An entry from before this attempt started is a leftover, not this attempt's save.
    r = run_check_action_save(tmp_path, [cache("setup-uv-Linux-X64-python-3.11-abc", OLD)])
    assert r.returncode == 1
    assert "no setup-uv Action save for the read-write job" in r.stdout


@pytest.mark.parametrize("py", ["3.12", "3.13"])
def test_check_action_save_fails_when_a_restore_only_or_none_job_saved(tmp_path, py):
    r = run_check_action_save(
        tmp_path,
        [cache("setup-uv-Linux-X64-python-3.11-abc", FRESH), cache(f"setup-uv-Linux-X64-python-{py}-abc", FRESH)],
    )
    assert r.returncode == 1
    assert "a restore-only or none job saved a setup-uv cache this attempt" in r.stdout


def test_check_action_save_fails_on_a_pruned_key(tmp_path):
    r = run_check_action_save(
        tmp_path,
        [cache("setup-uv-Linux-X64-python-3.11-abc", FRESH), cache("setup-uv-Linux-X64-python-3.11-abc-pruned", FRESH)],
    )
    assert r.returncode == 1
    assert "a setup-uv Actions cache key is pruned" in r.stdout


def test_check_action_save_ignores_non_setup_uv_keys(tmp_path):
    # A fresh 3.12 key that is not a setup-uv key must not look like a restore-only save.
    r = run_check_action_save(
        tmp_path,
        [cache("setup-uv-Linux-X64-python-3.11-abc", FRESH), cache("some-other-cache-python-3.12", FRESH)],
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_action_save_passes_with_a_same_second_fractional_3_11_key(tmp_path):
    # created_at carries a fraction of the same second as the recorded time; it is
    # a save made in that second, so it must count, not sort as older.
    r = run_check_action_save(tmp_path, [cache("setup-uv-Linux-X64-python-3.11-abc", SAME_SECOND)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "read-write saved this attempt" in r.stdout


def test_check_action_save_fails_on_a_same_second_fractional_3_12_key(tmp_path):
    # A 3.12 save in the same second as the restore-only time is a restore-only save.
    r = run_check_action_save(
        tmp_path,
        [cache("setup-uv-Linux-X64-python-3.11-abc", FRESH), cache("setup-uv-Linux-X64-python-3.12-abc", SAME_SECOND)],
    )
    assert r.returncode == 1
    assert "a restore-only or none job saved a setup-uv cache this attempt" in r.stdout


@pytest.mark.parametrize("py", ["3.12", "3.13"])
def test_check_action_save_ignores_an_old_restore_only_or_none_key(tmp_path, py):
    # A 3.12/3.13 key from before its job time is a leftover, not this attempt's save.
    r = run_check_action_save(
        tmp_path,
        [cache("setup-uv-Linux-X64-python-3.11-abc", FRESH), cache(f"setup-uv-Linux-X64-python-{py}-abc", OLD)],
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "read-write saved this attempt" in r.stdout


def run_cache_mode_check(tmp_path: Path, caches: list[dict]) -> subprocess.CompletedProcess:
    path_env = fake_gh_on_path(tmp_path, caches=caches)
    s = run_step("cache-mode-read-blocks-write", "No Actions cache entry was made")
    key = "self-test-cache-mode-123-1"
    env = {**path_env, "GH_TOKEN": "fake", "REPO": REPO, "REF": "refs/heads/main", "KEY": key}
    return run_bash(s["run"], cwd=tmp_path, env=env)


def test_cache_mode_check_passes_when_the_key_is_absent(tmp_path):
    r = run_cache_mode_check(tmp_path, [cache("another-cache-key", FRESH)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no Actions cache entry made" in r.stdout


def test_cache_mode_check_fails_when_the_key_was_written(tmp_path):
    r = run_cache_mode_check(tmp_path, [cache("self-test-cache-mode-123-1", FRESH)])
    assert r.returncode == 1
    assert "an Actions cache entry was made despite cache-mode: read" in r.stdout


# --- The self-test workflow has the per-ref concurrency group ------------------


def test_self_test_workflow_has_a_per_ref_concurrency_group():
    wf = yaml.safe_load(SELF_TEST.read_text())
    conc = wf["concurrency"]
    assert conc["group"] == "self-test-python-actions-${{ github.ref }}"
    assert conc["cancel-in-progress"] is False
