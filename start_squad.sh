#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-workspace}"
SESSION="${SESSION:-ai_squad}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux not found in PATH" >&2
  exit 1
fi

python3 init_bus.py --root "$ROOT_DIR" >/dev/null

# If session exists, attach instead of failing.
if tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux attach -t "$SESSION"
  exit 0
fi

tmux new-session -d -s "$SESSION" -n squad

tmux send-keys -t "$SESSION":squad.0 "python3 -u master.py --root \"$ROOT_DIR\" --tmux-session \"$SESSION\"" C-m

tmux select-pane -t "$SESSION":squad.0
tmux attach -t "$SESSION"
