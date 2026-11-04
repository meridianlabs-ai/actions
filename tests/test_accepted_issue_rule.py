"""Tests for the sync-labels and policy-text-check composite actions.

The tests lift the run: script out of each action.yml and run it under bash:
sync-labels with a stand-in gh on PATH that records its argument vector,
policy-text-check in a directory that holds the caller's files. A static test
checks the shape of labels.json.

Run with `python3 -m pytest` from the repo root (needs pytest and PyYAML;
.github/workflows/tests.yml does the same in CI).
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
LABELS = json.loads((ROOT / "labels.json").read_text())
POLICY_FILES = ["CONTRIBUTING.md", "AGENTS.md"]


def run_action(action: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    (step,) = yaml.safe_load((ROOT / action / "action.yml").read_text())["runs"]["steps"]
    assert step["shell"] == "bash"
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        cwd=cwd,
        env={**os.environ, "GITHUB_ACTION_PATH": str(ROOT / action), **env},
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("label", LABELS, ids=lambda label: label.get("name"))
def test_label_shape(label):
    assert set(label) == {"name", "color", "description"}
    assert re.fullmatch(r"[0-9A-Fa-f]{6}", label["color"])
    assert label["name"] and label["description"]


# --- sync-labels -----------------------------------------------------------------

FAKE_GH = """#!/usr/bin/env bash
# Record gh's arguments as one JSON list per call.
python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@" >> "$FAKE_GH_CALLS"
"""


def test_sync_labels_creates_or_updates_each_label(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    calls = tmp_path / "gh_calls"
    r = run_action(
        "sync-labels",
        tmp_path,
        {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_GH_CALLS": str(calls), "GH_TOKEN": "t", "GH_REPO": "o/r"},
    )
    assert r.returncode == 0, r.stderr
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [
        ["label", "create", label["name"], "--color", label["color"], "--description", label["description"], "--force"]
        for label in LABELS
    ]


# --- policy-text-check -----------------------------------------------------------


def with_markers(name: str, text: str) -> str:
    return (
        "# Local intro\n\n"
        f"<!-- BEGIN shared text from meridianlabs-ai/actions policy/{name}. Change it there, not here. -->\n"
        f"{text}"
        f"<!-- END shared text from meridianlabs-ai/actions policy/{name} -->\n\n"
        "Local outro\n"
    )


def policy(name: str) -> str:
    return (ROOT / "policy" / name).read_text()


def changed_line(name: str) -> str:
    return with_markers(name, policy(name).replace("accepted", "approved", 1))


def no_markers(name: str) -> str:
    return policy(name)


@pytest.mark.parametrize(
    "broken, make_text, message",
    [
        ("AGENTS.md", changed_line, "differs from policy/AGENTS.md"),
        ("CONTRIBUTING.md", changed_line, "differs from policy/CONTRIBUTING.md"),
        ("AGENTS.md", no_markers, "AGENTS.md has no shared text markers"),
        ("CONTRIBUTING.md", lambda name: None, "CONTRIBUTING.md is missing"),
    ],
)
def test_policy_text_check_fails(tmp_path, broken, make_text, message):
    for name in POLICY_FILES:
        text = make_text(name) if name == broken else with_markers(name, policy(name))
        if text is not None:
            (tmp_path / name).write_text(text)
    r = run_action("policy-text-check", tmp_path, {})
    assert r.returncode == 1
    assert message in r.stdout
    assert "Copy the text" in r.stdout


def test_policy_text_check_passes_on_the_exact_text(tmp_path):
    for name in POLICY_FILES:
        (tmp_path / name).write_text(with_markers(name, policy(name)))
    r = run_action("policy-text-check", tmp_path, {})
    assert r.returncode == 0, r.stdout + r.stderr


def test_policy_text_check_reports_every_failure(tmp_path):
    r = run_action("policy-text-check", tmp_path, {})
    assert r.returncode == 1
    assert all(f"{name} is missing" in r.stdout for name in POLICY_FILES)
