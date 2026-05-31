#!/usr/bin/env bash
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="/usr/local/bin/matted"

if [[ ! -f "$ENGINE_DIR/init_bus.py" || ! -f "$ENGINE_DIR/master.py" ]]; then
  echo "Erro: init_bus.py ou master.py nao encontrados em: $ENGINE_DIR" >&2
  exit 1
fi

if [[ ! -w "$(dirname "$LAUNCHER")" ]]; then
  echo "Permissao necessaria para escrever em $LAUNCHER" >&2
  echo "Execute: sudo $0" >&2
  exit 1
fi

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
set -euo pipefail

ENGINE_DIR="$ENGINE_DIR"
PROJECT_ROOT="\$(pwd)"
SQUAD_FILE="\$PROJECT_ROOT/.matted_squad"

if [[ -f "\$SQUAD_FILE" ]]; then
  SQUAD_NAME="\$(head -n 1 "\$SQUAD_FILE" | tr -d '\r\n')"
else
  echo -n "Digite o nome do Squad/Projeto para esta sessao: "
  read -r SQUAD_NAME

  if [[ -z "\$SQUAD_NAME" ]]; then
    SQUAD_NAME="matted_squad"
  fi

  printf '%s\n' "\$SQUAD_NAME" > "\$SQUAD_FILE"
fi

SESSION="\$(printf '%s' "\$SQUAD_NAME" | tr -c '[:alnum:]_-' '_' | sed 's/^_*//; s/_*\$//')"
if [[ -z "\$SESSION" ]]; then
  SESSION="matted_squad"
  printf '%s\n' "\$SESSION" > "\$SQUAD_FILE"
fi

ACTION="start"
case "\${1:-}" in
  --restart|restart)
    ACTION="restart"
    shift
    ;;
  --kill|kill)
    ACTION="kill"
    shift
    ;;
  --status|status)
    ACTION="status"
    shift
    ;;
  --help|-h|help)
    cat <<USAGE
Uso:
  matted             Anexa na sessao existente ou inicia uma nova
  matted --restart  Mata a sessao atual e inicia outra lendo o .env atual
  matted --kill     Mata a sessao atual e sai
  matted --status   Mostra sessao e provider configurado
USAGE
    exit 0
    ;;
esac

read_env_value() {
  local key="\$1"
  local file="\$2"
  [[ -f "\$file" ]] || return 1
  awk -F= -v key="\$key" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    {
      k=\$1
      gsub(/^[[:space:]]+|[[:space:]]+\$/, "", k)
      if (k == key) {
        v=substr(\$0, index(\$0, "=") + 1)
        gsub(/^[[:space:]]+|[[:space:]]+\$/, "", v)
        gsub(/^["'"'"']|["'"'"']\$/, "", v)
        print v
      }
    }
  ' "\$file" | tail -n 1
}

configured_provider="\$(read_env_value ACTIVE_LLM_PROVIDER "\$ENGINE_DIR/.env" || true)"
project_provider="\$(read_env_value ACTIVE_LLM_PROVIDER "\$PROJECT_ROOT/.env" || true)"
if [[ -n "\$project_provider" ]]; then
  configured_provider="\$project_provider"
fi
configured_provider="\${configured_provider:-codex}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found in PATH" >&2
  exit 1
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux not found in PATH" >&2
  exit 1
fi

if [[ "\$ACTION" == "status" ]]; then
  if tmux has-session -t "\$SESSION" 2>/dev/null; then
    echo "Sessao: \$SESSION (rodando)"
  else
    echo "Sessao: \$SESSION (parada)"
  fi
  echo "Provider configurado: \$configured_provider"
  echo "Projeto: \$PROJECT_ROOT"
  exit 0
fi

if [[ "\$ACTION" == "kill" ]]; then
  if tmux has-session -t "\$SESSION" 2>/dev/null; then
    tmux kill-session -t "\$SESSION"
    echo "Sessao encerrada: \$SESSION"
  else
    echo "Sessao ja estava parada: \$SESSION"
  fi
  exit 0
fi

if [[ "\$ACTION" == "restart" ]] && tmux has-session -t "\$SESSION" 2>/dev/null; then
  tmux kill-session -t "\$SESSION"
fi

if tmux has-session -t "\$SESSION" 2>/dev/null; then
  echo "Anexando na sessao existente: \$SESSION (provider ja carregado pela sessao)"
  echo "Para reler .env e trocar LLM, use: matted --restart"
  tmux attach-session -t "\$SESSION"
  exit 0
fi

python3 "\$ENGINE_DIR/init_bus.py" --root "\$PROJECT_ROOT" >/dev/null

echo "Iniciando sessao: \$SESSION"
echo "Provider configurado: \$configured_provider"
tmux new-session -d -s "\$SESSION" -n squad -c "\$ENGINE_DIR"
printf -v MASTER_CMD '%q ' python3 -u "\$ENGINE_DIR/master.py" --root "\$PROJECT_ROOT" --tmux-session "\$SESSION"
tmux send-keys -t "\$SESSION":squad.0 "\$MASTER_CMD" C-m
tmux select-pane -t "\$SESSION":squad.0
tmux attach -t "\$SESSION"
EOF

chmod +x "$LAUNCHER"

echo "CLI instalado com sucesso:"
echo "  $LAUNCHER -> $ENGINE_DIR"
echo
echo "Uso:"
echo "  cd /caminho/do/projeto"
echo "  matted"
