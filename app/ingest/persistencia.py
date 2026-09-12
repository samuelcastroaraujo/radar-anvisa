"""Persistência dos DTOs de `anvisalegis.py` no Postgres (upsert idempotente).

Chaves naturais (tipo_ato, numero, ano) — não IDs — são o que amarra tudo
aqui, porque é isso que o AnvisaLegis nos dá; o `id` (uuid) só existe depois
que a norma é gravada pela primeira vez.
"""

from __future__ import annotations

import hashlib

import asyncpg
import asyncpg.pool

from app.ingest.anvisalegis import AtoParseado, RelacaoBruta

# pool.acquire() entrega um PoolConnectionProxy, não uma Connection crua —
# as duas têm a mesma API relevante aqui (fetchrow/execute).
Conn = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

# (tipo_ato, numero, ano) -> id. Compartilhado entre atos no mesmo run pelo
# chamador (ver scripts/carga_historica_310.py) para não resolver de novo
# uma norma citada por dezenas de outras (ex.: uma RDC muito referenciada).
NormaIdCache = dict[tuple[str, str, int], str]


async def upsert_norma(conn: Conn, ato: AtoParseado, modulo_origem: int) -> str:
    conteudo_para_hash = ato.texto_integral or ato.ementa or ""
    hash_conteudo = (
        hashlib.sha256(conteudo_para_hash.encode("utf-8")).hexdigest()
        if conteudo_para_hash
        else None
    )
    row = await conn.fetchrow(
        """
        insert into norma (
            tipo_ato, numero, ano, data_publicacao, ementa, orgao_emissor,
            status_vigencia, modulo_origem, url_origem, texto_integral, hash_conteudo
        )
        values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        on conflict (tipo_ato, numero, ano) do update set
            data_publicacao = coalesce(excluded.data_publicacao, norma.data_publicacao),
            ementa          = coalesce(excluded.ementa, norma.ementa),
            orgao_emissor   = coalesce(excluded.orgao_emissor, norma.orgao_emissor),
            url_origem      = excluded.url_origem,
            texto_integral  = case when excluded.texto_integral <> ''
                                    then excluded.texto_integral else norma.texto_integral end,
            hash_conteudo   = coalesce(excluded.hash_conteudo, norma.hash_conteudo),
            -- "revogada" é evidência forte (veio da própria listagem de
            -- revogadas do portal) — nunca deixamos outra carga reverter isso.
            status_vigencia = case when norma.status_vigencia = 'revogada'
                                    then 'revogada' else excluded.status_vigencia end
        returning id
        """,
        ato.tipo_ato,
        ato.numero,
        ato.ano,
        ato.data_publicacao,
        ato.ementa,
        ato.orgao_emissor,
        ato.status_vigencia,
        modulo_origem,
        ato.url_origem,
        ato.texto_integral,
        hash_conteudo,
    )
    assert row is not None  # "insert ... returning" sempre retorna 1 linha
    return str(row["id"])


async def get_or_create_norma_id(
    conn: Conn, tipo_ato: str, numero: str, ano: int, cache: NormaIdCache | None = None
) -> str:
    """Resolve o id de uma norma citada como destino de uma relação. Se ela
    ainda não foi carregada (ex.: referenciada por um ato vigente mas ela
    própria só existe numa fonte que ainda não crawleamos), cria um registro
    mínimo com status 'desconhecido' — melhor que perder a relação."""
    chave = (tipo_ato, numero, ano)
    if cache is not None and chave in cache:
        return cache[chave]
    row = await conn.fetchrow(
        "select id from norma where tipo_ato=$1 and numero=$2 and ano=$3",
        tipo_ato,
        numero,
        ano,
    )
    if row:
        id_ = str(row["id"])
    else:
        row = await conn.fetchrow(
            """
            insert into norma (tipo_ato, numero, ano, status_vigencia, url_origem)
            values ($1, $2, $3, 'desconhecido', $4)
            on conflict (tipo_ato, numero, ano) do update set tipo_ato = excluded.tipo_ato
            returning id
            """,
            tipo_ato,
            numero,
            ano,
            f"https://anvisalegis.datalegis.net/ (referenciada, não crawleada: "
            f"{tipo_ato} {numero}/{ano})",
        )
        assert row is not None
        id_ = str(row["id"])
    if cache is not None:
        cache[chave] = id_
    return id_


async def upsert_relacao(
    conn: Conn,
    ato_id: str,
    ato: AtoParseado,
    relacao: RelacaoBruta,
    cache: NormaIdCache | None = None,
) -> None:
    destino_id_da_referencia = await get_or_create_norma_id(
        conn, relacao.destino_tipo_ato, relacao.destino_numero, relacao.destino_ano, cache
    )
    if relacao.invertida:
        origem_id, destino_id, fonte = destino_id_da_referencia, ato_id, "texto-listagem-revogadas"
    else:
        origem_id, destino_id, fonte = ato_id, destino_id_da_referencia, "linktexto"
    if origem_id == destino_id:
        return  # auto-referência (ex.: citação da própria ementa) — ignora
    await conn.execute(
        """
        insert into norma_relacao (origem_id, destino_id, tipo, dispositivo, fonte)
        values ($1,$2,$3,$4,$5)
        on conflict (origem_id, destino_id, tipo, coalesce(dispositivo, '')) do nothing
        """,
        origem_id,
        destino_id,
        relacao.tipo,
        relacao.dispositivo,
        fonte,
    )


def _relacoes_unicas(relacoes: list[RelacaoBruta]) -> list[RelacaoBruta]:
    """Um mesmo LinkTexto pode aparecer dezenas de vezes no texto de um ato
    (ex.: uma tabela de anexo que cita a mesma norma em cada linha) — sem
    deduplicar aqui, cada repetição vira uma ida ao banco à toa."""
    vistos: set[tuple[str, str, str, int, str | None, bool]] = set()
    unicas = []
    for r in relacoes:
        chave = (
            r.tipo,
            r.destino_tipo_ato,
            r.destino_numero,
            r.destino_ano,
            r.dispositivo,
            r.invertida,
        )
        if chave not in vistos:
            vistos.add(chave)
            unicas.append(r)
    return unicas


async def salvar_ato(
    conn: Conn, ato: AtoParseado, modulo_origem: int, cache: NormaIdCache | None = None
) -> str:
    ato_id = await upsert_norma(conn, ato, modulo_origem)
    if cache is not None:
        cache[(ato.tipo_ato, ato.numero, ato.ano)] = ato_id
    for relacao in _relacoes_unicas(ato.relacoes):
        await upsert_relacao(conn, ato_id, ato, relacao, cache)
    return ato_id
