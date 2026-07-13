# Releasing Meridian packages

This is the maintainer runbook for how Meridian (`meridianlabs-ai`) packages are
released. It is the single source of truth for the shared process; the release
automation it describes lives in this repo (`meridianlabs-ai/actions`) as
reusable workflows that every package repo calls.

Contributors don't need most of this — the one-paragraph version is in each
repo's `CONTRIBUTING.md`. Read on if you cut releases, review release PRs, or
maintain the automation.

- [How a release happens](#how-a-release-happens)
- [Conventional commits](#conventional-commits)
- [Cadence](#cadence)
- [Cutting a release](#cutting-a-release)
- [Who can release](#who-can-release)
- [Hotfix / maintenance-branch releases](#hotfix--maintenance-branch-releases)
- [Break-glass manual publish](#break-glass-manual-publish)
- [How it's wired](#how-its-wired)
- [Per-repo specifics](#per-repo-specifics)
- [`inspect_ai` (cross-org)](#inspect_ai-cross-org)

## How a release happens

Every repo uses [Release Please](https://github.com/googleapis/release-please).
The loop is the same everywhere:

1. You merge normal PRs to `main`. Release Please watches `main` and keeps a
   rolling **release PR** open, updating the version + `CHANGELOG.md` from the
   conventional-commit history as commits land.
2. When you're ready to ship, you **merge the release PR**. That tags the
   release and creates a GitHub Release.
3. The GitHub Release triggers the repo's **publish** workflow, which **pauses
   on an environment approval gate**.
4. A release approver approves the deployment → the package publishes (PyPI,
   npm, and/or the VS Code marketplace, depending on the repo).

You never hand-edit `CHANGELOG.md` or bump a version number. Release Please owns
both.

## Conventional commits

Because every repo **squash-merges**, the **PR title becomes the commit
message** Release Please parses. So the PR title must be a
[Conventional Commit](https://www.conventionalcommits.org/): `<type>: <description>`.

Only `feat:` and `fix:` appear in release notes and drive the version bump —
reserve them for user-facing changes. Everything else (`docs:`, `refactor:`,
`perf:`, `test:`, `build:`, `chore:`, `ci:`) is excluded from the notes.

The allowed types are defined once in
[`conventional-commit-types.json`](./conventional-commit-types.json) and enforced
on PR titles by the shared `pr-title-lint.yml` workflow.

## Cadence

**Weekly, on Tuesdays, by default.** Cut a release each Tuesday *if there's
anything to ship* — a quiet week means no release (no empty version bumps).
Tuesday leaves three business days to catch regressions before the weekend.

Cadence is **merge timing, not configuration** — every repo runs the identical
automation, so a repo can ship more or less often with zero config change. See
[Per-repo specifics](#per-repo-specifics) for overrides (e.g. `inspect_harbor`
ships on-demand).

**Every release starts with a human clicking merge.** No auto-merge anywhere for
now.

## Cutting a release

1. Confirm the open release PR's version + notes look right.
2. Merge it (squash). This needs a code-owner approval — see
   [Who can release](#who-can-release).
3. Watch for the **publish approval** — you'll get a Slack ping in
   `#release-approvals` with a deep link to the paused run (GitHub's own
   deployment-review notifications are unreliable, so we ping Slack).
4. Approve the deployment. The package publishes.

## Who can release

Two independent gates — both are used:

1. **Merging the release PR (CODEOWNERS).** The release PR is the only PR that
   touches `CHANGELOG.md` and `.release-please-manifest.json`, so a `CODEOWNERS`
   entry on those paths requires a release approver's review on release PRs
   specifically, without affecting normal feature PRs.
2. **Approving the publish (environment reviewers) — the binding gate.** Publish
   jobs run in a GitHub deployment environment (`pypi`, `npm`, `marketplace`)
   with **required reviewers**. Even if someone else merges the release PR,
   nothing ships until a designated approver signs off. This is the real "who
   can ship" control.

Each repo's approvers are its domain maintainer(s) plus `@dragonstyle` and
`@jjallaire` as org-wide backstops (≥3 per repo). Bots and external contributors
are excluded.

## Hotfix / maintenance-branch releases

When `main` has unreleased work but one fix must ship now, you can't release off
`main` (that would drag everything queued there). Use a maintenance branch
rooted at the last released tag. Example: last shipped `1.4.0`, need `1.4.1`:

1. **Branch from the tag, not `main`:** `git checkout -b hotfix/1.4.x 1.4.0 && git push -u origin hotfix/1.4.x`
2. **Cherry-pick the fix** as a `fix:` commit and push it.
3. **Run Release Please against that branch:** repo → **Actions → Release → Run
   workflow**, leave the ref on `main` (that's just where the workflow file is
   read from) and set the **`target-branch`** input to `hotfix/1.4.x`. It opens
   a release PR proposing `1.4.1` computed only from that branch.
4. **Merge it** → tag `1.4.1` → approve the publish.
5. **Forward-port the fix to `main`** — Release Please will *not* do this for
   you; skip it and the next regular release regresses the bug.

If nothing else is unreleased on `main`, skip all this — just let the normal
`main` release flow ship the fix.

> **Governance note:** branch rulesets are scoped to `main`, so a `hotfix/**`
> release PR may not enforce code-owner review. The publish environment approval
> still applies, so nothing ships unreviewed — but treat the merge with the same
> care as a `main` release.

## Break-glass manual publish

Every Python repo keeps its `publish.yaml` `workflow_dispatch` as a fallback. It
can build+publish from any branch in a true emergency, but it sidesteps
Release Please's changelog/version automation and can desync the manifest.
Fire-extinguisher, not process.

## How it's wired

The release logic lives **once**, here in `actions`, as reusable workflows
(`on: workflow_call`). Each package repo has a thin caller. Callers pin to the
moving major tag **`@v1`**, which we advance deliberately (not `@main`), so a
change to a shared workflow doesn't hit every repo on its next release.

| Reusable workflow | Used by | Does |
| --- | --- | --- |
| [`release-please-python.yml`](./.github/workflows/release-please-python.yml) | PyPI repos | Release Please only. Publish stays **in the package repo's `publish.yaml`** (`on: release`), because PyPI trusted publishing (OIDC) rejects a cross-repo workflow. |
| [`release-please-vscode.yml`](./.github/workflows/release-please-vscode.yml) | `inspect_vscode` | Release Please **plus** publish (VS Marketplace + Open VSX), which is token-auth so it can run here. |
| [`pr-title-lint.yml`](./.github/workflows/pr-title-lint.yml) | all | Lints PR titles against the shared type list. |

**Auth is a GitHub App**, not a personal PAT. "Meridian Release Bot" (org
`meridianlabs-ai`, minimal permissions: Contents + Pull requests read/write)
mints a short-lived token per run via `actions/create-github-app-token`. The
App id/key are org secrets (`RELEASE_PLEASE_APP_ID`,
`RELEASE_PLEASE_APP_PRIVATE_KEY`), so every repo inherits them — zero per-repo
secret setup. The App token (unlike the default `GITHUB_TOKEN`) can trigger the
downstream workflows a release needs.

**Versioning** derives from the git tag via `hatch-vcs` (Python) or
`package.json` (node); Release Please owns the tag, so there's no static version
field. Bump strategy is per-repo, set in each repo's `.release-please-config.json`
(standard semver, or a pre-1.0 "feat = patch" mode). Force an exact version with
a `Release-As: X.Y.Z` footer on a commit to `main`.

**The file set in each Python repo:** a thin `release.yaml` caller, the retained
`publish.yaml` (now `on: release`), `.release-please-config.json`,
`.release-please-manifest.json`, `.github/CODEOWNERS`, and a `pr-title-lint.yml`
caller.

## Per-repo specifics

The process above is identical across repos. The deviations:

| Repo | Deviation |
| --- | --- |
| `inspect_harbor` | Ships **on-demand**, not weekly — merge its release PR whenever there's a releasable change. |
| `inspect_vscode` | `node` release type; publishes to the **VS Code Marketplace + Open VSX** (not PyPI); publish runs inside the reusable workflow. |
| `inspect_scout` | **Dual publish** (PyPI + the npm viewer lib), each with its own environment gate. **Depends on `inspect_ai`** — see below. |
| Bare-tag repos (`inspect_viz`, `inspect_swe`, `inspect_scout`, `petri_dish`, `petri_bloom`) | Tags have **no `v` prefix** (`0.4.2`, not `v0.4.2`); set per-package in the manifest config. |
| `inspect_ai` | Different org — see [below](#inspect_ai-cross-org). |

### `inspect_scout` → `inspect_ai` dependency

Scout's `main` tracks `inspect_ai`'s `main` (installed from git). A **published**
scout must not depend on a moving git ref — PyPI rejects direct-reference deps
and a git ref isn't reproducible. Two workflows enforce this on the release PR:

- **`release-dep-guard.yml`** — a **required check** that fails the release PR if
  any dependency still points at a git ref.
- **`release-pin-deps.yml`** — auto-rewrites `inspect_ai @ git+…@main` to the
  currently-shipped PyPI version (`inspect-ai>=X`) on the release PR, and
  `build.yaml` then validates against that shipped version. If scout genuinely
  needs an unreleased `inspect_ai`, the build fails loudly — the signal to ship
  `inspect_ai` first.

The authoritative detail lives in the header comments of those workflow files in
the `inspect_scout` repo; this is the summary.

## `inspect_ai` (cross-org)

`inspect_ai` lives in **`UKGovernmentBEIS`**, not `meridianlabs-ai`, so this plan
doesn't port over directly: the enablement steps (secrets, environment
reviewers, rulesets, App install) are admin-only in an org where Meridian isn't
admin, and the Meridian App/org secrets don't cross orgs. Releasing `inspect_ai`
through this system is a **joint effort with BEIS/AISI** using a BEIS-owned
credential and jointly-agreed approvers — handled separately from the Meridian
repos.
