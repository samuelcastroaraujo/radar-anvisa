"""Consultas de leitura pra `/timeline`, `/consultas-publicas` e o `/health`
completo (seção 9 do briefing) — nada aqui grava dado, só lê o que os
ingestores de `app/ingest/` já gravaram.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import asyncpg
import asyncpg.pool

Conn = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection


@dataclass
class ItemTimeline:
    tipo: str  # norma | noticia | consulta_publica | dou
    titulo: str
    data: datetime
    url: str | None
    status_vigencia: str | None = None


async def buscar_timeline(conn: Conn, dias: int, limite: int = 100) -> list[ItemTimeline]:
    """União de norma/noticia/dou_materia publicados nos últimos `dias`
    dias, mais recente primeiro. `noticia` já cobre tanto notícia quanto
    consulta pública (categoria), então o `tipo` no resultado vem da coluna,
    não de qual tabela originou a linha."""
    linhas = await conn.fetch(
        """
        (
            select 'norma' as tipo,
                   tipo_ato || ' ' || numero || '/' || ano as titulo,
                   data_publicacao::timestamptz as data,
                   url_origem as url,
                   status_vigencia
            from norma
            where data_publicacao >= now() - make_interval(days => $1)
        )
        union all
        (
            select coalesce(categoria, 'noticia') as tipo,
                   titulo,
                   data_publicacao as data,
                   url,
                   null as status_vigencia
            from noticia
            where data_publicacao >= now() - make_interval(days => $1)
        )
        union all
        (
            select 'dou' as tipo,
                   titulo,
                   edicao::timestamptz as data,
                   url,
                   null as status_vigencia
            from dou_materia
            where edicao >= (now() - make_interval(days => $1))::date
        )
        order by data desc
        limit $2
        """,
        dias,
        limite,
    )
    return [
        ItemTimeline(
            tipo=linha["tipo"],
            titulo=linha["titulo"],
            data=linha["data"],
            url=linha["url"],
            status_vigencia=linha["status_vigencia"],
        )
        for linha in linhas
    ]


@dataclass
class ConsultaPublicaResumo:
    titulo: str
    assunto: str | None
    data_dou: datetime | None
    prazo_inicio: date | None
    prazo_fim: date | None
    url: str
    aberta: bool


async def buscar_consultas_publicas(
    conn: Conn, apenas_abertas: bool = True
) -> list[ConsultaPublicaResumo]:
    condicao = "and (prazo_fim is null or prazo_fim >= current_date)" if apenas_abertas else ""
    linhas = await conn.fetch(
        f"""
        select titulo, resumo as assunto, data_publicacao as data_dou,
               prazo_inicio, prazo_fim, url,
               (prazo_fim is null or prazo_fim >= current_date) as aberta
        from noticia
        where categoria = 'consulta_publica'
        {condicao}
        order by prazo_fim nulls last
        """  # noqa: S608 (condicao é constante fixa, não input do usuário)
    )
    return [
        ConsultaPublicaResumo(
            titulo=linha["titulo"],
            assunto=linha["assunto"],
            data_dou=linha["data_dou"],
            prazo_inicio=linha["prazo_inicio"],
            prazo_fim=linha["prazo_fim"],
            url=linha["url"],
            aberta=linha["aberta"],
        )
        for linha in linhas
    ]


@dataclass
class StatusFonte:
    fonte: str
    status: str | None
    iniciado_em: datetime
    terminado_em: datetime | None
    itens_novos: int
    erro: str | None


async def buscar_status_fontes(conn: Conn) -> list[StatusFonte]:
    """Última execução registrada de cada fonte em `job_execucao` — é isso
    que dá o `/health` completo (seção 9): não basta a API estar de pé, as
    fontes de ingestão precisam estar rodando de verdade."""
    linhas = await conn.fetch(
        """
        select distinct on (fonte) fonte, status, iniciado_em, terminado_em,
               itens_novos, erro
        from job_execucao
        order by fonte, iniciado_em desc
        """
    )
    return [
        StatusFonte(
            fonte=linha["fonte"],
            status=linha["status"],
            iniciado_em=linha["iniciado_em"],
            terminado_em=linha["terminado_em"],
            itens_novos=linha["itens_novos"],
            erro=linha["erro"],
        )
        for linha in linhas
    ]
