# Guia de deploy e integrações — RADAR ANVISA

Passo a passo pra colocar o sistema completo no ar: backend no Railway,
frontend na Vercel, e as duas integrações opcionais de alerta (Telegram e
email via Resend). Cada seção diz claramente o que é obrigatório e o que é
opcional.

**Já está no ar:**
- Backend: https://radar-anvisa-production.up.railway.app
- Frontend: https://frontend-phi-ten-lhullf72f6.vercel.app
- Repositório: https://github.com/samuelcastroaraujo/radar-anvisa (privado)

Pendente: conectar o GitHub ao projeto da Vercel pra deploy automático a
cada push (só dá pra fazer pelo dashboard, ver seção 4), e as credenciais
opcionais de alerta (seção 3).

```
Vercel (frontend, Next.js)
   │  API_BASE_URL (server-only) — chamada servidor-a-servidor, sem CORS
   ▼
Railway (backend, FastAPI + scheduler 06:00 BRT)
   │
   ├── Supabase (Postgres + pgvector) ── obrigatório
   ├── OpenRouter (chat + embeddings) ── obrigatório
   ├── INLABS (login/senha, DOU)     ── obrigatório pra ingestão do DOU
   ├── Resend (email)                ── opcional, só p/ canal "email" de alerta
   └── Telegram Bot API              ── opcional, só p/ canal "telegram" de alerta
```

## 0. Pré-requisitos

- Conta no [GitHub](https://github.com) — Railway e Vercel fazem deploy a
  partir de um repositório.
- Conta no [Railway](https://railway.app) e na [Vercel](https://vercel.com)
  (dá pra logar com a conta do GitHub nas duas).
- O que você já tem (não mexe): projeto no Supabase com o schema aplicado,
  chave da OpenRouter, credenciais do INLABS — tudo já está no seu `.env`
  local. **Este guia não pede pra você recriar nada disso**, só usar os
  mesmos valores no Railway.

## 1. Subir o repositório pro GitHub

Ainda não há remote configurado neste repositório local. Crie um
repositório **privado** no GitHub (o projeto tem lógica de scraping e
estrutura de dados que não precisa ser pública) e:

```bash
git remote add origin https://github.com/SEU-USUARIO/radar-anvisa.git
git branch -M main
git push -u origin main
```

Railway e Vercel vão se conectar a esse repositório.

## 2. Backend no Railway

### 2.1. Criar o serviço

1. No Railway, **New Project → Deploy from GitHub repo** → selecione o
   repositório.
2. O Railway detecta o `Dockerfile` na raiz automaticamente (não precisa
   mexer em nada — `railway.json` já configura o healthcheck em `/health`).
3. Em **Settings → Networking**, gere um domínio público (**Generate
   Domain**). Você vai receber algo como
   `https://radar-anvisa-production.up.railway.app` — **anote essa URL**,
   ela é o `API_BASE_URL` do frontend (passo 4).

### 2.2. Variáveis de ambiente

Em **Variables**, adicione (mesmos valores do seu `.env` local, exceto
onde indicado):

| Variável | Obrigatória | Valor |
|---|---|---|
| `DATABASE_URL` | ✅ | a do Supabase — ver nota de connection pooling abaixo |
| `OPENROUTER_API_KEY` | ✅ | a mesma do `.env` local |
| `OPENROUTER_BASE_URL` | ✅ | `https://openrouter.ai/api/v1` |
| `INLABS_EMAIL` / `INLABS_PASSWORD` | ✅ | as mesmas do `.env` local |
| `ENVIRONMENT` | recomendado | `production` |
| `SCHEDULER_HABILITADO` | recomendado | `true` (é o padrão; só precisa setar se quiser desligar) |
| `RESEND_API_KEY` | opcional | ver seção 3 |
| `TELEGRAM_BOT_TOKEN` | opcional | ver seção 3 |

**Nota sobre `DATABASE_URL` — não é opcional no Railway.** A conexão
direta do Supabase (`db.<ref>.supabase.co:5432`) só resolve em **IPv6**
hoje em dia, e o Railway não tem saída IPv6 — o deploy sobe, mas o
`/health` trava em "Network is unreachable" ao tentar abrir o pool.
Confirmado tentando de verdade no primeiro deploy deste projeto. Use a
connection string do **pooler** (Supabase → seu projeto → **Connect** →
aba **"Transaction pooler"**), formato:

```
postgresql://postgres.<PROJECT-REF>:<SENHA>@aws-<0-ou-1>-<região>.pooler.supabase.com:6543/postgres
```

A região (`aws-0-us-west-2`, `aws-0-sa-east-1`, etc.) é a mesma que
aparece em **Project Settings → General → Region**.

**E, junto com o pooler, uma segunda mudança obrigatória no código**: o
Supavisor (pooler do Supabase) roda em modo "transaction", que não
suporta os *prepared statements* que o `asyncpg` usa por padrão — sem
ajuste, toda query dá `DuplicatePreparedStatementError` (também só
apareceu rodando de verdade contra o pooler, não em teste local com
conexão direta). Por isso todo `asyncpg.create_pool(...)` do projeto passa
`statement_cache_size=0` — já corrigido no código, nada que você precise
fazer, só documentado aqui pra explicar por que esse parâmetro existe.

### 2.3. Deploy

Qualquer `git push` na branch conectada (`main`) dispara um novo deploy
automaticamente. Acompanhe os logs em **Deployments** — o build roda
`uv sync --frozen --no-dev` e depois sobe o Uvicorn.

### 2.4. Validar

```bash
curl https://SEU-APP.up.railway.app/health
# {"status":"ok"}

curl https://SEU-APP.up.railway.app/health/fontes
# lista o histórico de job_execucao — vazio até o scheduler rodar às 06:00
# BRT, ou até você disparar manualmente (próximo bloco)
```

Pra não esperar até 06:00 do dia seguinte pra ver o scheduler funcionar,
dispare um dos jobs manualmente **de dentro do container** (Railway →
seu serviço → aba **Shell**, ou `railway run` localmente com o CLI):

```bash
python -m scripts.verificar_alertas
```

## 3. Alertas — Telegram e email (opcionais)

O canal **webhook** já funciona sem nenhuma credencial nova (só precisa de
uma regra com `destino.url`) — foi o único canal testado com envio real
durante o desenvolvimento (ver `CLAUDE.md`, M7). Telegram e email exigem
uma credencial que você ainda não tem.

### 3.1. Telegram

1. No Telegram, procure **@BotFather** e envie `/newbot`.
2. Escolha um nome de exibição e um username terminado em `bot` (ex.:
   `radar_anvisa_bot`).
3. O BotFather devolve um token no formato `123456789:AAH...` — isso é o
   `TELEGRAM_BOT_TOKEN`. Cole no Railway (seção 2.2).
4. Pra descobrir o `chat_id` de destino (o seu, ou de um grupo):
   - Abra uma conversa com o bot que você criou e mande qualquer mensagem
     (ex.: "oi") — ou, pra um grupo, adicione o bot ao grupo e mande uma
     mensagem lá.
   - Chame (no navegador ou `curl`):
     ```
     https://api.telegram.org/bot<SEU_TOKEN>/getUpdates
     ```
   - Procure `"chat":{"id": ...}` na resposta — esse número é o `chat_id`.
5. Crie a regra de alerta apontando pra esse `chat_id`:
   ```bash
   curl -X POST https://SEU-APP.up.railway.app/alertas \
     -H "Content-Type: application/json" \
     -d '{
       "nome": "Novidades sobre rotulagem",
       "termos": ["rotulagem"],
       "canais": ["telegram"],
       "destino": {"chat_id": "SEU_CHAT_ID"}
     }'
   ```

### 3.2. Email (Resend)

1. Crie conta em [resend.com](https://resend.com).
2. **API Keys → Create API Key** → copie o valor (só aparece uma vez) —
   isso é o `RESEND_API_KEY`. Cole no Railway.
3. **Limitação do modo sandbox** (sem verificar domínio próprio): o
   remetente padrão do projeto é `onboarding@resend.dev`, e a Resend só
   permite mandar pra o **e-mail da sua própria conta** nesse modo — é
   suficiente pra alertar você mesmo, mas não pra mandar pra terceiros.
   Pra liberar qualquer destinatário, verifique um domínio seu em
   **Domains** (adiciona 2-3 registros DNS TXT/CNAME) e passe
   `"from": "RADAR ANVISA <alertas@seudominio.com>"` no `destino` da regra.
4. Criar a regra:
   ```bash
   curl -X POST https://SEU-APP.up.railway.app/alertas \
     -H "Content-Type: application/json" \
     -d '{
       "nome": "Novidades sobre rotulagem",
       "termos": ["rotulagem"],
       "canais": ["email"],
       "destino": {"email": "voce@seuemail.com"}
     }'
   ```

Depois de criar qualquer regra, rode `python -m scripts.verificar_alertas`
manualmente (seção 2.4) pra não esperar até o próximo dia — se casar com
algo recente, a notificação chega na hora.

## 4. Frontend na Vercel

1. **Add New → Project** → importe o mesmo repositório do GitHub.
2. Em **Root Directory**, clique em Edit e selecione **`frontend`** — é
   essencial, o Next.js não está na raiz do repo.
3. A Vercel detecta Next.js automaticamente (build command, output, etc.
   não precisam de ajuste).
4. Em **Environment Variables**, adicione:

   | Nome | Valor |
   |---|---|
   | `API_BASE_URL` | a URL do Railway do passo 2.1 (ex.: `https://radar-anvisa-production.up.railway.app`) |

   **Sem `NEXT_PUBLIC_` de propósito** — essa URL só é lida no servidor do
   Next.js (Server Components + `app/api/chat/route.ts`), nunca no
   browser, por isso não precisa (e não deve) ser uma env var pública. Ver
   `CLAUDE.md`, M6, pra decisão completa.
5. **Deploy**. A Vercel te dá uma URL tipo
   `https://radar-anvisa.vercel.app`.

### Validar

Abra a URL da Vercel: a página inicial é o chat. Teste com uma pergunta
real (ex.: "A RDC 27/2010 ainda está vigente?") e confira `/timeline` e
`/consultas-publicas` no menu.

## 5. Checklist final

- [ ] `GET https://SEU-APP.up.railway.app/health` → `{"status":"ok"}`
- [ ] `GET https://SEU-APP.up.railway.app/health/fontes` → mostra as 5
      fontes depois da primeira execução do scheduler (ou de um disparo
      manual)
- [ ] Chat na Vercel responde com citação de norma e status de vigência
- [ ] `/timeline` e `/consultas-publicas` na Vercel mostram dado real
- [ ] (opcional) uma regra de alerta por webhook/telegram/email dispara de
      verdade

## 6. Problemas comuns

- **Railway: build falha em `uv sync`** — confira se `uv.lock` está
  commitado e sincronizado com `pyproject.toml` (`uv lock --check` local).
- **Scheduler não aparece em `/health/fontes` no dia seguinte** — o cron é
  `06:00 America/Sao_Paulo`; se o serviço reiniciou depois desse horário,
  só roda no próximo dia (não há "catch-up" automático do agendamento em
  si — mas os scripts de INLABS/notícias são idempotentes/incrementais,
  então rodar manualmente uma vez cobre o que ficou pra trás).
- **CORS** — não precisa configurar nada no backend; o frontend nunca fala
  com o Railway direto do browser (ver arquitetura no topo deste arquivo).
- **`Network is unreachable` no `/health`** — `DATABASE_URL` está com a
  conexão direta (IPv6-only); troque pro pooler (seção 2.2).
- **`DuplicatePreparedStatementError` em qualquer endpoint que usa o
  banco** — só acontece se algum `asyncpg.create_pool` novo no código
  esquecer `statement_cache_size=0` (obrigatório com o pooler).
- **Canal email só entrega pra você mesmo** — modo sandbox da Resend (ver
  seção 3.2); verifique um domínio pra liberar outros destinatários.
