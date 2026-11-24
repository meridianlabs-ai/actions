"""Tests for the Slack approval pings' use of slack-ids.json.

Three places turn approvers' GitHub logins into Slack mentions through
slack-ids.json: the release-please job of release-please-python.yml and of
release-please-vscode.yml, and the slack-approval-ping composite action. The
two workflows fetch the map from their own commit (job.workflow_sha), so a
change to it on actions main reaches a caller only when the caller's pin
moves, and they mention no one if the checkout is any other commit. The
composite reads the map next to itself, which is already the pinned commit.
In all three, only values shaped like a Slack member ID become mentions, so
a map value can't add other Slack markup to the message.

The notify scripts are lifted from the YAML and run under bash with a curl
stand-in that records the payload. Run with `python3 -m pytest` from the repo
root; git, jq and bash must be on PATH.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {
    "python": ROOT / ".github" / "workflows" / "release-please-python.yml",
    "vscode": ROOT / ".github" / "workflows" / "release-please-vscode.yml",
}
ACTION = ROOT / "slack-approval-ping" / "action.yml"
FETCH = "Fetch Slack ID map"
NOTIFY = "Notify Slack — release awaiting publish approval"
SLACK_ID = "U012AB3CD"

FAKE_CURL = f'''#!{sys.executable}
"""Stand-in for curl: records the --data payload in $FAKE_CURL_LOG."""
import os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_CURL_LOG"], "w") as log:
    log.write(args[args.index("--data") + 1])
'''

# Map values that are not Slack member IDs. Each would otherwise be wrapped in
# <@...> and could add a channel-wide mention or a labelled link to the ping.
NOT_IDS = ["TODO", "", "!channel", "U012AB3CD> <!channel", "U012AB3CD|Review", "<https://example.com|Review>", "u012ab3cd", "U012AB3CD\nx", 42, {"id": SLACK_ID}]


def release_please_step(kind: str, name: str) -> dict:
    for s in yaml.safe_load(WORKFLOWS[kind].read_text())["jobs"]["release-please"]["steps"]:
        if s.get("name") == name:
            return s
    raise KeyError(name)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def write_map(directory: Path, ids: dict) -> str:
    """Write slack-ids.json as the only file of a one-commit repo and return its commit."""
    directory.mkdir()
    (directory / "slack-ids.json").write_text(json.dumps({"_comment": "GitHub login -> Slack member ID", **ids}))
    git(directory, "init", "-q")
    git(directory, "add", "slack-ids.json")
    git(directory, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "map")
    return git(directory, "rev-parse", "HEAD")


def run_script(script: str, cwd: Path, env: dict) -> tuple[subprocess.CompletedProcess, str | None]:
    bin_dir = cwd / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(FAKE_CURL)
    (bin_dir / "curl").chmod(0o755)
    log = cwd / "curl.log"
    full_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(cwd), "FAKE_CURL_LOG": str(log), **env}
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=cwd, env=full_env, capture_output=True, text=True)
    text = json.loads(log.read_text())["text"] if log.exists() else None
    return result, text


def ping_workflow(kind: str, tmp_path: Path, ids: dict, approvers: str, *, map_sha: str | None = None) -> str:
    head = write_map(tmp_path / "_meridian_actions", ids)
    env = {
        "SLACK_APPROVALS_WEBHOOK_URL": "https://hooks.example/x",
        "APPROVERS": approvers,
        "MAP_SHA": head if map_sha is None else map_sha,
        "REPO": "meridianlabs-ai/example",
        "TAG": "v1.2.3",
        "SHA": "a" * 40,
        "RUN_URL": "https://github.com/run",
        "ACTIONS_URL": "https://github.com/run",
    }
    result, text = run_script(release_please_step(kind, NOTIFY)["run"], tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert text is not None
    return text


def ping_action(tmp_path: Path, ids: dict, approvers: str) -> str:
    action_path = tmp_path / "actions" / "slack-approval-ping"
    action_path.parent.mkdir()
    write_map(action_path.parent / "map", ids)
    (action_path.parent / "map" / "slack-ids.json").rename(action_path.parent / "slack-ids.json")
    action_path.mkdir()
    [step] = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    env = {"WEBHOOK": "https://hooks.example/x", "APPROVERS": approvers, "MESSAGE": ":rocket: release", "RUN_URL": "https://github.com/run", "ACTION_PATH": str(action_path)}
    result, text = run_script(step["run"], tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert text is not None
    return text


@pytest.mark.parametrize("kind", sorted(WORKFLOWS))
def test_map_is_fetched_from_the_workflows_own_commit(kind):
    fetch = release_please_step(kind, FETCH)
    assert fetch["with"]["repository"] == "${{ job.workflow_repository }}"
    assert fetch["with"]["ref"] == "${{ job.workflow_sha }}"
    assert fetch["with"]["persist-credentials"] is False
    assert fetch["with"]["sparse-checkout"] == "slack-ids.json"
    assert release_please_step(kind, NOTIFY)["env"]["MAP_SHA"] == "${{ job.workflow_sha }}"


@pytest.mark.parametrize("kind", sorted(WORKFLOWS))
def test_workflow_ping_mentions_approvers_with_slack_ids(kind, tmp_path):
    text = ping_workflow(kind, tmp_path, {"alice": SLACK_ID, "bob": "W0987ZYX"}, "alice, bob carol")
    assert f"<@{SLACK_ID}> <@W0987ZYX>" in text


@pytest.mark.parametrize("kind", sorted(WORKFLOWS))
def test_workflow_ping_mentions_no_one_from_another_commit(kind, tmp_path):
    # An empty ref makes actions/checkout fetch the default branch, so a map
    # at any commit but the workflow's own is not used.
    text = ping_workflow(kind, tmp_path, {"alice": SLACK_ID}, "alice", map_sha="b" * 40)
    assert "<@" not in text
    assert "awaiting approval" in text


@pytest.mark.parametrize("kind", sorted(WORKFLOWS))
@pytest.mark.parametrize("value", NOT_IDS)
def test_workflow_ping_drops_values_that_are_not_slack_ids(kind, value, tmp_path):
    text = ping_workflow(kind, tmp_path, {"alice": value, "bob": SLACK_ID}, "alice bob")
    assert text.count("<") == 1 and f"<@{SLACK_ID}>" in text
    assert "!channel" not in text and "Review>" not in text


@pytest.mark.parametrize("value", NOT_IDS)
def test_action_ping_drops_values_that_are_not_slack_ids(value, tmp_path):
    text = ping_action(tmp_path, {"alice": value, "bob": SLACK_ID}, "alice bob")
    assert text.count("<") == 1 and f"<@{SLACK_ID}>" in text
    assert "!channel" not in text and "Review>" not in text


def test_action_ping_mentions_approvers_with_slack_ids(tmp_path):
    text = ping_action(tmp_path, {"alice": SLACK_ID}, "alice")
    assert f":rocket: release <@{SLACK_ID}> Review: https://github.com/run" == text


def test_every_entry_in_slack_ids_json_is_a_slack_id():
    ids = json.loads((ROOT / "slack-ids.json").read_text())
    bad = {login: value for login, value in ids.items() if not login.startswith("_") and not (value == "TODO" or isinstance(value, str) and re.fullmatch(r"[UW][A-Z0-9]+", value))}
    assert bad == {}
