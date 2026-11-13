"""Tests for build-python-dist.yml.

The build runs the calling repo's build backend, so it holds only the read-only
job token, has no environment, no secret and no `id-token: write`, persists no
checkout credential and uses no Actions cache. It installs the build tools from
the caller's hashed requirements file and builds with them alone. The caller
publishes from its own job, which downloads this run's artifact by its ID.

Run with `python3 -m pytest` from the repo root (needs pytest and PyYAML).
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "build-python-dist.yml"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def build() -> dict:
    return workflow()["jobs"]["build"]


def steps_using(action: str) -> list[dict]:
    return [s for s in build()["steps"] if str(s.get("uses", "")).startswith(f"{action}@")]


# --- The build holds no publishing credential ---------------------------------------


def test_the_build_is_the_only_job():
    assert list(workflow()["jobs"]) == ["build"]


def test_the_workflow_token_is_read_only_and_no_job_widens_it():
    assert workflow()["permissions"] == {"contents": "read"}
    assert all("permissions" not in j for j in workflow()["jobs"].values())


def test_no_job_has_an_environment():
    assert all("environment" not in j for j in workflow()["jobs"].values())


def test_no_job_references_a_secret():
    assert "secrets." not in WORKFLOW.read_text()


def test_the_build_persists_no_credential_and_uses_no_actions_cache():
    assert build()["cache-mode"] == "none"
    checkouts = steps_using("actions/checkout")
    assert checkouts
    assert all(s["with"].get("persist-credentials") is False for s in checkouts)
    installs = [s for s in build()["steps"] if "install-python-dependencies@" in str(s.get("uses", ""))]
    assert [s["with"]["actions-cache"] for s in installs] == ["none"]


# --- The build -----------------------------------------------------------------------


def test_the_checkout_fetches_the_tags_and_lfs_follows_the_input():
    [checkout] = steps_using("actions/checkout")
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["lfs"] == "${{ inputs.lfs }}"


def test_the_build_tools_install_from_the_hashed_requirements_file():
    [install] = [s for s in build()["steps"] if "install-python-dependencies@" in str(s.get("uses", ""))]
    assert install["uses"].startswith("meridianlabs-ai/actions/install-python-dependencies@")
    assert install["with"]["versions"] == "hashed-requirements"
    assert install["with"]["requirements-file"] == "scripts/build-dist/requirements.txt"
    assert install["with"]["package-format"] == "wheels-only"
    assert install["with"]["python-version"] == "${{ inputs.python-version }}"
    assert install["with"]["environment-dir"] == "${{ runner.temp }}/build-venv"


def test_the_build_uses_only_the_installed_build_tools():
    [step] = [s for s in build()["steps"] if s.get("name") == "Build"]
    assert '"$RUNNER_TEMP/build-venv/bin/python" -m build --no-isolation' in step["run"]


def test_no_expression_reaches_a_run_script():
    for s in build()["steps"]:
        assert "${{" not in (s.get("run") or ""), s.get("name")


def test_the_upload_fails_on_no_files_and_a_rerun_replaces_it():
    [upload] = steps_using("actions/upload-artifact")
    assert upload["with"] == {
        "name": "dist",
        "path": "dist/",
        "if-no-files-found": "error",
        "overwrite": True,
    }


def test_the_artifact_id_output_comes_from_the_upload_step():
    [upload] = steps_using("actions/upload-artifact")
    assert build()["outputs"] == {"artifact-id": f"${{{{ steps.{upload['id']}.outputs.artifact-id }}}}"}
    outputs = workflow()["on"]["workflow_call"]["outputs"]
    assert list(outputs) == ["artifact-id"]
    assert outputs["artifact-id"]["value"] == "${{ jobs.build.outputs.artifact-id }}"


def test_the_inputs_are_python_version_and_lfs():
    inputs = workflow()["on"]["workflow_call"]["inputs"]
    assert list(inputs) == ["python-version", "lfs"]
    assert inputs["python-version"]["required"] is True
    assert (inputs["lfs"]["type"], inputs["lfs"]["default"]) == ("boolean", False)


def test_every_action_is_pinned_by_commit():
    for s in build()["steps"]:
        uses = s.get("uses")
        if uses:
            ref = uses.split("@", 1)[1]
            assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), uses


# --- The caller template in the header ----------------------------------------------


def caller_template() -> dict:
    header = WORKFLOW.read_text().split("# Caller:\n", 1)[1].split("\n\n", 1)[0]
    body = "\n".join(line.removeprefix("#").removeprefix("  ") for line in header.splitlines())
    return yaml.safe_load(body.replace("<the repo's own publish condition>", "true"))


def test_the_caller_template_publishes_only_the_artifact_this_run_built():
    jobs = caller_template()["jobs"]
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["build"]["uses"].startswith("meridianlabs-ai/actions/.github/workflows/build-python-dist.yml@")
    publish = jobs["publish"]
    assert publish["needs"] == "build"
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["environment"]["name"] == "pypi"
    assert [s["uses"].split("@")[0] for s in publish["steps"]] == ["actions/download-artifact", "pypa/gh-action-pypi-publish"]
    assert publish["steps"][0]["with"] == {"artifact-ids": "${{ needs.build.outputs.artifact-id }}", "path": "dist/"}
    assert all("run" not in s for s in publish["steps"])


def test_the_tests_workflow_runs_on_changes_to_this_workflow():
    # pull_request has no paths filter (test_repository.py), so only push lists it.
    on = yaml.safe_load(TESTS_WORKFLOW.read_text())["on"]
    assert ".github/workflows/build-python-dist.yml" in on["push"]["paths"]
