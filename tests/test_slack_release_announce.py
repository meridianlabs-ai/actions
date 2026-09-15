"""Tests for slack-release-announce/convert.py and the step that calls it.

The release notes the action posts are release-please's rendering of merged
commit subjects, i.e. text written by any contributor to the calling repo,
and Slack reads `<...>` in mrkdwn as a link or a special mention
(`<!channel>`, `<!subteam^ID>`, `<@UID>`). The converter must never emit a
`<` that does not open an http(s) link, whatever the markdown contained.

Run with `python3 -m pytest` from the repo root (needs pytest and PyYAML;
`.github/workflows/tests.yml` does the same in CI).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION_DIR = ROOT / "slack-release-announce"


def load_converter():
    spec = importlib.util.spec_from_file_location("slack_release_convert", ACTION_DIR / "convert.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


converter = load_converter()
convert = converter.convert

# What a `<...>` may look like in the output: an http(s) target with none of
# `<`, `>`, `|` or whitespace, then a label with no `<` or `>`.
SAFE_LINK = re.compile(r"<https?://[^\s<>|]+\|[^<>]*>")


def assert_only_links(mrkdwn: str) -> None:
    rest = SAFE_LINK.sub("", mrkdwn)
    assert "<" not in rest and ">" not in rest, mrkdwn


def convert_one(markdown: str) -> str:
    (line,) = convert(markdown)
    assert_only_links(line)
    return line


# --- Injection through link syntax --------------------------------------------


@pytest.mark.parametrize(
    "markdown, literal",
    [
        ("* feat: [everyone](!channel) faster evals", "[everyone](!channel)"),
        ("* feat: [@eng](!subteam^S0123ABCDEF) rollout", "[@eng](!subteam^S0123ABCDEF)"),
        ("* fix: [@dragonstyle](@U08L7H4NBQT) review", "[@dragonstyle](@U08L7H4NBQT)"),
    ],
)
def test_mention_syntax_in_a_link_target_is_emitted_as_text(markdown, literal):
    line = convert_one(markdown)
    assert "<" not in line
    assert literal in line


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "ftp://files.example/x",
        "//evil.example/x",
        "https://x.example/a|b",
        "https://x.example/<!channel>",
        "https://x.example/a b",
        "https://x.example/a>",
    ],
)
def test_non_http_or_unclean_target_is_not_a_link(target):
    line = convert_one(f"* see [here]({target})")
    assert "<" not in line
    assert line.startswith("• see [here](")


def test_a_bad_link_does_not_disturb_a_good_one_on_the_same_line():
    line = convert_one("[ok](https://x.example/) and [bad](!channel)")
    assert line == "<https://x.example/|ok> and [bad](!channel)"


# --- Ordinary release notes ---------------------------------------------------


def test_http_link_becomes_a_slack_link():
    line = convert_one("* fix: thing ([#12](https://github.com/o/r/pull/12))")
    assert line == "• fix: thing (<https://github.com/o/r/pull/12|#12>)"


def test_ampersand_in_target_and_control_characters_in_label_are_escaped():
    line = convert_one("[a <b> & c](https://x.example/?a=1&b=2)")
    assert line == "<https://x.example/?a=1&amp;b=2|a &lt;b&gt; &amp; c>"


def test_heading():
    assert convert_one("## Features") == "*Features*"
    assert convert_one("### [Compare](https://x.example/compare)") == "*<https://x.example/compare|Compare>*"


def test_bullet():
    assert convert_one("- fix: thing") == "• fix: thing"
    assert convert_one("  * nested") == "  • nested"


def test_bare_angle_brackets_and_raw_mentions_in_prose_are_escaped():
    assert convert_one("* fix: handle a < b and <script>") == "• fix: handle a &lt; b and &lt;script&gt;"
    assert convert_one("<!channel> <@U08L7H4NBQT> <!subteam^S01>") == "&lt;!channel&gt; &lt;@U08L7H4NBQT&gt; &lt;!subteam^S01&gt;"


def test_release_body_end_to_end_only_contains_http_links():
    body = """## [0.3.1](https://github.com/o/r/compare/v0.3.0...v0.3.1) (2026-09-15)

### Features

* [everyone](!channel) new eval ([#1](https://github.com/o/r/pull/1))
* ping [@eng](!subteam^S0123ABCDEF) & [me](@U08L7H4NBQT)
* keep a < b ([abc123](https://github.com/o/r/commit/abc123))

### Bug Fixes

- docs: [text](javascript:alert(1)) <!here>"""
    text = converter.section_text(body)
    assert_only_links(text)
    assert text.count("<https://") == 3


# --- Payload --------------------------------------------------------------------


def run_script(env: dict) -> dict:
    r = subprocess.run(["python3", str(ACTION_DIR / "convert.py")], env={**os.environ, **env}, text=True, capture_output=True, check=True)
    return json.loads(r.stdout)


def test_script_prints_the_payload():
    p = run_script({"TITLE": ":rocket: r v1 released", "RELEASE_URL": "https://github.com/o/r/releases/tag/v1", "RELEASE_BODY": "## Features\n* [x](!channel) y"})
    header, notes, link = p["blocks"]
    assert header["text"] == {"type": "plain_text", "text": ":rocket: r v1 released", "emoji": True}
    assert notes["text"]["text"] == "*Features*\n• [x](!channel) y"
    assert link["text"]["text"] == "<https://github.com/o/r/releases/tag/v1|View the full release>"


def test_empty_body_and_truncation():
    assert run_script({"TITLE": "t", "RELEASE_URL": "https://x.example/", "RELEASE_BODY": ""})["blocks"][1]["text"]["text"] == "_(no release notes)_"
    long = "\n".join(f"* line {i:03d} " + "x" * 40 for i in range(200))
    text = run_script({"TITLE": "t", "RELEASE_URL": "https://x.example/", "RELEASE_BODY": long})["blocks"][1]["text"]["text"]
    assert text.endswith(converter.TRUNCATED)
    assert len(text) <= converter.MAX_BODY + len(converter.TRUNCATED) + 1


# --- The composite step ---------------------------------------------------------

FAKE_CURL = """#!/usr/bin/env bash
# Record curl's arguments, one per line, instead of posting anywhere.
printf '%s\\n' "$@" > "$FAKE_CURL_ARGS"
"""


def run_step(tmp_path: Path, env: dict) -> tuple[subprocess.CompletedProcess, list[str] | None]:
    step = yaml.safe_load((ACTION_DIR / "action.yml").read_text())["runs"]["steps"][0]
    assert step["shell"] == "bash"
    assert set(step["env"]) == {"WEBHOOK", "TITLE", "RELEASE_URL", "RELEASE_BODY"}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(FAKE_CURL)
    (bin_dir / "curl").chmod(0o755)
    args_file = tmp_path / "curl-args"
    full_env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_ACTION_PATH": str(ACTION_DIR),
        "FAKE_CURL_ARGS": str(args_file),
        **env,
    }
    r = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], cwd=tmp_path, env=full_env, text=True, capture_output=True, check=False)
    return r, args_file.read_text().splitlines() if args_file.exists() else None


def test_step_posts_the_converted_payload_to_the_webhook(tmp_path):
    r, args = run_step(
        tmp_path,
        {"WEBHOOK": "https://hooks.example/T/B/x", "TITLE": "t", "RELEASE_URL": "https://x.example/r", "RELEASE_BODY": "* [all](!channel) done"},
    )
    assert r.returncode == 0, r.stderr
    assert args is not None and args[-1] == "https://hooks.example/T/B/x"
    payload = json.loads(args[args.index("--data") + 1])
    assert payload["blocks"][1]["text"]["text"] == "• [all](!channel) done"


def test_step_without_a_webhook_posts_nothing(tmp_path):
    r, args = run_step(tmp_path, {"WEBHOOK": "", "TITLE": "t", "RELEASE_URL": "https://x.example/r", "RELEASE_BODY": "x"})
    assert r.returncode == 0, r.stderr
    assert "skipping" in r.stdout
    assert args is None
