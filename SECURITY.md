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
  `inspect_ai`, an `analyze` job that runs a Claude agent and a `publish`
  job that writes fork issues and Atlas cards.
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

## Trust boundaries specific to this repo

- **Test output and CI data are third-party-shaped input.** Any code the
  tests exercise (a dependency released that day, a PR under analysis) can
  shape the logs, timings and run metadata the agents read. Once an agent
  has read them, its runner is untrusted: a shim on `$GITHUB_PATH`, a line
  in `$GITHUB_ENV` or a `.pth` file can outlive the agent step.
- **`workflow_dispatch` inputs are data, never syntax.** Only people with
  write access can dispatch, and no input reaches a shell script as
  syntax. Shape validation is per workflow: the scheduled suites validate
  their ref characters and pytest arguments, ci-perf passes its
  unrestricted ref directly to checkout, and triage passes the selected run
  ID to `gh` as a quoted argument.
- **Prior-run artifacts are trusted by provenance where a workflow checks
  it.** A push or dispatch run executes whatever copy of a workflow its
  branch carries, so the scheduled-test skip cache accepts a last-tested
  SHA only from a successful scheduled run on the default branch. Automatic
  triage is gated on a failed scheduled test run; manual triage trusts the
  run the dispatcher selects and validates its context fields (a 40-hex
  SHA, a Slack channel ID, a Slack timestamp) without independently
  checking that run's event or branch.
- **Release notes are contributor text.** The announce action reads notes
  assembled from merged commit subjects in the calling repo and treats them
  as untrusted when it builds Slack mrkdwn.
- **Upstream `main` is trusted.** The scheduled suites run upstream
  `inspect_ai` and `inspect_swe` code unpinned with the provider keys the
  tests need, and the ci-perf analysis runs upstream's tooling unpinned;
  Meridian maintains those repositories, so a compromise there is a larger
  incident than these workflows.
- **PR and issue text** that the stubs react to follows the shared model in
  `meridianlabs-ai/agents`; this repo only decides which events reach it.

## Guarantees (true on `main`)

- The `analyze` and `agent` jobs hold only the read-only job token and an
  Anthropic key (ci-perf's from a dedicated Console workspace with a spend
  cap). The triage agent's tools are reads plus file writes under the
  landing directory; the writes it wants are a manifest that the `land`
  job validates and performs.
- The two agent workflows perform their GitHub and Atlas writes in separate
  jobs on fresh runners, from artifacts those jobs validate first, under a
  GitHub App token minted for that job and scoped to the `inspect_ai` fork
  with only the permissions the job uses (a PAT fallback stays in the
  expression while the app secrets roll out). Triage's `land` job checks
  out nothing and posts its Slack reply with a separate Slack token held
  only there; ci-perf's `publish` job checks out upstream `inspect_ai` at
  the SHA the analysis used, to run the publisher's own validator.
- No `workflow_dispatch` input, step output or event field is expanded
  inside a `run:` script; they reach bash through `env:` and are quoted.
- Artifact content is validated against a shape before it becomes a ref, an
  output or a destination, and a value that fails its check is dropped, not
  passed on; the scheduled-test skip cache also checks its producer's event
  and branch (see the trust boundaries above for where that check stops).
- The Slack destination of a triage reply comes from the validated context
  of the failed run, never from the agent's manifest. The release-note
  converter escapes Slack control syntax and emits only `http(s)` links;
  callers must supply a trusted release URL for the separate full-release
  link, which is not converted.
- The `analyze` job runs under an egress allow-list with sudo disabled and
  refuses to publish evidence that contains the Anthropic key.
- The stubs pass the shared workflows exactly the secrets they name, never
  `secrets: inherit`.

## By design

- The `workflow_dispatch` ref on ci-perf is unrestricted: a dispatcher may
  analyze any upstream ref, because the agent job holds no write token and
  publication requires the ref to be `main` (decision: Ransom, 2026-09-08).
- The reviewer stub runs on demand only, on an `@review` comment from a
  collaborator or the machine account's hand-back; nothing reviews a PR on
  open (decision: Ransom, 2026-09-14).

## Adding or changing a workflow here

1. Inputs, artifact fields and event text reach bash through `env:` or
   files, are validated against a shape where one exists, and are used as
   quoted variables.
2. Write credentials (app secrets, tokens, webhooks) stay out of any job
   that runs an agent or third-party code; such a job gets the job token
   and its model key and nothing else, and its writes land in a separate
   job from what it produced.
3. Mint the narrowest token for a write: one repository, only the
   permissions the job uses, in the job that writes.
4. `tests/` parses the workflows and runs their scripts; extend the matching
   test file, and add a new workflow's path to `tests.yml`.
5. Run `python3 -m pytest -q tests` and `actionlint` on the changed file
   before opening the PR (see `AGENTS.md`).
6. Do not widen an agent job's permissions or tool allow list without a test
   for the write vector it closes.

## Further reading

- [`SECURITY.md`](https://github.com/meridianlabs-ai/agents/blob/main/SECURITY.md)
  in `meridianlabs-ai/agents`: the shared model for the agent workflows the
  stubs call, including how PR and issue text is trusted.
- [`design/credential-separation.md`](https://github.com/meridianlabs-ai/agents/blob/main/design/credential-separation.md)
  in `meridianlabs-ai/agents`: the two-job pattern, the manifest contract
  and the GitHub App identity these workflows follow.
- [`AGENTS.md`](AGENTS.md): the rules the tests enforce in this repo.
