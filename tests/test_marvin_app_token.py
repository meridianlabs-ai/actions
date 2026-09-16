"""The marvin identity in inspect-ai-ci-perf.yml and triage-test-failures.yml.

Each workflow is an untrusted agent job (analyze, agent) followed by a
trusted job on a fresh runner (publish, land) that is the only one to write
as marvin: fork issues and comments, assignees, and Atlas items, all on
meridianlabs-ai/inspect_ai. Phase 2 of the credential separation has the
trusted job mint a one-hour installation token of the meridian-marvin GitHub
App for exactly that repository and those permissions, falling back to the
MARVIN_TOKEN PAT while the app secrets roll out. The YAML is the only place
these facts live, so this file asserts them: the app secrets, the minted
token and the PAT appear in the trusted job only; the mint step is scoped to
the fork with issues and organization-projects write and runs only when both
secrets are configured; and every read of the identity is the one fallback
expression, after the mint step, never inside a `run:` script.

Run with `python3 -m pytest` from the repo root (needs pytest and PyYAML;
`.github/workflows/tests.yml` does the same in CI).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_PERF = ROOT / ".github" / "workflows" / "inspect-ai-ci-perf.yml"
TRIAGE = ROOT / ".github" / "workflows" / "triage-test-failures.yml"

# Anything that is, or yields, a marvin identity.
MARVIN = re.compile(r"secrets\.MARVIN_[A-Z_]+|steps\.mint\b|create-github-app-token")
TOKEN = "${{ steps.mint.outputs.token || secrets.MARVIN_TOKEN }}"
CONFIGURED = "${{ secrets.MARVIN_APP_CLIENT_ID != '' && secrets.MARVIN_APP_PRIVATE_KEY != '' }}"

CASES = [
    pytest.param(CI_PERF, "publish", id="ci-perf"),
    pytest.param(TRIAGE, "land", id="triage"),
]


def load(workflow: Path) -> dict:
    return yaml.safe_load(workflow.read_text())


def mint_index(steps: list[dict]) -> int:
    (index,) = [i for i, s in enumerate(steps) if s.get("id") == "mint"]
    return index


@pytest.mark.parametrize("workflow, trusted", CASES)
def test_the_marvin_identity_appears_in_the_trusted_job_only(workflow, trusted):
    wf = load(workflow)
    outside_jobs = yaml.safe_dump({k: v for k, v in wf.items() if k != "jobs"})
    assert MARVIN.search(outside_jobs) is None, MARVIN.findall(outside_jobs)
    for name, job in wf["jobs"].items():
        hits = MARVIN.findall(yaml.safe_dump(job))
        if name == trusted:
            assert set(hits) >= {"secrets.MARVIN_TOKEN", "secrets.MARVIN_APP_CLIENT_ID", "secrets.MARVIN_APP_PRIVATE_KEY", "steps.mint", "create-github-app-token"}, hits
        else:
            assert hits == [], f"{workflow.name} {name}: {hits}"


@pytest.mark.parametrize("workflow, trusted", CASES)
def test_the_mint_step_is_scoped_to_fork_issues_and_atlas(workflow, trusted):
    job = load(workflow)["jobs"][trusted]
    mint = job["steps"][mint_index(job["steps"])]
    assert mint["uses"] == "actions/create-github-app-token@v3"
    assert mint["with"] == {
        "client-id": "${{ secrets.MARVIN_APP_CLIENT_ID }}",
        "private-key": "${{ secrets.MARVIN_APP_PRIVATE_KEY }}",
        "owner": "meridianlabs-ai",
        "repositories": "inspect_ai",
        "permission-issues": "write",
        "permission-organization-projects": "write",
    }
    # Mint only when both app secrets are configured, else the PAT fallback
    # below carries the run; a step-level `if` cannot read secrets, so the
    # job's env answers the question.
    assert mint["if"] == "env.MARVIN_APP_CONFIGURED == 'true'"
    assert job["env"]["MARVIN_APP_CONFIGURED"] == CONFIGURED
    assert "skip-token-revoke" not in mint["with"]


@pytest.mark.parametrize("workflow, trusted", CASES)
def test_every_read_of_the_identity_is_the_fallback_expression_after_the_mint_step(workflow, trusted):
    job = load(workflow)["jobs"][trusted]
    steps = job["steps"]
    mint = mint_index(steps)
    reads = 0
    for i, s in enumerate(steps):
        if i == mint:
            continue
        assert "${{ secrets." not in (s.get("run") or "") and "steps.mint" not in (s.get("run") or ""), s.get("name")
        for expression in re.findall(r"\$\{\{[^}]*\}\}", yaml.safe_dump(s)):
            if MARVIN.search(expression) is None:
                continue
            assert expression == TOKEN, f"{workflow.name} {trusted} / {s.get('name')}: {expression}"
            assert i > mint, f"{s.get('name')} reads the token before the mint step"
            reads += 1
    assert reads >= 1
    # The app secrets themselves are read by the mint step and the job env only.
    for i, s in enumerate(steps):
        if i != mint:
            assert "MARVIN_APP_" not in yaml.safe_dump(s), s.get("name")
