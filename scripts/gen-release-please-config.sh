#!/usr/bin/env bash
# Generate a repo's committed .release-please-config.json from the shared
# conventional-commit-types.json, so the changelog type list stays
# single-sourced. release-please-action reads its config from the repo (via the
# API), so the config must be committed — run this at migration time and again
# whenever the shared type list changes.
#
# Usage:
#   gen-release-please-config.sh <release-type> [versioning]
#     release-type   python | node
#     versioning     (optional) e.g. always-bump-patch. Omit for the
#                    Conventional Commits default (feat->minor, fix->patch).
#
#   Output is written to stdout — redirect it, e.g.:
#     scripts/gen-release-please-config.sh python always-bump-patch > .release-please-config.json
#
# Tags are v-prefixed (release-please default). We do NOT set include-v-in-tag:
# version state lives in .release-please-manifest.json (not tags), and hatch-vcs
# strips the leading v, so the tag prefix is cosmetic. (In manifest mode a
# top-level include-v-in-tag is ignored anyway.)
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
types_file="$repo_root/conventional-commit-types.json"

release_type="${1:?release-type required (python|node)}"
versioning="${2:-}"

jq \
  --arg rt "$release_type" \
  --arg ver "$versioning" \
  '{ "release-type": $rt }
   + (if $ver != "" then { "versioning": $ver } else {} end)
   + { "packages": {
         ".": {
           "changelog-sections": .,
           "release-notes-config": { "grouping": true }
         }
       } }' "$types_file"
