# Dependencies

*This document gives the rules that the Inspect repositories, inspect_ai included, use to install and change dependencies. It gives only the rules that are the same in each repository. A repository can have exceptions and extra steps. Its `CONTRIBUTING.md` lists them.*

**Summary:**
- PR CI and the slow and nightly tests install exactly the versions in `uv.lock`.
- The in-house packages (`inspect-*`, `petri*`) come from PyPI, as all other dependencies do. No repository installs an in-house package from git.
- A release gets into `uv.lock` after it has been on PyPI for 7 days. This time is the cooldown. A compromised release is usually found and removed in less than 7 days.
- The in-house packages have no cooldown. Some provider SDKs and sandbox providers have a 6-hour cooldown. An emergency entry lets one release in before its cooldown ends.
- By design, the canaries install the newest release of each dependency. They ignore the lock file and the cooldown. The canary jobs hold no secrets, so a bad release has nothing to steal there.
- When a canary fails, the usual cause is a change in a dependency, not a change in our code.

## Scope

This document is about the main Python project of a repository. That is the `pyproject.toml` and the `uv.lock` at the root of the repository.

A repository can have other installs that have their own lock files or their own rules, for example a nested package, a script with its own lock file, or the agent jobs. This document does not describe them. See the repository's `CONTRIBUTING.md`.

## How each flow installs dependencies

PR CI and the slow and nightly tests use the same install, so they share one column.

| Dependency | PR CI, slow tests and nightly tests | Canaries |
|---|---|---|
| PyPI packages: third-party packages, provider SDKs and the in-house packages (`inspect-*`, `petri*`) | Exactly the versions and hashes in `uv.lock`. uv does not resolve versions. | The newest release. uv resolves the versions at install time. |
| Release build tools: `build` and the build backend | Exactly the versions and hashes in `scripts/build-dist/requirements.txt`. | Not installed. |
| The build backend for the editable install | **Not locked.** uv resolves `[build-system] requires` at install time, without `uv.lock` or hashes. The cooldown applies. | uv resolves it at install time. |
| JavaScript that uses pnpm | Exactly the pnpm lock file (`--frozen-lockfile`). | The newest releases, if the canary installs JavaScript. |
| Actions cache | A test, build or release job that holds secrets, an `id-token` or write permissions restores no cache. | No cache. |
| A `uv.lock` that does not agree with `pyproject.toml` | The job fails. The message tells you that `uv.lock` is out of date. | Not applicable. The canaries use no lock file. |

This table does not tell how the lock files change. For that, see "Cooldown settings" and "Change dependencies" below.

The commands:
- **PR CI, slow tests and nightly tests:** The shared action `meridianlabs-ai/actions/install-python-dependencies` runs with `versions: locked`. This action runs `uv sync --locked`. CI does not change `uv.lock` before the install.
- **Canaries:** The canaries are in a separate repository, [meridianlabs-ai/canaries](https://github.com/meridianlabs-ai/canaries). They install with no lock file and no cooldown. The canary jobs hold no secrets. They use a read-only token and no cache.

## Cooldown settings

A cooldown is the minimum time that a release must be on PyPI before uv can use it.

| Setting | Value | Location |
|---|---|---|
| Default cooldown | 7 days | `[tool.uv] exclude-newer = "7 days"` in `pyproject.toml`. |
| Short cooldown | 6 hours | `[tool.uv.exclude-newer-package]`: `<package> = "6 hours"`. Use it only for a direct dependency that is a model provider SDK or a sandbox provider (`e2b`, `modal`). All other third-party packages keep the 7-day cooldown. This includes packages that a provider needs, for example torch, the AWS SDKs and `certifi`. |
| In-house packages | None | `[tool.uv.exclude-newer-package]`: `<package> = false`. Use it for each in-house package (`inspect-*`, `petri*`) that the project depends on. We publish these packages, so we do not wait for them. |
| Emergency entry | A fixed time | `[tool.uv.exclude-newer-package]`: `<package> = "<RFC 3339 time>"`. The entry is temporary. A test fails when the entry is 7 days old. |
| Dependabot | 7 days | The `uv` entry in `.github/dependabot.yml`: `cooldown.default-days: 7`. The 6-hour packages and the in-house packages are in `cooldown.exclude` and in the short-cooldown group. Most repositories name that group `six-hour-cooldown`. inspect_ai names it `provider-sdks`. A repository that has no short-cooldown packages yet may not have the group; add the group when you add the first package. |
| Canaries | None | The canaries install with no lock file and no cooldown (`--no-config` or `--exclude-newer false`). Only the canaries use these two options. |
| JavaScript that uses pnpm | 7 days | `minimumReleaseAge: 10080` in `pnpm-workspace.yaml`. |
| uv in CI | One version | The shared action `meridianlabs-ai/actions/setup-uv` sets the uv version. |
| Minimum uv for a checkout | `>=0.13.4` | `required-version` in `[tool.uv]`. An older uv does not sync or lock the project. uv 0.13.4 is the first version that does not trust the compromised emSign root (GHSA-hq85-5cpj-mr62). |

Two important facts:
- **Dependabot counts its cooldown in full days.** Thus Dependabot cannot set a 6-hour cooldown. Dependabot does not apply its cooldown to the packages in `cooldown.exclude`. uv applies the 6-hour cooldown, or no cooldown for an in-house package. Thus a Dependabot PR can propose a release of a 6-hour package that is less than 6 hours old. In that case, `uv lock` fails in the PR. This is expected. The next Dependabot run succeeds.
- **The cooldown continues to apply to the dependencies of an in-house package.** A new release of an in-house package can go into `uv.lock` immediately. If that release needs a newer release of a third-party package, uv uses that release only after its cooldown.

A repository can tell Dependabot to ignore an in-house package when a different workflow updates that package. For example, inspect_flow ignores inspect-ai because its `inspect-update.yml` updates inspect-ai from PyPI. A package that Dependabot ignores must not be in `cooldown.exclude` or in that short-cooldown group. The repository's `CONTRIBUTING.md` tells about the exception.

## Change dependencies

These procedures apply to a repository that has a `pyproject.toml` and a `uv.lock` at its root. A repository can have extra steps. Its `CONTRIBUTING.md` lists them.

**Set up a checkout.** Run the `uv sync --locked` command that the repository gives, for example `uv sync --locked --extra dev`. Use uv 0.13.4 or later. `uv sync --locked` installs exactly `uv.lock`, as CI does. If `uv.lock` does not agree with `pyproject.toml`, the command fails. A plain `uv sync` does not fail. It resolves the versions again and changes `uv.lock`. Do not use `pip install -e ".[dev]"` or `uv pip install` to set up the checkout. Both commands ignore `uv.lock`. `pip` also ignores the cooldown.

**Add or change a dependency.**
1. Edit the dependency in `pyproject.toml`, in the place that declares it: `[project] dependencies`, an extra, or a dependency group.
2. Run `uv lock`. For a new package, uv selects the newest release that the cooldown and the version limits permit. For a package that is already in `uv.lock`, uv keeps the locked version if the version limits permit it.
3. Commit `pyproject.toml` and `uv.lock` together.

**Correct the CI error that `uv.lock` is out of date.** This error occurs when `pyproject.toml` changed and `uv.lock` did not. Run `uv lock` and commit `uv.lock`. Do not run `uv lock --upgrade`. That command updates every package in the lock file.

**Use a newer release after its cooldown.** Run `uv lock --upgrade-package <package>` and commit `uv.lock`. If `pyproject.toml` does not permit the newer release, first change the version there. A change of only `uv.lock` does not change the published package. Users of the published package can continue to install an older release.

**Use a new API or a necessary fix from a newer release.** If the code of the package needs the newer release, increase the minimum version of the dependency in `pyproject.toml`, for example `inspect-ai>=<version>`. Change the minimum version in the place that declares the dependency. Then run `uv lock` and commit both files.

**Use an unreleased change in an in-house package.** You can use an editable install of a local checkout for your own work. See "Work with a local checkout of an in-house package" below. But CI installs exactly `uv.lock`, so a PR cannot merge until:
1. The change is in a release of the in-house package on PyPI.
2. This repository's minimum version (`>=`) of the package in `pyproject.toml` is that release or later.
3. `uv.lock` names that release. Run `uv lock` after you change the minimum version, and commit both files.

**Use a release before its cooldown ends.** Usually, wait for the cooldown. If the release is urgent, for example a security fix, add an emergency entry:
1. In `pyproject.toml`, under `[tool.uv.exclude-newer-package]`, add `<package> = "<timestamp>"`. Use an RFC 3339 time a short time after the upload time of the release. The package must be a direct dependency. If the package has a 6-hour entry, replace that entry.
2. Do not add the emergency entry to `.github/dependabot.yml`. If you replaced a 6-hour entry and the package is in the two Dependabot lists for the 6-hour packages, remove it from those lists. "Add a package to the 6-hour cooldown" names the two lists.
3. Run `uv lock --upgrade-package <package>`.
4. Run `uv run pytest tests/test_dependency_cooldown.py`.
5. Commit all the changed files.

**Remove an emergency entry.** Do not remove the entry before the usual cooldown of the package includes the release. If you do, `uv lock` moves the package back to the previous release.
1. If the entry replaced a 6-hour entry: when the release is 6 hours old, change the entry back to `"6 hours"`. If you removed the package from the two Dependabot lists, put it back.
2. If the package had no 6-hour entry: when the release is 7 days old, delete the entry. The test fails at that time.
3. Run `uv lock --upgrade-package <package>`. A plain `uv lock` does not always remove the entry from `uv.lock`, and then CI fails.
4. Run `uv run pytest tests/test_dependency_cooldown.py`.
5. Commit all the changed files.

**Add a package to the 6-hour cooldown.** Use the 6-hour cooldown only for a direct dependency that is a model provider SDK or a sandbox provider (`e2b`, `modal`). Do not use it for an in-house package. See "Remove the cooldown of an in-house package".
1. In `pyproject.toml`, under `[tool.uv.exclude-newer-package]`, add `<package> = "6 hours"`. If the table does not exist, add it.
2. In `.github/dependabot.yml`, add the package to `cooldown.exclude` of the `uv` entry.
3. In the same file, add the package to the short-cooldown group (`six-hour-cooldown` in most repositories; `provider-sdks` in inspect_ai). If `cooldown.exclude` or the group does not exist, add it.
4. Run `uv lock`. The new entry changes `uv.lock`.
5. Run `uv run pytest tests/test_dependency_cooldown.py`. The test makes sure that `pyproject.toml` and `.github/dependabot.yml` agree. If the test fails, its message tells you what to change. For example, a package that Dependabot ignores must not be in the two lists.
6. Commit all the changed files.

**Remove the cooldown of an in-house package.** Do this for each in-house package (`inspect-*`, `petri*`) that the project depends on.
1. In `pyproject.toml`, under `[tool.uv.exclude-newer-package]`, add `<package> = false`. If the package has a 6-hour entry, replace that entry. If the table does not exist, add it.
2. In `.github/dependabot.yml`, add the package to `cooldown.exclude` of the `uv` entry and to the short-cooldown group, as for a 6-hour package. If Dependabot ignores the package, do not add it to these lists.
3. Run `uv lock`. The new entry changes `uv.lock`.
4. Run `uv run pytest tests/test_dependency_cooldown.py`. If the test still expects every `exclude-newer-package` entry to be `"6 hours"` or a date, update the test in the same change so it accepts `false` for in-house packages.
5. Commit all the changed files.

**Update the release build tools.** This procedure applies to a repository that has `scripts/build-dist`. The release build tools are in `scripts/build-dist/requirements.txt`, with exact versions and hashes. `scripts/build-dist/requirements.in` names the tools. To update the file manually:
1. Go to `scripts/build-dist`.
2. If you add or remove a tool, edit `requirements.in`.
3. The first lines of `requirements.txt` give the command that made the file. Run that command again with `--upgrade`. Put quotation marks around `7 days`.
4. Commit the changed files.

**A Dependabot PR fails to lock a 6-hour package.** Dependabot probably proposed a release that is less than 6 hours old. See "Two important facts" above. Do nothing. The next Dependabot run updates the PR. If the PR continues to fail, there is a real version conflict. Read the log of the Dependabot job.

**Work with a local checkout of an in-house package.** For local work only, you can install your checkout of an in-house package, for example inspect_ai, over the locked release. This install does not change `pyproject.toml` or `uv.lock`, and CI does not use it. See "Use an unreleased change in an in-house package".
1. Run the setup command, for example `uv sync --locked --extra dev`.
2. Run `uv pip install --no-deps -e <checkout>`, for example `uv pip install --no-deps -e ../inspect_ai`. With `--no-deps`, uv installs only your checkout and resolves no versions.
3. If your checkout needs a package that `uv.lock` does not contain, the import fails. Install that one package by name: `uv pip install <package>`. Run the command in the repository directory, so that uv applies the cooldown. Do not use `--no-config` or `--exclude-newer false`.
4. Run commands with `.venv/bin/<command>` or `uv run --no-sync <command>`. A plain `uv run` or `uv sync` installs the locked release again.

The two commands in steps 2 and 3 are the only permitted uses of `uv pip install` in a checkout.
