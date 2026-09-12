"""Remediação única — corrige a promoção indevida de status_vigencia
causada por um bug real de direção em `_extrair_relacoes`
(app/ingest/anvisalegis.py, corrigido nesta mesma sessão).

O bug: ao processar o texto de uma norma VIGENTE que tem dispositivos
(artigos/incisos) revogados individualmente ao longo do tempo (ex.: "Art.
12 - (Revogado pela RDC X)"), a extração de relações sempre gravava
`invertida=False` — ou seja, "esta norma revoga X" — quando na verdade é
o contrário ("X revogou um dispositivo desta norma"). Como o
pós-processamento do M2 (`_derivar_status_por_relacao`) promove pra
'revogada' qualquer norma que seja ALVO de uma relação `tipo='revoga'`,
isso marcou normas genuinamente vigentes (o alvo real da citação, "X" no
exemplo) como revogadas por engano.

Confirmado com uma checagem de sanidade real contra a base de produção:
142 dessas relações têm direção fisicamente impossível (a origem foi
publicada ANTES da norma que ela supostamente "revoga" — não existe
revogação retroativa no tempo).

Estratégia de correção: busca a lista de vigentes de verdade no portal
(com o parser já corrigido) e usa isso como fonte de verdade — qualquer
norma que está genuinamente na listagem de vigentes do AnvisaLegis hoje,
mas está marcada 'revogada' no banco, teve sua promoção revertida
diretamente (bypass do "ratchet" de `upsert_norma`, que nunca reverte
'revogada' sozinho — de propósito aqui, porque temos evidência
authoritative fresca de que está errado). As relações `tipo='revoga'`
com `fonte='linktexto'` que apontavam pra essas normas são removidas (são
exatamente a causa raiz).

Uso:
    uv run python -m scripts.corrigir_direcao_revoga [--aplicar]

Sem `--aplicar`, roda em modo dry-run (só reporta o que faria).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import asyncpg

from app.config import get_settings
from app.ingest.anvisalegis import AnvisaLegisClient, carregar_vigentes


async def main(aplicar: bool) -> None:
    settings = get_settings()
    if not settings.database_url:
        print("ERRO: DATABASE_URL não configurada (.env).", file=sys.stderr)
        raise SystemExit(1)

    pool = await asyncpg.create_pool(
        dsn=settings.database_url, min_size=1, max_size=3, statement_cache_size=0
    )
    cliente = AnvisaLegisClient()
    try:
        print("Buscando lista de vigentes de verdade no portal (parser corrigido)...", flush=True)
        atos_vigentes = await carregar_vigentes(cliente)
        chaves_vigentes = {(a.tipo_ato, a.numero, a.ano) for a in atos_vigentes}
        print(
            f"{len(chaves_vigentes)} normas confirmadas como vigentes no portal agora.", flush=True
        )

        async with pool.acquire() as conn:
            candidatos = await conn.fetch(
                "select id, tipo_ato, numero, ano from norma where status_vigencia = 'revogada'"
            )
            afetadas = [
                r for r in candidatos if (r["tipo_ato"], r["numero"], r["ano"]) in chaves_vigentes
            ]
            print(
                f"\n{len(afetadas)} normas marcadas 'revogada' no banco mas confirmadas "
                f"'vigente' no portal agora (candidatas à correção):",
                flush=True,
            )
            for r in afetadas[:30]:
                print(f"  {r['tipo_ato']} {r['numero']}/{r['ano']}")
            if len(afetadas) > 30:
                print(f"  ... e mais {len(afetadas) - 30}")

            if not aplicar:
                print("\nDRY-RUN — nada foi alterado. Rode com --aplicar pra corrigir de verdade.")
                return

            total_relacoes_removidas = 0
            for r in afetadas:
                resultado = await conn.execute(
                    "delete from norma_relacao where destino_id = $1 and tipo = 'revoga' "
                    "and fonte = 'linktexto'",
                    r["id"],
                )
                total_relacoes_removidas += int(resultado.split()[-1])
                await conn.execute(
                    "update norma set status_vigencia = 'vigente' where id = $1", r["id"]
                )
            print(
                f"\nCorrigido: {len(afetadas)} normas revertidas para 'vigente', "
                f"{total_relacoes_removidas} relações incorretas removidas."
            )
    finally:
        await cliente.aclose()
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true", help="aplica a correção de verdade")
    args = parser.parse_args()
    asyncio.run(main(args.aplicar))
