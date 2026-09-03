# Inspect AI Scheduled Tests

This repository contains a GitHub Actions workflow that runs [inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai) tests on a regular schedule and on-demand.

## Workflow: `inspect-ai-scheduled-tests.yml`

### Purpose

The workflow addresses the need to run inspect_ai tests that are excluded from the regular CI flow:
- **Slow tests** (`--runslow`): Long-running tests that are too slow for regular CI
- **API tests** (`--runapi`): Tests that require external API credentials or secrets

### Schedule

- **Automatic**: Slow tests run daily at 2 AM UTC
- **Manual**: Both test types can be triggered manually via GitHub Actions UI

### Jobs

#### Slow Tests Job
- **Trigger**: Runs automatically on schedule or when manually triggered with `run_slow_tests: true`
- **Purpose**: Executes tests marked as slow using `pytest --runslow`
- **Timeout**: 60 minutes
- **Dependencies**: Installs inspect_ai with development dependencies

#### API Tests Job
- **Trigger**: Only runs when manually triggered with `run_api_tests: true`
- **Purpose**: Executes tests that require API access using `pytest --runapi`
- **Timeout**: 30 minutes
- **Dependencies**: Installs inspect_ai with development dependencies
- **Requirements**: May require repository secrets for API credentials

### Required Setup

#### Slow Tests
No additional setup is required for slow tests. The workflow will automatically install inspect_ai with development dependencies and run the slow tests.

#### API Tests  
API tests may require repository secrets to be configured if the tests need external API credentials. Check the inspect_ai repository documentation for specific requirements.

### Manual Execution

To run the workflow manually:

1. Go to the Actions tab in your GitHub repository
2. Select "Inspect AI Scheduled Tests"
3. Click "Run workflow"
4. Choose options:
   - **Run slow tests**: Execute slow tests (default: true)
   - **Run API tests**: Execute API tests (default: false, requires secrets)

### Error Reporting

When tests fail:
- GitHub Actions will show the failure in the workflow run
- Error messages are marked with `::error::` for visibility
- TODO: Future enhancement to add Slack notifications or other alerting mechanisms

### Workflow Features

- **Repository Checkout**: Automatically clones the latest inspect_ai code
- **Python Setup**: Uses Python 3.11
- **Dependency Installation**: Installs inspect_ai with development dependencies
- **Option Verification**: Checks if pytest options are available
- **Verbose Output**: Uses `-v` flag for detailed test output
- **Timeout Protection**: Prevents jobs from running indefinitely (60 minutes)
- **Conditional Execution**: Job only runs when appropriate conditions are met

### Customization

To modify the workflow:

1. **Schedule**: Edit the cron expression in the `schedule` section
2. **Python Version**: Change `python-version` in the setup steps
3. **Timeout**: Adjust `timeout-minutes` value as needed
4. **Reporting**: Implement custom notification logic in the "Report test results" step

## Workflow: `inspect-ai-windows-tests.yml`

### Purpose

inspect_ai's own CI (`build.yml`) runs ubuntu-only, so Windows-specific breakage in pure-Python paths (path separators, file URIs, sockets, subprocess) goes undetected (e.g. UKGovernmentBEIS/inspect_ai#4765). This workflow runs the **fast unit suite** — the same pytest invocation as the main repo's `test` job — on `windows-latest`, nightly.

Slow/sandbox tests are deliberately excluded: GitHub's Windows runners cannot run Linux containers.

### Schedule

- **Automatic**: Nightly at 05:30 UTC against inspect_ai `main`
- **Manual**: `workflow_dispatch` with optional `inspect_ai_ref` and `pytest_args`
- **Dev cycle**: Pushing a branch named `windows-tests**` to this repo triggers a run

### Reporting

Scheduled runs post pass/fail to Slack (same bot/channel as the scheduled slow tests, without `@channel`). Dispatch and dev-cycle runs report via the Actions UI only.