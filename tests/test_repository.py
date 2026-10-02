"""Repository-wide checks.

The triage and ci-perf agent jobs mint their model credential through
Anthropic workload identity federation, and the model broker that held their
long-lived keys is gone (design/agent-jobs-workload-identity.md, step 5). The
two org secrets are deleted after that, and a workflow that still named one
would get an empty value rather than an error, so no workflow or action may
name either.

Run with `python3 -m pytest` from the repo root (needs pytest;
`.github/workflows/tests.yml` does the same in CI).
"""

from __future__ import annotations

from pathlib import Path

GITHUB = Path(__file__).resolve().parents[1] / ".github"
RETIRED_SECRETS = ("CI_PERF_ANTHROPIC_API_KEY", "TRIAGE_ANTHROPIC_API_KEY")


def test_no_workflow_or_action_names_a_retired_anthropic_key():
    files = [p for p in GITHUB.rglob("*") if p.is_file()]
    assert any(p.parent.name == "workflows" for p in files)  # the walk found the workflows
    named = [
        (str(p.relative_to(GITHUB.parent)), secret)
        for p in files
        for secret in RETIRED_SECRETS
        if secret in p.read_text(errors="replace")
    ]
    assert named == []
