#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="${REPORELAY_SESSION:-reporelay}"
RUNNER="${SCRIPT_DIR}/run-reporelay.sh"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Session '$SESSION' already exists. Attach with: tmux attach -t $SESSION"
  exit 0
fi

tmux new-session -d -s "$SESSION" "$RUNNER"
echo "Started tmux session '$SESSION'. Attach with: tmux attach -t $SESSION"
