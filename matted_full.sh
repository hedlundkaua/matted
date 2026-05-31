#!/usr/bin/env bash
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
SESSION="${SESSION:-$(basename "$PROJECT_ROOT" | tr -c '[:alnum:]_-' '_' | sed 's/^_*//; s/_*$//')}"
SESSION="${SESSION:-matted_squad}"

# 1. Load .env for launcher context (master/agents also load from factory).
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$PROJECT_ROOT/.env"
  set +a
fi

# 1.1 Bootstrap DB for new projects (or when DB was deleted).
if [[ ! -f "$PROJECT_ROOT/squad.db" ]]; then
  python3 "$ENGINE_DIR/init_bus.py" --root "$PROJECT_ROOT" >/dev/null
fi

# 2. Ensure tmux server/session.
if tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux kill-session -t "$SESSION"
fi
tmux new-session -d -s "$SESSION" -n "master" -c "$ENGINE_DIR"
tmux set-option -t "$SESSION" -g pane-border-status bottom
tmux set-option -t "$SESSION" -g pane-border-format '#[align=right] #{?@agent_label,#{@agent_label},#[bold]#T#[default]} '
tmux set-option -pt "$SESSION":master.0 @agent_label '#[fg=colour196,bold]master#[default]'
tmux set-option -pt "$SESSION":master.0 pane-border-style 'fg=colour196'
tmux set-option -pt "$SESSION":master.0 pane-active-border-style 'fg=colour196,bold'
tmux set-option -pt "$SESSION":master.0 window-style 'default'
tmux set-option -pt "$SESSION":master.0 window-active-style 'default'

# 3. Launch master pointed to current project.
printf -v MASTER_CMD '%q ' python3 -u "$ENGINE_DIR/master.py" --root "$PROJECT_ROOT" --tmux-session "$SESSION"
tmux send-keys -t "$SESSION":master.0 "$MASTER_CMD" C-m

# 4. Attach.
tmux select-pane -t "$SESSION":master.0
tmux attach-session -t "$SESSION"
