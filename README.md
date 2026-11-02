# actions
Global GitHub actions. To report a vulnerability, read [SECURITY.md](SECURITY.md). For what these workflows trust and guarantee, read [THREAT_MODEL.md](THREAT_MODEL.md).

## Reusable release workflows

Shared release automation for Meridian repos. **See the [release runbook](https://github.com/meridianlabs-ai/meridian-docs/tree/main/releases) in meridian-docs** — how releases work, cadence, who-can-release, hotfixes, and per-repo specifics. Standardized on Release Please, authed by the "Meridian Release Bot" GitHub App (org secrets `RELEASE_PLEASE_APP_ID` / `RELEASE_PLEASE_APP_PRIVATE_KEY`). Pin callers to `@v1`.

- **`.github/workflows/pr-title-lint.yml`** — enforce Conventional Commits on PR titles (we squash-merge, so the PR title becomes the commit). Allowed types are read from `conventional-commit-types.json`.
- **`.github/workflows/release-please-vscode.yml`** — Release Please + publish for a VS Code extension (Marketplace + Open VSX + `.vsix` release asset). The publish job verifies that the built `.vsix` names exactly `extension-id` at the release tag's version before any PAT is used; see the header comment and [THREAT_MODEL.md](THREAT_MODEL.md).
- **`.github/workflows/release-please-python.yml`** — Release Please for Python packages (release-please only; publishing stays in the repo's `publish.yaml`, auto-triggered by the GitHub Release — PyPI trusted publishing can't run from a cross-repo reusable workflow).
- **`conventional-commit-types.json`** — single source of truth for commit types, feeding both PR-title lint and the release-please changelog sections.
- **`scripts/gen-release-please-config.sh`** — generate a repo's committed `.release-please-config.json` from the shared type list.
- **`slack-approval-ping/`** — composite action: post an "awaiting approval" ping to `#release-approvals`, @-mentioning approvers, **deep-linking to the exact paused run**. Call it from a non-gated notify job in the *same run* as the gated publish (so `run-url` = `github.run_id` is the paused run). Reads `slack-ids.json` via `$GITHUB_ACTION_PATH`.
- **`slack-release-announce/`** — composite action: post a release *announcement* (release notes → Slack mrkdwn) to a per-repo Slack webhook. Distinct from the approval ping; call it after a release is created, passing the reusable workflow's `html_url`/`body` outputs + a per-repo `SLACK_WEBHOOK_URL`.

Both release-please workflows post a **Slack "awaiting approval"** ping when a release is cut, if the secret `SLACK_APPROVALS_WEBHOOK_URL` is set (a **dedicated approvals channel** webhook, separate from any general update stream). GitHub's own deployment-review notifications are unreliable, so this ensures approvers hear about it. The ping fires from the release-please job (before the environment gate), links to the repo's Actions, no-ops silently if the secret is absent, and **@-mentions the approvers** passed via the `approvers` input (space/comma-separated GitHub logins), resolved to Slack IDs through **`slack-ids.json`** (login → Slack member ID; fill in the `TODO`s). Since the Slack app is incoming-webhook only (no bot token), mentions in the approvals channel are used rather than true DMs.

After Release Please runs, both release-please workflows rename its hard-coded `### ⚠ BREAKING CHANGES` heading to `### Breaking Changes` in the open release PR's body (which becomes the GitHub Release notes) and in the newest section of the release branch's root `CHANGELOG.md`. Callers need no change. While a release PR is open with a breaking change, every Release run rewrites the release PR, because Release Please no longer finds its body unchanged.

Minimal caller examples are in each workflow's header comment.

## Python CI actions

Two composite actions do the Python installs in CI. setup-uv holds the one uv
version for all callers; install-python-dependencies installs the packages, and
each call says where the versions come from. Pin both by the full commit SHA
with a `# vX.Y.Z` comment, as above; Dependabot bumps them. The self-test
workflow (`.github/workflows/self-test-python-actions.yml`) runs them on a
runner, and `tests/test_python_actions.py` tests their scripts.

- **`setup-uv/`** — composite action: install the pinned uv (and, if asked, a
  Python) with a stated Actions cache rule. Inputs: `python-version` (empty
  installs no Python) and `actions-cache` (`read-write`, `restore-only` or
  `none`). It runs once per job. A job that needs uv but no packages calls it
  directly.
- **`install-python-dependencies/`** — composite action: install Python
  dependencies from `uv.lock` (`versions: locked`), from a hashed requirements
  file (`versions: hashed-requirements`) or at the newest releases
  (`versions: unlocked`). It calls `setup-uv` once per job. A job with an
  unlocked step must set `cache-mode: none` or `read`, because an unlocked
  install can run code nobody has reviewed. See the action's `action.yml` for
  the full input list.

## Workflows

### Docs Link Check

Checks the published docs sites of inspect_ai, inspect_scout, inspect_viz, inspect_swe and inspect_flow for broken links and anchors every Monday at 12:00 UTC, and posts the result to Slack.

- **File**: `.github/workflows/docs-link-check.yml`, with lychee settings in `.github/lychee.toml`
- Reads each site's `sitemap.xml` and runs [lychee](https://github.com/lycheeverse/lychee) on every link of every page, including links between the sites. Changelogs are skipped.
- Checks the live sites, so a broken link shows up once it is deployed.
- The run summary lists each broken link and the page it is on. The Slack message links to it and does not ping the channel.
- A push that changes the workflow or `lychee.toml` runs the check without posting to Slack.

### Inspect AI Scheduled Tests

A GitHub Actions workflow that runs [inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai) slow tests on a regular schedule.

- **File**: `.github/workflows/inspect-ai-scheduled-tests.yml`
- **Documentation**: [INSPECT_AI_TESTS.md](INSPECT_AI_TESTS.md)
- **Purpose**: Execute slow tests (`--runslow`) that are excluded from regular CI

**Features:**
- Daily scheduled execution at 2 AM UTC
- Manual trigger capability
- 60-minute timeout for long-running tests
- Proper error handling and reporting
