# RepoRelay

RepoRelay watches GitHub issues and pull requests for trigger phrases and hands the conversation to an external command (for example `codex exec -`). It crawls a root directory for local git repositories, maps each one to its GitHub `owner/repo`, and executes the command from that repository's working tree. Results are posted straight back to the originating thread.

## Highlights
- Polls the GitHub REST API; no inbound webhooks or background services required.
- Automatically discovers repositories (optionally recursive) with per-repo state tracking.
- Understands comment intent (`codexe`, `codexe new`, `codexe resume <id>`), resuming the most recent Codex run by default.
- Streams context (issue/PR body, optional parent, full comment history) to the external command via stdin.
- Posts the command's stdout to GitHub (stdout is trimmed and ANSI-stripped) and adds an 👀 reaction to the triggering comment on success.
- Ships with tmux and direct launch scripts for unattended operation.

## Requirements
- Python 3.9+
- `requests` (`pip install -r requirements.txt`)
- GitHub personal access token with Issues (and optionally Pull Request) read/write scope (`GITHUB_TOKEN`).

## Quick Start
```bash
# From the repo root
pip install -r requirements.txt

# Configure environment (either export directly or create an .env file)
export GITHUB_TOKEN=ghp_yourtoken
export REPORELAY_ROOT=/path/to/root-with-many-repos

# Launch in the foreground
./RepoRelay/run-reporelay.sh

# …or keep it running in tmux
./RepoRelay/tmux-reporelay.sh
# later
tmux attach -t reporelay
```

## Configuration
RepoRelay first loads environment variables from a `.env` file in this directory (if present), then reads `REPORELAY_*` overrides:
- `REPORELAY_ROOT` (`parent of this checkout`): Top-level directory that contains the repos to watch.
- `REPORELAY_RECURSIVE` (`0`): Set to `1` to walk subdirectories recursively.
- `REPORELAY_REQUIRE_MARKER` (`0`): Set to `1` to only include repos containing `.reporelay-enabled` .
- `REPORELAY_EXCLUDE_DIRS` (empty): Comma-separated directory names to skip during discovery.
- `REPORELAY_REGEX` (`codexe`): Case-insensitive regex used to detect triggers.
- `REPORELAY_MATCH_TARGET` (`comments`): Set to `issue_or_comments` to also match issue titles/bodies.
- `REPORELAY_IGNORE_SELF` (`0`): Leave at `0` to process comments written by the authenticated account; set to `1` to skip self-authored comments and avoid loops.
- `REPORELAY_POLL_SECONDS` (`20`): Poll interval for the GitHub API loop.
- `REPORELAY_PER_REPO_PAUSE` (`0.3`): Sleep inserted between repos to spread API calls.
- `REPORELAY_STATE` (`$REPORELAY_ROOT/.reporelay_state.json`): JSON file storing per-repo watermarks and history .
- `REPORELAY_LOCKFILE` (`$REPORELAY_ROOT/.reporelay.lock`): Prevents multiple watcher instances in the same root.
- `CODEX_CMD` (`codex`): External command to execute.
- `CODEX_ARGS` (`exec -`): Arguments passed to `CODEX_CMD` for new runs.
- `CODEX_RESUME_ARGS` (`resume`): Arguments used when resuming a Codex run; combined with the run id.
- `REPORELAY_DEFAULT_RESUME` (`1`): When `1`, a plain `codexe` resumes the last run if present; set to `0` to always start new unless `resume` appears.
- `REPORELAY_RESUME_SEND_CONTEXT` (`0`): When `1`, still sends the assembled context on resume (stdin).
- `REPORELAY_FORWARD_GITHUB_TOKEN` (`0`): When `1`, forwards `GITHUB_TOKEN` into the subprocess environment; otherwise it is scrubbed.

## State, Logging, and Shutdown
- State is stored in `.reporelay_state.json`.
- Logs stream to stdout/stderr; attach to the tmux session to observe them live.
- Graceful exit: `Ctrl-C` inside the tmux pane or `tmux kill-session -t reporelay`.
- A `.reporelay.lock` file guards against double-starts; remove it only if the process truly exited.

## Testing
Run `python -m unittest discover -s tests` from the repo root. A minimal `mock_codex.py` fixture lives in `RepoRelay/` for dry runs.

## Documentation
- `RepoRelay/TEST_PLAN.md` – manual / agent validation scenarios.
