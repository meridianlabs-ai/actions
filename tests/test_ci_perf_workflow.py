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


# --- The analyze job: the model key never reaches the agent -----------------
#
# The whole-agent isolation of the security work: the key reaches only the
# broker step, harden-runner keeps sudo (B1), and the analysis runs the entire
# Claude process as a dedicated unprivileged user through the isolated-agent
# action rather than as the runner user through claude-code-action.


def analyze_steps() -> list[dict]:
    return yaml.safe_load(CI_PERF.read_text())["jobs"]["analyze"]["steps"]


def analyze_step(name: str) -> dict:
    for s in analyze_steps():
        if s.get("id") == name or str(s.get("name", "")).startswith(name):
            return s
    raise KeyError(name)


def test_analyze_key_reaches_only_the_broker():
    for s in analyze_steps():
        if s.get("id") == "broker":
            assert set(re.findall(r"secrets\.([A-Z_]+)", yaml.safe_dump(s))) == {"CI_PERF_ANTHROPIC_API_KEY"}
        else:
            assert "ANTHROPIC_API_KEY" not in yaml.safe_dump(s), s.get("name")
    broker = analyze_step("broker")
    assert broker["uses"] == "./actions-repo/.github/actions/model-broker"
    assert broker["with"]["api-key"] == "${{ secrets.CI_PERF_ANTHROPIC_API_KEY }}"
    checkout = analyze_step("Check out the isolation actions")
    assert checkout["with"]["sparse-checkout"] == ".github/actions"


def test_analyze_harden_runner_blocks_egress_and_does_not_disable_sudo():
    with_ = analyze_step("Harden runner")["with"]
    assert with_["egress-policy"] == "block"
    # B1: the pre hook would drop sudo before the broker/agent-user bootstrap.
    assert "disable-sudo-and-containers" not in with_ and "disable-sudo" not in with_
    endpoints = with_["allowed-endpoints"].split()
    assert all(e.endswith(":443") for e in endpoints)
    assert "api.anthropic.com:443" in endpoints  # the broker's upstream


def test_analyze_runs_the_whole_agent_as_the_isolated_user():
    agent = analyze_step("analysis")
    assert agent["uses"] == "./actions-repo/.github/actions/isolated-agent"
    w = agent["with"]
    assert w["token"] == "${{ steps.broker.outputs.token }}"
    assert w["base-url"] == "${{ steps.broker.outputs.base-url }}"
    assert w["github-token"] == "${{ github.token }}"
    assert w["write-dir"] == "${{ env.CI_PERF_OUTPUT_DIR }}"
    assert "--model fable" in w["claude-args"]
    # forward-env carries only non-secret values the prompt names.
    forwarded = {ln.split("=", 1)[0] for ln in w["forward-env"].splitlines() if "=" in ln}
    assert forwarded == {"CI_PERF_OUTPUT_DIR", "CI_PERF_RUN_URL"}
    assert "ANTHROPIC" not in w["forward-env"] and "secrets." not in w["forward-env"]
    assert "secrets." not in yaml.safe_dump(agent)
    # The job output that gates publish comes from the action's conclusion.
    outputs = yaml.safe_load(CI_PERF.read_text())["jobs"]["analyze"]["outputs"]
    assert outputs["analysis_conclusion"] == "${{ steps.analysis.outputs.conclusion }}"
    assert "anthropics/claude-code-action" not in yaml.safe_dump(yaml.safe_load(CI_PERF.read_text())["jobs"]["analyze"])


def test_analyze_has_no_runner_side_secret_scan_and_publishes_unconditionally():
    names = [s.get("name") for s in analyze_steps()]
    # The removed control: no post-agent secret scan (there is no reusable
    # secret to screen for, and it would run where the agent could shim it).
    assert not any("Refuse to publish" in (n or "") for n in names)
    after = analyze_steps()[[s.get("id") for s in analyze_steps()].index("analysis") + 1:]
    assert [s["name"] for s in after] == ["Stop the model broker", "Show report", "Retain CI evidence outside Git"]
    for s in after:
        assert set(re.findall(r"secrets\.([A-Z_]+)", yaml.safe_dump(s))) == set(), s["name"]
    assert analyze_step("Show report")["if"] == "always()"
    assert analyze_step("Retain CI evidence outside Git")["if"] == "always()"


def test_analyze_holds_read_permissions_and_no_marvin_identity():
    job = yaml.safe_load(CI_PERF.read_text())["jobs"]["analyze"]
    assert set(job["permissions"].values()) == {"read"}
    text = yaml.safe_dump(job)
    assert "MARVIN" not in text and "create-github-app-token" not in text and "steps.mint" not in text


def test_no_secret_input_or_step_output_expression_inside_an_analyze_run_script():
    for s in analyze_steps():
        hit = re.search(r"\$\{\{\s*(inputs|steps|needs|secrets|github\.event)\b", s.get("run") or "")
        assert hit is None, f"{s.get('name')}: {hit.group(0)} inside run:"
