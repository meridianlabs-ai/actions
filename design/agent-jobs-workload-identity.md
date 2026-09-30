# Agent jobs on workload identity federation

Status: proposed, 2026-09-30. Issue: none (task from Ransom, 2026-09-30).
Author: agent (Claude), reviewed by Codex; see the PR.

Decided before this design (decision: Ransom, 2026-09-30): the two agent
jobs move to Anthropic workload identity federation (WIF) and the model
broker goes. His reasoning: "easier to restrict the value of the token than
to close all possible security threats to steal it." The agent may read a
short-lived Anthropic token. What limits its value is the spend-capped
workspace, the token's lifetime, and a federation rule restricted to the
workflow.

Disclosure: this repository is public, and this design names open Claude
Security findings and an unreported gap in full detail. Ransom chose that
(decision: Ransom, 2026-09-30), although SECURITY.md asks for no public
pull request about a security problem.

Line numbers are from `main` at 3916d26 unless another tree is named.
meridianlabs-ai/agents was read at c23eb39 (the merge of agents #178,
`design/untrusted-agent-job.md`, called "the agents design" below).
anthropics/claude-code-action was read at `v1` = fd1c128 (2026-09-29,
Claude Code 2.1.285).

## Why

`inspect-ai-ci-perf.yml` (job `analyze`) and `triage-test-failures.yml`
(job `agent`) run a Claude agent over third-party-shaped input: upstream CI
logs, PR text, test output and public fork issues. Each job references a
long-lived Anthropic API key (`CI_PERF_ANTHROPIC_API_KEY`,
`TRIAGE_ANTHROPIC_API_KEY`). The key goes to the model broker, a process
running as the `model-broker` user. The agent runs as a third user,
`claude-agent`, and gets only the broker's per-run loopback token.

SECURITY.md → Guarantees (SECURITY.md:182-224, 273-289) rests on that. It
says the agent never holds a reusable credential, so the job summary,
artifact, issue body and Slack text the jobs publish unscreened "carry
nothing worth leaking". That premise is already broken, in two ways,
before any agent-to-runner escape:

- **Runner.Worker holds the key from job start.** The key is a secret that
  a step references, so GitHub puts it in the job message and the runner
  keeps it in memory for masking (SECURITY.md:295-302). Any process running
  as `runner` can ask the .NET runtime to dump that memory through its
  same-uid diagnostic socket. The agent is kept from that socket only by
  being another uid.
- **`runner` has passwordless sudo**, which harden-runner deliberately
  leaves on (SECURITY.md:273-279). So root reads the broker's memory as
  well.

So any code that runs as `runner` in these jobs holds the key. Three open
findings from the Claude Security scan of 3916d26 (created 2026-09-25) run
code as `runner` or root after the key is in the job:

| Finding | Severity | What runs, or is read, as `runner` |
|---|---|---|
| 4773473 | High | `isolated-agent` installs Claude Code as root with `sudo -n env "PATH=$PATH" npm install -g @anthropic-ai/claude-code` when `claude` is not on the image (`.github/actions/isolated-agent/action.yml:149-151`). A stock `ubuntu-latest` has none. The install has no version, no integrity check and no `--ignore-scripts`, and runs after `Start the model broker` (ci-perf :163, then :244; triage :135, then :532). |
| 4773275 | High | ci-perf's `Test CI tooling` (inspect-ai-ci-perf.yml:210-213) runs an unpinned `python -m pip install pytest` and pytest as the sudo-capable `runner`. |
| 4773274 | Medium | ci-perf's `Show report` (:290-302) and `Retain CI evidence outside Git` (:304-311) run as `runner` over the agent-writable output directory, with link-following `test -f`, `cat` and upload-artifact. |

Two related triage findings: 4773277 (Medium: agent-authored text reaches
the runner's stdout, where a `::` line is a workflow command, and
`::add-mask::` voids the validated Slack destination) and 4773278 (Low:
manual triage accepts a fork PR's workflow run as the trusted context
producer).

**An unreported gap (found 2026-09-30, verified here).** ci-perf's
`workflow_dispatch` input `inspect_ai_ref` is a free string
(inspect-ai-ci-perf.yml:110-114). `Checkout upstream source for analysis`
passes it straight to `actions/checkout` as `ref` (:196-203). The job then
runs that ref's own scripts as `runner`: `test_ci_perf.py` under pytest
(:212-213), then `collect_ci_data.py`, `summarize_ci_data.py` and
`publish_ci_findings.py --read-history` (:222-229). Upstream serves every
fork pull request's head as `refs/pull/<n>/head`: a fetch of
`refs/pull/5614/head` (a PR from a fork outside Meridian) succeeded on
2026-09-30, and 4,430 such refs are listed. A raw commit SHA from a fork PR
is fetchable the same way. So a write-access dispatcher who names an
outsider's PR ref runs outsider code in the job that holds the key. The
2026-09-08 decision to allow any ref (SECURITY.md:303-305, By design)
reasoned from "the agent job holds no write token and publication requires
`main`". It did not consider the key.

meridianlabs-ai/agents set the boundary this design adopts: **the agent job
holds nothing the agent may not have; the model credential is the declared
exception** (the agents design → The boundary). Moving to WIF makes the
model credential the only thing of value in these jobs, and bounds that
credential by the workspace, its lifetime and the federation rule. The
findings above then lose their payoff instead of each needing a patch.

## Goals and non-goals

Goals:

- `analyze` and the triage `agent` job hold nothing beyond what the agent
  may have: the read-only job token; the WIF model credential for their
  own capped workspace, scoped to inference; and the OIDC request token
  that mints it, which no relying party exchanges for more.
- No long-lived Anthropic key is referenced by any job in these two
  workflows. The broker, `isolated-agent`, their tests and the two secrets
  are retired.
- One Claude launch path for every Meridian agent job: agents'
  `claude-agent-launcher` with claude-code-action in agent mode, as the
  agents repository's own jobs run it.
- No trusted job (`publish`, `land`) takes a decision from the agent job's
  outputs other than a choice between outcomes that are all safe. The
  agents design → "How land may use the job's word" treats the whole agent
  job, its `runner` uid, root and every output it reports as untrusted.
- ci-perf runs only trusted upstream code in a job that holds a
  credential. Third-party code runs where it holds nothing.
- SECURITY.md says what is true afterwards, including what the unscreened
  outputs can now carry.
- Each implementation step ships alone, in an order that fits the agents
  rollout.

Non-goals:

- **Keeping the model credential from the agent.** It is the declared
  exception (decision: Ransom, 2026-09-30, and agents' SECURITY.md →
  Adding or changing a workflow, decision: Ransom, 2026-09-23).
- **Changing what the agents do**: the prompts, the triage manifest
  contract, the land and publish jobs' writes. Paths in the prompts change
  with the workspace layout; nothing else.
- **The scheduled suites' provider keys**, the Actions cache findings and
  the VSIX validator (see Not this design).
- **New work in meridianlabs-ai/agents.** The launcher composites need no
  change (Design → Why the launcher). The design depends on two things on
  the agents side, both already in or implied by the agents design: its
  step 3 shipping land's `comment-numbers` input, with the agents design's
  note on triage changed to match (Design → triage after the change, P4);
  and a Console change narrowing agents' federation rule (Design →
  Federation rules, P3).

## Current behaviour

### The model broker

- `model-broker` (`.github/actions/model-broker/action.yml`) runs as
  `runner` with sudo. It creates the `model-broker` system user (:89-91),
  hands it the key through a file only that user can read (:103), and
  starts `model_broker.py` detached as that user (:107-113). The broker
  deletes the key file once it has read it, listens on 127.0.0.1, accepts
  only its per-run token (`broker-run-<48 hex>`, :102), and forwards Messages
  API calls, and nothing else, to `api.anthropic.com`.
- **Stale text.** The action's description says it runs before
  harden-runner, whose `disable-sudo-and-containers` "then takes sudo and
  Docker away", and that `check_isolation.sh` is "also next to this file"
  (:14-24). Its sudo error says it "must run before harden-runner disables
  it" (:86). The workflows (ci-perf :170-177, triage :148-153) and
  SECURITY.md (:275-279) say harden-runner deliberately does not disable
  sudo, and `check_isolation.sh` lives in `isolated-agent`. Deleting the
  action removes this text.

### isolated-agent

`.github/actions/isolated-agent/action.yml` runs the Claude CLI directly,
not through claude-code-action:

1. **Setup** as `runner` with sudo (:100-258): creates `claude-agent` (not
   `agent`, which harden-runner uses; :114-135), installs Claude Code
   (:149-151, finding 4773473), makes the write directory
   `runner:claude-agent` 2775 (:162), stages prompt and settings
   world-readable under `$RUNNER_TEMP/isolated-agent` (:166-175), and gives
   the agent search-only ACL entries on every ancestor that denies it
   traversal, keeping each directory's ACL mask (:207-241).
2. **Check** (:261-289): runs `check_isolation.sh` as the agent under
   `env -i`.
3. **Run** (:292-349): `sudo -u claude-agent env -i` with `HOME`, `PATH`,
   `ANTHROPIC_BASE_URL` (the broker), `ANTHROPIC_API_KEY` (the per-run
   token), `GH_TOKEN` (the job token),
   `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and the caller's
   `forward-env` pairs (:317-333). No runner command-file variable and no
   `ACTIONS_ID_TOKEN_*` reach it. `claude -p` prints its final message to
   its stdout, which is the step's log (:340). That is the route of
   4773277.
4. **Gate** (:357-366): fails the job unless the agent exited 0.

`check_isolation.sh` fails closed when any of these does not hold:

- the agent is not root or `model-broker`, and is the expected user
  (:36-42);
- it cannot `sudo` or reach Docker through `docker info` (:45-48);
- `kernel.yama.ptrace_scope` is 1 or stricter (:52-53);
- the broker runs as `model-broker`, and its key, token, directory and
  `/proc` entries are closed (:56-77);
- every other user's `/proc/<pid>/environ` and `mem` are unreadable, with
  at least one such process present (:83-94);
- no `.NET` diagnostic socket under `$TMPDIR` or `/tmp` is connectable
  (:99-115);
- the runner command files are not writable (:120-123);
- the runner's registration key `.credentials_rsaparams` is unreadable
  (:134-137);
- the broker answers on loopback and refuses a wrong token and a foreign
  route (:141-157).

### The two jobs and what the trusted jobs believe

**ci-perf** (`inspect-ai-ci-perf.yml`):

- `analyze` (:130-311) holds contents, actions, issues and pull-requests
  read.
- It checks out this repository's `.github/actions` to `actions-repo/`
  (:156-161) and starts the broker (:163-168).
- harden-runner blocks egress to all but nine hosts (:178-191).
- It checks out upstream at the dispatcher's ref (:196-203), then runs
  `Test CI tooling` (:210-213) and the collection (:217-232). The
  collection writes `raw.json`, `summary.json`, `measurements.md` and
  `previous-summaries.json` to `$RUNNER_TEMP/ci-perf`, and records three
  sha256 values as step outputs.
- The agent runs through `isolated-agent`, with that directory as its
  write directory (:244-272).
- The job's outputs (:143-149) are `source_sha`, the three hashes,
  `analysis_conclusion` and `artifact_name`.

`publish` (:313-470):

- runs on schedules, and on dispatches with `dry_run == false` and
  `inspect_ai_ref == 'main'` (:315);
- checks out upstream at `needs.analyze.outputs.source_sha` (:352-358) and
  runs that commit's `publish_ci_findings.py` with the marvin token
  (:393-401);
- downloads the artifact named by `needs.analyze.outputs.artifact_name`
  (:365-369), and checks the three files against the hashes `analyze`
  reported (:371-383).

So `publish` runs code at a SHA the agent job reported, while holding the
marvin identity.

**triage** (`triage-test-failures.yml`):

- `agent` (:98-1083) holds contents and actions read.
- It resolves the upstream attempt (:176-193), downloads the failed logs
  into `triage/` (:195), and resolves and validates the triage context that
  the upstream `report` job recorded (:245, :404).
- It checks out upstream at the context's SHA to `inspect_ai/` (:448-456),
  runs the agent with `$RUNNER_TEMP/landing` as its write directory
  (:532-915), composes the manifest (:969-1063) and emits it (:1065-1072).
- Its outputs `channel` and `thread_ts` (:114-118) come from the context
  step.

`land` (:1085-1160) posts the Slack reply to
`needs.agent.outputs.channel || secrets.SLACK_CHANNEL_ID` and
`needs.agent.outputs.thread_ts` (:1159-1160). So the Slack destination is a
value the agent job reported. That is why 4773277's `::add-mask::` of the
channel ID matters: GitHub drops a job output that contains a masked
value, and land falls back to the default channel.

### claude-code-action in agent mode (at fd1c128)

- **Agent mode.** A `prompt` input selects it: `prepareAgentMode`,
  src/modes/agent/index.ts:20-144. It creates no comment. It runs
  `checkHumanActor` (:30): `GET /users/{actor}`, and a non-User actor must
  be in `allowed_bots`.
- **Write-permission check.** For `workflow_run` and entity events,
  `checkWritePermissions` runs (src/entrypoints/run.ts:196-209). The actors
  of the recent scheduled runs are Users: `ransomr` for the scheduled tests
  and triage, `epatey` for ci-perf (runs 36701343813, 36703400725 and
  36548144383).
- **Git configuration.** `configureGitAuth` sets the git user and points
  `origin` at `https://x-access-token:<github_token>@github.com/<this
  repository>.git` (src/github/operations/git-config.ts:22-48, 128-131). It
  does this in whatever repository the workspace holds. A failure is
  logged and ignored (agent/index.ts:59-65).
- **WIF.** `setupWorkloadIdentity` (base-action/src/workload-identity.ts:121-196)
  does the following:
  - requests an OIDC token for audience `https://api.anthropic.com`;
  - writes it to `$RUNNER_TEMP/claude-workload-identity/identity-token`
    (0700 directory, 0600 file) and rewrites it every 4 minutes;
  - writes a minimal profile at `…/config-<fingerprint>/configs/default.json`;
  - exports `ANTHROPIC_IDENTITY_TOKEN_FILE`, `ANTHROPIC_CONFIG_DIR` and
    `ANTHROPIC_PROFILE`, and deletes the directory when the step ends.
- **The CLI.** It is 2.1.285, pinned as a literal in run.ts:80. With
  `path_to_claude_code_executable`, the action runs that file instead, and
  appends its directory to `GITHUB_PATH` (:61-77).
- **Logging.** Without `show_full_output`, the action logs only the SDK's
  init message and a sanitized result object (base-action/src/run-claude-sdk.ts:117-160).
  `display_report` defaults to `false`, so no transcript reaches the step
  summary.
- **Failure.** A failed CLI run throws, and the step fails
  (run-claude-sdk.ts:253-285, run.ts:316-325). The `conclusion` output is
  set only on success.

### agents' launcher (at c23eb39)

Three composites make it up:

- `create-codex-user` with `user: claude-agent` (its action.yml:175-207):
  - snapshots `$GITHUB_WORKSPACE/.git/config` (:263), so the workspace root
    must be a git checkout;
  - creates the user, checks the job PATH and writes `safe.directory` to
    the system git config;
  - creates the landing directory `$RUNNER_TEMP/claude-agent`,
    `runner:claude-agent` 2775, and `/opt/meridian-agent`.

  `grant: none` leaves the checkout runner-owned and read-only to the
  agent.
- `claude-agent-launcher`:
  - installs Claude Code root-owned under `/opt/meridian-agent/claude` at
    the version claude-code-action pins. It reads the `claudeCodeVersion`
    literal from the downloaded action fail-closed, installs with
    `curl https://claude.ai/install.sh`, runs the installer as root under
    `env -i`, and checks `claude --version` (its action.yml:123-148). The
    installer verifies its bootstrap binary's sha256 against the release
    manifest on downloads.claude.ai, then runs `claude install <version>`.
  - records the workspace HEAD, runs the `pre` isolation check, kills
    every `claude-agent` process and re-creates the landing directory
    empty (:158-196).
- The wrapper `claude`, which the action spawns as the CLI:
  - rebuilds the argv without the action's MCP servers, and an allow-listed
    environment (claude:136-175);
  - refuses any token other than the job token (:65-83, 241-246);
  - resets `origin` to the credential-free URL (:213);
  - hands off to `agent-ns-launch`.

`agent_ns.py` then does the rest:

- It grants the agent read on the WIF token file (`grant_wif`,
  agent_ns.py:257-285) and writes the agent its own copy of the profile
  (:288-318).
- It starts the CLI as `claude-agent` in a private PID and mount namespace.
  A tmpfs covers the runner's home and `/tmp`, and only four directories
  are bound back: the workspace (read-only under `grant: none`), the
  landing directory, `$RUNNER_TEMP/scratch` (`none` only, required) and
  the WIF directory (read-only) (`bind_plan`, :445-455).
- Inside the namespace it runs `check-isolation.sh --phase namespace`
  first.

## Design

### The boundary after the change

What each agent job holds once the design is fully shipped (step 6):

| Held in `analyze` / triage `agent` | Reachable by the agent | Why the agent may have it |
|---|---|---|
| Job token (contents and actions read, plus issues and pull-requests read for ci-perf) | yes, as `GH_TOKEN` | read-only, this repository, expires with the job |
| The GitHub OIDC JWT for audience `https://api.anthropic.com`, rewritten every 4 minutes | yes (the launcher's ACL grant) | single-use (`jti`); Anthropic's exchange is the only relying party for that audience |
| The minted Anthropic access token (`sk-ant-oat01-…`) | yes, in its own config directory | **the declared exception**: `workspace:inference` in the workflow's own capped workspace, at most 600 s per token |
| OIDC request token (`ACTIONS_ID_TOKEN_REQUEST_*`), in `runner`'s step environment | no (namespace and `env -i`) | a `runner` compromise can mint JWTs for any audience. Anthropic's rule for this workflow grants the exception. The Claude App exchange has no installation to issue from on this repository (prerequisite P1). No other relying party trusts this repository (P3) |
| `ACTIONS_RUNTIME_TOKEN` | no | cache access is read-only (`cache-mode: read`, ci-perf :123, triage :81). The artifacts it can upload are untrusted data to every consumer (below) |
| Anthropic API key, marvin, App or Slack credentials | **none in the job** | — |

**What the trusted jobs take from the agent job.** Nothing that decides a
destination, a checkout or code to run:

| Use | Values the agent job can force | Why each is safe |
|---|---|---|
| `publish` needs `analyze` succeeded and `analysis_conclusion == success` | publish or not | the publisher still validates the findings, and the agent can already write any valid findings |
| `publish` checks the three data files against `analyze`'s hashes | pass or fail | hygiene against the agent user editing the data; a `runner` compromise can forge them, which buys false numbers in a fork issue, as the agent could already write false findings |
| `land` skips on `needs.agent.result` skipped or cancelled | land nothing | the agent can already make its run land nothing |
| The `ci-perf-*` artifact | any content | already validated as untrusted data by `publish_ci_findings.py`, which writes only fork issues |
| The triage `landing` artifact | any manifest, written after the composer ran | `land`'s validator refuses a label, another assignee, a second issue action, a foreign issue repository, a bundle and every PR, reply, thread and hand-back field (SECURITY.md:130-154). It does **not** today refuse generic `comments[]` on arbitrary numbers of **this** repository (validate_manifest.py:663-680 checks shape only). With `comment-numbers: event` and no event number (P4) it refuses them too |

The checkout SHA `publish` runs code from comes from a new trusted `resolve`
job, and the Slack destination from a new trusted triage `context` job. The
artifact name is computed by `publish` itself. Details below.

### Why the launcher, not WIF inside isolated-agent

The task offered two shapes:

1. keep `isolated-agent` and add WIF to it;
2. replace it with agents' `claude-agent-launcher` plus claude-code-action
   in agent mode.

This design takes **option 2**. The evidence:

- **It works for these jobs with no launcher change,** once the upstream
  checkout sits at the workspace root:
  - `create-codex-user` and the launcher need a git checkout there
    (create-codex-user:263, launcher action.yml:160-161, wrapper :211-214);
  - the triage and ci-perf agents only read, so `grant: none` fits them,
    with the checkout mounted read-only;
  - `$RUNNER_TEMP/scratch` must exist; each job creates it empty;
  - neither job's actors trip the action's actor checks (Current behaviour).

  Nothing in the wrapper or namespace depends on the agents repository's
  job graph.
- **The launcher's isolation check covers everything `check_isolation.sh`
  covers, except the broker probes.** With no broker, those have no
  subject. Compared item by item:
  - user, sudo, Docker and Yama checks: the same, plus a connect to the
    Docker socket (check-isolation.sh:55-85);
  - other users' processes: the private `/proc` shows only the namespace,
    and every foreign `environ`, `mem` and `cwd` is closed (:98-118);
  - `.NET` sockets: found through `/proc/net/unix`, not only a glob
    (:123-152);
  - command files: all five, and neither readable nor writable, instead of
    three not writable (wrapper :256-261; check :183-185);
  - the registration key: its whole install directory is unreachable
    (wrapper :259);
  - `env -i` with no OIDC or command-file variables: the wrapper's
    allow-list (:136-175);
  - traversal ACLs are replaced by the tmpfs and four binds, checked by
    "nothing under the runner's home but the binds" (:157-181).

  **No gap was found.** The one new reach, the WIF directory, is intended.
- **WIF comes built.** The action requests, writes and refreshes the
  identity token. The launcher's `grant_wif` and `write_agent_config` give
  the agent read on the token and its own credential cache. Option 1 would
  write that again in this repository: a runner-side daemon that rewrites
  a JWT every 4 minutes for the agent's lifetime, a profile directory, ACLs
  and an env contract. It would then test it apart from agents' copy.
- **It pins the CLI.** The version is claude-code-action's pinned
  `claudeCodeVersion`, read fail-closed. The vendor installer checks its
  bootstrap binary against the release manifest's sha256. There are no npm
  lifecycle scripts, and the install is root-owned. That answers "pin the
  Claude Code install anyway" with code that already exists.
- **Agent text leaves the step log.** The action logs a sanitized result,
  not the agent's final message, so 4773277's stdout route is gone from
  the agent step.
- **One path.** Ransom leans to whichever leaves one launch path, and the
  earlier discussion leaned to option 2. The agents design's own Not this
  design names these two workflows as the remaining outliers.

The costs, accepted:

- claude-code-action `@v1` is a moving tag, and the action runs as `runner`
  in the job. Under this boundary a bad release gains nothing beyond the
  agent's credential. What it can still do is break the run, and the
  launcher's version read fails closed on an unexpected layout.
- Four more agents composites are referenced `@main`. This is the same
  trust as the `emit-landing@main` and `land@main` that triage already uses.
- The workspace layout changes, and so do the prompts' paths and the
  triage allow-list rules (below).
- `isolated-agent`'s own hosted-runner tests in this repository go.
  agents' `tests/test_claude_agent_launcher.py` covers the launcher.

### Workspace layout

Upstream `inspect_ai` is checked out at the workspace root (the default
`path`), not in `inspect_ai/`. There is no `actions-repo/` checkout, since
no local action is left. The prompts drop the `inspect_ai/` prefix:

- triage: `git -C inspect_ai log` becomes `git log`, and the allow rules
  become `Bash(git log *)`, `Bash(git show *)` and `Bash(git blame *)`.
  `triage/` is written inside the checkout's working tree, where the agent
  reads it read-only;
- ci-perf: `.claude/skills/ci-perf/SKILL.md`, which upstream keeps as a
  symlink into `.agents/skills`.

`--setting-sources user` goes in both jobs' `claude_args`. The CLI then
loads no settings, hooks, `.mcp.json`, `CLAUDE.md` or skills from the
checkout. That keeps today's behaviour, where the working directory is not
a repository. agents' reviewer uses the same flag for the same reason
(claude-review.yml at c23eb39, its `claude_args`).

`configureGitAuth` points the checkout's `origin` at this repository, and
the wrapper then resets it to the credential-free URL. Neither agent
fetches, so this has no effect.

### ci-perf after the change

Four jobs: `resolve → tooling → analyze → publish`.

**`resolve`** (new; trusted; `permissions: {}`; runs no third-party code)
resolves the ref to one commit of upstream's branches or tags:

```bash
set -euo pipefail
ref="${INSPECT_AI_REF:-main}"          # the input, through env:
fail() { echo "::error::inspect_ai_ref: $*"; exit 1; }
[[ "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$ ]] || fail "'$ref' is not a branch or tag name"
case "$ref" in *..*|*//*|*/|*.lock|*@\{*) fail "'$ref' is not a branch or tag name" ;; esac
lines=$(git ls-remote https://github.com/UKGovernmentBEIS/inspect_ai.git \
          "refs/heads/$ref" "refs/tags/$ref" "refs/tags/$ref^{}")
branch=$(awk -v r="refs/heads/$ref" '$2 == r {print $1}' <<<"$lines")
tag=$(awk -v r="refs/tags/$ref^{}" '$2 == r {print $1}' <<<"$lines")
[ -n "$tag" ] || tag=$(awk -v r="refs/tags/$ref" '$2 == r {print $1}' <<<"$lines")
if [ -n "$branch" ] && [ -n "$tag" ]; then fail "'$ref' names both a branch and a tag"; fi
sha="${branch:-$tag}"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || fail "'$ref' is not a branch or tag of UKGovernmentBEIS/inspect_ai"
{ echo "sha<<EOF"; echo "$sha"; echo "EOF"; } >>"$GITHUB_OUTPUT"
```

- The name is matched only under `refs/heads/` and `refs/tags/`. So
  `refs/pull/<n>/head`, a raw SHA or `pull/5614/head` finds nothing and
  fails.
- Output: `sha`.
- The input's description becomes "Upstream inspect_ai branch or tag
  (default main); commits and pull-request refs are refused".

Why these refs are trusted: upstream branches and tags are pushed only by
accounts with write access to UKGovernmentBEIS/inspect_ai. That is the
population SECURITY.md already trusts for upstream `main` (:172-176).
Meridian's agents push to the fork, not upstream, and the machine account
cannot write upstream. Open question 1 asks whether to narrow this to
`main` alone.

**`tooling`** (new; `needs: resolve`; `permissions: contents: read`; no
secret, no `id-token`) checks out `needs.resolve.outputs.sha`, sets up
Python and runs `Test CI tooling` as today. `pip install pytest` from PyPI
is third-party code, and here it holds only the read-only token. That
closes 4773275 before WIF ships, while the key still exists. After WIF it
keeps third-party code out of the credentialed job (the tier-2 rule in
agents' SECURITY.md). `analyze` needs `tooling`: its result decides only
whether the analysis runs, and both outcomes are safe.

**`analyze`** (`needs: [resolve, tooling]`; permissions as today plus
`id-token: write`). In order:

1. Harden runner (the allow-list below).
2. Check out upstream at `needs.resolve.outputs.sha` to the workspace root,
   with `persist-credentials: false`.
3. Set up Python, then `Collect timings and prior summaries` unchanged, into
   the runner-private `$RUNNER_TEMP/ci-perf`. The scripts come from the
   trusted ref and are stdlib only (checked at upstream 4833f2e). The three
   hash outputs stay.
4. `Create agent user`: `create-codex-user@main` with `user: claude-agent`
   and `grant: none`.
5. `Create the scratch directory`: `sudo install -d -o claude-agent -g
   claude-agent -m 0700 "$RUNNER_TEMP/scratch"` (the namespace requires the
   bind; nothing uses it).
6. `Prepare the agent launch`: `claude-agent-launcher@main` with
   `grant: none`.
7. `Stage the collected data for the agent`: `sudo install -o claude-agent
   -g claude-agent -m 0644` copies each of the four collected files into
   `$RUNNER_TEMP/claude-agent`. Agent-owned copies come back through the
   import in step 10 without a warning each. The runner's own copies stay
   in `$RUNNER_TEMP/ci-perf`. This step must follow the launcher, which
   empties the landing directory.
8. `Analyze CI performance`: `anthropics/claude-code-action@v1` (the step
   block below). `CI_PERF_OUTPUT_DIR` for the agent is
   `$RUNNER_TEMP/claude-agent`.
9. `Reclaim workspace from the agent`: `reclaim-codex-workspace@main` with
   `user: claude-agent`, under `if: always() && steps.agentuser.outcome ==
   'success'`. It kills any survivor, removes the WIF ACL entries and the
   agent's config directory, and restores `.git/config`.
10. `Import the agent's files`: `import-codex-final@main`, `mode: dir`,
    `dest: $RUNNER_TEMP/ci-perf-agent`, gated on the reclaim.
11. `Assemble the evidence`: copies `report.md` and `findings.json` from the
    import into `$RUNNER_TEMP/ci-perf`, if present. Nothing else the agent
    wrote is kept.
12. `Show report` and `Retain CI evidence outside Git`, unchanged but for
    reading only the runner-private `$RUNNER_TEMP/ci-perf` (below).

Outputs: the three hashes, and `analysis_conclusion: ${{
steps.analysis.outputs.conclusion }}`. `source_sha` and `artifact_name` are
removed.

**`publish`** (`needs: [resolve, analyze]`; `if:` unchanged):

- checks out `needs.resolve.outputs.sha`;
- downloads `ci-perf-${{ github.run_id }}-${{ github.run_attempt }}`,
  computed in `publish`;
- nothing else changes.

### triage after the change

Three jobs: `context → agent → land`.

**`context`** (new; trusted; `permissions: contents: read, actions: read`;
no agent, no third-party code):

- carries today's trigger condition (:101-104) and the `UPSTREAM_RUN_*`
  env;
- runs three steps moved verbatim from `agent`: `Resolve the upstream run
  attempt`, `Resolve the trusted triage context` and `Validate the triage
  context`;
- outputs `attempt`, `sha`, `exact`, `channel` and `thread_ts`.

**`agent`**:

- `needs: context`, and `if: always() && needs.context.result !=
  'skipped' && needs.context.result != 'cancelled'`;
- permissions: contents and actions read, plus `id-token: write`;
- the data and agent steps below carry `if: needs.context.result ==
  'success'`.

In order:

1. Harden runner.
2. Check out upstream at `needs.context.outputs.sha` to the workspace root
   (`fetch-depth: 0`, `persist-credentials: false`).
3. `Download failed logs from upstream run` and `Collect installed package
   versions`, unchanged but for taking `needs.context.outputs.attempt`.
   They write `triage/` in the workspace.
4. `Record the checked-out SHA` (the start SHA for emit-landing).
5. `Create agent user`, `Create the scratch directory` and `Prepare the
   agent launch`, as for ci-perf.
6. `Run Claude triage agent`: `anthropics/claude-code-action@v1` (below).
   The landing directory the agent writes is `$RUNNER_TEMP/claude-agent`.
7. `Reclaim workspace from the agent`, as for ci-perf.
8. `Import the landing files`: `import-codex-final@main`, `mode: dir`, with
   the default `dest` of `$RUNNER_TEMP/landing`.
9. `Compose landing manifest`, unchanged. It reads the imported
   `$RUNNER_TEMP/landing`, and adds one problem: "the triage context could
   not be resolved" when `needs.context.result != 'success'`. A failed
   `context` job therefore becomes a manifest with `error.fail_run` and no
   `slack` entry. `land` posts Slack only for a manifest's
   `slack.text_file` (agents land/action.yml:1284), and with no issue or
   PR bound its error report goes to the run log (:1531-1558). So the run
   fails red with the error in its log and **no Slack post**. That is what
   a failed attempt resolution produces today too (the composer at
   :1048-1056 writes no `slack` without the agent's file); there is no
   automatic Slack fallback, and this design adds none.
10. `Emit landing manifest`, unchanged.

It has no job outputs.

**`land`** (`needs: [context, agent]`; `if:` unchanged):

- `slack-channel: ${{ needs.context.outputs.channel ||
  secrets.SLACK_CHANNEL_ID }}`;
- `slack-thread-ts: ${{ needs.context.outputs.thread_ts }}`;
- `comment-numbers: event`, with neither `pr-number` nor `issue-number`
  set (P4, from step 4).

**Why `comment-numbers: event`.** Under this boundary a `runner`
compromise of the agent job can upload any manifest after the composer
ran. Today's validator constrains `issues[]` (repository, count, labels,
assignees) but checks a generic `comments[]` entry's shape only
(agents validate_manifest.py:663-680). The review's probe of c23eb39
passed a manifest with two comments on this repository's #17 and #18
under triage's exact inputs, and `land` would post them to its `repo`
(land/action.yml:1017). The machine account's token reaches this
repository through the `MARVIN_TOKEN` fallback
(triage-test-failures.yml:1127-1136). The agents design adds
`comment-numbers` in its step 3: with `event`, the validator refuses any
number other than the `pr-number` or `issue-number` input
(untrusted-agent-job.md, Land enforces what the composers enforced,
item 1). Triage passes neither, so every `comments[]` entry is refused,
while its one permitted fork action still travels through `issues[]`
under the existing allow-lists. The agents design currently says triage
keeps `*` (its line 415); that sentence changes with this dependency.
Waiting for agents' default to flip would not be enough, since triage
must set the value itself.

With the destination out of the agent job, an `::add-mask::` the agent
emits in its own job masks nothing `land` reads. That fixes 4773277's
consequence. The agent step no longer echoes the agent's text either
(claude-code-action's sanitized log). The compose step still prints agent-
chosen key names inside its `::error::` lines. Under this boundary that
buys only annotations in the agent job's own log.

4773278 is **not** fixed: `context` trusts the dispatcher's run exactly as
today. `context` is where its check goes (Not this design).

### The agent step

Triage (ci-perf differs only as noted):

```yaml
- name: Run Claude triage agent
  id: claude
  if: needs.context.result == 'success'
  uses: anthropics/claude-code-action@v1
  with:
    # WIF identifiers, not secrets (Design → Federation rules); filled in
    # from the Console when the rule exists.
    anthropic_federation_rule_id: fdrl_…            # actions-triage
    anthropic_organization_id: <Meridian's organization id>
    anthropic_service_account_id: svac_…            # actions-triage
    anthropic_workspace_id: wrkspc_…                # the triage capped workspace
    # The job token, so the action mints no Claude App token.
    github_token: ${{ github.token }}
    path_to_claude_code_executable: ${{ steps.launcher.outputs.executable }}
    classify_inline_comments: "false"
    claude_args: >-
      --model opus
      --setting-sources user
      --add-dir ${{ runner.temp }}/claude-agent
    settings: |
      {
        "env": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        "permissions": {
          "allow": [ …today's list, with /landing/ → /claude-agent/ and
                     `git -C inspect_ai <verb>` → `git <verb>` … ],
          "deny": [ …today's list, unchanged … ]
        }
      }
    prompt: |
      …today's prompt, with the paths above…
```

- **ci-perf.** `claude_args` also carries today's `--allowedTools
  Bash,Read,Edit,Write,Grep,Glob`. `settings` is `{"env":
  {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CI_PERF_OUTPUT_DIR":
  "<runner.temp>/claude-agent", "CI_PERF_RUN_URL": "<run url>"}}`. The
  wrapper's environment allow-list (claude:145-175) drops every other
  variable, so today's `forward-env` pairs travel as settings `env`.
- **No `anthropic_api_key`.** The action ignores WIF when a key is set
  (workload-identity.ts:128-136).
- **`classify_inline_comments: "false"`**, as in agents: nothing may be
  posted from the buffered post-step.
- **Implementation checks.** The implementation confirms that settings
  `env` reaches the CLI under the wrapper: the ci-perf agent finds
  `CI_PERF_OUTPUT_DIR`, and harden-runner reports no blocked telemetry
  call. If the telemetry variable is not honoured there, harden-runner
  still blocks the host. The cost is annotations, and a line in Not this
  design.

### Federation rules (Anthropic Console; cannot be verified from this repository)

An org admin (Ransom) configures the following. The IDs are addresses, not
secrets, and go into the workflows as literals. The organization's GitHub
Actions issuer already exists (agents' rule uses it).

| Resource | ci-perf | triage |
|---|---|---|
| Service account | new, e.g. `actions-ci-perf`, a member of **only** the capped workspace that holds `CI_PERF_ANTHROPIC_API_KEY` today | new, e.g. `actions-triage`, a member of only the triage key's capped workspace |
| Rule `subject_prefix` (exact, no `*`) | `repo:meridianlabs-ai/actions:ref:refs/heads/main` | the same |
| `audience` | `https://api.anthropic.com` | the same |
| `claims` (exact strings) | `repository_id: "1032501921"`, `repository_owner_id: "196480008"`, `workflow_ref: "meridianlabs-ai/actions/.github/workflows/inspect-ai-ci-perf.yml@refs/heads/main"` | the same IDs, `workflow_ref: "…/triage-test-failures.yml@refs/heads/main"` |
| `condition` (CEL) | `claims.event_name in ["schedule", "workflow_dispatch"]` | `claims.event_name in ["workflow_run", "workflow_dispatch"]` |
| `workspace_id` | the ci-perf capped workspace | the triage capped workspace |
| `oauth_scope` | `workspace:inference` | the same |
| `token_lifetime_seconds` | 600 | 600 |

Why these values:

- **The subject and claims.** The Console docs define `subject_prefix`
  without `*` as exact, `claims` as exact top-level strings, and `condition`
  as CEL over `claims`; all matchers are ANDed (platform.claude.com, WIF
  reference → Rule matching semantics, read 2026-09-30). `schedule`,
  `workflow_run` and a dispatch from `main` all carry
  `ref: refs/heads/main`. A dispatch from any other branch of this
  repository runs that branch's copy of the workflow, and its `sub` and
  `workflow_ref` name the branch, so the rule refuses it. A workflow in
  this repository other than these two has another `workflow_ref`. Neither
  workflow has a `workflow_call` trigger, so no other workflow can run
  them as its own jobs.
- **`workspace:inference`** grants Messages (with streaming and token
  counting), Models and the OpenAI-compatible chat endpoint. Anything else
  (Files, Skills, Managed Agents) answers 403 (WIF reference → OAuth
  scopes). That is the restriction the broker enforced by route, now
  enforced by Anthropic.
- **600 seconds** is the Console wizard's default. The minted token
  lives the lesser of that and twice the JWT's remaining life (WIF → Token
  lifetime and refresh). claude-code-action rewrites the JWT every 240 s,
  so each CLI refresh finds an unused JWT. A shorter lifetime would race
  the rewrite and hit `jti_reused`.
- **The capped workspaces** are the ones the keys come from today, so the
  spend caps carry over. The admin confirms that each still has a cap, and
  that nothing else lives in it: no other keys, files or skills.

**P3: no other rule accepts these jobs' tokens.** agents' rule
`fdrl_01GpNgJm9jE6ZfvcqoJYQL2Y` matches CEL `repository_owner ==
"meridianlabs-ai"` alone (agents design/architecture.md:109-116,
2098-2103). Once these jobs hold `id-token: write`, their JWTs satisfy it.
The agent can read the JWT file and knows the rule ID, which is public in
every agent workflow. So it could mint tokens in the "Claude Code Agent"
workspace, outside this workflow's cap.

Before step 4, that rule is narrowed to the workflows that use it. As
found in local clones' default branches on 2026-09-30, those are:

- agents' `claude.yml`, `claude-review.yml`, `claude-auto.yml` and
  `claude-auto-review.yml` at `refs/heads/main`, matched on
  `job_workflow_ref`;
- inspect_flow's `inspect-ai-main-failure.yml` and `inspect-update.yml`,
  and ts-mono's `dependabot-fix.yml`, matched on `workflow_ref`.

This is a Console change on agents' rule, one of the two agents-side
dependencies (the other is P4). It follows the agents design's own rule for
new trusts ("never matches on `repository_owner` alone"). The admin
checks every other rule on the GitHub issuer the same way. Open question 3
covers the alternative of accepting that workspace's cap too.

### Ordering prerequisites

- **P1: the Claude GitHub App is not installed on meridianlabs-ai/actions.**
  The App exchange mints a token from any JWT with audience
  `claude-code-github-action`, and needs no secret (the agents design →
  claude-code-action's token and modes). `claude[bot]` has posted on this
  repository (most recently 2026-09-14), so the App is installed here.
  Until it is removed, the new `id-token: write` would let `runner` mint a
  token with contents, pull-requests and issues write on this repository,
  where its workflows live. There are two ways to meet P1:
  - (a) Remove this repository from the App's installation once agents
    steps 1 and 4 have shipped. After them every reusable workflow the
    stubs here call passes `github_token`, and none needs the App.
  - (b) Wait for agents step 6, which uninstalls the App everywhere.

  Open question 2 asks which. Either way, a canary checks it before step 5
  merges: a `push`-triggered job with `id-token: write` on a scratch branch
  of this repository tries the exchange, and it must fail.
- **P2: the two service accounts and rules exist** (the table above).
- **P3: agents' rule is narrowed** (above).
- **P4: agents' land has the `comment-numbers` input** (agents design step
  3), so triage's `land` can pass `comment-numbers: event` (Design →
  triage after the change). Step 4 needs it; it can also ship alone
  earlier, as soon as the input exists, since it constrains only what
  triage never posts.

### harden-runner egress

- **`analyze`** keeps its list minus `pypi.org` and
  `files.pythonhosted.org`, since nothing in `analyze` installs from PyPI
  after `tooling` moves out. The list is `api.anthropic.com`,
  `api.github.com`, `github.com`, `claude.ai`, `downloads.claude.ai`,
  `registry.npmjs.org` and `release-assets.githubusercontent.com`.
- **The triage `agent` job** keeps its list unchanged.
- **Who uses each host:**
  - `api.anthropic.com`: the CLI inside the agent's namespace (inference
    and the `/v1/oauth/token` exchange). Today only the broker called it.
  - `claude.ai` and `downloads.claude.ai`: the launcher's installer.
  - `registry.npmjs.org`: the action's `bun install --production`.
  - `release-assets.githubusercontent.com`: `setup-bun`.

  This set is the one observed in audit mode when ci-perf last ran
  claude-code-action (run 34880688791, ci-perf :45-65). The OIDC endpoint
  is Actions infrastructure, which harden-runner allows without listing.
  The namespace shares the host network namespace, so the firewall applies
  to the agent.
- **`resolve`, `tooling` and `context`** get no harden-runner: they hold
  nothing that reaches beyond the job, as the scheduled suites.

The comments that say `api.anthropic.com` is "reached only by the broker"
change.

### Post-agent reads (4773274)

`Show report` and `Retain CI evidence outside Git` read only
`$RUNNER_TEMP/ci-perf`, which the agent never had. The directory holds the
runner's collected files, plus `report.md` and `findings.json` as copied
from `import-codex-final`. That import opens each entry `O_NOFOLLOW`
relative to the directory, and accepts only regular single-link files owned
by `claude-agent`, within 1 MiB each and 8 MiB in all. The triage compose
and emit steps likewise read only the imported `$RUNNER_TEMP/landing`.

Is it still worth doing when a `runner` compromise gains only the agent's
credential plus the OIDC request token? Yes, as hygiene. A planted link in
the old shape would make an honest runner step publish the link's target:
for example its own `/proc/self/environ`, with the OIDC request token and
`ACTIONS_RUNTIME_TOKEN`, into a public summary or artifact. That leak is
bounded by the job's end and by the table above. But the import already
exists and runs in every agents Claude job, so the cost is one step.

### Pinning the Claude Code install

- **After steps 4 and 5** the launcher installs the exact `claudeCodeVersion`
  claude-code-action pins, root-owned, through the vendor installer (see
  Current behaviour). No npm lifecycle script runs. The checksum comes from
  the release manifest on the same host as the binary. A repository-pinned
  hash would be stronger, and belongs in the launcher for every caller (Not
  this design).
- **Before then**, step 1 replaces `isolated-agent`'s `npm install -g`
  (4773473, High, open while the key exists). It uses the same vendor
  installer at an exact version literal: `sudo env -i
  HOME=/opt/claude-code PATH=/usr/sbin:/usr/bin:/sbin:/bin bash
  "$installer" 2.1.285`, then `chown -R root:root` and a `--version` check,
  as the launcher does, with `/opt/claude-code/.local/bin` put first on the
  PATH the setup and run steps resolve `claude` from. `env -i` also clears
  `SUDO_USER`, which the
  installer otherwise refuses (install.sh, "do not run this installer with
  sudo").

### SECURITY.md after the change

The text lands with the step that makes it true.

- **Trust boundaries, `workflow_dispatch` inputs (:47-52)**, step 2:
  "ci-perf resolves its ref to a branch or tag of UKGovernmentBEIS/inspect_ai
  in a job that runs nothing else, and refuses commits and pull-request
  refs".
- **Trust boundaries, "Upstream `main` is trusted" (:172-176)**, step 2:
  add upstream branches and tags, which ci-perf may analyze, pushed only
  by upstream's write-access holders.
- **Guarantees, first bullet (:182-224)**:
  - Step 4 splits it per workflow and rewrites triage's; step 5 rewrites
    ci-perf's; step 6 merges them.
  - The rewrite: the agent jobs hold nothing the agent may not have. That
    is the read-only job token and the model credential, the declared
    exception: a WIF token for the workflow's own spend-capped workspace,
    scoped to `workspace:inference`, living at most 600 s, minted under a
    rule that matches only that workflow on `main`. They also hold the
    OIDC request token that mints it, which nothing else exchanges.
  - The agent runs as `claude-agent` through agents' launcher, in its own
    PID and mount namespace, as defence in depth. The rest of the bullet
    (the redirect-target check) stays.
- **Guarantees, the two-job bullet (:225-232)**, steps 2-3: `publish`
  checks out the SHA the `resolve` job resolved, and `land`'s Slack
  destination comes from the `context` job. Neither comes from the agent
  job.
- **Guarantees, the Slack and validator bullet (:244-253)**, step 4: the
  validator's refusals add generic comments (`comment-numbers: event`
  with no event number), and the bullet says the refusals hold "whatever
  the agent job uploaded", including after a `runner` compromise.
- **Guarantees, egress and unscreened output (:273-289)**, step 6.
  "There is no reusable secret to screen for" becomes: what the outputs can
  carry is what the agent can read. That is:
  - the read-only job token (expired at job end);
  - public inputs;
  - a minted Anthropic token, usable by whoever copies it for the rest of
    its life (at most 600 s), for inference in the capped workspace;
  - an identity JWT, single-use and audience-bound.

  The unscreened outputs are:
  - the job summary, which is visible while the job runs;
  - the artifact, which is downloadable once uploaded;
  - the issue body and Slack text, which are posted after the job.

  Screening stays pointless for the old reason: a screen on the agent's
  runner is the agent's to defeat.
- **By design, first bullet (:295-302)**, step 6: the agent can spend the
  capped workspace for the job's lifetime, plus the remaining life of the
  last token it minted. Runner.Worker holds no model secret. The .NET
  socket text goes.
- **By design, second bullet (:303-305)**, step 2: replaced by the ref
  rule above and the reason: the job that runs the ref's scripts holds a
  credential (agents' SECURITY.md tier-2 rule, "restricted to trusted
  refs").
- **Adding or changing a workflow, item 2 (:321-328)**, step 6: the model
  credential is Anthropic WIF through claude-code-action with
  `github_token: ${{ github.token }}`, launched through agents'
  `claude-agent-launcher`. The job requests `id-token: write` for that
  alone, under a dedicated rule pinned to the workflow file on `main`, and
  no agent job references a long-lived model key.
- **Verification notes (:343-377)**, step 4: the redirect-target check is
  repeated. The triage `settings:` block changes paths, and the installed
  release becomes the action's pin (2.1.285 at fd1c128) instead of npm's
  latest. The note is updated with the result.

AGENTS.md changes with step 6: the test paragraph's description of
`tests/test_model_broker.py`, and "Rules the tests enforce", bullet 2.

### Retiring the keys

- **Revoke** each key in the Anthropic Console once its workflow has run
  green on WIF: triage after step 4, ci-perf after step 5. Revoking, not
  just deleting the GitHub secret, is the rotation. The key sat in
  Runner.Worker's memory and within root's reach on every run, so it is
  treated as possibly copied. Until the revoke, reverting the step's PR
  restores the broker path.
- **Delete** the `CI_PERF_ANTHROPIC_API_KEY` and `TRIAGE_ANTHROPIC_API_KEY`
  secrets in step 6. A search on 2026-09-30 found them referenced only in
  these two workflows among the local clones of Meridian repositories. The
  step's PR repeats the search with `gh search code --owner
  meridianlabs-ai`.
- The test suites' `ANTHROPIC_API_KEY` is separate and unchanged.

## Alternatives considered

- **Option 1: keep `isolated-agent` and add WIF to it.** The action would:
  - fetch the OIDC token for `https://api.anthropic.com` as `runner`;
  - start a background process that rewrites the token file every 4
    minutes for the agent's lifetime, and stop it afterwards;
  - write the profile directory, grant the agent read on both, and pass
    `ANTHROPIC_FEDERATION_RULE_ID`, `…_ORGANIZATION_ID`,
    `…_SERVICE_ACCOUNT_ID`, `…_WORKSPACE_ID`,
    `ANTHROPIC_IDENTITY_TOKEN_FILE`, `ANTHROPIC_CONFIG_DIR` and
    `ANTHROPIC_PROFILE` through `env -i`.

  It keeps the workspace layout, the prompts and most of
  `tests/test_model_broker.py`. But it re-implements, in a second
  repository, what claude-code-action and `agent_ns.py` already do. It
  keeps two launchers with different isolation mechanisms (ancestor ACLs
  here, a namespace in agents), and still needs its own install pin and
  its own stdout fix for 4773277. If it were made shared, it would belong
  in agents beside `openai-wif-proxy`, and it would then be a second
  Claude path there too. Rejected in favour of one path.
- **Keep the broker and drop `runner`'s sudo** (option (ii) in the agents
  design's Not this design). Runner.Worker holds the key from job start,
  and any `runner` process can reach it through the same-uid diagnostic
  socket, so this does not close the leak. The broker also stays a second
  credential mechanism. Rejected (the task says so, and the socket is
  why).
- **Keep `inspect_ai/` nested and give the launcher a repository at the
  root** (a sparse checkout of this repository, or a `git init`). This
  keeps the prompts' paths and the triage allow-list rules. It adds a
  checkout or a synthetic repository whose only purpose is to satisfy the
  composites' `.git` assumptions, and a later launcher change could
  reasonably break it. The checkout under investigation at the root is the
  shape the launcher is built for.
- **An agents companion: launcher support for a workspace with no
  repository under `grant: none`.** This would skip the `.git` snapshot,
  the HEAD record, the origin reset and the `.git/config` by-value check.
  It needs changes and tests in three composites for two callers, where
  moving the checkout needs none.
- **ci-perf: restrict the ref, or move the scripts where they hold
  nothing?** The task offered either. Moving only the scripts is not
  enough: the credentialed job still checks out the ref, and the agent
  reads its `SKILL.md` and can run its scripts with unrestricted Bash. The
  ref's content steers a job that holds the model credential. Agents'
  tier-2 rule asks for trusted refs in exactly this case. So the ref is
  restricted. Separately, `pytest` from PyPI, the one third-party install,
  moves to `tooling`.
- **ci-perf: move the collection into its own job, so the hashes come from
  outside `analyze`.** It would make the data-integrity check hold against
  a `runner` compromise of `analyze`. That buys correctness of numbers in a
  fork issue whose prose the agent writes anyway, and costs a second
  checkout and an artifact hand-over whose name `analyze` could squat.
  Rejected; the hashes are hygiene against the agent user.
- **ci-perf: keep `source_sha` from `analyze`, and have `publish` check it
  is in upstream `main`'s history** (compare API, status `identical` or
  `ahead`). This would also work, but it adds an API call and a rule about
  API statuses. The `resolve` job needs no check, and also gives
  `tooling` and `analyze` one SHA.
- **triage: land re-derives the Slack destination itself.** `land` holds
  the Slack and marvin credentials. Running the context resolution there
  would put artifact downloads and parsing of another run's data into the
  most privileged job. The `context` job follows the agents pattern: a
  trusted job's outputs carry the decisions to land.
- **A custom OIDC audience for these rules**
  (`anthropic_oidc_audience`), so agents' owner-only rule does not accept
  the agent's JWT. `runner` can mint a JWT for any audience, and under this
  boundary `runner` is untrusted. The Console-side narrowing (P3) is the
  control; the audience would be decoration.

## Compatibility and migration

- **Stored formats.** The triage manifest, the `landing` artifact and its
  name are unchanged. The `ci-perf-<run>-<attempt>` artifact holds the same
  six files. Any other file the agent writes is no longer uploaded. The
  `-published` artifact is unchanged. None of this repository's workflows
  has a viewer schema or generated types.
- **`workflow_dispatch` input `inspect_ai_ref`.** Raw SHAs, `refs/pull/*`
  and other names that are not a branch or tag of upstream are refused in
  `resolve`, with a message naming the rule. Default and schedule
  behaviour are unchanged. Anyone who dispatched ci-perf on a commit
  pushes a branch or tag upstream instead.
- **Triage `run_id`**: unchanged.
- **Job graph.** New jobs `resolve` and `tooling` (ci-perf) and `context`
  (triage). The triage `agent` job loses its `channel` and `thread_ts`
  outputs, and ci-perf's `analyze` loses `source_sha` and `artifact_name`.
  Nothing outside these workflows reads them. No required checks name
  these jobs. The extra jobs add roughly a minute of wall time.
- **Composite actions.** `.github/actions/model-broker` and
  `.github/actions/isolated-agent` are deleted in step 6. A search of
  every Meridian clone on 2026-09-30 found only these two workflows
  referencing them, by relative path. `tests.yml` keeps running on
  `.github/actions/**` until then, and drops the path when nothing is
  left under it.
- **Agent behaviour.** Same prompts but for paths, same `opus` alias, same
  tools. The CLI becomes the action's pinned release (2.1.285 today),
  where it was npm's latest, so it moves when `@v1` moves.
- **The agent step now depends on the action's actor checks.** A cron or
  scheduled run whose actor is not a User with write access would fail
  the agent step. Today's actors are `ransomr` and `epatey`. If a machine
  account ever edits the cron, `allowed_bots` is the fix.
- **A failed `context` job** fails the triage run with the error in
  `land`'s log and posts nothing to Slack, as a failed attempt resolution
  does today. No Slack fallback is added.
- **Rollback.** Steps 1-3 revert independently. Steps 4 and 5 each revert
  to the broker path until their key is revoked.

## Security

What untrusted input reaches the new code, and how it is handled:

- **`inspect_ai_ref`** (write-access dispatcher) reaches only `resolve`,
  through `env:`. It is shape-checked, and used as a quoted argument after
  a fixed `refs/heads/` or `refs/tags/` prefix. The result must be 40 hex
  before it is written with a heredoc. Nothing is parsed as syntax.
- **`run_id` and the upstream run's artifact** reach `context`, handled
  exactly as today by the moved steps. 4773278 remains.
- **Agent-written files** reach `runner` only through `import-codex-final`:
  no-follow, owner-checked and capped. The compose step and `publish`
  treat them as untrusted, as today. The data the agent was given is
  republished from the runner's copy.
- **A forged triage manifest** (uploaded by a compromised agent-job
  `runner` after the composer ran) meets only `land`'s validator. With
  triage's existing inputs plus `comment-numbers: event` (P4), every
  posting field outside the one permitted fork issue action is refused:
  generic comments, inline review comments, replies, thread resolutions,
  PR fields and the hand-back. Without P4, generic comments on this
  repository would land as the machine account, which is why step 4
  waits for it.
- **The upstream checkout** is untrusted for triage (4773278 can select a
  fork SHA) and trusted for ci-perf (restricted ref). Neither is loaded as
  configuration (`--setting-sources user`), and it is read-only to the
  agent.
- **The model credential.** The agent can read the JWT file and its
  minted token, and can therefore:
  - spend inference in its workflow's capped workspace while the job runs,
    plus up to 600 s after its last mint;
  - write the token into any output it controls, where a reader may spend
    it for its remaining life.

  It cannot use Files, Skills or Managed Agents (`workspace:inference`),
  another workspace (the rule's `workspace_id`, and P3), or a token from
  another workflow or branch (the rule's claims). This is the decision's
  trade: its value is bounded, not its secrecy.
- **The OIDC request token** stays in `runner`'s step environment: not the
  agent's (`env -i` and the namespace), but within reach of a `runner` or
  root compromise. It mints JWTs for any audience:
  - Anthropic accepts them for this workflow's rule (the exception);
  - the Claude App exchange has no installation after P1;
  - no other relying party is known to trust this repository. The agents
    design's step 5 checklist covers PyPI, npm and other OIDC trusts org
    wide. This repository publishes nothing itself (its release workflows
    are reusable, and run with the caller's claims).
- **claude-code-action and the agents composites** run as `runner` with
  sudo, referenced by moving refs. A compromised release gains what a
  `runner` compromise gains: the rows above, and outputs no trusted job
  believes. It could break or skew a run.
- **What the broker enforced that WIF enforces differently:**
  - "Messages only" becomes `workspace:inference`: a scope, which also
    admits Models and the OpenAI-compatible chat endpoint;
  - "loopback, this job" becomes a 600 s token lifetime plus the rule's
    claims;
  - the per-run token in outputs was worthless, and a minted token in an
    output is worth up to 600 s of capped inference.

  These are the losses the decision accepted.
- **Findings, after step 6:**
  - 4773473: closed by the launcher's pinned install, and by step 1 in the
    meantime.
  - 4773275: closed by `tooling` (step 2).
  - 4773274: closed by the import (step 5), hygiene under this boundary.
  - 4773277: its consequence is gone (step 3 moves the destination, and
    step 4 removes the stdout echo).
  - 4773278: open (Not this design).
  - The unreported ref gap: closed by `resolve` (step 2).
  - Generic `comments[]` in a forged triage manifest (found in review
    round 1 of this design; not a scan finding): closed by
    `comment-numbers: event` (P4, step 4).

## Testing

All in `tests/`, run by `tests.yml` on every change to these paths. No new
test needs network, Docker or a model: `gh` and `git` are stand-ins on
`PATH`, as the existing tests do.

- **`tests/test_ci_perf_workflow.py`:**
  - `resolve` (the script lifted from the YAML, with a `git` stand-in that
    prints `ls-remote` lines): default `main`; a branch; an annotated tag,
    where the peeled SHA wins; a lightweight tag; a name that is both,
    refused; `refs/pull/5614/head`, `pull/5614/head`, a 40-hex SHA, `..`,
    a leading `-`, a newline, `@{` and 201 characters, all refused with no
    output written; `ls-remote` output with extra lines that are not exact
    matches, ignored.
  - The job graph: `publish` checks out `needs.resolve.outputs.sha` and
    computes the artifact name. No `needs.analyze.outputs` expression
    reaches a `ref:`, a `name:` or a `run:`. `tooling` holds
    `contents: read` only, references no secret and has no `id-token`.
    `analyze` needs `tooling`.
  - `analyze`:
    - references no secret, and holds exactly its four read permissions
      plus `id-token: write`;
    - its claude-code-action step passes `github_token: ${{ github.token
      }}`, the four WIF literals, no `anthropic_api_key`,
      `path_to_claude_code_executable` from the launcher,
      `--setting-sources user` and `--add-dir` in `claude_args`, and
      settings `env` with the two ci-perf variables;
    - create-codex-user and the launcher use `grant: none`;
    - the harden-runner list is exactly the seven hosts, and sudo is not
      disabled;
    - no step references `isolated-agent` or `model-broker`.
  - `Show report` and the upload read only `$RUNNER_TEMP/ci-perf`: a test
    runs the step with a symlink planted in the landing directory, and
    shows the link's target is not read.
  - The existing publish-side tests stay.
- **`tests/test_triage_workflow.py`:**
  - The context tests (attempt, resolve, validate) move their lifting to
    `jobs.context`.
  - `land`'s `slack-channel` and `slack-thread-ts` read `needs.context`,
    and no `needs.agent.outputs` expression remains in `land`.
  - The agent job structure, as for `analyze`, with the triage event list.
  - Compose: a failed `context` becomes `error.fail_run` with no `slack`
    entry (the test asserts both, since no Slack post is the intended
    behaviour), and compose reads the imported directory.
  - `land` passes `comment-numbers: event` and neither `pr-number` nor
    `issue-number`. Against the land validator at the ref triage uses
    (the existing `TRIAGE_VALIDATOR_REF` cross-check), forged manifests
    are refused under triage's exact inputs: generic `comments[]` on this
    repository (the review's #17/#18 case), `review_comments`,
    `replies`, `resolve_threads`, `pr` and `handback: true`. A manifest
    with one permitted `issues[]` action still passes. The cross-check
    xfails on a validator ref without the input, as it already does for
    older contract changes.
  - The permission-rule tests: the landing path becomes `/claude-agent/`,
    and `git -C inspect_ai` becomes `git`. The write-vector cases stay.
  - `test_agent_job_timeout_matches_the_broker_lifetime` and the
    broker-pointing tests are removed with the broker.
- **`tests/test_model_broker.py`** is deleted in step 6, with both actions.
  Until then step 1 adds a test that the install recipe names an exact
  version and runs under `env -i`.
- **A repository-wide test** (step 6): no workflow references
  `CI_PERF_ANTHROPIC_API_KEY` or `TRIAGE_ANTHROPIC_API_KEY`.
- **A per-workflow permission test** (steps 4 and 5), scoped to
  `inspect-ai-ci-perf.yml` and `triage-test-failures.yml` only: in each,
  exactly one job has `id-token: write` (`analyze`, triage `agent`), and
  that job's claude-code-action step passes `github_token: ${{
  github.token }}`. The five existing reusable-workflow caller jobs keep
  their grants and are outside this test: `claude.yml` jobs `claude` and
  `claude-auto`, `claude-review.yml` job `review`, and `claude-auto.yml`
  jobs `ci-fix` and `review-fix`. They call agents' reusable workflows,
  whose own jobs run WIF and pass `github_token` (agents steps 1 and 4);
  removing their grant would break those workflows.
- **Hosted proof, per workflow, after its migration step** (recorded in
  the step's PR):
  1. **It reaches the model.** For ci-perf: a `workflow_dispatch` on
     `main` with `dry_run: true`. For triage: the next failed scheduled
     run, or, if none comes within three days, a dispatch against the
     latest failed run, accepting one comment on its existing issue. The
     run is green, and the Console's authentication history shows the
     exchange under the new rule.
  2. **It reaches nothing else.**
     - The log shows the launcher's namespace-phase isolation line.
     - harden-runner's summary shows no blocked call beyond known
       telemetry.
     - A dispatch from a scratch branch of this repository, with an
       identical file, fails at the agent step with `authentication_error`,
       and the history shows the claims mismatch.
     - The rule in the Console reads `workspace:inference` and 600 s.
     - For P1, the scratch-branch canary's App exchange fails.
     - For P3, agents' rule reads the narrowed condition, and a dispatch of
       the unchanged agents stubs still succeeds.
  3. **The redirect-target verification** (SECURITY.md → Verification
     notes) is repeated with the triage settings of step 4 and the CLI
     release the action pins.

## Implementation plan

Each step is one PR in this repository unless it says otherwise. Each runs
`python3 -m pytest -q tests` and `actionlint` on the changed files, and
updates the SECURITY.md text it makes true.

1. **Pin isolated-agent's Claude Code install (4773473, interim).**
   - Change: `.github/actions/isolated-agent/action.yml`, the install recipe
     under Design → Pinning, at the exact version claude-code-action pins
     at the time.
   - Test: `tests/test_model_broker.py`.
   - Independent of everything else, and deleted again in step 6. If steps
     4 and 5 are expected within days, it can be skipped.
2. **ci-perf: `resolve` and `tooling` jobs, the restricted ref, and
   `publish` from `resolve`.** This closes 4773275 and the ref gap while the
   key still exists.
   - Files: `.github/workflows/inspect-ai-ci-perf.yml`,
     `tests/test_ci_perf_workflow.py`, SECURITY.md (trust boundaries;
     By design, second bullet; the two-job guarantee).
   - Independent of agents.
3. **triage: the `context` job, and `land` reading from it.** This fixes
   4773277's consequence.
   - Files: `.github/workflows/triage-test-failures.yml`,
     `tests/test_triage_workflow.py`, SECURITY.md (the Slack-destination
     sentence of the two-job guarantee).
   - Independent of agents.
   - *Prerequisites, before steps 4 and 5 (not PRs here):*
     - P2: Ransom creates the two service accounts and rules;
     - P3: Ransom narrows agents' rule, with a line in agents' design and
       SECURITY.md recording it;
     - P1: this repository leaves the Claude App's installation, either
       after agents steps 1 and 4 or at agents step 6 (open question 2),
       verified by the canary;
     - P4 (before step 4 only): agents step 3 ships land's
       `comment-numbers` input, and the agents design's sentence that
       triage keeps `*` is corrected.
4. **triage on WIF and the launcher.**
   - The job shape under Design → triage and the agent step: the layout,
     the prompt paths, the settings, `id-token: write`, `land`'s
     `comment-numbers: event` (if it has not shipped alone already) and
     the new comments.
   - Tests: `tests/test_triage_workflow.py`.
   - SECURITY.md: the split first guarantee, and Verification notes
     re-checked.
   - After a green run: revoke `TRIAGE_ANTHROPIC_API_KEY` in the Console.
5. **ci-perf on WIF and the launcher.**
   - The shape under Design → ci-perf, with its egress list.
   - Tests: `tests/test_ci_perf_workflow.py`.
   - SECURITY.md: ci-perf's half of the first guarantee.
   - After a green run: revoke `CI_PERF_ANTHROPIC_API_KEY`.
   - Steps 4 and 5 can ship in either order.
6. **Retire the broker.**
   - Delete `.github/actions/model-broker/`, `.github/actions/isolated-agent/`
     and `tests/test_model_broker.py`.
   - `tests.yml`: drop `.github/actions/**`.
   - Add the repository-wide test.
   - Rewrite SECURITY.md (Guarantees merged, egress and unscreened output,
     By design first bullet, Adding a workflow item 2) and AGENTS.md (the
     test paragraph, "Rules the tests enforce" bullet 2).
   - Delete the two secrets, after the organization-wide search.

## Open questions

1. **Which upstream refs may ci-perf analyze?**
   - (a) Any branch or tag of UKGovernmentBEIS/inspect_ai, which keeps
     testing a tooling branch before it merges.
   - (b) `main` only.

   Recommendation: (a). Upstream branches are pushed only by its
   write-access holders, whom SECURITY.md already trusts for `main`.
2. **How to meet P1 for this repository?**
   - (a) Remove meridianlabs-ai/actions from the Claude App's installation
     as soon as agents steps 1 and 4 have shipped. That unblocks steps 4-5
     early, and loses the App's features on this repository only, which
     the agents step-5 checklist can confirm are unused here.
   - (b) Wait for agents step 6.

   Recommendation: (a). It is reversible, and the agents design's
   reasoning applies unchanged.
3. **agents' owner-only rule (P3).**
   - (a) Narrow it to the seven workflows that use it.
   - (b) Leave it, and accept that these two agents can also spend the
     "Claude Code Agent" workspace up to its cap.

   Recommendation: (a). Without it, "limited by the spend-capped
   workspace" is true only if that workspace is capped too. (a) costs one
   line in the rule for each future direct caller.

## Not this design

- **4773278**: manual triage accepts a fork PR's workflow run as the
  trusted context producer. The fix is a check in the new `context` job:
  the selected run's `event` is `schedule`, its `head_repository` is this
  repository and its `head_branch` is the default branch.
- **The Actions cache findings of group 2b**: 4773471 (scheduled tests) and
  4773276 (inspect_swe nightly), which need a workflow-level `cache-mode:
  read`.
- **The scheduled suites' provider keys** (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY` and the rest, in `inspect-ai-scheduled-tests.yml` and
  `inspect-swe-nightly-tests.yml`).
- **The VSIX validator** (4773470).
- **A repository-pinned sha256 for the Claude Code binary**, in agents'
  launcher, for every caller. Today the checksum comes from the vendor's
  manifest on the download host.
- **Pinning claude-code-action to a SHA** (also in the agents design's Not
  this design).
- **4773340 (namespace escape through host cron)** applies to the launcher
  wherever it runs. It is agents' to fix (`/etc/cron.deny`), and under
  this boundary it reaches only the agent's own credential.
- **`CI_PERF_SCHEDULED=1` never reaches the ci-perf agent**: `env -i` drops
  it today, and the wrapper's allow-list would too. The skill reads it as
  "no user is present". Settings `env` could carry it; this is a behaviour
  change to decide separately.
- **Agent-chosen text in the compose step's `::error::` lines.** After step
  3 it can only annotate the agent job's own log; quoting or escaping it is
  cosmetic.
