-- M4 — métricas por resposta do chat (seção 10 do briefing):
-- "nº de chunks recuperados, se houve citação, latência, custo de tokens"

create table if not exists chat_metrica (
  id                  uuid primary key default gen_random_uuid(),
  pergunta            text not null,
  intencao            text not null,        -- norma_especifica | tematica | temporal | consulta_publica | nao_encontrado
  n_chunks_recuperados int not null default 0,
  teve_citacao        boolean not null default false,
  tokens_entrada      int,
  tokens_saida        int,
  custo_estimado      numeric,
  latencia_ms         int not null,
  criado_em           timestamptz default now()
);

create index if not exists chat_metrica_criado_em_idx on chat_metrica (criado_em desc);
