"""The agent stubs: claude.yml, claude-review.yml and claude-auto.yml.

Each stub is a copy of the matching file in meridianlabs-ai/agents
`examples/`, calling that repo's reusable workflows, plus this repo's
recorded deviations. The YAML is the only place these facts live, so this
file asserts the ones a resync must not lose: the machine account reaches the
reusable workflows as the GitHub App's two secrets, named one by one (never
`secrets: inherit`, never the retired PAT); the reviewer runs on demand only
(decision: Ransom, 2026-09-14, actions#112); and the `@auto` loop's
`workflow_run` trigger names CI workflows that exist here and run on pull
requests, since a name that matches nothing fires nothing; and the `auto`
label kickoff admits no bot and not the machine account (Claude Security
finding 4628345).

Run with `python3 -m pytest` from the repo root (needs pytest and PyYAML;
`.github/workflows/tests.yml` does the same in CI).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
STUBS = ["claude.yml", "claude-review.yml", "claude-auto.yml"]
APP_SECRETS = {"MARVIN_APP_CLIENT_ID", "MARVIN_APP_PRIVATE_KEY"}


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def triggers(workflow: dict) -> dict:
    # PyYAML reads a bare `on:` key as the boolean True.
    return workflow.get("on", workflow.get(True))


@pytest.mark.parametrize("stub", STUBS)
def test_every_job_passes_the_app_secrets_one_by_one_and_not_the_pat(stub):
    for name, job in load(stub)["jobs"].items():
        assert job["uses"].startswith("meridianlabs-ai/agents/.github/workflows/"), (name, job["uses"])
        secrets = job["secrets"]
        assert isinstance(secrets, dict), (name, secrets)
        assert APP_SECRETS <= secrets.keys(), (name, sorted(secrets))
        assert "MARVIN_TOKEN" not in secrets, name
        for key, value in secrets.items():
            assert value == "${{ secrets.%s }}" % key, (name, key, value)


def test_the_reviewer_runs_on_demand_only():
    assert triggers(load("claude-review.yml")) == {"issue_comment": {"types": ["created"]}}


def test_the_auto_label_kickoff_admits_no_bot_and_not_the_machine_account():
    # Claude Security finding 4628345: the `claude-auto` job's label path
    # admitted `meridian-marvin[bot]` so a triage-applied `auto` label (an
    # untrusted agent's decision, written by the machine account) started the
    # dev agent. The label path now carries the same machine-account guard as
    # every other path, under both logins; the reusable workflow's trigger
    # check is the authority and refuses them too. A human's label is the
    # only kickoff.
    job = load("claude.yml")["jobs"]["claude-auto"]
    cond = " ".join(job["if"].split())
    assert "meridian-marvin[bot]" not in cond
    assert ("github.event.label.name == 'auto' && github.actor != 'i-am-marvin' "
            "&& !endsWith(github.actor, '[bot]') )") in cond
    text = (WORKFLOWS / "claude.yml").read_text()
    assert "meridian-marvin[bot]'" not in text
    assert text.count("github.actor != 'i-am-marvin'") == 4  # the guard's comment, and three guards


def test_the_auto_loop_reacts_to_ci_workflows_that_run_on_pull_requests_here():
    named = triggers(load("claude-auto.yml"))["workflow_run"]["workflows"]
    pr_ci = {
        wf["name"]
        for path in WORKFLOWS.glob("*.yml")
        if path.name not in STUBS
        for wf in [yaml.safe_load(path.read_text())]
        if "pull_request" in triggers(wf)
    }
    assert named, "workflow_run.workflows is empty"
    assert set(named) <= pr_ci, (named, sorted(pr_ci))
