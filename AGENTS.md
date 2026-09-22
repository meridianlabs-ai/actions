# AGENTS.md — actions

Instructions for any agent working in this repository. Tool-agnostic; an
agent's own entry file (`CLAUDE.md`) imports this rather than duplicating
it.

## What this repo is

`meridianlabs-ai/actions` holds the GitHub Actions shared by the Meridian
repos: reusable release workflows and composite actions (`README.md`,
`RELEASING.md`), the scheduled test runs of `inspect_ai` and `inspect_swe`
and the triage of their failures (`INSPECT_AI_TESTS.md`), and the ci-perf
run. The workflows are the product; there is no application code.

## The workflow scripts are tested

The `run:` blocks and permission rules in `.github/workflows/*.yml` are
tested in `tests/`: each test file lifts the scripts out of the YAML and
executes them under bash, with stand-ins for `gh` and `pytest` on `PATH`
where the script calls them (`tests/test_triage_workflow.py`,
`tests/test_scheduled_workflows.py`, `tests/test_ci_perf_workflow.py`;
`tests/test_slack_release_announce.py` covers the announce converter;
`tests/test_model_broker.py` covers the model broker of
`.github/actions/model-broker` in-process and, when a Docker daemon is
available, the whole `.github/actions/isolated-agent` lifecycle — the agent
user's isolation from the broker and from a runner/.NET-diagnostic sentinel,
the bootstrap's reach into a workspace under the runner's private home, and
the `env -i` launch — end to end in an Ubuntu container laid out like a
hosted runner; and, when the suite itself runs on a GitHub-hosted runner,
the same three step scripts in place against that runner's real layout,
Runner.Worker, Yama and command files). Both actions live under the
`.github/actions/**` path `tests.yml` lists.
`.github/workflows/tests.yml` runs them in CI on every push and pull request
that touches a path it lists.

So a change to a workflow script is not done until:

- its test is added or updated in the matching `tests/` file (a task that
  says "keep the diff to the workflow file" is about churn elsewhere, not a
  reason to leave new logic untested);
- the workflow's path is in the `paths:` lists of `tests.yml` if it is new;
- the checks below pass.

The permission-rule tests match commands against the agent's allow and deny
lists with an approximation of Claude Code's Bash-pattern matching. They
show that no rule shape grants a listed write vector; they do not model the
installed CLI's decision, which also involves its command parser, its
separate check of redirect targets against the file rules, protected paths
and the effective settings. An `allow` from that helper is not proof that
Claude Code runs the command, and a redirect the helper passes still
requires the CLI's separate file-write check; `SECURITY.md` → "Verification notes" records the checks of
the real permission engine and when to repeat them.

## Checks

```
pip install pytest pyyaml        # or a venv; the repo has no lock file
python3 -m pytest -q tests       # the broker's container tests need Docker; MODEL_BROKER_SKIP_DOCKER=1 skips them
actionlint .github/workflows/<changed>.yml
python3 -c 'import yaml,sys; yaml.safe_load(open(sys.argv[1]))' .github/workflows/<changed>.yml
```

`actionlint` over the whole repo reports pre-existing findings in
`inspect-ai-ci-perf.yml`, `inspect-ai-scheduled-tests.yml` and
`inspect-swe-nightly-tests.yml`; a change is held to its own file being
clean.

## Rules the tests enforce

- Untrusted text is never parsed as syntax: `workflow_dispatch` inputs,
  artifacts from earlier runs, test output and agent-written files reach
  bash through `env:` or files, are validated against a shape (a 40-hex
  SHA, a Slack channel ID) before they become a ref, an output or a
  destination, and step outputs are written with heredoc delimiters.
- A job that runs an agent over untrusted input holds only the job token;
  the model key is held by the model broker (`.github/actions/model-broker`,
  a separate Unix user) and the whole Claude process runs as a third,
  unprivileged user through `.github/actions/isolated-agent` with only the
  broker's per-run loopback token, so the key is not in the agent's
  environment, files or reachable processes (not the broker's, not the
  runner's .NET worker). harden-runner keeps sudo here on purpose: its
  hardening is a `pre` hook that runs before the bootstrap that needs sudo,
  so the agent's powerlessness comes from its user, not from disabling sudo.
  Writes to issues and Slack happen in a separate job from a validated
  manifest (see the headers of `triage-test-failures.yml` and
  `inspect-ai-ci-perf.yml`). Do not widen an agent job's permissions or allow
  list without a test for the write vector it closes, and put no secret in a
  step that runs after the agent.
- A job that installs and runs the day's dependency closure (the scheduled
  and nightly test suites) holds nothing that reaches beyond the job: the
  workflow declares a read-only token, its checkouts set
  `persist-credentials: false`, and it restores and saves no Actions cache.
  An artifact a later trusted step consumes is bound to its producer, not to
  its name: the scheduled run's `report` job refuses a name another job of
  its attempt took (a re-run may upload the names again) and records the id
  and digest of the `triage-context` it uploaded in its own log; triage
  resolves the upstream attempt once, reads every part of the run at it, and
  downloads that id or nothing; the skip cache takes the newest
  `last-inspect-ai-sha` of a successful run (see the headers of
  `inspect-ai-scheduled-tests.yml` and `triage-test-failures.yml`).

## PRs

- Titles are Conventional Commits (we squash-merge, so the title becomes
  the commit); allowed types are in `conventional-commit-types.json`.
- Say why in the commit message and the PR body, and list adjacent
  problems you did not fix under "Not this PR" rather than fixing them.
