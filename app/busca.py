"""Busca híbrida — seção 7 do briefing (só a parte de recuperação; o LLM
entra no M4): vetorial (cosine, top 30) + full-text em português (top 30),
fundidas por Reciprocal Rank Fusion, top 8 final. Filtros por tema, ano,
tipo_ato e status_vigencia.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg
import asyncpg.pool

from app.embeddings import gerar_embeddings

Conn = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

TOP_VETORIAL = 30
TOP_TEXTO = 30
TOP_FINAL = 8
RRF_K = 60


@dataclass
class FiltroBusca:
    tema: str | None = None
    ano: int | None = None
    tipo_ato: str | None = None
    status_vigencia: str | None = None
    norma_id: str | None = None
    """Escopa a busca a uma única norma — usado pelo chat (app/chat.py)
    quando a pergunta já identificou um ato específico, pra buscar os
    trechos mais relevantes dentro dele em vez de pegar todos os chunks."""


@dataclass
class ResultadoBusca:
    chunk_id: str
    norma_id: str
    tipo_ato: str
    numero: str
    ano: int
    status_vigencia: str
    ementa: str | None
    rotulo: str
    conteudo: str
    url_origem: str
    score: float


_CAMPOS_RESULTADO = (
    "chunk_id",
    "norma_id",
    "tipo_ato",
    "numero",
    "ano",
    "status_vigencia",
    "ementa",
    "rotulo",
    "conteudo",
    "url_origem",
)


def _vetor_para_sql(embedding: list[float]) -> str:
    return "[" + ",".join(str(x) for x in embedding) + "]"


def _where_filtro(filtro: FiltroBusca | None, proximo_indice: int) -> tuple[str, list[object]]:
    """Monta `and coluna = $N` para cada filtro presente, a partir de
    `proximo_indice` — os parâmetros posicionais anteriores (embedding/
    query de texto, limite) já ocupam $1 e $2."""
    condicoes = []
    valores: list[object] = []
    i = proximo_indice
    if filtro:
        if filtro.tema:
            condicoes.append(f"${i} = any(n.tema)")
            valores.append(filtro.tema)
            i += 1
        if filtro.ano:
            condicoes.append(f"n.ano = ${i}")
            valores.append(filtro.ano)
            i += 1
        if filtro.tipo_ato:
            condicoes.append(f"n.tipo_ato = ${i}")
            valores.append(filtro.tipo_ato)
            i += 1
        if filtro.status_vigencia:
            condicoes.append(f"n.status_vigencia = ${i}")
            valores.append(filtro.status_vigencia)
            i += 1
        if filtro.norma_id:
            condicoes.append(f"c.norma_id = ${i}")
            valores.append(filtro.norma_id)
            i += 1
    if not condicoes:
        return "", []
    return " and " + " and ".join(condicoes), valores


async def busca_vetorial(
    conn: Conn,
    query_embedding: list[float],
    filtro: FiltroBusca | None = None,
    top: int = TOP_VETORIAL,
) -> list[asyncpg.Record]:
    where, valores = _where_filtro(filtro, 3)
    sql = f"""
        select c.id::text as chunk_id, c.norma_id::text as norma_id, n.tipo_ato, n.numero, n.ano,
               n.status_vigencia, n.ementa, c.rotulo, c.conteudo, n.url_origem
        from chunk c join norma n on n.id = c.norma_id
        where true {where}
        order by (c.embedding::halfvec(3072)) <=> $1::halfvec(3072)
        limit $2
    """
    return await conn.fetch(sql, _vetor_para_sql(query_embedding), top, *valores)


async def busca_texto(
    conn: Conn, query: str, filtro: FiltroBusca | None = None, top: int = TOP_TEXTO
) -> list[asyncpg.Record]:
    where, valores = _where_filtro(filtro, 3)
    sql = f"""
        select c.id::text as chunk_id, c.norma_id::text as norma_id, n.tipo_ato, n.numero, n.ano,
               n.status_vigencia, n.ementa, c.rotulo, c.conteudo, n.url_origem
        from chunk c join norma n on n.id = c.norma_id
        where c.tsv @@ plainto_tsquery('portuguese', $1) {where}
        order by ts_rank(c.tsv, plainto_tsquery('portuguese', $1)) desc
        limit $2
    """
    return await conn.fetch(sql, query, top, *valores)


def fundir_rrf(
    resultados_vetorial: list[asyncpg.Record],
    resultados_texto: list[asyncpg.Record],
    k: int = RRF_K,
    top: int = TOP_FINAL,
) -> list[ResultadoBusca]:
    """Reciprocal Rank Fusion: score(chunk) = soma de 1/(k+posição) em cada
    lista onde ele aparece. Funde vetorial+texto sem precisar normalizar
    escalas incompatíveis (cosine distance vs. ts_rank)."""
    scores: dict[str, float] = {}
    dados: dict[str, asyncpg.Record] = {}
    for lista in (resultados_vetorial, resultados_texto):
        for posicao, row in enumerate(lista, start=1):
            cid = str(row["chunk_id"])
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + posicao)
            dados.setdefault(cid, row)

    ordenados = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top]
    return [
        ResultadoBusca(**{campo: dados[cid][campo] for campo in _CAMPOS_RESULTADO}, score=score)
        for cid, score in ordenados
    ]


async def buscar(
    conn: Conn, query: str, filtro: FiltroBusca | None = None, top: int = TOP_FINAL
) -> list[ResultadoBusca]:
    """Ponto de entrada da busca híbrida: gera o embedding da query, roda
    as duas buscas e funde por RRF."""
    query_embedding = (await gerar_embeddings([query]))[0]
    vetorial = await busca_vetorial(conn, query_embedding, filtro)
    texto = await busca_texto(conn, query, filtro)
    return fundir_rrf(vetorial, texto, top=top)
