"""M5 — carga das consultas públicas ativas (módulo 630).

Uso:
    uv run python -m scripts.carga_participacao_social

Roda o dia inteiro de novo é barato aqui (só ~19-30 itens, ao contrário do
módulo 310) — não precisa de lógica incremental separada, o
`on conflict (url)` já cobre a idempotência.
"""

from __future__ import annotations

import asyncio
import sys

import asyncpg

from app.config import get_settings
from app.ingest.anvisalegis import AnvisaLegisClient
from app.ingest.participacao_social import carregar_cps_ativas, upsert_consulta_publica

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        print("ERRO: DATABASE_URL não configurada (.env).", file=sys.stderr)
        raise SystemExit(1)

    pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=1, max_size=3)
    cliente = AnvisaLegisClient()
    job_id = await pool.fetchval(
        "insert into job_execucao (fonte, status) values "
        "('anvisalegis_630_participacao_social', 'em_andamento') returning id"
    )
    try:
        print("Descobrindo consultas públicas ativas...", flush=True)
        cps = await carregar_cps_ativas(cliente)
        print(f"{len(cps)} consultas públicas encontradas. Gravando...", flush=True)
        async with pool.acquire() as conn:
            for cp in cps:
                await upsert_consulta_publica(conn, cp)
                print(
                    f"  CP {cp.numero}/{cp.ano} — prazo até {cp.prazo_fim} — {cp.titulo[:60]}",
                    flush=True,
                )
        await pool.execute(
            "update job_execucao set terminado_em = now(), status = 'ok', "
            "itens_novos = $2 where id = $1",
            job_id,
            len(cps),
        )
        print(f"\nConcluído: {len(cps)} consultas públicas gravadas.")
    except Exception as e:  # noqa: BLE001
        await pool.execute(
            "update job_execucao set terminado_em = now(), status = 'erro', erro = $2 "
            "where id = $1",
            job_id,
            str(e),
        )
        raise
    finally:
        await cliente.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
