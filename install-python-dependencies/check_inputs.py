#!/usr/bin/env python3
"""Check the inputs of the install-python-dependencies action.

GitHub does not stop a call with a missing required input or a bad value, so
this script does. It reads each input from an environment variable and stops
before uv runs. Each error is a GitHub ::error:: line that names the input.

The rules:
  - versions and python-version are required.
  - each enumerated input holds one of its values.
  - a one-line input holds no newline and no carriage return, and a list input
    holds no carriage return (the runner also starts a line at a carriage
    return).
  - a list item does not start with '-' (so a caller cannot add a uv option).
  - an input applies only to the versions value its table row allows.
  - unlocked needs actions-cache none and project-uv-settings ignore.
  - unlocked with project-install skip installs at least one package.
  - wheels-only is not for locked.
  - a non-local also-install-without-deps item is not for locked, and no such
    item is for hashed-requirements.
  - script is not with environment-dir, also-install-without-deps, extras,
    default-dependency-groups exclude or project-install skip.
  - a second call matches the setup-uv already run in this job.
"""

from __future__ import annotations

import os
import sys

VERSIONS_VALUES = ("locked", "hashed-requirements", "unlocked")
CACHE_VALUES = ("read-write", "restore-only", "none")
ENUMS = {
    "project-install": ("editable", "skip"),
    "default-dependency-groups": ("include", "exclude"),
    "project-uv-settings": ("apply", "ignore"),
    "package-format": ("any", "wheels-only"),
    "actions-cache": CACHE_VALUES,
}
ONE_LINE = (
    "versions",
    "python-version",
    "project-dir",
    "environment-dir",
    "project-install",
    "default-dependency-groups",
    "requirements-file",
    "script",
    "project-uv-settings",
    "package-format",
    "actions-cache",
)
LIST_INPUTS = ("extras", "also-install-unlocked", "also-install-without-deps")

ENV = {
    "versions": "VERSIONS",
    "python-version": "PYTHON_VERSION",
    "project-dir": "PROJECT_DIR",
    "environment-dir": "ENVIRONMENT_DIR",
    "project-install": "PROJECT_INSTALL",
    "extras": "EXTRAS",
    "default-dependency-groups": "DEFAULT_GROUPS",
    "requirements-file": "REQUIREMENTS_FILE",
    "script": "SCRIPT",
    "also-install-unlocked": "UNLOCKED_EXTRA",
    "also-install-without-deps": "WITHOUT_DEPS",
    "project-uv-settings": "PROJECT_UV_SETTINGS",
    "package-format": "PACKAGE_FORMAT",
    "actions-cache": "ACTIONS_CACHE",
}


def fail(name: str, message: str) -> None:
    print(f"::error::{name}: {message}")
    sys.exit(1)


def items(value: str) -> list[str]:
    """Split a list input into its non-blank lines."""
    return [line for line in value.split("\n") if line.strip()]


def is_local_path(item: str) -> bool:
    return item.startswith(("/", "./", "../")) or item in (".", "..")


def main() -> None:
    v = {name: os.environ.get(env, "") for name, env in ENV.items()}

    versions = v["versions"]
    if not versions:
        fail("versions", "is required. Use one of: " + ", ".join(VERSIONS_VALUES) + ".")
    if not v["python-version"]:
        fail("python-version", "is required.")

    # Before any error that shows an input value, so no value adds a line.
    for name in ONE_LINE:
        if "\n" in v[name] or "\r" in v[name]:
            fail(name, "must be one line.")
    for name in LIST_INPUTS:
        if "\r" in v[name]:
            fail(name, "must hold no carriage return.")

    if versions not in VERSIONS_VALUES:
        fail("versions", f"unknown value '{versions}'. Use one of: {', '.join(VERSIONS_VALUES)}.")

    for name, allowed in ENUMS.items():
        if v[name] not in allowed:
            fail(name, f"unknown value '{v[name]}'. Use one of: {', '.join(allowed)}.")

    for name in LIST_INPUTS:
        for item in items(v[name]):
            if item.lstrip().startswith("-"):
                fail(name, f"item '{item}' starts with '-'. List items are packages or paths, not options.")

    # requirements-file applies only to hashed-requirements, and is required there.
    if versions == "hashed-requirements":
        if not v["requirements-file"]:
            fail("requirements-file", "is required with versions: hashed-requirements.")
    elif v["requirements-file"]:
        fail("requirements-file", "applies only to versions: hashed-requirements.")

    # Inputs that apply to one versions value only.
    if v["script"] and versions != "locked":
        fail("script", "applies only to versions: locked.")
    if v["default-dependency-groups"] != "include" and versions != "locked":
        fail("default-dependency-groups", "applies only to versions: locked.")
    if v["also-install-unlocked"].strip() and versions != "unlocked":
        fail("also-install-unlocked", "applies only to versions: unlocked.")
    if v["extras"].strip() and versions == "hashed-requirements":
        fail("extras", "does not apply to versions: hashed-requirements.")
    if v["project-install"] != "editable" and versions == "hashed-requirements":
        fail("project-install", "does not apply to versions: hashed-requirements.")

    # also-install-without-deps rules.
    without_deps = items(v["also-install-without-deps"])
    if without_deps and versions == "hashed-requirements":
        fail("also-install-without-deps", "does not apply to versions: hashed-requirements.")
    if versions == "locked":
        for item in without_deps:
            if not is_local_path(item):
                fail("also-install-without-deps", f"item '{item}' must be a local path with versions: locked.")

    # package-format: wheels-only is not for locked.
    if v["package-format"] == "wheels-only" and versions == "locked":
        fail("package-format", "wheels-only does not apply to versions: locked.")

    # unlocked has no cooldown, so it restores no Actions cache and ignores the
    # project uv settings.
    if versions == "unlocked":
        if v["actions-cache"] != "none":
            fail("actions-cache", "must be none with versions: unlocked.")
        if v["project-uv-settings"] != "ignore":
            fail("project-uv-settings", "must be ignore with versions: unlocked.")
        if v["project-install"] == "skip" and not items(v["also-install-unlocked"]) and not without_deps:
            fail(
                "project-install/also-install-unlocked/also-install-without-deps",
                "project-install: skip installs nothing. Give also-install-unlocked or also-install-without-deps.",
            )

    # script runs its own Python environment in the uv cache.
    if v["script"]:
        if v["environment-dir"]:
            fail("environment-dir", "does not apply with script.")
        if without_deps:
            fail("also-install-without-deps", "does not apply with script.")
        if v["extras"].strip():
            fail("extras", "does not apply with script.")
        if v["default-dependency-groups"] != "include":
            fail("default-dependency-groups", "does not apply with script.")
        if v["project-install"] != "editable":
            fail("project-install", "does not apply with script.")

    # A later call in the job reuses the setup-uv already run. It must match.
    recorded = os.environ.get("RECORDED_SETUP", "")
    if recorded:
        current = f"{v['python-version']}/{v['actions-cache']}"
        if recorded != current:
            fail(
                "python-version/actions-cache",
                f"'{current}' differs from the setup-uv already run in this job ('{recorded}'). "
                "Every install-python-dependencies call in a job uses the same python-version and actions-cache.",
            )


if __name__ == "__main__":
    main()
