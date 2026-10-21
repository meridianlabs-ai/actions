"""Tests for docs-link-check.yml.

The check job runs lychee over pages fetched from the live docs sites, so it
holds only the read-only job token: no secret, no persisted checkout
credential and no Actions cache. The Slack token is in the report job, which
reads nothing from check but its result and never pings the channel. The
sitemap script is lifted from the YAML and run under bash with a stand-in
for curl on PATH.

Run with `python3 -m pytest` from the repo root (needs pytest and PyYAML).
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "docs-link-check.yml"
LYCHEE_CONFIG = ROOT / ".github" / "lychee.toml"
SITEMAP_STEP = "List pages from the sitemaps"


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def step(job: str, name: str) -> dict:
    return next(s for s in workflow()["jobs"][job]["steps"] if s.get("name") == name)


def sites() -> list[str]:
    script = step("check", SITEMAP_STEP)["run"]
    block = re.search(r"sites=\((.*?)\)", script, re.S)
    assert block, "sites=( ... ) not found"
    return block.group(1).split()


def sitemap(*locs: str) -> str:
    urls = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


def run_sitemap_step(tmp_path: Path, sitemaps: dict[str, str], truncated: str | None = None) -> subprocess.CompletedProcess:
    """Run the step with a curl stand-in that serves `sitemaps` (site -> body) and 404s anything else.

    For the site named by `truncated`, curl prints its sitemap and then fails, as a transfer cut off midway does.
    """
    served = tmp_path / "served"
    served.mkdir()
    for i, (site, body) in enumerate(sitemaps.items()):
        (served / f"{i}.url").write_text(f"{site}/sitemap.xml")
        (served / f"{i}.xml").write_text(body)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'url="${!#}"\n'
        f'for f in "{served}"/*.url; do\n'
        '  if [ "$(cat "$f")" = "$url" ]; then cat "${f%.url}.xml"; [ "$url" = "$TRUNCATED/sitemap.xml" ] && exit 18; exit 0; fi\n'
        "done\n"
        'echo "curl: (22) The requested URL returned error: 404" >&2\n'
        "exit 22\n"
    )
    curl.chmod(0o755)
    # GitHub runs `run:` scripts with `bash --noprofile --norc -eo pipefail`.
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step("check", SITEMAP_STEP)["run"]],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "TRUNCATED": truncated or ""},
        text=True,
        capture_output=True,
        check=False,
    )


def all_sitemaps(**overrides: str) -> dict[str, str]:
    maps = {site: sitemap(f"{site}/index.html") for site in sites()}
    maps.update(overrides)
    return maps


# --- The check job holds nothing but the read-only job token -----------------------


def test_the_job_token_is_declared_read_only_everywhere():
    wf = workflow()
    assert wf["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in wf["jobs"].values())


def test_no_checkout_persists_the_job_token():
    checkouts = [s for s in workflow()["jobs"]["check"]["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts
    assert all(s["with"].get("persist-credentials") is False for s in checkouts)


def test_no_job_uses_an_actions_cache_and_the_token_cannot_save_one():
    for job in workflow()["jobs"].values():
        for s in job["steps"]:
            assert "cache" not in str(s.get("uses", "")).lower(), s
    keys = [line for line in WORKFLOW.read_text().splitlines() if re.match(r"\s*cache-mode\s*:", line)]
    assert keys == ["cache-mode: read"], keys


def test_the_check_job_references_no_secret_and_no_other_jobs_output():
    text = yaml.safe_dump(workflow()["jobs"]["check"])
    assert "secrets." not in text
    assert "needs." not in text


def test_the_report_reads_only_the_check_jobs_result():
    report = yaml.safe_dump(workflow()["jobs"]["report"])
    assert set(re.findall(r"needs\.check\.(\w+)", report)) == {"result"}


def test_no_expression_reaches_a_run_script():
    for name, job in workflow()["jobs"].items():
        for s in job["steps"]:
            assert "${{" not in (s.get("run") or ""), (name, s.get("name"))


def test_the_report_never_pings_the_channel():
    payload = step("report", "Report results to Slack")["with"]["payload"]
    assert not re.search(r"<!(channel|here|everyone)>", payload)


def test_a_push_run_checks_links_but_posts_nothing():
    wf = workflow()
    assert "github.event_name != 'push'" in wf["jobs"]["report"]["if"]
    assert "if" not in wf["jobs"]["check"]


# --- The sitemap script ---------------------------------------------------------------


def test_every_page_of_every_site_is_listed(tmp_path):
    maps = {site: sitemap(f"{site}/index.html", f"{site}/reference/api.html") for site in sites()}
    r = run_sitemap_step(tmp_path, maps)
    assert r.returncode == 0, r.stderr
    expected = [f"{site}/{page}" for site in sites() for page in ("index.html", "reference/api.html")]
    assert (tmp_path / "pages.txt").read_text().splitlines() == expected


def test_pages_outside_the_site_and_changelogs_are_dropped(tmp_path):
    site = sites()[0]
    maps = all_sitemaps(**{site: sitemap(f"{site}/index.html", f"{site}/CHANGELOG.html", "https://example.com/elsewhere.html", f"{site}x/lookalike.html")})
    r = run_sitemap_step(tmp_path, maps)
    assert r.returncode == 0, r.stderr
    pages = (tmp_path / "pages.txt").read_text().splitlines()
    assert pages[0] == f"{site}/index.html"
    assert not any("CHANGELOG" in p or "example.com" in p or "lookalike" in p for p in pages)


def test_a_sitemap_with_no_pages_fails_the_job(tmp_path):
    site = sites()[2]
    r = run_sitemap_step(tmp_path, all_sitemaps(**{site: sitemap(f"{site}/CHANGELOG.html")}))
    assert r.returncode == 1
    assert f"::error::No pages found in {site}/sitemap.xml" in r.stdout


def test_a_missing_sitemap_fails_the_job(tmp_path):
    maps = all_sitemaps()
    del maps[sites()[1]]
    r = run_sitemap_step(tmp_path, maps)
    assert r.returncode == 1
    assert f"::error::Could not fetch {sites()[1]}/sitemap.xml" in r.stdout


def test_a_sitemap_cut_off_midway_fails_the_job_instead_of_dropping_pages(tmp_path):
    site = sites()[3]
    r = run_sitemap_step(tmp_path, all_sitemaps(), truncated=site)
    assert r.returncode == 1
    assert f"::error::Could not fetch {site}/sitemap.xml" in r.stdout


# --- The lychee config ------------------------------------------------------------------


def test_lychee_reads_the_config_this_repo_checks_in_and_the_pages_the_script_lists():
    args = step("check", "Check links")["with"]["args"].split()
    assert args[args.index("--config") + 1] == ".github/lychee.toml"
    assert LYCHEE_CONFIG.is_file()
    assert args[args.index("--files-from") + 1] == "pages.txt"
    assert ": > pages.txt" in step("check", SITEMAP_STEP)["run"]


def test_a_page_that_cannot_be_fetched_does_not_end_the_run():
    assert tomllib.loads(LYCHEE_CONFIG.read_text())["skip_missing"] is True


@pytest.mark.parametrize(
    "url, excluded",
    [
        ("https://inspect.aisi.org.uk/reference/inspect_ai.agent.qmd#react", True),
        ("https://meridianlabs-ai.github.io/inspect_flow/index.qmd", True),
        ("https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#react", False),
        ("https://inspect.aisi.org.uk/qmd-notes.html", False),
    ],
)
def test_only_qmd_urls_are_excluded(url, excluded):
    patterns = tomllib.loads(LYCHEE_CONFIG.read_text())["exclude"]
    assert any(re.search(p, url) for p in patterns) is excluded
