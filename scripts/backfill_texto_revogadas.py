"""Backfill do texto integral das normas revogadas — a pedido do usuário,
pra cobertura de busca mais completa (hoje só 1.129 das 4.579 normas têm
texto pesquisável; a maioria das ~2.476 revogadas sem texto só tem a
ementa). Ver CLAUDE.md pra decisão completa e os dois bugs reais achados
no caminho (parser de texto individual, direção de relação).

Uso:
    uv run python -m scripts.backfill_texto_revogadas [--limite N]

Idempotente: só processa normas com `status_vigencia='revogada'` e
`texto_integral` vazio — uma execução interrompida no meio retoma de onde
parou sem reprocessar o que já foi feito.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import time

import asyncpg

from app.config import get_settings
from app.ingest.anvisalegis import (
    AnvisaLegisClient,
    AtoParseado,
    extrair_texto_ato,
    parse_indices_revogados,
)
from app.ingest.persistencia import NormaIdCache, upsert_relacao

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main(limite: int | None) -> None:
    settings = get_settings()
    if not settings.database_url:
        print("ERRO: DATABASE_URL não configurada (.env).", file=sys.stderr)
        raise SystemExit(1)

    pool = await asyncpg.create_pool(
        dsn=settings.database_url, min_size=1, max_size=3, statement_cache_size=0
    )
    cliente = AnvisaLegisClient()
    inicio = time.monotonic()
    job_id = await pool.fetchval(
        "insert into job_execucao (fonte, status) values "
        "('backfill_texto_revogadas', 'em_andamento') returning id"
    )

    total_atualizadas = 0
    total_relacoes_novas = 0
    cache: NormaIdCache = {}
    try:
        # ordem "ano asc" de propósito: bate com a ordem em que
        # `anos_revogadas()` devolve os anos, então um `--limite` pequeno
        # (pra teste) processa rápido em vez de varrer décadas de anos que
        # não têm nenhum alvo antes de achar o primeiro.
        pendentes = await pool.fetch(
            "select id, tipo_ato, numero, ano from norma "
            "where status_vigencia = 'revogada' "
            "and (texto_integral is null or texto_integral = '') "
            "order by ano asc, numero"
        )
        pendentes_por_chave = {(r["tipo_ato"], r["numero"], r["ano"]): r["id"] for r in pendentes}
        print(f"{len(pendentes_por_chave)} normas revogadas pendentes de texto.", flush=True)
        if limite:
            chaves_alvo = set(list(pendentes_por_chave)[:limite])
        else:
            chaves_alvo = set(pendentes_por_chave)

        anos = await cliente.anos_revogadas()
        for ano in anos:
            if not chaves_alvo:
                break
            for pagina in await cliente.atos_revogados_do_ano(ano):
                for indice in parse_indices_revogados(pagina):
                    numero = str(int("".join(c for c in indice.numero_bruto if c.isdigit()) or "0"))
                    chave = (indice.tipo_ato, numero, int(indice.ano))
                    if chave not in chaves_alvo:
                        continue
                    chaves_alvo.discard(chave)
                    norma_id = pendentes_por_chave[chave]

                    pagina_individual = await cliente.texto_individual_do_ato(
                        indice.tipo_ato, indice.numero_bruto, indice.seq, indice.ano, indice.orgao
                    )
                    resultado = extrair_texto_ato(pagina_individual)
                    if resultado is None:
                        continue
                    texto, relacoes = resultado
                    hash_conteudo = hashlib.sha256(texto.encode("utf-8")).hexdigest()
                    # upsert_relacao recebe um AtoParseado mas não usa nenhum
                    # campo dele (parâmetro vestigial) — mínimo só pra bater o tipo.
                    ato_minimo = AtoParseado(
                        tipo_ato=indice.tipo_ato,
                        numero=numero,
                        ano=int(indice.ano),
                        data_publicacao=None,
                        ementa=None,
                        texto_integral="",
                        url_origem="",
                        status_vigencia="revogada",
                    )

                    async with pool.acquire() as conn:
                        await conn.execute(
                            "update norma set texto_integral = $1, hash_conteudo = $2 "
                            "where id = $3",
                            texto,
                            hash_conteudo,
                            norma_id,
                        )
                        for relacao in relacoes:
                            await upsert_relacao(conn, str(norma_id), ato_minimo, relacao, cache)
                            total_relacoes_novas += 1

                    total_atualizadas += 1
                    if total_atualizadas % 25 == 0:
                        print(
                            f"  {total_atualizadas}/{len(pendentes_por_chave)} — "
                            f"último: {indice.tipo_ato} {numero}/{indice.ano}",
                            flush=True,
                        )

        await pool.execute(
            "update job_execucao set terminado_em = now(), status = 'ok', "
            "itens_novos = $2 where id = $1",
            job_id,
            total_atualizadas,
        )
        print(
            f"\nConcluído: {total_atualizadas} normas com texto preenchido, "
            f"{total_relacoes_novas} relações novas. Tempo: {time.monotonic() - inicio:.0f}s"
        )
        if chaves_alvo:
            # normas de antes do 1º ano coberto por `anos_revogadas()`
            # (hoje, 1979) não aparecem na navegação por ano de revogadas —
            # só existem no banco como alvo de relação inferida, não como
            # item realmente crawleado do módulo 310. Não é bug, é limite
            # real da fonte — reportado, não escondido.
            print(
                f"{len(chaves_alvo)} não encontradas na navegação por ano (provavelmente pré-1979):"
            )
            for tipo, numero, ano in sorted(chaves_alvo, key=lambda c: c[2])[:20]:
                print(f"  {tipo} {numero}/{ano}")
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limite", type=int, default=None, help="processar só as N primeiras pendentes"
    )
    args = parser.parse_args()
    asyncio.run(main(args.limite))
