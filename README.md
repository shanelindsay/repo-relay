# RepoRelay

RepoRelay is a lightweight agent-side service that watches your GitHub repositories for trigger comments and forwards the full thread context to an external command (most teams use `codex exec -`). The command runs with its working directory set to the repository that raised the trigger and the result is posted straight back to the conversation, complete with an 👀 reaction on the original comment.

## Why RepoRelay?
- **Zero webhooks:** simple polling loop that survives restarts and works from any server or tmux session.
- **Multi-repo aware:** automatically maps `owner/repo` to local clones under a single root directory.
- **Resume friendly:** understands `codexe`, `codexe new`, and `codexe resume <id>` so Codex runs can continue where they left off.
- **Safe defaults:** context is trimmed/ANSI-stripped before posting; `GITHUB_TOKEN` is scrubbed from the subprocess unless explicitly forwarded.

## Quick Start
```bash
pip install -r requirements.txt

# Configure environment (or add to an .env next to the repo)
export GITHUB_TOKEN=ghp_yourtoken
export REPORELAY_ROOT=/absolute/path/to/projects

# Run once
./RepoRelay/run-reporelay.sh

# Or keep it alive inside tmux
./RepoRelay/tmux-reporelay.sh
# later
tmux attach -t reporelay
```

The watcher prints logs to stdout/ stderr. Successful runs add an 👀 reaction to the triggering comment and post stdout as a new GitHub comment. Failures post stderr in a fenced block along with the exit code.

## Configuration
RepoRelay reads `REPORELAY_*` variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `REPORELAY_ROOT` | `$PWD` | Parent folder containing the git repos to scan |
| `REPORELAY_RECURSIVE` | `0` | Set to `1` to walk subdirectories recursively |
| `REPORELAY_REGEX` | `codexe` | Case-insensitive regex used to detect trigger comments |
| `REPORELAY_MATCH_TARGET` | `comments` | Use `issue_or_comments` to match titles/bodies |
| `REPORELAY_IGNORE_SELF` | `1` | Skip comments authored by the authenticated account |
| `REPORELAY_REQUIRE_MARKER` | `0` | Only watch repos with `.reporelay-enabled` (or legacy `.posis-enabled`) |
| `REPORELAY_PER_REPO_PAUSE` | `0.3` | Seconds slept between repo polls |
| `REPORELAY_POLL_SECONDS` | `20` | Base polling period |
| `REPORELAY_STATE` | `$ROOT/.reporelay_state.json` | Path to state file (falls back to legacy) |
| `REPORELAY_LOCKFILE` | `$ROOT/.reporelay.lock` | Prevents double starts (falls back to legacy) |
| `REPORELAY_DEFAULT_RESUME` | `1` | Resume last Codex run when the comment is simply `codexe …` |
| `REPORELAY_RESUME_SEND_CONTEXT` | `0` | If `1`, still pipes the issue context on resume |
| `REPORELAY_FORWARD_GITHUB_TOKEN` | `0` | Forward `GITHUB_TOKEN` into the subprocess if set to `1` |
| `CODEX_CMD` | `codex` | External command to execute |
| `CODEX_ARGS` | `exec -` | Arguments for new Codex runs |
| `CODEX_RESUME_ARGS` | `resume` | Arguments for resuming Codex runs |
| `CODEX_TIMEOUT` | `3600` | Seconds before the external command is killed |

## Project Layout
```
RepoRelay/
  README.md           # in-depth usage guide
  TEST_PLAN.md        # manual/agent validation scenarios
  watcher.py          # main polling loop
  run-reporelay.sh    # foreground launcher (loads .env if present)
  tmux-reporelay.sh   # tmux launcher (session name configurable via REPORELAY_SESSION)
  mock_codex.py       # stub command for tests / dry runs
requirements.txt
tests/test_reporelay_watcher.py
```

## Development
- Run `python -m unittest discover -s tests` before committing.
- The watcher logs under the `reporelay` logger; adjust `logging.basicConfig` in `RepoRelay/watcher.py` if you need custom formatting.
- To simulate the external command locally, point `CODEX_CMD` at `./RepoRelay/mock_codex.py` for quick smoke tests.

## Documentation
- `RepoRelay/TEST_PLAN.md` – manual / agent validation scenarios.
- `docs/automation/README.md` – YAML automations spec, schema, and examples.

## License
MIT
