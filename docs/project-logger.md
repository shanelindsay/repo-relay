# Project Logger (GitHub Projects v2)

This repository includes a small script and workflow to log agent runs as items in a GitHub Projects v2 board using the built‑in `GITHUB_TOKEN` (with project scope enabled).

## What it does

- Creates a draft item at task start and sets fields
- Links an Issue or PR URL to the same item (kept as text fields)
- Updates status and end date when the run finishes or fails
- Optionally records tokens and duration (minutes)

No cost tracking is included.

## Prerequisites

- A Projects v2 board at user or org scope.
- The following fields exist in that Project (case‑sensitive):
  - Status (single‑select): Planned, In progress, Blocked, PR open, Done, Failed
  - Start date (date)
  - End date (date)
  - Duration minutes (number)
  - Repository (text) — or use the built‑in Repository field if you prefer
  - Branch (text)
  - Issue URL (text)
  - PR URL (text)
  - Tokens total (number)
  - Model (text)
  - Run ID (text)

> Note: Create these once in the Project UI. The script looks them up by name.

## Configure

Set repository Variables for the workflow (Settings → Secrets and variables → Actions → Variables):

- `PROJECT_OWNER` — org or username that owns the Project
- `PROJECT_NUMBER` — the Project number (integer)
- Optional: `PROJECT_DEFAULT_MODEL` — default model name (defaults to `gpt-5-codex`)

No secret is required beyond the default `GITHUB_TOKEN`. The workflow exports it as `GH_TOKEN` for the `gh` CLI.

## Using the workflow (repository_dispatch)

Send `repository_dispatch` events with one of these actions:

- `task_started`
- `task_link_issue`
- `pr_opened`
- `task_done`
- `task_failed`

Example (using gh):

```bash
gh api repos/:owner/:repo/dispatches \
  -f event_type=task_started \
  -f client_payload='{"title":"Task: Improve logging","body":"Run kicked off","run_id":"RUN-42","model":"gpt-5-codex","branch":"feature/logging","repo":"owner/repo"}'
```

Subsequent events should include `item_id` returned from the `task_started` run (you can capture the workflow output from logs or manage this mapping in your runner). For example:

```bash
gh api repos/:owner/:repo/dispatches \
  -f event_type=task_done \
  -f client_payload='{"item_id":"PVTI_xxx","tokens_total":12345,"start_ts":1697500000,"end_ts":"now","model":"gpt-5-codex","run_id":"RUN-42"}'
```

## Using the script directly

You can bypass the workflow and call the script from your runner:

```bash
export PROJECT_OWNER=your-org
export PROJECT_NUMBER=1
export GH_TOKEN=$GITHUB_TOKEN   # must include project scope

# Start (defaults to gpt-5-codex if --model omitted)
id=$(scripts/project-logger.sh start --title "Task: Foo" --body "short" \
      --run-id RUN-123 --model gpt-5-codex --branch feat/foo --repo owner/repo)

# Link Issue
scripts/project-logger.sh link --item-id "$id" --issue-url https://github.com/owner/repo/issues/123

# PR opened
scripts/project-logger.sh pr --item-id "$id" --pr-url https://github.com/owner/repo/pull/456

# Finish
scripts/project-logger.sh finish --item-id "$id" --status Done \
  --tokens-total 6789 --start-ts 1697500000 --end-ts now --model gpt-5-codex --run-id RUN-123
```

## Notes

- The script updates one field per `gh project item-edit` call (CLI constraint).
- Field names must match exactly; warnings are logged if a field is missing.
- Status options are looked up by label; create them once in the UI.
- Duration is computed only if start/end timestamps are provided to `finish`.
