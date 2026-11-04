## How contributions work

There are two paths:

- **Qualified contributors**: maintainers and individuals recorded (by
  account id) in [`.github/qualified.yml`](.github/qualified.yml) — may open
  PRs directly.

- **Everyone else**: start from an issue a maintainer has labeled `accepted`
  before writing code, and reference it from your PR (`Fixes #NNN`). This
  applies regardless of your contribution history here, and regardless of
  who filed the issue — what matters is that a maintainer has accepted it.
  PRs that aren't linked to an accepted issue may be closed; if a linked
  issue is later accepted, reopen the PR.
  **Exception:** trivial documentation fixes (typos, broken links — docs
  files only, under 25 changed lines) are always welcome directly.

Whatever your path, check for an existing open PR addressing the same
problem before opening yours: duplicates are closed in favor of the earlier
PR unless a maintainer has asked for an alternative.

Issues a maintainer has labeled `deferred` are decisions not to prioritize
that work right now. PRs against a deferred issue are closed —
comment on the issue with new evidence or demand if you think it should be
revisited.

**Why:** like most open-source projects, we now receive a large volume of
unrequested, often agent-generated PRs. Reviewing a PR is time-consuming;
agreeing on a direction in an issue first is much easier. This policy spends
our review time on work we've agreed we want and spares you writing code we
can't merge.
