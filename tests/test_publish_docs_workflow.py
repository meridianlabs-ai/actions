"""Tests for publish-docs.yml.

The docs site is published only from the tag of a published release, so the
check-ref script is lifted from the YAML and run under bash with a stand-in
for gh on PATH. The build job runs the calling repo's code, so it holds only
the read-only job token, persists no checkout credential and uses no Actions
cache; the deploy job, which holds `pages: write` and `id-token: write`, runs
no repo code.

Run with `python3 -m pytest` from the repo root (needs pytest, PyYAML and jq).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-docs.yml"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
REPO = "meridianlabs-ai/inspect_swe"


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def job(name: str) -> dict:
    return workflow()["jobs"][name]


def run_check_ref(tmp_path: Path, ref: str, releases: dict[str, dict]) -> subprocess.CompletedProcess:
    """Run check-ref for `ref` with a gh stand-in that serves `releases` (tag -> release JSON) and 404s anything else."""
    served = tmp_path / "releases.json"
    served.write_text(json.dumps({f"repos/{REPO}/releases/tags/{tag}": body for tag, body in releases.items()}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        '[ "$1" = api ] || exit 2\n'
        f'jq -e --arg p "$2" \'.[$p]\' "{served}" || {{ echo "gh: Not Found (HTTP 404)" >&2; exit 1; }}\n'
    )
    gh.chmod(0o755)
    script = job("check-ref")["steps"][0]["run"]
    # A step with `shell: bash` runs as `bash --noprofile --norc -eo pipefail`.
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "REPO": REPO, "REF": ref},
        text=True,
        capture_output=True,
        check=False,
    )


# --- Only the tag of a published release publishes ---------------------------------


def test_the_tag_of_a_published_release_passes(tmp_path):
    result = run_check_ref(tmp_path, "refs/tags/0.2.72", {"0.2.72": {"tag_name": "0.2.72", "draft": False}})
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/feature", "refs/pull/12/merge"])
def test_a_branch_is_refused(tmp_path, ref):
    result = run_check_ref(tmp_path, ref, {"0.2.72": {"tag_name": "0.2.72", "draft": False}})
    assert result.returncode == 1
    assert "run this from a release tag" in result.stdout


def test_a_tag_with_no_release_is_refused(tmp_path):
    result = run_check_ref(tmp_path, "refs/tags/0.2.73", {"0.2.72": {"tag_name": "0.2.72", "draft": False}})
    assert result.returncode == 1
    assert "no published release has this tag" in result.stdout


def test_a_draft_release_is_refused(tmp_path):
    result = run_check_ref(tmp_path, "refs/tags/0.2.73", {"0.2.73": {"tag_name": "0.2.73", "draft": True}})
    assert result.returncode == 1


def test_a_release_for_another_tag_is_refused(tmp_path):
    result = run_check_ref(tmp_path, "refs/tags/0.2.73", {"0.2.73": {"tag_name": "0.2.72", "draft": False}})
    assert result.returncode == 1


def test_the_tag_is_url_encoded_before_it_reaches_the_api(tmp_path):
    result = run_check_ref(tmp_path, "refs/tags/a/b", {"a%2Fb": {"tag_name": "a/b", "draft": False}})
    assert result.returncode == 0, result.stderr + result.stdout


def test_build_waits_for_the_ref_check_and_deploy_for_the_build():
    assert job("build")["needs"] == "check-ref"
    assert job("deploy")["needs"] == "build"


# --- Repo code runs only with the read-only job token --------------------------------


def test_only_the_deploy_job_widens_the_read_only_token():
    wf = workflow()
    assert wf["permissions"] == {"contents": "read"}
    assert "permissions" not in job("check-ref")
    assert "permissions" not in job("build")
    assert job("deploy")["permissions"] == {"pages": "write", "id-token": "write"}


def test_the_build_persists_no_credential_and_uses_no_actions_cache():
    build = job("build")
    assert build["cache-mode"] == "none"
    checkouts = [s for s in build["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts
    assert all(s["with"].get("persist-credentials") is False for s in checkouts)
    installs = [s for s in build["steps"] if "install-python-dependencies@" in str(s.get("uses", ""))]
    assert [s["with"]["versions"] for s in installs] == ["locked"]
    assert [s["with"]["actions-cache"] for s in installs] == ["none"]


def test_the_deploy_job_runs_nothing_but_deploy_pages():
    steps = job("deploy")["steps"]
    assert [str(s.get("uses", "")).split("@")[0] for s in steps] == ["actions/deploy-pages"]
    assert all("run" not in s for s in steps)


def test_no_job_references_a_secret():
    assert "secrets." not in WORKFLOW.read_text()


def test_no_expression_reaches_a_run_script():
    for name, j in workflow()["jobs"].items():
        for s in j["steps"]:
            assert "${{" not in (s.get("run") or ""), (name, s.get("name"))


def test_every_action_is_pinned_by_commit():
    for name, j in workflow()["jobs"].items():
        for s in j["steps"]:
            uses = s.get("uses")
            if uses:
                ref = uses.split("@", 1)[1]
                assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), (name, uses)


def test_the_tests_workflow_runs_on_changes_to_this_workflow():
    on = yaml.safe_load(TESTS_WORKFLOW.read_text())["on"]
    for event in ("push", "pull_request"):
        assert ".github/workflows/publish-docs.yml" in on[event]["paths"], event
