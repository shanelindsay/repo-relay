# Automation (YAML)

Define lightweight automations as YAML files stored in a repository folder. Each file describes when to trigger and what to run. The orchestrator scans the folder, validates each YAML file, and executes matching automations on events or on a schedule.

- Default folder: `.automations/` (configurable via `AUTOMATIONS_DIR`)
- File format: YAML. Configs are versioned with `version: 1` (see `schema/automation.schema.json`).
- Purpose: Make it easy to declaratively codify small, repeatable "run the agent with these instructions" tasks without writing code.

## Concepts
- Trigger: When to run. Supported types: `schedule`, `github_issue`, `pull_request`.
- Run: What to do when the trigger fires. Typically calls the agent/command with model + instructions in the target repo context.
- Parameters: Model selection, repo coordinates, free-text instructions, and optional environment.

## Trigger Types
- `schedule`: time-based triggers interpreted in UTC (GMT). Supports a simple frequency form or cron.
  - frequency: `daily` | `weekly` | `monthly`
  - at: `HH:MM` (24-hour, default `00:00`)
  - day_of_week: `mon..sun` (weekly only)
  - day_of_month: `1..31` (monthly only)
  - or `cron`: five-field `minute hour day month dayOfWeek` string
- `github_issue` - fires on GitHub issue lifecycle events
  - types: `opened` | `closed` | `reopened` | `labeled` | `unlabeled`
- `pull_request` - fires on PR lifecycle events
  - types: `opened` | `closed` | `reopened` | `synchronize` (new commits) | `labeled` | `unlabeled`
  - optional filters: `branches`, `paths`

Note: Each automation supports exactly one trigger (`on` accepts exactly one key).

## Run Parameters
- `model` - how the agent runs
  - `name` (default `gpt-5-5`)
  - `variant` (default `gpt-5-codecs`)
  - `reasoning` (default `medium`)
- `repo` - GitHub repo in `owner/name` form (defaults to the current repo if omitted)
- `instructions` - free text block with the task to perform
- `command` - optional override of the runner (default: `codex exec -`)
- `env` - key/value environment exported to the run (supports simple `${VAR}` interpolation)

Notes:
- Model names here reflect project-level defaults for this feature, not external service guarantees. Adjust as your runner requires.
- Defaults are applied by the orchestrator when `model` is omitted: `name=gpt-5-5`, `variant=gpt-5-codecs`, `reasoning=medium`. When `at` is omitted for schedules, the orchestrator uses `00:00`.

## Folder Layout
```
.automations/
  nightly-deps.yaml
  issue-triage.yaml
  pr-smoke.yaml
```

## Example: Nightly task (daily at 02:00 UTC)
```yaml
version: 1
name: Nightly Dependency Check
description: Run agent to review deps and open PRs if needed
on:
  schedule:
    frequency: daily
    at: '02:00'   # UTC
run:
  model:
    name: gpt-5-5
    variant: gpt-5-codecs
    reasoning: medium
  repo: owner/repo
  instructions: |
    Scan the repository for outdated third-party dependencies.
    Generate a summary and prepare changes as a PR when safe.
```

## Example: Issue-opened triage
```yaml
version: 1
name: Issue Triage
description: Label and respond to new issues
on:
  github_issue:
    types: [opened]
run:
  repo: owner/repo
  instructions: |
    Read the new issue and suggest labels and priority.
    Ask clarifying questions if details are missing.
```

## Example: PR synchronize (new commits)
```yaml
version: 1
name: PR Smoke Tests
description: Re-run analysis when PR updates
on:
  pull_request:
    types: [synchronize]
    branches: [main]
run:
  repo: owner/repo
  instructions: |
    Re-evaluate the latest changes and comment with any failures
    or risky diffs. Keep feedback concise and actionable.
```

## Validation
- JSON Schema lives at `docs/automation/schema/automation.schema.json`.
- A ready-to-use GitHub Actions workflow is provided in `docs/automation/ci/validate-automations.yml`. Copy it to `.github/workflows/` to enable CI validation of `.automations/*.yml` and `.automations/*.yaml`.

## Orchestrator Responsibilities
- Discover: read files from `.automations/` (supports `*.yml` and `*.yaml`).
- Validate: parse and reject invalid configs with clear errors.
- Subscribe: map trigger types to event sources (internal scheduler, GitHub events, or polling).
- Execute: run the command in the target repo working tree with `instructions` piped to stdin (or as an argument, depending on your runner).
- Idempotency: avoid duplicate executions (use content hashes + timestamps for schedules; delivery ids for GitHub events).
- Observability: log decisions; surface successes and failures to GitHub comments where applicable.

Reporting defaults:
- Issue events: post a new comment on the issue.
- PR events: post a PR comment (or a review comment when appropriate).
- Schedule events: update or append to a repository issue titled "Automation: Scheduled Runs Log" (configurable), or log-only when disabled.

## Security & Safeguards
- Scrub secrets from logs and error messages. Redact any key matching `/[A-Z0-9_]*(TOKEN|SECRET|KEY)/` by default. Do not forward `GITHUB_TOKEN` unless explicitly enabled.
- Allow per-automation `env` to set minimal environment needed; prefer repo/runner defaults for everything else.
- Enforce sane execution timeouts at the orchestrator.

## Roadmap
- Add `conditions` for boolean filters (branch, path, labels).
- Support matrix runs (e.g., multiple repos or paths).
- Built-in templates for common workflows.

## Filter Semantics
- For `pull_request` filters, all specified filter types must match (logical AND across `branches`, `paths`). Within each list, any value may match (logical OR within the list).

## Monthly Day Handling
- For `monthly` schedules, if `day_of_month` exceeds the number of days in a month, the run is skipped for that month (cron-like semantics).

