#!/usr/bin/env bash
# Install Python dependencies for the install-python-dependencies action.
#
# check_inputs.py has already validated every input. Each input arrives as an
# environment variable, never as a ${{ }} expression. Commands are built as
# bash arrays, with no eval and no unquoted expansion, so no input becomes a uv
# option or a second command.
set -euo pipefail

# Where the Python environment goes. Empty means uv's default,
# <project-dir>/.venv (this step runs in <project-dir>).
if [ -n "$ENVIRONMENT_DIR" ]; then
  env_dir=$ENVIRONMENT_DIR
else
  env_dir=$PWD/.venv
fi

# Split a multi-line input into an array, dropping blank lines.
lines() { printf '%s\n' "$1" | sed '/^[[:space:]]*$/d'; }
mapfile -t extras < <(lines "$EXTRAS")
mapfile -t unlocked_extra < <(lines "$UNLOCKED_EXTRA")
mapfile -t no_deps < <(lines "$WITHOUT_DEPS")

# project-uv-settings: ignore drops [tool.uv] and uv.toml (--no-config).
cfg=()
[ "$PROJECT_UV_SETTINGS" = ignore ] && cfg=(--no-config)

# wheels-only applies to the main install only, never to also-install-without-deps.
fmt=()
[ "$PACKAGE_FORMAT" = wheels-only ] && fmt=(--only-binary :all:)

# Only an unlocked install drops the cooldown.
newer=()
[ "$VERSIONS" = unlocked ] && newer=(--exclude-newer false)

case "$VERSIONS" in
  locked)
    if [ -n "$SCRIPT" ]; then
      # The script Python environment lives in the uv cache. A later step runs
      # the script.
      uv sync "${cfg[@]}" --locked --script "$SCRIPT"
    else
      # Put the project environment where environment-dir asks; else uv's default.
      [ -n "$ENVIRONMENT_DIR" ] && export UV_PROJECT_ENVIRONMENT=$env_dir
      f=()
      for e in "${extras[@]}"; do f+=(--extra "$e"); done
      [ "$DEFAULT_GROUPS" = exclude ] && f+=(--no-default-groups)
      [ "$PROJECT_INSTALL" = skip ] && f+=(--no-install-project)
      uv sync "${cfg[@]}" --locked "${f[@]}" || {
        echo "::error file=uv.lock::uv.lock is missing or out of date. Run uv lock and commit it."
        exit 1
      }
    fi
    ;;
  hashed-requirements)
    uv venv "$env_dir"
    uv pip install "${cfg[@]}" --python "$env_dir" --require-hashes "${fmt[@]}" --no-deps -r "$REQUIREMENTS_FILE"
    ;;
  unlocked)
    spec=.
    [ ${#extras[@]} -gt 0 ] && spec=".[$(IFS=,; echo "${extras[*]}")]"
    t=()
    [ "$PROJECT_INSTALL" = editable ] && t+=(-e "$spec")
    t+=("${unlocked_extra[@]}")
    uv venv "$env_dir"
    uv pip install "${cfg[@]}" --python "$env_dir" "${newer[@]}" "${fmt[@]}" "${t[@]}"
    ;;
esac

# also-install-without-deps items, each with --no-deps. Never for a script
# Python environment (check_inputs.py forbids that pair).
if [ -z "$SCRIPT" ]; then
  for p in "${no_deps[@]}"; do
    uv pip install "${cfg[@]}" --python "$env_dir" "${newer[@]}" --no-deps "$p"
  done
fi
