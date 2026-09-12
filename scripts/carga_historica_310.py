"""M2 — carga histórica do módulo 310 (normas regulatórias da ANVISA).

Uso:
    uv run python scripts/carga_historica_310.py [--so-vigentes | --so-revogadas]

Carrega vigentes e revogadas (ver app/ingest/anvisalegis.py e
research/FONTES.md), grava no Postgres via app/ingest/persistencia.py,
deriva status_vigencia por relação (regra da seção 5 do briefing) e imprime
um relatório de contagem comparado com os números do próprio portal.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime

import asyncpg

from app.config import get_settings
from app.ingest.anvisalegis import (
    COD_MODULO_310,
    AnvisaLegisClient,
    AtoParseado,
    carregar_revogadas,
    carregar_vigentes,
)
from app.ingest.persistencia import NormaIdCache, salvar_ato

# Números publicados pelo próprio portal (research/FONTES.md, Addendum M2) —
# usados só para comparação no relatório final, não como meta a forçar.
CONTAGENS_PORTAL = {
    "vigentes": 1138,
    "revogadas": 2438,
    "alteradoras": 464,
    "retificadoras": 291,
    "revogadoras": 22,
}


CONCORRENCIA_GRAVACAO = 8
"""As requisições HTTP ao AnvisaLegis são sequenciais e limitadas a 1/s (ver
RateLimiter em anvisalegis.py) — isso já respeita o crawling educado. Mas
gravar no Postgres é uma etapa totalmente separada, sem relação com o site;
não há motivo pra fazer isso 1 ato por vez também. O gargalo aqui é só a
latência de rede até o Supabase, então algumas gravações concorrentes (não
paralelas de verdade, é tudo I/O) aceleram bastante sem sobrecarregar nada."""


async def _processar_lote(
    pool: asyncpg.Pool, atos: list[AtoParseado], rotulo: str, cache: NormaIdCache
) -> None:
    semaforo = asyncio.Semaphore(CONCORRENCIA_GRAVACAO)
    concluidos = 0
    lock_contador = asyncio.Lock()

    async def _um(ato: AtoParseado) -> None:
        nonlocal concluidos
        # sem transação explícita de propósito: cada upsert já é atômico
        # sozinho (autocommit por statement). Segurar uma transação aberta
        # por ato inteiro (norma + N relações) fazia atos concorrentes que
        # citam a mesma norma-alvo travarem uns nos outros esperando o
        # commit alheio — foi isso que travou a primeira tentativa com
        # concorrência. Perder atomicidade "norma+relações" é aceitável
        # aqui: tudo é idempotente, rodar de novo só completa o que faltou.
        async with semaforo, pool.acquire() as conn:
            await salvar_ato(conn, ato, COD_MODULO_310, cache)
        async with lock_contador:
            concluidos += 1
            if concluidos % 50 == 0 or concluidos == len(atos):
                print(f"  [{rotulo}] {concluidos}/{len(atos)} processados...", flush=True)

    await asyncio.gather(*(_um(ato) for ato in atos))


async def _derivar_status_por_relacao(pool: asyncpg.Pool) -> int:
    """Regra da seção 5: relação tipo='revoga' apontando pra uma norma ->
    ela é revogada, mesmo que a listagem de origem não tivesse deixado claro."""
    async with pool.acquire() as conn:
        resultado = await conn.execute(
            """
            update norma n
            set status_vigencia = 'revogada'
            from norma_relacao r
            where r.destino_id = n.id
              and r.tipo = 'revoga'
              and n.status_vigencia <> 'revogada'
            """
        )
    # asyncpg retorna algo como "UPDATE 12"
    return int(resultado.split()[-1])


async def _relatorio(pool: asyncpg.Pool) -> str:
    async with pool.acquire() as conn:
        por_status = await conn.fetch(
            "select status_vigencia, count(*) as n from norma group by 1 order by 2 desc"
        )
        total_normas = await conn.fetchval("select count(*) from norma")
        total_relacoes = await conn.fetchval("select count(*) from norma_relacao")
        por_tipo_relacao = await conn.fetch(
            "select tipo, count(*) as n from norma_relacao group by 1 order by 2 desc"
        )
        # quantas normas distintas atuam como origem de cada tipo de relação
        # (isso é o que corresponde às contagens "alteradoras/retificadoras/
        # revogadoras" do portal, que são por ATO, não por relação individual)
        atores_por_tipo = await conn.fetch(
            """
            select tipo, count(distinct origem_id) as n
            from norma_relacao
            group by 1
            order by 2 desc
            """
        )
        desconhecidas = await conn.fetchval(
            "select count(*) from norma where status_vigencia = 'desconhecido'"
        )

    linhas = ["", "=" * 70, "RELATÓRIO DE CONTAGEM — M2 (módulo 310)", "=" * 70]
    linhas.append(f"\nTotal de normas na base: {total_normas}")
    linhas.append("\nPor status_vigencia:")
    for r in por_status:
        linhas.append(f"  {r['status_vigencia']:<20} {r['n']}")
    linhas.append(
        f"\n  (portal: vigentes={CONTAGENS_PORTAL['vigentes']}, "
        f"revogadas={CONTAGENS_PORTAL['revogadas']})"
    )
    linhas.append(
        f"\n  Normas com status 'desconhecido' (citadas em alguma relação mas "
        f"ainda não crawleadas): {desconhecidas}"
    )
    linhas.append(f"\nTotal de relações no grafo (norma_relacao): {total_relacoes}")
    linhas.append("\nPor tipo de relação (nº de relações):")
    for r in por_tipo_relacao:
        linhas.append(f"  {r['tipo']:<20} {r['n']}")
    linhas.append("\nPor tipo de relação (nº de ATOS distintos que fazem esse papel):")
    for r in atores_por_tipo:
        linhas.append(f"  {r['tipo']:<20} {r['n']}")
    linhas.append(
        f"\n  (portal, para comparação: alteradoras={CONTAGENS_PORTAL['alteradoras']}, "
        f"retificadoras={CONTAGENS_PORTAL['retificadoras']}, "
        f"revogadoras={CONTAGENS_PORTAL['revogadoras']} — não é 1:1 com "
        "'altera'/'retifica'/'revoga' porque nossa extração é heurística "
        "por regex sobre o texto, não a classificação oficial do portal;"
        " ver research/FONTES.md, Addendum M2)"
    )
    linhas.append("=" * 70)
    return "\n".join(linhas)


async def _job(pool: asyncpg.Pool, fonte: str) -> str:
    return str(
        await pool.fetchval(
            "insert into job_execucao (fonte, status) values ($1, 'em_andamento') returning id",
            fonte,
        )
    )


async def _job_concluir(
    pool: asyncpg.Pool, job_id: str, status: str, novos: int, atualizados: int, erro: str | None
) -> None:
    await pool.execute(
        """
        update job_execucao
        set terminado_em = now(), status = $2, itens_novos = $3,
            itens_atualizados = $4, erro = $5
        where id = $1
        """,
        job_id,
        status,
        novos,
        atualizados,
        erro,
    )


async def main(so_vigentes: bool, so_revogadas: bool) -> None:
    settings = get_settings()
    if not settings.database_url:
        print("ERRO: DATABASE_URL não configurada (.env).", file=sys.stderr)
        raise SystemExit(1)

    # statement_cache_size=0 — ver comentário em app/db.py: obrigatório
    # com o pooler do Supabase (Supavisor, modo transaction) em produção.
    pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=2,
        max_size=CONCORRENCIA_GRAVACAO,
        statement_cache_size=0,
    )
    cliente = AnvisaLegisClient()
    cache: NormaIdCache = {}
    inicio = time.monotonic()
    try:
        if not so_revogadas:
            job_id = await _job(pool, "anvisalegis_310_vigentes")
            print("Descobrindo anos de normas vigentes...", flush=True)
            antes = await pool.fetchval("select count(*) from norma")
            try:
                atos = await carregar_vigentes(cliente)
                print(f"{len(atos)} atos vigentes encontrados. Gravando...", flush=True)
                await _processar_lote(pool, atos, "vigentes", cache)
                depois = await pool.fetchval("select count(*) from norma")
                await _job_concluir(
                    pool, job_id, "ok", depois - antes, len(atos) - (depois - antes), None
                )
                print(f"Vigentes: {depois - antes} novos (de {len(atos)} processados).")
            except Exception as e:  # noqa: BLE001 — job precisa registrar qualquer falha
                await _job_concluir(pool, job_id, "erro", 0, 0, str(e))
                print(f"ERRO na carga de vigentes: {e}", file=sys.stderr)
                raise

        if not so_vigentes:
            job_id = await _job(pool, "anvisalegis_310_revogadas")
            print("Descobrindo anos de normas revogadas...", flush=True)
            antes = await pool.fetchval("select count(*) from norma")
            try:
                atos = await carregar_revogadas(cliente)
                print(f"{len(atos)} atos revogados encontrados. Gravando...", flush=True)
                await _processar_lote(pool, atos, "revogadas", cache)
                depois = await pool.fetchval("select count(*) from norma")
                await _job_concluir(
                    pool, job_id, "ok", depois - antes, len(atos) - (depois - antes), None
                )
                print(f"Revogadas: {depois - antes} novos (de {len(atos)} processados).")
            except Exception as e:  # noqa: BLE001
                await _job_concluir(pool, job_id, "erro", 0, 0, str(e))
                print(f"ERRO na carga de revogadas: {e}", file=sys.stderr)
                raise

        print("\nDerivando status_vigencia por relação (regra da seção 5)...")
        n_atualizadas = await _derivar_status_por_relacao(pool)
        print(f"{n_atualizadas} normas atualizadas para 'revogada' por relação explícita.")

        print(await _relatorio(pool))
        print(f"\nTempo total: {time.monotonic() - inicio:.0f}s — {datetime.now(UTC).isoformat()}")
    finally:
        await cliente.aclose()
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--so-vigentes", action="store_true")
    parser.add_argument("--so-revogadas", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.so_vigentes, args.so_revogadas))
