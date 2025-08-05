# Inspect AI Scheduled Tests

This repository contains a GitHub Actions workflow that runs [inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai) tests on a regular schedule.

## Workflow: `inspect-ai-scheduled-tests.yml`

### Purpose

The workflow addresses the need to run inspect_ai tests that are excluded from the regular CI flow because they are:
- Slow running tests (`--runslow`)  
- Tests requiring paid API calls (`--runapi`)

### Schedule

- **Automatic**: Runs daily at 2 AM UTC
- **Manual**: Can be triggered manually via GitHub Actions UI with options to select which tests to run

### Jobs

#### 1. Slow Tests Job
- **Trigger**: Runs automatically on schedule or when manually triggered with `run_slow_tests: true`
- **Purpose**: Executes tests marked as slow using `pytest --runslow`
- **Timeout**: 60 minutes
- **Dependencies**: Installs inspect_ai with development dependencies

#### 2. API Tests Job
- **Trigger**: Only runs when manually triggered with `run_api_tests: true`
- **Purpose**: Executes tests requiring API calls using `pytest --runapi`
- **Timeout**: 120 minutes
- **Requirements**: Requires API keys to be configured as GitHub secrets

### Required Setup

#### For API Tests
To enable API tests, configure the following secrets in your GitHub repository:

1. Go to Settings → Secrets and variables → Actions
2. Add the following secrets as needed:
   - `OPENAI_API_KEY`: OpenAI API key
   - `ANTHROPIC_API_KEY`: Anthropic API key
   - Add other API keys as required by inspect_ai

#### Environment Variables
The workflow environment section includes commented examples of common API keys. Uncomment and modify as needed based on which APIs inspect_ai uses.

### Manual Execution

To run the workflow manually:

1. Go to the Actions tab in your GitHub repository
2. Select "Inspect AI Scheduled Tests"
3. Click "Run workflow"
4. Choose options:
   - **Run slow tests**: Execute slow tests (default: true)
   - **Run API tests**: Execute API tests requiring credentials (default: false)

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
- **Timeout Protection**: Prevents jobs from running indefinitely
- **Conditional Execution**: Jobs only run when appropriate conditions are met

### Customization

To modify the workflow:

1. **Schedule**: Edit the cron expression in the `schedule` section
2. **Python Version**: Change `python-version` in the setup steps
3. **Timeouts**: Adjust `timeout-minutes` values as needed
4. **API Keys**: Modify the `env` section in the API tests job
5. **Reporting**: Implement custom notification logic in the "Report test results" steps