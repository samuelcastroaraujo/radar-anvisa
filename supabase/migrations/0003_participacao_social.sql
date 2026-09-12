-- M5 — participação social (consultas públicas) reaproveita a tabela
-- `noticia` (categoria='consulta_publica', como o schema original já
-- previa), só falta guardar o prazo de contribuição — ver
-- research/FONTES.md, Addendum M5, sobre `#prazoContribuicao`.

alter table noticia add column if not exists prazo_inicio date;
alter table noticia add column if not exists prazo_fim date;

create index if not exists noticia_prazo_fim_idx on noticia (prazo_fim)
  where categoria = 'consulta_publica';
