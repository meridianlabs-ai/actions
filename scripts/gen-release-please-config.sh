#!/usr/bin/env bash
# Generate a repo's committed .release-please-config.json from the shared
# conventional-commit-types.json, so the changelog type list stays
# single-sourced. release-please-action reads its config from the repo (via the
# API), so the config must be committed — run this at migration time and again
# whenever the shared type list changes.
#
# Usage:
#   gen-release-please-config.sh <release-type> [versioning-mode] [tag-format]
#     release-type      python | node
#     versioning-mode   (optional; default `default`):
#       default          Conventional Commits default: feat->minor, fix->patch,
#                        chore/docs/etc -> no release.
#       patch-pre-major  feat->patch, fix->patch, breaking (feat!:)->minor,
#                        chore/docs -> no release. Pre-1.0 only.
#     tag-format        (optional; default `v`):
#       v                v-prefixed tags (release-please default), e.g. v1.2.3
#       bare             bare tags, e.g. 1.2.3 (preserve a repo's existing bare
#                        scheme). Emits include-v-in-tag: false PER-PACKAGE
#                        (top-level is ignored in manifest mode).
#
#   Output is written to stdout — redirect it, e.g.:
#     scripts/gen-release-please-config.sh python patch-pre-major bare > .release-please-config.json
#
# Notes:
# - Version state lives in the manifest, not tags; hatch-vcs strips the leading
#   v — so tag-format is cosmetic, but we preserve each repo's existing scheme.
# - Do NOT use always-bump-patch: it releases on EVERY commit (chore/docs/CI).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
types_file="$repo_root/conventional-commit-types.json"

release_type="${1:?release-type required (python|node)}"
mode="${2:-default}"
tagfmt="${3:-v}"

case "$mode" in
  default) ver_extra='{}' ;;
  patch-pre-major) ver_extra='{"bump-minor-pre-major": true, "bump-patch-for-minor-pre-major": true}' ;;
  *) echo "unknown versioning-mode: $mode (use: default | patch-pre-major)" >&2; exit 2 ;;
esac

case "$tagfmt" in
  v) tag_extra='{}' ;;
  bare) tag_extra='{"include-v-in-tag": false}' ;;
  *) echo "unknown tag-format: $tagfmt (use: v | bare)" >&2; exit 2 ;;
esac

jq \
  --arg rt "$release_type" \
  --argjson ver "$ver_extra" \
  --argjson tag "$tag_extra" \
  '{ "release-type": $rt,
     "packages": {
       ".": ( { "changelog-sections": . } + $ver + $tag )
     } }' "$types_file"
