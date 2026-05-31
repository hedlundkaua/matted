# Dynamic Swarm Local

Este projeto executa um swarm local com `master.py` + agentes em `tmux` e permite trocar o provedor de LLM via `.env` sem alterar código.

## 1) Pré-requisitos

- Python 3.8+
- `tmux`
- Dependências Python instaladas no ambiente (`.venv` recomendado)

## 2) Configurar LLM no `.env`

Arquivo: `.env`

```env
# Escolha do provedor
# valores: codex | openrouter | openclaude
ACTIVE_LLM_PROVIDER=codex

# OpenRouter (necessário quando ACTIVE_LLM_PROVIDER=openrouter/openclaude)
OPENROUTER_API_KEY=coloque_sua_chave_aqui
OPENROUTER_MODEL=google/gemma-4-31b-it

# Codex (opcional)
MATTED_CODEX_SANDBOX=workspace-write
MATTED_CODEX_APPROVAL=on-request
MATTED_CODEX_VERBOSE=0

# OpenRouter local tools (opcional)
MATTED_OPENROUTER_LOCAL_TOOLS=1
MATTED_OPENROUTER_COMMAND_TIMEOUT=120
MATTED_OPENROUTER_ALLOW_DESTRUCTIVE=0
MATTED_OPENROUTER_APPROVALS=1
MATTED_OPENROUTER_STREAM=1
```

### Provedores suportados

- `codex`: usa CLI Codex local.
- `openrouter`: usa API OpenRouter com ferramentas locais do Matted para workers.
- `openclaude`: alias para `openrouter`.

### OpenRouter com ferramentas locais

Quando `ACTIVE_LLM_PROVIDER=openrouter`, os workers usam um tool-loop local por padrão (`MATTED_OPENROUTER_LOCAL_TOOLS=1`). Isso permite:

- ler e listar arquivos do projeto
- criar e sobrescrever arquivos
- editar arquivos por substituição exata
- executar comandos diretos na raiz do projeto, como testes e lint

Comandos destrutivos óbvios ficam bloqueados por padrão. Para permitir, defina `MATTED_OPENROUTER_ALLOW_DESTRUCTIVE=1`.

Antes de editar arquivos ou executar comandos destrutivos, o worker pede aprovação interativa no pane do agente:

```text
 Edit file
 app.py

 Do you want to make this edit to app.py?
 > 1. Yes
   2. Yes, allow all edits during this session
   3. No
```

Para desativar aprovações interativas, defina `MATTED_OPENROUTER_APPROVALS=0`.

Streaming de resposta do OpenRouter fica ligado por padrão (`MATTED_OPENROUTER_STREAM=1`). Para voltar ao modo antigo, sem mostrar tokens em tempo real, defina `MATTED_OPENROUTER_STREAM=0`.

## 3) Como trocar de LLM

1. Edite `ACTIVE_LLM_PROVIDER` no `.env`.
2. Se escolher `openrouter`/`openclaude`, confirme `OPENROUTER_API_KEY` e `OPENROUTER_MODEL`.
3. Reinicie a sessão do swarm para aplicar:
   - encerre a sessão `tmux` atual
   - execute novamente `matted` ou `./matted_full.sh`

## 4) Como iniciar

### Opção A: comando global

```bash
matted
```

Observação: em projeto novo, o launcher cria `squad.db` automaticamente se não existir.

### Opção B: launcher local

```bash
./matted_full.sh
```

Também faz bootstrap automático do `squad.db` quando necessário.

## 5) Verificação rápida

Após subir:

- No painel do master, confirme que tarefas estão sendo roteadas normalmente.
- Se estiver usando OpenRouter e a chave estiver inválida, erros de autenticação aparecerão no log do agente/master.
- Se estiver usando Codex sem o binário instalado, o provedor retornará erro de execução do CLI.

## 6) Troubleshooting

- `ACTIVE_LLM_PROVIDER invalido`:
  - use apenas `codex`, `openrouter` ou `openclaude`.
- OpenRouter falhando:
  - valide `OPENROUTER_API_KEY`.
  - teste outro `OPENROUTER_MODEL`.
- Sessão `tmux` não abre:
  - confirme instalação do `tmux`.
  - feche sessões antigas e reinicie o launcher.

### Pausa global de execução

No prompt do `master`, você pode controlar o processamento:

- `pausar` (ou `pause`): pausa consumo de novas tarefas pelos agentes.
- `retomar` (ou `resume`): retoma processamento normal.

Isso atualiza `projeto.status_global` e os agentes respeitam o estado `Pausado`.

### Frontend com falha `npm ci` (`EAI_AGAIN`)

Esse erro é de rede/DNS do ambiente, não do agente. Fluxo recomendado:

1. Validar host:
   - `nslookup registry.npmjs.org`
   - `curl -I https://registry.npmjs.org`
2. Configurar registry/mirror se necessário:
   - `npm config set registry https://registry.npmjs.org/`
   - ou registry corporativo interno.
3. Reexecutar:
   - `npm ci`
   - `npm run build`
   - `npm run test:storage` (ou script oficial equivalente)

## 7) Segurança

- Não versionar `.env` com chave real.
- Se uma chave foi exposta em log/captura de tela, faça rotação imediata.
