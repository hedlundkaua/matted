# Plano inicial do backend

## Contexto

O projeto esta em `Planejamento`, sem tecnologias definidas e com a primeira
tarefa direcionada ao agente `backend`.

## Stack recomendada

- Python 3.12+
- FastAPI para API HTTP
- Pydantic para validacao e contratos
- SQLAlchemy 2.x para persistencia
- SQLite no desenvolvimento local
- PostgreSQL em producao, se houver necessidade de concorrencia e operacao real
- pytest para testes automatizados

## Escopo inicial

Criar uma API backend pequena, modular e testavel com:

- endpoint de saude: `GET /health`
- estrutura de configuracao por variaveis de ambiente
- camada de banco isolada
- migracoes preparadas para evolucao do schema
- testes de contrato para endpoints publicos

## Modelo inicial sugerido

Como ainda nao existe dominio de produto definido, o backend deve comecar com
uma fundacao tecnica e evitar entidades de negocio prematuras.

Entidades tecnicas iniciais:

- `ProjectStatus`: representa o estado global do projeto
- `Task`: representa unidades de trabalho executadas por agentes
- `HistoryEvent`: registra eventos relevantes do fluxo

## Proximos passos

1. Confirmar se o dominio do produto sera o proprio orquestrador multiagente ou
   uma aplicacao nova.
2. Criar esqueleto FastAPI em `backend/`.
3. Definir contratos minimos de API em testes antes de expandir regras.
4. Separar configuracao, persistencia, rotas e schemas desde o primeiro commit.
