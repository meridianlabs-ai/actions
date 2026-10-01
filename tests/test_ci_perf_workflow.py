"""Tests for the publish job of inspect-ai-ci-perf.yml: where its publication result comes from.

The job downloads the analysis artifact the agent wrote, validates it, runs
the upstream publisher (which writes published.json on success) and adds the
published issues to Atlas. The artifact can carry a published.json of its
own, so the job must never present that file as this run's publication: the
Publish step deletes the path before the publisher runs, and the two
reporting steps (Show published issues, Retain published issue URLs outside
Git) run only when the publisher succeeded, whatever the Atlas step did after
it. The tests lift the job's steps from the YAML and run them in order under
bash with GitHub's step gating modelled explicitly (default success(),
`always()`, and the `always() && steps.<id>.outcome == '<value>'` shape this
job uses; any other condition fails the test until it is modelled). The publisher and gh
are stand-ins on PATH: the publisher follows the upstream contract (writes
published.json only when it returns successfully, may have made issue writes
before failing) and records what it saw; gh records every call and serves
canned issues. The `uses:` steps are modelled: download places the fixture,
upload records what it would have uploaded.

Run with `python3 -m pytest` from the repo root (needs pytest and PyYAML;
`.github/workflows/tests.yml` does the same in CI). Where sha256sum lacks
--check (macOS), a stand-in with GNU's --check semantics is put on PATH.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_PERF = ROOT / ".github" / "workflows" / "inspect-ai-ci-perf.yml"
ISSUE = "https://github.com/meridianlabs-ai/inspect_ai/issues/{}"
SEED = json.dumps([ISSUE.format(999)])  # a result the artifact carried
RUN_URL = "https://github.com/meridianlabs-ai/actions/actions/runs/123"
REPORTING_IF = "always() && steps.publish.outcome == 'success'"

DOWNLOAD = "Download CI evidence"
VALIDATE = "Validate proposed findings"
PUBLISH = "Publish fork issues and trend summary"
ATLAS = "Add published issues to Atlas"
SHOW = "Show published issues"
RETAIN = "Retain published issue URLs outside Git"

# The normal artifact: the three hashed inputs, the analysis context and the
# agent's report and findings. Contents are opaque to the stubbed publisher.
INPUTS = {
    "raw.json": '{"runs": []}\n',
    "measurements.md": "# Measurements\n",
    "summary.json": '{"schema_version": 1}\n',
    "previous-summaries.json": "[]\n",
    "report.md": "# Report\n",
    "findings.json": "[]\n",
}


def publish_steps() -> list[dict]:
    return yaml.safe_load(CI_PERF.read_text())["jobs"]["publish"]["steps"]


def step(name: str) -> dict:
    for s in publish_steps():
        if s.get("id") == name or str(s.get("name", "")).startswith(name):
            return s
    raise KeyError(name)


def key(s: dict) -> str:
    return s.get("id") or s["name"]


def run_bash(script: str, *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    # GitHub runs `run:` scripts with `bash --noprofile --norc -eo pipefail`.
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=cwd, env={**os.environ, **env}, text=True, capture_output=True, check=False)


def bin_dir(tmp_path: Path, **scripts: str) -> Path:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for name, body in scripts.items():
        (d / name).write_text(body)
        (d / name).chmod(0o755)
    return d


# --- Stand-ins ---------------------------------------------------------------------

# `python`: the publisher, following the upstream contract, or the Atlas body.
FAKE_PYTHON = f'''#!{sys.executable}
"""Stand-in for python: the upstream publisher per $FAKE_STORE/publisher.json, or `python -` run for real."""
import json, os, pathlib, subprocess, sys

args = sys.argv[1:]
if args == ["-"]:
    sys.exit(subprocess.run([sys.executable, "-"], stdin=sys.stdin).returncode)
if not args or not args[0].endswith("publish_ci_findings.py"):
    sys.exit(f"fake python: unexpected call {{args}}")
store = pathlib.Path(os.environ["FAKE_STORE"])
mode = json.loads((store / "publisher.json").read_text())
result = pathlib.Path(args[args.index("--directory") + 1]) / "published.json"
on_entry = "directory" if result.is_dir() else "file" if result.exists() else "absent"
with (store / "publisher-calls.jsonl").open("a") as log:
    log.write(json.dumps({{"argv": args, "result_on_entry": on_entry}}) + "\\n")
if "--publish" not in args:
    if not mode["validate"]:
        sys.exit("ValueError: Findings failed validation")
    print("Dry-run: validated 0 findings; no GitHub writes")
    sys.exit(0)
if mode["publish"] == "fail-after-write":
    # One finding's issue was created before the next write raised.
    subprocess.run(["gh", "api", "repos/meridianlabs-ai/inspect_ai/issues", "--input", "-"], input=json.dumps({{"title": "finding one"}}), text=True, check=True)
if mode["publish"] != "ok":
    sys.exit("RuntimeError: publication failed")
result.write_text(json.dumps(mode["urls"]))
'''

# `gh`: records every call; serves the issues in $FAKE_STORE/issues.json.
FAKE_GH = f'''#!{sys.executable}
"""Stand-in for gh: canned fork issues, recorded writes, a failing project mutation on request."""
import json, os, pathlib, re, sys

args = sys.argv[1:]
store = pathlib.Path(os.environ["FAKE_STORE"])
stdin = sys.stdin.read() if "--input" in args else ""
with (store / "gh-calls.jsonl").open("a") as log:
    log.write(json.dumps({{"argv": args, "stdin": stdin}}) + "\\n")
path = args[1] if args[:1] == ["api"] else ""
if path == "graphql":
    if (store / "gh-fail").exists():
        sys.exit("gh: GraphQL: Could not resolve to a node")
    print("{{}}")
elif path == "repos/meridianlabs-ai/inspect_ai/issues":
    print(json.dumps({{"number": 101}}))
elif re.fullmatch(r"repos/meridianlabs-ai/inspect_ai/issues/\\d+/assignees", path):
    print("{{}}")
elif re.fullmatch(r"repos/meridianlabs-ai/inspect_ai/issues/\\d+", path):
    number = path.rsplit("/", 1)[1]
    issues = json.loads((store / "issues.json").read_text())
    print(json.dumps({{"number": int(number), "node_id": f"NODE_{{number}}", "assignees": [{{"login": a}} for a in issues[number]]}}))
else:
    sys.exit(f"fake gh: unexpected call {{args}}")
'''

# GNU `sha256sum --check` for hosts whose sha256sum lacks it (macOS).
FAKE_SHA256SUM = f'''#!{sys.executable}
import hashlib, sys
if sys.argv[1:] != ["--check"]:
    sys.exit(f"fake sha256sum: unexpected call {{sys.argv[1:]}}")
status = 0
for line in sys.stdin:
    digest, path = line.rstrip("\\n").split("  ", 1)
    ok = hashlib.sha256(open(path, "rb").read()).hexdigest() == digest
    print(f"{{path}}: {{'OK' if ok else 'FAILED'}}")
    status |= not ok
sys.exit(status)
'''


def gnu_sha256sum() -> bool:
    """Whether the host's sha256sum verifies `<hash>  <path>` lines on stdin (GNU does; macOS prints usage)."""
    empty = hashlib.sha256(b"").hexdigest()
    return subprocess.run(["sha256sum", "--check"], input=f"{empty}  /dev/null\n", text=True, capture_output=True, check=False).returncode == 0


# --- The job, with GitHub's step gating modelled -----------------------------------------


@dataclass
class JobRun:
    outcomes: dict[str, str] = field(default_factory=dict)  # by step id or name: success, failure, skipped
    logs: dict[str, subprocess.CompletedProcess] = field(default_factory=dict)
    summary: str = ""
    upload: str | None = None  # what Retain would have uploaded, None when it did not run
    gh_calls: list[dict] = field(default_factory=list)
    publisher_calls: list[dict] = field(default_factory=list)


def should_run(condition: str | None, outcomes: dict[str, str], job_ok: bool, job_env: dict[str, str]) -> bool:
    if condition is None:
        return job_ok  # the default success() gate
    if condition == "always()":
        return True
    if m := re.fullmatch(r"always\(\) && steps\.(\w+)\.outcome == '(\w+)'", condition):
        return outcomes[m.group(1)] == m.group(2)
    if m := re.fullmatch(r"env\.(\w+) == '(\w+)'", condition):
        return job_env[m.group(1)] == m.group(2)
    raise AssertionError(f"step condition not modelled: {condition}")


def run_publish_job(
    tmp_path: Path,
    *,
    seed: str | None = SEED,
    seed_is_directory: bool = False,
    conclusion: str = "success",
    tampered: bool = False,
    validate: bool = True,
    publish: str = "ok",
    urls: list[str] = (),
    assignees: dict[int, list[str]] | None = None,
    atlas_fails: bool = False,
) -> JobRun:
    out = tmp_path / "ci-perf"
    store = tmp_path / "store"
    store.mkdir()
    (store / "publisher.json").write_text(json.dumps({"validate": validate, "publish": publish, "urls": list(urls)}))
    (store / "issues.json").write_text(json.dumps({str(n): a for n, a in (assignees or {}).items()}))
    if atlas_fails:
        (store / "gh-fail").touch()
    stubs = {"python": FAKE_PYTHON, "gh": FAKE_GH}
    if not gnu_sha256sum():
        stubs["sha256sum"] = FAKE_SHA256SUM
    summary = tmp_path / "summary.md"
    summary.touch()
    job_env = {
        "PATH": f"{bin_dir(tmp_path, **stubs)}:{os.environ['PATH']}",
        "FAKE_STORE": str(store),
        "CI_PERF_OUTPUT_DIR": str(out),
        "CI_PERF_RUN_ATTEMPT": "1",
        "CI_PERF_RUN_URL": RUN_URL,
        "GITHUB_STEP_SUMMARY": str(summary),
        "PYTHONDONTWRITEBYTECODE": "1",
        "MARVIN_APP_CONFIGURED": "false",  # the PAT fallback
    }
    sha = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in INPUTS.items()}
    needs = {
        "analysis_conclusion": conclusion,
        "raw_sha": "0" * 64 if tampered else sha["raw.json"],
        "measurements_sha": sha["measurements.md"],
        "summary_sha": sha["summary.json"],
    }
    run = JobRun()

    def render(text: str) -> str:
        def value(m: re.Match) -> str:
            expression = m.group(1).strip()
            if expression == "steps.mint.outputs.token || secrets.MARVIN_TOKEN":
                return "fake-marvin-token"
            if expression == "env.CI_PERF_OUTPUT_DIR":
                return str(out)
            if expression in ("github.run_id", "github.run_attempt"):
                return "1"
            if m2 := re.fullmatch(r"needs\.analyze\.outputs\.(\w+)", expression):
                return needs[m2.group(1)]
            if m2 := re.fullmatch(r"steps\.(\w+)\.outcome", expression):
                return run.outcomes[m2.group(1)]
            raise AssertionError(f"expression not modelled: {expression}")

        return re.sub(r"\$\{\{(.*?)\}\}", value, text)

    job_ok = True
    for s in publish_steps():
        k = key(s)
        if not should_run(s.get("if"), run.outcomes, job_ok, job_env):
            run.outcomes[k] = "skipped"
            continue
        if "uses" in s:
            if s["name"] == DOWNLOAD:
                out.mkdir()
                for name, text in INPUTS.items():
                    (out / name).write_text(text)
                if seed_is_directory:
                    (out / "published.json").mkdir()
                    (out / "published.json" / "x").write_text(seed or "")
                elif seed is not None:
                    (out / "published.json").write_text(seed)
            elif s["name"] == RETAIN:
                path = Path(render(s["with"]["path"]))
                if path.is_file():
                    run.upload = path.read_text()
                elif s["with"]["if-no-files-found"] == "error":
                    run.outcomes[k] = "failure"
                    job_ok = False
                    continue
            run.outcomes[k] = "success"
            continue
        assert "${{" not in s["run"], f"{s['name']}: expression inside run:"
        env = {**job_env, **{name: render(str(v)) for name, v in (s.get("env") or {}).items()}}
        r = run_bash(s["run"], cwd=tmp_path, env=env)
        run.logs[k] = r
        run.outcomes[k] = "success" if r.returncode == 0 else "failure"
        job_ok = job_ok and r.returncode == 0
    run.summary = summary.read_text()
    for name, target in (("gh-calls.jsonl", run.gh_calls), ("publisher-calls.jsonl", run.publisher_calls)):
        if (store / name).exists():
            target.extend(json.loads(line) for line in (store / name).read_text().splitlines())
    return run


def outcome(run: JobRun, name: str) -> str:
    return run.outcomes[key(step(name))]


def atlas_calls(run: JobRun) -> list[dict]:
    """gh calls made by the Atlas body: issue reads, project mutations and assignments."""
    return [c for c in run.gh_calls if c["argv"][1] != "repos/meridianlabs-ai/inspect_ai/issues"]


def assert_nothing_reported(run: JobRun) -> None:
    assert outcome(run, SHOW) == "skipped" and outcome(run, RETAIN) == "skipped", run.outcomes
    assert run.summary == "" and run.upload is None
    assert "999" not in run.summary


# --- The YAML -----------------------------------------------------------------------------


def test_reporting_runs_only_on_publisher_success_and_atlas_keeps_the_default_gate():
    names = [key(s) for s in publish_steps()]
    publish, atlas, show, retain = (step(n) for n in (PUBLISH, ATLAS, SHOW, RETAIN))
    assert publish["id"] == "publish" and atlas["id"] == "atlas"
    assert names.index("publish") < names.index("atlas") < names.index(key(show)) < names.index(key(retain))
    assert "if" not in publish and "if" not in atlas  # default success(): skipped after any failure
    assert show["if"] == REPORTING_IF and retain["if"] == REPORTING_IF
    assert show["env"] == {"ATLAS_OUTCOME": "${{ steps.atlas.outcome }}"}
    assert retain["with"]["path"] == "${{ env.CI_PERF_OUTPUT_DIR }}/published.json"
    assert retain["with"]["if-no-files-found"] == "error"


def test_the_publish_step_deletes_a_downloaded_result_before_the_publisher_runs():
    lines = [line for line in step(PUBLISH)["run"].splitlines() if line.strip()]
    assert lines[0] == 'rm -rf -- "$CI_PERF_OUTPUT_DIR/published.json"'
    assert "publish_ci_findings.py" in lines[1] and "--publish" in step(PUBLISH)["run"]


def test_no_expression_inside_a_publish_run_script():
    for s in publish_steps():
        assert "${{" not in (s.get("run") or ""), s.get("name")


# --- Failure before or inside the publisher: a seeded result is never reported -------------


@pytest.mark.parametrize("failure", [{"tampered": True}, {"validate": False}, {"conclusion": "failure"}, {"conclusion": ""}],
                         ids=["hash-mismatch", "findings-rejected", "analysis-not-success", "analysis-never-ran"])
def test_a_seeded_result_is_not_reported_when_validation_fails(tmp_path, failure):
    run = run_publish_job(tmp_path, **failure)
    assert outcome(run, VALIDATE) == "failure"
    assert outcome(run, PUBLISH) == "skipped" and outcome(run, ATLAS) == "skipped"
    assert_nothing_reported(run)
    assert run.gh_calls == []
    assert not any("--publish" in c["argv"] for c in run.publisher_calls)
    # The seed survives on disk (nothing deleted it) but nothing consumes it.
    assert (tmp_path / "ci-perf" / "published.json").read_text() == SEED


@pytest.mark.parametrize("publish", ["fail", "fail-after-write"])
def test_a_seeded_result_is_not_reported_when_the_publisher_fails(tmp_path, publish):
    run = run_publish_job(tmp_path, publish=publish)
    assert outcome(run, VALIDATE) == "success" and outcome(run, PUBLISH) == "failure"
    assert outcome(run, ATLAS) == "skipped"
    assert_nothing_reported(run)
    # The publisher never saw the seed, and left no result behind.
    (call,) = [c for c in run.publisher_calls if "--publish" in c["argv"]]
    assert call["result_on_entry"] == "absent"
    assert not (tmp_path / "ci-perf" / "published.json").exists()
    # Partial publication: an issue was created before the failure, and still nothing is reported.
    writes = [c for c in run.gh_calls if c["argv"][1] == "repos/meridianlabs-ai/inspect_ai/issues"]
    assert len(writes) == (1 if publish == "fail-after-write" else 0)
    assert atlas_calls(run) == []


def test_a_failed_publisher_without_a_seed_reports_nothing_and_does_not_crash(tmp_path):
    run = run_publish_job(tmp_path, seed=None, publish="fail")
    assert outcome(run, PUBLISH) == "failure"
    assert_nothing_reported(run)


# --- Success: the publisher's result is what is reported -----------------------------------

TWO = [ISSUE.format(101), ISSUE.format(102)]


@pytest.mark.parametrize("seed", [SEED, None, "directory"], ids=["seeded", "no-seed", "seeded-directory"])
@pytest.mark.parametrize("urls", [[], TWO], ids=["empty", "two-issues"])
def test_successful_publication_reports_the_publisher_result(tmp_path, seed, urls):
    run = run_publish_job(tmp_path, seed=None if seed is None else SEED, seed_is_directory=seed == "directory", urls=urls, assignees={101: [], 102: ["someone"]})
    assert all(v == "success" for k, v in run.outcomes.items() if k != "mint"), run.outcomes
    (call,) = [c for c in run.publisher_calls if "--publish" in c["argv"]]
    assert call["result_on_entry"] == "absent"
    assert (tmp_path / "ci-perf" / "published.json").read_text() == json.dumps(urls)
    # Shown and retained: the publisher's result, and no Atlas caveat.
    assert run.summary.startswith("Published fork issues (the publisher's result for this run):\n\n" + json.dumps(urls) + "\n")
    assert "999" not in run.summary and "Atlas step outcome" not in run.summary
    assert run.upload == json.dumps(urls)
    # Atlas: each issue added once, only the unassigned one assigned to Eric.
    calls = atlas_calls(run)
    if not urls:
        assert calls == []
    else:
        assert [c["argv"][1] for c in calls if c["argv"][1] == "graphql"] == ["graphql", "graphql"]
        assert [c for c in calls if "--method" in c["argv"]] == [{"argv": ["api", "repos/meridianlabs-ai/inspect_ai/issues/101/assignees", "--method", "POST", "--input", "-", "--silent"], "stdin": json.dumps({"assignees": ["epatey"]})}]


def test_the_publisher_result_is_still_reported_when_atlas_fails(tmp_path):
    run = run_publish_job(tmp_path, urls=TWO, assignees={101: [], 102: []}, atlas_fails=True)
    assert outcome(run, PUBLISH) == "success" and outcome(run, ATLAS) == "failure"
    assert outcome(run, SHOW) == "success" and outcome(run, RETAIN) == "success"
    assert json.dumps(TWO) in run.summary and "999" not in run.summary
    assert "Atlas step outcome: failure." in run.summary and "not proof" in run.summary
    assert run.upload == json.dumps(TWO)
    # The failure stopped Atlas at the first mutation: no assignment happened.
    assert not any("--method" in c["argv"] for c in run.gh_calls)


# --- The analyze job: WIF and agents' launcher ---------------------------------------------
#
# analyze holds nothing the agent may not have: the read-only job token and
# the model credential, a WIF token for ci-perf's capped workspace that
# claude-code-action mints from the job's OIDC token. agents' launcher starts
# the CLI as claude-agent in its own namespace, with the checkout read-only.
# The runner reads what the agent wrote only through import-codex-final, and
# shows and uploads only its own directory (finding 4773274).


def analyze_steps() -> list[dict]:
    return yaml.safe_load(CI_PERF.read_text())["jobs"]["analyze"]["steps"]


def analyze_step(name: str) -> dict:
    for s in analyze_steps():
        if s.get("id") == name or str(s.get("name", "")).startswith(name):
            return s
    raise KeyError(name)


AGENTS = "meridianlabs-ai/agents/.github/actions/{}@main"
WIF = {
    "anthropic_federation_rule_id": "fdrl_011HrZrxamS56HuRWRMbqYBH",
    "anthropic_organization_id": "be5d0086-bc43-45d2-9184-20ecdd647aa7",
    "anthropic_service_account_id": "svac_01GhGfdWv1dYwL9WEPE2esoV",
    "anthropic_workspace_id": "wrkspc_0111jFg7osvhUziZXCNh5sfB",
}
ALLOWED_ENDPOINTS = {
    "api.anthropic.com:443",
    "api.github.com:443",
    "github.com:443",
    "claude.ai:443",
    "downloads.claude.ai:443",
    "registry.npmjs.org:443",
    "release-assets.githubusercontent.com:443",
}
AFTER_AGENT = ["Reclaim workspace from the agent", "Import the agent's files", "Assemble the evidence", "Show report", "Retain CI evidence outside Git"]
COLLECTED = ("raw.json", "summary.json", "measurements.md", "previous-summaries.json")


def test_analyze_runs_the_agent_through_the_launcher_in_order():
    assert [s["name"] for s in analyze_steps()] == [
        "Harden runner",
        "Initialize output directory",
        "Checkout upstream source for analysis",
        "Set up Python",
        "Collect timings and prior summaries",
        "Create agent user",
        "Create the scratch directory",
        "Prepare the agent launch",
        "Stage the collected data for the agent",
        "Analyze CI performance",
        *AFTER_AGENT,
    ]


def test_analyze_references_no_secret_and_holds_reads_plus_id_token():
    job = jobs()["analyze"]
    assert job["permissions"] == {"contents": "read", "actions": "read", "issues": "read", "pull-requests": "read", "id-token": "write"}
    text = yaml.safe_dump(job)
    assert "secrets." not in text
    assert "MARVIN" not in text and "create-github-app-token" not in text and "steps.mint" not in text


def test_no_model_broker_isolated_agent_or_api_key_remains():
    assert "ANTHROPIC_API_KEY" not in CI_PERF.read_text()  # not even in a comment
    text = yaml.safe_dump(workflow())
    for gone in ("anthropic_api_key", "claude_code_oauth_token", "model-broker", "isolated-agent", "actions-repo"):
        assert gone not in text, gone
    for job in jobs().values():
        assert not any(str(s.get("uses", "")).startswith("./") for s in job["steps"])


def test_only_analyze_can_request_an_oidc_token_and_its_agent_passes_the_job_token():
    assert "permissions" not in workflow()  # no workflow-level grant reaching every job
    holders = [name for name, job in jobs().items() if "id-token" in (job.get("permissions") or {})]
    assert holders == ["analyze"]
    actions = [s for job in jobs().values() for s in job["steps"] if str(s.get("uses", "")).startswith("anthropics/claude-code-action")]
    assert actions == [analyze_step("analysis")]
    assert actions[0]["with"]["github_token"] == "${{ github.token }}"


def test_the_agent_step_authenticates_with_wif_through_the_launcher():
    agent = analyze_step("analysis")
    assert agent["uses"] == "anthropics/claude-code-action@v1"
    w = agent["with"]
    assert {k: w[k] for k in WIF} == WIF
    assert w["github_token"] == "${{ github.token }}"
    assert w["path_to_claude_code_executable"] == "${{ steps.launcher.outputs.executable }}"
    assert w["classify_inline_comments"] == "false"
    # No static credential (the action ignores WIF when a key is set), and no
    # transcript in the log or the step summary (the defaults).
    assert set(w) == {*WIF, "github_token", "path_to_claude_code_executable", "classify_inline_comments", "claude_args", "settings", "prompt"}
    # The job output that gates publish comes from the action's conclusion.
    assert jobs()["analyze"]["outputs"]["analysis_conclusion"] == "${{ steps.analysis.outputs.conclusion }}"


def test_the_agent_step_args_and_settings():
    w = analyze_step("analysis")["with"]
    args = w["claude_args"].replace("${{ runner.temp }}", "/rt").split()
    # The Opus alias, not a dated model id, at default reasoning effort.
    assert args[args.index("--model") + 1] == "opus"
    assert not any(a.startswith("--effort") for a in args)
    assert args[args.index("--allowedTools") + 1] == "Bash,Read,Edit,Write,Grep,Glob"
    # Nothing is loaded from the checkout: no settings, hooks, CLAUDE.md or skills.
    assert args[args.index("--setting-sources") + 1] == "user"
    assert args[args.index("--add-dir") + 1] == "/rt/claude-agent"
    # The wrapper's environment allow-list drops every other variable, so the
    # two values the prompt names travel as settings env; neither is a secret.
    rendered = w["settings"].replace("${{ runner.temp }}", "/rt").replace("${{ env.CI_PERF_RUN_URL }}", RUN_URL)
    assert "${{" not in rendered
    assert json.loads(rendered) == {"env": {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CI_PERF_OUTPUT_DIR": "/rt/claude-agent",
        "CI_PERF_RUN_URL": RUN_URL,
    }}


def test_the_checkout_is_at_the_workspace_root_and_the_paths_follow_it():
    (checkout,) = upstream_checkouts("analyze")
    assert "path" not in checkout["with"]  # the launcher needs a git checkout at the root
    collect = analyze_step("data")["run"]
    assert collect.count("python .claude/skills/ci-perf/scripts/") == 3
    prompt = analyze_step("analysis")["with"]["prompt"]
    assert ".claude/skills/ci-perf/SKILL.md" in prompt and "design/ci-perf/" in prompt
    assert "inspect_ai/" not in collect and "inspect_ai/" not in prompt.replace("meridianlabs-ai/inspect_ai", "")


def test_the_launcher_composites_take_grant_none_and_gate_in_order():
    agentuser = analyze_step("agentuser")
    assert agentuser["uses"] == AGENTS.format("create-codex-user")
    assert agentuser["with"] == {"user": "claude-agent", "grant": "none"}
    launcher = analyze_step("launcher")
    assert launcher["uses"] == AGENTS.format("claude-agent-launcher")
    assert launcher["with"] == {"grant": "none"}
    # grant: none binds $RUNNER_TEMP/scratch into the namespace and requires it.
    assert analyze_step("Create the scratch directory")["run"].strip() == 'sudo install -d -o claude-agent -g claude-agent -m 0700 "$RUNNER_TEMP/scratch"'
    reclaim = analyze_step("agentreclaim")
    assert reclaim["uses"] == AGENTS.format("reclaim-codex-workspace")
    assert reclaim["with"] == {"user": "claude-agent"}
    assert reclaim["if"] == "always() && steps.agentuser.outcome == 'success'"
    imp = analyze_step("import")
    assert imp["uses"] == AGENTS.format("import-codex-final")
    assert imp["with"] == {"mode": "dir", "dest": "${{ runner.temp }}/ci-perf-agent"}
    assert imp["if"] == "always() && steps.agentreclaim.outcome == 'success'"
    assert analyze_step("Assemble the evidence")["if"] == "always() && steps.import.outcome == 'success'"


def test_analyze_harden_runner_allows_exactly_its_hosts_and_does_not_disable_sudo():
    harden = analyze_steps()[0]
    assert harden["name"] == "Harden runner"
    with_ = harden["with"]
    assert with_["egress-policy"] == "block"
    # B1: the pre hook would drop sudo before the agent-user setup and the launcher.
    assert "disable-sudo-and-containers" not in with_ and "disable-sudo" not in with_
    endpoints = with_["allowed-endpoints"].split()
    assert len(endpoints) == len(ALLOWED_ENDPOINTS) and set(endpoints) == ALLOWED_ENDPOINTS


def test_no_step_after_the_agent_holds_a_secret_or_reads_the_landing_directory():
    names = [s["name"] for s in analyze_steps()]
    after = analyze_steps()[names.index("Analyze CI performance") + 1:]
    assert [s["name"] for s in after] == AFTER_AGENT
    for s in after:
        assert "secrets." not in yaml.safe_dump(s), s["name"]
    # Only the reclaim (by user) and the import (its default source) touch
    # the landing directory; the rest read the runner's own directory.
    for s in after[2:]:
        assert "claude-agent" not in yaml.safe_dump(s), s["name"]
    assert analyze_step("Show report")["if"] == "always()"
    retain = analyze_step("Retain CI evidence outside Git")
    assert retain["if"] == "always()"
    assert retain["with"]["path"] == "${{ runner.temp }}/ci-perf/"


# `sudo`: records its argv; runs `install` without the owner flags, which
# need root.
FAKE_SUDO = f'''#!{sys.executable}
import os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_SUDO_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
if args[:1] != ["install"]:
    sys.exit(f"fake sudo: unexpected call {{args}}")
rest, i = [], 1
while i < len(args):
    if args[i] in ("-o", "-g"):
        i += 2
        continue
    rest.append(args[i])
    i += 1
sys.exit(subprocess.run(["install", *rest]).returncode)
'''


def test_the_agent_gets_copies_of_the_collected_data(tmp_path):
    rt = tmp_path / "rt"
    out = rt / "ci-perf"
    out.mkdir(parents=True)
    (rt / "claude-agent").mkdir()
    for name in COLLECTED:
        (out / name).write_text(INPUTS[name])
    log = tmp_path / "sudo.log"
    env = {"PATH": f"{bin_dir(tmp_path, sudo=FAKE_SUDO)}:{os.environ['PATH']}", "RUNNER_TEMP": str(rt), "CI_PERF_OUTPUT_DIR": str(out), "FAKE_SUDO_LOG": str(log)}
    r = run_bash(analyze_step("Stage the collected data for the agent")["run"], cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stderr
    assert log.read_text().splitlines() == [f"install -o claude-agent -g claude-agent -m 0644 {out}/{n} {rt}/claude-agent/{n}" for n in COLLECTED]
    for name in COLLECTED:
        assert (rt / "claude-agent" / name).read_text() == (out / name).read_text()


def run_post_agent(tmp_path: Path, *, imported: dict[str, str] | None) -> tuple[Path, str]:
    """Run Assemble the evidence and Show report after an agent that planted
    links in its landing directory. `imported` is what import-codex-final
    copied (regular files only), None when it refused the import."""
    rt = tmp_path / "rt"
    out = rt / "ci-perf"
    out.mkdir(parents=True)
    for name in COLLECTED:
        (out / name).write_text(INPUTS[name])
    secret = tmp_path / "environ"
    secret.write_text("ACTIONS_ID_TOKEN_REQUEST_TOKEN=SECRET\n")
    landing = rt / "claude-agent"
    landing.mkdir()
    for name in ("report.md", "findings.json", "measurements.md"):
        (landing / name).symlink_to(secret)
    if imported is not None:
        (rt / "ci-perf-agent").mkdir()
        for name, text in imported.items():
            (rt / "ci-perf-agent" / name).write_text(text)
    summary = tmp_path / "summary.md"
    summary.touch()
    job_env = {"RUNNER_TEMP": str(rt), "CI_PERF_OUTPUT_DIR": str(out), "GITHUB_STEP_SUMMARY": str(summary)}
    values = {"runner.temp": str(rt), "github.event_name == 'workflow_dispatch' && inputs.dry_run": "true"}

    def render(text: str) -> str:
        return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda m: values[m.group(1)], text)

    for name in ("Assemble the evidence", "Show report"):
        s = analyze_step(name)
        env = {**job_env, **{k: render(str(v)) for k, v in (s.get("env") or {}).items()}}
        r = run_bash(s["run"], cwd=tmp_path, env=env)
        assert r.returncode == 0, (name, r.stderr)
    return out, summary.read_text()


def test_post_agent_reads_never_follow_the_agents_links(tmp_path):
    # The agent also rewrote its staged raw.json: only report.md and
    # findings.json join the runner's collected data.
    out, summary = run_post_agent(tmp_path, imported={"report.md": "# Imported report\n", "findings.json": "[]\n", "raw.json": "tampered\n"})
    assert "SECRET" not in summary
    assert summary == "Dry-run: true\n\n# Imported report\n\n# Measurements\n\n"
    assert (out / "report.md").read_text() == "# Imported report\n" and not (out / "report.md").is_symlink()
    assert (out / "findings.json").read_text() == "[]\n"
    assert (out / "raw.json").read_text() == INPUTS["raw.json"]
    assert sorted(p.name for p in out.iterdir()) == sorted([*COLLECTED, "report.md", "findings.json"])


def test_a_refused_import_adds_nothing_from_the_agent(tmp_path):
    out, summary = run_post_agent(tmp_path, imported=None)
    assert "SECRET" not in summary
    assert summary == "Dry-run: true\n\n# Measurements\n\n"
    assert sorted(p.name for p in out.iterdir()) == sorted(COLLECTED)


def test_every_job_restores_caches_but_cannot_save_them():
    # Workflow-level cache-mode: read (meridianlabs-ai/agents
    # design/agent-cache-scope.md). A job-level key would override it, so the
    # file carries exactly one, at column 0.
    wf = yaml.safe_load(CI_PERF.read_text())
    assert wf["cache-mode"] == "read"
    keys = [l for l in CI_PERF.read_text().splitlines() if re.match(r"\s*cache-mode\s*:", l)]
    assert keys == ["cache-mode: read"], keys


def test_no_secret_input_or_step_output_expression_inside_an_analyze_run_script():
    for s in analyze_steps():
        hit = re.search(r"\$\{\{\s*(inputs|steps|needs|secrets|github\.event)\b", s.get("run") or "")
        assert hit is None, f"{s.get('name')}: {hit.group(0)} inside run:"


# --- resolve and tooling: one upstream `main` commit, third-party code away from the key --
#
# resolve fixes the upstream `main` SHA that tooling, analyze and publish all
# check out; no job reads a ref from a person. tooling runs the pip install
# and upstream's script tests with only a read-only token. publish takes its
# checkout SHA from resolve and computes its artifact name itself, so neither
# comes from the job whose runner the agent had.

UPSTREAM = "https://github.com/UKGovernmentBEIS/inspect_ai.git"
MAIN_SHA = "0123456789abcdef0123456789abcdef01234567"
ARTIFACT = "ci-perf-${{ github.run_id }}-${{ github.run_attempt }}"


def workflow() -> dict:
    return yaml.safe_load(CI_PERF.read_text())


def jobs() -> dict:
    return workflow()["jobs"]


# `git`: records its argv and prints $FAKE_LS_REMOTE, exiting $FAKE_GIT_STATUS.
FAKE_GIT = f'''#!{sys.executable}
import os, pathlib, sys
pathlib.Path(os.environ["FAKE_GIT_ARGV"]).write_text("\\n".join(sys.argv[1:]))
sys.stdout.write(os.environ.get("FAKE_LS_REMOTE", ""))
sys.exit(int(os.environ.get("FAKE_GIT_STATUS", "0")))
'''


def run_resolve(tmp_path: Path, ls_remote: str, status: int = 0) -> tuple[subprocess.CompletedProcess, str, list[str]]:
    (s,) = jobs()["resolve"]["steps"]
    output = tmp_path / "github_output"
    output.touch()
    argv = tmp_path / "git-argv"
    env = {
        "PATH": f"{bin_dir(tmp_path, git=FAKE_GIT)}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output),
        "FAKE_GIT_ARGV": str(argv),
        "FAKE_LS_REMOTE": ls_remote,
        "FAKE_GIT_STATUS": str(status),
    }
    r = run_bash(s["run"], cwd=tmp_path, env=env)
    return r, output.read_text(), argv.read_text().splitlines() if argv.exists() else []


@pytest.mark.parametrize("ls_remote", [
    f"{MAIN_SHA}\trefs/heads/main\n",
    # ls-remote's pattern matches any ref ending in refs/heads/main; only the exact name counts.
    f"{'f' * 40}\trefs/remotes/fork/refs/heads/main\n{MAIN_SHA}\trefs/heads/main\n{'e' * 40}\trefs/heads/main-old\n",
], ids=["one-line", "extra-lines"])
def test_resolve_writes_the_sha_of_refs_heads_main(tmp_path, ls_remote):
    r, output, argv = run_resolve(tmp_path, ls_remote)
    assert r.returncode == 0, r.stderr
    assert argv == ["ls-remote", UPSTREAM, "refs/heads/main"]
    assert output == f"sha<<EOF\n{MAIN_SHA}\nEOF\n"


@pytest.mark.parametrize(("ls_remote", "status"), [
    ("", 0),
    (f"{MAIN_SHA[:39]}\trefs/heads/main\n", 0),
    (f"{MAIN_SHA.upper()}\trefs/heads/main\n", 0),
    (f"{'f' * 40}\trefs/heads/main-old\n", 0),
    (f"{MAIN_SHA}\trefs/heads/main\n{'e' * 40}\trefs/heads/main\n", 0),
    (f"{MAIN_SHA}\trefs/heads/main\n", 128),
], ids=["no-line", "short-sha", "not-lowercase-hex", "no-exact-ref", "two-main-lines", "ls-remote-failed"])
def test_resolve_fails_and_writes_nothing_without_one_valid_sha(tmp_path, ls_remote, status):
    r, output, _ = run_resolve(tmp_path, ls_remote, status)
    assert r.returncode != 0
    assert output == ""


def test_resolve_holds_nothing_and_runs_no_action():
    job = jobs()["resolve"]
    assert job["permissions"] == {}
    assert "needs" not in job
    assert job["outputs"] == {"sha": "${{ steps.main.outputs.sha }}"}
    (s,) = job["steps"]
    assert s["id"] == "main" and "uses" not in s and "env" not in s
    assert "${{" not in s["run"] and "secrets." not in yaml.safe_dump(job)


def test_dispatch_takes_no_ref():
    assert set(workflow()["on"]["workflow_dispatch"]["inputs"]) == {"dry_run"}
    assert "inspect_ai_ref" not in CI_PERF.read_text()
    assert jobs()["publish"]["if"] == "github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && inputs.dry_run == false)"


def upstream_checkouts(job: str) -> list[dict]:
    return [s for s in jobs()[job]["steps"] if str(s.get("uses", "")).startswith("actions/checkout@") and s["with"].get("repository") == "UKGovernmentBEIS/inspect_ai"]


@pytest.mark.parametrize("job", ["tooling", "analyze", "publish"])
def test_every_upstream_checkout_is_the_resolved_commit(job):
    (checkout,) = upstream_checkouts(job)
    assert checkout["with"]["ref"] == "${{ needs.resolve.outputs.sha }}"
    assert checkout["with"]["persist-credentials"] is False
    assert "resolve" in ([jobs()[job]["needs"]] if isinstance(jobs()[job]["needs"], str) else jobs()[job]["needs"])


def test_the_job_graph():
    assert list(jobs()) == ["resolve", "tooling", "analyze", "publish"]
    assert jobs()["tooling"]["needs"] == "resolve"
    assert jobs()["analyze"]["needs"] == ["resolve", "tooling"]
    assert jobs()["publish"]["needs"] == ["resolve", "analyze"]


def test_tooling_runs_the_third_party_install_with_only_a_read_token():
    job = jobs()["tooling"]
    assert job["permissions"] == {"contents": "read"}
    text = yaml.safe_dump(job)
    assert "secrets." not in text and "id-token" not in text and "steps.mint" not in text
    (test,) = [s for s in job["steps"] if s.get("name") == "Test CI tooling"]
    lines = test["run"].splitlines()
    assert lines[0] == "python -m pip install pytest"
    assert "inspect_ai/.claude/skills/ci-perf/scripts/test_ci_perf.py" in lines[1]
    for s in job["steps"]:
        assert "${{" not in (s.get("run") or ""), s.get("name")


def test_analyze_installs_nothing_from_pypi_and_no_longer_reports_a_sha_or_name():
    job = jobs()["analyze"]
    assert not any(s.get("name") == "Test CI tooling" for s in job["steps"])
    assert not any("pip install" in (s.get("run") or "") for s in job["steps"])
    assert set(job["outputs"]) == {"raw_sha", "measurements_sha", "summary_sha", "analysis_conclusion"}


def test_publish_takes_nothing_but_hashes_and_the_conclusion_from_analyze():
    steps = publish_steps()
    assert step(DOWNLOAD)["with"]["name"] == ARTIFACT
    upload = [s for s in analyze_steps() if s.get("name") == "Retain CI evidence outside Git"][0]
    assert upload["with"]["name"] == ARTIFACT  # the name analyze uploads under
    for s in steps:
        for value in [s.get("run") or "", *(str(v) for k, v in (s.get("with") or {}).items() if k in ("ref", "name", "path", "repository"))]:
            assert "needs.analyze" not in value, s.get("name")
    used = set(re.findall(r"needs\.analyze\.outputs\.(\w+)", yaml.safe_dump(jobs()["publish"])))
    assert used == {"analysis_conclusion", "raw_sha", "measurements_sha", "summary_sha"}
    assert set(re.findall(r"needs\.analyze\.outputs\.(\w+)", yaml.safe_dump(step(VALIDATE)["env"]))) == used
