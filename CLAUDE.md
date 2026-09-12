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
frontend/         # Next.js (App Router) — M6, ver seção própria abaixo
  src/
    app/          # páginas (chat em `/`, `/timeline`, `/consultas-publicas`)
                   # e o proxy `app/api/chat/route.ts`
    components/   # `components/ui/*` (primitivas estilo shadcn, escritas à
                   # mão — ver decisão do M6) + componentes de página
    lib/
      api.ts      # cliente do backend — só roda no servidor do Next.js
      utils.ts    # `cn()` (clsx + tailwind-merge)
      format.ts   # formatação de data/prazo em pt-BR
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

# frontend (M6) — com o backend rodando em localhost:8000
cd frontend && npm install
npm run dev            # http://localhost:3000
npm run lint && npm run build   # o CI roda os dois
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

### M3 — Chunking + embeddings + busca híbrida
- **Embeddings via OpenRouter, não OpenAI direto**: a chave disponível era
  da OpenRouter (`sk-or-v1-...`), que expõe um proxy compatível com a API
  da OpenAI. Testado de verdade: `openai/text-embedding-3-large` na
  OpenRouter devolve exatamente 3072 dimensões, igual ao pedido no
  briefing — só muda o gateway (`base_url` + chave), o SDK `openai` e o
  modelo continuam os mesmos. `app/embeddings.py`.
- `app/chunking.py`: divide por unidade jurídica via regex de fronteira
  (`Art. N`, `ANEXO N`) — nunca corta um artigo no meio porque cada
  unidade vira uma fatia só depois de já estar delimitada. Artigo/anexo
  grande demais (>1.500 tokens, contados com `tiktoken` cl100k_base como
  aproximação) é dividido por §/Parágrafo único/inciso romano.
  **Bug pego com dado real**: dividir só por parágrafo em branco (sem
  agrupar) transformou uma tabela de anexo grande (norma de 1966) em 1.451
  fragmentos, a maioria com 2-3 tokens — inútil pra embedding. Corrigido
  com agrupamento guloso (`_agrupar_gulosamente`) que junta partes
  pequenas até ~1.500 tokens antes de virar chunk.
- `app/chunk_store.py`: grava vetor sem a dependência `pgvector` — formata
  como o literal de texto que o Postgres já entende (`'[0.1,...]'::vector`).
  Usa `executemany` em vez de um insert por chunk: com dezenas de milhares
  de chunks, uma ida à rede por linha seria o gargalo dominante do job
  (rede até o Supabase, não CPU) — mesma lição do M2.
- `app/busca.py`: híbrida de verdade (vetorial `<=>` sobre o cast pra
  `halfvec` que indexa + full-text `plainto_tsquery('portuguese', …)`),
  fundidas por Reciprocal Rank Fusion (k=60), filtros por tema/ano/
  tipo_ato/status_vigencia. `scripts/busca_cli.py` é o "CLI de busca pra
  validar qualidade antes de plugar o LLM" pedido no M3.
- `scripts/busca_cli.py` precisou de `sys.stdout.reconfigure(encoding=
  "utf-8")` — o console do Windows (cp1252) derruba o script na primeira
  vez que tenta imprimir um caractere fora do cp1252 (não é só acento
  saindo errado como no AnvisaLegis — aqui é `UnicodeEncodeError` de
  verdade, sem esse reconfigure).
- **Segundo bug pego só depois de rodar a carga inteira**: o corte bruto de
  último recurso (`_cortar_bruto`, quando nem §/inciso/parágrafo dividem o
  bloco) estimava ~4 caracteres/token — bom pra prosa comum, ruim pra
  tabela de anexo densa em números/símbolos (tokeniza mais apertado). 561
  chunks (2%) saíram com até 3.171 tokens, mais que o dobro do limite.
  Corrigido cortando pela sequência real de tokens do `tiktoken` (encode →
  fatia → decode), que garante o limite sempre, não estima. As 72 normas
  afetadas foram reprocessadas (chunks antigos apagados e recriados).

### Resultado final da carga de chunking + embeddings

```
1.129 normas com texto_integral → 27.355 chunks (média 316 tokens, máximo
exatamente 1.500 — 0 chunks acima do limite depois da correção)
Custo real (OpenRouter): ~US$ 1,70
Tempo: ~30 min (carga completa) + reprocessamento dos 72 corrigidos
```

Validação de qualidade (`scripts/busca_cli.py`) com as próprias perguntas
de exemplo do briefing:
- *"rotulagem nutricional de suplemento alimentar"* → RDC 243/2018
  ("Dispõe sobre os requisitos sanitários dos suplementos alimentares")
  em 1º lugar, vigente. Resposta certa.
- *"farmácia magistral boas práticas de manipulação"* → RDC 67/2007 em
  1º lugar, **corretamente marcada como REVOGADA** — o requisito mais
  crítico do projeto (nunca tratar norma revogada como vigente) já
  funciona de ponta a ponta, da ingestão à busca.

Pendência pequena e não-bloqueante: 20 de 2.595 normas revogadas (0,8%)
têm `ementa` = "Série Histórica" em vez da ementa de verdade — um link de
navegação da listagem que o parser do M2 pegou por engano em vez do `<p>`
de ementa nesses casos específicos. Não afeta status_vigencia nem a busca
em si (o texto do chunk continua correto), só o campo `ementa` exibido.

### M4 — Chat RAG
- **LLM via OpenRouter, não Anthropic direto** (`app/llm.py`): mesma
  decisão do M3 — a chave disponível é da OpenRouter, que também expõe
  `anthropic/claude-sonnet-4.5` (via Amazon Bedrock) no formato de chat
  completions da OpenAI. Testado de verdade antes de decidir. O SDK
  `anthropic` (dependência do M1) acabou não sendo usado — o endpoint de
  chat completions da OpenRouter para modelos Anthropic não fala o
  protocolo nativo da Anthropic (`/v1/messages`), só o formato OpenAI:
  usar o SDK `anthropic` exigiria reimplementar a tradução de formato à
  toa, então ficamos só com `openai.AsyncOpenAI` apontado pro
  `base_url` da OpenRouter, igual ao M3.
- `app/intent.py`: roteamento de intenção (seção 7) — norma específica
  (regex + lookup direto), temporal (`norma.data_publicacao`, com aviso
  explícito de que notícias/DOU ainda não estão indexados — isso é M5),
  consulta pública (resposta honesta e determinística, sem chamar o LLM à
  toa, porque o módulo 630 ainda não foi ingerido) e temática (busca
  híbrida do M3).
- **Achado real, não suposto**: a base tem o mesmo tipo de ato sob siglas
  diferentes dependendo de qual parte do AnvisaLegis alimentou aquele
  registro (`INM`/`IN`/"INSTRUÇÃO NORMATIVA " truncada, `RES`/`RE`,
  `POR`/`PRT`, e uma "PORTARIA CONJUNTA MS" truncada em 20 caracteres ao
  lado da sigla `PCJ`). Resolvido sem migração destrutiva: o lookup por
  norma específica busca por um *grupo* de siglas equivalentes
  (`GRUPOS_TIPO_ATO`), não uma sigla exata — mais seguro que arriscar
  fundir linhas que podem não ser duplicatas de verdade.
- Resposta "não encontrei" (RDC inexistente, consulta pública) é
  **determinística, sem gastar uma chamada de LLM** — mais barato e
  garante zero alucinação nesses casos por construção, não por sorte do
  modelo.
- `supabase/migrations/0002_chat_metricas.sql`: tabela `chat_metrica`
  (pergunta, intenção, nº chunks recuperados, teve citação, tokens,
  custo, latência) — seção 10 do briefing.
- `tests/golden_qa.yaml` + `tests/test_golden_qa.py`: 33 perguntas reais
  (número + status conferidos direto no banco antes de escrever a
  pergunta, nenhum inventado), incluindo as 2 de anti-alucinação da seção
  10 (RDC que não existe). Marcado `golden_qa` e excluído do `pytest -q`
  padrão via `addopts` (faz chamadas reais de LLM, ~7 min, custo real) —
  roda de propósito com `uv run pytest -m golden_qa`.
- **2 causas raiz achadas rodando o golden QA pela primeira vez** (33
  perguntas, não só as que eu tinha testado manualmente — 3 testes
  falharam, por estas 2 causas):
  1. "Portaria Conjunta" não era reconhecida como norma específica (o
     regex não esperava a palavra extra entre "Portaria" e o número) —
     virava busca temática e claro que não achava a norma certa.
     Corrigido com um grupo `PCJ` dedicado (achado do item anterior).
  2. O teste original checava `intencao == "nao_encontrado"` pros casos
     de RDC inexistente, mas o código deliberadamente mantém a intenção
     *detectada* (`norma_especifica`) mesmo quando não encontra nada —
     é mais útil pra métricas saber "quantas perguntas sobre norma
     específica não acharam nada" do que perder essa distinção. Corrigi
     o teste, não o código: o que importa pra anti-alucinação é
     `normas` vir vazio, não o rótulo da intenção.

Resultado: **33/33 perguntas do golden QA passando** depois das
correções. Métricas agregadas reais (`chat_metrica`, 40 respostas
incluindo testes manuais): latência média ~9,8s, ~3,5 chunks recuperados
por resposta, 87% com citação de norma na resposta, custo total ~US$0,40.

### M5 — Tempo real (INLABS + notícias + módulo 630 + scheduler + endpoints)

- **Módulo 630 (Consultas Públicas)**: `cod_menu=9789` = "CONSULTA PÚBLICA
  ATIVA (19)", achado testando de verdade contra o portal — bate exato com
  o "19 CP abertas" do enunciado. Guardado na tabela `noticia`
  (`categoria='consulta_publica'`, como o schema já previa), com duas
  colunas novas (`prazo_inicio`/`prazo_fim`, migration `0003`) extraídas do
  campo `#prazoContribuicao` ("DD/MM/AAAA a DD/MM/AAAA") de cada página de
  detalhe, com suporte a prorrogação (`#prorrogacaoPrazo`). Rodado de
  verdade: 19 CPs carregadas.
- **Notícias gov.br — o RSS documentado no briefing está morto (404)**.
  Pior: o `window.__data` embutido no HTML ignora `b_start` no SSR (sempre
  devolve a página 1, não importa o que a URL peça — confirmado comparando
  respostas). A alternativa real: o Plone expõe sua própria API REST no
  mesmo domínio, sob `++api++`
  (`https://www.gov.br/anvisa/++api++/pt-br/...`), que **essa sim** pagina
  de verdade. Cada notícia é um item Volto em blocos (`blocks` +
  `blocks_layout`); só os tipos `html` (stripa tags com `selectolax`) e
  `slate` (já vem com `plaintext` pronto) carregam texto de corpo. Carga
  inicial rodada de verdade: **105 notícias** (jan–mar/2026); resto do ano
  corrente e anos anteriores (2023–2025 também têm conteúdo, confirmado)
  ficam para as próximas execuções incrementais do job diário — mesmo
  padrão de completude gradual aceito no M2.
- **INLABS — login/download já validados no M0**; o que faltava era o
  parser. Achado real ao escrever `app/ingest/inlabs.py`: **um parser
  HTML5 (`selectolax`) não é seguro para esse XML** — `<![CDATA[...]]>`
  dentro de tag desconhecida vira "bogus comment" pela tokenização HTML5,
  apagando o conteúdo de `<Identifica>`/`<Texto>`. Trocado para
  `xml.etree.ElementTree` (stdlib), que respeita CDATA — confirmado com as
  duas amostras reais do M0. Rodado de verdade contra 11/09/2026: 18
  matérias da ANVISA (DO1: 14 Resolução-RE + 1 Aresto, DO2: 3 Portarias,
  DO3: 1 Aviso de Licitação). **Achado de negócio, não bug**: essas
  "Resolução-RE" têm numeração própria (~3.500) bem mais alta que a série
  RES/RE que o módulo 310 cataloga (~1.020 em 2026) — 0 vínculos com
  `norma` é o resultado correto, não um linker quebrado.
- **Scheduler** (`app/scheduler.py`): APScheduler, cron 06:00
  `America/Sao_Paulo`, dispara os 4 scripts de carga **como subprocesso**
  (não importando `main()` direto) — isola falha de um pool asyncpg de uma
  fonte do loop de eventos da API e das outras fontes. Não existe, em
  nenhuma fonte documentada, um endpoint de "o que mudou desde ontem" —
  por isso o módulo 310 é re-rodado por inteiro todo dia (idempotente via
  upsert). Liga junto com a API só se `DATABASE_URL` estiver configurada
  (`scheduler_habilitado=true` por padrão, desligável no `.env` — útil
  para rodar a API em ambiente de teste sem disparar ingestão).
- **Endpoints novos** (`app/timeline.py` + rotas em `app/main.py`):
  - `GET /timeline?dias=30&limite=100` — união (`UNION ALL`) de
    norma/noticia/dou_materia por data, mais recente primeiro. Validado
    com todas as 4 fontes aparecendo (`norma`, `noticia`,
    `consulta_publica`, `dou`) numa janela de 365 dias contra o Supabase
    real.
  - `GET /consultas-publicas?apenas_abertas=true` — as CPs do módulo 630
    com prazo, ordenadas por prazo mais próximo primeiro.
  - `GET /health/fontes` — última execução de cada fonte em
    `job_execucao` (status, itens novos, erro). O `/health` original
    virou liveness pura (não toca banco); saúde das fontes é endpoint
    separado, para não confundir "API no ar" com "ingestão saudável".

Resultado: módulo 630 (19 CPs), notícias gov.br (105, incremental),
INLABS (18 matérias/dia testado), scheduler armado, 3 endpoints novos
validados contra o Supabase real. Pendente: backfill completo de notícias
de anos anteriores (fica incremental, não bloqueia); vínculo DOU→norma só
cobre os tipos que o módulo 310 já cataloga.

### M6 — Frontend Next.js

- **`npm install`/`npx create-next-app`/`npx shadcn init` batem num
  bloqueio real deste ambiente**: um `~/.npmrc` de segurança (`allow-scripts
  = ["@anthropic-ai/claude-code"]`) impede qualquer outro pacote de rodar
  script de instalação. Resolvido sem enfraquecer a política — instalação
  em duas etapas: primeiro `npm install --ignore-scripts` (nenhuma das
  libs usadas aqui — Next, React, Tailwind v4, class-variance-authority,
  clsx, tailwind-merge, lucide-react — precisa de postinstall pra
  funcionar; confirmado rodando `next build`/`next dev` de verdade depois).
- **`shadcn/ui` sem a CLI**: o `npx shadcn init`/`add` sempre dispara um
  `npm install` interno sem `--ignore-scripts`, esbarrando no mesmo
  bloqueio, sem flag pra contornar. Em vez de enfraquecer a política de
  segurança do ambiente, os componentes (`button`, `card`, `badge`,
  `input`) foram escritos à mão em `src/components/ui/`, seguindo a
  convenção real do shadcn (Tailwind + `class-variance-authority` +
  `cn()` local) — o `components.json` gerado antes do bloqueio foi
  mantido como documentação do estilo (`base-nova`, `neutral`), então
  `npx shadcn add <algo>` volta a funcionar sozinho se essa restrição for
  relaxada no futuro.
- **Arquitetura cliente/servidor**: o browser nunca fala com o FastAPI
  direto. Server Components (`/timeline`, `/consultas-publicas`) chamam o
  backend server-to-server via `src/lib/api.ts` (env `API_BASE_URL`, sem
  `NEXT_PUBLIC_`, nunca vai pro bundle do cliente); o chat (que precisa de
  interatividade) é um Client Component que fala só com
  `app/api/chat/route.ts`, um proxy fino que roda no servidor do Next.js e
  repassa pro FastAPI. Vantagem sobre expor o FastAPI direto ao browser:
  **zero configuração de CORS no backend** (nada mudou em `app/main.py`) e
  a URL/porta real do backend nunca aparece no bundle do cliente — mesmo
  padrão que já vale em produção (Vercel → Railway são dois serviços
  falando servidor-a-servidor).
- **Next.js gera `AGENTS.md`/`CLAUDE.md` sozinho dentro de `frontend/`**
  na primeira vez que roda (`next dev`/`build`) — colidiria com o
  `CLAUDE.md` real da raiz do repo (que seguimos à risca aqui). Removido e
  desligado via `agentRules: false` em `next.config.ts`.
- Três páginas, todas testadas de ponta a ponta contra o backend real
  (`uvicorn` local + `npm run dev`, não mock): chat (`/`, via `POST
  /chat`), linha do tempo (`/timeline`, com filtro de janela 7/30/90/365
  dias) e consultas públicas (`/consultas-publicas`, com toggle
  abertas/todas e contagem de dias restantes calculada no cliente a
  partir de `prazo_fim`). `status_vigencia` sempre com o mesmo badge
  verde/vermelho (`NormaBadge`) em qualquer lugar que uma norma apareça —
  é o requisito mais crítico do produto, não dá pra deixar como texto
  solto em só um lugar.
- Validado com perguntas reais através da stack inteira (browser →
  Next.js → FastAPI → RAG → Postgres/LLM): "A RDC 27/2010 ainda está
  vigente?" devolve a resposta certa (REVOGADA, badge vermelho) e "O que
  diz a RDC 99999/2025?" devolve `normas: []` (zero alucinação) — os dois
  testes mais críticos do projeto, agora também na camada visual.

## Milestones (status)

- [x] M0 — Reconhecimento das fontes.
- [x] M1 — Esqueleto (repo, uv, FastAPI, schema Supabase, `.env.example`,
      Docker, CI).
- [x] M2 — Ingestão do módulo 310 (carga histórica + grafo de relações +
      status derivado). 4.579 normas, 11.848 relações no Supabase.
- [x] M3 — Chunking + embeddings + busca híbrida. 27.355 chunks
      embeddados, busca validada com as perguntas de exemplo do briefing.
- [x] M4 — Chat RAG (`/chat` + golden QA). 33/33 perguntas passando.
- [x] M5 — Tempo real (INLABS, notícias gov.br, módulo 630, scheduler,
      `/timeline`, `/consultas-publicas`, `/health/fontes`).
- [x] M6 — Frontend Next.js (chat, timeline, consultas públicas).
- [ ] M7 — Alertas.
- [ ] M8 (opcional) — Ponte com licitações.
