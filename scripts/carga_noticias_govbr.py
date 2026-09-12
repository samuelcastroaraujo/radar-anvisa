"""M5 — carga incremental das notícias do gov.br/anvisa.

Uso:
    uv run python -m scripts.carga_noticias_govbr [ano]

Sem argumento, usa o ano corrente (fuso BRT). Estratégia incremental: a
listagem vem sempre em ordem decrescente de publicação (mais nova primeiro,
confirmado nas amostras reais — ver research/FONTES.md, Addendum M5), então
paramos de paginar assim que encontramos uma notícia cujo hash de conteúdo
já está gravado — tudo daí pra trás já foi visto numa execução anterior.
Isso é o que faz a execução diária custar poucas requisições em vez de
repaginar o ano inteiro (até ~600 itens) toda vez.

Na primeira execução (banco vazio), isso naturalmente vira uma carga
completa do ano corrente — os anos anteriores exigem uma chamada manual à
parte (`listar_todas_do_ano`), fora do escopo do job diário.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

import asyncpg

from app.config import get_settings
from app.ingest.govbr_noticias import (
    GovBrClient,
    calcular_hash,
    eh_noticia_de_verdade,
    noticia_ja_existe_com_hash,
    upsert_noticia,
)

# console do Windows não é UTF-8 por padrão — sem isso, título com caractere
# como "‑" (hífen não separável) derruba o job com UnicodeEncodeError no
# meio da carga (achado real rodando esse script).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FUSO_BRT = timedelta(hours=-3)


async def carregar_incremental(pool: asyncpg.Pool, cliente: GovBrClient, ano: int) -> int:
    b_start = 0
    total_gravadas = 0
    async with pool.acquire() as conn:
        while True:
            total, pagina = await cliente.listar_pagina(ano, b_start=b_start)
            if not pagina:
                break
            parou_por_ja_visto = False
            for item in pagina:
                if not eh_noticia_de_verdade(item.url):
                    continue
                completa = await cliente.carregar_completa(item.url)
                hash_atual = calcular_hash(completa.conteudo)
                if await noticia_ja_existe_com_hash(conn, item.url, hash_atual):
                    parou_por_ja_visto = True
                    break
                await upsert_noticia(conn, completa)
                total_gravadas += 1
                print(f"  + {completa.titulo[:70]}", flush=True)
            if parou_por_ja_visto:
                break
            b_start += len(pagina)
            if b_start >= total:
                break
    return total_gravadas


async def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        print("ERRO: DATABASE_URL não configurada (.env).", file=sys.stderr)
        raise SystemExit(1)

    ano = int(sys.argv[1]) if len(sys.argv) > 1 else (datetime.now(UTC) + FUSO_BRT).year

    # statement_cache_size=0 — ver comentário em app/db.py: obrigatório
    # com o pooler do Supabase (Supavisor, modo transaction) em produção.
    pool = await asyncpg.create_pool(
        dsn=settings.database_url, min_size=1, max_size=3, statement_cache_size=0
    )
    cliente = GovBrClient()
    job_id = await pool.fetchval(
        "insert into job_execucao (fonte, status) values "
        "('govbr_noticias', 'em_andamento') returning id"
    )
    try:
        print(f"Carregando notícias do gov.br/anvisa — ano {ano}...", flush=True)
        total = await carregar_incremental(pool, cliente, ano)
        await pool.execute(
            "update job_execucao set terminado_em = now(), status = 'ok', "
            "itens_novos = $2 where id = $1",
            job_id,
            total,
        )
        print(f"\nConcluído: {total} notícias novas/atualizadas.")
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
