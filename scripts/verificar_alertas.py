"""M7 — verificação diária de alertas.

Uso:
    uv run python -m scripts.verificar_alertas

Roda depois das 4 fontes de ingestão no scheduler (`app/scheduler.py`) —
precisa que o dia já tenha sido carregado pra ter algo "recente" pra casar
contra as regras. Idempotente via `alerta_disparo` (mesmo alerta+item+canal
nunca notifica duas vezes) — rodar de novo no mesmo dia é seguro.
"""

from __future__ import annotations

import asyncio
import sys

import asyncpg

from app.alertas import rodar_verificacao
from app.config import get_settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        print("ERRO: DATABASE_URL não configurada (.env).", file=sys.stderr)
        raise SystemExit(1)

    pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=1, max_size=3)
    job_id = await pool.fetchval(
        "insert into job_execucao (fonte, status) values ('alertas', 'em_andamento') returning id"
    )
    try:
        async with pool.acquire() as conn:
            enviadas = await rodar_verificacao(conn)
        await pool.execute(
            "update job_execucao set terminado_em = now(), status = 'ok', "
            "itens_novos = $2 where id = $1",
            job_id,
            enviadas,
        )
        print(f"Concluído: {enviadas} notificações novas enviadas.")
    except Exception as e:  # noqa: BLE001
        await pool.execute(
            "update job_execucao set terminado_em = now(), status = 'erro', erro = $2 "
            "where id = $1",
            job_id,
            str(e),
        )
        raise
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
