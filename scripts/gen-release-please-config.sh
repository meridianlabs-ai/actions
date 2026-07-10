#!/usr/bin/env bash
# Generate a repo's committed .release-please-config.json from the shared
# conventional-commit-types.json, so the changelog type list stays
# single-sourced. release-please-action reads its config from the repo (via the
# API), so the config must be committed — run this at migration time and again
# whenever the shared type list changes.
#
# Usage:
#   gen-release-please-config.sh <release-type> [versioning-mode]
#     release-type      python | node
#     versioning-mode   (optional):
#       default          Conventional Commits default: feat->minor, fix->patch,
#                        chore/docs/etc -> no release. (Also used when omitted.)
#       patch-pre-major  feat->patch, fix->patch, breaking (feat!:)->minor,
#                        chore/docs -> no release. Pre-1.0 only (reverts to
#                        feat->minor once the repo hits 1.0). Use for repos that
#                        want "feat = patch, minors rare/deliberate".
#
#   Output is written to stdout — redirect it, e.g.:
#     scripts/gen-release-please-config.sh python patch-pre-major > .release-please-config.json
#
# Notes:
# - Tags are v-prefixed (release-please default); version state lives in the
#   manifest, not tags, and hatch-vcs strips the leading v, so the prefix is
#   cosmetic. We do NOT set include-v-in-tag.
# - Do NOT use always-bump-patch: it releases on EVERY commit (chore/docs/CI),
#   producing no-op releases. It's meant only for backport branches.
# - Versioning keys are placed per-package (top-level tag/versioning options can
#   be ignored in manifest mode — learned from include-v-in-tag).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
types_file="$repo_root/conventional-commit-types.json"

release_type="${1:?release-type required (python|node)}"
mode="${2:-default}"

case "$mode" in
  default) extra='{}' ;;
  patch-pre-major) extra='{"bump-minor-pre-major": true, "bump-patch-for-minor-pre-major": true}' ;;
  *) echo "unknown versioning-mode: $mode (use: default | patch-pre-major)" >&2; exit 2 ;;
esac

jq \
  --arg rt "$release_type" \
  --argjson extra "$extra" \
  '{ "release-type": $rt,
     "packages": {
       ".": ( { "changelog-sections": ., "release-notes-config": { "grouping": true } } + $extra )
     } }' "$types_file"
