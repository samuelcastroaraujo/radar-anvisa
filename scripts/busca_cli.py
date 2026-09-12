"""M3 — CLI de busca híbrida, pra validar qualidade da recuperação antes de
plugar o LLM (M4).

Uso:
    uv run python -m scripts.busca_cli "rotulagem nutricional de suplemento alimentar"
    uv run python -m scripts.busca_cli "farmácia magistral" --tipo-ato RDC --status vigente
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import textwrap

import asyncpg

from app.busca import FiltroBusca, buscar
from app.config import get_settings

# console do Windows nem sempre usa UTF-8 por padrão (cp1252) — sem isso,
# imprimir acento ou qualquer caractere fora do cp1252 derruba o script.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_BADGE = {"vigente": "[VIGENTE]", "revogada": "[REVOGADA]", "desconhecido": "[?]"}


async def main(query: str, filtro: FiltroBusca, top: int) -> None:
    settings = get_settings()
    if not settings.database_url:
        print("ERRO: DATABASE_URL não configurada (.env).", file=sys.stderr)
        raise SystemExit(1)

    pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            resultados = await buscar(conn, query, filtro, top=top)

        if not resultados:
            print("Nada encontrado na base indexada para essa consulta.")
            return

        print(
            f'\nQuery: "{query}"' + (f" | filtro: {filtro}" if any(vars(filtro).values()) else "")
        )
        print("=" * 78)
        for i, r in enumerate(resultados, start=1):
            badge = _BADGE.get(r.status_vigencia, "[?]")
            print(f"\n[{i}] score={r.score:.4f}  {badge} {r.status_vigencia}")
            print(f"    {r.tipo_ato} {r.numero}/{r.ano} — {r.rotulo}")
            if r.ementa:
                print(f"    Ementa: {textwrap.shorten(r.ementa, width=140)}")
            print(f"    Fonte: {r.url_origem}")
            print(f"    Trecho: {textwrap.shorten(r.conteudo, width=200)}")
        print()
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("query", help="pergunta ou termo de busca")
    parser.add_argument("--tema")
    parser.add_argument("--ano", type=int)
    parser.add_argument("--tipo-ato", dest="tipo_ato")
    parser.add_argument(
        "--status",
        dest="status_vigencia",
        choices=["vigente", "revogada", "revogada_parcial", "substituida", "desconhecido"],
    )
    parser.add_argument("--top", type=int, default=8)
    args = parser.parse_args()

    filtro = FiltroBusca(
        tema=args.tema, ano=args.ano, tipo_ato=args.tipo_ato, status_vigencia=args.status_vigencia
    )
    asyncio.run(main(args.query, filtro, args.top))
