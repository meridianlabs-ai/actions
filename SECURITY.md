# Security

## Reporting a vulnerability

Report a vulnerability in this repository's workflows or actions through
GitHub's private vulnerability reporting:
[open a draft advisory](https://github.com/meridianlabs-ai/actions/security/advisories/new)
on this repository. It reaches the maintainers only. Do not open a public
issue, a pull request, or a Slack thread for it: the fork issues, boards
and channels these workflows write to are public or shared.

A problem in the shared agent workflows this repo calls belongs to
[meridianlabs-ai/agents](https://github.com/meridianlabs-ai/agents/blob/main/SECURITY.md);
one in `inspect_ai` itself belongs upstream.

## What runs here

Meridian's scheduled and event-driven workflows for the `inspect_ai` fork
and its satellites, and nothing that serves end users:

- `inspect-ai-ci-perf.yml`: CI performance analysis of upstream
  `inspect_ai` `main`: a `resolve` job that fixes the commit, a `tooling`
  job that tests upstream's ci-perf scripts, an `analyze` job that runs a
  Claude agent and a `publish` job that writes fork issues and Atlas cards.
- `triage-test-failures.yml`: triage of a failed scheduled test run, an
  `agent` job that reads the logs and a `land` job that files the fork
  issue and posts to Slack.
- `inspect-ai-scheduled-tests.yml` and `inspect-swe-nightly-tests.yml`: the
  slow test suites of `inspect_ai` and `inspect_swe`, run against the
  provider APIs, with a Slack notification on failure.
- `release-please-*.yml`, `pr-title-lint.yml`, `slack-release-announce/`
  and `slack-approval-ping/`: the reusable release workflows and composite
  actions the Meridian repos call.
- `claude.yml`, `claude-review.yml`, `claude-auto.yml`: stubs that call the
  shared agent workflows in `meridianlabs-ai/agents` for this repo's own
  issues and PRs.

## Threat model

What these workflows trust, what they guarantee and what is by design is
in [`THREAT_MODEL.md`](THREAT_MODEL.md).
