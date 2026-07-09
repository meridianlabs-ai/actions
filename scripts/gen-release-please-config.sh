#!/usr/bin/env bash
# Generate a repo's committed .release-please-config.json from the shared
# conventional-commit-types.json, so the changelog type list stays
# single-sourced. release-please-action reads its config from the repo (via the
# API), so the config must be committed — run this at migration time and again
# whenever the shared type list changes.
#
# Usage:
#   gen-release-please-config.sh <release-type> <include-v-in-tag> [versioning]
#     release-type      python | node
#     include-v-in-tag  true | false     (false = bare tags like 0.3.2)
#     versioning        (optional) e.g. always-bump-patch. Omit for the
#                       Conventional Commits default (feat->minor, fix->patch).
#
#   Output is written to stdout — redirect it, e.g.:
#     scripts/gen-release-please-config.sh python false always-bump-patch > .release-please-config.json
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
types_file="$repo_root/conventional-commit-types.json"

release_type="${1:?release-type required (python|node)}"
include_v="${2:?include-v-in-tag required (true|false)}"
versioning="${3:-}"

jq \
  --arg rt "$release_type" \
  --argjson vtag "$include_v" \
  --arg ver "$versioning" \
  '{ "release-type": $rt, "include-v-in-tag": $vtag }
   + (if $ver != "" then { "versioning": $ver } else {} end)
   + { "packages": {
         ".": {
           "changelog-sections": .,
           "release-notes-config": { "grouping": true }
         }
       } }' "$types_file"
