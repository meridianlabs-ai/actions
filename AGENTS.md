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
`tests/test_scheduled_workflows.py`; `tests/test_slack_release_announce.py`
covers the announce converter). `.github/workflows/tests.yml` runs them in CI
on every push and pull request that touches a path it lists.

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
python3 -m pytest -q tests
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
- A job that runs an agent over untrusted input holds only the job token
  and the model key; writes to issues and Slack happen in a separate job
  from a validated manifest (see the header of
  `triage-test-failures.yml`). Do not widen an agent job's permissions or
  allow list without a test for the write vector it closes.

## PRs

- Titles are Conventional Commits (we squash-merge, so the title becomes
  the commit); allowed types are in `conventional-commit-types.json`.
- Say why in the commit message and the PR body, and list adjacent
  problems you did not fix under "Not this PR" rather than fixing them.
