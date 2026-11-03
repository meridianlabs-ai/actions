"""Repository-wide checks.

The triage and ci-perf agent jobs mint their model credential through
Anthropic workload identity federation, and the model broker that held their
long-lived keys is gone (design/agent-jobs-workload-identity.md, step 5). The
two org secrets are deleted after that, and a workflow that still named one
would get an empty value rather than an error, so no workflow or action may
name either.

An App installation token gets every permission the App holds unless the
step that mints it names fewer, so every mint step names its permissions.
The release steps ask for the Meridian Release Bot's two write permissions.

Run with `python3 -m pytest` from the repo root (needs pytest;
`.github/workflows/tests.yml` does the same in CI).
"""

from __future__ import annotations

from pathlib import Path

import yaml

GITHUB = Path(__file__).resolve().parents[1] / ".github"
RETIRED_SECRETS = ("CI_PERF_ANTHROPIC_API_KEY", "TRIAGE_ANTHROPIC_API_KEY")
APP_TOKEN = "actions/create-github-app-token@"
RELEASE_WORKFLOWS = ("release-please-python.yml", "release-please-vscode.yml")


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


def app_token_steps():
    """(file, step) for every create-github-app-token step in a workflow or action."""
    found = []
    for path in sorted([*GITHUB.glob("workflows/*.yml"), *GITHUB.parent.glob("*/action.yml")]):
        doc = yaml.safe_load(path.read_text())
        jobs = doc.get("jobs", {}).values() if "jobs" in doc else [doc.get("runs", {})]
        for job in jobs:
            for step in job.get("steps") or []:
                if step.get("uses", "").startswith(APP_TOKEN):
                    found.append((path.name, step))
    return found


def test_every_app_token_names_its_permissions():
    steps = app_token_steps()
    assert {name for name, _ in steps} >= set(RELEASE_WORKFLOWS)  # the walk found them
    for name, step in steps:
        permissions = [key for key in step["with"] if key.startswith("permission-")]
        assert permissions, f"{name}: name the permissions the token needs"


def test_release_tokens_ask_for_the_release_apps_permissions():
    for name, step in app_token_steps():
        if name in RELEASE_WORKFLOWS:
            scoped = {k: v for k, v in step["with"].items() if k.startswith("permission-")}
            assert scoped == {"permission-contents": "write", "permission-pull-requests": "write"}, name
