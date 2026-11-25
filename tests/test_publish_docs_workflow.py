"""Tests for publish-docs.yml.

The docs site is published only from the tag of a published release (the
`tag` input when a caller passes one, else the run's ref), so the build job's
ref check is lifted from the YAML and run under bash with a stand-in for gh on
PATH, and the checkout renders the tag the check passed. The build job runs
the calling repo's code, so it holds only the read-only job token, persists no
checkout credential and uses no Actions cache; the deploy job, which holds
`pages: write` and `id-token: write`, runs no repo code.

Run with `python3 -m pytest` from the repo root (needs pytest, PyYAML and jq).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-docs.yml"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
REPO = "meridianlabs-ai/inspect_swe"
RUN_SHA = "a" * 40
TAG_SHA = "b" * 40


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def job(name: str) -> dict:
    return workflow()["jobs"][name]


def ref_check() -> dict:
    return job("build")["steps"][0]


class CheckResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    output: str


def run_check_ref(
    tmp_path: Path,
    ref: str,
    releases: dict[str, dict],
    gh_exit: int = 0,
    tag: str = "",
    commits: dict[str, str] | None = None,
    compare: str | None = "identical",
    failing: tuple[str, ...] = (),
) -> CheckResult:
    """Run the ref check for `ref` with a gh stand-in that serves `releases` (tag -> release JSON) and 404s anything else.

    `tag` is the workflow's `tag` input. With `gh_exit`, gh prints what it
    serves and then fails, as a lookup cut off midway does. `commits` maps a
    URL-encoded tag to the commit it points at (default: every release's tag
    at TAG_SHA), and `compare` is the status GitHub reports comparing TAG_SHA
    with the run's commit RUN_SHA (None: the comparison 404s). A release
    records TAG_SHA as its target_commitish unless its body says otherwise.
    gh prints what it serves for a path starting with one of `failing` and
    then fails. `output` is what the step wrote to $GITHUB_OUTPUT.
    """
    if commits is None:
        commits = {t: TAG_SHA for t in releases}
    paths: dict[str, dict] = {f"repos/{REPO}/releases/tags/{t}": {"target_commitish": TAG_SHA, **body} for t, body in releases.items()}
    paths.update({f"repos/{REPO}/commits/refs/tags/{t}": {"sha": sha} for t, sha in commits.items()})
    if compare is not None:
        paths[f"repos/{REPO}/compare/{TAG_SHA}...{RUN_SHA}"] = {"status": compare}
    served = tmp_path / "releases.json"
    served.write_text(json.dumps(paths))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    fail_cases = "".join(f'  "repos/{REPO}/{prefix}"*) exit 1 ;;\n' for prefix in failing)
    gh.write_text(
        "#!/usr/bin/env bash\n"
        '[ "$1" = api ] || exit 2\n'
        f'jq -e --arg p "$2" \'.[$p]\' "{served}" || {{ echo "gh: Not Found (HTTP 404)" >&2; exit 1; }}\n'
        f'case "$2" in\n{fail_cases}esac\n'
        f"exit {gh_exit}\n"
    )
    gh.chmod(0o755)
    output = tmp_path / "github_output"
    output.touch()
    script = ref_check()["run"]
    # A step with `shell: bash` runs as `bash --noprofile --norc -eo pipefail`;
    # test_the_ref_check_runs_under_pipefail checks the step asks for it.
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "REPO": REPO,
            "REF": ref,
            "TAG": tag,
            "RUN_SHA": RUN_SHA,
            "GITHUB_OUTPUT": str(output),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    return CheckResult(result.returncode, result.stdout, result.stderr, output.read_text())


# --- Only the tag of a published release publishes ---------------------------------


def test_the_tag_of_a_published_release_passes(tmp_path):
    result = run_check_ref(tmp_path, "refs/tags/0.2.72", {"0.2.72": {"tag_name": "0.2.72", "draft": False}})
    assert result.returncode == 0, result.stderr + result.stdout
    # The checkout's default ref and commit stay in use.
    assert result.output == ""


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/feature", "refs/pull/12/merge"])
def test_a_branch_is_refused(tmp_path, ref):
    result = run_check_ref(tmp_path, ref, {"0.2.72": {"tag_name": "0.2.72", "draft": False}})
    assert result.returncode == 1
    assert "run this from a release tag" in result.stdout
    assert result.output == ""


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


def test_a_failed_lookup_is_refused_even_if_it_printed_a_release(tmp_path):
    result = run_check_ref(tmp_path, "refs/tags/0.2.72", {"0.2.72": {"tag_name": "0.2.72", "draft": False}}, gh_exit=1)
    assert result.returncode == 1
    assert result.output == ""


# --- A release workflow on main passes the tag it released --------------------------


@pytest.mark.parametrize("compare", ["identical", "ahead"])
def test_the_tag_input_of_a_published_release_passes_as_its_commit(tmp_path, compare):
    # The checkout gets the commit the check resolved, not the tag name, so a
    # tag moved after the check (or before a re-run) changes nothing.
    result = run_check_ref(
        tmp_path, "refs/heads/main", {"0.2.72": {"tag_name": "0.2.72", "draft": False}}, tag="0.2.72", compare=compare
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.output == f"ref<<EOF\n{TAG_SHA}\nEOF\n"


@pytest.mark.parametrize("compare", ["behind", "diverged"])
def test_a_tag_input_whose_commit_is_not_in_the_runs_history_is_refused(tmp_path, compare):
    # The github-pages environment admits the run's branch, so only commits
    # that branch already holds may be rendered.
    result = run_check_ref(
        tmp_path, "refs/heads/main", {"0.2.72": {"tag_name": "0.2.72", "draft": False}}, tag="0.2.72", compare=compare
    )
    assert result.returncode == 1
    assert f"its commit {TAG_SHA} is not in this run's history ({RUN_SHA})" in result.stdout
    assert result.output == ""


@pytest.mark.parametrize("commits", [{"0.2.72": "c" * 40}, {"0.2.72": "main"}, {"0.2.72": "B" * 40}], ids=["moved", "name", "upper"])
def test_a_tag_input_that_does_not_point_at_its_releases_commit_is_refused(tmp_path, commits):
    # The release records the commit it was cut from; a tag moved since, even
    # to another commit in the run's history, would render different docs from
    # the package that was published.
    result = run_check_ref(
        tmp_path, "refs/heads/main", {"0.2.72": {"tag_name": "0.2.72", "draft": False}}, tag="0.2.72", commits=commits
    )
    assert result.returncode == 1
    assert f"the tag 0.2.72 does not point at its release's commit {TAG_SHA}" in result.stdout
    assert result.output == ""


@pytest.mark.parametrize("target", ["main", "", None, "b" * 39])
def test_a_tag_input_whose_release_records_no_commit_is_refused(tmp_path, target):
    result = run_check_ref(
        tmp_path, "refs/heads/main", {"0.2.72": {"tag_name": "0.2.72", "draft": False, "target_commitish": target}}, tag="0.2.72"
    )
    assert result.returncode == 1
    assert "the release of 0.2.72 records no commit" in result.stdout
    assert result.output == ""


@pytest.mark.parametrize(
    ("endpoint", "error"),
    [
        ("releases/", "no published release has this tag"),
        ("commits/", "could not look up the commit of the tag 0.2.72"),
        ("compare/", f"could not compare {TAG_SHA} with the run's commit {RUN_SHA}"),
    ],
)
def test_a_failed_lookup_is_refused_even_if_it_printed_an_answer(tmp_path, endpoint, error):
    result = run_check_ref(
        tmp_path, "refs/heads/main", {"0.2.72": {"tag_name": "0.2.72", "draft": False}}, tag="0.2.72", failing=(endpoint,)
    )
    assert result.returncode == 1
    assert error in result.stdout
    assert result.output == ""


def test_a_tag_input_whose_commit_lookup_404s_says_the_lookup_failed(tmp_path):
    result = run_check_ref(tmp_path, "refs/heads/main", {"0.2.72": {"tag_name": "0.2.72", "draft": False}}, tag="0.2.72", commits={})
    assert result.returncode == 1
    assert "could not look up the commit of the tag 0.2.72" in result.stdout


def test_a_tag_input_is_url_encoded_in_the_commit_lookup(tmp_path):
    result = run_check_ref(tmp_path, "refs/heads/main", {"a%2Fb": {"tag_name": "a/b", "draft": False}}, tag="a/b")
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.output == f"ref<<EOF\n{TAG_SHA}\nEOF\n"


def test_the_tag_input_wins_over_the_runs_tag(tmp_path):
    result = run_check_ref(
        tmp_path, "refs/tags/0.2.72", {"0.2.72": {"tag_name": "0.2.72", "draft": False}}, tag="0.2.73"
    )
    assert result.returncode == 1
    assert "no published release has this tag" in result.stdout
    assert result.output == ""


@pytest.mark.parametrize(
    "releases",
    [{}, {"0.2.73": {"tag_name": "0.2.73", "draft": True}}],
    ids=["no release", "draft release"],
)
def test_a_tag_input_without_a_published_release_is_refused(tmp_path, releases):
    result = run_check_ref(tmp_path, "refs/heads/main", releases, tag="0.2.73")
    assert result.returncode == 1
    assert result.output == ""


def test_the_checkout_renders_the_ref_the_check_passed():
    steps = job("build")["steps"]
    assert ref_check()["id"] == "ref"
    assert ref_check()["env"]["TAG"] == "${{ inputs.tag }}"
    assert ref_check()["env"]["RUN_SHA"] == "${{ github.sha }}"
    assert steps[1]["with"]["ref"] == "${{ steps.ref.outputs.ref }}"
    assert workflow()["on"]["workflow_call"]["inputs"]["tag"]["default"] == ""


def test_the_ref_check_runs_under_pipefail():
    # Without an explicit `shell: bash`, GitHub runs `bash -e {0}`, with no pipefail.
    assert ref_check()["shell"] == "bash"


def test_the_ref_check_runs_before_the_checkout_and_deploy_waits_for_the_build():
    steps = job("build")["steps"]
    assert ref_check()["name"] == "Allow only the tag of a published release"
    assert str(steps[1].get("uses", "")).startswith("actions/checkout@")
    assert job("deploy")["needs"] == "build"


def restore_tag() -> dict:
    return next(s for s in job("build")["steps"] if s.get("name") == "Restore the release tag")


def test_the_checked_out_commit_gets_its_release_tag_back():
    # Checking out a SHA fetches no tags, and a project versioned from git
    # tags (hatch-vcs) would install as a dev version.
    steps = job("build")["steps"]
    assert steps.index(restore_tag()) == 2
    assert restore_tag()["if"] == "${{ steps.ref.outputs.ref != '' }}"
    assert restore_tag()["env"] == {"TAG": "${{ inputs.tag }}", "SHA": "${{ steps.ref.outputs.ref }}"}


@pytest.mark.parametrize("tag", ["0.2.72", "v1.2.3", "inspect-flow-v0.17.0"])
def test_restoring_the_tag_makes_git_describe_name_the_release(tmp_path, tag):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "release"], cwd=tmp_path, check=True, env=env)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, env=env, capture_output=True, text=True).stdout.strip()
    result = subprocess.run(["bash", "-e", "-c", restore_tag()["run"]], cwd=tmp_path, env={**env, "TAG": tag, "SHA": sha}, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    described = subprocess.run(["git", "describe", "--tags", "--exact-match"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert described.stdout.strip() == tag


# --- Repo code runs only with the read-only job token --------------------------------


def test_only_the_deploy_job_widens_the_read_only_token():
    wf = workflow()
    assert wf["permissions"] == {"contents": "read"}
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
    assert ".github/workflows/publish-docs.yml" in on["push"]["paths"]
