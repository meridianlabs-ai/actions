"""Tests for .github/workflows/triage-test-failures.yml's agent-job scripts.

The workflow's trust boundary is in three places a YAML file cannot test on
its own: the step that validates the triage-context artifact before any
field becomes a checkout ref, a step output or the Slack destination; the
step that composes the landing manifest from what the agent wrote
(whitelisting, normalizing, and turning every dropped action into a
fail_run error); and the agent's permission rules, whose allow list must not
reach a write outside the landing directory. The two `run:` blocks are
lifted from the workflow and executed under bash exactly as the runner
would; the rules are matched with the glob semantics Claude Code documents
for Bash rules (`*` matches any text, a compound command is checked one
subcommand at a time).

Run with `python3 -m pytest` from the repo root (needs pytest and PyYAML;
`.github/workflows/tests.yml` does the same in CI). The composed manifests
are also passed through the agents repo's validator — the land job's first
step — fetched from meridianlabs-ai/agents at `main` (override the path
with TRIAGE_VALIDATOR, or the ref with TRIAGE_VALIDATOR_REF).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "triage-test-failures.yml"
RUNNER_TEMP = "/home/runner/work/_temp"
ISSUES_REPO = "meridianlabs-ai/inspect_ai"
SHA = "0123456789abcdef0123456789abcdef01234567"
VALIDATOR_URL = "https://raw.githubusercontent.com/meridianlabs-ai/agents/{ref}/.github/scripts/validate_manifest.py"


def load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def agent_step(name_or_id: str) -> dict:
    for step in load_workflow()["jobs"]["agent"]["steps"]:
        if step.get("id") == name_or_id or step.get("name") == name_or_id:
            return step
    raise KeyError(name_or_id)


def run_bash(script: str, *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **env}
    return subprocess.run(["bash", "-c", script], cwd=cwd, env=full_env, text=True, capture_output=True)


# --- Validate the triage context --------------------------------------------


def parse_outputs(text: str) -> dict:
    """Parse $GITHUB_OUTPUT written with heredoc delimiters, refusing any other line shape."""
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.fullmatch(r"([a-z_]+)<<(\w+)", lines[i])
        assert m, f"unexpected output line {lines[i]!r} (every output must use a heredoc delimiter)"
        key, delim = m.groups()
        j = lines.index(delim, i + 1)
        out[key] = "\n".join(lines[i + 1 : j])
        i = j + 1
    return out


def validate(tmp_path: Path, artifact: str | None) -> tuple[dict, subprocess.CompletedProcess]:
    (tmp_path / "triage").mkdir()
    if artifact is not None:
        (tmp_path / "triage" / "slack_thread.json").write_text(artifact)
    gh_out = tmp_path / "output.txt"
    gh_out.touch()
    r = run_bash(agent_step("context")["run"], cwd=tmp_path, env={"GITHUB_OUTPUT": str(gh_out)})
    assert r.returncode == 0, r.stderr
    return parse_outputs(gh_out.read_text()), r


GOOD = {"channel": "C0123ABCDEF", "thread_ts": "1726000000.123456", "inspect_ai_sha": SHA, "inspect_ai_ref": "main"}


def test_context_valid_artifact_passes_every_field(tmp_path):
    out, _ = validate(tmp_path, json.dumps(GOOD))
    assert out == {"sha": SHA, "exact": "true", "channel": "C0123ABCDEF", "thread_ts": "1726000000.123456"}


def test_context_absent_artifact_falls_back_to_main(tmp_path):
    out, _ = validate(tmp_path, None)
    assert out == {"sha": "main", "exact": "false", "channel": "", "thread_ts": ""}


def test_context_newline_in_sha_cannot_add_an_output(tmp_path):
    # The issue's verification 3: a SHA of "main\nexact=true" is rejected,
    # falls back to main with exact=false, and the output file has ONE sha.
    out, r = validate(tmp_path, json.dumps({**GOOD, "inspect_ai_sha": "main\nexact=true"}))
    assert out["sha"] == "main" and out["exact"] == "false"
    assert (tmp_path / "output.txt").read_text().count("sha<<") == 1
    assert "not a 40-hex SHA" in r.stdout


@pytest.mark.parametrize(
    "field, value, needle",
    [
        ("inspect_ai_sha", SHA[:39], "not a 40-hex SHA"),
        ("inspect_ai_sha", SHA.upper(), "not a 40-hex SHA"),
        ("channel", "../evil", "not a Slack channel ID"),
        ("channel", "c0123abcdef", "not a Slack channel ID"),
        ("channel", "U0123ABCDEF", "not a Slack channel ID"),
        ("thread_ts", "abc\nfoo=bar", "not a Slack timestamp"),
        ("thread_ts", "1726000000", "not a Slack timestamp"),
        ("thread_ts", "1.2 x", "not a Slack timestamp"),
    ],
)
def test_context_malformed_field_is_dropped_with_a_warning(tmp_path, field, value, needle):
    out, r = validate(tmp_path, json.dumps({**GOOD, field: value}))
    expected = {"sha": SHA, "exact": "true", "channel": "C0123ABCDEF", "thread_ts": "1726000000.123456"}
    if field == "inspect_ai_sha":
        expected.update(sha="main", exact="false")
    else:
        expected[field] = ""
    assert out == expected
    assert needle in r.stdout


@pytest.mark.parametrize(
    "artifact",
    ["[1,2,3]", "{not json", '{"channel":123,"thread_ts":null,"inspect_ai_sha":["x"]}', "", "null"],
)
def test_context_non_object_or_non_string_fields_fall_back(tmp_path, artifact):
    out, _ = validate(tmp_path, artifact)
    assert out == {"sha": "main", "exact": "false", "channel": "", "thread_ts": ""}


# --- Compose landing manifest -----------------------------------------------


def compose(tmp_path: Path, manifest_extra, *, outcome="success", files=()) -> tuple[dict, subprocess.CompletedProcess]:
    landing = tmp_path / "landing"
    landing.mkdir(exist_ok=True)
    for name in files:
        (landing / name).write_text(f"body of {name}\n")
    if manifest_extra is not None:
        text = manifest_extra if isinstance(manifest_extra, str) else json.dumps(manifest_extra)
        (landing / "manifest-extra.json").write_text(text)
    extra = tmp_path / "landing-extra.json"
    step = agent_step("landing")
    env = {
        "DIR": str(landing),
        "EXTRA": str(extra),
        "CLAUDE_OUTCOME": outcome,
        "RUN_URL": "https://example.test/run",
        "ISSUES_REPO": ISSUES_REPO,
        "ASSIGNEE": step["env"]["ASSIGNEE"],
    }
    r = run_bash(step["run"], cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(extra.read_text()), r


def issue_entry(**fields) -> dict:
    return {"repo": ISSUES_REPO, **fields}


def test_compose_new_issue_with_slack_is_pinned_and_owned(tmp_path):
    extra, r = compose(
        tmp_path,
        {
            "issues": [{"repo": "meridianlabs-ai/actions", "title": "Triage: test_foo fails", "body_file": "issue.md",
                        "labels": ["auto", "bogus"], "assignees": ["evil"]}],
            "slack": {"text_file": "slack.txt"},
        },
        files=["issue.md", "slack.txt"],
    )
    # repo pinned to the fork, labels reduced to `auto`, the owner always set on a create.
    assert extra == {
        "issues": [issue_entry(title="Triage: test_foo fails", body_file="issue.md", labels=["auto"], assignees=["ransomr"])],
        "slack": {"text_file": "slack.txt"},
    }
    assert "::error::" not in r.stdout


def test_compose_comment_on_closed_unowned_issue(tmp_path):
    extra, _ = compose(
        tmp_path,
        {"issues": [{"comment_on": 444, "body_file": "comment.md", "reopen": True, "assignees": ["ransomr", "evil"],
                     "labels": ["auto"]}],
         "slack": {"text_file": "slack.txt"}},
        files=["comment.md", "slack.txt"],
    )
    # A comment gets a placeholder title (the schema requires one), no labels,
    # `reopen`, and only the owner among the assignees.
    assert extra["issues"] == [issue_entry(title="Comment on #444", body_file="comment.md", assignees=["ransomr"],
                                           reopen=True, comment_on=444)]


def test_compose_comment_without_reopen_or_owner(tmp_path):
    extra, _ = compose(
        tmp_path,
        {"issues": [{"comment_on": 444, "body_file": "comment.md", "reopen": "yes"}], "slack": {"text_file": "s.txt"}},
        files=["comment.md", "s.txt"],
    )
    assert extra["issues"] == [issue_entry(title="Comment on #444", body_file="comment.md", comment_on=444)]


def test_compose_reopen_on_a_create_is_dropped(tmp_path):
    extra, _ = compose(tmp_path, {"issues": [{"title": "t", "body_file": "a.md", "reopen": True}],
                                  "slack": {"text_file": "s.txt"}}, files=["a.md", "s.txt"])
    assert "reopen" not in extra["issues"][0]
    assert "error" not in extra


def test_compose_no_manifest_after_a_successful_agent_is_an_error(tmp_path):
    extra, _ = compose(tmp_path, None)
    assert extra == {"error": {"message": extra["error"]["message"], "fail_run": True}}
    assert "no Slack reply" in extra["error"]["message"]


def test_compose_failed_agent_step_is_an_error(tmp_path):
    extra, _ = compose(tmp_path, None, outcome="failure")
    assert extra["error"]["fail_run"] is True
    assert "outcome 'failure'" in extra["error"]["message"]


def test_compose_unparsable_manifest_lands_nothing_and_fails(tmp_path):
    extra, r = compose(tmp_path, "{nope", files=["slack.txt"])
    assert "issues" not in extra and "slack" not in extra
    assert extra["error"]["fail_run"] is True
    assert "not valid JSON" in extra["error"]["message"]


@pytest.mark.parametrize(
    "bad",
    [
        {"body_file": "a.md"},                                # a create needs a title
        {"title": "t", "body_file": "../x.md"},               # file names never leave the landing dir
        {"title": "t", "body_file": ".hidden"},               # hidden files are not uploaded
        {"comment_on": 1.5, "body_file": "a.md"},             # not a positive integer
        {"comment_on": "444", "body_file": "a.md"},
        {"title": "t"},                                       # no body
        "not an object",
    ],
)
def test_compose_malformed_issue_entry_is_dropped_and_fails_the_run(tmp_path, bad):
    # Blocking finding, review round 1: a discarded action must not leave a
    # green landing. The valid Slack text still lands; the error travels too.
    extra, _ = compose(tmp_path, {"issues": [bad], "slack": {"text_file": "s.txt"}}, files=["a.md", "s.txt"])
    assert "issues" not in extra
    assert extra["slack"] == {"text_file": "s.txt"}
    assert extra["error"]["fail_run"] is True
    assert "1 malformed issue entr(ies)" in extra["error"]["message"]


def test_compose_second_issue_action_is_dropped_and_fails_the_run(tmp_path):
    extra, _ = compose(
        tmp_path,
        {"issues": [{"title": "x", "body_file": "a.md"}, {"title": "y", "body_file": "b.md"}], "slack": {"text_file": "s.txt"}},
        files=["a.md", "b.md", "s.txt"],
    )
    assert [i["body_file"] for i in extra["issues"]] == ["a.md"]
    assert "2 issue actions where one is allowed" in extra["error"]["message"]
    assert extra["error"]["fail_run"] is True


def test_compose_unknown_keys_are_ignored_and_fail_the_run(tmp_path):
    # `comments` (or any other manifest field) from the agent is a posting
    # channel the split closed; ignored, and reported.
    extra, _ = compose(tmp_path, {"comments": [{"number": 1, "body_file": "a.md"}], "slack": {"text_file": "s.txt"}},
                       files=["a.md", "s.txt"])
    assert "comments" not in extra and "issues" not in extra
    assert "keys it may not set (comments)" in extra["error"]["message"]


def test_compose_valid_issue_without_slack_fails_the_run(tmp_path):
    # Every triage owes the thread a reply: the issue lands, the run fails.
    extra, _ = compose(tmp_path, {"issues": [{"title": "t", "body_file": "a.md"}]}, files=["a.md"])
    assert extra["issues"][0]["title"] == "t"
    assert "no Slack reply" in extra["error"]["message"]
    assert extra["error"]["fail_run"] is True


def test_compose_slack_without_text_file_fails_the_run(tmp_path):
    extra, _ = compose(tmp_path, {"slack": {"channel": "C123", "text": "hi"}, "issues": [{"title": "t", "body_file": "a.md"}]},
                       files=["a.md"])
    assert "slack" not in extra
    assert "names no valid text_file" in extra["error"]["message"]


# --- The composed manifest against the land job's validator -----------------


@pytest.fixture(scope="session")
def validator(tmp_path_factory) -> Path:
    override = os.environ.get("TRIAGE_VALIDATOR")
    if override:
        return Path(override)
    ref = os.environ.get("TRIAGE_VALIDATOR_REF", "main")
    target = tmp_path_factory.mktemp("agents") / "validate_manifest.py"
    try:
        with urllib.request.urlopen(VALIDATOR_URL.format(ref=ref), timeout=30) as resp:
            target.write_bytes(resp.read())
    except OSError as exc:  # no network: the cross-check cannot run
        pytest.skip(f"could not fetch the agents validator: {exc}")
    return target


def land_validate(validator: Path, tmp_path: Path, extra: dict) -> subprocess.CompletedProcess:
    """emit-landing's merge (core fields win) and the land job's validator call."""
    core = {"schema": 1, "repo": "meridianlabs-ai/actions", "run_id": 42, "branch": "triage",
            "start_sha": SHA, "head_sha": SHA, "has_bundle": False, "pr_number": None, "issue_number": None}
    (tmp_path / "landing" / "manifest.json").write_text(json.dumps({**extra, **core}))
    return subprocess.run(
        ["python3", str(validator), "--dir", str(tmp_path / "landing"), "--repo", "meridianlabs-ai/actions",
         "--run-id", "42", "--default-branch", "main", "--refused-branches", "main",
         "--allowed-issue-repos", ISSUES_REPO, "--branch-prefix", "triage", "--refuse-bundle"],
        text=True, capture_output=True,
    )


@pytest.mark.parametrize(
    "manifest_extra, files",
    [
        ({"issues": [{"title": "Triage: t", "body_file": "issue.md", "labels": ["auto"]}], "slack": {"text_file": "slack.txt"}},
         ["issue.md", "slack.txt"]),
        ({"issues": [{"comment_on": 444, "body_file": "c.md", "reopen": True, "assignees": ["ransomr"]}],
          "slack": {"text_file": "slack.txt"}}, ["c.md", "slack.txt"]),
        ({"slack": {"text_file": "slack.txt"}}, ["slack.txt"]),
        (None, []),
    ],
)
def test_composed_manifest_passes_the_land_validator(validator, tmp_path, manifest_extra, files):
    extra, _ = compose(tmp_path, manifest_extra, files=files)
    r = land_validate(validator, tmp_path, extra)
    assert r.returncode == 0, r.stdout + r.stderr


def test_composer_never_forwards_a_destination_or_a_foreign_repo(validator, tmp_path):
    extra, _ = compose(
        tmp_path,
        {"issues": [{"repo": "evil/repo", "title": "t", "body_file": "a.md"}],
         "slack": {"text_file": "s.txt", "channel": "C0EVIL0000", "thread_ts": "1.2"}},
        files=["a.md", "s.txt"],
    )
    assert extra["issues"][0]["repo"] == ISSUES_REPO
    assert extra["slack"] == {"text_file": "s.txt"}
    assert land_validate(validator, tmp_path, extra).returncode == 0


# --- The agent's permission rules --------------------------------------------


def permissions() -> dict:
    raw = agent_step("claude")["with"]["settings"].replace("${{ runner.temp }}", RUNNER_TEMP)
    return json.loads(raw)["permissions"]


def bash_rule_matches(rule: str, command: str) -> bool:
    """Claude Code's Bash rule matching: `*` matches any text (spaces included);
    `Bash(ls *)` also matches bare `ls`; the legacy `prefix:*` form is a prefix."""
    if not rule.startswith("Bash(") or not rule.endswith(")"):
        return False
    pattern = rule[5:-1]
    if pattern.endswith(":*"):
        return command.startswith(pattern[:-2])
    if pattern.endswith(" *") and command == pattern[:-2]:
        return True
    return fnmatch.fnmatchcase(command, pattern)


def subcommands(command: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*(?:&&|\|\||;|\|)\s*", command) if part.strip()]


def decision(command: str) -> str:
    """deny > allow > ask, per subcommand; `ask` is a denial in a headless run."""
    perms = permissions()
    verdicts = []
    for part in subcommands(command):
        if any(bash_rule_matches(r, part) for r in perms["deny"]):
            return "deny"
        verdicts.append("allow" if any(bash_rule_matches(r, part) for r in perms["allow"]) else "ask")
    return "allow" if all(v == "allow" for v in verdicts) else "ask"


@pytest.mark.parametrize(
    "command",
    [
        "gh issue list --repo meridianlabs-ai/inspect_ai --state all --search 'Triage test_foo in:title,body' --limit 20",
        "gh issue view 444 --repo meridianlabs-ai/inspect_ai --comments",
        "gh run view 123 --repo meridianlabs-ai/actions --log-failed",
        "gh api repos/meridianlabs-ai/inspect_ai/issues/444 --jq .state",
        f"git -C inspect_ai log --oneline -50 {SHA}..origin/main",
        f"git -C inspect_ai log --oneline {SHA}..origin/main -- src/inspect_ai/model/_openai.py",
        "git -C inspect_ai show abc123",
        "git -C inspect_ai blame -L 10,20 src/inspect_ai/model/_openai.py",
        "grep -n 'FAILED' triage/failed.log",
    ],
)
def test_reads_the_triage_needs_are_allowed(command):
    assert decision(command) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        # Blocking finding, review round 1: git log/show write a file with --output.
        "git -C inspect_ai log -1 --format=tformat:x --output=/tmp/outside-landing.txt",
        "git -C inspect_ai log -1 --output /tmp/outside-landing.txt",
        "git -C inspect_ai show HEAD --output=/tmp/outside-landing.txt",
        # The gh verbs and gh-api flags that write.
        "gh issue create --repo meridianlabs-ai/inspect_ai --title t --body b",
        "gh issue comment 444 --repo meridianlabs-ai/inspect_ai --body hi",
        "gh issue edit 444 --repo meridianlabs-ai/inspect_ai --add-label auto",
        "gh issue reopen 444 --repo meridianlabs-ai/inspect_ai",
        "gh issue close 444 --repo meridianlabs-ai/inspect_ai",
        "gh api repos/meridianlabs-ai/inspect_ai/issues -f title=t",
        "gh api repos/meridianlabs-ai/inspect_ai/issues -F title=@t",
        "gh api repos/meridianlabs-ai/inspect_ai/issues --field title=t",
        "gh api repos/meridianlabs-ai/inspect_ai/issues --raw-field title=t",
        "gh api repos/meridianlabs-ai/inspect_ai/issues/444/comments --input body.json",
        "gh api -X POST repos/meridianlabs-ai/inspect_ai/issues/444/comments",
        "gh api repos/meridianlabs-ai/inspect_ai/issues/444 --method PATCH",
        # The old Slack channel.
        "curl -sS -X POST https://slack.com/api/chat.postMessage",
        "wget https://example.test",
        # A denied subcommand denies the compound command.
        "gh issue list --repo meridianlabs-ai/inspect_ai && gh issue create --repo meridianlabs-ai/inspect_ai --title t",
    ],
)
def test_writes_are_denied(command):
    assert decision(command) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        # Not granted: no rule matches, and a headless run denies what would prompt.
        "cd inspect_ai",
        "cd inspect_ai && git log --oneline -5",
        "git log --oneline -5",
        "git -C inspect_ai push origin HEAD",
        "git -C inspect_ai checkout main",
        "git -C .. log -1",
        "gh api graphql -f query='{viewer{login}}'",
        "gh api user",
        "gh pr create --title t",
        "jq . triage/run.json",
        "head -50 triage/failed.log",
        "sed -n '1w /tmp/outside-landing.txt' triage/failed.log",
        "grep FAILED triage/failed.log | head -5",
        "python3 -c 'print(1)'",
        "env",
    ],
)
def test_everything_else_is_not_granted(command):
    assert decision(command) != "allow"


def test_file_writes_are_scoped_to_the_landing_directory():
    perms = permissions()
    landing = f"Edit(//{RUNNER_TEMP.lstrip('/')}/landing/**)"
    assert landing in perms["allow"], perms["allow"]
    # No bare Edit/Write/NotebookEdit grant, and no path rule that reaches
    # outside the landing directory (Edit rules govern Write too).
    for rule in perms["allow"]:
        if rule.split("(")[0] in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
            assert rule == landing, rule
    assert "Write" not in perms["allow"] and "Edit" not in perms["allow"]


def test_agent_job_holds_no_secret_but_the_anthropic_key():
    wf = load_workflow()
    agent = yaml.safe_dump(wf["jobs"]["agent"])
    secrets = set(re.findall(r"secrets\.([A-Z_]+)", agent))
    assert secrets == {"ANTHROPIC_API_KEY", "GITHUB_TOKEN"}, secrets
    land = yaml.safe_dump(wf["jobs"]["land"])
    assert "MARVIN_TOKEN" in land and "SLACK_BOT_TOKEN" in land
    assert "checkout" not in land
