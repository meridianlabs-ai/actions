# actions
Global GitHub actions

## Workflows

### Inspect AI Scheduled Tests

A GitHub Actions workflow that runs [inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai) tests on a regular schedule.

- **File**: `.github/workflows/inspect-ai-scheduled-tests.yml`
- **Documentation**: [INSPECT_AI_TESTS.md](INSPECT_AI_TESTS.md)
- **Purpose**: Execute slow tests (`--runslow`) and API tests (`--runapi`) that are excluded from regular CI

**Features:**
- Daily scheduled execution at 2 AM UTC
- Manual trigger with configurable options
- Separate jobs for slow tests and API tests
- Proper timeout and error handling
- Support for API key configuration via GitHub secrets
