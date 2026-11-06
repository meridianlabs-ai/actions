## Contribution policy — required checks before opening a PR

These rules are mandatory for AI coding agents (Claude Code, Codex, Cursor,
Devin, and similar) preparing contributions. Human contributors: see
[CONTRIBUTING.md](CONTRIBUTING.md).

1. **Accepted-issue check.** Is the account you are operating for on the
   qualified roster (recorded by account id in `.github/qualified.yml`)? If
   not, every non-trivial PR requires a linked issue labeled `accepted` by a
   maintainer. This applies regardless of the account's merged-PR history and
   regardless of who filed the issue — an issue filed by the same account the
   PR comes from is fine once a maintainer accepts it, and an issue filed by
   anyone else still needs the label. Without an accepted linked issue, your
   PR will be closed. Do not open it: file your evidence on an
   issue and stop; open the PR only after a maintainer accepts the issue.
2. **Deferred check.** Is the issue you are addressing labeled `deferred`?
   The project has decided not to prioritize it. Do not open a PR against it
   (it will be closed). If you have new evidence or demand,
   comment on the issue and stop.
3. **Duplicate check.** Search open PRs (and the linked issue's existing
   PRs) for work addressing the same problem. If a PR already exists, do not
   open a competing one — review it or comment there instead. If you believe
   your approach is materially better, make that case in a comment on the
   existing PR and wait for a maintainer's direction; if invited to proceed,
   link the two PRs in your description.
4. **Trivial-fix exception.** Documentation-only fixes (typo, broken link;
   docs files only, under 25 changed lines) may be opened directly by anyone.
5. **Value re-evaluation.** Before opening any PR, objectively re-assess it:
   does it fix a demonstrated problem, with evidence (a reproduction or a
   failing test)? If the need is speculative or the fix unverified, do not
   proceed. File an issue with your evidence instead.
