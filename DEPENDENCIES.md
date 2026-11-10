# Dependencies

*This document tells how the Inspect repositories, inspect_ai included, install and change dependencies. There are two ways to install. PR CI and the slow and nightly tests install from the lock file. The canaries install the newest release of each dependency. The data is correct on 2026-11-10. The shared CI actions use uv 0.13.5.*

**Summary:**
- PR CI and the slow and nightly tests install exactly the versions in `uv.lock`. A repository that needs unreleased inspect_ai changes first moves inspect-ai in `uv.lock` to inspect_ai `main`.
- A release gets into `uv.lock` after it has been on PyPI for 7 days. This time is the cooldown. An emergency entry is the one exception. A compromised release is usually found and removed in less than 7 days. Provider SDKs and Inspect packages have a 6-hour cooldown.
- By design, the canaries install the newest release of each dependency. They ignore the lock file and the 7-day cooldown. No other workflow does this, apart from the known problems and the two intentional exceptions in the next section.
- The canary jobs hold no secrets, so a bad release has nothing to steal there.
- When a canary fails, the usual cause is a change in a dependency, not a change in our code. There is one other cause. The canary of a repository that needs unreleased inspect_ai changes tests with the newest PyPI release of inspect-ai. That canary fails while the repository's `main` uses an inspect_ai change that is not released. The inspect_sentinel canary is different. It installs inspect_ai `main` and inspect_scout `main` from git.

## How each flow installs dependencies

PR CI and the slow and nightly tests use the same install, so they share one column. The canaries install the newest releases. The other flow does not, apart from the known problems and the two intentional exceptions below. The next section tells which repositories need unreleased inspect_ai changes.

| Dependency | PR CI, slow tests and nightly tests | Canaries |
|---|---|---|
| PyPI packages: third-party packages, provider SDKs and Inspect packages | Exactly the versions and hashes in `uv.lock`. uv does not resolve versions. | The newest release. uv resolves the versions at install time. |
| inspect-ai, in repositories that use only released inspect_ai | Exactly the release in `uv.lock`. | The newest PyPI release. |
| inspect-ai, in repositories that need unreleased inspect_ai | CI first moves `uv.lock` to the newest commit on inspect_ai `main`. Then CI installs exactly `uv.lock`. | The newest PyPI release. `UV_NO_SOURCES=1` makes uv ignore the git source. Exception: the inspect_sentinel canary installs inspect_ai `main` and inspect_scout `main` from git. |
| Release build tools: `build` and the build backend (setuptools and setuptools_scm in inspect_ai, hatchling and hatch-vcs in the other repositories) | Exactly the versions and hashes in `scripts/build-dist/requirements.txt` (`--require-hashes`). The build uses `--no-isolation`. | Not installed. |
| The build backend for the editable install | **Not locked.** uv resolves `[build-system] requires` at install time, without `uv.lock` or hashes. The cooldown applies. | uv resolves it at install time. |
| JavaScript in ts-mono, inspect_vscode, the inspect_ai viewer and the inspect_scout viewer | Exactly the pnpm lock file (`--frozen-lockfile`). | inspect_vscode: the newest releases, with no lock file. No other canary installs this JavaScript. |
| JavaScript in inspect_viz | **Not exactly `yarn.lock`.** CI runs plain `yarn install`, without `--frozen-lockfile`. If `yarn.lock` does not agree with `package.json`, yarn resolves new versions and the job does not fail. | The newest releases. No lock file. |
| Actions cache | A job that holds secrets, an `id-token` or write permissions restores no cache. This rule is for the test, build and release jobs. This document does not include the agent jobs (the Claude workflows). | No cache. |
| A `uv.lock` that does not agree with `pyproject.toml` | The job fails. The message is *"uv.lock is missing or out of date. Run uv lock and commit it."* In a repository that needs unreleased inspect_ai, the message is *"The lockfile at `uv.lock` needs to be updated"*. | Not applicable. The canaries use no lock file. |

This table does not tell how the lock files change. For that, see "Cooldown settings" and "Change dependencies" below.

The commands that each column runs:
- **PR CI, slow tests and nightly tests:** The shared action `meridianlabs-ai/actions/install-python-dependencies` runs with `versions: locked`. This action runs `uv sync --locked`. A repository that needs unreleased inspect_ai first runs `uv lock --check` and then `uv lock --upgrade-package inspect-ai`. Its `.github/actions/uv-sync` action does these steps.
- **Canaries:** Most canaries use the same shared action with `versions: unlocked` and `project-uv-settings: ignore`. The action then runs `uv pip install --no-config --exclude-newer false --upgrade -e ".[<extras>]"`. The canary workflow sets `UV_NO_SOURCES=1` for this step. The canaries for inspect_flow, inspect_steward, inspect_ranges, inspect_viz and inspect_vscode use their own install commands. The inspect_flow and inspect_steward canaries keep the repository's uv settings. Thus their 6-hour entries stay in effect. The inspect_steward canary also keeps the git source for inspect-harbor.
- The canary jobs hold no secrets. They use a read-only token and no cache. They are in a separate repository, [meridianlabs-ai/canaries](https://github.com/meridianlabs-ai/canaries). Only the alert job of that repository holds a secret, the Slack credentials. The alert job installs no dependencies.

The workflows in each flow:

| Flow | Workflows |
|---|---|
| PR CI, slow tests and nightly tests | The PR CI of each repository. This includes the inspect_ai slow tests. The workflow `inspect-ai-scheduled-tests.yml` in meridianlabs-ai/actions runs every 2 hours. It installs the inspect_ai `uv.lock` at the tested commit. The workflow `inspect-swe-nightly-tests.yml` in meridianlabs-ai/actions runs at 08:00 UTC. It first moves the inspect_swe `uv.lock` to the newest commit on inspect_ai `main`, as the inspect_swe PR CI does. Then it installs exactly `uv.lock`. |
| Canaries | meridianlabs-ai/canaries has one canary for each repository, apart from ts-mono and inspect_charxiv. The inspect_ai and inspect_flow canaries run every 2 hours. The other canaries run at 05:30 UTC. A failed canary runs every 2 hours until it passes. |

Known problems that the design does not permit:
- inspect_harbor `harbor-parity.yml` and inspect_vscode `proxy-routes.yml` install the newest release of each dependency. Other jobs in these repositories restore Actions caches. Canaries for these two repositories already exist. Delete the two workflows. See inspect_harbor#224 and inspect_vscode#235.
- inspect_viz installs JavaScript with plain `yarn install`, without `--frozen-lockfile`. See inspect_viz#59.
- No repository locks the build backend for the editable install. See actions#172.
- The inspect_flow action `.github/actions/setup` sets no uv version. See inspect_flow#913.
- The `make install` target in inspect_harbor and inspect_sandboxes runs plain `uv sync`. See inspect_harbor#223 and inspect_sandboxes#113.

Two intentional exceptions:
- The inspect_sentinel PR CI installs the test environment of the LiteLLM guardrail with `versions: unlocked`. The jobs are `litellm-guardrail` and `py-build` in `build.yaml`. The LiteLLM version is fixed. The other packages are the newest releases, with no cooldown. These jobs hold no secrets.
- The inspect_flow workflow `inspect_ai_main.yaml` installs inspect_flow with inspect_ai `main`. It runs every 3 hours and holds no secrets. The inspect_flow PR CI does not use inspect_ai `main`. inspect_flow gets inspect-ai from PyPI. At this time, `inspect_ai_main.yaml` runs `uv sync --dev --upgrade` and then installs inspect_ai `main` from git. `uv sync --dev --upgrade` installs the newest releases that the cooldown permits.

## Where inspect-ai and other Meridian packages come from

**Why there are two ways.** Some Meridian repositories frequently need a change in inspect_ai before inspect_ai publishes the change to PyPI. For example, an inspect_scout feature can need a new inspect_ai API. That API can be on inspect_ai `main` some days before the next release. These repositories must build and test with inspect_ai `main`, so they get inspect-ai from git. But a package on PyPI cannot depend on a git commit. Thus the release of each of these repositories names a PyPI release of inspect-ai.

Other repositories use only released inspect_ai. They get inspect-ai from PyPI, as for all other dependencies. This method is simpler. `uv.lock` names one release, and Dependabot updates it.

Thus, for each repository, the question is: **does the repository need unreleased inspect_ai changes?**

| | Needs unreleased inspect_ai changes | Uses only released inspect_ai |
|---|---|---|
| **Repositories** | inspect_scout, inspect_sentinel, inspect_swarm and inspect_swe. inspect_sentinel also needs unreleased inspect_scout changes. | inspect_flow, inspect_harbor, inspect_sandboxes, inspect_petri, inspect_ranges, inspect_steward, petri_bloom, petri_dish and inspect_charxiv. |
| **Declaration in `pyproject.toml`** | A minimum version in `[project] dependencies`. Also, `[tool.uv.sources]` points to inspect_ai `main` in git. | A minimum version in `[project] dependencies`. |
| **inspect-ai in PR CI** | The newest commit on inspect_ai `main`. Before the install, CI runs `uv lock --upgrade-package inspect-ai`. After the install, CI restores `uv.lock`. The cooldown does not apply, because a git commit has no PyPI upload time. | The release that `uv.lock` names. The 6-hour cooldown applies. |
| **inspect-ai on a developer's computer** | The commit that `uv.lock` names. To install the newest commit on `main`, run `uv sync --upgrade-package inspect-ai --extra dev`. | The release in `uv.lock`. |
| **inspect-ai in the release** | The release PR sets `UV_NO_SOURCES=1`. Thus the release PR tests the newest PyPI release that is at least 6 hours old. Then `release-pin-deps.yml` increases the `inspect-ai>=` minimum version to that release. Thus a feature that needs unreleased inspect_ai cannot ship until inspect_ai releases the change. | A PyPI release that satisfies the minimum version in `[project] dependencies`. The published package does not contain `uv.lock`. `uv.lock` controls only the installs in CI and in a checkout. Nothing increases the minimum version automatically, and no CI job tests it (actions#173). |
| **Who updates inspect-ai** | CI moves `uv.lock` on each run and does not commit the change. After an inspect_ai change is merged to `main`, nobody must update the committed `uv.lock`. The next CI run uses the change. To start a CI run, run the checks again or push a commit. Dependabot ignores inspect-ai. Thus the commit in the committed `uv.lock` changes only when a person commits a new `uv.lock`. This step is optional. The commit in `uv.lock` can be much older than `main`. Only a new local `uv sync` installs that older commit. | Dependabot updates it each day. The exception is inspect_flow. In inspect_flow, the agent workflow `inspect-update.yml` updates inspect-ai each week. An inspect_ai release can need changes in inspect_flow. |

inspect_ai installs itself from its own checkout.

Exceptions:
- inspect_ai locks inspect-scout and inspect-sentinel from PyPI. These two packages are in the inspect_ai `dev` extra.
- inspect_steward also gets inspect-harbor from inspect_harbor `main` in git. `uv.lock` records the commit. No automation updates this commit. Update it manually.
- inspect_viz does not depend on inspect-ai.

## Cooldown settings

A cooldown is the minimum time that a release must be on PyPI before uv can use it.

| Setting | Value | Location |
|---|---|---|
| Default cooldown | 7 days | `[tool.uv] exclude-newer = "7 days"` in the `pyproject.toml` of each Python repository. |
| Short cooldown | 6 hours | `[tool.uv.exclude-newer-package]`: `<package> = "6 hours"`. Use it only for a direct dependency that is a model provider SDK or an Inspect package. In inspect_sandboxes, a sandbox provider SDK is also permitted. All other packages keep the 7-day cooldown. This includes packages that a provider needs, for example torch, accelerate, the AWS SDKs and `certifi`. |
| Emergency entry | A fixed time | `[tool.uv.exclude-newer-package]`: `<package> = "<RFC 3339 time>"`. The entry is temporary. Remove it when it is 7 days old. At that time, a test fails. |
| Dependabot | 7 days | The `uv` entry in `.github/dependabot.yml`: `cooldown.default-days: 7`. The 6-hour packages are in `cooldown.exclude` and have their own group. |
| Canaries | None | The canaries install with `--exclude-newer false`. Most canaries also use `--no-config`, which removes the per-package entries. Exception: the inspect_flow and inspect_steward canaries keep their 6-hour entries. |
| JavaScript in ts-mono and inspect_vscode | 7 days | `minimumReleaseAge: 10080` in `pnpm-workspace.yaml`. The npm Dependabot cooldown is 7 days. |
| uv in CI | 0.13.5 | The shared action `meridianlabs-ai/actions/setup-uv`. The inspect_ai `build.yml` also sets `UV_VERSION: 0.13.5`. Exception: the inspect_flow action `.github/actions/setup` sets no uv version. |
| Minimum uv for a checkout | `>=0.13.4` | `required-version` in `[tool.uv]`, in all Python repositories, inspect_ai included. An older uv does not sync or lock the project. uv 0.13.4 is the first version that does not trust the compromised emSign root (GHSA-hq85-5cpj-mr62). |

The 6-hour packages of each repository are in its `pyproject.toml`, under `[tool.uv.exclude-newer-package]`.

Two important facts:
- **Dependabot counts its cooldown in full days.** Thus Dependabot cannot set a 6-hour cooldown. Dependabot does not apply its cooldown to the 6-hour packages, and uv applies the 6-hour cooldown. Thus a Dependabot PR can propose a release that is less than 6 hours old. In that case, `uv lock` fails in the PR. This is expected. The next daily Dependabot run succeeds.
- **A git source has no PyPI upload time.** Thus the cooldown does not apply to a git source. A repository that needs unreleased inspect_ai changes gets the newest commit on `main` immediately. The cooldown continues to apply to all the dependencies of that commit.

## Change dependencies

These procedures apply to all repositories, inspect_ai included. Each repository's `CONTRIBUTING.md` gives its setup command and links to this file.

**Set up a checkout.** Run the `uv sync --locked` command that the repository's `CONTRIBUTING.md` gives, for example `uv sync --locked --extra dev`. Use uv 0.13.4 or later. `uv sync --locked` installs exactly `uv.lock`, as CI does. If `uv.lock` does not agree with `pyproject.toml`, the command fails. A plain `uv sync` does not fail. It resolves the versions again and changes `uv.lock`. Do not use `pip install -e ".[dev]"` or `uv pip install` to set up the checkout. Both commands ignore `uv.lock`. `pip` also ignores the cooldown.

**Add or change a dependency.**
1. Edit the dependency in `pyproject.toml`, or run `uv add <package>`.
2. Run `uv lock`. uv resolves the new dependency with the cooldown, so you get the newest release that the cooldown permits.
3. Commit `pyproject.toml` and `uv.lock` together.

**Correct the CI error "uv.lock is missing or out of date".** In some repositories, the message is "The lockfile at `uv.lock` needs to be updated". This error occurs when `pyproject.toml` changed and `uv.lock` did not. Run `uv lock` and commit `uv.lock`. Do not run `uv lock --upgrade`. That command updates every package in the lock file.

**Use a newer release after its cooldown.** Run `uv lock --upgrade-package <package>` and commit `uv.lock`. Alternatively, wait for the daily Dependabot PR. This changes only `uv.lock`. Users of the published package can continue to install an older release.

**Use a new API or a necessary fix from a newer release.** If the code of the package needs the newer release, increase the minimum version of the dependency in `pyproject.toml`, for example `inspect-ai>=<version>`. Change the minimum version where `pyproject.toml` declares the dependency. Do not move an optional dependency into `[project] dependencies`. Then run `uv lock` and commit both files. If only the tests need the newer release, do not change the minimum version.

In inspect_ai, the model provider SDKs are optional. They are in the `dev` extra, not in `[project] dependencies`. For these SDKs, inspect_ai checks the installed version when the provider starts. For example, see `MIN_VERSION` and `verify_required_version` in `src/inspect_ai/model/_providers/providers.py`. For such an SDK, increase `MIN_VERSION` and the minimum version in the `dev` extra to the same version. Then run `uv lock` and commit all the changed files.

**Use a release before its cooldown ends.** Usually, wait for the cooldown. If the release is urgent, for example a security fix, add an emergency entry:
1. In `pyproject.toml`, under `[tool.uv.exclude-newer-package]`, add `<package> = "<timestamp>"`. Use an RFC 3339 time a short time after the upload time of the release. The package must be a direct dependency. If the package has a 6-hour entry, do not replace that entry. Wait for the 6-hour cooldown.
2. Do not add the entry to `.github/dependabot.yml`.
3. Run `uv lock --upgrade-package <package>`.
4. In inspect_ai only: if `sync_models.py` locks the package, add the same entry to the inline script metadata of `sync_models.py`. Then run `uv lock --script src/inspect_ai/model/_model_data/sync_models.py --no-config --upgrade-package <package>`.
5. Commit all the changed files.

**Remove an emergency entry.** A test in `tests/test_dependency_cooldown.py` fails when an emergency entry is 7 days old. Remove the entry at that time. The 7-day cooldown then includes the release. Do not remove the entry earlier. If you do, `uv lock` moves the package back to the previous release.
1. Delete the entry from `pyproject.toml`.
2. Run `uv lock --upgrade-package <package>`. A plain `uv lock` does not always remove the entry from `uv.lock`, and then CI fails.
3. In inspect_ai only: if you added the entry to `sync_models.py`, delete it there and run the `uv lock --script` command from step 4 above.
4. Commit all the changed files.

**Add a package to the 6-hour cooldown.** Use the 6-hour cooldown only for a direct dependency that is a model provider SDK or an Inspect package. In inspect_sandboxes, a sandbox provider SDK is also permitted.
1. In `pyproject.toml`, under `[tool.uv.exclude-newer-package]`, add `<package> = "6 hours"`. If the table does not exist, add it.
2. In `.github/dependabot.yml`, add the package to `cooldown.exclude` of the `uv` entry.
3. In the same file, add the package to the 6-hour group. In inspect_ai, the group is `provider-sdks`. In the other repositories, the group is `six-hour-cooldown`. If `cooldown.exclude` or the group does not exist, add it. Copy the form from the inspect_scout `.github/dependabot.yml`.
   - Exception: do not do steps 2 and 3 for a package that is in the `ignore` list of the `uv` entry. Dependabot does not update that package. For example, Dependabot ignores inspect-ai in inspect_scout, inspect_sentinel, inspect_swarm, inspect_swe and inspect_flow.
4. Run `uv lock`. The new entry changes `uv.lock`.
5. Run `uv run pytest tests/test_dependency_cooldown.py`. The test makes sure that `pyproject.toml` and `.github/dependabot.yml` agree.
6. Commit all the changed files.

**Update the release build tools.** The release build tools are in `scripts/build-dist/requirements.txt`, with exact versions and hashes. `scripts/build-dist/requirements.in` names the tools. Dependabot updates `requirements.txt` each week, with the 7-day cooldown. To update the file manually:
1. Go to `scripts/build-dist`.
2. If you add or remove a tool, edit `requirements.in`.
3. Run `uv pip compile --no-config --generate-hashes --python-version 3.11 --python-platform x86_64-unknown-linux-gnu --exclude-newer "7 days" --only-binary :all: --upgrade requirements.in -o requirements.txt`.
4. Commit the changed files.

**A Dependabot PR fails to lock a 6-hour package.** Dependabot probably proposed a release that is less than 6 hours old. See "Two important facts" above. Do nothing. The next daily Dependabot run updates the PR. If the PR continues to fail, there is a real version conflict. Read the log of the Dependabot job.

**Get the newest inspect_ai `main` in a repository that needs unreleased inspect_ai changes.** Run `uv sync --upgrade-package inspect-ai --extra dev`. In inspect_sentinel, also add `--upgrade-package inspect-scout`. If inspect_ai `main` requires a different version of another package, uv also changes that package. All other packages keep their locked versions. It is optional to commit the changed `uv.lock`. CI always moves `uv.lock` to the newest commit on `main`.

**Work with a local inspect_ai checkout.** In a repository that needs unreleased inspect_ai changes, install your checkout over the locked inspect-ai:
1. Run `uv sync --locked --extra dev`.
2. Run `uv pip install --no-deps -e ../inspect_ai`. With `--no-deps`, uv installs only your checkout and resolves no versions.
3. If your inspect_ai checkout needs a package that `uv.lock` does not contain, the import fails. Install that one package by name: `uv pip install <package>`. Run the command in the repository directory, so that uv applies the cooldown. Do not use `--no-config` or `--exclude-newer false`.
4. Run commands with `.venv/bin/<command>` or `uv run --no-sync <command>`. A plain `uv run` or `uv sync` installs the locked inspect-ai again.

In inspect_sentinel, the same procedure applies to a local inspect_scout checkout. In step 2, use `../inspect_scout`.

The two commands in steps 2 and 3 are the only permitted uses of `uv pip install` in a checkout.
