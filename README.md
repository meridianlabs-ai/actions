# actions
Global GitHub actions

## Reusable release workflows

Shared release automation for Meridian repos (see `Meridian/Release Process.md` in the ops vault for the full plan). Standardized on Release Please, authed by the "Meridian Release Bot" GitHub App (org secrets `RELEASE_PLEASE_APP_ID` / `RELEASE_PLEASE_APP_PRIVATE_KEY`). Pin callers to `@v1`.

- **`.github/workflows/pr-title-lint.yml`** — enforce Conventional Commits on PR titles (we squash-merge, so the PR title becomes the commit). Allowed types are read from `conventional-commit-types.json`.
- **`.github/workflows/release-please-vscode.yml`** — Release Please + publish for a VS Code extension (Marketplace + Open VSX + `.vsix` release asset).
- **`.github/workflows/release-please-python.yml`** — Release Please for Python packages (release-please only; publishing stays in the repo's `publish.yaml`, auto-triggered by the GitHub Release — PyPI trusted publishing can't run from a cross-repo reusable workflow).
- **`conventional-commit-types.json`** — single source of truth for commit types, feeding both PR-title lint and the release-please changelog sections.
- **`scripts/gen-release-please-config.sh`** — generate a repo's committed `.release-please-config.json` from the shared type list.
- **`slack-approval-ping/`** — composite action: post an "awaiting approval" ping to `#release-approvals`, @-mentioning approvers, **deep-linking to the exact paused run**. Call it from a non-gated notify job in the *same run* as the gated publish (so `run-url` = `github.run_id` is the paused run). Reads `slack-ids.json` via `$GITHUB_ACTION_PATH`.
- **`slack-release-announce/`** — composite action: post a release *announcement* (release notes → Slack mrkdwn) to a per-repo Slack webhook. Distinct from the approval ping; call it after a release is created, passing the reusable workflow's `html_url`/`body` outputs + a per-repo `SLACK_WEBHOOK_URL`.

Both release-please workflows post a **Slack "awaiting approval"** ping when a release is cut, if the secret `SLACK_APPROVALS_WEBHOOK_URL` is set (a **dedicated approvals channel** webhook, separate from any general update stream). GitHub's own deployment-review notifications are unreliable, so this ensures approvers hear about it. The ping fires from the release-please job (before the environment gate), links to the repo's Actions, no-ops silently if the secret is absent, and **@-mentions the approvers** passed via the `approvers` input (space/comma-separated GitHub logins), resolved to Slack IDs through **`slack-ids.json`** (login → Slack member ID; fill in the `TODO`s). Since the Slack app is incoming-webhook only (no bot token), mentions in the approvals channel are used rather than true DMs.

Minimal caller examples are in each workflow's header comment.

## Workflows

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
