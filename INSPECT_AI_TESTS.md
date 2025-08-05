# Inspect AI Scheduled Tests

This repository contains a GitHub Actions workflow that runs [inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai) slow tests on a regular schedule.

## Workflow: `inspect-ai-scheduled-tests.yml`

### Purpose

The workflow addresses the need to run inspect_ai tests that are excluded from the regular CI flow because they are slow running tests (`--runslow`).

### Schedule

- **Automatic**: Runs daily at 2 AM UTC
- **Manual**: Can be triggered manually via GitHub Actions UI

### Jobs

#### Slow Tests Job
- **Trigger**: Runs automatically on schedule or when manually triggered with `run_slow_tests: true`
- **Purpose**: Executes tests marked as slow using `pytest --runslow`
- **Timeout**: 60 minutes
- **Dependencies**: Installs inspect_ai with development dependencies

### Required Setup

No additional setup is required for slow tests. The workflow will automatically install inspect_ai with development dependencies and run the slow tests.

### Manual Execution

To run the workflow manually:

1. Go to the Actions tab in your GitHub repository
2. Select "Inspect AI Scheduled Tests"
3. Click "Run workflow"
4. Choose option:
   - **Run slow tests**: Execute slow tests (default: true)

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