"""Tests for .github/workflows/inspect-ai-ci-perf.yml's analyze job.

The job's trust boundary is that the agent never holds the Anthropic key:
the key reaches exactly one step, the model broker (.github/actions/
model-broker, tested in test_model_broker.py), which runs before
harden-runner takes sudo away; the agent step is pointed at the broker and
given its per-run token; and nothing that runs after the agent on that
runner holds a secret or decides whether the agent's output is published.
These are facts of the YAML, so this file asserts them from the YAML.

Run with `python3 -m pytest` from the repo root (needs pytest and PyYAML;
`.github/workflows/tests.yml` does the same in CI).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "inspect-ai-ci-perf.yml"
BROKER_ACTION = "./actions-repo/.github/actions/model-broker"
CHECK = "bash actions-repo/.github/actions/model-broker/check_isolation.sh"


def load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def analyze_steps() -> list[dict]:
    return load_workflow()["jobs"]["analyze"]["steps"]


def step(name: str) -> dict:
    for s in analyze_steps():
        if s.get("name") == name or s.get("id") == name:
            return s
    raise KeyError(name)


def secrets_in(obj) -> set[str]:
    return set(re.findall(r"secrets\.([A-Z_]+)", yaml.safe_dump(obj)))


def test_the_key_reaches_only_the_broker_which_starts_before_harden_runner():
    names = [s.get("name") for s in analyze_steps()]
    order = ["Check out the model broker", "Start the model broker", "Harden runner",
             "Check the model key is out of the agent's reach", "Analyze CI performance"]
    assert [n for n in names if n in order] == order
    for s in analyze_steps():
        if s.get("id") == "broker":
            assert secrets_in(s) == {"CI_PERF_ANTHROPIC_API_KEY"}
        else:
            assert "ANTHROPIC_API_KEY" not in yaml.safe_dump(s), s.get("name")
    broker = step("broker")
    assert broker["uses"] == BROKER_ACTION
    assert broker["with"]["api-key"] == "${{ secrets.CI_PERF_ANTHROPIC_API_KEY }}"
    checkout = step("Check out the model broker")
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {"path": "actions-repo", "sparse-checkout": ".github/actions/model-broker", "persist-credentials": False}


def test_harden_runner_blocks_egress_and_disables_sudo_and_containers():
    with_ = step("Harden runner")["with"]
    assert with_["egress-policy"] == "block"
    assert with_["disable-sudo-and-containers"] is True
    endpoints = with_["allowed-endpoints"].split()
    assert all(e.endswith(":443") for e in endpoints)
    assert "api.anthropic.com:443" in endpoints  # the broker's upstream


def test_the_isolation_check_runs_after_harden_runner_as_the_runner_user():
    check = step("Check the model key is out of the agent's reach")
    assert check["run"].strip() == CHECK
    assert "if" not in check and "env" not in check


def test_the_agent_is_pointed_at_the_broker_with_the_run_token():
    agent = step("analysis")
    assert agent["uses"].startswith("anthropics/claude-code-action@")
    assert agent["with"]["anthropic_api_key"] == "${{ steps.broker.outputs.token }}"
    assert agent["env"]["ANTHROPIC_BASE_URL"] == "${{ steps.broker.outputs.base-url }}"
    assert agent["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert secrets_in(agent) == set()


def test_nothing_after_the_agent_holds_a_secret_or_gates_publication():
    steps = analyze_steps()
    after = steps[[s.get("id") for s in steps].index("analysis") + 1:]
    assert [s["name"] for s in after] == ["Stop the model broker", "Show report", "Retain CI evidence outside Git"]
    for s in after:
        assert secrets_in(s) == set(), s["name"]
    # Publication is not conditioned on a check that ran on the agent's
    # runner: there is no reusable secret to screen for, and such a check
    # would be the agent's to defeat.
    assert step("Show report")["if"] == "always()"
    assert step("Retain CI evidence outside Git")["if"] == "always()"
    stop = step("Stop the model broker")
    assert stop["if"] == "always() && steps.broker.outcome == 'success'"
    assert stop["env"] == {"STOP_FILE": "${{ steps.broker.outputs.stop-file }}"}
    assert 'touch "$STOP_FILE"' in stop["run"]


def test_the_analyze_job_holds_read_permissions_and_no_marvin_identity():
    job = load_workflow()["jobs"]["analyze"]
    assert set(job["permissions"].values()) == {"read"}
    assert secrets_in(job) == {"CI_PERF_ANTHROPIC_API_KEY"}
    text = yaml.safe_dump(job)
    assert "MARVIN" not in text and "create-github-app-token" not in text and "steps.mint" not in text


def test_no_secret_input_or_step_output_expression_inside_a_run_script():
    for job_name, job in load_workflow()["jobs"].items():
        for s in job["steps"]:
            hit = re.search(r"\$\{\{\s*(inputs|steps|needs|secrets|github\.event)\b", s.get("run") or "")
            assert hit is None, f"{job_name} / {s.get('name')}: {hit.group(0)} inside run:"
