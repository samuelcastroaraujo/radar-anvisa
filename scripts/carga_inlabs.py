"""M5 — carga diária do DOU via INLABS, filtrado para matérias da ANVISA.

Uso:
    uv run python -m scripts.carga_inlabs [AAAA-MM-DD]

Sem argumento, usa o dia anterior (fuso BRT) — é o que já confirmamos no M0
que existe no servidor quando esse job roda de manhã (a pasta do dia
corrente ainda não existe àquela hora). Passar uma data explícita serve
para reprocessar um dia específico.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

import asyncpg

from app.config import get_settings
from app.ingest.inlabs import InlabsClient, upsert_materia_dou

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FUSO_BRT = timedelta(hours=-3)


async def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        print("ERRO: DATABASE_URL não configurada (.env).", file=sys.stderr)
        raise SystemExit(1)

    if len(sys.argv) > 1:
        dia = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    else:
        dia = (datetime.now(UTC) + FUSO_BRT).date() - timedelta(days=1)

    pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=1, max_size=3)
    cliente = InlabsClient()
    job_id = await pool.fetchval(
        "insert into job_execucao (fonte, status) values ('inlabs_dou', 'em_andamento') "
        "returning id"
    )
    try:
        print(f"Baixando DOU de {dia.isoformat()} (seções ANVISA)...", flush=True)
        materias = await cliente.baixar_dia_anvisa(dia)
        print(f"{len(materias)} matérias da ANVISA encontradas. Gravando...", flush=True)
        async with pool.acquire() as conn:
            for materia in materias:
                await upsert_materia_dou(conn, materia)
                print(f"  [{materia.secao}] {materia.titulo[:70]}", flush=True)
        await pool.execute(
            "update job_execucao set terminado_em = now(), status = 'ok', "
            "itens_novos = $2 where id = $1",
            job_id,
            len(materias),
        )
        print(f"\nConcluído: {len(materias)} matérias gravadas.")
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
