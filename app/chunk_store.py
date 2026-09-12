"""Persistência dos chunks (app/chunking.py) com seus embeddings
(app/embeddings.py) no Postgres — upsert idempotente por (norma_id, ordem).

`asyncpg` não sabe adaptar `list[float]` pro tipo `vector` do pgvector
sozinho — em vez de acrescentar a dependência `pgvector` só por isso,
formatamos o vetor como o literal de texto que o Postgres aceita
(`'[0.1,0.2,...]'::vector`), que é suportado nativamente.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg
import asyncpg.pool

Conn = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection


@dataclass
class ChunkParaGravar:
    ordem: int
    rotulo: str
    conteudo: str
    tokens: int
    embedding: list[float]


def _vetor_para_sql(embedding: list[float]) -> str:
    return "[" + ",".join(str(x) for x in embedding) + "]"


async def norma_ja_tem_chunks(conn: Conn, norma_id: str) -> bool:
    return bool(await conn.fetchval("select 1 from chunk where norma_id = $1 limit 1", norma_id))


async def upsert_chunks(conn: Conn, norma_id: str, chunks: list[ChunkParaGravar]) -> None:
    if not chunks:
        return
    # executemany faz 1 ida-e-volta de rede pro lote inteiro (protocolo
    # estendido do Postgres), em vez de 1 por chunk — com dezenas de
    # milhares de chunks na base inteira, inserir um de cada vez seria
    # o gargalo dominante do job (rede até o Supabase, não CPU).
    valores = [
        (norma_id, c.ordem, c.rotulo, c.conteudo, c.tokens, _vetor_para_sql(c.embedding))
        for c in chunks
    ]
    await conn.executemany(
        """
        insert into chunk (norma_id, ordem, rotulo, conteudo, tokens, embedding)
        values ($1, $2, $3, $4, $5, $6::vector)
        on conflict (norma_id, ordem) do update set
            rotulo    = excluded.rotulo,
            conteudo  = excluded.conteudo,
            tokens    = excluded.tokens,
            embedding = excluded.embedding
        """,
        valores,
    )


async def upsert_chunks_de_varias_normas(
    conn: Conn, chunks_por_norma: list[tuple[str, list[ChunkParaGravar]]]
) -> None:
    """Como `upsert_chunks`, mas para várias normas numa única ida ao banco
    — usado pelo job de carga em massa (scripts/chunking_embeddings_310.py),
    onde esperar uma rodada de rede por norma seria o gargalo dominante."""
    valores = [
        (norma_id, c.ordem, c.rotulo, c.conteudo, c.tokens, _vetor_para_sql(c.embedding))
        for norma_id, chunks in chunks_por_norma
        for c in chunks
    ]
    if not valores:
        return
    await conn.executemany(
        """
        insert into chunk (norma_id, ordem, rotulo, conteudo, tokens, embedding)
        values ($1, $2, $3, $4, $5, $6::vector)
        on conflict (norma_id, ordem) do update set
            rotulo    = excluded.rotulo,
            conteudo  = excluded.conteudo,
            tokens    = excluded.tokens,
            embedding = excluded.embedding
        """,
        valores,
    )
