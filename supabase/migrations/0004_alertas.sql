-- M7 — disparo de alertas (seção "Alertas" do briefing).
--
-- `alerta_regra` já existia desde o schema original (0001); o que faltava
-- era como não notificar a mesma regra pro mesmo item mais de uma vez —
-- por isso essa tabela nova, no mesmo espírito de idempotência total da
-- seção 5 (ver `-- [M1]` em 0001_schema.sql pros outros exemplos).
create table if not exists alerta_disparo (
  id          uuid primary key default gen_random_uuid(),
  alerta_id   uuid not null references alerta_regra(id) on delete cascade,
  item_tipo   text not null,   -- norma | noticia | consulta_publica | dou
  item_chave  text not null,   -- id (uuid) da norma/noticia/dou_materia
  canal       text not null,   -- email | telegram | webhook
  status      text not null,   -- ok | erro
  erro        text,
  criado_em   timestamptz default now(),
  -- por canal: se um canal falha e os outros funcionam, só o que falhou
  -- pode ser reprocessado depois sem reenviar pros que já deram certo.
  unique (alerta_id, item_tipo, item_chave, canal)
);

create index if not exists alerta_disparo_alerta_idx on alerta_disparo (alerta_id);
