# RADAR ANVISA

Chatbot regulatório com base de conhecimento viva sobre normativa e notícias da ANVISA.

## O que é

Sistema que monitora, indexa e responde perguntas sobre toda a normativa e as notícias
da ANVISA (RDC, IN, RE, Portarias, Consultas Públicas, DOU), com **atualização diária
automática** e **citação obrigatória da fonte** (número do ato, data e link direto).

Responde perguntas como:

- "Qual RDC rege rotulagem nutricional de suplemento alimentar hoje?"
- "A RDC 27/2010 ainda está vigente? Foi alterada por quê?"
- "O que a ANVISA publicou sobre farmácia magistral nos últimos 30 dias?"
- "Tem consulta pública aberta sobre alimentos? Qual o prazo?"
- "Me resume as mudanças da última RDC sobre BPF."

## Requisitos inegociáveis

- Nunca responder sem citar número do ato, data e link direto.
- Nunca tratar norma revogada como vigente — status de vigência é a informação mais
  crítica do sistema.
- Quando não houver base indexada suficiente, dizer que não sabe. Zero alucinação de
  número de RDC.

## Stack

- **Backend:** Python 3.12 + FastAPI (gerenciador de pacotes: `uv`)
- **Banco:** Supabase (Postgres) com extensões `pgvector` e `pg_trgm`
- **Embeddings:** OpenAI `text-embedding-3-large` (dim 3072)
- **LLM:** Claude Sonnet via API Anthropic (SDK `anthropic`)
- **Scheduler:** APScheduler (ou cron do Railway)
- **Scraping:** `httpx` + `selectolax` (ou BeautifulSoup + lxml); Playwright só se
  comprovadamente necessário
- **PDF:** `pymupdf`
- **Frontend:** Next.js (App Router) + TypeScript + Tailwind + shadcn/ui
- **Deploy:** backend no Railway, frontend na Vercel

## Fontes de dados

- **AnvisaLegis** (base normativa) — https://anvisalegis.datalegis.net
- **Notícias e informes** — https://www.gov.br/anvisa/pt-br
- **DOU em tempo real** — INLABS (Imprensa Nacional)
- **Dados abertos** — https://www.gov.br/anvisa/pt-br/acessoainformacao/dadosabertos

Detalhamento de endpoints, encoding e formatos em [`research/FONTES.md`](research/FONTES.md).

## Rodando localmente

Pré-requisitos: Python 3.12 e [`uv`](https://docs.astral.sh/uv/).

```bash
cp .env.example .env        # preencher DATABASE_URL, ANTHROPIC_API_KEY, etc.
uv sync --dev
uv run uvicorn app.main:app --reload
# em outro terminal, aplicar o schema no Supabase (uma vez):
psql "$DATABASE_URL" -f supabase/migrations/0001_schema.sql
```

Qualidade (o CI roda os três a cada push):

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy app
uv run pytest -q
```

Com Docker:

```bash
docker build -t radar-anvisa .
docker run --env-file .env -p 8000:8000 radar-anvisa
```

## Status

M1, M2 e M3 prontos: a base de normas está populada de verdade no Supabase
— 4.579 normas e quase 12 mil relações entre elas (quem altera, revoga,
substitui quem), carregadas direto do AnvisaLegis, e mais de 27 mil trechos
já com embedding e busca híbrida (vetorial + full-text) funcionando —
testada com as próprias perguntas de exemplo do produto ("qual RDC rege
rotulagem nutricional de suplemento alimentar" já responde certo, e norma
revogada aparece corretamente marcada como revogada). Ainda não há chat
com IA (isso é o M4). Detalhes de arquitetura, decisões e os relatórios
completos em `CLAUDE.md`.
