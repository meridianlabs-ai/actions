"""Tests for pr-title-lint.yml.

The type list is read from the commit of the reusable workflow itself
(job.workflow_repository, job.workflow_sha), not from a ref the caller passes,
so it follows the SHA the caller pins. The extract step is lifted from the YAML
and run under bash against a temporary git repo: it fails unless the checkout
is that commit, since an empty ref makes actions/checkout fetch the default
branch.

Run with `python3 -m pytest` from the repo root (needs pytest, PyYAML, git and jq).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pr-title-lint.yml"
TYPES = ROOT / "conventional-commit-types.json"
REPO = "meridianlabs-ai/actions"


def steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["lint"]["steps"]


def step(name: str) -> dict:
    return next(s for s in steps() if s.get("name") == name)


def checkout(tmp_path: Path) -> str:
    """Make _meridian_actions a git repo holding the real type list; return its HEAD."""
    repo = tmp_path / "_meridian_actions"
    repo.mkdir()
    shutil.copy(TYPES, repo / TYPES.name)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "types"], check=True)
    return subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def run_extract(tmp_path: Path, sha: str) -> subprocess.CompletedProcess:
    # A step without `shell:` runs as `bash -e {0}`.
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-c", step("Extract allowed types")["run"]],
        cwd=tmp_path,
        env={**os.environ, "TYPES_REPO": REPO, "TYPES_SHA": sha, "GITHUB_OUTPUT": str(tmp_path / "out")},
        text=True,
        capture_output=True,
        check=False,
    )


def test_types_are_checked_out_from_the_workflow_commit():
    with_ = step("Fetch shared conventional-commit types")["with"]
    assert with_["repository"] == "${{ job.workflow_repository }}"
    assert with_["ref"] == "${{ job.workflow_sha }}"
    assert with_["persist-credentials"] is False
    assert "types-ref" not in yaml.safe_dump(steps())


def test_the_extract_step_takes_the_contexts_through_env():
    extract = step("Extract allowed types")
    assert extract["env"] == {
        "TYPES_REPO": "${{ job.workflow_repository }}",
        "TYPES_SHA": "${{ job.workflow_sha }}",
    }
    assert "${{" not in extract["run"]


def test_the_types_of_the_workflow_commit_are_output(tmp_path):
    head = checkout(tmp_path)
    result = run_extract(tmp_path, head)
    assert result.returncode == 0, result.stderr + result.stdout
    assert f"Reading types from {REPO}@{head}" in result.stdout
    expected = subprocess.run(["jq", "-r", ".[].type", str(TYPES)], check=True, capture_output=True, text=True).stdout
    assert "feat\nfix\n" in expected
    assert (tmp_path / "out").read_text() == f"types<<EOF\n{expected}EOF\n"


def test_a_checkout_of_another_commit_fails(tmp_path):
    checkout(tmp_path)
    result = run_extract(tmp_path, "0" * 40)
    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert not (tmp_path / "out").exists()


def test_an_empty_workflow_sha_fails(tmp_path):
    checkout(tmp_path)
    result = run_extract(tmp_path, "")
    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert not (tmp_path / "out").exists()
