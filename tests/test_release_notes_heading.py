"""Tests for the "Rename the breaking changes heading" step of the reusable release workflows.

Release Please's changelog template hard-codes "### \u26a0 BREAKING CHANGES" and
rewrites the release PR on every update, so release-please-python.yml and
release-please-vscode.yml run the same step after it: rename the heading in
the open release PR's body (the GitHub Release notes are taken from it when
the PR is merged) and in the newest section of CHANGELOG.md on the release
branch, leaving everything else byte-identical and changing nothing when the
heading is absent or already renamed. The step is lifted from the YAML and
run under bash with a stand-in for gh on PATH that keeps the PR, the branch
and its CHANGELOG.md in a state file and records every call.

Run with `python3 -m pytest` from the repo root (needs pytest, PyYAML and jq;
`.github/workflows/tests.yml` does the same in CI).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = [ROOT / ".github" / "workflows" / f"release-please-{kind}.yml" for kind in ("python", "vscode")]
NAME = "Rename the breaking changes heading"
REPO = "meridianlabs-ai/inspect_scout"
BRANCH = "release-please--branches--main"
GENERATED = "### \u26a0 BREAKING CHANGES"
RENAMED = "### Breaking Changes"
HEAD = "c" * 40


def steps(workflow: Path) -> list[dict]:
    return yaml.safe_load(workflow.read_text())["jobs"]["release-please"]["steps"]


def step(workflow: Path = WORKFLOWS[0]) -> dict:
    return next(s for s in steps(workflow) if s.get("name") == NAME)


# --- Wiring -----------------------------------------------------------------------


def test_both_workflows_run_the_same_step_right_after_release_please():
    for workflow in WORKFLOWS:
        names = [s.get("id") or s.get("name") for s in steps(workflow)]
        assert names.index(NAME) == names.index("release") + 1, workflow
        assert step(workflow) == step(), workflow


def test_step_uses_the_app_token_and_release_pleases_target_branch():
    for workflow in WORKFLOWS:
        release = next(s for s in steps(workflow) if s.get("id") == "release")
        env = step(workflow)["env"]
        assert env["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}" == release["with"]["token"]
        assert env["TARGET_BRANCH"] == release["with"]["target-branch"]
        assert env["REPO"] == "${{ github.repository }}"


def test_step_runs_every_time_and_never_fails_the_job():
    # Unconditional: the release PR is looked up rather than taken from the
    # release step's outputs, which are empty when Release Please leaves the PR
    # as it was. A failure must not skip a release's publish.
    s = step()
    assert "if" not in s
    assert s["continue-on-error"] is True
    assert "${{" not in s["run"]


# --- Behavior -----------------------------------------------------------------------

FAKE_GH = r'''#!/usr/bin/env python3
"""Stand-in for gh: the release PR and branch live in $FAKE_GH_DIR/state.json."""
import base64, hashlib, json, os, pathlib, subprocess, sys

args = sys.argv[1:]
store = pathlib.Path(os.environ["FAKE_GH_DIR"])
state = json.loads((store / "state.json").read_text())
with (store / "calls.log").open("a") as log:
    log.write(json.dumps(args) + "\n")
repo = os.environ["REPO"]


def value(flag):
    return args[args.index(flag) + 1] if flag in args else None


def fail(message):
    sys.stderr.write(message + "\n")
    sys.exit(1)


def blob(changelog):
    data = changelog.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


jq = value("--jq")
if args[:2] == ["pr", "list"]:
    assert value("--repo") == repo and value("--state") == "open", args
    assert value("--label") == "autorelease: pending", args
    out = [pr for pr in state["prs"] if pr["baseRefName"] == value("--base")]
    data = json.dumps([{k: pr[k] for k in value("--json").split(",")} for pr in out])
elif args[:1] == ["api"] and value("--input"):
    if state.get("fail_writes"):
        fail("gh: Expected branch to point to x (HTTP 422)")
    payload = json.loads(pathlib.Path(value("--input")).read_text())
    if args[1] == "graphql":
        commit = payload["variables"]["input"]
        branch = commit["branch"]["branchName"]
        assert commit["branch"]["repositoryNameWithOwner"] == repo
        if commit["expectedHeadOid"] != state["branches"][branch]["head"]:
            fail("gh: Expected branch to point to a different commit")
        (change,) = commit["fileChanges"]["additions"]
        assert change["path"] == "CHANGELOG.md"
        state["branches"][branch]["changelog"] = base64.b64decode(change["contents"]).decode("utf-8")
        state["branches"][branch]["head"] = "%040x" % (len(state["commits"]) + 1)
        state["commits"].append(commit)
    else:
        assert args[1:4] == ["-X", "PATCH", args[3]] and args[3].startswith(f"repos/{repo}/pulls/"), args
        number = int(args[3].rsplit("/", 1)[1])
        next(pr for pr in state["prs"] if pr["number"] == number)["body"] = payload["body"]
        state["patches"].append(number)
    data = "{}"
elif args[:1] == ["api"]:
    path, _, query = args[1].partition("?")
    prefix = f"repos/{repo}/"
    assert path.startswith(prefix), args
    path = path[len(prefix):]
    if path.startswith("branches/"):
        data = json.dumps({"commit": {"sha": state["branches"][path[len("branches/"):]]["head"]}})
    elif path == "contents/CHANGELOG.md":
        assert value("-H") == "Accept: application/vnd.github.object", args
        sha = query.removeprefix("ref=")
        match = [b for b in state["branches"].values() if b["head"] == sha]
        if not match or match[0]["changelog"] is None:
            fail("gh: Not Found (HTTP 404)")
        data = json.dumps({"sha": blob(match[0]["changelog"]), "content": "", "encoding": "none"})
    elif path.startswith("git/blobs/"):
        found = [b["changelog"] for b in state["branches"].values() if b["changelog"] is not None and blob(b["changelog"]) == path[len("git/blobs/"):]]
        found or fail("gh: Not Found (HTTP 404)")
        encoded = base64.b64encode(found[0].encode("utf-8")).decode("ascii")
        # GitHub wraps a blob's base64 content at 60 characters.
        data = json.dumps({"content": "\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60)), "encoding": "base64"})
    else:
        fail(f"fake gh: unexpected call {args}")
else:
    fail(f"fake gh: unexpected call {args}")
(store / "state.json").write_text(json.dumps(state))
if jq is not None:
    data = subprocess.run(["jq", "-r", jq], input=data, text=True, capture_output=True, check=True).stdout
sys.stdout.write(data)
'''


def section(version: str, heading: str, previous: str) -> str:
    return (
        f"## [{version}](https://github.com/{REPO}/compare/{previous}...{version}) (2026-10-15)\n\n\n"
        f"{heading}\n\n"
        "* require Python 3.11 \u2014 see the \u26a0 note ([#62](x))\n\n"
        "### Features\n\n"
        "* add a scanner ([#60](x)) ([a11afc9](y))\n"
    )


def changelog(new: str, old: str = GENERATED) -> str:
    return "# Changelog\n\n" + section("0.4.50", new, "0.4.49") + "\n" + section("0.4.49", old, "0.4.48") + "\n## 0.4.1 (2025-11-02)\n\n- Older notes.\n"


def body(heading: str, version: str = "0.4.50") -> str:
    return (
        ":robot: I have created a release *beep* *boop*\n---\n\n\n"
        + section(version, heading, "0.4.49")
        + "\n---\nThis PR was generated with [Release Please](https://github.com/googleapis/release-please)."
    )


def pr(number: int = 7, heading: str = GENERATED, *, branch: str = BRANCH, base: str = "main", fork: bool = False) -> dict:
    return {"number": number, "body": body(heading), "headRefName": branch, "baseRefName": base, "isCrossRepository": fork}


class Run:
    def __init__(self, tmp_path: Path, prs: list[dict], branches: dict[str, str | None], *, target: str = "main", fail_writes: bool = False):
        self.store = tmp_path / "gh"
        self.store.mkdir()
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(FAKE_GH)
        (bindir / "gh").chmod(0o755)
        state = {
            "prs": prs,
            "branches": {name: {"head": HEAD, "changelog": text} for name, text in branches.items()},
            "commits": [],
            "patches": [],
            "fail_writes": fail_writes,
        }
        (self.store / "state.json").write_text(json.dumps(state))
        self.env = {"PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_GH_DIR": str(self.store), "GH_TOKEN": "token", "REPO": REPO, "TARGET_BRANCH": target}
        self.tmp_path = tmp_path

    def __call__(self) -> subprocess.CompletedProcess:
        # GitHub runs `run:` scripts with `bash --noprofile --norc -eo pipefail`.
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step()["run"]], cwd=self.tmp_path, env={**os.environ, **self.env}, text=True, capture_output=True, check=False)

    @property
    def state(self) -> dict:
        return json.loads((self.store / "state.json").read_text())

    def calls(self) -> list[list[str]]:
        log = self.store / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def writes(self) -> int:
        return len(self.state["commits"]) + len(self.state["patches"])


def ok(r: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert r.returncode == 0, r.stdout + r.stderr
    return r


def test_heading_is_renamed_in_the_body_and_the_newest_changelog_section(tmp_path):
    run = Run(tmp_path, [pr()], {BRANCH: changelog(GENERATED)})
    ok(run())
    state = run.state
    # Everything else is byte-identical, including an older section's heading.
    assert state["branches"][BRANCH]["changelog"] == changelog(RENAMED)
    assert state["prs"][0]["body"] == body(RENAMED)
    (commit,) = state["commits"]
    assert commit["expectedHeadOid"] == HEAD
    assert commit["message"]["headline"].startswith("chore: ")
    assert state["patches"] == [7]


def test_running_again_changes_nothing(tmp_path):
    run = Run(tmp_path, [pr()], {BRANCH: changelog(GENERATED)})
    ok(run())
    ok(run())
    assert run.writes() == 2


def test_release_without_breaking_changes_is_left_alone(tmp_path):
    run = Run(tmp_path, [pr(heading="### Bug Fixes")], {BRANCH: changelog("### Bug Fixes", "### Bug Fixes")})
    ok(run())
    assert run.writes() == 0
    assert run.state["branches"][BRANCH]["changelog"] == changelog("### Bug Fixes", "### Bug Fixes")


@pytest.mark.parametrize("line", [f"{GENERATED} (extra)", f"#{GENERATED}", f" {GENERATED}", "### \u26a0\ufe0f BREAKING CHANGES", "### BREAKING CHANGES", f"* mentions {GENERATED} inline"])
def test_only_the_exact_heading_line_is_renamed(line, tmp_path):
    run = Run(tmp_path, [pr(heading=line)], {BRANCH: changelog(line)})
    ok(run())
    assert run.writes() == 0


def test_crlf_body_keeps_its_line_endings(tmp_path):
    crlf = pr()
    crlf["body"] = body(GENERATED).replace("\n", "\r\n")
    run = Run(tmp_path, [crlf], {BRANCH: changelog(RENAMED)})
    ok(run())
    assert run.state["prs"][0]["body"] == body(RENAMED).replace("\n", "\r\n")
    assert run.state["commits"] == []


def test_no_open_release_pr_changes_nothing(tmp_path):
    run = Run(tmp_path, [], {BRANCH: changelog(GENERATED)})
    assert "No open release PR on main" in ok(run()).stdout
    assert [c[:2] for c in run.calls()] == [["pr", "list"]]


@pytest.mark.parametrize(
    "other",
    [
        pr(8, fork=True),
        pr(8, branch="feature/release-notes"),
        pr(8, branch="release-please--branches--mainline"),
        pr(8, branch="release-please--branches--hotfix/1.4.x", base="main"),
    ],
    ids=["fork", "other-branch", "prefix-only", "other-target"],
)
def test_prs_other_than_this_targets_release_pr_are_ignored(other, tmp_path):
    run = Run(tmp_path, [other], {other["headRefName"]: changelog(GENERATED)})
    ok(run())
    assert run.writes() == 0


def test_component_release_branch_and_hotfix_target_are_handled(tmp_path):
    # inspect_vscode and inspect-skills name a component in the branch; a
    # hotfix run targets its maintenance branch.
    component = "release-please--branches--hotfix/1.4.x--components--inspect-ai"
    run = Run(tmp_path, [pr(9, branch=component, base="hotfix/1.4.x")], {component: changelog(GENERATED)}, target="hotfix/1.4.x")
    ok(run())
    assert run.state["branches"][component]["changelog"] == changelog(RENAMED)
    assert run.state["patches"] == [9]


def test_branch_without_a_changelog_still_gets_its_body_renamed(tmp_path):
    run = Run(tmp_path, [pr()], {BRANCH: None})
    ok(run())
    assert run.state["commits"] == []
    assert run.state["prs"][0]["body"] == body(RENAMED)


def test_failed_write_fails_the_step_with_an_error_annotation(tmp_path):
    # e.g. Release Please pushed again after the branch head was read.
    run = Run(tmp_path, [pr()], {BRANCH: changelog(GENERATED)}, fail_writes=True)
    r = run()
    assert r.returncode == 1
    assert "::error::gh api graphql" in r.stdout
    assert run.state["prs"][0]["body"] == body(GENERATED)


def test_large_changelog_round_trips(tmp_path):
    # Over 1 MB the contents API returns no content, so the step reads the blob.
    big = changelog(GENERATED) + "".join(f"## 0.0.{i} (2020-01-01)\n\n- note \u00e9 {i}\n\n" for i in range(40000))
    assert len(big.encode("utf-8")) > 1_000_000
    run = Run(tmp_path, [pr()], {BRANCH: big})
    ok(run())
    assert run.state["branches"][BRANCH]["changelog"] == big.replace(GENERATED, RENAMED, 1)

