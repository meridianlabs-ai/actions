# Threat model

What the workflows and actions in this repository trust, what they
guarantee, and what is by design. To report a vulnerability, see
[`SECURITY.md`](SECURITY.md).

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
  their ref characters and pytest arguments, ci-perf takes no ref: it
  analyzes upstream `main`, resolved in a job that runs nothing else, and
  triage passes the selected run ID to `gh` as a quoted argument.
- **Prior-run artifacts are trusted by provenance where a workflow checks
  it, and a run is not a producer.** A push or dispatch run executes
  whatever copy of a workflow its branch carries, so the scheduled-test
  skip cache accepts a last-tested SHA only from a successful scheduled run
  on the default branch. Within a run, the artifact namespace is shared by
  every job, and the scheduled run's `slow-tests` and `static-analysis`
  jobs execute third-party code before the `report` job uploads, so the
  name `triage-context` in a failed scheduled run proves nothing about who
  wrote it. Triage therefore resolves its context in a `context` job that
  runs no agent and no third-party code, before the agent job starts, and
  consumes only the artifact whose id and digest the `report` job recorded
  in its own job log (a channel no other job of the run can write to),
  found through the jobs API for the attempt being triaged, checked against the run's artifact list and against the
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
  and thread. The `report` job is only as trusted as the workflow file its
  run executed, so triage takes a run only when the API reports its event
  as `schedule`, its head repository as this repository and its head branch
  as the default branch. This holds for a run the dispatcher selects as for
  the run whose completion fired triage, so a dispatcher cannot name a fork
  pull request's run as the producer. Artifact names are unique within a
  run attempt, not within a run: a re-run is a new attempt and uploads the
  names again, so a run's
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
  and would outlive the offending release and a key rotation. The token
  enforces the save half: both workflows declare `cache-mode: read` at
  workflow level, so the runtime token that Runner.Worker holds, as the same
  user as the closure, cannot save a cache entry. The default
  token setting itself was not read (the API query needs repository
  administration), and no write exploit was observed: the job logs of runs
  before the declaration already listed Contents, Metadata and Packages as
  read-only, so the declaration fixes in the file what a setting provided.
- **Triage never authorizes autonomous implementation.** The issue the
  triage workflow files carries no label. Until 2026-09-22 its composing
  step forwarded the `auto` label on the agent's say-so, and on the
  `inspect_ai` fork that label starts the autonomous coding agent; the
  agent had just read test output anyone can shape, so the label was a
  privilege the untrusted principal granted itself and the machine account
  merely wrote (Claude Security finding 4628345). The machine account is
  the writer of these issues, not the authority behind them: a maintainer
  who reads the brief applies `auto` themselves, and the fork's kickoff
  (the `claude.yml` stub, and the trigger check of the shared workflow it
  calls) accepts a human labeler's write access and refuses a label the
  machine account applied. The same holds for the ci-perf publisher in
  upstream `inspect_ai`, which used to label its findings `auto`.
- **Triage's issue policies are enforced twice, three of them on the land
  runner.** The "Compose landing manifest" step on the agent's runner drops
  every label, reduces assignees to `ransomr`, keeps one issue action per
  run and carries a failed agent step into the manifest's
  `error.fail_run`. The `land` job passes the first three to the validator
  on its fresh runner as `allowed-issue-labels: ""`,
  `allowed-issue-assignees` and `max-issues: "1"`, alongside the generic
  manifest contract (schema and known keys, body and text file references,
  the allowed issue repository, no bundle under `refuse-bundle`, and no
  `pr` or `handback` field under `refuse-pr`: a `pr.open` needs no push,
  it adopts or opens a PR for a branch already on origin and labels it,
  and the `MARVIN_TOKEN` fallback reaches this repository's pull requests
  where the minted token does not; the Slack destination is a `land`
  input, not a manifest field), so a manifest forged past the composing
  step after a separate compromise of the agent runner (a shim on
  `$GITHUB_PATH`, a line in `$GITHUB_ENV`) is refused whole rather than
  landed with a label, another owner, several issue actions, a comment on
  this repository, a labelled PR or an `@review`. The fourth policy is the composer's alone: such a forged
  manifest could drop its `error`, but could not make an already failed
  agent job green, since the job result is the runner's and `land` runs
  after a failure either way. This is enforcement of an authorization
  boundary, not a response to a demonstrated write primitive: the
  redirection route finding 4628349 described was replayed against the
  real permission engine and refused (see "Verification notes"), and that
  qualification stands.
- **A built `.vsix` is repo output, not evidence.** In
  `release-please-vscode.yml` the `build` job runs the consuming repo's
  install hooks, scripts, tests and `vsce:package`, so the package it
  uploads, that file's name and any version it reports are repo-controlled,
  and vsce and ovsx publish to whatever publisher, name and version the
  package's own manifests declare. Separating build from publish confines
  where that code runs; it does not bind the publisher-wide PATs to one
  extension. The `publish` job's "Verify VSIX" step does that (see the
  guarantees below); what it cannot do is stop a compromised release commit
  from becoming the content of the one extension version it authorizes,
  which is what building is. A Marketplace PAT covers every extension of
  every publisher its account manages and an Open VSX token every namespace
  of its account; tokens scoped to the one publisher, where a marketplace
  allows it, are an administrative defence separate from this check.
- **Release notes are contributor text.** The announce action reads notes
  assembled from merged commit subjects in the calling repo and treats them
  as untrusted when it builds Slack mrkdwn.
- **Upstream `main` is trusted.** The scheduled suites run upstream
  `inspect_ai` and `inspect_swe` code unpinned with the provider keys the
  tests need, and the ci-perf analysis runs upstream's tooling unpinned, at
  the `main` commit its `resolve` job read;
  Meridian maintains those repositories, so a compromise there is a larger
  incident than these workflows.
- **PR and issue text** that the stubs react to follows the shared model in
  `meridianlabs-ai/agents`; this repo only decides which events reach it.

## Guarantees (true on `main`)

- The two agent jobs, ci-perf's `analyze` and triage's `agent`, hold
  nothing the agent may not have: the read-only job token, and the model
  credential, which is the declared exception. That credential is an
  Anthropic workload identity federation token that `claude-code-action`
  mints from the job's GitHub OIDC token, with `github_token` set to the
  job token. It is for the workflow's own Console workspace, which has a
  spend cap (triage's is not the one the test suites' `ANTHROPIC_API_KEY`
  bills). It is scoped to `workspace:inference`, lives at most 600
  seconds, and is minted under a rule that matches only that workflow file
  on `main`, for the events it runs on. The job also holds the OIDC
  request token that mints it, in the runner's step environment and not
  the agent's. Nothing else exchanges that token: the Claude GitHub App is
  not installed on this repository, and no other federation rule admits
  either workflow. No long-lived Anthropic key, marvin credential or Slack
  token is in either job. The agent can read its model token and put it in
  any output it controls. What bounds it is the cap, the scope and the
  lifetime: the agent, or whoever copies the token, can spend the
  workspace while the job runs and for up to 600 s after the last mint.
  As defence in depth the agent runs as `claude-agent` through
  `meridianlabs-ai/agents`' `claude-agent-launcher`: Claude Code
  root-owned at the release `claude-code-action@v1` pins, in its own PID
  and mount namespace, with an allow-listed environment (no runner
  command-file or OIDC request variable), the checkout mounted read-only
  and `$RUNNER_TEMP/claude-agent` as its output directory. The
  launcher's isolation check fails the job before the CLI starts if the
  agent can `sudo`, reach Docker, or reach another user's processes, the
  runner command files or the runner's install directory. The runner reads
  the agent's files only through `import-codex-final`, which copies
  regular, single-link, agent-owned files, size-capped, and follows no
  link. ci-perf's `Show report` and evidence artifact read only the
  runner's own directory, which the agent never had. Claude Code loads no
  settings, hooks or instructions from the checkout
  (`--setting-sources user`). With `--permission-mode default`, which both
  jobs pass, a headless run refuses calls that need approval and match no
  allow rule; without a mode, 2.1.287, the release `claude-code-action@v1`
  pins, starts in auto mode and sends such calls to a model classifier
  instead. Tools that need no approval, such as `Agent`, remain available
  even when unlisted. ci-perf allows Bash, Write and Edit broadly, so there
  the mode does not confine filesystem writes: the read-only mount and the
  namespace do. Triage's tools are reads plus file writes under the
  landing directory; the writes it wants are a manifest that the `land`
  job validates and performs. In the `default` mode Claude Code checks the
  target of a shell output redirect against those same file rules, so an
  allowed read command such as `grep` is not a write outside the landing
  directory through `>` or `>>` in the releases checked under
  "Verification notes"; that check belongs to the installed Claude Code
  release, not to this repo.
- The two agent workflows perform their GitHub and Atlas writes in separate
  jobs on fresh runners, from artifacts those jobs validate first, under a
  GitHub App token minted for that job and scoped to the `inspect_ai` fork
  with only the permissions the job uses (a PAT fallback stays in the
  expression while the app secrets roll out). Triage's `land` job checks
  out nothing and posts its Slack reply with a separate Slack token held
  only there, to the destination the `context` job resolved; it reads no
  output of the agent job. ci-perf's `publish` job checks out upstream
  `inspect_ai` at the commit the `resolve` job resolved, the one the
  analysis used, to run the publisher's own validator, and names the
  artifact it downloads itself. Neither the SHA nor the artifact name comes
  from the agent job.
- No `workflow_dispatch` input, step output or event field is expanded
  inside a `run:` script; they reach bash through `env:` and are quoted.
- Artifact content is validated against a shape before it becomes a ref, an
  output or a destination, and a value that fails its check is dropped, not
  passed on; the scheduled-test skip cache and triage also check the
  producing run's event and branch, and triage checks that the `report` job
  produced its context before reading a field (see the trust boundaries above for where those
  checks stop).
- The scheduled test workflows declare a read-only job token, persist no
  credential into a checkout and use no Actions cache, and their
  workflow-level `cache-mode: read` makes the token refuse a cache save;
  the `report` job refuses to upload an artifact name another job of its
  attempt took.
- The Slack destination of a triage reply comes from the context of the
  failed run that its `report` job produced, resolved by the `context` job
  before the agent runs. It never comes from the agent's manifest, the
  agent job or a same-named artifact another job of that run supplied, so
  nothing the agent prints (an `::add-mask::` of the channel ID included)
  moves the reply. The issues triage files carry no label, and the `land`
  job's validator refuses a manifest that names one, names an owner other than
  `ransomr`, carries more than one issue action, carries a comment outside
  `issues[]` (`comment-numbers: event` with no event number refuses every
  `comments[]` entry), or carries a `pr`, `handback`, review, reply or
  thread field. Those refusals hold whatever the agent job uploaded,
  including a manifest uploaded after a compromise of its runner. The release-note
  converter escapes Slack control syntax and emits only `http(s)` links;
  callers must supply a trusted release URL for the separate full-release
  link, which is not converted.
- The VS Code publish job publishes only a package it has bound to the
  release: before any step holds a PAT, its inline validator opens the
  downloaded `.vsix` without executing anything in it and fails the job
  unless `extension/package.json` and `extension.vsixmanifest` agree and
  name exactly the caller's `extension-id` at the version in the
  release-please tag (`vX.Y.Z`, `X.Y.Z`, or either with a release-please
  component prefix). The artifact directory must hold exactly one regular
  file with a plain name; an archive with repeated or case-variant manifest
  entries, unsafe entry names, symlinks or Info-ZIP Unicode Path fields
  (which rename an entry for vsce's reader only), a `package.json` with
  duplicate keys or the `NaN`/`Infinity` constants JavaScript rejects, or a
  `vsixmanifest` with a DTD or with more than one `Metadata` or `Identity`
  element in any namespace is rejected. Python's zipfile and yauzl (vsce's
  reader) find a zip's central directory differently (finding 4773470), so
  before zipfile reads the archive the validator checks its structure: the
  end-of-central-directory record must be the last 22 bytes of the file,
  with no comment, no Zip64 fields and one disk; no Zip64 end record, Zip64
  locator or second end-record signature may appear outside entry data; the
  central directory the end record names must end exactly at it and hold
  the number of entries it counts; zipfile must list those same entries;
  and each manifest's local header (or data descriptor) must match its
  directory record. The signature scan skips entry data, where compressed
  or stored bytes can match by chance (decision: Ransom, 2026-09-30,
  relaxing the fix's "anywhere in the archive" criterion so honest releases
  do not fail). That is safe because neither reader looks for these records
  there: the end record is pinned to the last 22 bytes, the only place a
  Zip64 locator is read from (the 20 bytes before it) lies in the central
  directory, and the directory and every local header lie outside the data
  ranges, which come from the checked directory and may not overlap. A
  later step, still before any PAT, reads the package
  with `readVSIXPackage` from the pinned vsce (the function and yauzl that
  `vsce publish --packagePath` uses) and fails unless both manifests there
  name the same identity and version. Nothing the build job output is
  used afterwards: the verified path, identity and version feed the publish
  commands, the already-published checks (exact JSON comparison of the
  listing's publisher, name and versions, a rerun convenience rather than a
  control) and the release upload. The checks do not defend against parser
  differentials beyond those they reject by name, and the marketplaces
  parse the uploaded package with their own readers.
- Both agent jobs run under harden-runner's egress allow-list (Anthropic,
  reached by each job's agent CLI; GitHub; the action's and the
  launcher's installers).
  harden-runner does not disable sudo here: its
  hardening runs in a `pre` hook that GitHub runs before every step, so a
  `disable-sudo-and-containers` would take sudo away before the agent-user
  setup and launcher that need it; the agent is powerless because it runs
  as an unprivileged user, not because the runner's sudo was removed. The
  allow-list stops connections to any other host. It does not stop an
  upload to an attacker-owned resource on a listed multi-tenant host
  (`github.com`, `api.github.com`, `registry.npmjs.org`) authenticated with
  a credential the attacker placed in the agent's input. What such an
  upload, or any agent output, can carry is what the agent can read: the
  read-only job token (this repository, expired when the job ends), the
  inputs, which are public, a minted Anthropic token, usable by whoever
  copies it for inference in the capped workspace for the rest of its life
  (at most 600 s), and the identity JWT, which is single-use and bound to
  Anthropic's audience. The agent jobs' outputs are published unscreened:
  the job summary appears when the agent job finishes, the artifact can be
  downloaded once it is uploaded, and the issue body and Slack text are
  posted by the later jobs. They are not screened for secrets because a screen
  on the agent's runner would be the agent's to defeat.
- The stubs pass the shared workflows exactly the secrets they name, never
  `secrets: inherit`.

## By design

- Each agent can spend its own capped workspace budget through its WIF
  token for the job's lifetime plus the remaining life of the last token
  it minted; the caps bound both. No long-lived model key reaches either
  job, so GitHub's runner process holds none in memory.
- ci-perf analyzes upstream `main` only (decision: Ransom, 2026-09-30).
  The job that runs the analyzed commit's scripts holds a credential, so
  it runs only a trusted ref: the tier-2 rule of agents' THREAT_MODEL.md says
  a credentialed workflow a person runs on a chosen ref is "restricted to
  trusted refs". An upstream branch is not trusted code just because a
  write-access holder pushed it. This replaces the 2026-09-08 decision to
  let a dispatcher analyze any upstream ref, which did not consider that
  the job holds the model key.
- The reviewer stub runs on demand only, on an `@review` comment from a
  collaborator or the machine account's hand-back; nothing reviews a PR on
  open (decision: Ransom, 2026-09-14).
- A triage-filed fix brief reaches the autonomous coding agent only when a
  maintainer labels the issue `auto` after reading it. Triage could carry
  an opt-in of its own (a `workflow_dispatch` input, an allow-list of
  files), but a human's label after the fact is the decision Ransom
  approved (2026-09-22): routine triage creates a reviewable issue and
  authorizes nothing.

## Adding or changing a workflow here

The rules and checks for a workflow change are in [`AGENTS.md`](AGENTS.md).

## Verification notes

Dated checks of controls that live in a dependency rather than in this
repo's files, and when to repeat them.

- **Claude Code redirect-target checks in the triage agent job (checked
  2026-09-21, repeated 2026-10-01).** A Bash allow rule such as
  `Bash(grep *)` does not extend to the command's output redirect: Claude
  Code checks the redirect target against the file-write rules separately,
  an application-level permission check rather than an OS sandbox
  ([documentation](https://code.claude.com/docs/en/permissions#redirections)).
  First checked in Claude Code 2.1.274 and 2.1.278, the releases pinned by
  the `claude-code-action@v1` revisions of 2026-09-17
  (`3b8197d3d486006dd4af54613517f21ac6ac625e`) and 2026-09-21
  (`b949468893d8bba436c9c71ea860b1f5f344804e`). Repeated on 2026-10-01 in
  2.1.287, the release pinned by the revision `v1` resolved to that day
  (`e8d2aa53ec36a9249e49dea43785343ccbe0ec12`), which the launcher
  installs. The settings were the workflow's `settings:` block, passed as a
  `--settings` file as the launcher's wrapper passes it, and its
  `claude_args` (`--permission-mode default`, `--setting-sources user`,
  `--add-dir` of the landing directory), with the landing directory under a
  runner-style temp path and a git checkout as the working directory. Every
  path was writable by the agent's uid, so a refusal was the CLI's. Direct
  Write calls and absolute-path redirects with `>`, `>>`, `2>`, `&>` and
  `>|` into the landing directory ran, from `grep` and from `git log`. The
  corresponding writes and redirects into the working directory and into
  another temp path were refused. Separate `>>` cases were refused too:
  relative paths into the working directory and the temp directory,
  absolute paths to stand-ins for the runner's `GITHUB_ENV`, `GITHUB_PATH`
  and `GITHUB_STEP_SUMMARY` files (the stand-ins stayed empty), and
  `$GITHUB_ENV` quoted and unquoted. `git log --output` was denied,
  `git -C <another directory> log` and `cd .. && git log` were refused, and
  `/dev/null` and `2>&1` were allowed. Without `--permission-mode`, 2.1.287
  starts a headless run in auto mode, which 2.1.278 did not: it ran the
  Write call and the `>`, `>>` and `git log >` redirects into the working
  directory, and sent the other calls above to a model classifier, which
  refused them in the check only because the stand-in model gave no
  verdict. Hence the workflow's `--permission-mode default`. In the job the
  checkout is also mounted read-only and the runner's command files are
  outside the agent's namespace, so those writes fail there whatever the
  CLI decides. Limits: these were the official Linux x64 (under emulation)
  and ARM64 builds, run directly with a deterministic stand-in model and
  fake data in an isolated container, not through the action, the Agent
  SDK and the launcher's namespace; and they covered the redirect operators
  listed, not every way a program can write (symlinks, command
  substitution, here-documents and allowed programs' own output options
  were not surveyed). `tests/test_triage_workflow.py` approximates only the
  Bash-pattern step of the decision and cannot stand in for this check.
  Repeat it with the installed CLI when `@v1` moves to a revision that pins
  another release, when the runner image or architecture changes, or when
  the `settings:` block or `claude_args` change. The records are kept with
  the maintainers' security notes, not in this repository.

## Further reading

- [`THREAT_MODEL.md`](https://github.com/meridianlabs-ai/agents/blob/main/THREAT_MODEL.md)
  in `meridianlabs-ai/agents`: the shared model for the agent workflows the
  stubs call, including how PR and issue text is trusted.
- [`design/credential-separation.md`](https://github.com/meridianlabs-ai/agents/blob/main/design/credential-separation.md)
  in `meridianlabs-ai/agents`: the two-job pattern, the manifest contract
  and the GitHub App identity these workflows follow.
- [`AGENTS.md`](AGENTS.md): the rules the tests enforce in this repo.
