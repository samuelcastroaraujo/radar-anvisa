-- RADAR ANVISA — schema inicial (M1)
-- Corresponde à seção 4 do briefing, com extensões mínimas e documentadas
-- (marcadas com "-- [M1]") necessárias para a idempotência total exigida
-- na seção 5. Rodar direto no SQL editor do Supabase, ou via:
--   psql "$DATABASE_URL" -f supabase/migrations/0001_schema.sql

create extension if not exists vector;
create extension if not exists pg_trgm;

-- Ato normativo (RDC, IN, RE, Portaria, Consulta Pública, Guia...)
create table if not exists norma (
  id                uuid primary key default gen_random_uuid(),
  tipo_ato          text not null,            -- RDC, IN, RE, PRT, CP, AP, GUIA
  numero            text not null,
  ano               int  not null,
  data_publicacao   date,
  data_vigencia     date,                     -- quando entra em vigor, se diferente
  ementa            text,
  orgao_emissor     text,
  tema              text[],                   -- alimentos, medicamentos, cosmeticos...
  status_vigencia   text not null default 'desconhecido',
  -- vigente | revogada | revogada_parcial | substituida | desconhecido
  modulo_origem     int,                      -- 310, 135, 630...
  url_origem        text not null,
  url_pdf           text,
  texto_integral    text,
  texto_compilado   text,                     -- consolidado com alterações, quando disponível
  hash_conteudo     text,                     -- sha256 do texto, p/ detectar mudança
  coletado_em       timestamptz default now(),
  atualizado_em     timestamptz default now(),
  unique (tipo_ato, numero, ano)
);

-- [M1] mantém atualizado_em correto em qualquer UPDATE, sem depender do
-- código da aplicação lembrar de setá-lo — evita "esquecer" em algum
-- caminho de reprocessamento e quebrar a detecção de mudança por data.
create or replace function set_atualizado_em() returns trigger as $$
begin
  new.atualizado_em = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_norma_atualizado_em on norma;
create trigger trg_norma_atualizado_em
  before update on norma
  for each row execute function set_atualizado_em();

-- Grafo de relações entre normas: o coração da confiabilidade
create table if not exists norma_relacao (
  id            uuid primary key default gen_random_uuid(),
  origem_id     uuid references norma(id) on delete cascade,   -- a norma que age
  destino_id    uuid references norma(id) on delete cascade,   -- a norma afetada
  tipo          text not null,   -- altera | revoga | revoga_parcial | retifica | regulamenta | substitui
  dispositivo   text,            -- "art. 5º, §2º"
  data_efeito   date,
  fonte         text
);

-- [M1] evita duplicar a mesma relação ao reprocessar o mesmo ato (coalesce
-- porque dispositivo pode ser nulo para relações "no ato inteiro" — e uma
-- constraint UNIQUE de tabela não aceita expressão, só coluna, daí o índice).
create unique index if not exists norma_relacao_unica_idx
  on norma_relacao (origem_id, destino_id, tipo, coalesce(dispositivo, ''));

-- Chunks para RAG
create table if not exists chunk (
  id           uuid primary key default gen_random_uuid(),
  norma_id     uuid references norma(id) on delete cascade,
  ordem        int not null,
  rotulo       text,        -- "Art. 12", "Anexo II", "Ementa"
  conteudo     text not null,
  tokens       int,
  embedding    vector(3072),
  tsv          tsvector generated always as (to_tsvector('portuguese', conteudo)) stored,
  -- [M1] permite upsert idempotente ao re-chunkear uma norma que mudou.
  unique (norma_id, ordem)
);
-- [M1] ivfflat trava em 2000 dimensões (limite do pgvector) e o embedding
-- é de 3072 (text-embedding-3-large) — confirmado ao aplicar a migration
-- de verdade neste projeto Supabase (erro real:
-- "column cannot have more than 2000 dimensions for ivfflat index").
-- Solução do próprio pgvector para >2000 dims: indexar via HNSW sobre um
-- cast para halfvec (metade da precisão só no índice; a coluna continua
-- vector(3072) em precisão cheia). pgvector 0.8.2 confirmado disponível
-- no projeto (suporta halfvec até 4000 dims).
create index if not exists chunk_embedding_idx
  on chunk using hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);
create index if not exists chunk_tsv_idx on chunk using gin (tsv);

-- Notícias, informes, alertas
create table if not exists noticia (
  id              uuid primary key default gen_random_uuid(),
  titulo          text not null,
  resumo          text,
  conteudo        text,
  categoria       text,       -- noticia | informe | alerta | consulta_publica
  tema            text[],
  data_publicacao timestamptz,
  url             text unique not null,
  hash_conteudo   text,
  embedding       vector(3072),
  coletado_em     timestamptz default now()
);

-- Publicações do DOU captadas via INLABS
create table if not exists dou_materia (
  id              uuid primary key default gen_random_uuid(),
  edicao          date not null,
  secao           text,
  orgao           text,
  titulo          text,
  texto           text,
  url             text,
  norma_id        uuid references norma(id),   -- vínculo quando identificado
  processado_em   timestamptz default now(),
  -- [M1] idMateria do INLABS é a chave natural confirmada em research/FONTES.md
  -- (attrs idMateria/id do <article> no XML) — sem isso não dá pra rodar o
  -- job duas vezes no mesmo dia sem duplicar.
  id_materia_inlabs text unique
);

-- Controle de ingestão
create table if not exists job_execucao (
  id           uuid primary key default gen_random_uuid(),
  fonte        text not null,
  iniciado_em  timestamptz default now(),
  terminado_em timestamptz,
  status       text,          -- ok | parcial | erro
  itens_novos  int default 0,
  itens_atualizados int default 0,
  erro         text
);

-- Assinaturas de alerta
create table if not exists alerta_regra (
  id          uuid primary key default gen_random_uuid(),
  nome        text not null,
  termos      text[],        -- palavras-chave
  temas       text[],
  canais      text[],        -- email | telegram | webhook
  destino     jsonb,
  ativo       boolean default true
);
