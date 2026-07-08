#!/usr/bin/env bash
# Generate a repo's committed .release-please-config.json from the shared
# conventional-commit-types.json, so the changelog type list stays
# single-sourced. release-please-action reads its config from the repo (via the
# API), so the config must be committed — run this at migration time and again
# whenever the shared type list changes.
#
# Usage:
#   gen-release-please-config.sh <release-type> <include-v-in-tag> [output]
#     release-type      python | node
#     include-v-in-tag  true | false   (false = bare tags like 0.3.2)
#     output            defaults to .release-please-config.json
#
# Example:
#   scripts/gen-release-please-config.sh python false > .release-please-config.json
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
types_file="$repo_root/conventional-commit-types.json"

release_type="${1:?release-type required (python|node)}"
include_v="${2:?include-v-in-tag required (true|false)}"
out="${3:-}"

config="$(jq \
  --arg rt "$release_type" \
  --argjson vtag "$include_v" \
  '{
    "release-type": $rt,
    "include-v-in-tag": $vtag,
    "packages": {
      ".": {
        "changelog-sections": .,
        "release-notes-config": { "grouping": true }
      }
    }
  }' "$types_file")"

if [ -n "$out" ]; then
  printf '%s\n' "$config" > "$out"
  echo "wrote $out" >&2
else
  printf '%s\n' "$config"
fi
