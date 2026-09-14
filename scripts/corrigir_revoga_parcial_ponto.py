"""Remediação única — corrige a promoção indevida de `status_vigencia` para
'revogada' causada por um bug real de regex em `_revoga_so_um_dispositivo`
(app/ingest/anvisalegis.py, corrigido nesta mesma sessão a pedido do
usuário, que reportou a RDC 243/2018 aparecendo como revogada quando na
verdade está vigente).

O bug: `_RE_ROTULO_DISPOSITIVO_ANTES` não previa um ponto final entre o
rótulo do dispositivo e o "(Revogado pela X)" — "Parágrafo único.\xa0\n
(Revogado pela X)" é o padrão real do HTML do AnvisaLegis pra parágrafo
único (diferente de "Art. 12 -"/"III -", que não têm ponto). Sem bater no
regex, `_revoga_so_um_dispositivo` devolvia False e a revogação de UM
parágrafo virava `revoga` (total) em vez de `revoga_parcial` — e como
`_derivar_status_por_relacao` (scripts/carga_historica_310.py) promove pra
'revogada' qualquer norma ALVO de uma relação `tipo='revoga'`, isso marcou
normas genuinamente vigentes (como a própria RDC 243/2018, confirmada
'vigente' ao vivo no portal) como revogadas por engano.

Por que o bug persiste sozinho mesmo depois de corrigir o regex: o upsert
de `norma` (`app/ingest/persistencia.py::upsert_norma`) tem um "ratchet" de
propósito — depois que `status_vigencia='revogada'`, nenhuma carga
posterior reverte isso sozinha, mesmo com o parser já corrigido. E a
relação `revoga` errada, já gravada, sobrevive a uma nova carga porque o
`on conflict ... do nothing` não troca `tipo='revoga'` por
`tipo='revoga_parcial'` pra uma chave já existente — só corrige um script
de remediação dedicado, bypassando o ratchet, como este.

Estratégia: crawlear a listagem de vigentes ao vivo (parser já corrigido)
e, pra cada ato genuinamente vigente hoje:
  1. remover as relações `revoga`/`revoga_parcial` invertidas (fonte
     'texto-listagem-revogadas' — vem do processamento do próprio texto do
     ato, tanto normas vigentes quanto revogadas) que apontam pra ele —
     são exatamente as candidatas a estarem com a classificação antiga
     (errada);
  2. reinserir as relações corretas extraídas pelo parser já corrigido;
  3. se depois disso não sobrar nenhuma relação `tipo='revoga'` (de
     qualquer fonte) apontando pra esse ato, reverter `status_vigencia`
     pra 'vigente' (bypass do ratchet — mesma lógica de
     `scripts/corrigir_direcao_revoga.py`).

Uso:
    uv run python -m scripts.corrigir_revoga_parcial_ponto [--aplicar]

Sem `--aplicar`, roda em modo dry-run (só reporta o que faria).
"""

from __future__ import annotations

import argparse
import asyncio
import pickle
import sys
import time
from pathlib import Path

import asyncpg

from app.config import get_settings
from app.ingest.anvisalegis import AnvisaLegisClient, AtoParseado, carregar_vigentes
from app.ingest.persistencia import NormaIdCache, upsert_relacao

_CACHE = Path(__file__).parent / ".cache_vigentes.pkl"

TIMEOUT_QUERY = 15.0  # segundos — nunca deixar uma query pendurar pool inteiro


async def _corrigir_ato(
    conn: asyncpg.pool.PoolConnectionProxy,
    ato: AtoParseado,
    norma_id: str,
    status_atual: str,
    cache: NormaIdCache,
    aplicar: bool,
) -> tuple[int, bool]:
    """Retorna (nº de relações removidas, se o status foi/seria revertido)."""
    relacoes_invertidas = [r for r in ato.relacoes if r.invertida]

    if not aplicar:
        # dry-run: só conta quantas relações 'revoga' (não revoga_parcial)
        # existentes no banco, vindas dessa fonte, apontam pra este ato —
        # são as candidatas a estarem com a classificação antiga (errada).
        n = await conn.fetchval(
            "select count(*) from norma_relacao "
            "where destino_id = $1 and tipo = 'revoga' and fonte = 'texto-listagem-revogadas'",
            norma_id,
            timeout=TIMEOUT_QUERY,
        )
        restantes = (
            0
            if n
            else await conn.fetchval(
                "select count(*) from norma_relacao where destino_id = $1 and tipo = 'revoga'",
                norma_id,
                timeout=TIMEOUT_QUERY,
            )
        )
        reverteria = status_atual == "revogada" and n > 0 and restantes == 0
        return int(n or 0), reverteria

    resultado = await conn.execute(
        "delete from norma_relacao where destino_id = $1 "
        "and tipo in ('revoga', 'revoga_parcial') and fonte = 'texto-listagem-revogadas'",
        norma_id,
        timeout=TIMEOUT_QUERY,
    )
    removidas = int(resultado.split()[-1])

    for relacao in relacoes_invertidas:
        await upsert_relacao(conn, norma_id, ato, relacao, cache)

    restantes = await conn.fetchval(
        "select count(*) from norma_relacao where destino_id = $1 and tipo = 'revoga'",
        norma_id,
        timeout=TIMEOUT_QUERY,
    )
    revertido = False
    if status_atual == "revogada" and restantes == 0:
        await conn.execute(
            "update norma set status_vigencia = 'vigente' where id = $1",
            norma_id,
            timeout=TIMEOUT_QUERY,
        )
        revertido = True
    return removidas, revertido


async def main(aplicar: bool) -> None:
    settings = get_settings()
    if not settings.database_url:
        print("ERRO: DATABASE_URL não configurada (.env).", file=sys.stderr)
        raise SystemExit(1)

    pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=1,
        max_size=3,
        statement_cache_size=0,
        # Rodando duas vezes sem isso, o processo pendurou (sem erro nenhum)
        # numa query dentro de `upsert_relacao`/`get_or_create_norma_id`
        # (persistencia.py), que não recebem `timeout=` explícito — provável
        # engasgo momentâneo do túnel `railway run` até o pooler do Supabase.
        # `command_timeout` vira o padrão de toda query desse pool, sem
        # precisar anotar cada call site.
        command_timeout=15.0,
    )
    cliente = AnvisaLegisClient()
    cache: NormaIdCache = {}
    try:
        if _CACHE.exists():
            print(f"Usando cache local de vigentes ({_CACHE})...", flush=True)
            atos_vigentes = pickle.loads(_CACHE.read_bytes())
        else:
            print("Buscando lista de vigentes ao vivo no portal (parser corrigido)...", flush=True)
            atos_vigentes = await carregar_vigentes(cliente)
            _CACHE.write_bytes(pickle.dumps(atos_vigentes))
        print(f"{len(atos_vigentes)} normas vigentes confirmadas no portal agora.", flush=True)

        print("Resolvendo ids no banco (1 consulta em lote)...", flush=True)
        async with pool.acquire() as conn:
            linhas = await conn.fetch(
                "select tipo_ato, numero, ano, id, status_vigencia from norma",
                timeout=60.0,
            )
        por_chave = {(r["tipo_ato"], r["numero"], r["ano"]): r for r in linhas}
        print(f"{len(por_chave)} normas no banco carregadas em memória.", flush=True)

        total_removidas = 0
        total_revertidas = 0
        afetadas: list[str] = []
        falhas: list[str] = []
        for i, ato in enumerate(atos_vigentes, start=1):
            row = por_chave.get((ato.tipo_ato, ato.numero, ato.ano))
            if row is None:
                # ainda não crawleado localmente (raro; a carga histórica cobre
                # quase tudo) — nada a corrigir aqui, a próxima carga completa cuida.
                continue
            norma_id, status_atual = str(row["id"]), row["status_vigencia"]
            t0 = time.monotonic()
            try:
                # conexão nova por ato (não reaproveitada pro loop inteiro) —
                # se uma trava/erra, não contamina as próximas 1000+.
                async with pool.acquire() as conn:
                    removidas, revertido_ou_reverteria = await asyncio.wait_for(
                        _corrigir_ato(conn, ato, norma_id, status_atual, cache, aplicar),
                        timeout=45.0,
                    )
            except (TimeoutError, asyncpg.PostgresError, OSError) as exc:
                # um engasgo pontual do túnel não pode derrubar a corrida
                # inteira de novo — loga e segue pro próximo ato.
                falhas.append(f"{ato.tipo_ato} {ato.numero}/{ato.ano} ({exc!r})")
                print(
                    f"  [{i}/{len(atos_vigentes)}] {ato.tipo_ato} {ato.numero}/{ato.ano} "
                    f"FALHOU ({exc!r}) após {time.monotonic() - t0:.1f}s",
                    flush=True,
                )
                continue
            dt = time.monotonic() - t0
            if removidas > 0:
                total_removidas += removidas
                if revertido_ou_reverteria:
                    total_revertidas += 1
                    afetadas.append(f"{ato.tipo_ato} {ato.numero}/{ato.ano}")
                print(
                    f"  [{i}/{len(atos_vigentes)}] {ato.tipo_ato} {ato.numero}/{ato.ano} "
                    f"-> {removidas} relação(ões), revertida={revertido_ou_reverteria} "
                    f"({dt:.1f}s)",
                    flush=True,
                )
            elif dt > 3.0:
                print(
                    f"  [{i}/{len(atos_vigentes)}] {ato.tipo_ato} {ato.numero}/{ato.ano} "
                    f"demorou {dt:.1f}s sem nada a corrigir",
                    flush=True,
                )
            if i % 100 == 0:
                print(f"  ... {i}/{len(atos_vigentes)} processadas", flush=True)

        print(
            f"\n{total_removidas} relação(ões) 'revoga'/'revoga_parcial' com classificação "
            f"antiga {'removida(s)' if aplicar else 'a remover'}."
        )
        verbo = "revertida(s)" if aplicar else "a reverter"
        print(
            f"{total_revertidas} norma(s) marcada(s) 'revogada' no banco, mas confirmada(s) "
            f"'vigente' no portal e sem nenhuma outra relação 'revoga' pendente — {verbo}:"
        )
        for nome in afetadas[:40]:
            print(f"  {nome}")
        if len(afetadas) > 40:
            print(f"  ... e mais {len(afetadas) - 40}")

        if falhas:
            print(
                f"\n{len(falhas)} ato(s) pulado(s) por erro/timeout pontual "
                "(rode de novo pra tentar):"
            )
            for nome in falhas[:20]:
                print(f"  {nome}")

        if not aplicar:
            print("\nDRY-RUN — nada foi alterado. Rode com --aplicar pra corrigir de verdade.")
    finally:
        await cliente.aclose()
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true", help="aplica a correção de verdade")
    args = parser.parse_args()
    asyncio.run(main(args.aplicar))
