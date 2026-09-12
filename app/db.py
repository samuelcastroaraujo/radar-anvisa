"""Pool de conexões Postgres (asyncpg) com o banco Supabase.

Um único pool de módulo, criado no lifespan do FastAPI (ver `app/main.py`)
e fechado no shutdown. Nada aqui assume nenhuma tabela específica — o
schema vive em `supabase/migrations/`.
"""

import asyncpg

from app.config import get_settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        settings = get_settings()
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL não configurada (ver .env.example).")
        # statement_cache_size=0: em produção o DATABASE_URL é o pooler do
        # Supabase (Supavisor, modo transaction) — prepared statements do
        # asyncpg colidem entre conexões físicas diferentes por trás do
        # pooler (achado real fazendo o primeiro deploy: DuplicatePrepared
        # StatementError). Sem custo relevante aqui (não é um hot loop de
        # milhares de queries por segundo na mesma query).
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url, min_size=1, max_size=5, statement_cache_size=0
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
