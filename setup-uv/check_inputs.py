#!/usr/bin/env python3
"""Check the inputs of the setup-uv action.

GitHub does not stop a call with a bad input, so this script does. It reads
each input from an environment variable and stops on a second setup-uv in the
job, a newline in an input, or an unknown actions-cache value. Each error is a
GitHub ::error:: line that names the input.
"""

from __future__ import annotations

import os
import sys

CACHE_VALUES = ("read-write", "restore-only", "none")


def fail(name: str, message: str) -> None:
    print(f"::error::{name}: {message}")
    sys.exit(1)


def main() -> None:
    python_version = os.environ.get("PYTHON_VERSION", "")
    actions_cache = os.environ.get("ACTIONS_CACHE", "")

    # setup-uv runs once per job. A second call records nothing new and would
    # install a second, possibly different, uv.
    if os.environ.get("RECORDED_SETUP", ""):
        fail("setup-uv", "already ran in this job. Call setup-uv once per job.")

    for name, value in (("python-version", python_version), ("actions-cache", actions_cache)):
        if "\n" in value:
            fail(name, "must be one line.")

    if actions_cache not in CACHE_VALUES:
        fail("actions-cache", f"unknown value '{actions_cache}'. Use one of: {', '.join(CACHE_VALUES)}.")


if __name__ == "__main__":
    main()
