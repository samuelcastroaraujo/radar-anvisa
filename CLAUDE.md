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

### M2 — Ingestão do módulo 310
- O `Protocol Fonte` do M1 não se encaixa no AnvisaLegis: uma única resposta
  de "ano" já traz dezenas de atos de uma vez (é assim que dá pra cobrir os
  1.138 vigentes com ~38 requisições em vez de 1.138 uma a uma). Em vez de
  forçar o `Protocol`, `app/ingest/anvisalegis.py` expõe funções diretas
  (`carregar_vigentes`, `carregar_revogadas`).
- Vigentes: endpoint real descoberto lendo o JS da própria página (não
  documentado em lugar nenhum) — `TematicaAction.php?acao=abrirVinculos` dá
  o texto integral de todos os atos de um ano numa resposta só. Ver
  Addendum M2 em `research/FONTES.md`.
- Revogadas: só ementa + aviso de revogação por ato (não o texto integral)
  — pegar o texto de cada uma das 2.438 exigiria 2.438 requisições
  individuais; fica pra quando o M3 (chunking) precisar do texto de
  qualquer forma.
- **Bug real pego rodando de verdade:** a listagem de revogadas pagina em
  blocos de 50; a primeira carga histórica achou só 1.598 dos 2.438 porque
  `abrirResenhaAnoData&ano=YYYY` só devolve a página 1. A paginação de
  verdade são 3 chamadas em sequência na mesma sessão HTTP
  (`abrirResenhaAnoData` → `carregarPaginaResenhaAno` pro total de páginas →
  `abrirPaginaResenhaAno&pagina=N` pro resto) — nenhuma leva `&ano=`, o ano
  fica em estado de sessão. Ver Addendum M2 em `research/FONTES.md`.
- Extração de relações tem duas fontes: `LinkTexto(...)` estruturado
  (confiável, dá tipo/número/ano/dispositivo do ato referenciado) e um
  fallback textual por regex ("Revogada pela X") só usado na listagem de
  revogadas, que não tem `LinkTexto` para esse aviso específico. O tipo da
  relação (`altera`/`revoga`/`retifica`/...) nos dois casos é heurístico
  (verbo mais próximo do link no texto) — não é a classificação oficial do
  portal, então os números do relatório de M2 não batem 1:1 com "464
  alteradoras/291 retificadoras/22 revogadoras" do portal (usados só como
  referência de ordem de grandeza).
- `status_vigencia`: authoritative pela listagem de origem (vigentes→
  vigente, revogadas→revogada), com um passo de pós-processamento que
  promove pra 'revogada' qualquer norma alvo de uma relação `tipo='revoga'`
  (regra da seção 5) — implementado em `scripts/carga_historica_310.py`.
- Toda norma citada como destino de uma relação mas ainda não crawleada
  ganha um registro mínimo com `status_vigencia='desconhecido'`
  (`get_or_create_norma_id` em `app/ingest/persistencia.py`) — nunca perdemos
  a relação, mas também nunca inventamos um status pra ela.
- `scripts/` precisou de um `__init__.py` e rodar como `python -m
  scripts.carga_historica_310` (não `python scripts/carga_historica_310.py`)
  — do contrário `from app...` falha porque o diretório do script vira
  `sys.path[0]`, não a raiz do projeto.
- Gravação no banco é concorrente (`CONCORRENCIA_GRAVACAO = 8`, ver
  `scripts/carga_historica_310.py`) — o crawling em si continua sequencial a
  1 req/s (isso é sobre o site, não muda), mas não há motivo pra gravar no
  Postgres um ato de cada vez. **Sem transação por ato de propósito**: a
  primeira tentativa com concorrência travou porque vários atos concorrentes
  citando a mesma norma-alvo ficavam esperando o commit uns dos outros
  (lock de linha do Postgres em INSERT ... ON CONFLICT ainda não commitado).
  Cada upsert já é atômico sozinho; perder a atomicidade "norma+relações"
  como um pacote é aceitável aqui porque tudo é idempotente.

### Resultado da carga histórica (rodada de verdade contra o Supabase)

```
Total de normas na base: 4579
  revogada       2595   (portal: 2438)
  vigente        1010   (portal: 1138)
  desconhecido    974   (citadas em alguma relação, ainda não crawleadas
                          diretamente — não é erro, é o grafo crescendo
                          além do módulo 310 sozinho: leis, decretos, etc.)

Relações no grafo: 11.848
  referencia  10305 relações / 1116 atos  (citação/base legal, não altera nada)
  revoga         769 / 499   (portal "revogadoras": 22 — categorias diferentes,
                               ver nota abaixo)
  altera         514 / 278   (portal "alteradoras": 464)
  substitui      151 / 119
  regulamenta     98 / 73
  revoga_parcial   8 / 8
  retifica         3 / 2     (portal "retificadoras": 291)
```

`vigente` (1010) e `revogada` (2595) não batem exato com o portal (1138 e
2438) porque: (a) a derivação por relação promove atos do lado "vigentes"
pra "revogada" quando uma relação `revoga` aponta pra eles (122 casos), o
que é correto pela regra da seção 5, mas move a contagem; (b) a extração de
`revoga`/`altera`/`retifica` é heurística (verbo mais próximo do link no
texto, não a classificação editorial do portal) — por isso "revogadoras
(22)" e "retificadoras (291)" do portal não têm correspondência 1:1 com os
769/3 que achamos. `desconhecido` (974) são normas de fora do módulo 310
(leis, decretos, outras resoluções) citadas como base legal ou revogadas/
alteradas por atos que carregamos, mas que a gente não crawleou como
"primary" ainda — ficam registradas com o mínimo (tipo/número/ano) pra não
perder a relação, prontas pra serem enriquecidas quando/se forem crawleadas.

## Milestones (status)

- [x] M0 — Reconhecimento das fontes.
- [x] M1 — Esqueleto (repo, uv, FastAPI, schema Supabase, `.env.example`,
      Docker, CI).
- [x] M2 — Ingestão do módulo 310 (carga histórica + grafo de relações +
      status derivado). 4.579 normas, 11.848 relações no Supabase.
- [ ] M3 — Chunking + embeddings + busca híbrida.
- [ ] M4 — Chat RAG (`/chat` + golden QA).
- [ ] M5 — Tempo real (INLABS, RSS/notícias, módulo 630, scheduler, `/timeline`).
- [ ] M6 — Frontend Next.js.
- [ ] M7 — Alertas.
- [ ] M8 (opcional) — Ponte com licitações.
