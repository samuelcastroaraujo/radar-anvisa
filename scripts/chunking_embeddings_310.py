"""M3 — chunking + embeddings das normas já carregadas no M2.

Uso:
    uv run python -m scripts.chunking_embeddings_310 [--limite N]

Para cada norma com `texto_integral` preenchido e que ainda não tem chunks,
divide o texto em unidades jurídicas (app/chunking.py), gera embeddings em
lote (app/embeddings.py) e grava tudo em `chunk` (app/chunk_store.py).
Idempotente: uma norma que já tem pelo menos 1 chunk é pulada — rodar de
novo só cobre o que ficou faltando (ex.: normas novas de uma carga futura).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import asyncpg

from app.chunk_store import ChunkParaGravar, upsert_chunks_de_varias_normas
from app.chunking import ChunkBruto, montar_chunks, texto_para_embeddar
from app.config import get_settings
from app.embeddings import gerar_embeddings

LOTE_NORMAS_POR_RODADA = 20
"""Quantas normas processar antes de fazer 1 chamada de embeddings — junta
os chunks de várias normas pra aproveitar melhor o lote de 100 da API,
mas sem acumular tudo de uma vez em memória."""


def _montar_chunks_de_norma(norma: asyncpg.Record) -> list[ChunkBruto] | None:
    if not norma["texto_integral"]:
        return None
    brutos = montar_chunks(norma["ementa"], norma["texto_integral"])
    return brutos or None


async def main(limite: int | None) -> None:
    settings = get_settings()
    if not settings.database_url:
        print("ERRO: DATABASE_URL não configurada (.env).", file=sys.stderr)
        raise SystemExit(1)
    if not settings.openrouter_api_key:
        print("ERRO: OPENROUTER_API_KEY não configurada (.env).", file=sys.stderr)
        raise SystemExit(1)

    pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=2, max_size=8)
    inicio = time.monotonic()

    job_id = await pool.fetchval(
        "insert into job_execucao (fonte, status) values ('chunking_embeddings_310', "
        "'em_andamento') returning id"
    )

    total_normas_processadas = 0
    total_chunks_gravados = 0
    try:
        normas = await pool.fetch(
            "select id, tipo_ato, numero, ano, ementa, texto_integral from norma "
            "where texto_integral is not null and texto_integral <> '' "
            "order by ano desc, numero"
        )
        if limite:
            normas = normas[:limite]
        ja_processadas = {
            str(r["norma_id"]) for r in await pool.fetch("select distinct norma_id from chunk")
        }
        normas = [n for n in normas if str(n["id"]) not in ja_processadas]
        print(
            f"{len(normas)} normas com texto_integral pendentes de chunking "
            f"({len(ja_processadas)} já processadas antes).",
            flush=True,
        )

        for inicio_lote in range(0, len(normas), LOTE_NORMAS_POR_RODADA):
            lote = normas[inicio_lote : inicio_lote + LOTE_NORMAS_POR_RODADA]

            pendentes: list[tuple[asyncpg.Record, list[ChunkBruto]]] = []
            for norma in lote:
                brutos = _montar_chunks_de_norma(norma)
                if brutos:
                    pendentes.append((norma, brutos))

            if not pendentes:
                continue

            # um único lote de textos pra embeddar, cobrindo todas as
            # normas pendentes desta rodada
            textos: list[str] = []
            indices: list[tuple[int, int]] = []  # (posição em `pendentes`, ordem do chunk)
            for pos, (norma, brutos) in enumerate(pendentes):
                for ordem, bruto in enumerate(brutos):
                    textos.append(
                        texto_para_embeddar(
                            norma["tipo_ato"],
                            norma["numero"],
                            norma["ano"],
                            norma["ementa"],
                            bruto.rotulo,
                            bruto.conteudo,
                        )
                    )
                    indices.append((pos, ordem))

            embeddings = await gerar_embeddings(textos)

            chunks_por_norma: list[list[ChunkParaGravar]] = [[] for _ in pendentes]
            for (pos, ordem), embedding in zip(indices, embeddings, strict=True):
                bruto = pendentes[pos][1][ordem]
                chunks_por_norma[pos].append(
                    ChunkParaGravar(
                        ordem=ordem,
                        rotulo=bruto.rotulo,
                        conteudo=bruto.conteudo,
                        tokens=bruto.tokens,
                        embedding=embedding,
                    )
                )

            lote_para_gravar = [
                (str(norma["id"]), chunks)
                for (norma, _brutos), chunks in zip(pendentes, chunks_por_norma, strict=True)
            ]
            async with pool.acquire() as conn:
                await upsert_chunks_de_varias_normas(conn, lote_para_gravar)
            total_normas_processadas += len(pendentes)
            total_chunks_gravados += sum(len(c) for c in chunks_por_norma)

            print(
                f"  {min(inicio_lote + LOTE_NORMAS_POR_RODADA, len(normas))}/{len(normas)} "
                f"normas consideradas — {total_normas_processadas} processadas, "
                f"{total_chunks_gravados} chunks gravados até agora.",
                flush=True,
            )

        await pool.execute(
            "update job_execucao set terminado_em = now(), status = 'ok', "
            "itens_novos = $2 where id = $1",
            job_id,
            total_normas_processadas,
        )
    except Exception as e:  # noqa: BLE001 — job precisa registrar qualquer falha
        await pool.execute(
            "update job_execucao set terminado_em = now(), status = 'erro', erro = $2 "
            "where id = $1",
            job_id,
            str(e),
        )
        raise
    finally:
        print(
            f"\nConcluído: {total_normas_processadas} normas, "
            f"{total_chunks_gravados} chunks. Tempo: {time.monotonic() - inicio:.0f}s"
        )
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limite", type=int, default=None, help="processar só as N primeiras normas"
    )
    args = parser.parse_args()
    asyncio.run(main(args.limite))
