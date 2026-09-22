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
  shape the logs, timings and run metadata the agents read, and the fork
  issues the triage agent searches are public. Once an agent has read
  them, its runner is untrusted: a shim on `$GITHUB_PATH`, a line in
  `$GITHUB_ENV` or a `.pth` file can outlive the agent step, so no step
  that runs after the agent on that runner holds a secret or decides
  whether the agent's output is published.
- **`workflow_dispatch` inputs are data, never syntax.** Only people with
  write access can dispatch, and no input reaches a shell script as
  syntax. Shape validation is per workflow: the scheduled suites validate
  their ref characters and pytest arguments, ci-perf passes its
  unrestricted ref directly to checkout, and triage passes the selected run
  ID to `gh` as a quoted argument.
- **Prior-run artifacts are trusted by provenance where a workflow checks
  it, and a run is not a producer.** A push or dispatch run executes
  whatever copy of a workflow its branch carries, so the scheduled-test
  skip cache accepts a last-tested SHA only from a successful scheduled run
  on the default branch. Within a run, the artifact namespace is shared by
  every job, and the scheduled run's `slow-tests` and `static-analysis`
  jobs execute third-party code before the `report` job uploads, so the
  name `triage-context` in a failed scheduled run proves nothing about who
  wrote it. Triage therefore consumes only the artifact whose id and digest
  the `report` job recorded in its own job log (a channel no other job of
  the run can write to), found through the jobs API for the attempt being
  triaged, checked against the run's artifact list and against the
  downloaded bytes, and only then shape-validated (a 40-hex SHA, a Slack
  channel ID, a Slack timestamp). A `report` job whose upload did not
  succeed, a log with no or several recorded identities, an id missing from
  the run or a digest that does not match all mean "no context": the reply
  goes to the default Slack destination and the agent investigates
  upstream `main` with `exact=false`. Triage resolves the attempt once,
  before it reads anything: the attempt whose completion fired it, or the
  latest attempt of the run a dispatcher selects. The failed log, the run
  metadata, the failing run's install log and the context are all read at
  that attempt, so a triage that runs after the upstream run gained another
  attempt does not pair one attempt's failures with another's tested SHA
  and thread. Manual triage trusts the run the dispatcher selects with the
  same producer check, without independently checking that run's event or
  branch. Artifact names are unique within a run attempt, not within a run:
  a re-run is a new attempt and uploads the names again, so a run's
  artifact list can hold one of each name per attempt. The `report` job
  refuses to upload a name that an artifact created since its own attempt
  started already carries (no `overwrite`; only another job of the attempt
  can have made it), fails, and so fails the run, while it notes and
  uploads beside an artifact from a previous attempt. The skip cache's
  `last-inspect-ai-sha` has no log binding. The skip cache selects the
  newest unexpired `last-inspect-ai-sha` from a successful scheduled run.
  This does not independently authenticate its producer: a later
  successful attempt that skips `report` (its `check-commit` found the
  commit already tested) can leave an earlier attempt's artifact eligible.
  The upload-conflict check protects attempts whose `report` actually
  uploads. A forged SHA can cause incorrect skipping in later runs while
  that artifact remains selected; it cannot select triage's checkout or
  Slack destination. What this rests on: a job's
  log is written by that job's steps alone; `actions/upload-artifact`
  refuses a duplicate name within an attempt unless told to overwrite, and
  its client documents re-runs as a source of same-named artifacts in one
  run; the API's `run_started_at` is the latest attempt's start. Not
  established here: that code in a test job can recover the runner's
  artifact-service token and upload (a published technique, not
  reproduced); the consumers do not depend on it, since triage takes the
  attempt's own `report` job as the producer and its recorded id as the
  artifact.
- **Jobs that run the dependency closure hold nothing that reaches beyond
  the job.** The scheduled suites install upstream's dev closure from PyPI
  unlocked (the point of the run) and execute it. Their job token is
  declared read-only in the workflow (`permissions: contents: read`, with
  `actions: read` only on the jobs that read this repository's run data),
  so its reach does not depend on the repository's default token setting;
  the checkouts do not persist it; and no Actions cache is restored or
  saved in those jobs, because a cache written after that code ran would
  be installed by every later scheduled run, before the keys-bearing step,
  and would outlive the offending release and a key rotation. The default
  token setting itself was not read (the API query needs repository
  administration), and no write exploit was observed: the job logs of runs
  before the declaration already listed Contents, Metadata and Packages as
  read-only, so the declaration fixes in the file what a setting provided.
- **Triage's issue policies are enforced on the agent's runner.** The
  triage workflow's "Compose landing manifest" step reduces an issue's
  labels to `auto` and its assignees to `ransomr`, keeps one issue action
  per run and carries a failed agent step into the manifest's
  `error.fail_run`. That step runs on the same runner as the agent, after
  it. The `land` job's validator, on its fresh runner, independently
  enforces the generic manifest contract: schema and known keys, body and
  text file references, the allowed issue repository, and no bundle under
  `refuse-bundle`; the Slack destination is a `land` input, not a manifest
  field. It does not repeat those four triage policies. They hold against
  a mistyped or hostile `manifest-extra.json`; after a separate compromise
  of the agent runner (a shim on `$GITHUB_PATH`, a line in `$GITHUB_ENV`)
  a forged manifest could reach `land` with other label or assignee
  values, several issue actions or no `error`, within the one repository
  the validator allows. It could not make an already failed agent job
  green: the job result is the runner's, and `land` runs after a failure
  either way. Repeating those policies in the validator is separate
  hardening, not a fix for a demonstrated write primitive.
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

- The `analyze` and `agent` jobs hold only the read-only job token; their
  Anthropic keys (`CI_PERF_ANTHROPIC_API_KEY` and `TRIAGE_ANTHROPIC_API_KEY`,
  each from a dedicated Console workspace with a spend cap; triage does not
  use the `ANTHROPIC_API_KEY` the test suites run with) are never in the
  agent's environment, files or reachable processes. Three Unix users carry
  the separation. The runner user does the trusted bootstrap. The model
  broker (`.github/actions/model-broker`) reads the key into a process
  running as the `model-broker` user and forwards Messages API calls, and
  nothing else, to `api.anthropic.com`. The whole Claude process and every
  tool it spawns run as a third, unprivileged user (`claude-agent`, chosen so
  its home does not collide with harden-runner's `/home/agent`) through
  `.github/actions/isolated-agent`, whose `ANTHROPIC_API_KEY` is only the
  broker's per-run token (usable only against the broker on loopback while
  the job runs) and whose `ANTHROPIC_BASE_URL` is the broker. That agent
  cannot read the broker user's key file or memory; cannot read the runner
  process's memory or its .NET diagnostic socket, where GitHub's runner keeps
  every job secret for masking; cannot `sudo` or reach Docker; and runs under
  `env -i` with no runner command-file variables (so it cannot rewrite a
  later trusted step) and no OIDC request variables (so it cannot mint
  tokens). The isolated-agent action runs an isolation check as the agent
  user and fails the job before the agent if any of that does not hold. The
  triage agent's tools are reads plus file writes under the landing
  directory; the writes it wants are a manifest that the `land` job validates
  and performs. Claude Code checks the target of a shell output redirect
  against those same file rules, so an allowed read command such as `grep`
  is not a write outside the landing directory through `>` or `>>` in the
  releases checked under "Verification notes"; that check belongs to the
  installed Claude Code release, not to this repo.
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
  and branch, and triage checks that the `report` job produced its context
  before reading a field (see the trust boundaries above for where those
  checks stop).
- The scheduled test workflows declare a read-only job token, persist no
  credential into a checkout and use no Actions cache; the `report` job
  refuses to upload an artifact name another job of its attempt took.
- The Slack destination of a triage reply comes from the context of the
  failed run that its `report` job produced, never from the agent's
  manifest and never from a same-named artifact another job of that run
  supplied. The release-note
  converter escapes Slack control syntax and emits only `http(s)` links;
  callers must supply a trusted release URL for the separate full-release
  link, which is not converted.
- Both agent jobs run under harden-runner's egress allow-list (Anthropic,
  reached only by the broker; GitHub; the action's installer; for ci-perf
  the Python package indexes). harden-runner does not disable sudo here: its
  hardening runs in a `pre` hook that GitHub runs before every step, so a
  `disable-sudo-and-containers` would take sudo away before the broker start
  and agent-user setup that need it; the agent is powerless because it runs
  as an unprivileged user, not because the runner's sudo was removed. The
  allow-list stops connections to any other host. It does not stop an
  upload to an attacker-owned resource on a listed multi-tenant host
  (`github.com`, `api.github.com`, `registry.npmjs.org`) authenticated with
  a credential the attacker placed in the agent's input, and the job
  summary, artifact, issue body and Slack text an agent job produces are
  published unscreened. What any of these can carry is what the agent can
  read: the run token, the read-only job token (this repository, expired
  when the job ends) and the inputs, which are public. Agent output is not
  screened for secrets because a screen on the agent's runner would be the
  agent's to defeat and there is no reusable secret to screen for.
- The stubs pass the shared workflows exactly the secrets they name, never
  `secrets: inherit`.

## By design

- The agent can spend the capped workspace budget through the broker for as
  long as its job runs; the cap bounds it. GitHub's runner process holds the
  job's secrets in memory for masking (its design). The agent is a different
  Unix user from that process, so it cannot read its memory or environment,
  nor reach the .NET diagnostic socket that would let a same-user process ask
  the runtime to dump itself; `kernel.yama.ptrace_scope` of 1 or stricter,
  which the isolation check requires, is defence in depth behind that user
  boundary.
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
   and nothing else, and its writes land in a separate job from what it
   produced. The model key goes to `.github/actions/model-broker` (a process
   under the `model-broker` user); the whole agent runs as a separate
   unprivileged user through `.github/actions/isolated-agent` with only the
   broker's per-run token and base URL, and no step after the agent holds a
   secret.
3. Mint the narrowest token for a write: one repository, only the
   permissions the job uses, in the job that writes.
4. `tests/` parses the workflows and runs their scripts; extend the matching
   test file, and add a new workflow's path to `tests.yml`.
5. Run `python3 -m pytest -q tests` and `actionlint` on the changed file
   before opening the PR (see `AGENTS.md`).
6. Do not widen an agent job's permissions or tool allow list without a test
   for the write vector it closes.

## Verification notes

Dated checks of controls that live in a dependency rather than in this
repo's files, and when to repeat them.

- **Claude Code redirect-target checks in the triage agent job (checked
  2026-09-21).** A Bash allow rule such as `Bash(grep *)` does not extend
  to the command's output redirect: Claude Code checks the redirect target
  against the file-write rules separately, an application-level permission
  check rather than an OS sandbox
  ([documentation](https://code.claude.com/docs/en/permissions#redirections)).
  Checked in Claude Code 2.1.274, the release pinned by the
  `claude-code-action@v1` revision the workflow resolved on 2026-09-17
  (`3b8197d3d486006dd4af54613517f21ac6ac625e`), and 2.1.278, the release
  pinned by the revision `v1` resolved to on 2026-09-21
  (`b949468893d8bba436c9c71ea860b1f5f344804e`). The action pins its Claude
  Code release internally; the `@v1` reference moves between revisions.
  The settings were the permissions block of the workflow's `settings:`
  input, with the landing directory under a runner-style temp path and the
  `inspect_ai` clone below the working directory. Direct Write calls and
  absolute-path redirects with `>`, `>>`, `2>`, `&>` and `>|` into the
  landing directory ran; the corresponding writes and redirects into the
  working directory and into another temp path were refused. Separate `>>`
  cases were refused too: relative paths into the working directory and
  the temp directory, absolute paths to pre-created stand-ins for the
  runner's `GITHUB_ENV`, `GITHUB_PATH` and `GITHUB_STEP_SUMMARY` files
  (the stand-ins stayed empty), and `$GITHUB_ENV` quoted and unquoted.
  `git --output` was denied; `/dev/null` and `2>&1` were allowed. Limits:
  these were the official Linux ARM64 builds of those two releases, run
  directly with a deterministic stand-in model and fake data in an isolated
  container, not the Linux x64 binary the hosted runner installs and not
  through the action and Agent SDK; and they covered the
  redirect operators listed, not every way a program can write (symlinks,
  command substitution, here-documents and allowed programs' own output
  options were not surveyed). `tests/test_triage_workflow.py` approximates
  only the Bash-pattern step of the decision and cannot stand in for this
  check. Repeat it with the installed CLI when `@v1` moves to a revision
  that pins another release, when the runner image or architecture
  changes, or when the `settings:` block changes. The records are kept
  with the maintainers' security notes, not in this repository.

## Further reading

- [`SECURITY.md`](https://github.com/meridianlabs-ai/agents/blob/main/SECURITY.md)
  in `meridianlabs-ai/agents`: the shared model for the agent workflows the
  stubs call, including how PR and issue text is trusted.
- [`design/credential-separation.md`](https://github.com/meridianlabs-ai/agents/blob/main/design/credential-separation.md)
  in `meridianlabs-ai/agents`: the two-job pattern, the manifest contract
  and the GitHub App identity these workflows follow.
- [`AGENTS.md`](AGENTS.md): the rules the tests enforce in this repo.
