# CLAUDE.md — RADAR ANVISA

Notas de arquitetura, comandos e decisões de projeto. Atualizado a cada
milestone (ver seção 11 do briefing original, resumida em "Milestones" abaixo).

## O que é

Chatbot regulatório com base de conhecimento viva: monitora, indexa e
responde perguntas sobre normativa e notícias da ANVISA, com atualização
diária automática e citação obrigatória da fonte. Ver `README.md` para a
visão de produto e `research/FONTES.md` para o reconhecimento das fontes
externas (endpoints reais testados, encoding, estruturas de dados).

## Stack

- Backend: Python 3.12 + FastAPI, gerenciado com `uv`.
- Banco: Supabase (Postgres) com `pgvector` + `pg_trgm`.
- Embeddings: OpenAI `text-embedding-3-large` (dim 3072).
- LLM: Claude Sonnet via SDK `anthropic`.
- Scraping: `httpx` + `selectolax`; `pymupdf` para PDF.
- Scheduler: APScheduler (job diário 06:00 BRT, a partir do M5).
- Frontend: Next.js + TypeScript + Tailwind + shadcn/ui (a partir do M6).
- Deploy: backend no Railway, frontend na Vercel.

## Layout do repositório

```
app/
  config.py       # Settings (pydantic-settings), lê .env
  db.py           # pool asyncpg, criado/fechado no lifespan do FastAPI
  main.py         # app FastAPI + /health
  ingest/
    base.py       # Protocol `Fonte` + DTOs (ItemBruto, DocumentoBruto,
                   # NormaDTO, NoticiaDTO) — contrato de todo ingestor
    <fonte>.py    # um módulo por fonte real (M2: anvisalegis; M5: gov.br
                   # notícias, inlabs) — ainda não existem
supabase/
  migrations/
    0001_schema.sql   # schema completo (norma, norma_relacao, chunk,
                       # noticia, dou_materia, job_execucao, alerta_regra)
research/
  FONTES.md       # reconhecimento das fontes externas (M0) — ler antes de
                   # mexer em qualquer ingestor
  samples/        # HTML/XML/JSON brutos coletados durante o M0, para
                   # referência de estrutura real (não são fixtures de teste)
tests/
```

## Comandos

```bash
# instalar dependências (dev incluso)
uv sync --dev

# rodar a API localmente
uv run uvicorn app.main:app --reload

# qualidade (o CI roda os três)
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest -q

# aplicar o schema no Supabase (uma vez, ou a cada nova migration)
psql "$DATABASE_URL" -f supabase/migrations/0001_schema.sql
```

`research/` está fora do escopo do ruff/mypy (`extend-exclude`/`exclude` no
`pyproject.toml`) — os arquivos lá são cópias de terceiros ou HTML/XML brutos
coletados como evidência, não código do projeto.

## Decisões de projeto (por milestone)

### M0 — Reconhecimento
- User-Agent próprio do projeto em todo crawling, nunca `ClaudeBot` (está na
  lista de bots bloqueados no `robots.txt` do AnvisaLegis) — ver
  `research/FONTES.md` seção 0.
- AnvisaLegis: decodificar sempre `iso-8859-1` e depois `html.unescape()`
  antes de persistir (duas camadas de encoding confirmadas). INLABS: XML já
  vem UTF-8 com BOM (`utf-8-sig`), mais simples.
- INLABS: validar `Content-Type`/magic bytes do zip antes de aceitar um
  download — pedir uma data sem pasta ainda publicada retorna a home HTML
  com HTTP 200, não um erro.
- Endpoint de alertas de segurança do gov.br mudou de plataforma (agora
  `consultas.anvisa.gov.br`, SPA Angular) — não mapeado ainda, ver pendências
  em `research/FONTES.md`.

### M1 — Esqueleto
- `[tool.uv] package = false`: o projeto é uma aplicação, não uma lib a
  publicar — evita a necessidade de um build-backend.
- Acesso ao Postgres via `asyncpg` direto (não um ORM) — o schema já é
  pensado em SQL puro (pgvector, ivfflat, tsvector gerado), um ORM añadiria
  camada sem ganho aqui. Se algo pedir mais estrutura (migrations versionadas
  automáticas, etc.) revisitar depois.
- Schema SQL segue a seção 4 do briefing quase literal; três extensões
  pontuais, marcadas com `-- [M1]` no arquivo, para cumprir a exigência de
  idempotência total da seção 5:
  - trigger `atualizado_em` em `norma` (não depender do código lembrar de
    setar);
  - `unique (origem_id, destino_id, tipo, coalesce(dispositivo,''))` em
    `norma_relacao` (evita duplicar relação ao reprocessar);
  - `unique (norma_id, ordem)` em `chunk` (permite upsert ao re-chunkear);
  - `id_materia_inlabs text unique` em `dou_materia` (chave natural real,
    confirmada no M0 como o atributo `idMateria` do XML do INLABS — sem isso
    não dá pra rodar o job do DOU duas vezes no mesmo dia sem duplicar).
- `/health` nesta fase é só liveness. O `/health` completo da seção 9
  (status das últimas ingestões por fonte) só faz sentido a partir do M5,
  quando `job_execucao` passa a ter dados de verdade — implementar antes
  seria simular um retorno vazio sem valor real.
- Ainda **não existe nenhum ingestor real** em `app/ingest/` além do
  contrato (`base.py`) — isso é trabalho do M2 (AnvisaLegis) e M5 (gov.br
  notícias, INLABS).
- CI (`.github/workflows/ci.yml`) só roda de fato quando o repo for
  empurrado para o GitHub — ainda é só local nesta sessão.

## Milestones (status)

- [x] M0 — Reconhecimento das fontes.
- [x] M1 — Esqueleto (repo, uv, FastAPI, schema Supabase, `.env.example`,
      Docker, CI).
- [ ] M2 — Ingestão do módulo 310 (carga histórica + grafo de relações +
      status derivado).
- [ ] M3 — Chunking + embeddings + busca híbrida.
- [ ] M4 — Chat RAG (`/chat` + golden QA).
- [ ] M5 — Tempo real (INLABS, RSS/notícias, módulo 630, scheduler, `/timeline`).
- [ ] M6 — Frontend Next.js.
- [ ] M7 — Alertas.
- [ ] M8 (opcional) — Ponte com licitações.
