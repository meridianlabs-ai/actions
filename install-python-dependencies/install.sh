#!/usr/bin/env bash
# Install Python dependencies for the install-python-dependencies action.
#
# check_inputs.py has already validated every input. Each input arrives as an
# environment variable, never as a ${{ }} expression. Commands are built as
# bash arrays, with no eval and no unquoted expansion, so no input becomes a uv
# option or a second command. environment-dir becomes an absolute path, so uv
# never reads it as an option or as a Python version request. An also-install
# item is positional too, but check_inputs.py rejects an item that starts with -.
set -euo pipefail

# Where the Python environment goes, as an absolute path. Empty means uv's
# default, <project-dir>/.venv. A relative path is relative to <project-dir>
# (this step runs in <project-dir>).
env_dir=${ENVIRONMENT_DIR:-.venv}
[[ $env_dir == /* ]] || env_dir=$PWD/$env_dir

# Split a multi-line input into an array, dropping blank lines.
lines() { printf '%s\n' "$1" | sed '/^[[:space:]]*$/d'; }
mapfile -t extras < <(lines "$EXTRAS")
mapfile -t unlocked_extra < <(lines "$UNLOCKED_EXTRA")
mapfile -t no_deps < <(lines "$WITHOUT_DEPS")

# project-uv-settings: ignore drops uv.toml and the [tool.uv] settings
# (--no-config). [tool.uv.sources] still applies.
cfg=()
[ "$PROJECT_UV_SETTINGS" = ignore ] && cfg=(--no-config)

# wheels-only applies to the main install only, never to also-install-without-deps.
fmt=()
[ "$PACKAGE_FORMAT" = wheels-only ] && fmt=(--only-binary :all:)

# Reuse only an existing Python environment. Without --allow-existing, uv venv
# refuses a directory that is not empty.
reuse=()
[ -f "$env_dir/pyvenv.cfg" ] && reuse=(--allow-existing)

# Only an unlocked install drops the cooldown, and upgrades so that a reused
# Python environment does not keep an older version.
newer=()
[ "$VERSIONS" = unlocked ] && newer=(--exclude-newer false --upgrade)

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
      # Keep the uv output in the log. Name uv.lock only when uv names it.
      out=$(mktemp)
      if ! uv sync "${cfg[@]}" --locked "${f[@]}" 2>&1 | tee "$out"; then
        if grep -Fq -e 'The lockfile at `uv.lock` needs to be updated' \
          -e 'Unable to find lockfile at `uv.lock`' "$out"; then
          echo "::error::uv.lock is missing or out of date. Run uv lock and commit it."
        else
          echo "::error::uv sync --locked failed. The uv output above gives the cause."
        fi
        exit 1
      fi
    fi
    ;;
  hashed-requirements)
    # A later hashed-requirements or unlocked call with the same environment-dir
    # installs into that Python environment.
    # --reinstall: uv skips a package that a reused Python environment already
    # has, and then does not check its hash.
    uv venv "${cfg[@]}" "${reuse[@]}" "$env_dir"
    uv pip install "${cfg[@]}" --python "$env_dir" --require-hashes --reinstall "${fmt[@]}" --no-deps -r "$REQUIREMENTS_FILE"
    ;;
  unlocked)
    spec=.
    [ ${#extras[@]} -gt 0 ] && spec=".[$(IFS=,; echo "${extras[*]}")]"
    t=()
    [ "$PROJECT_INSTALL" = editable ] && t+=(-e "$spec")
    t+=("${unlocked_extra[@]}")
    uv venv "${cfg[@]}" "${reuse[@]}" "$env_dir"
    # t is empty when the call has only also-install-without-deps items.
    if [ ${#t[@]} -gt 0 ]; then
      uv pip install "${cfg[@]}" --python "$env_dir" "${newer[@]}" "${fmt[@]}" "${t[@]}"
    fi
    ;;
esac

# also-install-without-deps items, each with --no-deps. Never for a script
# Python environment (check_inputs.py forbids that pair).
if [ -z "$SCRIPT" ]; then
  for p in "${no_deps[@]}"; do
    uv pip install "${cfg[@]}" --python "$env_dir" "${newer[@]}" --no-deps "$p"
  done
fi
