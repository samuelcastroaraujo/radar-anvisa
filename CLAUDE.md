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
- Scraping: `httpx` + `selectolax`; `pymupdf` para PDF. Exceção pontual:
  `curl_cffi` só em `app/ingest/consultas_alimentos.py` (Cloudflare Bot
  Management bloqueia o fingerprint de TLS do `httpx` nesse domínio
  específico — ver seção "Consulta de registro de produtos" abaixo).
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

### M7 — Alertas

- **Sem credencial de Resend/Telegram no `.env`** (usuário optou por não
  fornecer agora — ver decisão registrada abaixo). Construídos os 3 canais
  por completo conforme a doc oficial de cada API, mas só o **webhook foi
  testado de ponta a ponta com envio real** (contra `webhook.site`, um
  coletor de teste público) — email (Resend) e telegram (Bot API) ficam
  implementados e prontos, mas **não disparados de verdade** até existir
  chave. Isso é uma pendência explícita, não uma suposição escondida.
- `alerta_regra` já existia desde o schema original (0001); faltava só
  como não notificar a mesma regra pro mesmo item duas vezes —
  `alerta_disparo` (migration `0004`), com `unique (alerta_id, item_tipo,
  item_chave, canal)`, mesmo espírito de idempotência total do resto do
  projeto.
- **"Recente" é `coletado_em`/`processado_em`, nunca `data_publicacao`**:
  o módulo 310 é re-crawleado por inteiro todo dia (decisão do M5), então
  se o alerta disparasse em cima da data de publicação da norma, uma RDC
  de 1999 apareceria como "nova" toda manhã. As duas colunas de
  "primeiro-visto" **não são tocadas pelo `on conflict do update`** de
  nenhum ingestor (conferido lendo o código dos 4 e confirmado com uma
  re-inserção real de uma norma já existente: `coletado_em` não mudou) —
  esse é o sinal correto de "genuinamente novo pro sistema".
- **`temas` é best-effort**: `norma.tema` existe desde o M1 mas nenhum
  ingestor até hoje o preenche (achado construindo o M7, não um bug novo)
  — fica pronto pra quando essa extração existir; hoje só `termos`
  (substring case-insensitive) encontra alguma coisa de verdade.
- **2 bugs reais achados testando de ponta a ponta contra um webhook de
  verdade** (não em teste unitário — só apareceram rodando contra a API
  real do `webhook.site`, que devolveu 429 depois de uma rajada de
  requisições):
  1. `rodar_verificacao` contava **tentativas** de `alerta_disparo`
     (diferença de contagem da tabela antes/depois), não **sucessos** —
     um canal que falhava (ex.: 429) ainda incrementava "N notificações
     enviadas" no log do job. Corrigido: `notificar` agora retorna quantos
     canais foram enviados **com sucesso**, e é essa soma que o job
     reporta.
  2. `ja_disparado` tratava qualquer linha em `alerta_disparo` — inclusive
     `status='erro'` — como "já tratado", bloqueando pra sempre o reenvio
     depois de uma falha transitória (o 429 do teste real). Corrigido:
     só `status='ok'` conta como já disparado; `registrar_disparo` trocou
     `on conflict do nothing` por `do update` pra uma tentativa nova poder
     substituir uma linha de erro antiga pela de sucesso.
- **Achado operacional, não de código**: a primeira tentativa de teste
  E2E usou uma regra sem `termos`/`temas` (que casa com "qualquer coisa
  nova", uso legítimo) contra um banco onde as 4.579 normas do M2 tinham
  `coletado_em` de "hoje" (todo o histórico foi carregado nesta mesma
  sessão) — resultado: ~4.600 tentativas de webhook de uma vez, gerando
  429 no `webhook.site`. Não é um bug do motor de alertas (em operação
  normal, só normas genuinamente novas por dia teriam `coletado_em`
  recente — um número pequeno), é um artefato de todo o histórico ter
  sido carregado no mesmo dia que o M7 foi construído. Revalidado depois
  com uma regra de termo específico contra um item de teste isolado: 1
  match, 1 envio, 1 registro em `alerta_disparo`, dedupe confirmado numa
  segunda execução.
- Endpoints REST simples pra gerenciar regras — sem autenticação (mesmo
  estado do resto da API até aqui; ponto de atenção antes de expor a
  internet, não resolvido neste milestone): `POST /alertas` (valida que
  `destino` tem o campo certo pro(s) canal(is) escolhido(s) — ex.: canal
  `webhook` exige `destino.url` — antes de gravar), `GET /alertas`,
  `PATCH /alertas/{id}` (liga/desliga), `DELETE /alertas/{id}`.
- `app/scheduler.py`: `scripts.verificar_alertas` roda por último na lista
  de scripts diários — só depois que as 4 fontes já carregaram é que
  existe "recente" de verdade pra casar contra as regras.

### Deploy real (produção)

Backend no Railway (`https://radar-anvisa-production.up.railway.app`) e
frontend na Vercel (`https://frontend-phi-ten-lhullf72f6.vercel.app`),
repositório em `github.com/samuelcastroaraujo/radar-anvisa` (privado).
Passo a passo completo em `DEPLOY.md`. **2 bugs reais achados só ao
implantar de verdade** (nenhum dos dois aparecia em dev local):

1. **A conexão direta do Supabase só resolve em IPv6** (confirmado com
   `getaddrinfo` real — nenhum registro A, só AAAA) — o Railway não tem
   saída IPv6, então o container subia mas o `/health` travava em
   `OSError: [Errno 101] Network is unreachable` ao abrir o pool.
   Corrigido trocando `DATABASE_URL` de produção pra connection string do
   **pooler** do Supabase (Supavisor, `aws-0-us-west-2.pooler.supabase.com:
   6543` — a região se descobre em Project Settings → General, não é
   óbvio a partir da connection string direta).
2. **O pooler em modo transaction não suporta os prepared statements que
   o `asyncpg` usa por padrão** — toda query dava
   `DuplicatePreparedStatementError` (só apareceu depois de corrigir o
   bug 1 e a API já estar recebendo tráfego de verdade). Corrigido com
   `statement_cache_size=0` em todo `asyncpg.create_pool` do projeto
   (`app/db.py` + 6 scripts) — sem isso, o pooler é inutilizável com esse
   driver.

Também corrigidos **antes** do deploy (achados revisando o `Dockerfile`,
nunca chegaram a rodar em produção assim): faltava `COPY scripts
./scripts` (o scheduler dispara cada fonte como `python -m scripts.carga_
*`, sem a pasta no container isso quebraria todo dia com
`ModuleNotFoundError`) e o `CMD` fixava a porta em 8000 em vez de
respeitar o `$PORT` que o Railway injeta.

Validado de ponta a ponta em produção depois das correções: `/health`,
`/health/fontes` (mostrando o histórico real de `job_execucao`),
`/timeline` e `/consultas-publicas` com dado real do Supabase, e `/chat`
(direto no Railway e via proxy da Vercel) respondendo "A RDC 27/2010
ainda está vigente?" corretamente com **REVOGADA** — o requisito mais
crítico do projeto, validado com tráfego real de produção, não só em
teste local.

Pendente: conectar o repositório GitHub ao projeto da Vercel *e* ao
serviço do Railway pra deploy automático a cada push (a CLI não autoriza
sozinha essas integrações — precisa ser feito uma vez pelo dashboard de
cada um; ver achado abaixo); e as credenciais opcionais de alerta
(Resend/Telegram, ver M7).

**Achado real, só descoberto tentando fazer um deploy de verdade (pós-M7,
"registro de produtos")**: nem Railway nem Vercel builda sozinho a partir
de `git push` **neste projeto** — os dois foram linkados via CLI, não
pela integração GitHub App. Confirmado batendo `git push` real e o
endpoint novo continuar 404 em produção por 10+ minutos; `railway
deployment list` mostrou que o único deploy disparado no período tinha
`meta.reason: "redeploy"` (reaproveita a imagem já buildada, não builda
de novo — foi um `railway redeploy` de uma sessão anterior, não o push).
Deploy de verdade exige `railway up --detach` (Railway) e, da raiz do
repo (não de dentro de `frontend/` — o projeto na Vercel tem "Root
Directory" = `frontend`, então rodar de dentro da própria pasta falha
com "Root Directory frontend does not exist"), `vercel deploy --prod`
(Vercel, com o link de `frontend/.vercel/project.json` copiado pra
`.vercel/` na raiz). Os dois CLIs já estavam autenticados neste ambiente
(`railway whoami` → conta do Railway; `vercel whoami` →
`karineluizadv-8915`) — não precisei pedir credencial nova. Documentado
o passo a passo real em `DEPLOY.md` (seções 2.3 e 4). **Achado à parte**:
o domínio padrão `frontend-karineluizadv-8915.vercel.app` (o que o
usuário estava usando) tem a proteção "Vercel Authentication" (SSO) do
time ligada — 302 pro login da Vercel pra quem não é do time; o alias
customizado publicado (`frontend-phi-ten-....vercel.app`) não tem essa
proteção e é o que efetivamente funciona como URL pública. `vercel
deploy --prod` promove pra produção e atualiza todos os aliases de
produção existentes automaticamente (não precisa re-apontar nada à mão).

**3º bug real, achado com tráfego de produção de verdade (não pelo
deploy em si — pela primeira pergunta genérica de um usuário)**:
`_contexto_tematico` (`app/chat.py`) guardava `ResultadoBusca.norma_id`
direto em `NormaCitada.id` sem `str()` — o dataclass anota o campo como
`str`, mas o asyncpg devolve `uuid.UUID` de verdade pra coluna
`norma_id`. Como dataclass não valida tipo em runtime, isso nunca
quebrou: nem em uso local, nem nas 33/33 perguntas do golden QA — porque
`tests/test_golden_qa.py` chama `app.chat.responder()` **direto**,
pulando inteiramente a validação Pydantic real que só acontece na
fronteira HTTP (`NormaCitadaResponse` em `app/main.py`). Só estourou
quando um usuário de verdade mandou uma pergunta genérica (roteada pro
caminho temático) pelo `/chat` publicado. Corrigido em duas camadas: cast
`::text` direto na SQL de `app/busca.py` (`busca_vetorial`/`busca_texto`,
que tinha o mesmo problema latente em `chunk_id`, só nunca exercitado) e
`str()` explícito em `_contexto_tematico`. Teste de regressão novo,
`tests/test_chat.py`, reproduz o formato exato que o asyncpg devolve
(`uuid.UUID` cru) e valida contra o `NormaCitadaResponse` Pydantic real —
confirmado que ele falha sem o fix (revertido temporariamente pra provar)
e passa com ele. **Lição**: golden QA cobre a qualidade das respostas do
RAG, mas não substitui um teste que atravesse a fronteira HTTP real —
esse tipo de bug de serialização só um teste como esse pega.

### Correção crítica de status_vigencia (pós-M7, a pedido do usuário)

Pedido original: tornar a base "confiável, com acesso a todas as RDCs" pro
setor de nutrição da indústria. Investigando a cobertura real pra
responder isso (backfill do texto das revogadas — ver próxima seção),
achei um **bug real de direção que promovia normas genuinamente vigentes
para 'revogada' por engano** — o requisito #1, inegociável, do projeto
inteiro.

**A causa raiz**: `_extrair_relacoes` (app/ingest/anvisalegis.py) lê o
texto de uma norma e, quando acha um `LinkTexto(...)` perto da palavra
"revog" (ou "altera"/"retifica"/etc.), grava uma relação — mas até agora
sempre assumia voz ativa ("esta norma revoga a norma X"). Uma norma
VIGENTE pode perfeitamente ter, no meio do próprio texto, artigos ou
incisos individuais marcados "(Revogado pela RDC X)" — voz **passiva**:
é X quem revogou aquele dispositivo específico, não o ato inteiro que
está sendo lido. Sem essa distinção, a relação saía invertida (origem e
destino trocados) e o pós-processamento da seção 5
(`_derivar_status_por_relacao`, que promove pra 'revogada' qualquer norma
alvo de uma relação `tipo='revoga'`) marcava **X** — a norma que
revogou o dispositivo, tipicamente ainda vigente — como revogada.

**Nunca apareceu nos testes porque nunca tinha sido testado contra o
texto de uma norma antiga o bastante pra ter esse padrão** — as amostras
de teste até então eram todas relativamente enxutas. Só apareceu ao
processar de verdade o texto de uma norma vigente de 1966 (durante o
backfill das revogadas, ver próxima seção) cheia de incisos revogados
individualmente ao longo de décadas.

**Confirmado com uma checagem de sanidade direta contra a produção**:
buscar `norma_relacao` com `tipo='revoga'` onde a origem foi publicada
**depois** da data da norma que ela supostamente revoga — fisicamente
impossível (não existe revogação retroativa) — achou **142 casos**. Uma
delas era a **RDC 67/2007**, citada no relatório do M3 como prova de que
o requisito de vigência funcionava (`busca_cli.py` a mostrava
corretamente "REVOGADA") — na real, essa "prova" estava validando o
efeito colateral do mesmo bug, não a correção do sistema. Confirmado
direto na fonte: RDC 67/2007 **não aparece** na listagem de revogadas ao
vivo do AnvisaLegis, e **aparece** na de vigentes.

**Correção em duas partes**:
1. `app/ingest/anvisalegis.py`: `VERBOS_RELACAO_PASSIVA` detecta a voz
   passiva ("revogad[ao] pel[ao]", "alterad[ao] pel[ao]", etc.) e marca
   `invertida=True`. Dentro disso, `_revoga_so_um_dispositivo` distingue
   "Art. 12 - (Revogado pela X)" (um dispositivo só → `revoga_parcial`,
   que não dispara a promoção de status) de "Revogada pela X" sozinho, no
   topo da página de um ato genuinamente revogado por inteiro (→
   `revoga`, que dispara). `tests/test_anvisalegis.py` tinha um teste que
   **afirmava o comportamento do bug como correto** (`invertida=False`
   pra esse caso) — corrigido pra refletir o comportamento certo, não o
   código pro teste.
2. `scripts/corrigir_direcao_revoga.py` (remediação de dado, não só de
   código): busca a lista de vigentes de verdade no portal com o parser
   corrigido e usa isso como fonte de verdade — qualquer norma marcada
   'revogada' no banco mas confirmada 'vigente' ao vivo tem a promoção
   revertida e a relação incorreta removida. Rodado em ponto fixo (3
   iterações, cada uma limpa uma camada — a primeira remoção de relação
   ruim permite o pós-processamento re-derivar com dado mais limpo, o que
   revela a próxima camada) até convergir em **0 candidatos**.

**Resultado final, verificado**: `vigente` 1010→**1129** (bate exato com
a contagem ao vivo do portal), `revogada` 2595→**2476**, zero relações
`revoga` com direção fisicamente impossível (eram 142). RDC 67/2007, RDC
243/2018, RDC 87/2008 e RDC 883/2024 confirmadas `vigente` — as duas
primeiras são exatamente os exemplos usados nos relatórios do M3/M4 como
prova do funcionamento do sistema. `tests/golden_qa.yaml` tinha uma
pergunta que codificava a resposta errada antiga pra RDC 67/2007
(`status_esperado: revogada`) — corrigida pra `vigente`. Golden QA
re-rodado depois da correção: ver resultado no fim desta seção.

**Achado à parte, não relacionado a este bug**: `Portaria 1081/2023`
(usada em outra pergunta do golden QA) está com `status_vigencia=
'desconhecido'` — gap de cobertura pré-existente (nunca foi crawleada
diretamente, só citada), não causado nem corrigido por este trabalho.

**Efeito colateral aproveitado**: `AnvisaLegisClient` tinha timeout de
30s, curto demais pra alguns anos de vigentes com resposta grande (ex.:
1966, 600KB+) — deu `ReadTimeout` de verdade rodando a remediação.
Aumentado pra 90s.

### Backfill de texto das revogadas — resultado final e limite documentado da fonte

`scripts/backfill_texto_revogadas.py` (ver docstring do próprio arquivo)
precisou de 3 rodadas reais pra chegar ao resultado final:

1. Primeira rodada crashou depois de ~3h por conexão de pool caindo
   (`fix(backfill)`, ver commit — retry com backoff + skip-e-continua em
   vez de derrubar o job inteiro).
2. Reiniciada, rodou até o fim (`exited 0`) mas parou em 88,4% — o
   `job_execucao` dessa execução (`iniciado_em` 01:01 UTC) nunca chegou a
   marcar `terminado_em`/`status`, ficou `em_andamento` órfão (processo
   morreu sem logar, provavelmente PC hibernou/terminal fechado no meio
   da madrugada) — só percebido comparando com o processo real (nenhum
   `python.exe` rodando) contra a contagem real do banco.
3. Rodada final (`itens_novos=89`, 602s, `job_execucao` com `status='ok'`
   de verdade): confirma platô de **328 normas revogadas (11,6% de 2.821)
   que nunca vão ganhar `texto_integral` pelos endpoints do AnvisaLegis
   conhecidos hoje** — não é falha de parsing, é ausência real na fonte.

**Investigado e confirmado ao vivo, não só suposto**: as 328 têm
`url_origem` = placeholder `"(referenciada, não crawleada)"` — ou seja,
nenhuma delas nunca apareceu numa página de vigentes/revogadas que o
crawler já visitou; existem no banco só porque são alvo de uma relação
`revoga` de outro ato (regra da seção 5). Composição: 261 `RES`
(Resolução simples, diferente de `RDC`), 42 `POR`, 9 `INM`, 5 `RDC`, e 11
que nem são do módulo 310 (`LEI`/`DEC`/`DLG`/`PIM`/`DEP`/`GDT` — só
existiriam no Planalto/INLABS). Exemplo checado ao vivo: `RES 2185/2023`
não aparece nem na listagem de revogadas nem na de vigentes de 2023 no
AnvisaLegis (buscas reais feitas contra o site, não suposição) — o tipo
`RES` (ao contrário de `RDC`) parece ter cobertura inconsistente por ano
nesse portal. Testado também `abrirLegislacao` (cod_menu 9434, a "página
de busca" do módulo) como alternativa à navegação por ano — devolve a
página cheia sem filtrar por tipo/número/ano via GET simples (o filtro
real deve depender de uma chamada AJAX feita pelo JS da página, não
inspecionada). **Decisão do usuário**: aceitar os 328 como limite
documentado da fonte em vez de investigar mais fundo (reverse-engineering
do JS via browser) — não bloqueia o uso da base.

Resultado final aceito: **2.493 de 2.821 revogadas com `texto_integral`
(88,4%)**.

### Busca e filtro por data na `/timeline` (pós-M7, a pedido do usuário)

Pedido: poder escolher qualquer data na linha do tempo e pesquisar por RDC,
IN ou qualquer palavra-chave — em especial digitar um número (ex.: "243") e
achar todas as normas com esse número, de qualquer tipo de ato.

- `GET /timeline` ganhou três parâmetros novos, todos opcionais e
  compatíveis com o comportamento antigo: `q` (substring case-insensitive),
  `data_inicio`/`data_fim` (intervalo explícito de calendário). Quando
  qualquer um dos dois últimos é passado, `dias` é ignorado — o lado que
  faltar vira aberto (`data_inicio` ausente = 1900-01-01, `data_fim`
  ausente = hoje). Validação nova: 422 se `data_inicio > data_fim`.
- `buscar_timeline` (`app/timeline.py`) trocou o filtro relativo
  (`now() - make_interval(...)`) por um intervalo `[data_inicio,
  data_fim]` explícito calculado no endpoint — mais simples de testar e é
  o mesmo código que atende tanto o atalho "últimos N dias" quanto um
  período escolhido à mão. `q`, quando presente, filtra contra
  `tipo_ato || numero || ano || ementa` pra `norma` (é por isso que
  digitar "243" acha `RDC 243/2018` e `IN 243/2023` juntos — o número é
  parte do texto buscado, não uma coluna separada), `titulo || resumo`
  pra `noticia`, e só `titulo` pra `dou_materia` (não tem outro texto
  curto pra somar).
- Testado direto contra o Supabase de produção (não só unit test): `q=243`
  com janela ampla devolveu exatamente `IN 243/2023` e `RDC 243/2018`;
  `data_inicio=2018-01-01&data_fim=2018-12-31&q=RDC` devolveu só normas
  RDC publicadas em 2018, mais recente primeiro; intervalo invertido
  devolveu 422 como esperado.
- Frontend (`frontend/src/app/timeline/page.tsx`): formulário `GET` puro
  (sem JS de cliente — consistente com o resto da página, que já mudava
  `dias` via `<Link>`) com campo de busca e dois `<input type="date">`.
  Atalhos de "7/30/90/365 dias" continuam existindo; ao digitar uma
  palavra-chave sem escolher período explícito, o cliente manda `dias`
  bem alto (3650, o teto que o backend aceita) em vez de 30 — do
  contrário "pesquisar 243" não acharia a RDC 243/2018 (mais de 30 dias
  atrás) e pareceria quebrado. `lib/api.ts::buscarTimeline` trocou de
  posicional pra um objeto de filtro (`FiltroTimeline`) — único call site
  já atualizado.
- `npm run lint`, `npm run build`, `uv run ruff`/`mypy`/`pytest -q` (39
  passando) todos verificados depois da mudança.

### Segundo bug de direção `revoga` — rótulo de dispositivo incompleto (pós-M7, a pedido do usuário)

Pedido original: usuário reportou que a RDC 243/2018 aparecia como
"revogada" na busca (`q=243`), quando na verdade está **vigente com
alterações** — mesmo requisito #1 do projeto (nunca tratar norma vigente
como revogada).

**Causa raiz**: `_RE_ROTULO_DISPOSITIVO_ANTES`
(`app/ingest/anvisalegis.py`), usado por `_revoga_so_um_dispositivo` pra
distinguir "revogação de UM dispositivo" (`revoga_parcial`, não derruba o
ato) de "revogação do ato inteiro" (`revoga`, derruba), tinha dois
problemas reais, os dois só visíveis processando texto de verdade (não em
amostra pequena):

1. **Ponto final depois do rótulo**: "Parágrafo único.\xa0\n (Revogado
   pela X)" tem um "." entre o rótulo e o "(Revogado..." — diferente de
   "Art. 12 -"/"III -", que não têm. Sem tolerar esse `\.?`, o regex não
   batia e a revogação de um parágrafo único virava `revoga` (total).
2. **"ANEXO N" não era um rótulo reconhecido**: revogar um anexo inteiro
   (ex.: "ANEXO III\xa0\n\n\n(Revogado pela X)") também não derruba o ato
   — mesma lógica de "Art. N" — mas só "Art./§/parágrafo único/inciso/
   alínea" estavam na lista. Também "ANEXO" sozinho, sem número (RDC
   242/2018, ato com um único anexo), e "CAPÍTULO N"/"SEÇÃO N"/"TÍTULO N"
   (RDC 708/2022: "CAPÍTULO XIX\n\n\n(Revogado pela X)") — mesma lógica,
   outros níveis da hierarquia jurídica (Título > Capítulo > Seção >
   Subseção > Artigo).
3. **Janela de contexto curta demais**: 120 chars de HTML bruto (antes de
   tirar as tags) bastava pra "Art. 12 -" mas não pra "ANEXO III" quando
   vem envolto em mais markup (`</b><p class="link-revogado"><em ...>`) —
   193 chars reais no caso da RDC 818/2023. Alargada pra 300 chars;
   seguro porque `_RE_ROTULO_DISPOSITIVO_ANTES` ancora no fim da janela
   (`$`), então uma janela maior só arrisca sobrar contexto irrelevante no
   início, nunca casar o rótulo errado. Junto, trocado pra pegar a
   **última** ocorrência do verbo na janela (não a primeira) — com janela
   maior, a primeira ocorrência podia ser de um dispositivo anterior.

Sem essas correções, o pós-processamento (`_derivar_status_por_relacao`)
promovia o ato (que é o ALVO da relação `revoga` invertida — ver bug
irmão, seção "Correção crítica de status_vigencia") a 'revogada' por
engano, mesmo ele sendo genuinamente vigente.

**Por que persistia sozinho, mesmo corrigindo o regex**: `upsert_norma`
tem um "ratchet" de propósito (uma vez 'revogada', nenhuma carga
posterior reverte sozinha) — e a relação `revoga` errada, já gravada,
sobrevive porque `on conflict ... do nothing` não troca `tipo='revoga'`
por `'revoga_parcial'` numa chave já existente. Corrigido com
`scripts/corrigir_revoga_parcial_ponto.py` (mesmo padrão de
`corrigir_direcao_revoga.py`): recrawleia vigentes ao vivo com o parser
corrigido, remove as relações antigas com classificação errada
(`fonte='texto-listagem-revogadas'`), reinsere as corretas e reverte
`status_vigencia` pra 'vigente' quando não sobra nenhum `revoga` real
apontando pro ato.

**Resultado real, rodado contra produção** (várias rodadas, convergindo à
medida que cada variante de rótulo era achada): das 62 normas
inicialmente achadas (`status_vigencia='revogada'` no banco mas
confirmadas 'vigente' ao vivo), **58 corrigidas** — incluindo a própria
RDC 243/2018 (o pedido original do usuário) e outras usadas em relatórios
anteriores como prova do sistema (RDC 818/2023, RDC 723/2022, IN 161/2022,
RDC 31/2010, RDC 915/2024, etc.).

**Limite conhecido, não corrigido nesta rodada**: 4 normas continuam
'revogada' por engano — `scripts/corrigir_revoga_parcial_ponto.py` estourou
timeout nelas mesmo com 45s por ato (RDC 585/2021, RDC 81/2008, RDC
17/2007, RDC 16/2007 — todas com muitas relações `invertida` pra
reinserir, cada `get_or_create_norma_id` é uma ida à rede pelo túnel
`railway run`/pooler do Supabase). Não é falha de classificação — é só
questão de rodar de novo com mais paciência (ou de dentro do próprio
Railway, sem o túnel). RDC 585/2021 em particular também tem um segundo
problema, esse sim de classificação: uma tabela de anexo grande onde cada
LINHA cita "(Revogado pela X)" sem nenhum rótulo formal de dispositivo
antes (célula de tabela com nome de substância, não "Art. N"/"ANEXO N")
— precisaria de parsing consciente da estrutura da tabela (contagem de
`<tr>`/`<td>` envolvente), não só regex. Mesmo espírito do limite já
documentado dos "328 normas sem texto_integral": aceito como pendência
específica, não bloqueia o restante da correção. `scripts/
corrigir_revoga_parcial_ponto.py` é idempotente e seguro de rodar de novo
a qualquer momento pra tentar essas 4 (ou pegar normas novas que caiam no
mesmo padrão).

### Consulta de registro de produtos de alimentos/suplementos (pós-M7, a pedido do usuário)

Pedido: pesquisar pelo RADAR ANVISA o registro/notificação de um suplemento
alimentar, como em `https://consultas.anvisa.gov.br/#/alimentos/`.

- **Endpoint real achado lendo o JS-fonte do Angular** (mesma plataforma
  `consultas.anvisa.gov.br` já sinalizada como SPA não mapeada no M0, só
  que o módulo "alimentos", não "alertas de segurança"): `GET /api/
  consulta/alimento/produtos/?filter[nomeProduto]=...&filter[marca]=...
  &page=N&count=N` (busca) e `GET /api/consulta/alimento/produtos/
  {numeroProcesso}` (detalhe). Pelo menos um filtro é obrigatório — a
  própria API rejeita busca vazia (`HTTP 500`, `MSG-004`). Ver Addendum
  pós-M7 em `research/FONTES.md` para o reconhecimento completo.
- **Achado real, não suposto**: processo inexistente devolve **HTTP 500**
  (não 404), com `error: "Nenhuma apresentação encontrada"`.
  `detalhe_produto` (`app/ingest/consultas_alimentos.py`) distingue esse
  500 específico (vira `None`) de um 500 de verdade (propaga/retry) pelo
  corpo da resposta, não só pelo status code.
- **Achado que mudou a implementação**: o domínio está atrás de Cloudflare
  Bot Management, que bloqueia (403) toda requisição feita com `httpx`
  (a lib HTTP do resto do projeto) mesmo com headers idênticos aos de uma
  requisição que passa via `curl` — confirmado isolando variável por
  variável (não é User-Agent, não é HTTP/2; é o fingerprint de TLS/JA3 do
  `httpcore`/`ssl` do Python, diferente do `curl`). Testado repetidas
  vezes contra a API real: `httpx` deu 403 em 4/4 tentativas (mesmo com o
  retry/backoff padrão do projeto já ativo), `curl` passou 2/2. Resolvido
  trocando a dependência de rede **só deste módulo** para `curl_cffi`
  (bindings pra libcurl com impersonation de TLS de navegador de verdade,
  `impersonate="chrome"`) — confirmado 3/3 contra a API real antes de
  virar dependência (`uv add curl_cffi`), com decisão do usuário de não
  mexer no resto do projeto (nenhum outro domínio crawleado até hoje
  precisou disso).
- **Implementado como consulta ao vivo, não ingestão/RAG**: diferente dos
  outros módulos em `app/ingest/`, não existe "carregar tudo" — é lookup
  pontual sob demanda, mesmo raciocínio do roteamento `norma_especifica`
  no `/chat` (M4): não faz sentido indexar o catálogo inteiro de produtos
  regularizados pra embeddings. Dois endpoints novos em `app/main.py`,
  sem tocar o banco: `GET /produtos/alimentos` (busca, com paginação) e
  `GET /produtos/alimentos/{numero_processo}` (detalhe, 404 se não achar
  — a tradução do 500 "processo não encontrado" da ANVISA pra um 404
  decente na nossa própria API).
- Testes (`tests/test_consultas_alimentos.py`) usam amostras reais salvas
  em `research/samples/` (busca, detalhe, processo inexistente) com um
  `FakeSession` injetado no lugar do `curl_cffi.AsyncSession` — não existe
  um `MockTransport` equivalente ao do `httpx` pra essa lib, então o
  cliente expõe um `Protocol` mínimo (`get`/`close`) só pra permitir essa
  injeção nos testes.
- Validado de ponta a ponta contra a API real (não só teste mockado, via
  `TestClient` + rede de verdade): busca por "creatina" e "whey" retornou
  produtos reais (CNPJ do detentor, situação Ativo/Inativo, tipo Notificado/
  Registrado, categorias, marcas), detalhe de um processo específico trouxe
  a lista completa de marcas associadas, e processo inexistente devolveu
  404 limpo pela nossa API.
- **2º achado real, só apareceu testando paginação de verdade (página 2
  depois da 1ª, não numa chamada isolada)**: `totalElements`/
  `numberOfElements` da API da ANVISA não refletem o resultado real da
  busca — são função só do `count` pedido (`totalElements = count+2`,
  `numberOfElements = count+1`, **sempre**, confirmado com "whey",
  "creatina" e "vitamina c" dando exatamente os mesmos números pro mesmo
  `count`). Consequência: `content` sempre vem com 1 item a mais do que
  o pedido, com o último item de uma página igual ao primeiro da
  seguinte. Não é efeito de como chamamos a API: o controller Angular da
  própria página oficial (`alimentos.result`) usa esse mesmo
  `totalElements` pra alimentar sua tabela — o bug também aparece pro
  usuário final no site de verdade. Decisão do usuário: não expor
  `total_elementos`/`total_paginas` no `/produtos/alimentos` (seria um
  número fabricado). `_parse_resultado` corta `content` pro tamanho
  pedido — verificado ao vivo (1 item por página, páginas 1/2/3,
  comparado com uma busca de referência `count=20`) que isso dá uma
  sequência sem lacuna nem duplicata. Ver Addendum pós-M7 em
  `research/FONTES.md` para os números completos do isolamento.
- **Filtro de situação e paginação configurável (a pedido do usuário)**:
  `situacao_registro` ("Ativo"/"Inativo") em `buscar_produtos`/
  `GET /produtos/alimentos` — a API espera `'S'`/`'N'` em
  `filter[situacaoProduto]` (confirmado ao vivo), tradução fica só no
  módulo. Sozinho já conta como filtro válido (mesmo comportamento da
  página oficial — dá pra "listar todos os ativos"). Frontend
  (`/produtos`) ganhou dois `<select>` (Situação, Por página: 10/20/50),
  formulário GET puro, mesmo padrão sem JS de cliente do resto do site.
- **Menu "Registros de produtos" no frontend** (a pedido do usuário):
  nova rota `/produtos`, mesmo padrão de `/timeline`/`/consultas-
  publicas` (Server Component, form GET). `SituacaoRegistroBadge`
  reaproveita as cores verde/vermelho de `NormaBadge`.
- **Achado sério de deploy, só descoberto tentando publicar de
  verdade**: nem Railway nem Vercel builda sozinho a partir de `git push`
  neste projeto (os dois foram linkados via CLI, não pela integração
  GitHub App) — ver seção "Deploy real (produção)" acima pro achado
  completo e o passo a passo real em `DEPLOY.md`.

### Integração da consulta de produtos com o `/chat` (a pedido do usuário)

Pedido: o chat também responder perguntas de registro/regularização de
produto, não só ter a aba separada.

- **Nova intenção `produto_alimento`** (`app/intent.py`), roteada
  ANTES de `consulta_publica`/`temporal`: o sinal linguístico real é
  diferente do de norma — normas são "vigentes"/"revogadas", produtos
  são "registrados"/"regularizados"/"notificados". O regex exige um
  verbo copulativo (`está`/`são`/`é`/`foi`/`tem`/`têm`...) logo ANTES do
  particípio, não o particípio sozinho — sem isso, uma pergunta temática
  sobre o PROCESSO em geral ("como funciona a notificação de
  suplementos?") seria roteada errado pra consulta de produto em vez de
  ir pro RAG temático. Testado com 14 frases reais (8 devem ativar,
  6 não) antes de aceitar o regex — `tests/test_intent.py`.
- **Extração dos termos de busca via LLM** (`app/llm.py::
  extrair_termos_busca_produto`), decisão do usuário: uma chamada
  dedicada e barata transforma a pergunta livre ("a whey da growth
  ainda tá regularizada?") em JSON `{nome_produto, marca,
  detentor_registro}` — mesmo padrão do resto do projeto (Python decide
  o que buscar, LLM só extrai/formata), sem precisar de tool-calling.
  Nunca lança em resposta malformada (JSON quebrado -> tudo `None`,
  tratado como "não identificado").
- **3 achados reais, só apareceram testando de ponta a ponta com
  perguntas de verdade** (não em teste mockado — as duas primeiras
  perguntas reais testadas deram falso "não encontrei" pra produtos que
  se sabia existirem):
  1. **`nome_produto` composto demais não bate com a descrição
     telegráfica da ANVISA**: a LLM extraía "whey protein" (fazendo
     sentido em português), mas o cadastro da ANVISA só tem "WHEY 23.40
     HIGH POTE PEAD" — sem a palavra "protein" em lugar nenhum, filtro
     de substring não bate. Corrigido no prompt de extração: instrução
     explícita pra manter `nome_produto` na palavra única mais genérica
     possível ("whey", não "whey protein").
  2. **Achado mais sério, na raiz (afetava `/produtos` também, não só o
     chat)**: `filter[detentorRegistro]` da API da ANVISA só aceita
     **CNPJ exato** — razão social, mesmo completa e exata, devolve 0
     resultados sempre. A página oficial nunca manda texto livre pra
     esse filtro: o campo "Empresa" lá é um autocomplete
     (`input-empresa.directive.js`) que resolve nome -> CNPJ num
     endpoint separado (`/api/empresa/?filter[razaoSocial]=...`, achado
     lendo `empresa.service.js`) antes de filtrar produtos. Corrigido na
     raiz, em `buscar_produtos` (`app/ingest/consultas_alimentos.py`):
     quando `detentor_registro` não parece CNPJ (`_eh_cnpj`), resolve via
     novo `buscar_empresas` primeiro — beneficia `/produtos` e o chat ao
     mesmo tempo, sem duplicar lógica.
  3. **Mais de uma empresa pode bater com o mesmo nome parcial**: grupos
     econômicos com várias razões sociais quase idênticas, cada uma seu
     CNPJ (achado real buscando "belapin": 4 CNPJs candidatos, só **um**
     tinha o produto perguntado, e não era o primeiro da lista). Corrigido
     tentando até 3 CNPJs candidatos em sequência, parando no primeiro
     que trouxer produto (`_MAX_CANDIDATOS_EMPRESA`).
- **Degrau de relaxamento de busca** (`app/chat.py::
  _combinacoes_busca`/`_buscar_produtos_para_chat`): mesmo com o prompt
  de extração melhorado, a LLM ainda pode duplicar a mesma empresa nos
  campos `marca` e `detentor_registro` (achado real: "creatina da
  belapin" virou `marca="belapin"` + `detentor_registro="belapin
  industria..."`, e como todo filtro é AND, isso zerava o resultado
  mesmo o produto existindo). Em vez de tentar blindar o prompt pra
  sempre acertar 100% (frágil por natureza), a busca tenta uma escada de
  combinações cada vez menos específica (tudo -> nome+empresa ->
  nome+marca -> só nome -> só marca -> só empresa), parando na primeira
  que trouxer resultado — robusto a erro de extração por construção, não
  por sorte do prompt.
- **`RespostaChat`/`RespostaChatAPI` ganharam `produtos: list[...]`**
  (paralelo a `normas`) — `ProdutoCitado` (chat) e `ProdutoCitadoResponse`
  (API) espelham só os campos que viram card no frontend (não o objeto
  `ProdutoAlimento` inteiro). Frontend (`app/page.tsx`, chat): cards de
  produto no mesmo lugar dos cards de norma, reaproveitando
  `SituacaoRegistroBadge`.
- Validado de ponta a ponta com tráfego real (2 chamadas de LLM +
  chamadas reais na ANVISA, não teste mockado): "o whey da absolut
  nutrition está regularizado?" -> achou os 5 produtos reais, todos
  ATIVO, resposta cita cada um com nº de notificação e vencimento;
  "a creatina 100% da belapin industria e comercio de alimenticios está
  registrada?" -> achou os 2 produtos reais (só depois do fix de
  resolução de empresa — antes dava falso "não encontrei"), e a própria
  LLM acrescentou espontaneamente a distinção "notificado" vs
  "registrado" (dado real do campo `tipo_regularizacao` no contexto);
  pergunta com produto/marca inventados -> "não encontrei" honesto, sem
  alucinar. `teve_citacao` também passou a considerar produtos citados,
  não só normas.

### Marca do produto nos cards de `/produtos` + correção de lentidão (pós-M7, a pedido do usuário)

Pedido: mostrar "Marca do Produto" em cada card da listagem (ex.:
`JUST WHEY PROTEIN ISOLADO` pro processo `25351585937202398`), sem
prejudicar a velocidade de resposta.

- **Achado real**: a busca em lista da ANVISA nunca traz `marcas` (campo
  sempre `null` na resposta de busca, confirmado em todos os itens da
  amostra real) — só o endpoint de detalhe tem, e não existe endpoint em
  lote. `buscar_produtos` ganhou `enriquecer_marcas=True` (opt-in): busca
  o detalhe de cada item da página pra preencher `marcas`.
- **Bug real, achado revisando "está devagar" depois do primeiro
  deploy**: a primeira versão já tinha um semáforo de concorrência, mas
  cada chamada de detalhe ainda passava pelo `RateLimiter` de 1 req/s
  compartilhado por todo o cliente — o semáforo limitava quantas tarefas
  ficavam *em voo*, mas todas esperavam a mesma fila de 1s antes de
  disparar. Na prática, zero concorrência de verdade, ~N segundos pra N
  itens (pior que parecia ao ler o código).
- **2º achado, testando o fix do 1º ao vivo contra a API real**: tirar o
  limite por completo (só o semáforo, sem nenhum limiter) faz uma busca
  ampla (~50 itens, ex. `nome_produto=whey`) tomar **HTTP 429** da
  própria ANVISA numa fração real dos itens — o 1 req/s do resto do
  projeto (seção 0 de `research/FONTES.md`) não é só cortesia arbitrária,
  esse domínio rate-limita de verdade sob rajada. Corrigido com um
  `RateLimiter` **dedicado e mais rápido** só pro enriquecimento
  (`ConsultasAnvisaClient._limiter_enriquecimento`, ~4 req/s) por cima de
  um teto de concorrência (6) — mais rápido que 1 req/s, mas ainda um
  limite de verdade, não um bypass total. Cache em memória com TTL de 1h
  (`_cache_marcas`) evita repetir a mesma chamada de detalhe entre buscas
  populares ("whey", "creatina"). Teto de 15s pro enriquecimento inteiro;
  item que não termina a tempo só fica sem marca, não derruba a listagem.
- **3º achado, efeito colateral de testar o 2º ao vivo repetidas vezes
  contra a API real pra calibrar**: a própria bateria de testes deixou o
  IP temporariamente rate-limitado pela ANVISA (até a busca em lista
  simples, sempre em 1 req/s, chegou a tomar 429) — não é um bug da
  aplicação, é efeito de martelar a API de verdade várias vezes seguidas
  em poucos minutos só pra encontrar o teto seguro; a decisão foi parar
  de testar ao vivo (não faz sentido continuar pressionando a API de
  produção só pra calibrar um número) e escolher parâmetros com margem
  de segurança em vez do teto exato. Revelou uma lacuna real, essa sim
  corrigida: `HTTPError`/`RequestException` não tratados em
  `/produtos/alimentos` e `/produtos/alimentos/{numero_processo}`
  vazavam como 500 cru pro frontend depois que o `@retry` esgotava as 4
  tentativas — agora viram 503 com mensagem honesta ("ANVISA está
  limitando ou fora do ar agora"), já que a falha é da fonte externa, não
  desta API. `tests/test_produtos_endpoint.py` cobre os dois endpoints
  com um `HTTPError` simulado (não bate na rede).
- Validado ao vivo (antes da calibração de concorrência degradar por
  causa do próprio teste): busca real por `marca=just+whey&detentor_
  registro=10769880000119` devolveu os 3 produtos reais com marca
  correta (`JUST WHEY PROTEIN ISOLADO` no processo citado no pedido) em
  0,75s — a mesma busca do exemplo do usuário, ponta a ponta (browser →
  Vercel → Railway → ANVISA), sem tocar o banco.

### Login (pós-M7, a pedido do usuário)

Pedido: proteger o site com usuário e senha. **Decisão do usuário**: login
único compartilhado (não multiusuário, sem tabela de conta no Supabase) e
só o frontend fica atrás do login — a API no Railway continua respondendo
direto pra quem souber a URL (não divulgada), sem exigir autenticação
própria (ponto de atenção documentado no M7 pra `/alertas` já valia pro
resto da API; continua valendo).

- `frontend/src/lib/auth.ts`: usuário/senha vêm só de variável de
  ambiente (`AUTH_USERNAME`/`AUTH_PASSWORD`), nunca do banco. Sessão é um
  cookie HttpOnly cujo valor é um HMAC-SHA256 de um payload fixo, assinado
  com `AUTH_SECRET` — não guarda usuário/senha no cookie, só prova que
  quem o tem passou pelo `/api/login` (ou conhece `AUTH_SECRET`, que
  nunca sai do servidor). Falha fechada: sem as 3 variáveis configuradas,
  nenhuma sessão é criada nem validada — nunca abre a porta por engano
  num ambiente mal configurado.
- `frontend/src/proxy.ts` — **não `middleware.ts`**: achado real rodando
  `npm run build` pela primeira vez com esse arquivo, o Next.js 16.3.5 já
  trata `middleware.ts` como convenção depreciada em favor de `proxy.ts`
  (mesmo comportamento, arquivo/nome de export diferente); migrado com o
  codemod oficial (`npx @next/codemod@canary middleware-to-proxy .`) em
  vez de escrever a convenção nova já deprecada. Intercepta toda rota
  (exceto `/login`, `/api/login`, `/api/logout` e os assets estáticos do
  Next) e redireciona pra `/login?next=<rota original>` se o cookie de
  sessão não bater — cobre página E rota (`/api/chat` incluso, senão
  daria pra falar com o backend sem passar pelo login só chamando o proxy
  do Next direto).
- `POST /api/login`: compara usuário/senha em tempo aproximadamente
  constante (evita vazar por timing quantos caracteres bateram — risco
  baixo aqui, mas barato de mitigar), seta o cookie e redireciona pro
  `next` original. `next` é validado (`proximaUrlSegura`) pra aceitar só
  caminho interno começando com `/` e não `//` — sem isso,
  `next=//evil.com` seria um open redirect (o browser trata `//` como
  "mesmo protocolo, outro domínio"). `POST /api/logout` limpa o cookie.
- Formulário de login é HTML puro (`<form method="POST">`), sem JS de
  cliente — mesmo padrão do resto do frontend (`/timeline`,
  `/consultas-publicas`, `/produtos` já eram forms GET puros).
  `components/nav.tsx` ganhou um botão "Sair" (mesmo padrão, form POST) e
  não renderiza os links de navegação na própria tela de `/login`.
- Validado de ponta a ponta contra o servidor de dev real (não só
  lint/build): sem cookie -> 307 pra `/login`; senha errada -> volta com
  `?erro=1`; login certo -> cookie setado, `/` e `/produtos` voltam 200;
  `/api/chat` sem cookie também redireciona (não só as páginas); cookie
  forjado (valor arbitrário, sem o HMAC certo) é rejeitado; logout limpa
  o cookie e a próxima requisição volta a exigir login.
- **Pendente, não resolvido nesta sessão**: `AUTH_USERNAME`/
  `AUTH_PASSWORD`/`AUTH_SECRET` de produção ainda não foram definidos na
  Vercel (só existem valores de desenvolvimento local em
  `frontend/.env.local`, fora do git) — o deploy de produção só fica
  protegido de verdade depois que o usuário decidir o usuário/senha reais
  e eu configurar via `vercel env add` (ou o usuário configurar direto no
  dashboard da Vercel).

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
- [x] M7 — Alertas (`alerta_regra`/`alerta_disparo`, canais email/telegram/
      webhook — só webhook testado com envio real, os outros dois pendentes
      de credencial —, `/alertas` CRUD, scheduler).
- [ ] M8 (opcional) — Ponte com licitações.
