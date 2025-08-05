# actions
Global GitHub actions

## Workflows

### Inspect AI Scheduled Tests

A GitHub Actions workflow that runs [inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai) slow tests on a regular schedule.

- **File**: `.github/workflows/inspect-ai-scheduled-tests.yml`
- **Documentation**: [INSPECT_AI_TESTS.md](INSPECT_AI_TESTS.md)
- **Purpose**: Execute slow tests (`--runslow`) that are excluded from regular CI

**Features:**
- Daily scheduled execution at 2 AM UTC
- Manual trigger capability
- 60-minute timeout for long-running tests
- Proper error handling and reporting
